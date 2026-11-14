"""Shared scheduling/address primitives — the single source of truth.

Split out of rules.py (2026-09-11) because scripts/shadow_dump.py had hand-copied
~30 lines of it and the two copies drifted: the mirror still matched street
numbers by substring ("1641" matched "16413") and its in_window() was missing the
midnight-crossing guard, so the audit report graded slots by rules production had
already retired.

This module is deliberately Python 3.9-compatible: no PEP 604 (`X | None`)
annotations, no 3.10+ syntax, and stdlib-only imports. shadow_dump.py must run on
/usr/bin/python3 (3.9.6), the only interpreter with firebase_admin installed, and
that is why it could not import rules.py in the first place. Keep it that way —
anything needing newer syntax belongs in rules.py, not here.
"""

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

AZ_TZ = ZoneInfo("America/Phoenix")

# Showing windows (America/Phoenix). weekday(): Mon=0 .. Sun=6
SHOWING_WINDOWS = {
    0: (time(10, 0), time(18, 30)),  # Mon
    1: (time(10, 0), time(15, 0)),   # Tue
    2: (time(10, 0), time(18, 30)),  # Wed
    3: (time(10, 0), time(15, 0)),   # Thu
    4: (time(10, 0), time(18, 30)),  # Fri
    5: (time(10, 0), time(14, 0)),   # Sat
    6: (time(10, 0), time(14, 0)),   # Sun
}

MIN_NOTICE_HOURS = 2          # same-day allowed since 2026-07-27
SHOWING_MINUTES = 30

ALEX = {"name": "Alex Foley", "email": "alex@azfoleyhomes.com"}
JACE = {"name": "Jace Johnson", "email": "jacejohnson.re@gmail.com"}
RHETT = {"name": "Rhett Lueck", "email": "rhettlueck@gmail.com"}
BRIANNA_VIEWER = "azfoleyhomes@gmail.com"  # viewer only, NEVER the agent
AGENTS = {"alex": ALEX, "jace": JACE, "rhett": RHETT}
AGENT_NAMES = frozenset(a["name"] for a in AGENTS.values())

_DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw",
                 "north", "south", "east", "west"}
_STREET_TYPES = {"ave", "avenue", "st", "street", "dr", "drive", "rd", "road",
                 "ln", "lane", "ct", "court", "blvd", "boulevard", "way",
                 "pl", "place", "cir", "circle", "trl", "trail", "pkwy",
                 "parkway", "loop", "ter", "terrace"}


def number_and_core(address):
    """'3309 E San Remo Ave' -> ('3309', 'san remo'); (None, None) if unparseable."""
    tokens = re.findall(r"[a-z0-9']+", (address or "").lower())
    if not tokens or not tokens[0].isdigit():
        return None, None
    core = [t for t in tokens[1:] if t not in _DIRECTIONALS and t not in _STREET_TYPES]
    return tokens[0], " ".join(core)


def number_present(number, haystack):
    """Whole-token street-number match: '1641' must not match inside '16413'
    (regression test caught the substring version blocking the wrong house)."""
    return re.search(r"\b%s\b" % re.escape(number), haystack) is not None


def address_matches(pattern, haystack):
    """Same-property-only rule: BOTH the street number (as a whole token) and
    the street-name core of `pattern` must appear in `haystack`."""
    number, core = number_and_core((pattern or "").split(",")[0])
    if not number or not core:
        return False
    hay = (haystack or "").lower()
    return number_present(number, hay) and core in hay


def addr_slug(address):
    """'1641 E Coronado Rd, Phoenix...' -> '1641-e-coronado-rd' (fact-sheet key)."""
    street = (address or "").split(",")[0].strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", street).strip("-")


def in_window(start_az):
    """True when a SHOWING_MINUTES slot starting at start_az fits that day's window."""
    lo, hi = SHOWING_WINDOWS[start_az.weekday()]
    end_dt = start_az + timedelta(minutes=SHOWING_MINUTES)
    # A slot that crosses midnight wrapped .time() to 00:00 and passed the
    # close check - 11:30 PM counted as "in window" (round-2 shadow, Lyndsey).
    if end_dt.date() != start_az.date():
        return False
    # start inside window and the 30-min slot must end by window close
    return lo <= start_az.time() and end_dt.time() <= hi
