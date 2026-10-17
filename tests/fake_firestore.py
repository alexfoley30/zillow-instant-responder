"""In-memory stand-in for the Firestore client the ledger talks to.

Not a mock that records calls - a working store with the two semantics the
ledger's idempotency actually rests on:

  * document.create() raises AlreadyExists when the doc exists. That single
    atomic primitive is what stops a renter getting the same email twice.
  * SERVER_TIMESTAMP resolves at write time to a real UTC datetime, so the
    ledger's age/TTL arithmetic runs for real instead of against a sentinel.

Transactions use the REAL google.cloud.firestore transactional decorator
(commit/rollback/retry path included); only the wire is replaced. Writes
made inside a transaction are buffered and land on commit, so a rollback
leaves the store untouched, as it would in production.
"""

import copy
from datetime import datetime, timezone

from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.transforms import Increment


def _resolve(value, now):
    if value is firestore.SERVER_TIMESTAMP:
        return now
    return value


def _apply(dest: dict, patch: dict, now: datetime) -> dict:
    for key, value in patch.items():
        if isinstance(value, Increment):
            dest[key] = (dest.get(key) or 0) + value.value
        else:
            dest[key] = _resolve(value, now)
    return dest


class _Snapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field):
        if self._data is None:
            raise KeyError(field)
        return self._data.get(field)


class _DocumentRef:
    def __init__(self, store: dict, doc_id: str):
        self._store = store
        self.id = doc_id

    def create(self, data: dict):
        if self.id in self._store:
            raise AlreadyExists(f"document {self.id} already exists")
        self._store[self.id] = _apply({}, data, datetime.now(timezone.utc))
        return self._store[self.id]

    def set(self, data: dict, merge: bool = False):
        now = datetime.now(timezone.utc)
        base = self._store.get(self.id, {}) if merge else {}
        self._store[self.id] = _apply(dict(base), data, now)
        return self._store[self.id]

    def get(self, transaction=None):
        return _Snapshot(self.id, copy.deepcopy(self._store.get(self.id)))

    def delete(self):
        self._store.pop(self.id, None)


class _Query:
    """Supports the three shapes the ledger uses: where(filter=FieldFilter),
    positional where(field, op, value), limit() and stream()."""

    def __init__(self, store: dict, rows=None):
        self._store = store
        self._rows = rows if rows is not None else list(store.items())

    def _filtered(self, field, op, value):
        def keep(item):
            actual = (item[1] or {}).get(field)
            if op == "==":
                return actual == value
            if op == "in":
                return actual in value
            raise NotImplementedError(f"fake firestore: operator {op!r}")
        return _Query(self._store, [r for r in self._rows if keep(r)])

    def where(self, field=None, op=None, value=None, filter=None):  # noqa: A002
        if filter is not None:
            return self._filtered(filter.field_path, filter.op_string, filter.value)
        return self._filtered(field, op, value)

    def limit(self, n):
        return _Query(self._store, self._rows[:n])

    def stream(self):
        return iter([_Snapshot(k, copy.deepcopy(v)) for k, v in self._rows])


class _Collection(_Query):
    def __init__(self, store: dict):
        super().__init__(store)

    def document(self, doc_id: str):
        return _DocumentRef(self._store, doc_id)

    def stream(self):
        return iter([_Snapshot(k, copy.deepcopy(v))
                     for k, v in list(self._store.items())])


class _Transaction:
    """Buffers writes until commit, like the real one. Carries the private
    attributes google's @transactional decorator drives it through."""

    _read_only = False
    _max_attempts = 5

    def __init__(self):
        self._id = b"fake-txn"
        self._writes = []
        self.committed = False
        self.rolled_back = False

    def _clean_up(self):
        self._writes = []

    def _begin(self, retry_id=None):
        self._id = b"fake-txn"

    def _commit(self):
        now = datetime.now(timezone.utc)
        for ref, patch in self._writes:
            _apply(ref._store.setdefault(ref.id, {}), patch, now)
        self._writes = []
        self.committed = True
        return []

    def _rollback(self):
        self._writes = []
        self.rolled_back = True

    def update(self, ref, patch):
        self._writes.append((ref, dict(patch)))

    def set(self, ref, patch, merge=False):
        self._writes.append((ref, dict(patch)))


class FakeFirestore:
    """db handle: fake.collection('zillow_sends').document(key) ... plus
    fake.docs('zillow_sends') for tests to read the store back."""

    def __init__(self):
        self.collections = {}

    def collection(self, name: str):
        return _Collection(self.collections.setdefault(name, {}))

    def transaction(self):
        return _Transaction()

    # ---- test-side accessors -------------------------------------------
    def docs(self, name: str) -> dict:
        return self.collections.setdefault(name, {})

    def doc(self, name: str, doc_id: str):
        return self.collections.setdefault(name, {}).get(doc_id)

    def put(self, name: str, doc_id: str, data: dict):
        self.collections.setdefault(name, {})[doc_id] = dict(data)


def install(monkeypatch, ledger_module) -> FakeFirestore:
    """Point the ledger at a fresh in-memory store for one test."""
    fake = FakeFirestore()
    monkeypatch.setattr(ledger_module, "_DB", fake)
    monkeypatch.setattr(ledger_module, "init_db", lambda: fake)
    return fake
