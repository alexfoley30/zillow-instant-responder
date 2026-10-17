"""handle_new_inquiry: the instant first reply.

This is the single busiest renter-facing path in the service and it had no
test of its own. It does all four risky things at once - it gates on the
blocked-address list, writes the thread doc, picks which template a stranger
receives, and decides whether Alex gets paged. The expensive failure is
specific and has happened before (alondra, 2026-08-04): a leased home that
answers with a tour invite.

find_existing_showing runs for real here against a stubbed calendar feed, so
the consolidate-first branch is exercised through the same matching code
production uses.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import facts  # noqa: E402
import ledger  # noqa: E402
import responder  # noqa: E402
import templates as T  # noqa: E402

RELAY = "reply+9f1c@convo.zillow.com"
BLOCKED = ["1641 E Coronado Rd", "1294 E Apricot Ln", "3309 E San Remo Ave"]

APRICOT_EVENT = {
    "id": "ev_apricot",
    "summary": "Showing — 1294 E Apricot Ln with Natalie",
    "location": "1294 E Apricot Ln, Gilbert, AZ 85298",
    "description": ("Zillow inquiry.\nInquirers: Natalie.\n"
                    "Agent: Alex Foley (alex@azfoleyhomes.com)."),
    "start": {"dateTime": "2030-01-09T15:15:00-07:00"},
    "end": {"dateTime": "2030-01-09T15:45:00-07:00"},
}


class _Harness:
    def __init__(self, monkeypatch, blocked=None, events=None):
        self.stages = []
        self.threads = []
        self.escalations = []

        def _send_stage(thread_id, stage_key, relay, body, labels_add,
                        labels_remove, meta, trigger_message_id=""):
            self.stages.append({
                "stage": stage_key, "relay": relay, "body": body,
                "labels_add": list(labels_add), "labels_remove": list(labels_remove),
                "template": meta.get("template"), "trigger": trigger_message_id,
            })
            return "sent"

        monkeypatch.setattr(responder, "send_stage", _send_stage)
        monkeypatch.setattr(
            responder, "needs_human",
            lambda tid, name, addr, line, urgent=False:
                self.escalations.append((tid, name, addr, line, urgent)))
        monkeypatch.setattr(responder.ledger, "blocked_addresses",
                            lambda: list(BLOCKED if blocked is None else blocked))
        monkeypatch.setattr(
            responder.ledger, "create_thread",
            lambda tid, **f: self.threads.append((tid, f)))
        monkeypatch.setattr(responder.cal, "list_events",
                            lambda days=7: list(events or []))

    @property
    def only_stage(self):
        assert len(self.stages) == 1, f"expected ONE send, got {len(self.stages)}"
        return self.stages[0]

    @property
    def only_thread(self):
        assert len(self.threads) == 1
        return self.threads[0][1]


def _run(thread_id="t_new", first_name="Alondra",
         address="1641 E Coronado Rd, Phoenix, AZ 85006",
         renter_text="Hi, I am interested in this home and would like to see it.",
         message_id="1a05f00d"):
    return responder.handle_new_inquiry(thread_id, first_name, address, RELAY,
                                        renter_text, message_id)


# ------------------------------------------------- blocked-address gate

def test_leased_home_gets_the_rented_reply_and_never_a_tour_invite(monkeypatch):
    h = _Harness(monkeypatch)
    assert _run() == "sent"
    stage = h.only_stage
    assert stage["stage"] == "leased"
    assert stage["template"] == "leased"
    assert stage["body"] == T.leased_reply("Alondra",
                                           "1641 E Coronado Rd, Phoenix, AZ 85006")
    assert "has been rented and is no longer available" in stage["body"]
    # The 8/4 alondra failure mode, asserted on the copy itself.
    assert "come take a look" not in stage["body"]
    assert T.WINDOWS_BLOCK not in stage["body"]
    assert T.APP_LINK not in stage["body"]
    # Handled, not awaiting-renter: nobody should be waiting on a reply here.
    assert stage["labels_add"] == [responder.HANDLED_LABEL]
    assert responder.AWAITING_LABEL not in stage["labels_add"]
    assert h.only_thread["state"] == ledger.LEASED
    assert h.only_thread["property_address"] == "1641 E Coronado Rd, Phoenix, AZ 85006"
    assert h.only_thread["relay_email"] == RELAY


def test_house_next_door_to_a_leased_one_still_gets_a_tour(monkeypatch):
    # 1641 is blocked; 1643 is a different, rentable house. Street-number
    # bleed here would tell a live lead their home is gone.
    h = _Harness(monkeypatch)
    assert _run(address="1643 E Coronado Rd, Phoenix, AZ 85006") == "sent"
    assert h.only_stage["template"] == "availability_ask"
    assert h.only_thread["state"] == ledger.ACKED


def test_blocked_list_read_failure_still_answers_the_lead(monkeypatch):
    # blocked_addresses() swallows its own errors and returns [] - pin the
    # consequence: the instant reply still goes out rather than nothing.
    h = _Harness(monkeypatch, blocked=[])
    assert _run() == "sent"
    assert h.only_stage["template"] == "availability_ask"


# ------------------------------------------------- the ordinary first reply

def test_fresh_inquiry_with_no_showing_asks_for_availability(monkeypatch):
    h = _Harness(monkeypatch)
    assert _run(first_name="Daliana",
                address="3896 E Alfalfa Dr, Gilbert, AZ 85298") == "sent"
    stage = h.only_stage
    assert stage["stage"] == "first_reply"
    assert stage["template"] == "availability_ask"
    assert stage["body"] == T.availability_ask(
        "Daliana", "3896 E Alfalfa Dr, Gilbert, AZ 85298")
    assert stage["relay"] == RELAY
    assert stage["trigger"] == "1a05f00d"
    assert stage["labels_add"] == [responder.AWAITING_LABEL]
    doc = h.only_thread
    assert doc["state"] == ledger.ACKED
    assert doc["renter_name"] == "Daliana"
    assert doc["has_open_question"] is False
    assert doc["offered_start_iso"] is None
    assert doc["offered_event_id"] == ""
    assert h.escalations == []


def test_existing_showing_offers_that_slot_and_records_it(monkeypatch):
    # Consolidate first: the house already has a tour, so offer it instead of
    # asking for times and ending up with two events at one address.
    h = _Harness(monkeypatch, blocked=[], events=[APRICOT_EVENT])
    assert _run(first_name="Matthew",
                address="1294 E Apricot Ln, Gilbert, AZ 85298") == "sent"
    stage = h.only_stage
    assert stage["template"] == "offer_existing"
    assert "Wednesday, January 9, at 3:15 PM" in stage["body"]
    assert "I can add you right in" in stage["body"]
    doc = h.only_thread
    assert doc["state"] == ledger.OFFERED
    assert doc["offered_start_iso"] == "2030-01-09T15:15:00-07:00"
    assert doc["offered_event_id"] == "ev_apricot"


def test_showing_at_a_different_house_is_not_offered(monkeypatch):
    h = _Harness(monkeypatch, blocked=[], events=[APRICOT_EVENT])
    assert _run(first_name="Kim", address="1294 E Eugie Ave, Phoenix, AZ") == "sent"
    assert h.only_stage["template"] == "availability_ask"
    assert h.only_thread["state"] == ledger.ACKED


def test_calendar_outage_never_blocks_the_instant_reply(monkeypatch):
    def _boom(days=7):
        raise RuntimeError("composio events_list 503")

    h = _Harness(monkeypatch, blocked=[])
    monkeypatch.setattr(responder.cal, "list_events", _boom)
    assert _run(address="3896 E Alfalfa Dr, Gilbert, AZ") == "sent"
    assert h.only_stage["template"] == "availability_ask"


# ------------------------------------------------- questions: one send, always

def test_matched_standing_rule_answers_inline_in_a_single_send(monkeypatch):
    # The 7/30-8/1 shadow soak caught first_reply + a separate standing-rule
    # email going out 68ms apart. One send, answer folded in, no escalation.
    h = _Harness(monkeypatch, blocked=[])
    assert _run(first_name="Tynisha",
                address="736 E Gold Dust Way, Gilbert, AZ",
                renter_text="Hi! Does the home come with a washer and dryer?") == "sent"
    stage = h.only_stage
    assert stage["template"] == "availability_ask+appliances"
    assert facts.APPLIANCES_LINE in stage["body"]
    assert T.QUESTION_ACK not in stage["body"]  # a real answer beats the ack
    assert stage["labels_add"] == [responder.AWAITING_LABEL]
    assert responder.NEEDS_REPLY_LABEL not in stage["labels_add"]
    assert h.only_thread["has_open_question"] is False
    assert h.escalations == []


def test_unanswerable_question_acks_labels_and_pages_alex_once(monkeypatch):
    h = _Harness(monkeypatch, blocked=[])
    text = "Is there an HOA, and does the back gate open onto the greenbelt?"
    assert _run(first_name="Jessica", address="2118 S El Marino, Mesa, AZ",
                renter_text=text) == "sent"
    stage = h.only_stage
    assert T.QUESTION_ACK in stage["body"]
    assert stage["labels_add"] == [responder.AWAITING_LABEL,
                                   responder.NEEDS_REPLY_LABEL]
    assert h.only_thread["has_open_question"] is True
    # We promised a follow-up in that ack line, so exactly one page goes out.
    (tid, name, addr, line, urgent), = h.escalations
    assert (tid, name) == ("t_new", "Jessica")
    assert line == text[:120]
    assert urgent is False


def test_a_link_with_a_query_string_is_not_a_question(monkeypatch):
    # URLs carry '?'. Treating relay boilerplate as a question paged Alex and
    # labeled the thread needs-reply for a lead who asked nothing.
    h = _Harness(monkeypatch, blocked=[])
    text = ("I saw the listing at https://www.zillow.com/homedetails/x_zpid/"
            "?utm_source=email&t=1 and I want to see it.")
    assert _run(first_name="Cody", address="3308 S Mariana Cir, Tempe, AZ",
                renter_text=text) == "sent"
    stage = h.only_stage
    assert stage["template"] == "availability_ask"
    assert T.QUESTION_ACK not in stage["body"]
    assert stage["labels_add"] == [responder.AWAITING_LABEL]
    assert h.only_thread["has_open_question"] is False
    assert h.escalations == []


def test_no_page_when_the_promised_reply_never_sent(monkeypatch):
    # The ack line is a promise. The same question DOES page Alex when the
    # send succeeds (test above); a failed send means the renter was never
    # promised anything, so the page must not go out either.
    unanswerable = "Is there an HOA, and does the back gate open onto the greenbelt?"
    assert facts.standing_rule_for(unanswerable) is None  # ack path, not answer path
    h = _Harness(monkeypatch, blocked=[])
    monkeypatch.setattr(responder, "send_stage",
                        lambda *a, **k: "skipped:send-failed")
    assert _run(first_name="Jessica", renter_text=unanswerable,
                address="2118 S El Marino, Mesa, AZ") == "skipped:send-failed"
    assert h.escalations == []
    # the thread doc still records the open question for the next sweep
    assert h.only_thread["has_open_question"] is True
