"""A reply whose send failed must be retryable through /reprocess (the U3 strand).
The dup-content exemption looked for "no-send:send-failed", which nothing writes;
send_stage writes "skipped:send-failed". Review 2026-11-04."""
from datetime import datetime, timezone

import ledger


class _Snap:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return self._d


class _Query:
    def __init__(self, docs):
        self._docs = docs

    def where(self, *a, **k):
        return self

    def stream(self):
        return iter(self._docs)


class _DB:
    def __init__(self, docs):
        self._docs = docs

    def collection(self, name):
        return _Query(self._docs)


def _wire(monkeypatch, outcome):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(ledger, "init_db",
                        lambda: _DB([_Snap({"outcome": outcome, "processed_at": now})]))
    return now


def test_send_failed_outcome_lifts_the_dup_gate(monkeypatch):
    now = _wire(monkeypatch, "skipped:send-failed")
    assert ledger._thread_send_failed_since("t1", now) is True


def test_send_failed_after_event_created_also_lifts_it(monkeypatch):
    now = _wire(monkeypatch, "skipped:send-failed-event-created")
    assert ledger._thread_send_failed_since("t1", now) is True


def test_dup_content_outcome_keeps_the_gate(monkeypatch):
    now = _wire(monkeypatch, "skipped:dup-content")
    assert ledger._thread_send_failed_since("t1", now) is False
