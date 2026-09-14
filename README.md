# Zillow Instant Responder

Sub-30-second auto-reply to new Zillow rental inquiries. It started as just the "instant
layer" in front of a separate 5-minute sweep, but since the v2 cutover (see the header
comment in `render.yaml`) this service is the single writer for every renter-facing send:
the instant ack, booking and calendar replies (`calendar_logic.py`), and the in-process
maintenance ticker (`cron.py`). Firestore is the ledger.

## How it works

```
New Zillow email lands in alex@azfoleyhomes.com
   -> Composio Gmail trigger fires a webhook POST to this service
   -> service parses inquirer name + property address from the subject
   -> sends Template 1 availability-ask in-thread (via Composio)
   -> labels the thread zillow/awaiting-renter
Renter has a reply in a few seconds, 24/7, independent of Alex's Mac.
```

The reply copy itself is still template-only, but there is AI in the path now: every
renter-facing send goes through `review_gate()` in `responder.py`, which calls
`llm.review_reply()`, and renter replies are classified with `llm.classify_reply()`.
Both are Claude Haiku micro-calls and both fail open, so a missing `ANTHROPIC_API_KEY`
degrades to the deterministic guards instead of blocking sends. Set the key in
production anyway, it is what makes the second look work.

The service is no longer standard-library only: `requirements.txt` pins its four
dependencies and `render.yaml` builds with `pip install -r requirements.txt`.

## What's already done (by Claude)

- `responder.py` - webhook receiver, HTTP routes, reply/send logic (idempotent, secret-verified)
- `gmail_client.py` - Composio Gmail calls, payload parsing (`extract_event()`), labels
- `ledger.py` - Firestore ledger, the single source of truth for thread state
- `calendar_logic.py`, `rules.py`, `facts.py`, `templates.py` - booking, guardrails, copy
- `llm.py` - the Haiku micro-calls: `classify_reply()` (with a regex fallback) and the
  pre-send `review_reply()`. Both fail open.
- `cron.py` - in-process maintenance ticker (retries, nudges, showing reminders)
- `render.yaml` - one-file deploy config for Render
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

   `render.yaml` is the current list of what the service reads, and it declares more
   than the five above (`COMPOSIO_USER_ID`, `NEEDS_REPLY_LABEL_ID`, `ANTHROPIC_API_KEY`,
   `POKE_ENDPOINT`, `DRY_RUN`, `GOOGLE_APPLICATION_CREDENTIALS`). Check it before you
   deploy. The Firestore service-account JSON goes in as a Render secret file.
5. Deploy. Copy the live URL (e.g. `https://zillow-instant-responder.onrender.com`) and
   paste it into the Composio trigger from Step 2.

### Step 4 — Test

Hit the URL in a browser. The `GET /` health check answers
`zillow-pipeline ok (dry_run=True)`, where the flag mirrors the `DRY_RUN` env var, so a
service that has been cut over live reads `dry_run=False`.
Then send a test Zillow-style inquiry (or wait for a real one) and confirm a reply goes
out in seconds and the thread gets the `zillow/awaiting-renter` label.

## Notes / honesty

- The exact Composio v3 execute endpoint path and trigger payload shape can drift with
  their API version. The parsing is defensive about payload shape, but if the first
  real trigger logs a parse miss, send Claude the logged payload and it'll adjust
  `extract_event()` in one edit. That function lives in `gmail_client.py` now, not in
  `responder.py`.
- Booking replies are no longer backstopped by the old 5-minute sweep: this service owns
  them, and `cron.py` runs its own 20-minute maintenance loop in-process (label retries,
  stuck sends, stale-lead nudges, showing reminders).
- If you ever want to avoid the monthly host cost, the alternative is running this on an
  always-on office machine behind a Cloudflare Tunnel — more setup, Mac/office-dependent,
  but $0/mo. Ask Claude if you want that path instead.
