"""Locks down the two abstractions pulled out of hand-copied code (2026-11-26).

1. The send-lock ladder (reserve -> already-sent/in-flight -> recover ->
   Gmail-check -> takeover), the pre-send review escalation, and the
   label-or-queue tail each existed in FOUR copies: send_stage,
   book_accepted_offer, and both branches of _book_valid_slot. These tests
   pin the single copy's behaviour, including send_stage's distinct recovery
   audit strings, so the copies cannot come back unnoticed.

2. route_question's if/elif chain re-listed facts._RULES by hand and had
   fallen two rules behind it: `application_request` and
   `no_business_sublease` matched a question, found no branch and escalated
   to a human, while the SAME question in a first message was answered inline
   from facts.ANSWER_LINES (Trever, 250 W Queen Creek #102, 2026-11-12;
   Alex: "everyone gets an app, and a response, period").
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DRY_RUN", "true")

import facts  # noqa: E402
import responder  # noqa: E402
import templates as T  # noqa: E402


# ------------------------------------------------------------- the send lock

def _lock(monkeypatch, verdict, alex_replied=False, takeover=True):
    calls = {"sent": [], "takeovers": []}
    led = responder.ledger
    monkeypatch.setattr(led, "reserve_send", lambda t, s, m: verdict)
    monkeypatch.setattr(led, "mark_sent",
                        lambda t, s, **f: calls["sent"].append((s, f)))
    monkeypatch.setattr(led, "takeover_send",
                        lambda t, s: calls["takeovers"].append(s) or takeover)
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: [])
    monkeypatch.setattr(responder.gm, "alex_replied_after",
                        lambda m, mid: alex_replied)
    return calls


def test_acquired_lock_lets_the_caller_through(monkeypatch):
    _lock(monkeypatch, "acquired")
    assert responder._acquire_send_lock("t1", "first_reply", {}) is None


def test_already_sent_and_in_flight_skip(monkeypatch):
    _lock(monkeypatch, "already-sent")
    assert responder._acquire_send_lock("t1", "s", {}) == "skipped:already-sent"
    _lock(monkeypatch, "in-flight")
    assert responder._acquire_send_lock("t1", "s", {}) == "skipped:in-flight"


def test_recover_backfills_when_alex_already_replied(monkeypatch):
    calls = _lock(monkeypatch, "recover", alex_replied=True)
    assert responder._acquire_send_lock("t1", "s", {}, "m1") == "skipped:recovered"
    assert calls["sent"] == [("s", {"recovered": "backfilled"})]
    assert calls["takeovers"] == []


def test_send_stage_keeps_its_own_recovery_audit_strings(monkeypatch):
    # send_stage recorded "backfilled-from-gmail" / "recovered-already-sent"
    # while the three booking copies said "backfilled" / "recovered". The
    # shared helper must still be able to say either.
    calls = _lock(monkeypatch, "recover", alex_replied=True)
    out = responder._acquire_send_lock(
        "t1", "s", {}, "m1", recovered_tag="backfilled-from-gmail",
        recovered_result="skipped:recovered-already-sent")
    assert out == "skipped:recovered-already-sent"
    assert calls["sent"] == [("s", {"recovered": "backfilled-from-gmail"})]


def test_recover_takes_over_a_stale_doc(monkeypatch):
    calls = _lock(monkeypatch, "recover", alex_replied=False, takeover=True)
    assert responder._acquire_send_lock("t1", "s", {}, "m1") is None
    assert calls["takeovers"] == ["s"]
    assert calls["sent"] == []


def test_lost_takeover_never_sends(monkeypatch):
    _lock(monkeypatch, "recover", alex_replied=False, takeover=False)
    assert responder._acquire_send_lock("t1", "s", {}, "m1") == \
        "skipped:takeover-lost"


# -------------------------------------------------------- the review gate tail

def test_review_send_verdict_lets_the_caller_through(monkeypatch):
    monkeypatch.setattr(responder, "review_gate", lambda *a, **k: (True, ""))
    assert responder._review_or_escalate("t1", "s", "body", "booking_new") is None


def test_review_block_marks_failed_and_escalates_once(monkeypatch):
    failures, escalations = [], []
    monkeypatch.setattr(responder, "review_gate",
                        lambda *a, **k: (False, "contradicts the renter"))
    monkeypatch.setattr(responder.ledger, "mark_failed",
                        lambda t, s, e: failures.append((s, e)))
    monkeypatch.setattr(responder, "_escalate_review_block",
                        lambda t, r, tpl: escalations.append((r, tpl)))
    assert responder._review_or_escalate("t1", "s", "body", "booking_new") == \
        "no-send:review-blocked"
    assert failures == [("s", "review-blocked: contradicts the renter")]
    assert escalations == [("contradicts the renter", "booking_new")]


# ------------------------------------------------------------- the label tail

def _labels(monkeypatch, boom=False):
    calls = {"applied": [], "queued": []}

    def modify(thread_id, add, remove):
        if boom:
            raise RuntimeError("gmail down")
        calls["applied"].append((add, remove))

    monkeypatch.setattr(responder.gm, "modify_labels", modify)
    monkeypatch.setattr(responder.ledger, "set_labels_pending",
                        lambda t, s, a, r: calls["queued"].append((s, a, r)))
    return calls


def test_labels_applied_on_the_happy_path(monkeypatch):
    calls = _labels(monkeypatch)
    responder._apply_labels("t1", "s", ["L_ADD"], ["L_RM"], "booking")
    assert calls["applied"] == [(["L_ADD"], ["L_RM"])]
    assert calls["queued"] == []


def test_label_failure_is_queued_and_never_resends(monkeypatch):
    calls = _labels(monkeypatch, boom=True)
    responder._apply_labels("t1", "s", ["L_ADD"], ["L_RM"], "booking")
    assert calls["queued"] == [("s", ["L_ADD"], ["L_RM"])]


def test_no_labels_is_a_no_op(monkeypatch):
    calls = _labels(monkeypatch)
    responder._apply_labels("t1", "s", [], [], "nudge")
    assert calls["applied"] == [] and calls["queued"] == []


# ------------------------------------------------- standing-rule reply routing

def _route(monkeypatch, question):
    """route_question with the send captured. Returns (result, template, body)."""
    sent = {}

    def send_stage(thread_id, stage, relay, body, add, remove, meta, mid=""):
        sent.update(template=meta.get("template"), body=body, add=add,
                    remove=remove)
        return "sent"

    monkeypatch.setattr(responder, "send_stage", send_stage)
    monkeypatch.setattr(responder, "needs_human",
                        lambda *a, **k: sent.setdefault("escalated", True))
    monkeypatch.setattr(responder.gm, "modify_labels", lambda *a, **k: None)
    out = responder.route_question("t1", "Trever", "250 W Queen Creek Rd #102",
                                   "relay@convo.zillow.com", question, "m1")
    return out, sent


def test_application_request_on_a_reply_gets_the_link_not_a_ping(monkeypatch):
    # The drift the if/elif chain hid: this escalated silently before.
    out, sent = _route(monkeypatch, "Can you send me an application?")
    assert out == "sent"
    assert sent["template"] == "standing:application_request"
    assert facts.APP_LINK in sent["body"]
    assert "escalated" not in sent


def test_business_use_on_a_reply_gets_the_brokerage_decline(monkeypatch):
    out, sent = _route(
        monkeypatch, "Could I run a day care business out of the home?")
    assert out == "sent"
    assert sent["template"] == "standing:no_business_sublease"
    assert "residential use only" in sent["body"]


def test_every_standing_rule_can_answer_a_reply():
    """The invariant the table buys: no rule in facts._RULES can reach
    route_question without an answer to send."""
    answerable = (set(responder._STANDING_TEMPLATES)
                  | {k for k, v in facts.ANSWER_LINES.items() if v}
                  | {"apply_first"})
    assert {key for key, _rx in facts._RULES} <= answerable


def test_the_bespoke_templates_are_unchanged(monkeypatch):
    for question, template, expected in [
        ("Do you have a floor plan?", "standing:floor_plan",
         T.floor_plan_reply("Trever")),
        ("Can you install a doggy door?", "standing:as_is",
         T.as_is_reply("Trever")),
        ("Is this a safe neighborhood?", "standing:fair_housing",
         T.fair_housing_steer("Trever")),
    ]:
        _out, sent = _route(monkeypatch, question)
        assert sent["template"] == template
        assert sent["body"] == expected


def test_the_fact_answer_rules_are_unchanged(monkeypatch):
    for question, template, line in [
        ("Does it come with a washer and dryer?", "standing:appliances",
         facts.APPLIANCES_LINE),
        ("What are the requirements to get approved?", "standing:requirements",
         facts.REQUIREMENTS_LINE),
        ("Could we do a virtual tour?", "standing:virtual_tour",
         facts.VIRTUAL_TOUR_LINE),
        ("When is the earliest move in date?", "standing:move_in_now",
         facts.MOVE_IN_NOW_LINE),
    ]:
        _out, sent = _route(monkeypatch, question)
        assert sent["template"] == template
        assert sent["body"] == T.fact_answer("Trever", line)


def test_negotiation_still_goes_to_the_one_ack_path(monkeypatch):
    seen = []
    monkeypatch.setattr(responder, "handle_negotiation",
                        lambda t, n, r, m: seen.append(n) or "sent")
    assert responder.route_question(
        "t1", "Trever", "addr", "relay@convo.zillow.com",
        "Any flexibility on the rent?", "m1") == "sent"
    assert seen == ["Trever"]


def test_an_unknown_question_still_escalates_without_sending(monkeypatch):
    out, sent = _route(monkeypatch, "Which way does the back yard face?")
    assert out == "no-send:escalated"
    assert sent.get("escalated") is True
    assert "body" not in sent
