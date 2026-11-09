"""create_showing_event: the write that actually books a showing.

responder._book_new calls it BEFORE the confirmation email (the atomic booking
tripwire), so what it puts on the wire and in the showings ledger is what the
renter is promised. Every existing test monkeypatches it away, so none of this
was covered:

  * the Composio payload - a wrong start_datetime/timezone pairing books the
    showing at the wrong hour, and send_updates="all" means Google emails the
    attendees whatever we send.
  * the round trip - parse_inquirers, _event_agent and agent_name_from_event
    all read descriptions back off the calendar. The 8/25 audit found the
    cancel parser had never matched what the creator wrote, which left ghost
    events; nothing re-locks that contract at the creating end.
  * event-id extraction - create_showing_event must find the id in the real
    response shapes and raise when there is none. A phantom id confirms a
    showing that does not exist; a needless raise tells the renter nothing
    while the event sits on the calendar.
  * the ledger write - zillow_showings owns who is on each showing, and the
    code deliberately lets a failure there abort the booking.
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calendar_logic as cal  # noqa: E402
import rules  # noqa: E402

AZ = rules.AZ_TZ
ADDRESS = "1110 E Redfield Rd, Tempe, AZ, 85283"
# Wednesday 6:00 PM - inside the Wed 10:00-18:30 window.
START = datetime(2026, 11, 12, 18, 0, tzinfo=AZ)


class _Stub:
    """Capture the calendar write and the showings-ledger write."""

    def __init__(self, monkeypatch, response=None, ledger_raises=None):
        self.calls = []
        self.showings = {}
        self._response = response or {"data": {"response_data": {"id": "ev_abc123"}}}
        self._ledger_raises = ledger_raises
        monkeypatch.setattr(cal, "composio_execute", self._exec)
        monkeypatch.setattr(cal.ledger, "upsert_showing", self._upsert)
        monkeypatch.setattr(cal.ledger, "get_showing", self.showings.get)

    def _exec(self, slug, params):
        self.calls.append((slug, params))
        return self._response

    def _upsert(self, event_id, **fields):
        if self._ledger_raises:
            raise self._ledger_raises
        self.showings.setdefault(event_id, {}).update(
            {k: v for k, v in fields.items() if v is not None})

    def create_params(self):
        return next(p for s, p in self.calls if s == "GOOGLECALENDAR_CREATE_EVENT")


# ------------------------------------------------- what lands on the calendar

def test_event_is_booked_at_the_local_phoenix_time(monkeypatch):
    """start_datetime must be the NAIVE local time paired with an explicit
    America/Phoenix timezone. A UTC-stamped value here would silently move
    every showing seven hours."""
    stub = _Stub(monkeypatch)
    cal.create_showing_event(ADDRESS, "Madison", START, rules.ALEX)
    p = stub.create_params()
    assert p["start_datetime"] == "2026-11-12T18:00:00"
    assert p["timezone"] == "America/Phoenix"
    assert p["event_duration_minutes"] == rules.SHOWING_MINUTES == 30
    assert p["calendar_id"] == "primary"


def test_event_carries_the_address_and_renter(monkeypatch):
    stub = _Stub(monkeypatch)
    cal.create_showing_event(ADDRESS, "Madison", START, rules.ALEX)
    p = stub.create_params()
    assert p["summary"] == "Showing — 1110 E Redfield Rd with Madison"
    # Location keeps the full address: _agent_conflict feeds it to
    # drive_gap_minutes, which needs the city/zip to pick a gap tier.
    assert p["location"] == ADDRESS
    assert rules.drive_gap_minutes(p["location"], "2118 S El Marino, Mesa, AZ 85202") == 45


def test_alex_and_the_viewer_are_invited_and_the_renter_is_not(monkeypatch):
    """send_updates='all' emails every attendee, so the attendee list is
    renter-visible blast radius. Brianna is a viewer, never the agent, and
    the renter only ever hears from us through the Zillow relay."""
    stub = _Stub(monkeypatch)
    cal.create_showing_event(ADDRESS, "Madison", START, rules.ALEX)
    p = stub.create_params()
    assert p["attendees"] == ["alex@azfoleyhomes.com", "azfoleyhomes@gmail.com"]
    assert p["send_updates"] == "all"
    assert not any("convo.zillow.com" in a for a in p["attendees"])


def test_a_cover_booking_invites_that_agent_alongside_alex(monkeypatch):
    """Alex first, then Jace, then Rhett (2026-11-04). A cover showing still
    sits on Alex's calendar - the agent is invited, not substituted."""
    stub = _Stub(monkeypatch)
    cal.create_showing_event(ADDRESS, "Madison", START, rules.JACE)
    p = stub.create_params()
    assert p["attendees"] == ["alex@azfoleyhomes.com", "azfoleyhomes@gmail.com",
                              "jacejohnson.re@gmail.com"]


# ------------------------------------------------- round trip off the calendar

def test_description_parses_back_as_the_renter_and_the_agent(monkeypatch):
    """The creator and the cancel/fold readers must agree. If this drifts,
    remove_renter_or_cancel returns 'kept-unparsed' and ghost events pile up."""
    stub = _Stub(monkeypatch)
    cal.create_showing_event(ADDRESS, "Madison", START, rules.ALEX)
    desc = stub.create_params()["description"]
    assert cal.parse_inquirers(desc) == ["Madison"]
    # Legacy path: no zillow_showings doc, so the description is the only
    # record of who meets the renter.
    monkeypatch.setattr(cal.ledger, "get_showing", lambda eid: None)
    assert cal.agent_name_from_event({"id": "ev_abc123", "description": desc}) == "Alex Foley"


def test_a_cover_event_counts_against_that_agent_not_alex(monkeypatch):
    """_event_agent reads the description to decide whose day an event eats.
    A Jace showing that read as 'alex' would make Alex look booked and push
    his own leads onto a cover agent for no reason."""
    stub = _Stub(monkeypatch)
    cal.create_showing_event(ADDRESS, "Madison", START, rules.JACE)
    params = stub.create_params()
    event = {"id": "ev_abc123", "summary": params["summary"],
             "description": params["description"], "location": params["location"]}
    assert cal._event_agent(event) == "jace"
    assert cal.agent_name_from_event(event) == "Jace Johnson"


def test_a_renter_named_alex_is_not_mistaken_for_the_agent(monkeypatch):
    stub = _Stub(monkeypatch)
    cal.create_showing_event(ADDRESS, "Alex", START, rules.JACE)
    desc = stub.create_params()["description"]
    assert cal.parse_inquirers(desc) == ["Alex"]
    assert cal._event_agent({"description": desc}) == "jace"


# ------------------------------------------------- the showings ledger write

def test_the_showing_is_recorded_as_the_source_of_truth(monkeypatch):
    """zillow_showings owns agent_name and renters since the 8/25
    calendar-as-projection refactor; fold and cancel read it before the
    description."""
    stub = _Stub(monkeypatch)
    event_id = cal.create_showing_event(ADDRESS, "Madison", START, rules.JACE)
    assert stub.showings[event_id] == {
        "address": ADDRESS,
        "start_iso": "2026-11-12T18:00:00-07:00",
        "agent_name": "Jace Johnson",
        "renters": ["Madison"],
    }


def test_a_ledger_failure_aborts_the_booking(monkeypatch):
    """Deliberate: "a showing whose renters can't be recorded must not
    confirm". Raising lands in _book_new's mark_failed instead of promising
    a showing nothing can later fold into or cancel."""
    _Stub(monkeypatch, ledger_raises=RuntimeError("firestore unavailable"))
    with pytest.raises(RuntimeError, match="firestore unavailable"):
        cal.create_showing_event(ADDRESS, "Madison", START, rules.ALEX)


# ------------------------------------------------- reading back the event id

@pytest.mark.parametrize("response", [
    {"data": {"response_data": {"id": "ev_abc123"}}},
    {"data": {"event": {"id": "ev_abc123"}}},
    {"data": {"id": "ev_abc123"}},
])
def test_event_id_is_found_in_every_response_shape(monkeypatch, response):
    """Composio has shipped all three envelopes. The id is what gets stored
    on the thread and used to cancel later, so missing it is not cosmetic."""
    _Stub(monkeypatch, response=response)
    assert cal.create_showing_event(ADDRESS, "Madison", START, rules.ALEX) == "ev_abc123"


def test_a_rejected_create_raises_instead_of_returning_a_phantom_id(monkeypatch):
    """The 8/25 Alec phantom fold: email sent, calendar write quietly
    rejected. A create with no id must fail loud so _book_new returns
    skipped:event-failed and never sends a confirmation."""
    _Stub(monkeypatch, response={"successful": False,
                                 "error": "Invalid request: calendar not found"})
    with pytest.raises(RuntimeError, match="create_event returned no id"):
        cal.create_showing_event(ADDRESS, "Madison", START, rules.ALEX)


def test_no_showing_is_recorded_when_the_create_failed(monkeypatch):
    stub = _Stub(monkeypatch, response={"data": {}})
    with pytest.raises(RuntimeError):
        cal.create_showing_event(ADDRESS, "Madison", START, rules.ALEX)
    assert stub.showings == {}
