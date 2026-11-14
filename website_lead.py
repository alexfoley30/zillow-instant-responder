"""Website lead hook: Netlify Forms -> this service -> one Poke text to Alex.

Netlify's outgoing form webhook cannot send a custom header, so this route is
NOT behind the X-Webhook-Secret check. Instead the URL carries a token derived
from POKE_API_KEY (HMAC, one way): nothing new to configure on Render, and a
leaked URL can only cause a rate-limited nuisance ping, never recover the key.

    POST /hooks/website-lead/<token>   body = Netlify submission JSON

Added 2026-09-17 (Alex: "integrate poke into info@", scoped to website leads,
then "use Render" so no key has to be pasted into Netlify).
"""

import hashlib
import hmac
import logging
import os
import threading
import time
from collections import deque

log = logging.getLogger("zillow-instant.website-lead")

PATH_PREFIX = "/hooks/website-lead/"
TOKEN_CONTEXT = b"boundlessaz-website-lead-v1"
LEAD_FORMS = {"owner-lead": "owner lead", "contact": "website message"}
MAX_PER_HOUR = 30
REMEMBERED_IDS = 500

_lock = threading.Lock()
_recent_times = deque()
_seen_ids = deque()


def lead_token() -> str:
    """Token for the hook URL. Empty when the service has no Poke key, which
    disables the route entirely (nothing to ping with anyway)."""
    key = os.environ.get("POKE_API_KEY", "")
    if not key:
        return ""
    return hmac.new(key.encode(), TOKEN_CONTEXT, hashlib.sha256).hexdigest()[:40]


def path_matches(path: str) -> bool:
    if not path.startswith(PATH_PREFIX):
        return False
    expected = lead_token()
    if not expected:
        return False
    given = path[len(PATH_PREFIX):].strip("/")
    return hmac.compare_digest(given, expected)


def _clean(value, limit=300) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


def build_message(payload: dict):
    """Netlify submission JSON -> Poke text, or None for forms we do not ping."""
    form = payload.get("form_name", "")
    kind = LEAD_FORMS.get(form)
    if not kind:
        return None
    data = payload.get("data") or {}
    name = _clean(data.get("name"))
    phone = _clean(data.get("phone"))
    email = _clean(data.get("email"))
    lines = [f"Needs you: new {kind} from boundlessaz.com"]
    who = [name or "No name given", f"call/text {phone}" if phone else "", email]
    lines.append(", ".join(x for x in who if x))
    if form == "owner-lead":
        city = _clean(data.get("property-city")) or "City not given"
        count = _clean(data.get("house-count")) or "?"
        lines.append(f"{city}, {count} house(s)")
    if form == "contact" and data.get("topic"):
        lines.append(f"Topic: {_clean(data.get('topic'))}")
    message = _clean(data.get("message"), 400)
    if message:
        lines.append(f"Says: {message}")
    lines.append(f"Page: {_clean(data.get('source-page')) or form}")
    return "\n".join(lines)


def _admit(submission_id: str, now=None) -> str:
    """Rate limit and dedupe. Returns "" when the ping may go, else a reason."""
    now = time.time() if now is None else now
    with _lock:
        if submission_id:
            if submission_id in _seen_ids:
                return "duplicate"
            _seen_ids.append(submission_id)
            while len(_seen_ids) > REMEMBERED_IDS:
                _seen_ids.popleft()
        while _recent_times and now - _recent_times[0] > 3600:
            _recent_times.popleft()
        if len(_recent_times) >= MAX_PER_HOUR:
            return "rate-limited"
        _recent_times.append(now)
    return ""


def handle(payload: dict, ping) -> tuple:
    """Returns (http_code, text). `ping` is gmail_client.poke_ping or a stub."""
    if not isinstance(payload, dict):
        return 400, "bad payload"
    message = build_message(payload)
    if message is None:
        return 200, "ignored-form"
    reason = _admit(str(payload.get("id") or ""))
    if reason:
        log.warning("website lead not pinged (%s): %s", reason, message.splitlines()[1][:80])
        return (200 if reason == "duplicate" else 429), reason
    ok = ping(message)
    log.info("website lead %s: %s", "pinged" if ok else "PING FAILED", message.splitlines()[1][:80])
    return (200, "poked") if ok else (502, "poke-failed")


def reset_for_tests():
    with _lock:
        _recent_times.clear()
        _seen_ids.clear()
