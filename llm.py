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

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "time_candidates": {
            "type": "array",
            "items": {
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
            },
        },
        "wants_same_day": {"type": "boolean"},
        "question_text": {"type": ["string", "null"]},
        "cancel_reason": {"type": ["string", "null"]},
    },
    "required": ["intent", "time_candidates", "wants_same_day", "question_text",
                 "cancel_reason"],
    "additionalProperties": False,
}

PROMPT = """You are classifying a renter's email reply in a rental-showing scheduling thread.

If the intent is "cancellation" and the renter STATED a reason for canceling
(car trouble, illness, work, found another place, etc.), put a short phrase of
that reason in cancel_reason (e.g. "dead car battery"). Otherwise cancel_reason
is null. Never invent a reason.

Current date/time in Phoenix, Arizona: {now}
The last message WE sent them (for resolving "yes"/"that works"):
---
{last_alex}
---

The renter's reply:
---
{renter_text}
---

Classify the reply and extract any proposed showing times.
- intent one of: accept_offer (agrees to the specific time we offered), propose_time (names their own specific day AND time), vague_time (wants a tour but no exact time: "this weekend", "afternoon"), question (asks about the property), negotiation (asks about lowering rent / making an offer), modification_request (asks us to change the home: turf, paint, appliances), cancellation (can't make a BOOKED showing / wants to cancel an appointment - NOT for declining interest), benign_closer (thanks / see you then / sounds good / politely bowing out or no longer interested - "we'll only be here 3 months, thanks anyway" is benign_closer, nothing to act on), applied (says they submitted an application), other.
- time_candidates: for each concrete date the renter proposes, date as YYYY-MM-DD (resolve "tomorrow"/"Friday" against Phoenix now, never a past date), time as 24h HH:MM if they named an exact time else null, after/before as 24h HH:MM bounds when they say things like "after 3" or "before noon" (else null), raw = their words.
- wants_same_day: true when they ask to see it today / ASAP / "in an hour".
- question_text: the property question in their words, else null.
If the reply mixes intents (question + time), pick the intent that drives scheduling and still fill question_text."""


def _api_call(renter_text: str, last_alex: str, now_phx: datetime) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=8.0, max_retries=1)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": PROMPT.format(
                now=now_phx.strftime("%A, %B %d, %Y at %I:%M %p"),
                last_alex=(last_alex or "(none)")[:2000],
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


def classify_reply(renter_text: str, last_alex: str, now_phx: datetime) -> dict:
    """Haiku first, regex fallback. Result always matches SCHEMA's shape
    (plus `_fallback: True` when the regex path produced it)."""
    result = _api_call(renter_text, last_alex, now_phx)
    if result is None:
        return _regex_classify(renter_text, now_phx)
    result.setdefault("time_candidates", [])
    result.setdefault("wants_same_day", False)
    result.setdefault("question_text", None)
    if result.get("intent") not in INTENTS:
        result["intent"] = "other"
    return result
