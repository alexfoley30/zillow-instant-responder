# Zillow Instant Responder

Sub-30-second auto-reply to new Zillow rental inquiries.

As of the v2 single-writer rebuild (2026-07-28) this service owns EVERY
renter-facing send: instant first replies, question handling, time parsing,
booking, confirmations, counters, nudges. The Claude sweep is no longer the
"smart layer" — it is a non-sending watchdog that POSTs `/reprocess` when it
finds drift. See the module docstring at the top of `responder.py` for the
per-message flow.

## How it works

```
New Zillow email lands in alex@azfoleyhomes.com
   -> Composio Gmail trigger fires a webhook POST to this service
   -> service parses inquirer name + property address from the subject
   -> sends Template 1 availability-ask in-thread (via Composio)
   -> labels the thread zillow/awaiting-renter
Renter has a reply in a few seconds, 24/7, independent of Alex's Mac.
```

There IS a little AI in the path now, but only at the edges: `llm.py` makes Haiku
micro-calls that classify a renter reply and extract time candidates. The model
extracts, it never decides — every booking decision runs through the
deterministic validators in `rules.py` / `calendar_logic.py`. So you do need an
`ANTHROPIC_API_KEY`, and the service has real dependencies: they are pinned in
`requirements.txt` and Render installs them via the `buildCommand` in
`render.yaml`.

## What's already done (by Claude)

- `responder.py` — HTTP handler + per-message routing (idempotent, secret-verified)
- `gmail_client.py` — Composio Gmail/Calendar wrapper, thread fetch, message parsing
- `ledger.py` — Firestore ledger, the create-once send locks that kill duplicates
- `calendar_logic.py` — calendar ops + the deterministic slot engine
- `rules.py` — pure scheduling rules, zero I/O, fully unit-testable
- `llm.py` — Haiku classify/extract micro-calls with regex fallback
- `facts.py` — standing-rule question router, then fact sheets
- `templates.py` — every renter-facing string in one place
- `cron.py` — in-process 20-minute maintenance ticker (label retries, stuck sends, nudges)
- `scripts/` — `shadow_dump.py`, `sync_facts.py`
- `tests/` — the regression suite (`python -m pytest -q`)
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
4. In the service's Environment tab, set:
   - `COMPOSIO_API_KEY` = your new key from Step 1
   - `COMPOSIO_CONNECTED_ACCOUNT_ID` = `gmail_boast-punnet`
   - `AWAITING_RENTER_LABEL_ID` = `Label_1717014254813700027`  (zillow/awaiting-renter)
   - `HANDLED_LABEL_ID` = `Label_6932202305849666189`  (zillow/handled)
   - `WEBHOOK_SECRET` = make up a long random string; use the same value in Step 2's header
5. Deploy. Copy the live URL (e.g. `https://zillow-instant-responder.onrender.com`) and
   paste it into the Composio trigger from Step 2.

### Step 4 — Test

Hit the URL in a browser — you should see `zillow-pipeline ok (dry_run=true)`
(health check; `render.yaml` points `healthCheckPath` at `/`). The `dry_run`
flag echoes the `DRY_RUN` env var, so this doubles as a "am I live or in shadow
mode?" check. Then send a test Zillow-style inquiry (or wait for a real one) and
confirm a reply goes out in seconds and the thread gets the
`zillow/awaiting-renter` label.

## Routes

| Method + path | What it does |
|---|---|
| `GET /` | Health check. Returns `zillow-pipeline ok (dry_run=<bool>)`. |
| `POST /` | The Composio Gmail trigger webhook. Guarded by `WEBHOOK_SECRET`. |
| `POST /reprocess` | The watchdog sweep asks the service to re-look at a thread. Guarded by `REPROCESS_SECRET` (falls back to `WEBHOOK_SECRET`). |
| `POST /send-approved` | Poke approval loop: Alex answered a needs-you ping and the approval MCP relays it here. Poke never emails renters, it commands this endpoint, so the service stays the single writer. Same secret as `/reprocess`. |

## Notes / honesty

- The exact Composio v3 execute endpoint path and trigger payload shape can drift with
  their API version. The parsing is defensive about payload shape, but if the first
  real trigger logs a parse miss, send Claude the logged payload and it'll adjust
  `extract_event()` in one edit. That function moved out of `responder.py` in the v2
  rebuild — it now lives in `gmail_client.py`.
- Once this is live and confirmed, the sweep can be slowed down further. It no longer
  sends anything, so it's purely a drift watchdog that POSTs `/reprocess`.
  Tell Claude when ready.
- If you ever want to avoid the monthly host cost, the alternative is running this on an
  always-on office machine behind a Cloudflare Tunnel — more setup, Mac/office-dependent,
  but $0/mo. Ask Claude if you want that path instead.
