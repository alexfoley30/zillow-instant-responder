"""Calendar operations + the deterministic slot engine.

Everything renter-affecting here is called with the send lock already held by
the caller (responder.py). Booking order is fixed: validate -> create/fold
event -> confirmation send -> labels (the ATOMIC BOOKING TRIPWIRE).
"""

import logging
import re
from datetime import datetime, timedelta, timezone

import ledger
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


def slot_acceptable_to_renter(start_az: datetime, declined_iso: list = None,
                              earliest_daily: str = None,
                              latest_daily: str = None) -> bool:
    """Whole-conversation renter facts (2026-08-25, 'give the rules ears'):
    a slot the renter already declined, or one outside their stated daily
    availability ('I get off around 6pm'), is never valid and never worth
    offering - three identical 3:15 offers went to a renter whose every
    message said 6pm because nothing consulted these facts."""
    hm = start_az.strftime("%H:%M")
    if earliest_daily and hm < earliest_daily:
        return False
    if latest_daily and hm > latest_daily:
        return False
    for iso in declined_iso or []:
        try:
            d = datetime.fromisoformat(iso)
            if d.tzinfo is None:
                d = d.replace(tzinfo=AZ_TZ)
            if abs((d - start_az).total_seconds()) <= 900:
                return False
        except (ValueError, TypeError):
            continue
    return True


def validate_slot(start_az: datetime, address: str, events: list,
                  bounds: dict = None, now_az: datetime = None,
                  ignore_same_house: bool = False,
                  declined_iso: list = None, earliest_daily: str = None,
                  latest_daily: str = None,
                  enforce_daily_bounds: bool = True) -> dict:
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
    # Daily bounds steer times WE pick (counters, fold offers). A renter
    # EXPLICITLY proposing a time can obviously make that time, so the book
    # path passes enforce_daily_bounds=False - otherwise Alec's stored
    # "after 6pm weekdays" would veto his own "Saturday 11am works".
    # Declined slots stay vetoed everywhere.
    if not slot_acceptable_to_renter(
            start_az, declined_iso,
            earliest_daily if enforce_daily_bounds else None,
            latest_daily if enforce_daily_bounds else None):
        return {"ok": False, "reason": "renter-cannot-make-it", "fold": None}

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

    # Existing showing at the same house wins ONCE -> offer the fold instead
    # of double-booking. The caller passes ignore_same_house=True after the
    # renter has already been offered that slot and countered with their own
    # time (Alec 2026-08-25: three identical "come at 3:15" replies to a
    # renter who said 6pm every time, then a fold into the 3:15 he refused).
    if not ignore_same_house:
        existing = find_existing_showing(address, events=day_events,
                                         min_lead_hours=0)
        if (existing
                and abs((existing["start_az"] - start_az).total_seconds()) < 3600 * 6
                and slot_acceptable_to_renter(existing["start_az"], declined_iso,
                                              earliest_daily, latest_daily)):
            # Never offer a slot the renter already declined or can't make -
            # their own valid time wins in that case.
            return {"ok": False, "reason": "same-house-slot-exists",
                    "fold": existing}

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
                    agent, jace_cover = rules.RHETT, "drive-gap"
                    break

    return {"ok": True, "agent": agent, "same_day": same_day, "jace_cover": jace_cover}


def counter_slots(address: str, events: list, now_az: datetime = None,
                  count: int = 2, declined_iso: list = None,
                  earliest_daily: str = None, latest_daily: str = None) -> list:
    """Earliest valid exact slots for Template 4 - same-day first by design.
    Renter facts flow through so counters never propose a time the renter
    already said they can't make (Alec 8/24: countered 1:00/1:30 PM to a
    renter whose messages all said after 6pm)."""
    now_az = now_az or datetime.now(AZ_TZ)

    def free(dt):
        return validate_slot(dt, address, events, now_az=now_az,
                             declined_iso=declined_iso,
                             earliest_daily=earliest_daily,
                             latest_daily=latest_daily).get("ok", False)

    return rules.next_valid_slots(now_az, count=count, is_free=free)


# ---------------------------------------------------------------- writes

# Inquirer names on showing events. Canonical format (2026-08-25) is a
# dedicated line "Inquirers: A, B." but three older formats exist on real
# events and all must parse: the one-line "Zillow inquiry. Inquirer: Jessica.
# Agent: Alex Foley (email)." create format, the legacy fold that appended
# ", Matthew." AFTER the Agent clause, and hand-edited variants. Getting this
# wrong deleted a shared event on 8/21 and silently kept ghost events until
# the 8/25 audit caught that the "fixed" parser never matched production text.
_INQ_RE = re.compile(r"Inquirers?:\s*(.*?)(?=\.?\s*Agent:|\n|$)", re.IGNORECASE)
_POST_AGENT_NAMES_RE = re.compile(
    r"Agent:[^()\n]*\([^)]*\)\s*\.?\s*,\s*(.+)$", re.IGNORECASE | re.DOTALL)
_AGENT_CLAUSE_RE = re.compile(r"Agent:\s*[^,\n(]+?\([^)]*\)", re.IGNORECASE)


def _composio_ok(res: dict, context: str):
    """Raise when a Composio execute reported failure. A silent 400 here is
    how the phantom fold happened (2026-08-25 Alec: email sent, calendar
    write quietly rejected); calendar mutations must fail LOUD so the book
    paths treat them as failures."""
    ok = isinstance(res, dict) and (
        res.get("successful") is True
        or (isinstance(res.get("data"), dict)
            and res["data"].get("successful", True) is not False))
    if not ok:
        raise RuntimeError(f"composio {context} failed: {str(res)[:200]}")
    return res


def _full_update_fields(ev: dict, description: str) -> dict:
    """Composio GOOGLECALENDAR_UPDATE_EVENT is a full REPLACE (live probe
    2026-08-27): any field omitted is ERASED - a description-only update
    stripped the probe event's title, attendees, and duration. Always send
    the complete field set rebuilt from the event we just read. The tool
    also REQUIRES start_datetime now; the old description-only calls were
    400-ing silently."""
    start_raw = str((ev.get("start") or {}).get("dateTime") or "")
    end_raw = str((ev.get("end") or {}).get("dateTime") or "")
    duration = rules.SHOWING_MINUTES
    try:
        s = datetime.fromisoformat(start_raw)
        e = datetime.fromisoformat(end_raw)
        duration = max(5, int((e - s).total_seconds() // 60))
    except (ValueError, TypeError):
        pass
    fields = {
        "calendar_id": "primary",
        "event_id": ev.get("id", ""),
        "summary": ev.get("summary") or "Showing",
        "description": description,
        "start_datetime": start_raw[:19],
        "event_duration_minutes": duration,
        "timezone": "America/Phoenix",
        "send_updates": "none",
    }
    attendees = [a.get("email") for a in ev.get("attendees") or []
                 if a.get("email")]
    if attendees:
        fields["attendees"] = attendees
    if ev.get("location"):
        fields["location"] = ev["location"]
    return fields


def _split_names(blob: str) -> list:
    return [n.strip(" .\n") for n in blob.split(",") if n.strip(" .\n")]


def parse_inquirers(desc: str) -> list:
    """Every inquirer name recorded on a showing description, any era."""
    desc = desc or ""
    m = _INQ_RE.search(desc)
    if not m:
        return []
    names = _split_names(m.group(1))
    pm = _POST_AGENT_NAMES_RE.search(desc[m.end():])
    if pm:
        names += _split_names(pm.group(1))
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def _rebuild_description(desc: str, names: list) -> str:
    """Canonical description carrying `names`, preserving the Agent clause."""
    out = "Zillow inquiry.\nInquirers: " + ", ".join(names) + "."
    am = _AGENT_CLAUSE_RE.search(desc or "")
    if am:
        out += "\n" + am.group(0).rstrip(".") + "."
    return out


def create_showing_event(address: str, first_name: str, start_az: datetime,
                         agent: dict) -> str:
    """Create the 30-minute showing event. Returns the event id."""
    end_az = start_az + timedelta(minutes=rules.SHOWING_MINUTES)
    attendees = {rules.ALEX["email"], agent["email"], rules.BRIANNA_VIEWER}
    res = composio_execute("GOOGLECALENDAR_CREATE_EVENT", {
        "calendar_id": "primary",
        "summary": f"Showing — {address.split(',')[0].strip()} with {first_name}",
        "location": address,
        "description": (f"Zillow inquiry.\nInquirers: {first_name}.\n"
                        f"Agent: {agent['name']} ({agent['email']})."),
        "start_datetime": start_az.strftime("%Y-%m-%dT%H:%M:%S"),
        "event_duration_minutes": rules.SHOWING_MINUTES,
        "timezone": "America/Phoenix",
        "attendees": sorted(attendees),
        "send_updates": "all",
    })
    data = res.get("data", res)
    ev = data.get("response_data") or data.get("event") or data
    event_id = (ev or {}).get("id") or ""
    if not event_id:
        raise RuntimeError(f"create_event returned no id: {str(res)[:200]}")
    # Ledger record is part of the booking, not bookkeeping: a showing whose
    # renters can't be recorded must not confirm (same tripwire philosophy
    # as event-before-email). Raising here lands in the book path's
    # mark_failed like any other create failure.
    ledger.upsert_showing(event_id, address=address,
                          start_iso=start_az.isoformat(),
                          agent_name=agent["name"], renters=[first_name])
    return event_id


def agent_name_from_event(event: dict, default: str = "Alex Foley") -> str:
    """Who actually meets the renter. Ledger first (zillow_showings owns
    agent_name since 8/25), description parse as the legacy fallback - fold
    confirmations used to hardcode Alex while the event said Rhett/Jace."""
    event_id = (event or {}).get("id", "")
    if event_id:
        try:
            doc = ledger.get_showing(event_id)
            if doc and doc.get("agent_name"):
                return doc["agent_name"]
        except Exception as e:  # noqa: BLE001
            log.error("showing agent lookup failed for %s: %s", event_id, e)
    m = re.search(r"Agent:\s*([^(\n]+?)\s*\(",
                  str((event or {}).get("description", "")))
    return m.group(1).strip() if m else default


def _ledger_renters(event_id: str):
    """Renter list from the showings ledger, or None when no doc exists
    (pre-refactor event) so callers fall back to description parsing."""
    try:
        doc = ledger.get_showing(event_id)
        if doc and doc.get("renters"):
            return list(doc["renters"])
    except Exception as e:  # noqa: BLE001 - legacy parser still covers us
        log.error("showing ledger read failed for %s: %s", event_id, e)
    return None


def fold_renter_into_event(event: dict, first_name: str) -> str:
    """Clustering: append the renter to the existing event's inquirer list.
    NEVER creates a second event. Returns the event id. Ledger first, then
    the description is re-rendered from the full list (one-way)."""
    event_id = event.get("id", "")
    desc = str(event.get("description", ""))
    names = _ledger_renters(event_id)
    if names is None:
        names = parse_inquirers(desc)
    if first_name.strip().lower() in [n.lower() for n in names]:
        return event_id  # already folded (idempotent)
    names.append(first_name.strip())
    ledger.upsert_showing(event_id, renters=names)
    _composio_ok(composio_execute(
        "GOOGLECALENDAR_UPDATE_EVENT",
        _full_update_fields(event, _rebuild_description(desc, names))),
        f"fold update {event_id}")
    return event_id


def cancel_event(event_id: str):
    composio_execute("GOOGLECALENDAR_DELETE_EVENT", {
        "calendar_id": "primary",
        "event_id": event_id,
    })


def remove_renter_or_cancel(event_id: str, first_name: str) -> str:
    """Fold-aware cancellation (Alex ruling 2026-08-22, after one renter's
    cancel deleted a shared consolidated event on 8/21): take this renter off
    the showing's renter list; DELETE the event only when they were the last
    renter on it. The showings ledger is the authority; the description
    parser only covers pre-refactor events. Deleting is the dangerous branch,
    so ambiguity fails toward KEEPING the event: 'kept-unparsed' tells the
    caller a human must look. Returns 'removed' | 'canceled' | 'kept-unparsed'."""
    ev = None
    try:
        for cand in list_events(30):
            if cand.get("id") == event_id:
                ev = cand
                break
    except Exception as e:  # noqa: BLE001
        log.error("remove_renter lookup failed for %s: %s", event_id, e)
    desc = str((ev or {}).get("description", ""))

    names = _ledger_renters(event_id)
    if names is None:
        if not ev:
            # No ledger record AND can't see the event: never blind-delete a
            # possibly-shared showing.
            return "kept-unparsed"
        names = parse_inquirers(desc)

    target = (first_name or "").strip().lower()
    if not names:
        # No renter list anywhere (hand-made or pre-format event). Solo only
        # if the title reads "... with <this renter>"; a bare substring test
        # would match renter names hiding inside street names.
        if ev and target and re.search(rf"with\s+{re.escape(target)}\b",
                                       str(ev.get("summary", "")),
                                       re.IGNORECASE):
            cancel_event(event_id)
            _delete_showing_quiet(event_id)
            return "canceled"
        return "kept-unparsed"
    if target not in [n.lower() for n in names]:
        return "kept-unparsed"  # someone else's event - never delete it
    remaining = [n for n in names if n.lower() != target]
    if not remaining:
        if ev:
            cancel_event(event_id)
        else:
            try:  # ledger knows the showing but the event is already gone
                cancel_event(event_id)
            except Exception as e:  # noqa: BLE001
                log.error("cancel of unseen event %s: %s", event_id, e)
        _delete_showing_quiet(event_id)
        return "canceled"
    try:
        ledger.upsert_showing(event_id, renters=remaining)
    except Exception as e:  # noqa: BLE001
        log.error("showing ledger update failed for %s: %s", event_id, e)
    if ev:
        _composio_ok(composio_execute(
            "GOOGLECALENDAR_UPDATE_EVENT",
            _full_update_fields(ev, _rebuild_description(desc, remaining))),
            f"remove update {event_id}")
    return "removed"


def _delete_showing_quiet(event_id: str):
    try:
        ledger.delete_showing(event_id)
    except Exception as e:  # noqa: BLE001
        log.error("showing ledger delete failed for %s: %s", event_id, e)
