"""The Firestore send lock - ledger.reserve_send / takeover_send / mark_*.

This is the primitive the whole no-duplicates design rests on ("No lock, no
send. Ever." - responder.py header), and it had no test of its own: every
existing test monkeypatches reserve_send away and asserts on what the caller
did with a hard-coded verdict. So the verdicts themselves - the ones that
decide whether a renter gets a second copy of an email, or never gets the
first - were never exercised.

These call the real ledger functions against an in-memory Firestore that
keeps the semantics that matter: create() raises AlreadyExists, merges merge,
SERVER_TIMESTAMP resolves at write time, Increment increments, and the real
@firestore.transactional decorator drives takeover_send's transaction body.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DRY_RUN", "true")

import ledger  # noqa: E402
from fakes import install_firestore  # noqa: E402

THREAD = "1a0504f3183c6691"
STAGE = "reply__1a0545afd0fe9807"
KEY = f"{THREAD}__{STAGE}"
META = {"template": "propose_times",
        "body_sha256": "9f2c1b0a4d7e8a35",
        "to_relay": "reply+abc123@convo.zillow.com",
        "trigger_message_id": "1a0545afd0fe9807"}


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def _sends(db):
    return db.data.setdefault("zillow_sends", {})


# ---------------------------------------------------------------- acquire

def test_first_reserve_acquires_and_writes_the_lock_doc(monkeypatch):
    db = install_firestore(monkeypatch, ledger)
    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 1
    assert doc["template"] == "propose_times"
    assert doc["to_relay"] == "reply+abc123@convo.zillow.com"
    assert isinstance(doc["reserved_at"], datetime)


def test_second_worker_on_a_fresh_reservation_is_told_in_flight(monkeypatch):
    """Two Composio webhook deliveries land seconds apart. The second must
    not send, and must not disturb the first worker's reservation."""
    db = install_firestore(monkeypatch, ledger)
    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    reserved_at = db.doc("zillow_sends", KEY)["reserved_at"]
    assert ledger.reserve_send(THREAD, STAGE, META) == "in-flight"
    assert db.doc("zillow_sends", KEY)["attempts"] == 1
    assert db.doc("zillow_sends", KEY)["reserved_at"] == reserved_at


def test_reserve_after_a_completed_send_is_already_sent(monkeypatch):
    db = install_firestore(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_sent(THREAD, STAGE)
    assert db.doc("zillow_sends", KEY)["status"] == "sent"
    assert isinstance(db.doc("zillow_sends", KEY)["sent_at"], datetime)
    # the meta from the reservation survives the merge - the audit trail is
    # what tells a human WHICH email went out
    assert db.doc("zillow_sends", KEY)["template"] == "propose_times"
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_abandoned_docs_are_terminal_not_retryable(monkeypatch):
    """UNFORGET S8: a failed send the world overtook (home leased) is
    abandoned. Abandoning must stop the retry, not restart it."""
    db = install_firestore(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_failed(THREAD, STAGE, "Composio 500")
    ledger.mark_abandoned(THREAD, STAGE, "thread LEASED")
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "abandoned"
    assert doc["abandoned_reason"] == "thread LEASED"
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"
    assert ledger.takeover_send(THREAD, STAGE) is False


def test_a_stale_reservation_is_recoverable_and_a_failed_one_too(monkeypatch):
    """A worker that died mid-send leaves `reserved` behind. After the TTL the
    verdict is 'recover' - the caller Gmail-checks, then takes over."""
    stale = _ago(minutes=ledger.RESERVE_TTL_MIN + 5)
    db = install_firestore(monkeypatch, ledger, {"zillow_sends": {
        KEY: {**META, "status": "reserved", "reserved_at": stale, "attempts": 1}}})
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"
    assert db.doc("zillow_sends", KEY)["attempts"] == 1  # verdict alone writes nothing

    db.data["zillow_sends"][KEY]["status"] = "failed"
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_three_failed_attempts_stop_retrying(monkeypatch):
    """Give up instead of hammering a send that keeps failing; the thread
    surfaces to a human through NEEDS_HUMAN aging instead."""
    install_firestore(monkeypatch, ledger, {"zillow_sends": {
        KEY: {**META, "status": "failed", "attempts": 3,
              "reserved_at": _ago(hours=2)}}})
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_reservation_with_no_timestamp_is_recoverable(monkeypatch):
    """A doc written before reserved_at existed (or a partial write) must not
    read as 'someone is sending right now' forever."""
    install_firestore(monkeypatch, ledger, {"zillow_sends": {
        KEY: {**META, "status": "reserved", "attempts": 1}}})
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


# ---------------------------------------------------------------- takeover

def test_takeover_claims_a_stale_doc_and_counts_the_attempt(monkeypatch):
    stale = _ago(minutes=ledger.RESERVE_TTL_MIN + 5)
    db = install_firestore(monkeypatch, ledger, {"zillow_sends": {
        KEY: {**META, "status": "reserved", "reserved_at": stale, "attempts": 1}}})
    assert ledger.takeover_send(THREAD, STAGE) is True
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 2               # Increment ran inside the tx
    assert doc["reserved_at"] > stale         # the TTL clock restarted
    # and now that we hold a fresh reservation, nobody else may take it
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert db.doc("zillow_sends", KEY)["attempts"] == 2


def test_takeover_refuses_a_reservation_someone_else_still_owns(monkeypatch):
    """The exact double-send shape: worker B decides to recover while worker A
    is still in flight. B must lose."""
    fresh = _ago(minutes=1)
    db = install_firestore(monkeypatch, ledger, {"zillow_sends": {
        KEY: {**META, "status": "reserved", "reserved_at": fresh, "attempts": 1}}})
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert db.doc("zillow_sends", KEY)["reserved_at"] == fresh


def test_takeover_never_reopens_a_sent_doc(monkeypatch):
    db = install_firestore(monkeypatch, ledger, {"zillow_sends": {
        KEY: {**META, "status": "sent", "sent_at": _ago(hours=3)}}})
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert db.doc("zillow_sends", KEY)["status"] == "sent"


# ------------------------------------------------------- stale-doc sweep

def test_stale_reserved_docs_filters_in_python_not_in_a_compound_query(monkeypatch):
    """2026-09-02 (UNFORGET S1/S8): the old status+reserved_at query needed a
    composite index that never existed, so every recovery tick threw and 8
    docs sat unrecovered for a week. The sweep must return the old
    reserved/failed docs and leave fresh and terminal ones alone."""
    install_firestore(monkeypatch, ledger, {"zillow_sends": {
        "tA__reply__m1": {"status": "reserved", "reserved_at": _ago(hours=4)},
        "tB__reply__m2": {"status": "failed", "reserved_at": _ago(days=1)},
        "tC__reply__m3": {"status": "reserved", "reserved_at": _ago(minutes=2)},
        "tD__reply__m4": {"status": "sent", "sent_at": _ago(days=2)},
        "tE__reply__m5": {"status": "abandoned", "reserved_at": _ago(days=3)},
        "tF__reply__m6": {"status": "reserved"},  # never stamped
    }})
    ids = [s.id for s in ledger.stale_reserved_docs(older_than_min=30)]
    assert ids == ["tA__reply__m1", "tB__reply__m2", "tF__reply__m6"]


def test_stale_sweep_respects_its_limit(monkeypatch):
    install_firestore(monkeypatch, ledger, {"zillow_sends": {
        f"t{i}__reply__m{i}": {"status": "failed", "reserved_at": _ago(hours=5)}
        for i in range(6)}})
    assert len(ledger.stale_reserved_docs(older_than_min=30, limit=4)) == 4


# ---------------------------------------------------------------- bookkeeping

def test_mark_failed_records_a_truncated_error_without_losing_the_lock(monkeypatch):
    db = install_firestore(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_failed(THREAD, STAGE, "HTTPError 500: " + "x" * 900)
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "failed"
    assert len(doc["error"]) == 500
    assert doc["error"].startswith("HTTPError 500: xxx")
    assert doc["trigger_message_id"] == "1a0545afd0fe9807"


def test_label_retry_queue_round_trip(monkeypatch):
    """A label failure must never trigger a resend; it parks on the send doc
    and the cron picks it up from there."""
    db = install_firestore(monkeypatch, ledger)
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_sent(THREAD, STAGE)
    ledger.set_labels_pending(THREAD, STAGE, ["Label_6932202305849666189"],
                              ["Label_2252577853408309931"])
    pending = ledger.pending_label_docs()
    assert [s.id for s in pending] == [KEY]
    assert pending[0].to_dict()["labels_add"] == ["Label_6932202305849666189"]
    ledger.clear_labels_pending(THREAD, STAGE)
    assert ledger.pending_label_docs() == []
    assert db.doc("zillow_sends", KEY)["status"] == "sent"  # still sent, once
