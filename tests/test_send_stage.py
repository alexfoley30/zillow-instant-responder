"""responder.send_stage - the ONLY function that emails a renter.

Everything renter-facing funnels through it: first replies, offers, bookings,
nudges, showing reminders. It was covered only as a monkeypatched stub, so its
own behaviour - one email per (thread, stage) forever, no email at all when
the review blocks or DRY_RUN is on, no SECOND email when labels fail - was
never actually run.

These tests run the real send_stage over the real ledger lock (in-memory
Firestore) and the real gmail_client.send_reply, with only the Composio HTTP
call replaced. So the assertions are on the outgoing email itself: how many
went out, to which relay, with which body.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DRY_RUN", "true")

import ledger  # noqa: E402
import responder  # noqa: E402
import templates as T  # noqa: E402
from fakes import (RELAY, alex_msg, install_composio,  # noqa: E402
                   install_firestore, renter_msg)

THREAD = "1a0504f3183c6691"
TRIGGER = "1a0545afd0fe9807"
STAGE = f"reply__{TRIGGER}"
KEY = f"{THREAD}__{STAGE}"
ADDRESS = "2118 S El Marino, Mesa, AZ, 85202"
BODY = T.propose_times("Owen", ["today at 3:00 PM", "tomorrow at 10:30 AM"])
LABELS_ADD = ["Label_1717014254813700027"]
LABELS_REMOVE = ["Label_2252577853408309931"]
META = {"template": "propose_times"}

THREAD_MSGS = {THREAD: [
    renter_msg("1a0504f3183c6691", "Owen", "Is this still available?"),
    alex_msg("1a05050000000001", "Hi Owen, happy to get you in for a tour."),
    renter_msg(TRIGGER, "Owen", "Tomorrow morning would be great."),
]}


def _live(monkeypatch):
    """A live (not DRY_RUN) run with the review gate reaching its real
    fail-open path (no ANTHROPIC_API_KEY => llm.review_reply returns 'send')."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = install_firestore(monkeypatch, ledger)
    composio = install_composio(monkeypatch, responder.gm, threads=THREAD_MSGS)
    return db, composio


def _send(**kw):
    args = dict(thread_id=THREAD, stage_key=STAGE, relay=RELAY, body=BODY,
                labels_add=LABELS_ADD, labels_remove=LABELS_REMOVE,
                meta=dict(META), trigger_message_id=TRIGGER)
    args.update(kw)
    return responder.send_stage(**args)


# ---------------------------------------------------------------- happy path

def test_one_email_goes_out_with_the_exact_body_and_relay(monkeypatch):
    db, composio = _live(monkeypatch)
    assert _send() == "sent"
    assert composio.replies == [{"thread_id": THREAD, "recipient_email": RELAY,
                                 "message_body": BODY}]
    assert "3:00 PM" in composio.replies[0]["message_body"]
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "sent"
    assert doc["body_sha256"] == ledger.content_hash(BODY)
    assert doc["to_relay"] == RELAY
    assert doc["trigger_message_id"] == TRIGGER
    assert db.doc("zillow_threads", THREAD)["last_reply_template"] == "propose_times"
    assert composio.of("GMAIL_MODIFY_THREAD_LABELS") == [
        {"thread_id": THREAD, "add_label_ids": LABELS_ADD,
         "remove_label_ids": LABELS_REMOVE}]


def test_the_same_stage_never_emails_the_renter_twice(monkeypatch):
    """The duplicate that started the single-writer rebuild: same thread, same
    stage, processed again. The second run must be silent."""
    db, composio = _live(monkeypatch)
    assert _send() == "sent"
    assert _send() == "skipped:already-sent"
    assert _send() == "skipped:already-sent"
    assert len(composio.replies) == 1
    assert db.doc("zillow_sends", KEY)["status"] == "sent"


def test_a_concurrent_worker_in_flight_does_not_send(monkeypatch):
    db, composio = _live(monkeypatch)
    assert ledger.reserve_send(THREAD, STAGE, dict(META)) == "acquired"  # worker A
    assert _send() == "skipped:in-flight"                                # worker B
    assert composio.replies == []


def test_a_new_stage_on_the_same_thread_still_sends(monkeypatch):
    """The lock is per (thread, stage), not per thread - a later reply in the
    same conversation must not be swallowed."""
    db, composio = _live(monkeypatch)
    assert _send() == "sent"
    later = T.booking_confirmation("Owen", ADDRESS, "tomorrow at 10:30 AM",
                                   "Rhett Lueck")
    assert _send(stage_key="booked__ev_9x1", body=later,
                 meta={"template": "booking_confirmation"}) == "sent"
    assert [r["message_body"] for r in composio.replies] == [BODY, later]


# ---------------------------------------------------------------- no-send paths

def test_dry_run_shadows_the_send_and_touches_nothing_else(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    db = install_firestore(monkeypatch, ledger)
    composio = install_composio(monkeypatch, responder.gm, threads=THREAD_MSGS)
    assert _send() == "shadowed"
    assert composio.calls == []
    assert db.ids("zillow_sends") == []
    shadow = db.doc("zillow_shadow", KEY)
    assert shadow["would_body"] == BODY
    assert shadow["would_labels_add"] == LABELS_ADD
    assert shadow["template"] == "propose_times"


def test_a_blocked_review_sends_nothing_and_escalates(monkeypatch):
    """The pre-send review is the last stop before a renter reads it. A block
    must cost the send, mark the lock failed, and put a human on the thread."""
    db, composio = _live(monkeypatch)
    monkeypatch.setattr(responder.llm, "review_reply",
                        lambda t, b, tpl: {"verdict": "block",
                                           "reason": "contradicts the renter"})
    assert _send() == "no-send:review-blocked"
    assert composio.replies == []
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "failed"
    assert doc["error"] == "review-blocked: contradicts the renter"
    assert db.doc("zillow_threads", THREAD)["state"] == ledger.NEEDS_HUMAN
    # the escalation actually left the building on the email channel
    alerts = composio.of("GMAIL_SEND_EMAIL")
    assert len(alerts) == 1
    assert alerts[0]["recipient_email"] == "alex@azfoleyhomes.com"
    assert "URGENT" in alerts[0]["subject"]


def test_a_failed_send_is_retryable_and_never_counted_as_sent(monkeypatch):
    """Composio dropped the reply. The lock must say so, so the recovery tick
    can take it over rather than the renter being dropped silently."""
    db, composio = _live(monkeypatch)
    monkeypatch.setattr(responder.gm, "composio_execute",
                        _raise_on(composio, "GMAIL_REPLY_TO_THREAD",
                                  RuntimeError("Composio 502")))
    assert _send() == "skipped:send-failed"
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "failed"
    assert "502" in doc["error"]
    assert "sent_at" not in doc
    assert db.doc("zillow_threads", THREAD) is None  # nothing claimed as replied


def test_label_failure_queues_the_label_and_never_resends(monkeypatch):
    """A label API failure used to look like a failed send. The email is out;
    a resend would be a duplicate, so the labels park for the cron instead."""
    db, composio = _live(monkeypatch)
    monkeypatch.setattr(responder.gm, "composio_execute",
                        _raise_on(composio, "GMAIL_MODIFY_THREAD_LABELS",
                                  RuntimeError("Gmail 429")))
    assert _send() == "sent"
    assert len(composio.replies) == 1
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "sent"
    assert doc["labels_pending"] is True
    assert doc["labels_add"] == LABELS_ADD
    assert _send() == "skipped:already-sent"
    assert len(composio.replies) == 1


# ---------------------------------------------------------------- recovery

def test_recovery_backfills_instead_of_re_emailing_what_alex_already_answered(monkeypatch):
    """Stale reservation + Alex replied by hand after the trigger message =>
    the reply exists. Record it, do not send a second one."""
    db, composio = _live(monkeypatch)
    composio.threads = {THREAD: THREAD_MSGS[THREAD] + [
        alex_msg("1a0546000000000f", "Hi Owen, 10:30 tomorrow works.")]}
    ledger.reserve_send(THREAD, STAGE, dict(META))
    db.data["zillow_sends"][KEY]["reserved_at"] = _stale()
    assert _send() == "skipped:recovered-already-sent"
    assert composio.replies == []
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "sent"
    assert doc["recovered"] == "backfilled-from-gmail"


def test_recovery_sends_when_the_thread_shows_no_reply_went_out(monkeypatch):
    """Same stale reservation, but nobody answered the renter. Take the lock
    over and finish the job - this is the strand the renter was left in."""
    db, composio = _live(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, dict(META))
    db.data["zillow_sends"][KEY]["reserved_at"] = _stale()
    assert _send() == "sent"
    assert [r["message_body"] for r in composio.replies] == [BODY]
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "sent"
    assert doc["attempts"] == 2  # the takeover counted as a second attempt


def _stale():
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc)
            - timedelta(minutes=ledger.RESERVE_TTL_MIN + 5))


def _raise_on(composio, slug, exc):
    def _exec(tool_slug, arguments):
        if tool_slug == slug:
            composio.calls.append((tool_slug, arguments))
            raise exc
        return composio(tool_slug, arguments)
    return _exec
