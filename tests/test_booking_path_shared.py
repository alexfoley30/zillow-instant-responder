"""The three booking paths (accept-an-offer fold, adjacency-snap fold, create)
now run through one routine, responder._book_and_confirm, so they can no
longer drift apart. These tests pin the contract the three copies used to
state differently.

Two of them cover real drift the copies had accumulated:

  * the snap fold's BOOKED transition carried only event_id, so a snapped
    tour reached cron._showing_reminders with no booked_start_iso and had to
    be healed out of zillow_showings;
  * book_accepted_offer wrapped the calendar fold and the Gmail send in ONE
    try and recorded "skipped:book-failed" for either - a prefix
    ledger._thread_send_failed_since does not know, so a renter whose event
    existed but whose confirmation never sent stayed behind the dup-content
    gate on every /reprocess (the U3 strand, fixed for the other two paths
    in 2026-11 and missed here).
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import ledger  # noqa: E402
import responder  # noqa: E402
import rules  # noqa: E402

AZ = rules.AZ_TZ
ADDR = "2118 S El Marino, Mesa, AZ, 85202"
RELAY = "x@convo.zillow.com"


def _start_az():
    d = datetime.now(AZ).date() + timedelta(days=9)
    return datetime(d.year, d.month, d.day, 14, 0, tzinfo=AZ)


def _hit(start_az):
    return {"event": {"id": "evC", "description": "Zillow inquiry."},
            "start_az": start_az,
            "end_az": start_az + timedelta(minutes=30),
            "when_human": "Thursday, at 2:00 PM"}


def _wire(monkeypatch, *, fold=None, send=None):
    """Everything side-effectful recorded instead of executed."""
    calls = {"transitions": [], "failed": [], "sent": [], "replies": [],
             "folded": []}

    def _fold(event, name):
        calls["folded"].append((event["id"], name))
        if fold:
            raise fold
        return event["id"]

    def _send(thread_id, relay, body):
        if send:
            raise send
        calls["replies"].append(body)

    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(responder, "review_gate", lambda t, b, k: (True, ""))
    monkeypatch.setattr(responder, "_newer_renter_message_exists",
                        lambda t, m: False)
    monkeypatch.setattr(responder, "needs_human",
                        lambda *a, **k: calls.setdefault("nh", []).append(a))
    led = responder.ledger
    monkeypatch.setattr(led, "reserve_send", lambda t, s, m: "acquired")
    monkeypatch.setattr(led, "takeover_send", lambda t, s: True)
    monkeypatch.setattr(led, "mark_sent",
                        lambda t, s, **f: calls["sent"].append((s, f)))
    monkeypatch.setattr(led, "mark_failed",
                        lambda t, s, err: calls["failed"].append((s, err)))
    monkeypatch.setattr(led, "set_labels_pending", lambda *a, **k: None)
    monkeypatch.setattr(led, "transition",
                        lambda t, st, **f: calls["transitions"].append((st, f)))
    monkeypatch.setattr(responder.cal, "fold_renter_into_event", _fold)
    monkeypatch.setattr(responder.cal, "agent_name_from_event",
                        lambda e: "Rhett Lueck")
    monkeypatch.setattr(responder.gm, "send_reply", _send)
    monkeypatch.setattr(responder.gm, "modify_labels", lambda *a, **k: None)
    monkeypatch.setattr(responder.gm, "poke_ping", lambda m: True)
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: [])
    return calls


def _accept(monkeypatch, calls, start_az):
    monkeypatch.setattr(responder.cal, "find_existing_showings",
                        lambda a, **k: [_hit(start_az)])
    return responder.book_accepted_offer(
        "t1", {"offered_event_id": "evC"}, "Sam", ADDR, RELAY, "m9")


# ------------------------------------------- the fold path, happy and broken

def test_accepted_offer_books_and_stamps_the_whole_transition(monkeypatch):
    start_az = _start_az()
    calls = _wire(monkeypatch)
    assert _accept(monkeypatch, calls, start_az) == "sent"
    assert calls["folded"] == [("evC", "Sam")]
    state, fields = calls["transitions"][0]
    assert state == ledger.BOOKED
    assert fields["event_id"] == "evC"
    assert fields["booked_start_iso"] == start_az.isoformat()
    assert fields["agent"] == "Rhett Lueck"


def test_calendar_refusal_never_emails_a_confirmation(monkeypatch):
    calls = _wire(monkeypatch, fold=RuntimeError("composio 400"))
    out = _accept(monkeypatch, calls, _start_az())
    assert out == "skipped:event-failed"
    assert calls["replies"] == [] and calls["transitions"] == []


def test_send_failure_after_the_fold_stays_retryable(monkeypatch):
    """The event exists but the renter never heard: the recorded outcome must
    be one ledger's dup-content exemption recognises, or /reprocess can never
    deliver the confirmation."""
    calls = _wire(monkeypatch, send=RuntimeError("gmail 503"))
    out = _accept(monkeypatch, calls, _start_az())
    assert out == "skipped:send-failed-event-created"
    assert calls["folded"] == [("evC", "Sam")]   # calendar write did land
    assert calls["transitions"] == []            # not BOOKED on a silent fail
    stage, err = calls["failed"][0]
    assert "event_id=evC" in err                 # the event to clean up
    assert ledger._FAILED_SEND_PREFIXES and out.startswith(
        ledger._FAILED_SEND_PREFIXES)


# ------------------------------------------- the snap fold path

def test_snap_fold_stamps_the_showing_time_and_agent(monkeypatch):
    """cron._showing_reminders reads booked_start_iso off the thread; the
    snap fold used to leave it unset."""
    start_az = _start_az()
    calls = _wire(monkeypatch)
    monkeypatch.setattr(responder.cal, "find_existing_showings",
                        lambda a, **k: [_hit(start_az)])
    out = responder._book_valid_slot(
        "t1", {}, "Sam", ADDR, RELAY,
        start_az + timedelta(minutes=3),            # 3 min apart -> fold
        {"agent": rules.ALEX, "same_day": False}, [], "m9", "booked__m9")
    assert out == "sent"
    assert calls["folded"] == [("evC", "Sam")]
    state, fields = calls["transitions"][0]
    assert state == ledger.BOOKED
    assert fields["event_id"] == "evC"
    assert fields["booked_start_iso"] == start_az.isoformat()
    assert fields["agent"] == "Rhett Lueck"
    assert fields["snap_offered"] is True


# ------------------------------------------- one contract, all three paths

def test_every_booking_failure_outcome_lifts_the_dup_gate():
    """Each terminal failure the shared routine can return has to be a prefix
    ledger._thread_send_failed_since knows, or the renter is stranded."""
    for outcome in ("skipped:event-failed", "skipped:send-failed-event-created"):
        assert outcome.startswith(ledger._FAILED_SEND_PREFIXES), outcome
