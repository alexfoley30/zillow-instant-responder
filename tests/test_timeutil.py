"""The shared timestamp parser. These tests pin the behaviour the inlined
copies disagreed on: a trailing 'Z', an unparseable string, and what a string
with no UTC offset means (which is deliberately caller-declared, not global)."""

from datetime import datetime, timezone

import timeutil
from timeutil import AZ_TZ, iso_utc, parse_iso, parse_iso_az, to_az


def test_parse_iso_tolerates_trailing_z():
    """Half the old inline copies handled 'Z' and half raised on it."""
    got = parse_iso("2026-09-28T17:30:00Z")
    assert got == datetime(2026, 9, 28, 17, 30, tzinfo=timezone.utc)


def test_parse_iso_keeps_an_explicit_offset():
    got = parse_iso("2026-09-28T10:30:00-07:00")
    assert got.utcoffset().total_seconds() == -7 * 3600


def test_parse_iso_never_raises():
    for bad in (None, "", "not a date", "2026-13-45", 12345, [], {}):
        assert parse_iso(bad) is None


def test_parse_iso_leaves_a_naive_string_naive_by_default():
    """No default assumption: callers that care must say which zone they mean."""
    assert parse_iso("2026-09-28T10:30:00").tzinfo is None


def test_parse_iso_assume_tz_only_fills_a_missing_offset():
    naive = parse_iso("2026-09-28T10:30:00", assume_tz=AZ_TZ)
    assert naive.tzinfo is AZ_TZ
    # An explicit offset wins - assume_tz must never reinterpret it.
    aware = parse_iso("2026-09-28T10:30:00+00:00", assume_tz=AZ_TZ)
    assert aware.utcoffset().total_seconds() == 0


def test_parse_iso_az_reads_a_naive_string_as_utc():
    """Ledger timestamps are UTC; this must not drift with the host's zone."""
    got = parse_iso_az("2026-09-28T17:30:00")
    assert got.tzinfo is AZ_TZ
    assert got.hour == 10  # Arizona is UTC-7 year round


def test_parse_iso_az_converts_an_offset_string():
    assert parse_iso_az("2026-09-28T17:30:00Z").hour == 10
    assert parse_iso_az("bogus") is None


def test_to_az_matches_parse_iso_az_on_naive_input():
    naive = datetime(2026, 9, 28, 17, 30)
    assert to_az(naive) == parse_iso_az(naive.isoformat())
    assert to_az(None) is None


def test_iso_utc_shape_is_what_google_calendar_wants():
    dt = datetime(2026, 9, 28, 10, 30, tzinfo=AZ_TZ)
    assert iso_utc(dt) == "2026-09-28T17:30:00Z"


def test_az_tz_is_one_object_across_the_codebase():
    """rules.AZ_TZ used to be a second, independently constructed ZoneInfo."""
    import calendar_logic
    import responder
    import rules
    assert rules.AZ_TZ is timeutil.AZ_TZ
    assert responder.AZ_TZ is timeutil.AZ_TZ
    assert calendar_logic.AZ_TZ is timeutil.AZ_TZ
