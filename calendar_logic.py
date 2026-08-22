"""Calendar operations + the deterministic slot engine.

Everything renter-affecting here is called with the send lock already held by
the caller (responder.py). Booking order is fixed: validate -> create/fold
event -> confirmation send -> labels (the ATOMIC BOOKING TRIPWIRE).
"""

import logging
import re
from datetime import datetime, timedelta, timezone

import rules
from gmail_client import composio_execute

log = logging.getLogger("zillow-instant.calendar")

AZ_TZ = rules.AZ_TZ


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_events(days: int = 7, time_min: datetime = None) -> list:
    now = time_min or datetime.now(timezone.utc)
    res = composio_execute("GOOGLECALENDAR_EVENTS_LIST", {
        "calendarId": "primary",
        "timeMin": _iso_utc(now),
        "timeMax": _iso_utc(now + timedelta(days=days)),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 100,
    })
    data = res.get("data", res)
    items = data.get("items") or data.get("events") or []
    if isinstance(data, dict) and not items and isinstance(data.get("event_data"), dict):
        items = data["event_data"].get("event_data", []) or []
    return [ev for ev in items if isinstance(ev, dict)]


def _ev_text(ev: dict) -> str:
    return " ".join([str(ev.get("summary", "")), str(ev.get("location", "")),
                     str(ev.get("description", ""))]).lower()


def _ev_start(ev: dict) -> datetime | None:
    raw = (ev.get("start") or {}).get("dateTime")
    if not raw:
        return None  # all-day events aren't showings
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ev_end(ev: dict) -> datetime | None:
    raw = (ev.get("end") or {}).get("dateTime")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ev_all_day_covers(ev: dict, day) -> bool:
    """All-day events carry start.date (no dateTime). End date is exclusive."""
    raw = (ev.get("start") or {}).get("date")
    if not raw:
        return False
    try:
        s = datetime.strptime(raw, "%Y-%m-%d").date()
        end_raw = (ev.get("end") or {}).get("date")
        e = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else s + timedelta(days=1)
    except (ValueError, TypeError):
        return False
    return s <= day < e


def fmt_showing_time(start_az: datetime) -> str:
    """'Wednesday, July 8, at 6:30 PM' with today/tomorrow prefix when true."""
    now_az = datetime.now(AZ_TZ)
    day = start_az.strftime("%A, %B %-d")
    if start_az.date() == now_az.date():
        day = f"today, {day}"
    elif start_az.date() == (now_az + timedelta(days=1)).date():
        day = f"tomorrow, {day}"
    return f"{day}, at {start_az.strftime('%-I:%M %p')}"


def find_existing_showings(address: str, events: list = None,
                           min_lead_hours: float = 2.0, limit: int = 2) -> list:
    """CONSOLIDATE FIRST: all upcoming showings at THIS house (same street
    number + name core, 'showing'/'open house' in text), soonest first,
    capped at `limit`. Each item: {event, start_az, when_human}.

    Added 2026-08-22 (Jamie/Redfield): "Are you available today?" got the
    generic windows-ask while TWO same-day showings sat on the calendar -
    the vague_time branch never consulted the calendar. This is its lookup."""
    try:
        events = events if events is not None else list_events(7)
    except Exception as e:  # noqa: BLE001
        log.error("find_existing_showings list failed for %r: %s", address, e)
        return []
    hits = []
    for ev in events:
        hay = _ev_text(ev)
        if not rules.same_property(address, hay):
            continue
        if "showing" not in hay and "open house" not in hay:
            continue
        start = _ev_start(ev)
        if not start:
            continue
        if start < datetime.now(timezone.utc) + timedelta(hours=min_lead_hours):
            continue
        start_az = start.astimezone(AZ_TZ)
        hits.append({"event": ev, "start_az": start_az,
                     "when_human": fmt_showing_time(start_az)})
    hits.sort(key=lambda h: h["start_az"])
    return hits[:limit]


def find_existing_showing(address: str, events: list = None,
                          min_lead_hours: float = 2.0) -> dict | None:
    """Single-slot wrapper around find_existing_showings (soonest or None)."""
    hits = find_existing_showings(address, events, min_lead_hours, limit=1)
    return hits[0] if hits else None


def validate_slot(start_az: datetime, address: str, events: list,
                  bounds: dict = None, now_az: datetime = None) -> dict:
    """Deterministic validation of one candidate slot.
    Returns {"ok": True, "agent": {...}, "same_day": bool} or
    {"ok": False, "reason": str, "fold": {...}|None}.

    Same-day candidates book with Jace and skip Alex-calendar overlap and
    drive-time checks (his calendar is not visible); the only blocker is an
    existing event at the SAME property (fold instead) and the away-block.
    """
    now_az = now_az or datetime.now(AZ_TZ)
    bounds = bounds or {}

    if not rules.in_window(start_az):
        return {"ok": False, "reason": "out-of-window", "fold": None}
    if not rules.min_notice_ok(start_az, now_az):
        return {"ok": False, "reason": "too-soon", "fold": None}
    if not rules.respects_renter_bounds(start_az, bounds.get("after"), bounds.get("before")):
        return {"ok": False, "reason": "violates-renter-bounds", "fold": None}

    # NOTE: the old datetime.min fallback here raised OverflowError the moment
    # an all-day event (no start.dateTime) appeared in the window - year 1
    # minus UTC-7 is out of range. Three live renters hit it 2026-08-05.
    # All-day events now count toward the day (so away blocks finally register);
    # the timed-overlap checks below skip them naturally (_ev_start is None).
    day_events = []
    for ev in events:
        s = _ev_start(ev)
        if s is not None:
            if s.astimezone(AZ_TZ).date() == start_az.date():
                day_events.append(ev)
        elif _ev_all_day_covers(ev, start_az.date()):
            day_events.append(ev)

    # Away block applies to every candidate day.
    status = rules.away_block_status(rules.away_events(day_events, start_az.date()))
    if status == "blocked":
        return {"ok": False, "reason": "away-blocked", "fold": None}

    # Existing showing at the same house always wins -> fold, never double-book.
    existing = find_existing_showing(address, events=day_events, min_lead_hours=0)
    if existing and abs((existing["start_az"] - start_az).total_seconds()) < 3600 * 6:
        return {"ok": False, "reason": "same-house-slot-exists", "fold": existing}

    same_day = rules.is_same_day(start_az, now_az)
    agent = rules.pick_agent(start_az, now_az)
    if status == "alex-away" and agent["email"] == rules.ALEX["email"]:
        agent = rules.RHETT  # cover default is Rhett (Alex 2026-08-13; was Jace)

    # AGENT FALLBACK (2026-07-30, Kim/New Town): a valid requested time never
    # bounces because Alex is busy - it books with the cover agent instead
    # (Rhett since 2026-08-13, was Jace). Counters are
    # reserved for times that are themselves invalid (window/notice/bounds/
    # away-block), handled above.
    jace_cover = None
    if not same_day and agent["email"] == rules.ALEX["email"]:
        end_az = start_az + timedelta(minutes=rules.SHOWING_MINUTES)
        for ev in day_events:
            s, e = _ev_start(ev), _ev_end(ev)
            if not s or not e:
                continue
            s_az, e_az = s.astimezone(AZ_TZ), e.astimezone(AZ_TZ)
            if s_az < end_az and start_az < e_az:
                agent, jace_cover = rules.RHETT, "alex-conflict"
                break
            ev_addr = str(ev.get("location", "")) or str(ev.get("summary", ""))
            if not rules.same_property(address, _ev_text(ev)) and ev_addr.strip():
                gap = rules.drive_gap_minutes(address, ev_addr)
                if (s_az >= end_az and (s_az - end_az) < timedelta(minutes=gap)) or \
                   (e_az <= start_az and (start_az - e_az) < timedelta(minutes=gap)):
                    agent, jace_cover = rules.JACE, "drive-gap"
                    break

    return {"ok": True, "agent": agent, "same_day": same_day, "jace_cover": jace_cover}


def counter_slots(address: str, events: list, now_az: datetime = None,
                  count: int = 2) -> list:
    """Earliest valid exact slots for Template 4 - same-day first by design."""
    now_az = now_az or datetime.now(AZ_TZ)

    def free(dt):
        return validate_slot(dt, address, events, now_az=now_az).get("ok", False)

    return rules.next_valid_slots(now_az, count=count, is_free=free)


# ---------------------------------------------------------------- writes

def create_showing_event(address: str, first_name: str, start_az: datetime,
                         agent: dict) -> str:
    """Create the 30-minute showing event. Returns the event id."""
    end_az = start_az + timedelta(minutes=rules.SHOWING_MINUTES)
    attendees = {rules.ALEX["email"], agent["email"], rules.BRIANNA_VIEWER}
    res = composio_execute("GOOGLECALENDAR_CREATE_EVENT", {
        "calendarId": "primary",
        "summary": f"Showing — {address.split(',')[0].strip()} with {first_name}",
        "location": address,
        "description": (f"Zillow inquiry. Inquirer: {first_name}. "
                        f"Agent: {agent['name']} ({agent['email']})."),
        "start_datetime": start_az.strftime("%Y-%m-%dT%H:%M:%S"),
        "event_duration_minutes": rules.SHOWING_MINUTES,
        "timezone": "America/Phoenix",
        "attendees": sorted(attendees),
        "send_updates": True,
    })
    data = res.get("data", res)
    ev = data.get("response_data") or data.get("event") or data
    event_id = (ev or {}).get("id") or ""
    if not event_id:
        raise RuntimeError(f"create_event returned no id: {str(res)[:200]}")
    return event_id


def fold_renter_into_event(event: dict, first_name: str) -> str:
    """Clustering: append the renter to the existing event's description.
    NEVER creates a second event. Returns the event id."""
    event_id = event.get("id", "")
    desc = str(event.get("description", "")).rstrip(". \n")
    if first_name.lower() in desc.lower():
        return event_id  # already folded (idempotent)
    if "Inquirer:" in desc:
        new_desc = desc + f", {first_name}."
    else:
        new_desc = (desc + f"\nInquirer: {first_name}.").strip()
    composio_execute("GOOGLECALENDAR_UPDATE_EVENT", {
        "calendarId": "primary",
        "event_id": event_id,
        "description": new_desc,
        "send_updates": True,
    })
    return event_id


def cancel_event(event_id: str):
    composio_execute("GOOGLECALENDAR_DELETE_EVENT", {
        "calendarId": "primary",
        "event_id": event_id,
    })


def remove_renter_or_cancel(event_id: str, first_name: str) -> str:
    """Fold-aware cancellation (Alex ruling 2026-08-22, after one renter's
    cancel deleted a shared consolidated event on 8/21): take this renter off
    the event's Inquirer list; DELETE the event only when they were the last
    renter on it. Returns 'removed' | 'canceled'."""
    ev = None
    try:
        for cand in list_events(7):
            if cand.get("id") == event_id:
                ev = cand
                break
    except Exception as e:  # noqa: BLE001
        log.error("remove_renter lookup failed for %s: %s", event_id, e)
    desc = str((ev or {}).get("description", ""))
    m = re.search(r"Inquirer:\s*([^\n]+)", desc)
    if not ev or not m or not first_name:
        cancel_event(event_id)
        return "canceled"
    names = [n.strip() for n in m.group(1).rstrip(". ").split(",") if n.strip()]
    remaining = [n for n in names if n.lower() != first_name.strip().lower()]
    if not remaining:
        cancel_event(event_id)
        return "canceled"
    new_line = "Inquirer: " + ", ".join(remaining) + "."
    new_desc = desc[:m.start()] + new_line + desc[m.end():]
    composio_execute("GOOGLECALENDAR_UPDATE_EVENT", {
        "calendarId": "primary",
        "event_id": event_id,
        "description": new_desc,
        "send_updates": True,
    })
    return "removed"
