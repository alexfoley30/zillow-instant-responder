"""Alex books every showing from his own calendar (Alex 2026-10-25: "make all
showings for me moving forward. My team is shit and aren't renting them out",
"reply to everyone using only my calendar"). Calls the real rules and
calendar_logic functions."""
from datetime import datetime

import calendar_logic as cal
import rules

AZ = rules.AZ_TZ
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=AZ)          # Monday 9am
ADDR = "1018 E Marigold Ln, Gilbert, AZ 85234"


def _ev(summary, start, end, location=""):
    return {"summary": summary, "location": location,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()}}


def test_same_day_books_alex_not_rhett():
    start = datetime(2026, 10, 26, 14, 0, tzinfo=AZ)
    assert rules.pick_agent(start, NOW) == rules.ALEX
    v = cal.validate_slot(start, ADDR, [], now_az=NOW)
    assert v["ok"] and v["agent"] == rules.ALEX and v["same_day"]
    assert "jace_cover" not in v          # no cover agent exists any more


def test_next_day_books_alex():
    start = datetime(2026, 10, 27, 11, 0, tzinfo=AZ)
    assert cal.validate_slot(start, ADDR, [], now_az=NOW)["agent"] == rules.ALEX


def test_thread_override_still_wins():
    start = datetime(2026, 10, 26, 14, 0, tzinfo=AZ)
    assert rules.pick_agent(start, NOW, "jace") == rules.JACE


def test_busy_alex_is_not_free_same_day():
    start = datetime(2026, 10, 26, 14, 0, tzinfo=AZ)
    busy = _ev("Dentist", datetime(2026, 10, 26, 13, 45, tzinfo=AZ),
               datetime(2026, 10, 26, 14, 45, tzinfo=AZ))
    v = cal.validate_slot(start, ADDR, [busy], now_az=NOW)
    assert v == {"ok": False, "reason": "alex-busy", "fold": None}


def test_busy_alex_is_not_free_next_day():
    start = datetime(2026, 10, 27, 11, 0, tzinfo=AZ)
    busy = _ev("Owner meeting", datetime(2026, 10, 27, 10, 30, tzinfo=AZ),
               datetime(2026, 10, 27, 11, 30, tzinfo=AZ))
    assert cal.validate_slot(start, ADDR, [busy], now_az=NOW)["reason"] == "alex-busy"


def test_no_drive_time_is_not_free():
    start = datetime(2026, 10, 27, 11, 0, tzinfo=AZ)
    other = _ev("Walkthrough", datetime(2026, 10, 27, 10, 0, tzinfo=AZ),
                datetime(2026, 10, 27, 10, 45, tzinfo=AZ),
                location="4627 E Night Glow Dr, Cave Creek, AZ 85331")
    assert cal.validate_slot(start, ADDR, [other], now_az=NOW)["reason"] == "drive-gap"


def test_same_house_overlap_is_not_a_conflict():
    start = datetime(2026, 10, 27, 11, 15, tzinfo=AZ)
    there = _ev("1018 E Marigold Ln showing", datetime(2026, 10, 27, 11, 0, tzinfo=AZ),
                datetime(2026, 10, 27, 11, 30, tzinfo=AZ), location=ADDR)
    v = cal.validate_slot(start, ADDR, [there], now_az=NOW, ignore_same_house=True)
    assert v["ok"] and v["agent"] == rules.ALEX


def test_any_away_event_blocks_the_day():
    away = {"summary": "Out of town", "start": {"date": "2026-10-27"},
            "end": {"date": "2026-10-28"}}
    assert rules.away_block_status([away]) == "blocked"
    assert rules.away_block_status([]) == "clear"
    start = datetime(2026, 10, 27, 11, 0, tzinfo=AZ)
    assert cal.validate_slot(start, ADDR, [away], now_az=NOW)["reason"] == "away-blocked"


def test_counter_offers_skip_alex_busy_times():
    busy = _ev("Closing", datetime(2026, 10, 26, 11, 0, tzinfo=AZ),
               datetime(2026, 10, 26, 13, 0, tzinfo=AZ))
    slots = cal.counter_slots(ADDR, [busy], now_az=NOW, count=2)
    assert slots
    for s in slots:
        assert not (datetime(2026, 10, 26, 10, 30, tzinfo=AZ) <= s < datetime(2026, 10, 26, 13, 0, tzinfo=AZ))
