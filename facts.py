"""Question routing: deterministic standing-rule router FIRST, fact sheets second.

v1 policy (approved plan): standing rules answer the bulk of real traffic with
canned templates; genuine property-specific questions go to NEEDS_HUMAN with
the "checking with the owner" reply + a needs-you Poke. LLM-composed fact
answers are a v1.1 upgrade, only if NEEDS_HUMAN volume annoys Alex.
"""

import re

# Standing rules (property-facts/README.md). Order matters: first hit wins.
_RULES = [
    # floor plans / dimensions / sqft-per-room: always "unavailable, come see it"
    ("floor_plan", re.compile(
        r"(floor ?plan|lay ?out|dimensions|measurements|how (big|large) is the "
        r"(bed|living|master|room)|room size|sq ?ft of)", re.IGNORECASE)),
    # negotiation is handled by intent, but catch question-phrased ones too
    ("apply_first", re.compile(
        r"(lower the (rent|price)|negotiat|take \$|accept \$|come down on|"
        r"any flexibility on (the )?(rent|price)|rent negotiable)", re.IGNORECASE)),
    # modifications: as-is
    ("as_is", re.compile(
        r"(install|add|put in|replace|swap|change out|paint|turf|fence in|"
        r"can you (fix|update|upgrade))", re.IGNORECASE)),
    # appliances: always included on Alex listings
    ("appliances", re.compile(
        r"(washer|dryer|fridge|refrigerator|appliances (included|come with)|"
        r"come[s]? with (a )?(washer|dryer|fridge))", re.IGNORECASE)),
    # fair housing: neighbors / demographics / schools quality / safety
    ("fair_housing", re.compile(
        r"(neighborhood safe|is it safe|crime|what are the neighbors|"
        r"good schools|school district (good|rating)|kind of people|"
        r"demographic)", re.IGNORECASE)),
]

APPLIANCES_LINE = ("Yes, the refrigerator, washer, and dryer are all included "
                   "with the home.")


def standing_rule_for(question_text: str) -> str | None:
    """Return the rule key for a question, or None if no standing rule applies:
    floor_plan | apply_first | as_is | appliances | fair_housing"""
    if not question_text:
        return None
    for key, rx in _RULES:
        if rx.search(question_text):
            return key
    return None
