"""Regression tests for rules.py - windows, notice, renter bounds, address
matching. Every named case here is a bug that actually shipped or nearly did.

Run: zillow-venv python -m pytest tests/ -q
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rules  # noqa: E402


# ---------------------------------------------------------------- in_window
# Mon/Wed/Fri 10:00-18:30, Tue/Thu 10:00-15:00, Sat/Sun 10:00-14:00.
# 2026-08-03 is a Monday.

def dt(day, hour, minute=0):
    return datetime(2026, 8, day, hour, minute)


def test_monday_last_slot_exactly_fits():
    assert rules.in_window(dt(3, 18, 0))          # 6:00-6:30 PM ends at close


def test_monday_past_close_rejected():
    assert not rules.in_window(dt(3, 18, 15))     # would end 6:45 PM


def test_midnight_wraparound_rejected():
    # Round-2 shadow bug (Lyndsey): 11:30 PM slot wrapped to 00:00 and PASSED
    # the close check, producing phantom counter-slots.
    assert not rules.in_window(dt(3, 23, 30))


def test_tuesday_short_window():
    assert rules.in_window(dt(4, 14, 30))         # Tue 2:30 ends 3:00 = close
    assert not rules.in_window(dt(4, 14, 45))     # ends 3:15


def test_weekend_window():
    assert rules.in_window(dt(8, 13, 30))         # Sat 1:30 ends 2:00
    assert not rules.in_window(dt(8, 14, 0))      # Sat 2:00 start = close


def test_before_open_rejected():
    assert not rules.in_window(dt(3, 9, 30))


# ------------------------------------------------------------- min notice

def test_two_hour_notice():
    now = dt(3, 12, 0)
    assert rules.min_notice_ok(dt(3, 14, 0), now)
    assert not rules.min_notice_ok(dt(3, 13, 59), now)


# -------------------------------------------------- renter bounds (Luccia)

def test_after_bound_enforced():
    # Luccia said "after 3pm" and got offered 2 PM. Never again.
    assert not rules.respects_renter_bounds(dt(3, 14, 0), "15:00", None)
    assert rules.respects_renter_bounds(dt(3, 15, 0), "15:00", None)


def test_before_bound_uses_slot_end():
    assert rules.respects_renter_bounds(dt(3, 11, 30), None, "12:00")
    assert not rules.respects_renter_bounds(dt(3, 11, 45), None, "12:00")


# ------------------------------------------- address matching (exact keys)

def test_number_and_core():
    assert rules.number_and_core("3309 E San Remo Ave") == ("3309", "san remo")
    assert rules.number_and_core("no leading number") == (None, None)


def test_same_property_requires_both_parts():
    title = "Showing — 1294 E Apricot Ln with Natalie"
    assert rules.same_property("1294 E Apricot Ln, Gilbert, AZ", title)
    assert not rules.same_property("1290 E Apricot Ln", title)
    assert not rules.same_property("1294 E Eugie Ave", title)


def test_blocked_address_exact_match():
    blocked = ["1641 E Coronado Rd", "35031 N Palomino Way"]
    assert rules.is_blocked_address("1641 E Coronado Rd, Phoenix, AZ 85006", blocked)
    assert not rules.is_blocked_address("1642 E Coronado Rd, Phoenix, AZ", blocked)


def test_blocked_address_no_substring_number():
    # The standing no-substring rule: '1641' must not match inside '16413'.
    blocked = ["1641 E Coronado Rd"]
    assert not rules.is_blocked_address("16413 E Coronado Rd, Phoenix, AZ", blocked)


def test_addr_slug():
    assert rules.addr_slug("1641 E Coronado Rd, Phoenix, AZ 85006") == "1641-e-coronado-rd"


# --------------------------------------- weekday snap (8/22 Adam Friday bug)

def _snap(raw, llm_date, now):
    cls = {"time_candidates": [{"raw": raw, "date": llm_date, "time": "13:00",
                                "after": None, "before": None}]}
    rules.snap_weekday_dates(cls, now)
    return cls["time_candidates"][0]


def test_snap_fixes_adam_friday_as_saturday():
    from datetime import datetime
    # Thu Aug 20 2026, 11:24 PM AZ; model resolved "Friday" to Sat 8/22
    now = datetime(2026, 8, 20, 23, 24, tzinfo=rules.AZ_TZ)
    c = _snap("Friday around 1-1:30", "2026-08-22", now)
    assert c["date"] == "2026-08-21"
    assert c.get("weekday_snapped") is True


def test_snap_leaves_correct_weekday_alone():
    from datetime import datetime
    now = datetime(2026, 8, 20, 23, 24, tzinfo=rules.AZ_TZ)
    c = _snap("Saturday morning", "2026-08-22", now)
    assert c["date"] == "2026-08-22"
    assert "weekday_snapped" not in c


def test_snap_ignores_non_weekday_raw():
    from datetime import datetime
    now = datetime(2026, 8, 20, 23, 24, tzinfo=rules.AZ_TZ)
    c = _snap("tomorrow at 2", "2026-08-21", now)
    assert c["date"] == "2026-08-21"
    assert "weekday_snapped" not in c


def test_snap_fills_missing_date_from_weekday():
    from datetime import datetime
    now = datetime(2026, 8, 20, 23, 24, tzinfo=rules.AZ_TZ)
    c = _snap("Sunday works", None, now)
    assert c["date"] == "2026-08-23"


def test_snap_next_weekday_rolls_forward_on_mismatch():
    from datetime import datetime
    # Thursday; model botched "next Friday" to Saturday -> snap rolls a week
    # out instead of tomorrow. A weekday-CONSISTENT model date is left alone.
    now = datetime(2026, 8, 20, 12, 0, tzinfo=rules.AZ_TZ)
    c = _snap("next Friday", "2026-08-22", now)
    assert c["date"] == "2026-08-28"
    c2 = _snap("next Friday", "2026-08-28", now)
    assert c2["date"] == "2026-08-28"
    assert "weekday_snapped" not in c2
