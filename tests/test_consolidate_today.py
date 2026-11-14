"""Consolidate on "today" and before countering (Alex 2026-11-04, Kylinn at
1018 E Marigold: asked for 4:00 today at 2:17pm while a 5:30 showing sat on the
calendar; "if they say today we always offer the time we already have")."""
from datetime import datetime, timedelta

import calendar_logic as cal
import llm
import rules
import templates as T

AZ = rules.AZ_TZ
ADDR = "1018 E Marigold Ln, Tempe, AZ, 85281"


def _future_monday():
    d = datetime.now(AZ).date() + timedelta(days=14)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


def _showing(d, hh, mm):
    s = datetime(d.year, d.month, d.day, hh, mm, tzinfo=AZ)
    return {"id": "evL", "summary": "Showing — 1018 E Marigold Ln with Leyna",
            "location": "1018 E Marigold Ln, Tempe, AZ 85281",
            "start": {"dateTime": s.isoformat()},
            "end": {"dateTime": (s + timedelta(minutes=30)).isoformat()}}


def test_today_request_folds_into_todays_showing_even_hours_apart():
    d = _future_monday()
    now = datetime(d.year, d.month, d.day, 9, 0, tzinfo=AZ)
    ask = datetime(d.year, d.month, d.day, 11, 0, tzinfo=AZ)   # 6.5h before 5:30
    v = cal.validate_slot(ask, ADDR, [_showing(d, 17, 30)], now_az=now)
    assert not v["ok"] and v["reason"] == "same-house-slot-exists"
    assert v["fold"]["event"]["id"] == "evL"


def test_other_day_keeps_the_six_hour_window():
    d = _future_monday()
    now = datetime(d.year, d.month, d.day, 9, 0, tzinfo=AZ) - timedelta(days=1)
    ask = datetime(d.year, d.month, d.day, 11, 0, tzinfo=AZ)
    v = cal.validate_slot(ask, ADDR, [_showing(d, 17, 30)], now_az=now)
    assert v.get("reason") != "same-house-slot-exists"


def test_existing_showing_is_findable_for_the_counter():
    d = _future_monday()
    hits = cal.find_existing_showings(ADDR, events=[_showing(d, 17, 30)],
                                      min_lead_hours=1, limit=10)
    assert hits and hits[0]["start_az"].hour == 17


def test_counter_existing_template_names_both_times():
    body = T.counter_existing("Kylinn", "1018 E Marigold Ln", "today at 4:00 PM",
                              "today, Wednesday, November 4, at 5:30 PM")
    assert "(today at 4:00 PM) won't work" in body
    assert "5:30 PM" in body and "1018 E Marigold Ln" in body
    assert "Great question" not in body


def test_review_prompt_allows_counters():
    assert "counter_existing" in llm.REVIEW_PROMPT
    assert "Block such a reply only if a time it OFFERS" in llm.REVIEW_PROMPT
