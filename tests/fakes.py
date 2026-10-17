"""In-memory stand-ins for the two outside boundaries the pipeline talks to.

These are NOT re-implementations of our logic - they are the transport layer
only, so the production functions under test (ledger.reserve_send,
responder.send_stage, cron._nudge_and_close) run their real code paths against
real document shapes:

  FakeFirestore  - the slice of google-cloud-firestore the ledger uses:
                   create() raising AlreadyExists (the idempotency primitive),
                   set(merge=), SERVER_TIMESTAMP resolved at write time the way
                   the server resolves it, Increment, where()/limit()/stream(),
                   and a transaction the REAL @firestore.transactional
                   decorator can drive.
  FakeComposio   - the Composio v3 execute endpoint gmail_client posts to,
                   recording every call and returning v3-shaped responses.

Nothing here decides anything the production code should be deciding.
"""

import copy
from datetime import datetime, timezone

from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.transforms import Increment


# ---------------------------------------------------------------- firestore

def _resolve(value, previous):
    """Server-side sentinel resolution, same as Firestore does on write."""
    if value is firestore.SERVER_TIMESTAMP:
        return datetime.now(timezone.utc)
    if isinstance(value, Increment):
        return (previous or 0) + value.value
    return value


def _apply(target: dict, patch: dict) -> dict:
    for k, v in patch.items():
        target[k] = _resolve(v, target.get(k))
    return target


_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


class FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field):
        if not self._data or field not in self._data:
            raise KeyError(field)
        return copy.deepcopy(self._data[field])


class FakeDocument:
    def __init__(self, docs: dict, doc_id: str, path: str):
        self._docs = docs
        self.id = doc_id
        self.path = path

    def create(self, data: dict):
        if self.id in self._docs:
            raise AlreadyExists(f"Document already exists: {self.path}")
        self._docs[self.id] = _apply({}, data)

    def set(self, data: dict, merge: bool = False):
        if merge and self.id in self._docs:
            _apply(self._docs[self.id], data)
        else:
            self._docs[self.id] = _apply({}, data)

    def update(self, data: dict):
        if self.id not in self._docs:
            raise KeyError(self.path)
        _apply(self._docs[self.id], data)

    def get(self, transaction=None):
        return FakeSnapshot(self.id, copy.deepcopy(self._docs.get(self.id)))

    def delete(self):
        self._docs.pop(self.id, None)


class FakeQuery:
    def __init__(self, docs: dict, clauses=None, limit=None):
        self._docs = docs
        self._clauses = list(clauses or [])
        self._limit = limit

    def where(self, field=None, op=None, value=None, filter=None):  # noqa: A002
        if filter is not None:
            field, op, value = filter.field_path, filter.op_string, filter.value
        return FakeQuery(self._docs, self._clauses + [(field, op, value)],
                         self._limit)

    def limit(self, n):
        return FakeQuery(self._docs, self._clauses, n)

    def stream(self):
        out = []
        for doc_id, data in list(self._docs.items()):
            if all(_OPS[op](data.get(field), value)
                   for field, op, value in self._clauses):
                out.append(FakeSnapshot(doc_id, copy.deepcopy(data)))
            if self._limit is not None and len(out) >= self._limit:
                break
        return iter(out)


class FakeCollection(FakeQuery):
    def __init__(self, docs: dict, name: str):
        super().__init__(docs)
        self.name = name

    def document(self, doc_id: str):
        return FakeDocument(self._docs, doc_id, f"{self.name}/{doc_id}")


class FakeTransaction:
    """Enough of google.cloud.firestore_v1.Transaction that the real
    @firestore.transactional decorator can run our production function body
    (it calls _clean_up/_begin/_commit and reads _id/_read_only/_max_attempts).
    """

    _read_only = False
    _max_attempts = 5

    def __init__(self):
        self._id = None
        self._writes = []

    def _clean_up(self):
        self._writes = []
        self._id = None

    def _begin(self, retry_id=None):
        self._id = b"fake-transaction"

    def _commit(self):
        for ref, fields in self._writes:
            ref.update(fields)
        self._writes = []
        self._id = None
        return []

    def _rollback(self):
        self._clean_up()

    def update(self, ref, fields):
        self._writes.append((ref, fields))


class FakeFirestore:
    """`data` is {collection_name: {doc_id: {field: value}}}."""

    def __init__(self, data: dict = None):
        self.data = {k: copy.deepcopy(v) for k, v in (data or {}).items()}

    def collection(self, name: str):
        return FakeCollection(self.data.setdefault(name, {}), name)

    def transaction(self):
        return FakeTransaction()

    # convenience for assertions
    def doc(self, collection: str, doc_id: str):
        return self.data.get(collection, {}).get(doc_id)

    def ids(self, collection: str):
        return sorted(self.data.get(collection, {}))


def install_firestore(monkeypatch, ledger_module, data: dict = None):
    """Point ledger.init_db() (and therefore every module that calls it) at an
    in-memory database."""
    db = FakeFirestore(data)
    monkeypatch.setattr(ledger_module, "_DB", db)
    monkeypatch.setattr(ledger_module, "init_db", lambda: db)
    return db


# ---------------------------------------------------------------- composio

class FakeComposio:
    """Stands in for gmail_client.composio_execute.

    `threads` maps thread_id -> the message list GMAIL_FETCH_MESSAGE_BY_THREAD_ID
    should return; `fail` maps a tool slug to an exception to raise.
    """

    def __init__(self, threads: dict = None, fail: dict = None):
        self.threads = threads or {}
        self.fail = fail or {}
        self.calls = []

    def __call__(self, tool_slug: str, arguments: dict) -> dict:
        self.calls.append((tool_slug, arguments))
        if tool_slug in self.fail:
            raise self.fail[tool_slug]
        if tool_slug == "GMAIL_FETCH_MESSAGE_BY_THREAD_ID":
            return {"successful": True,
                    "data": {"messages": self.threads.get(
                        arguments.get("thread_id"), [])}}
        if tool_slug == "GMAIL_REPLY_TO_THREAD":
            return {"successful": True,
                    "data": {"id": "1a06000000000001",
                             "threadId": arguments.get("thread_id")}}
        if tool_slug == "GMAIL_SEND_EMAIL":
            return {"successful": True,
                    "data": {"id": "1a06000000000002",
                             "threadId": "1a06000000000002"}}
        return {"successful": True, "data": {}}

    # -- assertions helpers ------------------------------------------------
    def of(self, tool_slug: str) -> list:
        """Every argument dict passed to one tool, in call order."""
        return [args for slug, args in self.calls if slug == tool_slug]

    @property
    def replies(self) -> list:
        return self.of("GMAIL_REPLY_TO_THREAD")


def install_composio(monkeypatch, gmail_module, threads: dict = None,
                     fail: dict = None):
    fake = FakeComposio(threads, fail)
    monkeypatch.setattr(gmail_module, "composio_execute", fake)
    return fake


# ---------------------------------------------------------------- messages

RELAY = "reply+abc123@convo.zillow.com"


def renter_msg(message_id: str, name: str, text: str,
               relay: str = RELAY) -> dict:
    """A Zillow relay message in the shape Composio returns."""
    return {
        "messageId": message_id,
        "threadId": "thread-1",
        "sender": f"{name} <{relay}>",
        "messageText": (f"{name} says:\n{text}\n\n"
                        "Thanks for using Zillow Rental Manager"),
    }


def alex_msg(message_id: str, text: str) -> dict:
    return {
        "messageId": message_id,
        "threadId": "thread-1",
        "sender": "Alex Foley <alex@azfoleyhomes.com>",
        "messageText": text,
    }
