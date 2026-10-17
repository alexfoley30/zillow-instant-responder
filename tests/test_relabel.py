"""The one relabel() path that all eight former copies collapsed into."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger  # noqa: E402
import responder  # noqa: E402


class _Spy:
    def __init__(self, boom=False):
        self.calls, self.queued, self.boom = [], [], boom

    def modify_labels(self, thread_id, add, remove):
        self.calls.append((thread_id, add, remove))
        if self.boom:
            raise RuntimeError("gmail 503")
        return {}

    def set_labels_pending(self, thread_id, stage_key, add, remove):
        self.queued.append((thread_id, stage_key, add, remove))


def _patch(monkeypatch, spy):
    monkeypatch.setattr(responder.gm, "modify_labels", spy.modify_labels)
    monkeypatch.setattr(ledger, "set_labels_pending", spy.set_labels_pending)


def test_happy_path_moves_labels(monkeypatch):
    spy = _Spy()
    _patch(monkeypatch, spy)
    assert responder.relabel("t1", ["A"], ["B"], "test") is True
    assert spy.calls == [("t1", ["A"], ["B"])]
    assert spy.queued == []


def test_empty_move_is_a_noop(monkeypatch):
    spy = _Spy()
    _patch(monkeypatch, spy)
    assert responder.relabel("t1", [], [], "test") is True
    assert spy.calls == []


def test_failure_never_raises(monkeypatch):
    spy = _Spy(boom=True)
    _patch(monkeypatch, spy)
    assert responder.relabel("t1", ["A"], [], "test") is False


def test_failure_queues_retry_when_a_stage_owns_the_send(monkeypatch):
    spy = _Spy(boom=True)
    _patch(monkeypatch, spy)
    assert responder.relabel("t1", ["A"], ["B"], "test", stage_key="s9") is False
    assert spy.queued == [("t1", "s9", ["A"], ["B"])]


def test_failure_without_a_stage_does_not_queue(monkeypatch):
    spy = _Spy(boom=True)
    _patch(monkeypatch, spy)
    assert responder.relabel("t1", ["A"], ["B"], "test") is False
    assert spy.queued == []
