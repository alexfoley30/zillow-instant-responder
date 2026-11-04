"""Address + showing-window primitives, shared by the service and the scripts.

Why this module exists: scripts/shadow_dump.py has to run on Apple's
/usr/bin/python3 (3.9), the only interpreter on Alex's laptop with
firebase_admin installed, and 3.9 raises TypeError at import on the PEP 604
`X | None` annotations used throughout rules.py / ledger.py. So the ~30 lines
of rules.py it needed were hand-copied into it, with a comment saying to keep
them in sync. They were not kept in sync, and both known bugs the originals
fixed came back in the copy (substring street-number matching, midnight-
crossing showing windows).

Everything here is therefore deliberately 3.9-compatible: no PEP 604 unions,
no `X | None`, nothing but re + datetime. rules.py re-exports these names, so
`rules.number_and_core` and friends keep working for every existing caller.
"""

import re
from datetime import time, timedelta

# Showing windows (America/Phoenix). weekday(): Mon=0 .. Sun=6
SHOWING_WINDOWS = {
    0: (time(10, 0), time(18, 30)),  # Mon
    1: (time(10, 0), time(15, 0)),   # Tue
    2: (time(10, 0), time(18, 30)),  # Wed
    3: (time(10, 0), time(15, 0)),   # Thu
    4: (time(10, 0), time(18, 30)),  # Fri
    5: (time(10, 0), time(14, 0)),   # Sat
    6: (time(10, 0), time(14, 0)),   # Sun
}

MIN_NOTICE_HOURS = 2          # same-day allowed since 2026-07-27
SHOWING_MINUTES = 30

_DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw",
                 "north", "south", "east", "west"}
_STREET_TYPES = {"ave", "avenue", "st", "street", "dr", "drive", "rd", "road",
                 "ln", "lane", "ct", "court", "blvd", "boulevard", "way",
                 "pl", "place", "cir", "circle", "trl", "trail", "pkwy",
                 "parkway", "loop", "ter", "terrace"}

# Leased / off-market homes. The service reads this list from Firestore
# (zillow_config/properties) so marking a home leased never needs a redeploy;
# scripts/sync_facts.py is what pushes this list there, and shadow_dump audits
# against the same list. Notes are kept beside each address so the audit can
# print why a home is on it.
LEASED_PROPERTIES = [
    ("3309 E San Remo Ave", "Gilbert 85234 - leased 2026-07-04"),
    ("8743 E Palo Verde Dr", "Scottsdale 85250 - leased 2026-07-08"),
    ("22500 N Greenland Park Dr", "Maricopa 85139 - leased 2026-08-01"),
    ("1641 E Coronado Rd", "Phoenix 85006 - leased 2026-08-01"),
    ("1294 E Apricot Ln", "Gilbert 85298 - leased 2026-08-09 ($3,249)"),
    ("3896 E Alfalfa Dr", "Gilbert 85298 - leased 2026-08-20 ($3,399)"),
    ("736 E Gold Dust Way", "Gilbert - leased (Alex audit 2026-08-22)"),
    ("300 N Gila Springs Blvd", "Chandler - leased (Alex audit 2026-08-22)"),
    ("1110 E Redfield Rd", "Tempe 85283 - leased 2026-08-25 ($2,999, Courtney)"),
    ("26009 S New Town Dr", "Sun Lakes 85248 - leased 2026-08-28 (McCombs/McCawley)"),
    ("2118 S El Marino", "Mesa 85202 - lease out for signature 2026-08-31"),
    ("3308 S Mariana Cir", "Tempe 85282 - lease out 2026-09-02 ($3,249)"),
    ("1407 W Glenrosa Ave", "Phoenix 85013 - rented (Alex confirmed 2026-10-05)"),
]

BLOCKED_ADDRESSES = [addr for addr, _note in LEASED_PROPERTIES]


def street_of(address):
    """Everything before the first comma: '1641 E Coronado Rd, Phoenix, AZ
    85006' -> '1641 E Coronado Rd'. The display form used in pings and
    calendar summaries, and the input to every key below."""
    return (address or "").split(",")[0].strip()


def number_and_core(address):
    """'3309 E San Remo Ave' -> ('3309', 'san remo'); (None, None) if unparseable."""
    tokens = re.findall(r"[a-z0-9']+", (address or "").lower())
    if not tokens or not tokens[0].isdigit():
        return None, None
    core = [t for t in tokens[1:] if t not in _DIRECTIONALS and t not in _STREET_TYPES]
    return tokens[0], " ".join(core)


def number_present(number, haystack):
    """Whole-token street-number match: '1641' must not match inside '16413'
    (regression test caught the substring version blocking the wrong house)."""
    return re.search(r"\b%s\b" % re.escape(number), haystack) is not None


def same_property(address, haystack):
    """Same-property-only rule: BOTH street number and street-name core present."""
    number, core = number_and_core(street_of(address))
    if not number or not core:
        return False
    hay = (haystack or "").lower()
    return number_present(number, hay) and core in hay


def addr_slug(address):
    """'1641 E Coronado Rd, Phoenix...' -> '1641-e-coronado-rd' (fact-sheet key)."""
    return re.sub(r"[^a-z0-9]+", "-", street_of(address).lower()).strip("-")


def is_blocked_address(address, blocked_list):
    a = (address or "").lower()
    for blocked in blocked_list or []:
        number, core = number_and_core(blocked)
        if number and core and number_present(number, a) and core in a:
            return True
    return False


def in_window(start_az):
    """True when a SHOWING_MINUTES slot starting at start_az fits that day's
    window. A slot that crosses midnight wrapped .time() to 00:00 and passed
    the close check - 11:30 PM counted as "in window" (round-2 shadow,
    Lyndsey) - so the day is checked explicitly."""
    lo, hi = SHOWING_WINDOWS[start_az.weekday()]
    end_dt = start_az + timedelta(minutes=SHOWING_MINUTES)
    if end_dt.date() != start_az.date():
        return False
    # start inside window and the 30-min slot must end by window close
    return lo <= start_az.time() and end_dt.time() <= hi
