"""Haiku micro-calls: classify a renter reply + extract time candidates.

The model EXTRACTS, it never DECIDES. Every booking decision runs through the
deterministic validators in rules.py / calendar_logic.py. On any LLM failure we
fall back to a conservative regex parse, and past that to NEEDS_HUMAN - the
pipeline never guesses.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta

log = logging.getLogger("zillow-instant.llm")

MODEL = "claude-haiku-4-5"

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
- time_candidates: for each concrete date the renter proposes in the NEWEST message, date as YYYY-MM-DD (resolve "tomorrow"/"Friday" against Phoenix now, never a past date), time as 24h HH:MM if they named an exact time else null, after/before as 24h HH:MM bounds ("after 3" -> after 15:00), raw = their words.
- wants_same_day: true when the newest message asks to see it today / ASAP.
- question_text: the property question in their words, else null.
- cancel_reason: if intent is cancellation and they STATED a reason, a short phrase of it; else null. Never invent one.
- constraints: short phrases for every standing scheduling constraint the renter has stated ANYWHERE in the conversation ("off work by 6pm most days", "Thursdays only after 6:30", "out of state until the 30th"). Empty list if none.
- earliest_daily / latest_daily: when the renter has stated a RECURRING daily bound on when they can show up ("I get off around 6pm" -> earliest_daily "18:00"; "mornings only, before noon" -> latest_daily "12:00"), as 24h HH:MM. Null when they never stated one. These describe the renter's general availability, not one specific day.
- declined_times: every specific slot WE offered anywhere in the conversation that the renter refused or brushed past by countering with a different time. Resolve each to date + 24h time (raw = their refusing words, e.g. "I cannot do 3:15pm"). Empty list if none.
If the newest message mixes intents (question + time), pick the intent that drives scheduling and still fill question_text."""


def _api_call(renter_text: str, transcript: str, now_phx: datetime) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=10.0, max_retries=1)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1536,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": PROMPT.format(
                now=now_phx.strftime("%A, %B %d, %Y at %I:%M %p"),
                transcript=(transcript or "(no earlier messages)")[:9000],
                renter_text=(renter_text or "")[:3000],
            )}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)
    except Exception as e:  # noqa: BLE001
        log.error("Haiku classify failed, falling back to regex: %s", e)
        return None


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
    (plus `_fallback: True` when the regex path produced it). `transcript`
    is the full conversation (2026-08-25: the model reads the WHOLE thread,
    so constraints and declined slots survive across messages); callers
    that pass none get the legacy last-exchange context."""
    if transcript is None:
        transcript = (f"US: {last_alex or '(none)'}\n"
                      f"RENTER: {renter_text or ''}")
    result = _api_call(renter_text, transcript, now_phx)
    if result is None:
        result = _regex_classify(renter_text, now_phx)
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

Block the reply ONLY if it clearly does one of these:
1. Contradicts something the renter said (offers or confirms a time they said they cannot do, ignores a stated constraint like "only after 6pm", says "see you then" for a slot they declined).
2. Repeats a previous outgoing message nearly verbatim (the renter already received this exact ask or offer). EXCEPTION: a showing_reminder template is a scheduled reminder about an already-confirmed tour - restating the time, address and who is meeting them is its whole job, so never block it merely for overlapping the booking confirmation. Block a reminder only if the tour it names was cancelled or the renter already said they cannot make it.
3. Addresses the wrong person or wrong property, or answers a completely different question than the renter asked.

Anything else sends. Imperfect but harmless replies send. When unsure, send. reason: one short sentence (used in an alert to the property manager when blocked).

Conversation (oldest first; US = us, RENTER = the prospect):
---
{transcript}
---

Reply about to be sent (template: {template}):
---
{outgoing}
---"""


def review_reply(transcript: str, outgoing_body: str, template: str) -> dict:
    """Second look before a renter-facing send. FAILS OPEN: any error means
    {"verdict": "send"} - the deterministic guards still stand, and a review
    outage must never stop the pipeline."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"verdict": "send", "reason": "review-unavailable"}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=10.0, max_retries=1)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=256,
            output_config={"format": {"type": "json_schema",
                                      "schema": REVIEW_SCHEMA}},
            messages=[{"role": "user", "content": REVIEW_PROMPT.format(
                transcript=(transcript or "(none)")[:9000],
                template=template or "unknown",
                outgoing=(outgoing_body or "")[:4000],
            )}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        out = json.loads(text)
        if out.get("verdict") not in ("send", "block"):
            return {"verdict": "send", "reason": "review-malformed"}
        return out
    except Exception as e:  # noqa: BLE001
        log.error("review_reply failed (failing open): %s", e)
        return {"verdict": "send", "reason": "review-error"}
