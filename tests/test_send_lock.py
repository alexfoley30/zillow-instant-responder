"""The Firestore idempotency core: ledger.reserve_send + claim_content_hash.

"No lock, no send. Ever." Every renter-facing send in the service first asks
reserve_send for a verdict, and every inbound message first asks
claim_content_hash whether Zillow has already shown us these words. Both
functions are pure create-once logic over Firestore, both have a history of
production incidents written into their docstrings (mikayla + Jessica 8/29,
Schneider 8/30), and neither had a test: the suites that touch a send lock
all replace reserve_send with a lambda returning "acquired".

These drive the real functions against a Firestore double that behaves like
the service does - create() raises AlreadyExists, SERVER_TIMESTAMP resolves
to a server-side write time - and assert on the verdicts and the documents
that come back out.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from firebase_admin import firestore  # noqa: E402
from google.api_core.exceptions import AlreadyExists  # noqa: E402

import ledger  # noqa: E402

THREAD = "1a0504f3183c6691"
STAGE = "first_reply"
LOCK_ID = f"{THREAD}__{STAGE}"
META = {"template": "availability_ask", "to_relay": "reply+x@convo.zillow.com",
        "trigger_message_id": "1a0504f3183c6691",
        "body_sha256": "6b1f8c9a2d3e4f50"}
RENTER_TEXT = "Yes, Wednesday at 3:30 works for me."


def _now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------- Firestore double


def _resolve(data):
    """Server-side write semantics: SERVER_TIMESTAMP becomes a real time."""
    out = {}
    for key, value in (data or {}).items():
        out[key] = _now() if value is firestore.SERVER_TIMESTAMP else value
    return out


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = None if data is None else dict(data)

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return None if self._data is None else dict(self._data)

    def get(self, field):
        return None if self._data is None else self._data.get(field)


class _DocRef:
    def __init__(self, store, path, doc_id):
        self._store, self._path, self._id = store, path, doc_id

    def create(self, data):
        if self._path in self._store:
            raise AlreadyExists(self._path)
        self._store[self._path] = _resolve(data)

    def set(self, data, merge=False):
        if merge and self._path in self._store:
            self._store[self._path].update(_resolve(data))
        else:
            self._store[self._path] = _resolve(data)

    def get(self, transaction=None):
        return _Snap(self._id, self._store.get(self._path))

    def delete(self):
        self._store.pop(self._path, None)


class _Query:
    def __init__(self, store, collection, clauses=(), cap=None):
        self._store, self._collection = store, collection
        self._clauses, self._cap = list(clauses), cap

    def where(self, field=None, op=None, value=None, filter=None):  # noqa: A002
        if filter is not None:
            field, op, value = filter.field_path, filter.op_string, filter.value
        return _Query(self._store, self._collection,
                      self._clauses + [(field, op, value)], self._cap)

    def limit(self, count):
        return _Query(self._store, self._collection, self._clauses, count)

    def _matches(self, data):
        for field, op, value in self._clauses:
            got = data.get(field)
            if op == "==" and got != value:
                return False
            if op == "in" and got not in value:
                return False
        return True

    def stream(self):
        prefix = f"{self._collection}/"
        hits = [_Snap(path[len(prefix):], data)
                for path, data in sorted(self._store.items())
                if path.startswith(prefix) and self._matches(data)]
        return iter(hits if self._cap is None else hits[:self._cap])


class _Collection(_Query):
    def document(self, doc_id):
        return _DocRef(self._store, f"{self._collection}/{doc_id}", doc_id)


class FakeFirestore:
    """Just the surface ledger.py uses, with Firestore's create-once rule."""

    def __init__(self):
        self.docs = {}
        self.unavailable = None   # collection name whose reads 503

    def collection(self, name):
        if name == self.unavailable:
            raise RuntimeError("503 Firestore unavailable")
        return _Collection(self.docs, name)

    # test helpers
    def seed(self, collection, doc_id, **fields):
        self.docs[f"{collection}/{doc_id}"] = dict(fields)

    def doc(self, collection, doc_id):
        return self.docs.get(f"{collection}/{doc_id}")


@pytest.fixture()
def db(monkeypatch):
    fake = FakeFirestore()
    monkeypatch.setattr(ledger, "init_db", lambda: fake)
    return fake


# ------------------------------------------------------- reserve_send


def test_first_reserve_acquires_and_writes_the_lock_doc(db):
    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    doc = db.doc("zillow_sends", LOCK_ID)
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 1
    assert doc["template"] == "availability_ask"
    assert doc["body_sha256"] == META["body_sha256"]
    assert isinstance(doc["reserved_at"], datetime)


def test_each_stage_locks_independently_on_one_thread(db):
    assert ledger.reserve_send(THREAD, "first_reply", META) == "acquired"
    assert ledger.reserve_send(THREAD, "nudge", META) == "acquired"
    assert ledger.reserve_send(THREAD, "first_reply", META) == "in-flight"
    assert sorted(db.docs) == [f"zillow_sends/{THREAD}__first_reply",
                               f"zillow_sends/{THREAD}__nudge"]


def test_a_sent_stage_never_sends_again(db):
    db.seed("zillow_sends", LOCK_ID, status="sent", sent_at=_now(),
            template="availability_ask")
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_reserve_does_not_overwrite_the_sent_doc(db):
    sent_at = _now() - timedelta(hours=3)
    db.seed("zillow_sends", LOCK_ID, status="sent", sent_at=sent_at,
            template="availability_ask")
    ledger.reserve_send(THREAD, STAGE, META)
    assert db.doc("zillow_sends", LOCK_ID) == {
        "status": "sent", "sent_at": sent_at, "template": "availability_ask"}


def test_an_abandoned_stage_is_terminal(db):
    db.seed("zillow_sends", LOCK_ID, status="abandoned",
            abandoned_reason="thread LEASED", abandoned_at=_now())
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_a_fresh_reservation_reads_as_another_worker_in_flight(db):
    db.seed("zillow_sends", LOCK_ID, status="reserved",
            reserved_at=_now() - timedelta(minutes=2), attempts=1)
    assert ledger.reserve_send(THREAD, STAGE, META) == "in-flight"


def test_a_reservation_past_the_ttl_is_recoverable(db):
    db.seed("zillow_sends", LOCK_ID, status="reserved",
            reserved_at=_now() - timedelta(minutes=ledger.RESERVE_TTL_MIN + 1),
            attempts=1)
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_a_reservation_with_no_timestamp_is_recoverable(db):
    """A doc whose SERVER_TIMESTAMP never landed has no age to trust, so it
    must not read as in-flight forever."""
    db.seed("zillow_sends", LOCK_ID, status="reserved", reserved_at=None,
            attempts=1)
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_a_failed_send_stays_retryable_under_three_attempts(db):
    db.seed("zillow_sends", LOCK_ID, status="failed", attempts=2,
            error="composio 503 upstream timeout",
            reserved_at=_now() - timedelta(minutes=40))
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_a_failed_send_gives_up_after_three_attempts(db):
    """Three strikes stops the retry loop; the thread ages out through
    NEEDS_HUMAN instead of hammering Composio."""
    db.seed("zillow_sends", LOCK_ID, status="failed", attempts=3,
            error="composio 503 upstream timeout",
            reserved_at=_now() - timedelta(minutes=40))
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


# ------------------------------------------------------- claim_content_hash


def test_first_sighting_of_renter_words_is_claimed(db):
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    key = f"{THREAD}__{ledger.content_hash(RENTER_TEXT)}"
    assert isinstance(db.doc("zillow_msg_hashes", key)["created_at"], datetime)


def test_zillows_duplicate_copy_is_rejected(db):
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False


def test_the_same_words_with_different_spacing_are_one_claim(db):
    """Zillow's second copy of a message arrives with its own message id and
    whitespace, so the gate hashes normalized text."""
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert ledger.claim_content_hash(
        THREAD, f"  {RENTER_TEXT.upper()}  \n") is False


def test_the_same_words_on_another_thread_are_a_new_claim(db):
    assert ledger.claim_content_hash(THREAD, "Yes") is True
    assert ledger.claim_content_hash("1a16eec59561deee", "Yes") is True


def test_identical_words_after_the_window_are_a_genuine_resend(db):
    """'yes' twice, a week apart, is two renters' worth of intent."""
    key = f"{THREAD}__{ledger.content_hash(RENTER_TEXT)}"
    stale = _now() - timedelta(hours=ledger.CONTENT_HASH_WINDOW_H + 2)
    db.seed("zillow_msg_hashes", key, created_at=stale)
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert db.doc("zillow_msg_hashes", key)["created_at"] > stale


def test_a_retry_after_a_failed_message_is_let_through_every_time(db):
    """mikayla + Jessica (8/29) then Schneider (8/30): the claim exists
    because WE processed this text and then failed, so the renter never got
    the reply the claim protects. The claim must stay anchored to the
    original processing - refreshing it made the SECOND retry a dupe again."""
    key = f"{THREAD}__{ledger.content_hash(RENTER_TEXT)}"
    claimed_at = _now() - timedelta(minutes=20)
    db.seed("zillow_msg_hashes", key, created_at=claimed_at)
    db.seed("zillow_messages", "1a0504f3183c6691", thread_id=THREAD,
            outcome="error:RuntimeError",
            error="create_event returned no id",
            processed_at=claimed_at - timedelta(seconds=1))
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True
    assert db.doc("zillow_msg_hashes", key)["created_at"] == claimed_at
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is True


def test_a_failed_message_on_another_thread_does_not_open_the_gate(db):
    key = f"{THREAD}__{ledger.content_hash(RENTER_TEXT)}"
    db.seed("zillow_msg_hashes", key, created_at=_now() - timedelta(minutes=20))
    db.seed("zillow_messages", "1a16ef0b2c441f90", thread_id="1a16eec59561deee",
            outcome="error:RuntimeError", processed_at=_now())
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False


def test_a_successfully_handled_message_does_not_open_the_gate(db):
    key = f"{THREAD}__{ledger.content_hash(RENTER_TEXT)}"
    db.seed("zillow_msg_hashes", key, created_at=_now() - timedelta(minutes=20))
    db.seed("zillow_messages", "1a0504f3183c6691", thread_id=THREAD,
            outcome="classified", processed_at=_now() - timedelta(minutes=21))
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False


def test_a_failure_older_than_the_grace_window_does_not_open_the_gate(db):
    """The 10-minute grace exists because claim_message stamps a beat before
    claim_content_hash in the same request. A failure from an earlier,
    already-answered round is not this claim's strand."""
    key = f"{THREAD}__{ledger.content_hash(RENTER_TEXT)}"
    claimed_at = _now() - timedelta(hours=5)
    db.seed("zillow_msg_hashes", key, created_at=claimed_at)
    db.seed("zillow_messages", "1a04ff00aa11bb22", thread_id=THREAD,
            outcome="skipped:event-failed",
            processed_at=claimed_at - timedelta(minutes=11))
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False


def test_an_unreadable_failure_check_fails_closed_as_a_duplicate(db):
    """Fail toward silence, never toward a second email to the renter."""
    key = f"{THREAD}__{ledger.content_hash(RENTER_TEXT)}"
    db.seed("zillow_msg_hashes", key, created_at=_now() - timedelta(minutes=20))
    db.unavailable = "zillow_messages"   # the failed-send history read
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False


def test_a_plain_send_failure_outcome_does_NOT_open_the_gate(db):
    """DOCUMENTS A GAP, NOT DESIRED BEHAVIOUR.

    send_stage records a failed email as "skipped:send-failed"
    (responder.py), and the booking paths record "skipped:send-failed-
    event-created" - neither is in ledger._thread_send_failed_since's
    `failed_prefixes`, which instead lists "no-send:send-failed", a string
    nothing in the service produces. So the one failure mode the exemption
    was written for, an email that did not go out, still returns
    dup-content on every /reprocess - the mikayla + Jessica strand.
    Asserted as-is so the behaviour is visible and a fix flips this test.
    """
    key = f"{THREAD}__{ledger.content_hash(RENTER_TEXT)}"
    claimed_at = _now() - timedelta(minutes=20)
    db.seed("zillow_msg_hashes", key, created_at=claimed_at)
    db.seed("zillow_messages", "1a0504f3183c6691", thread_id=THREAD,
            outcome="skipped:send-failed",
            processed_at=claimed_at - timedelta(seconds=1))
    assert ledger.claim_content_hash(THREAD, RENTER_TEXT) is False
