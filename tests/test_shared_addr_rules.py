"""The shadow-dump script and the service must share ONE copy of the address
and window rules.

scripts/shadow_dump.py runs on Apple's python3.9 and cannot import rules.py
(PEP 604 annotations), so it used to hand-copy ~30 lines of it under a "keep
in sync" comment. They drifted, and both bugs the originals were written to
fix came back in the copy:

  * street-number matching went back to substring, so the leased-house audit
    matched 1641 E Coronado against 16413 E Coronado
  * in_window lost the midnight-crossing guard, so an 11:30 PM slot read as
    inside Monday's window

These tests fail if either regression returns, and if either file grows its
own private copy again.
"""
import ast
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import addr_rules  # noqa: E402
import rules  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHADOW_DUMP = os.path.join(REPO, "scripts", "shadow_dump.py")
SYNC_FACTS = os.path.join(REPO, "scripts", "sync_facts.py")

SHARED = ("number_and_core", "number_present", "same_property", "addr_slug",
          "is_blocked_address", "in_window", "street_of")


def _defined_names(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def _assigned_names(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            out.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return out


# ------------------------------------------------- one copy, not two

def test_rules_reexports_the_shared_functions():
    for name in SHARED:
        assert getattr(rules, name) is getattr(addr_rules, name), name


def test_shadow_dump_does_not_redefine_the_shared_rules():
    defined = _defined_names(SHADOW_DUMP)
    assert not defined & set(SHARED), sorted(defined & set(SHARED))


def test_shadow_dump_does_not_keep_its_own_window_or_street_tables():
    assigned = _assigned_names(SHADOW_DUMP)
    for name in ("SHOWING_WINDOWS", "SHOWING_MINUTES", "MIN_NOTICE_HOURS",
                 "_DIRECTIONALS", "_STREET_TYPES"):
        assert name not in assigned, name


def test_leased_list_is_not_copied_into_the_scripts():
    """Both scripts audit/publish the SAME leased list. shadow_dump's own copy
    had gone stale at 4 of 13 addresses, so nine leased homes were outside the
    exposure audit entirely."""
    addr_re = re.compile(r'"\d+ [NSEW]? ?[A-Z][A-Za-z]')
    for path in (SHADOW_DUMP, SYNC_FACTS):
        body = open(path, encoding="utf-8").read()
        assert not addr_re.search(body), path
    assert len(addr_rules.LEASED_PROPERTIES) == len(addr_rules.BLOCKED_ADDRESSES)
    assert "1641 E Coronado Rd" in addr_rules.BLOCKED_ADDRESSES


# ------------------------------------------------- the two drifted bugs

def test_street_number_match_is_whole_token():
    """'1641' must not match inside '16413' (the wrong-house block)."""
    assert rules.is_blocked_address("1641 E Coronado Rd, Phoenix",
                                    ["1641 E Coronado Rd"])
    assert not rules.is_blocked_address("16413 E Coronado Rd, Phoenix",
                                        ["1641 E Coronado Rd"])


def test_window_rejects_a_slot_that_crosses_midnight():
    """Lyndsey, round-2 shadow: 11:30 PM wrapped to 00:00 and read as open."""
    assert not rules.in_window(datetime(2026, 11, 2, 23, 30))   # Monday
    assert rules.in_window(datetime(2026, 11, 2, 10, 0))


def test_street_of_strips_everything_after_the_first_comma():
    assert rules.street_of("1641 E Coronado Rd, Phoenix, AZ 85006") == \
        "1641 E Coronado Rd"
    assert rules.street_of(None) == ""
