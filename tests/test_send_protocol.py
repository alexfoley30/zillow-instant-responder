"""The shared send-lock protocol (acquire_send_lock / review_or_block /
apply_labels / fyi_ping). These four steps used to be written out inline in
send_stage and again in each of the three booking paths; these tests pin the
one implementation they all now share. Nothing here touches the network."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import responder  # noqa: E402


def _no_escalation(monkeypatch):
    monkeypatch.setattr(responder, "needs_human", lambda *a, **k: None)


# ---------------------------------------------------------------- lock

def test_lock_acquired_returns_none(monkeypatch):
    monkeypatch.setattr(responder.ledger, "reserve_send",
                        lambda t, s, m: "acquired")
    assert responder.acquire_send_lock("t1", "s1", {}) is None


def test_lock_already_sent_and_in_flight_skip(monkeypatch):
    for verdict in ("already-sent", "in-flight"):
        monkeypatch.setattr(responder.ledger, "reserve_send",
                            lambda t, s, m, v=verdict: v)
        assert responder.acquire_send_lock("t1", "s1", {}) == f"skipped:{verdict}"


def test_lock_recover_backfills_when_alex_already_replied(monkeypatch):
    marks = []
    monkeypatch.setattr(responder.ledger, "reserve_send", lambda t, s, m: "recover")
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: [{"x": 1}])
    monkeypatch.setattr(responder.gm, "alex_replied_after", lambda msgs, mid: True)
    monkeypatch.setattr(responder.ledger, "mark_sent",
                        lambda t, s, **kw: marks.append(kw))
    out = responder.acquire_send_lock("t1", "s1", {}, "msg-1")
    assert out == "skipped:recovered-already-sent"
    assert marks == [{"recovered": "backfilled-from-gmail"}]


def test_lock_recover_takeover_lost(monkeypatch):
    monkeypatch.setattr(responder.ledger, "reserve_send", lambda t, s, m: "recover")
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: [])
    monkeypatch.setattr(responder.gm, "alex_replied_after", lambda msgs, mid: False)
    monkeypatch.setattr(responder.ledger, "takeover_send", lambda t, s: False)
    assert responder.acquire_send_lock("t1", "s1", {}) == "skipped:takeover-lost"


def test_lock_recover_takeover_won(monkeypatch):
    monkeypatch.setattr(responder.ledger, "reserve_send", lambda t, s, m: "recover")
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: [])
    monkeypatch.setattr(responder.gm, "alex_replied_after", lambda msgs, mid: False)
    monkeypatch.setattr(responder.ledger, "takeover_send", lambda t, s: True)
    assert responder.acquire_send_lock("t1", "s1", {}) is None


# ---------------------------------------------------------------- review

def test_review_pass_returns_none(monkeypatch):
    monkeypatch.setattr(responder, "review_gate", lambda t, b, k: (True, ""))
    assert responder.review_or_block("t1", "s1", "body", "counter") is None


def test_review_block_marks_failed_and_escalates(monkeypatch):
    failed, escalated = [], []
    monkeypatch.setattr(responder, "review_gate",
                        lambda t, b, k: (False, "contradicts renter"))
    monkeypatch.setattr(responder.ledger, "mark_failed",
                        lambda t, s, err: failed.append(err))
    monkeypatch.setattr(responder, "_escalate_review_block",
                        lambda t, why, tmpl: escalated.append((why, tmpl)))
    out = responder.review_or_block("t1", "s1", "body", "booking_new")
    assert out == "no-send:review-blocked"
    assert failed == ["review-blocked: contradicts renter"]
    assert escalated == [("contradicts renter", "booking_new")]


# ---------------------------------------------------------------- labels

def test_apply_labels_noop_when_nothing_to_change(monkeypatch):
    called = []
    monkeypatch.setattr(responder.gm, "modify_labels",
                        lambda *a, **k: called.append(a))
    responder.apply_labels("t1", "s1", [], [])
    assert called == []


def test_apply_labels_queues_on_failure(monkeypatch):
    queued = []

    def boom(*a, **k):
        raise RuntimeError("gmail down")

    monkeypatch.setattr(responder.gm, "modify_labels", boom)
    monkeypatch.setattr(responder.ledger, "set_labels_pending",
                        lambda t, s, add, rm: queued.append((add, rm)))
    responder.apply_labels("t1", "s1", ["L_ok"], ["L_wait"], "booking")
    assert queued == [(["L_ok"], ["L_wait"])]


# ---------------------------------------------------------------- fyi ping

def test_fyi_ping_swallows_failures(monkeypatch):
    def boom(_):
        raise RuntimeError("poke down")

    monkeypatch.setattr(responder.gm, "poke_ping", boom)
    responder.fyi_ping("Booked: someone", "booking")  # must not raise
