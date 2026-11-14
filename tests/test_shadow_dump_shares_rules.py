"""scripts/shadow_dump.py must SHARE rules.py, not re-implement it.

It used to keep a hand-written copy of ~30 lines of rules.py, because it has
to run under Apple's /usr/bin/python3 (3.9) and rules.py could not be
imported there. The copy drifted, in both directions that matter:

  * its in_window() had no midnight-crossing guard, so 11:30 PM read as
    "in window" (the round-2 shadow finding, Lyndsey)
  * its addr_matches() did a substring street-number test, the exact thing
    rules.py replaced with a word-boundary test so that "1641" stops
    matching inside "16413"

Both behaviours are already tested against rules.py in test_rules.py. These
tests guard the sharing itself, so the copy cannot come back quietly.

Source-level, not import-level, on purpose: importing shadow_dump pulls in
firebase_admin and sets GRPC_VERBOSITY/GLOG_minloglevel for the whole
session.
"""
import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import rules  # noqa: E402

RULES_PATH = os.path.join(REPO, "rules.py")
DUMP_PATH = os.path.join(REPO, "scripts", "shadow_dump.py")

# Names shadow_dump needs and must take from rules.py rather than redefine.
SHARED_NAMES = ["number_and_core", "in_window", "is_blocked_address",
                "SHOWING_WINDOWS", "MIN_NOTICE_HOURS", "SHOWING_MINUTES"]


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _tree(path):
    return ast.parse(_read(path))


# -------------------------------------------- rules.py stays 3.9-importable

def test_rules_is_valid_39_syntax():
    """ast refuses 3.10+ syntax when asked for 3.9, so this catches a match
    statement or similar sneaking into rules.py."""
    ast.parse(_read(RULES_PATH), feature_version=(3, 9))


def test_rules_keeps_future_annotations():
    """The one line that lets 3.9 import rules.py despite `X | None`."""
    tree = _tree(RULES_PATH)
    futures = [n for n in tree.body
               if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    assert any(a.name == "annotations" for n in futures for a in n.names), (
        "rules.py lost `from __future__ import annotations`; shadow_dump.py "
        "can no longer import it on Python 3.9")


def test_rules_imports_only_stdlib():
    """A third-party import in rules.py would break shadow_dump just as
    surely as 3.10 syntax: /usr/bin/python3 has only firebase/grpc."""
    tree = _tree(RULES_PATH)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    assert mods <= {"__future__", "re", "datetime", "zoneinfo"}, (
        "rules.py gained imports shadow_dump.py cannot satisfy on 3.9: %s"
        % sorted(mods - {"__future__", "re", "datetime", "zoneinfo"}))


# -------------------------------------------- shadow_dump does not re-fork

def test_shadow_dump_imports_rules():
    tree = _tree(DUMP_PATH)
    assert any(isinstance(n, ast.Import) and any(a.name == "rules" for a in n.names)
               for n in ast.walk(tree)), "shadow_dump.py no longer imports rules"


def _derives_from_rules(node):
    """True when the expression reads something off the `rules` module, i.e.
    `MIN_NOTICE_HOURS = rules.MIN_NOTICE_HOURS` rather than a fresh literal."""
    return any(isinstance(sub, ast.Attribute)
               and isinstance(sub.value, ast.Name) and sub.value.id == "rules"
               for sub in ast.walk(node))


def test_shadow_dump_defines_no_rules_copies():
    """Aliasing off `rules` is fine. Redefining with a fresh literal or a
    fresh function body is how the drift happened."""
    tree = _tree(DUMP_PATH)
    forked = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    for node in tree.body:
        if isinstance(node, ast.Assign) and not _derives_from_rules(node.value):
            forked |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    offenders = sorted(forked & set(SHARED_NAMES))
    assert not offenders, (
        "shadow_dump.py re-implements %s instead of taking them from "
        "rules.py; that copy drifted once already" % offenders)


def test_shadow_dump_is_valid_39_syntax():
    ast.parse(_read(DUMP_PATH), feature_version=(3, 9))


# -------------------------------------------- the two drifted behaviours

def test_blocked_address_number_is_whole_token():
    """The shape shadow_dump's copy had wrong: `number in address`."""
    blocked = ["1641 E Coronado Rd"]
    assert rules.is_blocked_address("1641 E Coronado Rd, Phoenix, AZ", blocked)
    assert not rules.is_blocked_address("16413 E Coronado Rd, Phoenix, AZ", blocked)


def test_in_window_rejects_slot_crossing_midnight():
    """The other shape shadow_dump's copy had wrong: no date check, so the
    30-minute slot's end time wrapped to 00:00 and passed the close test."""
    from datetime import datetime
    assert not rules.in_window(datetime(2026, 8, 3, 23, 30))
