#!/usr/bin/env python3
"""
Zillow pipeline — SINGLE-WRITER service. v2 (2026-07-28 rebuild).

This service owns EVERY renter-facing send: instant first replies, question
handling, time parsing, booking, confirmations, counters, nudges. The Claude
sweep is a non-sending watchdog that POSTs /reprocess when it finds drift.

Why: duplicates kept recurring because two writers (this service + the sweep)
each re-derived "should I reply?" from non-transactional Gmail state. Now every
send must first win a Firestore create-once lock (ledger.reserve_send); losing
the create means someone else already handled it. No lock, no send. Ever.

Flow per inbound message:
  secret -> extract -> self-send skip -> claim_message (dup delivery gate)
  -> Composio full-thread fetch -> claim_content_hash (Zillow dup-copy gate)
  -> alex_replied_after gate -> route:
       new inquiry  : leased? -> leased reply | consolidate-first -> OE/T1
       known thread : classify (Haiku extracts, code decides) ->
                      fold/book/counter/answer/apply-first/as-is/NEEDS_HUMAN
       legacy thread: reconstruct minimal state, then treat as known
DRY_RUN=true writes zillow_shadow docs instead of sending/labeling/booking.

Env (Render dashboard):
  COMPOSIO_API_KEY, COMPOSIO_CONNECTED_ACCOUNT_ID, COMPOSIO_USER_ID
  AWAITING_RENTER_LABEL_ID, HANDLED_LABEL_ID, NEEDS_REPLY_LABEL_ID
  WEBHOOK_SECRET, PORT
  GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/firebase-sa.json
  ANTHROPIC_API_KEY, POKE_ENDPOINT, DRY_RUN ("true"/"false")
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import calendar_logic as cal
import cron
import facts
import gmail_client as gm
import ledger
import llm
import rules
import templates as T

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zillow-instant")

AZ_TZ = ZoneInfo("America/Phoenix")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
# Auth for the watchdog's /reprocess endpoint (separate from the Composio
# webhook secret so the two can rotate independently).
REPROCESS_SECRET = os.environ.get("REPROCESS_SECRET", "") or WEBHOOK_SECRET
AWAITING_LABEL = os.environ.get("AWAITING_RENTER_LABEL_ID", "")
HANDLED_LABEL = os.environ.get("HANDLED_LABEL_ID", "")
NEEDS_REPLY_LABEL = os.environ.get("NEEDS_REPLY_LABEL_ID", "Label_2252577853408309931")


def dry_run() -> bool:
    return os.environ.get("DRY_RUN", "true").strip().lower() != "false"


SUBJECT_RE = re.compile(
    r"^(?:Re:\s*)?(?P<name>[A-Za-z][\w'’.-]*)\s+is\s+requesting\s+"
    r"(?:information about|an application for|(?:to|a) tour(?: of)?)\s+"
    r"(?P<address>.+?)\s*$",
    re.IGNORECASE,
)


def parse_subject(subject: str):
    if not subject:
        return None, None
    m = SUBJECT_RE.match(subject.strip())
    if not m:
        return None, None
    return m.group("name").strip(), m.group("address").strip()


# ---------------------------------------------------------------- send core

def send_stage(thread_id: str, stage_key: str, relay: str, body: str,
               labels_add: list, labels_remove: list, meta: dict,
               trigger_message_id: str = "") -> str:
    """The ONLY function that sends renter email. Wins the Firestore lock or
    does nothing. Returns 'sent' | 'shadowed' | 'skipped:<why>'."""
    meta = dict(meta)
    meta.update({
        "template": meta.get("template", stage_key),
        "body_sha256": ledger.content_hash(body),
        "to_relay": relay,
        "trigger_message_id": trigger_message_id,
    })

    if dry_run():
        ledger.write_shadow(thread_id, stage_key, {
            "would_body": body, "would_labels_add": labels_add,
            "would_labels_remove": labels_remove, **meta,
        })
        log.info("SHADOW %s %s", thread_id, stage_key)
        return "shadowed"

    verdict = ledger.reserve_send(thread_id, stage_key, meta)
    if verdict in ("already-sent", "in-flight"):
        log.info("send_stage skip (%s): %s %s", verdict, thread_id, stage_key)
        return f"skipped:{verdict}"
    if verdict == "recover":
        # Ambiguity resolves against the source of truth: the thread itself.
        msgs = gm.fetch_thread(thread_id)
        if gm.alex_replied_after(msgs, trigger_message_id):
            ledger.mark_sent(thread_id, stage_key, recovered="backfilled-from-gmail")
            return "skipped:recovered-already-sent"
        if not ledger.takeover_send(thread_id, stage_key):
            return "skipped:takeover-lost"

    try:
        gm.send_reply(thread_id, relay, body)
    except Exception as e:  # noqa: BLE001
        ledger.mark_failed(thread_id, stage_key, str(e))
        log.error("send failed %s %s: %s", thread_id, stage_key, e)
        return "skipped:send-failed"

    ledger.mark_sent(thread_id, stage_key)

    try:
        if labels_add or labels_remove:
            gm.modify_labels(thread_id, labels_add, labels_remove)
    except Exception as e:  # noqa: BLE001 - label failure NEVER triggers a resend
        log.error("label failed %s %s (queued for retry): %s", thread_id, stage_key, e)
        ledger.set_labels_pending(thread_id, stage_key, labels_add, labels_remove)

    return "sent"


def needs_human(thread_id: str, first_name: str, address: str, last_line: str):
    """Escalate: state + rate-limited needs-you Poke (1/day/thread)."""
    doc = ledger.get_thread(thread_id) or {}
    last_ping = doc.get("last_needs_human_ping_at")
    ledger.transition(thread_id, ledger.NEEDS_HUMAN)
    if last_ping and (datetime.now(AZ_TZ).astimezone(last_ping.tzinfo) - last_ping) < timedelta(days=1):
        return
    msg = (f'Needs you: {first_name} — {address.split(",")[0]} — '
           f'"{(last_line or "")[:80]}". Reply with your answer and I will '
           f'send it to the renter. [zt:{thread_id}]')
    if dry_run():
        ledger.write_shadow(thread_id, f"poke__{ledger.content_hash(msg)}",
                            {"would_poke": msg})
    else:
        gm.poke_ping(msg)
        ledger.upsert_thread(thread_id, last_needs_human_ping_at=datetime.now(AZ_TZ))


# ---------------------------------------------------------------- new inquiry

def handle_new_inquiry(thread_id: str, first_name: str, address: str,
                       relay: str, renter_text: str, message_id: str) -> str:
    if rules.is_blocked_address(address, ledger.blocked_addresses()):
        ledger.create_thread(thread_id, state=ledger.LEASED, renter_name=first_name,
                             property_address=address, relay_email=relay)
        return send_stage(thread_id, "leased", relay,
                          T.leased_reply(first_name, address),
                          [HANDLED_LABEL], [], {"template": "leased"}, message_id)

    # ONE send per inquiry, always (shadow soak 7/30-8/1 caught the double:
    # first_reply + a separate standing-rule email 68ms apart on 8 threads).
    # A matched standing rule answers INLINE in the first reply; an unmatched
    # question gets the ack line INLINE + a needs-you escalation. URL query
    # strings carry "?" so strip links before question detection.
    clean = re.sub(r"https?://\S+", "", renter_text or "")
    has_q = "?" in clean
    rule = facts.standing_rule_for(renter_text) if has_q else None
    answer = facts.ANSWER_LINES.get(rule) if rule else None

    existing = None
    try:
        existing = cal.find_existing_showing(address)
    except Exception as e:  # noqa: BLE001 - never block the instant reply
        log.error("consolidate-first lookup failed: %s", e)

    ack = has_q and not answer
    if existing:
        body = T.offer_existing(first_name, address, existing["when_human"],
                                ack=ack, answer=answer)
        state, template = ledger.OFFERED, "offer_existing"
        offered_iso = existing["start_az"].isoformat()
        offered_event = existing["event"].get("id", "")
    else:
        body = T.availability_ask(first_name, address, ack=ack, answer=answer)
        state, template = ledger.ACKED, "availability_ask"
        offered_iso, offered_event = None, ""

    if answer:
        template += f"+{rule}"

    ledger.create_thread(
        thread_id, state=state, renter_name=first_name, property_address=address,
        relay_email=relay, has_open_question=ack,
        offered_start_iso=offered_iso, offered_event_id=offered_event)

    labels = [AWAITING_LABEL] + ([NEEDS_REPLY_LABEL] if ack else [])
    result = send_stage(thread_id, "first_reply", relay, body, labels, [],
                        {"template": template}, message_id)
    if ack and result in ("sent", "shadowed"):
        # We promised a follow-up in the ack line - Alex must supply it.
        needs_human(thread_id, first_name, address, renter_text[:120])
    return result


# ---------------------------------------------------------------- questions

def route_question(thread_id: str, first_name: str, address: str, relay: str,
                   question_text: str, message_id: str) -> str:
    rule = facts.standing_rule_for(question_text)
    body = None
    if rule == "floor_plan":
        body = T.floor_plan_reply(first_name)
    elif rule == "as_is":
        body = T.as_is_reply(first_name)
    elif rule == "appliances":
        body = T.fact_answer(first_name, facts.APPLIANCES_LINE)
    elif rule == "requirements":
        body = T.fact_answer(first_name, facts.REQUIREMENTS_LINE)
    elif rule == "virtual_tour":
        body = T.fact_answer(first_name, facts.VIRTUAL_TOUR_LINE)
    elif rule == "fair_housing":
        body = T.fair_housing_steer(first_name)
    elif rule == "apply_first":
        return handle_negotiation(thread_id, first_name, relay, message_id)

    if body:
        return send_stage(thread_id, f"reply__{message_id}", relay, body,
                          [AWAITING_LABEL], [NEEDS_REPLY_LABEL],
                          {"template": f"standing:{rule}"}, message_id)

    # No standing rule: send NOTHING. The old "Great question! let me check"
    # ack meant every unknown question cost the renter two emails (Alex,
    # 8/5: "follow-up slop"). Escalate silently; Alex answers once via Poke
    # (/send-approved) or Gmail, and that one real answer is the whole thread.
    try:
        gm.modify_labels(thread_id, [NEEDS_REPLY_LABEL], [])
    except Exception as e:  # noqa: BLE001
        log.error("needs-reply label failed: %s", e)
    needs_human(thread_id, first_name, address, question_text)
    return "no-send:escalated"


def handle_negotiation(thread_id: str, first_name: str, relay: str,
                       message_id: str) -> str:
    doc = ledger.get_thread(thread_id) or {}
    if doc.get("negotiation_acked"):
        log.info("negotiation already acked on %s - silent", thread_id)
        return "skipped:one-ack-ever"
    result = send_stage(thread_id, "negotiation_ack", relay,
                        T.apply_first(first_name), [AWAITING_LABEL], [],
                        {"template": "apply_first"}, message_id)
    if result in ("sent", "shadowed"):
        ledger.upsert_thread(thread_id, negotiation_acked=True)
    return result


# ---------------------------------------------------------------- reply path

def handle_reply(thread_id: str, doc: dict, msgs: list, renter_text: str,
                 message_id: str) -> str:
    first_name = doc.get("renter_name") or "there"
    address = doc.get("property_address") or ""
    relay = gm.relay_from_thread(msgs) or doc.get("relay_email") or ""
    if not relay:
        return "error-no-relay"
    if doc.get("alex_owned"):
        return handle_alex_owned_reply(thread_id, doc, msgs, renter_text,
                                       message_id)

    last_alex = next((gm.msg_body(m) for m in reversed(msgs) if gm.is_from_alex(m)), "")
    now_az = datetime.now(AZ_TZ)
    cls = rules.snap_weekday_dates(
        llm.classify_reply(renter_text, last_alex, now_az), now_az)
    ledger.record_message_outcome(message_id, "classified", cls)
    intent = cls["intent"]
    log.info("reply %s intent=%s%s", thread_id, intent,
             " (regex-fallback)" if cls.get("_fallback") else "")

    state = doc.get("state", ledger.AWAITING_TIME)

    if intent == "cancellation":
        # "Cancellation" only means something when a showing exists. Without a
        # booking it is a decline ("will only be there 3 mos - thnx"), and the
        # reschedule ask reads tone-deaf - first live send 2026-08-04 (Kathy).
        if not doc.get("event_id") and state != ledger.BOOKED:
            try:
                gm.modify_labels(thread_id, [HANDLED_LABEL], [AWAITING_LABEL])
            except Exception as e:  # noqa: BLE001
                log.error("decline relabel failed: %s", e)
            ledger.transition(thread_id, ledger.CLOSED)
            return "no-send:decline"
        return handle_cancellation(thread_id, doc, first_name, relay,
                                   message_id, cls=cls, address=address)

    if state == ledger.LEASED:
        return "skipped:leased-thread"

    if intent == "benign_closer":
        try:
            if not dry_run():
                gm.modify_labels(thread_id, [HANDLED_LABEL], [AWAITING_LABEL])
        except Exception as e:  # noqa: BLE001
            log.error("benign relabel failed: %s", e)
        return "no-send:benign"

    if intent == "negotiation":
        return handle_negotiation(thread_id, first_name, relay, message_id)

    if intent == "modification_request":
        return send_stage(thread_id, f"reply__{message_id}", relay,
                          T.as_is_reply(first_name), [AWAITING_LABEL], [],
                          {"template": "as_is"}, message_id)

    if intent == "question":
        return route_question(thread_id, first_name, address, relay,
                              cls.get("question_text") or renter_text, message_id)

    if intent == "applied":
        needs_human(thread_id, first_name, address, "submitted an application")
        return "no-send:applied"

    if intent == "accept_offer":
        return book_accepted_offer(thread_id, doc, first_name, address, relay, message_id)

    if intent == "propose_time":
        return book_proposed_time(thread_id, doc, first_name, address, relay,
                                  cls, message_id, now_az)

    if intent == "vague_time":
        # CONSOLIDATE FIRST applies to vague replies too (2026-08-22, Jamie/
        # Redfield: "Are you available today?" got windows-ask while two
        # same-day showings sat on the calendar). Offer existing exact times;
        # windows-ask only when the calendar has nothing to offer.
        slots = []
        try:
            slots = cal.find_existing_showings(address) if address else []
        except Exception as e:  # noqa: BLE001
            log.error("vague-time consolidate lookup failed for %s: %s",
                      thread_id, e)
        if slots:
            when_h = slots[0]["when_human"]
            if len(slots) > 1:
                second = slots[1]
                if second["start_az"].date() == slots[0]["start_az"].date():
                    when_h += f" or at {second['start_az'].strftime('%-I:%M %p')}"
                else:
                    when_h += f" or {second['when_human']}"
            return send_stage(thread_id, f"reply__{message_id}", relay,
                              T.offer_existing_reply(first_name, when_h),
                              [AWAITING_LABEL], [],
                              {"template": "offer_existing_reply"}, message_id)
        return send_stage(thread_id, f"reply__{message_id}", relay,
                          T.windows_ask(first_name), [AWAITING_LABEL], [],
                          {"template": "windows_ask"}, message_id)

    # other / unparseable: fail safe, never guess.
    needs_human(thread_id, first_name, address, renter_text[:120])
    return "no-send:needs-human"


def book_accepted_offer(thread_id, doc, first_name, address, relay, message_id) -> str:
    """Renter said yes to the offered existing slot -> fold in + confirm."""
    existing = None
    try:
        existing = cal.find_existing_showing(address)
    except Exception as e:  # noqa: BLE001
        log.error("accept_offer lookup failed: %s", e)
    if not existing:
        # The renter accepted SOMETHING we can't see - usually a time Alex
        # negotiated by hand with no calendar event yet (Natalie + Yesenia,
        # 8/5: both got "what day works?" right after saying yes). Asking
        # again reads clueless; a human knows what was agreed.
        needs_human(thread_id, first_name, address,
                    "accepted a time but no matching calendar event - book it")
        return "no-send:accept-without-event"

    event_id = existing["event"].get("id", "")
    stage = f"booked__{event_id}"
    if dry_run():
        ledger.write_shadow(thread_id, stage, {
            "would_calendar": {"fold": True, "event_id": event_id,
                               "renter": first_name},
            "would_body": T.booking_confirmation(
                first_name, address, existing["when_human"], "Alex Foley"),
        })
        return "shadowed"

    verdict = ledger.reserve_send(thread_id, stage, {"template": "booking_fold"})
    if verdict in ("already-sent", "in-flight"):
        return f"skipped:{verdict}"
    if verdict == "recover":
        msgs = gm.fetch_thread(thread_id)
        if gm.alex_replied_after(msgs, message_id):
            ledger.mark_sent(thread_id, stage, recovered="backfilled")
            return "skipped:recovered"
        if not ledger.takeover_send(thread_id, stage):
            return "skipped:takeover-lost"

    try:
        cal.fold_renter_into_event(existing["event"], first_name)  # event FIRST
        gm.send_reply(thread_id, relay, T.booking_confirmation(
            first_name, address, existing["when_human"], "Alex Foley"))
    except Exception as e:  # noqa: BLE001
        ledger.mark_failed(thread_id, stage, str(e))
        return "skipped:book-failed"
    ledger.mark_sent(thread_id, stage, event_id=event_id)
    try:
        gm.modify_labels(thread_id, [HANDLED_LABEL], [AWAITING_LABEL])
    except Exception as e:  # noqa: BLE001
        ledger.set_labels_pending(thread_id, stage, [HANDLED_LABEL], [AWAITING_LABEL])
        log.error("label failed after fold: %s", e)
    ledger.transition(thread_id, ledger.BOOKED, event_id=event_id,
                      booked_start_iso=existing["start_az"].isoformat(),
                      agent="Alex Foley")
    return "sent"


def book_proposed_time(thread_id, doc, first_name, address, relay, cls,
                       message_id, now_az) -> str:
    # Calendar read failing here used to kill the whole handler AFTER the
    # "classified" outcome was recorded - Andrea's propose_time vanished
    # without a trace in the 7/30-8/1 shadow. Fail loud and safe instead.
    try:
        events = cal.list_events(10)
    except Exception as e:  # noqa: BLE001
        log.error("book_proposed_time list_events failed: %s", e)
        needs_human(thread_id, first_name, address,
                    f"calendar unavailable while booking: {str(e)[:80]}")
        return "no-send:calendar-error"
    for cand in cls.get("time_candidates", []):
        if not cand.get("date") or not cand.get("time"):
            continue
        try:
            h, m = map(int, cand["time"].split(":"))
            y, mo, d = map(int, cand["date"].split("-"))
            start_az = datetime(y, mo, d, h, m, tzinfo=AZ_TZ)
        except (ValueError, TypeError):
            continue
        if start_az < now_az:
            continue
        v = cal.validate_slot(start_az, address, events,
                              bounds={"after": cand.get("after"),
                                      "before": cand.get("before")},
                              now_az=now_az)
        if v.get("fold"):
            existing = v["fold"]
            return send_stage(
                thread_id, f"reply__{message_id}", relay,
                T.offer_existing(first_name, address, existing["when_human"]),
                [AWAITING_LABEL], [], {"template": "offer_existing_p2"}, message_id)
        if not v.get("ok"):
            continue

        agent, same_day = v["agent"], v["same_day"]
        jace_cover = v.get("jace_cover")
        when_human = cal.fmt_showing_time(start_az)
        jace_ping = None
        if same_day or jace_cover:
            reason = "same-day" if same_day else "Alex booked"
            jace_ping = (f"{agent['name'].split()[0]} assigned: {first_name} at {address.split(',')[0]} "
                         f"{start_az.strftime('%a %-m/%-d %-I:%M %p')} - {reason}. "
                         "Conflict? Reply fast.")

        if dry_run():
            ledger.write_shadow(thread_id, f"booked__pending_{message_id}", {
                "would_calendar": {"create": True, "start": start_az.isoformat(),
                                   "agent": agent["name"], "same_day": same_day,
                                   "jace_cover": jace_cover},
                "would_body": T.booking_confirmation(first_name, address,
                                                     when_human, agent["name"]),
                "would_poke": jace_ping,
            })
            return "shadowed"

        stage_probe = f"reply__{message_id}"
        verdict = ledger.reserve_send(thread_id, stage_probe,
                                      {"template": "booking_new"})
        if verdict in ("already-sent", "in-flight"):
            return f"skipped:{verdict}"
        if verdict == "recover":
            msgs = gm.fetch_thread(thread_id)
            if gm.alex_replied_after(msgs, message_id):
                ledger.mark_sent(thread_id, stage_probe, recovered="backfilled")
                return "skipped:recovered"
            if not ledger.takeover_send(thread_id, stage_probe):
                return "skipped:takeover-lost"

        try:
            event_id = cal.create_showing_event(address, first_name, start_az, agent)
        except Exception as e:  # noqa: BLE001
            ledger.mark_failed(thread_id, stage_probe, f"create_event: {e}")
            return "skipped:event-failed"
        try:
            gm.send_reply(thread_id, relay, T.booking_confirmation(
                first_name, address, when_human, agent["name"]))
        except Exception as e:  # noqa: BLE001
            ledger.mark_failed(thread_id, stage_probe, f"send-after-event: {e} "
                               f"event_id={event_id}")
            return "skipped:send-failed-event-created"
        ledger.mark_sent(thread_id, stage_probe, event_id=event_id)
        try:
            gm.modify_labels(thread_id, [HANDLED_LABEL], [AWAITING_LABEL])
        except Exception as e:  # noqa: BLE001
            ledger.set_labels_pending(thread_id, stage_probe,
                                      [HANDLED_LABEL], [AWAITING_LABEL])
            log.error("label failed after booking: %s", e)
        ledger.transition(thread_id, ledger.BOOKED, event_id=event_id,
                          booked_start_iso=start_az.isoformat(), agent=agent["name"])
        if jace_ping:
            gm.poke_ping(jace_ping)
        return "sent"

    # No candidate survived validation -> counter with nearest valid slots.
    try:
        slots = cal.counter_slots(address, events, now_az=now_az)
    except Exception as e:  # noqa: BLE001
        log.error("counter_slots failed: %s", e)
        slots = []
    if not slots:
        needs_human(thread_id, first_name, address, "no valid slots to counter")
        return "no-send:needs-human"
    return send_stage(thread_id, f"reply__{message_id}", relay,
                      T.counter_proposal(first_name,
                                         [cal.fmt_showing_time(s) for s in slots]),
                      [AWAITING_LABEL], [], {"template": "counter"}, message_id)


def handle_cancellation(thread_id, doc, first_name, relay, message_id,
                        cls=None, address=None) -> str:
    event_id = doc.get("event_id")
    # Reason-aware (Jamie 2026-08-22, "dead battery"): when the renter SAID
    # why, never ask why again. Slot-aware: after clearing them off the
    # calendar, offer the next remaining showing at the home if one exists.
    reason_given = bool((cls or {}).get("cancel_reason"))
    if dry_run():
        ledger.write_shadow(thread_id, f"reply__{message_id}", {
            "would_calendar": {"cancel": True, "event_id": event_id},
            "would_body": T.reschedule_after_cancel(first_name, reason_given),
        })
        return "shadowed"
    if event_id:
        try:
            # Fold-aware: shared consolidated events survive one renter's
            # cancel; only a solo event is deleted (Alex 2026-08-22).
            cal.remove_renter_or_cancel(event_id, doc.get("renter_name") or "")
        except Exception as e:  # noqa: BLE001
            log.error("cancel/remove failed (continuing): %s", e)
    next_when = None
    addr = address or doc.get("property_address") or ""
    if addr:
        try:
            for slot in cal.find_existing_showings(addr):
                if slot["event"].get("id") != event_id:
                    next_when = slot["when_human"]
                    break
        except Exception as e:  # noqa: BLE001
            log.error("post-cancel slot lookup failed: %s", e)
    result = send_stage(thread_id, f"reply__{message_id}", relay,
                        T.reschedule_after_cancel(first_name, reason_given,
                                                  next_when),
                        [AWAITING_LABEL], [HANDLED_LABEL],
                        {"template": "reschedule"}, message_id)
    ledger.transition(thread_id, ledger.AWAITING_TIME, event_id=None,
                      booked_start_iso=None)
    return result


def handle_alex_owned_reply(thread_id, doc, msgs, renter_text, message_id) -> str:
    """Alex runs this conversation - the service never sends here. But a
    cancellation of a booked showing is time-critical (someone will drive
    out), so the calendar clears immediately and Alex gets a needs-you ping;
    his Poke reply becomes the why/reschedule message via /send-approved.
    Protocol set by Alex 2026-08-05 after Natalie/Apricot canceled into
    silence: cancel calendar first, ask why, nudge to reschedule."""
    event_id = doc.get("event_id")
    if not event_id and doc.get("state") != ledger.BOOKED:
        return "skipped:alex-owned"
    last_alex = next((gm.msg_body(m) for m in reversed(msgs) if gm.is_from_alex(m)), "")
    cls = rules.snap_weekday_dates(
        llm.classify_reply(renter_text, last_alex, datetime.now(AZ_TZ)),
        datetime.now(AZ_TZ))
    if cls["intent"] != "cancellation":
        return "skipped:alex-owned"
    ledger.record_message_outcome(message_id, "classified", cls)
    if dry_run():
        ledger.write_shadow(thread_id, f"reply__{message_id}", {
            "would_calendar": {"cancel": True, "event_id": event_id},
            "would_poke": "alex-owned cancellation",
        })
        return "shadowed"
    if event_id:
        try:
            # Fold-aware: shared consolidated events survive one renter's
            # cancel; only a solo event is deleted (Alex 2026-08-22).
            cal.remove_renter_or_cancel(event_id, doc.get("renter_name") or "")
        except Exception as e:  # noqa: BLE001
            log.error("cancel/remove failed (continuing): %s", e)
    try:
        gm.modify_labels(thread_id, [NEEDS_REPLY_LABEL], [HANDLED_LABEL])
    except Exception as e:  # noqa: BLE001
        log.error("cancel relabel failed: %s", e)
    ledger.transition(thread_id, ledger.AWAITING_TIME, event_id=None,
                      booked_start_iso=None)
    needs_human(thread_id, doc.get("renter_name") or "the renter",
                doc.get("property_address") or "the property",
                "canceled the booked showing - calendar cleared. Reply with "
                "your message to them (ask why, offer a new time)")
    return "no-send:alex-owned-cancel"


# ---------------------------------------------------------------- routing

def route_message(thread_id: str, subject: str, sender: str, message_id: str) -> str:
    sender_l = (sender or "").lower()
    if gm.ALEX_EMAIL in sender_l:
        return "skipped:self-send"

    if not ledger.claim_message(message_id or f"noid-{thread_id}", thread_id):
        return "skipped:dup-delivery"

    msgs = gm.fetch_thread(thread_id)
    if not msgs:
        ledger.record_message_outcome(message_id, "empty-thread")
        return "skipped:empty-thread"

    renter_msg = gm.last_renter_message(msgs)
    renter_text = gm.extract_renter_text(renter_msg) if renter_msg else ""

    if renter_text and not ledger.claim_content_hash(thread_id, renter_text):
        ledger.record_message_outcome(message_id, "dup-content")
        return "skipped:dup-content"

    if gm.alex_replied_after(msgs, message_id):
        ledger.record_message_outcome(message_id, "already-answered")
        return "skipped:already-answered"

    doc = ledger.get_thread(thread_id)
    first_name, address = parse_subject(subject)
    if (not first_name or not address) and msgs:
        # Subject shapes like "requesting to tour" used to lose both fields
        # ("Hi there" greetings, blank-address confirmations, and a leased
        # home could get a showing invite because the blocked check had no
        # address). The body always carries them (2026-08-22).
        b_name, b_addr = gm.extract_identity_from_body(msgs[0])
        first_name = first_name or b_name
        address = address or b_addr

    if doc is None and first_name and len(msgs) <= 2 and not any(
            gm.is_from_alex(m) for m in msgs):
        relay = gm.relay_from_sender(sender) or gm.relay_from_thread(msgs)
        if not relay:
            ledger.record_message_outcome(message_id, "no-relay")
            return "error-no-relay"
        try:
            out = handle_new_inquiry(thread_id, first_name, address, relay,
                                     renter_text, message_id)
        except Exception as e:  # noqa: BLE001
            log.exception("handle_new_inquiry crashed on %s", thread_id)
            ledger.record_message_outcome(
                message_id, f"error:{type(e).__name__}",
                extra={"error": str(e)[:300], "renter_text": renter_text[:200]})
            return f"error:{type(e).__name__}"
        ledger.record_message_outcome(message_id, out,
                                      extra={"renter_text": renter_text[:200]})
        return out

    if doc is None:
        if not any(gm.is_from_relay(m) for m in msgs):
            ledger.record_message_outcome(message_id, "not-zillow")
            return "skipped:not-zillow"
        # Legacy in-flight thread from the pre-ledger era: reconstruct.
        name = first_name or "there"
        addr = address or ""
        alex_bodies = [gm.msg_body(m).lower() for m in msgs if gm.is_from_alex(m)]
        if any("you're all set" in b for b in alex_bodies):
            state = ledger.BOOKED
        elif any("has been rented" in b or "no longer available" in b
                 for b in alex_bodies):
            # The sweep already told this renter the home is rented - never
            # reconstruct as waiting (alondra's thread got nudged into a tour
            # invite for a leased house, 8/4).
            state = ledger.LEASED
        else:
            state = ledger.AWAITING_TIME
        ledger.create_thread(thread_id, state=state, renter_name=name,
                             property_address=addr,
                             relay_email=gm.relay_from_thread(msgs),
                             reconstructed=True)
        doc = ledger.get_thread(thread_id)

    if renter_msg is None or gm.msg_id(renter_msg) != (message_id or gm.msg_id(renter_msg)):
        # The triggering message isn't the newest renter message; process the
        # newest one (idempotency keys keep this safe).
        pass

    try:
        out = handle_reply(thread_id, doc, msgs, renter_text,
                           message_id or gm.msg_id(renter_msg or {}) or thread_id)
    except Exception as e:  # noqa: BLE001
        log.exception("handle_reply crashed on %s", thread_id)
        ledger.record_message_outcome(
            message_id, f"error:{type(e).__name__}",
            extra={"error": str(e)[:300], "renter_text": renter_text[:200]})
        return f"error:{type(e).__name__}"
    ledger.record_message_outcome(message_id, out,
                                  extra={"renter_text": renter_text[:200]})
    return out


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, text):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        self._send(200, f"zillow-pipeline ok (dry_run={dry_run()})")

    def do_POST(self):
        secret = self.headers.get("X-Webhook-Secret", "")
        required = (REPROCESS_SECRET
                    if self.path.rstrip("/") in ("/reprocess", "/send-approved")
                    else WEBHOOK_SECRET)
        if required and secret != required:
            self._send(401, "bad secret")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw or b"{}")
        except Exception as e:  # noqa: BLE001
            self._send(400, f"bad json: {e}")
            return

        try:
            if self.path.rstrip("/") == "/send-approved":
                # Poke approval loop: Alex answered a needs-you ping; the
                # approval MCP relays it here. The SERVICE stays the single
                # writer - Poke never emails renters, it commands this endpoint.
                thread_id = payload.get("thread_id", "")
                answer = (payload.get("answer") or "").strip()
                if not thread_id or not answer:
                    self._send(400, "missing thread_id or answer")
                    return
                doc = ledger.get_thread(thread_id)
                if not doc:
                    self._send(404, "unknown thread")
                    return
                relay = doc.get("relay_email") or ""
                if not relay:
                    msgs = gm.fetch_thread(thread_id)
                    relay = gm.relay_from_thread(msgs)
                if not relay:
                    self._send(422, "no relay address on thread")
                    return
                first_name = doc.get("renter_name") or "there"
                # Alex may have already answered by hand (Jamie 8/22: manual
                # reply at 1:16 PM, approved send 3 min later = double
                # apology). If the LAST message in the thread is from Alex,
                # skip unless the caller passes force:true.
                if not payload.get("force"):
                    try:
                        msgs_chk = gm.fetch_thread(thread_id)
                        if msgs_chk and gm.is_from_alex(msgs_chk[-1]):
                            self._send(200, "skipped:alex-replied-last")
                            return
                    except Exception as e:  # noqa: BLE001
                        log.error("send-approved freshness check failed "
                                  "(continuing): %s", e)
                stage = f"approved__{ledger.content_hash(answer)}"
                result = send_stage(
                    thread_id, stage, relay,
                    T.approved_answer(first_name, answer),
                    [AWAITING_LABEL], [NEEDS_REPLY_LABEL],
                    {"template": "approved_answer", "approved_via": "poke"})
                if result == "sent" and doc.get("state") in (
                        ledger.NEEDS_HUMAN, ledger.CLOSED):
                    # CLOSED included 2026-08-22: Jamie's price-drop reply
                    # landed on a dead-closed thread that stayed CLOSED and
                    # could not book until hand-reopened.
                    ledger.transition(thread_id, ledger.AWAITING_TIME)
                self._send(200, result)
                return

            if self.path.rstrip("/") == "/reprocess":
                thread_id = payload.get("thread_id", "")
                message_id = payload.get("message_id", "") or f"reproc-{thread_id}-{datetime.now(AZ_TZ).strftime('%Y%m%d%H%M')}"
                if not thread_id:
                    self._send(400, "missing thread_id")
                    return
                result = route_message(thread_id, payload.get("subject", ""),
                                       payload.get("sender", ""), message_id)
                self._send(200, result)
                return

            thread_id, subject, sender, message_id = gm.extract_event(payload)
            if not thread_id:
                self._send(200, "ignored-no-thread")
                return
            result = route_message(thread_id, subject or "", sender or "",
                                   message_id or "")
            self._send(200, result)
        except Exception as e:  # noqa: BLE001
            log.exception("handler error")
            self._send(200, f"error-logged: {e}")

    def log_message(self, *args):
        pass


def main():
    missing = [k for k in ("COMPOSIO_API_KEY", "COMPOSIO_CONNECTED_ACCOUNT_ID")
               if not os.environ.get(k)]
    if missing:
        log.warning("Missing env vars: %s", ", ".join(missing))
    # Ledger init failure must not crash-loop the service. Without a ledger the
    # pipeline FAILS CLOSED per-request (claim_message raises -> no send ever),
    # which is the safe direction; health stays up so the deploy is debuggable.
    try:
        ledger.init_db()
        cron.start_ticker()
    except Exception as e:  # noqa: BLE001
        log.error("LEDGER INIT FAILED - serving fail-closed, fix credentials: %s", e)
    log.info("Zillow pipeline v2 listening on :%s (dry_run=%s)", PORT, dry_run())
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
