"""_claim_stage is the send lock ladder the four book/send paths used to each
carry by hand. These lock the outcome strings every caller recorded before the
extraction, so a later edit to the single version cannot quietly change what
lands in zillow_messages.outcome.

send_stage is the one caller with its own wording ("backfilled-from-gmail" /
"skipped:recovered-already-sent"); book_accepted_offer and both
_book_valid_slot paths share the shorter pair.
"""

import ledger
import responder


def _wire(monkeypatch, verdict, alex_replied=False, takeover=True):
    """Stub the ledger/Gmail calls _claim_stage makes. Returns the mark_sent
    kwargs it recorded, so the `recovered` tag is assertable."""
    recorded = {}
    monkeypatch.setattr(ledger, "reserve_send",
                        lambda t, s, m: verdict)
    monkeypatch.setattr(ledger, "mark_sent",
                        lambda t, s, **kw: recorded.update(kw))
    monkeypatch.setattr(ledger, "takeover_send", lambda t, s: takeover)
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: [])
    monkeypatch.setattr(responder.gm, "alex_replied_after",
                        lambda msgs, mid: alex_replied)
    return recorded


def test_acquired_lets_the_caller_proceed(monkeypatch):
    _wire(monkeypatch, "acquired")
    assert responder._claim_stage("t1", "stage", {}) is None


def test_already_sent_and_in_flight_skip(monkeypatch):
    _wire(monkeypatch, "already-sent")
    assert responder._claim_stage("t1", "stage", {}) == "skipped:already-sent"
    _wire(monkeypatch, "in-flight")
    assert responder._claim_stage("t1", "stage", {}) == "skipped:in-flight"


def test_recover_backfills_when_alex_already_replied(monkeypatch):
    recorded = _wire(monkeypatch, "recover", alex_replied=True)
    assert responder._claim_stage("t1", "stage", {}, "m1") == "skipped:recovered"
    assert recorded == {"recovered": "backfilled"}


def test_send_stage_keeps_its_own_recovery_wording(monkeypatch):
    recorded = _wire(monkeypatch, "recover", alex_replied=True)
    out = responder._claim_stage(
        "t1", "stage", {}, "m1", recovered_tag="backfilled-from-gmail",
        recovered_result="skipped:recovered-already-sent")
    assert out == "skipped:recovered-already-sent"
    assert recorded == {"recovered": "backfilled-from-gmail"}


def test_recover_takes_the_stale_doc_over(monkeypatch):
    _wire(monkeypatch, "recover", alex_replied=False, takeover=True)
    assert responder._claim_stage("t1", "stage", {}, "m1") is None


def test_lost_takeover_never_sends(monkeypatch):
    _wire(monkeypatch, "recover", alex_replied=False, takeover=False)
    assert responder._claim_stage("t1", "stage", {}, "m1") == "skipped:takeover-lost"


# ---------------------------------------------------------------- labels

def test_label_failure_queues_instead_of_raising(monkeypatch):
    queued = {}

    def _boom(thread_id, add, remove):
        raise RuntimeError("gmail down")

    monkeypatch.setattr(responder.gm, "modify_labels", _boom)
    monkeypatch.setattr(ledger, "set_labels_pending",
                        lambda t, s, a, r: queued.update(add=a, remove=r))
    responder._apply_labels("t1", "stage", ["L_add"], ["L_rm"], "booking")
    assert queued == {"add": ["L_add"], "remove": ["L_rm"]}


def test_no_labels_means_no_call(monkeypatch):
    called = []
    monkeypatch.setattr(responder.gm, "modify_labels",
                        lambda *a: called.append(a))
    responder._apply_labels("t1", "stage", [], [])
    assert called == []


# ---------------------------------------------------------------- FYI ping

def test_fyi_ping_failure_is_swallowed(monkeypatch):
    def _boom(message):
        raise RuntimeError("poke down")

    monkeypatch.setattr(responder.gm, "poke_ping", _boom)
    responder._poke_quiet("Booked: ...", "booked FYI")  # must not raise
