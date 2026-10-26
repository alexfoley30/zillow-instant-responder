"""responder.send_stage - the only function that sends renter email.

Every duplicate-reply incident in this pipeline's history is a send that went
out without winning the Firestore lock, and every stranded renter is a send
recorded as sent that never left the building. send_stage decides both, and
nothing tested it: all eleven existing suites replace it with a stub, so the
real verdict handling, the real review-block escalation, and the real
"a label failure must never resend" ordering were never exercised once.

These call the real function with real templates and assert on what actually
came out - whether gm.send_reply ran, what the ledger recorded, and the
string the caller gets back.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import ledger  # noqa: E402
import responder  # noqa: E402
import templates as T  # noqa: E402

RELAY = "reply+1a16eec59561deee@convo.zillow.com"
THREAD = "1a16eec59561deee"
TRIGGER = "1a16ef0b2c441f90"
ADDR = "1018 E Marigold Ln, Gilbert, AZ 85298"
BODY = T.availability_ask("Ashley", ADDR)

# Composio GMAIL_FETCH_MESSAGE_BY_THREAD_ID shape (messageId/sender/
# messageText), carrying the real first-inquiry chrome that extract_renter_text
# cuts on ("<Name> says:" ... "Reply to <Name>").
RENTER_SPOKE_LAST = [
    {"messageId": TRIGGER, "sender": f"Ashley <{RELAY}>",
     "messageText": ("Ashley Reyes says:\n"
                     "I'd love to tour on Monday if there is availability.\n"
                     "Reply to Ashley\nSend application")},
]
ALEX_ALREADY_ANSWERED = RENTER_SPOKE_LAST + [
    {"messageId": "1a16ef5513c0aa21", "sender": "Alex Foley <alex@azfoleyhomes.com>",
     "messageText": "Hi Ashley, what time on Monday works for you?"},
]

THREAD_DOC = {"state": ledger.ACKED, "renter_name": "Ashley",
              "property_address": ADDR, "relay_email": RELAY}


class Pipeline:
    """Records every side effect send_stage can have. The lock verdict, the
    review verdict and the two failure injections are the knobs; everything
    else is the real production code path."""

    def __init__(self, monkeypatch, verdict="acquired", takeover=True,
                 review=None, send_error=None, label_error=None,
                 msgs=None, dry=False):
        self.verdict = verdict
        self.takeover = takeover
        self.review = review
        self.send_error = send_error
        self.label_error = label_error
        self.msgs = RENTER_SPOKE_LAST if msgs is None else msgs
        self.reserved, self.takeovers = [], 0
        self.mail, self.labels, self.labels_pending = [], [], []
        self.marked_sent, self.marked_failed = [], []
        self.thread_patches, self.transitions, self.shadow = [], [], []
        self.reviewed, self.pokes, self.alerts = [], [], []

        monkeypatch.setattr(responder, "dry_run", lambda: dry)
        monkeypatch.setattr(responder.ledger, "reserve_send", self._reserve)
        monkeypatch.setattr(responder.ledger, "takeover_send", self._takeover)
        monkeypatch.setattr(responder.ledger, "mark_sent", self._mark_sent)
        monkeypatch.setattr(responder.ledger, "mark_failed", self._mark_failed)
        monkeypatch.setattr(responder.ledger, "set_labels_pending",
                            lambda t, s, add, rm: self.labels_pending.append((t, s, add, rm)))
        monkeypatch.setattr(responder.ledger, "upsert_thread", self._upsert)
        monkeypatch.setattr(responder.ledger, "transition", self._transition)
        monkeypatch.setattr(responder.ledger, "write_shadow",
                            lambda t, s, payload: self.shadow.append((t, s, payload)))
        monkeypatch.setattr(responder.ledger, "get_thread", lambda t: dict(THREAD_DOC))
        monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: list(self.msgs))
        monkeypatch.setattr(responder.gm, "send_reply", self._send)
        monkeypatch.setattr(responder.gm, "modify_labels", self._labels)
        monkeypatch.setattr(responder.gm, "poke_ping", self._poke)
        monkeypatch.setattr(responder.gm, "alert_email", self._alert)
        monkeypatch.setattr(responder.llm, "review_reply", self._review_reply)

    # -- ledger
    def _reserve(self, thread_id, stage_key, meta):
        self.reserved.append((thread_id, stage_key, dict(meta)))
        return self.verdict

    def _takeover(self, thread_id, stage_key):
        self.takeovers += 1
        return self.takeover

    def _mark_sent(self, thread_id, stage_key, **fields):
        self.marked_sent.append((thread_id, stage_key, fields))

    def _mark_failed(self, thread_id, stage_key, error):
        self.marked_failed.append((thread_id, stage_key, error))

    def _upsert(self, thread_id, **fields):
        self.thread_patches.append(fields)

    def _transition(self, thread_id, state, **fields):
        self.transitions.append((state, fields))

    # -- gmail
    def _send(self, thread_id, relay, body):
        if self.send_error:
            raise RuntimeError(self.send_error)
        self.mail.append((thread_id, relay, body))
        return {"successful": True}

    def _labels(self, thread_id, add, remove):
        if self.label_error:
            raise RuntimeError(self.label_error)
        self.labels.append((add, remove))
        return {"successful": True}

    def _poke(self, msg):
        self.pokes.append(msg)
        return True

    def _alert(self, subject, body):
        self.alerts.append((subject, body))
        return True

    # -- review gate
    def _review_reply(self, transcript, outgoing, template, now_phx=None):
        self.reviewed.append((transcript, outgoing, template))
        return self.review or {"verdict": "send", "reason": ""}


def first_reply(pipe, body=BODY, template="availability_ask"):
    """The real first-reply call shape: in-thread reply + the awaiting label."""
    return responder.send_stage(
        THREAD, "first_reply", RELAY, body,
        [responder.AWAITING_LABEL], [responder.NEEDS_REPLY_LABEL],
        {"template": template}, TRIGGER)


# ------------------------------------------------------------- the send

def test_send_delivers_once_and_stamps_the_lock_with_the_body_hash(monkeypatch):
    pipe = Pipeline(monkeypatch)
    assert first_reply(pipe) == "sent"
    assert pipe.mail == [(THREAD, RELAY, BODY)]
    # The lock doc carries what was sent, so a recovery can tell copies apart.
    _, stage, meta = pipe.reserved[0]
    assert stage == "first_reply"
    assert meta["body_sha256"] == ledger.content_hash(BODY)
    assert meta["to_relay"] == RELAY
    assert meta["trigger_message_id"] == TRIGGER
    assert meta["template"] == "availability_ask"
    assert pipe.marked_sent == [(THREAD, "first_reply", {})]
    assert pipe.labels == [([responder.AWAITING_LABEL], [responder.NEEDS_REPLY_LABEL])]
    assert pipe.thread_patches == [{"last_reply_template": "availability_ask"}]


def test_stage_key_defaults_the_template_when_meta_omits_it(monkeypatch):
    pipe = Pipeline(monkeypatch)
    body = T.stale_nudge("Ashley", ADDR)
    assert responder.send_stage(THREAD, "nudge", RELAY, body, [], [], {}) == "sent"
    assert pipe.reserved[0][2]["template"] == "nudge"
    assert pipe.reviewed[0][2] == "nudge"


# ------------------------------------------------------------- the lock

def test_already_sent_lock_blocks_the_duplicate_copy(monkeypatch):
    pipe = Pipeline(monkeypatch, verdict="already-sent")
    assert first_reply(pipe) == "skipped:already-sent"
    assert pipe.mail == []
    assert pipe.marked_sent == []      # nothing re-stamped on a doc already sent
    assert pipe.labels == []
    assert pipe.reviewed == []         # not even a review call is spent


def test_in_flight_lock_blocks_the_concurrent_worker(monkeypatch):
    pipe = Pipeline(monkeypatch, verdict="in-flight")
    assert first_reply(pipe) == "skipped:in-flight"
    assert pipe.mail == []
    assert pipe.takeovers == 0


def test_recover_backfills_instead_of_sending_when_alex_already_answered(monkeypatch):
    pipe = Pipeline(monkeypatch, verdict="recover", msgs=ALEX_ALREADY_ANSWERED)
    assert first_reply(pipe) == "skipped:recovered-already-sent"
    assert pipe.mail == []
    assert pipe.takeovers == 0
    assert pipe.marked_sent == [
        (THREAD, "first_reply", {"recovered": "backfilled-from-gmail"})]


def test_recover_does_not_send_when_the_takeover_is_lost(monkeypatch):
    pipe = Pipeline(monkeypatch, verdict="recover", takeover=False)
    assert first_reply(pipe) == "skipped:takeover-lost"
    assert pipe.takeovers == 1
    assert pipe.mail == []
    assert pipe.marked_sent == []


def test_recover_sends_once_after_winning_the_takeover(monkeypatch):
    pipe = Pipeline(monkeypatch, verdict="recover", takeover=True)
    assert first_reply(pipe) == "sent"
    assert pipe.takeovers == 1
    assert pipe.mail == [(THREAD, RELAY, BODY)]


# ------------------------------------------------------------- review gate

def test_review_block_sends_nothing_and_escalates_as_urgent(monkeypatch):
    reason = "ignores the Monday the renter named"
    pipe = Pipeline(monkeypatch,
                    review={"verdict": "block", "reason": reason})
    assert first_reply(pipe) == "no-send:review-blocked"
    assert pipe.mail == []
    assert pipe.labels == []
    # The full reason survives on the lock doc; needs_human caps its quoted
    # note at 80 chars, so the escalation carries the head of it.
    assert pipe.marked_failed == [
        (THREAD, "first_reply", f"review-blocked: {reason}")]
    # A blocked reply becomes a human's job, not silence - and urgent, so the
    # 1/day escalation limit cannot swallow it.
    assert pipe.transitions[0][0] == ledger.NEEDS_HUMAN
    note = f"outgoing availability_ask blocked by pre-send review: {reason}"
    assert pipe.pokes == [
        f'URGENT same-day: Ashley \u2014 1018 E Marigold Ln \u2014 "{note[:80]}". '
        f"Reply with your answer and I will send it to the renter. "
        f"[zt:{THREAD}]"]
    assert pipe.alerts[0][0] == "URGENT same-day: Ashley - 1018 E Marigold Ln"
    assert note[:80] in pipe.alerts[0][1]


def test_review_reads_the_whole_thread_not_just_the_outgoing_body(monkeypatch):
    pipe = Pipeline(monkeypatch)
    assert first_reply(pipe) == "sent"
    transcript, outgoing, template = pipe.reviewed[0]
    assert transcript == ("RENTER: I'd love to tour on Monday if there is "
                          "availability.")
    assert outgoing == BODY and template == "availability_ask"


def test_alex_approved_answer_is_never_second_guessed(monkeypatch):
    """review_gate returns early for approved_answer: Alex's own words ship
    even when the reviewer would block them."""
    pipe = Pipeline(monkeypatch,
                    review={"verdict": "block", "reason": "would have blocked"})
    body = T.approved_answer("Ashley", "Yes, the yard is fully fenced.")
    assert first_reply(pipe, body=body, template="approved_answer") == "sent"
    assert pipe.reviewed == []
    assert pipe.mail == [(THREAD, RELAY, body)]


# ------------------------------------------------------------- failures

def test_send_failure_is_never_recorded_as_sent(monkeypatch):
    """The lock is the pipeline's memory. Stamping 'sent' on a send that
    raised would retire the stage forever and strand the renter in silence;
    the failure must stay recoverable."""
    pipe = Pipeline(monkeypatch, send_error="composio 503 upstream timeout")
    assert first_reply(pipe) == "skipped:send-failed"
    assert pipe.marked_sent == []
    assert len(pipe.marked_failed) == 1
    assert "503" in pipe.marked_failed[0][2]
    assert pipe.labels == []
    assert pipe.thread_patches == []


def test_label_failure_queues_a_retry_without_resending_the_email(monkeypatch):
    pipe = Pipeline(monkeypatch, label_error="GMAIL_MODIFY_THREAD_LABELS 500")
    assert first_reply(pipe) == "sent"
    assert pipe.mail == [(THREAD, RELAY, BODY)]   # exactly one copy
    assert pipe.marked_sent == [(THREAD, "first_reply", {})]
    assert pipe.labels_pending == [
        (THREAD, "first_reply",
         [responder.AWAITING_LABEL], [responder.NEEDS_REPLY_LABEL])]


def test_no_labels_requested_means_no_label_call(monkeypatch):
    pipe = Pipeline(monkeypatch)
    body = T.stale_nudge("Ashley", ADDR)
    assert responder.send_stage(THREAD, "nudge", RELAY, body, [], [],
                                {"template": "nudge"}) == "sent"
    assert pipe.labels == [] and pipe.labels_pending == []


# ------------------------------------------------------------- DRY_RUN

def test_dry_run_shadows_the_exact_body_and_takes_no_lock(monkeypatch):
    pipe = Pipeline(monkeypatch, dry=True)
    assert first_reply(pipe) == "shadowed"
    assert pipe.mail == [] and pipe.reserved == [] and pipe.labels == []
    thread_id, stage, payload = pipe.shadow[0]
    assert (thread_id, stage) == (THREAD, "first_reply")
    assert payload["would_body"] == BODY
    assert payload["would_labels_add"] == [responder.AWAITING_LABEL]
    assert payload["would_labels_remove"] == [responder.NEEDS_REPLY_LABEL]
    assert payload["body_sha256"] == ledger.content_hash(BODY)
