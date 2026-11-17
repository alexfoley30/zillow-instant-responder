"""Both Haiku calls go through ONE wrapper (llm._haiku_json) instead of two
hand-copied ones. These tests pin the shared contract and the drift the two
copies had accumulated: the review call used to derive "now in Phoenix" from
a hardcoded UTC-7 offset (and a deprecated utcnow()), while the classify call
took it from its caller.
"""

import json
import sys
import types
from datetime import datetime

import llm
import rules


def _fake_anthropic(monkeypatch, payload, seen=None):
    """Install a stub `anthropic` whose messages.create returns `payload`
    (a JSON-serializable dict, or an Exception to raise)."""
    class _Msgs:
        def create(self, **kw):
            if seen is not None:
                seen.update(kw)
            if isinstance(payload, BaseException):
                raise payload
            block = types.SimpleNamespace(type="text", text=json.dumps(payload))
            return types.SimpleNamespace(content=[block])

    class _Client:
        def __init__(self, **kw):
            if seen is not None:
                seen["client_kwargs"] = kw
            self.messages = _Msgs()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(Anthropic=_Client))


def test_no_api_key_returns_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm._haiku_json("p", {}, 16, "x") is None


def test_shared_wrapper_passes_model_schema_and_budget(monkeypatch):
    seen = {}
    _fake_anthropic(monkeypatch, {"ok": True}, seen)
    assert llm._haiku_json("my prompt", {"type": "object"}, 99, "x") == {"ok": True}
    assert seen["model"] == llm.MODEL
    assert seen["max_tokens"] == 99
    assert seen["output_config"]["format"]["schema"] == {"type": "object"}
    assert seen["messages"] == [{"role": "user", "content": "my prompt"}]
    # Timeout/retry policy is now stated once, not twice.
    assert seen["client_kwargs"]["timeout"] == 10.0
    assert seen["client_kwargs"]["max_retries"] == 1


def test_wrapper_swallows_failures_so_callers_can_fall_back(monkeypatch):
    _fake_anthropic(monkeypatch, RuntimeError("502 upstream"))
    assert llm._haiku_json("p", {}, 16, "x") is None


def test_classify_falls_back_to_regex_when_the_call_fails(monkeypatch):
    _fake_anthropic(monkeypatch, RuntimeError("boom"))
    out = llm.classify_reply("can I come Friday at 2pm?", "",
                             datetime(2026, 11, 17, 9, 0, tzinfo=rules.AZ_TZ))
    assert out["_fallback"] is True
    assert out["intent"] in llm.INTENTS


def test_review_fails_open_on_an_api_failure(monkeypatch):
    _fake_anthropic(monkeypatch, RuntimeError("boom"))
    assert llm.review_reply("t", "body", "counter") == {
        "verdict": "send", "reason": "review-unavailable"}


def test_review_fails_open_on_a_bogus_verdict(monkeypatch):
    _fake_anthropic(monkeypatch, {"verdict": "maybe", "reason": "?"})
    assert llm.review_reply("t", "body", "counter")["verdict"] == "send"
    assert llm.review_reply("t", "body", "counter")["reason"] == "review-malformed"


def test_review_now_line_uses_real_phoenix_time_not_a_fixed_offset(monkeypatch):
    """Arizona is UTC-7 year round, so the old hardcoded offset happened to
    be right - but it bypassed the project's one timezone source. Both
    prompts now read the Phoenix clock the same way."""
    seen = {}
    _fake_anthropic(monkeypatch, {"verdict": "send", "reason": "ok"}, seen)
    llm.review_reply("transcript", "body", "counter")
    expected = datetime.now(rules.AZ_TZ).strftime("%A, %B %d, %Y at")
    assert expected in seen["messages"][0]["content"]


def test_callers_still_control_the_now_line(monkeypatch):
    seen = {}
    _fake_anthropic(monkeypatch, {"verdict": "send", "reason": "ok"}, seen)
    llm.review_reply("transcript", "body", "counter",
                     now_phx=datetime(2026, 10, 12, 13, 0, tzinfo=rules.AZ_TZ))
    assert "Monday, October 12, 2026 at 01:00 PM" in seen["messages"][0]["content"]
