"""acquire_send_slot: the ONE send-lock preamble every send path shares.

Before this helper the reserve -> recover -> takeover dance was hand-written
four times (send_stage, book_accepted_offer, the snap fold, and the new
booking) and the copies had drifted: the three booking paths reported
"skipped:recovered" with recovered="backfilled" while send_stage reported
"skipped:recovered-already-sent" with "backfilled-from-gmail" for the exact
same event. These tests pin the single behaviour so a fifth copy cannot
quietly reintroduce a fifth dialect.
"""
import responder


def _wire(monkeypatch, verdict, alex_replied=False, takeover=True):
    calls = {"reserve": [], "sent": [], "takeover": [], "fetched": []}

    def reserve(tid, stage, meta):
        calls["reserve"].append((tid, stage, meta))
        return verdict

    monkeypatch.setattr(responder.ledger, "reserve_send", reserve)
    monkeypatch.setattr(responder.ledger, "mark_sent",
                        lambda tid, stage, **kw: calls["sent"].append((tid, stage, kw)))
    monkeypatch.setattr(responder.ledger, "takeover_send",
                        lambda tid, stage: calls["takeover"].append((tid, stage)) or takeover)
    monkeypatch.setattr(responder.gm, "fetch_thread",
                        lambda tid: calls["fetched"].append(tid) or [])
    monkeypatch.setattr(responder.gm, "alex_replied_after",
                        lambda msgs, mid: alex_replied)
    return calls


def test_acquired_slot_returns_none_so_the_caller_sends(monkeypatch):
    calls = _wire(monkeypatch, "acquired")
    assert responder.acquire_send_slot("t1", "reply__m1", {"template": "x"}, "m1") is None
    assert calls["reserve"] == [("t1", "reply__m1", {"template": "x"})]
    assert calls["fetched"] == []  # no Gmail read needed on a clean acquire


def test_already_sent_and_in_flight_are_reported_verbatim(monkeypatch):
    for verdict in ("already-sent", "in-flight"):
        _wire(monkeypatch, verdict)
        assert responder.acquire_send_slot("t1", "s", {}, "m1") == f"skipped:{verdict}"


def test_recover_with_an_alex_reply_backfills_once_under_one_name(monkeypatch):
    calls = _wire(monkeypatch, "recover", alex_replied=True)
    out = responder.acquire_send_slot("t2", "booked__e1", {"template": "booking_fold"}, "m9")
    assert out == "skipped:recovered-already-sent"
    assert calls["sent"] == [("t2", "booked__e1", {"recovered": "backfilled-from-gmail"})]
    assert calls["takeover"] == []  # nothing to take over: it already went out


def test_recover_without_an_alex_reply_takes_the_doc_over(monkeypatch):
    calls = _wire(monkeypatch, "recover", alex_replied=False, takeover=True)
    assert responder.acquire_send_slot("t3", "s", {}, "m1") is None
    assert calls["takeover"] == [("t3", "s")]
    assert calls["sent"] == []


def test_losing_the_takeover_race_never_sends(monkeypatch):
    _wire(monkeypatch, "recover", alex_replied=False, takeover=False)
    assert responder.acquire_send_slot("t4", "s", {}, "m1") == "skipped:takeover-lost"
