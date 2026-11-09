"""extract_event: the gate on whether an inbound inquiry is answered at all.

The webhook handler does `if not thread_id: return "ignored-no-thread"`, so a
parse miss here is total silence - no reply, no label, no escalation, nothing
in the ledger to age into NEEDS_HUMAN. That is the one failure mode this
pipeline cannot see. The README already names it as the fragile part ("if the
first real trigger logs a parse miss, send Claude the logged payload"), and it
had no test.

The three outputs each gate something different downstream:
  thread_id  - whether the request is processed at all
  subject    - parse_subject's renter name + property address, which pick the
               fact sheet and the blocked-address check
  message_id - claim_message's duplicate-delivery gate and message_by_id's
               "classify the TRIGGERING message, not whoever spoke last"

Composio nests the message differently across trigger versions; the function
is deliberately defensive about all of them, so each shape is pinned here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DRY_RUN", "true")

import gmail_client as gm  # noqa: E402
import responder  # noqa: E402

THREAD = "1a16eec59561deee"
SUBJECT = "Madison is requesting to tour 1110 E Redfield Rd, Tempe, AZ, 85283"
SENDER = "Madison <reply+1a16eec59561deee@convo.zillow.com>"
MSG_ID = "1a16eec59561deee-m1"


# ------------------------------------------------- the shapes Composio sends

def test_v3_trigger_payload_nested_under_data():
    assert gm.extract_event({"data": {
        "threadId": THREAD, "subject": SUBJECT,
        "sender": SENDER, "messageId": MSG_ID,
    }}) == (THREAD, SUBJECT, SENDER, MSG_ID)


def test_snake_case_and_from_are_accepted():
    """Composio has shipped both casings, and `from` instead of `sender`."""
    assert gm.extract_event({"data": {
        "thread_id": THREAD, "subject": SUBJECT,
        "from": SENDER, "message_id": MSG_ID,
    }}) == (THREAD, SUBJECT, SENDER, MSG_ID)


def test_payload_without_a_data_envelope():
    assert gm.extract_event({
        "threadId": THREAD, "subject": SUBJECT,
        "sender": SENDER, "messageId": MSG_ID,
    }) == (THREAD, SUBJECT, SENDER, MSG_ID)


def test_message_nested_one_level_deeper():
    """The "requesting to tour" trigger family wraps the message in its own
    dict; the id there is a bare `id`."""
    assert gm.extract_event({"data": {"message": {
        "threadId": THREAD, "subject": SUBJECT,
        "from": SENDER, "id": MSG_ID,
    }}}) == (THREAD, SUBJECT, SENDER, MSG_ID)


def test_raw_gmail_headers_fill_in_a_missing_subject_and_sender():
    """A payload carrying the raw Gmail message exposes subject/from only in
    payload.headers. Losing the subject here costs the property address."""
    assert gm.extract_event({"data": {
        "threadId": THREAD, "messageId": MSG_ID,
        "payload": {"headers": [
            {"name": "Delivered-To", "value": "alex@azfoleyhomes.com"},
            {"name": "Subject", "value": SUBJECT},
            {"name": "From", "value": SENDER},
        ]},
    }}) == (THREAD, SUBJECT, SENDER, MSG_ID)


def test_headers_never_overwrite_what_the_trigger_already_gave():
    """Gmail keeps the ORIGINAL Subject/From headers on a reply in a thread,
    so letting them win would relabel a renter's reply with the first
    inquiry's subject and sender."""
    assert gm.extract_event({"data": {
        "threadId": THREAD, "subject": "Re: Madison replied", "sender": SENDER,
        "messageId": "1a16eec59561deee-m4",
        "payload": {"headers": [
            {"name": "Subject", "value": SUBJECT},
            {"name": "From", "value": "noreply@zillow.com"},
        ]},
    }}) == (THREAD, "Re: Madison replied", SENDER, "1a16eec59561deee-m4")


# ------------------------------------------------- what the outputs feed

def test_the_extracted_subject_yields_the_renter_and_the_property():
    """The whole point of carrying `subject` through: parse_subject turns it
    into the name and address that pick the fact sheet and run the
    blocked-address check."""
    _, subject, _, _ = gm.extract_event({"data": {
        "threadId": THREAD, "subject": SUBJECT,
        "sender": SENDER, "messageId": MSG_ID,
    }})
    assert responder.parse_subject(subject) == (
        "Madison", "1110 E Redfield Rd, Tempe, AZ, 85283")


def test_the_extracted_message_id_selects_the_triggering_message():
    """Melissa 2026-08-23: two renter messages 13 seconds apart. The id this
    function returns is how message_by_id picks the one the webhook fired
    for, instead of whoever spoke last."""
    msgs = [
        {"messageId": MSG_ID, "sender": SENDER,
         "messageText": "Madison says: I can do 2pm today!"},
        {"messageId": "1a16eec59561deee-m2", "sender": SENDER,
         "messageText": "Madison says: or anytime today"},
    ]
    _, _, _, message_id = gm.extract_event({"data": {
        "threadId": THREAD, "subject": SUBJECT,
        "sender": SENDER, "messageId": MSG_ID,
    }})
    assert gm.message_by_id(msgs, message_id) is msgs[0]


def test_a_bare_id_is_still_taken_as_the_message_id():
    """claim_message's duplicate gate is keyed on this; returning nothing
    would let Zillow's double delivery through as two replies."""
    assert gm.extract_event(
        {"data": {"threadId": THREAD, "id": MSG_ID}})[3] == MSG_ID


# ------------------------------------------------- degrading safely

def test_a_payload_with_no_thread_id_is_reported_as_such():
    """The handler answers 200 ignored-no-thread on a falsy thread_id, so
    this must come back None rather than raise."""
    assert gm.extract_event({"data": {"subject": SUBJECT, "sender": SENDER}}) == (
        None, SUBJECT, SENDER, None)


def test_a_non_dict_message_does_not_crash_the_parse():
    assert gm.extract_event({"data": {"message": "unexpected",
                                      "subject": SUBJECT}})[0] is None


def test_a_non_dict_payload_key_does_not_crash_the_header_scan():
    assert gm.extract_event({"data": {"threadId": THREAD, "messageId": MSG_ID,
                                      "payload": "unexpected"}}) == (
        THREAD, None, None, MSG_ID)


def test_an_empty_payload_is_inert():
    assert gm.extract_event({}) == (None, None, None, None)
