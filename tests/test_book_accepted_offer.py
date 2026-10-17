"""responder.book_accepted_offer — the renter said yes, so this folds them
onto a calendar event and emails the confirmation.

No coverage before this file, despite being one of the two paths that can
put a renter in front of a house at a time nobody is holding. The findings
it has to keep honouring:

  * 2026-08-25 audit: book against the EVENT THAT WAS OFFERED. A showing
    created between the offer and the yes could otherwise hijack the fold,
    confirming a time the renter never agreed to.
  * Natalie + Yesenia, 2026-08-05: a yes to a time Alex arranged by hand
    has no calendar event to fold into. Both got "what day works?" back,
    which reads clueless - it must escalate instead.
  * ATOMIC BOOKING TRIPWIRE: the event is written BEFORE the confirmation
    email, so a failed calendar write never produces a confirmed renter.
  * Fold confirmations used to hardcode Alex while the event said Rhett.

find_existing_showings runs for real against realistic calendar payloads;
only the writes are captured.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import calendar_logic as cal  # noqa: E402
import responder  # noqa: E402

THREAD = "1a0504f3183c6691"
RELAY = "jessica.m.7k2d@convo.zillow.com"
ADDRESS = "2118 S El Marino, Mesa, AZ, 85202"
TRIGGER = "1a0545afd0fe9807"

OFFERED_ID = "ev_offered_3pm"
HIJACK_ID = "ev_created_later"


def _event(event_id, start_utc, agent_first="Rhett"):
    """A showing event shaped the way Composio returns them."""
    return {
        "id": event_id,
        "summary": f"Showing — 2118 S El Marino with Jessica ({agent_first})",
        "location": ADDRESS,
        "description": ("Zillow inquiry.\nInquirers: Jessica.\n"
                        "Agent: Rhett Lueck (rhettlueck@gmail.com)."),
        "start": {"dateTime": start_utc.isoformat()},
        "end": {"dateTime": (start_utc + timedelta(minutes=30)).isoformat()},
    }


class _Harness:
    def __init__(self, monkeypatch, events=None, newer=False,
                 verdict="acquired", takeover=True, alex_replied=False,
                 review=(True, ""), fold_raises=None, send_raises=None,
                 label_raises=None, dry=False, agent_name="Rhett Lueck"):
        self.emails = []
        self.folds = []
        self.labels = []
        self.labels_pending = []
        self.marked_sent = []
        self.marked_failed = []
        self.transitions = []
        self.escalations = []
        self.pokes = []
        self.shadow = []
        self.reserved = []
        self.takeover_calls = 0

        now = datetime.now(timezone.utc)
        self.hijack_start = now + timedelta(hours=6)
        self.offered_start = now + timedelta(hours=28)
        if events is None:
            events = [_event(HIJACK_ID, self.hijack_start),
                      _event(OFFERED_ID, self.offered_start)]
        self._events = events

        def _fold(event, first_name):
            if fold_raises:
                raise fold_raises
            self.folds.append((event.get("id"), first_name))
            return event.get("id")

        def _send_reply(thread_id, relay, body):
            if send_raises:
                raise send_raises
            self.emails.append((thread_id, relay, body))
            return {"successful": True}

        def _modify_labels(thread_id, add, remove):
            if label_raises:
                raise label_raises
            self.labels.append((thread_id, list(add), list(remove)))
            return {"successful": True}

        monkeypatch.setattr(responder, "dry_run", lambda: dry)
        monkeypatch.setattr(responder, "_newer_renter_message_exists",
                            lambda tid, mid: newer)
        monkeypatch.setattr(responder, "review_gate",
                            lambda tid, body, tpl, msgs=None: review)
        monkeypatch.setattr(
            responder, "needs_human",
            lambda tid, name, addr, line, urgent=False:
                self.escalations.append((tid, name, addr, line, urgent)))
        monkeypatch.setattr(cal, "list_events",
                            lambda days=7, time_min=None: list(self._events))
        monkeypatch.setattr(cal, "fold_renter_into_event", _fold)
        # agent_name_from_event stays REAL; the showings ledger is its source.
        monkeypatch.setattr(cal.ledger, "get_showing",
                            lambda eid: {"agent_name": agent_name})
        monkeypatch.setattr(
            responder.ledger, "reserve_send",
            lambda tid, stage, meta: self.reserved.append((tid, stage, dict(meta)))
            or verdict)
        monkeypatch.setattr(
            responder.ledger, "takeover_send",
            lambda tid, stage: self._takeover(takeover))
        monkeypatch.setattr(
            responder.ledger, "mark_sent",
            lambda tid, stage, **f: self.marked_sent.append((tid, stage, f)))
        monkeypatch.setattr(
            responder.ledger, "mark_failed",
            lambda tid, stage, err: self.marked_failed.append((tid, stage, err)))
        monkeypatch.setattr(
            responder.ledger, "set_labels_pending",
            lambda tid, stage, add, rm:
                self.labels_pending.append((tid, stage, list(add), list(rm))))
        monkeypatch.setattr(
            responder.ledger, "transition",
            lambda tid, state, **f: self.transitions.append((state, f)))
        monkeypatch.setattr(
            responder.ledger, "write_shadow",
            lambda tid, stage, payload:
                self.shadow.append((tid, stage, dict(payload))))
        monkeypatch.setattr(responder.ledger, "upsert_thread",
                            lambda tid, **f: None)
        monkeypatch.setattr(responder.ledger, "get_thread",
                            lambda tid: {"renter_name": "Jessica",
                                         "property_address": ADDRESS})
        monkeypatch.setattr(responder.gm, "send_reply", _send_reply)
        monkeypatch.setattr(responder.gm, "modify_labels", _modify_labels)
        monkeypatch.setattr(responder.gm, "poke_ping",
                            lambda msg: self.pokes.append(msg) or True)
        monkeypatch.setattr(responder.gm, "fetch_thread", lambda tid: [])
        monkeypatch.setattr(responder.gm, "alex_replied_after",
                            lambda msgs, mid: alex_replied)

    def _takeover(self, result):
        self.takeover_calls += 1
        return result

    def run(self, doc=None):
        return responder.book_accepted_offer(
            THREAD, doc if doc is not None else {}, "Jessica", ADDRESS,
            RELAY, TRIGGER)

    def when(self, start_utc):
        return cal.fmt_showing_time(start_utc.astimezone(cal.AZ_TZ))


# ------------------------- the 8/25 finding: the OFFERED event is the target

def test_yes_books_the_offered_event_not_the_soonest(monkeypatch):
    h = _Harness(monkeypatch)
    doc = {"state": responder.ledger.OFFERED, "offered_event_id": OFFERED_ID}
    assert h.run(doc) == "sent"

    assert h.folds == [(OFFERED_ID, "Jessica")]
    body = h.emails[0][2]
    assert h.when(h.offered_start) in body
    assert h.when(h.hijack_start) not in body, \
        "a showing created after the offer must not hijack the confirmation"
    assert "You're all set" in body


def test_soonest_showing_is_only_the_fallback(monkeypatch):
    # Thread never recorded what it offered (pre-8/25 docs still exist).
    h = _Harness(monkeypatch)
    assert h.run({"state": responder.ledger.OFFERED}) == "sent"
    assert h.folds == [(HIJACK_ID, "Jessica")]
    assert h.when(h.hijack_start) in h.emails[0][2]


def test_offered_event_gone_from_the_calendar_falls_back(monkeypatch):
    h = _Harness(monkeypatch)
    doc = {"offered_event_id": "ev_deleted_by_hand"}
    assert h.run(doc) == "sent"
    assert h.folds == [(HIJACK_ID, "Jessica")]


# --------------------------------- confirmation names whoever meets them

def test_confirmation_names_the_agent_on_the_event(monkeypatch):
    h = _Harness(monkeypatch, agent_name="Rhett Lueck")
    h.run({"offered_event_id": OFFERED_ID})
    assert "Rhett Lueck will meet you there" in h.emails[0][2]
    assert "I'll meet you there" not in h.emails[0][2]


def test_confirmation_uses_alex_voice_when_alex_is_the_agent(monkeypatch):
    h = _Harness(monkeypatch, agent_name="Alex Foley")
    h.run({"offered_event_id": OFFERED_ID})
    assert "I'll meet you there" in h.emails[0][2]


# ------------------------------------- yes with nothing to fold into (8/5)

def test_accept_with_no_visible_event_escalates_instead_of_asking_again(monkeypatch):
    h = _Harness(monkeypatch, events=[])
    assert h.run({"offered_event_id": OFFERED_ID}) == "no-send:accept-without-event"
    assert h.emails == [] and h.folds == []
    tid, name, addr, line, urgent = h.escalations[0]
    assert (tid, name, addr, urgent) == (THREAD, "Jessica", ADDRESS, True)
    assert "no matching calendar event" in line


def test_calendar_read_failure_escalates_rather_than_booking_blind(monkeypatch):
    h = _Harness(monkeypatch)
    monkeypatch.setattr(cal, "list_events", lambda days=7, time_min=None:
                        (_ for _ in ()).throw(RuntimeError("503 Composio")))
    assert h.run({"offered_event_id": OFFERED_ID}) == "no-send:accept-without-event"
    assert h.emails == [] and h.folds == []
    assert h.escalations[0][4] is True


# ------------------------------------------- a newer message beats the yes

def test_newer_renter_message_stops_the_booking(monkeypatch):
    h = _Harness(monkeypatch, newer=True)
    assert h.run({"offered_event_id": OFFERED_ID}) == \
        "no-send:superseded-by-newer-message"
    assert h.emails == [] and h.folds == []
    assert h.transitions == []
    tid, name, addr, line, urgent = h.escalations[0]
    assert urgent is True and "book by hand" in line


# ---------------------------------------------- the lock still rules here

@pytest.mark.parametrize("verdict,expected", [
    ("already-sent", "skipped:already-sent"),
    ("in-flight", "skipped:in-flight"),
])
def test_lost_lock_neither_folds_nor_emails(monkeypatch, verdict, expected):
    h = _Harness(monkeypatch, verdict=verdict)
    assert h.run({"offered_event_id": OFFERED_ID}) == expected
    assert h.folds == [] and h.emails == []
    assert h.reserved[0][1] == f"booked__{OFFERED_ID}"


def test_recover_defers_to_alexs_own_reply(monkeypatch):
    h = _Harness(monkeypatch, verdict="recover", alex_replied=True)
    assert h.run({"offered_event_id": OFFERED_ID}) == "skipped:recovered"
    assert h.folds == [] and h.emails == []


def test_recover_losing_the_takeover_books_nothing(monkeypatch):
    h = _Harness(monkeypatch, verdict="recover", takeover=False)
    assert h.run({"offered_event_id": OFFERED_ID}) == "skipped:takeover-lost"
    assert h.folds == [] and h.emails == []
    assert h.takeover_calls == 1


def test_review_block_stops_the_fold_too(monkeypatch):
    h = _Harness(monkeypatch, review=(False, "renter never agreed to this time"))
    assert h.run({"offered_event_id": OFFERED_ID}) == "no-send:review-blocked"
    assert h.folds == [] and h.emails == []
    assert "review-blocked" in h.marked_failed[0][2]
    assert h.escalations[0][4] is True


# ------------------------------------------------- ATOMIC BOOKING TRIPWIRE

def test_failed_calendar_write_never_confirms_the_renter(monkeypatch):
    h = _Harness(monkeypatch,
                 fold_raises=RuntimeError("composio fold update ev_offered_3pm "
                                          "failed: 400"))
    assert h.run({"offered_event_id": OFFERED_ID}) == "skipped:book-failed"
    assert h.emails == [], "no confirmation without the calendar write"
    assert h.marked_sent == []
    assert h.transitions == []
    assert "400" in h.marked_failed[0][2]


def test_send_failure_after_the_fold_is_recorded_failed_for_retry(monkeypatch):
    # The event exists but the renter was never told. Recording this failed
    # (not sent) is what lets the content-hash gate retry the thread.
    h = _Harness(monkeypatch, send_raises=RuntimeError("502 Composio upstream"))
    assert h.run({"offered_event_id": OFFERED_ID}) == "skipped:book-failed"
    assert h.folds == [(OFFERED_ID, "Jessica")]
    assert h.marked_sent == []
    assert h.transitions == []
    assert "502 Composio upstream" in h.marked_failed[0][2]


# ------------------------------------------------- state after a real booking

def test_booking_records_state_labels_and_the_fyi_ping(monkeypatch):
    h = _Harness(monkeypatch)
    assert h.run({"offered_event_id": OFFERED_ID}) == "sent"
    assert h.marked_sent == [(THREAD, f"booked__{OFFERED_ID}",
                              {"event_id": OFFERED_ID})]
    assert h.labels == [(THREAD, [responder.HANDLED_LABEL],
                         [responder.AWAITING_LABEL])]
    state, fields = h.transitions[0]
    assert state == responder.ledger.BOOKED
    assert fields["event_id"] == OFFERED_ID
    assert fields["agent"] == "Rhett Lueck"
    assert fields["booked_start_iso"] == \
        h.offered_start.astimezone(cal.AZ_TZ).isoformat()
    assert "Booked (added to existing showing): Jessica" in h.pokes[0]


def test_label_failure_after_the_fold_does_not_resend(monkeypatch):
    h = _Harness(monkeypatch, label_raises=RuntimeError("403 label modify"))
    assert h.run({"offered_event_id": OFFERED_ID}) == "sent"
    assert len(h.emails) == 1 and len(h.folds) == 1
    assert h.labels_pending == [(THREAD, f"booked__{OFFERED_ID}",
                                 [responder.HANDLED_LABEL],
                                 [responder.AWAITING_LABEL])]
    assert h.transitions[0][0] == responder.ledger.BOOKED


def test_dry_run_mirrors_the_fold_without_writing_anything(monkeypatch):
    h = _Harness(monkeypatch, dry=True)
    assert h.run({"offered_event_id": OFFERED_ID}) == "shadowed"
    assert h.folds == [] and h.emails == [] and h.reserved == []
    tid, stage, payload = h.shadow[0]
    assert stage == f"booked__{OFFERED_ID}"
    assert payload["would_calendar"] == {"fold": True,
                                         "event_id": OFFERED_ID,
                                         "renter": "Jessica"}
    assert h.when(h.offered_start) in payload["would_body"]
