"""scheduling_core is the ONE copy of address matching + showing windows.

rules.py and scripts/shadow_dump.py both used to carry these ~30 lines.
The copies drifted, and the shadow_dump copy still had two bugs this repo
had already fixed in rules.py (substring street numbers, no midnight guard).
These tests pin the dedupe so the copies cannot quietly come back.
"""
import ast
import os
import sys
from datetime import datetime, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rules  # noqa: E402
import scheduling_core as core  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHADOW = os.path.join(ROOT, "scripts", "shadow_dump.py")
SHARED = ("number_and_core", "_number_present", "same_property",
          "is_blocked_address", "addr_slug", "in_window", "addr_matches")


def test_rules_reexports_the_same_objects():
    """Not a copy with the same name - literally the same function object."""
    for name in SHARED:
        assert getattr(rules, name) is getattr(core, name), name
    assert rules.SHOWING_WINDOWS is core.SHOWING_WINDOWS
    assert rules.MIN_NOTICE_HOURS == core.MIN_NOTICE_HOURS
    assert rules.SHOWING_MINUTES == core.SHOWING_MINUTES


def test_shadow_dump_imports_instead_of_mirroring():
    tree = ast.parse(open(SHADOW, encoding="utf-8").read())
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    for name in SHARED:
        assert name not in defined, "shadow_dump re-mirrored %s" % name
    for name in ("SHOWING_WINDOWS", "SHOWING_MINUTES", "_DIRECTIONALS",
                 "_STREET_TYPES"):
        assert name not in assigned, "shadow_dump re-mirrored %s" % name
    imported = {m.module for m in ast.walk(tree) if isinstance(m, ast.ImportFrom)}
    assert "scheduling_core" in imported


def test_scheduling_core_stays_python_39_clean():
    """shadow_dump runs on Apple /usr/bin/python3 (3.9). A PEP 604 `X | None`
    annotation here raises TypeError at import there, which is exactly why
    the second copy existed. Keep stdlib-only too."""
    tree = ast.parse(open(os.path.join(ROOT, "scheduling_core.py"),
                          encoding="utf-8").read())
    for node in ast.walk(tree):
        ann = getattr(node, "annotation", None) or getattr(node, "returns", None)
        assert not isinstance(ann, ast.BinOp), "PEP 604 union breaks 3.9 import"
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.add((node.module or "").split(".")[0])
    assert roots <= {"re", "datetime"}, "non-stdlib import: %s" % roots


# ---- the two bugs the shadow_dump copy still carried --------------------

def test_street_number_is_never_a_substring():
    # shadow_dump's addr_matches used `number in a`: '1641' matched '16413'.
    assert core.addr_matches("1641 E Coronado Rd", "1641 E Coronado Rd, Phoenix")
    assert not core.addr_matches("1641 E Coronado Rd",
                                 "16413 E Coronado Rd, Phoenix, AZ")


def test_late_night_slot_is_out_of_window():
    # shadow_dump's in_window had no midnight guard: a 23:30 start's 30-min
    # end wrapped .time() to 00:00 and passed the window-close check.
    fri_2330 = datetime(2026, 10, 23, 23, 30)
    assert not core.in_window(fri_2330)
    assert core.SHOWING_WINDOWS[fri_2330.weekday()][1] == time(18, 30)
