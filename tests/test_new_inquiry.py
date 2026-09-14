"""Coverage for responder.handle_new_inquiry - the first email a renter ever gets.

This is the only path that reaches a stranger cold, and it had no test. Four
things go wrong here and each costs something real:

  * a leased home gets invited for a tour anyway (the blocked-address gate is
    the only thing standing in the way, and it runs before the calendar lookup)
  * a question earns a second email on top of the first (the 7/30-8/1 shadow
    soak caught first_reply + a standing-rule email 68ms apart on 8 threads)
  * a question we CAN answer gets the "I'm checking on that" ack anyway, so a
    human is pinged for an answer the router already had
  * the "?" inside a listing URL's query string counts as a question, which
    escalates every inquiry that pastes a link

The production function runs for real down to gm.send_reply, so the assertions
are against the bytes that would reach the renter.
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import facts  # noqa: E402
import ledger  # noqa: E402
import responder  # noqa: E402
import rules  # noqa: E402
import templates as T  # noqa: E402

THREAD = "1a0612c4e9915f02"
MSG_ID = "1a0612c4e9915f11"
RELAY = "reply+7h2pqd@convo.zillow.com"
OPEN = "2120 S El Marino Dr, Mesa, AZ 85202"
LEASED_HOME = "2118 S El Marino Dr, Mesa, AZ 85202"

PLAIN_INQUIRY = ("Jessica says: Hi, I saw your listing and I'm interested in "
                 "seeing the home. I can be flexible this week.")


class _Inquiry:
    """Runs handle_new_inquiry for real; Firestore, Composio and the calendar
    are the only things replaced."""

    def __init__(self, monkeypatch, blocked=(), existing=None):
        self.blocked = list(blocked)
        self.existing = existing
        self.created, self.upserts, self.transitions = [], [], []
        self.emails, self.label_calls, self.pokes, self.alerts = [], [], [], []
        self.showing_lookups = []

        monkeypatch.setenv("DRY_RUN", "false")
        led, gm, cal = responder.ledger, responder.gm, responder.cal
        monkeypatch.setattr(led, "blocked_addresses", lambda: list(self.blocked))
        monkeypatch.setattr(led, "create_thread",
                            lambda t, **f: self.created.append((t, f)))
        monkeypatch.setattr(led, "upsert_thread",
                            lambda t, **f: self.upserts.append((t, f)))
        monkeypatch.setattr(led, "transition",
                            lambda t, st, **f: self.transitions.append((t, st, f)))
        monkeypatch.setattr(led, "get_thread", lambda t: dict(
            self.created[0][1]) if self.created else {})
        monkeypatch.setattr(led, "reserve_send", lambda t, s, m: "acquired")
        monkeypatch.setattr(led, "mark_sent", lambda t, s, **f: None)
        monkeypatch.setattr(led, "mark_failed", lambda t, s, e: None)
        monkeypatch.setattr(cal, "find_existing_showing", self._find_showing)
        monkeypatch.setattr(gm, "fetch_thread", lambda t: [])
        monkeypatch.setattr(gm, "send_reply", self._send)
        monkeypatch.setattr(gm, "modify_labels", self._labels)
        monkeypatch.setattr(gm, "poke_ping",
                            lambda m: self.pokes.append(m) or True)
        monkeypatch.setattr(gm, "alert_email",
                            lambda s, b: self.alerts.append((s, b)) or True)
        monkeypatch.setattr(responder.llm, "review_reply",
                            lambda transcript, body, template: {"verdict": "ok"})

    def _find_showing(self, address, *a, **kw):
        self.showing_lookups.append(address)
        return self.existing

    def _send(self, thread_id, relay, body):
        self.emails.append((thread_id, relay, body))
        return {"data": {"id": "m1"}}

    def _labels(self, thread_id, add, remove):
        self.label_calls.append((thread_id, list(add), list(remove)))
        return {"data": {}}

    def run(self, address, renter_text=PLAIN_INQUIRY, first_name="Jessica"):
        return responder.handle_new_inquiry(
            THREAD, first_name, address, RELAY, renter_text, MSG_ID)

    @property
    def body(self):
        assert len(self.emails) == 1, f"expected one email, got {len(self.emails)}"
        return self.emails[0][2]

    @property
    def thread_fields(self):
        (_, fields), = self.created
        return fields

    def labels_added(self):
        return [l for _, add, _ in self.label_calls for l in add]


# ------------------------------------------------- blocked-address gate

def test_leased_home_gets_the_rented_reply_and_never_a_tour_invite(monkeypatch):
    h = _Inquiry(monkeypatch, blocked=["2118 S El Marino Dr"])

    assert h.run(LEASED_HOME) == "sent"

    body = h.body
    assert "has been rented and is no longer available" in body
    # nothing that reads as an invitation may survive into this reply
    assert "Pick a time" not in body
    assert "10:00 AM to 6:30 PM" not in body
    assert "tour" not in body.lower()
    assert "application" not in body.lower()

    assert h.thread_fields["state"] == ledger.LEASED
    assert h.labels_added() == [responder.HANDLED_LABEL]
    # the gate runs before the consolidate-first lookup: no calendar read at all
    assert h.showing_lookups == []


def test_a_different_house_on_the_blocked_street_still_gets_the_tour_invite(monkeypatch):
    """is_blocked_address matches on street number AND name core. 2120 is not
    2118, and blocking the neighbor would silently kill a live listing."""
    h = _Inquiry(monkeypatch, blocked=["2118 S El Marino Dr"])

    assert h.run(OPEN) == "sent"
    assert "has been rented" not in h.body
    assert T.WINDOWS_BLOCK in h.body
    assert h.thread_fields["state"] == ledger.ACKED


# ------------------------------------------------- consolidate first

def test_an_existing_showing_is_offered_instead_of_an_open_ask(monkeypatch):
    start = datetime.now(rules.AZ_TZ).replace(microsecond=0) + timedelta(days=2)
    h = _Inquiry(monkeypatch, existing={
        "event": {"id": "ev_9f31c", "summary": f"Showing — {OPEN} with Matthew"},
        "start_az": start,
        "when_human": "Friday, Sep 19 at 3:15 PM"})

    assert h.run(OPEN) == "sent"

    body = h.body
    assert "we actually have a showing already lined up there on " \
           "Friday, Sep 19 at 3:15 PM" in body
    assert "I can add you right in" in body

    fields = h.thread_fields
    assert fields["state"] == ledger.OFFERED
    assert fields["offered_start_iso"] == start.isoformat()
    assert fields["offered_event_id"] == "ev_9f31c"


# ------------------------------------------------- one send per inquiry

def test_an_unanswerable_question_costs_one_email_and_one_escalation(monkeypatch):
    h = _Inquiry(monkeypatch, blocked=[])
    text = ("Jessica says: Hi! What year was the home built? I'd also love to "
            "come see it this week.")

    assert h.run(OPEN, renter_text=text) == "sent"

    body = h.body  # asserts exactly one email went out
    assert T.QUESTION_ACK.strip() in body
    assert T.WINDOWS_BLOCK in body
    assert h.thread_fields["has_open_question"] is True
    assert responder.NEEDS_REPLY_LABEL in h.labels_added()

    poke, = h.pokes
    assert poke.startswith("Needs you: Jessica — 2120 S El Marino Dr")
    assert "What year was the home built?" in poke
    assert [state for _, state, _ in h.transitions] == [ledger.NEEDS_HUMAN]


def test_an_answerable_question_is_answered_inline_with_no_escalation(monkeypatch):
    h = _Inquiry(monkeypatch, blocked=[])
    text = ("Jessica says: Does the home come with a washer and dryer? "
            "Hoping to tour this weekend.")

    assert h.run(OPEN, renter_text=text) == "sent"

    body = h.body
    assert facts.APPLIANCES_LINE in body
    assert T.QUESTION_ACK.strip() not in body
    assert h.thread_fields["has_open_question"] is False
    assert responder.NEEDS_REPLY_LABEL not in h.labels_added()
    assert h.pokes == []
    assert h.transitions == []


def test_a_question_mark_inside_a_listing_url_is_not_a_question(monkeypatch):
    """Links are stripped before question detection. Without that, every
    inquiry quoting a Zillow URL pings a human for an answer nobody asked for."""
    h = _Inquiry(monkeypatch, blocked=[])
    text = ("Jessica says: I found the home here "
            "https://www.zillow.com/homedetails/2120-S-El-Marino-Dr"
            "?utm_source=email&utm_medium=share and I'm very interested.")

    assert h.run(OPEN, renter_text=text) == "sent"

    body = h.body
    assert T.QUESTION_ACK.strip() not in body
    assert h.thread_fields["has_open_question"] is False
    assert responder.NEEDS_REPLY_LABEL not in h.labels_added()
    assert h.pokes == []
