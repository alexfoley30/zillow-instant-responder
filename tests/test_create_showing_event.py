#!/usr/bin/env python3
"""calendar_logic.create_showing_event - the function that books a showing.

Untested until now. The fold and cancel paths around it are covered
(test_audit_regressions.py, test_alex_only_showings.py), but the one that
puts a brand-new event on Alex's calendar and writes the zillow_showings doc
ran unasserted - while the whole booking order ("validate -> create/fold
event -> confirmation send -> labels", the ATOMIC BOOKING TRIPWIRE) is built
on it raising when the write did not happen.

What it costs when it is wrong:

  * returning an id that was never created, or "" read as an id, confirms a
    showing that is not on anyone's calendar - the renter arrives at an empty
    house. This is the phantom-fold shape of the 2026-08-25 Alec bug, where
    the email went out and the calendar write had quietly been rejected.
  * a missing or wrong zillow_showings doc is the authority for who is on the
    showing: agent_name_from_event and remove_renter_or_cancel read it, so a
    bad write sends the wrong agent or deletes a shared event.
  * wrong attendees / send_updates means nobody is actually invited.
"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calendar_logic as cal  # noqa: E402
import rules  # noqa: E402

ADDRESS = "26009 S New Town Dr, Queen Creek, AZ 85142"
# Wednesday 2026-11-04, 3:30 PM Phoenix - inside the Wed 10:00-18:30 window.
START = datetime(2026, 11, 4, 15, 30, tzinfo=rules.AZ_TZ)


class _Calendar:
    """Records the Composio call and the showings-ledger write."""

    def __init__(self, monkeypatch, response, ledger_error=None):
        self.calls = []
        self.showings = {}
        self.order = []
        self._response = response
        self._ledger_error = ledger_error
        monkeypatch.setattr(cal, "composio_execute", self._exec)
        monkeypatch.setattr(cal.ledger, "upsert_showing", self._upsert)

    def _exec(self, slug, params):
        self.calls.append((slug, params))
        self.order.append("calendar")
        return self._response

    def _upsert(self, event_id, **fields):
        self.order.append("ledger")
        if self._ledger_error:
            raise self._ledger_error
        self.showings.setdefault(event_id, {}).update(fields)

    @property
    def params(self):
        return self.calls[0][1]


OK = {"data": {"response_data": {"id": "ev_9f3c1a", "status": "confirmed"}}}


# ---------------------------------------------------------------- happy path

def test_books_a_thirty_minute_showing_with_the_exact_calendar_payload(monkeypatch):
    c = _Calendar(monkeypatch, OK)
    assert cal.create_showing_event(ADDRESS, "Bonnie", START, rules.ALEX) == "ev_9f3c1a"

    slug, p = c.calls[0]
    assert slug == "GOOGLECALENDAR_CREATE_EVENT"
    assert p["calendar_id"] == "primary"
    assert p["summary"] == "Showing — 26009 S New Town Dr with Bonnie"
    assert p["location"] == ADDRESS
    assert p["description"] == ("Zillow inquiry.\nInquirers: Bonnie.\n"
                                "Agent: Alex Foley (alex@azfoleyhomes.com).")
    # Local wall time, no offset: the timezone field is what places it.
    assert p["start_datetime"] == "2026-11-04T15:30:00"
    assert p["timezone"] == "America/Phoenix"
    assert p["event_duration_minutes"] == 30 == rules.SHOWING_MINUTES
    # send_updates="all" is what actually invites people to the showing.
    assert p["send_updates"] == "all"
    assert len(c.calls) == 1


def test_alex_showing_invites_alex_and_the_viewer_without_duplicates(monkeypatch):
    c = _Calendar(monkeypatch, OK)
    cal.create_showing_event(ADDRESS, "Bonnie", START, rules.ALEX)
    assert c.params["attendees"] == ["alex@azfoleyhomes.com",
                                     "azfoleyhomes@gmail.com"]


def test_an_override_agent_is_added_without_dropping_alex(monkeypatch):
    """A thread-level override ('have Jace cover') still books on Alex's
    calendar, so Alex stays on the invite and the description names Jace -
    agent_name_from_event and the fold confirmation read that name."""
    c = _Calendar(monkeypatch, OK)
    cal.create_showing_event(ADDRESS, "Bonnie", START, rules.JACE)
    assert c.params["attendees"] == ["alex@azfoleyhomes.com",
                                     "azfoleyhomes@gmail.com",
                                     "jacejohnson.re@gmail.com"]
    assert c.params["description"].endswith(
        "Agent: Jace Johnson (jacejohnson.re@gmail.com).")


def test_the_summary_keeps_only_the_street_line(monkeypatch):
    c = _Calendar(monkeypatch, OK)
    cal.create_showing_event("  2118 S El Marino , Mesa, AZ, 85202  ",
                             "Owen", START, rules.ALEX)
    assert c.params["summary"] == "Showing — 2118 S El Marino with Owen"
    assert c.params["location"] == "  2118 S El Marino , Mesa, AZ, 85202  "


# ---------------------------------------------------------------- ledger doc

def test_the_showings_ledger_records_who_is_on_the_showing(monkeypatch):
    """zillow_showings owns agent_name and renters (calendar-as-projection,
    2026-08-25). Event descriptions are rendered FROM this doc."""
    c = _Calendar(monkeypatch, OK)
    event_id = cal.create_showing_event(ADDRESS, "Bonnie", START, rules.JACE)
    assert c.showings == {event_id: {
        "address": ADDRESS,
        "start_iso": "2026-11-04T15:30:00-07:00",
        "agent_name": "Jace Johnson",
        "renters": ["Bonnie"],
    }}


def test_the_calendar_event_exists_before_the_ledger_doc(monkeypatch):
    """Ledger-first would leave a renter recorded on a showing that failed to
    create."""
    c = _Calendar(monkeypatch, OK)
    cal.create_showing_event(ADDRESS, "Bonnie", START, rules.ALEX)
    assert c.order == ["calendar", "ledger"]


def test_a_ledger_write_failure_fails_the_booking(monkeypatch):
    """"A showing whose renters can't be recorded must not confirm." The
    raise is what lands this in the book path's mark_failed, so no
    confirmation email goes out."""
    c = _Calendar(monkeypatch, OK, ledger_error=RuntimeError("firestore down"))
    with pytest.raises(RuntimeError, match="firestore down"):
        cal.create_showing_event(ADDRESS, "Bonnie", START, rules.ALEX)
    assert c.showings == {}


# ---------------------------------------------------------------- id extraction
# Composio's response envelope has drifted repeatedly on this account, and an
# id read out of the wrong nest means a confirmed showing nobody can cancel.

@pytest.mark.parametrize("response", [
    {"data": {"response_data": {"id": "ev_9f3c1a"}}},
    {"data": {"event": {"id": "ev_9f3c1a"}}},
    {"data": {"id": "ev_9f3c1a"}},
    {"id": "ev_9f3c1a"},
])
def test_the_event_id_is_found_in_every_envelope_shape(monkeypatch, response):
    c = _Calendar(monkeypatch, response)
    assert cal.create_showing_event(ADDRESS, "Bonnie", START, rules.ALEX) == "ev_9f3c1a"
    assert c.showings["ev_9f3c1a"]["renters"] == ["Bonnie"]


@pytest.mark.parametrize("response", [
    {"successful": False, "error": "400 Invalid calendar id"},
    {"data": {"response_data": {}}},
    {"data": {"response_data": {"id": ""}}},
    {"data": {}},
    {},
])
def test_no_event_id_raises_and_records_nothing(monkeypatch, response):
    """A rejected or unrecognizable create must fail loud. Returning "" here
    is how an email confirms a showing that was never put on the calendar."""
    c = _Calendar(monkeypatch, response)
    with pytest.raises(RuntimeError, match="create_event returned no id"):
        cal.create_showing_event(ADDRESS, "Bonnie", START, rules.ALEX)
    assert c.showings == {}


def test_a_composio_transport_error_is_not_swallowed(monkeypatch):
    c = _Calendar(monkeypatch, OK)
    monkeypatch.setattr(cal, "composio_execute",
                        lambda slug, p: (_ for _ in ()).throw(
                            RuntimeError("Composio HTTP 502")))
    with pytest.raises(RuntimeError, match="Composio HTTP 502"):
        cal.create_showing_event(ADDRESS, "Bonnie", START, rules.ALEX)
    assert c.showings == {}
