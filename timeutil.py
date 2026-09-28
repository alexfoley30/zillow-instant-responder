"""Timestamp parsing and formatting, in one place.

Every timestamp this pipeline reads arrives as a string someone else wrote:
Google Calendar dateTime fields, Composio message headers, our own
`booked_start_iso` / `offered_start_iso` ledger fields. Before this module
each reader re-implemented the parse inline, and the copies had drifted:
four tolerated a trailing "Z" and four did not, so the same string parsed in
one call site and raised in another.

Two rules for the whole codebase:
  - `parse_iso` is the ONLY place `datetime.fromisoformat` is called on an
    external string. It always tolerates "Z" and never raises.
  - A naive string has no single right answer, so the caller states the
    assumption with `assume_tz`. Do not add a default: the declined-slot
    comparison assumes Arizona (renters type local times) while ledger
    timestamps assume UTC, and silently sharing one default would corrupt
    one of them.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

AZ_TZ = ZoneInfo("America/Phoenix")


def parse_iso(raw, assume_tz=None) -> datetime | None:
    """Parse an ISO-8601 string, tolerating a trailing 'Z'. Returns None for
    anything empty or unparseable - callers treat None as 'unknown' and fail
    safe rather than guessing a time. With `assume_tz`, a string carrying no
    offset is stamped with that zone; without it, a naive string stays naive."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None and assume_tz is not None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt


def parse_iso_az(raw) -> datetime | None:
    """`parse_iso` converted to Arizona time. A string with no offset is read
    as UTC (Render runs UTC, and every ledger timestamp we write is UTC or
    offset-carrying) rather than as the ambient system zone."""
    dt = parse_iso(raw, assume_tz=timezone.utc)
    return dt.astimezone(AZ_TZ) if dt else None


def to_az(dt: datetime | None) -> datetime | None:
    """A datetime object (typically a Firestore timestamp) in Arizona time.
    Naive input is read as UTC, matching `parse_iso_az`."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(AZ_TZ)


def iso_utc(dt: datetime) -> str:
    """'2026-09-28T17:30:00Z' - the shape Google Calendar's timeMin/timeMax want."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
