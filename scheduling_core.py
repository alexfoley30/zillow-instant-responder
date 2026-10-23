"""Address matching + showing-window primitives. The single source of truth.

Split out of rules.py (2026-10-23) because scripts/shadow_dump.py had to
re-implement these ~30 lines: it runs on Apple /usr/bin/python3 (3.9) and
rules.py uses PEP 604 `X | None` annotations that raise TypeError at import
on 3.9. The two copies drifted and the copy carried bugs this repo had
already fixed once:

  * addr_matches used a substring street-number test, so '1641' matched
    inside '16413' - the exact regression test_rules.py pins.
  * in_window had no midnight guard, so an 11:30 PM start counted as inside
    the window (its 30-min end wrapped .time() to 00:00 and passed the
    close check).

KEEP THIS MODULE PYTHON 3.9 COMPATIBLE: stdlib imports only, and no PEP 604
unions, or shadow_dump.py stops importing again and the copies come back.
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

_DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw",
                 "north", "south", "east", "west"}
_STREET_TYPES = {"ave", "avenue", "st", "street", "dr", "drive", "rd", "road",
                 "ln", "lane", "ct", "court", "blvd", "boulevard", "way",
                 "pl", "place", "cir", "circle", "trl", "trail", "pkwy",
                 "parkway", "loop", "ter", "terrace"}


# ---------------------------------------------------------------- address

def number_and_core(address):
    """'3309 E San Remo Ave' -> ('3309', 'san remo'); (None, None) if unparseable."""
    tokens = re.findall(r"[a-z0-9']+", (address or "").lower())
    if not tokens or not tokens[0].isdigit():
        return None, None
    core = [t for t in tokens[1:] if t not in _DIRECTIONALS and t not in _STREET_TYPES]
    return tokens[0], " ".join(core)


def _number_present(number, haystack):
    """Whole-token street-number match: '1641' must not match inside '16413'
    (regression test caught the substring version blocking the wrong house)."""
    return re.search(r"\b%s\b" % re.escape(number), haystack) is not None


def addr_matches(address, haystack):
    """Same-property-only rule: BOTH street number and street-name core present."""
    number, core = number_and_core((address or "").split(",")[0])
    if not number or not core:
        return False
    hay = (haystack or "").lower()
    return _number_present(number, hay) and core in hay


def same_property(address, haystack):
    return addr_matches(address, haystack)


def is_blocked_address(address, blocked_list):
    for blocked in blocked_list or []:
        if addr_matches(blocked, address):
            return True
    return False


def addr_slug(address):
    """'1641 E Coronado Rd, Phoenix...' -> '1641-e-coronado-rd' (fact-sheet key)."""
    street = (address or "").split(",")[0].strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", street).strip("-")


# ---------------------------------------------------------------- windows

def in_window(start_az):
    lo, hi = SHOWING_WINDOWS[start_az.weekday()]
    end_dt = start_az + timedelta(minutes=SHOWING_MINUTES)
    # A slot that crosses midnight wrapped .time() to 00:00 and passed the
    # close check - 11:30 PM counted as "in window" (round-2 shadow, Lyndsey).
    if end_dt.date() != start_az.date():
        return False
    # start inside window and the 30-min slot must end by window close
    return lo <= start_az.time() and end_dt.time() <= hi
