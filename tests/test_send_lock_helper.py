"""The send lock and the review gate are now ONE function each, not four
copies apiece (responder.claim_send / responder.blocked_by_review). These
tests pin every branch of both, so the next edit to the pipeline's
safety-critical preamble cannot quietly change one caller's behaviour.
"""

import gmail_client as gm
import ledger
import responder


# ---------------------------------------------------------------- claim_send

def _wire_reserve(monkeypatch, verdict, **over):
    calls = {"reserve": [], "mark_sent": [], "takeover": [], "fetch": []}

    def reserve(thread_id, stage_key, meta):
        calls["reserve"].append((thread_id, stage_key, meta))
        return verdict

    def mark_sent(thread_id, stage_key, **fields):
        calls["mark_sent"].append((thread_id, stage_key, fields))

    def takeover(thread_id, stage_key):
        calls["takeover"].append((thread_id, stage_key))
        return over.get("takeover", True)

    def fetch(thread_id):
        calls["fetch"].append(thread_id)
        return over.get("msgs", [])

    monkeypatch.setattr(ledger, "reserve_send", reserve)
    monkeypatch.setattr(ledger, "mark_sent", mark_sent)
    monkeypatch.setattr(ledger, "takeover_send", takeover)
    monkeypatch.setattr(gm, "fetch_thread", fetch)
    monkeypatch.setattr(gm, "alex_replied_after",
                        lambda msgs, mid: over.get("alex_replied", False))
    return calls


def test_acquired_lock_lets_the_caller_send(monkeypatch):
    calls = _wire_reserve(monkeypatch, "acquired")
    assert responder.claim_send("t1", "first_reply", {"template": "x"}) is None
    assert calls["reserve"] == [("t1", "first_reply", {"template": "x"})]
    assert calls["fetch"] == []  # no Gmail read on the happy path


def test_already_sent_and_in_flight_skip_without_touching_gmail(monkeypatch):
    for verdict in ("already-sent", "in-flight"):
        calls = _wire_reserve(monkeypatch, verdict)
        assert responder.claim_send("t1", "s", {}) == f"skipped:{verdict}"
        assert calls["fetch"] == []
        assert calls["takeover"] == []


def test_recover_backfills_when_alex_already_replied(monkeypatch):
    calls = _wire_reserve(monkeypatch, "recover", alex_replied=True)
    assert responder.claim_send("t1", "s", {}, "msg-9") == "skipped:recovered"
    assert calls["mark_sent"] == [("t1", "s", {"recovered": "backfilled"})]
    assert calls["takeover"] == []  # nothing to take over; it went out already


def test_recover_takes_over_when_nobody_replied(monkeypatch):
    calls = _wire_reserve(monkeypatch, "recover", alex_replied=False,
                          takeover=True)
    assert responder.claim_send("t1", "s", {}, "msg-9") is None
    assert calls["takeover"] == [("t1", "s")]
    assert calls["mark_sent"] == []


def test_lost_takeover_never_sends(monkeypatch):
    _wire_reserve(monkeypatch, "recover", alex_replied=False, takeover=False)
    assert responder.claim_send("t1", "s", {}) == "skipped:takeover-lost"


def test_send_stage_keeps_its_own_recovered_wording(monkeypatch):
    """send_stage's backfill outcome reads differently from the booking
    paths' and the watchdog reads it, so the refactor preserved it."""
    calls = _wire_reserve(monkeypatch, "recover", alex_replied=True)
    out = responder.claim_send(
        "t1", "first_reply", {}, "msg-9",
        recovered_note="backfilled-from-gmail",
        recovered_result="skipped:recovered-already-sent")
    assert out == "skipped:recovered-already-sent"
    assert calls["mark_sent"] == [
        ("t1", "first_reply", {"recovered": "backfilled-from-gmail"})]


# ---------------------------------------------------------- blocked_by_review

def _wire_review(monkeypatch, verdict, reason=""):
    calls = {"failed": [], "escalated": []}
    monkeypatch.setattr(responder, "review_gate",
                        lambda tid, body, template, msgs=None: verdict)
    monkeypatch.setattr(
        ledger, "mark_failed",
        lambda tid, stage, err: calls["failed"].append((tid, stage, err)))
    monkeypatch.setattr(
        responder, "_escalate_review_block",
        lambda tid, why, tpl: calls["escalated"].append((tid, why, tpl)))
    return calls


def test_cleared_review_records_nothing(monkeypatch):
    calls = _wire_review(monkeypatch, (True, ""))
    assert responder.blocked_by_review("t1", "s", "body", "counter") is None
    assert calls["failed"] == [] and calls["escalated"] == []


def test_blocked_review_marks_failed_and_escalates_once(monkeypatch):
    calls = _wire_review(monkeypatch, (False, "offers a time they declined"))
    out = responder.blocked_by_review("t1", "booked__e1", "body", "booking_new")
    assert out == "no-send:review-blocked"
    assert calls["failed"] == [
        ("t1", "booked__e1", "review-blocked: offers a time they declined")]
    assert calls["escalated"] == [
        ("t1", "offers a time they declined", "booking_new")]
