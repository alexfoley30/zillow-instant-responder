"""The shared send-lock / review-gate / label helpers.

send_stage and the three booking paths (fold, snap-fold, new event) each used
to hand-roll the same reserve -> recover -> takeover dance, the same
review-gate-then-mark-failed block, and the same label-with-queue-on-failure
block. The copies had already drifted: the booking paths returned
"skipped:recovered" and stamped recovered="backfilled" where send_stage
returned "skipped:recovered-already-sent" and stamped
recovered="backfilled-from-gmail", so the same event read two different ways
in the ledger depending on which path produced it.

These tests pin the single implementation and the fact that the send paths
route through it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import responder  # noqa: E402


# ---------------------------------------------------------------- the lock

def test_acquired_lock_returns_none(monkeypatch):
    monkeypatch.setattr(responder.ledger, "reserve_send", lambda t, s, m: "acquired")
    assert responder.acquire_send_lock("t1", "stage", {}) is None


def test_already_sent_and_in_flight_skip(monkeypatch):
    for verdict in ("already-sent", "in-flight"):
        monkeypatch.setattr(responder.ledger, "reserve_send",
                            lambda t, s, m, v=verdict: v)
        assert responder.acquire_send_lock("t1", "stage", {}) == f"skipped:{verdict}"


def test_recover_backfills_when_alex_already_replied(monkeypatch):
    marked = {}
    monkeypatch.setattr(responder.ledger, "reserve_send", lambda t, s, m: "recover")
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: [{"x": 1}])
    monkeypatch.setattr(responder.gm, "alex_replied_after", lambda msgs, mid: True)
    monkeypatch.setattr(responder.ledger, "mark_sent",
                        lambda t, s, **f: marked.update(f))
    out = responder.acquire_send_lock("t1", "stage", {}, "trigger-id")
    assert out == "skipped:recovered-already-sent"
    assert marked == {"recovered": "backfilled-from-gmail"}


def test_recover_takes_over_when_nobody_replied(monkeypatch):
    monkeypatch.setattr(responder.ledger, "reserve_send", lambda t, s, m: "recover")
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: [])
    monkeypatch.setattr(responder.gm, "alex_replied_after", lambda msgs, mid: False)
    monkeypatch.setattr(responder.ledger, "takeover_send", lambda t, s: True)
    assert responder.acquire_send_lock("t1", "stage", {}) is None


def test_lost_takeover_never_sends(monkeypatch):
    monkeypatch.setattr(responder.ledger, "reserve_send", lambda t, s, m: "recover")
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: [])
    monkeypatch.setattr(responder.gm, "alex_replied_after", lambda msgs, mid: False)
    monkeypatch.setattr(responder.ledger, "takeover_send", lambda t, s: False)
    assert responder.acquire_send_lock("t1", "stage", {}) == "skipped:takeover-lost"


# ---------------------------------------------------------------- review gate

def test_review_or_fail_passes_through(monkeypatch):
    monkeypatch.setattr(responder, "review_gate", lambda t, b, k: (True, ""))
    assert responder.review_or_fail("t1", "stage", "body", "tmpl") is None


def test_review_or_fail_marks_and_escalates(monkeypatch):
    seen = {}
    monkeypatch.setattr(responder, "review_gate",
                        lambda t, b, k: (False, "contradicts the renter"))
    monkeypatch.setattr(responder.ledger, "mark_failed",
                        lambda t, s, err: seen.update(error=err))
    monkeypatch.setattr(responder, "_escalate_review_block",
                        lambda t, why, tmpl: seen.update(escalated=tmpl))
    out = responder.review_or_fail("t1", "stage", "body", "booking_new")
    assert out == "no-send:review-blocked"
    assert seen == {"error": "review-blocked: contradicts the renter",
                    "escalated": "booking_new"}


# ---------------------------------------------------------------- labels

def test_apply_labels_noop_when_nothing_to_change(monkeypatch):
    calls = []
    monkeypatch.setattr(responder.gm, "modify_labels",
                        lambda *a, **k: calls.append(a))
    responder.apply_labels("t1", "stage", [], [])
    assert calls == []


def test_label_failure_queues_instead_of_raising(monkeypatch):
    queued = {}

    def boom(*_a, **_k):
        raise RuntimeError("gmail down")

    monkeypatch.setattr(responder.gm, "modify_labels", boom)
    monkeypatch.setattr(responder.ledger, "set_labels_pending",
                        lambda t, s, add, rm: queued.update(add=add, rm=rm))
    responder.apply_labels("t1", "stage", ["L_A"], ["L_B"], context="after fold")
    assert queued == {"add": ["L_A"], "rm": ["L_B"]}


# ------------------------------------------------- send paths use the lock

def test_send_stage_routes_through_the_shared_lock(monkeypatch):
    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(responder, "acquire_send_lock",
                        lambda *a, **k: "skipped:in-flight")
    out = responder.send_stage("t1", "stage", "r@convo.zillow.com", "body",
                               [], [], {"template": "windows_ask"})
    assert out == "skipped:in-flight"


def test_book_accepted_offer_routes_through_the_shared_lock(monkeypatch):
    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(responder, "_newer_renter_message_exists",
                        lambda t, m: False)
    monkeypatch.setattr(responder.cal, "find_existing_showings", lambda a: [
        {"event": {"id": "ev1"}, "when_human": "today, Friday, at 11:00 AM",
         "start_az": None}])
    monkeypatch.setattr(responder.cal, "agent_name_from_event",
                        lambda e: "Rhett Lueck")
    monkeypatch.setattr(responder, "acquire_send_lock",
                        lambda *a, **k: "skipped:in-flight")
    out = responder.book_accepted_offer("t1", {}, "Sam", "1641 E Coronado Rd",
                                        "r@convo.zillow.com", "m1")
    assert out == "skipped:in-flight"
