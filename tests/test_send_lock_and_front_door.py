"""Coverage for three paths where a failure costs a real renter something.

Everything renter-facing in this service funnels through a small number of
functions that, until now, no test ever executed - the existing suites
monkeypatch them away to get at the logic downstream. Each one below is the
last gate before an irreversible side effect:

1. responder.send_stage - "the ONLY function that sends renter email".
   Its reserve -> recover -> takeover -> review -> send -> label sequence is
   what stands between the renter and a duplicate email, and between a
   failed send and a silently dropped reply. Every existing test replaces it
   with `lambda *a, **k: "sent"`.

2. responder.handle_new_inquiry - the front door. It is the only caller of
   the blocked-address list on a first contact, so it decides whether a
   renter at a leased home gets "that home has been rented" or an invitation
   to tour it, and it is what writes the thread doc the rest of the state
   machine reads.

3. responder.book_accepted_offer - the fold-and-confirm booking path. It
   mutates a calendar event and tells a renter to show up somewhere. It had
   no coverage at all, including the 8/25 guard that books the event the
   thread actually offered rather than whatever showing is soonest.

The external edges (Composio Gmail, Google Calendar, Firestore) are captured
rather than executed; everything above them - templates, the standing-rule
router, the calendar lookup, the address matcher, the review gate - is the
real production code, and the assertions are on the bytes that would have
gone out and the fields that would have landed on the docs.
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import calendar_logic as cal  # noqa: E402
import facts  # noqa: E402
import ledger  # noqa: E402
import responder  # noqa: E402
import rules  # noqa: E402
import templates as T  # noqa: E402

AZ = rules.AZ_TZ

RELAY = "dana.7f3k@convo.zillow.com"
ALEX = "alex@azfoleyhomes.com"

# Two real entries from the leased list this pipeline gates on.
BLOCKED = ["1641 E Coronado Rd", "35031 N Palomino Way"]
LEASED_ADDR = "1641 E Coronado Rd, Phoenix, AZ 85006"
OPEN_ADDR = "2118 S El Marino, Mesa, AZ, 85202"


def _future_wed():
    """Next Wednesday, strictly ahead of the real clock: the showing lookup
    filters on now + 2h, so a pinned date would expire the fixture."""
    d = datetime.now(AZ).date() + timedelta(days=1)
    while d.weekday() != 2:
        d += timedelta(days=1)
    return d


def _showing_event(event_id, hhmm, address=OPEN_ADDR, inquirer="Chelsea",
                   agent="Rhett Lueck", agent_email="rhettlueck@gmail.com"):
    """A Google Calendar event in the shape create_showing_event leaves behind."""
    d = _future_wed()
    h, m = hhmm.split(":")
    end = f"{int(h):02d}:{int(m) + 30:02d}" if int(m) + 30 < 60 \
        else f"{int(h) + 1:02d}:{int(m) - 30:02d}"
    return {
        "id": event_id,
        "summary": f"Showing — {address.split(',')[0]} with {inquirer}",
        "location": address,
        "description": (f"Zillow inquiry.\nInquirers: {inquirer}.\n"
                        f"Agent: {agent} ({agent_email})."),
        "start": {"dateTime": f"{d}T{hhmm}:00-07:00"},
        "end": {"dateTime": f"{d}T{end}:00-07:00"},
    }


def _relay_msg(message_id, body="Hi, I'm interested in this home."):
    return {"messageId": message_id, "sender": RELAY, "messageText": body,
            "messageTimestamp": "2026-09-21T10:00:00+00:00"}


def _alex_msg(message_id, body="Hi Dana, happy to get you in."):
    return {"messageId": message_id, "sender": ALEX, "messageText": body,
            "messageTimestamp": "2026-09-21T10:05:00+00:00"}


class Edges:
    """Captures every irreversible edge the responder can touch, so a test can
    assert on what WOULD have been sent, written and changed.

    Firestore docs are kept as merged dicts (set(merge=True) semantics) so a
    later write does not erase an earlier field, which is what the real
    zillow_sends / zillow_threads docs do.
    """

    def __init__(self, monkeypatch, *, blocked=None, thread_msgs=None,
                 reserve="acquired", takeover=True, review=None,
                 send_error=None, label_error=None, live=True):
        self.reserve_verdict = reserve
        self.takeover_ok = takeover
        self.review = review or {"verdict": "ok", "reason": ""}
        self.send_error = send_error
        self.label_error = label_error
        self.thread_msgs = thread_msgs if thread_msgs is not None else []

        self.threads = {}        # thread_id -> merged zillow_threads doc
        self.send_docs = {}      # (thread_id, stage) -> merged zillow_sends doc
        self.shadow = {}         # (thread_id, stage) -> zillow_shadow doc
        self.reserved = []       # (thread_id, stage, meta) as reserve_send saw it
        self.emails = []         # (thread_id, relay, body) actually delivered
        self.labels = []         # (thread_id, add, remove) actually applied
        self.pokes = []
        self.alerts = []
        self.folds = []          # (event_id, first_name)

        mp = monkeypatch
        mp.setattr(responder, "dry_run", lambda: not live)
        mp.setattr(responder.llm, "review_reply",
                   lambda transcript, body, template: dict(self.review))

        mp.setattr(ledger, "blocked_addresses",
                   lambda: list(BLOCKED if blocked is None else blocked))
        mp.setattr(ledger, "get_thread", lambda tid: dict(self.threads.get(tid, {})))
        mp.setattr(ledger, "upsert_thread", self._upsert_thread)
        mp.setattr(ledger, "create_thread", self._create_thread)
        mp.setattr(ledger, "transition", self._transition)
        mp.setattr(ledger, "reserve_send", self._reserve_send)
        mp.setattr(ledger, "takeover_send", self._takeover_send)
        mp.setattr(ledger, "mark_sent", self._mark_sent)
        mp.setattr(ledger, "mark_failed", self._mark_failed)
        mp.setattr(ledger, "set_labels_pending", self._set_labels_pending)
        mp.setattr(ledger, "write_shadow", self._write_shadow)
        mp.setattr(ledger, "get_showing", lambda eid: None)
        mp.setattr(ledger, "upsert_showing", lambda eid, **f: None)

        mp.setattr(responder.gm, "fetch_thread", lambda tid: list(self.thread_msgs))
        mp.setattr(responder.gm, "send_reply", self._send_reply)
        mp.setattr(responder.gm, "modify_labels", self._modify_labels)
        mp.setattr(responder.gm, "poke_ping",
                   lambda msg: self.pokes.append(msg) or True)
        mp.setattr(responder.gm, "alert_email",
                   lambda subj, body: self.alerts.append((subj, body)) or True)

        mp.setattr(cal, "fold_renter_into_event", self._fold)

    # -- zillow_threads -----------------------------------------------------
    def _upsert_thread(self, thread_id, **fields):
        self.threads.setdefault(thread_id, {}).update(fields)

    def _create_thread(self, thread_id, **fields):
        fields.setdefault("state", ledger.NEW)
        self._upsert_thread(thread_id, **fields)

    def _transition(self, thread_id, state, **fields):
        self._upsert_thread(thread_id, state=state, **fields)

    # -- zillow_sends -------------------------------------------------------
    def _send_doc(self, thread_id, stage):
        return self.send_docs.setdefault((thread_id, stage), {})

    def _reserve_send(self, thread_id, stage, meta):
        self.reserved.append((thread_id, stage, dict(meta)))
        self._send_doc(thread_id, stage).update(dict(meta), status="reserved")
        return self.reserve_verdict

    def _takeover_send(self, thread_id, stage):
        return self.takeover_ok

    def _mark_sent(self, thread_id, stage, **fields):
        self._send_doc(thread_id, stage).update(fields, status="sent")

    def _mark_failed(self, thread_id, stage, error):
        self._send_doc(thread_id, stage).update(status="failed", error=str(error))

    def _set_labels_pending(self, thread_id, stage, add, remove):
        self._send_doc(thread_id, stage).update(
            labels_pending=True, labels_add=list(add), labels_remove=list(remove))

    def _write_shadow(self, thread_id, stage, payload):
        self.shadow[(thread_id, stage)] = dict(payload)

    # -- Composio -----------------------------------------------------------
    def _send_reply(self, thread_id, relay, body):
        if self.send_error:
            raise self.send_error
        self.emails.append((thread_id, relay, body))
        return {"successful": True}

    def _modify_labels(self, thread_id, add, remove):
        if self.label_error:
            raise self.label_error
        self.labels.append((thread_id, list(add), list(remove)))
        return {"successful": True}

    def _fold(self, event, first_name):
        self.folds.append((event.get("id"), first_name))
        return event.get("id")

    # -- helpers ------------------------------------------------------------
    def only_body(self):
        assert len(self.emails) == 1, f"expected one email, got {len(self.emails)}"
        return self.emails[0][2]


# ======================================================================
# 1. send_stage - the single gate every renter email passes through
# ======================================================================

FIRST_BODY = T.availability_ask("Dana", OPEN_ADDR)


def _send_stage(edges, stage="first_reply", body=FIRST_BODY,
                add=None, remove=None, template="availability_ask",
                trigger="m_dana_1"):
    return responder.send_stage(
        "t_dana", stage, RELAY, body,
        [responder.AWAITING_LABEL] if add is None else add,
        [] if remove is None else remove,
        {"template": template}, trigger)


def test_send_stage_delivers_the_exact_body_and_records_what_went_out(monkeypatch):
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_dana_1")])
    assert _send_stage(e) == "sent"

    # The renter gets the template verbatim, once, at the relay address.
    assert e.emails == [("t_dana", RELAY, FIRST_BODY)]
    assert "Here's when I have open this week" in e.only_body()
    assert e.labels == [("t_dana", [responder.AWAITING_LABEL], [])]

    # The lock doc has to describe the send well enough to recover from it:
    # the hash is what proves a later attempt is the same email.
    _, _, meta = e.reserved[0]
    assert meta["body_sha256"] == ledger.content_hash(FIRST_BODY)
    assert meta["to_relay"] == RELAY
    assert meta["template"] == "availability_ask"
    assert meta["trigger_message_id"] == "m_dana_1"
    assert e.send_docs[("t_dana", "first_reply")]["status"] == "sent"
    assert e.threads["t_dana"]["last_reply_template"] == "availability_ask"


def test_send_stage_refuses_a_second_email_for_a_stage_already_handled(monkeypatch):
    # The whole point of the Firestore create-once lock: losing it means
    # somebody else already replied, so this run must go quiet.
    for verdict in ("already-sent", "in-flight"):
        e = Edges(monkeypatch, reserve=verdict)
        assert _send_stage(e) == f"skipped:{verdict}"
        assert e.emails == []
        assert e.labels == []


def test_send_stage_recovery_backfills_instead_of_re_emailing(monkeypatch):
    # Stale lock doc, but Gmail shows a reply landed after the trigger: the
    # send happened, the bookkeeping just never caught up.
    e = Edges(monkeypatch, reserve="recover",
              thread_msgs=[_relay_msg("m_dana_1"), _alex_msg("a_1")])
    assert _send_stage(e) == "skipped:recovered-already-sent"
    assert e.emails == []
    doc = e.send_docs[("t_dana", "first_reply")]
    assert doc["status"] == "sent"
    assert doc["recovered"] == "backfilled-from-gmail"


def test_send_stage_recovery_sends_when_the_thread_shows_no_reply(monkeypatch):
    e = Edges(monkeypatch, reserve="recover",
              thread_msgs=[_relay_msg("m_dana_1")])
    assert _send_stage(e) == "sent"
    assert e.only_body() == FIRST_BODY

    # ...but only if we win the takeover. Losing it means another worker is
    # mid-send; a second email is worse than no email.
    lost = Edges(monkeypatch, reserve="recover", takeover=False,
                 thread_msgs=[_relay_msg("m_dana_1")])
    assert _send_stage(lost) == "skipped:takeover-lost"
    assert lost.emails == []


def test_send_stage_review_block_stops_the_email_and_hands_it_to_a_human(monkeypatch):
    reason = "repeats the 3:15 offer the renter already declined"
    e = Edges(monkeypatch, review={"verdict": "block", "reason": reason},
              thread_msgs=[_relay_msg("m_dana_1", "I cannot do 3:15pm")])
    assert _send_stage(e, template="offer_existing") == "no-send:review-blocked"
    assert e.emails == []

    doc = e.send_docs[("t_dana", "first_reply")]
    assert doc["status"] == "failed"
    assert reason in doc["error"]

    # A blocked reply must not just vanish into the logs.
    assert e.threads["t_dana"]["state"] == ledger.NEEDS_HUMAN
    # The ping names the template and carries as much of the reason as the
    # 80-char last-line budget allows, so Alex can act without digging.
    assert e.pokes and "outgoing offer_existing blocked by pre-send review" in e.pokes[0]
    assert reason[:25] in e.pokes[0]
    assert e.pokes[0].startswith("URGENT same-day: ")
    assert "[zt:t_dana]" in e.pokes[0]


def test_send_stage_approved_answers_skip_the_review_gate(monkeypatch):
    # Alex's own words are never second-guessed, even by a blocking verdict.
    body = T.approved_answer("Dana", "Yes, the water heater was replaced in May.")
    e = Edges(monkeypatch, review={"verdict": "block", "reason": "off topic"},
              thread_msgs=[_relay_msg("m_dana_1")])
    assert _send_stage(e, stage="approved__m1", body=body,
                       template="approved_answer") == "sent"
    assert e.only_body() == body


def test_send_stage_failed_delivery_is_never_recorded_as_sent(monkeypatch):
    # A send doc left at "sent" after a Composio failure is a reply the
    # renter never gets and nothing ever retries.
    e = Edges(monkeypatch, send_error=RuntimeError("Composio 503 upstream"),
              thread_msgs=[_relay_msg("m_dana_1")])
    assert _send_stage(e) == "skipped:send-failed"
    assert e.emails == []
    assert e.labels == []
    doc = e.send_docs[("t_dana", "first_reply")]
    assert doc["status"] == "failed"
    assert "503" in doc["error"]


def test_send_stage_label_failure_queues_the_labels_and_never_resends(monkeypatch):
    # The email is out. A label error must not turn into a second copy.
    e = Edges(monkeypatch, label_error=RuntimeError("label API 500"),
              thread_msgs=[_relay_msg("m_dana_1")])
    assert _send_stage(e, add=[responder.AWAITING_LABEL],
                       remove=[responder.NEEDS_REPLY_LABEL]) == "sent"
    assert e.only_body() == FIRST_BODY
    assert e.labels == []
    doc = e.send_docs[("t_dana", "first_reply")]
    assert doc["status"] == "sent"
    assert doc["labels_pending"] is True
    assert doc["labels_add"] == [responder.AWAITING_LABEL]
    assert doc["labels_remove"] == [responder.NEEDS_REPLY_LABEL]


def test_send_stage_dry_run_mirrors_the_email_without_sending_it(monkeypatch):
    e = Edges(monkeypatch, live=False)
    assert _send_stage(e) == "shadowed"
    assert e.emails == [] and e.labels == [] and e.reserved == []
    shadow = e.shadow[("t_dana", "first_reply")]
    assert shadow["would_body"] == FIRST_BODY
    assert shadow["would_labels_add"] == [responder.AWAITING_LABEL]
    assert shadow["body_sha256"] == ledger.content_hash(FIRST_BODY)


# ======================================================================
# 2. handle_new_inquiry - the front door and its blocked-address gate
# ======================================================================

# A plain statement of interest: no "?", so no ack line and nothing owed.
PLAIN_INTEREST = "Hi, I'm interested in this home and would like to see it."


def _new_inquiry(edges, address=OPEN_ADDR, text=PLAIN_INTEREST,
                 name="Dana", thread_id="t_new"):
    return responder.handle_new_inquiry(thread_id, name, address, RELAY,
                                        text, "m_new_1")


def test_new_inquiry_at_a_leased_home_is_told_it_is_rented_and_nothing_else(monkeypatch):
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_new_1")])
    monkeypatch.setattr(cal, "list_events", lambda n: [])

    assert _new_inquiry(e, address=LEASED_ADDR) == "sent"
    body = e.only_body()
    assert body == T.leased_reply("Dana", LEASED_ADDR)
    assert "has been rented and is no longer available" in body
    # The 8/4 failure in reverse: a renter at a gone home must never be
    # invited to tour it.
    assert "I'd love to get you in to see it" not in body
    assert T.WINDOWS_BLOCK not in body

    assert e.threads["t_new"]["state"] == ledger.LEASED
    assert e.labels == [("t_new", [responder.HANDLED_LABEL], [])]


def test_new_inquiry_matches_the_blocked_list_through_city_and_zip(monkeypatch):
    # The list stores "35031 N Palomino Way"; Zillow sends the full address.
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_new_1")])
    monkeypatch.setattr(cal, "list_events", lambda n: [])
    assert _new_inquiry(e, address="35031 N Palomino Way, Cave Creek, AZ 85331") == "sent"
    assert "has been rented" in e.only_body()

    # ...and a different house number on the same street is NOT blocked.
    open_house = Edges(monkeypatch, thread_msgs=[_relay_msg("m_new_1")])
    monkeypatch.setattr(cal, "list_events", lambda n: [])
    assert _new_inquiry(open_house, address="35035 N Palomino Way, Cave Creek, AZ 85331",
                        thread_id="t_open") == "sent"
    assert "has been rented" not in open_house.only_body()
    assert open_house.threads["t_open"]["state"] == ledger.ACKED


def test_new_inquiry_on_an_open_home_asks_for_a_time(monkeypatch):
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_new_1")])
    monkeypatch.setattr(cal, "list_events", lambda n: [])

    assert _new_inquiry(e) == "sent"
    assert e.only_body() == T.availability_ask("Dana", OPEN_ADDR)

    doc = e.threads["t_new"]
    assert doc["state"] == ledger.ACKED
    assert doc["has_open_question"] is False
    assert doc["offered_start_iso"] is None and doc["offered_event_id"] == ""
    assert e.labels == [("t_new", [responder.AWAITING_LABEL], [])]
    assert e.pokes == []


def test_new_inquiry_offers_the_showing_already_on_the_calendar(monkeypatch):
    ev = _showing_event("evC", "15:15")
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_new_1")])
    monkeypatch.setattr(cal, "list_events", lambda n: [ev])

    assert _new_inquiry(e) == "sent"
    hit = cal.find_existing_showing(OPEN_ADDR)
    assert e.only_body() == T.offer_existing("Dana", OPEN_ADDR, hit["when_human"])
    assert hit["when_human"] in e.only_body()

    doc = e.threads["t_new"]
    assert doc["state"] == ledger.OFFERED
    # The id and time are what book_accepted_offer later books against.
    assert doc["offered_event_id"] == "evC"
    assert doc["offered_start_iso"] == hit["start_az"].isoformat()


def test_new_inquiry_answers_a_standing_rule_inline_in_one_email(monkeypatch):
    # The 7/30-8/1 shadow soak caught first_reply + a separate standing-rule
    # email 68ms apart. One inquiry, one email, answer folded in.
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_new_1")])
    monkeypatch.setattr(cal, "list_events", lambda n: [])

    assert _new_inquiry(e, text="Does it come with a washer and dryer?") == "sent"
    body = e.only_body()
    assert len(e.emails) == 1
    assert facts.APPLIANCES_LINE in body
    assert T.QUESTION_ACK not in body  # a real answer replaces the ack
    assert e.reserved[0][2]["template"] == "availability_ask+appliances"
    assert e.threads["t_new"]["has_open_question"] is False
    assert e.pokes == []  # nothing left for Alex to answer


def test_new_inquiry_with_an_unanswerable_question_acks_and_escalates(monkeypatch):
    question = "Does the home have a water softener?"
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_new_1", question)])
    monkeypatch.setattr(cal, "list_events", lambda n: [])

    assert _new_inquiry(e, text=question) == "sent"
    body = e.only_body()
    assert T.QUESTION_ACK in body
    assert "let's get you set up to see the place" in body

    doc = e.threads["t_new"]
    assert doc["has_open_question"] is True
    # We promised a follow-up, so the thread must end up on a human.
    assert doc["state"] == ledger.NEEDS_HUMAN
    assert e.labels == [("t_new", [responder.AWAITING_LABEL,
                                   responder.NEEDS_REPLY_LABEL], [])]
    assert e.pokes and question[:40] in e.pokes[0]
    assert "1641" not in e.pokes[0] and "El Marino" in e.pokes[0]


def test_new_inquiry_does_not_mistake_a_link_for_a_question(monkeypatch):
    # Query strings carry "?" - an ack line here promises a follow-up that
    # nobody owes, and drags the thread onto the needs-you pile.
    text = ("Saw your listing at https://www.zillow.com/homedetails/2118-S-El-Marino"
            "?utm_source=email&fullpage=true and I'm interested.")
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_new_1", text)])
    monkeypatch.setattr(cal, "list_events", lambda n: [])

    assert _new_inquiry(e, text=text) == "sent"
    assert e.only_body() == T.availability_ask("Dana", OPEN_ADDR)
    assert T.QUESTION_ACK not in e.only_body()
    assert e.threads["t_new"]["has_open_question"] is False
    assert e.pokes == []


def test_new_inquiry_still_replies_when_the_calendar_is_down(monkeypatch):
    # Consolidate-first is a nicety; the sub-30-second reply is the product.
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_new_1")])
    monkeypatch.setattr(cal, "list_events",
                        lambda n: (_ for _ in ()).throw(RuntimeError("Composio 502")))
    assert _new_inquiry(e) == "sent"
    assert e.only_body() == T.availability_ask("Dana", OPEN_ADDR)
    assert e.threads["t_new"]["state"] == ledger.ACKED

    # Same promise if the lookup blows up above find_existing_showings.
    e2 = Edges(monkeypatch, thread_msgs=[_relay_msg("m_new_1")])
    monkeypatch.setattr(cal, "find_existing_showing",
                        lambda a: (_ for _ in ()).throw(RuntimeError("bad payload")))
    assert _new_inquiry(e2, thread_id="t_new2") == "sent"
    assert e2.only_body() == T.availability_ask("Dana", OPEN_ADDR)


# ======================================================================
# 3. book_accepted_offer - folding a renter onto a real calendar event
# ======================================================================

def _book(edges, doc=None, thread_id="t_yes"):
    return responder.book_accepted_offer(
        thread_id, doc or {}, "Dana", OPEN_ADDR, RELAY, "m_yes_1")


def _two_showings(monkeypatch, *, early="13:00", offered="16:30"):
    """Two upcoming showings at the same house, soonest first."""
    events = [_showing_event("evEARLY", early, inquirer="Marcus",
                             agent="Jace Johnson", agent_email="jace@azfoleyhomes.com"),
              _showing_event("evOFFERED", offered)]
    monkeypatch.setattr(cal, "list_events", lambda n: events)
    hits = cal.find_existing_showings(OPEN_ADDR, events=events)
    assert [h["event"]["id"] for h in hits] == ["evEARLY", "evOFFERED"]
    return hits


def test_accept_books_the_event_that_was_offered_not_the_soonest(monkeypatch):
    # 8/25 audit: a showing created between the offer and the yes hijacked
    # the fold, and the renter was confirmed for a time nobody offered them.
    hits = _two_showings(monkeypatch)
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_yes_1", "Yes, that works!")])

    assert _book(e, {"offered_event_id": "evOFFERED"}) == "sent"
    assert e.folds == [("evOFFERED", "Dana")]

    offered = hits[1]
    body = e.only_body()
    assert body == T.booking_confirmation("Dana", OPEN_ADDR,
                                          offered["when_human"], "Rhett Lueck")
    assert offered["when_human"] in body
    assert hits[0]["when_human"] not in body
    assert "Rhett Lueck will meet you there" in body

    doc = e.threads["t_yes"]
    assert doc["state"] == ledger.BOOKED
    assert doc["event_id"] == "evOFFERED"
    assert doc["booked_start_iso"] == offered["start_az"].isoformat()
    assert doc["agent"] == "Rhett Lueck"
    assert e.labels == [("t_yes", [responder.HANDLED_LABEL],
                         [responder.AWAITING_LABEL])]


def test_accept_falls_back_to_the_soonest_when_no_offer_was_recorded(monkeypatch):
    hits = _two_showings(monkeypatch)
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_yes_1", "Yes, that works!")])

    assert _book(e, {}) == "sent"
    assert e.folds == [("evEARLY", "Dana")]
    assert hits[0]["when_human"] in e.only_body()
    # Whoever runs that event is who the renter is told to expect.
    assert "Jace Johnson will meet you there" in e.only_body()
    assert e.threads["t_yes"]["agent"] == "Jace Johnson"


def test_accept_with_no_matching_event_escalates_instead_of_guessing(monkeypatch):
    # Natalie + Yesenia, 8/5: both said yes to a time Alex arranged by hand
    # and got "what day works?" back.
    monkeypatch.setattr(cal, "list_events", lambda n: [])
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_yes_1", "Yes, 4:30 works")])

    assert _book(e, {"offered_event_id": "evGONE"}) == "no-send:accept-without-event"
    assert e.emails == [] and e.folds == []
    assert e.threads["t_yes"]["state"] == ledger.NEEDS_HUMAN
    assert e.pokes and "accepted a time but no matching calendar event" in e.pokes[0]
    assert e.pokes[0].startswith("URGENT same-day: ")


def test_accept_defers_when_a_newer_renter_message_is_waiting(monkeypatch):
    # Booking off a stale trigger is how Alec landed in the slot he had
    # just refused.
    _two_showings(monkeypatch)
    e = Edges(monkeypatch, thread_msgs=[
        _relay_msg("m_yes_1", "Yes, that works!"),
        _relay_msg("m_yes_2", "Actually, can we make it Thursday?")])

    assert _book(e, {"offered_event_id": "evOFFERED"}) == \
        "no-send:superseded-by-newer-message"
    assert e.emails == [] and e.folds == []
    assert e.threads["t_yes"]["state"] == ledger.NEEDS_HUMAN


def test_accept_never_confirms_a_showing_the_calendar_refused(monkeypatch):
    # The event is written FIRST on purpose: a confirmation for a fold that
    # did not happen sends someone to a house nobody is opening.
    _two_showings(monkeypatch)
    e = Edges(monkeypatch, thread_msgs=[_relay_msg("m_yes_1", "Yes, that works!")])
    monkeypatch.setattr(cal, "fold_renter_into_event",
                        lambda ev, n: (_ for _ in ()).throw(
                            RuntimeError("GOOGLECALENDAR_UPDATE_EVENT failed")))

    assert _book(e, {"offered_event_id": "evOFFERED"}) == "skipped:book-failed"
    assert e.emails == []
    assert e.threads.get("t_yes", {}).get("state") != ledger.BOOKED
    doc = e.send_docs[("t_yes", "booked__evOFFERED")]
    assert doc["status"] == "failed"
    assert "GOOGLECALENDAR_UPDATE_EVENT" in doc["error"]


def test_accept_with_a_lost_lock_does_not_fold_or_confirm_twice(monkeypatch):
    _two_showings(monkeypatch)
    e = Edges(monkeypatch, reserve="already-sent",
              thread_msgs=[_relay_msg("m_yes_1", "Yes, that works!")])

    assert _book(e, {"offered_event_id": "evOFFERED"}) == "skipped:already-sent"
    assert e.folds == [] and e.emails == []
    assert e.threads.get("t_yes", {}).get("state") != ledger.BOOKED


def test_accept_label_failure_still_leaves_the_thread_booked(monkeypatch):
    hits = _two_showings(monkeypatch)
    e = Edges(monkeypatch, label_error=RuntimeError("label API 500"),
              thread_msgs=[_relay_msg("m_yes_1", "Yes, that works!")])

    assert _book(e, {"offered_event_id": "evOFFERED"}) == "sent"
    assert hits[1]["when_human"] in e.only_body()
    assert e.threads["t_yes"]["state"] == ledger.BOOKED
    doc = e.send_docs[("t_yes", "booked__evOFFERED")]
    assert doc["status"] == "sent"
    assert doc["labels_add"] == [responder.HANDLED_LABEL]
    assert doc["labels_remove"] == [responder.AWAITING_LABEL]
