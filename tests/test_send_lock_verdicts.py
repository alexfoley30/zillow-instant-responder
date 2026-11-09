"""The send lock itself: ledger.reserve_send's verdict state machine.

responder.py's header states the rule the whole service is built on - "every
send must first win a Firestore create-once lock (ledger.reserve_send); losing
the create means someone else already handled it. No lock, no send. Ever."

Every test in this repo that reaches a renter-facing send monkeypatches
reserve_send to the string "acquired", so the function that decides whether a
renter gets emailed twice, once, or never had no test of its own. Each verdict
below is a different real outcome:

  acquired     -> we send
  already-sent -> we stay silent (a second email would be the duplicate bug
                  this module exists to kill)
  in-flight    -> we stay silent (another worker is mid-send)
  recover      -> we Gmail-check, then takeover_send, then send (a renter
                  stranded behind a dead worker gets their reply)

Confusing "recover" for "in-flight" strands a renter; confusing it for
"already-sent" strands them permanently; confusing "already-sent" for
"acquired" double-emails them. The doc data below is the shape
reserve_send/mark_sent/mark_failed actually write.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.api_core.exceptions import AlreadyExists  # noqa: E402
from firebase_admin import firestore  # noqa: E402

import ledger  # noqa: E402

THREAD = "1a16eec59561deee"
STAGE = "reply__1a16eec59561deee-m2"

# What send_stage passes as meta (responder.send_stage).
META = {
    "template": "propose_times",
    "body_sha256": ledger.content_hash("Happy to get you in - does Wednesday work?"),
    "to_relay": "reply+1a16eec59561deee@convo.zillow.com",
    "trigger_message_id": "1a16eec59561deee-m2",
}


class _Snap:
    def __init__(self, data):
        self._d = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._d) if self._d is not None else None


def _resolve_server_timestamps(doc):
    """Firestore resolves SERVER_TIMESTAMP sentinels to real timestamps on
    write, so a doc read back always carries a datetime. Without this the
    fake hands reserve_send a Sentinel and the TTL comparison explodes."""
    return {k: (datetime.now(timezone.utc) if v is firestore.SERVER_TIMESTAMP else v)
            for k, v in doc.items()}


class _DocRef:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def create(self, doc):
        if self._path in self._store:
            raise AlreadyExists(self._path)
        self._store[self._path] = _resolve_server_timestamps(doc)

    def get(self):
        return _Snap(self._store.get(self._path))


class _Collection:
    def __init__(self, store, name):
        self._store, self._name = store, name

    def document(self, doc_id):
        return _DocRef(self._store, f"{self._name}/{doc_id}")


class _DB:
    """Firestore's create-once primitive: create() raises AlreadyExists."""

    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _Collection(self._store, name)


def _wire(monkeypatch, existing=None):
    """Back the ledger with one in-memory zillow_sends doc (or none)."""
    store = {}
    if existing is not None:
        store[f"zillow_sends/{THREAD}__{STAGE}"] = dict(existing)
    monkeypatch.setattr(ledger, "init_db", lambda: _DB(store))
    return store


def _doc(store):
    return store[f"zillow_sends/{THREAD}__{STAGE}"]


def _ago(minutes):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


# ------------------------------------------------- winning the lock

def test_first_send_acquires_and_writes_a_reserved_doc(monkeypatch):
    store = _wire(monkeypatch)
    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    doc = _doc(store)
    assert doc["status"] == "reserved"
    assert doc["attempts"] == 1
    # The meta must survive onto the doc: cron._recover_stuck reads
    # trigger_message_id off it to Gmail-check a stuck send, and the
    # stuck-send alert names the template.
    assert doc["trigger_message_id"] == "1a16eec59561deee-m2"
    assert doc["template"] == "propose_times"
    assert doc["to_relay"] == "reply+1a16eec59561deee@convo.zillow.com"


def test_the_lock_is_per_thread_and_stage(monkeypatch):
    """A second stage on the same thread is its own lock - winning the first
    reply must not silence the later booking confirmation."""
    store = _wire(monkeypatch)
    assert ledger.reserve_send(THREAD, STAGE, META) == "acquired"
    assert ledger.reserve_send(THREAD, "booking_new", META) == "acquired"
    assert ledger.reserve_send(THREAD, STAGE, META) == "in-flight"
    assert f"zillow_sends/{THREAD}__booking_new" in store


# ------------------------------------------------- staying silent

def test_sent_doc_blocks_a_second_email(monkeypatch):
    store = _wire(monkeypatch, {"status": "sent", "sent_at": _ago(90),
                                "template": "propose_times"})
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"
    # The winning doc must not be clobbered by the loser's attempt.
    assert _doc(store)["status"] == "sent"


def test_abandoned_doc_is_never_retried(monkeypatch):
    """mark_abandoned is terminal (thread leased, or a human answered)."""
    _wire(monkeypatch, {"status": "abandoned", "abandoned_at": _ago(30),
                        "abandoned_reason": "review-blocked, escalated at block time"})
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


def test_fresh_reservation_reads_as_in_flight(monkeypatch):
    """Inside RESERVE_TTL_MIN another worker owns it; we do not send."""
    _wire(monkeypatch, {"status": "reserved",
                        "reserved_at": _ago(ledger.RESERVE_TTL_MIN - 1),
                        "attempts": 1})
    assert ledger.reserve_send(THREAD, STAGE, META) == "in-flight"


def test_failed_doc_gives_up_after_three_attempts(monkeypatch):
    """Three failures stop the retry loop; the thread ages into NEEDS_HUMAN
    instead of re-sending forever."""
    _wire(monkeypatch, {"status": "failed", "attempts": 3,
                        "error": "composio GMAIL_REPLY_TO_THREAD failed"})
    assert ledger.reserve_send(THREAD, STAGE, META) == "already-sent"


# ------------------------------------------------- recovering a stranded send

def test_stale_reservation_is_recoverable(monkeypatch):
    """Past the TTL the previous worker is presumed dead. 'recover' is not
    permission to send - send_stage Gmail-checks and then takes the lock
    over - but reading this as 'in-flight' would strand the renter."""
    _wire(monkeypatch, {"status": "reserved",
                        "reserved_at": _ago(ledger.RESERVE_TTL_MIN + 1),
                        "attempts": 1})
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_failed_doc_under_the_cap_is_recoverable(monkeypatch):
    _wire(monkeypatch, {"status": "failed", "attempts": 2,
                        "error": "composio GMAIL_REPLY_TO_THREAD failed"})
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_reserved_doc_with_no_timestamp_does_not_block_forever(monkeypatch):
    """A reservation whose SERVER_TIMESTAMP never materialized has no age to
    compare, so it can never fall out of the TTL. It must read as recoverable,
    not in-flight - otherwise that renter is silenced permanently."""
    _wire(monkeypatch, {"status": "reserved", "attempts": 1})
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_failed_doc_with_no_attempt_count_is_recoverable(monkeypatch):
    """mark_failed writes status+error only; it never writes attempts. A
    failed doc that was never reserved by this code path must still retry."""
    _wire(monkeypatch, {"status": "failed", "error": "send failed"})
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"
