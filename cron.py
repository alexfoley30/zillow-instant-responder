"""In-process ticker: the maintenance loops that keep the ledger honest.

Every 20 minutes:
  1. retry pending label applications (label failure never resends email)
  2. recover stuck `reserved` send docs (Gmail-check then backfill)
  3. stale-lead nudge: waiting-on-renter > 48h -> one nudge EVER per thread
  4. dead-lead close: waiting > 7 days -> CLOSED (no send)
  5. showing reminders: one the evening before, one ~2h out, each asking the
     renter to confirm the tour still works (Alex 2026-08-29)

Nudges are sends, and sends belong to this service - the 3x/day watchdog is
too coarse for the 48h nudge, which is why this lives in-process.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import calendar_logic as cal
import gmail_client as gm
import ledger
import rules
import templates as T

log = logging.getLogger("zillow-instant.cron")

TICK_SECONDS = 20 * 60
NUDGE_AFTER_H = 48
CLOSE_AFTER_D = 7

# Showing reminders. The evening-before pass opens at 4pm local and stays open
# until midnight, so a missed tick (Render restart) still catches it - the send
# lock, not the window, is what stops a second copy.
REMIND_EVENING_FROM_H = 16
REMIND_SOON_H = 2
# A tour booked minutes ago does not need a "coming up in a couple hours" note
# on top of the confirmation the renter is still reading.
REMIND_MIN_AGE_MIN = 90


def _retry_labels():
    for snap in ledger.pending_label_docs():
        data = snap.to_dict() or {}
        thread_id = snap.id.split("__")[0]
        try:
            gm.modify_labels(thread_id, data.get("labels_add", []),
                             data.get("labels_remove", []))
            ledger.clear_labels_pending(thread_id, snap.id.split("__", 1)[1])
            log.info("label retry ok: %s", snap.id)
        except Exception as e:  # noqa: BLE001
            log.error("label retry failed %s: %s", snap.id, e)


def _recover_stuck():
    for snap in ledger.stale_reserved_docs(older_than_min=30):
        key = snap.id
        thread_id, stage_key = key.split("__", 1)
        data = snap.to_dict() or {}
        try:
            msgs = gm.fetch_thread(thread_id)
            if gm.alex_replied_after(msgs, data.get("trigger_message_id", "")):
                ledger.mark_sent(thread_id, stage_key, recovered="cron-backfill")
                log.info("stuck send backfilled from Gmail: %s", key)
            else:
                # Never auto-resend from cron: an untraceable half-sent state
                # goes to a human instead of risking a duplicate. And a human
                # must actually HEAR about it (audit 8/25: this path only
                # logged, then _age_needs_human closed the thread silently at
                # 7 days - the renter was dropped without anyone being told).
                ledger.transition(thread_id, ledger.NEEDS_HUMAN)
                log.warning("stuck send needs human: %s", key)
                note = (f"Stuck half-sent reply on thread {thread_id} "
                        f"(stage {stage_key}) - check the Gmail thread before "
                        "anyone emails this renter.")
                try:
                    gm.poke_ping("Needs you: " + note)
                except Exception as e:  # noqa: BLE001
                    log.error("stuck-send ping failed: %s", e)
                try:
                    gm.alert_email(f"Needs you: stuck send {thread_id}", note)
                except Exception as e:  # noqa: BLE001
                    log.error("stuck-send alert email failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.error("recover check failed %s: %s", key, e)


def _age_needs_human():
    """NEEDS_HUMAN threads used to live forever (34 piled up by 2026-08-22,
    16 at already-leased homes). Alex's ruling: blocked-address escalations
    flip to LEASED silently; anything else unresolved for 7 days closes
    silently. Alex-owned threads are never touched (he may be working the
    deal off-channel)."""
    db = ledger.init_db()
    now = datetime.now(timezone.utc)
    try:
        blocked = ledger.blocked_addresses()
    except Exception:  # noqa: BLE001
        blocked = []
    q = (db.collection("zillow_threads")
         .where("state", "==", ledger.NEEDS_HUMAN).limit(50))
    for snap in q.stream():
        doc = snap.to_dict() or {}
        if doc.get("alex_owned"):
            continue
        if rules.is_blocked_address(doc.get("property_address") or "", blocked):
            ledger.transition(snap.id, ledger.LEASED,
                              closed_reason="needs-human-at-leased")
            continue
        last = doc.get("last_action_at") or doc.get("updated_at") or doc.get("created_at")
        if last and now - last > timedelta(days=CLOSE_AFTER_D):
            ledger.transition(snap.id, ledger.CLOSED,
                              closed_reason="needs-human-stale-7d")


def _nudge_and_close():
    import responder  # lazy: avoids circular import at module load

    _age_needs_human()

    db = ledger.init_db()
    now = datetime.now(timezone.utc)
    q = (db.collection("zillow_threads")
         .where("state", "in", list(ledger.WAITING_STATES)).limit(50))
    try:
        blocked = ledger.blocked_addresses()
    except Exception:  # noqa: BLE001
        blocked = []
    for snap in q.stream():
        doc = snap.to_dict() or {}
        thread_id = snap.id
        if doc.get("alex_owned"):
            continue
        # A leased-address thread must never be nudged - alondra was told
        # "rented" by the sweep, then the nudge re-invited her to tour (8/4).
        if rules.is_blocked_address(doc.get("property_address") or "", blocked):
            ledger.transition(thread_id, ledger.LEASED)
            continue
        last = doc.get("last_action_at") or doc.get("updated_at") or doc.get("created_at")
        if not last:
            continue
        age = now - last
        if age > timedelta(days=CLOSE_AFTER_D):
            ledger.transition(thread_id, ledger.CLOSED, closed_reason="stale-7d")
            continue
        if age > timedelta(hours=NUDGE_AFTER_H) and not doc.get("nudged"):
            # Verify against the thread before nudging - the renter may have
            # replied through a path we missed (never nudge over their reply).
            try:
                msgs = gm.fetch_thread(thread_id)
            except Exception as e:  # noqa: BLE001
                log.error("nudge fetch failed %s: %s", thread_id, e)
                continue
            if msgs and gm.is_from_relay(msgs[-1]):
                continue  # renter spoke last: reprocess will handle it, not a nudge
            relay = gm.relay_from_thread(msgs) or doc.get("relay_email", "")
            if not relay:
                continue
            result = responder.send_stage(
                thread_id, "nudge", relay,
                T.stale_nudge(doc.get("renter_name", "there"),
                              doc.get("property_address", "the home")),
                [], [], {"template": "nudge"})
            if result in ("sent", "shadowed"):
                ledger.upsert_thread(thread_id, nudged=True)


def reminder_due(start_az, now_az, booked_at_az=None):
    """Which reminder (if any) this showing is owed right now.

    Returns "2h" | "day_before" | None. Pure, so the windows are testable
    without Firestore or Gmail.

    The evening-before pass is a CALENDAR-DAY test, not a 24-hour countdown:
    Alex asked for "the day before", and a tour booked the same morning has no
    day before. Showing windows run 10:00-18:30, so a 4pm-onward evening pass
    and a two-hour-out pass both land at civil hours on their own - no quiet
    hours logic needed.
    """
    if start_az is None or now_az is None:
        return None
    delta = start_az - now_az
    if delta <= timedelta(0):
        return None  # tour already started; TOURED follow-up is a separate job
    if delta <= timedelta(hours=REMIND_SOON_H):
        if booked_at_az and (now_az - booked_at_az) < timedelta(minutes=REMIND_MIN_AGE_MIN):
            return None
        return "2h"
    if start_az.date() == (now_az + timedelta(days=1)).date() \
            and now_az.hour >= REMIND_EVENING_FROM_H:
        return "day_before"
    return None


def _as_az(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(rules.AZ_TZ)


def _showing_reminders():
    """Ask every booked renter whether their tour still works - once the
    evening before, once about two hours out (Alex 2026-08-29: an agent
    driving across the valley for a renter who moved on is the expensive
    failure). Both are ordinary sends, so they inherit the send lock, the
    review gate and DRY_RUN for free.

    Keyed on event_id, not thread: a reschedule makes a new event and the new
    showing legitimately earns its own pair of reminders.
    """
    import responder  # lazy: avoids circular import at module load

    db = ledger.init_db()
    now_az = datetime.now(rules.AZ_TZ)
    try:
        blocked = ledger.blocked_addresses()
    except Exception:  # noqa: BLE001
        blocked = []
    q = (db.collection("zillow_threads")
         .where("state", "==", ledger.BOOKED).limit(50))
    for snap in q.stream():
        doc = snap.to_dict() or {}
        thread_id = snap.id
        if doc.get("alex_owned"):
            continue  # he is working it off-channel; do not talk over him
        address = doc.get("property_address") or ""
        if rules.is_blocked_address(address, blocked):
            continue  # home is gone; a reminder would summon them to nothing
        event_id = doc.get("event_id")
        if not event_id:
            continue
        raw_start = doc.get("booked_start_iso")
        agent_name = doc.get("agent") or ""
        if not raw_start:
            # Manual Poke-approved bookings and legacy reconstructions reach
            # BOOKED without stamping the time on the thread (Jessica, 8/28:
            # last_action "booked-manual-alex-approved", event_id set,
            # booked_start_iso None). zillow_showings is the ledger's own
            # record of when a showing is and who runs it, so read it there
            # and heal the thread rather than silently skipping the one
            # renter who actually has an upcoming tour.
            try:
                showing = ledger.get_showing(event_id) or {}
            except Exception as e:  # noqa: BLE001
                log.error("showing lookup failed %s: %s", event_id, e)
                continue
            raw_start = showing.get("start_iso")
            agent_name = agent_name or showing.get("agent_name") or ""
            if raw_start:
                try:
                    ledger.upsert_thread(thread_id, booked_start_iso=raw_start,
                                         agent=agent_name or None)
                except Exception as e:  # noqa: BLE001
                    log.error("booked_start_iso backfill failed %s: %s", thread_id, e)
        if not raw_start:
            log.warning("BOOKED thread %s has no showing time (event %s) - "
                        "no reminder possible", thread_id, event_id)
            continue
        try:
            start_az = datetime.fromisoformat(raw_start).astimezone(rules.AZ_TZ)
        except Exception as e:  # noqa: BLE001
            log.error("bad booked_start_iso on %s (%r): %s", thread_id, raw_start, e)
            continue

        which = reminder_due(start_az, now_az,
                             _as_az(doc.get("last_action_at")))
        if not which:
            continue

        relay = doc.get("relay_email") or ""
        if not relay:
            try:
                relay = gm.relay_from_thread(gm.fetch_thread(thread_id)) or ""
            except Exception as e:  # noqa: BLE001
                log.error("reminder relay lookup failed %s: %s", thread_id, e)
                continue
        if not relay:
            continue

        first_name = doc.get("renter_name") or "there"
        agent_name = agent_name or "Alex Foley"
        when_human = cal.fmt_showing_time(start_az)
        if which == "2h":
            stage = f"reminder_2h__{event_id}"
            body = T.showing_reminder_2h(first_name, address, when_human, agent_name)
        else:
            stage = f"reminder_day__{event_id}"
            body = T.showing_reminder_day_before(first_name, address, when_human,
                                                 agent_name)

        result = responder.send_stage(thread_id, stage, relay, body, [], [],
                                      {"template": f"showing_reminder_{which}",
                                       "event_id": event_id})
        log.info("showing reminder %s %s -> %s", which, thread_id, result)


def run_tick():
    try:
        _retry_labels()
    except Exception as e:  # noqa: BLE001
        log.error("label tick failed: %s", e)
    try:
        _recover_stuck()
    except Exception as e:  # noqa: BLE001
        log.error("recover tick failed: %s", e)
    try:
        _nudge_and_close()
    except Exception as e:  # noqa: BLE001
        log.error("nudge tick failed: %s", e)
    try:
        _showing_reminders()
    except Exception as e:  # noqa: BLE001
        log.error("reminder tick failed: %s", e)


def start_ticker():
    def _loop():
        time.sleep(60)  # let the server settle before the first tick
        while True:
            run_tick()
            time.sleep(TICK_SECONDS)

    t = threading.Thread(target=_loop, daemon=True, name="ledger-ticker")
    t.start()
    return t
