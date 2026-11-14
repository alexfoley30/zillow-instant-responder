"""Both Haiku callers go through the one shared structured call.

classify and review each used to carry their own copy of the client setup,
the json_schema output_config and the first-text-block extraction, and the
copies had drifted: only review_reply had a fallback for a missing now_phx.
The classify side of that block had no test at all, so these cover it.
"""

import json
import os
import sys
import types
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm  # noqa: E402

NOW = datetime(2026, 10, 12, 13, 0)


def _fake_anthropic(seen, payload):
    class _Msgs:
        def create(self, **kw):
            seen.update(kw)
            block = types.SimpleNamespace(type="text", text=json.dumps(payload))
            return types.SimpleNamespace(content=[block])

    class _Client:
        def __init__(self, **kw):
            seen["client_kwargs"] = kw
            self.messages = _Msgs()

    return types.SimpleNamespace(Anthropic=_Client)


def test_classify_api_path_sends_schema_and_clock(monkeypatch):
    seen = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(seen, {
        "intent": "accept_offer", "time_candidates": [], "wants_same_day": False,
        "question_text": None, "cancel_reason": None, "constraints": [],
        "earliest_daily": None, "latest_daily": None, "declined_times": [],
    }))
    out = llm._api_call("that works", "RENTER: that works", NOW)
    assert out["intent"] == "accept_offer"
    assert seen["model"] == llm.MODEL
    assert seen["max_tokens"] == 1536
    assert seen["output_config"]["format"]["schema"] is llm.SCHEMA
    assert seen["client_kwargs"]["timeout"] == 10.0
    assert seen["client_kwargs"]["max_retries"] == 1
    # the shared clock format, not a second copy of the strftime string
    assert "Monday, October 12, 2026 at 01:00 PM" in seen["messages"][0]["content"]


def test_classify_falls_back_to_regex_when_the_call_raises(monkeypatch):
    class _Boom:
        def __init__(self, **kw):
            raise RuntimeError("no api")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(Anthropic=_Boom))
    assert llm._api_call("that works", "", NOW) is None
    # classify_reply still answers, via the conservative regex path
    cls = llm.classify_reply("that works", "", NOW)
    assert cls["_fallback"] is True


def test_classify_returns_none_without_a_key_and_never_calls_out(monkeypatch):
    called = []
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(
        Anthropic=lambda **kw: called.append(kw)))
    assert llm._api_call("hi", "", NOW) is None
    assert called == []


def test_phx_stamp_fallback_is_shared_by_both_prompts(monkeypatch):
    assert llm.phx_stamp(NOW) == "Monday, October 12, 2026 at 01:00 PM"
    # no now_phx: the fallback review_reply used to own alone
    assert llm.phx_stamp(None)
    seen = {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setitem(sys.modules, "anthropic",
                        _fake_anthropic(seen, {"verdict": "send", "reason": "ok"}))
    llm.review_reply("RENTER: hi", "body", "availability_ask")
    assert "at " in seen["messages"][0]["content"]
