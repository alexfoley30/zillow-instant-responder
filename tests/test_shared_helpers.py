"""Guards for the two shared helpers that replaced copy-pasted blocks:
responder._apply_labels / _relabel_handled and llm._json_call.

These exist so the de-duplication cannot quietly change behaviour: a label
failure must never raise into the send path, and it must only queue a retry
when there is a stage doc to hang it on.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm  # noqa: E402
import responder  # noqa: E402


# ------------------------------------------------------------ label helper

class _Recorder:
    def __init__(self, fail=False):
        self.fail = fail
        self.modify_calls = []
        self.pending_calls = []

    def modify_labels(self, thread_id, add, remove):
        self.modify_calls.append((thread_id, add, remove))
        if self.fail:
            raise RuntimeError("gmail 500")

    def set_labels_pending(self, thread_id, stage_key, add, remove):
        self.pending_calls.append((thread_id, stage_key, add, remove))


def _patched(monkeypatch, rec):
    monkeypatch.setattr(responder.gm, "modify_labels", rec.modify_labels)
    monkeypatch.setattr(responder.ledger, "set_labels_pending",
                        rec.set_labels_pending)


def test_relabel_handled_sends_handled_minus_awaiting(monkeypatch):
    rec = _Recorder()
    _patched(monkeypatch, rec)
    responder._relabel_handled("t1", "reply__m1")
    assert rec.modify_calls == [
        ("t1", [responder.HANDLED_LABEL], [responder.AWAITING_LABEL])]
    assert rec.pending_calls == []


def test_label_failure_with_stage_queues_a_retry(monkeypatch):
    rec = _Recorder(fail=True)
    _patched(monkeypatch, rec)
    responder._relabel_handled("t1", "reply__m1")  # must not raise
    assert rec.pending_calls == [
        ("t1", "reply__m1", [responder.HANDLED_LABEL],
         [responder.AWAITING_LABEL])]


def test_label_failure_without_stage_is_dropped_not_queued(monkeypatch):
    """The decline / benign paths never reserved a send, so there is no stage
    doc for cron's _retry_labels to key off. Queuing one would write a
    pending-label doc under an empty stage key."""
    rec = _Recorder(fail=True)
    _patched(monkeypatch, rec)
    responder._relabel_handled("t1")  # must not raise
    assert rec.pending_calls == []


def test_empty_label_change_touches_gmail_not_at_all(monkeypatch):
    rec = _Recorder()
    _patched(monkeypatch, rec)
    responder._apply_labels("t1", [], [], "reply__m1")
    assert rec.modify_calls == []


# ------------------------------------------------------------- llm wrapper

def test_review_reply_without_a_key_is_unavailable_not_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.review_reply("t", "body", "tpl") == {
        "verdict": "send", "reason": "review-unavailable"}


def test_review_reply_fails_open_on_a_broken_call(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_json_call", lambda *a, **k: None)
    assert llm.review_reply("t", "body", "tpl") == {
        "verdict": "send", "reason": "review-error"}


def test_review_reply_rejects_a_verdict_outside_the_enum(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_json_call", lambda *a, **k: {"verdict": "maybe"})
    assert llm.review_reply("t", "body", "tpl") == {
        "verdict": "send", "reason": "review-malformed"}


def test_review_reply_passes_a_block_through(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "_json_call",
                        lambda *a, **k: {"verdict": "block", "reason": "dup"})
    assert llm.review_reply("t", "body", "tpl")["verdict"] == "block"


def test_json_call_treats_a_non_object_response_as_failure(monkeypatch):
    """Both callers .get() on the result straight away, so a bare list or
    string coming back from the model is a failure, not a result."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class _Block:
        type = "text"
        text = '["not", "an", "object"]'

    class _Client:
        def __init__(self, **_kw):
            self.messages = self

        def create(self, **_kw):
            return type("R", (), {"content": [_Block()]})()

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    assert llm._json_call("p", {}, 16, "test") is None


def test_classify_reply_falls_back_to_regex_when_the_key_is_missing(monkeypatch):
    from datetime import datetime
    import rules
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = llm.classify_reply("Can I see it Tuesday at 3pm?", "",
                             datetime(2026, 9, 7, 9, 0, tzinfo=rules.AZ_TZ))
    assert out["_fallback"] is True
    assert out["intent"] in llm.INTENTS


# ------------------------------------------------------- address / name / time

def test_street_only_drops_everything_after_the_street():
    import rules
    assert rules.street_only("1641 E Coronado Rd, Phoenix, AZ 85006") == \
        "1641 E Coronado Rd"
    assert rules.street_only("1641 E Coronado Rd") == "1641 E Coronado Rd"
    assert rules.street_only("  3309 E San Remo Ave , Gilbert") == \
        "3309 E San Remo Ave"
    assert rules.street_only(None) == ""
    assert rules.street_only("") == ""


def test_first_name_of():
    import rules
    assert rules.first_name_of("Jace Johnson") == "Jace"
    assert rules.first_name_of("Alex") == "Alex"
    assert rules.first_name_of("   ") == ""
    assert rules.first_name_of(None) == ""


def test_fmt_ping_time_is_the_terse_form():
    from datetime import datetime
    import calendar_logic as cal
    import rules
    when = datetime(2026, 9, 9, 18, 30, tzinfo=rules.AZ_TZ)   # a Wednesday
    assert cal.fmt_ping_time(when) == "Wed 9/9 6:30 PM"


# --------------------------------------------------------------- booked pings

def test_booked_ping_shape():
    assert responder._booked_ping(
        "Booked", "Anna", "1641 E Coronado Rd, Phoenix, AZ 85006",
        "Wed 9/9 6:30 PM with Jace") == \
        "Booked: Anna - 1641 E Coronado Rd - Wed 9/9 6:30 PM with Jace. [FYI]"


def test_booked_ping_failure_never_breaks_a_completed_booking(monkeypatch):
    """The calendar write and the renter email are already done by the time a
    ping goes out, so a dead Poke must not turn a success into a failure."""
    def boom(_msg):
        raise RuntimeError("poke down")
    monkeypatch.setattr(responder.gm, "poke_ping", boom)
    responder._send_booked_ping("Booked", "Anna", "1 Main St", "now")


# ---------------------------------------------------------- dual-channel alert

def test_escalate_reports_true_if_either_channel_lands(monkeypatch):
    import gmail_client as gm
    monkeypatch.setattr(gm, "poke_ping", lambda m: False)
    monkeypatch.setattr(gm, "alert_email", lambda s, b: True)
    assert gm.escalate("subj", "msg") is True

    monkeypatch.setattr(gm, "poke_ping", lambda m: True)
    monkeypatch.setattr(gm, "alert_email", lambda s, b: False)
    assert gm.escalate("subj", "msg") is True


def test_escalate_reports_false_only_when_both_fail(monkeypatch):
    """needs_human stamps its 1/day rate limit on this answer, so a wrong
    True silently swallows the retry."""
    import gmail_client as gm
    monkeypatch.setattr(gm, "poke_ping", lambda m: False)
    monkeypatch.setattr(gm, "alert_email", lambda s, b: False)
    assert gm.escalate("subj", "msg") is False


def test_escalate_survives_either_channel_raising(monkeypatch):
    import gmail_client as gm

    def boom(*_a):
        raise RuntimeError("down")
    monkeypatch.setattr(gm, "poke_ping", boom)
    monkeypatch.setattr(gm, "alert_email", lambda s, b: True)
    assert gm.escalate("subj", "msg") is True

    monkeypatch.setattr(gm, "poke_ping", lambda m: True)
    monkeypatch.setattr(gm, "alert_email", boom)
    assert gm.escalate("subj", "msg") is True

    monkeypatch.setattr(gm, "poke_ping", boom)
    monkeypatch.setattr(gm, "alert_email", boom)
    assert gm.escalate("subj", "msg") is False


def test_email_only_note_never_reaches_the_text_message(monkeypatch):
    """The ping becomes an SMS. 'Reply to the renter via the thread in Gmail'
    is inbox advice and has no business in one."""
    import gmail_client as gm
    seen = {}
    monkeypatch.setattr(gm, "poke_ping",
                        lambda m: seen.__setitem__("ping", m) or True)
    monkeypatch.setattr(gm, "alert_email",
                        lambda s, b: seen.__setitem__("email", b) or True)
    gm.escalate("subj", "core message", "\n\nlong inbox-only footer")
    assert seen["ping"] == "core message"
    assert seen["email"] == "core message\n\nlong inbox-only footer"
