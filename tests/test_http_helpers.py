"""_post_json: the one JSON-POST shape every outbound call in gmail_client
now goes through. These tests pin the three behaviours the four hand-rolled
copies used to encode separately, because nothing else in the suite reaches
this code - composio_execute is monkeypatched out everywhere else.
"""

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gmail_client as gm  # noqa: E402


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch, body: bytes):
    """Swap urlopen for a recorder. Returns the list of seen requests."""
    seen = []

    def _urlopen(req, timeout=None):
        seen.append({
            "url": req.full_url,
            "method": req.get_method(),
            "headers": {k.lower(): v for k, v in req.headers.items()},
            "payload": json.loads(req.data),
            "timeout": timeout,
        })
        return _FakeResponse(body)

    monkeypatch.setattr(gm.urllib.request, "urlopen", _urlopen)
    return seen


def test_post_json_always_sends_content_type_and_merges_headers(monkeypatch):
    seen = _capture(monkeypatch, b"{}")
    gm._post_json("https://example.test/x", {"a": 1}, {"X-Extra": "y"}, timeout=7)
    assert seen[0]["method"] == "POST"
    assert seen[0]["headers"]["content-type"] == "application/json"
    assert seen[0]["headers"]["x-extra"] == "y"
    assert seen[0]["payload"] == {"a": 1}
    assert seen[0]["timeout"] == 7


def test_composio_execute_carries_api_key_and_gmail_account(monkeypatch):
    monkeypatch.setattr(gm, "COMPOSIO_API_KEY", "key-123")
    monkeypatch.setattr(gm, "CONNECTED_ACCOUNT_ID", "ca_abc")
    monkeypatch.setattr(gm, "COMPOSIO_USER_ID", "user-1")
    seen = _capture(monkeypatch, json.dumps({"successful": True}).encode())

    assert gm.composio_execute("GMAIL_SEND_EMAIL", {"subject": "hi"}) == {
        "successful": True}
    assert seen[0]["headers"]["x-api-key"] == "key-123"
    assert seen[0]["payload"]["connected_account_id"] == "ca_abc"
    assert seen[0]["payload"]["arguments"] == {"subject": "hi"}
    assert seen[0]["timeout"] == 20


def test_composio_execute_omits_account_for_calendar_tools(monkeypatch):
    monkeypatch.setattr(gm, "CONNECTED_ACCOUNT_ID", "ca_abc")
    seen = _capture(monkeypatch, json.dumps({"successful": True}).encode())
    gm.composio_execute("GOOGLECALENDAR_CREATE_EVENT", {"summary": "x"})
    assert "connected_account_id" not in seen[0]["payload"]


def test_composio_execute_retries_send_updates_as_boolean(monkeypatch):
    monkeypatch.setattr(gm, "COMPOSIO_API_KEY", "key-123")
    monkeypatch.setattr(gm, "CONNECTED_ACCOUNT_ID", "")
    bodies = [
        json.dumps({"successful": False,
                    "error": "send_updates must be a boolean"}).encode(),
        json.dumps({"successful": True}).encode(),
    ]
    seen = []

    def _urlopen(req, timeout=None):
        seen.append(json.loads(req.data))
        return _FakeResponse(bodies[len(seen) - 1])

    monkeypatch.setattr(gm.urllib.request, "urlopen", _urlopen)

    out = gm.composio_execute("GOOGLECALENDAR_UPDATE_EVENT",
                              {"send_updates": "none"})
    assert out == {"successful": True}
    assert len(seen) == 2
    # The retry keeps the api-key path and coerces "none" -> False.
    assert seen[1]["arguments"]["send_updates"] is False


def test_poke_v2_treats_an_empty_200_as_delivered(monkeypatch):
    """Poke v2 answers with an empty body. Parsing it as JSON would raise and
    silently demote every ping to the legacy endpoint."""
    monkeypatch.setattr(gm, "POKE_API_KEY", "poke-key")
    seen = _capture(monkeypatch, b"")
    assert gm.poke_ping("hello") is True
    assert seen[0]["url"] == gm.POKE_V2_URL
    assert seen[0]["headers"]["authorization"] == "Bearer poke-key"
    assert seen[0]["timeout"] == 10


def test_poke_falls_back_to_legacy_and_reads_success(monkeypatch):
    monkeypatch.setattr(gm, "POKE_API_KEY", "")
    monkeypatch.setattr(gm, "POKE_ENDPOINT", "https://legacy.test/ping")
    seen = _capture(monkeypatch, json.dumps({"success": True}).encode())
    assert gm.poke_ping("hello") is True
    assert seen[0]["url"] == "https://legacy.test/ping"
    assert "authorization" not in seen[0]["headers"]


def test_poke_legacy_reports_failure_when_success_is_false(monkeypatch):
    monkeypatch.setattr(gm, "POKE_API_KEY", "")
    monkeypatch.setattr(gm, "POKE_ENDPOINT", "https://legacy.test/ping")
    _capture(monkeypatch, json.dumps({"success": False}).encode())
    assert gm.poke_ping("hello") is False


def test_poke_skips_entirely_with_no_endpoint_configured(monkeypatch):
    monkeypatch.setattr(gm, "POKE_API_KEY", "")
    monkeypatch.setattr(gm, "POKE_ENDPOINT", "")
    assert gm.poke_ping("hello") is False


def test_haiku_json_returns_none_for_a_non_object_body(monkeypatch):
    """A list body used to reach the callers and blow up on .get/.setdefault
    outside any try. The review gate must fail OPEN instead."""
    import llm

    class _Msgs:
        def create(self, **kw):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text="[1, 2]")])

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setitem(
        sys.modules, "anthropic",
        types.SimpleNamespace(Anthropic=lambda **kw: types.SimpleNamespace(
            messages=_Msgs())))
    assert llm._haiku_json("x", {}, {}, 10, "t") is None
    assert llm.review_reply("t", "body", "booking_new")["verdict"] == "send"
