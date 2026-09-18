#!/usr/bin/env python3
"""Who took the application. Writes the application fields on a zillow_threads
doc (see ledger.py, "application attribution"). Runs on Alex's Mac.

    python3 scripts/application.py log --property "26009 S New Town Dr" --renter Bonnie --by alex \
        [--date 2026-08-26] [--leased 2026-08-27] [--note "..."] [--thread <thread_id>]
    python3 scripts/application.py show [--property "2118 S El Marino"]
    python3 scripts/application.py scoreboard

Matching is exact: house number + street words, and the renter's first name.
Two or more matching threads stop the write; pass --thread to pick one.
Credentials: GOOGLE_APPLICATION_CREDENTIALS, else ~/.config/boundless/firebase-sa.json.
"""

import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SA_DEFAULT = os.path.expanduser("~/.config/boundless/firebase-sa.json")
if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and os.path.exists(SA_DEFAULT):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SA_DEFAULT

import ledger  # noqa: E402


def _short(t: dict) -> str:
    return (f"{t.get('id','')[:18]:18s} {str(t.get('property_address',''))[:32]:32s} "
            f"{str(t.get('renter_name',''))[:14]:14s} state={str(t.get('state',''))[:9]:9s} "
            f"shown-by={str(t.get('agent') or '-')[:12]:12s} booked={str(t.get('booked_start_iso') or '')[:10]:10s} "
            f"app-by={str(t.get('application_by') or '-')[:12]:12s} app={str(t.get('application_at') or '')[:10]:10s} "
            f"lease={str(t.get('lease_signed_at') or '')[:10]}")


def cmd_log(a):
    threads = ledger.list_threads()
    if a.thread:
        hits = [t for t in threads if t["id"] == a.thread]
    else:
        hits = ledger.match_threads(threads, a.property, a.renter)
    if not hits:
        print("no thread matches; exact keys are street number + street words and the renter's first name.")
        near = [t for t in threads if ledger.street_key(t.get("property_address", "")) == ledger.street_key(a.property or "")]
        for t in near:
            print("  same property:", _short(t))
        return 1
    if len(hits) > 1:
        print(f"{len(hits)} threads match; pick one with --thread <id>:")
        for t in hits:
            print("  ", _short(t))
        return 1
    t = hits[0]
    fields = ledger.set_application(t["id"], a.by, a.date or "", a.note or "", a.leased or "")
    print("logged on", t["id"], "|", t.get("property_address"), "|", t.get("renter_name"))
    print({k: v for k, v in fields.items() if k != "application_logged_at"})
    return 0


def cmd_show(a):
    threads = ledger.list_threads()
    if a.property:
        pk = ledger.street_key(a.property)
        threads = [t for t in threads if ledger.street_key(t.get("property_address", "")) == pk]
    threads.sort(key=lambda t: (str(t.get("property_address", "")), str(t.get("booked_start_iso") or "")))
    for t in threads:
        if a.property or t.get("application_by"):
            print(_short(t))
    return 0


def cmd_scoreboard(a):
    threads = ledger.list_threads()
    shown = Counter(ledger.agent_full_name(t["agent"]) for t in threads if t.get("agent") and t.get("booked_start_iso"))
    apps = Counter(t["application_by"] for t in threads if t.get("application_by"))
    leases = Counter(t["application_by"] for t in threads if t.get("application_by") and t.get("lease_signed_at"))
    names = sorted(set(shown) | set(apps) | set(leases))
    print(f"{'agent':16s} {'showings (door opened)':24s} {'applications taken':20s} {'leases signed'}")
    for n in names:
        print(f"{n:16s} {shown.get(n, 0):<24d} {apps.get(n, 0):<20d} {leases.get(n, 0)}")
    print("\nshowings count threads with a booked slot and an agent (pipeline era, Aug 2026 on).")
    print("applications and leases count only what a person logged with `application.py log`.")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("log"); s.add_argument("--property"); s.add_argument("--renter"); s.add_argument("--by", required=True)
    s.add_argument("--date", default=""); s.add_argument("--leased", default=""); s.add_argument("--note", default=""); s.add_argument("--thread", default="")
    s.set_defaults(fn=cmd_log)
    s = sub.add_parser("show"); s.add_argument("--property", default=""); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("scoreboard"); s.set_defaults(fn=cmd_scoreboard)
    a = p.parse_args()
    if a.cmd == "log" and not a.thread and not (a.property and a.renter):
        p.error("log needs --property and --renter, or --thread")
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
