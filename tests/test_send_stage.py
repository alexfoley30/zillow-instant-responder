"""Coverage for responder.send_stage - the ONLY function that sends renter email.

Every other test in this suite monkeypatches send_stage away and asserts on
what it was handed, so the arbitration the real function performs
(reserve -> Gmail-check -> takeover -> pre-send review -> send -> label) has
never run under test. Both failure modes it exists to prevent cost something
real: a second copy of a reply the renter already read, and a renter stranded
because a label failure was mistaken for a send failure.

Nothing here stubs send_stage. Firestore and Composio are replaced at their
module boundaries; the send arbitration itself is the production code.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import ledger  # noqa: E402
import responder  # noqa: E402
import templates as T  # noqa: E402

THREAD = "1a0504f3183c6691"
TRIGGER = "1a0504f3183c66a2"
RELAY = "reply+xk29qb@convo.zillow.com"
ADDRESS = "2118 S El Marino Dr, Mesa, AZ 85202"
BODY = T.availability_ask("Jessica", ADDRESS)

RENTER_MSG = {
    "sender": "Jessica <reply+xk29qb@convo.zillow.com>",
    "messageId": TRIGGER,
    "messageText": "Jessica says: Hi! Is this still available? I'd love to see it.",
}
ALEX_MSG = {
    "sender": "Alex Foley <alex@azfoleyhomes.com>",
    "messageId": "1a0505037ab4ba48",
    "messageText": "Hi Jessica, thanks for reaching out about 2118 S El Marino Dr!",
}


class _Harness:
    """Records every side effect send_stage can have. Nothing leaves the process."""

    def __init__(self, monkeypatch, reserve="acquired", takeover=True,
                 msgs=None, review=None, send_error=None, label_error=None):
        self.reserve_verdict = reserve
        self.takeover_ok = takeover
        self.msgs = list(msgs if msgs is not None else [RENTER_MSG])
        self.review = review or {"verdict": "ok"}
        self.send_error = send_error
        self.label_error = label_error
        self.reserved, self.takeovers, self.shadows = [], [], []
        self.sent, self.failed, self.upserts = [], [], []
        self.transitions, self.labels_pending = [], []
        self.emails, self.label_calls, self.pokes, self.alerts = [], [], [], []

        monkeypatch.setenv("DRY_RUN", "false")
        led, gm = responder.ledger, responder.gm
        monkeypatch.setattr(led, "reserve_send", self._reserve)
        monkeypatch.setattr(led, "takeover_send", self._takeover)
        monkeypatch.setattr(led, "write_shadow",
                            lambda t, s, p: self.shadows.append((t, s, p)))
        monkeypatch.setattr(led, "mark_sent",
                            lambda t, s, **f: self.sent.append((t, s, f)))
        monkeypatch.setattr(led, "mark_failed",
                            lambda t, s, e: self.failed.append((t, s, e)))
        monkeypatch.setattr(led, "set_labels_pending",
                            lambda t, s, a, r: self.labels_pending.append((t, s, a, r)))
        monkeypatch.setattr(led, "upsert_thread",
                            lambda t, **f: self.upserts.append((t, f)))
        monkeypatch.setattr(led, "transition",
                            lambda t, st, **f: self.transitions.append((t, st, f)))
        monkeypatch.setattr(led, "get_thread", lambda t: {
            "renter_name": "Jessica", "property_address": ADDRESS})
        monkeypatch.setattr(gm, "fetch_thread", lambda t: list(self.msgs))
        monkeypatch.setattr(gm, "send_reply", self._send)
        monkeypatch.setattr(gm, "modify_labels", self._labels)
        monkeypatch.setattr(gm, "poke_ping",
                            lambda m: self.pokes.append(m) or True)
        monkeypatch.setattr(gm, "alert_email",
                            lambda s, b: self.alerts.append((s, b)) or True)
        monkeypatch.setattr(responder.llm, "review_reply",
                            lambda transcript, body, template: dict(self.review))

    def _reserve(self, thread_id, stage_key, meta):
        self.reserved.append((thread_id, stage_key, dict(meta)))
        return self.reserve_verdict

    def _takeover(self, thread_id, stage_key):
        self.takeovers.append((thread_id, stage_key))
        return self.takeover_ok

    def _send(self, thread_id, relay, body):
        if self.send_error:
            raise RuntimeError(self.send_error)
        self.emails.append((thread_id, relay, body))
        return {"data": {"id": "sent1"}}

    def _labels(self, thread_id, add, remove):
        if self.label_error:
            raise RuntimeError(self.label_error)
        self.label_calls.append((thread_id, list(add), list(remove)))
        return {"data": {}}


def _run(**kw):
    return responder.send_stage(
        THREAD, "first_reply", RELAY, BODY,
        [responder.AWAITING_LABEL], [responder.NEEDS_REPLY_LABEL],
        {"template": "availability_ask"}, TRIGGER, **kw)


# ------------------------------------------------- dry run

def test_dry_run_shadows_the_exact_body_and_never_emails(monkeypatch):
    """DRY_RUN must not even reserve the lock: the shadow ledger is the whole
    output, and it has to carry the body verbatim or the soak proves nothing."""
    h = _Harness(monkeypatch)
    monkeypatch.setenv("DRY_RUN", "true")

    assert _run() == "shadowed"
    assert h.emails == []
    assert h.reserved == []

    (thread_id, stage_key, payload), = h.shadows
    assert (thread_id, stage_key) == (THREAD, "first_reply")
    assert payload["would_body"] == BODY
    assert payload["would_labels_add"] == [responder.AWAITING_LABEL]
    assert payload["would_labels_remove"] == [responder.NEEDS_REPLY_LABEL]
    assert payload["to_relay"] == RELAY
    assert payload["trigger_message_id"] == TRIGGER
    assert payload["body_sha256"] == ledger.content_hash(BODY)


# ------------------------------------------------- lock loss

def test_already_sent_stage_never_sends_a_second_copy(monkeypatch):
    h = _Harness(monkeypatch, reserve="already-sent")
    assert _run() == "skipped:already-sent"
    assert h.emails == []
    assert h.takeovers == []
    assert h.label_calls == []


def test_in_flight_reservation_yields_to_the_other_worker(monkeypatch):
    h = _Harness(monkeypatch, reserve="in-flight")
    assert _run() == "skipped:in-flight"
    assert h.emails == []
    assert h.takeovers == []


# ------------------------------------------------- recovery

def test_recover_backfills_instead_of_resending_when_gmail_shows_a_reply(monkeypatch):
    """A stale `reserved` doc whose reply actually went out: Gmail is the
    source of truth, so the doc is backfilled and the renter hears nothing
    twice."""
    h = _Harness(monkeypatch, reserve="recover", msgs=[RENTER_MSG, ALEX_MSG])

    assert _run() == "skipped:recovered-already-sent"
    assert h.emails == []
    assert h.takeovers == []
    assert h.sent == [(THREAD, "first_reply",
                       {"recovered": "backfilled-from-gmail"})]


def test_recover_takes_over_and_sends_once_when_gmail_shows_no_reply(monkeypatch):
    """Same stale doc, but the reply never landed: take the lock over and send
    exactly one copy."""
    h = _Harness(monkeypatch, reserve="recover", msgs=[RENTER_MSG])

    assert _run() == "sent"
    assert h.takeovers == [(THREAD, "first_reply")]
    assert h.emails == [(THREAD, RELAY, BODY)]
    assert h.sent == [(THREAD, "first_reply", {})]


def test_recover_sends_nothing_when_the_takeover_is_lost(monkeypatch):
    h = _Harness(monkeypatch, reserve="recover", takeover=False, msgs=[RENTER_MSG])

    assert _run() == "skipped:takeover-lost"
    assert h.emails == []
    assert h.failed == []


# ------------------------------------------------- pre-send review

def test_review_block_stops_the_send_and_escalates_urgently(monkeypatch):
    """The review gate is the last thing between a wrong reply and the renter.
    A block must cost an email, not just a log line."""
    h = _Harness(monkeypatch, review={
        "verdict": "block",
        "reason": "offers a time the renter already declined"})

    assert _run() == "no-send:review-blocked"
    assert h.emails == []

    (_, _, error), = h.failed
    assert error == ("review-blocked: offers a time the renter already declined")
    assert [state for _, state, _ in h.transitions] == [ledger.NEEDS_HUMAN]

    poke, = h.pokes
    assert poke.startswith("URGENT same-day: Jessica — 2118 S El Marino Dr")
    # the escalation line is clipped to 80 chars, so the reason arrives cut off
    # but still names the blocked template and the review that stopped it
    assert "outgoing availability_ask blocked by pre-send review" in poke
    assert f"[zt:{THREAD}]" in poke
    assert h.alerts and "2118 S El Marino Dr" in h.alerts[0][0]


# ------------------------------------------------- send + label outcomes

def test_successful_send_records_the_template_for_the_next_turn(monkeypatch):
    h = _Harness(monkeypatch)

    assert _run() == "sent"
    assert h.emails == [(THREAD, RELAY, BODY)]
    assert h.label_calls == [(THREAD, [responder.AWAITING_LABEL],
                              [responder.NEEDS_REPLY_LABEL])]
    assert h.upserts == [(THREAD, {"last_reply_template": "availability_ask"})]
    # the lock meta carries the hash of what actually went out
    assert h.reserved[0][2]["body_sha256"] == ledger.content_hash(BODY)


def test_label_failure_queues_a_retry_and_never_resends_the_email(monkeypatch):
    """Labeling is bookkeeping. Treating its failure as a send failure is how
    a renter gets the same email twice."""
    h = _Harness(monkeypatch, label_error="503 label service unavailable")

    assert _run() == "sent"
    assert h.emails == [(THREAD, RELAY, BODY)]
    assert h.failed == []
    assert h.labels_pending == [(THREAD, "first_reply",
                                 [responder.AWAITING_LABEL],
                                 [responder.NEEDS_REPLY_LABEL])]


def test_send_failure_marks_the_doc_failed_and_leaves_it_recoverable(monkeypatch):
    """The failed doc is what the cron recovery pass and the content-hash
    strand exemption both key off, so the error has to be written down."""
    h = _Harness(monkeypatch, send_error="Composio 500 on GMAIL_REPLY_TO_THREAD")

    assert _run() == "skipped:send-failed"
    assert h.emails == []
    assert h.sent == []
    assert h.label_calls == []
    (thread_id, stage_key, error), = h.failed
    assert (thread_id, stage_key) == (THREAD, "first_reply")
    assert "Composio 500 on GMAIL_REPLY_TO_THREAD" in error
