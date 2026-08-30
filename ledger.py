"""Firestore ledger — the single source of truth for what has been processed and sent.

Every duplicate-send bug this pipeline has ever had traces back to re-deriving
"did we already reply?" from Gmail at read time. This module replaces that with
create-once documents in Firestore (project boundless-portal-c94d0):

  zillow_messages/{gmailMessageId}          processed-message gate (double delivery)
  zillow_msg_hashes/{threadId}__{sha16}     content-hash gate (Zillow duplicate copies)
  zillow_sends/{threadId}__{stageKey}       THE send lock (reserve -> send -> sent)
  zillow_threads/{gmailThreadId}            per-thread state machine
  zillow_shadow/{threadId}__{stageKey}      dry-run mirror of would-be sends
  property_facts/{addr-slug}                fact sheets synced from property-facts/*.md
  zillow_config/properties                  blocked (leased) address list

Firestore document create() is atomic: it raises AlreadyExists if the doc
exists. That is the idempotency primitive. We never decide to send based on
anything except winning that create.
"""

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.exceptions import AlreadyExists, FailedPrecondition
from google.cloud.firestore_v1.base_query import FieldFilter

log = logging.getLogger("zillow-instant.ledger")

_DB = None

# States. ACKED/OFFERED/AWAITING_TIME all mean "waiting on renter" and route
# identically; they are stored distinctly only for observability.
NEW = "NEW"
ACKED = "ACKED"
OFFERED = "OFFERED"
AWAITING_TIME = "AWAITING_TIME"
BOOKED = "BOOKED"
NEEDS_HUMAN = "NEEDS_HUMAN"
LEASED = "LEASED"
CLOSED = "CLOSED"

WAITING_STATES = {ACKED, OFFERED, AWAITING_TIME}

# How long a `reserved` send doc is considered "another worker is in flight"
# before recovery may take it over.
RESERVE_TTL_MIN = 10
CONTENT_HASH_WINDOW_H = 48


def init_db():
    """Initialize firebase-admin once. Uses GOOGLE_APPLICATION_CREDENTIALS
    (Render secret file) when set, else falls back to ADC (laptop dev)."""
    global _DB
    if _DB is not None:
        return _DB
    if not firebase_admin._apps:
        sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if sa_path and os.path.exists(sa_path):
            cred = credentials.Certificate(sa_path)
        else:
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred, {"projectId": "boundless-portal-c94d0"})
    _DB = firestore.client()
    return _DB


def _now():
    return datetime.now(timezone.utc)


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().lower().encode()).hexdigest()[:16]


# ---------------------------------------------------------------- gates

def claim_message(message_id: str, thread_id: str) -> bool:
    """Create-once gate on the Gmail message id. True = we own this message.
    False = it was already processed (duplicate webhook delivery)."""
    db = init_db()
    try:
        db.collection("zillow_messages").document(message_id).create({
            "thread_id": thread_id,
            "processed_at": firestore.SERVER_TIMESTAMP,
            "outcome": "claimed",
        })
        return True
    except AlreadyExists:
        return False


def record_message_outcome(message_id: str, outcome: str, classification: dict = None,
                           extra: dict = None):
    db = init_db()
    patch = {"outcome": outcome}
    if classification is not None:
        patch["classification"] = classification
    if extra:
        patch.update(extra)
    db.collection("zillow_messages").document(message_id).set(patch, merge=True)


def claim_content_hash(thread_id: str, text: str) -> bool:
    """Create-once gate on the renter text content within a 48h window.
    Catches Zillow's duplicate notification copies (new message_id, same words).
    True = first sighting. False = duplicate copy, skip."""
    db = init_db()
    key = f"{thread_id}__{content_hash(text)}"
    ref = db.collection("zillow_msg_hashes").document(key)
    try:
        ref.create({"created_at": firestore.SERVER_TIMESTAMP})
        return True
    except AlreadyExists:
        snap = ref.get()
        created = snap.get("created_at") if snap.exists else None
        if created and _now() - created > timedelta(hours=CONTENT_HASH_WINDOW_H):
            # Old enough that identical text is plausibly a genuine re-send
            # ("yes" twice a week apart). Refresh the window and treat as new.
            ref.set({"created_at": firestore.SERVER_TIMESTAMP})
            return True
        if _thread_send_failed_since(thread_id, created):
            # The claim exists because WE processed this text - and then the
            # send/booking failed after the claim (mikayla + Jessica, 8/29:
            # /reprocess returned dup-content forever, renter stranded).
            # A failure newer than the claim means the renter never got the
            # reply the claim was protecting against duplicating, so a re-run
            # is a retry, not a dupe. Do NOT refresh created_at here: the
            # refresh moved the claim clock past the failure and made the
            # SECOND retry a dupe again (Schneider, 8/30, three reprocess
            # rounds). The claim stays anchored to the original processing;
            # retries keep passing until one actually succeeds.
            return True
        return False


def _thread_send_failed_since(thread_id: str, claimed_at) -> bool:
    """True when this thread has a message whose processing FAILED at or after
    the moment the content hash was claimed - the strand signature.

    The 10-minute grace matters: claim_message stamps processed_at a beat
    BEFORE claim_content_hash stamps created_at in the same request, so the
    very message whose failure stranded the hash always looks slightly older
    than the claim. Without the grace the exemption can never fire for the
    primary case (verified live on Mitchell's thread, 2026-08-29)."""
    failed_prefixes = ("skipped:event-failed", "error:", "no-send:calendar-error",
                       "no-send:send-failed")
    try:
        db = init_db()
        q = db.collection("zillow_messages").where(
            filter=FieldFilter("thread_id", "==", thread_id)).stream()
        for m in q:
            d = m.to_dict() or {}
            outcome = str(d.get("outcome") or "")
            if not outcome.startswith(failed_prefixes):
                continue
            processed = d.get("processed_at")
            if (claimed_at is None or processed is None
                    or processed >= claimed_at - timedelta(minutes=10)):
                return True
    except Exception as e:  # noqa: BLE001
        log.error("failed-send exemption check errored (treating as dup): %s", e)
    return False


# ---------------------------------------------------------------- threads

def get_thread(thread_id: str) -> dict | None:
    db = init_db()
    snap = db.collection("zillow_threads").document(thread_id).get()
    return snap.to_dict() if snap.exists else None


def upsert_thread(thread_id: str, **fields) -> None:
    db = init_db()
    fields["updated_at"] = firestore.SERVER_TIMESTAMP
    db.collection("zillow_threads").document(thread_id).set(fields, merge=True)


def create_thread(thread_id: str, **fields) -> None:
    fields.setdefault("state", NEW)
    fields["created_at"] = firestore.SERVER_TIMESTAMP
    upsert_thread(thread_id, **fields)


def transition(thread_id: str, state: str, **fields) -> None:
    fields["state"] = state
    fields["last_action_at"] = firestore.SERVER_TIMESTAMP
    upsert_thread(thread_id, **fields)


# ---------------------------------------------------------------- send lock

def _send_ref(thread_id: str, stage_key: str):
    return init_db().collection("zillow_sends").document(f"{thread_id}__{stage_key}")


def reserve_send(thread_id: str, stage_key: str, meta: dict) -> str:
    """Try to acquire the send lock for (thread, stage).

    Returns one of:
      "acquired"       - we own it; proceed to send
      "already-sent"   - a send for this stage already went out; skip
      "in-flight"      - another worker reserved it recently; skip
      "recover"        - stale reserved/failed doc; caller must Gmail-check
                         then call takeover_send() before sending
    """
    ref = _send_ref(thread_id, stage_key)
    doc = dict(meta)
    doc.update({
        "status": "reserved",
        "reserved_at": firestore.SERVER_TIMESTAMP,
        "attempts": 1,
    })
    try:
        ref.create(doc)
        return "acquired"
    except AlreadyExists:
        snap = ref.get()
        data = snap.to_dict() or {}
        status = data.get("status")
        if status == "sent":
            return "already-sent"
        reserved_at = data.get("reserved_at")
        age_ok = reserved_at and (_now() - reserved_at) < timedelta(minutes=RESERVE_TTL_MIN)
        if status == "reserved" and age_ok:
            return "in-flight"
        if status == "failed" and data.get("attempts", 0) >= 3:
            return "already-sent"  # give up: humans will see it in NEEDS_HUMAN aging
        return "recover"


def takeover_send(thread_id: str, stage_key: str) -> bool:
    """Take over a stale reserved/failed send doc via transaction. True = we own it."""
    db = init_db()
    ref = _send_ref(thread_id, stage_key)

    @firestore.transactional
    def _tx(tx):
        snap = ref.get(transaction=tx)
        data = snap.to_dict() or {}
        if data.get("status") == "sent":
            return False
        reserved_at = data.get("reserved_at")
        if data.get("status") == "reserved" and reserved_at and \
                (_now() - reserved_at) < timedelta(minutes=RESERVE_TTL_MIN):
            return False
        tx.update(ref, {
            "status": "reserved",
            "reserved_at": firestore.SERVER_TIMESTAMP,
            "attempts": firestore.Increment(1),
        })
        return True

    try:
        return _tx(db.transaction())
    except (FailedPrecondition, Exception) as e:  # noqa: BLE001 - any tx race = don't send
        log.error("takeover_send tx failed for %s__%s: %s", thread_id, stage_key, e)
        return False


def mark_sent(thread_id: str, stage_key: str, **fields):
    fields.update({"status": "sent", "sent_at": firestore.SERVER_TIMESTAMP})
    _send_ref(thread_id, stage_key).set(fields, merge=True)


def mark_failed(thread_id: str, stage_key: str, error: str):
    _send_ref(thread_id, stage_key).set(
        {"status": "failed", "error": str(error)[:500]}, merge=True)


def set_labels_pending(thread_id: str, stage_key: str, add_label_ids: list, remove_label_ids: list):
    _send_ref(thread_id, stage_key).set({
        "labels_pending": True,
        "labels_add": add_label_ids,
        "labels_remove": remove_label_ids,
    }, merge=True)


def clear_labels_pending(thread_id: str, stage_key: str):
    _send_ref(thread_id, stage_key).set({"labels_pending": False}, merge=True)


def pending_label_docs(limit: int = 20):
    db = init_db()
    return list(db.collection("zillow_sends")
                .where("labels_pending", "==", True).limit(limit).stream())


def stale_reserved_docs(older_than_min: int = 30, limit: int = 20):
    db = init_db()
    cutoff = _now() - timedelta(minutes=older_than_min)
    return list(db.collection("zillow_sends")
                .where("status", "in", ["reserved", "failed"])
                .where("reserved_at", "<", cutoff).limit(limit).stream())


# ---------------------------------------------------------------- shadow

def write_shadow(thread_id: str, stage_key: str, payload: dict):
    """DRY_RUN mirror: everything a live run would have sent/created."""
    db = init_db()
    payload = dict(payload)
    payload["created_at"] = firestore.SERVER_TIMESTAMP
    db.collection("zillow_shadow").document(f"{thread_id}__{stage_key}").set(payload)


# ---------------------------------------------------------------- config + facts

# ---------------------------------------------------------------- showings
# The ledger owns WHO IS ON EACH SHOWING (2026-08-25, calendar-as-projection
# refactor phase A). Calendar event descriptions are rendered FROM these docs
# and never parsed back as a database; calendar_logic.parse_inquirers survives
# only as the migration fallback for events created before this collection.

def upsert_showing(event_id: str, address: str = None, start_iso: str = None,
                   agent_name: str = None, renters: list = None) -> None:
    fields = {"updated_at": _now()}
    if address is not None:
        fields["address"] = address
    if start_iso is not None:
        fields["start_iso"] = start_iso
    if agent_name is not None:
        fields["agent_name"] = agent_name
    if renters is not None:
        fields["renters"] = renters
    init_db().collection("zillow_showings").document(event_id).set(
        fields, merge=True)


def get_showing(event_id: str) -> dict | None:
    snap = init_db().collection("zillow_showings").document(event_id).get()
    return snap.to_dict() if snap.exists else None


def delete_showing(event_id: str) -> None:
    init_db().collection("zillow_showings").document(event_id).delete()


def blocked_addresses() -> list:
    """Leased/off-market list from zillow_config/properties. Falls back to []
    on any error (never blocks the instant reply)."""
    try:
        snap = init_db().collection("zillow_config").document("properties").get()
        if snap.exists:
            return snap.get("blocked_addresses") or []
    except Exception as e:  # noqa: BLE001
        log.error("blocked_addresses read failed: %s", e)
    return []


def get_facts(addr_slug: str) -> str | None:
    try:
        snap = init_db().collection("property_facts").document(addr_slug).get()
        return snap.get("raw_md") if snap.exists else None
    except Exception as e:  # noqa: BLE001
        log.error("get_facts(%s) failed: %s", addr_slug, e)
        return None


def get_standing_rules() -> str | None:
    return get_facts("_standing_rules")
