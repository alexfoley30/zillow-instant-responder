"""Haiku micro-calls: classify a renter reply + extract time candidates.

The model EXTRACTS, it never DECIDES. Every booking decision runs through the
deterministic validators in rules.py / calendar_logic.py. On any LLM failure we
fall back to a conservative regex parse, and past that to NEEDS_HUMAN - the
pipeline never guesses.

Backup AI (2026-10-25, Alex: "do them all as a back up"): when the Claude call
errors or times out, the same prompt and the same JSON schema go to a second
provider over the OpenAI-compatible chat completions API (xAI Grok or OpenAI,
picked by env vars). Its output must pass the same schema check or it is
thrown away. With FALLBACK_LLM_API_KEY unset the backup is inert and every
path behaves exactly as before. Each answer is tagged with the provider that
wrote it (`_provider` on a classification, `provider` on a review).
"""

import json
import logging
import os
import re
import ssl
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import certifi

log = logging.getLogger("zillow-instant.llm")

# Same trust policy as gmail_client: verify against the pinned certifi bundle,
# not the OS store (2026-10-23 eMudhra compromise, see requirements.txt).
TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# Provider tags recorded with each answer.
PROVIDER_CLAUDE = "claude"
PROVIDER_REGEX = "regex"
FALLBACK_TIMEOUT_S = 15

# Dated snapshot pin (2026-08-30): the bare alias auto-follows whatever
# Anthropic promotes next, and this pipeline's pins are load-bearing (see the
# 8/24 Render outage). Bump deliberately, with the golden classify cases green.
MODEL = "claude-haiku-4-5-20251001"

INTENTS = ["accept_offer", "propose_time", "vague_time", "question",
           "negotiation", "modification_request", "cancellation",
           "benign_closer", "applied", "other"]

_TIME_ITEM = {
    "type": "object",
    "properties": {
        "date": {"type": ["string", "null"]},
        "time": {"type": ["string", "null"]},
        "after": {"type": ["string", "null"]},
        "before": {"type": ["string", "null"]},
        "raw": {"type": "string"},
    },
    "required": ["date", "time", "after", "before", "raw"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "time_candidates": {"type": "array", "items": _TIME_ITEM},
        "wants_same_day": {"type": "boolean"},
        "question_text": {"type": ["string", "null"]},
        "cancel_reason": {"type": ["string", "null"]},
        # Whole-conversation fields (2026-08-25, "give the rules ears"):
        # cumulative facts from the ENTIRE thread, not just this reply.
        "constraints": {"type": "array", "items": {"type": "string"}},
        "earliest_daily": {"type": ["string", "null"]},
        "latest_daily": {"type": ["string", "null"]},
        "declined_times": {"type": "array", "items": _TIME_ITEM},
    },
    "required": ["intent", "time_candidates", "wants_same_day", "question_text",
                 "cancel_reason", "constraints", "earliest_daily",
                 "latest_daily", "declined_times"],
    "additionalProperties": False,
}

PROMPT = """You are the reading brain for a rental-showing scheduling assistant. Read the ENTIRE conversation, then classify the renter's NEWEST message and extract the renter's cumulative scheduling picture.

Current date/time in Phoenix, Arizona: {now}

Full conversation so far (oldest first; US = the property manager, RENTER = the prospect). The renter's NEWEST message is the last RENTER entry:
---
{transcript}
---

The renter's NEWEST message (the one to classify):
---
{renter_text}
---

Return:
- intent, for the NEWEST message only, one of: accept_offer (agrees to the specific time we offered), propose_time (names their own specific day AND time), vague_time (wants a tour but no exact time: "this weekend", "afternoon"), question (asks about the property), negotiation (asks about lowering rent / making an offer), modification_request (asks us to change the home: turf, paint, appliances), cancellation (can't make a BOOKED showing / wants to cancel an appointment - NOT for declining interest), benign_closer (thanks / see you then / sounds good / politely bowing out or no longer interested), applied (says they submitted an application), other. A statement of preference like "tomorrow preferably" with no agreement to our specific offered time is NOT accept_offer.
- time_candidates: for each concrete date the renter proposes in the NEWEST message, date as YYYY-MM-DD (resolve "tomorrow"/"Friday" against Phoenix now, never a past date; a bare weekday name ALWAYS means the NEXT occurrence of that weekday, 1 to 7 days ahead - "Monday" said on a Sunday is tomorrow, never a week later), time as 24h HH:MM if they named an exact time else null, after/before as 24h HH:MM bounds ("after 3" -> after 15:00), raw = their words.
- wants_same_day: true when the newest message asks to see it today / ASAP.
- question_text: the property question in their words, else null.
- cancel_reason: if intent is cancellation and they STATED a reason, a short phrase of it; else null. Never invent one.
- constraints: short phrases for every standing scheduling constraint the renter has stated ANYWHERE in the conversation ("off work by 6pm most days", "Thursdays only after 6:30", "out of state until the 30th"). Empty list if none.
- earliest_daily / latest_daily: when the renter has stated a RECURRING daily bound on when they can show up ("I get off around 6pm" -> earliest_daily "18:00"; "mornings only, before noon" -> latest_daily "12:00"), as 24h HH:MM. Null when they never stated one. These describe the renter's general availability, not one specific day.
- declined_times: every specific slot WE offered anywhere in the conversation that the renter refused or brushed past by countering with a different time. Resolve each to date + 24h time (raw = their refusing words, e.g. "I cannot do 3:15pm"). Empty list if none.
If the newest message mixes intents (question + time), pick the intent that drives scheduling and still fill question_text."""


# ---------------------------------------------------------------- backup AI

def _fallback_config() -> dict | None:
    """The backup provider, or None when it is not configured (the default:
    no key means today's behavior, untouched)."""
    key = os.environ.get("FALLBACK_LLM_API_KEY", "").strip()
    if not key:
        return None
    base = os.environ.get("FALLBACK_LLM_BASE_URL", "").strip().rstrip("/")
    model = os.environ.get("FALLBACK_LLM_MODEL", "").strip()
    if not base or not model:
        log.error("backup AI has a key but no FALLBACK_LLM_BASE_URL or "
                  "FALLBACK_LLM_MODEL - backup disabled")
        return None
    return {"key": key, "base": base, "model": model,
            "effort": os.environ.get("FALLBACK_LLM_REASONING_EFFORT", "").strip()}


def fallback_provider_tag() -> str | None:
    cfg = _fallback_config()
    return f"fallback:{cfg['model']}" if cfg else None


_JSON_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict,
               "null": type(None)}


def matches_schema(value, schema: dict) -> bool:
    """Strict check of `value` against the subset of JSON Schema used by
    SCHEMA / REVIEW_SCHEMA (type, enum, properties, required,
    additionalProperties: false, items). Claude's output is held to these
    schemas by Anthropic's structured outputs; the backup's is held to them
    here, so a junk answer can never reach the booking logic."""
    types_ = schema.get("type")
    if types_ is not None:
        types_ = types_ if isinstance(types_, list) else [types_]
        if not any(isinstance(value, _JSON_TYPES[t]) and
                   not (t != "boolean" and isinstance(value, bool))
                   for t in types_):
            return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if any(k not in value for k in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and \
                any(k not in props for k in value):
            return False
        return all(matches_schema(value[k], props[k])
                   for k in value if k in props)
    if isinstance(value, list) and "items" in schema:
        return all(matches_schema(v, schema["items"]) for v in value)
    return True


def _fallback_json(prompt: str, schema: dict, schema_name: str,
                   purpose: str) -> dict | None:
    """Ask the backup provider. Returns the parsed answer only when it matches
    `schema`; None on no config, any error, or junk."""
    cfg = _fallback_config()
    if cfg is None:
        return None
    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": schema_name, "schema": schema, "strict": True}},
    }
    if cfg["effort"]:
        body["reasoning_effort"] = cfg["effort"]
    req = urllib.request.Request(
        f"{cfg['base']}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {cfg['key']}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=FALLBACK_TIMEOUT_S,
                                    context=TLS_CONTEXT) as r:
            data = json.loads(r.read().decode())
        text = data["choices"][0]["message"]["content"] or ""
        out = json.loads(text)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:300]
        except Exception:  # noqa: BLE001
            detail = b""
        log.error("backup AI %s HTTP %s (%s): %s", purpose, e.code,
                  cfg["model"], detail)
        return None
    except Exception as e:  # noqa: BLE001
        log.error("backup AI %s failed (%s): %s", purpose, cfg["model"], e)
        return None
    if not matches_schema(out, schema):
        log.error("backup AI %s returned output outside the schema (%s), "
                  "discarded", purpose, cfg["model"])
        return None
    return out


# Which provider wrote the review for the current request thread, so the send
# ledger can record it next to the template (send_stage pops it right after
# review_gate; ThreadingHTTPServer gives every request its own thread).
_review_seen = threading.local()


def pop_review_provider() -> str | None:
    p = getattr(_review_seen, "provider", None)
    _review_seen.provider = None
    return p


# ---------------------------------------------------------------- classify

def _classify_prompt(renter_text: str, transcript: str, now_phx: datetime) -> str:
    return PROMPT.format(
        now=now_phx.strftime("%A, %B %d, %Y at %I:%M %p"),
        transcript=(transcript or "(no earlier messages)")[:9000],
        renter_text=(renter_text or "")[:3000],
    )


def _api_call(renter_text: str, transcript: str, now_phx: datetime) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    prompt = _classify_prompt(renter_text, transcript, now_phx)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=10.0, max_retries=1)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1536,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        out = json.loads(text)
        out["_provider"] = PROVIDER_CLAUDE
        return out
    except Exception as e:  # noqa: BLE001
        log.error("Haiku classify failed: %s", e)
    backup = _fallback_json(prompt, SCHEMA, "renter_reply", "classify")
    if backup is None:
        log.error("classify: no AI answer, falling back to regex")
        return None
    backup["_provider"] = fallback_provider_tag()
    log.warning("classify answered by BACKUP AI (%s)", backup["_provider"])
    return backup


# ---------------------------------------------------------------- regex fallback

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
_CANCEL_RE = re.compile(r"(can'?t make|cancel|no longer|won'?t be able|resched)", re.IGNORECASE)
_ACCEPT_RE = re.compile(r"^(yes|yep|yeah|that works|sounds good|see you|perfect|ok|okay)\b", re.IGNORECASE)
_TODAY_RE = re.compile(r"\b(today|asap|right now|this afternoon|this evening|in an hour)\b", re.IGNORECASE)


def _regex_classify(renter_text: str, now_phx: datetime) -> dict:
    t = (renter_text or "").strip()
    low = t.lower()
    out = {"intent": "other", "time_candidates": [], "wants_same_day": bool(_TODAY_RE.search(low)),
           "question_text": None, "_fallback": True}
    if not t:
        return out
    if _CANCEL_RE.search(low):
        out["intent"] = "cancellation"
        return out
    # explicit day + time
    date = None
    if "tomorrow" in low:
        date = (now_phx + timedelta(days=1)).date()
    elif "today" in low:
        date = now_phx.date()
    else:
        for i, wd in enumerate(_WEEKDAYS):
            if wd in low or (wd[:3] + " ") in low or low.endswith(wd[:3]):
                ahead = (i - now_phx.weekday()) % 7
                if ahead == 0 and not _TIME_RE.search(low):
                    ahead = 7  # bare weekday name with no time = next week's
                date = (now_phx + timedelta(days=ahead)).date()
                break
    tm = _TIME_RE.search(low)
    if date and tm:
        hour = int(tm.group(1)) % 12 + (12 if tm.group(3).lower() == "pm" else 0)
        out["intent"] = "propose_time"
        out["time_candidates"] = [{
            "date": date.isoformat(),
            "time": f"{hour:02d}:{int(tm.group(2) or 0):02d}",
            "after": None, "before": None, "raw": t[:120],
        }]
        return out
    if _ACCEPT_RE.match(low) and len(t) < 80:
        out["intent"] = "accept_offer"
        return out
    if "?" in t:
        out["intent"] = "question"
        out["question_text"] = t[:400]
        return out
    return out


_CONVO_DEFAULTS = {"constraints": [], "earliest_daily": None,
                   "latest_daily": None, "declined_times": []}


def classify_reply(renter_text: str, last_alex: str, now_phx: datetime,
                   transcript: str = None) -> dict:
    """Haiku first, regex fallback. Result always matches SCHEMA's shape
    (plus `_fallback: True` when the regex path produced it, and
    `_provider`: "claude", "fallback:<model>" or "regex"). `transcript`
    is the full conversation (2026-08-25: the model reads the WHOLE thread,
    so constraints and declined slots survive across messages); callers
    that pass none get the legacy last-exchange context."""
    if transcript is None:
        transcript = (f"US: {last_alex or '(none)'}\n"
                      f"RENTER: {renter_text or ''}")
    result = _api_call(renter_text, transcript, now_phx)
    if result is None:
        result = _regex_classify(renter_text, now_phx)
        result["_provider"] = PROVIDER_REGEX
    log.info("classify answered by %s", result.get("_provider"))
    result.setdefault("time_candidates", [])
    result.setdefault("wants_same_day", False)
    result.setdefault("question_text", None)
    result.setdefault("cancel_reason", None)
    for k, v in _CONVO_DEFAULTS.items():
        result.setdefault(k, list(v) if isinstance(v, list) else v)
    if result.get("intent") not in INTENTS:
        result["intent"] = "other"
    return result


# ---------------------------------------------------------------- review gate

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["send", "block"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}

REVIEW_PROMPT = """You are the final proofreading gate for an automated rental-showing email assistant. Below is the full conversation with a renter, followed by the reply the system is ABOUT TO SEND.

Current date/time in Phoenix, Arizona: {now}. Resolve the renter's relative days against it ("this Wednesday" means the next Wednesday from today). If the reply confirms a day and time the renter proposed themselves, that is a match, not a contradiction.

Block the reply ONLY if it clearly does one of these:
1. Contradicts something the renter said (offers or confirms a time they said they cannot do, ignores a stated constraint like "only after 6pm", says "see you then" for a slot they declined).
2. Repeats a previous outgoing message nearly verbatim (the renter already received this exact ask or offer). EXCEPTION: a showing_reminder template is a scheduled reminder about an already-confirmed tour - restating the time, address and who is meeting them is its whole job, so never block it merely for overlapping the booking confirmation. Block a reminder only if the tour it names was cancelled or the renter already said they cannot make it.
3. Addresses the wrong person or wrong property, or answers a completely different question than the renter asked.

NOT a contradiction (never block for this): a move-in date, move-in window or lease start ("looking to move between October 28th and November 1st", "need a place by the 1st", "lease ends in December") says when the renter wants to LIVE there, not when they can TOUR. Renters tour days or weeks before they move in, so a tour date earlier than their move-in date is normal and correct. Only constraints about when they can visit (days, hours, "after work", "this Wednesday afternoon") count against a tour time. (Added 2026-10-12: a correct Wednesday booking was blocked because the renter's move-in window started two weeks later.)

Anything else sends. Imperfect but harmless replies send. When unsure, send. reason: one short sentence (used in an alert to the property manager when blocked).

Conversation (oldest first; US = us, RENTER = the prospect):
---
{transcript}
---

Reply about to be sent (template: {template}):
---
{outgoing}
---"""


def review_reply(transcript: str, outgoing_body: str, template: str,
                 now_phx: datetime = None) -> dict:
    """Second look before a renter-facing send. FAILS OPEN: any error means
    {"verdict": "send"} - the deterministic guards still stand, and a review
    outage must never stop the pipeline. When Claude errors (or answers
    outside the schema) the backup AI gets the same prompt; its verdict is
    used only if it matches REVIEW_SCHEMA. Both down = today's fail-open.
    The result carries `provider` (who wrote the verdict)."""
    out = _review(transcript, outgoing_body, template, now_phx)
    _review_seen.provider = out.get("provider")
    log.info("review %s answered by %s: %s", template, out.get("provider"),
             out.get("verdict"))
    return out


def _review(transcript, outgoing_body, template, now_phx) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"verdict": "send", "reason": "review-unavailable",
                "provider": None}
    prompt = REVIEW_PROMPT.format(
        now=(now_phx or datetime.utcnow() - timedelta(hours=7)
             ).strftime("%A, %B %d, %Y at %I:%M %p"),
        transcript=(transcript or "(none)")[:9000],
        template=template or "unknown",
        outgoing=(outgoing_body or "")[:4000],
    )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=10.0, max_retries=1)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=256,
            output_config={"format": {"type": "json_schema",
                                      "schema": REVIEW_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        out = json.loads(text)
        if out.get("verdict") in ("send", "block"):
            out["provider"] = PROVIDER_CLAUDE
            return out
        failed = {"verdict": "send", "reason": "review-malformed",
                  "provider": None}
    except Exception as e:  # noqa: BLE001
        log.error("review_reply failed: %s", e)
        failed = {"verdict": "send", "reason": "review-error", "provider": None}
    backup = _fallback_json(prompt, REVIEW_SCHEMA, "send_review", "review")
    if backup is None:
        log.error("review_reply: no AI answer (failing open, %s)",
                  failed["reason"])
        return failed
    backup["provider"] = fallback_provider_tag()
    log.warning("review answered by BACKUP AI (%s): %s",
                backup["provider"], backup["verdict"])
    return backup
