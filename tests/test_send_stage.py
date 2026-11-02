#!/usr/bin/env python3
"""responder.send_stage - the ONLY function that emails a renter.

Nothing covered this. Every other test in the suite monkeypatches send_stage
away to get at the logic around it, so the function that actually decides
whether a renter receives mail ran unasserted. What it gets wrong is
expensive in both directions:

  * send twice  - the duplicate-reply class of bug this whole service was
    rebuilt to kill (see responder.py's module docstring). The Firestore
    lock verdict is the only thing standing between a double webhook
    delivery and two emails.
  * send never  - a renter sits unanswered. A failed send recorded as
    `sent` is invisible to cron._recover_stuck, so the strand is permanent.
  * send anyway - a review-blocked body, or one whose label write failed,
    must not be re-sent on the next tick.

The thread fixture is real Zillow relay mail (same shape as test_extract.py)
so review_gate builds its transcript through the production
gm.msg_body -> gm.extract_renter_text -> gm.thread_transcript chain rather
than from a hand-written string.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger  # noqa: E402
import responder  # noqa: E402
import templates as T  # noqa: E402

ADDRESS = "1018 E Marigold Ln, San Tan Valley, AZ 85140"
RELAY = "reply+4ekrkfrk3z20p@convo.zillow.com"
THREAD = "1a16eec59561deee"
TRIGGER_ID = "1a16eec59561deee"

# Real relay shape: renter words after "<Name> says:", Zillow chrome after.
INQUIRY_HTML = (
    '<div><a href="https://zillow.com/r/58y56j72rjw54"><img alt="Brand logo">'
    "</img></a></div><div>New message</div>"
    '<div><span style="font-weight:bold">Ashley</span> says:</div>'
    "<div>I&#39;d love to tour on Monday if there is availability.</div>"
    '<div><a href="https://zillow.com/rental-manager/inbox/">Reply to Ashley</a>'
    "</div><div>You can also reply directly to this email</div>"
    "<div>Have questions or need help?</div>"
)

THREAD_MSGS = [
    {"messageId": TRIGGER_ID,
     "sender": "Ashley <reply+4ekrkfrk3z20p@convo.zillow.com>",
     "messageText": INQUIRY_HTML},
]

# The body a real first reply carries, built by the production template.
BODY = T.availability_ask("Ashley", ADDRESS, day="Monday")


class _Recorder:
    """Every renter-affecting side effect send_stage can perform."""

    def __init__(self):
        self.emails = []          # (thread_id, relay, body)
        self.shadow = []          # (thread_id, stage_key, payload)
        self.reserved = []        # (thread_id, stage_key, meta)
        self.taken_over = []      # (thread_id, stage_key)
        self.sent = []            # (thread_id, stage_key, fields)
        self.failed = []          # (thread_id, stage_key, error)
        self.labels = []          # (thread_id, add, remove)
        self.labels_pending = []  # (thread_id, stage_key, add, remove)
        self.thread_patches = []  # (thread_id, fields)
        self.escalations = []     # (first_name, address, last_line, urgent)
        self.reviewed = []        # (transcript, body, template)


def _wire(monkeypatch, *, verdict="acquired", takeover=True, alex_replied=False,
          review=None, send_error=None, label_error=None, dry=False):
    """Point send_stage's collaborators at an in-memory recorder.

    Only I/O is replaced: ledger.content_hash, review_gate and the templates
    are the production code throughout.
    """
    rec = _Recorder()
    monkeypatch.setattr(responder, "dry_run", lambda: dry)

    monkeypatch.setattr(responder.ledger, "write_shadow",
                        lambda tid, stage, payload:
                            rec.shadow.append((tid, stage, payload)))
    monkeypatch.setattr(responder.ledger, "reserve_send",
                        lambda tid, stage, meta:
                            rec.reserved.append((tid, stage, meta)) or verdict)
    monkeypatch.setattr(responder.ledger, "takeover_send",
                        lambda tid, stage:
                            rec.taken_over.append((tid, stage)) or takeover)
    monkeypatch.setattr(responder.ledger, "mark_sent",
                        lambda tid, stage, **f: rec.sent.append((tid, stage, f)))
    monkeypatch.setattr(responder.ledger, "mark_failed",
                        lambda tid, stage, error:
                            rec.failed.append((tid, stage, error)))
    monkeypatch.setattr(responder.ledger, "set_labels_pending",
                        lambda tid, stage, add, rm:
                            rec.labels_pending.append((tid, stage, add, rm)))
    monkeypatch.setattr(responder.ledger, "upsert_thread",
                        lambda tid, **f: rec.thread_patches.append((tid, f)))
    monkeypatch.setattr(responder.ledger, "get_thread",
                        lambda tid: {"renter_name": "Ashley",
                                     "property_address": ADDRESS})

    def _send(tid, relay, body):
        if send_error:
            raise send_error
        rec.emails.append((tid, relay, body))
        return {"successful": True}

    def _labels(tid, add, rm):
        if label_error:
            raise label_error
        rec.labels.append((tid, add, rm))
        return {"successful": True}

    monkeypatch.setattr(responder.gm, "send_reply", _send)
    monkeypatch.setattr(responder.gm, "modify_labels", _labels)
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda tid: THREAD_MSGS)
    monkeypatch.setattr(responder.gm, "alex_replied_after",
                        lambda msgs, mid: alex_replied)

    def _review(transcript, body, template, now_phx=None):
        rec.reviewed.append((transcript, body, template))
        return dict(review or {"verdict": "send"})

    monkeypatch.setattr(responder.llm, "review_reply", _review)
    monkeypatch.setattr(responder, "needs_human",
                        lambda tid, name, addr, last_line, urgent=False:
                            rec.escalations.append((name, addr, last_line, urgent)))
    return rec


def _run(stage_key="first_reply", labels_add=None, labels_remove=None,
         template="availability_ask+named_day", body=BODY):
    return responder.send_stage(
        THREAD, stage_key, RELAY, body,
        [responder.AWAITING_LABEL] if labels_add is None else labels_add,
        [] if labels_remove is None else labels_remove,
        {"template": template}, TRIGGER_ID)


# ---------------------------------------------------------------- happy path

def test_won_lock_sends_the_body_verbatim_then_marks_sent_and_labels(monkeypatch):
    rec = _wire(monkeypatch)
    assert _run() == "sent"
    # Exactly one email, to the relay, carrying the template body unaltered.
    assert rec.emails == [(THREAD, RELAY, BODY)]
    assert "What time on Monday is best for you" in rec.emails[0][2]
    assert rec.sent == [(THREAD, "first_reply", {})]
    assert rec.failed == []
    assert rec.labels == [(THREAD, [responder.AWAITING_LABEL], [])]
    assert rec.thread_patches == [
        (THREAD, {"last_reply_template": "availability_ask+named_day"})]


def test_lock_meta_carries_the_body_hash_relay_and_trigger_id(monkeypatch):
    """The lock doc is the audit trail of what went out, and body_sha256 is
    what the content gates compare against. A wrong hash there makes a
    duplicate body look like a new one."""
    rec = _wire(monkeypatch)
    _run()
    tid, stage, meta = rec.reserved[0]
    assert (tid, stage) == (THREAD, "first_reply")
    assert meta["body_sha256"] == ledger.content_hash(BODY)
    assert len(meta["body_sha256"]) == 16
    assert meta["to_relay"] == RELAY
    assert meta["trigger_message_id"] == TRIGGER_ID
    assert meta["template"] == "availability_ask+named_day"


def test_stage_key_defaults_the_template_when_meta_omits_it(monkeypatch):
    rec = _wire(monkeypatch)
    assert responder.send_stage(THREAD, "leased", RELAY,
                                T.leased_reply("Ashley", ADDRESS),
                                [responder.HANDLED_LABEL], [], {},
                                TRIGGER_ID) == "sent"
    assert rec.reserved[0][2]["template"] == "leased"
    assert rec.reviewed[0][2] == "leased"


# ---------------------------------------------------------------- dry run

def test_dry_run_shadows_the_would_be_email_and_takes_no_lock(monkeypatch):
    rec = _wire(monkeypatch, dry=True)
    assert _run() == "shadowed"
    assert rec.emails == []
    assert rec.reserved == []          # never touch the live lock in DRY_RUN
    assert rec.labels == [] and rec.sent == []
    tid, stage, payload = rec.shadow[0]
    assert (tid, stage) == (THREAD, "first_reply")
    assert payload["would_body"] == BODY
    assert payload["would_labels_add"] == [responder.AWAITING_LABEL]
    assert payload["would_labels_remove"] == []
    assert payload["body_sha256"] == ledger.content_hash(BODY)
    assert payload["to_relay"] == RELAY


# ---------------------------------------------------------------- lock verdicts

def test_already_sent_lock_sends_no_second_email(monkeypatch):
    rec = _wire(monkeypatch, verdict="already-sent")
    assert _run() == "skipped:already-sent"
    assert rec.emails == []
    assert rec.sent == [] and rec.failed == [] and rec.labels == []


def test_in_flight_lock_yields_to_the_other_worker(monkeypatch):
    rec = _wire(monkeypatch, verdict="in-flight")
    assert _run() == "skipped:in-flight"
    assert rec.emails == []
    assert rec.taken_over == []        # in-flight is never stolen
    assert rec.reviewed == []          # no review spend on a skipped send


def test_recover_backfills_the_lock_when_alex_already_answered(monkeypatch):
    """Stale lock + a human reply on the thread = our send is moot. Record it
    as sent so recovery stops re-offering it; do NOT mail the renter on top
    of Alex."""
    rec = _wire(monkeypatch, verdict="recover", alex_replied=True)
    assert _run() == "skipped:recovered-already-sent"
    assert rec.emails == []
    assert rec.sent == [(THREAD, "first_reply",
                         {"recovered": "backfilled-from-gmail"})]
    assert rec.taken_over == []


def test_recover_losing_the_takeover_sends_nothing(monkeypatch):
    rec = _wire(monkeypatch, verdict="recover", alex_replied=False,
                takeover=False)
    assert _run() == "skipped:takeover-lost"
    assert rec.emails == []
    assert rec.taken_over == [(THREAD, "first_reply")]
    assert rec.sent == [] and rec.failed == []


def test_recover_winning_the_takeover_sends_once(monkeypatch):
    rec = _wire(monkeypatch, verdict="recover", alex_replied=False,
                takeover=True)
    assert _run() == "sent"
    assert rec.emails == [(THREAD, RELAY, BODY)]
    assert rec.sent == [(THREAD, "first_reply", {})]


# ---------------------------------------------------------------- review gate

def test_review_block_sends_nothing_and_escalates_with_the_reason(monkeypatch):
    rec = _wire(monkeypatch, review={
        "verdict": "block", "reason": "ignores the Monday she asked for"})
    assert _run() == "no-send:review-blocked"
    assert rec.emails == []
    assert rec.labels == []
    assert rec.failed == [(THREAD, "first_reply",
                           "review-blocked: ignores the Monday she asked for")]
    # The block is handed to Alex in his own words, not swallowed.
    name, addr, last_line, urgent = rec.escalations[0]
    assert (name, addr) == ("Ashley", ADDRESS)
    assert "ignores the Monday she asked for" in last_line
    assert urgent is True


def test_review_block_without_a_reason_still_marks_and_escalates(monkeypatch):
    rec = _wire(monkeypatch, review={"verdict": "block"})
    assert _run() == "no-send:review-blocked"
    assert rec.emails == []
    assert rec.failed == [(THREAD, "first_reply",
                           "review-blocked: review blocked")]
    assert rec.escalations and "review blocked" in rec.escalations[0][2]


def test_review_reads_the_renters_real_words_not_tag_soup(monkeypatch):
    """review_gate builds its transcript from the live Composio thread. If
    the HTML flattening regresses, the reviewer sees markup and rubber-stamps
    everything - so pin that it sees words."""
    rec = _wire(monkeypatch)
    _run()
    transcript, reviewed_body, template = rec.reviewed[0]
    assert transcript == ("RENTER: I'd love to tour on Monday if there is "
                          "availability.")
    assert "Brand logo" not in transcript and "<div" not in transcript
    assert reviewed_body == BODY
    assert template == "availability_ask+named_day"


def test_approved_answer_bypasses_the_review_gate(monkeypatch):
    """Alex's own words are never second-guessed: a blocking reviewer must not
    be able to hold his answer (responder.review_gate early return)."""
    rec = _wire(monkeypatch, review={"verdict": "block", "reason": "nope"})
    body = T.approved_answer("Ashley", "Yes, the yard is fully fenced.")
    assert responder.send_stage(THREAD, "approved__m1", RELAY, body,
                                [responder.HANDLED_LABEL], [],
                                {"template": "approved_answer"},
                                TRIGGER_ID) == "sent"
    assert rec.emails == [(THREAD, RELAY, body)]
    assert "fully fenced" in rec.emails[0][2]
    assert rec.reviewed == []
    assert rec.failed == []


# ---------------------------------------------------------------- failures

def test_send_failure_is_marked_failed_and_never_marked_sent(monkeypatch):
    """A failed send recorded as `sent` is a permanently stranded renter:
    cron._recover_stuck only ever revisits reserved/failed docs."""
    rec = _wire(monkeypatch, send_error=RuntimeError("Composio 500 upstream"))
    assert _run() == "skipped:send-failed"
    assert rec.emails == []
    assert rec.sent == []
    assert rec.failed == [(THREAD, "first_reply", "Composio 500 upstream")]
    assert rec.labels == []            # no labels on a send that never landed
    assert rec.thread_patches == []


def test_label_failure_still_counts_as_sent_and_queues_the_retry(monkeypatch):
    """The email is out. Reporting anything but `sent` here would have the
    next tick mail the renter a second copy, so the label write is queued
    instead."""
    rec = _wire(monkeypatch, label_error=RuntimeError("Gmail 503"))
    assert _run(labels_add=[responder.AWAITING_LABEL],
                labels_remove=[responder.NEEDS_REPLY_LABEL]) == "sent"
    assert rec.emails == [(THREAD, RELAY, BODY)]
    assert rec.sent == [(THREAD, "first_reply", {})]
    assert rec.failed == []
    assert rec.labels_pending == [
        (THREAD, "first_reply",
         [responder.AWAITING_LABEL], [responder.NEEDS_REPLY_LABEL])]


def test_no_labels_requested_means_no_label_call_at_all(monkeypatch):
    rec = _wire(monkeypatch, label_error=RuntimeError("would blow up if called"))
    assert _run(labels_add=[], labels_remove=[]) == "sent"
    assert rec.emails == [(THREAD, RELAY, BODY)]
    assert rec.labels_pending == []


def test_thread_bookkeeping_failure_does_not_lose_the_send(monkeypatch):
    """upsert_thread is bookkeeping. It must never turn a delivered email
    into a failure the pipeline would retry."""
    rec = _wire(monkeypatch)
    monkeypatch.setattr(responder.ledger, "upsert_thread",
                        lambda tid, **f: (_ for _ in ()).throw(
                            RuntimeError("firestore down")))
    assert _run() == "sent"
    assert rec.emails == [(THREAD, RELAY, BODY)]
    assert rec.sent == [(THREAD, "first_reply", {})]
    assert rec.labels == [(THREAD, [responder.AWAITING_LABEL], [])]
