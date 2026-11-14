"""Natalie, 250 W Queen Creek Rd #102, 2026-11-08. "Monday would be awesome.
How does 11 sound?" got "Monday is already booked up" while Monday 11am was
open. Two bugs:
1. Reply-format emails were read from Zillow's hidden preview, which is cut off
   ("How does..."), so the 11 never reached the classifier.
2. Alex's declined, two-day family blocks counted as busy on the day they start.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DRY_RUN", "true")

import calendar_logic as cal  # noqa: E402
import gmail_client as gm  # noqa: E402
import rules  # noqa: E402

AZ = rules.AZ_TZ
ADDR = "250 W Queen Creek Rd #102, Chandler, AZ, 85248"

# Excerpt of the real relay email , chrome kept.
REPLY_HTML = """<!DOCTYPE html><html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8" /><style>a {text-decoration: none}</style></head><body>
<table><tr><td width="600"><span style="display: none !important; font-size:1px;">Hi Alex,\r\n\r\nIdeally, I’d want to move fairly fast on any place as I need to move over the next few days if possible. \r\n\r\nMonday would be awesome. How does...</span> <table><tbody><tr><td>
<a href="https://zillow.com/r/5833rjavcask7"><img src="https://zillowstatic.com/logo.png" alt="Zillow Rentals"/></a></td></tr>
<tr><td><div>New message from a renter</div></td></tr></tbody></table>
<table><tbody><tr><td><div>Regarding your listing at:</div></td></tr><tr><td><div>250 W Queen Creek Rd #102, Chandler, AZ 85248</div></td></tr>
<tr><td><a href="https://www.zillow.com/rental-manager/inbox/x">Reply on Zillow</a></td></tr>
<tr><td><div>Some rental inquiries may be scams. If a message asks you to scan a QR code, don&#x27;t respond.</div></td></tr></tbody></table>
<div><table><tbody><tr><td><div>RENTER&#x27;S NAME</div><div>Jane Renter </div></td></tr>
<tr><td><div>RENTER&#x27;S MESSAGE</div><div>Hi Alex,\r\n\r\nIdeally, I’d want to move fairly fast on any place as I need to move over the next few days if possible. \r\n\r\nMonday would be awesome. How does 11 sound? </div></td></tr></tbody></table></div>
</td></tr></tbody></table><div style="display:none;max-height:0;overflow:hidden;font-size:0;">00000000-0000-0000-0000-000000000000</div></td></tr></table></body></html>"""


def test_reply_reads_the_full_message_not_the_cut_preview():
    text = gm.extract_renter_text({"messageText": REPLY_HTML})
    assert "How does 11 sound?" in text
    assert "..." not in text
    assert "00000000-" not in text and "Jane Renter" not in text


def _ev(summary, start, end, declined=False):
    ev = {"summary": summary, "start": {"dateTime": start},
          "end": {"dateTime": end}}
    if declined:
        ev["attendees"] = [{"email": "alex@azfoleyhomes.com", "self": True,
                            "responseStatus": "declined"}]
    return ev


NOW = datetime(2026, 11, 8, 19, 40, tzinfo=AZ)


def test_declined_family_block_does_not_make_alex_busy():
    events = [_ev("Cloonan Kids with Dad", "2026-11-09T08:00:00-07:00",
                  "2026-11-11T08:00:00-07:00", declined=True)]
    for day in (9, 10):
        r = cal.validate_slot(datetime(2026, 11, day, 11, 0, tzinfo=AZ),
                              ADDR, events, now_az=NOW)
        assert r["ok"], (day, r)


def test_accepted_multi_day_event_blocks_every_day_it_covers():
    events = [_ev("Conference", "2026-11-09T08:00:00-07:00",
                  "2026-11-11T08:00:00-07:00")]
    for day in (9, 10):
        r = cal.validate_slot(datetime(2026, 11, day, 11, 0, tzinfo=AZ),
                              ADDR, events, now_az=NOW)
        assert not r["ok"], day
    # It ended at 8am Wednesday, so 11am Wednesday is open.
    assert cal.validate_slot(datetime(2026, 11, 11, 11, 0, tzinfo=AZ),
                             ADDR, events, now_az=NOW)["ok"]
