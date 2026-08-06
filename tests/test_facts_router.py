"""Regression tests for the deterministic standing-rule router in facts.py.

The as_is word-boundary cases lock down the 7/30-8/1 shadow bug: an
unanchored 'add' matched inside 'address'/'additional' in relay boilerplate
and misfired on 8 of 12 new inquiries.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import facts  # noqa: E402


def rule(q):
    return facts.standing_rule_for(q)


# ---------------------------------------------------------------- as_is

def test_as_is_real_modification():
    assert rule("Would you be able to install a doggy door?") == "as_is"
    assert rule("Can we paint the bedroom?") == "as_is"


def test_as_is_not_fooled_by_address():
    # 'add' inside 'address' - the exact 8-of-12 misfire.
    assert rule("What is the exact address?") != "as_is"


def test_as_is_not_fooled_by_additional():
    assert rule("Is there additional parking available?") != "as_is"


# ----------------------------------------------------------- requirements

def test_requirements_direct():
    assert rule("What are the requirements to get approved?") == "requirements"


def test_requirements_credit_phrasing():
    # Alondra (Coronado) and Daliana (Gold Dust), 8/5.
    assert rule("Are you flexible with good income but not good credit?") == "requirements"
    assert rule("My credit score isn't great, is that a problem?") == "requirements"


# ----------------------------------------------------------- virtual tour

def test_virtual_tour_variants():
    assert rule("Could we do a virtual tour instead?") == "virtual_tour"
    assert rule("Can we facetime to see the place?") == "virtual_tour"
    assert rule("Do you offer a video walkthrough?") == "virtual_tour"


# ------------------------------------------------------------- the rest

def test_appliances():
    assert rule("Does it come with a washer and dryer?") == "appliances"


def test_fair_housing():
    assert rule("Is the neighborhood safe?") == "fair_housing"


def test_negotiation_routes_to_apply_first():
    assert rule("Any flexibility on the rent?") == "apply_first"


def test_unknown_question_returns_none():
    # Unknown questions must escalate (needs_human), never guess an answer.
    assert rule("What is the average electric bill in summer?") is None


def test_answer_lines_cover_all_routable_rules():
    # Every rule the router can return on a NEW inquiry must have an inline
    # answer line (the one-send-per-inquiry fix depends on it).
    for key in ("as_is", "requirements", "virtual_tour", "appliances",
                "fair_housing", "apply_first", "floor_plan"):
        assert key in facts.ANSWER_LINES, f"missing ANSWER_LINES[{key}]"
        assert "Great question" not in facts.ANSWER_LINES[key]
