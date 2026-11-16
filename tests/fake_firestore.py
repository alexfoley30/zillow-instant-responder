"""An in-memory stand-in for the Firestore client `ledger.init_db()` returns.

Only the service is faked; every assertion in the tests that use this file runs
against the real `ledger` functions and the real documents those functions
write. The pieces that matter for correctness are modelled honestly:

  * `create()` raises `google.api_core.exceptions.AlreadyExists` when the
    document exists - that raise IS the pipeline's idempotency primitive.
  * `firestore.SERVER_TIMESTAMP` resolves on write to the clock the test
    controls, so the reserve TTL and the 48h content-hash window are exercised
    with real `datetime` arithmetic instead of being stubbed away.
  * `firestore.Increment` applies, so `attempts` counts the way the real
    backend counts it.
  * `set(merge=True)` merges and `set()` replaces, as Firestore does.
  * transactions run through the REAL `firestore.transactional` decorator
    (`FakeTransaction` implements the protocol it drives), so takeover keeps
    the real commit/rollback wrapper.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.api_core.exceptions import AlreadyExists  # noqa: E402
from google.cloud import firestore  # noqa: E402
from google.cloud.firestore_v1.transforms import Increment  # noqa: E402


class Clock:
    """Test-controlled UTC clock. `advance()` is how these tests age a lock."""

    def __init__(self, start=None):
        self.now = start or datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        from datetime import timedelta
        self.now = self.now + timedelta(**kw)
        return self.now


def _resolve(data: dict, existing: dict, now) -> dict:
    """Apply the write sentinels the ledger uses, as the backend would."""
    out = {}
    for k, v in data.items():
        if v is firestore.SERVER_TIMESTAMP:
            out[k] = now()
        elif isinstance(v, Increment):
            out[k] = (existing.get(k) or 0) + v.value
        else:
            out[k] = v
    return out


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None

    def get(self, field):
        if self._data is None:
            raise KeyError(field)
        return self._data.get(field)


class _Ref:
    def __init__(self, store, path):
        self._store = store
        self.path = path

    def create(self, data):
        if self.path in self._store.docs:
            raise AlreadyExists(f"document already exists: {self.path}")
        self._store.docs[self.path] = _resolve(data, {}, self._store.clock)
        self._store.writes.append(("create", self.path))

    def get(self, transaction=None):
        return _Snap(self.path.split("/")[-1], self._store.docs.get(self.path))

    def set(self, data, merge=False):
        existing = self._store.docs.get(self.path) or {}
        resolved = _resolve(data, existing, self._store.clock)
        if merge:
            merged = dict(existing)
            merged.update(resolved)
            self._store.docs[self.path] = merged
        else:
            self._store.docs[self.path] = resolved
        self._store.writes.append(("set" if not merge else "merge", self.path))

    def update(self, data):
        if self.path not in self._store.docs:
            raise AlreadyExists(f"no such document: {self.path}")
        existing = self._store.docs[self.path]
        existing.update(_resolve(data, existing, self._store.clock))
        self._store.writes.append(("update", self.path))

    def delete(self):
        self._store.docs.pop(self.path, None)
        self._store.writes.append(("delete", self.path))


class _Query:
    def __init__(self, store, prefix, conds=(), limit=None):
        self._store = store
        self._prefix = prefix
        self._conds = list(conds)
        self._limit = limit

    def where(self, field=None, op=None, value=None, filter=None):  # noqa: A002
        if filter is not None:
            field, op, value = filter.field_path, filter.op_string, filter.value
        return _Query(self._store, self._prefix,
                      self._conds + [(field, op, value)], self._limit)

    def limit(self, n):
        return _Query(self._store, self._prefix, self._conds, n)

    def stream(self):
        out = []
        for path, data in self._store.docs.items():
            if not path.startswith(self._prefix + "/"):
                continue
            if all(_match(data, f, op, v) for f, op, v in self._conds):
                out.append(_Snap(path.split("/")[-1], data))
        return iter(out[:self._limit] if self._limit else out)

    def document(self, doc_id):
        return _Ref(self._store, f"{self._prefix}/{doc_id}")


def _match(data, field, op, value):
    actual = (data or {}).get(field)
    if op == "==":
        return actual == value
    if op == "<":
        return actual is not None and actual < value
    if op == "in":
        return actual in value
    raise NotImplementedError(f"fake firestore: operator {op!r}")


class FakeTransaction:
    """Implements the protocol `firestore.transactional` drives, so the real
    decorator (begin -> callable -> commit, rollback on raise) runs unchanged."""

    _read_only = False
    _max_attempts = 1

    def __init__(self, store):
        self._store = store
        self._id = None
        self._writes = []

    def _clean_up(self):
        self._writes = []

    def _begin(self, retry_id=None):
        self._id = b"fake-txn"

    def _commit(self):
        for ref, data in self._writes:
            ref.update(data)
        self._writes = []
        self._store.commits += 1
        return []

    def _rollback(self):
        self._writes = []
        self._store.rollbacks += 1

    def update(self, ref, data):
        self._writes.append((ref, data))


class FakeFirestore:
    def __init__(self, clock=None):
        self.clock = clock or Clock()
        self.docs = {}
        self.writes = []
        self.commits = 0
        self.rollbacks = 0

    def collection(self, name):
        return _Query(self, name)

    def transaction(self):
        return FakeTransaction(self)

    # ---- helpers for tests
    def doc(self, collection, doc_id):
        return self.docs.get(f"{collection}/{doc_id}")

    def install(self, monkeypatch, ledger_module):
        monkeypatch.setattr(ledger_module, "init_db", lambda: self)
        monkeypatch.setattr(ledger_module, "_now", self.clock)
        return self
