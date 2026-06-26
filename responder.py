#!/usr/bin/env python3
"""
Zillow Instant Responder — sub-30-second auto-reply to new Zillow rental inquiries.

Architecture (the "instant layer"):
  New Zillow email lands
    -> Composio Gmail trigger fires a webhook POST to THIS service
    -> we parse the inquirer's first name + property address from the subject
    -> send Template 1 (availability ask) in-thread via Composio
    -> apply the zillow/awaiting-renter label
  ...all in a few seconds, no LLM in the hot path.

The "smart layer" (booking, calendar, drive-time) stays on the existing 5-minute
sweep, which picks up the renter's reply from the zillow/awaiting-renter label.

This service is deliberately deterministic: no AI, no token cost, trivial to host.

Env vars required (set these in your host's dashboard, never hardcode):
  COMPOSIO_API_KEY        - your (rotated) Composio key, e.g. ck_xxx
  COMPOSIO_CONNECTED_ACCOUNT_ID - the Gmail connected-account id (gmail_boast-punnet)
  AWAITING_RENTER_LABEL_ID - Gmail label id for zillow/awaiting-renter (e.g. Label_NN)
  HANDLED_LABEL_ID        - Gmail label id for zillow/handled
  WEBHOOK_SECRET          - shared secret; Composio sends it, we verify it
  PORT                    - provided by the host (Render/Railway set this)
"""

import os
import re
import sys
import json
import logging
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zillow-instant")

COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY", "")
CONNECTED_ACCOUNT_ID = os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID", "")
COMPOSIO_USER_ID = os.environ.get("COMPOSIO_USER_ID", "")
AWAITING_LABEL = os.environ.get("AWAITING_RENTER_LABEL_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
PORT = int(os.environ.get("PORT", "8080"))

COMPOSIO_BASE = "https://backend.composio.dev/api/v3"

# Application link reused in the reply
APP_LINK = "https://www.arizonaeliteproperties.com/vacancies"

SIGNATURE = (
    "Alex Foley\n"
    "Arizona Elite Properties\n"
    "2425 S Stearman Dr, Suite #120\n"
    "Chandler, AZ 85286\n"
    "Phone: 480-815-9313\n"
    "Email: alex@azfoleyhomes.com"
)


def availability_ask(first_name: str, address: str) -> str:
    """Template 1 — Availability Ask, with the vacant-now line."""
    return (
        f"Hi {first_name},\n\n"
        f"Thanks for your interest in {address}! I'd love to get a showing scheduled for you.\n\n"
        "Good news — the home is vacant right now, so we can get you in to see it right away "
        "and you could move in quickly once you're approved.\n\n"
        "What days and times work best over the next week? Here are the windows I have "
        "available (all times Phoenix):\n\n"
        "   Monday through Friday, daytime\n"
        "   Saturday, late morning to early afternoon\n\n"
        "Send me two or three options and I'll lock one in. To get a jump on the paperwork, "
        f"you can start an application any time here:\n\n{APP_LINK}\n\n"
        "Looking forward to meeting you!\n\n"
        f"{SIGNATURE}"
    )


# Subject looks like: "Karen is requesting information about 22924 E Nightingale Rd, Queen Creek, AZ, 85142"
# or "Re: {FirstName} is requesting an application for {address}"
SUBJECT_RE = re.compile(
    r"^(?:Re:\s*)?(?P<name>[A-Za-z][\w'’.-]*)\s+is\s+requesting\s+"
    r"(?:information about|an application for)\s+(?P<address>.+?)\s*$",
    re.IGNORECASE,
)


def parse_subject(subject: str):
    """Return (first_name, address) or (None, None) if it doesn't match a Zillow inquiry."""
    if not subject:
        return None, None
    m = SUBJECT_RE.match(subject.strip())
    if not m:
        return None, None
    return m.group("name").strip(), m.group("address").strip()


def composio_execute(tool_slug: str, arguments: dict) -> dict:
    """Call a Composio tool via the v3 execute endpoint.
    Only send connected_account_id if it's a real account id (ca_...). A misconfigured
    env (e.g. the API key pasted in by mistake) would otherwise cause a 400
    ConnectedAccountNotFound; falling back to user_id lets Composio resolve the
    user's default Gmail connection."""
    url = f"{COMPOSIO_BASE}/tools/execute/{tool_slug}"
    payload = {"user_id": COMPOSIO_USER_ID, "arguments": arguments}
    if CONNECTED_ACCOUNT_ID.startswith("ca_"):
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
        log.error("Composio %s HTTP %s: %s", tool_slug, e.code, e.read().decode(errors="ignore")[:300])
        raise
    except Exception as e:
        log.error("Composio %s error: %s", tool_slug, e)
        raise


RELAY_RE = re.compile(r"<([^>]+@[^>]+)>")


def relay_from_sender(sender: str) -> str:
    """Extract the convo.zillow.com relay address from a 'Name <addr>' sender string."""
    if not sender:
        return ""
    m = RELAY_RE.search(sender)
    if m:
        return m.group(1).strip()
    s = sender.strip()
    return s if "@" in s else ""


def already_handled(thread_id: str) -> bool:
    """Idempotency: skip if Alex already replied in this thread.
    On a Composio error we DO NOT silently skip (that hid failures) — we log and
    return False so the reply is still attempted and any real error surfaces."""
    try:
        res = composio_execute("GMAIL_FETCH_MESSAGE_BY_THREAD_ID", {"thread_id": thread_id})
        data = res.get("data", res)
        msgs = data.get("messages", []) if isinstance(data, dict) else []
        for m in msgs:
            sender = (m.get("sender") or m.get("from") or "").lower()
            if "alex@azfoleyhomes.com" in sender:
                return True
    except Exception as e:
        log.error("already_handled fetch failed for %s, proceeding to reply: %s", thread_id, e)
        return False
    return False


def handle_inquiry(thread_id: str, subject: str, sender: str = "", message_id: str = None):
    first_name, address = parse_subject(subject)
    if not first_name:
        log.info("Subject not a Zillow inquiry, skipping: %r", subject)
        return "skipped-not-inquiry"

    relay = relay_from_sender(sender)
    if not relay:
        log.error("No relay address parsed from sender %r on thread %s", sender, thread_id)
        return "error-no-relay"

    if already_handled(thread_id):
        log.info("Thread %s already has an Alex reply, skipping", thread_id)
        return "skipped-already-handled"

    body = availability_ask(first_name, address)

    composio_execute("GMAIL_REPLY_TO_THREAD", {
        "thread_id": thread_id,
        "message_body": body,
        "recipient_email": relay,  # reply to the per-inquirer relay address
    })
    log.info("Sent availability-ask to %s (%s) re: %s", first_name, relay, address)

    if AWAITING_LABEL:
        composio_execute("GMAIL_MODIFY_THREAD_LABELS", {
            "thread_id": thread_id,
            "add_label_ids": [AWAITING_LABEL],
            "remove_label_ids": [],
        })
    return f"replied:{first_name}:{address}"


def extract_event(payload: dict):
    """Pull thread_id / subject / message_id out of a Composio Gmail trigger payload.
    Composio nests the message under data; be defensive about shape."""
    d = payload.get("data", payload)
    # Common shapes
    thread_id = d.get("threadId") or d.get("thread_id")
    subject = d.get("subject")
    sender = d.get("sender") or d.get("from")
    message_id = d.get("messageId") or d.get("message_id") or d.get("id")
    # Sometimes under d["message"] or d["payload"]
    if not thread_id and isinstance(d.get("message"), dict):
        m = d["message"]
        thread_id = m.get("threadId") or m.get("thread_id")
        subject = subject or m.get("subject")
        sender = sender or m.get("sender") or m.get("from")
        message_id = message_id or m.get("id")
    # Subject / From may live in headers
    headers = d.get("payload", {}).get("headers", []) if isinstance(d.get("payload"), dict) else []
    for h in headers:
        nm = h.get("name", "").lower()
        if nm == "subject" and not subject:
            subject = h.get("value")
        elif nm == "from" and not sender:
            sender = h.get("value")
    return thread_id, subject, sender, message_id


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, text):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        # Health check for the host
        self._send(200, "zillow-instant-responder ok")

    def do_POST(self):
        # Verify shared secret (header or query)
        secret = self.headers.get("X-Webhook-Secret", "")
        if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
            self._send(401, "bad secret")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw or b"{}")
        except Exception as e:
            self._send(400, f"bad json: {e}")
            return

        try:
            thread_id, subject, sender, message_id = extract_event(payload)
            if not thread_id:
                log.info("No thread_id in payload, ignoring")
                self._send(200, "ignored-no-thread")
                return
            result = handle_inquiry(thread_id, subject or "", sender or "", message_id)
            self._send(200, result)
        except Exception as e:
            log.exception("handler error")
            # 200 so Composio doesn't hammer retries; we logged it
            self._send(200, f"error-logged: {e}")

    def log_message(self, *args):
        pass  # quiet default access logs; we use our own logger


def main():
    missing = [k for k in ("COMPOSIO_API_KEY", "COMPOSIO_CONNECTED_ACCOUNT_ID") if not os.environ.get(k)]
    if missing:
        log.warning("Missing env vars: %s (service will start but calls will fail)", ", ".join(missing))
    log.info("Zillow instant responder listening on :%s", PORT)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
