"""Guards on the two shared helpers extracted from hand-rolled copies:
responder._relabel (label swap + retry queue) and gmail_client._post_json
(the one outbound HTTP POST). Both refactors claim "no behaviour change",
so the split these tests pin is the point.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gmail_client as gm  # noqa: E402
import responder  # noqa: E402


# ------------------------------------------------- responder._relabel

class _Recorder:
    def __init__(self, raise_on_modify=False):
        self.modified = []
        self.pending = []
        self.raise_on_modify = raise_on_modify

    def modify_labels(self, thread_id, add, remove):
        self.modified.append((thread_id, add, remove))
        if self.raise_on_modify:
            raise RuntimeError("gmail 503")

    def set_labels_pending(self, thread_id, stage_key, add, remove):
        self.pending.append((thread_id, stage_key, add, remove))


def _patch(monkeypatch, rec):
    monkeypatch.setattr(responder.gm, "modify_labels", rec.modify_labels)
    monkeypatch.setattr(responder.ledger, "set_labels_pending",
                        rec.set_labels_pending)


def test_relabel_swaps_labels(monkeypatch):
    rec = _Recorder()
    _patch(monkeypatch, rec)
    responder._relabel("t1", ["A"], ["B"], "after send", stage_key="s1")
    assert rec.modified == [("t1", ["A"], ["B"])]
    assert rec.pending == []


def test_relabel_noop_when_nothing_to_change(monkeypatch):
    rec = _Recorder()
    _patch(monkeypatch, rec)
    responder._relabel("t1", [], [], "after send", stage_key="s1")
    assert rec.modified == []


def test_relabel_failure_never_raises(monkeypatch):
    rec = _Recorder(raise_on_modify=True)
    _patch(monkeypatch, rec)
    responder._relabel("t1", ["A"], ["B"], "after send", stage_key="s1")


def test_relabel_with_stage_key_queues_the_retry(monkeypatch):
    rec = _Recorder(raise_on_modify=True)
    _patch(monkeypatch, rec)
    responder._relabel("t1", ["A"], ["B"], "after booking", stage_key="s1")
    assert rec.pending == [("t1", "s1", ["A"], ["B"])]


def test_relabel_without_stage_key_does_not_queue(monkeypatch):
    # Pre-existing behaviour of the decline / benign / needs-reply / cancel
    # paths: the swap is dropped, not replayed by cron._retry_labels.
    rec = _Recorder(raise_on_modify=True)
    _patch(monkeypatch, rec)
    responder._relabel("t1", ["A"], ["B"], "decline")
    assert rec.pending == []


# ------------------------------------------------- gmail_client._post_json

def test_post_json_sends_json_and_merges_headers(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["body"] = json.loads(req.data)
        seen["timeout"] = timeout
        seen["content_type"] = req.get_header("Content-type")
        seen["api_key"] = req.get_header("X-api-key")
        return _Resp()

    monkeypatch.setattr(gm.urllib.request, "urlopen", fake_urlopen)
    raw = gm._post_json("https://example.test/x", {"a": 1},
                        {"x-api-key": "k"}, timeout=7)
    assert json.loads(raw) == {"ok": True}
    assert seen["url"] == "https://example.test/x"
    assert seen["method"] == "POST"
    assert seen["body"] == {"a": 1}
    assert seen["timeout"] == 7
    assert seen["content_type"] == "application/json"
    assert seen["api_key"] == "k"


def test_post_json_default_timeout_is_20(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(gm.urllib.request, "urlopen", fake_urlopen)
    gm._post_json("https://example.test/x", {})
    assert seen["timeout"] == 20
