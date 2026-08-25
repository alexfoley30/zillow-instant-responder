"""Regressions for the 2026-08-25 five-day audit.

Four HIGH findings, each locked down here against the REAL functions with
production-format data (the previous fold-cancel test validated an inline
copy of the logic against invented formatting, which is how a broken fix
shipped as fixed):

1. remove_renter_or_cancel never matched production descriptions - solo
   cancels left ghost events, shared events were un-editable.
2. /send-approved freshness guard ate approved answers whenever the
   pipeline's own ack was the last thread message.
3. no_business_sublease auto-declined innocent texts ("mother uses home
   health aide visits", "run a small business from my laptop").
4. The courtesy-closer override silently dropped real acceptances of live
   offers ("Sounds good!" after "say the word and I'll lock it in").
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import calendar_logic as cal  # noqa: E402
import facts  # noqa: E402
import gmail_client as gm  # noqa: E402
import responder  # noqa: E402
import rules  # noqa: E402

AZ = rules.AZ_TZ

PROD_SOLO = ("Zillow inquiry. Inquirer: Jessica. "
             "Agent: Alex Foley (alex@azfoleyhomes.com).")
PROD_FOLDED = ("Zillow inquiry. Inquirer: Jessica. "
               "Agent: Alex Foley (alex@azfoleyhomes.com)., Matthew.")


class _CalendarStub:
    """Capture composio calls; feed remove_renter_or_cancel a fixed event."""

    def __init__(self, monkeypatch, event):
        self.calls = []
        self.event = event
        monkeypatch.setattr(cal, "composio_execute", self._exec)
        monkeypatch.setattr(cal, "list_events",
                            lambda days=7, time_min=None: [event] if event else [])

    def _exec(self, slug, params):
        self.calls.append((slug, params))
        return {"data": {}}

    def slugs(self):
        return [s for s, _ in self.calls]


# ------------------------------------------------- 1. fold-aware cancel

def test_solo_cancel_production_format_deletes_event(monkeypatch):
    ev = {"id": "ev1", "summary": "Showing — 2118 S El Marino with Jessica",
          "description": PROD_SOLO}
    stub = _CalendarStub(monkeypatch, ev)
    assert cal.remove_renter_or_cancel("ev1", "Jessica") == "canceled"
    assert "GOOGLECALENDAR_DELETE_EVENT" in stub.slugs()


def test_shared_cancel_removes_only_that_renter(monkeypatch):
    ev = {"id": "ev1", "summary": "Jace Showing: 2118 S El Marino",
          "description": PROD_FOLDED}
    stub = _CalendarStub(monkeypatch, ev)
    assert cal.remove_renter_or_cancel("ev1", "Jessica") == "removed"
    assert "GOOGLECALENDAR_DELETE_EVENT" not in stub.slugs()
    update = next(p for s, p in stub.calls if s == "GOOGLECALENDAR_UPDATE_EVENT")
    assert "Matthew" in update["description"]
    assert "Jessica" not in update["description"]
    assert "Agent: Alex Foley (alex@azfoleyhomes.com)" in update["description"]


def test_first_listed_renter_removable_from_legacy_fold(monkeypatch):
    ev = {"id": "ev1", "summary": "x", "description": PROD_FOLDED}
    stub = _CalendarStub(monkeypatch, ev)
    assert cal.remove_renter_or_cancel("ev1", "Matthew") == "removed"
    update = next(p for s, p in stub.calls if s == "GOOGLECALENDAR_UPDATE_EVENT")
    assert "Jessica" in update["description"]


def test_unknown_renter_never_deletes_someone_elses_event(monkeypatch):
    ev = {"id": "ev1", "summary": "Showing — 1110 E Redfield with Adam",
          "description": PROD_SOLO}
    stub = _CalendarStub(monkeypatch, ev)
    assert cal.remove_renter_or_cancel("ev1", "Melissa") == "kept-unparsed"
    assert stub.slugs() == []


def test_event_not_found_keeps_instead_of_blind_delete(monkeypatch):
    stub = _CalendarStub(monkeypatch, None)
    assert cal.remove_renter_or_cancel("gone", "Jessica") == "kept-unparsed"
    assert stub.slugs() == []


def test_fold_then_cancel_roundtrip_canonical(monkeypatch):
    ev = {"id": "ev1", "summary": "x", "description": PROD_SOLO}
    stub = _CalendarStub(monkeypatch, ev)
    cal.fold_renter_into_event(ev, "Sezer")
    folded = next(p for s, p in stub.calls if s == "GOOGLECALENDAR_UPDATE_EVENT")
    assert cal.parse_inquirers(folded["description"]) == ["Jessica", "Sezer"]
    # fold is idempotent by NAME LIST, not substring-of-description
    ev2 = {"id": "ev1", "summary": "x", "description": folded["description"]}
    n_before = len(stub.calls)
    cal.fold_renter_into_event(ev2, "sezer")
    assert len(stub.calls) == n_before


def test_fold_renter_named_alex_not_swallowed_by_agent_email(monkeypatch):
    # Old idempotency check was `first_name in desc` - a renter named Alex
    # no-opped because "alex" sits inside the agent's email address.
    ev = {"id": "ev1", "summary": "x", "description": PROD_SOLO}
    stub = _CalendarStub(monkeypatch, ev)
    cal.fold_renter_into_event(ev, "Alex")
    update = next(p for s, p in stub.calls if s == "GOOGLECALENDAR_UPDATE_EVENT")
    assert cal.parse_inquirers(update["description"]) == ["Jessica", "Alex"]


def test_agent_name_read_from_event():
    ev = {"description": "Zillow inquiry.\nInquirers: A.\n"
                         "Agent: Rhett Lueck (rhettlueck@gmail.com)."}
    assert cal.agent_name_from_event(ev) == "Rhett Lueck"
    assert cal.agent_name_from_event({}) == "Alex Foley"


# ------------------------------------------------- 2. freshness guard input

def test_msg_time_parses_iso_epoch_and_rfc2822():
    iso = gm.msg_time({"messageTimestamp": "2026-08-24T21:54:03Z"})
    assert iso == datetime(2026, 8, 24, 21, 54, 3, tzinfo=timezone.utc)
    epoch_ms = str(int(iso.timestamp() * 1000))
    assert gm.msg_time({"internalDate": epoch_ms}) == iso
    rfc = gm.msg_time({"date": "Mon, 24 Aug 2026 14:54:03 -0700"})
    assert rfc == iso
    assert gm.msg_time({}) is None
    assert gm.msg_time({"messageTimestamp": "garbage"}) is None


# ------------------------------------------------- 3. sublease over-match

def test_sublease_rule_still_fires_on_real_business_use():
    hits = [
        "Can I sublease a room to my cousin?",
        "Is Airbnb allowed when we travel?",
        "I want to run my home healthcare business out of the property",
        "I have a cleaning business I'd like to operate from this home",
        "could we use the house for a daycare",
        "asking about short term rental options",
    ]
    for text in hits:
        assert facts.standing_rule_for(text) == "no_business_sublease", text


def test_sublease_rule_ignores_innocent_texts():
    innocents = [
        "My mother lives with us and uses home health aide visits",
        "I run a small business from my laptop",
        "We're opening a daycare nearby so the location is perfect",
        "My son is starting a new school in the fall",
    ]
    for text in innocents:
        assert facts.standing_rule_for(text) != "no_business_sublease", text


def test_corporate_housing_no_longer_auto_declined():
    assert facts.standing_rule_for(
        "Do you offer corporate housing terms?") != "no_business_sublease"


# ------------------------------------------------- 4. closer vs live offer

def _now():
    return datetime.now(AZ)


def test_offer_is_live_states():
    now = _now()
    future = (now + timedelta(hours=5)).isoformat()
    past = (now - timedelta(hours=5)).isoformat()
    assert responder._offer_is_live({"state": responder.ledger.OFFERED}, now)
    assert responder._offer_is_live({"offered_start_iso": future}, now)
    assert not responder._offer_is_live({"offered_start_iso": past}, now)
    assert responder._offer_is_live({"last_reply_template": "propose_times"}, now)
    assert not responder._offer_is_live({"last_reply_template": "booking_new"}, now)
    assert not responder._offer_is_live({}, now)


def test_affirmative_closer_is_a_yes_not_noise():
    for text in ("Sounds good!", "Perfect!", "Okay great."):
        assert responder._CLOSER_RE.match(text), text
        assert responder._AFFIRM_RE.search(text), text
    for text in ("Okay thank you", "Thanks!", "Got it", "No problem"):
        assert responder._CLOSER_RE.match(text), text
        assert not responder._AFFIRM_RE.search(text), text


# ------------------------------------------------- 5. weekday snap ambiguity

def test_snap_skips_multi_weekday_raw():
    now = datetime(2026, 8, 20, 18, 0, tzinfo=AZ)  # Thursday
    cls = {"time_candidates": [
        {"raw": "Saturday or Sunday works", "date": "2026-08-23",
         "time": "10:00"}]}
    out = rules.snap_weekday_dates(cls, now)
    assert out["time_candidates"][0]["date"] == "2026-08-23"  # untouched
    assert "weekday_snapped" not in out["time_candidates"][0]


def test_snap_still_fixes_single_weekday_mismatch():
    now = datetime(2026, 8, 20, 18, 0, tzinfo=AZ)  # Thursday
    cls = {"time_candidates": [
        {"raw": "Friday around 1", "date": "2026-08-22", "time": "13:00"}]}
    out = rules.snap_weekday_dates(cls, now)
    assert out["time_candidates"][0]["date"] == "2026-08-21"  # the Friday
    assert out["time_candidates"][0]["weekday_snapped"] is True
