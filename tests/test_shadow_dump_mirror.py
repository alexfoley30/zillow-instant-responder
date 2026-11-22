"""scripts/shadow_dump.py must run on Apple's /usr/bin/python3 (3.9), which
cannot import rules.py (PEP 604 `X | None` annotations raise at import), so it
re-implements ~30 lines of it and its header says they "must be kept in sync
with it". Nothing enforced that, and both copies had silently drifted into the
very bugs rules.py's own comments record:

  * addr_matches tested the street number as a SUBSTRING, so '1641 E Coronado'
    matched '16413 E Coronado' and reported the wrong house as blocked.
  * in_window had no midnight guard, so a 30-minute slot starting 11:30 PM
    wrapped .time() to 00:00 and passed the window-close check.

These tests are the sync check. They compare the mirror against rules.py
directly, so the next edit to either side fails here instead of in an audit.
"""
import importlib.util
import os
import sys
from datetime import datetime, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rules  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts", "shadow_dump.py")


def _load_mirror():
    spec = importlib.util.spec_from_file_location("shadow_dump_under_test",
                                                  _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sd = _load_mirror()


# ---------------------------------------------------------------- constants

def test_showing_windows_match():
    assert sd.SHOWING_WINDOWS == rules.SHOWING_WINDOWS


def test_scalar_constants_match():
    assert sd.MIN_NOTICE_HOURS == rules.MIN_NOTICE_HOURS
    assert sd.SHOWING_MINUTES == rules.SHOWING_MINUTES


def test_address_token_sets_match():
    assert sd._DIRECTIONALS == rules._DIRECTIONALS
    assert sd._STREET_TYPES == rules._STREET_TYPES


def test_known_agents_match_the_roster():
    assert sd.KNOWN_AGENTS == {a["name"] for a in rules.AGENTS.values()}


# ---------------------------------------------------------------- functions

# Blocked-list entries are street-only, the shape zillow_config/properties and
# scripts/sync_facts.py actually store (see EXPECTED_BLOCKED in shadow_dump).
_BLOCKED = [
    "3309 E San Remo Ave",
    "1641 E Coronado Rd",
    "164 E Coronado Rd",
    "22500 N Greenland Park Dr",
    "250 W Queen Creek Rd",
    "1294 E Apricot Ln",
    "no number here",
    "",
]

# Inbound addresses arrive from Zillow subjects with city/state/zip attached.
_ADDRESSES = [
    "3309 E San Remo Ave",
    "3309 E San Remo Ave, Gilbert, AZ 85234",
    "1641 E Coronado Rd, Phoenix, AZ 85006",
    "16413 E Coronado Rd, Phoenix, AZ 85006",
    "1641 W Coronado Road, Phoenix, AZ 85006",
    "22500 N Greenland Park Dr, Maricopa, AZ 85139",
    "250 W Queen Creek Rd #102, Chandler, AZ 85248",
    "1294 E Apricot Ln",
    "no number here",
    "",
]


def test_number_and_core_matches_rules():
    for addr in _BLOCKED + _ADDRESSES:
        assert sd.number_and_core(addr) == rules.number_and_core(addr), addr


def test_addr_matches_matches_is_blocked_address():
    compared = 0
    for blocked in _BLOCKED:
        for address in _ADDRESSES:
            assert sd.addr_matches(blocked, address) == \
                rules.is_blocked_address(address, [blocked]), (blocked, address)
            compared += 1
    assert compared == len(_BLOCKED) * len(_ADDRESSES)


def test_the_cross_product_actually_exercises_both_verdicts():
    """Guard the guard: a pairing list that never matches anything would make
    the parity test above pass no matter how far the mirror drifts."""
    verdicts = {sd.addr_matches(b, a) for b in _BLOCKED for a in _ADDRESSES}
    assert verdicts == {True, False}


def test_street_number_is_not_a_substring_match():
    """The drift this test was written for: 1641 must not block 16413."""
    assert not sd.addr_matches("1641 E Coronado Rd",
                               "16413 E Coronado Rd, Phoenix, AZ 85006")
    assert sd.addr_matches("1641 E Coronado Rd",
                           "1641 E Coronado Rd, Phoenix, AZ 85006")


def _every_half_hour_of_the_week():
    # A full week from a Monday, every :00/:30, including the midnight edges.
    start = datetime(2026, 11, 16, 0, 0, tzinfo=rules.AZ_TZ)
    return [start + timedelta(minutes=30 * i) for i in range(7 * 48)]


def test_in_window_matches_rules_all_week():
    for dt in _every_half_hour_of_the_week():
        assert sd.in_window(dt) == rules.in_window(dt), dt.isoformat()


def test_late_night_slot_is_out_of_window():
    """The drift this test was written for: 11:30 PM is not in window."""
    assert not sd.in_window(datetime(2026, 11, 20, 23, 30, tzinfo=rules.AZ_TZ))
    assert sd.in_window(datetime(2026, 11, 20, 18, 0, tzinfo=rules.AZ_TZ))


def test_in_window_rejects_a_slot_that_would_end_past_close():
    friday_close = rules.SHOWING_WINDOWS[4][1]
    assert friday_close == time(18, 30)
    # 18:30 start would end 19:00, past close, on both sides.
    late = datetime(2026, 11, 20, 18, 30, tzinfo=rules.AZ_TZ)
    assert sd.in_window(late) == rules.in_window(late) is False
