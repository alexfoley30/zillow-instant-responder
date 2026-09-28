"""responder.send_stage - the ONLY function that puts an email in front of a
renter, and the one every other handler delegates to.

The whole suite stubbed it out, so nothing checked that it actually sends
once, records what it sent, and stays quiet when the lock says someone else
already did. What it costs when a branch is wrong is concrete: a renter gets
Alex's availability email twice, or gets nothing while Firestore claims the
reply went out.

Gmail and the review model are the only things faked here - they are network.
The lock is the real Firestore ledger logic running against an in-memory
store, so "did we send twice?" is answered by the production code path.

Run: python -m pytest tests/test_send_stage.py -q
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import fake_firestore  # noqa: E402
import ledger  # noqa: E402
import responder  # noqa: E402
import templates as T  # noqa: E402

THREAD = "199c1f2a3b4c5d6e"
STAGE = "availability_ask"
KEY = f"{THREAD}__{STAGE}"
RELAY = "t.199c1f2a3b4c5d6e@convo.zillow.com"
ADDRESS = "3309 E San Remo Ave, Gilbert, AZ 85298"

AWAITING = "Label_1717014254813700027"   # zillow/awaiting-renter
NEEDS_REPLY = "Label_2252577853408309931"

BODY = T.availability_ask("Mikayla", ADDRESS)
META = {"template": "availability_ask", "renter_name": "Mikayla",
        "property_address": ADDRESS}

RENTER_MSG = {"messageId": "199c1f2a3b4c5d6e",
              "sender": "Mikayla <t.199c1f2a3b4c5d6e@convo.zillow.com>",
              "messageText": "Hi! Is 3309 E San Remo still available?"}
ALEX_MSG = {"messageId": "199c1f2a3b4c5d70",
            "sender": "Alex Foley <alex@azfoleyhomes.com>",
            "messageText": "Hi Mikayla, yes it is - when works for a tour?"}


class Gmail:
    """Stand-in for the Composio wire. Records the real bodies and label ids
    send_stage hands it, and can be told to fail like Composio does."""

    def __init__(self, thread=(), send_error=None, label_error=None):
        self.thread = list(thread)
        self.send_error = send_error
        self.label_error = label_error
        self.sent = []       # (thread_id, relay, body)
        self.labelled = []   # (thread_id, add, remove)

    def install(self, monkeypatch):
        monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: list(self.thread))
        monkeypatch.setattr(responder.gm, "send_reply", self._send)
        monkeypatch.setattr(responder.gm, "modify_labels", self._labels)
        return self

    def _send(self, thread_id, relay, body):
        if self.send_error:
            raise self.send_error
        self.sent.append((thread_id, relay, body))
        return {"successful": True}

    def _labels(self, thread_id, add, remove):
        if self.label_error:
            raise self.label_error
        self.labelled.append((thread_id, list(add), list(remove)))
        return {"successful": True}


def setup(monkeypatch, live=True, thread=(), **gmail_kwargs):
    """Fresh ledger store + fake Gmail. Review gate passes unless a test
    overrides llm.review_reply (no API key -> review_reply fails open)."""
    monkeypatch.setenv("DRY_RUN", "false" if live else "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = fake_firestore.install(monkeypatch, ledger)
    gmail = Gmail(thread=thread, **gmail_kwargs).install(monkeypatch)
    return db, gmail


def send(**kw):
    kw.setdefault("labels_add", [AWAITING])
    kw.setdefault("labels_remove", [NEEDS_REPLY])
    kw.setdefault("meta", META)
    kw.setdefault("trigger_message_id", "199c1f2a3b4c5d6e")
    return responder.send_stage(THREAD, STAGE, RELAY, BODY, **kw)


# ---------------------------------------------------------------- dry run

def test_dry_run_mirrors_the_exact_body_and_sends_nothing(monkeypatch):
    db, gmail = setup(monkeypatch, live=False)

    assert send() == "shadowed"

    assert gmail.sent == []
    assert gmail.labelled == []
    shadow = db.doc("zillow_shadow", KEY)
    assert shadow["would_body"] == BODY
    assert "Thanks for reaching out about 3309 E San Remo Ave" in shadow["would_body"]
    assert shadow["would_labels_add"] == [AWAITING]
    assert shadow["would_labels_remove"] == [NEEDS_REPLY]
    assert shadow["to_relay"] == RELAY
    assert shadow["template"] == "availability_ask"
    # A dry run must not burn the lock, or the first live run would be mute.
    assert db.docs("zillow_sends") == {}


# ---------------------------------------------------------------- happy path

def test_live_send_delivers_the_body_and_records_what_went_out(monkeypatch):
    db, gmail = setup(monkeypatch)

    assert send() == "sent"

    assert gmail.sent == [(THREAD, RELAY, BODY)]
    assert gmail.labelled == [(THREAD, [AWAITING], [NEEDS_REPLY])]
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "sent"
    assert doc["body_sha256"] == ledger.content_hash(BODY)
    assert doc["to_relay"] == RELAY
    assert doc["trigger_message_id"] == "199c1f2a3b4c5d6e"
    assert doc.get("labels_pending") is None
    assert db.doc("zillow_threads", THREAD)["last_reply_template"] == "availability_ask"


def test_the_renter_never_gets_the_same_stage_twice(monkeypatch):
    """The whole point of the lock: a replayed webhook must be silent."""
    db, gmail = setup(monkeypatch)

    assert send() == "sent"
    assert send() == "skipped:already-sent"
    assert send() == "skipped:already-sent"

    assert len(gmail.sent) == 1
    assert gmail.sent[0][2] == BODY


def test_a_sibling_worker_mid_send_is_not_raced(monkeypatch):
    db, gmail = setup(monkeypatch)
    ledger.reserve_send(THREAD, STAGE, {"template": "availability_ask"})

    assert send() == "skipped:in-flight"
    assert gmail.sent == []
    assert db.doc("zillow_sends", KEY)["status"] == "reserved"


# ------------------------------------------------------------ recovery

def test_recovery_backfills_instead_of_resending_when_alex_already_replied(
        monkeypatch):
    """Stale lock + an Alex message after the trigger = the reply exists in
    Gmail. Close the books, do not hand the renter a second copy."""
    db, gmail = setup(monkeypatch, thread=[RENTER_MSG, ALEX_MSG])
    ledger.reserve_send(THREAD, STAGE, {"template": "availability_ask"})
    db.docs("zillow_sends")[KEY]["reserved_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=45))

    assert send() == "skipped:recovered-already-sent"

    assert gmail.sent == []
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "sent"
    assert doc["recovered"] == "backfilled-from-gmail"


def test_recovery_sends_when_the_thread_shows_the_renter_still_waiting(
        monkeypatch):
    db, gmail = setup(monkeypatch, thread=[RENTER_MSG])
    ledger.reserve_send(THREAD, STAGE, {"template": "availability_ask"})
    db.docs("zillow_sends")[KEY]["reserved_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=45))

    assert send() == "sent"

    assert gmail.sent == [(THREAD, RELAY, BODY)]
    assert db.doc("zillow_sends", KEY)["attempts"] == 2  # takeover bumped it


def test_recovery_stands_down_when_another_worker_wins_the_takeover(monkeypatch):
    db, gmail = setup(monkeypatch, thread=[RENTER_MSG])
    ledger.reserve_send(THREAD, STAGE, {"template": "availability_ask"})
    db.docs("zillow_sends")[KEY]["reserved_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=45))
    monkeypatch.setattr(responder.ledger, "takeover_send", lambda t, s: False)

    assert send() == "skipped:takeover-lost"
    assert gmail.sent == []


# ------------------------------------------------------------ review gate

def test_a_blocked_review_stops_the_email_and_escalates(monkeypatch):
    db, gmail = setup(monkeypatch, thread=[RENTER_MSG])
    ledger.create_thread(THREAD, renter_name="Mikayla", property_address=ADDRESS)
    monkeypatch.setattr(
        responder.llm, "review_reply",
        lambda transcript, body, template: {
            "verdict": "block",
            "reason": "offers a tour at a house the renter already declined"})
    pokes = []
    monkeypatch.setattr(responder.gm, "poke_ping", lambda m: pokes.append(m) or True)
    monkeypatch.setattr(responder.gm, "alert_email", lambda s, b: True)

    assert send() == "no-send:review-blocked"

    assert gmail.sent == []
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "failed"
    assert doc["error"] == ("review-blocked: offers a tour at a house the "
                            "renter already declined")
    thread_doc = db.doc("zillow_threads", THREAD)
    assert thread_doc["state"] == ledger.NEEDS_HUMAN
    assert len(pokes) == 1
    assert "Mikayla" in pokes[0] and "3309 E San Remo Ave" in pokes[0]


def test_alex_approved_words_skip_the_review_entirely(monkeypatch):
    """approved_answer is Alex's own text; a model must not veto it."""
    db, gmail = setup(monkeypatch, thread=[RENTER_MSG])

    def never(*a, **k):
        raise AssertionError("review_reply must not run on approved_answer")
    monkeypatch.setattr(responder.llm, "review_reply", never)

    body = T.approved_answer("Mikayla", "Yes, the fridge stays with the house.")
    out = responder.send_stage(THREAD, "approved_answer", RELAY, body,
                               [], [], {"template": "approved_answer"})

    assert out == "sent"
    assert gmail.sent == [(THREAD, RELAY, body)]


def test_gmail_unreachable_during_review_still_lets_the_reply_go_out(monkeypatch):
    """FAILS OPEN by design: the deterministic guards already ran, and a
    review that cannot read the thread must not become an outage."""
    db, gmail = setup(monkeypatch)
    monkeypatch.setattr(
        responder.gm, "fetch_thread",
        lambda t: (_ for _ in ()).throw(RuntimeError("composio 504 timeout")))

    assert send() == "sent"
    assert gmail.sent == [(THREAD, RELAY, BODY)]
    assert db.doc("zillow_sends", KEY)["status"] == "sent"


def test_the_review_sees_the_whole_thread_and_the_real_draft(monkeypatch):
    """A gate handed the wrong text is no gate. It must get the conversation
    so far, the exact body about to go out, and the template name."""
    db, gmail = setup(monkeypatch, thread=[RENTER_MSG, ALEX_MSG])
    seen = {}

    def capture(transcript, outgoing_body, template):
        seen.update(transcript=transcript, body=outgoing_body, template=template)
        return {"verdict": "send"}
    monkeypatch.setattr(responder.llm, "review_reply", capture)

    assert send() == "sent"

    assert seen["transcript"] == (
        "RENTER: Hi! Is 3309 E San Remo still available?\n"
        "US: Hi Mikayla, yes it is - when works for a tour?")
    assert seen["body"] == BODY
    assert seen["template"] == "availability_ask"


# ------------------------------------------------------------ failures

def test_a_failed_send_is_recorded_as_failed_not_sent(monkeypatch):
    """Marking it sent would strand the renter forever; failed leaves the
    stage recoverable on the next tick."""
    db, gmail = setup(monkeypatch, send_error=RuntimeError("composio 502 upstream"))

    assert send() == "skipped:send-failed"

    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "failed"
    assert doc["error"] == "composio 502 upstream"
    assert "sent_at" not in doc
    assert gmail.labelled == []
    assert ledger.reserve_send(THREAD, STAGE, META) == "recover"


def test_a_label_failure_never_costs_the_renter_a_second_email(monkeypatch):
    """Labels are bookkeeping. They get queued for retry; the send stands."""
    db, gmail = setup(monkeypatch, label_error=RuntimeError("label id not found"))

    assert send() == "sent"

    assert gmail.sent == [(THREAD, RELAY, BODY)]
    doc = db.doc("zillow_sends", KEY)
    assert doc["status"] == "sent"
    assert doc["labels_pending"] is True
    assert doc["labels_add"] == [AWAITING]
    assert doc["labels_remove"] == [NEEDS_REPLY]
    assert [s.id for s in ledger.pending_label_docs()] == [KEY]
    # And the stage is still closed: a retry must not re-email.
    assert send() == "skipped:already-sent"
    assert len(gmail.sent) == 1


def test_no_labels_means_no_label_call_at_all(monkeypatch):
    db, gmail = setup(monkeypatch)

    assert send(labels_add=[], labels_remove=[]) == "sent"

    assert gmail.labelled == []
    assert db.doc("zillow_sends", KEY)["status"] == "sent"


def test_bookkeeping_failure_does_not_undo_a_delivered_email(monkeypatch):
    db, gmail = setup(monkeypatch)
    monkeypatch.setattr(
        responder.ledger, "upsert_thread",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("firestore 503")))

    assert send() == "sent"
    assert gmail.sent == [(THREAD, RELAY, BODY)]
    assert db.doc("zillow_sends", KEY)["status"] == "sent"
