"""send_stage: the ONLY function that sends renter email.

Every other test in this suite monkeypatches send_stage away, so the branch
that actually decides "does a renter get this email, once?" had no coverage of
its own. The costs on the two sides are asymmetric and both real: a lost
verdict check sends a renter a second copy of a reply, and a swallowed failure
marks a send as delivered that never left the building.

What is pinned here: no send without winning the Firestore lock, the recover
path's Gmail tie-break, a review block that never reaches the renter, and the
rule that a label failure is NOT a send failure (it must never trigger a
resend).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ledger  # noqa: E402
import responder  # noqa: E402

RELAY = "reply+abc123@convo.zillow.com"
BODY = ("Hi Makynna,\n\nThanks for reaching out about 3896 E Alfalfa Dr! "
        "I'd love to get you in to see it.\n\nAlex Foley")


class _Harness:
    """Records every side effect send_stage can have instead of doing it."""

    def __init__(self, monkeypatch, verdict="acquired", dry=False,
                 review=(True, ""), send_raises=None, label_raises=None,
                 alex_replied=False, takeover=True):
        self.sends = []
        self.labels = []
        self.shadow = []
        self.marked_sent = []
        self.marked_failed = []
        self.labels_pending = []
        self.thread_patches = []
        self.escalations = []
        self.reserved = []
        self.takeovers = []

        def _send_reply(thread_id, relay, body):
            if send_raises:
                raise send_raises
            self.sends.append((thread_id, relay, body))
            return {"successful": True}

        def _modify_labels(thread_id, add, remove):
            if label_raises:
                raise label_raises
            self.labels.append((thread_id, add, remove))
            return {"successful": True}

        def _reserve(thread_id, stage_key, meta):
            self.reserved.append((thread_id, stage_key, meta))
            return verdict

        def _takeover(thread_id, stage_key):
            self.takeovers.append((thread_id, stage_key))
            return takeover

        monkeypatch.setattr(responder, "dry_run", lambda: dry)
        monkeypatch.setattr(responder, "review_gate",
                            lambda tid, body, template, msgs=None: review)
        monkeypatch.setattr(
            responder, "needs_human",
            lambda tid, name, addr, line, urgent=False:
                self.escalations.append((tid, name, addr, line, urgent)))
        monkeypatch.setattr(responder.ledger, "reserve_send", _reserve)
        monkeypatch.setattr(responder.ledger, "takeover_send", _takeover)
        monkeypatch.setattr(
            responder.ledger, "mark_sent",
            lambda tid, stage, **kw: self.marked_sent.append((tid, stage, kw)))
        monkeypatch.setattr(
            responder.ledger, "mark_failed",
            lambda tid, stage, err: self.marked_failed.append((tid, stage, err)))
        monkeypatch.setattr(
            responder.ledger, "set_labels_pending",
            lambda tid, stage, add, rm: self.labels_pending.append((tid, stage, add, rm)))
        monkeypatch.setattr(
            responder.ledger, "upsert_thread",
            lambda tid, **f: self.thread_patches.append((tid, f)))
        monkeypatch.setattr(
            responder.ledger, "write_shadow",
            lambda tid, stage, payload: self.shadow.append((tid, stage, payload)))
        monkeypatch.setattr(responder.ledger, "get_thread",
                            lambda tid: {"renter_name": "Makynna",
                                         "property_address": "3896 E Alfalfa Dr, Gilbert, AZ"})
        monkeypatch.setattr(responder.gm, "send_reply", _send_reply)
        monkeypatch.setattr(responder.gm, "modify_labels", _modify_labels)
        monkeypatch.setattr(responder.gm, "fetch_thread", lambda tid: [])
        monkeypatch.setattr(responder.gm, "alex_replied_after",
                            lambda msgs, mid: alex_replied)

    def run(self, **kw):
        kw.setdefault("thread_id", "t_alfalfa")
        kw.setdefault("stage_key", "first_reply")
        kw.setdefault("relay", RELAY)
        kw.setdefault("body", BODY)
        kw.setdefault("labels_add", ["Label_awaiting"])
        kw.setdefault("labels_remove", [])
        kw.setdefault("meta", {"template": "availability_ask"})
        kw.setdefault("trigger_message_id", "1a0504f3183c6691")
        return responder.send_stage(**kw)


# ---------------------------------------------------- dry run writes no email

def test_dry_run_shadows_the_exact_body_and_sends_nothing(monkeypatch):
    h = _Harness(monkeypatch, dry=True)
    assert h.run() == "shadowed"
    assert h.sends == []
    assert h.labels == []
    assert h.reserved == []  # the lock is never even taken in a shadow run
    (tid, stage, payload), = h.shadow
    assert (tid, stage) == ("t_alfalfa", "first_reply")
    assert payload["would_body"] == BODY
    assert payload["would_labels_add"] == ["Label_awaiting"]
    assert payload["to_relay"] == RELAY
    assert payload["template"] == "availability_ask"


# ---------------------------------------------------- the lock is the gate

@pytest.mark.parametrize("verdict", ["already-sent", "in-flight"])
def test_lost_lock_never_emails_the_renter(monkeypatch, verdict):
    h = _Harness(monkeypatch, verdict=verdict)
    assert h.run() == f"skipped:{verdict}"
    assert h.sends == []
    assert h.labels == []
    assert h.marked_sent == []
    assert h.marked_failed == []


def test_acquired_lock_sends_the_body_verbatim_to_the_relay(monkeypatch):
    h = _Harness(monkeypatch)
    assert h.run() == "sent"
    assert h.sends == [("t_alfalfa", RELAY, BODY)]
    assert h.marked_sent == [("t_alfalfa", "first_reply", {})]
    assert h.labels == [("t_alfalfa", ["Label_awaiting"], [])]
    assert h.thread_patches == [("t_alfalfa",
                                 {"last_reply_template": "availability_ask"})]


def test_meta_carries_the_body_hash_and_relay_into_the_lock(monkeypatch):
    # The lock doc is the audit trail for "what exactly went out"; a hash of
    # the wrong string makes a duplicate undetectable after the fact.
    h = _Harness(monkeypatch)
    h.run()
    (_, _, meta), = h.reserved
    assert meta["body_sha256"] == ledger.content_hash(BODY)
    assert meta["to_relay"] == RELAY
    assert meta["trigger_message_id"] == "1a0504f3183c6691"
    assert meta["template"] == "availability_ask"


def test_stage_key_is_the_default_template_label(monkeypatch):
    h = _Harness(monkeypatch)
    h.run(stage_key="leased", meta={})
    (_, _, meta), = h.reserved
    assert meta["template"] == "leased"


# ---------------------------------------------------- recover: ask Gmail

def test_recover_backfills_instead_of_resending_when_alex_already_replied(monkeypatch):
    # A stale lock doc plus a reply already on the thread means the renter HAS
    # their answer. Sending again is the duplicate this whole module exists to
    # prevent, so the lock is reconciled to 'sent' and nothing goes out.
    h = _Harness(monkeypatch, verdict="recover", alex_replied=True)
    assert h.run() == "skipped:recovered-already-sent"
    assert h.sends == []
    assert h.takeovers == []
    assert h.marked_sent == [("t_alfalfa", "first_reply",
                              {"recovered": "backfilled-from-gmail"})]


def test_recover_takes_over_a_stale_lock_and_then_sends(monkeypatch):
    h = _Harness(monkeypatch, verdict="recover", alex_replied=False, takeover=True)
    assert h.run() == "sent"
    assert h.takeovers == [("t_alfalfa", "first_reply")]
    assert h.sends == [("t_alfalfa", RELAY, BODY)]


def test_recover_losing_the_takeover_race_sends_nothing(monkeypatch):
    h = _Harness(monkeypatch, verdict="recover", alex_replied=False, takeover=False)
    assert h.run() == "skipped:takeover-lost"
    assert h.sends == []
    assert h.marked_sent == []


# ---------------------------------------------------- review gate

def test_review_block_stops_the_email_and_escalates_urgently(monkeypatch):
    h = _Harness(monkeypatch, review=(False, "repeats the 3:15 offer she declined"))
    assert h.run() == "no-send:review-blocked"
    assert h.sends == []
    assert h.labels == []
    assert h.marked_sent == []
    (tid, stage, err), = h.marked_failed
    assert (tid, stage) == ("t_alfalfa", "first_reply")
    assert err == "review-blocked: repeats the 3:15 offer she declined"
    (_, name, addr, line, urgent), = h.escalations
    assert name == "Makynna"
    assert addr.startswith("3896 E Alfalfa Dr")
    assert "availability_ask" in line and "repeats the 3:15 offer" in line
    assert urgent is True


# ---------------------------------------------------- send failure

def test_send_failure_is_recorded_and_never_marked_sent(monkeypatch):
    h = _Harness(monkeypatch, send_raises=RuntimeError("composio 400 invalid thread_id"))
    assert h.run() == "skipped:send-failed"
    assert h.marked_sent == []
    assert h.labels == []          # no labels on a thread that got no reply
    assert h.thread_patches == []  # and no last_reply_template either
    (tid, stage, err), = h.marked_failed
    assert (tid, stage) == ("t_alfalfa", "first_reply")
    assert "composio 400 invalid thread_id" in err


# ---------------------------------------------------- labels are not the send

def test_label_failure_queues_a_retry_and_stays_sent(monkeypatch):
    # The renter has the email. Re-running the stage to fix a label would
    # re-send it, so the label work is queued on the send doc instead and the
    # stage still reports 'sent'.
    h = _Harness(monkeypatch, label_raises=RuntimeError("Gmail 503"))
    assert h.run(labels_add=["Label_awaiting"],
                 labels_remove=["Label_needs_reply"]) == "sent"
    assert h.sends == [("t_alfalfa", RELAY, BODY)]
    assert h.marked_sent == [("t_alfalfa", "first_reply", {})]
    assert h.labels_pending == [("t_alfalfa", "first_reply",
                                 ["Label_awaiting"], ["Label_needs_reply"])]
    assert h.marked_failed == []


def test_no_labels_requested_means_no_label_call(monkeypatch):
    h = _Harness(monkeypatch)
    assert h.run(labels_add=[], labels_remove=[]) == "sent"
    assert h.labels == []
    assert h.labels_pending == []


def test_bookkeeping_failure_after_the_send_does_not_mask_the_send(monkeypatch):
    # upsert_thread is explicitly best-effort: the renter already has the
    # email, so a Firestore hiccup here must not turn 'sent' into an error.
    h = _Harness(monkeypatch)

    def _boom(tid, **f):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(responder.ledger, "upsert_thread", _boom)
    assert h.run() == "sent"
    assert h.sends == [("t_alfalfa", RELAY, BODY)]
    assert h.labels == [("t_alfalfa", ["Label_awaiting"], [])]
