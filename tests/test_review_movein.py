"""Pre-send review must not treat a move-in window as tour availability.

2026-10-12, Alexandra (1018 E Marigold Ln, thread 1a12f75c033b7499): she
asked to tour "this Wednesday afternoon, likely around 1 or 2" and said she
moves "between October 28th and November 1st". The booking for Wed Oct 14
1:00 PM was review-blocked ("October 14 is outside their available
timeframe"), the thread went NEEDS_HUMAN and the renter sat unanswered all
day. These tests run the REAL review_reply against her real conversation.
"""

import json
import os
import sys
import types
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import llm  # noqa: E402
import templates as T  # noqa: E402

ALEXANDRA_TRANSCRIPT = (
    "RENTER: I would like to schedule a tour.\n"
    "US: Hi Alexandra, Thanks for reaching out about 1018 E Marigold Ln, "
    "Tempe, AZ, 85281! I'd love to get you in to see it. When are you hoping "
    "to move, and what day and time works to come take a look? Here's when "
    "I have open this week (Phoenix time): Mon, Wed, Fri: 10:00 AM to 6:30 PM "
    "Tue, Thu: 10:00 AM to 3:00 PM Sat, Sun: 10:00 AM to 2:00 PM Pick a time "
    "that works and I'll lock it in for you.\n"
    "RENTER: I'm looking to move in a few weeks between October 28th and "
    "November 1st. I have time this Wednesday afternoon, likely around 1 or 2 "
    "if that works?"
)

NOW = datetime(2026, 10, 12, 5, 10)  # when her reply arrived, Phoenix

BOOKING_BODY = T.booking_confirmation(
    "Alexandra", "1018 E Marigold Ln, Tempe, AZ, 85281",
    "Wednesday, October 14 at 1:00 PM", "Rhett Lueck")


def test_prompt_separates_move_in_from_tour_time():
    p = llm.REVIEW_PROMPT
    assert "move-in" in p.lower()
    assert "not when they can TOUR" in p


def test_review_reply_sends_full_prompt_and_passes_verdict(monkeypatch):
    seen = {}

    class _Msgs:
        def create(self, **kw):
            seen["prompt"] = kw["messages"][0]["content"]
            block = types.SimpleNamespace(
                type="text", text=json.dumps({"verdict": "send", "reason": "ok"}))
            return types.SimpleNamespace(content=[block])

    class _Client:
        def __init__(self, **kw):
            self.messages = _Msgs()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(Anthropic=_Client))
    out = llm.review_reply(ALEXANDRA_TRANSCRIPT, BOOKING_BODY, "booking_new",
                           now_phx=NOW)
    assert out["verdict"] == "send"
    assert "Monday, October 12, 2026" in seen["prompt"]
    assert "October 28th and November 1st" in seen["prompt"]
    assert "not when they can TOUR" in seen["prompt"]


live = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY")
                          or os.environ.get("ANTHROPIC_API_KEY") == "test",
                          reason="live review replay needs ANTHROPIC_API_KEY")


@live
def test_live_alexandra_booking_now_sends():
    out = llm.review_reply(ALEXANDRA_TRANSCRIPT, BOOKING_BODY, "booking_new",
                           now_phx=NOW)
    assert out["verdict"] == "send", out


@live
def test_live_real_tour_conflict_still_blocks():
    transcript = ALEXANDRA_TRANSCRIPT.replace(
        "I have time this Wednesday afternoon, likely around 1 or 2 if that "
        "works?", "I can't do Wednesday at all, Thursday after 2 is best.")
    out = llm.review_reply(transcript, BOOKING_BODY, "booking_new", now_phx=NOW)
    assert out["verdict"] == "block", out
