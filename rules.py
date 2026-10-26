"""Pure business rules — zero I/O, fully unit-testable.

Ports the scheduling rules from the sweep SKILL.md into code:
windows, 2-hour minimum notice, SAME-DAY LADDER (Jace default), drive-time
gap tiers, away-block detection, same-property matching, agent selection.
"""

# PEP 563: keeps every annotation a string so this module imports cleanly on
# Python 3.9, which evaluates `str | None` at def time and raises TypeError.
# scripts/shadow_dump.py runs on Apple 3.9 and imports this module, so the
# future import is load-bearing, not cosmetic (tests/test_rules.py locks it).
from __future__ import annotations

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

_DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw",
                 "north", "south", "east", "west"}
# Public: scripts/shadow_dump.py also checks parsed first names against it.
STREET_TYPES = {"ave", "avenue", "st", "street", "dr", "drive", "rd", "road",
                 "ln", "lane", "ct", "court", "blvd", "boulevard", "way",
                 "pl", "place", "cir", "circle", "trl", "trail", "pkwy",
                 "parkway", "loop", "ter", "terrace"}

_AWAY_RE = re.compile(
    r"(out of town|ooo|out of office|vacation|trip\b|stay at|travel|hotel|"
    r"lodge|resort)", re.IGNORECASE)


def number_and_core(address: str):
    """'3309 E San Remo Ave' -> ('3309', 'san remo'); (None, None) if unparseable."""
    tokens = re.findall(r"[a-z0-9']+", (address or "").lower())
    if not tokens or not tokens[0].isdigit():
        return None, None
    core = [t for t in tokens[1:] if t not in _DIRECTIONALS and t not in STREET_TYPES]
    return tokens[0], " ".join(core)


def _number_present(number: str, haystack: str) -> bool:
    """Whole-token street-number match: '1641' must not match inside '16413'
    (regression test caught the substring version blocking the wrong house)."""
    return re.search(rf"\b{re.escape(number)}\b", haystack) is not None


def same_property(address: str, haystack: str) -> bool:
    """Same-property-only rule: BOTH street number and street-name core present."""
    number, core = number_and_core((address or "").split(",")[0])
    if not number or not core:
        return False
    hay = (haystack or "").lower()
    return _number_present(number, hay) and core in hay


def addr_slug(address: str) -> str:
    """'1641 E Coronado Rd, Phoenix...' -> '1641-e-coronado-rd' (fact-sheet key)."""
    street = (address or "").split(",")[0].strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", street).strip("-")


def is_blocked_address(address: str, blocked_list: list) -> bool:
    a = (address or "").lower()
    for blocked in blocked_list or []:
        number, core = number_and_core(blocked)
        if number and core and _number_present(number, a) and core in a:
            return True
    return False


# ---------------------------------------------------------------- windows

def in_window(start_az: datetime) -> bool:
    lo, hi = SHOWING_WINDOWS[start_az.weekday()]
    end_dt = start_az + timedelta(minutes=SHOWING_MINUTES)
    # A slot that crosses midnight wrapped .time() to 00:00 and passed the
    # close check - 11:30 PM counted as "in window" (round-2 shadow, Lyndsey).
    if end_dt.date() != start_az.date():
        return False
    # start inside window and the 30-min slot must end by window close
    return lo <= start_az.time() and end_dt.time() <= hi


def min_notice_ok(start_az: datetime, now_az: datetime) -> bool:
    return start_az >= now_az + timedelta(hours=MIN_NOTICE_HOURS)


def respects_renter_bounds(start_az: datetime, after: str | None, before: str | None) -> bool:
    """Enforce the renter's own stated bounds ('after 3pm', 'before noon').
    after/before are 'HH:MM' 24h strings or None. The Luccia fix."""
    if after:
        h, m = map(int, after.split(":"))
        if start_az.time() < time(h, m):
            return False
    if before:
        h, m = map(int, before.split(":"))
        end = (start_az + timedelta(minutes=SHOWING_MINUTES)).time()
        if end > time(h, m):
            return False
    return True


def next_valid_slots(now_az: datetime, count: int = 2, days: int = 7,
                     is_free=lambda dt: True) -> list:
    """Earliest valid exact slots (on :00/:30) satisfying window + notice +
    the caller-supplied is_free predicate. Same-day slots first by design."""
    slots = []
    cursor = now_az + timedelta(hours=MIN_NOTICE_HOURS)
    cursor = cursor.replace(second=0, microsecond=0)
    if cursor.minute not in (0, 30):
        cursor += timedelta(minutes=(30 - cursor.minute % 30))
    end = now_az + timedelta(days=days)
    while cursor < end and len(slots) < count:
        if in_window(cursor) and is_free(cursor):
            slots.append(cursor)
            cursor += timedelta(minutes=30)
        else:
            cursor += timedelta(minutes=30)
    return slots


# ---------------------------------------------------------------- drive time

_CITY_RE = re.compile(r",\s*([A-Za-z .]+),\s*AZ", re.IGNORECASE)
_ZIP_RE = re.compile(r"\b(85\d{3})\b")
_FAR_PAIRS = {frozenset(p) for p in [
    ("queen creek", "scottsdale"), ("queen creek", "phoenix"),
    ("sun lakes", "scottsdale"),
]}


def _city(address: str) -> str:
    m = _CITY_RE.search(address or "")
    return m.group(1).strip().lower() if m else ""


def _zip(address: str) -> str:
    m = _ZIP_RE.search(address or "")
    return m.group(1) if m else ""


def drive_gap_minutes(addr_a: str, addr_b: str) -> int:
    """Required gap between showings at different addresses. Tiers from the
    SKILL: same zip 20 / same city 35 / metro cross-city 45 / far 60;
    Tempe<->Phoenix known route 40. Unknown -> larger buffer."""
    za, zb = _zip(addr_a), _zip(addr_b)
    ca, cb = _city(addr_a), _city(addr_b)
    if za and za == zb:
        return 20
    if ca and ca == cb:
        return 35
    if {ca, cb} == {"tempe", "phoenix"}:
        return 40
    if frozenset((ca, cb)) in _FAR_PAIRS:
        return 60
    if ca and cb:
        return 45
    return 60


# ---------------------------------------------------------------- away block

def away_events(events: list, day_az) -> list:
    """Events on the given AZ date whose text smells like travel/away."""
    out = []
    for ev in events or []:
        text = " ".join([str(ev.get("summary", "")), str(ev.get("description", ""))])
        if _AWAY_RE.search(text):
            out.append(ev)
    return out


def away_block_status(events_that_day: list) -> str:
    """'blocked' or 'clear'. Alex shows every house himself since 2026-10-25
    ("make all showings for me moving forward. My team ... aren't renting
    them out"), so any away event on his calendar blocks the day; there is
    no team cover any more. Renter never hears why - callers phrase it as
    'booked up'."""
    return "blocked" if away_events(events_that_day, None) else "clear"


# ---------------------------------------------------------------- agent pick

def pick_agent(start_az: datetime, now_az: datetime, thread_override: str | None = None) -> dict:
    """ALEX for every showing, same-day included (Alex 2026-10-25: "make all
    showings for me moving forward", "reply to everyone using only my
    calendar"; replaces the 8/13 Rhett same-day default). A thread-level
    override Alex gives by hand ('have Jace cover') still wins."""
    if thread_override and thread_override.lower() in AGENTS:
        return AGENTS[thread_override.lower()]
    return ALEX


def is_same_day(start_az: datetime, now_az: datetime) -> bool:
    return start_az.date() == now_az.date()


# ------------------------------------------- weekday consistency (8/22 Adam)

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


_CLOCK_TIME = re.compile(
    r"\b\d{1,2}(:\d{2})?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])|\b\d{1,2}:\d{2}\b"
    r"|\bnoon\b|\bat \d{1,2}\b|\baround \d{1,2}\b|\bafter \d{1,2}\b"
    r"|\bbefore \d{1,2}\b|\bbetween \d{1,2}", re.I)


def named_day_without_time(text: str):
    """The one weekday a first message names when it gives no clock time,
    e.g. 'I'd love to tour on Monday if there is availability' -> 'Monday'.

    Ashley, 2026-10-24 (1018 E Marigold Ln, thread 1a16eec59561deee): the
    generic first reply asked 'what day and time works', the pre-send review
    blocked it for ignoring her Monday, and she sat unanswered. None when no
    weekday, several weekdays ('Saturday or Sunday'), or a clock time is
    given - those keep the generic ask."""
    t = (text or "").lower()
    named = [name for name in _WEEKDAYS if re.search(rf"\b{name}s?\b", t)]
    if len(named) != 1 or _CLOCK_TIME.search(t):
        return None
    return named[0].capitalize()


def snap_weekday_dates(cls: dict, now_az: datetime) -> dict:
    """Deterministic guard on LLM date resolution (Adam, 2026-08-22: renter
    wrote 'Friday around 1-1:30' on a Thursday night; the model resolved it
    to 2026-08-22, a SATURDAY, and the booking pipeline faithfully booked
    the wrong day). The extractor extracts; this decides: when a candidate's
    raw text names a weekday, the date MUST fall on that weekday - otherwise
    recompute it as the next occurrence of the named weekday from now
    (today counts). 'next <weekday>' within a day of now rolls a week out.
    Mutates and returns cls."""
    from datetime import date as _date, timedelta as _td
    for c in cls.get("time_candidates") or []:
        raw = (c.get("raw") or "").lower()
        named_all = {wd for name, wd in _WEEKDAYS.items() if name in raw}
        if len(named_all) != 1:
            # Zero named weekdays, or several ("Saturday or Sunday works") -
            # snapping to dict order would override a correct model pick
            # (audit 8/25). Ambiguity trusts the model.
            continue
        named = named_all.pop()
        parsed = None
        try:
            parsed = _date.fromisoformat(c.get("date") or "")
        except (ValueError, TypeError):
            pass
        if parsed is not None and parsed.weekday() == named:
            continue  # model got it right
        days_ahead = (named - now_az.weekday()) % 7
        if "next " in raw and days_ahead <= 1:
            days_ahead += 7
        c["date"] = (now_az.date() + _td(days=days_ahead)).isoformat()
        c["weekday_snapped"] = True
    return cls
