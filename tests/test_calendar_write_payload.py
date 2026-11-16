"""The calendar mutations themselves: `create_showing_event`, and the payload
`_full_update_fields` builds for every fold and partial cancel.

What was missing. `create_showing_event` is stubbed out in every responder test
(`lambda a, n, s, ag: "evNEW"`), so the function that actually books the showing
and writes the `zillow_showings` doc had no assertions on it. And the fold /
cancel tests that do exist assert only on the `description` of the update - yet
`_full_update_fields` exists because a live probe on 2026-08-27 found that
GOOGLECALENDAR_UPDATE_EVENT is a full REPLACE: every field left out of the call
is ERASED. A regression that dropped `attendees`, `summary` or the duration
would have passed the whole suite while silently stripping the agent and the
renter off a real showing on Alex's calendar.

Also covered here: `_composio_ok` (the 8/25 "phantom fold" - email sent, calendar
write quietly rejected with a 400) and the branches where the `zillow_showings`
ledger, not the event description, is the authority on who is on a showing.
Those ledger-authority branches are production's normal path since the 8/25
refactor; every existing cancel test runs the legacy description fallback.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import calendar_logic as cal  # noqa: E402
import rules  # noqa: E402

AZ = rules.AZ_TZ
ADDR = "2118 S El Marino, Mesa, AZ 85202"
START = datetime(2026, 11, 18, 14, 0, tzinfo=AZ)

# Exactly what create_showing_event writes today.
PROD_DESC = ("Zillow inquiry.\nInquirers: Jessica.\n"
             "Agent: Rhett Lueck (rhettlueck@gmail.com).")


def _event(**over):
    """A showing as GOOGLECALENDAR_EVENTS_LIST returns it."""
    ev = {
        "id": "ev_elmarino_1118",
        "summary": "Showing — 2118 S El Marino with Jessica",
        "location": ADDR,
        "description": PROD_DESC,
        "start": {"dateTime": "2026-11-18T14:00:00-07:00"},
        "end": {"dateTime": "2026-11-18T14:30:00-07:00"},
        "attendees": [{"email": "alex@azfoleyhomes.com"},
                      {"email": "rhettlueck@gmail.com"},
                      {"email": "azfoleyhomes@gmail.com"}],
    }
    ev.update(over)
    return ev


class _Calendar:
    """Composio + the showings ledger captured. Everything in calendar_logic
    under test is the real function."""

    def __init__(self, monkeypatch, events=(), showings=None, create_ok=True,
                 update_res=None):
        self.calls = []
        self.showings = dict(showings or {})
        self.deleted_showings = []
        self._events = list(events)
        self._create_ok = create_ok
        self._update_res = update_res
        monkeypatch.setattr(cal, "composio_execute", self._exec)
        monkeypatch.setattr(cal, "list_events",
                            lambda days=7, time_min=None: list(self._events))
        monkeypatch.setattr(cal.ledger, "get_showing", self.showings.get)
        monkeypatch.setattr(cal.ledger, "upsert_showing", self._upsert)
        monkeypatch.setattr(cal.ledger, "delete_showing", self._delete)

    def _upsert(self, event_id, **fields):
        self.showings.setdefault(event_id, {}).update(
            {k: v for k, v in fields.items() if v is not None})

    def _delete(self, event_id):
        self.deleted_showings.append(event_id)
        self.showings.pop(event_id, None)

    def _exec(self, slug, params):
        self.calls.append((slug, params))
        if slug == "GOOGLECALENDAR_CREATE_EVENT":
            if not self._create_ok:
                return {"successful": True, "data": {"response_data": {}}}
            return {"successful": True,
                    "data": {"response_data": {"id": "ev_new_7742",
                                               "status": "confirmed"}}}
        if slug == "GOOGLECALENDAR_UPDATE_EVENT" and self._update_res is not None:
            return self._update_res
        return {"successful": True, "data": {"successful": True}}

    def slugs(self):
        return [s for s, _ in self.calls]

    def payload(self, slug):
        return next(p for s, p in self.calls if s == slug)


# ------------------------------------------------- create_showing_event

def test_booking_writes_the_event_and_returns_its_id(monkeypatch):
    stub = _Calendar(monkeypatch)
    event_id = cal.create_showing_event(ADDR, "Jessica", START, rules.RHETT)

    assert event_id == "ev_new_7742"
    p = stub.payload("GOOGLECALENDAR_CREATE_EVENT")
    assert p["summary"] == "Showing — 2118 S El Marino with Jessica"
    assert p["location"] == ADDR
    assert p["start_datetime"] == "2026-11-18T14:00:00"
    assert p["event_duration_minutes"] == rules.SHOWING_MINUTES
    assert p["timezone"] == "America/Phoenix"
    assert p["send_updates"] == "all"
    assert cal.parse_inquirers(p["description"]) == ["Jessica"]
    assert "Agent: Rhett Lueck (rhettlueck@gmail.com)" in p["description"]


def test_booking_invites_alex_the_agent_and_the_viewer(monkeypatch):
    stub = _Calendar(monkeypatch)
    cal.create_showing_event(ADDR, "Jessica", START, rules.RHETT)

    assert stub.payload("GOOGLECALENDAR_CREATE_EVENT")["attendees"] == sorted([
        "alex@azfoleyhomes.com", "rhettlueck@gmail.com",
        "azfoleyhomes@gmail.com"])


def test_booking_records_who_is_on_the_showing(monkeypatch):
    """The zillow_showings doc is the authority the cancel path reads, so the
    booking has to write it - address, start, agent and renter."""
    stub = _Calendar(monkeypatch)
    cal.create_showing_event(ADDR, "Jessica", START, rules.RHETT)

    doc = stub.showings["ev_new_7742"]
    assert doc["renters"] == ["Jessica"]
    assert doc["agent_name"] == "Rhett Lueck"
    assert doc["address"] == ADDR
    assert doc["start_iso"] == START.isoformat()


def test_a_create_that_returns_no_id_raises(monkeypatch):
    """No id means no showing exists. Raising is what makes the book path
    mark the send failed instead of emailing a confirmation for nothing."""
    _Calendar(monkeypatch, create_ok=False)
    with pytest.raises(RuntimeError, match="no id"):
        cal.create_showing_event(ADDR, "Jessica", START, rules.ALEX)


def test_a_booking_whose_ledger_write_fails_does_not_confirm(monkeypatch):
    """A showing whose renters can't be recorded must not be treated as
    booked - same tripwire philosophy as event-before-email."""
    stub = _Calendar(monkeypatch)
    monkeypatch.setattr(cal.ledger, "upsert_showing",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("firestore unavailable")))
    with pytest.raises(RuntimeError, match="firestore unavailable"):
        cal.create_showing_event(ADDR, "Jessica", START, rules.ALEX)
    assert "GOOGLECALENDAR_CREATE_EVENT" in stub.slugs()


# ---------------------------------- update payload is a FULL replace (8/27)

def test_a_fold_resends_every_field_it_must_not_erase(monkeypatch):
    ev = _event()
    stub = _Calendar(monkeypatch, events=[ev],
                     showings={"ev_elmarino_1118": {"renters": ["Jessica"]}})
    cal.fold_renter_into_event(ev, "Matthew")

    p = stub.payload("GOOGLECALENDAR_UPDATE_EVENT")
    assert p["event_id"] == "ev_elmarino_1118"
    assert p["summary"] == "Showing — 2118 S El Marino with Jessica"
    assert p["location"] == ADDR
    assert p["attendees"] == ["alex@azfoleyhomes.com", "rhettlueck@gmail.com",
                              "azfoleyhomes@gmail.com"]
    assert p["start_datetime"] == "2026-11-18T14:00:00"
    assert p["event_duration_minutes"] == 30
    assert p["timezone"] == "America/Phoenix"
    assert p["send_updates"] == "none"
    assert cal.parse_inquirers(p["description"]) == ["Jessica", "Matthew"]


def test_the_update_keeps_a_longer_showings_real_duration(monkeypatch):
    """Duration is read off the event, not assumed: a 60-minute showing Alex
    stretched by hand must not be shortened to 30 by a fold."""
    ev = _event(end={"dateTime": "2026-11-18T15:00:00-07:00"})
    stub = _Calendar(monkeypatch, events=[ev],
                     showings={"ev_elmarino_1118": {"renters": ["Jessica"]}})
    cal.fold_renter_into_event(ev, "Matthew")

    assert stub.payload("GOOGLECALENDAR_UPDATE_EVENT")["event_duration_minutes"] == 60


def test_an_unparseable_time_falls_back_to_the_standard_length(monkeypatch):
    ev = _event(start={"dateTime": ""}, end={})
    stub = _Calendar(monkeypatch, events=[ev],
                     showings={"ev_elmarino_1118": {"renters": ["Jessica"]}})
    cal.fold_renter_into_event(ev, "Matthew")

    p = stub.payload("GOOGLECALENDAR_UPDATE_EVENT")
    assert p["event_duration_minutes"] == rules.SHOWING_MINUTES
    assert p["summary"] == "Showing — 2118 S El Marino with Jessica"


def test_an_event_with_no_attendees_omits_the_key(monkeypatch):
    """Never send attendees: [] - on a full replace that is a positive
    instruction to clear the invite list."""
    ev = _event()
    ev.pop("attendees")
    stub = _Calendar(monkeypatch, events=[ev],
                     showings={"ev_elmarino_1118": {"renters": ["Jessica"]}})
    cal.fold_renter_into_event(ev, "Matthew")

    assert "attendees" not in stub.payload("GOOGLECALENDAR_UPDATE_EVENT")


def test_the_agent_clause_survives_a_fold(monkeypatch):
    ev = _event()
    stub = _Calendar(monkeypatch, events=[ev],
                     showings={"ev_elmarino_1118": {"renters": ["Jessica"]}})
    cal.fold_renter_into_event(ev, "Matthew")

    desc = stub.payload("GOOGLECALENDAR_UPDATE_EVENT")["description"]
    assert "Agent: Rhett Lueck (rhettlueck@gmail.com)." in desc
    assert cal.agent_name_from_event({"id": "unknown", "description": desc}) \
        == "Rhett Lueck"


# ---------------------------------------- a rejected write must fail LOUD

def test_a_silently_rejected_fold_raises_instead_of_confirming(monkeypatch):
    """2026-08-25 (Alec): Composio returned a 400 the fold ignored, so the
    renter got "you're added" for a calendar write that never landed."""
    ev = _event()
    _Calendar(monkeypatch, events=[ev],
              showings={"ev_elmarino_1118": {"renters": ["Jessica"]}},
              update_res={"successful": False, "error": "400 invalid event id"})
    with pytest.raises(RuntimeError, match="fold update ev_elmarino_1118"):
        cal.fold_renter_into_event(ev, "Matthew")


def test_a_rejection_nested_in_the_data_envelope_also_raises(monkeypatch):
    ev = _event()
    _Calendar(monkeypatch, events=[ev],
              showings={"ev_elmarino_1118": {"renters": ["Jessica", "Matthew"]}},
              update_res={"data": {"successful": False, "error": "400"}})
    with pytest.raises(RuntimeError, match="remove update"):
        cal.remove_renter_or_cancel("ev_elmarino_1118", "Jessica")


# -------------------------- cancel: the showings ledger is the authority

def test_a_shared_showing_the_description_does_not_know_about_is_kept(monkeypatch):
    """The ledger carries both renters while the event description still reads
    solo. Trusting the description here is exactly how one renter's cancel
    deleted a shared consolidated event on 2026-08-21."""
    ev = _event()
    stub = _Calendar(monkeypatch, events=[ev],
                     showings={"ev_elmarino_1118": {"renters": ["Jessica", "Matthew"]}})

    assert cal.remove_renter_or_cancel("ev_elmarino_1118", "Matthew") == "removed"
    assert "GOOGLECALENDAR_DELETE_EVENT" not in stub.slugs()
    assert stub.showings["ev_elmarino_1118"]["renters"] == ["Jessica"]
    assert cal.parse_inquirers(
        stub.payload("GOOGLECALENDAR_UPDATE_EVENT")["description"]) == ["Jessica"]
    assert stub.deleted_showings == []


def test_the_last_renter_cancelling_deletes_event_and_showing_doc(monkeypatch):
    ev = _event()
    stub = _Calendar(monkeypatch, events=[ev],
                     showings={"ev_elmarino_1118": {"renters": ["Jessica"]}})

    assert cal.remove_renter_or_cancel("ev_elmarino_1118", "Jessica") == "canceled"
    assert stub.payload("GOOGLECALENDAR_DELETE_EVENT") == {
        "calendar_id": "primary", "event_id": "ev_elmarino_1118"}
    assert "GOOGLECALENDAR_UPDATE_EVENT" not in stub.slugs()
    assert stub.deleted_showings == ["ev_elmarino_1118"]


def test_a_renter_the_ledger_never_listed_cannot_delete_the_showing(monkeypatch):
    """Someone else's event. The description mentioning them is not enough -
    the ledger decides, and deleting is the dangerous branch."""
    ev = _event()
    stub = _Calendar(monkeypatch, events=[ev],
                     showings={"ev_elmarino_1118": {"renters": ["Matthew"]}})

    assert cal.remove_renter_or_cancel("ev_elmarino_1118", "Jessica") \
        == "kept-unparsed"
    assert stub.slugs() == []
    assert stub.showings["ev_elmarino_1118"]["renters"] == ["Matthew"]


def test_a_sole_renter_whose_event_is_already_gone_still_cleans_up(monkeypatch):
    """The ledger knows the showing but the event is no longer on the calendar
    (deleted by hand). The delete is still attempted and the stale showing doc
    must not be left behind to mis-answer the next cancel."""
    stub = _Calendar(monkeypatch, events=[],
                     showings={"ev_elmarino_1118": {"renters": ["Jessica"]}})

    assert cal.remove_renter_or_cancel("ev_elmarino_1118", "Jessica") == "canceled"
    assert stub.slugs() == ["GOOGLECALENDAR_DELETE_EVENT"]
    assert stub.deleted_showings == ["ev_elmarino_1118"]


def test_a_shared_showing_whose_event_is_invisible_is_never_deleted(monkeypatch):
    """Can't see the event, but the ledger says someone else is still on it:
    record the removal, delete nothing."""
    stub = _Calendar(monkeypatch, events=[],
                     showings={"ev_elmarino_1118": {"renters": ["Jessica", "Matthew"]}})

    assert cal.remove_renter_or_cancel("ev_elmarino_1118", "Jessica") == "removed"
    assert stub.slugs() == []
    assert stub.showings["ev_elmarino_1118"]["renters"] == ["Matthew"]
    assert stub.deleted_showings == []


def test_cancelling_is_name_case_insensitive(monkeypatch):
    ev = _event()
    stub = _Calendar(monkeypatch, events=[ev],
                     showings={"ev_elmarino_1118": {"renters": ["Jessica", "Matthew"]}})

    assert cal.remove_renter_or_cancel("ev_elmarino_1118", "  jessica ") == "removed"
    assert stub.showings["ev_elmarino_1118"]["renters"] == ["Matthew"]


def test_a_fold_recorded_in_the_ledger_is_idempotent(monkeypatch):
    """A redelivered booking must not add the renter twice or touch the
    calendar again."""
    ev = _event()
    stub = _Calendar(monkeypatch, events=[ev],
                     showings={"ev_elmarino_1118": {"renters": ["Jessica", "Matthew"]}})

    assert cal.fold_renter_into_event(ev, "matthew") == "ev_elmarino_1118"
    assert stub.slugs() == []
    assert stub.showings["ev_elmarino_1118"]["renters"] == ["Jessica", "Matthew"]
