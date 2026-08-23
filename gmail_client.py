"""Composio Gmail/Calendar wrapper + message parsing helpers.

All Gmail reads go through Composio GMAIL_FETCH_MESSAGE_BY_THREAD_ID — the
native Gmail MCP truncates/hides newest replies (memory:
zillow-search-truncation-gotcha), so Composio full-thread fetches are the only
authoritative read in this pipeline.
"""

import html as _html
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


_HTML_BLOCK_RE = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_HTML_BREAK_RE = re.compile(r"<br\s*/?>|</(p|div|tr|li|h[1-6]|table)\s*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LOOKS_HTML_RE = re.compile(r"<(br|div|p|table|html|a|span|td)\b|&(nbsp|#\d+|amp|quot);",
                            re.IGNORECASE)


def html_to_text(raw: str) -> str:
    """Flatten an HTML email body to plain text, preserving line structure.

    Block-level tags become newlines so the renter's paragraphs survive; every
    other tag is dropped. Entities are unescaped LAST so that a literal
    '&lt;br&gt;' in renter text can never turn into markup we then strip.
    """
    if not raw:
        return ""
    s = _HTML_BLOCK_RE.sub(" ", raw)
    s = _HTML_BREAK_RE.sub("\n", s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = _html.unescape(s)
    s = s.replace(" ", " ").replace("‌", "")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def msg_body(m: dict) -> str:
    """Plain text of a message. NEVER returns raw markup.

    Composio's GMAIL_FETCH_MESSAGE_BY_THREAD_ID puts RAW HTML in `messageText`
    and calls the plain-text snippet `preview.body` - not `snippet`, so the old
    fallback here never fired either. Handing that HTML to extract_renter_text
    made it return "" for EVERY renter message (verified 60/60 in the shadow
    ledger): the tag soup is one long line containing zillow.com hrefs, so the
    chrome filter broke on line 1. Haiku then classified 37 of 46 replies as
    `other` and escalated instead of answering. Flatten to text here so every
    caller downstream sees words.

    `preview.body` is only a ~200-char snippet, so it is a fallback, not the
    primary - a long renter message must not get silently truncated.
    """
    raw = m.get("messageText") or ""
    if raw:
        if _LOOKS_HTML_RE.search(raw):
            text = html_to_text(raw)
            if text:
                return text
        else:
            return raw
    prev = m.get("preview")
    if isinstance(prev, dict) and prev.get("body"):
        return html_to_text(prev["body"])
    return m.get("snippet") or ""


def is_from_alex(m: dict) -> bool:
    return ALEX_EMAIL in msg_sender(m)


def is_from_relay(m: dict) -> bool:
    return "convo.zillow.com" in msg_sender(m)


def message_by_id(msgs: list, message_id: str) -> dict | None:
    """The TRIGGERING message, not whoever spoke last (Melissa 2026-08-23:
    'I can do 2pm today!' and 'or anytime today' arrived 13 seconds apart;
    processing the first webhook against the LAST renter message swallowed
    the exact time entirely). Returns None for Alex-sent or unknown ids."""
    if not message_id:
        return None
    for m in msgs or []:
        if m.get("messageId") == message_id:
            return None if is_from_alex(m) else m
    return None


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
def _phrase(*words):
    r"""Join words with \s+ so a phrase still matches when html_to_text turned
    the space between them into a newline. Literal-space patterns silently
    stopped matching once msg_body started flattening block tags to \n, which
    let 'Reply to Anna Send application' leak into the renter text."""
    return r"\s+".join(words)


RENTER_SAYS_RE = re.compile(
    r"says:\s*(?P<msg>.+?)(?:" + "|".join([
        _phrase("Reply", "to", r"\w"),
        _phrase("Send", "application"),
        _phrase("About", "[A-Z]"),
        _phrase("You", "can", "also", "reply"),
    ]) + ")",
    re.IGNORECASE | re.DOTALL,
)
# Zillow chrome lines that survive a naive extract on reply emails. This list
# stays DELIBERATELY narrow: a hit here TRUNCATES the renter's message at that
# line, so anything a renter might plausibly type must not be in it. ("About
# <Name>" and "Credit score" are Zillow's profile block, but a renter writing
# "About Wednesday..." or "my credit score is 720" would lose the rest of their
# message - RENTER_SAYS_RE already terminates before that block, so they are
# not needed here.)
_CHROME_RE = re.compile("(" + "|".join([
    _phrase("Have", "questions", "or", "need", "help"),
    _phrase("Fair", "Housing", "Act"),
    r"zillow\.com",
    "unsubscribe",
    _phrase("This", "email", "was", "sent"),
    _phrase("Some", "rental", "inquiries", "may", "be", "scams"),
]) + ")", re.IGNORECASE)

# Reply-relay chrome markers. Renter text sits BEFORE these, and the plaintext
# body often arrives as ONE long line, so this must cut INLINE, not per-line.
# (Shadow soak 7/30-8/1: Monica's "6:30pm tomorrow is good" classified as
# intent=other because the extract carried 30 lines of this junk to Haiku.)
_CUT_RE = re.compile("(" + "|".join([
    _phrase("Brand", "logo"),
    _phrase("New", "message", "from", "a", "renter"),
    _phrase("Regarding", "your", "listing"),
    _phrase("Reply", "on", "Zillow"),
    _phrase("rental", "inquiries", "may", "be", "scams"),
    _phrase("Send", "application"),
    _phrase("You", "can", "also", "reply"),
    _phrase("Have", "questions", "or", "need", "help"),
    _phrase("Fair", "Housing"),
    "unsubscribe",
    _phrase("This", "email", "was", "sent"),
    _phrase("View", "listing"),
]) + ")", re.IGNORECASE)


_BODY_NAME_RE = re.compile(
    r"RENTER'?.?S NAME</div>\s*<div[^>]*>\s*([A-Za-z][A-Za-z'’. -]{0,40}?)\s*<",
    re.IGNORECASE | re.DOTALL)
_BODY_ADDR_RE = re.compile(
    r"Regarding your listing at:</div>\s*</td>\s*</tr>\s*<tr>\s*<td[^>]*>"
    r"\s*<div[^>]*>\s*([^<]{5,90}?)\s*<",
    re.IGNORECASE | re.DOTALL)
_BODY_ADDR_LOOSE_RE = re.compile(
    r"Regarding your listing at:.{0,400}?>\s*(\d{2,6}[^<]{5,80}?)\s*<",
    re.IGNORECASE | re.DOTALL)


def extract_identity_from_body(message: dict) -> tuple:
    """Fallback for email shapes whose SUBJECT loses the renter name/address
    (the "requesting to tour" family, 2026-08-22). Zillow's HTML body always
    carries a RENTER'S NAME block and a "Regarding your listing at:" block -
    pull both from the raw messageText. Returns (first_name|None, address|None)."""
    html = str(message.get("messageText") or "")
    if not html:
        return None, None
    name = None
    m = _BODY_NAME_RE.search(html)
    if m:
        name = m.group(1).strip().split()[0].strip(".,")
    addr = None
    m = _BODY_ADDR_RE.search(html) or _BODY_ADDR_LOOSE_RE.search(html)
    if m:
        addr = m.group(1).strip().rstrip(".")
    return (name or None), (addr or None)


def extract_renter_text(m: dict) -> str:
    """Best-effort extraction of the renter's own words from a relay message.
    First-inquiry format: text sits after '<Name> says:'. Reply format: text
    sits at the very top, before the relay chrome ('Brand logo New message
    from a renter ...') and any quoted history."""
    body = msg_body(m)
    hit = RENTER_SAYS_RE.search(body)
    if hit:
        text = hit.group("msg")
    else:
        cut = _CUT_RE.search(body)
        text = body[:cut.start()] if cut else body
    # Strip quoted history + leftover chrome lines.
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if lines:
                lines.append("")
            continue
        if s.startswith(">") or (s.startswith("On ") and s.endswith("wrote:")):
            break
        if _CHROME_RE.search(s):
            break
        lines.append(s)
        if len(lines) > 30:
            break
    return "\n".join(lines).strip()[:800]


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
