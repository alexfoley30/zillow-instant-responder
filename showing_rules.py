"""Address matching + showing windows, shared by rules.py and scripts/.

This module is deliberately PLAIN: no PEP 604 (`X | None`) annotations, no
dataclasses, nothing newer than Python 3.7 syntax. scripts/shadow_dump.py
must run under /usr/bin/python3 (3.9.6, the only interpreter here with
firebase_admin installed), and that is why these ~40 lines used to be
hand-mirrored into that script - where they then drifted back into two bugs
rules.py had already fixed (a substring street-number match, and a slot that
crossed midnight counting as in-window). One copy, importable by both.

Keep it dependency-free: importing this must never pull in ledger, llm,
gmail_client or anything with a modern-syntax annotation.
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


def address_matches(address, haystack):
    """Same-property-only rule: BOTH street number and street-name core present."""
    number, core = number_and_core((address or "").split(",")[0])
    if not number or not core:
        return False
    hay = (haystack or "").lower()
    return number_present(number, hay) and core in hay


def in_window(start_az):
    """True when a 30-minute showing starting at `start_az` fits that day's
    window. A slot that crosses midnight wrapped .time() to 00:00 and passed
    the close check - 11:30 PM counted as "in window" (round-2 shadow,
    Lyndsey)."""
    lo, hi = SHOWING_WINDOWS[start_az.weekday()]
    end_dt = start_az + timedelta(minutes=SHOWING_MINUTES)
    if end_dt.date() != start_az.date():
        return False
    # start inside window and the 30-min slot must end by window close
    return lo <= start_az.time() and end_dt.time() <= hi
