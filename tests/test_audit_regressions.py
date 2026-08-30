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
    """Capture composio + showings-ledger calls; feed the cancel/fold path a
    fixed event and an optional ledger renter list (None = legacy event with
    no zillow_showings doc, so the description parser is the fallback)."""

    def __init__(self, monkeypatch, event, ledger_renters=None):
        self.calls = []
        self.event = event
        self.showings = (
            {} if ledger_renters is None
            else {(event or {}).get("id"): {"renters": list(ledger_renters)}})
        monkeypatch.setattr(cal, "composio_execute", self._exec)
        monkeypatch.setattr(cal, "list_events",
                            lambda days=7, time_min=None: [event] if event else [])
        monkeypatch.setattr(cal.ledger, "get_showing", self.showings.get)
        monkeypatch.setattr(cal.ledger, "upsert_showing", self._upsert)
        monkeypatch.setattr(cal.ledger, "delete_showing",
                            lambda eid: self.showings.pop(eid, None))

    def _upsert(self, event_id, **fields):
        self.showings.setdefault(event_id, {}).update(
            {k: v for k, v in fields.items() if v is not None})

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


def test_ledger_renters_beat_description_parsing(monkeypatch):
    # Phase-A refactor: when a zillow_showings doc exists, it is the
    # authority - a mangled description no longer decides anything.
    ev = {"id": "ev1", "summary": "x", "description": "hand-edited garbage"}
    stub = _CalendarStub(monkeypatch, ev, ledger_renters=["Jessica", "Matthew"])
    assert cal.remove_renter_or_cancel("ev1", "Jessica") == "removed"
    assert stub.showings["ev1"]["renters"] == ["Matthew"]
    assert "GOOGLECALENDAR_DELETE_EVENT" not in stub.slugs()


def test_ledger_solo_cancel_removes_showing_doc(monkeypatch):
    ev = {"id": "ev1", "summary": "x", "description": ""}
    stub = _CalendarStub(monkeypatch, ev, ledger_renters=["Jessica"])
    assert cal.remove_renter_or_cancel("ev1", "Jessica") == "canceled"
    assert "GOOGLECALENDAR_DELETE_EVENT" in stub.slugs()
    assert "ev1" not in stub.showings


def test_fold_writes_showing_ledger(monkeypatch):
    ev = {"id": "ev1", "summary": "x", "description": PROD_SOLO}
    stub = _CalendarStub(monkeypatch, ev)  # legacy event, no doc yet
    cal.fold_renter_into_event(ev, "Sezer")
    assert stub.showings["ev1"]["renters"] == ["Jessica", "Sezer"]


def test_ledger_known_showing_survives_invisible_event(monkeypatch):
    # Event beyond the lookup window (or already gone): the ledger record
    # alone is enough to act instead of returning kept-unparsed.
    stub = _CalendarStub(monkeypatch, None)
    stub.showings["evX"] = {"renters": ["Jessica", "Matthew"]}
    assert cal.remove_renter_or_cancel("evX", "Matthew") == "removed"
    assert stub.showings["evX"]["renters"] == ["Jessica"]


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


# ------------------------------------------------- 5. Alec 8/25: consolidate once

def _next_wed():
    """Next Wednesday strictly in the future - these fixtures must never pin
    a calendar date (the 8/26-pinned original expired overnight: the fold
    lookup filters against the real clock)."""
    d = datetime.now(AZ).date() + timedelta(days=1)
    while d.weekday() != 2:
        d += timedelta(days=1)
    return d


def _chelsea_event():
    d = _next_wed()
    return {
        "id": "evC",
        "summary": "Showing — 2118 S El Marino with Chelsea",
        "location": "2118 S El Marino, Mesa, AZ, 85202",
        "description": ("Zillow inquiry.\nInquirers: Chelsea.\n"
                        "Agent: Rhett Lueck (rhettlueck@gmail.com)."),
        "start": {"dateTime": f"{d}T15:15:00-07:00"},
        "end": {"dateTime": f"{d}T15:45:00-07:00"},
    }


def _wed_ctx():
    """(now_az, slot_1800) with now = the evening before the showing day."""
    d = _next_wed()
    now = datetime(d.year, d.month, d.day, 18, 0, tzinfo=AZ) - timedelta(days=1)
    return now, datetime(d.year, d.month, d.day, 18, 0, tzinfo=AZ)


def test_same_house_fold_offered_first_time():
    now, slot = _wed_ctx()
    v = cal.validate_slot(slot, "2118 S El Marino, Mesa, AZ, 85202",
                          [_chelsea_event()], now_az=now)
    assert not v["ok"] and v["reason"] == "same-house-slot-exists"
    assert v["fold"]["event"]["id"] == "evC"


def test_same_house_bypassed_after_renter_declined_the_offer():
    # Alec 2026-08-25: he was offered 3:15 and answered "6pm" three times;
    # the fold rule re-offered 3:15 every time. With ignore_same_house the
    # renter's valid counter-time books instead.
    now, slot = _wed_ctx()
    v = cal.validate_slot(slot, "2118 S El Marino, Mesa, AZ, 85202",
                          [_chelsea_event()], now_az=now,
                          ignore_same_house=True)
    assert v["ok"], v
    assert v.get("fold") is None


def test_newer_renter_message_blocks_booking(monkeypatch):
    relay = "x@convo.zillow.com"
    msgs = [
        {"messageId": "m1", "sender": relay},
        {"messageId": "a1", "sender": "alex@azfoleyhomes.com"},
        {"messageId": "m2", "sender": relay},  # the unread correction
    ]
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda tid: msgs)
    assert responder._newer_renter_message_exists("t", "m1") is True
    assert responder._newer_renter_message_exists("t", "m2") is False
    monkeypatch.setattr(responder.gm, "fetch_thread",
                        lambda tid: (_ for _ in ()).throw(RuntimeError("net")))
    assert responder._newer_renter_message_exists("t", "m1") is False  # fail open


# ------------------------------------------------- 6. whole-thread ears (8/25)

def test_slot_acceptable_to_renter():
    slot = datetime(2026, 8, 26, 15, 15, tzinfo=AZ)
    assert cal.slot_acceptable_to_renter(slot) is True
    assert cal.slot_acceptable_to_renter(slot, earliest_daily="18:00") is False
    assert cal.slot_acceptable_to_renter(
        datetime(2026, 8, 26, 18, 0, tzinfo=AZ), earliest_daily="18:00") is True
    assert cal.slot_acceptable_to_renter(slot, latest_daily="12:00") is False
    assert cal.slot_acceptable_to_renter(
        slot, declined_iso=["2026-08-26T15:15"]) is False
    assert cal.slot_acceptable_to_renter(
        slot, declined_iso=["2026-08-26T18:00"]) is True


def test_validate_rejects_slot_renter_cannot_make():
    now, slot = _wed_ctx()
    v = cal.validate_slot(slot.replace(hour=15, minute=15),
                          "2118 S El Marino, Mesa, AZ, 85202", [],
                          now_az=now, earliest_daily="18:00")
    assert not v["ok"] and v["reason"] == "renter-cannot-make-it"


def test_fold_never_offers_slot_renter_cannot_make():
    # THE Alec ear: existing 3:15 showing, renter's whole thread says 6pm.
    # The fold must not fire; his 6pm books on its own merits instead.
    now, slot = _wed_ctx()
    v = cal.validate_slot(slot, "2118 S El Marino, Mesa, AZ, 85202",
                          [_chelsea_event()], now_az=now,
                          earliest_daily="18:00")
    assert v["ok"], v
    assert v.get("fold") is None


def test_explicit_proposal_overrides_daily_bound_but_not_declines():
    # A renter naming an exact time can obviously make that time: the book
    # path disables the daily bound (else Alec's "after 6pm weekdays" would
    # veto his own "Saturday 11am works"). Declined slots stay vetoed.
    now = datetime(2026, 8, 25, 9, 0, tzinfo=AZ)
    v = cal.validate_slot(datetime(2026, 8, 26, 11, 0, tzinfo=AZ),
                          "2118 S El Marino, Mesa, AZ, 85202", [],
                          now_az=now, earliest_daily="18:00",
                          enforce_daily_bounds=False)
    assert v["ok"], v
    v2 = cal.validate_slot(datetime(2026, 8, 26, 11, 0, tzinfo=AZ),
                           "2118 S El Marino, Mesa, AZ, 85202", [],
                           now_az=now, declined_iso=["2026-08-26T11:00"],
                           enforce_daily_bounds=False)
    assert not v2["ok"] and v2["reason"] == "renter-cannot-make-it"


def test_counter_slots_respect_daily_bound():
    # Alec 8/24: countered 1:00/1:30 PM to an after-6pm renter. Counters
    # must skip anything before earliest_daily.
    now = datetime(2026, 8, 25, 9, 0, tzinfo=AZ)  # Tue morning
    slots = cal.counter_slots("2118 S El Marino, Mesa, AZ, 85202", [],
                              now_az=now, earliest_daily="18:00")
    for s in slots:
        assert s.strftime("%H:%M") >= "18:00", s
        assert s.weekday() in (0, 2, 4), s  # only Mon/Wed/Fri windows reach 6pm


def test_classify_fallback_carries_convo_fields():
    import llm
    out = llm.classify_reply("random text", "", datetime(2026, 8, 25, 9, 0))
    for k in ("constraints", "earliest_daily", "latest_daily",
              "declined_times", "cancel_reason"):
        assert k in out, k


def test_thread_transcript_labels_and_strips():
    msgs = [
        {"sender": "x@convo.zillow.com",
         "messageText": "Alec says: I want a tour\nThanks for using Zillow"},
        {"sender": "Alex Foley <alex@azfoleyhomes.com>",
         "messageText": "Hi Alec, come at 3:15"},
        {"sender": "noreply@something.com", "messageText": "spam"},
    ]
    t = gm.thread_transcript(msgs)
    assert t.startswith("RENTER:")
    assert "US: Hi Alec" in t
    assert "spam" not in t


def test_renter_facts_precedence():
    doc = {"declined_slots_iso": ["2026-08-26T15:15"],
           "earliest_daily": "18:00"}
    fresh = {"declined_times": [{"date": "2026-08-27", "time": "10:00"}],
             "earliest_daily": None, "latest_daily": "20:00"}
    f = responder._renter_facts(doc, fresh)
    assert f["declined_iso"] == ["2026-08-27T10:00"]  # fresh extraction wins
    assert f["earliest_daily"] == "18:00"  # doc fills the gap
    assert f["latest_daily"] == "20:00"
    f2 = responder._renter_facts(doc, {})
    assert f2["declined_iso"] == ["2026-08-26T15:15"]  # doc fallback


def test_review_gate_blocks_and_fails_open(monkeypatch):
    msgs = [{"sender": "x@convo.zillow.com", "messageText": "Renter says: hi"}]
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda tid: msgs)
    monkeypatch.setattr(responder.llm, "review_reply",
                        lambda t, b, tpl: {"verdict": "block",
                                           "reason": "contradicts renter"})
    ok, why = responder.review_gate("t1", "body", "windows_ask")
    assert ok is False and "contradicts" in why
    monkeypatch.setattr(responder.llm, "review_reply",
                        lambda t, b, tpl: {"verdict": "send", "reason": ""})
    assert responder.review_gate("t1", "body", "windows_ask")[0] is True
    # Alex's approved words are never second-guessed
    assert responder.review_gate("t1", "body", "approved_answer")[0] is True
    # fetch failure fails open
    monkeypatch.setattr(responder.gm, "fetch_thread",
                        lambda tid: (_ for _ in ()).throw(RuntimeError("net")))
    monkeypatch.setattr(responder.llm, "review_reply",
                        lambda t, b, tpl: {"verdict": "block", "reason": "x"})
    assert responder.review_gate("t1", "body", "windows_ask")[0] is True


# ------------------------------------------------- 7. weekday snap ambiguity

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


# ------------------------------------------------- 8. bug-echo 8/27 fixes

def test_needs_human_stamp_only_when_a_channel_delivered(monkeypatch):
    calls = {"upserts": []}
    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(responder.ledger, "get_thread", lambda t: {})
    monkeypatch.setattr(responder.ledger, "transition", lambda *a, **k: None)
    monkeypatch.setattr(responder.ledger, "write_shadow", lambda *a, **k: None)
    monkeypatch.setattr(responder.ledger, "content_hash", lambda s: "x")
    monkeypatch.setattr(responder.ledger, "upsert_thread",
                        lambda t, **kw: calls["upserts"].append(kw))
    # both channels fail -> NO stamp (next run may retry)
    monkeypatch.setattr(responder.gm, "poke_ping", lambda m: False)
    monkeypatch.setattr(responder.gm, "alert_email", lambda s, b: False)
    responder.needs_human("t1", "Renter", "1 Test St", "hello")
    assert calls["upserts"] == []
    # one channel succeeds -> stamped
    monkeypatch.setattr(responder.gm, "alert_email", lambda s, b: True)
    responder.needs_human("t1", "Renter", "1 Test St", "hello")
    assert any("last_needs_human_ping_at" in kw for kw in calls["upserts"])


def test_agent_name_prefers_showings_ledger(monkeypatch):
    monkeypatch.setattr(cal.ledger, "get_showing",
                        lambda eid: {"agent_name": "Rhett Lueck"})
    ev = {"id": "e1", "description": "Agent: Jace Johnson (j@x.com)."}
    assert cal.agent_name_from_event(ev) == "Rhett Lueck"
    monkeypatch.setattr(cal.ledger, "get_showing", lambda eid: None)
    assert cal.agent_name_from_event(ev) == "Jace Johnson"


def _cancel_harness(monkeypatch, slots, newer=False):
    state = {"transitions": [], "sends": [], "pings": []}
    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(responder, "_newer_renter_message_exists",
                        lambda t, m: newer)
    monkeypatch.setattr(responder, "needs_human",
                        lambda *a, **k: state["pings"].append(a))
    monkeypatch.setattr(responder.cal, "remove_renter_or_cancel",
                        lambda e, n: "removed")
    monkeypatch.setattr(responder.cal, "find_existing_showings",
                        lambda a: slots)
    monkeypatch.setattr(responder, "send_stage",
                        lambda *a, **k: state["sends"].append(a) or "sent")
    monkeypatch.setattr(responder.ledger, "transition",
                        lambda t, s, **kw: state["transitions"].append((s, kw)))
    return state


def test_cancel_arms_the_offered_next_slot(monkeypatch):
    nxt = datetime(2026, 8, 28, 15, 0, tzinfo=AZ)
    slots = [{"event": {"id": "evNEXT"}, "start_az": nxt,
              "when_human": "Friday, August 28, at 3:00 PM"}]
    st = _cancel_harness(monkeypatch, slots)
    out = responder.handle_cancellation(
        "t1", {"event_id": "evOLD", "renter_name": "Jamie"}, "Jamie",
        "r@convo.zillow.com", "m1", cls={}, address="1 Test St")
    assert out == "sent"
    s, kw = st["transitions"][-1]
    assert s == responder.ledger.OFFERED
    assert kw["offered_event_id"] == "evNEXT"
    assert kw["event_id"] is None


def test_cancel_clears_stale_offer_when_no_next_slot(monkeypatch):
    st = _cancel_harness(monkeypatch, [])
    responder.handle_cancellation(
        "t1", {"event_id": "evOLD", "renter_name": "Jamie"}, "Jamie",
        "r@convo.zillow.com", "m1", cls={}, address="1 Test St")
    s, kw = st["transitions"][-1]
    assert s == responder.ledger.AWAITING_TIME
    assert kw["offered_event_id"] is None and kw["offered_start_iso"] is None


def test_cancel_defers_to_newer_renter_message(monkeypatch):
    st = _cancel_harness(monkeypatch, [], newer=True)
    out = responder.handle_cancellation(
        "t1", {"event_id": "evOLD", "renter_name": "Jamie"}, "Jamie",
        "r@convo.zillow.com", "m1", cls={}, address="1 Test St")
    assert out == "no-send:superseded-by-newer-message"
    assert st["pings"], "must escalate urgently"
    assert not st["sends"] and not st["transitions"]


def test_calendar_updates_send_full_field_set(monkeypatch):
    # Live probe 2026-08-27: Composio UPDATE_EVENT is a full REPLACE and
    # requires start_datetime - a description-only update erased the probe
    # event's title, attendees, and duration. Every update must rebuild the
    # complete field set from the event it just read.
    ev = {"id": "ev1", "summary": "Jace Showing: 2118 S El Marino",
          "location": "2118 S El Marino, Mesa, AZ, 85202",
          "description": PROD_FOLDED,
          "start": {"dateTime": "2030-01-09T15:15:00-07:00"},
          "end": {"dateTime": "2030-01-09T15:45:00-07:00"},
          "attendees": [{"email": "alex@azfoleyhomes.com"},
                        {"email": "jacejohnson.re@gmail.com"}]}
    stub = _CalendarStub(monkeypatch, ev)
    assert cal.remove_renter_or_cancel("ev1", "Jessica") == "removed"
    upd = next(p for s, p in stub.calls if s == "GOOGLECALENDAR_UPDATE_EVENT")
    assert upd["summary"] == "Jace Showing: 2118 S El Marino"
    assert upd["start_datetime"] == "2030-01-09T15:15:00"
    assert upd["event_duration_minutes"] == 30
    assert upd["attendees"] == ["alex@azfoleyhomes.com",
                                "jacejohnson.re@gmail.com"]
    assert upd["send_updates"] == "none"


def test_composio_failure_raises_loud(monkeypatch):
    ev = {"id": "ev1", "summary": "x", "description": PROD_SOLO,
          "start": {"dateTime": "2030-01-09T15:15:00-07:00"},
          "end": {"dateTime": "2030-01-09T15:45:00-07:00"}}
    stub = _CalendarStub(monkeypatch, ev)
    monkeypatch.setattr(cal, "composio_execute",
                        lambda slug, p: {"successful": False,
                                         "error": "400 whatever"})
    import pytest
    with pytest.raises(RuntimeError):
        cal.fold_renter_into_event(ev, "Sezer")


def test_newest_guard_lets_synthesized_reprocess_through(monkeypatch):
    """A reproc- id processes the newest renter message by construction, so
    the newer-message guard must not fire on it (Schneider strand, 8/30)."""
    import responder
    msgs = [
        {"sender": "Schneider <x@convo.zillow.com>", "messageId": "1a0504f3183c6691"},
        {"sender": "Alex Foley <alex@azfoleyhomes.com>", "messageId": "1a0505037ab4ba48"},
        {"sender": "Schneider <x@convo.zillow.com>", "messageId": "1a0545afd0fe9807"},
    ]
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda tid: msgs)
    monkeypatch.setattr(responder.gm, "is_from_relay",
                        lambda m: "convo.zillow.com" in m["sender"])
    monkeypatch.setattr(responder.gm, "msg_id", lambda m: m["messageId"])
    assert responder._newer_renter_message_exists(
        "1a0504f3183c6691", "reproc-1a0504f3183c6691-202608301342") is False
    # the real protection stays: a stale REAL trigger id is still superseded
    assert responder._newer_renter_message_exists(
        "1a0504f3183c6691", "1a0504f3183c6691") is True


# ------------------------------------------------- 8. adjacency snap (8/30)

def _snap_harness(monkeypatch, doc, propose_hhmm):
    """Run book_proposed_time against the Chelsea event with everything
    side-effectful recorded instead of executed."""
    calls = {"send_stage": [], "created": [], "folded": [], "transitions": [],
             "replies": []}
    ev = _chelsea_event()
    now, _ = _wed_ctx()
    d = _next_wed()
    monkeypatch.setattr(responder.cal, "list_events", lambda n: [ev])
    monkeypatch.setattr(responder, "_newer_renter_message_exists",
                        lambda t, m: False)
    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(responder, "review_gate", lambda t, b, k: (True, ""))
    monkeypatch.setattr(responder, "needs_human",
                        lambda *a, **k: calls.setdefault("nh", []).append(a))
    monkeypatch.setattr(
        responder, "send_stage",
        lambda tid, stage, relay, body, add, rm, meta, mid=None:
            calls["send_stage"].append((stage, meta.get("template"), body))
            or "sent")
    monkeypatch.setattr(responder.ledger, "reserve_send",
                        lambda t, s, m: "acquired")
    monkeypatch.setattr(responder.ledger, "mark_sent", lambda *a, **k: None)
    monkeypatch.setattr(responder.ledger, "mark_failed", lambda *a, **k: None)
    monkeypatch.setattr(
        responder.ledger, "transition",
        lambda t, st, **f: calls["transitions"].append((st, f)))
    monkeypatch.setattr(
        responder.cal, "fold_renter_into_event",
        lambda event, name: calls["folded"].append((event["id"], name)) or "evC")
    monkeypatch.setattr(
        responder.cal, "create_showing_event",
        lambda a, n, s, ag: calls["created"].append(s) or "evNEW")
    monkeypatch.setattr(responder.cal, "agent_name_from_event",
                        lambda e: "Rhett Lueck")
    monkeypatch.setattr(responder.gm, "send_reply",
                        lambda t, r, b: calls["replies"].append(b))
    monkeypatch.setattr(responder.gm, "poke_ping", lambda m: None)
    monkeypatch.setattr(responder.gm, "modify_labels", lambda *a, **k: None)
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: [])
    cls = {"time_candidates": [{"date": d.isoformat(), "time": propose_hhmm,
                                "after": None, "before": None}]}
    out = responder.book_proposed_time(
        "t1", doc, "Sam", "2118 S El Marino, Mesa, AZ, 85202",
        "x@convo.zillow.com", cls, "m9", now)
    return out, calls


def test_snap_offers_existing_slot_within_the_hour(monkeypatch):
    # Renter declined the first offer, then proposed 16:00 next to the 15:15
    # showing: one more convenience offer, no second event, offer armed.
    doc = {"last_reply_template": "offer_existing"}
    out, calls = _snap_harness(monkeypatch, doc, "16:00")
    assert out == "sent"
    assert calls["created"] == [] and calls["folded"] == []
    assert calls["send_stage"][0][1] == "offer_existing_snap"
    st, fields = calls["transitions"][0]
    assert st == responder.ledger.OFFERED
    assert fields["offered_event_id"] == "evC" and fields["snap_offered"] is True


def test_snap_folds_when_minutes_apart(monkeypatch):
    # 15:18 vs the 15:15 showing: nothing to negotiate, fold + confirm.
    doc = {"last_reply_template": "offer_existing"}
    out, calls = _snap_harness(monkeypatch, doc, "15:18")
    assert out == "sent"
    assert calls["folded"] == [("evC", "Sam")] and calls["created"] == []
    assert any("You're all set" in b for b in calls["replies"])
    st, fields = calls["transitions"][0]
    assert st == responder.ledger.BOOKED and fields["event_id"] == "evC"


def test_snap_fires_once_then_renters_time_books(monkeypatch):
    # snap already spent: their valid 16:00 books its own event, no third push.
    doc = {"last_reply_template": "offer_existing_snap", "snap_offered": True}
    out, calls = _snap_harness(monkeypatch, doc, "16:00")
    assert out == "sent"
    assert calls["folded"] == [] and len(calls["created"]) == 1
    assert not any(t == "offer_existing_snap"
                   for _, t, _ in calls["send_stage"])
