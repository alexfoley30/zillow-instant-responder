# Zillow Instant Responder

Sub-30-second auto-reply to new Zillow rental inquiries, plus every renter-facing
send after it. Since the v2 rebuild (2026-07-28) this service is the SINGLE WRITER:
it owns instant first replies, question handling, time parsing, booking, calendar
writes, confirmations, counters and nudges. The 5-minute Claude sweep is now a
non-sending watchdog that POSTs `/reprocess` when it spots drift. See the module
docstring at the top of `responder.py` for the authoritative flow.

## How it works

```
New Zillow email lands in alex@azfoleyhomes.com
   -> Composio Gmail trigger fires a webhook POST to this service
   -> service parses inquirer name + property address from the subject
   -> sends Template 1 availability-ask in-thread (via Composio)
   -> labels the thread zillow/awaiting-renter
Renter has a reply in a few seconds, 24/7, independent of Alex's Mac.
```

Costs stay low because the AI in the hot path is Haiku micro-calls only (classify
and extract; code decides). It is not free: `ANTHROPIC_API_KEY` is required, and
`llm.py` makes the calls. The service also has real dependencies now, pinned in
`requirements.txt` (firebase-admin, anthropic, google-cloud-firestore,
google-api-core), so Render runs `pip install -r requirements.txt` at build time.
The "pure standard library, no pip install" claim in earlier versions of this
runbook is obsolete; do not rely on it.

## What's already done (by Claude)

- `responder.py`: webhook receiver, routing and HTTP handlers (idempotent, secret-verified)
- `ledger.py`: Firestore create-once send locks (`reserve_send`); no lock, no send
- `gmail_client.py`: Composio Gmail/Calendar wrapper plus `extract_event()` and parsing
- `calendar_logic.py`: showing slots, booking, reschedules, cancellations
- `llm.py`: Haiku classify/extract micro-calls
- `rules.py`, `facts.py`, `templates.py`: routing rules, property facts, reply copy
- `website_lead.py`: Netlify `owner-lead` form hook, one Poke text to Alex
- `cron.py`: scheduled nudges and showing reminders
- `scripts/`: `application.py` (lease attribution), `sync_facts.py`, `shadow_dump.py`
- `tests/`: `python -m pytest -q`, the regression net for all of the above
- `render.yaml`: one-file deploy config for Render
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
   - `ANTHROPIC_API_KEY` = for the Haiku classify/extract calls in `llm.py`
   - `POKE_ENDPOINT` = sanctioned pings (needs-you, away-block, same-day Jace)
   - `DRY_RUN` = `"true"` until cutover (shadow mode). Flip to `"false"` to go live.
   - `GOOGLE_APPLICATION_CREDENTIALS` = `/etc/secrets/firebase-sa.json`, which is the
     Render **Secret File** named `firebase-sa.json`. Never commit it.

   `render.yaml` declares all of these; it is the list to check against, not this one.
5. Deploy. Copy the live URL (e.g. `https://zillow-instant-responder.onrender.com`) and
   paste it into the Composio trigger from Step 2.

### Step 4 — Test

Hit the URL in a browser and you should see `zillow-pipeline ok (dry_run=true)`, which is
the health check (`healthCheckPath: /` in `render.yaml`). The `dry_run` value tells you
straight away which mode the service is in.

Then send a test Zillow-style inquiry (or wait for a real one) and confirm a reply goes
out in seconds and the thread gets the `zillow/awaiting-renter` label. With `DRY_RUN=true`
nothing is sent or labeled; the service writes `zillow_shadow` docs instead.

Routes the service serves:

| Route | Method | What it does |
|---|---|---|
| `/` | GET | Health check, prints the dry-run mode |
| `/` | POST | Composio Gmail trigger, the main inbound path |
| `/reprocess` | POST | The Claude sweep watchdog asks for a re-derive |
| `/send-approved` | POST | Release a reply that was held for a human |
| `/hooks/website-lead/<token>` | POST | Netlify `owner-lead` form, see `website_lead.py` |

## Notes / honesty

- The exact Composio v3 execute endpoint path and trigger payload shape can drift with
  their API version. `responder.py` is defensive about payload shape, but if the first
  real trigger logs a parse miss, send Claude the logged payload and it'll adjust
  `extract_event()` in one edit. That function lives in `gmail_client.py`, not in
  `responder.py`, since the v2 rebuild.
- The 5-minute sweep no longer sends anything, so it can be slowed to ~10-15 min, cutting
  cost further. It stays useful as the drift watchdog that POSTs `/reprocess`.
  Tell Claude when ready.
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
