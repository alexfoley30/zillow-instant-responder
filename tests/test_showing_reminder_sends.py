"""The reminder SEND loop: cron._showing_reminders.

tests/test_showing_reminders.py covers `reminder_due` and the two templates in
isolation. The loop that actually mails booked renters had no test, so nothing
held down the three things it decides:

  * a reminder must never go to a renter whose home is already leased
    ("home is gone; a reminder would summon them to nothing");
  * it must name the agent on the BOOKING, not a stale `agent` field on the
    thread doc (the 10/23 stale-Rhett reminder);
  * a BOOKED thread with no `booked_start_iso` must heal from zillow_showings
    rather than silently skip the one renter with an upcoming tour
    (Jessica, 8/28: "booked-manual-alex-approved", event_id set, no start).

The real `responder.send_stage` runs, so the asserted body is the body that
would reach Composio.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import calendar_logic as cal  # noqa: E402
import cron  # noqa: E402
import ledger  # noqa: E402
import rules  # noqa: E402
import templates as T  # noqa: E402
from _fake_firestore import FakeDB, Snap  # noqa: E402

AZ = rules.AZ_TZ

# Tuesday 2026-12-01, 10:00 AM Arizona - inside the Tue 10:00-15:00 window.
START = datetime(2026, 12, 1, 10, 0, tzinfo=AZ)
EVENING_BEFORE = datetime(2026, 11, 30, 17, 0, tzinfo=AZ)   # Mon 5pm
TWO_HOURS_OUT = datetime(2026, 12, 1, 8, 30, tzinfo=AZ)
AFTERNOON_BEFORE = datetime(2026, 11, 30, 13, 0, tzinfo=AZ)  # before the 4pm pass

OPEN_ADDR = "2701 E Willetta St, Phoenix, AZ, 85008"
# scripts/sync_facts.py BLOCKED_ADDRESSES: "leased 2026-08-09 ($3,249)".
LEASED_ADDR = "1294 E Apricot Ln, Gilbert, AZ 85298"
RELAY = "owen+58y56j72rjw@convo.zillow.com"
EVENT_ID = "7ek3rk9f2z20p"


def _booked(address=OPEN_ADDR, event_id=EVENT_ID, start_iso=START.isoformat(),
            **extra):
    doc = {"state": ledger.BOOKED, "renter_name": "Owen",
           "property_address": address, "relay_email": RELAY,
           "event_id": event_id, "booked_start_iso": start_iso,
           # booked two days ago: well past the 90-minute fresh-booking mute
           "last_action_at": (START - timedelta(days=2)).astimezone(timezone.utc)}
    doc.update(extra)
    return {k: v for k, v in doc.items() if v is not None}


def _wire(monkeypatch, now, threads, showings=None):
    import responder

    calls = {"sends": [], "stages": [], "upserts": [], "metas": []}
    showings = showings or {}

    class FakeNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.astimezone(timezone.utc)

    monkeypatch.setattr(cron, "datetime", FakeNow)
    monkeypatch.setattr(cal, "datetime", FakeNow)   # fmt_showing_time's today/tomorrow
    monkeypatch.setattr(cron.ledger, "init_db",
                        lambda: FakeDB({ledger.BOOKED: list(threads)}))
    monkeypatch.setattr(cron.ledger, "blocked_addresses",
                        lambda: ["1294 E Apricot Ln", "3309 E San Remo Ave"])
    monkeypatch.setattr(cron.ledger, "get_showing", lambda eid: showings.get(eid))
    monkeypatch.setattr(cron.ledger, "upsert_thread",
                        lambda tid, **f: calls["upserts"].append((tid, f)))
    monkeypatch.setattr(cron.gm, "fetch_thread", lambda tid: [])

    def reserve(tid, stage, meta):
        calls["stages"].append(stage)
        calls["metas"].append(meta)
        return "acquired"

    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(responder, "review_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cron.ledger, "reserve_send", reserve)
    monkeypatch.setattr(cron.ledger, "mark_sent", lambda *a, **k: None)
    monkeypatch.setattr(cron.ledger, "mark_failed", lambda *a, **k: None)
    monkeypatch.setattr(cron.gm, "send_reply",
                        lambda tid, relay, body: calls["sends"].append(
                            (tid, relay, body)))
    monkeypatch.setattr(cron.gm, "modify_labels", lambda *a, **k: None)
    return calls


# ------------------------------------------------------------- the two sends

def test_evening_before_sends_the_day_before_body_with_the_real_time(monkeypatch):
    calls = _wire(monkeypatch, EVENING_BEFORE, [Snap("t-owen", _booked())],
                  showings={EVENT_ID: {"agent_name": "Alex Foley"}})
    cron._showing_reminders()

    when = "tomorrow, Tuesday, December 1, at 10:00 AM"
    assert calls["sends"] == [("t-owen", RELAY, T.showing_reminder_day_before(
        "Owen", OPEN_ADDR, when, "Alex Foley"))]
    body = calls["sends"][0][2]
    assert when in body
    assert OPEN_ADDR in body
    assert "Quick reminder about your tour" in body
    assert "Does that still work on your end?" in body
    # Keyed on the event, so a reschedule earns its own pair of reminders.
    assert calls["stages"] == [f"reminder_day__{EVENT_ID}"]
    assert calls["metas"][0]["event_id"] == EVENT_ID


def test_two_hours_out_sends_the_shorter_body(monkeypatch):
    calls = _wire(monkeypatch, TWO_HOURS_OUT, [Snap("t-owen", _booked())],
                  showings={EVENT_ID: {"agent_name": "Alex Foley"}})
    cron._showing_reminders()

    when = "today, Tuesday, December 1, at 10:00 AM"
    assert calls["sends"] == [("t-owen", RELAY, T.showing_reminder_2h(
        "Owen", OPEN_ADDR, when, "Alex Foley"))]
    assert "Coming up in a couple hours" in calls["sends"][0][2]
    assert calls["stages"] == [f"reminder_2h__{EVENT_ID}"]


def test_nothing_is_owed_before_the_four_pm_pass(monkeypatch):
    calls = _wire(monkeypatch, AFTERNOON_BEFORE, [Snap("t-owen", _booked())],
                  showings={EVENT_ID: {"agent_name": "Alex Foley"}})
    cron._showing_reminders()
    assert calls["sends"] == [] and calls["stages"] == []


def test_a_tour_booked_minutes_ago_gets_no_two_hour_note(monkeypatch):
    """The confirmation is still on their screen."""
    doc = _booked(last_action_at=(TWO_HOURS_OUT - timedelta(minutes=10))
                  .astimezone(timezone.utc))
    calls = _wire(monkeypatch, TWO_HOURS_OUT, [Snap("t-owen", doc)],
                  showings={EVENT_ID: {"agent_name": "Alex Foley"}})
    cron._showing_reminders()
    assert calls["sends"] == []


# ------------------------------------------- who the renter is told to expect

def test_reminder_names_the_agent_on_the_booking_not_the_thread_doc(monkeypatch):
    """The 10/23 stale-Rhett reminder: the thread doc still said Rhett after
    the showing was reassigned, and the renter was told to expect him."""
    calls = _wire(monkeypatch, EVENING_BEFORE,
                  [Snap("t-owen", _booked(agent="Rhett Lueck"))],
                  showings={EVENT_ID: {"agent_name": "Alex Foley"}})
    cron._showing_reminders()

    body = calls["sends"][0][2]
    assert "I'll meet you there" in body
    assert "Rhett" not in body


def test_a_cover_agent_is_named_in_full(monkeypatch):
    """Jace and Rhett cover Alex's conflicts again since 2026-11-04."""
    calls = _wire(monkeypatch, EVENING_BEFORE, [Snap("t-owen", _booked())],
                  showings={EVENT_ID: {"agent_name": "Jace Johnson"}})
    cron._showing_reminders()

    body = calls["sends"][0][2]
    assert "Jace Johnson will meet you there" in body
    assert "I'll meet you there" not in body


def test_agent_falls_back_to_alex_when_nothing_records_one(monkeypatch):
    calls = _wire(monkeypatch, EVENING_BEFORE, [Snap("t-owen", _booked())],
                  showings={EVENT_ID: {}})
    cron._showing_reminders()
    assert "I'll meet you there" in calls["sends"][0][2]


# --------------------------------------------------------- the leased guard

def test_no_reminder_for_a_home_that_has_been_leased(monkeypatch):
    """A reminder would summon the renter to a house that is gone."""
    calls = _wire(monkeypatch, EVENING_BEFORE,
                  [Snap("t-owen", _booked(address=LEASED_ADDR))],
                  showings={EVENT_ID: {"agent_name": "Alex Foley"}})
    cron._showing_reminders()

    assert calls["sends"] == []
    assert calls["stages"] == []


def test_leased_guard_does_not_swallow_the_rest_of_the_queue(monkeypatch):
    calls = _wire(monkeypatch, EVENING_BEFORE, [
        Snap("t-gone", _booked(address=LEASED_ADDR, event_id="evGone")),
        Snap("t-owen", _booked()),
    ], showings={EVENT_ID: {"agent_name": "Alex Foley"},
                 "evGone": {"agent_name": "Alex Foley"}})
    cron._showing_reminders()

    assert [tid for tid, _, _ in calls["sends"]] == ["t-owen"]


# ------------------------------------------------- the Jessica 8/28 backfill

def test_missing_booked_start_is_healed_from_the_showings_ledger(monkeypatch):
    """Manual Poke-approved bookings reach BOOKED with event_id but no start.
    zillow_showings knows when the tour is, so read it there and heal."""
    doc = _booked(start_iso=None, agent=None)
    assert "booked_start_iso" not in doc
    calls = _wire(monkeypatch, EVENING_BEFORE, [Snap("t-jessica", doc)],
                  showings={EVENT_ID: {"start_iso": START.isoformat(),
                                       "agent_name": "Alex Foley"}})
    cron._showing_reminders()

    assert ("t-jessica", {"booked_start_iso": START.isoformat(),
                          "agent": "Alex Foley"}) in calls["upserts"]
    assert len(calls["sends"]) == 1
    assert "tomorrow, Tuesday, December 1, at 10:00 AM" in calls["sends"][0][2]


def test_no_showing_time_anywhere_sends_nothing(monkeypatch):
    calls = _wire(monkeypatch, EVENING_BEFORE,
                  [Snap("t-ghost", _booked(start_iso=None))],
                  showings={EVENT_ID: {}})
    cron._showing_reminders()
    assert calls["sends"] == [] and calls["upserts"] == []


def test_an_unparseable_booked_start_is_logged_not_raised(monkeypatch):
    calls = _wire(monkeypatch, EVENING_BEFORE,
                  [Snap("t-bad", _booked(start_iso="next tuesday at ten")),
                   Snap("t-owen", _booked())],
                  showings={EVENT_ID: {"agent_name": "Alex Foley"}})
    cron._showing_reminders()
    assert [tid for tid, _, _ in calls["sends"]] == ["t-owen"]


def test_booked_thread_with_no_event_is_skipped(monkeypatch):
    calls = _wire(monkeypatch, EVENING_BEFORE,
                  [Snap("t-noev", _booked(event_id=None))])
    cron._showing_reminders()
    assert calls["sends"] == []


# -------------------------------------------------------------- other skips

def test_alex_owned_booking_gets_no_reminder(monkeypatch):
    calls = _wire(monkeypatch, EVENING_BEFORE,
                  [Snap("t-owen", _booked(alex_owned=True))],
                  showings={EVENT_ID: {"agent_name": "Alex Foley"}})
    cron._showing_reminders()
    assert calls["sends"] == []


def test_a_thread_with_no_relay_is_skipped(monkeypatch):
    calls = _wire(monkeypatch, EVENING_BEFORE,
                  [Snap("t-owen", _booked(relay_email=None))],
                  showings={EVENT_ID: {"agent_name": "Alex Foley"}})
    cron._showing_reminders()
    assert calls["sends"] == []
