"""cron._nudge_and_close - the loop that emails renters nobody is watching.

It is the one send path with no inbound message behind it: a 48h timer fires
and the pipeline speaks first. Its guards were untested end to end, and two of
them exist because they already failed in production:

  * alondra (2026-08-04): the sweep told her the home was rented, then the
    nudge re-invited her to tour it. A waiting thread at a blocked address must
    flip to LEASED and stay silent.
  * "never nudge over their reply": if the renter spoke last, reprocess owns
    the thread, not the nudge.

These run the real cron function against an in-memory Firestore and a recorded
Composio, so the nudge that does go out is asserted as an actual email body.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DRY_RUN", "true")

import cron  # noqa: E402
import ledger  # noqa: E402
import templates as T  # noqa: E402
from fakes import (RELAY, alex_msg, install_composio,  # noqa: E402
                   install_firestore, renter_msg)

EL_MARINO = "2118 S El Marino, Mesa, AZ, 85202"
CORONADO = "1641 E Coronado Rd, Phoenix, AZ 85006"   # leased, on the blocklist
BLOCKED = {"zillow_config": {"properties": {
    "blocked_addresses": ["1641 E Coronado Rd", "3309 E San Remo Ave"]}}}


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def _thread(state, address, hours_idle, **extra):
    doc = {"state": state, "property_address": address,
           "renter_name": "Owen", "relay_email": RELAY,
           "last_action_at": _ago(hours=hours_idle)}
    doc.update(extra)
    return doc


THREADS = {
    # 60h since our last word, renter never answered: this one earns the nudge
    "t-due": _thread(ledger.OFFERED, EL_MARINO, 60),
    # the renter DID answer - reprocess owns this, a nudge would talk over her
    "t-renter-spoke": _thread(ledger.ACKED, EL_MARINO, 60, renter_name="Mikayla"),
    # one nudge EVER per thread
    "t-already-nudged": _thread(ledger.AWAITING_TIME, EL_MARINO, 60, nudged=True),
    # alondra: home is gone, so is the conversation
    "t-leased": _thread(ledger.ACKED, CORONADO, 60, renter_name="Alondra"),
    # not idle long enough yet
    "t-fresh": _thread(ledger.OFFERED, EL_MARINO, 12),
    # nine days: close it, silently
    "t-stale": _thread(ledger.ACKED, EL_MARINO, 24 * 9, renter_name="Schneider"),
    # Alex is working it off-channel; the pipeline does not interrupt
    "t-alex": _thread(ledger.OFFERED, EL_MARINO, 60, alex_owned=True),
}

MSGS = {
    "t-due": [renter_msg("m1", "Owen", "Is this still available?"),
              alex_msg("m2", "Hi Owen, what day works for a tour?")],
    "t-renter-spoke": [alex_msg("m3", "Hi Mikayla, what day works?"),
                       renter_msg("m4", "Mikayla", "Sorry for the delay, Friday?")],
    "t-already-nudged": [alex_msg("m5", "Hi Owen, what day works?")],
    "t-leased": [alex_msg("m6", "Hi Alondra, what day works?")],
    "t-fresh": [alex_msg("m7", "Hi Owen, what day works?")],
    "t-stale": [alex_msg("m8", "Hi Schneider, what day works?")],
    "t-alex": [alex_msg("m9", "Hi Owen, what day works?")],
}


def _tick(monkeypatch, threads=None):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = install_firestore(monkeypatch, ledger,
                           {"zillow_threads": dict(threads or THREADS), **BLOCKED})
    composio = install_composio(monkeypatch, cron.gm, threads=MSGS)
    return db, composio


def _state(db, thread_id):
    return db.doc("zillow_threads", thread_id)["state"]


# ---------------------------------------------------------------- the nudge

def test_exactly_one_thread_gets_nudged_and_the_email_is_the_nudge(monkeypatch):
    db, composio = _tick(monkeypatch)
    cron._nudge_and_close()
    assert composio.replies == [{
        "thread_id": "t-due",
        "recipient_email": RELAY,
        "message_body": T.stale_nudge("Owen", EL_MARINO),
    }]
    body = composio.replies[0]["message_body"]
    assert "2118 S El Marino" in body and "Owen" in body
    assert db.doc("zillow_threads", "t-due")["nudged"] is True
    assert db.doc("zillow_sends", "t-due__nudge")["status"] == "sent"


def test_a_second_tick_never_sends_a_second_nudge(monkeypatch):
    """Both belts: the nudged flag on the thread AND the send lock behind it."""
    db, composio = _tick(monkeypatch)
    cron._nudge_and_close()
    cron._nudge_and_close()
    assert len(composio.replies) == 1
    # even with the flag lost (a crash between send and upsert), the lock holds
    db.data["zillow_threads"]["t-due"].pop("nudged")
    cron._nudge_and_close()
    assert len(composio.replies) == 1


def test_a_renter_who_answered_is_never_nudged_over(monkeypatch):
    db, composio = _tick(monkeypatch)
    cron._nudge_and_close()
    assert [r["thread_id"] for r in composio.replies] == ["t-due"]
    assert "nudged" not in db.doc("zillow_threads", "t-renter-spoke")
    assert _state(db, "t-renter-spoke") == ledger.ACKED


def test_alondra_a_leased_address_is_closed_out_not_re_invited(monkeypatch):
    """The whole point: no email, and the thread leaves the waiting states so
    it can never come back around on a later tick."""
    db, composio = _tick(monkeypatch)
    cron._nudge_and_close()
    assert "t-leased" not in [r["thread_id"] for r in composio.replies]
    assert _state(db, "t-leased") == ledger.LEASED
    assert db.ids("zillow_sends") == ["t-due__nudge"]


def test_seven_day_old_thread_closes_without_a_word(monkeypatch):
    db, composio = _tick(monkeypatch)
    cron._nudge_and_close()
    doc = db.doc("zillow_threads", "t-stale")
    assert doc["state"] == ledger.CLOSED
    assert doc["closed_reason"] == "stale-7d"
    assert "t-stale" not in [r["thread_id"] for r in composio.replies]


def test_fresh_and_alex_owned_threads_are_left_exactly_as_they_were(monkeypatch):
    db, composio = _tick(monkeypatch)
    cron._nudge_and_close()
    for thread_id in ("t-fresh", "t-alex"):
        assert _state(db, thread_id) == ledger.OFFERED
        assert "nudged" not in db.doc("zillow_threads", thread_id)
        assert thread_id not in [r["thread_id"] for r in composio.replies]
    # Alex's thread was not even fetched from Gmail
    assert "t-alex" not in [a.get("thread_id")
                            for a in composio.of("GMAIL_FETCH_MESSAGE_BY_THREAD_ID")]


def test_a_thread_with_no_relay_anywhere_is_skipped_not_guessed(monkeypatch):
    db, composio = _tick(monkeypatch, {
        "t-norelay": _thread(ledger.OFFERED, EL_MARINO, 60, relay_email="")})
    composio.threads = {"t-norelay": [alex_msg("m1", "Hi Owen, what day works?")]}
    cron._nudge_and_close()
    assert composio.replies == []
    assert "nudged" not in db.doc("zillow_threads", "t-norelay")


def test_a_gmail_outage_delays_the_nudge_instead_of_sending_blind(monkeypatch):
    db, composio = _tick(monkeypatch, {"t-due": THREADS["t-due"]})
    monkeypatch.setattr(cron.gm, "fetch_thread",
                        lambda tid: (_ for _ in ()).throw(RuntimeError("Gmail 503")))
    cron._nudge_and_close()
    assert composio.replies == []
    assert "nudged" not in db.doc("zillow_threads", "t-due")
    assert _state(db, "t-due") == ledger.OFFERED  # still eligible next tick


# ------------------------------------------------- NEEDS_HUMAN aging (8/22)

def test_needs_human_at_a_leased_address_flips_to_leased(monkeypatch):
    """34 NEEDS_HUMAN threads had piled up, 16 of them at homes that were
    already leased. Those close themselves; the rest wait a week."""
    db, composio = _tick(monkeypatch, {
        "t-nh-leased": _thread(ledger.NEEDS_HUMAN, CORONADO, 3),
        "t-nh-recent": _thread(ledger.NEEDS_HUMAN, EL_MARINO, 24 * 2),
        "t-nh-old": _thread(ledger.NEEDS_HUMAN, EL_MARINO, 24 * 8),
        "t-nh-alex": _thread(ledger.NEEDS_HUMAN, EL_MARINO, 24 * 30,
                             alex_owned=True),
    })
    cron._nudge_and_close()
    assert db.doc("zillow_threads", "t-nh-leased")["state"] == ledger.LEASED
    assert (db.doc("zillow_threads", "t-nh-leased")["closed_reason"]
            == "needs-human-at-leased")
    assert _state(db, "t-nh-recent") == ledger.NEEDS_HUMAN
    assert db.doc("zillow_threads", "t-nh-old")["closed_reason"] == "needs-human-stale-7d"
    assert _state(db, "t-nh-alex") == ledger.NEEDS_HUMAN
    assert composio.replies == []


def test_the_blocklist_the_guard_reads_is_the_one_in_firestore(monkeypatch):
    """The gate is only as good as the list behind it: zillow_config/properties
    is what blocked_addresses() returns, and a house on it must match the
    thread's full postal address."""
    db, composio = _tick(monkeypatch)
    assert ledger.blocked_addresses() == ["1641 E Coronado Rd",
                                          "3309 E San Remo Ave"]
    assert cron.rules.is_blocked_address(CORONADO, ledger.blocked_addresses())
    assert not cron.rules.is_blocked_address(EL_MARINO, ledger.blocked_addresses())
