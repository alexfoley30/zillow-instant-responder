"""rules_core.py is the single source of truth for the window/address rules.

scripts/shadow_dump.py used to carry a hand-copied mirror of these ~30 lines and
the copies drifted: the mirror matched street numbers by substring and its
in_window() had no midnight-crossing guard, so the audit report graded shadow
sends against rules production had already retired. These tests pin both
behaviours and assert the script now shares the module rather than copying it.

Run: zillow-venv python -m pytest tests/ -q
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import rules  # noqa: E402
import rules_core  # noqa: E402


def test_street_number_matches_whole_token_only():
    # '1641' must not match inside '16413' - the mirror's `number in a` did.
    assert rules_core.address_matches("1641 E Coronado Rd",
                                      "16413 e coronado rd, phoenix, az") is False
    assert rules_core.address_matches("1641 E Coronado Rd",
                                      "1641 e coronado rd, phoenix, az") is True


def test_in_window_rejects_slot_crossing_midnight():
    # 11:30 PM + 30 min wrapped to 00:00 and passed the close check.
    assert rules_core.in_window(datetime(2026, 8, 3, 23, 30)) is False
    assert rules_core.in_window(datetime(2026, 8, 3, 18, 0)) is True


def test_rules_reexports_the_shared_objects():
    for name in ("in_window", "number_and_core", "addr_slug",
                 "SHOWING_WINDOWS", "MIN_NOTICE_HOURS", "AGENTS"):
        assert getattr(rules, name) is getattr(rules_core, name), name


def test_shadow_dump_imports_instead_of_mirroring():
    import shadow_dump

    assert shadow_dump.in_window is rules_core.in_window
    assert shadow_dump.address_matches is rules_core.address_matches
    assert shadow_dump.AGENT_NAMES is rules_core.AGENT_NAMES
    assert shadow_dump.MIN_NOTICE_HOURS == rules_core.MIN_NOTICE_HOURS
