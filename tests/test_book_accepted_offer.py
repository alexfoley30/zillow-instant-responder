"""responder.book_accepted_offer - the renter said yes, so a real showing
gets a real renter on it and a real confirmation goes out.

This is the most expensive path in the service and nothing tested it. It
carries the ATOMIC BOOKING TRIPWIRE (calendar write FIRST, email second,
because "You're all set!" for a showing that is not on the calendar is how
Alec was stood up on 8/25) and the offered-event rule (a showing created
between the offer and the yes must not hijack the fold - audit 8/25).

The fold, the slot lookup and the newest-message guard all run as the real
production functions here; only Composio, Gmail and Firestore are doubled,
so the assertions are on the actual calendar payload, the actual email body
and the actual thread state.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import pytest  # noqa: E402

import calendar_logic as cal  # noqa: E402
import ledger  # noqa: E402
import responder  # noqa: E402
import rules  # noqa: E402

AZ = rules.AZ_TZ
THREAD = "1a0545afd0fe9807"
TRIGGER = "1a0545b17c33ee01"
RELAY = "reply+1a0545afd0fe9807@convo.zillow.com"
ADDR = "2118 S El Marino, Mesa, AZ, 85202"

# Production description format written by cal.create_showing_event.
DESC = ("Zillow inquiry.\nInquirers: Jessica.\n"
        "Agent: Alex Foley (alex@azfoleyhomes.com).")

RENTER_SPOKE_LAST = [
    {"messageId": TRIGGER, "sender": f"Owen <{RELAY}>",
     "messageText": "That time works, see you then!\nReply on Zillow"},
]
RENTER_WROTE_AGAIN = RENTER_SPOKE_LAST + [
    {"messageId": "1a0545c99b12dd34", "sender": f"Owen <{RELAY}>",
     "messageText": "Actually can we do the later one?\nReply on Zillow"},
]


def _showing_day():
    """A showing two days out, so the 2h minimum lead is never in play."""
    return (datetime.now(AZ) + timedelta(days=2)).date()


def _event(event_id, hour, minute=0):
    day = _showing_day()
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=AZ)
    end = start + timedelta(minutes=rules.SHOWING_MINUTES)
    return {
        "id": event_id,
        "summary": "Showing — 2118 S El Marino with Jessica",
        "location": ADDR,
        "description": DESC,
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Phoenix"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Phoenix"},
        "attendees": [{"email": "alex@azfoleyhomes.com"},
                      {"email": "azfoleyhomes@gmail.com"}],
    }


SOON_HOUR, OFFERED_HOUR = 10, 13


def _start_az(hour):
    day = _showing_day()
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=AZ)


class Booking:
    """Doubles the three external services; the booking logic is real."""

    def __init__(self, monkeypatch, events=None, msgs=None, verdict="acquired",
                 calendar_ok=True, label_error=None, renters=("Jessica",),
                 dry=False):
        self.events = [_event("evSOON", SOON_HOUR), _event("evOFFERED", OFFERED_HOUR)] \
            if events is None else events
        self.msgs = RENTER_SPOKE_LAST if msgs is None else msgs
        self.verdict = verdict
        self.calendar_ok = calendar_ok
        self.label_error = label_error
        self.composio, self.mail, self.labels = [], [], []
        self.labels_pending, self.marked_sent, self.marked_failed = [], [], []
        self.transitions, self.thread_patches, self.shadow = [], [], []
        self.pokes, self.alerts = [], []
        self.showings = {"evSOON": {"renters": list(renters),
                                    "agent_name": "Alex Foley"},
                         "evOFFERED": {"renters": list(renters),
                                       "agent_name": "Alex Foley"}}

        monkeypatch.setattr(responder, "dry_run", lambda: dry)
        monkeypatch.setattr(cal, "list_events",
                            lambda days=7, time_min=None: list(self.events))
        monkeypatch.setattr(cal, "composio_execute", self._composio)
        monkeypatch.setattr(cal.ledger, "get_showing", self.showings.get)
        monkeypatch.setattr(cal.ledger, "upsert_showing", self._upsert_showing)
        monkeypatch.setattr(responder.ledger, "reserve_send", self._reserve)
        monkeypatch.setattr(responder.ledger, "takeover_send", lambda t, s: True)
        monkeypatch.setattr(responder.ledger, "mark_sent", self._mark_sent)
        monkeypatch.setattr(responder.ledger, "mark_failed", self._mark_failed)
        monkeypatch.setattr(responder.ledger, "set_labels_pending",
                            lambda t, s, a, r: self.labels_pending.append((s, a, r)))
        monkeypatch.setattr(responder.ledger, "transition", self._transition)
        monkeypatch.setattr(responder.ledger, "upsert_thread", self._upsert_thread)
        monkeypatch.setattr(responder.ledger, "write_shadow",
                            lambda t, s, payload: self.shadow.append((s, payload)))
        monkeypatch.setattr(responder.ledger, "get_thread",
                            lambda t: {"state": ledger.OFFERED, "renter_name": "Owen",
                                       "property_address": ADDR})
        monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: list(self.msgs))
        monkeypatch.setattr(responder.gm, "send_reply", self._send)
        monkeypatch.setattr(responder.gm, "modify_labels", self._labels)
        monkeypatch.setattr(responder.gm, "poke_ping", self._poke)
        monkeypatch.setattr(responder.gm, "alert_email", self._alert)
        monkeypatch.setattr(responder.llm, "review_reply",
                            lambda transcript, body, template, now_phx=None:
                            {"verdict": "send", "reason": ""})

    # -- composio (calendar)
    def _composio(self, slug, params):
        self.composio.append((slug, params))
        if not self.calendar_ok:
            return {"successful": False,
                    "error": "400 Invalid value for: event_id"}
        return {"successful": True, "data": {"response_data": {"id": params.get("event_id")}}}

    def _upsert_showing(self, event_id, **fields):
        self.showings.setdefault(event_id, {}).update(
            {k: v for k, v in fields.items() if v is not None})

    # -- ledger
    def _reserve(self, thread_id, stage_key, meta):
        self.reserved = (stage_key, dict(meta))
        return self.verdict

    def _mark_sent(self, thread_id, stage_key, **fields):
        self.marked_sent.append((stage_key, fields))

    def _mark_failed(self, thread_id, stage_key, error):
        self.marked_failed.append((stage_key, error))

    def _transition(self, thread_id, state, **fields):
        self.transitions.append((state, fields))

    def _upsert_thread(self, thread_id, **fields):
        self.thread_patches.append(fields)

    # -- gmail
    def _send(self, thread_id, relay, body):
        self.mail.append((relay, body))
        return {"successful": True}

    def _labels(self, thread_id, add, remove):
        if self.label_error:
            raise RuntimeError(self.label_error)
        self.labels.append((add, remove))
        return {"successful": True}

    def _poke(self, msg):
        self.pokes.append(msg)
        return True

    def _alert(self, subject, body):
        self.alerts.append((subject, body))
        return True

    # -- helpers
    def slugs(self):
        return [slug for slug, _ in self.composio]

    def updates(self):
        return [params for slug, params in self.composio
                if slug == "GOOGLECALENDAR_UPDATE_EVENT"]


def accept(doc=None, message_id=TRIGGER):
    return responder.book_accepted_offer(
        THREAD, doc if doc is not None else {"offered_event_id": "evOFFERED"},
        "Owen", ADDR, RELAY, message_id)


# ------------------------------------------------------- the booking


def test_yes_folds_the_offered_event_and_confirms_that_time(monkeypatch):
    b = Booking(monkeypatch)
    assert accept() == "sent"

    # The event that was OFFERED, not the soonest one at the same house.
    assert b.slugs() == ["GOOGLECALENDAR_UPDATE_EVENT"]
    update = b.updates()[0]
    assert update["event_id"] == "evOFFERED"
    assert update["description"] == (
        "Zillow inquiry.\nInquirers: Jessica, Owen.\n"
        "Agent: Alex Foley (alex@azfoleyhomes.com).")
    assert update["summary"] == "Showing — 2118 S El Marino with Jessica"
    assert update["event_duration_minutes"] == rules.SHOWING_MINUTES
    assert b.showings["evOFFERED"]["renters"] == ["Jessica", "Owen"]

    # One confirmation, naming the offered slot and the agent who meets them.
    when = cal.fmt_showing_time(_start_az(OFFERED_HOUR))
    assert len(b.mail) == 1
    relay, body = b.mail[0]
    assert relay == RELAY
    assert body.startswith("Hi Owen,")
    assert "You're all set!" in body
    assert when in body
    assert "I'll meet you there" in body

    assert b.marked_sent == [("booked__evOFFERED", {"event_id": "evOFFERED"})]
    assert b.labels == [([responder.HANDLED_LABEL], [responder.AWAITING_LABEL])]
    state, fields = b.transitions[-1]
    assert state == ledger.BOOKED
    assert fields == {"event_id": "evOFFERED",
                      "booked_start_iso": _start_az(OFFERED_HOUR).isoformat(),
                      "agent": "Alex Foley"}
    assert b.pokes and when in b.pokes[0] and "[FYI]" in b.pokes[0]


def test_without_a_recorded_offer_the_soonest_showing_is_the_fallback(monkeypatch):
    b = Booking(monkeypatch)
    assert accept(doc={}) == "sent"
    assert b.updates()[0]["event_id"] == "evSOON"
    assert cal.fmt_showing_time(_start_az(SOON_HOUR)) in b.mail[0][1]


def test_a_vanished_offered_event_falls_back_instead_of_dropping_the_yes(monkeypatch):
    """The offered event was deleted before the renter answered; the other
    showing at the same house is still a real slot for them."""
    b = Booking(monkeypatch, events=[_event("evSOON", SOON_HOUR)])
    assert accept() == "sent"
    assert b.updates()[0]["event_id"] == "evSOON"


# ------------------------------------------------------- the tripwire


def test_a_rejected_calendar_write_blocks_the_confirmation(monkeypatch):
    """8/25, Alec: Composio quietly 400'd the fold and the email went out
    anyway, so a renter believed they were booked onto an event that never
    carried them. The calendar write must fail loud and stop the email."""
    b = Booking(monkeypatch, calendar_ok=False)
    assert accept() == "skipped:book-failed"
    assert b.mail == []
    assert b.labels == []
    assert b.transitions == []            # nothing reaches BOOKED
    assert b.marked_sent == []
    assert len(b.marked_failed) == 1
    stage, error = b.marked_failed[0]
    assert stage == "booked__evOFFERED"
    assert "fold update evOFFERED" in error


def test_a_renter_already_on_the_event_is_not_folded_twice(monkeypatch):
    """A retry after a fold that succeeded and an email that did not: the
    fold is idempotent, and the renter still gets their confirmation."""
    b = Booking(monkeypatch, renters=("Jessica", "Owen"))
    assert accept() == "sent"
    assert b.updates() == []
    assert len(b.mail) == 1
    assert b.showings["evOFFERED"]["renters"] == ["Jessica", "Owen"]


def test_a_label_failure_cannot_undo_the_booking(monkeypatch):
    b = Booking(monkeypatch, label_error="GMAIL_MODIFY_THREAD_LABELS 500")
    assert accept() == "sent"
    assert len(b.mail) == 1
    assert b.labels_pending == [("booked__evOFFERED",
                                 [responder.HANDLED_LABEL],
                                 [responder.AWAITING_LABEL])]
    assert b.transitions[-1][0] == ledger.BOOKED


# ------------------------------------------------------- the refusals


def test_the_send_lock_stops_a_second_confirmation(monkeypatch):
    b = Booking(monkeypatch, verdict="already-sent")
    assert accept() == "skipped:already-sent"
    assert b.composio == []
    assert b.mail == []
    assert b.transitions == []


def test_a_yes_with_no_visible_event_escalates_instead_of_asking_again(monkeypatch):
    """Natalie + Yesenia, 8/5: both said yes to a time Alex had arranged by
    hand, and both got "what day works?" back. Asking again reads clueless."""
    b = Booking(monkeypatch, events=[])
    assert accept() == "no-send:accept-without-event"
    assert b.mail == []
    assert b.composio == []
    assert b.transitions[0][0] == ledger.NEEDS_HUMAN
    assert b.pokes and b.pokes[0].startswith("URGENT same-day: Owen")
    assert "accepted a time but no matching calendar event" in b.pokes[0]


def test_a_newer_renter_message_hands_the_booking_to_a_human(monkeypatch):
    """Alec, 8/25: a yes booked off a stale trigger while "I cannot do
    3:15pm" sat unread next to it."""
    b = Booking(monkeypatch, msgs=RENTER_WROTE_AGAIN)
    assert accept() == "no-send:superseded-by-newer-message"
    assert b.composio == []
    assert b.mail == []
    assert b.transitions[0][0] == ledger.NEEDS_HUMAN
    assert "book by hand" in b.pokes[0]


def test_dry_run_shadows_the_fold_and_sends_nothing(monkeypatch):
    b = Booking(monkeypatch, dry=True)
    assert accept() == "shadowed"
    assert b.composio == [] and b.mail == [] and b.transitions == []
    stage, payload = b.shadow[0]
    assert stage == "booked__evOFFERED"
    assert payload["would_calendar"] == {"fold": True, "event_id": "evOFFERED",
                                         "renter": "Owen"}
    assert cal.fmt_showing_time(_start_az(OFFERED_HOUR)) in payload["would_body"]
