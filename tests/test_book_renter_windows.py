"""A renter who gives a day, a time or a window gets booked, not asked again
(Alex 2026-11-08: "I feel like we need to get rid of the poke nudge system and
just book things"). In the week before, 13 of 37 renter messages ended
review-blocked with a Poke and nothing sent. Every test drives the real
responder functions; only Gmail, Firestore, the calendar API and the LLM are
replaced, the same way the other responder tests do it."""
import copy
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import responder  # noqa: E402
import rules  # noqa: E402
import templates as T  # noqa: E402

AZ = rules.AZ_TZ
ADDR = "220 N 22nd Pl #2094, Mesa, AZ, 85213"
RELAY = "relay@convo.zillow.com"

TJ = ("Any chance you'd be free for a tour this Wednesday between 2 and 5 pm? "
      "Or this Friday anytime after 2 pm?")
SUN_235PM = datetime(2026, 11, 8, 14, 35, tzinfo=AZ)   # when TJ wrote


def _cand(d, t=None, after=None, before=None, raw=""):
    return {"date": d, "time": t, "after": after, "before": before, "raw": raw}


def _cls(intent, cands, question=None, same_day=False):
    return {"intent": intent, "time_candidates": cands,
            "wants_same_day": same_day, "question_text": question,
            "cancel_reason": None, "constraints": [], "earliest_daily": None,
            "latest_daily": None, "declined_times": []}


TJ_CLS = _cls("question", [
    _cand("2026-11-11", after="14:00", before="17:00",
          raw="this Wednesday between 2 and 5 pm"),
    _cand("2026-11-13", after="14:00", raw="this Friday anytime after 2 pm")])


def _ev(summary, start, end, location=""):
    return {"summary": summary, "location": location,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()}}


def _wire(monkeypatch, now, cls_by_text, events=(), reviews=(), msgs=()):
    """Everything side-effectful recorded instead of executed. cls_by_text
    maps a phrase in the renter's text to what the classifier returns."""
    calls = {"created": [], "replies": [], "reserved": [], "pings": [],
             "nh": [], "upserts": [], "transitions": [], "reviews": []}

    class FakeNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    def classify(text, last_alex, now_phx, transcript=None):
        for key, cls in cls_by_text.items():
            if key in (text or ""):
                return copy.deepcopy(cls)
        return _cls("other", [])

    queue = list(reviews)

    def gate(thread_id, body, template, msgs=None):
        calls["reviews"].append(template)
        return queue.pop(0) if queue else (True, "")

    led = responder.ledger
    monkeypatch.setattr(responder, "datetime", FakeNow)
    monkeypatch.setattr(responder, "dry_run", lambda: False)
    monkeypatch.setattr(responder.llm, "classify_reply", classify)
    monkeypatch.setattr(responder, "review_gate", gate)
    monkeypatch.setattr(responder, "needs_human",
                        lambda *a, **k: calls["nh"].append(a))
    monkeypatch.setattr(led, "blocked_addresses", lambda: [])
    monkeypatch.setattr(led, "create_thread", lambda *a, **k: None)
    monkeypatch.setattr(led, "get_thread", lambda t: {})
    monkeypatch.setattr(led, "record_message_outcome", lambda *a, **k: None)
    monkeypatch.setattr(led, "upsert_thread",
                        lambda t, **f: calls["upserts"].append(f))
    monkeypatch.setattr(led, "transition",
                        lambda t, st, **f: calls["transitions"].append((st, f)))
    monkeypatch.setattr(led, "reserve_send",
                        lambda t, s, m: calls["reserved"].append(s) or "acquired")
    monkeypatch.setattr(led, "mark_sent", lambda *a, **k: None)
    monkeypatch.setattr(led, "mark_failed", lambda *a, **k: None)
    monkeypatch.setattr(led, "set_labels_pending", lambda *a, **k: None)
    monkeypatch.setattr(responder.cal, "list_events", lambda n=7: list(events))
    monkeypatch.setattr(responder.cal, "create_showing_event",
                        lambda a, n, s, ag: calls["created"].append(
                            (s, ag["name"])) or "evNEW")
    monkeypatch.setattr(responder.gm, "send_reply",
                        lambda t, r, b: calls["replies"].append(b))
    monkeypatch.setattr(responder.gm, "modify_labels", lambda *a, **k: None)
    monkeypatch.setattr(responder.gm, "poke_ping",
                        lambda m: calls["pings"].append(m) or True)
    monkeypatch.setattr(responder.gm, "fetch_thread", lambda t: list(msgs))
    monkeypatch.setattr(responder.gm, "relay_from_thread", lambda m: RELAY)
    monkeypatch.setattr(responder.gm, "thread_transcript", lambda m: "")
    monkeypatch.setattr(responder.gm, "msg_body", lambda m: m.get("text", ""))
    monkeypatch.setattr(responder.gm, "is_from_alex",
                        lambda m: m.get("from") == "alex")
    monkeypatch.setattr(responder.gm, "is_from_relay",
                        lambda m: m.get("from") == "renter")
    monkeypatch.setattr(responder.gm, "extract_renter_text",
                        lambda m: m.get("text", ""))
    monkeypatch.setattr(responder.gm, "msg_id", lambda m: m.get("id", ""))
    monkeypatch.setattr(responder.gm, "msg_time", lambda m: None)
    return calls


# ------------------------------------------------- A. first message books

def test_tj_first_message_books_wednesday_inside_his_window(monkeypatch):
    calls = _wire(monkeypatch, SUN_235PM, {"Wednesday between 2": TJ_CLS})
    out = responder.handle_new_inquiry("t-tj", "TJ", ADDR, RELAY, TJ, "m-tj")
    assert out == "sent"
    assert calls["created"] == [(datetime(2026, 11, 11, 14, 0, tzinfo=AZ),
                                 "Alex Foley")]
    assert calls["reserved"] == ["first_reply"]   # still one send per inquiry
    body = calls["replies"][0]
    assert "You're all set" in body and "Wednesday, November 11" in body
    assert "2:00 PM" in body
    assert "Great question" not in body           # his "?" was the tour ask
    assert "what day and time" not in body.lower()
    assert calls["nh"] == []                      # nobody Poked
    assert calls["pings"][0].startswith("Booked: TJ")   # booked FYI stays


def test_window_prefers_an_alex_slot_over_a_cover_agent(monkeypatch):
    busy = _ev("Owner meeting", datetime(2026, 11, 11, 13, 45, tzinfo=AZ),
               datetime(2026, 11, 11, 14, 40, tzinfo=AZ),
               location="1 N Center St, Mesa, AZ, 85213")
    calls = _wire(monkeypatch, SUN_235PM, {"Wednesday between 2": TJ_CLS},
                  events=[busy])
    assert responder.handle_new_inquiry("t-tj", "TJ", ADDR, RELAY, TJ,
                                        "m-tj") == "sent"
    assert calls["created"] == [(datetime(2026, 11, 11, 15, 0, tzinfo=AZ),
                                 "Alex Foley")]


def test_first_message_with_a_specific_time_books_it(monkeypatch):
    text = "Is it still available? Could I see it Tuesday at 11am?"
    cls = _cls("propose_time", [_cand("2026-11-10", "11:00",
                                      raw="Tuesday at 11am")])
    calls = _wire(monkeypatch, SUN_235PM, {"Tuesday at 11am": cls})
    out = responder.handle_new_inquiry("t-1", "Dana", ADDR, RELAY, text, "m1")
    assert out == "sent"
    assert calls["created"] == [(datetime(2026, 11, 10, 11, 0, tzinfo=AZ),
                                 "Alex Foley")]
    assert "Tuesday, November 10" in calls["replies"][0]


def test_first_message_with_a_bare_day_gets_times_on_that_day(monkeypatch):
    text = "Could I see it Saturday?"
    cls = _cls("vague_time", [_cand("2026-11-14", raw="Saturday")])
    calls = _wire(monkeypatch, SUN_235PM, {"Saturday": cls})
    out = responder.handle_new_inquiry("t-2", "Ashley", ADDR, RELAY, text, "m2")
    assert out == "sent"
    assert calls["created"] == []
    body = calls["replies"][0]
    assert body.count("Saturday, November 14") == 2
    proposed = calls["upserts"][-1]["proposed_slots_iso"]
    assert all(p.startswith("2026-11-14") for p in proposed)


def test_first_message_without_a_day_keeps_the_plain_ask(monkeypatch):
    calls = _wire(monkeypatch, SUN_235PM, {})
    out = responder.handle_new_inquiry("t-3", "Sam", ADDR, RELAY,
                                       "I would like to schedule a tour.", "m3")
    assert out == "sent"
    assert "what day and time works" in calls["replies"][0]


# ------------------------------------------------- B. their day, with a why

def test_saturday_after_hours_counters_on_saturday_with_the_reason(monkeypatch):
    # Crystal 2026-11-06: "530 would be 330 your time" for Saturday got
    # Tuesday times with no why, and the review blocked it.
    now = datetime(2026, 11, 6, 13, 0, tzinfo=AZ)      # Friday
    cls = _cls("propose_time", [_cand("2026-11-07", "15:30",
                                      raw="530 would be 330 your time")])
    calls = _wire(monkeypatch, now, {"330 your time": cls})
    doc = {"renter_name": "Crystal", "property_address": ADDR,
           "relay_email": RELAY, "last_reply_template": "propose_times"}
    out = responder.handle_reply("t-c", doc, [], "I guess 530 would be 330 "
                                 "your time", "m4")
    assert out == "sent"
    body = calls["replies"][0]
    assert ("My last tour on Saturdays starts at 1:30 PM (Arizona time), "
            "so 3:30 PM is past that.") in body
    assert body.count("Saturday, November 7") == 2
    assert calls["nh"] == []


def test_friday_evening_past_close_pushes_saturday(monkeypatch):
    # Marcus 2026-11-11, Willetta: "I could do Friday at 630pm". The counter
    # said 6:30 "falls outside" Fridays 10:00 AM to 6:30 PM, the review
    # blocked the contradiction, and Alex asked why we weren't pushing Saturday.
    now = datetime(2026, 11, 11, 20, 45, tzinfo=AZ)    # Wednesday night
    cls = _cls("propose_time", [_cand("2026-11-13", "18:30",
                                      raw="Friday at 630pm")])
    calls = _wire(monkeypatch, now, {"Friday at 630pm": cls})
    doc = {"renter_name": "Marcus", "property_address": ADDR,
           "relay_email": RELAY, "last_reply_template": "availability_ask"}
    out = responder.handle_reply("t-mv", doc, [], "hello, I was hoping to move "
                                 "in by December 6th... I could do Friday at "
                                 "630pm.", "m-mv")
    assert out == "sent"
    body = calls["replies"][0]
    assert ("My last tour on Fridays starts at 6:00 PM (Arizona time), so "
            "6:30 PM is past that.") in body
    assert body.count("Saturday, November 14") == 2
    assert "Friday, November 13" not in body
    assert calls["created"] == [] and calls["nh"] == []


def test_monday_evening_past_close_offers_saturday_then_sunday(monkeypatch):
    now = datetime(2026, 11, 9, 9, 0, tzinfo=AZ)       # Monday morning
    cls = _cls("propose_time", [_cand("2026-11-09", "19:00",
                                      raw="tonight at 7")])
    sat_off = {"summary": "Out of town", "start": {"date": "2026-11-14"},
               "end": {"date": "2026-11-15"}}
    calls = _wire(monkeypatch, now, {"tonight at 7": cls}, events=[sat_off])
    doc = {"renter_name": "Jo", "property_address": ADDR,
           "relay_email": RELAY, "last_reply_template": "availability_ask"}
    assert responder.handle_reply("t-jo", doc, [], "Could I come tonight at "
                                  "7?", "m-jo") == "sent"
    body = calls["replies"][0]
    assert "My last tour on Mondays starts at 6:00 PM" in body
    assert body.count("Sunday, November 15") == 2
    assert "Monday, November 9" not in body and "Tuesday" not in body


def test_sunday_ask_with_sunday_full_says_so(monkeypatch):
    # 2026-11-05: "Can we come by on Sunday?" got Monday with no why.
    now = datetime(2026, 11, 5, 14, 54, tzinfo=AZ)     # Thursday
    away = {"summary": "Out of town", "start": {"date": "2026-11-08"},
            "end": {"date": "2026-11-09"}}
    cls = _cls("vague_time", [_cand("2026-11-08", raw="Sunday")])
    calls = _wire(monkeypatch, now, {"Sunday": cls}, events=[away])
    doc = {"renter_name": "Mia", "property_address": ADDR,
           "relay_email": RELAY, "last_reply_template": "availability_ask"}
    out = responder.handle_reply("t-m", doc, [], "Can we come by on Sunday?",
                                 "m5")
    assert out == "sent"
    body = calls["replies"][0]
    assert "Sunday is already booked up on my end." in body
    assert "Sunday, November 8" not in body


def test_sunday_ask_with_sunday_open_offers_sunday(monkeypatch):
    now = datetime(2026, 11, 5, 14, 54, tzinfo=AZ)
    cls = _cls("vague_time", [_cand("2026-11-08", raw="this Sunday")])
    calls = _wire(monkeypatch, now, {"Sunday": cls})
    doc = {"renter_name": "Mia", "property_address": ADDR,
           "relay_email": RELAY, "last_reply_template": "availability_ask"}
    assert responder.handle_reply("t-m", doc, [], "Available this Sunday "
                                  "for a tour!", "m6") == "sent"
    assert "Sunday, November 8" in calls["replies"][0]
    assert "booked up" not in calls["replies"][0]


def test_reply_with_a_day_window_books_instead_of_offering(monkeypatch):
    now = datetime(2026, 11, 9, 9, 0, tzinfo=AZ)       # Monday
    cls = _cls("vague_time", [_cand("2026-11-11", after="12:00",
                                    raw="Wednesday afternoon")])
    calls = _wire(monkeypatch, now, {"Wednesday afternoon": cls})
    doc = {"renter_name": "Lee", "property_address": ADDR,
           "relay_email": RELAY, "last_reply_template": "availability_ask"}
    assert responder.handle_reply("t-l", doc, [], "Wednesday afternoon "
                                  "works", "m7") == "sent"
    assert calls["created"] == [(datetime(2026, 11, 11, 12, 0, tzinfo=AZ),
                                 "Alex Foley")]


def test_reason_lines_read_plainly():
    sat = date(2026, 11, 7)
    items = [{"kind": "window", "date": sat, "slots": [],
              "after": "15:00", "before": None}]
    assert responder._reason_line(items, []) == (
        "I only show homes on Saturdays from 10:00 AM to 2:00 PM (Arizona "
        "time), so Saturday, November 7 after 3:00 PM falls outside that.")
    wed = {"kind": "window", "date": date(2026, 11, 11),
           "slots": responder._window_slots(date(2026, 11, 11), "14:00",
                                            "17:00"),
           "after": "14:00", "before": "17:00"}
    assert [s.strftime("%H:%M") for s in wed["slots"]] == [
        "14:00", "14:30", "15:00", "15:30", "16:00", "16:30"]
    assert responder._reason_line([wed], [(wed, "alex-busy")]) == (
        "Wednesday, November 11 between 2:00 PM and 5:00 PM is already "
        "booked up on my end.")
    body = T.counter_proposal("Sam", ["Monday, November 9, at 10:00 AM"],
                              reason="Sunday is already booked up on my end.")
    assert "Sunday is already booked up on my end. Here's what I can do" in body
    assert "doesn't quite work" not in body


# ------------------------------------------------- C. the 12:30 bug

def test_yes_to_the_second_proposal_books_that_time(monkeypatch):
    # Crystal 2026-11-08: offered Tuesday 12:00 and 12:30, she wrote
    # "tuesday at 1230 would be great", and 12:00 was confirmed.
    now = datetime(2026, 11, 8, 9, 54, tzinfo=AZ)
    noon = datetime(2026, 11, 10, 12, 0, tzinfo=AZ)
    half = datetime(2026, 11, 10, 12, 30, tzinfo=AZ)
    calls = _wire(monkeypatch, now,
                  {"1230": _cls("accept_offer", [])})
    doc = {"renter_name": "Crystal", "property_address": ADDR,
           "relay_email": RELAY, "last_reply_template": "propose_times",
           "proposed_slots_iso": [noon.isoformat(), half.isoformat()]}
    out = responder.handle_reply("t-c", doc, [], "tuesday at 1230 would be "
                                 "great", "m8")
    assert out == "sent"
    assert calls["created"] == [(half, "Alex Foley")]
    assert "12:30 PM" in calls["replies"][0]


def test_pick_proposed_slot_bare_yes_takes_the_first():
    now = datetime(2026, 11, 8, 9, 54, tzinfo=AZ)
    a = datetime(2026, 11, 10, 12, 0, tzinfo=AZ)
    b = datetime(2026, 11, 10, 13, 30, tzinfo=AZ)
    doc = {"proposed_slots_iso": [a.isoformat(), b.isoformat()]}
    assert responder._pick_proposed_slot(doc, {}, "Sounds good!", now) == a
    assert responder._pick_proposed_slot(doc, {}, "the 1:30 please", now) == b
    assert responder._pick_proposed_slot(doc, {}, "130pm works", now) == b
    cls = {"time_candidates": [_cand("2026-11-10", "13:30")]}
    assert responder._pick_proposed_slot(doc, cls, "yes", now) == b


# ------------------------------------------------- D. block, then book

def test_blocked_windows_ask_falls_back_to_booking_their_day(monkeypatch):
    # 2026-11-06: the renter had already named a day; the windows-ask was
    # blocked and Alex was Poked. Now the day gets booked and nobody is Poked.
    now = datetime(2026, 11, 9, 9, 0, tzinfo=AZ)
    msgs = [{"from": "renter", "id": "m1",
             "text": "Wednesday between 2 and 5 works for us"},
            {"from": "alex", "id": "a1", "text": "Here are a few times"},
            {"from": "renter", "id": "m2",
             "text": "If this rental is still available, we would like to tour"}]
    window = _cls("vague_time", [_cand("2026-11-11", after="14:00",
                                       before="17:00")])
    calls = _wire(monkeypatch, now,
                  {"Wednesday between 2": window,
                   "still available": _cls("vague_time", [])},
                  reviews=[(False, "Renter already proposed Wednesday")],
                  msgs=msgs)
    doc = {"renter_name": "Ray", "property_address": ADDR,
           "relay_email": RELAY, "last_reply_template": "propose_times"}
    out = responder.handle_reply("t-r", doc, msgs, msgs[-1]["text"], "m2")
    assert out == "sent"
    assert calls["reviews"] == ["windows_ask", "booking_new"]
    assert calls["reserved"] == ["reply__m2", "reply__m2__retry"]
    assert calls["created"] == [(datetime(2026, 11, 11, 14, 0, tzinfo=AZ),
                                 "Alex Foley")]
    assert calls["nh"] == []


def test_blocked_reply_with_nothing_to_book_still_escalates(monkeypatch):
    now = datetime(2026, 11, 9, 9, 0, tzinfo=AZ)
    msgs = [{"from": "renter", "id": "m2", "text": "I would like to tour"}]
    calls = _wire(monkeypatch, now, {"tour": _cls("vague_time", [])},
                  reviews=[(False, "repeats the windows")], msgs=msgs)
    doc = {"renter_name": "Ray", "property_address": ADDR,
           "relay_email": RELAY, "last_reply_template": "availability_ask"}
    out = responder.handle_reply("t-r", doc, msgs, "I would like to tour", "m2")
    assert out == "no-send:review-blocked"
    assert calls["replies"] == []
    assert len(calls["nh"]) == 1
    assert "held for a second look" in calls["nh"][0][3]


def test_has_schedule_hint():
    assert rules.has_schedule_hint(TJ)
    assert rules.has_schedule_hint("free tomorrow?")
    assert rules.has_schedule_hint("could do 12:30")
    assert not rules.has_schedule_hint("I would like to schedule a tour.")
    assert not rules.has_schedule_hint("Is the address 220 N 22nd Pl?")


def test_window_ignores_a_same_house_showing_outside_the_window(monkeypatch):
    # A 10:00 showing at the house is no use to a renter who said 2 to 5:
    # book inside the window instead of offering 10:00.
    from datetime import timedelta
    wed = datetime.now(AZ).date() + timedelta(days=14)
    while wed.weekday() != 2:
        wed += timedelta(days=1)
    now = datetime(wed.year, wed.month, wed.day, 8, 0, tzinfo=AZ) - timedelta(days=2)
    there = _ev("Showing at 220 N 22nd Pl #2094 with Kim",
                datetime(wed.year, wed.month, wed.day, 10, 0, tzinfo=AZ),
                datetime(wed.year, wed.month, wed.day, 10, 30, tzinfo=AZ),
                location=ADDR)
    there["id"] = "evK"
    cls = _cls("vague_time", [_cand(wed.isoformat(), after="14:00",
                                    before="17:00")])
    calls = _wire(monkeypatch, now, {"between 2 and 5": cls}, events=[there])
    doc = {"renter_name": "Lee", "property_address": ADDR,
           "relay_email": RELAY, "last_reply_template": "availability_ask"}
    assert responder.handle_reply("t-l", doc, [], "Wednesday between 2 and 5",
                                  "m9") == "sent"
    assert calls["created"] == [(datetime(wed.year, wed.month, wed.day, 14, 0,
                                          tzinfo=AZ), "Alex Foley")]


# ------------------------------------------------- today is full: no Poke

def test_wants_today_when_full_offers_times_without_a_poke(monkeypatch):
    # Alex 2026-11-08: "kill the today poke". 7pm Sunday, nothing left today.
    sun_7pm = datetime(2026, 11, 8, 19, 0, tzinfo=AZ)
    calls = _wire(monkeypatch, sun_7pm, {})
    out = responder._offer_times(
        "t1", {}, "TJ", ADDR, RELAY, _cls("vague_time", [], same_day=True),
        "can I see it today?", "m1", sun_7pm, [], wants_today=True)
    assert out == "sent"
    assert calls["nh"] == [] and calls["pings"] == []
    assert len(calls["replies"]) == 1
