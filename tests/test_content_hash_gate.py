"""ledger.claim_content_hash - the duplicate-copy gate, and the escape hatch
that keeps a renter from being stranded behind it.

Zillow sends the same renter words twice under two message ids, so the gate
has to say no. But the gate is claimed BEFORE the reply is attempted, so a
send or booking that blows up afterwards leaves a claim guarding a reply
that never went out - and /reprocess then answers "dup-content" forever
while the renter waits (mikayla + Jessica, 2026-08-29). Two invariants carry
the fix and neither had a test:

  * a failure at or after the claim re-opens the gate (with a 10-minute
    grace, because claim_message stamps a beat before claim_content_hash in
    the same request - Mitchell, 8/29);
  * the re-open must NOT refresh created_at, or the second retry is a dupe
    again (Schneider, 8/30, three reprocess rounds).

Run: python -m pytest tests/test_content_hash_gate.py -q
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import fake_firestore  # noqa: E402
import ledger  # noqa: E402

THREAD = "199c1f2a3b4c5d6e"
RENTER_TEXT = ("Hi! Yes, tomorrow at 3:15 works for me. "
               "Is the fridge included? - Mikayla")


def _key(text=RENTER_TEXT, thread=THREAD):
    return f"{thread}__{ledger.content_hash(text)}"


def _claimed_at(db, text=RENTER_TEXT, thread=THREAD):
    return db.doc("zillow_msg_hashes", _key(text, thread))["created_at"]


def _backdate(db, minutes, text=RENTER_TEXT, thread=THREAD):
    db.docs("zillow_msg_hashes")[_key(text, thread)]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes))


def _message(db, msg_id, outcome, minutes_ago, thread=THREAD):
    db.put("zillow_messages", msg_id, {
        "thread_id": thread,
        "outcome": outcome,
        "processed_at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    })


# ---------------------------------------------------------------- the gate

def test_first_sighting_claims_the_text(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert isinstance(_claimed_at(db), datetime)


def test_zillows_second_copy_of_the_same_words_is_refused(monkeypatch):
    fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False


def test_the_refusal_does_not_move_the_claim_clock(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)
    _backdate(db, 120)
    anchor = _claimed_at(db)

    ledger.claim_content_hash(THREAD, RENTER_TEXT)

    assert _claimed_at(db) == anchor


def test_relay_reformatting_of_the_same_words_still_counts_as_a_copy(monkeypatch):
    """html_to_text can hand back the same sentence with different case and
    surrounding whitespace; the hash normalises so it is still one message."""
    fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)

    assert ledger.claim_content_hash(
        THREAD, f"  {RENTER_TEXT.upper()}\n") is False


def test_a_genuinely_different_reply_is_not_blocked(monkeypatch):
    fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)

    assert ledger.claim_content_hash(
        THREAD, "Actually I cannot do 3:15pm, sorry!") is True


def test_the_same_words_from_a_different_renter_thread_pass(monkeypatch):
    fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, "Is it still available?")

    assert ledger.claim_content_hash(
        "199d0000aaaa1111", "Is it still available?") is True


# ------------------------------------------------------------ 48h window

def test_the_same_yes_a_week_later_is_a_new_message(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, "yes")
    _backdate(db, 7 * 24 * 60, text="yes")
    stale = _claimed_at(db, text="yes")

    assert ledger.claim_content_hash(THREAD, "yes") is True
    assert _claimed_at(db, text="yes") > stale  # window restarted


def test_inside_the_48h_window_it_is_still_a_duplicate(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, "yes")
    _backdate(db, 47 * 60, text="yes")

    assert ledger.claim_content_hash(THREAD, "yes") is False


# -------------------------------------------------- the strand exemption

def test_a_booking_failure_after_the_claim_reopens_the_gate(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)
    _backdate(db, 60)
    _message(db, "199c1f2a3b4c5d6e", "skipped:event-failed", minutes_ago=59)

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True


def test_a_crash_outcome_reopens_the_gate(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)
    _backdate(db, 60)
    _message(db, "199c1f2a3b4c5d6e", "error:HTTPError", minutes_ago=59)

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True


def test_a_calendar_error_reopens_the_gate(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)
    _backdate(db, 60)
    _message(db, "199c1f2a3b4c5d6e", "no-send:calendar-error", minutes_ago=59)

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True


def test_a_reply_that_actually_went_out_keeps_the_gate_shut(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)
    _backdate(db, 60)
    _message(db, "199c1f2a3b4c5d6e", "sent", minutes_ago=59)
    _message(db, "199c1f2a3b4c5d70", "classified", minutes_ago=58)

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False


def test_another_threads_failure_does_not_unlock_this_one(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)
    _backdate(db, 60)
    _message(db, "199aaaa0000bbbb1", "skipped:event-failed", minutes_ago=59,
             thread="199d0000aaaa1111")

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False


def test_the_ten_minute_grace_covers_the_claim_ordering(monkeypatch):
    """claim_message stamps processed_at a beat BEFORE claim_content_hash
    stamps created_at in the same request, so the very failure that stranded
    the claim always looks slightly older than it (Mitchell, 2026-08-29)."""
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)
    _backdate(db, 60)
    _message(db, "199c1f2a3b4c5d6e", "skipped:event-failed", minutes_ago=63)

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True


def test_a_failure_from_an_older_round_does_not_reopen_the_gate(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)
    _backdate(db, 60)
    _message(db, "199aaaa0000bbbb1", "skipped:event-failed", minutes_ago=200)

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False


def test_repeated_reprocess_keeps_passing_until_a_send_succeeds(monkeypatch):
    """The Schneider case (2026-08-30): refreshing created_at on the exemption
    moved the claim clock past the failure, so retry #2 was a dupe again and
    three reprocess rounds died in a row. The anchor must not move."""
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)
    _backdate(db, 60)
    anchor = _claimed_at(db)
    _message(db, "199c1f2a3b4c5d6e", "skipped:event-failed", minutes_ago=59)

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert _claimed_at(db) == anchor
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert _claimed_at(db) == anchor


def test_an_unreadable_message_history_is_treated_as_a_duplicate(monkeypatch):
    """Can't prove a strand -> don't re-send. The safe side of this gate is
    silence, not a second email."""
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.claim_content_hash(THREAD, RENTER_TEXT)
    real_collection = db.collection

    def flaky(name):
        if name == "zillow_messages":
            raise RuntimeError("503 Firestore unavailable")
        return real_collection(name)
    monkeypatch.setattr(db, "collection", flaky)

    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False
