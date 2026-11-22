"""U83 (2026-11-20/21, 2701 E Willetta St).

1. Hand-typed offers were invisible. Jace got sick; Alex wrote Jose and Emilsa
   himself on their Zillow threads offering "tomorrow at 10". Both said yes
   (Jose 5:40pm, Emilsa 11:28pm) and both were held with the Needs-you
   "URGENT same-day: ... sounded like a yes but no live offer stands" instead
   of being booked. Alex confirmed both by hand at 6:45am.
2. Plain confirmations of an already-booked tour Poked as urgent: Jose 11/20
   1:43pm "I will be there at 6pm today" (booked 6pm), Ki 11/18 twice
   "yes, will be there".

Every test drives the real responder.handle_reply; only Gmail, Firestore, the
calendar API and the LLM are replaced (the harness from
test_book_renter_windows)."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DRY_RUN", "true")

import pytest  # noqa: E402

import responder  # noqa: E402
import rules  # noqa: E402
import templates as T  # noqa: E402
from test_book_renter_windows import _cand, _cls, _wire  # noqa: E402

AZ = rules.AZ_TZ
ADDR = "2701 E Willetta St, Phoenix, AZ, 85008"
FRI_1PM = datetime(2026, 11, 20, 13, 0, tzinfo=AZ)      # Alex types the offer
FRI_540PM = datetime(2026, 11, 20, 17, 40, tzinfo=AZ)   # Jose says yes
SAT_10AM = datetime(2026, 11, 21, 10, 0, tzinfo=AZ)
FRI_6PM = datetime(2026, 11, 20, 18, 0, tzinfo=AZ)

# What Alex's hand-typed Gmail reply looks like once msg_body flattens it:
# his words, his signature (phone digits, "Suite 120") and the quoted history.
ALEX_HAND = (
    "Hi Jose, so sorry, Jace got sick today. Could you do tomorrow at 10 "
    "instead? I'll meet you there myself.\n\n"
    "Alex Foley\nRealtor & Property Manager\n"
    "480-815-9313  |  alex@azfoleyhomes.com\n"
    "2425 S Stearman Dr, Suite 120, Chandler, AZ 85286\n\n"
    "On Fri, Nov 20, 2026 at 1:43 PM Jose <x@convo.zillow.com> wrote:\n"
    "> I will be there at 6pm today")


def _booked_doc(name="Jose", start=FRI_6PM, event_id="evJACE6"):
    return {"renter_name": name, "property_address": ADDR,
            "relay_email": "relay@convo.zillow.com",
            "state": responder.ledger.BOOKED, "event_id": event_id,
            "booked_start_iso": start.isoformat(), "agent": "Jace Johnson",
            "last_reply_template": "first_reply"}


def _thread(alex_text, alex_at=FRI_1PM):
    return [
        {"id": "m-1", "from": "renter", "text": "Hi, can I tour Friday at 6?",
         "at": datetime(2026, 11, 19, 9, 0, tzinfo=AZ)},
        {"id": "a-1", "from": "alex", "text": T.booking_confirmation(
            "Jose", ADDR, "Friday, November 20, at 6:00 PM", "Jace Johnson"),
         "at": datetime(2026, 11, 19, 9, 1, tzinfo=AZ)},
        {"id": "a-2", "from": "alex", "text": alex_text, "at": alex_at},
        {"id": "m-yes", "from": "renter", "text": "Yes that works",
         "at": FRI_540PM},
    ]


def _wire_u83(monkeypatch, now, cls_by_text, msgs, events=()):
    calls = _wire(monkeypatch, now, cls_by_text, events=events, msgs=msgs)
    monkeypatch.setattr(responder.gm, "msg_time", lambda m: m.get("at"))
    calls["removed"] = []
    monkeypatch.setattr(responder.cal, "remove_renter_or_cancel",
                        lambda ev, name: calls["removed"].append((ev, name))
                        or "removed")
    return calls


# ------------------------------------------------------- (a) hand offer books

def test_yes_to_alex_hand_typed_offer_books_that_slot(monkeypatch):
    msgs = _thread(ALEX_HAND)
    calls = _wire_u83(monkeypatch, FRI_540PM,
                      {"Yes that works": _cls("accept_offer", [])}, msgs)
    out = responder.handle_reply("t-jose", _booked_doc(), msgs,
                                 "Yes that works", "m-yes")
    assert out == "sent"
    assert calls["created"] == [(SAT_10AM, "Alex Foley")]
    assert calls["nh"] == []                       # no Needs-you Poke
    body = calls["replies"][0]
    assert "You're all set" in body and "10:00 AM" in body
    assert calls["reviews"] == ["booking_new"]     # review gate still runs
    st, f = calls["transitions"][-1]
    assert st == responder.ledger.BOOKED
    assert f["booked_start_iso"] == SAT_10AM.isoformat()
    # The reschedule takes Jose off Jace's 6pm showing (fold-aware remove).
    assert calls["removed"] == [("evJACE6", "Jose")]


def test_hand_offer_on_an_unbooked_thread_books_and_removes_nothing(monkeypatch):
    msgs = _thread(ALEX_HAND)
    calls = _wire_u83(monkeypatch, FRI_540PM,
                      {"Yes that works": _cls("accept_offer", [])}, msgs)
    doc = {"renter_name": "Emilsa", "property_address": ADDR,
           "state": responder.ledger.NEEDS_HUMAN
           if hasattr(responder.ledger, "NEEDS_HUMAN") else "NEEDS_HUMAN"}
    assert responder.handle_reply("t-em", doc, msgs, "Yes that works",
                                  "m-yes") == "sent"
    assert calls["created"] == [(SAT_10AM, "Alex Foley")]
    assert calls["removed"] == [] and calls["nh"] == []


def test_hand_offer_the_renter_declined_is_not_booked(monkeypatch):
    msgs = _thread(ALEX_HAND)
    cls = _cls("accept_offer", [])
    cls["declined_times"] = [{"date": "2026-11-21", "time": "10:00"}]
    calls = _wire_u83(monkeypatch, FRI_540PM, {"Yes that works": cls}, msgs)
    out = responder.handle_reply("t-jose", _booked_doc(), msgs,
                                 "Yes that works", "m-yes")
    assert out == "no-send:hand-offer-invalid"
    assert calls["created"] == [] and calls["replies"] == []
    assert len(calls["nh"]) == 1


def test_renter_message_after_alex_offer_means_no_hand_offer(monkeypatch):
    msgs = _thread(ALEX_HAND)
    msgs.insert(3, {"id": "m-mid", "from": "renter", "text": "hmm let me check",
                    "at": datetime(2026, 11, 20, 15, 0, tzinfo=AZ)})
    assert responder._hand_offer_slot(msgs, "m-yes", FRI_540PM) is None


# ------------------------------------------------------- (b) vague still Pokes

@pytest.mark.parametrize("alex_text", [
    "Hi Jose, Jace got sick. Can we do tomorrow morning instead?",
    "Could you do tomorrow at 10 or 11?",
    "Can you come at 10?",                         # no day
    "How about Saturday at 8?",                    # 8am or 8pm?
])
def test_vague_hand_offer_still_pokes(monkeypatch, alex_text):
    msgs = _thread(alex_text)
    calls = _wire_u83(monkeypatch, FRI_540PM,
                      {"Yes that works": _cls("accept_offer", [])}, msgs)
    out = responder.handle_reply("t-jose", _booked_doc(), msgs,
                                 "Yes that works", "m-yes")
    assert out == "no-send:accept-no-live-offer"
    assert calls["created"] == [] and calls["replies"] == []
    assert "no live offer stands" in calls["nh"][0][3]


@pytest.mark.parametrize("text,want", [
    (ALEX_HAND, SAT_10AM),
    ("How about Saturday at 10:30am?", datetime(2026, 11, 21, 10, 30, tzinfo=AZ)),
    ("I can do 11/21 at 2pm at 2701 E Willetta",
     datetime(2026, 11, 21, 14, 0, tzinfo=AZ)),
    ("tomorrow at noon works", datetime(2026, 11, 21, 12, 0, tzinfo=AZ)),
    ("Mon, Wed, Fri: 10:00 AM to 6:30 PM", None),   # windows_ask shape
    (T.booking_confirmation("Jose", ADDR, "tomorrow, Saturday, November 21, "
                            "at 10:00 AM", "Alex Foley"), None),
])
def test_hand_offer_parser(text, want):
    assert rules.hand_offer_slot(text, FRI_1PM) == want


# ------------------------------------------- (c) confirmation on a booked tour

@pytest.mark.parametrize("text,cls", [
    ("I will be there at 6pm today", _cls("accept_offer", [])),
    ("I will be there at 6pm today",
     _cls("propose_time", [_cand("2026-11-20", "18:00", raw="6pm today")])),
    ("Yes, will be there", _cls("accept_offer", [])),
])
def test_confirmation_of_booked_tour_is_quiet(monkeypatch, text, cls):
    msgs = [{"id": "a-1", "from": "alex", "text": T.showing_reminder_2h(
                "Jose", ADDR, "today, Friday, November 20, at 6:00 PM",
                "Jace Johnson"), "at": datetime(2026, 11, 20, 13, 30, tzinfo=AZ)},
            {"id": "m-c", "from": "renter", "text": text,
             "at": datetime(2026, 11, 20, 13, 43, tzinfo=AZ)}]
    now = datetime(2026, 11, 20, 13, 43, tzinfo=AZ)
    calls = _wire_u83(monkeypatch, now, {text: cls}, msgs)
    out = responder.handle_reply("t-jose", _booked_doc(), msgs, text, "m-c")
    assert out == "no-send:booked-confirmation"
    assert calls["nh"] == [] and calls["pings"] == []
    assert calls["created"] == [] and calls["replies"] == []
    assert calls["transitions"] == []


# ------------------------------- (d) booked thread, different time: unchanged

def test_booked_renter_asking_for_a_different_time_takes_the_old_path(monkeypatch):
    text = "Can we do 4pm instead?"
    cls = _cls("propose_time", [_cand("2026-11-20", "16:00", raw="4pm")])
    msgs = [{"id": "m-d", "from": "renter", "text": text,
             "at": datetime(2026, 11, 20, 9, 0, tzinfo=AZ)}]
    now = datetime(2026, 11, 20, 9, 0, tzinfo=AZ)
    calls = _wire_u83(monkeypatch, now, {text: cls}, msgs)
    seen = []
    monkeypatch.setattr(responder, "book_proposed_time",
                        lambda *a, **k: seen.append(a[5]) or "sent")
    out = responder.handle_reply("t-jose", _booked_doc(), msgs, text, "m-d")
    assert out == "sent"
    assert seen and seen[0]["time_candidates"][0]["time"] == "16:00"


def test_booked_yes_naming_another_time_still_pokes(monkeypatch):
    text = "yes I can be there at 4"
    msgs = [{"id": "m-e", "from": "renter", "text": text,
             "at": datetime(2026, 11, 20, 9, 0, tzinfo=AZ)}]
    now = datetime(2026, 11, 20, 9, 0, tzinfo=AZ)
    calls = _wire_u83(monkeypatch, now,
                      {text: _cls("accept_offer",
                                  [_cand("2026-11-20", "16:00", raw="4")])}, msgs)
    out = responder.handle_reply("t-jose", _booked_doc(), msgs, text, "m-e")
    assert out == "no-send:accept-no-live-offer"
    assert len(calls["nh"]) == 1


def test_bare_yes_on_booked_thread_with_nothing_to_answer_still_pokes(monkeypatch):
    text = "Yes"
    msgs = [{"id": "m-f", "from": "renter", "text": text,
             "at": datetime(2026, 11, 20, 9, 0, tzinfo=AZ)}]
    now = datetime(2026, 11, 20, 9, 0, tzinfo=AZ)
    calls = _wire_u83(monkeypatch, now, {text: _cls("accept_offer", [])}, msgs)
    out = responder.handle_reply("t-jose", _booked_doc(), msgs, text, "m-f")
    assert out == "no-send:accept-no-live-offer"
    assert len(calls["nh"]) == 1
