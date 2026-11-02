#!/usr/bin/env python3
"""ledger.reserve_send / takeover_send - the Firestore create-once send lock.

"No lock, no send. Ever." (responder.py module docstring). This lock is the
single thing that keeps a double webhook delivery, a cron recovery tick and a
/reprocess from each mailing the same renter. Nothing covered it: the rest of
the suite stubs reserve_send to a literal "acquired", so the verdict logic -
five distinct outcomes read off a Firestore doc - ran unasserted.

The failures are the expensive ones in both directions:

  * a stale lock read as live wedges the thread and the renter is never
    answered (UNFORGET S1/S8: 8 docs sat unrecovered for a week);
  * a live lock read as stale mails the renter twice;
  * a terminal doc (sent/abandoned) read as recoverable re-sends a reply the
    renter already has.

These run against an in-memory Firestore stand-in that keeps the semantics
the lock actually depends on: create() raises AlreadyExists, SERVER_TIMESTAMP
materializes to a real UTC timestamp at write time, Increment accumulates,
and set(merge=True) merges. takeover_send runs through the REAL
firestore.transactional decorator so its retry/commit wrapper is exercised,
not bypassed.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud.firestore_v1.transforms import Increment

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger  # noqa: E402

THREAD = "1a17c18a5f6ca383"
STAGE = "reply__1a17c18a5f6ca390"
KEY = f"{THREAD}__{STAGE}"

# What responder.send_stage actually puts in the lock doc.
META = {
    "template": "propose_times",
    "body_sha256": ledger.content_hash("Hi Victor,\n\nHappy to get you in."),
    "to_relay": "reply+30209nyucca@convo.zillow.com",
    "trigger_message_id": "1a17c18a5f6ca390",
}


def _utc(minutes_ago=0):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


# ------------------------------------------------- in-memory Firestore

class _Snap:
    def __init__(self, data):
        self._d = None if data is None else dict(data)

    @property
    def exists(self):
        return self._d is not None

    def to_dict(self):
        return dict(self._d) if self._d is not None else None

    def get(self, field):
        return (self._d or {}).get(field)


class _Ref:
    def __init__(self, store, key):
        self._store, self._key = store, key

    def _resolve(self, fields):
        existing = self._store.data.get(self._key) or {}
        out = {}
        for k, v in fields.items():
            if v is ledger.firestore.SERVER_TIMESTAMP:
                out[k] = _utc()
            elif isinstance(v, Increment):
                out[k] = (existing.get(k) or 0) + v.value
            else:
                out[k] = v
        return out

    def create(self, fields):
        if self._key in self._store.data:
            raise AlreadyExists(f"document {self._key} already exists")
        self._store.data[self._key] = self._resolve(fields)
        self._store.writes.append(("create", self._key))

    def get(self, transaction=None):
        self._store.reads.append(self._key)
        return _Snap(self._store.data.get(self._key))

    def set(self, fields, merge=False):
        resolved = self._resolve(fields)
        if merge and self._key in self._store.data:
            self._store.data[self._key].update(resolved)
        else:
            self._store.data[self._key] = resolved
        self._store.writes.append(("set", self._key))

    def update(self, fields):
        # Firestore's update is not an upsert: it raises NOT_FOUND on a
        # missing document, and takeover_send depends on that.
        if self._key not in self._store.data:
            raise NotFound(f"no document to update: {self._key}")
        self._store.data[self._key].update(self._resolve(fields))
        self._store.writes.append(("update", self._key))


class _Collection:
    def __init__(self, store, name):
        self._store, self._name = store, name

    def document(self, doc_id):
        return _Ref(self._store, f"{self._name}/{doc_id}")


class _Tx:
    """Minimal Transaction the real firestore.transactional can drive."""

    _read_only = False
    _max_attempts = 1

    def __init__(self, store):
        self._store = store
        self._id = b"tx-1"
        self._buffer = []
        self.committed = False
        self.rolled_back = False

    def _clean_up(self):
        self._buffer = []

    def _begin(self, retry_id=None):
        self._id = b"tx-1"

    def update(self, ref, fields):
        self._buffer.append((ref, fields))

    def _commit(self):
        for ref, fields in self._buffer:
            ref.update(fields)
        self._buffer = []
        self.committed = True
        return []

    def _rollback(self):
        self._buffer = []
        self.rolled_back = True


class _FakeDB:
    def __init__(self):
        self.data = {}
        self.writes = []
        self.reads = []
        self.transactions = []

    def collection(self, name):
        return _Collection(self, name)

    def transaction(self):
        tx = _Tx(self)
        self.transactions.append(tx)
        return tx

    # test helpers -------------------------------------------------
    def seed_send(self, **fields):
        self.data[f"zillow_sends/{KEY}"] = dict(fields)

    def send_doc(self, key=KEY):
        return self.data.get(f"zillow_sends/{key}")


@pytest.fixture
def db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(ledger, "_DB", fake)
    return fake


# ------------------------------------------------- reserve_send

def test_first_caller_acquires_and_the_doc_records_the_send(db):
    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    doc = db.send_doc()
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 1
    assert doc["template"] == "propose_times"
    assert doc["body_sha256"] == META["body_sha256"]
    assert doc["trigger_message_id"] == "1a17c18a5f6ca390"
    assert isinstance(doc["reserved_at"], datetime)
    assert db.writes == [("create", f"zillow_sends/{KEY}")]


def test_second_caller_on_a_live_reservation_is_in_flight(db):
    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    assert ledger.reserve_send(THREAD, STAGE, META) == "in-flight"


def test_losing_the_race_leaves_the_winners_doc_untouched(db):
    """The lock doc is the record of what went out. A loser that overwrote it
    would make the audit trail - and the attempt count recovery reads - lie."""
    ledger.reserve_send(THREAD, STAGE, META)
    winner = dict(db.send_doc())
    ledger.reserve_send(THREAD, STAGE, dict(META, body_sha256="deadbeefdeadbeef",
                                            template="counter"))
    assert db.send_doc() == winner
    assert db.send_doc()["attempts"] == 1
    assert db.send_doc()["template"] == "propose_times"


def test_a_sent_doc_never_reopens(db):
    db.seed_send(status="sent", sent_at=_utc(600), attempts=1)
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_an_abandoned_doc_never_reopens(db):
    """Terminal by design (lease-out, human answered): never retried, never
    re-reported."""
    db.seed_send(status="abandoned", abandoned_reason="thread LEASED",
                 reserved_at=_utc(4000))
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_a_reservation_inside_the_ttl_is_still_in_flight(db):
    db.seed_send(status="reserved", reserved_at=_utc(ledger.RESERVE_TTL_MIN - 1),
                 attempts=1)
    assert ledger.reserve_send(THREAD, STAGE, META) == "in-flight"


def test_a_reservation_past_the_ttl_becomes_recoverable(db):
    db.seed_send(status="reserved", reserved_at=_utc(ledger.RESERVE_TTL_MIN + 1),
                 attempts=1)
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_a_reservation_with_no_timestamp_is_recoverable_not_wedged(db):
    """A doc whose SERVER_TIMESTAMP never materialized has no age. Reading
    that as live would strand the renter permanently."""
    db.seed_send(status="reserved", reserved_at=None, attempts=1)
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_a_failed_doc_is_recoverable_until_three_attempts(db):
    db.seed_send(status="failed", error="Composio 500", attempts=1,
                 reserved_at=_utc(30))
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"
    db.seed_send(status="failed", error="Composio 500", attempts=2,
                 reserved_at=_utc(30))
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_a_failed_doc_gives_up_at_three_attempts(db):
    """Three failures stop the retry loop; the thread surfaces through
    NEEDS_HUMAN aging instead of hammering Composio."""
    db.seed_send(status="failed", error="Composio 500", attempts=3,
                 reserved_at=_utc(30))
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_a_failed_doc_with_no_attempt_count_is_recoverable(db):
    db.seed_send(status="failed", error="Composio 500", reserved_at=_utc(30))
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_each_stage_on_a_thread_locks_independently(db):
    """One lock per (thread, stage). A renter's second message must still get
    its own reply."""
    assert ledger.reserve_send(THREAD, "first_reply", META) == "acquired"
    assert ledger.reserve_send(THREAD, "reply__m2", META) == "acquired"
    assert ledger.reserve_send(THREAD, "first_reply", META) == "in-flight"
    assert sorted(db.data) == [f"zillow_sends/{THREAD}__first_reply",
                               f"zillow_sends/{THREAD}__reply__m2"]


def test_mark_sent_then_a_retry_is_refused_the_lock(db):
    """The end-to-end no-duplicate property: once the renter has the email,
    no later caller can send it again."""
    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    ledger.mark_sent(THREAD, STAGE)
    doc = db.send_doc()
    assert doc["status"] == "sent"
    assert isinstance(doc["sent_at"], datetime)
    assert doc["body_sha256"] == META["body_sha256"]   # merge, not replace
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_mark_failed_reopens_the_lock_for_one_retry(db):
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_failed(THREAD, STAGE, "urllib HTTPError 503")
    doc = db.send_doc()
    assert doc["status"] == "failed"
    assert doc["error"] == "urllib HTTPError 503"
    assert doc["attempts"] == 1                        # merge kept the count
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_mark_failed_truncates_a_runaway_error(db):
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_failed(THREAD, STAGE, "x" * 5000)
    assert len(db.send_doc()["error"]) == 500


def test_mark_abandoned_closes_the_lock_for_good(db):
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_abandoned(THREAD, STAGE, "review-blocked, escalated at block time")
    doc = db.send_doc()
    assert doc["status"] == "abandoned"
    assert doc["abandoned_reason"] == "review-blocked, escalated at block time"
    assert isinstance(doc["abandoned_at"], datetime)
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_mark_abandoned_truncates_a_runaway_reason(db):
    ledger.reserve_send(THREAD, STAGE, META)
    ledger.mark_abandoned(THREAD, STAGE, "y" * 900)
    assert len(db.send_doc()["abandoned_reason"]) == 200


# ------------------------------------------------- takeover_send

def test_takeover_claims_a_stale_reservation_and_counts_the_attempt(db):
    stale = _utc(ledger.RESERVE_TTL_MIN + 5)
    db.seed_send(status="reserved", reserved_at=stale, attempts=1,
                 **META)
    assert ledger.takeover_send(THREAD, STAGE) is True
    doc = db.send_doc()
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 2
    assert doc["reserved_at"] > stale                  # clock restarted
    assert doc["body_sha256"] == META["body_sha256"]
    assert db.transactions[0].committed is True


def test_takeover_refuses_a_live_reservation(db):
    fresh = _utc(1)
    db.seed_send(status="reserved", reserved_at=fresh, attempts=1)
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert db.send_doc() == {"status": "reserved", "reserved_at": fresh,
                             "attempts": 1}


def test_takeover_refuses_a_sent_doc(db):
    db.seed_send(status="sent", sent_at=_utc(500), attempts=1)
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert db.send_doc()["status"] == "sent"
    assert db.send_doc()["attempts"] == 1


def test_takeover_refuses_an_abandoned_doc(db):
    db.seed_send(status="abandoned", abandoned_reason="thread CLOSED")
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert db.send_doc()["status"] == "abandoned"


def test_takeover_claims_a_failed_doc(db):
    db.seed_send(status="failed", error="Composio 500", attempts=1,
                 reserved_at=_utc(45))
    assert ledger.takeover_send(THREAD, STAGE) is True
    doc = db.send_doc()
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 2


def test_takeover_refuses_when_the_transaction_errors(db, monkeypatch):
    """Any transaction trouble means we do not know who owns the send, and
    "don't send" is the only safe answer (ledger swallows every tx exception
    and returns False)."""
    def boom(self, transaction=None):
        raise RuntimeError("firestore aborted")

    db.seed_send(status="reserved", reserved_at=_utc(60), attempts=1)
    monkeypatch.setattr(_Ref, "get", boom)
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert db.send_doc()["attempts"] == 1
    assert db.send_doc()["status"] == "reserved"


def test_takeover_on_a_doc_that_vanished_claims_nothing(db):
    """A send doc that is gone has no owner to take over. The decision logic
    reads an empty dict and falls through to the update, which Firestore
    refuses with NOT_FOUND - so the attempt resolves to False rather than
    conjuring a reservation out of nothing."""
    assert ledger.takeover_send(THREAD, STAGE) is False
    assert db.send_doc() is None
    assert db.transactions[0].rolled_back is True
