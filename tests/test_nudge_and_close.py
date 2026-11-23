"""The 48h nudge and the 7-day close: cron._nudge_and_close / _age_needs_human.

These two loops were the only renter-emailing code in the service with no test
at all, and the one incident the code still carries a comment about is exactly
the kind a test holds down:

  "A leased-address thread must never be nudged - alondra was told 'rented' by
   the sweep, then the nudge re-invited her to tour (8/4)."

Everything side-effectful is recorded instead of executed, but the real
`responder.send_stage` runs, so the body asserted below is the body Composio
would have been handed. Addresses come from the live blocked list in
scripts/sync_facts.py; the blocked check itself is the real
`rules.is_blocked_address`.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cron  # noqa: E402
import ledger  # noqa: E402
import templates as T  # noqa: E402
from _fake_firestore import FakeDB, Snap  # noqa: E402

NOW = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)

# Live, leased: scripts/sync_facts.py BLOCKED_ADDRESSES ("leased 2026-07-04").
LEASED_ADDR = "3309 E San Remo Ave, Gilbert, AZ 85234"
OPEN_ADDR = "2701 E Willetta St, Phoenix, AZ, 85008"

RELAY = "alondra+r58y56j72@convo.zillow.com"
RENTER_MSG = {"messageId": "m-renter",
              "sender": f"Alondra <{RELAY}>",
              "messageText": "Is this place still available?"}
ALEX_MSG = {"messageId": "m-alex",
            "sender": "Alex Foley <alex@azfoleyhomes.com>",
            "messageText": T.windows_ask("Alondra")}


def _thread(address=OPEN_ADDR, state=ledger.ACKED, age_hours=50, **extra):
    doc = {"state": state, "renter_name": "Alondra",
           "property_address": address, "relay_email": RELAY,
           "last_action_at": NOW - timedelta(hours=age_hours)}
    doc.update(extra)
    return doc


def _wire(monkeypatch, waiting=(), needs_human=(), msgs=(RENTER_MSG, ALEX_MSG)):
    """Returns a dict of everything the loops tried to do to the outside world."""
    import responder

    calls = {"sends": [], "stages": [], "transitions": [], "upserts": []}

    class FakeNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW

    by_state = {ledger.NEEDS_HUMAN: list(needs_human)}
    for snap in waiting:
        by_state.setdefault(snap.to_dict()["state"], []).append(snap)

    monkeypatch.setattr(cron, "datetime", FakeNow)
    monkeypatch.setattr(cron.ledger, "init_db", lambda: FakeDB(by_state))
    monkeypatch.setattr(cron.ledger, "blocked_addresses",
                        lambda: ["3309 E San Remo Ave", "1294 E Apricot Ln",
                                 "220 N 22nd Pl"])
    monkeypatch.setattr(cron.ledger, "transition",
                        lambda tid, state, **f: calls["transitions"].append(
                            (tid, state, f)))
    monkeypatch.setattr(cron.ledger, "upsert_thread",
                        lambda tid, **f: calls["upserts"].append((tid, f)))
    monkeypatch.setattr(cron.gm, "fetch_thread", lambda tid: list(msgs))
    # Real send_stage, fake wire: these are the primitives it bottoms out in.
    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(responder, "review_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(cron.ledger, "reserve_send",
                        lambda tid, stage, meta: calls["stages"].append(stage)
                        or "acquired")
    monkeypatch.setattr(cron.ledger, "mark_sent", lambda *a, **k: None)
    monkeypatch.setattr(cron.ledger, "mark_failed", lambda *a, **k: None)
    monkeypatch.setattr(cron.gm, "send_reply",
                        lambda tid, relay, body: calls["sends"].append(
                            (tid, relay, body)))
    monkeypatch.setattr(cron.gm, "modify_labels", lambda *a, **k: None)
    return calls


# ------------------------------------------------------------- the nudge send

def test_stale_thread_gets_the_real_nudge_body_once(monkeypatch):
    calls = _wire(monkeypatch, waiting=[Snap("t-aliyah", _thread())])
    cron._nudge_and_close()

    assert calls["sends"] == [("t-aliyah", RELAY,
                               T.stale_nudge("Alondra", OPEN_ADDR))]
    body = calls["sends"][0][2]
    assert OPEN_ADDR in body
    assert "What day and time is good for you?" in body
    assert T.SIGNATURE in body
    assert calls["stages"] == ["nudge"]
    assert ("t-aliyah", {"nudged": True}) in calls["upserts"]
    assert calls["transitions"] == []


def test_nudge_goes_to_the_relay_address_from_the_thread(monkeypatch):
    """The recipient is resolved from the live Gmail thread, not guessed."""
    calls = _wire(monkeypatch, waiting=[Snap("t-aliyah", _thread(relay_email=""))])
    cron._nudge_and_close()
    assert [c[1] for c in calls["sends"]] == [RELAY]


# ---------------------------------------------- the 8/4 alondra regression

def test_leased_home_is_never_nudged_and_flips_to_leased(monkeypatch):
    """alondra, 8/4: told "rented" by the sweep, then re-invited to tour."""
    calls = _wire(monkeypatch,
                  waiting=[Snap("t-alondra", _thread(address=LEASED_ADDR))])
    cron._nudge_and_close()

    assert calls["sends"] == []
    assert calls["transitions"] == [("t-alondra", ledger.LEASED, {})]
    assert calls["upserts"] == []


def test_blocked_match_survives_a_spelled_out_street_type(monkeypatch):
    """The blocked list stores "3309 E San Remo Ave"; Zillow subjects carry
    every spelling. A miss here is a tour invite for a rented home."""
    calls = _wire(monkeypatch, waiting=[
        Snap("t-a", _thread(address="3309 E. San Remo Avenue, Gilbert, AZ, 85234")),
        Snap("t-b", _thread(address="220 N 22nd Pl #2094, Mesa, AZ, 85213")),
    ])
    cron._nudge_and_close()

    assert calls["sends"] == []
    assert [(tid, st) for tid, st, _ in calls["transitions"]] == [
        ("t-a", ledger.LEASED), ("t-b", ledger.LEASED)]


def test_a_different_house_on_the_same_street_still_gets_nudged(monkeypatch):
    """"1641 E Coronado Rd" is blocked; 16413 is a different house. The
    substring version of this check blocked the wrong home."""
    calls = _wire(monkeypatch, waiting=[
        Snap("t-c", _thread(address="16413 E Coronado Rd, Phoenix, AZ, 85006"))])
    monkeypatch.setattr(cron.ledger, "blocked_addresses",
                        lambda: ["1641 E Coronado Rd"])
    cron._nudge_and_close()

    assert len(calls["sends"]) == 1
    assert calls["transitions"] == []


# ------------------------------------------------------- never talk over them

def test_renter_who_spoke_last_is_not_nudged(monkeypatch):
    """Their reply is reprocess's job. A nudge on top of it reads as deaf."""
    calls = _wire(monkeypatch, waiting=[Snap("t-aliyah", _thread())],
                  msgs=(ALEX_MSG, RENTER_MSG))
    cron._nudge_and_close()

    assert calls["sends"] == []
    assert calls["upserts"] == []
    assert calls["transitions"] == []


def test_already_nudged_thread_is_never_nudged_again(monkeypatch):
    calls = _wire(monkeypatch, waiting=[
        Snap("t-aliyah", _thread(age_hours=120, nudged=True))])
    cron._nudge_and_close()
    assert calls["sends"] == []


def test_alex_owned_thread_is_left_entirely_alone(monkeypatch):
    """He may be working the deal off-channel."""
    calls = _wire(monkeypatch, waiting=[
        Snap("t-aliyah", _thread(age_hours=400, alex_owned=True))])
    cron._nudge_and_close()

    assert calls["sends"] == []
    assert calls["transitions"] == []
    assert calls["upserts"] == []


# ------------------------------------------------------------ the two clocks

def test_inside_forty_eight_hours_nothing_happens(monkeypatch):
    calls = _wire(monkeypatch, waiting=[Snap("t-aliyah", _thread(age_hours=47))])
    cron._nudge_and_close()
    assert calls["sends"] == [] and calls["transitions"] == []


def test_seven_days_closes_the_thread_without_a_send(monkeypatch):
    calls = _wire(monkeypatch, waiting=[Snap("t-aliyah", _thread(age_hours=24 * 8))])
    cron._nudge_and_close()

    assert calls["sends"] == []
    assert calls["transitions"] == [
        ("t-aliyah", ledger.CLOSED, {"closed_reason": "stale-7d"})]


def test_every_waiting_state_is_swept(monkeypatch):
    """ACKED/OFFERED/AWAITING_TIME all mean "waiting on renter" and must all
    reach the nudge - the query is a single "in" filter over the set."""
    waiting = [Snap(f"t-{s}", _thread(state=s)) for s in sorted(ledger.WAITING_STATES)]
    calls = _wire(monkeypatch, waiting=waiting)
    cron._nudge_and_close()

    assert sorted(tid for tid, _, _ in calls["sends"]) == \
        sorted(f"t-{s}" for s in ledger.WAITING_STATES)


def test_a_thread_with_no_timestamps_is_skipped_not_crashed(monkeypatch):
    doc = _thread()
    doc.pop("last_action_at")
    calls = _wire(monkeypatch, waiting=[Snap("t-aliyah", doc)])
    cron._nudge_and_close()
    assert calls["sends"] == [] and calls["transitions"] == []


# --------------------------------------------------------- _age_needs_human

def test_needs_human_at_a_leased_home_flips_to_leased_silently(monkeypatch):
    """Alex's ruling: blocked-address escalations go LEASED, no email."""
    calls = _wire(monkeypatch, needs_human=[Snap("t-nh", _thread(
        address=LEASED_ADDR, state=ledger.NEEDS_HUMAN, age_hours=2))])
    cron._age_needs_human()

    assert calls["sends"] == []
    assert calls["transitions"] == [
        ("t-nh", ledger.LEASED, {"closed_reason": "needs-human-at-leased"})]


def test_needs_human_unresolved_for_seven_days_closes(monkeypatch):
    calls = _wire(monkeypatch, needs_human=[Snap("t-nh", _thread(
        state=ledger.NEEDS_HUMAN, age_hours=24 * 8))])
    cron._age_needs_human()

    assert calls["transitions"] == [
        ("t-nh", ledger.CLOSED, {"closed_reason": "needs-human-stale-7d"})]


def test_fresh_needs_human_is_left_for_a_human(monkeypatch):
    calls = _wire(monkeypatch, needs_human=[Snap("t-nh", _thread(
        state=ledger.NEEDS_HUMAN, age_hours=24 * 6))])
    cron._age_needs_human()
    assert calls["transitions"] == []


def test_alex_owned_needs_human_is_never_aged_out(monkeypatch):
    calls = _wire(monkeypatch, needs_human=[Snap("t-nh", _thread(
        state=ledger.NEEDS_HUMAN, age_hours=24 * 30, alex_owned=True))])
    cron._age_needs_human()
    assert calls["transitions"] == []


def test_nudge_pass_ages_needs_human_threads_too(monkeypatch):
    """_nudge_and_close calls _age_needs_human first; a NEEDS_HUMAN thread at
    a leased home must not need a separate tick to be cleaned up."""
    calls = _wire(
        monkeypatch,
        waiting=[Snap("t-wait", _thread())],
        needs_human=[Snap("t-nh", _thread(address=LEASED_ADDR,
                                          state=ledger.NEEDS_HUMAN))])
    cron._nudge_and_close()

    assert ("t-nh", ledger.LEASED,
            {"closed_reason": "needs-human-at-leased"}) in calls["transitions"]
    assert [tid for tid, _, _ in calls["sends"]] == ["t-wait"]
