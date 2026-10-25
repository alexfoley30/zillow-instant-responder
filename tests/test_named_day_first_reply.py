"""A first message that names a day gets asked for a time ON that day.

2026-10-24, Ashley (1018 E Marigold Ln, thread 1a16eec59561deee): "I'd love
the opportunity to tour on Monday if there is availability." The generic
first reply asked "what day and time works", the pre-send review blocked it
for ignoring her Monday, and she sat unanswered until Alex answered by hand.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import llm  # noqa: E402
import rules  # noqa: E402
import templates as T  # noqa: E402

ASHLEY = ("Good morning, I'd love the opportunity to tour on Monday if there "
          "is availability. Thank you!")
ADDRESS = "1018 E Marigold Ln, Tempe, AZ, 85281"


@pytest.mark.parametrize("text,day", [
    (ASHLEY, "Monday"),
    ("Could I see it Saturday?", "Saturday"),
    ("Is it available? I'm free tuesday afternoon", "Tuesday"),
    ("Any time on Fridays works for me", "Friday"),
    ("Is it pet friendly? I'm moving Monday-ish", "Monday"),
])
def test_named_day_found(text, day):
    assert rules.named_day_without_time(text) == day


@pytest.mark.parametrize("text", [
    "I would like to schedule a tour.",
    "Saturday or Sunday works for me",          # several days: generic ask
    "Monday at 2pm works",                       # clock time: generic ask
    "Can I come by Wednesday around 1?",
    "Thursday 10:30 would be great",
    "Monday works, noon if possible",
])
def test_no_single_day_without_time(text):
    assert rules.named_day_without_time(text) is None


def test_template_asks_for_time_on_the_named_day():
    body = T.availability_ask("Ashley", ADDRESS, day="Monday")
    assert "Monday works" in body
    assert "What time on Monday is best for you" in body
    assert "what day and time works" not in body


def test_template_without_day_is_unchanged():
    body = T.availability_ask("Ashley", ADDRESS)
    assert "what day and time works to come take a look" in body
    assert "Monday works" not in body


live = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY")
                          or os.environ.get("ANTHROPIC_API_KEY") == "test",
                          reason="live review replay needs ANTHROPIC_API_KEY")


@live
def test_live_review_sends_the_named_day_reply():
    body = T.availability_ask("Ashley", ADDRESS,
                              day=rules.named_day_without_time(ASHLEY))
    out = llm.review_reply(f"RENTER: {ASHLEY}", body, "availability_ask")
    assert out["verdict"] == "send", out
