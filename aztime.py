"""Arizona time in one place: the zone, the coercions, and every clock string
the pipeline prints.

Arizona has no DST, so this is the one timezone the whole pipeline ever shows a
renter. It was still being re-derived by hand in four places
(rules.AZ_TZ, responder.AZ_TZ, calendar_logic's alias, and a hand-rolled fixed
-7 offset in scripts/shadow_dump.py), and the strftime shapes that print it
were written out in six files. The copies had already drifted: the Haiku prompt
stamped "06:30 PM" where every renter-facing line stamped "6:30 PM", and a
naive datetime was read as UTC in cron.py but as Arizona in calendar_logic.py.

Name the format, don't re-type it. to_az() is the only place that decides what
a naive datetime means, and it makes the caller say.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

AZ_TZ = ZoneInfo("America/Phoenix")


def now() -> datetime:
    """Arizona wall clock, aware."""
    return datetime.now(AZ_TZ)


def to_az(dt, naive: str = "az"):
    """Any datetime -> Arizona, passing None through.

    A naive datetime has no right answer, so the caller states its origin:
    naive="utc" for stored timestamps (Firestore hands these back naive on
    some reads), naive="az" for strings the calendar already wrote in local
    Arizona time. Those two readings are seven hours apart; guessing is how
    a reminder fires in the middle of the night.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if naive == "utc" else AZ_TZ)
    return dt.astimezone(AZ_TZ)


def iso_utc(dt: datetime) -> str:
    """'2026-11-10T21:30:00Z' - the shape Google Calendar's list API wants."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- renter-facing

def clock(t) -> str:
    """'6:30 PM'. Takes a datetime or a bare time."""
    return t.strftime("%-I:%M %p")


def day(d) -> str:
    """'Wednesday, July 8'. Takes a datetime or a date."""
    return d.strftime("%A, %B %-d")


def weekday(d) -> str:
    """'Wednesday'."""
    return d.strftime("%A")


def relative_day(d, today: date = None) -> str:
    """day() with a 'today,' / 'tomorrow,' prefix when that is true."""
    today = today or now().date()
    only_date = d.date() if isinstance(d, datetime) else d
    if only_date == today:
        return f"today, {day(d)}"
    if only_date == today + timedelta(days=1):
        return f"tomorrow, {day(d)}"
    return day(d)


def day_and_time(start_az: datetime) -> str:
    """'Wednesday, July 8, at 6:30 PM', with today/tomorrow prefix when true.
    The one renter-facing way to name a showing."""
    return f"{relative_day(start_az)}, at {clock(start_az)}"


# ---------------------------------------------------------------- internal

def short_stamp(start_az: datetime) -> str:
    """'Wed 7/8 6:30 PM' - compact, for Poke pings to Alex, never to a renter."""
    return start_az.strftime("%a %-m/%-d %-I:%M %p")


def prompt_stamp(dt: datetime) -> str:
    """'Wednesday, July 08, 2026 at 06:30 PM' - "now" as the Haiku prompts
    state it. Zero-padded on purpose: this is the drifted shape the live
    prompts have always carried, and the classify goldens are pinned to it.
    Changing it is a prompt change, not a refactor."""
    return dt.strftime("%A, %B %d, %Y at %I:%M %p")
