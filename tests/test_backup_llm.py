"""Backup AI (2026-10-25, Alex: "do them all as a back up").

When the Claude call errors, the renter-reply reader and the pre-send
proofread ask a second provider over the OpenAI-compatible chat completions
API. These tests run the REAL classify_reply / review_reply / send_stage with
only the network faked: Anthropic's client is swapped for one that raises,
and urllib's urlopen for a recorder that returns what a provider would.
"""

import io
import json
import os
import sys
import types
import urllib.error
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import llm  # noqa: E402
import responder  # noqa: E402

NOW = datetime(2026, 10, 26, 9, 0)  # Monday morning, Phoenix
RENTER = "Could I come see it Wednesday at 4pm?"
TRANSCRIPT = f"US: When works for you?\nRENTER: {RENTER}"

GOOD_CLASSIFY = {
    "intent": "propose_time",
    "time_candidates": [{"date": "2026-10-28", "time": "16:00", "after": None,
                         "before": None, "raw": "Wednesday at 4pm"}],
    "wants_same_day": False, "question_text": None, "cancel_reason": None,
    "constraints": ["off work at 3"], "earliest_daily": "15:00",
    "latest_daily": None, "declined_times": [],
}


# ------------------------------------------------------------------ fakes

def _anthropic(monkeypatch, behavior):
    """behavior: an Exception to raise, or a dict Claude 'returns'."""
    calls = []

    class _Msgs:
        def create(self, **kw):
            calls.append(kw)
            if isinstance(behavior, Exception):
                raise behavior
            block = types.SimpleNamespace(type="text", text=json.dumps(behavior))
            return types.SimpleNamespace(content=[block])

    class _Client:
        def __init__(self, **kw):
            self.messages = _Msgs()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(Anthropic=_Client))
    return calls


class _Resp:
    def __init__(self, payload: bytes):
        self._b = payload

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _provider(monkeypatch, content=None, http_error=None, raise_exc=None):
    """Fake the backup provider. `content` is the message content string."""
    seen = []

    def _urlopen(req, timeout=None, context=None):
        seen.append({"url": req.full_url, "body": json.loads(req.data),
                     "auth": req.get_header("Authorization"),
                     "context": context, "timeout": timeout})
        if http_error:
            raise urllib.error.HTTPError(req.full_url, http_error, "err", {},
                                         io.BytesIO(b'{"error":"x"}'))
        if raise_exc:
            raise raise_exc
        return _Resp(json.dumps(
            {"choices": [{"message": {"content": content}}]}).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", _urlopen)
    return seen


def _configure_backup(monkeypatch, model="grok-test"):
    monkeypatch.setenv("FALLBACK_LLM_BASE_URL", "https://api.x.ai/v1/")
    monkeypatch.setenv("FALLBACK_LLM_API_KEY", "xai-test")
    monkeypatch.setenv("FALLBACK_LLM_MODEL", model)
    monkeypatch.delenv("FALLBACK_LLM_REASONING_EFFORT", raising=False)


def _no_backup(monkeypatch):
    for k in ("FALLBACK_LLM_BASE_URL", "FALLBACK_LLM_API_KEY",
              "FALLBACK_LLM_MODEL", "FALLBACK_LLM_REASONING_EFFORT"):
        monkeypatch.delenv(k, raising=False)


def _regex_today(monkeypatch):
    """What classify_reply returned before the backup existed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _no_backup(monkeypatch)
    out = llm.classify_reply(RENTER, "", NOW, transcript=TRANSCRIPT)
    out.pop("_provider")
    return out


# --------------------------------------------------------------- classify

def test_claude_raising_uses_backup_with_same_prompt_and_schema(monkeypatch):
    claude = _anthropic(monkeypatch, TimeoutError("anthropic timed out"))
    _configure_backup(monkeypatch)
    seen = _provider(monkeypatch, content=json.dumps(GOOD_CLASSIFY))

    out = llm.classify_reply(RENTER, "", NOW, transcript=TRANSCRIPT)

    assert out["_provider"] == "fallback:grok-test"
    assert not out.get("_fallback")  # a full AI read, not the regex
    assert out["intent"] == "propose_time"
    assert out["earliest_daily"] == "15:00"
    assert len(seen) == 1
    call = seen[0]
    assert call["url"] == "https://api.x.ai/v1/chat/completions"
    assert call["auth"] == "Bearer xai-test"
    assert call["context"] is llm.TLS_CONTEXT  # pinned certifi bundle
    assert call["body"]["model"] == "grok-test"
    # same prompt Claude got, same schema, strict
    assert call["body"]["messages"][0]["content"] == \
        claude[0]["messages"][0]["content"]
    fmt = call["body"]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] == llm.SCHEMA
    assert fmt["json_schema"]["strict"] is True
    assert "reasoning_effort" not in call["body"]


def test_reasoning_effort_only_sent_when_set(monkeypatch):
    _anthropic(monkeypatch, RuntimeError("500"))
    _configure_backup(monkeypatch, model="gpt-test")
    monkeypatch.setenv("FALLBACK_LLM_REASONING_EFFORT", "none")
    seen = _provider(monkeypatch, content=json.dumps(GOOD_CLASSIFY))
    llm.classify_reply(RENTER, "", NOW, transcript=TRANSCRIPT)
    assert seen[0]["body"]["reasoning_effort"] == "none"


def test_backup_key_unset_is_exactly_todays_behavior(monkeypatch):
    expected = _regex_today(monkeypatch)
    _anthropic(monkeypatch, RuntimeError("anthropic down"))
    _no_backup(monkeypatch)
    seen = _provider(monkeypatch, content=json.dumps(GOOD_CLASSIFY))

    out = llm.classify_reply(RENTER, "", NOW, transcript=TRANSCRIPT)

    assert seen == []  # the backup is never called
    assert out["_provider"] == "regex"
    out.pop("_provider")
    assert out == expected
    assert out["_fallback"] is True


def test_key_without_model_stays_inert(monkeypatch):
    _anthropic(monkeypatch, RuntimeError("anthropic down"))
    _configure_backup(monkeypatch)
    monkeypatch.delenv("FALLBACK_LLM_MODEL")
    seen = _provider(monkeypatch, content=json.dumps(GOOD_CLASSIFY))
    out = llm.classify_reply(RENTER, "", NOW, transcript=TRANSCRIPT)
    assert seen == [] and out["_fallback"] is True


@pytest.mark.parametrize("junk", [
    "not json at all",
    json.dumps({**GOOD_CLASSIFY, "intent": "book_it_now"}),       # enum
    json.dumps({k: v for k, v in GOOD_CLASSIFY.items()
                if k != "declined_times"}),                       # missing
    json.dumps({**GOOD_CLASSIFY, "bonus": 1}),                    # extra key
    json.dumps({**GOOD_CLASSIFY, "wants_same_day": "yes"}),       # type
    json.dumps({**GOOD_CLASSIFY, "time_candidates": [{"date": "x"}]}),  # item
    json.dumps([GOOD_CLASSIFY]),                                  # not object
])
def test_backup_junk_falls_through_to_keyword_parser(monkeypatch, junk):
    expected = _regex_today(monkeypatch)
    _anthropic(monkeypatch, RuntimeError("anthropic down"))
    _configure_backup(monkeypatch)
    seen = _provider(monkeypatch, content=junk)

    out = llm.classify_reply(RENTER, "", NOW, transcript=TRANSCRIPT)

    assert len(seen) == 1
    assert out["_provider"] == "regex"
    out.pop("_provider")
    assert out == expected


@pytest.mark.parametrize("kw", [{"http_error": 503},
                                {"raise_exc": TimeoutError("slow")}])
def test_backup_down_falls_through_to_keyword_parser(monkeypatch, kw):
    _anthropic(monkeypatch, RuntimeError("anthropic down"))
    _configure_backup(monkeypatch)
    _provider(monkeypatch, **kw)
    out = llm.classify_reply(RENTER, "", NOW, transcript=TRANSCRIPT)
    assert out["_fallback"] is True and out["_provider"] == "regex"


def test_claude_answering_never_calls_backup(monkeypatch):
    _anthropic(monkeypatch, GOOD_CLASSIFY)
    _configure_backup(monkeypatch)
    seen = _provider(monkeypatch, content="should not be used")
    out = llm.classify_reply(RENTER, "", NOW, transcript=TRANSCRIPT)
    assert seen == []
    assert out["_provider"] == "claude" and out["intent"] == "propose_time"


def test_no_anthropic_key_keeps_todays_regex_path(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _configure_backup(monkeypatch)
    seen = _provider(monkeypatch, content=json.dumps(GOOD_CLASSIFY))
    out = llm.classify_reply(RENTER, "", NOW, transcript=TRANSCRIPT)
    assert seen == [] and out["_provider"] == "regex"


# ----------------------------------------------------------------- review

def test_review_claude_raising_backup_block_is_used(monkeypatch):
    claude = _anthropic(monkeypatch, RuntimeError("overloaded"))
    _configure_backup(monkeypatch)
    seen = _provider(monkeypatch, content=json.dumps(
        {"verdict": "block", "reason": "renter said they cannot do Wednesday"}))

    out = llm.review_reply(TRANSCRIPT, "See you Wednesday!", "booking_new",
                           now_phx=NOW)

    assert out["verdict"] == "block"
    assert out["provider"] == "fallback:grok-test"
    body = seen[0]["body"]
    assert body["messages"][0]["content"] == claude[0]["messages"][0]["content"]
    assert body["response_format"]["json_schema"]["schema"] == llm.REVIEW_SCHEMA


def test_review_backup_send_is_used(monkeypatch):
    _anthropic(monkeypatch, RuntimeError("overloaded"))
    _configure_backup(monkeypatch)
    _provider(monkeypatch, content=json.dumps({"verdict": "send", "reason": "ok"}))
    out = llm.review_reply(TRANSCRIPT, "x", "booking_new", now_phx=NOW)
    assert out == {"verdict": "send", "reason": "ok",
                   "provider": "fallback:grok-test"}


@pytest.mark.parametrize("kw", [
    {"content": json.dumps({"verdict": "maybe", "reason": "?"})},
    {"content": "<html>"},
    {"http_error": 500},
])
def test_review_both_down_fails_open_as_today(monkeypatch, kw):
    _anthropic(monkeypatch, RuntimeError("overloaded"))
    _configure_backup(monkeypatch)
    _provider(monkeypatch, **kw)
    out = llm.review_reply(TRANSCRIPT, "x", "booking_new", now_phx=NOW)
    assert out["verdict"] == "send" and out["reason"] == "review-error"
    assert out["provider"] is None


def test_review_backup_unset_is_todays_behavior(monkeypatch):
    _anthropic(monkeypatch, RuntimeError("overloaded"))
    _no_backup(monkeypatch)
    seen = _provider(monkeypatch, content="unused")
    out = llm.review_reply(TRANSCRIPT, "x", "booking_new", now_phx=NOW)
    assert seen == []
    assert (out["verdict"], out["reason"]) == ("send", "review-error")
    # Claude malformed, no backup: today's review-malformed, still sends
    _anthropic(monkeypatch, {"verdict": "maybe", "reason": "?"})
    out = llm.review_reply(TRANSCRIPT, "x", "booking_new", now_phx=NOW)
    assert (out["verdict"], out["reason"]) == ("send", "review-malformed")
    # no Anthropic key: today's review-unavailable, backup not consulted
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    _configure_backup(monkeypatch)
    out = llm.review_reply(TRANSCRIPT, "x", "booking_new", now_phx=NOW)
    assert seen == [] and out["reason"] == "review-unavailable"


# --------------------------------------------- tag lands on the send ledger

def _send_harness(monkeypatch):
    rec = {"sent": [], "failed": [], "escalated": []}
    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(responder.ledger, "reserve_send", lambda t, s, m: "acquired")
    monkeypatch.setattr(responder.ledger, "mark_sent",
                        lambda t, s, **kw: rec["sent"].append((s, kw)))
    monkeypatch.setattr(responder.ledger, "mark_failed",
                        lambda t, s, e: rec["failed"].append((s, e)))
    monkeypatch.setattr(responder.ledger, "upsert_thread", lambda *a, **k: None)
    monkeypatch.setattr(responder.ledger, "content_hash", lambda b: "h")
    monkeypatch.setattr(responder, "_escalate_review_block",
                        lambda *a: rec["escalated"].append(a))
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda tid: [])
    monkeypatch.setattr(responder.gm, "thread_transcript", lambda m: TRANSCRIPT)
    monkeypatch.setattr(responder.gm, "send_reply", lambda *a: None)
    monkeypatch.setattr(responder.gm, "modify_labels", lambda *a: None)
    return rec


def test_send_doc_records_backup_proofreader(monkeypatch):
    rec = _send_harness(monkeypatch)
    _anthropic(monkeypatch, RuntimeError("overloaded"))
    _configure_backup(monkeypatch)
    _provider(monkeypatch, content=json.dumps({"verdict": "send", "reason": "ok"}))

    r = responder.send_stage("t1", "reply__m1", "r@convo.zillow.com", "body",
                             [], [], {"template": "counter"}, "m1")

    assert r == "sent"
    assert rec["sent"] == [("reply__m1", {"review_provider": "fallback:grok-test"})]


def test_send_blocked_by_backup_escalates(monkeypatch):
    rec = _send_harness(monkeypatch)
    _anthropic(monkeypatch, RuntimeError("overloaded"))
    _configure_backup(monkeypatch)
    _provider(monkeypatch, content=json.dumps(
        {"verdict": "block", "reason": "contradicts renter"}))
    r = responder.send_stage("t1", "reply__m1", "r@convo.zillow.com", "body",
                             [], [], {"template": "counter"}, "m1")
    assert r == "no-send:review-blocked"
    assert rec["sent"] == [] and rec["escalated"]
    assert "contradicts renter" in rec["failed"][0][1]


def test_send_with_both_down_still_sends_untagged(monkeypatch):
    rec = _send_harness(monkeypatch)
    _anthropic(monkeypatch, RuntimeError("overloaded"))
    _no_backup(monkeypatch)
    r = responder.send_stage("t1", "reply__m1", "r@convo.zillow.com", "body",
                             [], [], {"template": "counter"}, "m1")
    assert r == "sent"
    assert rec["sent"] == [("reply__m1", {"review_provider": None})]


# ------------------------------------------------------------ schema check

def test_matches_schema_basics():
    assert llm.matches_schema(GOOD_CLASSIFY, llm.SCHEMA)
    assert llm.matches_schema({"verdict": "send", "reason": ""}, llm.REVIEW_SCHEMA)
    assert not llm.matches_schema({"verdict": "send"}, llm.REVIEW_SCHEMA)
    assert not llm.matches_schema({"verdict": "send", "reason": None},
                                  llm.REVIEW_SCHEMA)
    assert not llm.matches_schema({**GOOD_CLASSIFY, "earliest_daily": 15},
                                  llm.SCHEMA)
    assert not llm.matches_schema({**GOOD_CLASSIFY, "constraints": [True]},
                                  llm.SCHEMA)
