"""Address + showing-window primitives, shared by rules.py and scripts/.

This exists for one reason: scripts/shadow_dump.py has to run on Apple's
/usr/bin/python3 (3.9, the only interpreter on Alex's laptop with
firebase_admin installed) and therefore cannot import rules.py, which uses
PEP 604 `X | None` annotations that raise TypeError at import on 3.9. It used
to carry its own hand-copied version of these ~30 lines "kept in sync by
hand", and they drifted: the audit script still had the substring street-number
match that blocked the wrong house and the pre-midnight-fix window check.

So: KEEP THIS FILE 3.9-COMPATIBLE. No `X | None` annotations, no match
statements, nothing newer than 3.9 syntax. Everything here is pure data and
pure functions with zero I/O, so there is nothing else to be careful about.
rules.py re-exports every name below, so `rules.SHOWING_WINDOWS`,
`rules.number_and_core` and friends keep working unchanged.
"""

import re
from datetime import time, timedelta

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
KNOWN_AGENT_NAMES = {a["name"] for a in AGENTS.values()}

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
    return re.search(r"\b" + re.escape(number) + r"\b", haystack) is not None


def matches_address(address, haystack):
    """Same-property-only rule: BOTH street number (as a whole token) and
    street-name core of `address` must appear in `haystack`."""
    number, core = number_and_core((address or "").split(",")[0])
    if not number or not core:
        return False
    hay = (haystack or "").lower()
    return number_present(number, hay) and core in hay


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
