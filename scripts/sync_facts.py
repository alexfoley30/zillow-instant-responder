#!/usr/bin/env python3
"""Sync property-facts/*.md + standing rules + blocked-address list into
Firestore. Runs on Alex's laptop (ADC) - the watchdog sweep re-runs this on a
sha-diff so edits to fact sheets reach the Render service without a deploy.

Usage: python3 scripts/sync_facts.py
"""

import hashlib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ledger  # noqa: E402

FACTS_DIR = os.path.expanduser(
    "~/Desktop/Boundless Real Estate - AI/property-facts")

# Leased/off-market list (was hardcoded in responder.py + SKILL.md - now lives
# in Firestore so marking a home leased never needs a redeploy).
BLOCKED_ADDRESSES = [
    "3309 E San Remo Ave",   # Gilbert 85234 - leased 2026-07-04
    "8743 E Palo Verde Dr",  # Scottsdale 85250 - leased 2026-07-08
    "22500 N Greenland Park Dr",  # Maricopa 85139 - leased 2026-08-01
    "1641 E Coronado Rd",    # Phoenix 85006 - leased 2026-08-01
    "1294 E Apricot Ln",     # Gilbert 85298 - leased 2026-08-09 ($3,249)
    "3896 E Alfalfa Dr",     # Gilbert 85298 - leased 2026-08-20 ($3,399)
    "736 E Gold Dust Way",   # Gilbert - leased (Alex audit 2026-08-22)
    "300 N Gila Springs Blvd",  # Chandler - leased (Alex audit 2026-08-22)
]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def main():
    db = ledger.init_db()
    synced = 0
    readme_rules = ""

    for name in sorted(os.listdir(FACTS_DIR)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(FACTS_DIR, name)
        raw = open(path, encoding="utf-8").read()
        if name == "README.md":
            m = re.search(r"Standing rules.*", raw, re.DOTALL)
            readme_rules = m.group(0) if m else raw
            continue
        slug = name[:-3]
        doc = db.collection("property_facts").document(slug)
        snap = doc.get()
        if snap.exists and snap.get("source_sha256") == sha(raw):
            continue
        doc.set({"raw_md": raw, "source_path": path, "source_sha256": sha(raw),
                 "updated_at": ledger.firestore.SERVER_TIMESTAMP})
        synced += 1
        print(f"synced: {slug}")

    if readme_rules:
        doc = db.collection("property_facts").document("_standing_rules")
        snap = doc.get()
        if not (snap.exists and snap.get("source_sha256") == sha(readme_rules)):
            doc.set({"raw_md": readme_rules, "source_sha256": sha(readme_rules),
                     "updated_at": ledger.firestore.SERVER_TIMESTAMP})
            print("synced: _standing_rules")
            synced += 1

    db.collection("zillow_config").document("properties").set(
        {"blocked_addresses": BLOCKED_ADDRESSES}, merge=True)
    print(f"blocked list set ({len(BLOCKED_ADDRESSES)} addresses). "
          f"{synced} docs synced.")


if __name__ == "__main__":
    main()
