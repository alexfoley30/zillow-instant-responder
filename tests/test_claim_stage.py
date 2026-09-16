"""The send-lock claim sequence, once, for all four call sites.

Before this was extracted, send_stage and the three booking paths each had
their own hand-copied copy and two of the four strings had drifted apart
(recovered="backfilled" vs "backfilled-from-gmail", "skipped:recovered" vs
"skipped:recovered-already-sent"). These lock the one remaining shape.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import responder  # noqa: E402


def _stub(monkeypatch, verdict, alex_replied=False, takeover=True):
    calls = {"reserve": [], "sent": [], "takeover": [], "fetched": []}
    monkeypatch.setattr(responder.ledger, "reserve_send",
                        lambda tid, stage, meta:
                        calls["reserve"].append((tid, stage, meta)) or verdict)
    monkeypatch.setattr(responder.gm, "fetch_thread",
                        lambda tid: calls["fetched"].append(tid) or ["msg"])
    monkeypatch.setattr(responder.gm, "alex_replied_after",
                        lambda msgs, mid: alex_replied)
    monkeypatch.setattr(responder.ledger, "mark_sent",
                        lambda tid, stage, **kw: calls["sent"].append((tid, stage, kw)))
    monkeypatch.setattr(responder.ledger, "takeover_send",
                        lambda tid, stage:
                        calls["takeover"].append((tid, stage)) or takeover)
    return calls


def test_claim_returns_none_when_lock_is_won(monkeypatch):
    calls = _stub(monkeypatch, "reserved")
    assert responder.claim_stage("t1", "reply__m1", {"template": "x"}, "m1") is None
    assert calls["reserve"] == [("t1", "reply__m1", {"template": "x"})]
    assert calls["fetched"] == []  # no Gmail read on the happy path


def test_claim_skips_already_sent_and_in_flight(monkeypatch):
    for verdict in ("already-sent", "in-flight"):
        calls = _stub(monkeypatch, verdict)
        assert responder.claim_stage("t1", "s", {}, "m1") == f"skipped:{verdict}"
        assert calls["takeover"] == []


def test_recover_backfills_when_alex_already_replied(monkeypatch):
    calls = _stub(monkeypatch, "recover", alex_replied=True)
    assert responder.claim_stage("t1", "s", {}, "m1") == "skipped:recovered-already-sent"
    assert calls["sent"] == [("t1", "s", {"recovered": "backfilled-from-gmail"})]
    assert calls["takeover"] == []  # never takes over a thread Alex answered


def test_recover_takes_over_when_alex_has_not_replied(monkeypatch):
    calls = _stub(monkeypatch, "recover", alex_replied=False, takeover=True)
    assert responder.claim_stage("t1", "s", {}, "m1") is None
    assert calls["takeover"] == [("t1", "s")]
    assert calls["sent"] == []


def test_recover_skips_when_takeover_is_lost(monkeypatch):
    _stub(monkeypatch, "recover", alex_replied=False, takeover=False)
    assert responder.claim_stage("t1", "s", {}, "m1") == "skipped:takeover-lost"
