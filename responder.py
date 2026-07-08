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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zillow-instant")

COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY", "")
CONNECTED_ACCOUNT_ID = os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID", "")
COMPOSIO_USER_ID = os.environ.get("COMPOSIO_USER_ID", "")
AWAITING_LABEL = os.environ.get("AWAITING_RENTER_LABEL_ID", "")
HANDLED_LABEL = os.environ.get("HANDLED_LABEL_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
PORT = int(os.environ.get("PORT", "8080"))

COMPOSIO_BASE = "https://backend.composio.dev/api/v3"

# Application link reused in the reply
APP_LINK = "https://www.arizonaeliteproperties.com/vacancies"

# Current-rentals page for leased/off-market redirects
FOR_RENT_LINK = "https://boundlessaz.com/for-rent.html"

# Leased / off-market properties. Inquiries matching one of these get the
# "leased" reply instead of an availability ask, and the thread is labeled
# zillow/handled (nothing for the sweep to book).
# KEEP IN SYNC with "LEASED / OFF-MARKET PROPERTIES" in
# ~/.claude/scheduled-tasks/zillow-inquiry-sweep/SKILL.md — remove an address
# only when Alex says it's back on the market.
BLOCKED_ADDRESSES = [
    "3309 E San Remo Ave",   # Gilbert, AZ 85234 — leased 2026-07-04
    "8743 E Palo Verde Dr",  # Scottsdale, AZ 85250 — leased 2026-07-08
]

# Matching mirrors the sweep: an inquiry is about a blocked house if its
# address contains BOTH the street NUMBER and the street-NAME core
# (directionals like E/W and street types like Ave/Rd stripped), case-insensitive.
_DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw",
                 "north", "south", "east", "west"}
_STREET_TYPES = {"ave", "avenue", "st", "street", "dr", "drive", "rd", "road",
                 "ln", "lane", "ct", "court", "blvd", "boulevard", "way",
                 "pl", "place", "cir", "circle", "trl", "trail", "pkwy",
                 "parkway", "loop", "ter", "terrace"}


def _number_and_core(address: str):
    """'3309 E San Remo Ave' -> ('3309', 'san remo'); (None, None) if unparseable."""
    tokens = re.findall(r"[a-z0-9']+", address.lower())
    if not tokens or not tokens[0].isdigit():
        return None, None
    core = [t for t in tokens[1:] if t not in _DIRECTIONALS and t not in _STREET_TYPES]
    return tokens[0], " ".join(core)


def is_blocked_address(address: str) -> bool:
    """True if the inquiry address matches a leased/off-market property."""
    a = address.lower()
    for blocked in BLOCKED_ADDRESSES:
        number, core = _number_and_core(blocked)
        if number and core and number in a and core in a:
            return True
    return False

SIGNATURE = (
    "Alex Foley\n"
    "Realtor & Property Manager\n"
    "Boundless Real Estate Arizona — Team Leader\n"
    "Powered by Arizona Elite Properties  |  License SA662452000\n"
    "480-815-9313  |  alex@azfoleyhomes.com  |  @alex.e.foley\n"
    "2425 S Stearman Dr, Suite 120, Chandler, AZ 85286"
)


def offer_existing(first_name: str, address: str, when_human: str) -> str:
    """Template OE — Offer Existing showing (CONSOLIDATE FIRST, Alex 2026-07-07:
    'we should be proposing our current bookings first'). The house already has a
    showing on the calendar, so the FIRST reply offers that exact time instead of
    an open-ended availability ask. If they can't make it, they tell us what works
    and the sweep schedules from there."""
    return (
        f"Hi {first_name},\n\n"
        f"Thanks for reaching out about {address}! Great timing, we actually have "
        f"a showing already lined up there on {when_human} (Arizona time). "
        "Any chance you could make that one? I can add you right in.\n\n"
        "If that time doesn't work, no problem at all. Just tell me what day and "
        "time works for you and I'll get you set up. Here's when I have open this "
        "week (Phoenix time):\n\n"
        "   Mon, Wed, Fri: 10:00 AM to 6:30 PM\n"
        "   Tue, Thu: 10:00 AM to 3:00 PM\n"
        "   Sat, Sun: 10:00 AM to 2:00 PM\n\n"
        "You can also start an application here whenever you're ready: "
        f"{APP_LINK}\n\n"
        "Looking forward to meeting you!\n\n"
        f"{SIGNATURE}"
    )


def availability_ask(first_name: str, address: str) -> str:
    """Template 1 — Availability Ask. Warm and casual: ask when they're looking to
    move (do NOT say the home is vacant), have them pick a specific time, soft-mention
    the application link. Exact-time booking: they pick a time, we lock that slot."""
    return (
        f"Hi {first_name},\n\n"
        f"Thanks for reaching out about {address}! I'd love to get you in to see it.\n\n"
        "When are you hoping to move, and what day and time works to come take a look? "
        "Here's when I have open this week (Phoenix time):\n\n"
        "   Mon, Wed, Fri: 10:00 AM to 6:30 PM\n"
        "   Tue, Thu: 10:00 AM to 3:00 PM\n"
        "   Sat, Sun: 10:00 AM to 2:00 PM\n\n"
        "Pick a time that works and I'll lock it in for you. You can also start an "
        f"application here whenever you're ready: {APP_LINK}\n\n"
        "Looking forward to meeting you!\n\n"
        f"{SIGNATURE}"
    )


def leased_reply(first_name: str, address: str) -> str:
    """Blocked-address reply — the home is leased; redirect to current rentals
    and invite them to share what they're looking for."""
    return (
        f"Hi {first_name},\n\n"
        f"Thanks for reaching out about {address}! I'm sorry to say that home has "
        "been leased and is no longer available.\n\n"
        "You can see everything we currently have for rent here: "
        f"{FOR_RENT_LINK}\n\n"
        "And if you tell me a little about what you're looking for (beds, area, "
        "budget, move-in date), I'm happy to point you toward anything that fits.\n\n"
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
    # CONNECTED_ACCOUNT_ID is the GMAIL account — attaching it to a calendar
    # tool would 400. Non-Gmail tools resolve via the user's default connection.
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
        log.error("Composio %s HTTP %s: %s", tool_slug, e.code, e.read().decode(errors="ignore")[:300])
        raise
    except Exception as e:
        log.error("Composio %s error: %s", tool_slug, e)
        raise


AZ_TZ = ZoneInfo("America/Phoenix")
SHOWING_MIN_LEAD_HOURS = 3  # don't offer a slot the renter can't realistically react to


def _fmt_showing_time(start_az: datetime) -> str:
    """'Wednesday, July 8, at 6:30 PM' (with today/tomorrow prefix when true)."""
    now_az = datetime.now(AZ_TZ)
    day = start_az.strftime("%A, %B %-d")
    if start_az.date() == now_az.date():
        day = f"today, {day}"
    elif start_az.date() == (now_az + timedelta(days=1)).date():
        day = f"tomorrow, {day}"
    t = start_az.strftime("%-I:%M %p")
    return f"{day}, at {t}"


def find_existing_showing(address: str):
    """CONSOLIDATE FIRST: look for an upcoming showing at THIS house on the
    calendar (next 7 days). Returns a human time string or None. Any failure
    returns None so the instant reply is never blocked."""
    # Street part only — the full inquiry address carries ", City, AZ, zip"
    # which would poison the street-name core.
    number, core = _number_and_core(address.split(",")[0])
    if not number or not core:
        return None
    try:
        now = datetime.now(timezone.utc)
        res = composio_execute("GOOGLECALENDAR_EVENTS_LIST", {
            "calendarId": "primary",
            "timeMin": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeMax": (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 50,
        })
        data = res.get("data", res)
        items = data.get("items") or data.get("events") or []
        if isinstance(data, dict) and not items and isinstance(data.get("event_data"), dict):
            items = data["event_data"].get("event_data", []) or []
        for ev in items:
            if not isinstance(ev, dict):
                continue
            hay = " ".join([
                str(ev.get("summary", "")),
                str(ev.get("location", "")),
                str(ev.get("description", "")),
            ]).lower()
            # Showing events only: same street number + street-name core.
            if number not in hay or core not in hay:
                continue
            if "showing" not in hay and "open house" not in hay:
                continue
            start_raw = (ev.get("start") or {}).get("dateTime")
            if not start_raw:
                continue  # all-day events aren't showings
            start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            if start < datetime.now(timezone.utc) + timedelta(hours=SHOWING_MIN_LEAD_HOURS):
                continue
            return _fmt_showing_time(start.astimezone(AZ_TZ))
    except Exception as e:
        log.error("find_existing_showing failed for %r (falling back to availability ask): %s", address, e)
    return None


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

    if is_blocked_address(address):
        body = leased_reply(first_name, address)
        composio_execute("GMAIL_REPLY_TO_THREAD", {
            "thread_id": thread_id,
            "message_body": body,
            "recipient_email": relay,
        })
        log.info("Sent leased reply to %s (%s) re: %s", first_name, relay, address)
        if HANDLED_LABEL:
            composio_execute("GMAIL_MODIFY_THREAD_LABELS", {
                "thread_id": thread_id,
                "add_label_ids": [HANDLED_LABEL],
                "remove_label_ids": [],
            })
        return f"replied-leased:{first_name}:{address}"

    # CONSOLIDATE FIRST (Alex 2026-07-07): if this house already has a showing
    # coming up, the first reply offers THAT exact time instead of the open ask.
    when_human = find_existing_showing(address)
    if when_human:
        body = offer_existing(first_name, address, when_human)
        sent_kind = "offer-existing"
    else:
        body = availability_ask(first_name, address)
        sent_kind = "availability-ask"

    composio_execute("GMAIL_REPLY_TO_THREAD", {
        "thread_id": thread_id,
        "message_body": body,
        "recipient_email": relay,  # reply to the per-inquirer relay address
    })
    log.info("Sent %s to %s (%s) re: %s", sent_kind, first_name, relay, address)

    if AWAITING_LABEL:
        composio_execute("GMAIL_MODIFY_THREAD_LABELS", {
            "thread_id": thread_id,
            "add_label_ids": [AWAITING_LABEL],
            "remove_label_ids": [],
        })
    return f"replied-{sent_kind}:{first_name}:{address}"


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
