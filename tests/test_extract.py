#!/usr/bin/env python3
"""Regression tests for msg_body / extract_renter_text.

THE BUG THIS LOCKS DOWN (2026-08-03): Composio returns the email body as raw
HTML in `messageText`. msg_body handed that straight to extract_renter_text,
whose chrome filter breaks on any line containing "zillow.com" - and the tag
soup is one long line full of zillow.com hrefs. Result: "" for EVERY renter
message (60/60 in the shadow ledger), so Haiku classified 37 of 46 replies as
`other` and escalated instead of replying.

The HTML below is excerpted from real Zillow relay mail on this account
(Anna Woodhouse / 300 N Gila Springs, Zachary Austin / 3896 E Alfalfa),
keeping the exact chrome that triggered the bug.

Run:  /opt/homebrew/bin/python3 tests/test_extract.py
      (gmail_client is stdlib-only, so any interpreter works)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gmail_client as gm  # noqa: E402

# --- fixture 1: first-inquiry format. Renter words sit after "<Name> says:".
FIRST_INQUIRY_HTML = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN">
<html xmlns="http://www.w3.org/1999/xhtml"><head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8"></meta>
<style>a {text-decoration: none}</style></head>
<body bgcolor="#FFFFFF"><img src="https://hotpads.com/t/1ec7c94f" height="0" width="0"></img>
<table cellpadding="0"><tr><td><table><tbody><tr><td style="text-align:center">
<a style="color:#006aff" href="https://zillow.com/r/58y56j72rjw54"><img
src="https://nodes3cdn.hotpads.com/zillow-rental-manager-logo.png" alt="Brand logo"></img></a>
</td></tr><tr><td><div style="font-size:25px">New message</div></td></tr></tbody></table>
<table><tbody><tr><td><div><a style="color:#006aff"
href="https://zillow.com/rental-manager/properties/4ekrkfrk3z20p/find-tenants?ref=inquiry">300
N Gila Springs Blvd #109, Chandler, AZ, 85226</a>.</div></td></tr>
<tr><td><div><span style="font-weight:bold">Anna Woodhouse</span> says:</div></td></tr>
<tr><td><div style="color:#2a2a33">I would like to schedule a tour.</div></td></tr>
<tr><td><a href="https://zillow.com/rental-manager/inbox/4ekrkfrk3z20p/301276737">Reply to
Anna</a></td></tr><tr><td><a href="https://zillow.com/rental-manager/applications/">Send
application</a></td></tr><tr><td><div>You can also reply directly to this email</div></td></tr>
<tr><td><div style="font-weight:700">About Anna Woodhouse</div></td></tr>
<tr><td><div>Credit score</div><div>720 to 850</div></td></tr>
<tr><td><span style="font-weight:bold">Reminder:</span> The federal <a
href="https://zillow.com/r/1k3qsngahv3h3">Fair Housing Act</a> prohibits housing
discrimination.</td></tr>
<tr><td><div>Have questions or need help? Find answers on our <a
href="https://zillow.com/r/3r280kuhv58mt">FAQ page</a>.</div></td></tr>
</tbody></table></td></tr></table></body></html>"""

# --- fixture 2: reply format. Renter words come FIRST, chrome after, then the
# quoted history of our own last email.
REPLY_HTML = """<div dir="ltr">Hi Alex thanks for reaching out we are ready to move in asap
or latest September 1st we are currently renting a BnB until we find a long term home.
Can we look at it today at 11:15?</div>
<table cellpadding="0"><tbody><tr><td><a href="https://zillow.com/r/58y56j72rjw54"><img
alt="Brand logo"></img></a></td></tr>
<tr><td><div>New message from a renter</div></td></tr>
<tr><td><div>Regarding your listing at: 3896 E Alfalfa Dr, Gilbert, AZ 85298</div></td></tr>
<tr><td><a href="https://zillow.com/rental-manager/inbox/">Reply on Zillow</a></td></tr>
<tr><td><div>Some rental inquiries may be scams. If a message asks you to scan a QR code,
be careful.</div></td></tr></tbody></table>
<div class="gmail_quote"><div class="gmail_attr">On Sat, Aug 1, 2026 at 7:08 AM Alex Foley
&lt;alex@azfoleyhomes.com&gt; wrote:<br></div><blockquote class="gmail_quote">
Hi Zachary,<br><br>Thanks for reaching out about 3896 E Alfalfa Dr! I'd love to get you in
to see it.<br></blockquote></div>"""

# --- fixture 3: entity-encoded apostrophes, the common real-world case.
ENTITY_HTML = ("<div dir=\"ltr\">I can&#39;t do 3. I could do 10am if that works for "
               "you?</div><table><tr><td><div>New message from a renter</div></td></tr>"
               "<tr><td><div>Regarding your listing at: 22500 N Greenland Park Dr</div>"
               "</td></tr></table>")

FAILURES = []


def check(name, got, want_contains=(), want_absent=()):
    ok = True
    for w in want_contains:
        if w.lower() not in (got or "").lower():
            FAILURES.append("%s: expected to contain %r\n     got: %r" % (name, w, got))
            ok = False
    for w in want_absent:
        if w.lower() in (got or "").lower():
            FAILURES.append("%s: should NOT contain %r\n     got: %r" % (name, w, got))
            ok = False
    print("  %-46s %s" % (name, "PASS" if ok else "FAIL"))


def main():
    print("\nmsg_body flattens HTML")
    check("first inquiry -> not empty",
          gm.msg_body({"messageText": FIRST_INQUIRY_HTML}),
          want_contains=["Anna Woodhouse says:", "schedule a tour"],
          want_absent=["<div", "<td", "href="])
    check("plain text passes through untouched",
          gm.msg_body({"messageText": "just words, no markup"}),
          want_contains=["just words, no markup"])
    check("falls back to preview.body when no messageText",
          gm.msg_body({"preview": {"body": "Can we do Monday at 3 pm?"}}),
          want_contains=["Monday at 3 pm"])
    check("empty message -> empty string",
          gm.msg_body({}), want_absent=["none"])

    print("\nextract_renter_text recovers the renter's words")
    check("REGRESSION: first inquiry is not empty",
          gm.extract_renter_text({"messageText": FIRST_INQUIRY_HTML}),
          want_contains=["schedule a tour"],
          want_absent=["Send application", "Credit score", "Fair Housing",
                       "zillow.com", "Brand logo"])
    # NB: "thanks for reaching out" is in the RENTER's own opening line here,
    # so it is not a valid marker for our quoted copy. Assert on a phrase only
    # we would write instead.
    check("REGRESSION: reply keeps renter words, drops chrome",
          gm.extract_renter_text({"messageText": REPLY_HTML}),
          want_contains=["11:15", "September 1st", "move in asap"],
          want_absent=["Brand logo", "New message from a renter", "may be scams",
                       "Regarding your listing", "wrote:", "I'd love to get you in"])
    check("REGRESSION: entities decode to real apostrophes",
          gm.extract_renter_text({"messageText": ENTITY_HTML}),
          want_contains=["can't do 3", "10am"],
          want_absent=["&#39;", "New message from a renter"])

    print("\nrenter words are never truncated by an over-eager chrome filter")
    # Each of these contains a phrase that also appears in Zillow chrome. If
    # the chrome filter matches, the rest of the renter's message is silently
    # lost - the same class of bug as the HTML one, just quieter.
    check("'About Wednesday' is renter text, not the profile block",
          gm.extract_renter_text({"messageText":
              "<div>About Wednesday - can we do 5:30 instead of 5?</div>"}),
          want_contains=["5:30 instead of 5"])
    check("a renter quoting their credit score keeps the rest",
          gm.extract_renter_text({"messageText":
              "<div>My credit score is 720 and I can move Sept 1. "
              "Can I see it Friday at 2?</div>"}),
          want_contains=["Friday at 2", "move Sept 1"])
    check("'reply to' inside renter text is not chrome",
          gm.extract_renter_text({"messageText":
              "<div>Feel free to reply to me here. Is Tuesday at noon open?</div>"}),
          want_contains=["Tuesday at noon"])
    check("renter asking about the application link survives",
          gm.extract_renter_text({"messageText":
              "<div>I don&#39;t see this property available to apply to when I "
              "click your link for arizonaeliteproperties.com/vacancies</div>"}),
          want_contains=["available to apply", "vacancies"])

    print("\nquoted history and our own copy never leak back in")
    check("our previous email is not treated as renter words",
          gm.extract_renter_text({"messageText": REPLY_HTML}),
          want_absent=["I'd love to get you in", "alex@azfoleyhomes.com"])

    print("\nthe exact failure mode that shipped")
    old_style = FIRST_INQUIRY_HTML  # what msg_body used to return verbatim
    print("     old msg_body returned raw HTML of %d chars; the chrome filter"
          % len(old_style))
    print("     broke on line 1 (zillow.com href) and yielded ''.")
    now = gm.extract_renter_text({"messageText": FIRST_INQUIRY_HTML})
    print("     now: %r" % now)

    print()
    if FAILURES:
        print("%d FAILURE(S):" % len(FAILURES))
        for f in FAILURES:
            print("  -", f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
