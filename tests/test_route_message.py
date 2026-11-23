"""responder.route_message - the gate every inbound Zillow email passes.

It had no test. It is the code that decides whether a renter is emailed at
all: the duplicate-delivery claim, Zillow's duplicate-copy content gate, the
"Alex already answered" check, the subject-shape fallback that recovers the
property address, and the legacy-thread reconstruction.

Two of those carry live tripwires that only an end-to-end test can hold:

  * route_message greps SENT MAIL for phrases owned by templates.py
    ("You're all set", "has been rented"/"no longer available") to decide
    whether a pre-ledger thread is BOOKED, LEASED or still waiting. Both
    files say "editing this copy requires updating the other". The tests
    below feed the REAL templates through the REAL router, so renaming a
    template string breaks them instead of breaking production. The 8/4
    incident is the reason: alondra's thread reconstructed as waiting and
    got nudged into a tour invite for a leased house.
  * the "requesting to tour" subject family carries no address, so the
    blocked-address check had nothing to match on and a leased home could
    hand out a showing invite (2026-08-22). The address has to come back
    out of the HTML body.

Only Gmail, Firestore and the two handlers are replaced; the parsing,
identity extraction and state decisions are the production ones.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import pytest  # noqa: E402

import ledger  # noqa: E402
import responder  # noqa: E402
import templates as T  # noqa: E402

THREAD = "1a17c18a5f6ca383"
ADDR = "2701 E Willetta St, Phoenix, AZ, 85008"
RELAY = "aaliyah+58y56j72rjw54@convo.zillow.com"
RENTER_SENDER = f"Aaliyah via Zillow <{RELAY}>"
ALEX_SENDER = "Alex Foley <alex@azfoleyhomes.com>"
SUBJECT = f"Aaliyah is requesting information about {ADDR}"

# The "requesting to tour" family: the subject carries neither the renter's
# name nor the address, so both have to come out of the HTML body blocks.
TOUR_SUBJECT = "A renter is requesting to tour your property"
TOUR_REQUEST_HTML = """<table cellpadding="0"><tbody>
<tr><td style="padding:0 0 8px"><div style="font-weight:700;color:#596b80">RENTER'S NAME</div>
<div style="color:#2a2a33;font-size:16px">Anna Woodhouse</div></td></tr>
<tr><td style="padding:0 0 8px"><div style="font-weight:700;color:#596b80">RENTER'S MESSAGE</div>
<div style="color:#2a2a33">I am requesting to tour this property. Is Monday open?</div></td></tr>
<tr><td><div style="font-weight:700;color:#596b80">Regarding your listing at:</div>
</td></tr><tr><td style="padding:0 0 16px">
<div style="color:#2a2a33;font-size:16px">300 N Gila Springs Blvd #109, Chandler, AZ, 85226</div>
</td></tr></tbody></table>"""


def renter(mid, text):
    return {"messageId": mid, "sender": RENTER_SENDER, "messageText": text}


def alex(mid, body):
    return {"messageId": mid, "sender": ALEX_SENDER, "messageText": body}


def _wire(monkeypatch, msgs, thread_doc=None, claim_message=True,
          claim_content=True, new_inquiry="sent", reply="sent"):
    """Records what route_message decided; the handlers are stubbed so their
    arguments (name, address, relay, reconstructed state) are the output."""
    calls = {"claims": [], "fetches": [], "outcomes": [], "created": [],
             "new_inquiry": [], "reply": []}
    led = responder.ledger

    def _new_inquiry(thread_id, first_name, address, relay, renter_text, mid):
        calls["new_inquiry"].append(
            (thread_id, first_name, address, relay, renter_text, mid))
        if isinstance(new_inquiry, Exception):
            raise new_inquiry
        return new_inquiry

    def _reply(thread_id, doc, messages, renter_text, mid):
        calls["reply"].append((thread_id, dict(doc or {}), renter_text, mid))
        if isinstance(reply, Exception):
            raise reply
        return reply

    def _create_thread(thread_id, **fields):
        calls["created"].append((thread_id, fields))

    monkeypatch.setattr(led, "claim_message",
                        lambda mid, tid: calls["claims"].append((mid, tid))
                        or claim_message)
    monkeypatch.setattr(led, "claim_content_hash",
                        lambda tid, text: claim_content)
    monkeypatch.setattr(led, "record_message_outcome",
                        lambda mid, outcome, classification=None, extra=None:
                        calls["outcomes"].append((mid, outcome, extra or {})))
    monkeypatch.setattr(
        led, "get_thread",
        lambda tid: (dict(calls["created"][-1][1]) if calls["created"]
                     else (dict(thread_doc) if thread_doc is not None else None)))
    monkeypatch.setattr(led, "create_thread", _create_thread)
    monkeypatch.setattr(responder.gm, "fetch_thread",
                        lambda tid: calls["fetches"].append(tid) or list(msgs))
    monkeypatch.setattr(responder, "handle_new_inquiry", _new_inquiry)
    monkeypatch.setattr(responder, "handle_reply", _reply)
    return calls


# ------------------------------------------------------------- cheap rejects

def test_our_own_outbound_never_reaches_firestore(monkeypatch):
    calls = _wire(monkeypatch, [])
    assert responder.route_message(THREAD, SUBJECT, ALEX_SENDER, "m1") \
        == "skipped:self-send"
    assert calls["claims"] == [] and calls["fetches"] == []


def test_duplicate_webhook_delivery_stops_before_the_thread_fetch(monkeypatch):
    """claim_message is the create-once gate; losing it means someone else
    already owns this message."""
    calls = _wire(monkeypatch, [renter("m1", "Hi")], claim_message=False)
    assert responder.route_message(THREAD, SUBJECT, RENTER_SENDER, "m1") \
        == "skipped:dup-delivery"
    assert calls["claims"] == [("m1", THREAD)]
    assert calls["fetches"] == []
    assert calls["new_inquiry"] == [] and calls["reply"] == []


def test_a_message_with_no_id_still_claims_a_deterministic_key(monkeypatch):
    calls = _wire(monkeypatch, [renter("m1", "Hi")], claim_message=False)
    responder.route_message(THREAD, SUBJECT, RENTER_SENDER, "")
    assert calls["claims"] == [(f"noid-{THREAD}", THREAD)]


def test_empty_thread_is_recorded_and_dropped(monkeypatch):
    calls = _wire(monkeypatch, [])
    assert responder.route_message(THREAD, SUBJECT, RENTER_SENDER, "m1") \
        == "skipped:empty-thread"
    assert calls["outcomes"] == [("m1", "empty-thread", {})]


def test_zillows_duplicate_copy_is_dropped_on_the_content_gate(monkeypatch):
    """Same words, new message id - Zillow sends duplicate notifications."""
    calls = _wire(monkeypatch, [renter("m2", "I would like to schedule a tour.")],
                  claim_content=False)
    assert responder.route_message(THREAD, SUBJECT, RENTER_SENDER, "m2") \
        == "skipped:dup-content"
    assert calls["outcomes"] == [("m2", "dup-content", {})]
    assert calls["new_inquiry"] == []


def test_a_message_alex_already_answered_is_never_answered_again(monkeypatch):
    msgs = [renter("m1", "Can I see it Monday?"),
            alex("m2", T.windows_ask("Aaliyah"))]
    calls = _wire(monkeypatch, msgs)
    assert responder.route_message(THREAD, SUBJECT, RENTER_SENDER, "m1") \
        == "skipped:already-answered"
    assert calls["outcomes"] == [("m1", "already-answered", {})]
    assert calls["reply"] == [] and calls["new_inquiry"] == []


def test_a_thread_with_no_relay_message_is_not_ours(monkeypatch):
    msgs = [{"messageId": "m1", "sender": "Owner <owner@example.com>",
             "messageText": "Any update on the Willetta house?"}]
    calls = _wire(monkeypatch, msgs)
    assert responder.route_message(THREAD, "Re: the Willetta house",
                                   "owner@example.com", "m1") \
        == "skipped:not-zillow"
    assert calls["outcomes"] == [("m1", "not-zillow", {})]
    assert calls["created"] == []


# ----------------------------------------------------------- the new inquiry

def test_first_inquiry_hands_the_handler_the_parsed_name_and_address(monkeypatch):
    text = "I would like to schedule a tour."
    calls = _wire(monkeypatch, [renter("m1", text)])
    assert responder.route_message(THREAD, SUBJECT, RENTER_SENDER, "m1") == "sent"
    assert calls["new_inquiry"] == [
        (THREAD, "Aaliyah", ADDR, RELAY, text, "m1")]
    assert calls["outcomes"] == [("m1", "sent", {"renter_text": text})]


def test_requesting_to_tour_recovers_the_address_from_the_body(monkeypatch):
    """2026-08-22: this subject shape lost the name AND the address, so the
    blocked-address check had nothing to match and a leased home could still
    get a showing invite."""
    calls = _wire(monkeypatch, [{"messageId": "m1", "sender": RENTER_SENDER,
                                 "messageText": TOUR_REQUEST_HTML}])
    assert responder.route_message(THREAD, TOUR_SUBJECT, RENTER_SENDER, "m1") \
        == "sent"
    thread_id, name, address, relay, renter_text, mid = calls["new_inquiry"][0]
    assert name == "Anna"
    assert address == "300 N Gila Springs Blvd #109, Chandler, AZ, 85226"
    assert relay == RELAY
    assert "Is Monday open?" in renter_text


def test_a_new_inquiry_with_no_relay_address_errors_without_sending(monkeypatch):
    calls = _wire(monkeypatch, [{"messageId": "m1", "sender": "",
                                 "messageText": "I'd love to tour this."}])
    assert responder.route_message(THREAD, SUBJECT, "", "m1") == "error-no-relay"
    assert calls["outcomes"] == [("m1", "no-relay", {})]
    assert calls["new_inquiry"] == []


def test_a_crash_in_the_inquiry_handler_is_recorded_not_swallowed(monkeypatch):
    calls = _wire(monkeypatch, [renter("m1", "Tour on Nov 16?")],
                  new_inquiry=RuntimeError("composio 500"))
    assert responder.route_message(THREAD, SUBJECT, RENTER_SENDER, "m1") \
        == "error:RuntimeError"
    mid, outcome, extra = calls["outcomes"][0]
    assert (mid, outcome) == ("m1", "error:RuntimeError")
    assert extra["error"] == "composio 500"
    assert extra["renter_text"] == "Tour on Nov 16?"


# ------------------------------------------------ known thread -> handle_reply

def test_a_known_thread_goes_straight_to_the_reply_handler(monkeypatch):
    doc = {"state": ledger.AWAITING_TIME, "renter_name": "Aaliyah",
           "property_address": ADDR, "relay_email": RELAY}
    calls = _wire(monkeypatch, [renter("m1", "Hi"), renter("m2", "Monday at 2?")],
                  thread_doc=doc)
    assert responder.route_message(THREAD, SUBJECT, RENTER_SENDER, "m2") == "sent"
    assert calls["created"] == []
    thread_id, got_doc, renter_text, mid = calls["reply"][0]
    assert (thread_id, mid) == (THREAD, "m2")
    assert got_doc["state"] == ledger.AWAITING_TIME
    assert renter_text == "Monday at 2?"


def test_a_crash_in_the_reply_handler_is_recorded_not_swallowed(monkeypatch):
    calls = _wire(monkeypatch, [renter("m1", "Monday at 2?")],
                  thread_doc={"state": ledger.ACKED}, reply=ValueError("bad slot"))
    assert responder.route_message(THREAD, SUBJECT, RENTER_SENDER, "m1") \
        == "error:ValueError"
    assert calls["outcomes"][0][1] == "error:ValueError"


# -------------------------------------- legacy reconstruction TRIPWIRE tests
# These feed the real templates through the real router. If templates.py
# changes the copy these greps depend on, these fail - which is the point.

def _reconstruct(monkeypatch, alex_body):
    """A pre-ledger thread: renter wrote, we answered with `alex_body`, renter
    wrote again. The second renter message is the trigger, so nothing of ours
    is newer and the router must rebuild the thread's state from sent mail."""
    msgs = [renter("m1", "Is this still available?"),
            alex("m2", alex_body),
            renter("m3", "Can I still come see it?")]
    calls = _wire(monkeypatch, msgs, thread_doc=None)
    out = responder.route_message(THREAD, SUBJECT, RENTER_SENDER, "m3")
    assert out == "sent"
    assert len(calls["created"]) == 1
    return calls["created"][0][1]


def test_a_renter_already_told_the_home_is_rented_rebuilds_as_leased(monkeypatch):
    """alondra, 8/4: reconstructed as waiting, then nudged into a tour invite
    for a house that was already gone. TRIPWIRE on templates.leased_reply."""
    fields = _reconstruct(monkeypatch, T.leased_reply("Aaliyah", ADDR))
    assert fields["state"] == ledger.LEASED


def test_a_confirmed_booking_rebuilds_as_booked(monkeypatch):
    """TRIPWIRE on templates.booking_confirmation ("You're all set")."""
    fields = _reconstruct(monkeypatch, T.booking_confirmation(
        "Aaliyah", ADDR, "tomorrow, Tuesday, December 1, at 10:00 AM",
        "Alex Foley"))
    assert fields["state"] == ledger.BOOKED


@pytest.mark.parametrize("template", [
    T.windows_ask("Aaliyah"),
    T.availability_ask("Aaliyah", ADDR),
    T.stale_nudge("Aaliyah", ADDR),
    T.propose_times("Aaliyah", ["Tuesday, December 1, at 10:00 AM"]),
])
def test_an_unanswered_scheduling_reply_rebuilds_as_waiting(monkeypatch, template):
    """Anything that is neither a booking nor a leased notice is still
    waiting on the renter - and must not be mistaken for either."""
    fields = _reconstruct(monkeypatch, template)
    assert fields["state"] == ledger.AWAITING_TIME


def test_reconstruction_carries_the_identity_the_reply_handler_needs(monkeypatch):
    fields = _reconstruct(monkeypatch, T.windows_ask("Aaliyah"))
    assert fields["renter_name"] == "Aaliyah"
    assert fields["property_address"] == ADDR
    assert fields["relay_email"] == RELAY
    assert fields["reconstructed"] is True


def test_reconstruction_without_a_parseable_subject_still_names_the_renter(monkeypatch):
    """"there" is the fallback greeting; it must never be empty, because the
    reply handler puts it straight into "Hi {name},"."""
    msgs = [renter("m1", "Is this still available?"),
            alex("m2", T.windows_ask("there")),
            renter("m3", "Can I still come see it?")]
    calls = _wire(monkeypatch, msgs, thread_doc=None)
    assert responder.route_message(THREAD, "Re: your Zillow listing",
                                   RENTER_SENDER, "m3") == "sent"
    fields = calls["created"][0][1]
    assert fields["renter_name"] == "there"
    assert fields["property_address"] == ""
