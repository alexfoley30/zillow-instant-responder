#!/usr/bin/env python3
"""Read what the Zillow responder WOULD have sent while stuck in DRY_RUN.

The service mirrors every would-be action into Firestore `zillow_shadow`
instead of emailing renters. Nobody has ever read it. This dumps it so the
copy and the scheduling decisions can be reviewed before flipping
DRY_RUN=false.

STRICTLY READ-ONLY. A runtime guard (see _arm_readonly_guard) makes every
Firestore write method raise, so an accidental write is impossible, not
merely absent.

RUN IT WITH APPLE PYTHON. This is not a preference:
    /usr/bin/python3 scripts/shadow_dump.py --count-only

  * /usr/bin/python3 (3.9.6) is the ONLY interpreter here with firebase_admin
    + google-cloud-firestore installed. Homebrew 3.13/3.14 lack them.
  * Because it is 3.9, this file must NOT import ledger/rules/calendar_logic/
    facts/gmail_client/llm - they use PEP 604 `X | None` annotations that
    raise TypeError at import on 3.9. The ~30 lines we need from rules.py are
    reimplemented below and must be kept in sync with it.
  * Credentials come from the service-account JSON, never ADC (ADC here is
    stale, has no quota_project_id, and hangs ~300s before failing).
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
import warnings
from datetime import datetime, time, timedelta, timezone

warnings.filterwarnings("ignore")  # 3.9 EOL FutureWarning + LibreSSL notice
# grpc spews "FD from fork parent still in poll list" across stdout when the
# process forks (e.g. --open). Must be set before grpc loads.
os.environ.setdefault("GRPC_VERBOSITY", "NONE")
os.environ.setdefault("GLOG_minloglevel", "3")

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    from google.api_core import exceptions as gexc
except ImportError as e:
    sys.exit(f"ERROR: {e}\nRun with /usr/bin/python3 - it is the only "
             f"interpreter here with firebase_admin installed.")

SA_PATH = os.path.expanduser("~/.config/boundless/firebase-sa.json")
PROJECT_ID = "boundless-portal-c94d0"
PHX = timezone(timedelta(hours=-7))          # Arizona: no DST, fixed offset
DEFAULT_OUT = os.path.expanduser("~/Desktop/zillow-shadow-review")

# Milestones used to date-stamp findings so old, already-fixed noise is not
# reported as new breakage.
MIRROR_SHIPPED = datetime(2026, 7, 30, 2, 44, tzinfo=timezone.utc)   # 1ff18cd
DOUBLE_SEND_FIXED = datetime(2026, 8, 1, 21, 0, tzinfo=timezone.utc)  # b955b2a

# Must match scripts/sync_facts.py BLOCKED_ADDRESSES.
EXPECTED_BLOCKED = [
    ("3309 E San Remo Ave", "Gilbert 85234 - leased 2026-07-04"),
    ("8743 E Palo Verde Dr", "Scottsdale 85250 - leased 2026-07-08"),
    ("22500 N Greenland Park Dr", "Maricopa 85139 - leased 2026-08-01"),
    ("1641 E Coronado Rd", "Phoenix 85006 - leased 2026-08-01"),
]

# Renters known to have hit the responder overnight 8/2-8/3 while it was
# shadowing. Absence of any of these is itself a finding. Matched on a word
# boundary: a substring test makes "anna" match "Deanna".
NAMED_CASES = ["Anna", "alondra", "Lyndsey"]

# A shadow stage that texts Alex, not the renter. Offering a showing at a
# leased house is only a real exposure if the renter would have SEEN it.
def is_poke_stage(stage_key):
    return (stage_key or "").startswith("poke__")

# ---- mirrored from rules.py (cannot import it on 3.9) --------------------
SHOWING_WINDOWS = {           # weekday(): Mon=0 .. Sun=6
    0: (time(10, 0), time(18, 30)), 1: (time(10, 0), time(15, 0)),
    2: (time(10, 0), time(18, 30)), 3: (time(10, 0), time(15, 0)),
    4: (time(10, 0), time(18, 30)), 5: (time(10, 0), time(14, 0)),
    6: (time(10, 0), time(14, 0)),
}
MIN_NOTICE_HOURS = 2
SHOWING_MINUTES = 30
KNOWN_AGENTS = {"Alex Foley", "Jace Johnson", "Rhett Lueck"}
_DIRECTIONALS = {"n", "s", "e", "w", "ne", "nw", "se", "sw",
                 "north", "south", "east", "west"}
_STREET_TYPES = {"ave", "avenue", "st", "street", "dr", "drive", "rd", "road",
                 "ln", "lane", "ct", "court", "blvd", "boulevard", "way",
                 "pl", "place", "cir", "circle", "trl", "trail", "pkwy",
                 "parkway", "loop", "ter", "terrace"}


def number_and_core(address):
    """'3309 E San Remo Ave' -> ('3309', 'san remo')."""
    tokens = re.findall(r"[a-z0-9']+", (address or "").lower())
    if not tokens or not tokens[0].isdigit():
        return None, None
    core = [t for t in tokens[1:]
            if t not in _DIRECTIONALS and t not in _STREET_TYPES]
    return tokens[0], " ".join(core)


def addr_matches(blocked, address):
    """Fuzzy match, same semantics as rules.is_blocked_address."""
    number, core = number_and_core(blocked)
    if not number or not core:
        return False
    a = (address or "").lower()
    return number in a and core in a


def in_window(start):
    lo, hi = SHOWING_WINDOWS[start.weekday()]
    end = (start + timedelta(minutes=SHOWING_MINUTES)).time()
    return lo <= start.time() and end <= hi


# ---- read-only guard ----------------------------------------------------

def _arm_readonly_guard():
    """Make every Firestore mutation raise. Armed before any query runs."""
    from google.cloud.firestore_v1 import (
        DocumentReference, CollectionReference, WriteBatch)

    def _deny(name):
        def blocked(*_a, **_kw):
            raise RuntimeError(
                "shadow_dump is read-only; %s() is disabled" % name)
        return blocked

    for cls, methods in ((DocumentReference, ("set", "create", "update", "delete")),
                         (CollectionReference, ("add",)),
                         (WriteBatch, ("commit",))):
        for m in methods:
            setattr(cls, m, _deny("%s.%s" % (cls.__name__, m)))


# ---- connect ------------------------------------------------------------

def connect(timeout):
    if not os.path.exists(SA_PATH):
        sys.exit("ERROR: service account not found at %s\n"
                 "This script cannot use ADC (expired, hangs ~300s)." % SA_PATH)
    try:
        cred = credentials.Certificate(SA_PATH)
    except (ValueError, KeyError):
        sys.exit("ERROR: %s is not a valid service-account key "
                 "(wrong JSON shape)." % SA_PATH)
    except PermissionError:
        sys.exit("ERROR: cannot read %s - try: chmod 600 %s" % (SA_PATH, SA_PATH))
    try:
        firebase_admin.initialize_app(cred, {"projectId": PROJECT_ID})
    except ValueError:
        pass  # already initialized
    _arm_readonly_guard()
    return firestore.client()


def die_on_gcp(e, timeout):
    """One actionable sentence per failure mode, never a traceback."""
    import google.auth.exceptions as authexc
    if isinstance(e, authexc.RefreshError):
        sys.exit("ERROR: key rejected. The service account key was likely "
                 "rotated or the account disabled.\nMint a new key: "
                 "console.cloud.google.com/iam-admin/serviceaccounts"
                 "?project=%s" % PROJECT_ID)
    if isinstance(e, gexc.PermissionDenied):
        sys.exit("ERROR: authenticated, but this service account lacks "
                 "roles/datastore.user on %s." % PROJECT_ID)
    if isinstance(e, (gexc.DeadlineExceeded, gexc.ServiceUnavailable)):
        sys.exit("ERROR: timed out after %ss. grpc+LibreSSL on Python 3.9 is "
                 "slow on first connect - retry with --timeout %d before "
                 "assuming an auth problem." % (timeout, max(180, timeout * 3)))
    if isinstance(e, gexc.NotFound):
        sys.exit("ERROR: project or Firestore database not found (%s)." % PROJECT_ID)
    sys.exit("ERROR: %s: %s" % (type(e).__name__, e))


# ---- fetch --------------------------------------------------------------

def stream(db, name, timeout, since=None, ids_only=False):
    col = db.collection(name)
    q = col
    if since is not None:
        q = col.where("created_at", ">=", since).order_by("created_at")
    if ids_only:
        q = q.select([])
    return list(q.stream(timeout=timeout))


def fetch_all(db, args):
    t = args.timeout
    try:
        # Cheapest doc first: it pays the TLS handshake, so an auth failure
        # surfaces as an auth error rather than a mystery hang mid-dump.
        cfg = db.collection("zillow_config").document("properties").get(timeout=t)
        blocked = (cfg.to_dict() or {}).get("blocked_addresses", []) if cfg.exists else []
        shadow = stream(db, "zillow_shadow", t, since=args._since)
        msgs = stream(db, "zillow_messages", t)
        threads = stream(db, "zillow_threads", t)
        sends = stream(db, "zillow_sends", t, ids_only=True)
    except Exception as e:  # noqa: BLE001
        die_on_gcp(e, t)
    return {
        "blocked": blocked,
        "shadow": [(d.id, d.to_dict() or {}) for d in shadow],
        "msgs": {d.id: (d.to_dict() or {}) for d in msgs},
        "threads": {d.id: (d.to_dict() or {}) for d in threads},
        "sent_ids": set(d.id for d in sends),
        "config_exists": cfg.exists,
    }


# ---- helpers ------------------------------------------------------------

def parse_id(doc_id):
    parts = doc_id.split("__", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (doc_id, "")


def phx(dt):
    return dt.astimezone(PHX) if dt else None


def human(dt):
    return phx(dt).strftime("%a %b %-d, %-I:%M %p") if dt else "unknown time"


def iso(dt):
    return dt.isoformat() if dt else None


def redact(email, full):
    if full or not email or "@" not in email:
        return email or ""
    local, _, dom = email.partition("@")
    return "%s%s@%s" % (local[:2], "*" * 4, dom)


def classify_event(stage_key, data):
    cal = data.get("would_calendar") or {}
    if cal.get("cancel"):
        return "cancel"
    if cal.get("fold"):
        return "book_fold"
    if cal.get("create"):
        return "book_new"
    if stage_key.startswith("poke__"):
        return "poke"
    return "reply"


def correlate(thread_id, stage_key, data, created, bundle):
    """Find the renter message that triggered this shadow doc. 4 tiers."""
    mid = (data.get("trigger_message_id") or "").strip()
    conf = "exact"
    if not mid:
        m = re.match(r"^(?:reply|booked__pending)__(.+)$", stage_key) or \
            re.match(r"^booked__pending_(.+)$", stage_key)
        if m:
            mid = m.group(1)
    if not mid and created:
        # Timing inference: claim_message stamps processed_at at the top of the
        # request, the shadow write happens later in that same request.
        best, best_ts = None, None
        for cand_id, msg in bundle["msgs"].items():
            if msg.get("thread_id") != thread_id:
                continue
            ts = msg.get("processed_at")
            if not ts or ts > created + timedelta(seconds=5):
                continue
            if best_ts is None or ts > best_ts:
                best, best_ts = cand_id, ts
        mid, conf = best, "inferred"
    if not mid:
        return {"message_id": None, "confidence": "none", "renter_text": None,
                "intent": None, "outcome": None, "fallback": False}
    msg = bundle["msgs"].get(mid, {})
    cls = msg.get("classification") or {}
    return {
        "message_id": mid,
        "confidence": conf if mid else "none",
        "renter_text": msg.get("renter_text"),
        "intent": cls.get("intent"),
        "outcome": msg.get("outcome"),
        "fallback": bool(cls.get("_fallback")),
        "processed_at": iso(msg.get("processed_at")),
    }


PLACEHOLDER_PATTERNS = [
    (r"\bHi there,", "greeting fell back to 'there' - renter name never parsed"),
    (r"about\s*[!,]", "address is empty in the sentence 'reaching out about ...'"),
    (r"[{}]", "literal brace left in the text - template not filled"),
    (r"\bNone\b", "the word 'None' leaked into renter-facing copy"),
    (r"\[Your Name\]", "placeholder signature '[Your Name]'"),
]


def check_body(body):
    out = []
    for pat, why in PLACEHOLDER_PATTERNS:
        if re.search(pat, body or ""):
            out.append(why)
    # The greeting is the most visible line in the email and the subject-line
    # parser can hand it a street suffix ("Hi Dr,") or an un-capitalised name
    # ("Hi alondra,"). Both ship as-is to the renter.
    g = re.match(r"Hi ([^,\n]+),", body or "")
    if g:
        name = g.group(1).strip()
        if name.lower() in _STREET_TYPES:
            out.append("greeting says 'Hi %s,' - a street suffix was parsed as "
                       "the renter's first name" % name)
        elif name == "there":
            out.append("greeting fell back to 'Hi there,' - name never parsed")
        elif name[:1].islower():
            out.append("greeting says 'Hi %s,' - name not capitalised" % name)
    return out


# ---- analysis -----------------------------------------------------------

def build_model(bundle, args):
    events = []
    for doc_id, data in bundle["shadow"]:
        thread_id, stage_key = parse_id(doc_id)
        created = data.get("created_at")
        th = bundle["threads"].get(thread_id, {})
        kind = classify_event(stage_key, data)
        ev = {
            "doc_id": doc_id, "thread_id": thread_id, "stage_key": stage_key,
            "kind": kind,
            "template": data.get("template"),
            "created_utc": iso(created), "created_phx": iso(phx(created)),
            "created_human": human(created),
            "_created": created,
            "body": data.get("would_body"),
            "poke": data.get("would_poke"),
            "calendar": data.get("would_calendar"),
            "labels_add": data.get("would_labels_add") or [],
            "labels_remove": data.get("would_labels_remove") or [],
            "body_sha256": data.get("body_sha256"),
            "to_relay": redact(data.get("to_relay"), args.full_emails),
            "already_live": doc_id in bundle["sent_ids"],
            "renter_name": th.get("renter_name"),
            "address": th.get("property_address"),
            "state": th.get("state"),
            "trigger": correlate(thread_id, stage_key, data, created, bundle),
            "body_issues": check_body(data.get("would_body")),
        }
        events.append(ev)
    events.sort(key=lambda e: (e["_created"] or datetime.min.replace(tzinfo=timezone.utc)))

    threads = {}
    for ev in events:
        t = threads.setdefault(ev["thread_id"], {
            "thread_id": ev["thread_id"],
            "renter_name": ev["renter_name"],
            "address": ev["address"],
            "state": ev["state"],
            "events": [],
        })
        t["events"].append(ev)

    blocklist = check_blocklist(bundle, threads)
    anomalies = find_anomalies(events, threads, bundle, blocklist)
    blockers = [a for a in anomalies if a["severity"] == "BLOCKER"]

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project": PROJECT_ID,
            "window": args._since.isoformat() if args._since else "all-time",
            "counts": {
                "shadow_docs": len(bundle["shadow"]),
                "threads_with_shadow": len(threads),
                "zillow_threads": len(bundle["threads"]),
                "zillow_messages": len(bundle["msgs"]),
                "zillow_sends": len(bundle["sent_ids"]),
            },
        },
        "blocklist": blocklist,
        "threads": list(threads.values()),
        "events": events,
        "anomalies": anomalies,
        "verdict": {
            "safe_to_flip": not blockers,
            "blocker_count": len(blockers),
            "reason": (blockers[0]["text"] if blockers
                       else "No blockers found in the shadow record."),
        },
        "named_cases": named_case_report(threads),
    }


def check_blocklist(bundle, threads):
    live = bundle["blocked"] or []
    rows = []
    for addr, note in EXPECTED_BLOCKED:
        present = any(addr.lower() == (b or "").lower() for b in live)
        exposed = []
        if not present:
            for t in threads.values():
                if not addr_matches(addr, t.get("address") or ""):
                    continue
                stages = [e["template"] or e["stage_key"] for e in t["events"]]
                if any(s == "leased" for s in stages):
                    continue
                # Only count stages the RENTER would have received. A thread
                # whose shadow record is escalation texts to Alex sent the
                # renter nothing wrong.
                renter_facing = [e["template"] or e["stage_key"] for e in t["events"]
                                 if not is_poke_stage(e["stage_key"])]
                exposed.append({"thread_id": t["thread_id"],
                                "renter": t.get("renter_name"),
                                "stages": stages,
                                "renter_facing": renter_facing})
        rows.append({"address": addr, "note": note, "in_firestore": present,
                     "exposed_threads": exposed})
    return {"live_list": live, "expected": rows,
            "missing": [r["address"] for r in rows if not r["in_firestore"]],
            "config_doc_exists": bundle["config_exists"]}


def named_case_report(threads):
    """Word-boundary match, and report EVERY hit - 'anna' as a substring
    silently matched Deanna and hid the real Anna."""
    out = []
    for name in NAMED_CASES:
        pat = re.compile(r"\b%s\b" % re.escape(name), re.IGNORECASE)
        hits = [t for t in threads.values() if pat.search(t.get("renter_name") or "")]
        for hit in hits:
            out.append({"name": name, "found": True,
                        "renter_name": hit.get("renter_name"),
                        "thread_id": hit["thread_id"],
                        "address": hit.get("address"),
                        "stages": [e["template"] or e["stage_key"]
                                   for e in hit["events"]]})
        if not hits:
            out.append({"name": name, "found": False, "renter_name": None,
                        "thread_id": None, "address": None, "stages": []})
    return out


def A(sev, code, text, thread_id=None, doc_id=None):
    return {"severity": sev, "code": code, "text": text,
            "thread_id": thread_id, "doc_id": doc_id}


def find_anomalies(events, threads, bundle, blocklist):
    out = []

    for addr in blocklist["missing"]:
        out.append(A("BLOCKER", "blocklist-missing",
                     "%s is leased but is NOT in the Firestore blocklist. The "
                     "responder does not know it is off-market." % addr))
    for row in blocklist["expected"]:
        for ex in row["exposed_threads"]:
            rf = ex.get("renter_facing") or []
            if rf:
                out.append(A("BLOCKER", "leased-offered",
                             "%s asked about %s (leased) and would have been "
                             "emailed '%s' instead of the rented notice."
                             % (ex["renter"] or "a renter", row["address"],
                                ", ".join(rf)),
                             ex["thread_id"]))
            else:
                out.append(A("WARN", "leased-escalated",
                             "%s asked about %s (leased). No wrong email would "
                             "have gone out - the thread only produced an "
                             "escalation text to Alex - but the responder still "
                             "does not know the house is off-market."
                             % (ex["renter"] or "a renter", row["address"]),
                             ex["thread_id"]))

    for ev in events:
        if ev["already_live"]:
            out.append(A("BLOCKER", "already-live",
                         "Stage %s was already sent for real. Flipping DRY_RUN "
                         "risks a duplicate." % ev["stage_key"],
                         ev["thread_id"], ev["doc_id"]))
        for issue in ev["body_issues"]:
            out.append(A("BLOCKER", "unrendered-copy",
                         "Email to %s has broken copy: %s"
                         % (ev["renter_name"] or "renter", issue),
                         ev["thread_id"], ev["doc_id"]))
        cal = ev["calendar"] or {}
        if cal.get("create") and cal.get("start"):
            try:
                start = datetime.fromisoformat(cal["start"])
                if not in_window(start):
                    out.append(A("BLOCKER", "booking-out-of-window",
                                 "Would book %s - outside the showing window "
                                 "for that day." % human(start),
                                 ev["thread_id"], ev["doc_id"]))
                if ev["_created"] and start < phx(ev["_created"]) + timedelta(
                        hours=MIN_NOTICE_HOURS):
                    out.append(A("BLOCKER", "booking-short-notice",
                                 "Would book %s - under the %dh minimum notice."
                                 % (human(start), MIN_NOTICE_HOURS),
                                 ev["thread_id"], ev["doc_id"]))
            except (ValueError, TypeError):
                out.append(A("WARN", "booking-bad-start",
                             "Booking start time is unparseable: %r"
                             % cal.get("start"), ev["thread_id"], ev["doc_id"]))
            agent = cal.get("agent")
            if agent and agent not in KNOWN_AGENTS:
                out.append(A("WARN", "unknown-agent",
                             "Booking assigned to '%s', who is not on the "
                             "showing roster." % agent,
                             ev["thread_id"], ev["doc_id"]))
        if ev["trigger"].get("fallback"):
            out.append(A("WARN", "llm-fallback",
                         "The AI classifier failed here and a regex guessed the "
                         "intent - the scheduling decision is low-confidence.",
                         ev["thread_id"], ev["doc_id"]))

    # duplicate replies per thread, split by the double-send fix date
    for t in threads.values():
        replies = [e for e in t["events"] if e["kind"] == "reply"]
        if len(replies) < 2:
            continue
        post = [e for e in replies if e["_created"] and e["_created"] > DOUBLE_SEND_FIXED]
        sev, code = ("BLOCKER", "double-send-new") if len(post) >= 2 else \
                    ("INFO", "double-send-historical")
        out.append(A(sev, code,
                     "%s has %d separate would-be replies%s."
                     % (t.get("renter_name") or t["thread_id"], len(replies),
                        "" if len(post) >= 2 else
                        " (before the Aug 1 double-send fix - already resolved)"),
                     t["thread_id"]))
        seen = {}
        for e in replies:
            if e["body_sha256"] and e["body_sha256"] in seen:
                out.append(A("WARN", "identical-body",
                             "The exact same email appears twice for %s."
                             % (t.get("renter_name") or t["thread_id"]),
                             t["thread_id"], e["doc_id"]))
            seen[e["body_sha256"]] = True

    for mid, msg in bundle["msgs"].items():
        oc = msg.get("outcome") or ""
        if oc.startswith("error"):
            out.append(A("WARN", "processing-error",
                         "A renter message errored out (%s): %r"
                         % (oc, (msg.get("renter_text") or "")[:80]),
                         msg.get("thread_id")))
        elif oc == "claimed":
            out.append(A("WARN", "died-mid-request",
                         "A renter message was claimed but never finished "
                         "processing - that renter got nothing.",
                         msg.get("thread_id")))

    for ev in events:
        if ev["_created"] and ev["_created"] < MIRROR_SHIPPED:
            out.append(A("INFO", "pre-mirror",
                         "Shadow doc dated before the mirror shipped - "
                         "unexpected.", ev["thread_id"], ev["doc_id"]))

    rank = {"BLOCKER": 0, "WARN": 1, "INFO": 2}
    out.sort(key=lambda a: rank[a["severity"]])
    return out


# ---- rendering ----------------------------------------------------------

def render_text(m):
    c = m["meta"]["counts"]
    v = m["verdict"]
    L = []
    L.append("")
    L.append("  %s" % ("SAFE TO FLIP" if v["safe_to_flip"]
                       else "DO NOT FLIP - %d blocker(s)" % v["blocker_count"]))
    L.append("  %s" % v["reason"])
    L.append("")
    L.append("  shadow docs %d across %d threads | messages %d | live sends %d"
             % (c["shadow_docs"], c["threads_with_shadow"],
                c["zillow_messages"], c["zillow_sends"]))
    bl = m["blocklist"]
    L.append("  blocklist in Firestore: %d/%d expected addresses%s"
             % (len(bl["expected"]) - len(bl["missing"]), len(bl["expected"]),
                (" | MISSING: " + ", ".join(bl["missing"])) if bl["missing"] else ""))
    for nc in m["named_cases"]:
        L.append("  %-18s %s" % ((nc["renter_name"] or nc["name"]) + ":",
                                 ", ".join(nc["stages"]) if nc["found"]
                                 else "NO SHADOW RECORD"))
    L.append("")
    return "\n".join(L)


CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
 max-width:900px;margin:0 auto;padding:24px 18px 80px;color:#1a1a1a;background:#fff}
h1{font-size:26px;margin:0 0 4px} h2{font-size:19px;margin:38px 0 12px;
 padding-bottom:6px;border-bottom:2px solid #eee}
h3{font-size:15px;margin:22px 0 8px}
.sub{color:#666;font-size:13px;margin-bottom:24px}
.verdict{padding:18px 20px;border-radius:10px;margin:18px 0;font-size:17px;font-weight:600}
.ok{background:#e7f6ec;border:1px solid #34a853;color:#0b6b2f}
.bad{background:#fdecea;border:1px solid #d93025;color:#a50e0e}
.verdict .why{font-weight:400;font-size:14px;margin-top:6px}
table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid #eee;vertical-align:top}
th{background:#fafafa;font-weight:600}
.card{border:1px solid #e2e2e2;border-radius:10px;padding:16px;margin:16px 0;background:#fff}
.card h3{margin-top:0}
.chip{display:inline-block;font-size:11px;padding:2px 8px;border-radius:20px;
 background:#eef1f5;color:#333;margin:0 5px 5px 0;font-weight:500}
.chip.warn{background:#fff3cd;color:#7a5b00}
.chip.bad{background:#fdecea;color:#a50e0e}
.chip.good{background:#e7f6ec;color:#0b6b2f}
.renter{background:#f6f7f9;border-left:3px solid #9aa4b2;padding:10px 14px;
 margin:10px 0;border-radius:0 6px 6px 0;font-size:14px}
.renter .who{font-size:12px;color:#666;margin-bottom:4px}
.email{border:1px solid #d6dae0;border-radius:8px;background:#fcfcfd;margin:10px 0}
.email .hdr{background:#f2f4f7;padding:7px 12px;font-size:12px;color:#555;
 border-bottom:1px solid #d6dae0;border-radius:8px 8px 0 0;display:flex;
 justify-content:space-between;align-items:center}
.email pre{margin:0;padding:14px;white-space:pre-wrap;word-wrap:break-word;
 font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
button{font:12px inherit;padding:3px 10px;border:1px solid #c3c8d0;background:#fff;
 border-radius:5px;cursor:pointer}
button:hover{background:#f0f2f5}
.sev{font-weight:600;font-size:11px;padding:2px 7px;border-radius:4px;margin-right:8px}
.sev.BLOCKER{background:#d93025;color:#fff} .sev.WARN{background:#f9ab00;color:#000}
.sev.INFO{background:#dadce0;color:#333}
.mono{font:12px ui-monospace,Menlo,monospace;color:#888}
.caveat{background:#f8f9fa;border-left:3px solid #c3c8d0;padding:12px 16px;
 font-size:13px;border-radius:0 6px 6px 0}
.miss{color:#a50e0e;font-weight:600}
@media(prefers-color-scheme:dark){
 body{background:#16181c;color:#e6e6e6} h2{border-color:#2c2f36}
 .card,.email{background:#1d2025;border-color:#31343b}
 .email .hdr{background:#24272d;border-color:#31343b;color:#aaa}
 .renter{background:#22252b;border-left-color:#555} th{background:#22252b}
 th,td{border-color:#2c2f36} .chip{background:#2a2e35;color:#ddd}
 .caveat{background:#1d2025} button{background:#24272d;color:#ddd;border-color:#3a3e45}
 .mono{color:#777}}
"""

JS = """
document.addEventListener('click',function(e){
 var b=e.target.closest('button[data-copy]'); if(!b)return;
 var pre=document.getElementById(b.getAttribute('data-copy'));
 navigator.clipboard.writeText(pre.textContent).then(function(){
  var o=b.textContent; b.textContent='copied'; setTimeout(function(){b.textContent=o},1200);
 });
});
"""


def E(s):
    return html.escape(str(s if s is not None else ""))


def email_block(body, idx, label=""):
    if not body:
        return ""
    return ("<div class='email'><div class='hdr'><span>%s</span>"
            "<button data-copy='b%d'>copy</button></div>"
            "<pre id='b%d'>%s</pre></div>" % (E(label), idx, idx, E(body)))


def render_html(m, args):
    v, bl, c = m["verdict"], m["blocklist"], m["meta"]["counts"]
    P = []
    P.append("<!doctype html><html><head><meta charset='utf-8'>"
             "<meta name='viewport' content='width=device-width,initial-scale=1'>"
             "<title>Zillow shadow review</title><style>%s</style></head><body>" % CSS)
    P.append("<h1>What the responder would have sent</h1>")
    P.append("<div class='sub'>Everything below is from DRY_RUN shadow mode. "
             "No renter received any of it. Generated %s &middot; window: %s</div>"
             % (E(human(datetime.now(timezone.utc))), E(m["meta"]["window"])))

    P.append("<div class='verdict %s'>%s<div class='why'>%s</div></div>"
             % ("ok" if v["safe_to_flip"] else "bad",
                "SAFE TO FLIP" if v["safe_to_flip"]
                else "DO NOT FLIP &mdash; %d blocker%s" % (
                    v["blocker_count"], "" if v["blocker_count"] == 1 else "s"),
                E(v["reason"])))

    P.append("<h2>1. Leased-house check</h2>")
    P.append("<p>These four homes are leased. The responder only knows that if "
             "the address is in the Firestore blocklist.</p>")
    P.append("<table><tr><th>Address</th><th>Status</th><th>In Firestore?</th></tr>")
    for r in bl["expected"]:
        P.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                 % (E(r["address"]), E(r["note"]),
                    "yes" if r["in_firestore"]
                    else "<span class='miss'>NO &mdash; exposed</span>"))
    P.append("</table>")
    if bl["missing"]:
        P.append("<p class='miss'>%d leased address(es) missing from the "
                 "blocklist. Flipping DRY_RUN would offer showings there.</p>"
                 % len(bl["missing"]))
    for r in bl["expected"]:
        for ex in r["exposed_threads"]:
            P.append("<div class='card'><h3>%s &mdash; %s</h3>"
                     "<p>Would have been sent <b>%s</b> instead of the rented "
                     "notice.</p></div>"
                     % (E(ex["renter"] or "renter"), E(r["address"]),
                        E(", ".join(ex["stages"]) or "?")))

    P.append("<h2>2. Last night's three inquiries</h2>")
    for nc in m["named_cases"]:
        if nc["found"]:
            P.append("<p><b>%s</b> &middot; %s &middot; %s</p>"
                     % (E(nc["renter_name"] or nc["name"]), E(nc["address"] or "?"),
                        E(", ".join(nc["stages"]))))
        else:
            P.append("<p><b>%s</b> &mdash; <span class='miss'>no shadow record. "
                     "The responder never processed this inquiry.</span></p>"
                     % E(nc["name"]))

    P.append("<h2>3. Every reply, by thread</h2>")
    idx = 0
    for t in sorted(m["threads"], key=lambda x: (x.get("renter_name") or "zz")):
        P.append("<div class='card'><h3>%s &middot; %s</h3>"
                 % (E(t.get("renter_name") or "unknown renter"),
                    E(t.get("address") or "unknown address")))
        P.append("<div><span class='chip'>state: %s</span>"
                 "<span class='chip mono'>%s</span></div>"
                 % (E(t.get("state") or "?"), E(t["thread_id"])))
        for ev in t["events"]:
            tr = ev["trigger"]
            if tr.get("renter_text"):
                P.append("<div class='renter'><div class='who'>Renter wrote%s:"
                         "</div>%s</div>"
                         % (" (matched by timing)" if tr["confidence"] == "inferred"
                            else "", E(tr["renter_text"])))
            chips = []
            if ev["template"]:
                chips.append("<span class='chip'>%s</span>" % E(ev["template"]))
            if tr.get("intent"):
                chips.append("<span class='chip'>intent: %s</span>" % E(tr["intent"]))
            elif ev["kind"] == "reply" and ev["stage_key"] == "first_reply":
                chips.append("<span class='chip'>first inquiry &mdash; not classified</span>")
            if tr.get("fallback"):
                chips.append("<span class='chip warn'>AI classifier failed &mdash; regex guess</span>")
            for bi in ev["body_issues"]:
                chips.append("<span class='chip bad'>%s</span>" % E(bi))
            if ev["already_live"]:
                chips.append("<span class='chip bad'>already sent for real</span>")
            P.append("<div>%s</div>" % "".join(chips))

            cal = ev["calendar"] or {}
            if cal:
                if cal.get("create"):
                    try:
                        st = datetime.fromisoformat(cal["start"])
                        ok = in_window(st)
                        P.append("<div><span class='chip %s'>would book %s</span>"
                                 "<span class='chip'>%s</span>%s%s</div>"
                                 % ("good" if ok else "bad", E(human(st)),
                                    E(cal.get("agent") or "?"),
                                    "<span class='chip'>same-day</span>" if cal.get("same_day") else "",
                                    "<span class='chip'>Jace cover</span>" if cal.get("jace_cover") else ""))
                    except (ValueError, TypeError):
                        P.append("<div><span class='chip bad'>unparseable booking time</span></div>")
                elif cal.get("fold"):
                    P.append("<div><span class='chip good'>would fold %s into the "
                             "existing showing</span></div>" % E(cal.get("renter") or ""))
                elif cal.get("cancel"):
                    P.append("<div><span class='chip'>would cancel the showing</span></div>")

            if ev["kind"] == "poke":
                P.append("<div class='renter'><div class='who'>Escalation text to "
                         "Alex (%s):</div>%s</div>"
                         % (E(ev["created_human"]), E(ev["poke"])))
            else:
                idx += 1
                P.append(email_block(ev["body"], idx,
                                     "We would have replied &middot; %s"
                                     % ev["created_human"]))
                if ev.get("poke"):
                    P.append("<div class='renter'><div class='who'>Plus an "
                             "escalation text:</div>%s</div>" % E(ev["poke"]))
        P.append("</div>")

    pokes = [e for e in m["events"] if e.get("poke")]
    P.append("<h2>4. Escalation texts</h2>")
    P.append("<p>%d would have been sent. <b>None have ever actually fired</b> "
             "&mdash; the Poke path has never run in production, so verify "
             "POKE_ENDPOINT is set in the Render dashboard before relying on "
             "it.</p>" % len(pokes))
    for e in pokes:
        P.append("<div class='renter'><div class='who'>%s &middot; %s</div>%s</div>"
                 % (E(e["renter_name"] or "?"), E(e["created_human"]), E(e["poke"])))

    books = [e for e in m["events"] if (e["calendar"] or {}).get("create")
             or (e["calendar"] or {}).get("fold")]
    P.append("<h2>5. Bookings</h2>")
    if not books:
        P.append("<p>No bookings in the shadow record.</p>")
    else:
        P.append("<table><tr><th>Renter</th><th>Address</th><th>When</th>"
                 "<th>Agent</th><th>Type</th><th>Valid?</th></tr>")
        for e in books:
            cal = e["calendar"]
            when, ok = "(fold into existing)", "&mdash;"
            if cal.get("create"):
                try:
                    st = datetime.fromisoformat(cal["start"])
                    when = human(st)
                    ok = "in window" if in_window(st) else "<span class='miss'>OUT OF WINDOW</span>"
                except (ValueError, TypeError):
                    when, ok = E(cal.get("start")), "<span class='miss'>unparseable</span>"
            P.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                     "<td>%s</td></tr>"
                     % (E(e["renter_name"]), E(e["address"]), E(when),
                        E(cal.get("agent") or "&mdash;"),
                        "new" if cal.get("create") else "fold", ok))
        P.append("</table>")

    P.append("<h2>6. Anomalies</h2>")
    if not m["anomalies"]:
        P.append("<p>None found.</p>")
    for a in m["anomalies"]:
        P.append("<p><span class='sev %s'>%s</span>%s</p>"
                 % (a["severity"], a["severity"], E(a["text"])))

    P.append("<h2>7. Method &amp; caveats</h2><div class='caveat'>")
    P.append("<p>This shows the <b>latest</b> would-be email per thread and "
             "stage. The mirror keeps one record per thread+stage and overwrites "
             "it, so if a stage ran twice you are seeing the last one. This is "
             "not a complete event log.</p>")
    P.append("<p>Times are Phoenix. Firestore stores UTC.</p>")
    P.append("<p>Renter messages are cut to the first 200 characters &mdash; "
             "that is the limit of what was recorded, not of this report.</p>")
    P.append("<p>Nothing before Wed Jul 29, 7:44 PM was mirrored; that is when "
             "the mirror shipped. The double-send bug was fixed Sat Aug 1, "
             "2:00 PM &mdash; duplicate pairs before that are already-fixed "
             "history and are labelled INFO.</p>")
    P.append("<p>Counts: %d shadow docs, %d threads, %d messages, %d live sends."
             "</p>" % (c["shadow_docs"], c["threads_with_shadow"],
                       c["zillow_messages"], c["zillow_sends"]))
    P.append("</div>")
    P.append("<script>%s</script></body></html>" % JS)
    return "\n".join(P)


# ---- main ---------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--count-only", action="store_true",
                   help="counts + blocklist only, no email bodies. Run this first.")
    p.add_argument("--since-hours", type=float)
    p.add_argument("--since", help="ISO start, e.g. 2026-08-03T00:00:00Z")
    p.add_argument("--thread", action="append", default=[])
    p.add_argument("--address")
    p.add_argument("--limit", type=int, default=2000)
    p.add_argument("--out-dir", default=DEFAULT_OUT)
    p.add_argument("--json", nargs="?", const=True, default=None)
    p.add_argument("--html", nargs="?", const=True, default=None)
    p.add_argument("--open", action="store_true")
    p.add_argument("--full-emails", action="store_true")
    p.add_argument("--anonymize", action="store_true")
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    args._since = None
    if args.since_hours:
        args._since = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
    elif args.since:
        args._since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))

    db = connect(args.timeout)
    bundle = fetch_all(db, args)

    if args.thread:
        keep = set(args.thread)
        bundle["shadow"] = [(i, d) for i, d in bundle["shadow"]
                            if parse_id(i)[0] in keep]
    if args.address:
        sub = args.address.lower()
        keep = set(tid for tid, t in bundle["threads"].items()
                   if sub in (t.get("property_address") or "").lower())
        bundle["shadow"] = [(i, d) for i, d in bundle["shadow"]
                            if parse_id(i)[0] in keep]
    if len(bundle["shadow"]) > args.limit:
        print("note: truncating to --limit %d of %d shadow docs"
              % (args.limit, len(bundle["shadow"])))
        bundle["shadow"] = bundle["shadow"][:args.limit]

    if args.count_only:
        print("\n  zillow_shadow    %d" % len(bundle["shadow"]))
        print("  zillow_messages  %d" % len(bundle["msgs"]))
        print("  zillow_threads   %d" % len(bundle["threads"]))
        print("  zillow_sends     %d" % len(bundle["sent_ids"]))
        print("\n  blocklist in Firestore (%d):" % len(bundle["blocked"]))
        for b in bundle["blocked"]:
            print("    - %s" % b)
        missing = [a for a, _ in EXPECTED_BLOCKED
                   if not any(a.lower() == (x or "").lower()
                              for x in bundle["blocked"])]
        if missing:
            print("\n  MISSING (leased but not blocked):")
            for a in missing:
                print("    ! %s" % a)
        print("")
        return 5 if missing else 0

    model = build_model(bundle, args)
    for ev in model["events"]:
        ev.pop("_created", None)

    print(render_text(model))

    if args.json or args.html or (args.json is None and args.html is None):
        os.makedirs(args.out_dir, exist_ok=True)
        stamp = datetime.now(PHX).strftime("%Y%m%d-%H%M")
        jpath = args.json if isinstance(args.json, str) else \
            os.path.join(args.out_dir, "shadow-%s.json" % stamp)
        hpath = args.html if isinstance(args.html, str) else \
            os.path.join(args.out_dir, "shadow-%s.html" % stamp)
        want_json = args.json is not None or args.html is None
        want_html = args.html is not None or args.json is None
        if want_json:
            with open(jpath, "w") as f:
                json.dump(model, f, indent=2, default=str)
            print("  JSON  %s" % jpath)
        if want_html:
            with open(hpath, "w") as f:
                f.write(render_html(model, args))
            print("  HTML  %s" % hpath)
            if args.open:
                subprocess.run(["open", hpath], check=False)
        print("")
    return 0 if model["verdict"]["safe_to_flip"] else 5


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        if "--debug" in sys.argv:
            raise
        sys.exit("ERROR: %s: %s\n(re-run with --debug for the traceback)"
                 % (type(e).__name__, e))
