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


# ---------------------------------------------------------------- 2026-11-21
# The three booking paths each hand-rolled send_stage's reserve -> review ->
# calendar -> email -> labels sequence. book_accepted_offer's copy recorded
# "skipped:book-failed" when the fold or the email blew up - a string
# failed_prefixes above does not match - so a renter whose confirmation died
# mid-booking stayed stuck behind "skipped:dup-content" on every /reprocess.
# All three now go through send_stage(commit=...), which records the outcomes
# the exemption actually knows about.
import os  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DRY_RUN", "true")

import responder  # noqa: E402


def _wire_send_stage(monkeypatch, recorded):
    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(responder, "review_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(responder.ledger, "reserve_send",
                        lambda t, s, m: "acquired")
    monkeypatch.setattr(responder.ledger, "content_hash", lambda b: "h")
    monkeypatch.setattr(responder.ledger, "upsert_thread", lambda *a, **k: None)
    monkeypatch.setattr(responder.ledger, "mark_sent",
                        lambda t, s, **f: recorded.update(sent=f))
    monkeypatch.setattr(responder.ledger, "mark_failed",
                        lambda t, s, e: recorded.update(failed=str(e)))
    monkeypatch.setattr(responder.ledger, "set_labels_pending",
                        lambda *a, **k: None)
    monkeypatch.setattr(responder.gm, "modify_labels", lambda *a, **k: None)


def _send(**kw):
    return responder.send_stage("t1", "booked__evC", "x@convo.zillow.com",
                                "You're all set!", [], [],
                                {"template": "booking_fold"}, "m1", **kw)


def test_email_failure_after_the_calendar_write_is_retryable(monkeypatch):
    recorded = {}
    _wire_send_stage(monkeypatch, recorded)
    monkeypatch.setattr(responder.gm, "send_reply",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    out = _send(commit=lambda: {"event_id": "evC"})
    assert out == "skipped:send-failed-event-created"
    assert out.startswith(ledger.SEND_FAILED_PREFIXES)  # lifts the dup gate
    assert "evC" in recorded["failed"]  # the orphan event is named


def test_calendar_failure_aborts_the_send_and_is_retryable(monkeypatch):
    recorded = {}
    sent = []
    _wire_send_stage(monkeypatch, recorded)
    monkeypatch.setattr(responder.gm, "send_reply", lambda *a: sent.append(a))
    out = _send(commit=lambda: (_ for _ in ()).throw(RuntimeError("no cal")))
    assert out == "skipped:event-failed"
    assert sent == []  # never promise a showing that has no event
    assert "no cal" in recorded["failed"]


def test_commit_fields_land_on_the_send_doc(monkeypatch):
    recorded = {}
    _wire_send_stage(monkeypatch, recorded)
    monkeypatch.setattr(responder.gm, "send_reply", lambda *a: None)
    assert _send(commit=lambda: {"event_id": "evNEW"}) == "sent"
    assert recorded["sent"] == {"event_id": "evNEW"}


def test_every_booking_outcome_is_known_to_the_dup_exemption():
    """Every outcome a booking can record must lift the dup-content gate.
    "skipped:book-failed", the string the old hand-rolled copy wrote, did
    not - which is the bug that made this worth collapsing."""
    for outcome in ("skipped:send-failed", "skipped:send-failed-event-created",
                    "skipped:event-failed"):
        assert outcome.startswith(ledger.SEND_FAILED_PREFIXES)
    assert not "skipped:book-failed".startswith(ledger.SEND_FAILED_PREFIXES)
