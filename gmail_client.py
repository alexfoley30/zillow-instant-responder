"""Composio Gmail/Calendar wrapper + message parsing helpers.

All Gmail reads go through Composio GMAIL_FETCH_MESSAGE_BY_THREAD_ID — the
native Gmail MCP truncates/hides newest replies (memory:
zillow-search-truncation-gotcha), so Composio full-thread fetches are the only
authoritative read in this pipeline.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request

log = logging.getLogger("zillow-instant.gmail")

COMPOSIO_BASE = "https://backend.composio.dev/api/v3"
COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY", "")
CONNECTED_ACCOUNT_ID = os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID", "")
COMPOSIO_USER_ID = os.environ.get("COMPOSIO_USER_ID", "")

ALEX_EMAIL = "alex@azfoleyhomes.com"

POKE_ENDPOINT = os.environ.get("POKE_ENDPOINT", "")


def composio_execute(tool_slug: str, arguments: dict) -> dict:
    """Call a Composio tool via the v3 execute endpoint. Attaches the Gmail
    connected-account id only to GMAIL_* tools (calendar tools resolve via the
    user's default connection; attaching the Gmail ca_ id would 400)."""
    url = f"{COMPOSIO_BASE}/tools/execute/{tool_slug}"
    payload = {"user_id": COMPOSIO_USER_ID, "arguments": arguments}
    if CONNECTED_ACCOUNT_ID.startswith("ca_") and tool_slug.startswith("GMAIL_"):
        payload["connected_account_id"] = CONNECTED_ACCOUNT_ID
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"x-api-key": COMPOSIO_API_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.error("Composio %s HTTP %s: %s", tool_slug, e.code,
                  e.read().decode(errors="ignore")[:300])
        raise
    except Exception as e:
        log.error("Composio %s error: %s", tool_slug, e)
        raise


# ---------------------------------------------------------------- reads

def fetch_thread(thread_id: str) -> list:
    """Authoritative full-thread fetch. Returns the raw message list (may be [])."""
    res = composio_execute("GMAIL_FETCH_MESSAGE_BY_THREAD_ID", {"thread_id": thread_id})
    data = res.get("data", res)
    msgs = data.get("messages", []) if isinstance(data, dict) else []
    return [m for m in msgs if isinstance(m, dict)]


def msg_sender(m: dict) -> str:
    return (m.get("sender") or m.get("from") or "").lower()


def msg_id(m: dict) -> str:
    return m.get("messageId") or m.get("message_id") or m.get("id") or ""


def msg_body(m: dict) -> str:
    return m.get("messageText") or m.get("snippet") or ""


def is_from_alex(m: dict) -> bool:
    return ALEX_EMAIL in msg_sender(m)


def is_from_relay(m: dict) -> bool:
    return "convo.zillow.com" in msg_sender(m)


def last_renter_message(msgs: list) -> dict | None:
    """Newest message from the Zillow relay, or None."""
    for m in reversed(msgs):
        if is_from_relay(m):
            return m
    return None


def alex_replied_after(msgs: list, message_id: str) -> bool:
    """True if an Alex outbound appears AFTER the message with message_id in
    thread order. If the id isn't found, falls back to 'is the very last
    message from Alex' (conservative: True means do not send)."""
    idx = None
    for i, m in enumerate(msgs):
        if msg_id(m) == message_id:
            idx = i
            break
    if idx is None:
        return bool(msgs) and is_from_alex(msgs[-1])
    return any(is_from_alex(m) for m in msgs[idx + 1:])


# The renter's actual words sit between "<Name> says:" and the next Zillow
# chrome line. Everything else in the relay email is boilerplate that ALWAYS
# contains question marks, so any text analysis must run on this extract only.
RENTER_SAYS_RE = re.compile(
    r"says:\s*(?P<msg>.+?)(?:Reply to \w|Send application|About [A-Z]|You can also reply)",
    re.IGNORECASE | re.DOTALL,
)
# Zillow chrome lines that survive a naive extract on reply emails.
_CHROME_RE = re.compile(
    r"(Have questions or need help|Fair Housing|zillow\.com|unsubscribe|"
    r"This email was sent|View listing)", re.IGNORECASE)


def extract_renter_text(m: dict) -> str:
    """Best-effort extraction of the renter's own words from a relay message.
    Reply emails often carry the text at the top before quoted history."""
    body = msg_body(m)
    hit = RENTER_SAYS_RE.search(body)
    if hit:
        return hit.group("msg").strip()
    # Fallback: take lines before the first quoted-history marker or chrome.
    lines = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            if lines:
                lines.append("")
            continue
        if s.startswith(">") or s.startswith("On ") and s.endswith("wrote:"):
            break
        if _CHROME_RE.search(s):
            break
        lines.append(s)
        if len(lines) > 30:
            break
    return "\n".join(lines).strip()


RELAY_RE = re.compile(r"<([^>]+@[^>]+)>")


def relay_from_sender(sender: str) -> str:
    if not sender:
        return ""
    m = RELAY_RE.search(sender)
    if m:
        return m.group(1).strip()
    s = sender.strip()
    return s if "@" in s else ""


def relay_from_thread(msgs: list) -> str:
    m = last_renter_message(msgs)
    return relay_from_sender(m.get("sender") or m.get("from") or "") if m else ""


# ---------------------------------------------------------------- writes

def send_reply(thread_id: str, relay: str, body: str) -> dict:
    return composio_execute("GMAIL_REPLY_TO_THREAD", {
        "thread_id": thread_id,
        "message_body": body,
        "recipient_email": relay,
    })


def modify_labels(thread_id: str, add: list, remove: list) -> dict:
    return composio_execute("GMAIL_MODIFY_THREAD_LABELS", {
        "thread_id": thread_id,
        "add_label_ids": [l for l in add if l],
        "remove_label_ids": [l for l in remove if l],
    })


def poke_ping(message: str) -> bool:
    """One of the three sanctioned pings. No-op (False) if POKE_ENDPOINT unset."""
    if not POKE_ENDPOINT:
        log.warning("POKE_ENDPOINT not configured - ping skipped: %s", message[:80])
        return False
    try:
        req = urllib.request.Request(
            POKE_ENDPOINT,
            data=json.dumps({"message": message}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()).get("success", False)
    except Exception as e:  # noqa: BLE001
        log.error("poke_ping failed: %s", e)
        return False


# ---------------------------------------------------------------- webhook payload

def extract_event(payload: dict):
    """Pull thread_id / subject / sender / message_id out of a Composio Gmail
    trigger payload. Composio nests the message under data; be defensive."""
    d = payload.get("data", payload)
    thread_id = d.get("threadId") or d.get("thread_id")
    subject = d.get("subject")
    sender = d.get("sender") or d.get("from")
    message_id = d.get("messageId") or d.get("message_id") or d.get("id")
    if not thread_id and isinstance(d.get("message"), dict):
        m = d["message"]
        thread_id = m.get("threadId") or m.get("thread_id")
        subject = subject or m.get("subject")
        sender = sender or m.get("sender") or m.get("from")
        message_id = message_id or m.get("id")
    headers = d.get("payload", {}).get("headers", []) if isinstance(d.get("payload"), dict) else []
    for h in headers:
        nm = h.get("name", "").lower()
        if nm == "subject" and not subject:
            subject = h.get("value")
        elif nm == "from" and not sender:
            sender = h.get("value")
    return thread_id, subject, sender, message_id
