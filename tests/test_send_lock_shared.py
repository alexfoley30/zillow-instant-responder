"""The send lock, the review gate and the label retry have ONE implementation.

The reserve -> recover -> takeover dance, the review-block escalation and the
label-failure queue used to be copy-pasted into four places (send_stage, the
accept fold, the adjacency-snap fold, the new booking). These tests pin the
shared helpers so a future edit cannot quietly apply to only one send path.
"""
import responder


def _quiet(monkeypatch):
    """Neutralise the collaborators the helpers reach for by default."""
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda tid: [])
    monkeypatch.setattr(responder.gm, "alex_replied_after", lambda msgs, mid: False)
    monkeypatch.setattr(responder.ledger, "mark_sent", lambda *a, **k: None)
    monkeypatch.setattr(responder.ledger, "mark_failed", lambda *a, **k: None)


# ------------------------------------------------------------- _claim_stage

def test_claim_proceeds_when_lock_is_won(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(responder.ledger, "reserve_send", lambda *a: "reserved")
    assert responder._claim_stage("t1", "first_reply", {}, "m1") is None


def test_claim_blocks_on_already_sent_and_in_flight(monkeypatch):
    _quiet(monkeypatch)
    for verdict in ("already-sent", "in-flight"):
        monkeypatch.setattr(responder.ledger, "reserve_send",
                            lambda *a, _v=verdict: _v)
        assert responder._claim_stage("t1", "s", {}, "m1") == f"skipped:{verdict}"


def test_claim_recover_backfills_when_alex_already_replied(monkeypatch):
    """Ambiguity resolves against the thread: Alex spoke, so nothing is owed."""
    _quiet(monkeypatch)
    marked = []
    monkeypatch.setattr(responder.ledger, "reserve_send", lambda *a: "recover")
    monkeypatch.setattr(responder.gm, "alex_replied_after", lambda msgs, mid: True)
    monkeypatch.setattr(responder.ledger, "mark_sent",
                        lambda tid, stage, **kw: marked.append((tid, stage, kw)))
    out = responder._claim_stage("t1", "booking_new", {}, "m1")
    assert out == "skipped:recovered-already-sent"
    assert marked == [("t1", "booking_new", {"recovered": "backfilled-from-gmail"})]


def test_claim_recover_takes_over_or_steps_aside(monkeypatch):
    _quiet(monkeypatch)
    monkeypatch.setattr(responder.ledger, "reserve_send", lambda *a: "recover")
    monkeypatch.setattr(responder.ledger, "takeover_send", lambda tid, stage: True)
    assert responder._claim_stage("t1", "s", {}, "m1") is None
    monkeypatch.setattr(responder.ledger, "takeover_send", lambda tid, stage: False)
    assert responder._claim_stage("t1", "s", {}, "m1") == "skipped:takeover-lost"


# ---------------------------------------------------------- _review_or_block

def test_review_pass_returns_none(monkeypatch):
    monkeypatch.setattr(responder, "review_gate", lambda *a, **k: (True, ""))
    assert responder._review_or_block("t1", "s", "body", "booking_new") is None


def test_review_block_fails_the_stage_and_escalates(monkeypatch):
    """mark_failed is what cron's review-blocked close-out recognises, so a
    block must always write it - and escalate once, at block time."""
    failed, escalated = [], []
    monkeypatch.setattr(responder, "review_gate",
                        lambda *a, **k: (False, "contradicts the renter"))
    monkeypatch.setattr(responder.ledger, "mark_failed",
                        lambda tid, stage, err: failed.append((tid, stage, err)))
    monkeypatch.setattr(responder, "_escalate_review_block",
                        lambda tid, why, tpl: escalated.append((tid, why, tpl)))
    out = responder._review_or_block("t1", "reply__m1", "body", "windows_ask")
    assert out == "no-send:review-blocked"
    assert failed == [("t1", "reply__m1",
                       "review-blocked: contradicts the renter")]
    assert escalated == [("t1", "contradicts the renter", "windows_ask")]


# -------------------------------------------------------------- _apply_labels

def test_labels_applied_once(monkeypatch):
    calls = []
    monkeypatch.setattr(responder.gm, "modify_labels",
                        lambda tid, add, rem: calls.append((tid, add, rem)))
    responder._apply_labels("t1", "s", ["L_HANDLED"], ["L_AWAIT"])
    assert calls == [("t1", ["L_HANDLED"], ["L_AWAIT"])]


def test_label_failure_is_queued_and_never_raises(monkeypatch):
    """A label failure must not fail the send that already went out."""
    pending = []

    def boom(*a):
        raise RuntimeError("Composio 503")

    monkeypatch.setattr(responder.gm, "modify_labels", boom)
    monkeypatch.setattr(responder.ledger, "set_labels_pending",
                        lambda tid, stage, add, rem: pending.append((tid, stage, add, rem)))
    responder._apply_labels("t1", "booking_new", ["L_HANDLED"], ["L_AWAIT"])
    assert pending == [("t1", "booking_new", ["L_HANDLED"], ["L_AWAIT"])]


def test_no_labels_is_a_no_op(monkeypatch):
    def boom(*a):
        raise AssertionError("must not call Gmail with nothing to change")

    monkeypatch.setattr(responder.gm, "modify_labels", boom)
    responder._apply_labels("t1", "nudge", [], [])
