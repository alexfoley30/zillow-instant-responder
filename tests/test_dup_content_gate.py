"""The content-hash gate (ledger.claim_content_hash) — the Firestore write
that decides whether a renter ever gets a reply.

Untested before this file, and wrong twice in production:

  * mikayla + Jessica (2026-08-29) — after a send failed mid-thread,
    /reprocess answered "dup-content" forever and both renters sat
    stranded with no reply at all.
  * Schneider (2026-08-30) — the first fix refreshed created_at when the
    strand exemption fired, which moved the claim clock past the failure
    and made the SECOND retry a dupe again: three reprocess rounds, still
    no reply.

These exercise the real ledger functions. The only Firestore behaviour
modelled is the one the module is built on, per ledger.py's own docstring:
document.create() is atomic and raises AlreadyExists when the document
already exists — "That is the idempotency primitive."
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from google.api_core.exceptions import AlreadyExists

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger  # noqa: E402

# Real Gmail-shaped ids and the renter text that actually strands threads:
# a one-word answer Zillow is happy to deliver twice.
THREAD = "1a0504f3183c6691"
YES = "Yes that time works for me, see you then!"


def _now():
    return datetime.now(timezone.utc)


def _resolve(data: dict) -> dict:
    """Server-side timestamp resolution, the way Firestore does it on write."""
    return {k: (_now() if v is ledger.firestore.SERVER_TIMESTAMP else v)
            for k, v in data.items()}


class _Snapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None

    def get(self, field):
        return self._data[field]


class _DocumentRef:
    def __init__(self, store, key):
        self._store = store
        self._key = key

    def create(self, data):
        if self._key in self._store:
            raise AlreadyExists("/".join(self._key))
        self._store[self._key] = _resolve(data)

    def set(self, data, merge=False):
        if merge and self._key in self._store:
            self._store[self._key].update(_resolve(data))
        else:
            self._store[self._key] = _resolve(data)

    def get(self):
        return _Snapshot(self._key[1], self._store.get(self._key))


class _Query:
    def __init__(self, store, collection, field_filter):
        self._store = store
        self._collection = collection
        self._ff = field_filter

    def stream(self):
        assert self._ff.op_string == "==", self._ff.op_string
        for (coll, doc_id), data in list(self._store.items()):
            if coll == self._collection and \
                    data.get(self._ff.field_path) == self._ff.value:
                yield _Snapshot(doc_id, data)


class _Collection:
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def document(self, doc_id):
        return _DocumentRef(self._store, (self._name, doc_id))

    def where(self, *args, filter=None, **kwargs):
        # ledger queries zillow_messages with the FieldFilter keyword form on
        # purpose (the positional form is deprecated and warns); if that ever
        # regresses, this test double should fail loudly rather than cope.
        assert filter is not None and not args, (args, kwargs)
        return _Query(self._store, self._name, filter)


class _FakeDB:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return _Collection(self.store, name)


@pytest.fixture
def db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(ledger, "init_db", lambda: fake)
    return fake


def _claim_key(thread_id=THREAD, text=YES):
    return ("zillow_msg_hashes", f"{thread_id}__{ledger.content_hash(text)}")


def _seed_claim(db, claimed_at, thread_id=THREAD, text=YES):
    db.store[_claim_key(thread_id, text)] = {"created_at": claimed_at}


def _seed_message(db, outcome, processed_at, message_id="1a0545afd0fe9807",
                  thread_id=THREAD):
    db.store[("zillow_messages", message_id)] = {
        "thread_id": thread_id,
        "outcome": outcome,
        "processed_at": processed_at,
    }


# ------------------------------------------------- the ordinary two outcomes

def test_first_sighting_claims_the_text_and_allows_the_reply(db):
    assert ledger.claim_content_hash(THREAD, YES) is True
    claim = db.store[_claim_key()]
    assert _now() - claim["created_at"] < timedelta(seconds=30)


def test_zillow_duplicate_copy_is_refused(db):
    # Zillow re-delivers the same renter words under a new message id; the
    # message-id gate can't see that, so this gate is what stops the second
    # reply going out.
    assert ledger.claim_content_hash(THREAD, YES) is True
    assert ledger.claim_content_hash(THREAD, YES) is False


def test_claim_is_scoped_to_one_thread(db):
    assert ledger.claim_content_hash(THREAD, YES) is True
    # A different renter saying the identical words must still get a reply.
    assert ledger.claim_content_hash("1a0601bb77cc4412", YES) is True


def test_same_words_a_week_later_are_a_genuine_resend(db):
    # Past the 48h window an identical "yes" is plausibly a real second
    # message, and the window moves forward with it.
    stale = _now() - timedelta(hours=49)
    _seed_claim(db, stale)
    assert ledger.claim_content_hash(THREAD, YES) is True
    assert db.store[_claim_key()]["created_at"] > stale
    assert _now() - db.store[_claim_key()]["created_at"] < timedelta(seconds=30)


def test_inside_the_window_a_successful_send_keeps_the_gate_shut(db):
    # The thread has history, but nothing failed — still a duplicate.
    claimed = _now() - timedelta(minutes=5)
    _seed_claim(db, claimed)
    _seed_message(db, "sent", claimed + timedelta(seconds=2))
    assert ledger.claim_content_hash(THREAD, YES) is False


# ------------------------------------------- the strand heal (8/29 and 8/30)

@pytest.mark.parametrize("outcome", [
    "skipped:event-failed",
    "error: 400 composio create_event failed",
    "no-send:calendar-error",
    "no-send:send-failed",
])
def test_failure_after_the_claim_unstrands_the_retry(db, outcome):
    # The claim exists because WE processed this text and then the send or
    # booking failed, so the renter never got the reply the claim was
    # protecting against duplicating. A re-run is a retry, not a dupe.
    claimed = _now() - timedelta(minutes=5)
    _seed_claim(db, claimed)
    _seed_message(db, outcome, claimed + timedelta(seconds=2))
    assert ledger.claim_content_hash(THREAD, YES) is True


def test_strand_exemption_leaves_the_claim_clock_alone(db):
    """Schneider, 2026-08-30 — the regression that shipped as a fix.

    Refreshing created_at when the exemption fires moves the claim past the
    failure that justified it, so the next retry is a dupe again. Retries
    must keep passing until one actually succeeds.
    """
    claimed = _now() - timedelta(minutes=5)
    _seed_claim(db, claimed)
    _seed_message(db, "skipped:event-failed", claimed + timedelta(seconds=2))

    assert ledger.claim_content_hash(THREAD, YES) is True
    assert db.store[_claim_key()]["created_at"] == claimed, \
        "exemption must not re-stamp created_at"
    # Second and third reprocess rounds — the ones that stayed stranded.
    assert ledger.claim_content_hash(THREAD, YES) is True
    assert ledger.claim_content_hash(THREAD, YES) is True
    assert db.store[_claim_key()]["created_at"] == claimed


def test_ten_minute_grace_covers_the_claim_stamp_ordering(db):
    # claim_message stamps processed_at a beat BEFORE claim_content_hash
    # stamps created_at in the same request, so the very message whose
    # failure stranded the hash always looks slightly older than the claim.
    # Without the grace the exemption can never fire for the primary case.
    claimed = _now() - timedelta(minutes=5)
    _seed_claim(db, claimed)
    _seed_message(db, "skipped:event-failed", claimed - timedelta(minutes=5))
    assert ledger.claim_content_hash(THREAD, YES) is True


def test_failure_older_than_the_grace_is_not_this_claims_strand(db):
    # A failure from an earlier, unrelated message must not hold the gate
    # open for every duplicate that follows.
    claimed = _now() - timedelta(minutes=5)
    _seed_claim(db, claimed)
    _seed_message(db, "skipped:event-failed", claimed - timedelta(minutes=20))
    assert ledger.claim_content_hash(THREAD, YES) is False


def test_another_threads_failure_never_unstrands_this_one(db):
    claimed = _now() - timedelta(minutes=5)
    _seed_claim(db, claimed)
    _seed_message(db, "skipped:event-failed", claimed + timedelta(seconds=2),
                  thread_id="1a0601bb77cc4412")
    assert ledger.claim_content_hash(THREAD, YES) is False


def test_exemption_lookup_error_fails_toward_duplicate(db, monkeypatch):
    # A broken zillow_messages query must not turn every duplicate copy
    # into a second email to the renter.
    claimed = _now() - timedelta(minutes=5)
    _seed_claim(db, claimed)
    _seed_message(db, "skipped:event-failed", claimed + timedelta(seconds=2))
    monkeypatch.setattr(ledger, "init_db", lambda: _PartialBoom(db.store))
    assert ledger.claim_content_hash(THREAD, YES) is False


class _PartialBoom(_FakeDB):
    """The hash doc reads fine; the zillow_messages query is what breaks."""

    def __init__(self, store):
        super().__init__()
        self.store = store

    def collection(self, name):
        if name == "zillow_messages":
            raise RuntimeError("503 Firestore unavailable")
        return super().collection(name)
