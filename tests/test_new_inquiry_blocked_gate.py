#!/usr/bin/env python3
"""The blocked-address gate on a BRAND-NEW inquiry.

blocked_booking_guard, the reply-side gate, has tests. The new-inquiry side
had none, and it is the one deciding whether someone asking about a rented
house hears "that home has been rented" or gets invited to tour it - the
exact email Alex has to apologize for (1407 W Glenrosa was blocked by hand
on 2026-10-05 after it rented). The other direction costs just as much: a
house wrongly matched as blocked tells a live prospect to look elsewhere.

These drive the real handle_new_inquiry with the real ledger writes against
tests/fake_firestore.py, intercepting only Composio, so the assertions land
on the email that would actually leave and the documents that would be
written. The calendar deliberately holds an upcoming showing at the blocked
house: if the gate ever stops firing, the renter gets "come join that
showing" and these tests say so.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DRY_RUN", "true")

import calendar_logic as cal  # noqa: E402
import fake_firestore  # noqa: E402
import gmail_client as gm  # noqa: E402
import ledger  # noqa: E402
import responder  # noqa: E402
import rules  # noqa: E402
import templates as T  # noqa: E402

AZ = rules.AZ_TZ

THREAD = "18f2c0a9b1c2d3e4"
MESSAGE_ID = "19a0b1c2d3e4f5a6"
RELAY = "x7k2m9@convo.zillow.com"
BLOCKED_ADDRESS = "1407 W Glenrosa Ave"
RENTED = "1407 W Glenrosa Ave, Phoenix, AZ 85015"
NEXT_DOOR = "1409 W Glenrosa Ave, Phoenix, AZ 85015"

# Zillow first-inquiry relay mail, trimmed to the blocks the extractor reads.
INQUIRY_HTML = (
    '<div><a style="color:#006aff" href="https://zillow.com/rental-manager/'
    'properties/4ekrkfrk3z20p/find-tenants?ref=inquiry">1407 W Glenrosa Ave, '
    'Phoenix, AZ, 85015</a>.</div>'
    '<tr><td><div><span style="font-weight:bold">Dana Whitfield</span> says:'
    '</div></td></tr>'
    '<tr><td><div style="color:#2a2a33">I would like to schedule a tour.'
    '</div></td></tr>'
    '<tr><td><a href="https://zillow.com/rental-manager/inbox/4ekrkfrk3z20p/'
    '301276737">Reply to Dana</a></td></tr>'
    '<tr><td><a href="https://zillow.com/rental-manager/applications/">Send '
    'application</a></td></tr>'
    '<tr><td><div style="font-weight:700">About Dana Whitfield</div></td></tr>'
    '<tr><td><div>Have questions or need help? Find answers on our '
    '<a href="https://zillow.com/r/3r280kuhv58mt">FAQ page</a>.</div></td></tr>'
)
MSGS = [{"messageId": MESSAGE_ID, "sender": f"Dana Whitfield <{RELAY}>",
         "messageText": INQUIRY_HTML}]


def _showing_tomorrow_at_glenrosa():
    """A real upcoming showing at the rented house: the thing the gate has to
    beat, since consolidate-first would otherwise offer this slot."""
    start = (datetime.now(AZ) + timedelta(days=1)).replace(
        hour=13, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=rules.SHOWING_MINUTES)
    return {
        "id": "ev_glenrosa_1",
        "summary": "Showing — 1407 W Glenrosa Ave with Marcus",
        "location": RENTED,
        "description": ("Zillow inquiry.\nInquirers: Marcus.\n"
                        "Agent: Alex Foley (alex@azfoleyhomes.com)."),
        "start": {"dateTime": start.astimezone(timezone.utc).isoformat()},
        "end": {"dateTime": end.astimezone(timezone.utc).isoformat()},
    }


def _wire(monkeypatch, blocked, events, fail_reads=()):
    """Live-mode wiring: real ledger against the fake, Composio captured."""
    db = fake_firestore.install(monkeypatch, fail_reads=fail_reads)
    db.store["zillow_config"] = {
        "properties": {"blocked_addresses": list(blocked)}}
    monkeypatch.setattr(responder, "dry_run", lambda: False)
    # No key = llm.review_reply fails open, so the pre-send review passes
    # without reaching Anthropic.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    gmail_calls, calendar_calls = [], []

    def gmail_exec(slug, args):
        gmail_calls.append((slug, args))
        if slug == "GMAIL_FETCH_MESSAGE_BY_THREAD_ID":
            return {"data": {"messages": MSGS}}
        return {"successful": True, "data": {"successful": True}}

    def calendar_exec(slug, args):
        calendar_calls.append((slug, args))
        return {"successful": True, "data": {"items": list(events)}}

    monkeypatch.setattr(gm, "composio_execute", gmail_exec)
    monkeypatch.setattr(cal, "composio_execute", calendar_exec)
    return db, gmail_calls, calendar_calls


def _bodies(gmail_calls):
    return [a["message_body"] for s, a in gmail_calls
            if s == "GMAIL_REPLY_TO_THREAD"]


def _labels(gmail_calls):
    return [(a["add_label_ids"], a["remove_label_ids"])
            for s, a in gmail_calls if s == "GMAIL_MODIFY_THREAD_LABELS"]


def test_a_blocked_house_gets_the_rented_reply_not_a_tour_invite(monkeypatch):
    db, gmail_calls, calendar_calls = _wire(
        monkeypatch, [BLOCKED_ADDRESS], [_showing_tomorrow_at_glenrosa()])
    renter_text = gm.extract_renter_text(MSGS[0])
    assert renter_text == "I would like to schedule a tour."

    result = responder.handle_new_inquiry(THREAD, "Dana", RENTED, RELAY,
                                          renter_text, MESSAGE_ID)

    assert result == "sent"
    assert _bodies(gmail_calls) == [T.leased_reply("Dana", RENTED)]
    body = _bodies(gmail_calls)[0]
    assert "has been rented and is no longer available" in body
    assert "showing already lined up" not in body   # consolidate-first copy
    assert "10:00 AM" not in body                   # the windows block
    assert "application" not in body.lower()
    # The gate fires before the calendar is ever consulted.
    assert calendar_calls == []

    reply = next(a for s, a in gmail_calls if s == "GMAIL_REPLY_TO_THREAD")
    assert reply["recipient_email"] == RELAY
    assert reply["thread_id"] == THREAD
    assert _labels(gmail_calls) == [([responder.HANDLED_LABEL], [])]

    thread = db.store["zillow_threads"][THREAD]
    assert thread["state"] == ledger.LEASED
    assert thread["renter_name"] == "Dana"
    assert thread["property_address"] == RENTED
    assert thread["relay_email"] == RELAY

    send = db.store["zillow_sends"][f"{THREAD}__leased"]
    assert send["status"] == "sent"
    assert send["template"] == "leased"
    assert send["body_sha256"] == ledger.content_hash(body)


def test_a_blocked_inquiry_is_told_once_even_on_a_second_copy(monkeypatch):
    """Zillow's duplicate notification must not produce a second "it's
    rented" email: the leased stage holds the same send lock as any other."""
    _, gmail_calls, _ = _wire(monkeypatch, [BLOCKED_ADDRESS], [])
    renter_text = gm.extract_renter_text(MSGS[0])

    first = responder.handle_new_inquiry(THREAD, "Dana", RENTED, RELAY,
                                         renter_text, MESSAGE_ID)
    second = responder.handle_new_inquiry(THREAD, "Dana", RENTED, RELAY,
                                          renter_text, "19a0b1c2d3e4f5a7")

    assert first == "sent"
    assert second == "skipped:already-sent"
    assert _bodies(gmail_calls) == [T.leased_reply("Dana", RENTED)]


def test_the_house_next_door_still_gets_the_availability_ask(monkeypatch):
    """1409 is not 1407. An over-eager match would tell a live prospect the
    home is gone - the same mistake, pointed the other way."""
    db, gmail_calls, _ = _wire(
        monkeypatch, [BLOCKED_ADDRESS], [_showing_tomorrow_at_glenrosa()])

    result = responder.handle_new_inquiry(THREAD, "Dana", NEXT_DOOR, RELAY,
                                          "I would like to schedule a tour.",
                                          MESSAGE_ID)

    assert result == "sent"
    assert _bodies(gmail_calls) == [T.availability_ask("Dana", NEXT_DOOR)]
    body = _bodies(gmail_calls)[0]
    assert "has been rented" not in body
    assert "what day and time works" in body
    # The 1407 showing belongs to another house, so it is not offered here.
    assert "1:00 PM" not in body
    assert _labels(gmail_calls) == [([responder.AWAITING_LABEL], [])]
    assert db.store["zillow_threads"][THREAD]["state"] == ledger.ACKED


def test_a_blocked_list_read_failure_still_sends_the_instant_reply(monkeypatch):
    """The blocked list lives in Firestore; a read failure must fail OPEN.
    Blocking the instant reply on it would cost every live lead its answer,
    and the reply path's guard catches a leased home before any booking."""
    db, gmail_calls, _ = _wire(monkeypatch, [BLOCKED_ADDRESS], [],
                               fail_reads=("zillow_config",))
    assert ledger.blocked_addresses() == []

    result = responder.handle_new_inquiry(THREAD, "Dana", RENTED, RELAY,
                                          "I would like to schedule a tour.",
                                          MESSAGE_ID)

    assert result == "sent"
    assert _bodies(gmail_calls) == [T.availability_ask("Dana", RENTED)]
    assert db.store["zillow_threads"][THREAD]["state"] == ledger.ACKED


def test_the_blocked_list_is_read_from_zillow_config(monkeypatch):
    """What the gate reads: the list a person maintains in
    zillow_config/properties, straight through, no reshaping."""
    db = fake_firestore.install(monkeypatch)
    db.store["zillow_config"] = {"properties": {"blocked_addresses": [
        BLOCKED_ADDRESS, "26009 S New Town Dr"]}}

    assert ledger.blocked_addresses() == [BLOCKED_ADDRESS,
                                          "26009 S New Town Dr"]
    assert rules.is_blocked_address(RENTED, ledger.blocked_addresses()) is True
    assert rules.is_blocked_address(NEXT_DOOR,
                                    ledger.blocked_addresses()) is False
