"""Pure business rules — zero I/O, fully unit-testable.

Ports the scheduling rules from the sweep SKILL.md into code:
windows, 2-hour minimum notice, SAME-DAY LADDER (Jace default), drive-time
gap tiers, away-block detection, same-property matching, agent selection.

The window/address/agent primitives live in rules_core.py and are re-exported
below, so scripts/shadow_dump.py (Python 3.9, cannot import this module) can
share them instead of keeping a hand-copied mirror in sync.
"""

import re
from datetime import datetime, time, timedelta

# Primitives shared with scripts/shadow_dump.py, which cannot import this module
# (it runs on 3.9 and this file uses PEP 604 annotations). Re-exported here so
# `rules.X` keeps working for every existing caller.
from rules_core import (  # noqa: F401 - re-exported as part of the rules API
    AGENT_NAMES,
    AGENTS,
    ALEX,
    AZ_TZ,
    BRIANNA_VIEWER,
    JACE,
    MIN_NOTICE_HOURS,
    RHETT,
    SHOWING_MINUTES,
    SHOWING_WINDOWS,
    _DIRECTIONALS,
    _STREET_TYPES,
    addr_slug,
    address_matches,
    in_window,
    number_and_core,
    number_present,
)

_AWAY_RE = re.compile(
    r"(out of town|ooo|out of office|vacation|trip\b|stay at|travel|hotel|"
    r"lodge|resort)", re.IGNORECASE)


def same_property(address: str, haystack: str) -> bool:
    """Same-property-only rule: BOTH street number and street-name core present."""
    return address_matches(address, haystack)


def is_blocked_address(address: str, blocked_list: list) -> bool:
    return any(address_matches(blocked, address) for blocked in blocked_list or [])


# ---------------------------------------------------------------- windows

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
    """'blocked' (whole team away), 'alex-away' (Jace/Rhett not on the event),
    or 'clear'. Renter never hears why - callers phrase it as 'booked up'."""
    aways = away_events(events_that_day, None)
    if not aways:
        return "clear"
    for ev in aways:
        attendees = " ".join(str(a.get("email", "")) for a in ev.get("attendees", []) or [])
        text = (" ".join([str(ev.get("summary", "")), str(ev.get("description", ""))])
                + " " + attendees).lower()
        team_on_event = JACE["email"] in text or RHETT["email"] in text or \
            "jace" in text or "rhett" in text
        if team_on_event:
            return "blocked"
    return "alex-away"


# ---------------------------------------------------------------- agent pick

def pick_agent(start_az: datetime, now_az: datetime, thread_override: str | None = None) -> dict:
    """Same-day bookings default to RHETT (Alex 2026-08-13, replacing the
    7/27 Jace default); Alex default for next-day+. A thread-level override
    ('have Jace cover') beats both."""
    if thread_override and thread_override.lower() in AGENTS:
        return AGENTS[thread_override.lower()]
    if start_az.date() == now_az.date():
        return RHETT
    return ALEX


def is_same_day(start_az: datetime, now_az: datetime) -> bool:
    return start_az.date() == now_az.date()


# ------------------------------------------- weekday consistency (8/22 Adam)

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


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
