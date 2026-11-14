"""Alex 2026-11-12: "everyone gets an app, and a response, period".

Four renters on 2701 E Willetta went quiet in one week:
- Jericka answered our offered Saturday 11:00 with "Sounds good!"; the
  classifier called it a sign-off and she was never booked.
- Aaliyah asked for an application and "a tour on Nov 16"; no weekday word,
  so her day was never read, the review blocked the plain ask, and she heard
  nothing for a day.
- Nicholas asked "Is this area safe?"; the fair-housing rule did not match.
- propose_times / counter first replies carried no application link.
Every test drives the real responder; only Gmail, Firestore, the calendar API
and the LLM are replaced (the harness from test_book_renter_windows)."""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DRY_RUN", "true")

import pytest  # noqa: E402

import facts  # noqa: E402
import responder  # noqa: E402
import rules  # noqa: E402
import templates as T  # noqa: E402
from test_book_renter_windows import _cand, _cls, _wire  # noqa: E402

AZ = rules.AZ_TZ
ADDR = "2701 E Willetta St, Phoenix, AZ, 85008"
RELAY = "relay@convo.zillow.com"
WED_3PM = datetime(2026, 11, 11, 15, 0, tzinfo=AZ)
THU_7AM = datetime(2026, 11, 12, 7, 33, tzinfo=AZ)


# ------------------------------------------------- Jericka: "Sounds good!"

def _offer_doc(state, offered_start):
    return {"renter_name": "Jericka", "property_address": ADDR,
            "relay_email": RELAY, "state": state,
            "offered_start_iso": offered_start.isoformat(),
            "offered_event_id": "evSAT", "last_reply_template": "offer_existing"}


def test_sounds_good_to_a_live_offer_books_even_if_called_a_closer(monkeypatch):
    _wire(monkeypatch, THU_7AM, {"Sounds good": _cls("benign_closer", [])})
    booked = []
    monkeypatch.setattr(responder, "book_accepted_offer",
                        lambda *a, **k: booked.append(a[0]) or "sent")
    doc = _offer_doc("NEEDS_HUMAN", datetime(2026, 11, 14, 11, 0, tzinfo=AZ))
    assert responder.handle_reply("t-j", doc, [], "Sounds good!", "m-j") == "sent"
    assert booked == ["t-j"]


def test_perfect_thanks_after_booking_stays_a_closer(monkeypatch):
    _wire(monkeypatch, THU_7AM, {"Perfect": _cls("benign_closer", [])})
    booked = []
    monkeypatch.setattr(responder, "book_accepted_offer",
                        lambda *a, **k: booked.append(a[0]) or "sent")
    doc = _offer_doc(responder.ledger.BOOKED,
                     datetime(2026, 11, 14, 11, 0, tzinfo=AZ))
    assert responder.handle_reply("t-j", doc, [], "Perfect, thanks!",
                                  "m-j2") == "no-send:benign"
    assert booked == []


def test_plain_thanks_to_a_live_offer_books_nothing(monkeypatch):
    _wire(monkeypatch, THU_7AM, {"Thanks": _cls("benign_closer", [])})
    booked = []
    monkeypatch.setattr(responder, "book_accepted_offer",
                        lambda *a, **k: booked.append(a[0]) or "sent")
    doc = _offer_doc("OFFERED", datetime(2026, 11, 14, 11, 0, tzinfo=AZ))
    assert responder.handle_reply("t-j", doc, [], "Thanks!", "m-j3") == \
        "no-send:benign"
    assert booked == []


# ------------------------------------------------- Aaliyah: "a tour on Nov 16"

AALIYAH = ("I'm interested in your property and would like to move forward. "
           "Can you send me an application for this property? Also I would "
           "greatly appreciate a tour on Nov 16 if you are available")


@pytest.mark.parametrize("text", [
    "a tour on Nov 16 if you are available", "Could I see it November 16th?",
    "Is 11/16 open?", "Can I come Dec. 3"])
def test_calendar_dates_are_schedule_hints(text):
    assert rules.has_schedule_hint(text)


@pytest.mark.parametrize("text", [
    "maybe 2 of us will come", "I have a 2 year old dog",
    "I would like to schedule a tour."])
def test_no_date_no_hint(text):
    assert not rules.has_schedule_hint(text)


def test_aaliyah_gets_times_on_nov_16_and_the_link(monkeypatch):
    cls = _cls("vague_time", [_cand("2026-11-16", raw="a tour on Nov 16")])
    calls = _wire(monkeypatch, WED_3PM, {"Nov 16": cls})
    out = responder.handle_new_inquiry("t-a", "Aaliyah", ADDR, RELAY, AALIYAH,
                                       "m-a")
    assert out == "sent"
    body = calls["replies"][0]
    assert "Monday, November 16" in body
    assert body.count(facts.APP_LINK) == 1
    assert "Great question" not in body
    assert calls["reserved"] == ["first_reply"]
    assert calls["nh"] == []


# ------------------------------------------------- nobody left in silence

def test_blocked_first_reply_still_sends_thanks_and_the_link(monkeypatch):
    calls = _wire(monkeypatch, WED_3PM, {},
                  reviews=[(False, "reply ignores the renter's day")])
    out = responder.handle_new_inquiry("t-b", "Sam", ADDR, RELAY,
                                       "I would like to schedule a tour.", "m-b")
    assert out == "sent"
    assert calls["reserved"] == ["first_reply", "first_reply__fallback"]
    assert len(calls["replies"]) == 1
    body = calls["replies"][0]
    assert body.startswith("Hi Sam,") and facts.APP_LINK in body
    assert "follow up with tour times" in body
    assert len(calls["nh"]) == 1          # Alex was still told to answer


def test_fallback_skipped_when_we_already_replied(monkeypatch):
    msgs = [{"id": "m-c", "from": "renter"}, {"id": "x", "from": "alex"}]
    calls = _wire(monkeypatch, WED_3PM, {}, reviews=[(False, "blocked")],
                  msgs=msgs)
    out = responder.handle_new_inquiry("t-c", "Sam", ADDR, RELAY,
                                       "I would like to schedule a tour.", "m-c")
    assert out == "no-send:review-blocked"
    assert calls["replies"] == []


def test_fallback_template_skips_the_review(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the fixed fallback must not be reviewed")
    monkeypatch.setattr(responder.llm, "review_reply", boom)
    assert responder.review_gate("t", "body", "first_reply_fallback") == (True, "")


def test_every_first_reply_carries_the_link(monkeypatch):
    calls = _wire(monkeypatch, WED_3PM, {})
    body = T.propose_times("Ann", ["Monday, November 16, at 10:00 AM"])
    assert facts.APP_LINK not in body
    responder.send_stage("t-p", "first_reply__retry", RELAY, body, [], [],
                         {"template": "propose_times"}, "m-p")
    responder.send_stage("t-p", "reply__m-q", RELAY, body, [], [],
                         {"template": "propose_times"}, "m-q")
    first, later = calls["replies"]
    assert first.count(facts.APP_LINK) == 1
    assert first.index(facts.APP_LINK) < first.index(T.SIGNATURE)
    assert facts.APP_LINK not in later


# ------------------------------------------------- Nicholas: "Is this area safe?"

@pytest.mark.parametrize("text", [
    "I'm actually still in Jersey rn. Moving to Phoenix in three weeks. "
    "Is this area safe?",
    "Is it a safe neighborhood?", "how's the neighborhood, is it a good area?",
    "Is the area safe at night?", "is this a bad part of town"])
def test_area_safety_is_fair_housing(text):
    assert facts.standing_rule_for(text) == "fair_housing"


@pytest.mark.parametrize("text", [
    "Is there a safe in the closet?", "Is the area by the pool shaded?",
    "What area of the yard gets sun?"])
def test_area_words_alone_are_not_fair_housing(text):
    assert facts.standing_rule_for(text) != "fair_housing"
