"""Question routing: deterministic standing-rule router FIRST, fact sheets second.

v1 policy (approved plan): standing rules answer the bulk of real traffic with
canned templates; genuine property-specific questions go to NEEDS_HUMAN with
the "checking with the owner" reply + a needs-you Poke. LLM-composed fact
answers are a v1.1 upgrade, only if NEEDS_HUMAN volume annoys Alex.
"""

import re

# Standing rules (property-facts/README.md). Order matters: first hit wins.
_RULES = [
    # business operation / sublease / STR arbitrage: blanket brokerage decline
    # (Alex 2026-08-18, Tynisha at New Town asked about running a licensed
    # home-healthcare business from the property). The decline reason is ALWAYS
    # residential-use-only + named-tenants-only, applied uniformly - never the
    # nature of who would live there (fair-housing).
    ("no_business_sublease", re.compile(
        r"(sub-?let|sub-?lease|re-?rent|rental arbitrage|\barbitrage\b|"
        r"air ?bnb|\bvrbo\b|short[- ]term rental|corporate housing|"
        r"(run|operate|start|open|use)\w*[^.\n]{0,40}"
        r"(business|facility|day ?care|child ?care|group home|care home|"
        r"assisted living|sober living|home[- ]?health)|"
        r"business[^.\n]{0,30}(out of|from|at) (the|this) (home|house|property)|"
        r"operate out of)", re.IGNORECASE)),
    # floor plans / dimensions / sqft-per-room: always "unavailable, come see it"
    ("floor_plan", re.compile(
        r"(floor ?plan|lay ?out|dimensions|measurements|how (big|large) is the "
        r"(bed|living|master|room)|room size|sq ?ft of)", re.IGNORECASE)),
    # negotiation is handled by intent, but catch question-phrased ones too
    ("apply_first", re.compile(
        r"(lower the (rent|price)|negotiat|take \$|accept \$|come down on|"
        r"any flexibility on (the )?(rent|price)|rent negotiable)", re.IGNORECASE)),
    # modifications: as-is. Word boundaries matter: an unanchored "add" matched
    # inside "address"/"additional" in relay boilerplate and misfired on 8 of 12
    # new inquiries during the 7/30-8/1 shadow soak.
    ("as_is", re.compile(
        r"\b(install|add|put in|replace|swap|change out|paint|turf|fence in)\b|"
        r"can you (fix|update|upgrade)", re.IGNORECASE)),
    # approval requirements: blanket 3x-income answer (Alex 2026-08-05, Daliana)
    ("requirements", re.compile(
        r"\b(requirements?( to (get )?approved)?|qualifications?|qualify|"
        r"get approved|approval process|income requirement|"
        r"(bad|poor|low|not( so)? good|good income but).{0,20}credit|"
        r"credit (score|check|isn.?t|not))\b", re.IGNORECASE)),
    # virtual tours: never live - video walkthrough by text (Alex 2026-08-05)
    ("virtual_tour", re.compile(
        r"(virtual (tour|showing|walk.?through)|video (tour|call|chat|walk.?through)|"
        r"facetime|face time|live tour|tour (over|by) (video|phone)|"
        r"see it virtually|remote (tour|showing))", re.IGNORECASE)),
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

VIRTUAL_TOUR_LINE = ("We don't do live virtual tours, but I have a video "
                     "walkthrough of the home I can text you - what's the best "
                     "phone number to send it to?")

REQUIREMENTS_LINE = ("The main thing we look for is income of roughly 3x the "
                     "monthly rent - beyond that, credit isn't an automatic "
                     "yes or no on its own, and the exact requirements come "
                     "together through the application as a whole.")

# Short inline answers for merging into the FIRST reply (one send per inquiry,
# never a second standing-rule email - shadow soak 7/30-8/1 caught the double).
ANSWER_LINES = {
    "no_business_sublease": (
        "Thanks for asking upfront - unfortunately our brokerage doesn't "
        "allow that. Our leases are residential use only for the tenants "
        "named on the lease, so operating a business from the home or "
        "re-renting it (sublease, Airbnb, or short-term rental) isn't "
        "something we can accommodate."),
    "as_is": ("Good question - the home is offered just as you see it, so we "
              "aren't able to make changes or additions."),
    "floor_plan": ("On the floor plan - we don't have one available to send, "
                   "but you're welcome to come see the layout in person."),
    "appliances": APPLIANCES_LINE,
    "requirements": REQUIREMENTS_LINE,
    "virtual_tour": VIRTUAL_TOUR_LINE,
    "fair_housing": ("On your question - I have to stick to details about the "
                     "property itself, but I'm happy to tell you all about the home."),
    "apply_first": ("On pricing - the best first step is getting an application "
                    "in; the owner reviews offers from applicants."),
}


def standing_rule_for(question_text: str) -> str | None:
    """Return the rule key for a question, or None if no standing rule applies:
    floor_plan | apply_first | as_is | appliances | fair_housing"""
    if not question_text:
        return None
    for key, rx in _RULES:
        if rx.search(question_text):
            return key
    return None
