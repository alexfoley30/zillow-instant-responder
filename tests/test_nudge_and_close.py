"""Coverage for cron._nudge_and_close and cron._age_needs_human.

These are the only renter-facing sends nobody triggers: they fire off a
Firestore sweep every 20 minutes with no inbound message to react to. Untested,
they carry the whole 8/4 alondra failure in one line - she had been told the
home was rented, and the nudge re-invited her to tour it. The blocked-address
check in this loop is what stops that, and it had no test.

The sweep runs for real against a fake Firestore query; the nudge goes through
the real responder.send_stage, so the assertions are on the email body itself.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import cron  # noqa: E402
import ledger  # noqa: E402
import responder  # noqa: E402

OPEN = "2120 S El Marino Dr, Mesa, AZ 85202"
LEASED_HOME = "2118 S El Marino Dr, Mesa, AZ 85202"
RELAY = "reply+7h2pqd@convo.zillow.com"

RENTER_MSG = {
    "sender": "Jessica <reply+7h2pqd@convo.zillow.com>",
    "messageId": "1a06130aa41b7c01",
    "messageText": "Jessica says: Hi, is this still available?",
}
ALEX_MSG = {
    "sender": "Alex Foley <alex@azfoleyhomes.com>",
    "messageId": "1a06130aa41b7c02",
    "messageText": "Hi Jessica, when are you hoping to move?",
}
THREAD_MSGS = [RENTER_MSG, ALEX_MSG]


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def waiting_thread(**over):
    """A thread parked in OFFERED with Alex's question as the last word."""
    doc = {"state": ledger.OFFERED, "renter_name": "Jessica",
           "property_address": OPEN, "relay_email": RELAY,
           "last_action_at": _ago(days=3)}
    doc.update(over)
    return doc


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _Query:
    """Just enough Firestore query surface for the two sweeps: a single
    state filter (== or in), a limit, and a stream."""

    def __init__(self, docs):
        self._docs = docs

    def where(self, field, op, value):
        def keep(doc):
            actual = doc.get(field)
            return actual == value if op == "==" else actual in value
        return _Query([(i, d) for i, d in self._docs if keep(d)])

    def limit(self, n):
        return _Query(self._docs[:n])

    def stream(self):
        return [_Snap(i, d) for i, d in self._docs]


class _Sweep:
    """Runs the real cron sweep over an in-memory thread collection."""

    def __init__(self, monkeypatch, threads, blocked=(), msgs=None):
        self.threads = dict(threads)
        self.blocked = list(blocked)
        self.msgs = list(msgs if msgs is not None else THREAD_MSGS)
        self.transitions, self.upserts = [], []
        self.emails, self.label_calls = [], []

        monkeypatch.setenv("DRY_RUN", "false")
        led, gm = cron.ledger, cron.gm
        monkeypatch.setattr(led, "init_db", lambda: self)
        monkeypatch.setattr(led, "blocked_addresses", lambda: list(self.blocked))
        monkeypatch.setattr(led, "transition", self._transition)
        monkeypatch.setattr(led, "upsert_thread", self._upsert)
        monkeypatch.setattr(led, "get_thread",
                            lambda t: dict(self.threads.get(t, {})))
        monkeypatch.setattr(led, "reserve_send", lambda t, s, m: "acquired")
        monkeypatch.setattr(led, "mark_sent", lambda t, s, **f: None)
        monkeypatch.setattr(led, "mark_failed", lambda t, s, e: None)
        monkeypatch.setattr(gm, "fetch_thread", lambda t: list(self.msgs))
        monkeypatch.setattr(gm, "send_reply", self._send)
        monkeypatch.setattr(gm, "modify_labels", self._labels)
        monkeypatch.setattr(gm, "poke_ping", lambda m: True)
        monkeypatch.setattr(gm, "alert_email", lambda s, b: True)
        monkeypatch.setattr(responder.llm, "review_reply",
                            lambda transcript, body, template: {"verdict": "ok"})

    # -- fake Firestore
    def collection(self, name):
        assert name == "zillow_threads", f"unexpected collection {name}"
        return _Query(sorted(self.threads.items()))

    # -- recorders
    def _transition(self, thread_id, state, **fields):
        self.transitions.append((thread_id, state, fields))
        self.threads.setdefault(thread_id, {})["state"] = state

    def _upsert(self, thread_id, **fields):
        self.upserts.append((thread_id, fields))
        self.threads.setdefault(thread_id, {}).update(fields)

    def _send(self, thread_id, relay, body):
        self.emails.append((thread_id, relay, body))
        return {"data": {"id": "m1"}}

    def _labels(self, thread_id, add, remove):
        self.label_calls.append((thread_id, list(add), list(remove)))
        return {"data": {}}

    def run(self):
        cron._nudge_and_close()
        return self

    def states(self):
        return {t: s for t, s, _ in self.transitions}


# ------------------------------------------------- blocked addresses

def test_a_leased_home_is_closed_out_instead_of_nudged(monkeypatch):
    """alondra, 8/4: told 'rented' by the sweep, then re-invited to tour by
    the nudge. The blocked check has to win before any send."""
    s = _Sweep(monkeypatch,
               {"t_leased": waiting_thread(property_address=LEASED_HOME)},
               blocked=["2118 S El Marino Dr"]).run()

    assert s.emails == []
    assert s.states() == {"t_leased": ledger.LEASED}


def test_the_house_next_door_is_still_nudged(monkeypatch):
    """2120 is not 2118: blocking the neighbor would mute a live listing."""
    s = _Sweep(monkeypatch, {"t_open": waiting_thread()},
               blocked=["2118 S El Marino Dr"]).run()

    assert [t for t, _, _ in s.emails] == ["t_open"]


# ------------------------------------------------- the nudge itself

def test_a_stale_lead_gets_one_nudge_naming_the_home(monkeypatch):
    s = _Sweep(monkeypatch, {"t_open": waiting_thread()}).run()

    (thread_id, relay, body), = s.emails
    assert thread_id == "t_open"
    assert relay == RELAY
    assert f"Just checking back in about {OPEN}" in body
    assert "What day and time is good for you?" in body
    assert "Alex Foley" in body
    # a nudge is not a re-invitation to a time we never offered
    assert "lined up" not in body

    assert ("t_open", {"nudged": True}) in s.upserts
    assert s.transitions == []


def test_a_thread_already_nudged_is_never_nudged_again(monkeypatch):
    s = _Sweep(monkeypatch,
               {"t_open": waiting_thread(nudged=True, last_action_at=_ago(days=5))}
               ).run()

    assert s.emails == []
    assert s.transitions == []


def test_a_renter_who_spoke_last_is_never_nudged_over(monkeypatch):
    """Their reply came in through a path the webhook missed; /reprocess owns
    it. Nudging on top of an unanswered renter message is the worst email in
    the set."""
    s = _Sweep(monkeypatch, {"t_open": waiting_thread()},
               msgs=[ALEX_MSG, RENTER_MSG]).run()

    assert s.emails == []
    assert s.upserts == []


def test_a_lead_inside_the_48_hour_window_is_left_alone(monkeypatch):
    s = _Sweep(monkeypatch,
               {"t_open": waiting_thread(last_action_at=_ago(hours=30))}).run()

    assert s.emails == []
    assert s.transitions == []


def test_alex_owned_threads_are_never_touched(monkeypatch):
    """He may be working the deal off-channel; the sweep must not email into it
    or close it."""
    s = _Sweep(monkeypatch,
               {"t_alex": waiting_thread(alex_owned=True,
                                         last_action_at=_ago(days=30))}).run()

    assert s.emails == []
    assert s.transitions == []


def test_a_dead_lead_closes_at_seven_days_without_a_send(monkeypatch):
    s = _Sweep(monkeypatch,
               {"t_dead": waiting_thread(last_action_at=_ago(days=8))}).run()

    assert s.emails == []
    assert s.transitions == [("t_dead", ledger.CLOSED,
                              {"closed_reason": "stale-7d"})]


# ------------------------------------------------- NEEDS_HUMAN aging

def test_a_needs_human_thread_at_a_leased_home_flips_to_leased(monkeypatch):
    """Alex's ruling: those close silently, at any age - 16 of the 34 that
    piled up by 8/22 were at homes already gone."""
    s = _Sweep(monkeypatch,
               {"t_nh": waiting_thread(state=ledger.NEEDS_HUMAN,
                                       property_address=LEASED_HOME,
                                       last_action_at=_ago(hours=1))},
               blocked=["2118 S El Marino Dr"]).run()

    assert s.emails == []
    assert s.transitions == [("t_nh", ledger.LEASED,
                              {"closed_reason": "needs-human-at-leased"})]


def test_a_stale_needs_human_thread_closes_after_seven_days(monkeypatch):
    s = _Sweep(monkeypatch,
               {"t_nh": waiting_thread(state=ledger.NEEDS_HUMAN,
                                       last_action_at=_ago(days=9))}).run()

    assert s.emails == []
    assert s.transitions == [("t_nh", ledger.CLOSED,
                              {"closed_reason": "needs-human-stale-7d"})]


def test_a_fresh_needs_human_thread_stays_open_for_alex(monkeypatch):
    s = _Sweep(monkeypatch,
               {"t_nh": waiting_thread(state=ledger.NEEDS_HUMAN,
                                       last_action_at=_ago(days=2))}).run()

    assert s.emails == []
    assert s.transitions == []
