"""The send lock: `ledger.reserve_send` / `ledger.takeover_send` and the
`recover` branch of `responder.send_stage` that depends on them.

Nothing tested this. Every other responder test replaces `reserve_send` with
`lambda t, s, m: "acquired"`, so the one function that decides whether a renter
gets an email - and the only thing standing between a redelivered webhook and a
second copy of the same reply - had no assertions on it at all. Both failure
directions cost something real: a wrong "acquired" emails a renter twice, a
wrong "already-sent" / "in-flight" strands them with no reply.

Firestore is the only thing faked (tests/fake_firestore.py). The documents under
test are the ones `reserve_send`, `mark_sent`, `mark_failed` and
`mark_abandoned` actually write, written by those functions, and the clock is
real `datetime` arithmetic against `RESERVE_TTL_MIN`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger  # noqa: E402
import responder  # noqa: E402
import templates as T  # noqa: E402
from fake_firestore import Clock, FakeFirestore  # noqa: E402

THREAD = "thread_ab12"
STAGE = "first_reply"
RELAY = "reply+abc123@convo.zillow.com"
META = {"template": "first_reply_fallback", "address": "2118 S El Marino, Mesa, AZ",
        "renter_name": "Jessica"}


def _db(monkeypatch):
    return FakeFirestore(Clock()).install(monkeypatch, ledger)


def _send_doc(db):
    return db.doc("zillow_sends", f"{THREAD}__{STAGE}")


# ------------------------------------------------------------ reserve_send

def test_first_reserve_acquires_and_writes_a_reserved_doc(monkeypatch):
    db = _db(monkeypatch)
    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    doc = _send_doc(db)
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 1
    assert doc["reserved_at"] == db.clock.now
    # the caller's meta rides along so the audit trail says what was sent
    assert doc["template"] == "first_reply_fallback"
    assert doc["renter_name"] == "Jessica"


def test_second_worker_inside_the_ttl_is_told_in_flight(monkeypatch):
    db = _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    first_reserved_at = _send_doc(db)["reserved_at"]
    db.clock.advance(minutes=9, seconds=59)

    assert ledger.reserve_send(THREAD, STAGE, META) == "in-flight"
    # and it must not have disturbed the live lock
    assert _send_doc(db)["reserved_at"] == first_reserved_at
    assert _send_doc(db)["attempts"] == 1


def test_reserved_doc_older_than_the_ttl_is_recoverable(monkeypatch):
    db = _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    db.clock.advance(minutes=10)
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_sent_doc_blocks_a_second_send(monkeypatch):
    db = _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_sent(THREAD, STAGE)
    db.clock.advance(hours=6)
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"
    assert _send_doc(db)["status"] == "sent"


def test_abandoned_doc_is_terminal_not_retried(monkeypatch):
    _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_abandoned(THREAD, STAGE, "thread leased")
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_failed_doc_is_recoverable_until_three_attempts(monkeypatch):
    db = _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_failed(THREAD, STAGE, "composio 500")
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"

    # two more real takeovers take `attempts` to 3; then the pipeline gives up
    # rather than hammering Composio, and the thread surfaces via NEEDS_HUMAN.
    db.clock.advance(minutes=11)
    assert ledger.takeover_send(THREAD, STAGE) is True
    ledger.mark_failed(THREAD, STAGE, "composio 500")
    db.clock.advance(minutes=11)
    assert ledger.takeover_send(THREAD, STAGE) is True
    ledger.mark_failed(THREAD, STAGE, "composio 500")
    assert _send_doc(db)["attempts"] == 3
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_reserved_doc_with_no_timestamp_is_recoverable(monkeypatch):
    """A doc whose write landed without reserved_at (partial write / clock
    skew) must not read as a live lock forever, or the renter is stranded."""
    db = _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    _send_doc(db).pop("reserved_at")
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


# ----------------------------------------------------------- takeover_send

def test_takeover_of_a_stale_lock_increments_attempts(monkeypatch):
    db = _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    db.clock.advance(minutes=30)

    assert ledger.takeover_send(THREAD, STAGE) is True
    doc = _send_doc(db)
    assert doc["attempts"] == 2
    assert doc["status"] == "reserved"
    assert doc["reserved_at"] == db.clock.now     # lock clock restarted
    assert db.commits == 1 and db.rollbacks == 0


def test_takeover_refuses_a_live_lock(monkeypatch):
    db = _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    held_at = _send_doc(db)["reserved_at"]
    db.clock.advance(minutes=5)

    assert ledger.takeover_send(THREAD, STAGE) is False
    assert _send_doc(db)["attempts"] == 1
    assert _send_doc(db)["reserved_at"] == held_at


def test_takeover_refuses_an_already_sent_doc(monkeypatch):
    db = _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_sent(THREAD, STAGE)
    db.clock.advance(hours=2)
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert _send_doc(db)["status"] == "sent"


def test_takeover_refuses_an_abandoned_doc(monkeypatch):
    db = _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_abandoned(THREAD, STAGE, "human answered")
    db.clock.advance(hours=2)
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert _send_doc(db)["status"] == "abandoned"


def test_takeover_of_a_fresh_failed_doc_is_allowed(monkeypatch):
    """The TTL guards `reserved` only: a send that already failed is free to
    retry at once, which is what /reprocess relies on."""
    db = _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_failed(THREAD, STAGE, "composio timeout")
    assert ledger.takeover_send(THREAD, STAGE) is True
    assert _send_doc(db)["status"] == "reserved"


def test_takeover_rolls_back_and_refuses_when_the_read_blows_up(monkeypatch):
    db = _db(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    db.clock.advance(minutes=30)

    def boom(*a, **k):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(ledger, "_send_ref", lambda t, s: type(
        "R", (), {"get": boom, "update": boom})())
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert _send_doc(db)["attempts"] == 1


# ------------------------------------------- send_stage over the real lock

class _Outbox:
    """Gmail/Composio replaced; the lock, the review gate and send_stage's own
    branching are the real thing."""

    def __init__(self, monkeypatch, thread_msgs=()):
        self.sent = []
        self.labels = []
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setattr(responder.gm, "send_reply", self._send)
        monkeypatch.setattr(responder.gm, "modify_labels",
                            lambda t, a, r: self.labels.append((a, r)))
        monkeypatch.setattr(responder.gm, "fetch_thread",
                            lambda t: list(thread_msgs))
        monkeypatch.setattr(responder.ledger, "upsert_thread",
                            lambda t, **f: None)

    def _send(self, thread_id, relay, body):
        self.sent.append((thread_id, relay, body))
        return {"successful": True}


BODY = T.first_reply_fallback("Jessica", "2118 S El Marino, Mesa, AZ")

RENTER_MSG = {"messageId": "m_renter_1",
              "sender": "Jessica <reply+abc123@convo.zillow.com>",
              "messageTimestamp": "2026-11-16T08:55:00+00:00"}
ALEX_MSG = {"messageId": "m_alex_1", "sender": "Alex Foley <alex@azfoleyhomes.com>",
            "messageTimestamp": "2026-11-16T08:56:00+00:00",
            "messageText": BODY}


def _stage(outbox):
    return responder.send_stage(THREAD, STAGE, RELAY, BODY,
                                [responder.AWAITING_LABEL], [], dict(META),
                                trigger_message_id="m_renter_1")


def test_redelivered_webhook_does_not_email_the_renter_twice(monkeypatch):
    db = _db(monkeypatch)
    box = _Outbox(monkeypatch, [RENTER_MSG])

    assert _stage(box) == "sent"
    assert _send_doc(db)["status"] == "sent"
    assert _stage(box) == "skipped:already-sent"
    assert len(box.sent) == 1
    assert box.labels == [([responder.AWAITING_LABEL], [])]


def test_concurrent_worker_sees_the_live_lock_and_sends_nothing(monkeypatch):
    _db(monkeypatch)
    box = _Outbox(monkeypatch, [RENTER_MSG])
    # another worker is mid-send: the lock exists, no mark_sent yet
    ledger.reserve_send(THREAD, STAGE, META)

    assert _stage(box) == "skipped:in-flight"
    assert box.sent == []


def test_stale_lock_with_a_reply_already_on_the_thread_backfills(monkeypatch):
    """The crashed worker DID send before dying. Gmail is the tiebreak, and
    the renter must not get a second copy."""
    db = _db(monkeypatch)
    box = _Outbox(monkeypatch, [RENTER_MSG, ALEX_MSG])
    ledger.reserve_send(THREAD, STAGE, META)
    db.clock.advance(minutes=45)

    assert _stage(box) == "skipped:recovered-already-sent"
    assert box.sent == []
    doc = _send_doc(db)
    assert doc["status"] == "sent"
    assert doc["recovered"] == "backfilled-from-gmail"


def test_stale_lock_with_no_reply_on_the_thread_sends_once(monkeypatch):
    """The crashed worker died BEFORE sending. The renter is owed a reply."""
    db = _db(monkeypatch)
    box = _Outbox(monkeypatch, [RENTER_MSG])
    ledger.reserve_send(THREAD, STAGE, META)
    db.clock.advance(minutes=45)

    assert _stage(box) == "sent"
    assert len(box.sent) == 1
    assert box.sent[0][1] == RELAY
    assert "Jessica" in box.sent[0][2]
    assert _send_doc(db)["status"] == "sent"
    assert _send_doc(db)["attempts"] == 2


def test_losing_the_takeover_race_sends_nothing(monkeypatch):
    db = _db(monkeypatch)
    box = _Outbox(monkeypatch, [RENTER_MSG])
    ledger.reserve_send(THREAD, STAGE, META)
    db.clock.advance(minutes=45)
    # the other worker refreshes the lock between our read and our takeover
    monkeypatch.setattr(responder.ledger, "takeover_send", lambda t, s: False)

    assert _stage(box) == "skipped:takeover-lost"
    assert box.sent == []


def test_a_failed_send_leaves_the_outcome_a_retry_can_see(monkeypatch):
    """send_stage must record `skipped:send-failed` on the send doc, because
    that string is what lifts the dup-content gate on /reprocess."""
    db = _db(monkeypatch)
    box = _Outbox(monkeypatch, [RENTER_MSG])
    monkeypatch.setattr(responder.gm, "send_reply",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("composio 503")))

    assert _stage(box) == "skipped:send-failed"
    doc = _send_doc(db)
    assert doc["status"] == "failed"
    assert "composio 503" in doc["error"]
    # and the stage is retryable, not wedged
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"
    assert box.sent == []


def test_label_failure_never_causes_a_second_email(monkeypatch):
    db = _db(monkeypatch)
    box = _Outbox(monkeypatch, [RENTER_MSG])
    monkeypatch.setattr(responder.gm, "modify_labels",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("label 500")))

    assert _stage(box) == "sent"
    assert len(box.sent) == 1
    doc = _send_doc(db)
    assert doc["status"] == "sent"
    assert doc["labels_pending"] is True
    assert doc["labels_add"] == [responder.AWAITING_LABEL]
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"
