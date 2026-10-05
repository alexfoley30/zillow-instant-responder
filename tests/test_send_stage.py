"""responder.send_stage — "the ONLY function that sends renter email."

The existing suite stubs send_stage out everywhere it appears, so the
function that decides whether a renter gets an email, no email, or two
emails had no tests of its own. What is locked down here is the behaviour
the service's whole duplicate-send design rests on (responder.py header:
"No lock, no send. Ever."):

  * every losing lock verdict sends NOTHING;
  * a blocked pre-send review sends nothing and escalates instead;
  * a send that throws is recorded failed and never recorded sent (a
    false 'sent' is what strands a renter forever - see the content-hash
    gate's 8/29 incident);
  * a LABEL failure after a successful send never produces a second email.

Bodies are the real templates, and the assertions are on what the renter
actually receives plus what lands in the send lock's audit fields.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import ledger  # noqa: E402
import responder  # noqa: E402
import templates as T  # noqa: E402

THREAD = "1a0504f3183c6691"
RELAY = "madison.k.3x9f@convo.zillow.com"
ADDRESS = "1110 E Redfield Rd, Tempe, AZ, 85283"
TRIGGER = "1a0545afd0fe9807"
BODY = T.availability_ask("Madison", ADDRESS)


class _Harness:
    """Record everything send_stage can do to the outside world."""

    def __init__(self, monkeypatch, verdict="acquired", dry=False,
                 takeover=True, alex_replied=False, review=(True, ""),
                 send_raises=None, label_raises=None):
        self.emails = []
        self.labels = []
        self.marked_sent = []
        self.marked_failed = []
        self.labels_pending = []
        self.shadow = []
        self.escalations = []
        self.thread_patches = []
        self.reserved = []
        self.takeover_calls = 0
        self._verdict = verdict
        self._takeover = takeover

        def _reserve(thread_id, stage_key, meta):
            self.reserved.append((thread_id, stage_key, dict(meta)))
            return self._verdict

        def _takeover_send(thread_id, stage_key):
            self.takeover_calls += 1
            return self._takeover

        def _send_reply(thread_id, relay, body):
            if send_raises:
                raise send_raises
            self.emails.append((thread_id, relay, body))
            return {"successful": True}

        def _modify_labels(thread_id, add, remove):
            if label_raises:
                raise label_raises
            self.labels.append((thread_id, list(add), list(remove)))
            return {"successful": True}

        monkeypatch.setattr(responder, "dry_run", lambda: dry)
        monkeypatch.setattr(responder, "review_gate",
                            lambda tid, body, tpl, msgs=None: review)
        monkeypatch.setattr(
            responder, "needs_human",
            lambda tid, name, addr, line, urgent=False:
                self.escalations.append((tid, name, addr, line, urgent)))
        monkeypatch.setattr(responder.ledger, "reserve_send", _reserve)
        monkeypatch.setattr(responder.ledger, "takeover_send", _takeover_send)
        monkeypatch.setattr(
            responder.ledger, "mark_sent",
            lambda tid, stage, **f: self.marked_sent.append((tid, stage, f)))
        monkeypatch.setattr(
            responder.ledger, "mark_failed",
            lambda tid, stage, err: self.marked_failed.append((tid, stage, err)))
        monkeypatch.setattr(
            responder.ledger, "set_labels_pending",
            lambda tid, stage, add, rm:
                self.labels_pending.append((tid, stage, list(add), list(rm))))
        monkeypatch.setattr(
            responder.ledger, "upsert_thread",
            lambda tid, **f: self.thread_patches.append((tid, f)))
        monkeypatch.setattr(
            responder.ledger, "write_shadow",
            lambda tid, stage, payload:
                self.shadow.append((tid, stage, dict(payload))))
        monkeypatch.setattr(
            responder.ledger, "get_thread",
            lambda tid: {"renter_name": "Madison", "property_address": ADDRESS})
        monkeypatch.setattr(responder.gm, "send_reply", _send_reply)
        monkeypatch.setattr(responder.gm, "modify_labels", _modify_labels)
        monkeypatch.setattr(responder.gm, "fetch_thread", lambda tid: [])
        monkeypatch.setattr(responder.gm, "alex_replied_after",
                            lambda msgs, mid: alex_replied)

    def run(self, stage_key="first_reply", body=BODY, template="availability_ask"):
        return responder.send_stage(
            THREAD, stage_key, RELAY, body,
            [responder.AWAITING_LABEL], [responder.NEEDS_REPLY_LABEL],
            {"template": template}, TRIGGER)


# ------------------------------------------------------------- the happy path

def test_acquired_lock_sends_exactly_one_email_then_labels(monkeypatch):
    h = _Harness(monkeypatch)
    assert h.run() == "sent"
    assert h.emails == [(THREAD, RELAY, BODY)]
    assert "Madison" in BODY and "1110 E Redfield Rd" in BODY  # real template
    assert h.labels == [(THREAD, [responder.AWAITING_LABEL],
                         [responder.NEEDS_REPLY_LABEL])]
    assert h.marked_sent == [(THREAD, "first_reply", {})]
    assert h.marked_failed == []


def test_lock_meta_records_the_body_hash_and_relay(monkeypatch):
    # The audit trail is the only way to tell later WHICH words went out.
    h = _Harness(monkeypatch)
    h.run()
    _, stage, meta = h.reserved[0]
    assert stage == "first_reply"
    assert meta["body_sha256"] == ledger.content_hash(BODY)
    assert meta["to_relay"] == RELAY
    assert meta["trigger_message_id"] == TRIGGER
    assert meta["template"] == "availability_ask"


def test_template_defaults_to_the_stage_key_when_meta_omits_it(monkeypatch):
    h = _Harness(monkeypatch)
    assert responder.send_stage(THREAD, "leased", RELAY, BODY, [], [], {},
                                TRIGGER) == "sent"
    assert h.reserved[0][2]["template"] == "leased"


# --------------------------------------------------- losing the lock: no send

@pytest.mark.parametrize("verdict,expected", [
    ("already-sent", "skipped:already-sent"),
    ("in-flight", "skipped:in-flight"),
])
def test_lost_lock_sends_nothing(monkeypatch, verdict, expected):
    h = _Harness(monkeypatch, verdict=verdict)
    assert h.run() == expected
    assert h.emails == []
    assert h.labels == []
    assert h.marked_sent == [] and h.marked_failed == []


def test_recover_defers_to_gmail_when_alex_already_replied(monkeypatch):
    # Ambiguity resolves against the thread itself: Alex answered by hand,
    # so the pipeline must not add its own reply underneath his.
    h = _Harness(monkeypatch, verdict="recover", alex_replied=True)
    assert h.run() == "skipped:recovered-already-sent"
    assert h.emails == []
    assert h.takeover_calls == 0
    assert h.marked_sent == [(THREAD, "first_reply",
                              {"recovered": "backfilled-from-gmail"})]


def test_recover_losing_the_takeover_sends_nothing(monkeypatch):
    h = _Harness(monkeypatch, verdict="recover", takeover=False)
    assert h.run() == "skipped:takeover-lost"
    assert h.emails == []
    assert h.takeover_calls == 1


def test_recover_winning_the_takeover_sends_once(monkeypatch):
    h = _Harness(monkeypatch, verdict="recover", takeover=True)
    assert h.run() == "sent"
    assert h.emails == [(THREAD, RELAY, BODY)]
    assert h.takeover_calls == 1


# ------------------------------------------------------- pre-send review block

def test_review_block_sends_nothing_and_escalates_urgently(monkeypatch):
    h = _Harness(monkeypatch, review=(False, "contradicts the renter's 6pm"))
    assert h.run() == "no-send:review-blocked"
    assert h.emails == []
    assert h.labels == []
    assert h.marked_sent == []
    tid, stage, err = h.marked_failed[0]
    assert (tid, stage) == (THREAD, "first_reply")
    assert "review-blocked" in err and "contradicts the renter's 6pm" in err
    tid, name, addr, line, urgent = h.escalations[0]
    assert (tid, name, addr, urgent) == (THREAD, "Madison", ADDRESS, True)
    assert "availability_ask" in line and "contradicts the renter's 6pm" in line


# ------------------------------------------------------------ send throws

def test_failed_send_is_never_recorded_as_sent(monkeypatch):
    # A false 'sent' is what leaves a renter permanently unanswered: the
    # content-hash gate's strand exemption keys off this failure outcome.
    h = _Harness(monkeypatch, send_raises=RuntimeError("502 Composio upstream"))
    assert h.run() == "skipped:send-failed"
    assert h.emails == []
    assert h.marked_sent == []
    assert h.labels == []
    tid, stage, err = h.marked_failed[0]
    assert (tid, stage) == (THREAD, "first_reply")
    assert "502 Composio upstream" in err


# ------------------------------- label failure must never cause a second email

def test_label_failure_keeps_the_send_and_queues_a_retry(monkeypatch):
    h = _Harness(monkeypatch, label_raises=RuntimeError("403 label modify"))
    assert h.run() == "sent"
    assert h.emails == [(THREAD, RELAY, BODY)], "exactly one email, no resend"
    assert h.marked_sent == [(THREAD, "first_reply", {})]
    assert h.marked_failed == [], "a label miss is not a send failure"
    assert h.labels_pending == [(THREAD, "first_reply",
                                 [responder.AWAITING_LABEL],
                                 [responder.NEEDS_REPLY_LABEL])]


def test_bookkeeping_failure_after_send_does_not_resend(monkeypatch):
    h = _Harness(monkeypatch)
    monkeypatch.setattr(
        responder.ledger, "upsert_thread",
        lambda tid, **f: (_ for _ in ()).throw(RuntimeError("503 Firestore")))
    assert h.run() == "sent"
    assert h.emails == [(THREAD, RELAY, BODY)]
    assert h.labels == [(THREAD, [responder.AWAITING_LABEL],
                         [responder.NEEDS_REPLY_LABEL])]


def test_no_labels_requested_skips_the_label_call(monkeypatch):
    h = _Harness(monkeypatch)
    assert responder.send_stage(THREAD, "first_reply", RELAY, BODY, [], [],
                                {"template": "availability_ask"},
                                TRIGGER) == "sent"
    assert h.emails == [(THREAD, RELAY, BODY)]
    assert h.labels == []


# ------------------------------------------------------------------ DRY_RUN

def test_dry_run_mirrors_the_send_without_touching_the_lock(monkeypatch):
    h = _Harness(monkeypatch, dry=True)
    assert h.run() == "shadowed"
    assert h.emails == [] and h.labels == []
    assert h.reserved == [], "DRY_RUN must not consume the send lock"
    tid, stage, payload = h.shadow[0]
    assert (tid, stage) == (THREAD, "first_reply")
    assert payload["would_body"] == BODY
    assert payload["would_labels_add"] == [responder.AWAITING_LABEL]
    assert payload["would_labels_remove"] == [responder.NEEDS_REPLY_LABEL]
    assert payload["body_sha256"] == ledger.content_hash(BODY)
