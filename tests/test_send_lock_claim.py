"""The send lock (no lock, no send) is the pipeline's whole duplicate defence,
and its reserve -> Gmail-check -> takeover preamble had been copy-pasted into
four call sites: send_stage, book_accepted_offer, the adjacency-snap fold and
new bookings. responder._claim_send_lock is now the single copy.

The copies had drifted in two VISIBLE details, so these tests pin them: the
booking paths stamp recovered="backfilled" and report "skipped:recovered",
while send_stage stamps "backfilled-from-gmail" and reports
"skipped:recovered-already-sent". Nothing had covered the recover branch at
all before, which is exactly the branch a refactor could quietly change.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gmail_client as gm  # noqa: E402
import ledger  # noqa: E402
import responder  # noqa: E402

BOOKING_KW = {}
SEND_STAGE_KW = {
    "log_skip": True,
    "recovered_tag": "backfilled-from-gmail",
    "recovered_result": "skipped:recovered-already-sent",
}


def _wire(monkeypatch, verdict, replied_after=False, takeover=True):
    marked = []
    monkeypatch.setattr(ledger, "reserve_send",
                        lambda t, s, m: verdict)
    monkeypatch.setattr(ledger, "mark_sent",
                        lambda t, s, **f: marked.append((t, s, f)))
    monkeypatch.setattr(ledger, "takeover_send", lambda t, s: takeover)
    monkeypatch.setattr(gm, "fetch_thread", lambda t: [{"id": "m1"}])
    monkeypatch.setattr(gm, "alex_replied_after", lambda msgs, mid: replied_after)
    return marked


# ---------------------------------------------------------- straightforward

def test_acquired_returns_none_so_the_caller_proceeds(monkeypatch):
    _wire(monkeypatch, "acquired")
    assert responder._claim_send_lock("t1", "stage", {}, "m1") is None


def test_already_sent_skips(monkeypatch):
    _wire(monkeypatch, "already-sent")
    assert responder._claim_send_lock("t1", "stage", {}, "m1") \
        == "skipped:already-sent"


def test_in_flight_skips(monkeypatch):
    _wire(monkeypatch, "in-flight")
    assert responder._claim_send_lock("t1", "stage", {}, "m1") \
        == "skipped:in-flight"


# ---------------------------------------------------------------- recover

def test_recover_backfills_when_alex_already_replied(monkeypatch):
    marked = _wire(monkeypatch, "recover", replied_after=True)
    out = responder._claim_send_lock("t1", "stage", {}, "m1", **BOOKING_KW)
    assert out == "skipped:recovered"
    assert marked == [("t1", "stage", {"recovered": "backfilled"})]


def test_send_stage_keeps_its_own_recovered_tag_and_result(monkeypatch):
    marked = _wire(monkeypatch, "recover", replied_after=True)
    out = responder._claim_send_lock("t1", "stage", {}, "m1", **SEND_STAGE_KW)
    assert out == "skipped:recovered-already-sent"
    assert marked == [("t1", "stage", {"recovered": "backfilled-from-gmail"})]


def test_recover_takes_over_and_proceeds_when_nothing_was_sent(monkeypatch):
    marked = _wire(monkeypatch, "recover", replied_after=False, takeover=True)
    assert responder._claim_send_lock("t1", "stage", {}, "m1") is None
    assert marked == []  # nothing was backfilled; we own it and must send


def test_lost_takeover_never_sends(monkeypatch):
    _wire(monkeypatch, "recover", replied_after=False, takeover=False)
    assert responder._claim_send_lock("t1", "stage", {}, "m1") \
        == "skipped:takeover-lost"


def test_recover_checks_the_trigger_message_not_the_stage(monkeypatch):
    """The Gmail check must be made against the triggering message id the
    caller passed - send_stage passes trigger_message_id, the booking paths
    pass message_id."""
    seen = []
    monkeypatch.setattr(ledger, "reserve_send", lambda t, s, m: "recover")
    monkeypatch.setattr(ledger, "mark_sent", lambda t, s, **f: None)
    monkeypatch.setattr(ledger, "takeover_send", lambda t, s: True)
    monkeypatch.setattr(gm, "fetch_thread", lambda t: [{"id": "m1"}])
    monkeypatch.setattr(gm, "alex_replied_after",
                        lambda msgs, mid: seen.append(mid) or False)
    responder._claim_send_lock("t1", "stage", {}, "trigger-42")
    assert seen == ["trigger-42"]


def test_meta_reaches_reserve_send_unchanged(monkeypatch):
    seen = []
    monkeypatch.setattr(ledger, "reserve_send",
                        lambda t, s, m: seen.append((t, s, m)) or "acquired")
    responder._claim_send_lock("t1", "stage", {"template": "booking_new"}, "m1")
    assert seen == [("t1", "stage", {"template": "booking_new"})]
