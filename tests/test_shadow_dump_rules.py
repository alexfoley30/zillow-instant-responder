"""scripts/shadow_dump.py must audit against rules.py, never a copy of it.

It used to carry a hand-mirrored ~30 lines because it runs on Apple's 3.9,
and the mirror drifted back into two bugs rules.py had already fixed:

  * a substring street-number test, so "1641 E Coronado" matched "16413 E
    Coronado" and the audit blamed the wrong house;
  * a window check with no midnight guard, so an 11:30 PM slot passed.

Both are asserted below against the old implementations, so if anyone
re-copies the rules the failure says exactly what breaks.
"""
import os
import re
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import rules  # noqa: E402

SHADOW = os.path.join(ROOT, "scripts", "shadow_dump.py")


# ------------------------------------------- the drift, pinned as behaviour

def _old_number_and_core(address):
    tokens = re.findall(r"[a-z0-9']+", (address or "").lower())
    if not tokens or not tokens[0].isdigit():
        return None, None
    return tokens[0], " ".join(
        t for t in tokens[1:]
        if t not in rules._DIRECTIONALS and t not in rules._STREET_TYPES)


def _old_addr_matches(blocked, address):
    """The copy shadow_dump used to carry: `number in a`, not a whole token."""
    number, core = _old_number_and_core(blocked)
    if not number or not core:
        return False
    a = (address or "").lower()
    return number in a and core in a


def _old_in_window(start):
    """The copy shadow_dump used to carry: no midnight-crossing guard."""
    lo, hi = rules.SHOWING_WINDOWS[start.weekday()]
    return lo <= start.time() and (start + timedelta(minutes=30)).time() <= hi


def test_street_number_is_a_whole_token_not_a_substring():
    blocked = "1641 E Coronado Rd"
    neighbour = "16413 E Coronado Rd, Phoenix, AZ 85006"
    assert _old_addr_matches(blocked, neighbour), "old copy had the bug"
    assert not rules.is_blocked_address(neighbour, [blocked])
    # and the actual blocked house still matches
    assert rules.is_blocked_address("1641 E Coronado Rd, Phoenix", [blocked])


def test_a_slot_crossing_midnight_is_out_of_window():
    late = datetime(2026, 9, 9, 23, 30)          # Wednesday 11:30 PM
    assert _old_in_window(late), "old copy had the bug"
    assert not rules.in_window(late)
    assert rules.in_window(datetime(2026, 9, 9, 14, 0))   # a real slot


# ------------------------------------------------- the copy stays gone

def test_shadow_dump_defines_no_private_copy_of_the_rules():
    src = open(SHADOW).read()
    for name in ("number_and_core", "addr_matches", "in_window",
                 "SHOWING_WINDOWS", "_DIRECTIONALS", "_STREET_TYPES"):
        assert f"def {name}(" not in src, f"shadow_dump re-defines {name}"
        assert not re.search(rf"^{name} = (?!rules\.)", src, re.M), \
            f"shadow_dump re-declares {name} instead of taking it from rules"


def test_rules_stays_importable_on_python_39():
    """shadow_dump runs on Apple's 3.9. Annotations must stay deferred and
    the syntax must stay 3.9-parseable, or the import dies at startup."""
    import ast
    src = open(os.path.join(ROOT, "rules.py")).read()
    assert "from __future__ import annotations" in src
    ast.parse(src, feature_version=(3, 9))


def test_shadow_dump_uses_the_real_rule_functions():
    pytest.importorskip("firebase_admin")
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import shadow_dump
    assert shadow_dump.in_window is rules.in_window
    assert shadow_dump.MIN_NOTICE_HOURS is rules.MIN_NOTICE_HOURS
    assert shadow_dump._STREET_TYPES is rules._STREET_TYPES
    # roster is derived, so adding an agent in rules.py updates the audit too
    assert shadow_dump.KNOWN_AGENTS == {a["name"] for a in rules.AGENTS.values()}
