"""In-process ticker: the maintenance loops that keep the ledger honest.

Every 20 minutes:
  1. retry pending label applications (label failure never resends email)
  2. recover stuck `reserved` send docs (Gmail-check then backfill)
  3. stale-lead nudge: waiting-on-renter > 48h -> one nudge EVER per thread
  4. dead-lead close: waiting > 7 days -> CLOSED (no send)

Nudges are sends, and sends belong to this service - the 3x/day watchdog is
too coarse for the 48h nudge, which is why this lives in-process.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import gmail_client as gm
import ledger
import templates as T

log = logging.getLogger("zillow-instant.cron")

TICK_SECONDS = 20 * 60
NUDGE_AFTER_H = 48
CLOSE_AFTER_D = 7


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
                # goes to a human instead of risking a duplicate.
                doc = ledger.get_thread(thread_id) or {}
                ledger.transition(thread_id, ledger.NEEDS_HUMAN)
                log.warning("stuck send needs human: %s", key)
        except Exception as e:  # noqa: BLE001
            log.error("recover check failed %s: %s", key, e)


def _nudge_and_close():
    import responder  # lazy: avoids circular import at module load

    db = ledger.init_db()
    now = datetime.now(timezone.utc)
    q = (db.collection("zillow_threads")
         .where("state", "in", list(ledger.WAITING_STATES)).limit(50))
    for snap in q.stream():
        doc = snap.to_dict() or {}
        thread_id = snap.id
        if doc.get("alex_owned"):
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


def start_ticker():
    def _loop():
        time.sleep(60)  # let the server settle before the first tick
        while True:
            run_tick()
            time.sleep(TICK_SECONDS)

    t = threading.Thread(target=_loop, daemon=True, name="ledger-ticker")
    t.start()
    return t
