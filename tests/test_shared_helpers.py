"""Guards for the two shared helpers that replaced copy-pasted blocks:
responder._apply_labels / _relabel_handled and llm._json_call.

These exist so the de-duplication cannot quietly change behaviour: a label
failure must never raise into the send path, and it must only queue a retry
when there is a stage doc to hang it on.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm  # noqa: E402
import responder  # noqa: E402


# ------------------------------------------------------------ label helper

class _Recorder:
    def __init__(self, fail=False):
        self.fail = fail
        self.modify_calls = []
        self.pending_calls = []

    def modify_labels(self, thread_id, add, remove):
        self.modify_calls.append((thread_id, add, remove))
        if self.fail:
            raise RuntimeError("gmail 500")

    def set_labels_pending(self, thread_id, stage_key, add, remove):
        self.pending_calls.append((thread_id, stage_key, add, remove))


def _patched(monkeypatch, rec):
    monkeypatch.setattr(responder.gm, "modify_labels", rec.modify_labels)
    monkeypatch.setattr(responder.ledger, "set_labels_pending",
                        rec.set_labels_pending)


def test_relabel_handled_sends_handled_minus_awaiting(monkeypatch):
    rec = _Recorder()
    _patched(monkeypatch, rec)
    responder._relabel_handled("t1", "reply__m1")
    assert rec.modify_calls == [
        ("t1", [responder.HANDLED_LABEL], [responder.AWAITING_LABEL])]
    assert rec.pending_calls == []


def test_label_failure_with_stage_queues_a_retry(monkeypatch):
    rec = _Recorder(fail=True)
    _patched(monkeypatch, rec)
    responder._relabel_handled("t1", "reply__m1")  # must not raise
    assert rec.pending_calls == [
        ("t1", "reply__m1", [responder.HANDLED_LABEL],
         [responder.AWAITING_LABEL])]


def test_label_failure_without_stage_is_dropped_not_queued(monkeypatch):
    """The decline / benign paths never reserved a send, so there is no stage
    doc for cron's _retry_labels to key off. Queuing one would write a
    pending-label doc under an empty stage key."""
    rec = _Recorder(fail=True)
    _patched(monkeypatch, rec)
    responder._relabel_handled("t1")  # must not raise
    assert rec.pending_calls == []


def test_empty_label_change_touches_gmail_not_at_all(monkeypatch):
    rec = _Recorder()
    _patched(monkeypatch, rec)
    responder._apply_labels("t1", [], [], "reply__m1")
    assert rec.modify_calls == []


# ------------------------------------------------------------- llm wrapper

def test_review_reply_without_a_key_is_unavailable_not_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.review_reply("t", "body", "tpl") == {
        "verdict": "send", "reason": "review-unavailable"}


def test_review_reply_fails_open_on_a_broken_call(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_json_call", lambda *a, **k: None)
    assert llm.review_reply("t", "body", "tpl") == {
        "verdict": "send", "reason": "review-error"}


def test_review_reply_rejects_a_verdict_outside_the_enum(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_json_call", lambda *a, **k: {"verdict": "maybe"})
    assert llm.review_reply("t", "body", "tpl") == {
        "verdict": "send", "reason": "review-malformed"}


def test_review_reply_passes_a_block_through(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_json_call",
                        lambda *a, **k: {"verdict": "block", "reason": "dup"})
    assert llm.review_reply("t", "body", "tpl")["verdict"] == "block"


def test_json_call_treats_a_non_object_response_as_failure(monkeypatch):
    """Both callers .get() on the result straight away, so a bare list or
    string coming back from the model is a failure, not a result."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class _Block:
        type = "text"
        text = '["not", "an", "object"]'

    class _Client:
        def __init__(self, **_kw):
            self.messages = self

        def create(self, **_kw):
            return type("R", (), {"content": [_Block()]})()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    assert llm._json_call("p", {}, 16, "test") is None


def test_classify_reply_falls_back_to_regex_when_the_key_is_missing(monkeypatch):
    from datetime import datetime
    import rules
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = llm.classify_reply("Can I see it Tuesday at 3pm?", "",
                             datetime(2026, 9, 7, 9, 0, tzinfo=rules.AZ_TZ))
    assert out["_fallback"] is True
    assert out["intent"] in llm.INTENTS
