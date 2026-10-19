#!/usr/bin/env python3
"""The Firestore gates standing between a renter and a duplicate email.

reserve_send/takeover_send (THE send lock) and claim_message/
claim_content_hash (the dup-delivery and dup-content gates) decide, on every
inbound message, whether anything is sent at all. Not one test drove them:
every existing test monkeypatches them out, so the lock's state machine and
the strand exemption that two live incidents reshaped (mikayla + Jessica
8/29 "dup-content forever", Schneider 8/30 "three reprocess rounds") were
running on nothing but their comments.

These call the real ledger functions against tests/fake_firestore.py and
assert on the documents that come back out.
"""

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DRY_RUN", "true")

import fake_firestore  # noqa: E402
import ledger  # noqa: E402

THREAD = "18f2c0a9b1c2d3e4"
OTHER_THREAD = "18f2c0a9ffffffff"
STAGE = "first_reply"
META = {"template": "availability_ask", "to_relay": "x7k2@convo.zillow.com"}
RENTER_TEXT = "Yes, Saturday at 11am works for me. See you then!"


def _send_doc(db, thread_id=THREAD, stage=STAGE):
    return db.store["zillow_sends"][f"{thread_id}__{stage}"]


# ------------------------------------------------------------- the send lock

def test_double_webhook_delivery_reserves_the_stage_once(monkeypatch):
    """Composio has delivered the same message twice; the second worker must
    come away with nothing to send."""
    db = fake_firestore.install(monkeypatch)

    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    assert ledger.reserve_send(THREAD, STAGE, META) == "in-flight"

    doc = _send_doc(db)
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 1          # the loser left no trace
    assert doc["template"] == "availability_ask"
    assert doc["to_relay"] == "x7k2@convo.zillow.com"


def test_a_sent_stage_never_reserves_again(monkeypatch):
    db = fake_firestore.install(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_sent(THREAD, STAGE, event_id="ev_9r4k1")

    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"

    doc = _send_doc(db)
    assert doc["status"] == "sent"
    assert doc["event_id"] == "ev_9r4k1"
    assert doc["template"] == "availability_ask"   # merge kept the reservation
    assert db.now() - doc["sent_at"] < timedelta(seconds=5)


def test_abandoned_is_terminal_too(monkeypatch):
    """A send reality overtook (thread leased) is never retried."""
    db = fake_firestore.install(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_abandoned(THREAD, STAGE, "thread leased")

    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"
    assert _send_doc(db)["abandoned_reason"] == "thread leased"


def test_stale_reservation_recovers_and_takeover_counts_the_attempt(monkeypatch):
    """A worker that died mid-send leaves a reserved doc. Past the TTL the
    next one may recover it - and only via takeover, which records the retry."""
    db = fake_firestore.install(monkeypatch)
    with db.at_offset(minutes=-(ledger.RESERVE_TTL_MIN + 1)):
        assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"

    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"
    assert _send_doc(db)["attempts"] == 1          # recover alone writes nothing

    assert ledger.takeover_send(THREAD, STAGE) is True
    doc = _send_doc(db)
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 2
    assert db.now() - doc["reserved_at"] < timedelta(seconds=5)


def test_takeover_loses_against_a_live_reservation(monkeypatch):
    """Inside the TTL the first worker is presumed in flight: taking the lock
    from it would put two copies of the same email on the thread."""
    db = fake_firestore.install(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, META)
    reserved_at = _send_doc(db)["reserved_at"]

    assert ledger.takeover_send(THREAD, STAGE) is False

    doc = _send_doc(db)
    assert doc["attempts"] == 1
    assert doc["reserved_at"] == reserved_at       # transaction rolled back


def test_takeover_refuses_a_stage_already_sent(monkeypatch):
    db = fake_firestore.install(monkeypatch)
    with db.at_offset(minutes=-60):
        ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_sent(THREAD, STAGE)

    assert ledger.takeover_send(THREAD, STAGE) is False
    assert _send_doc(db)["status"] == "sent"


def test_a_stage_that_failed_three_times_gives_up(monkeypatch):
    """Three real attempts is the ceiling: a fourth would keep hammering a
    renter-facing send that is clearly broken. Humans pick it up from
    NEEDS_HUMAN aging instead."""
    db = fake_firestore.install(monkeypatch)
    with db.at_offset(minutes=-60):
        assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    ledger.mark_failed(THREAD, STAGE, "composio GMAIL_REPLY_TO_THREAD 500")

    for _ in range(2):
        assert ledger.reserve_send(THREAD, STAGE, META) == "recover"
        assert ledger.takeover_send(THREAD, STAGE) is True
        ledger.mark_failed(THREAD, STAGE, "composio GMAIL_REPLY_TO_THREAD 500")

    doc = _send_doc(db)
    assert doc["attempts"] == 3
    assert doc["error"] == "composio GMAIL_REPLY_TO_THREAD 500"
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_the_lock_is_per_thread_and_per_stage(monkeypatch):
    """The first reply's lock must not block this thread's booking
    confirmation, and one renter's lock must never gate another's."""
    db = fake_firestore.install(monkeypatch)

    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    assert ledger.reserve_send(THREAD, "booked__ev_9r4k1", META) == "acquired"
    assert ledger.reserve_send(OTHER_THREAD, STAGE, META) == "acquired"

    assert sorted(db.store["zillow_sends"]) == sorted([
        f"{THREAD}__{STAGE}",
        f"{THREAD}__booked__ev_9r4k1",
        f"{OTHER_THREAD}__{STAGE}",
    ])


# --------------------------------------------------------- duplicate gates

def test_duplicate_webhook_delivery_is_claimed_once(monkeypatch):
    db = fake_firestore.install(monkeypatch)
    message_id = "19a0b1c2d3e4f5a6"

    assert ledger.claim_message(message_id, THREAD) is True
    assert ledger.claim_message(message_id, THREAD) is False

    ledger.record_message_outcome(message_id, "sent",
                                  classification={"intent": "propose_time"})
    doc = db.store["zillow_messages"][message_id]
    assert doc["outcome"] == "sent"
    assert doc["classification"]["intent"] == "propose_time"
    assert doc["thread_id"] == THREAD      # the claim's own fields survived


def test_zillows_second_copy_of_the_same_words_is_a_dup(monkeypatch):
    """Zillow re-notifies with a new message id and identical words; the
    content hash is what catches that. Hashing normalizes case and edge
    whitespace, so a requoted copy collides too."""
    fake_firestore.install(monkeypatch)

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False
    assert ledger.claim_content_hash(
        THREAD, "  " + RENTER_TEXT.upper() + "\n") is False
    # Another renter saying the same thing is not a duplicate.
    assert ledger.claim_content_hash(OTHER_THREAD, RENTER_TEXT) is True


def test_the_same_words_days_later_are_a_genuine_resend(monkeypatch):
    """"Yes" twice a week apart is two messages, not one. Past the 48h
    window the claim is refreshed and the renter gets an answer."""
    db = fake_firestore.install(monkeypatch)
    key = f"{THREAD}__{ledger.content_hash(RENTER_TEXT)}"
    with db.at_offset(hours=-(ledger.CONTENT_HASH_WINDOW_H + 1)):
        assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert (db.now() - db.store["zillow_msg_hashes"][key]["created_at"]
            < timedelta(seconds=5))


def test_a_failed_send_lets_every_retry_through(monkeypatch):
    """mikayla + Jessica 8/29: the claim outlived the send that failed after
    it, so /reprocess answered dup-content forever and the renter was never
    replied to. Schneider 8/30: refreshing the claim on the exemption moved
    its clock past the failure and made the SECOND retry a dupe again. So the
    exemption must hold, unrefreshed, until a run actually succeeds."""
    db = fake_firestore.install(monkeypatch)
    message_id = "19a0b1c2d3e4f5a6"
    key = f"{THREAD}__{ledger.content_hash(RENTER_TEXT)}"

    assert ledger.claim_message(message_id, THREAD) is True
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    ledger.record_message_outcome(message_id, "skipped:event-failed")
    claimed_at = db.store["zillow_msg_hashes"][key]["created_at"]

    for _ in range(3):
        assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
        assert db.store["zillow_msg_hashes"][key]["created_at"] == claimed_at


def test_a_processed_message_still_blocks_the_duplicate_copy(monkeypatch):
    """The exemption keys on FAILURE. A message that was handled leaves the
    gate shut, which is what stops the second Zillow copy."""
    fake_firestore.install(monkeypatch)
    message_id = "19a0b1c2d3e4f5a6"
    ledger.claim_message(message_id, THREAD)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)
    ledger.record_message_outcome(message_id, "sent")

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False


def test_an_older_failure_is_not_this_claims_strand(monkeypatch):
    """A failure half an hour before this text was ever claimed belongs to
    some earlier message. Only one at or after the claim (inside the
    10-minute stamp-order grace) means the reply this claim guards never
    went out."""
    db = fake_firestore.install(monkeypatch)
    with db.at_offset(minutes=-30):
        ledger.claim_message("19a0b1c2d3e4f5a0", THREAD)
        ledger.record_message_outcome("19a0b1c2d3e4f5a0", "error:RuntimeError")

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False


def test_a_failure_on_another_thread_never_unlocks_this_one(monkeypatch):
    """The exemption query is thread-scoped; without that filter one stranded
    renter would reopen the dup gate for every thread in the ledger."""
    fake_firestore.install(monkeypatch)
    ledger.claim_message("19a0b1c2d3e4f5b7", OTHER_THREAD)
    ledger.record_message_outcome("19a0b1c2d3e4f5b7", "no-send:send-failed")

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False
