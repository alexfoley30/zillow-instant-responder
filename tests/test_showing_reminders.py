"""Showing-reminder windows and copy (added 2026-08-29).

Alex asked for two confirm-your-tour emails: one the day before, one about two
hours out. These tests call the production decision function and the production
templates - no inline re-implementations, because a hand-copied test is exactly
how the 8/22 consolidated-cancel "fix" shipped dead (memory: tests-call-real-
functions).
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cron  # noqa: E402
import templates as T  # noqa: E402
from rules import AZ_TZ  # noqa: E402


def az(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=AZ_TZ)


# ------------------------------------------------------------- 2h window

def test_two_hours_out_is_due():
    start = az(2026, 9, 1, 10)
    assert cron.reminder_due(start, az(2026, 9, 1, 8, 30)) == "2h"


def test_just_inside_two_hours_is_due():
    start = az(2026, 9, 1, 10)
    assert cron.reminder_due(start, az(2026, 9, 1, 8, 1)) == "2h"


def test_more_than_two_hours_out_same_day_is_not_due():
    """Same-day booking, 5 hours out: no day-before exists and it is not yet
    2h out, so nothing is owed."""
    start = az(2026, 9, 1, 15)
    assert cron.reminder_due(start, az(2026, 9, 1, 10)) is None


def test_past_showing_is_never_due():
    start = az(2026, 9, 1, 10)
    assert cron.reminder_due(start, az(2026, 9, 1, 10, 1)) is None
    assert cron.reminder_due(start, az(2026, 9, 2, 9)) is None


def test_fresh_booking_skips_the_two_hour_note():
    """Booked 10 minutes ago for a tour 2h out: the confirmation is still on
    their screen, a reminder on top of it is noise."""
    start = az(2026, 9, 1, 10)
    now = az(2026, 9, 1, 8, 30)
    assert cron.reminder_due(start, now, booked_at_az=now - timedelta(minutes=10)) is None


def test_old_booking_still_gets_the_two_hour_note():
    start = az(2026, 9, 1, 10)
    now = az(2026, 9, 1, 8, 30)
    assert cron.reminder_due(start, now,
                             booked_at_az=now - timedelta(hours=30)) == "2h"


# -------------------------------------------------------- day-before window

def test_evening_before_is_due():
    start = az(2026, 9, 1, 10)
    assert cron.reminder_due(start, az(2026, 8, 31, 16)) == "day_before"
    assert cron.reminder_due(start, az(2026, 8, 31, 21)) == "day_before"


def test_afternoon_before_four_is_too_early():
    start = az(2026, 9, 1, 10)
    assert cron.reminder_due(start, az(2026, 8, 31, 15, 59)) is None


def test_two_days_out_is_not_due():
    start = az(2026, 9, 3, 10)
    assert cron.reminder_due(start, az(2026, 8, 31, 20)) is None


def test_same_day_booking_never_gets_a_day_before():
    """The whole point of the calendar-day test: a tour booked this morning
    for this afternoon has no day before to remind on."""
    start = az(2026, 9, 1, 16)
    for hour in (8, 11, 13):
        assert cron.reminder_due(start, az(2026, 9, 1, hour)) is None


def test_evening_before_wins_over_nothing_late_at_night():
    """23:40 the night before a 10am tour is still the day-before window,
    not the 2h window."""
    start = az(2026, 9, 1, 10)
    assert cron.reminder_due(start, az(2026, 8, 31, 23, 40)) == "day_before"


def test_missing_inputs_are_safe():
    assert cron.reminder_due(None, az(2026, 9, 1, 8)) is None
    assert cron.reminder_due(az(2026, 9, 1, 10), None) is None


# ------------------------------------------------------------------ copy

def test_reminders_ask_rather_than_announce():
    """Alex's ask was to CONFIRM the showing still works - both templates have
    to contain a question, or they are just FYIs."""
    day = T.showing_reminder_day_before("Owen", "2118 S El Marino, Mesa, AZ, 85202",
                                        "tomorrow, Tuesday, September 1, at 10:00 AM",
                                        "Rhett Lueck")
    soon = T.showing_reminder_2h("Owen", "2118 S El Marino, Mesa, AZ, 85202",
                                 "today, Tuesday, September 1, at 10:00 AM",
                                 "Rhett Lueck")
    assert "?" in day and "?" in soon


def test_reminders_carry_time_address_and_agent():
    for body in (
        T.showing_reminder_day_before("Owen", "2118 S El Marino", "tomorrow at 10:00 AM", "Rhett Lueck"),
        T.showing_reminder_2h("Owen", "2118 S El Marino", "today at 10:00 AM", "Rhett Lueck"),
    ):
        assert "Owen" in body
        assert "2118 S El Marino" in body
        assert "10:00 AM" in body
        assert "Rhett Lueck will meet you there" in body
        assert T.SIGNATURE in body


def test_alex_meets_them_himself():
    body = T.showing_reminder_2h("Owen", "2118 S El Marino", "today at 10:00 AM",
                                 "Alex Foley")
    assert "I'll meet you there" in body
    assert "Alex Foley will meet you there" not in body


def test_reminders_are_not_the_booking_confirmation():
    """The pre-send review gate blocks near-verbatim repeats. The reminders
    must not read as the confirmation, or every one of them gets blocked."""
    confirm = T.booking_confirmation("Owen", "2118 S El Marino",
                                     "tomorrow at 10:00 AM", "Rhett Lueck")
    day = T.showing_reminder_day_before("Owen", "2118 S El Marino",
                                        "tomorrow at 10:00 AM", "Rhett Lueck")
    soon = T.showing_reminder_2h("Owen", "2118 S El Marino",
                                 "today at 10:00 AM", "Rhett Lueck")
    assert "You're all set" not in day and "You're all set" not in soon
    for a, b in ((confirm, day), (confirm, soon), (day, soon)):
        assert a != b
    # and they differ from each other in their opening line, not just the tail
    assert day.splitlines()[2] != soon.splitlines()[2]


def test_no_em_dashes_in_reminder_body():
    """House rule: the signature's em dash is the only sanctioned one."""
    for body in (
        T.showing_reminder_day_before("Owen", "addr", "when", "Rhett Lueck"),
        T.showing_reminder_2h("Owen", "addr", "when", "Rhett Lueck"),
    ):
        assert "—" not in body.replace(T.SIGNATURE, "")
