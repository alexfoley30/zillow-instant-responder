"""The three send primitives extracted from send_stage and the booking paths.

Before the extraction, claim_stage / review_or_block / apply_labels were
copy-pasted four times each (send_stage, fold, snap-fold, new event) and the
send_stage copy had already drifted to its own two recover strings. These
tests pin the behaviour of the single copy, including that drift, because the
recover branch has no coverage anywhere else and those strings land in
zillow_messages outcomes the watchdog reads.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import gmail_client as gm  # noqa: E402
import ledger  # noqa: E402
import responder  # noqa: E402


def _reserve(monkeypatch, verdict):
    monkeypatch.setattr(ledger, "reserve_send", lambda *a, **k: verdict)


# ---------------------------------------------------------------- claim_stage

def test_claim_stage_returns_empty_when_lock_acquired(monkeypatch):
    _reserve(monkeypatch, "acquired")
    assert responder.claim_stage("t1", "s1", {}) == ""


def test_claim_stage_passes_through_already_sent_and_in_flight(monkeypatch):
    for verdict in ("already-sent", "in-flight"):
        _reserve(monkeypatch, verdict)
        assert responder.claim_stage("t1", "s1", {}) == f"skipped:{verdict}"


def test_recover_backfills_when_alex_already_replied(monkeypatch):
    """A stale doc plus an Alex reply after the trigger = already handled."""
    _reserve(monkeypatch, "recover")
    monkeypatch.setattr(gm, "fetch_thread", lambda tid: [{"x": 1}])
    monkeypatch.setattr(gm, "alex_replied_after", lambda msgs, mid: True)
    marked = {}
    monkeypatch.setattr(ledger, "mark_sent",
                        lambda tid, sk, **f: marked.update(f))

    # booking-path defaults
    assert responder.claim_stage("t1", "s1", {}, "m1") == "skipped:recovered"
    assert marked == {"recovered": "backfilled"}

    # send_stage keeps its own two strings verbatim
    marked.clear()
    assert responder.claim_stage(
        "t1", "s1", {}, "m1",
        recovered_marker="backfilled-from-gmail",
        recovered_result="skipped:recovered-already-sent",
    ) == "skipped:recovered-already-sent"
    assert marked == {"recovered": "backfilled-from-gmail"}


def test_recover_takes_over_or_stands_down(monkeypatch):
    _reserve(monkeypatch, "recover")
    monkeypatch.setattr(gm, "fetch_thread", lambda tid: [])
    monkeypatch.setattr(gm, "alex_replied_after", lambda msgs, mid: False)

    monkeypatch.setattr(ledger, "takeover_send", lambda tid, sk: True)
    assert responder.claim_stage("t1", "s1", {}, "m1") == ""

    monkeypatch.setattr(ledger, "takeover_send", lambda tid, sk: False)
    assert responder.claim_stage("t1", "s1", {}, "m1") == "skipped:takeover-lost"


# ------------------------------------------------------------ review_or_block

def test_review_or_block_sends_on_pass(monkeypatch):
    monkeypatch.setattr(responder, "review_gate", lambda *a, **k: (True, ""))
    assert responder.review_or_block("t1", "s1", "body", "booking_new") == ""


def test_review_or_block_marks_failed_and_escalates(monkeypatch):
    monkeypatch.setattr(responder, "review_gate",
                        lambda *a, **k: (False, "contradicts the renter"))
    failed, escalated = {}, {}
    monkeypatch.setattr(ledger, "mark_failed",
                        lambda tid, sk, err: failed.update(stage=sk, err=err))
    monkeypatch.setattr(responder, "_escalate_review_block",
                        lambda tid, why, tmpl: escalated.update(why=why,
                                                                template=tmpl))
    out = responder.review_or_block("t1", "s1", "body", "booking_fold")
    assert out == "no-send:review-blocked"
    assert failed == {"stage": "s1",
                      "err": "review-blocked: contradicts the renter"}
    assert escalated == {"why": "contradicts the renter",
                         "template": "booking_fold"}


# -------------------------------------------------------------- apply_labels

def test_apply_labels_writes_once(monkeypatch):
    calls = []
    monkeypatch.setattr(gm, "modify_labels",
                        lambda tid, add, rm: calls.append((add, rm)))
    responder.apply_labels("t1", "s1", ["A"], ["B"])
    assert calls == [(["A"], ["B"])]


def test_apply_labels_skips_empty_label_sets(monkeypatch):
    calls = []
    monkeypatch.setattr(gm, "modify_labels",
                        lambda tid, add, rm: calls.append(1))
    responder.apply_labels("t1", "s1", [], [])
    assert calls == []


def test_label_failure_queues_for_retry_and_never_raises(monkeypatch):
    """A label failure must NEVER propagate - it would look like a send
    failure and invite a duplicate reply. The cron ticker retries from the
    send doc instead."""
    def _boom(tid, add, rm):
        raise RuntimeError("gmail 503")

    monkeypatch.setattr(gm, "modify_labels", _boom)
    pending = {}
    monkeypatch.setattr(ledger, "set_labels_pending",
                        lambda tid, sk, add, rm: pending.update(add=add, rm=rm))
    responder.apply_labels("t1", "s1", ["HANDLED"], ["AWAITING"], "fold")
    assert pending == {"add": ["HANDLED"], "rm": ["AWAITING"]}
