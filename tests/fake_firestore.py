"""In-memory Firestore with the exact semantics the ledger bets on.

Three Firestore behaviours carry this pipeline's whole no-duplicate-send
story: `create()` is atomic and raises AlreadyExists, SERVER_TIMESTAMP is
stamped by the server at write time, and a transaction's writes land all at
once or not at all. Every existing test monkeypatches the ledger functions
that lean on them, so the gates themselves ran untested. This fake supplies
those three behaviours and nothing else, letting the REAL ledger code do the
deciding while a test reads the documents it wrote out of `db.store`.

`store` is {collection: {doc_id: fields}}. `at_offset()` moves the server
clock the fake stamps with, which is how a reservation is made to look ten
minutes stale without sleeping for ten minutes.
"""

import contextlib
import copy
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.api_core.exceptions import AlreadyExists, ServiceUnavailable  # noqa: E402
from google.cloud.firestore_v1.transforms import Increment, Sentinel  # noqa: E402

import ledger  # noqa: E402


class _Snapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field):
        # DocumentSnapshot.get raises on a missing field; blocked_addresses
        # and get_facts both rely on that landing in their except branch.
        if self._data is None or field not in self._data:
            raise KeyError(field)
        return copy.deepcopy(self._data[field])


class _DocRef:
    def __init__(self, db, collection, doc_id):
        self._db = db
        self._collection = collection
        self.id = doc_id

    @property
    def _docs(self):
        return self._db.store.setdefault(self._collection, {})

    def create(self, data):
        if self.id in self._docs:
            raise AlreadyExists(f"{self._collection}/{self.id}")
        self._docs[self.id] = self._db._resolve(data, {})

    def get(self, transaction=None):
        self._db._maybe_fail_read(self._collection)
        return _Snapshot(self.id, self._docs.get(self.id))

    def set(self, data, merge=False):
        base = dict(self._docs.get(self.id) or {}) if merge else {}
        base.update(self._db._resolve(data, base))
        self._docs[self.id] = base

    def update(self, data):
        if self.id not in self._docs:
            raise KeyError(f"no such document {self._collection}/{self.id}")
        self.set(data, merge=True)

    def delete(self):
        self._docs.pop(self.id, None)


class _Query:
    def __init__(self, db, collection, filters=(), limit=None):
        self._db = db
        self._collection = collection
        self._filters = list(filters)
        self._limit = limit

    def where(self, field_path=None, op_string=None, value=None, filter=None):
        if filter is not None:
            field_path, op_string, value = (filter.field_path,
                                            filter.op_string, filter.value)
        return _Query(self._db, self._collection,
                      self._filters + [(field_path, op_string, value)],
                      self._limit)

    def limit(self, count):
        return _Query(self._db, self._collection, self._filters, count)

    def _matches(self, fields):
        for path, op, value in self._filters:
            have = fields.get(path)
            if op == "==" and have != value:
                return False
            if op == "in" and have not in value:
                return False
            if op not in ("==", "in"):
                raise NotImplementedError(f"operator {op!r}")
        return True

    def stream(self):
        self._db._maybe_fail_read(self._collection)
        out = []
        for doc_id, fields in self._db.store.get(self._collection, {}).items():
            if self._matches(fields):
                out.append(_Snapshot(doc_id, fields))
            if self._limit is not None and len(out) >= self._limit:
                break
        return iter(out)


class _Collection(_Query):
    def document(self, doc_id):
        return _DocRef(self._db, self._collection, doc_id)


class _Transaction:
    """Enough of firestore_v1.Transaction for the REAL @firestore.transactional
    decorator to drive it. Writes buffer and land on commit, so a transaction
    that raises leaves the send doc untouched - the property takeover_send
    depends on to not hand two workers the same lock."""

    _read_only = False

    def __init__(self, db, max_attempts=1):
        self._db = db
        self._max_attempts = max_attempts
        self._id = None
        self._writes = []

    def _clean_up(self):
        self._writes = []

    def _begin(self, retry_id=None):
        self._id = b"fake-txn"

    def _commit(self):
        for ref, data in self._writes:
            ref.set(data, merge=True)
        self._writes = []
        self._id = None
        return []

    def _rollback(self):
        self._writes = []
        self._id = None

    def update(self, ref, data):
        self._writes.append((ref, data))


class FakeFirestore:
    def __init__(self, store=None, now_offset=timedelta(), fail_reads=()):
        self.store = store if store is not None else {}
        self.now_offset = now_offset
        self.fail_reads = set(fail_reads)

    # -------------------------------------------------- server behaviours
    def now(self):
        return datetime.now(timezone.utc) + self.now_offset

    @contextlib.contextmanager
    def at_offset(self, **kwargs):
        """Run writes as though the server clock were shifted (negative
        values = in the past)."""
        previous = self.now_offset
        self.now_offset = timedelta(**kwargs)
        try:
            yield self
        finally:
            self.now_offset = previous

    def _resolve(self, data, existing):
        out = {}
        for key, value in data.items():
            if isinstance(value, Sentinel):
                out[key] = self.now()
            elif isinstance(value, Increment):
                out[key] = (existing.get(key) or 0) + value.value
            else:
                out[key] = copy.deepcopy(value)
        return out

    def _maybe_fail_read(self, collection):
        if collection in self.fail_reads:
            raise ServiceUnavailable(f"503 reading {collection}")

    # -------------------------------------------------- client surface
    def collection(self, name):
        return _Collection(self, name)

    def transaction(self, **kwargs):
        return _Transaction(self)


def install(monkeypatch, **kwargs):
    """Point the ledger at a fresh fake and hand it back."""
    db = FakeFirestore(**kwargs)
    monkeypatch.setattr(ledger, "init_db", lambda: db)
    return db
