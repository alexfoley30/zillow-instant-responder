#!/usr/bin/env python3
"""Both Haiku micro-calls share ONE transport (llm._json_call).

classify (_api_call) and the review gate (review_reply) used to carry their
own copy of the same request: same client options, same json_schema output
config, same first-text-block parse, same Phoenix "now" line. The copies had
already drifted - the review gate hand-rolled `utcnow() - 7h` while classify
took `now_phx` from its caller - so these lock in that there is one transport,
one timestamp format, and that each caller still owns its OWN fallback
(classify falls back to the regex parse, review fails open to "send").
"""

import json
import os
import sys
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm  # noqa: E402

NOW = datetime(2026, 10, 12, 5, 10)  # Monday, Phoenix


def _fake_anthropic(recorder, payload):
    class _Msgs:
        def create(self, **kw):
            recorder.append(kw)
            block = types.SimpleNamespace(type="text", text=json.dumps(payload))
            return types.SimpleNamespace(content=[block])

    class _Client:
        def __init__(self, **kw):
            recorder.append(kw)
            self.messages = _Msgs()

    return types.SimpleNamespace(Anthropic=_Client)


def test_now_line_matches_both_prompts():
    assert llm._now_line(NOW) == "Monday, October 12, 2026 at 05:10 AM"


def test_now_line_defaults_to_phoenix_utc_minus_7():
    """Arizona keeps no DST, so the no-argument default is exactly UTC-7."""
    utc_now = datetime.utcnow()
    line = llm._now_line()
    assert line in (
        (utc_now - timedelta(hours=7)).strftime(llm._NOW_FMT),
        # tolerate a minute rolling over mid-assert
        (utc_now - timedelta(hours=7, minutes=-1)).strftime(llm._NOW_FMT),
        (utc_now - timedelta(hours=7, minutes=1)).strftime(llm._NOW_FMT),
    )


def test_classify_and_review_use_the_same_transport(monkeypatch):
    """Same model pin, same client options, same json_schema wrapper for both,
    with only max_tokens and the schema differing."""
    calls = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setitem(sys.modules, "anthropic",
                        _fake_anthropic(calls, {"verdict": "send", "reason": "ok"}))

    llm.review_reply("RENTER: hi", "body", "booking_new", now_phx=NOW)
    review_client, review_req = calls[0], calls[1]

    calls.clear()
    monkeypatch.setitem(sys.modules, "anthropic",
                        _fake_anthropic(calls, {"intent": "question"}))
    llm._api_call("hi", "RENTER: hi", NOW)
    classify_client, classify_req = calls[0], calls[1]

    assert review_client == classify_client == {
        "api_key": "test", "timeout": 10.0, "max_retries": 1}
    assert review_req["model"] == classify_req["model"] == llm.MODEL
    for req in (review_req, classify_req):
        assert req["output_config"]["format"]["type"] == "json_schema"
        assert "Monday, October 12, 2026" in req["messages"][0]["content"]
    assert review_req["output_config"]["format"]["schema"] is llm.REVIEW_SCHEMA
    assert classify_req["output_config"]["format"]["schema"] is llm.SCHEMA
    assert (review_req["max_tokens"], classify_req["max_tokens"]) == (256, 1536)


def test_missing_key_keeps_each_caller_s_own_fallback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm._api_call("hi", "t", NOW) is None
    assert llm.review_reply("t", "b", "tpl") == {
        "verdict": "send", "reason": "review-unavailable"}


def test_transport_failure_keeps_each_caller_s_own_fallback(monkeypatch):
    """_json_call raises; classify drops to regex, review fails open."""
    def _boom(*_a, **_kw):
        raise RuntimeError("transport down")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(llm, "_json_call", _boom)
    assert llm._api_call("hi", "t", NOW) is None
    assert llm.review_reply("t", "b", "tpl")["reason"] == "review-error"
    # classify_reply still answers, from the regex fallback
    out = llm.classify_reply("I can't make it, please cancel", "", NOW)
    assert out["intent"] == "cancellation"
    assert out["_fallback"] is True
