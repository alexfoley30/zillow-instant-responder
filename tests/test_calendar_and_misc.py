"""All-day event coverage (the OverflowError incident), malformed-message
extraction, ledger hash purity, and template tone guards."""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calendar_logic as cal  # noqa: E402
import gmail_client as gm  # noqa: E402
import templates as T  # noqa: E402
from ledger import content_hash  # noqa: E402


# ------------------------------------------------- all-day events (8/5 bug)
# Three live renters crashed on datetime.min.astimezone() when an all-day
# event hit the day filter. _ev_all_day_covers is the fix.

def test_all_day_covers_its_day():
    ev = {"start": {"date": "2026-08-05"}, "end": {"date": "2026-08-06"}}
    assert cal._ev_all_day_covers(ev, date(2026, 8, 5))


def test_all_day_end_date_exclusive():
    ev = {"start": {"date": "2026-08-05"}, "end": {"date": "2026-08-06"}}
    assert not cal._ev_all_day_covers(ev, date(2026, 8, 6))


def test_multi_day_vacation_block():
    ev = {"start": {"date": "2026-08-10"}, "end": {"date": "2026-08-14"}}
    assert cal._ev_all_day_covers(ev, date(2026, 8, 12))
    assert not cal._ev_all_day_covers(ev, date(2026, 8, 14))


def test_timed_event_is_not_all_day():
    ev = {"start": {"dateTime": "2026-08-05T18:15:00-07:00"}}
    assert not cal._ev_all_day_covers(ev, date(2026, 8, 5))


def test_garbage_dates_do_not_crash():
    assert not cal._ev_all_day_covers({"start": {"date": "not-a-date"}}, date(2026, 8, 5))
    assert not cal._ev_all_day_covers({}, date(2026, 8, 5))


# ------------------------------------------- malformed messages (fixture 8)

def test_empty_body_yields_empty():
    assert gm.extract_renter_text({"messageText": ""}) == ""


def test_missing_body_key_yields_empty():
    assert gm.extract_renter_text({}) == ""


def test_chrome_only_body_yields_empty():
    chrome = ("Brand logo New message from a renter Regarding your listing "
              "at: 123 Main St Reply on Zillow Some rental inquiries may be "
              "scams.")
    assert gm.extract_renter_text({"messageText": chrome}).strip() == ""


def test_whitespace_soup_yields_empty():
    assert gm.extract_renter_text({"messageText": "\r\n\r\n   \r\n  \r\n"}).strip() == ""


# ---------------------------------------------------------- ledger hashing

def test_content_hash_stable_and_short():
    a = content_hash("yes no prob!")
    assert a == content_hash("yes no prob!")
    assert len(a) == 16
    assert a != content_hash("yes no prob")


# ------------------------------------------------------- template tone/protocol

def test_reschedule_asks_why():
    body = T.reschedule_after_cancel("Natalie")
    assert "timing thing" in body            # protocol step 2: ask why
    assert "off the" in body                 # confirms calendar cleared
    assert "Great question" not in body


def test_leased_reply_never_invites_tour():
    body = T.leased_reply("Alondra", "1641 E Coronado Rd")
    assert "rented" in body
    assert "tour" not in body.lower()


# ----------------------------------------- vague-time consolidate (8/22 Jamie)

def _showing_event(summary, location, start_iso):
    return {"summary": summary, "location": location,
            "start": {"dateTime": start_iso}}


def test_find_existing_showings_returns_all_sorted():
    from datetime import datetime, timedelta, timezone
    base = datetime.now(timezone.utc) + timedelta(hours=6)
    later = base + timedelta(hours=2)
    events = [
        _showing_event("Rhett Showing: 1110 E Redfield Rd",
                       "1110 E Redfield Rd, Tempe, AZ, 85283",
                       later.isoformat()),
        _showing_event("Rhett Showing: 1110 E Redfield Rd",
                       "1110 E Redfield Rd, Tempe, AZ, 85283",
                       base.isoformat()),
        _showing_event("Jace Showing: 2118 S El Marino",
                       "2118 S El Marino, Mesa, AZ", base.isoformat()),
    ]
    hits = cal.find_existing_showings("1110 E Redfield Rd, Tempe, AZ, 85283",
                                      events=events)
    assert len(hits) == 2
    assert hits[0]["start_az"] < hits[1]["start_az"]


def test_find_existing_showings_skips_short_lead():
    from datetime import datetime, timedelta, timezone
    soon = datetime.now(timezone.utc) + timedelta(minutes=30)
    events = [_showing_event("Showing: 1110 E Redfield Rd",
                             "1110 E Redfield Rd, Tempe, AZ, 85283",
                             soon.isoformat())]
    assert cal.find_existing_showings("1110 E Redfield Rd, Tempe, AZ, 85283",
                                      events=events) == []


def test_find_existing_showing_single_still_works():
    from datetime import datetime, timedelta, timezone
    base = datetime.now(timezone.utc) + timedelta(hours=6)
    events = [_showing_event("Showing: 1110 E Redfield Rd",
                             "1110 E Redfield Rd, Tempe, AZ, 85283",
                             base.isoformat())]
    hit = cal.find_existing_showing("1110 E Redfield Rd, Tempe, AZ, 85283",
                                    events=events)
    assert hit and hit["when_human"]


def test_offer_existing_reply_template():
    body = T.offer_existing_reply("Jamie", "today, Friday, at 11:00 AM or at 1:00 PM")
    assert "11:00 AM" in body and "1:00 PM" in body
    assert "add you right in" in body
    assert "exact time" in body


# --------------------------------- 8/22 debug batch: tour shape + fold-cancel

def test_subject_regex_tour_variants():
    import responder as R
    assert R.parse_subject(
        "Madison is requesting to tour 1110 E Redfield Rd, Tempe, AZ, 85283"
    ) == ("Madison", "1110 E Redfield Rd, Tempe, AZ, 85283")
    assert R.parse_subject(
        "Courtney is requesting a tour of 1110 E Redfield Rd, Tempe, AZ, 85283"
    )[0] == "Courtney"
    assert R.parse_subject(
        "Re: Jamie is requesting information about 1110 E Redfield Rd, Tempe, AZ, 85283"
    ) == ("Jamie", "1110 E Redfield Rd, Tempe, AZ, 85283")


def test_body_identity_extraction():
    html = ('<div>RENTER\N{RIGHT SINGLE QUOTATION MARK}S NAME</div>'
            '<div style="x">Jamie Dallas </div>'
            '<div>Regarding your listing at:</div></td></tr><tr><td align="left">'
            '<div style="y">1110 E Redfield Rd, Tempe, AZ 85283</div>')
    name, addr = gm.extract_identity_from_body({"messageText": html})
    assert name == "Jamie"
    assert addr == "1110 E Redfield Rd, Tempe, AZ 85283"


def test_body_identity_extraction_empty():
    assert gm.extract_identity_from_body({"messageText": ""}) == (None, None)
    assert gm.extract_identity_from_body({}) == (None, None)


def test_remove_renter_parsing_logic():
    import re as _re
    desc = "Zillow inquiry. Inquirer: Sezer, Dylan.\nAgent: Rhett."
    m = _re.search(r"Inquirer:\s*([^\n]+)", desc)
    names = [n.strip() for n in m.group(1).rstrip(". ").split(",") if n.strip()]
    assert names == ["Sezer", "Dylan"]
    remaining = [n for n in names if n.lower() != "sezer"]
    assert remaining == ["Dylan"]


# ------------------------------- reason-aware reschedule (8/22 Jamie battery)

def test_reschedule_reason_given_skips_why():
    body = T.reschedule_after_cancel("Jamie", reason_given=True)
    assert "did something change" not in body
    assert "timing thing" not in body
    assert "No worries at all" in body


def test_reschedule_no_reason_still_asks_why():
    body = T.reschedule_after_cancel("Jamie", reason_given=False)
    assert "did something change on your end" in body


def test_reschedule_offers_next_slot():
    body = T.reschedule_after_cancel("Jamie", reason_given=True,
                                     next_when="today, Friday, at 1:00 PM")
    assert "showing the home again today, Friday, at 1:00 PM" in body
    assert "put you back in" in body


def test_llm_schema_has_cancel_reason():
    import llm
    assert "cancel_reason" in llm.SCHEMA["properties"]
    assert "cancel_reason" in llm.SCHEMA["required"]
