# Zillow Instant Responder

Sub-30-second auto-reply to new Zillow rental inquiries. This is the "instant layer."
The existing 5-minute sweep stays as the "smart layer" for booking/calendar.

## How it works

```
New Zillow email lands in alex@azfoleyhomes.com
   -> Composio Gmail trigger fires a webhook POST to this service
   -> service parses inquirer name + property address from the subject
   -> sends Template 1 availability-ask in-thread (via Composio)
   -> labels the thread zillow/awaiting-renter
Renter has a reply in a few seconds, 24/7, independent of Alex's Mac.
```

Claude is used only for narrow micro-calls (`llm.py`: classify and extract). Code, never
the model, decides what gets sent, so token cost stays tiny. The service is no longer
stdlib-only: `render.yaml` builds with `pip install -r requirements.txt`, and
`ANTHROPIC_API_KEY` plus a Firebase service-account file are both required.

## Routes

The service answers five paths, not just one (`responder.py`, `Handler.do_POST`):

| Path | Who calls it | Auth |
|---|---|---|
| `GET /` | Render health check | none |
| `POST /` | the Composio Gmail trigger | `X-Webhook-Secret: <WEBHOOK_SECRET>` |
| `POST /reprocess` | the Claude sweep, when it spots drift | `X-Webhook-Secret: <REPROCESS_SECRET>`, falling back to `WEBHOOK_SECRET` |
| `POST /send-approved` | the Poke approval relay, after Alex answers a needs-you ping | same as `/reprocess` |
| `POST /hooks/website-lead/<token>` | the boundlessaz.com Netlify form notification | the token in the URL, derived from `POKE_API_KEY`; with no key the route is off |

## What's already done (by Claude)

- `responder.py` — the webhook receiver + reply logic (idempotent, secret-verified)
- `gmail_client.py` — Composio Gmail/Calendar wrapper and message parsing
- `calendar_logic.py` — availability windows, event create/update/delete
- `ledger.py` — Firestore single-writer locks and thread state
- `rules.py`, `templates.py`, `facts.py` — routing rules, reply copy, property facts
- `llm.py` — the Haiku classify/extract micro-calls
- `website_lead.py` — the Netlify owner-lead hook (see Routes above)
- `cron.py` — nudges, reminders and stuck-send alerts
- `scripts/` — `application.py`, `sync_facts.py`, `shadow_dump.py`
- `render.yaml` — one-file deploy config for Render
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
4. In the service's Environment tab, set every var `render.yaml` marks `sync: false`
   (that file is the authority; this list mirrors it):
   - `COMPOSIO_API_KEY` = your new key from Step 1
   - `COMPOSIO_CONNECTED_ACCOUNT_ID` = `gmail_boast-punnet`
   - `COMPOSIO_USER_ID` = the Composio user id for that connected account
   - `AWAITING_RENTER_LABEL_ID` = `Label_1717014254813700027`  (zillow/awaiting-renter)
   - `HANDLED_LABEL_ID` = `Label_6932202305849666189`  (zillow/handled)
   - `NEEDS_REPLY_LABEL_ID` = the zillow/needs-reply label id
   - `WEBHOOK_SECRET` = make up a long random string; use the same value in Step 2's header
   - `ANTHROPIC_API_KEY` = for the Haiku classify/extract calls
   - `POKE_ENDPOINT` = where needs-you / away-block / same-day pings go
   Already set for you in `render.yaml`: `DRY_RUN` (ships as `"true"`, flip to `"false"`
   to go live) and `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/firebase-sa.json`. Upload
   the Firebase service-account JSON as a Render Secret File named `firebase-sa.json`.
5. Deploy. Copy the live URL (e.g. `https://zillow-instant-responder.onrender.com`) and
   paste it into the Composio trigger from Step 2.

### Step 4 — Test

Hit the URL in a browser — you should see `zillow-pipeline ok (dry_run=true)` (health
check; the flag echoes `DRY_RUN`, so `dry_run=false` means it is live).
Then send a test Zillow-style inquiry (or wait for a real one) and confirm a reply goes
out in seconds and the thread gets the `zillow/awaiting-renter` label.

## Notes / honesty

- The exact Composio v3 execute endpoint path and trigger payload shape can drift with
  their API version. `responder.py` is defensive about payload shape, but if the first
  real trigger logs a parse miss, send Claude the logged payload and it'll adjust
  `extract_event()` in `gmail_client.py` in one edit.
- Once this is live and confirmed, the 5-minute sweep can be slowed to ~10-15 min (it's
  now just a backstop for booking replies), cutting cost further. Tell Claude when ready.
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
