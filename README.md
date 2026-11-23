# Zillow Instant Responder

Sub-30-second auto-reply to new Zillow rental inquiries. Booking, calendar and the
maintenance loops now run in this service too (`calendar_logic.py`, `cron.py`); the
external sweep is only a coarse backstop.

## How it works

```
New Zillow email lands in alex@azfoleyhomes.com
   -> Composio Gmail trigger fires a webhook POST to this service
   -> service parses inquirer name + property address from the subject
   -> sends Template 1 availability-ask in-thread (via Composio)
   -> labels the thread zillow/awaiting-renter
Renter has a reply in a few seconds, 24/7, independent of Alex's Mac.
```

The first availability-ask is template-driven, so the common path costs no tokens.
An LLM is used for the narrower jobs — reply classification and the pre-send review
in `llm.py` — so `ANTHROPIC_API_KEY` is required and `pip install -r requirements.txt`
is a real build step. The pins in `requirements.txt` are load-bearing; read the
comments there before bumping one.

## What's already done (by Claude)

- `responder.py` — webhook receiver + routing (idempotent, secret-verified)
- `gmail_client.py` — Gmail/Composio calls, including `extract_event()` payload parsing
- `templates.py` — the reply templates; `rules.py` — roster and scheduling constants
- `calendar_logic.py` — slot validation and agent assignment
- `ledger.py` — Firestore `zillow_threads` state and the send lock
- `llm.py` — reply classification and the pre-send review
- `cron.py` — in-process 20-minute ticker: label retries, stuck-send recovery,
  48h nudge, 7-day close, showing reminders
- `website_lead.py` — the Netlify form intake at `/hooks/website-lead/<token>`
- `render.yaml` — one-file deploy config for Render
- `scripts/` — `application.py` (lease/application logging), `sync_facts.py`,
  `shadow_dump.py`
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
   - `AWAITING_RENTER_LABEL_ID` = `Label_1717014254813700027`  (zillow/awaiting-renter)
   - `HANDLED_LABEL_ID` = `Label_6932202305849666189`  (zillow/handled)
   - `WEBHOOK_SECRET` = make up a long random string; use the same value in Step 2's header
5. Deploy. Copy the live URL (e.g. `https://zillow-instant-responder.onrender.com`) and
   paste it into the Composio trigger from Step 2.

### Step 4 — Test

Hit the URL in a browser — you should see `zillow-pipeline ok (dry_run=True)` or
`(dry_run=False)` (health check, `GET /`).

`DRY_RUN` defaults to **true**, so a fresh deploy parses and logs but sends nothing.
Set `DRY_RUN=false` in the Environment tab once the health check looks right. Then
send a test Zillow-style inquiry (or wait for a real one) and confirm a reply goes
out in seconds and the thread gets the `zillow/awaiting-renter` label.

Other POST routes on the service, for reference:

- `/` — the Composio trigger webhook (guarded by `WEBHOOK_SECRET`)
- `/reprocess` and `/send-approved` — operator endpoints, guarded by
  `REPROCESS_SECRET` rather than `WEBHOOK_SECRET`
- `/hooks/website-lead/<token>` — Netlify form intake; the token is in the URL

## Notes / honesty

- The exact Composio v3 execute endpoint path and trigger payload shape can drift with
  their API version. The parsing is defensive about payload shape, but if the first
  real trigger logs a parse miss, send Claude the logged payload and it'll adjust
  `extract_event()` in `gmail_client.py` in one edit.
- Booking replies and the nudge/reminder loops are handled in-process by `cron.py`
  on a 20-minute tick, so the external sweep is already just a backstop.
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
