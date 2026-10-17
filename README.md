# Zillow Instant Responder

Sub-30-second auto-reply to new Zillow rental inquiries. Since the v2 rebuild
(2026-07-28) this service is the SINGLE WRITER: it owns every renter-facing send,
including question handling, time parsing, booking and confirmations. The old
5-minute sweep no longer sends. It is a non-sending watchdog that POSTs `/reprocess`
when it finds drift.

## How it works

```
New Zillow email lands in alex@azfoleyhomes.com
   -> Composio Gmail trigger fires a webhook POST to this service (path /)
   -> secret -> extract -> self-send skip -> claim_message (dup-delivery gate)
   -> Composio full-thread fetch -> claim_content_hash (Zillow dup-copy gate)
   -> alex_replied_after gate -> route:
        new inquiry  : leased reply, or consolidate-first, or Template 1
        known thread : classify (Haiku extracts, code decides) -> fold / book /
                       counter / answer / apply-first / as-is / NEEDS_HUMAN
   -> every send must first win a Firestore create-once lock (ledger.reserve_send)
   -> labels the thread zillow/awaiting-renter
Renter has a reply in a few seconds, 24/7, independent of Alex's Mac.
```

Haiku is used for narrow extract/classify micro-calls only (`llm.py`). The model
extracts, code decides, so token cost stays tiny, but `ANTHROPIC_API_KEY` is required.
Dependencies are pinned in `requirements.txt` and installed by Render's build command
(`pip install -r requirements.txt`), so there is a build step.

Other HTTP routes on the same service:

| Route | Auth | What it does |
|---|---|---|
| `GET /` | none | health check, returns `zillow-pipeline ok (dry_run=...)` |
| `POST /` | `X-Webhook-Secret: <WEBHOOK_SECRET>` | the Composio Gmail trigger |
| `POST /reprocess` | `X-Webhook-Secret: <REPROCESS_SECRET>` | watchdog drift re-run |
| `POST /send-approved` | `X-Webhook-Secret: <REPROCESS_SECRET>` | send an answer Alex approved (`thread_id` + `answer`) |
| `POST /hooks/website-lead/<token>` | token in the URL | Netlify owner-lead form ping (see `website_lead.py`) |

## What's already done (by Claude)

- `responder.py` - webhook receiver, routing and reply logic (idempotent, secret-verified)
- `gmail_client.py` - Composio Gmail/Calendar wrapper, full-thread fetch, `extract_event()`
- `calendar_logic.py` - calendar operations and the deterministic slot engine
- `ledger.py` - the Firestore ledger: create-once send locks and thread state
- `llm.py` - Haiku classify/extract micro-calls with regex and NEEDS_HUMAN fallbacks
- `rules.py` - pure scheduling and agent-selection rules, zero I/O
- `facts.py` - standing-rule question router and property fact sheets
- `templates.py` - every renter-facing string in one place
- `cron.py` - in-process ticker: label retries, stuck-send recovery, nudges, reminders
- `website_lead.py` - the Netlify owner-lead webhook
- `scripts/` - `application.py` (lease attribution), `sync_facts.py`, `shadow_dump.py`
- `render.yaml` - one-file deploy config for Render
- `tests/` - the pytest suite (`python -m pytest -q`)
- This runbook

## What only YOU can do (firm reasons)

These three steps require creating accounts / entering credentials / approving access,
which Claude is not permitted to do on your behalf. Each is a few minutes.

### Step 1 — Rotate your Composio key (security, do this first)

The old key (`ck_7SYX8U2k...`) was pasted into a chat transcript, so treat it as exposed.
1. Go to app.composio.dev -> Settings -> API Keys.
2. Revoke the old key, generate a new one. Copy it (you'll paste it in Step 3).

### Step 2 — Create the Composio Gmail trigger

1. In the Composio dashboard, open the Gmail app -> Triggers.
2. Enable the "New Gmail Message" trigger (a.k.a. GMAIL_NEW_GMAIL_MESSAGE) for the
   connected account `gmail_boast-punnet` (alex@azfoleyhomes.com).
3. Filter it to inbound Zillow only if the UI allows a sender/query filter:
   `from:convo.zillow.com`. (If it can't pre-filter, that's fine — the service ignores
   non-inquiry subjects, so worst case it gets a few extra harmless POSTs.)
4. Set the trigger's webhook/callback URL to your deployed service URL from Step 3,
   path `/`, and add the shared secret header `X-Webhook-Secret: <WEBHOOK_SECRET>`
   if the trigger UI supports custom headers. (If not, we rely on the secret in the URL.)

### Step 3 — Deploy the service (Render, ~$7/mo always-on)

1. Create a free account at render.com.
2. New -> Web Service -> connect this folder's git repo (or "Deploy from a public Git repo"
   pointing at wherever you push this folder).
3. Render auto-detects `render.yaml`. Pick the **Starter** plan (NOT Free — Free sleeps
   and kills instant response).
4. In the service's Environment tab, set:
   - `COMPOSIO_API_KEY` = your new key from Step 1
   - `COMPOSIO_CONNECTED_ACCOUNT_ID` = `gmail_boast-punnet`
   - `COMPOSIO_USER_ID`
   - `AWAITING_RENTER_LABEL_ID` = `Label_1717014254813700027`  (zillow/awaiting-renter)
   - `HANDLED_LABEL_ID` = `Label_6932202305849666189`  (zillow/handled)
   - `NEEDS_REPLY_LABEL_ID`
   - `WEBHOOK_SECRET` = make up a long random string; use the same value in Step 2's header
   - `REPROCESS_SECRET` = a second random string for `/reprocess` and `/send-approved`,
     so it can rotate independently of the Composio webhook secret. Falls back to
     `WEBHOOK_SECRET` when unset.
   - `ANTHROPIC_API_KEY` for the Haiku micro-calls
   - `POKE_ENDPOINT` and `POKE_API_KEY` for the sanctioned pings
   - `DRY_RUN` = `"true"` until cutover, then `"false"` to go live
   - `GOOGLE_APPLICATION_CREDENTIALS` = `/etc/secrets/firebase-sa.json`, with the
     firebase service-account JSON uploaded as a Render **Secret File** named
     `firebase-sa.json`. Never commit credentials.
5. Deploy. Copy the live URL (e.g. `https://zillow-instant-responder.onrender.com`) and
   paste it into the Composio trigger from Step 2.

### Step 4 — Test

Hit the URL in a browser. You should see `zillow-pipeline ok (dry_run=true)` (health
check). Then send a test Zillow-style inquiry (or wait for a real one) and confirm a
reply goes out in seconds and the thread gets the `zillow/awaiting-renter` label.

Nothing reaches a renter while `DRY_RUN` is `"true"`: the service mirrors every
would-be action into the Firestore `zillow_shadow` collection instead, and
`scripts/shadow_dump.py` prints what it would have sent.

## Notes / honesty

- The exact Composio v3 execute endpoint path and trigger payload shape can drift with
  their API version. `responder.py` is defensive about payload shape, but if the first
  real trigger logs a parse miss, send Claude the logged payload and it'll adjust
  `extract_event()` (in `gmail_client.py`) in one edit.
- The 5-minute sweep has already been demoted to a non-sending watchdog, so its
  interval is now only a drift-detection question, not a renter-response one.
- If you ever want to avoid the monthly host cost, the alternative is running this on an
  always-on office machine behind a Cloudflare Tunnel — more setup, Mac/office-dependent,
  but $0/mo. Ask Claude if you want that path instead.

## Who took the application (added 2026-09-17)

The `agent` field on a `zillow_threads` doc only records who opened the door for the showing.
Alex: "i rented all those houses, not jace". So a lease is attributed by five fields a person
writes, never the responder: `application_by`, `application_at`, `application_note`,
`application_logged_at`, `lease_signed_at`.

```bash
cd zillow-instant-responder
# log one (exact match on street number + street words and the renter's first name)
python3 scripts/application.py log --property "26009 S New Town Dr" --renter Bonnie --by alex --leased 2026-08-27
# two threads match? the tool lists them; pick one
python3 scripts/application.py log --thread <thread_id> --by jace --date 2026-09-20
# see what is logged, or everything on one property
python3 scripts/application.py show
python3 scripts/application.py show --property "2118 S El Marino"
# per-agent: showings (door opened) vs applications taken vs leases signed
python3 scripts/application.py scoreboard
```

`--by` accepts a first name (alex, jace, rhett, cody, bre, alexa) or a full name. Dates are
YYYY-MM-DD and stay empty when unknown; nothing is guessed. Credentials: the service-account
file at `~/.config/boundless/firebase-sa.json` is used when `GOOGLE_APPLICATION_CREDENTIALS`
is unset. Run it with the BoundlessPipeline `zillow-venv` python.
