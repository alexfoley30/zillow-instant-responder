"""Stuck-send backstop: review blocks close out, real stuck sends alert ONCE.
2026-10-26: 201 "Needs you: stuck send" emails in a week for two threads, one
every 20 minutes, because a review-blocked "failed" doc came back every tick."""
import cron


class Snap:
    def __init__(self, sid, data):
        self.id, self._d = sid, data

    def to_dict(self):
        return self._d


def _wire(monkeypatch, snaps):
    calls = {"abandoned": [], "pings": [], "emails": [], "alerted": [], "needs": []}
    monkeypatch.setattr(cron.ledger, "stale_reserved_docs", lambda older_than_min=30: snaps)
    monkeypatch.setattr(cron.ledger, "get_thread", lambda tid: {"state": cron.ledger.NEEDS_HUMAN})
    monkeypatch.setattr(cron.ledger, "mark_abandoned",
                        lambda tid, stage, reason: calls["abandoned"].append((tid, stage, reason)))
    monkeypatch.setattr(cron.ledger, "transition", lambda tid, st, **kw: calls["needs"].append(tid))
    monkeypatch.setattr(cron.ledger, "mark_stuck_alerted",
                        lambda tid, stage: calls["alerted"].append((tid, stage)))
    monkeypatch.setattr(cron.gm, "fetch_thread", lambda tid: [])
    monkeypatch.setattr(cron.gm, "alex_replied_after", lambda msgs, mid: False)
    monkeypatch.setattr(cron.gm, "poke_ping", lambda m: calls["pings"].append(m))
    monkeypatch.setattr(cron.gm, "alert_email", lambda s, b: calls["emails"].append(s))
    return calls


def test_review_blocked_doc_is_closed_out_without_alert(monkeypatch):
    calls = _wire(monkeypatch, [Snap("t1__first_reply", {
        "status": "failed", "error": "review-blocked: offers a showing at 1am"})])
    cron._recover_stuck()
    assert calls["abandoned"] == [("t1", "first_reply", "review-blocked, escalated at block time")]
    assert calls["emails"] == [] and calls["pings"] == [] and calls["needs"] == []


def test_real_stuck_send_alerts_once_and_is_marked(monkeypatch):
    calls = _wire(monkeypatch, [Snap("t2__reply__m1", {
        "status": "reserved", "trigger_message_id": "m1"})])
    cron._recover_stuck()
    assert calls["emails"] == ["Needs you: stuck send t2"]
    assert calls["alerted"] == [("t2", "reply__m1")]


def test_already_alerted_doc_stays_quiet(monkeypatch):
    calls = _wire(monkeypatch, [Snap("t3__reply__m2", {
        "status": "reserved", "trigger_message_id": "m2", "stuck_alerted_at": "x"})])
    cron._recover_stuck()
    assert calls["emails"] == [] and calls["pings"] == [] and calls["alerted"] == []
