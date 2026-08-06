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
