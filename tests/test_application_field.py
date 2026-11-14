"""Application attribution helpers (ledger.py): exact keys, no substring
matching, agent name expansion. No Firestore access."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ledger  # noqa: E402

THREADS = [
    {"id": "a", "property_address": "1294 E Apricot Ln, Gilbert, AZ, 85298", "renter_name": "Angela"},
    {"id": "b", "property_address": "1294 E Apricot Ln, Gilbert, AZ 85298", "renter_name": "Jamie"},
    {"id": "c", "property_address": "26009 S New Town Dr, Sun Lakes, AZ, 85248", "renter_name": "Kim"},
    {"id": "d", "property_address": "26009 S New Town Dr, Sun Lakes, AZ, 85248", "renter_name": "Kimberly"},
    {"id": "e", "property_address": "26009 S New Town Dr, Sun Lakes, AZ, 85248", "renter_name": "Bonnie"},
    {"id": "f", "property_address": "26009 S New Town Dr, Sun Lakes, AZ, 85248", "renter_name": "Bonnie"},
]


def test_street_key_ignores_city_state_zip_punctuation():
    assert ledger.street_key("1294 E Apricot Ln, Gilbert, AZ 85298") == ledger.street_key("1294 E Apricot Ln, Gilbert, AZ, 85298")
    assert ledger.street_key("1294 E. Apricot Ln.") == "1294eapricotln"
    assert ledger.street_key("") == ""


def test_no_substring_matching_on_property_or_name():
    assert ledger.match_threads(THREADS, "1294 E Apricot", "Angela") == []       # partial street
    assert [t["id"] for t in ledger.match_threads(THREADS, "26009 S New Town Dr", "Kim")] == ["c"]  # not Kimberly
    assert ledger.match_threads(THREADS, "26009 S New Town Dr", "Kimb") == []


def test_first_name_key_and_ambiguity():
    assert ledger.first_name_key("Bonnie L. McCombs") == "bonnie"
    assert ledger.first_name_key("  ") == ""
    assert [t["id"] for t in ledger.match_threads(THREADS, "1294 E Apricot Ln, Gilbert, AZ", "angela")] == ["a"]
    assert len(ledger.match_threads(THREADS, "26009 S New Town Dr", "Bonnie")) == 2   # caller must pick --thread
    assert ledger.match_threads(THREADS, "", "Bonnie") == []


def test_agent_full_name():
    assert ledger.agent_full_name("jace") == "Jace Johnson"
    assert ledger.agent_full_name("Alex Foley") == "Alex Foley"
    assert ledger.agent_full_name("Bre") == "Brianna Foley"
    assert ledger.agent_full_name("Someone New") == "Someone New"
