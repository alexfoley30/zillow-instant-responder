# Zillow Instant Responder

Sub-30-second auto-reply to new Zillow rental inquiries. Since the 2026-07-28 v2
rebuild this service is the SINGLE WRITER: it owns every renter-facing send,
booking and calendar work included. The Claude sweep is a non-sending watchdog
that POSTs `/reprocess` when it spots drift.

## How it works

```
New Zillow email lands in alex@azfoleyhomes.com
   -> Composio Gmail trigger fires a webhook POST to this service
   -> service parses inquirer name + property address from the subject
   -> sends Template 1 availability-ask in-thread (via Composio)
   -> labels the thread zillow/awaiting-renter
Renter has a reply in a few seconds, 24/7, independent of Alex's Mac.
```

The hot path (first reply) is still plain code, no model call, so a new inquiry
costs nothing to answer. Classifying and extracting from renter replies does use
Haiku micro-calls (`llm.py`), so `ANTHROPIC_API_KEY` is required.

The service is no longer stdlib-only: `requirements.txt` pins firebase-admin,
google-cloud-firestore, google-api-core and anthropic, and Render's build step is
`pip install -r requirements.txt`. The pins are load-bearing, read the comments in
`requirements.txt` before changing any of them.

## What's already done (by Claude)

- `responder.py` - webhook receiver, routing and reply logic (idempotent, secret-verified)
- `ledger.py` - Firestore ledger, the create-once send locks and thread state
- `gmail_client.py` - Composio/Gmail calls, including `extract_event()`
- `calendar_logic.py`, `rules.py`, `templates.py`, `facts.py` - booking, policy, copy
- `llm.py` - the Haiku classify/extract micro-calls
- `cron.py` - nudges, reminders and the stuck-send sweep
- `website_lead.py` - the Netlify owner-lead hook (`/hooks/website-lead/<token>`)
- `render.yaml` - one-file deploy config for Render
- `tests/` - the regression suite (`python -m pytest -q`)
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
4. In the service's Environment tab, set every key `render.yaml` marks `sync: false`:
   - `COMPOSIO_API_KEY` = your new key from Step 1
   - `COMPOSIO_CONNECTED_ACCOUNT_ID` = `gmail_boast-punnet`
   - `COMPOSIO_USER_ID`
   - `AWAITING_RENTER_LABEL_ID` = `Label_1717014254813700027`  (zillow/awaiting-renter)
   - `HANDLED_LABEL_ID` = `Label_6932202305849666189`  (zillow/handled)
   - `NEEDS_REPLY_LABEL_ID`
   - `WEBHOOK_SECRET` = make up a long random string; use the same value in Step 2's header
   - `ANTHROPIC_API_KEY` = for the Haiku classify/extract calls
   - `POKE_ENDPOINT` = where needs-you pings go
   - `DRY_RUN` = `true` until cutover (`render.yaml` ships it as `true`)
   - the firebase service-account JSON goes in as a Render SECRET FILE named
     `firebase-sa.json`; `GOOGLE_APPLICATION_CREDENTIALS` already points at it
5. Deploy. Copy the live URL (e.g. `https://zillow-instant-responder.onrender.com`) and
   paste it into the Composio trigger from Step 2.

### Step 4 — Test

Hit the URL in a browser (`GET /`, the Render health check path) and you should see
`zillow-pipeline ok (dry_run=true)`, or `dry_run=false` once you have cut over.
Then send a test Zillow-style inquiry (or wait for a real one) and confirm a reply goes
out in seconds and the thread gets the `zillow/awaiting-renter` label.

## Notes / honesty

- The exact Composio v3 execute endpoint path and trigger payload shape can drift with
  their API version. `responder.py` is defensive about payload shape, but if the first
  real trigger logs a parse miss, send Claude the logged payload and it'll adjust
  `extract_event()` in `gmail_client.py` in one edit.
- The 5-minute sweep no longer sends anything; it only POSTs `/reprocess` when it finds
  drift, so it can be slowed to ~10-15 min to cut cost further. Tell Claude when ready.
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
