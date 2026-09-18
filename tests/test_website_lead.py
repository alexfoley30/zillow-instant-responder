"""Website lead hook (Netlify Forms -> Poke): token derivation, path auth,
message shape, rate limit and dedupe. Nothing here touches the network."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import website_lead as wl  # noqa: E402

OWNER = {"id": "sub-1", "form_name": "owner-lead", "data": {
    "name": "Test Owner", "phone": "480-555-0100", "email": "owner@example.com",
    "property-city": "Queen Creek", "house-count": "2 to 4",
    "message": "  Vacant now,\n want to start   October.  ",
    "source-page": "property-management-queen-creek", "ip": "1.2.3.4"}}
CONTACT = {"id": "sub-2", "form_name": "contact", "data": {
    "name": "Renter Person", "email": "r@example.com", "topic": "Renting a home",
    "message": "Is the Yucca house still open?"}}


def setup_function(_):
    wl.reset_for_tests()


def test_token_empty_without_key(monkeypatch):
    monkeypatch.delenv("POKE_API_KEY", raising=False)
    assert wl.lead_token() == ""
    assert not wl.path_matches(wl.PATH_PREFIX + "anything")


def test_token_stable_and_one_way(monkeypatch):
    monkeypatch.setenv("POKE_API_KEY", "unit-test-key")
    t = wl.lead_token()
    assert len(t) == 40 and t == wl.lead_token()
    assert "unit-test-key" not in t
    monkeypatch.setenv("POKE_API_KEY", "other-key")
    assert wl.lead_token() != t


def test_path_auth(monkeypatch):
    monkeypatch.setenv("POKE_API_KEY", "unit-test-key")
    t = wl.lead_token()
    assert wl.path_matches(wl.PATH_PREFIX + t)
    assert wl.path_matches(wl.PATH_PREFIX + t + "/")
    assert not wl.path_matches(wl.PATH_PREFIX + t[:-1] + "0")
    assert not wl.path_matches("/reprocess")


def test_owner_message_shape():
    msg = wl.build_message(OWNER)
    assert msg.splitlines() == [
        "Needs you: new owner lead from boundlessaz.com",
        "Test Owner, call/text 480-555-0100, owner@example.com",
        "Queen Creek, 2 to 4 house(s)",
        "Says: Vacant now, want to start October.",
        "Page: property-management-queen-creek",
    ]


def test_contact_message_and_ignored_form():
    msg = wl.build_message(CONTACT)
    assert msg.startswith("Needs you: new website message from boundlessaz.com\n")
    assert "Topic: Renting a home" in msg and msg.endswith("Page: contact")
    assert wl.build_message({"form_name": "newsletter", "data": {}}) is None
    assert wl.build_message({}) is None


def test_message_is_capped():
    long = dict(OWNER, id="sub-3", data=dict(OWNER["data"], message="x" * 5000))
    assert len(wl.build_message(long)) < 600


def test_handle_pings_once_and_dedupes():
    sent = []
    code, text = wl.handle(OWNER, lambda m: sent.append(m) or True)
    assert (code, text) == (200, "poked") and len(sent) == 1
    code, text = wl.handle(OWNER, lambda m: sent.append(m) or True)
    assert (code, text) == (200, "duplicate") and len(sent) == 1


def test_handle_ignored_bad_and_failed():
    assert wl.handle({"form_name": "newsletter"}, lambda m: True) == (200, "ignored-form")
    assert wl.handle("nope", lambda m: True) == (400, "bad payload")
    assert wl.handle(OWNER, lambda m: False) == (502, "poke-failed")


def test_rate_limit():
    sent = []
    for i in range(wl.MAX_PER_HOUR):
        assert wl.handle(dict(OWNER, id=f"s{i}"), lambda m: sent.append(m) or True)[1] == "poked"
    assert wl.handle(dict(OWNER, id="one-more"), lambda m: sent.append(m) or True) == (429, "rate-limited")
    assert len(sent) == wl.MAX_PER_HOUR
