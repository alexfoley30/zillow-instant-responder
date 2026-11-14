"""Address matching and showing-window primitives, shared by rules.py and
scripts/shadow_dump.py.

This module exists for one reason: scripts/shadow_dump.py must run on Apple
Python 3.9 (the only interpreter with firebase_admin installed) and therefore
cannot import rules.py, which uses PEP 604 `X | None` annotations. Before
this module the ~30 lines it needed were copy-pasted into shadow_dump with a
"keep in sync with rules.py" comment, and they drifted anyway: the copy
matched street numbers by substring (so 1641 matched 16413, the exact bug
rules.py has a regression test for) and its in_window had no midnight guard.

KEEP THIS FILE 3.9-COMPATIBLE. No `X | None` annotations, no match
statements, no walrus in comprehensions. Stdlib only.
"""

import re
from datetime import timedelta, time

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

DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw",
                 "north", "south", "east", "west"}
STREET_TYPES = {"ave", "avenue", "st", "street", "dr", "drive", "rd", "road",
                 "ln", "lane", "ct", "court", "blvd", "boulevard", "way",
                 "pl", "place", "cir", "circle", "trl", "trail", "pkwy",
                 "parkway", "loop", "ter", "terrace"}


def number_and_core(address):
    """'3309 E San Remo Ave' -> ('3309', 'san remo'); (None, None) if unparseable."""
    tokens = re.findall(r"[a-z0-9']+", (address or "").lower())
    if not tokens or not tokens[0].isdigit():
        return None, None
    core = [t for t in tokens[1:]
            if t not in DIRECTIONALS and t not in STREET_TYPES]
    return tokens[0], " ".join(core)


def number_present(number, haystack):
    """Whole-token street-number match: '1641' must not match inside '16413'
    (regression test caught the substring version blocking the wrong house)."""
    return re.search(r"\b%s\b" % re.escape(number), haystack) is not None


def address_matches(pattern, address):
    """True when `address` names the same house as `pattern`: BOTH the street
    number (as a whole token) and the street-name core must be present."""
    number, core = number_and_core(pattern)
    if not number or not core:
        return False
    a = (address or "").lower()
    return number_present(number, a) and core in a


def is_blocked_address(address, blocked_list):
    a = (address or "").lower()
    for blocked in blocked_list or []:
        if address_matches(blocked, a):
            return True
    return False


def in_window(start_az):
    """True when a SHOWING_MINUTES slot starting at start_az fits inside that
    weekday's showing window."""
    lo, hi = SHOWING_WINDOWS[start_az.weekday()]
    end_dt = start_az + timedelta(minutes=SHOWING_MINUTES)
    # A slot that crosses midnight wrapped .time() to 00:00 and passed the
    # close check - 11:30 PM counted as "in window" (round-2 shadow, Lyndsey).
    if end_dt.date() != start_az.date():
        return False
    # start inside window and the 30-min slot must end by window close
    return lo <= start_az.time() and end_dt.time() <= hi
