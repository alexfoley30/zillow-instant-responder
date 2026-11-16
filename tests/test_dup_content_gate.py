"""`ledger.claim_content_hash` - the gate that decides whether a renter's words
get a reply or get dropped as a duplicate.

Only its helper `_thread_send_failed_since` had tests. The gate itself, which is
what the webhook and `/reprocess` actually call, had none - and its docstring
records three separate live incidents from getting it wrong:

  * mikayla + Jessica, 8/29: the claim outlived a failed send, `/reprocess`
    returned dup-content forever and the renters were never replied to.
  * Schneider, 8/30: the exemption refreshed `created_at`, which moved the claim
    clock past the failure, so the SECOND retry was a dupe again.
  * the 10-minute grace: `claim_message` stamps `processed_at` a beat BEFORE
    `claim_content_hash` stamps `created_at` in the same request, so the very
    failure that stranded the hash always looks slightly older than the claim.

Firestore is faked (tests/fake_firestore.py); the `zillow_messages` documents the
exemption reads are written by the real `claim_message` /
`record_message_outcome`, and the 48h window is real `datetime` arithmetic.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger  # noqa: E402
from fake_firestore import Clock, FakeFirestore  # noqa: E402

THREAD = "thread_mikayla"
OTHER_THREAD = "thread_schneider"

# What a renter actually writes, as extract_renter_text hands it over.
YES = "Yes! 4pm Thursday works great for me, see you then."
QUESTION = "Hi, is this still available? And do you allow a small dog?"


def _db(monkeypatch):
    return FakeFirestore(Clock()).install(monkeypatch, ledger)


def _hash_doc(db, thread_id, text):
    return db.doc("zillow_msg_hashes", f"{thread_id}__{ledger.content_hash(text)}")


def _process(message_id, thread_id, text, outcome):
    """One webhook delivery as the responder runs it: claim the message id,
    claim the content, then record how it ended."""
    ledger.claim_message(message_id, thread_id)
    claimed = ledger.claim_content_hash(thread_id, text)
    ledger.record_message_outcome(message_id, outcome)
    return claimed


# ------------------------------------------------------- first vs duplicate

def test_first_sighting_claims_and_stamps_the_window(monkeypatch):
    db = _db(monkeypatch)
    assert ledger.claim_content_hash(THREAD, YES) is True
    assert _hash_doc(db, THREAD, YES)["created_at"] == db.clock.now


def test_zillow_duplicate_copy_is_dropped(monkeypatch):
    """Zillow re-notifies the same words under a new message id; that second
    delivery must not produce a second reply."""
    db = _db(monkeypatch)
    assert ledger.claim_content_hash(THREAD, YES) is True
    first_at = _hash_doc(db, THREAD, YES)["created_at"]
    db.clock.advance(minutes=2)

    assert ledger.claim_content_hash(THREAD, YES) is False
    # the losing claim must not move the window either
    assert _hash_doc(db, THREAD, YES)["created_at"] == first_at


def test_the_hash_ignores_case_and_surrounding_whitespace(monkeypatch):
    _db(monkeypatch)
    assert ledger.claim_content_hash(THREAD, YES) is True
    assert ledger.claim_content_hash(THREAD, f"  {YES.upper()}\n") is False


def test_the_gate_is_per_thread_not_global(monkeypatch):
    """Two renters both answer "yes" - the second one is not a duplicate."""
    _db(monkeypatch)
    assert ledger.claim_content_hash(THREAD, YES) is True
    assert ledger.claim_content_hash(OTHER_THREAD, YES) is True


def test_different_words_on_the_same_thread_pass(monkeypatch):
    _db(monkeypatch)
    assert ledger.claim_content_hash(THREAD, QUESTION) is True
    assert ledger.claim_content_hash(THREAD, YES) is True


# ------------------------------------------------------------- 48h window

def test_identical_text_at_the_window_edge_is_still_a_duplicate(monkeypatch):
    db = _db(monkeypatch)
    ledger.claim_content_hash(THREAD, YES)
    db.clock.advance(hours=ledger.CONTENT_HASH_WINDOW_H)
    assert ledger.claim_content_hash(THREAD, YES) is False


def test_identical_text_past_the_window_is_a_genuine_resend(monkeypatch):
    """"yes" twice a week apart is two real messages, not a dupe."""
    db = _db(monkeypatch)
    ledger.claim_content_hash(THREAD, YES)
    db.clock.advance(hours=ledger.CONTENT_HASH_WINDOW_H, seconds=1)

    assert ledger.claim_content_hash(THREAD, YES) is True
    # the window slides forward so the NEXT 48h are protected again
    assert _hash_doc(db, THREAD, YES)["created_at"] == db.clock.now
    assert ledger.claim_content_hash(THREAD, YES) is False


# ------------------------------------------- failed-send exemption (U3/8-29)

def test_a_reply_whose_send_failed_can_be_reprocessed(monkeypatch):
    """mikayla + Jessica, 8/29: the claim was protecting a reply that never
    actually went out, so /reprocess must get through."""
    _db(monkeypatch)
    assert _process("m1", THREAD, YES, "skipped:send-failed") is True
    assert ledger.claim_content_hash(THREAD, YES) is True


def test_the_exemption_does_not_move_the_claim_clock(monkeypatch):
    """Schneider, 8/30: refreshing created_at pushed the claim past the
    failure, so retry #2 read as a dupe again and the renter stayed stranded.
    Retries must keep passing until one of them actually succeeds."""
    db = _db(monkeypatch)
    _process("m1", THREAD, YES, "skipped:send-failed")
    anchored_at = _hash_doc(db, THREAD, YES)["created_at"]

    for _ in range(3):
        db.clock.advance(minutes=20)
        assert ledger.claim_content_hash(THREAD, YES) is True
        assert _hash_doc(db, THREAD, YES)["created_at"] == anchored_at


def test_the_exemption_survives_the_processed_at_ordering_skew(monkeypatch):
    """claim_message stamps processed_at BEFORE claim_content_hash stamps
    created_at, so the stranding failure always looks slightly older than the
    claim. Without the 10-minute grace the exemption could never fire."""
    db = _db(monkeypatch)
    ledger.claim_message("m1", THREAD)
    db.clock.advance(seconds=3)          # the beat between the two stamps
    ledger.claim_content_hash(THREAD, YES)
    ledger.record_message_outcome("m1", "skipped:send-failed")

    assert ledger.claim_content_hash(THREAD, YES) is True


def test_an_event_failure_after_booking_also_lifts_the_gate(monkeypatch):
    _db(monkeypatch)
    assert _process("m1", THREAD, YES, "skipped:event-failed") is True
    assert ledger.claim_content_hash(THREAD, YES) is True


def test_a_successful_send_keeps_the_gate_shut(monkeypatch):
    """The whole point: once the renter HAS the reply, the same words must
    never earn a second one."""
    _db(monkeypatch)
    assert _process("m1", THREAD, YES, "sent") is True
    assert ledger.claim_content_hash(THREAD, YES) is False


def test_a_failure_on_someone_elses_thread_does_not_lift_our_gate(monkeypatch):
    _db(monkeypatch)
    _process("m_other", OTHER_THREAD, QUESTION, "skipped:send-failed")
    assert _process("m1", THREAD, YES, "sent") is True
    assert ledger.claim_content_hash(THREAD, YES) is False


def test_a_dropped_duplicate_does_not_itself_lift_the_gate(monkeypatch):
    """`skipped:dup-content` is a normal outcome, not a failure; treating it
    as one would turn every duplicate into a second reply."""
    _db(monkeypatch)
    _process("m1", THREAD, YES, "sent")
    ledger.claim_message("m2", THREAD)
    ledger.record_message_outcome("m2", "skipped:dup-content")
    assert ledger.claim_content_hash(THREAD, YES) is False


def test_a_firestore_error_in_the_exemption_check_fails_closed(monkeypatch):
    """Unknown state must not cost the renter a duplicate email: when the
    outcome query itself errors, the gate stays shut."""
    db = _db(monkeypatch)
    ledger.claim_content_hash(THREAD, YES)
    plain = db.collection

    def flaky(name):
        if name == "zillow_messages":
            raise RuntimeError("firestore unavailable")
        return plain(name)

    monkeypatch.setattr(db, "collection", flaky)
    assert ledger.claim_content_hash(THREAD, YES) is False
