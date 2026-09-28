"""The Firestore send lock - ledger.reserve_send / takeover_send.

This lock is the only thing standing between a renter and a second copy of
the same email, and the only thing that lets a crashed worker's stage be
picked up instead of stranded. Every branch of the verdict table was
untested; each one below is a distinct real-world cost:

  acquired      -> we send
  already-sent  -> a copy went out; a second would embarrass Alex
  in-flight     -> a sibling worker is mid-send; sending now double-books
  recover       -> the owner died; nobody sends unless we take over

Run: python -m pytest tests/test_send_lock.py -q
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
STAGE = "availability_ask"
KEY = f"{THREAD}__{STAGE}"

META = {
    "template": "availability_ask",
    "to_relay": "t.199c1f2a3b4c5d6e@convo.zillow.com",
    "body_sha256": "3f2a91c7b4e05d18",
    "trigger_message_id": "199c1f2a3b4c5d6e",
}


def _age(db, key, minutes):
    """Backdate the reserved_at stamp on an existing send doc."""
    db.docs("zillow_sends")[key]["reserved_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes))


# ---------------------------------------------------------------- acquire

def test_first_reserve_acquires_and_stores_the_send_metadata(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)

    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"

    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 1
    assert doc["template"] == "availability_ask"
    assert doc["body_sha256"] == "3f2a91c7b4e05d18"
    assert doc["to_relay"] == "t.199c1f2a3b4c5d6e@convo.zillow.com"
    assert isinstance(doc["reserved_at"], datetime)


def test_a_different_stage_on_the_same_thread_is_its_own_lock(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)

    assert ledger.reserve_send(THREAD, "availability_ask", META) == "acquired"
    assert ledger.reserve_send(THREAD, "booking_confirmation", META) == "acquired"

    assert sorted(db.docs("zillow_sends")) == [
        f"{THREAD}__availability_ask", f"{THREAD}__booking_confirmation"]


# ---------------------------------------------------------------- refusals

def test_second_worker_on_a_fresh_reservation_is_told_in_flight(monkeypatch):
    fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)

    assert ledger.reserve_send(THREAD, STAGE, META) == "in-flight"


def test_reserve_after_a_completed_send_reports_already_sent(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_sent(THREAD, STAGE)

    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"
    # The sent stamp survives the losing attempt - no reservation overwrite.
    assert db.doc("zillow_sends", KEY)["status"] == "sent"
    assert isinstance(db.doc("zillow_sends", KEY)["sent_at"], datetime)


def test_abandoned_stage_is_never_reopened(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_failed(THREAD, STAGE, "composio 502")
    ledger.mark_abandoned(THREAD, STAGE, "thread leased before retry")

    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"
    assert db.doc("zillow_sends", KEY)["abandoned_reason"] == (
        "thread leased before retry")


def test_three_failed_attempts_stop_retrying(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    db.docs("zillow_sends")[KEY]["attempts"] = 3
    ledger.mark_failed(THREAD, STAGE, "composio 502")

    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_a_failed_send_under_the_attempt_cap_is_recoverable(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_failed(THREAD, STAGE, "composio 502")
    assert db.doc("zillow_sends", KEY)["attempts"] == 1

    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_mark_failed_keeps_the_error_for_the_audit_trail(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_failed(THREAD, STAGE, "GMAIL_REPLY_TO_THREAD: 502 upstream")

    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "failed"
    assert doc["error"] == "GMAIL_REPLY_TO_THREAD: 502 upstream"
    assert doc["template"] == "availability_ask"  # merge, not overwrite


# ---------------------------------------------------- the TTL boundary

def test_reservation_inside_the_ttl_still_blocks(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    _age(db, KEY, ledger.RESERVE_TTL_MIN - 1)

    assert ledger.reserve_send(THREAD, STAGE, META) == "in-flight"


def test_reservation_past_the_ttl_opens_recovery(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    _age(db, KEY, ledger.RESERVE_TTL_MIN + 1)

    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


# ---------------------------------------------------------------- takeover

def test_takeover_of_a_dead_workers_reservation_bumps_attempts(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    _age(db, KEY, 30)
    stale_stamp = db.doc("zillow_sends", KEY)["reserved_at"]

    assert ledger.takeover_send(THREAD, STAGE) is True

    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 2
    assert doc["reserved_at"] > stale_stamp


def test_takeover_loses_to_a_rival_that_just_refreshed(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    _age(db, KEY, 30)
    assert ledger.takeover_send(THREAD, STAGE) is True  # rival gets there first

    # Second recoverer arrives a moment later against the refreshed doc.
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert db.doc("zillow_sends", KEY)["attempts"] == 2  # not bumped again


def test_takeover_refuses_once_the_email_has_gone_out(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    _age(db, KEY, 30)
    ledger.mark_sent(THREAD, STAGE)

    assert ledger.takeover_send(THREAD, STAGE) is False
    assert db.doc("zillow_sends", KEY)["status"] == "sent"


def test_takeover_refuses_an_abandoned_stage(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    _age(db, KEY, 30)
    ledger.mark_abandoned(THREAD, STAGE, "human answered in Gmail")

    assert ledger.takeover_send(THREAD, STAGE) is False


def test_takeover_treats_a_transaction_blowup_as_do_not_send(monkeypatch):
    """Any tx race resolves against sending: a lost lock must never become a
    second email."""
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    _age(db, KEY, 30)

    def boom():
        raise RuntimeError("firestore transaction aborted")
    monkeypatch.setattr(db, "transaction", boom)

    assert ledger.takeover_send(THREAD, STAGE) is False
    assert db.doc("zillow_sends", KEY)["attempts"] == 1  # nothing committed


# ------------------------------------------------- stale-doc sweep (cron)

def test_stale_reserved_docs_returns_only_the_overdue_non_terminal(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    for stage, minutes in (("availability_ask", 90), ("propose_times", 2)):
        ledger.reserve_send(THREAD, stage, META)
        _age(db, f"{THREAD}__{stage}", minutes)
    ledger.reserve_send(THREAD, "booking_confirmation", META)
    _age(db, f"{THREAD}__booking_confirmation", 90)
    ledger.mark_sent(THREAD, "booking_confirmation")

    ids = sorted(s.id for s in ledger.stale_reserved_docs(older_than_min=30))

    assert ids == [f"{THREAD}__availability_ask"]


def test_a_reservation_with_no_timestamp_is_swept_not_skipped(monkeypatch):
    """A doc that lost its reserved_at is exactly the kind that sits forever;
    the sweep has to pick it up rather than treat missing as fresh."""
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    del db.docs("zillow_sends")[KEY]["reserved_at"]

    assert [s.id for s in ledger.stale_reserved_docs(older_than_min=30)] == [KEY]


def test_label_retry_queue_carries_the_exact_label_ids(monkeypatch):
    db = fake_firestore.install(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_sent(THREAD, STAGE)
    ledger.set_labels_pending(THREAD, STAGE, ["Label_1717014254813700027"],
                              ["Label_6932202305849666189"])

    assert [s.id for s in ledger.pending_label_docs()] == [KEY]
    doc = db.doc("zillow_sends", KEY)
    assert doc["labels_add"] == ["Label_1717014254813700027"]
    assert doc["labels_remove"] == ["Label_6932202305849666189"]

    ledger.clear_labels_pending(THREAD, STAGE)
    assert list(ledger.pending_label_docs()) == []
    assert db.doc("zillow_sends", KEY)["status"] == "sent"  # untouched
