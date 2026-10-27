"""A later reply that names a day gets real times on that day.

2026-10-27, Victor (30209 N Yucca Dr, thread 1a17c18a5f6ca383): at 11:45pm
he answered the first reply with "I would like to check it out tomorrow".
The vague-time path sent the week's windows again, the pre-send review
blocked it for ignoring his day, the escalation said "outgoing windows_ask
blocked by pre-send review", Poke rewrote that as "Victor wants to come by
... for the windows", and he sat unanswered all night.
"""

import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import calendar_logic as cal  # noqa: E402
import ledger  # noqa: E402
import responder  # noqa: E402
import rules  # noqa: E402

AZ = cal.AZ_TZ
VICTOR = "I would like to check it out tomorrow"
ADDRESS = "30209 N Yucca Dr, Florence, AZ, 85132"
MON_1145PM = datetime(2026, 10, 26, 23, 45, tzinfo=AZ)
TUE = date(2026, 10, 27)
WED = date(2026, 10, 28)


def _cls(dates):
    return {"intent": "vague_time",
            "time_candidates": [{"date": d, "time": None, "after": None,
                                 "before": None, "raw": "tomorrow"}
                                for d in dates]}


def test_late_night_tomorrow_names_both_days():
    assert rules.named_tour_dates(_cls(["2026-10-27"]), VICTOR,
                                  MON_1145PM) == [TUE, WED]


def test_after_midnight_tomorrow_includes_the_coming_morning():
    tue_1am = datetime(2026, 10, 27, 1, 0, tzinfo=AZ)
    assert rules.named_tour_dates(_cls(["2026-10-28"]), VICTOR,
                                  tue_1am) == [TUE, WED]


def test_daytime_tomorrow_is_one_day():
    mon_noon = datetime(2026, 10, 26, 12, 0, tzinfo=AZ)
    assert rules.named_tour_dates(_cls(["2026-10-27"]), VICTOR,
                                  mon_noon) == [TUE]


def test_tomorrow_without_llm_dates_still_resolves():
    mon_noon = datetime(2026, 10, 26, 12, 0, tzinfo=AZ)
    assert rules.named_tour_dates({}, VICTOR, mon_noon) == [TUE]


def test_weekday_without_llm_dates_resolves():
    mon_noon = datetime(2026, 10, 26, 12, 0, tzinfo=AZ)
    assert rules.named_tour_dates({}, "Could I see it Wednesday?",
                                  mon_noon) == [WED]


def test_clock_time_or_nothing_named_gives_no_dates():
    mon_noon = datetime(2026, 10, 26, 12, 0, tzinfo=AZ)
    assert rules.named_tour_dates(_cls(["2026-10-27"]),
                                  "tomorrow at 2pm", mon_noon) == []
    assert rules.named_tour_dates({}, "I would like to schedule a tour.",
                                  mon_noon) == []


def test_counter_slots_respects_only_dates(monkeypatch):
    monkeypatch.setattr(cal, "validate_slot", lambda *a, **k: {"ok": True})
    slots = cal.counter_slots(ADDRESS, [], now_az=MON_1145PM, count=2,
                              only_dates={WED})
    assert slots and all(s.date() == WED for s in slots)


def test_victor_reply_offers_times_on_his_days(monkeypatch):
    """The real handle_reply path: no windows-ask, one time on each day."""
    class FakeNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return MON_1145PM

    sent = {}
    monkeypatch.setattr(responder, "datetime", FakeNow)
    monkeypatch.setattr(responder.llm, "classify_reply",
                        lambda *a, **k: _cls(["2026-10-27"]))
    monkeypatch.setattr(responder.gm, "relay_from_thread",
                        lambda msgs: "relay@convo.zillow.com")
    monkeypatch.setattr(responder.gm, "thread_transcript", lambda msgs: "")
    monkeypatch.setattr(ledger, "record_message_outcome", lambda *a, **k: None)
    monkeypatch.setattr(ledger, "upsert_thread", lambda *a, **k: None)
    monkeypatch.setattr(ledger, "transition", lambda *a, **k: None)
    monkeypatch.setattr(ledger, "blocked_addresses", lambda: [])
    monkeypatch.setattr(responder.cal, "find_existing_showings", lambda a: [])
    monkeypatch.setattr(responder.cal, "list_events", lambda n: [])
    monkeypatch.setattr(cal, "validate_slot", lambda *a, **k: {"ok": True})

    def fake_send(thread_id, stage, relay, body, add, rm, meta, mid=""):
        sent["body"], sent["template"] = body, meta["template"]
        return "sent"
    monkeypatch.setattr(responder, "send_stage", fake_send)

    doc = {"renter_name": "Victor", "property_address": ADDRESS,
           "relay_email": "relay@convo.zillow.com",
           "last_reply_template": "availability_ask"}
    out = responder.handle_reply("t-victor", doc, [], VICTOR, "m1")
    assert out == "sent"
    assert sent["template"] == "propose_times"
    assert "Tuesday, October 27" in sent["body"]
    assert "Wednesday, October 28" in sent["body"]
    assert "What exact day and time" not in sent["body"]


def test_escalation_reads_in_plain_english(monkeypatch):
    pings, emails = [], []
    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(ledger, "get_thread", lambda t: {})
    monkeypatch.setattr(ledger, "transition", lambda *a, **k: None)
    monkeypatch.setattr(ledger, "upsert_thread", lambda *a, **k: None)
    monkeypatch.setattr(responder.gm, "poke_ping",
                        lambda m: pings.append(m) or True)
    monkeypatch.setattr(responder.gm, "alert_email",
                        lambda s, b: emails.append(b) or True)
    monkeypatch.setattr(ledger, "get_thread",
                        lambda t: {"renter_name": "Victor",
                                   "property_address": ADDRESS})
    responder._escalate_review_block("t-victor", "Renter said tomorrow",
                                     "windows_ask")
    assert pings[0].startswith("NUDGE\nURGENT same-day: Zillow renter Victor, "
                               "30209 N Yucca Dr:")
    assert "windows_ask" not in pings[0] and "windows_ask" not in emails[0]
