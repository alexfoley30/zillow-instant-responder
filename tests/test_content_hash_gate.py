"""claim_content_hash: the gate that decides whether a renter hears back.

Zillow sends duplicate notification copies of the same renter message with
fresh message ids, so the content hash is the only thing standing between a
renter and a second identical reply. The flip side is worse and has happened
twice in production: when the claim outlives a failed send, /reprocess returns
dup-content forever and the renter is simply never answered (mikayla +
Jessica, 2026-08-29; Schneider needed three rounds, 2026-08-30).

Both fixes live in subtleties that are invisible to a reader - a ten-minute
grace on timestamp skew, and deliberately NOT refreshing the claim clock on
the strand path - so they are pinned here against a Firestore stand-in that
reproduces create-once semantics and server-resolved timestamps.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.api_core.exceptions import AlreadyExists  # noqa: E402
from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

import ledger  # noqa: E402

THREAD = "1a0504f3183c6691"
TEXT = "Tuesday at 10am works for me!"


# --------------------------------------------------------------- Firestore stand-in
# Narrow on purpose: create() is atomic and raises AlreadyExists, SERVER_TIMESTAMP
# resolves server-side to a real UTC instant, and a where() scan streams docs.
# Those three behaviours are the whole contract this module leans on.

def _resolve(data: dict) -> dict:
    from firebase_admin import firestore
    out = {}
    for k, v in (data or {}).items():
        out[k] = (datetime.now(timezone.utc)
                  if v is firestore.SERVER_TIMESTAMP else v)
    return out


class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None

    def get(self, field):
        return (self._data or {}).get(field)


class _Ref:
    def __init__(self, store, key):
        self._store, self._key = store, key

    def create(self, data):
        if self._key in self._store:
            raise AlreadyExists(self._key)
        self._store[self._key] = _resolve(data)

    def set(self, data, merge=False):
        if merge and self._key in self._store:
            self._store[self._key].update(_resolve(data))
        else:
            self._store[self._key] = _resolve(data)

    def get(self):
        return _Snap(self._store.get(self._key))


class _Query:
    def __init__(self, store, filt):
        self._store, self._filt = store, filt

    def stream(self):
        for data in list(self._store.values()):
            if (self._filt is None
                    or data.get(self._filt.field_path) == self._filt.value):
                yield _Snap(data)


class _Collection:
    def __init__(self, store):
        self._store = store

    def document(self, doc_id):
        return _Ref(self._store, doc_id)

    def where(self, filter=None, *a, **kw):  # noqa: A002 - mirrors the real kwarg
        return _Query(self._store, filter)


class _FakeDB:
    def __init__(self, fail_collections=()):
        self.data = {}
        self._fail = set(fail_collections)

    def collection(self, name):
        if name in self._fail:
            raise RuntimeError(f"firestore unavailable: {name}")
        return _Collection(self.data.setdefault(name, {}))


def _db(monkeypatch, fail_collections=()):
    db = _FakeDB(fail_collections)
    monkeypatch.setattr(ledger, "init_db", lambda: db)
    return db


def _hashes(db):
    return db.data.setdefault("zillow_msg_hashes", {})


def _key(thread_id, text):
    return f"{thread_id}__{ledger.content_hash(text)}"


def _log_message(db, thread_id, outcome, processed_at):
    msgs = db.data.setdefault("zillow_messages", {})
    msgs[f"m{len(msgs)}"] = {"thread_id": thread_id, "outcome": outcome,
                             "processed_at": processed_at}


# --------------------------------------------------------------- first sighting

def test_first_sighting_claims_the_text_and_stamps_it(monkeypatch):
    db = _db(monkeypatch)
    assert ledger.claim_content_hash(THREAD, TEXT) is True
    doc = _hashes(db)[_key(THREAD, TEXT)]
    assert isinstance(doc["created_at"], datetime)
    assert (datetime.now(timezone.utc) - doc["created_at"]) < timedelta(seconds=5)


def test_zillow_duplicate_copy_of_the_same_words_is_refused(monkeypatch):
    db = _db(monkeypatch)
    assert ledger.claim_content_hash(THREAD, TEXT) is True
    assert ledger.claim_content_hash(THREAD, TEXT) is False
    assert len(_hashes(db)) == 1


def test_whitespace_and_case_variants_count_as_the_same_message(monkeypatch):
    # Zillow's copies differ in wrapping and quoting; the hash normalises so
    # the second copy cannot slip through as "new words".
    _db(monkeypatch)
    assert ledger.claim_content_hash(THREAD, "Yes that works") is True
    assert ledger.claim_content_hash(THREAD, "  YES THAT WORKS \n") is False


def test_a_genuinely_different_reply_claims_separately(monkeypatch):
    db = _db(monkeypatch)
    assert ledger.claim_content_hash(THREAD, TEXT) is True
    assert ledger.claim_content_hash(THREAD, "Actually, can we do 2pm?") is True
    assert len(_hashes(db)) == 2


def test_the_same_words_from_a_different_renter_are_not_a_duplicate(monkeypatch):
    # Two renters both answering "yes" must both get a reply: the claim is
    # scoped per thread, not global.
    _db(monkeypatch)
    assert ledger.claim_content_hash(THREAD, "yes") is True
    assert ledger.claim_content_hash("1a0545afd0fe9807", "yes") is True


# --------------------------------------------------------------- 48h window

def test_identical_text_after_the_window_is_a_fresh_claim(monkeypatch):
    # "yes" twice a week apart is two real messages, not a duplicate.
    db = _db(monkeypatch)
    stale = datetime.now(timezone.utc) - timedelta(hours=49)
    _hashes(db)[_key(THREAD, "yes")] = {"created_at": stale}
    assert ledger.claim_content_hash(THREAD, "yes") is True
    refreshed = _hashes(db)[_key(THREAD, "yes")]["created_at"]
    assert refreshed > stale
    assert (datetime.now(timezone.utc) - refreshed) < timedelta(seconds=5)


def test_identical_text_just_inside_the_window_is_still_a_duplicate(monkeypatch):
    db = _db(monkeypatch)
    recent = datetime.now(timezone.utc) - timedelta(hours=47)
    _hashes(db)[_key(THREAD, "yes")] = {"created_at": recent}
    assert ledger.claim_content_hash(THREAD, "yes") is False
    assert _hashes(db)[_key(THREAD, "yes")]["created_at"] == recent


# --------------------------------------------------------------- strand exemption

def test_a_failed_send_since_the_claim_unstrands_the_retry(monkeypatch):
    # mikayla + Jessica, 8/29: the claim exists because WE processed this text
    # and then the booking blew up, so the renter never got the reply the
    # claim was protecting. A re-run is a retry, not a duplicate.
    db = _db(monkeypatch)
    claimed = datetime.now(timezone.utc) - timedelta(minutes=30)
    _hashes(db)[_key(THREAD, TEXT)] = {"created_at": claimed}
    _log_message(db, THREAD, "skipped:event-failed",
                 datetime.now(timezone.utc) - timedelta(minutes=29))
    assert ledger.claim_content_hash(THREAD, TEXT) is True


def test_the_strand_exemption_does_not_move_the_claim_clock(monkeypatch):
    # Schneider, 8/30: refreshing created_at here pushed the claim past the
    # failure, so the SECOND /reprocess was a duplicate again and the renter
    # stayed stranded through three rounds. The claim stays anchored to the
    # original processing and every retry passes until one succeeds.
    db = _db(monkeypatch)
    claimed = datetime.now(timezone.utc) - timedelta(minutes=30)
    _hashes(db)[_key(THREAD, TEXT)] = {"created_at": claimed}
    _log_message(db, THREAD, "error:RuntimeError",
                 datetime.now(timezone.utc) - timedelta(minutes=29))
    assert ledger.claim_content_hash(THREAD, TEXT) is True
    assert _hashes(db)[_key(THREAD, TEXT)]["created_at"] == claimed
    assert ledger.claim_content_hash(THREAD, TEXT) is True
    assert _hashes(db)[_key(THREAD, TEXT)]["created_at"] == claimed


def test_claim_order_skew_within_the_grace_still_unstrands(monkeypatch):
    # claim_message stamps processed_at a beat BEFORE claim_content_hash
    # stamps created_at in the same request, so the very message whose failure
    # stranded the hash always looks slightly older than the claim (verified
    # on Mitchell's thread, 8/29). Without the grace the fix never fires.
    db = _db(monkeypatch)
    claimed = datetime.now(timezone.utc) - timedelta(minutes=5)
    _hashes(db)[_key(THREAD, TEXT)] = {"created_at": claimed}
    _log_message(db, THREAD, "no-send:calendar-error",
                 claimed - timedelta(minutes=3))
    assert ledger.claim_content_hash(THREAD, TEXT) is True


def test_a_failure_older_than_the_grace_is_not_this_claims_failure(monkeypatch):
    # A failure from last week's exchange must not permanently exempt every
    # later duplicate on the thread - that would re-open the duplicate-send
    # hole the hash exists to close.
    db = _db(monkeypatch)
    claimed = datetime.now(timezone.utc) - timedelta(minutes=5)
    _hashes(db)[_key(THREAD, TEXT)] = {"created_at": claimed}
    _log_message(db, THREAD, "skipped:event-failed",
                 claimed - timedelta(hours=6))
    assert ledger.claim_content_hash(THREAD, TEXT) is False


def test_a_successfully_handled_thread_still_refuses_the_duplicate(monkeypatch):
    db = _db(monkeypatch)
    claimed = datetime.now(timezone.utc) - timedelta(minutes=30)
    _hashes(db)[_key(THREAD, TEXT)] = {"created_at": claimed}
    _log_message(db, THREAD, "sent", datetime.now(timezone.utc) - timedelta(minutes=29))
    _log_message(db, THREAD, "classified", datetime.now(timezone.utc))
    assert ledger.claim_content_hash(THREAD, TEXT) is False


def test_another_threads_failure_does_not_unstrand_this_one(monkeypatch):
    db = _db(monkeypatch)
    claimed = datetime.now(timezone.utc) - timedelta(minutes=30)
    _hashes(db)[_key(THREAD, TEXT)] = {"created_at": claimed}
    _log_message(db, "1a0545afd0fe9807", "skipped:event-failed",
                 datetime.now(timezone.utc))
    assert ledger.claim_content_hash(THREAD, TEXT) is False


def test_lookup_failure_treats_the_message_as_a_duplicate(monkeypatch):
    # Fail toward silence, not toward a second email: an unreadable message
    # log cannot be read as permission to re-send.
    db = _db(monkeypatch, fail_collections=["zillow_messages"])
    _hashes(db)[_key(THREAD, TEXT)] = {
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=30)}
    assert ledger.claim_content_hash(THREAD, TEXT) is False


# --------------------------------------------------------------- helper directly

def test_failed_since_reads_the_outcome_prefixes_it_documents(monkeypatch):
    db = _db(monkeypatch)
    claimed = datetime.now(timezone.utc)
    for outcome in ("skipped:event-failed", "error:RuntimeError",
                    "no-send:calendar-error"):
        db.data["zillow_messages"] = {}
        _log_message(db, THREAD, outcome, claimed)
        assert ledger._thread_send_failed_since(THREAD, claimed) is True, outcome
    for outcome in ("sent", "classified", "dup-content", "skipped:not-zillow"):
        db.data["zillow_messages"] = {}
        _log_message(db, THREAD, outcome, claimed)
        assert ledger._thread_send_failed_since(THREAD, claimed) is False, outcome


def test_field_filter_is_the_query_shape_the_ledger_uses(monkeypatch):
    # Guards the stand-in itself: ledger queries by keyword FieldFilter, and a
    # positional where() would silently stream the whole collection.
    db = _db(monkeypatch)
    _log_message(db, THREAD, "skipped:event-failed", datetime.now(timezone.utc))
    _log_message(db, "other-thread", "skipped:event-failed", datetime.now(timezone.utc))
    coll = db.collection("zillow_messages")
    rows = list(coll.where(filter=FieldFilter("thread_id", "==", THREAD)).stream())
    assert [r.to_dict()["thread_id"] for r in rows] == [THREAD]
