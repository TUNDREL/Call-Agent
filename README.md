# lineback

Webhook backend for the Tundrel voice agent. [Vapi](https://vapi.ai) answers the phone; when the caller wants an appointment, Vapi calls this service's tools. lineback saves the request and emails the owner.

## What it does today

- **Records appointment *requests*, never confirmed bookings.** Live scheduling is not connected yet, so the agent is told not to promise times. It collects the caller's preferred time and the request is saved as `pending` for a person to confirm.
- **Notifies the owner by email** for every new request. If email fails, the failure is recorded and the send is retried on the next request and on every restart.
- **Authenticates every webhook** (Bearer token or `X-Vapi-Secret`). With no secret configured it refuses all requests instead of accepting them.
- **Is safe against retries.** Vapi may resend a tool call; the same `toolCallId` returns the stored result and never creates a second request. The same request twice in one call is also collapsed.
- **Never crashes the call.** Malformed input gets a 4xx; a tool failure returns a safe fallback instruction ("take the caller's name and number") instead of a 500.

Not built yet: a real calendar, storing end-of-call reports (summaries, minutes), per-business configuration, emergency escalation. See [Roadmap](#roadmap).

## Quick start

```bash
uv sync
cp .env.example .env        # then fill it in (see below)
uv run lineback             # http://127.0.0.1:8000  (or: uv run uvicorn lineback.main:app --port 8000)
```

Generate the shared secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

For local development you can set `LINEBACK_ALLOW_INSECURE=1` instead of a secret, and use a tunnel such as ngrok for the Server URL.

## Vapi setup

1. **Server URL:** `https://<your-host>/webhook`
2. **Authentication:** in Vapi, create a **Bearer Token** credential whose value is your `LINEBACK_WEBHOOK_SECRET` and attach it to the Server URL / tools. (The legacy `X-Vapi-Secret` header with the same value also works.) A Vapi Server URL has no authentication until you attach a credential, so do this before going live.
3. **Tool schemas:** update the two tools.

   `checkAvailability`
   ```json
   {
     "type": "object",
     "properties": {
       "date": { "type": "string", "description": "The day the caller asked about, if they said one" },
       "serviceType": { "type": "string", "description": "What the caller needs, e.g. AC repair" }
     }
   }
   ```

   `bookAppointment`
   ```json
   {
     "type": "object",
     "properties": {
       "customerName":   { "type": "string", "description": "Caller's full name" },
       "serviceAddress": { "type": "string", "description": "Street address where service is needed" },
       "serviceType":    { "type": "string", "description": "e.g. AC not cooling, furnace not heating, maintenance" },
       "preferredTime":  { "type": "string", "description": "Preferred day and time window, in the caller's words" },
       "notes":          { "type": "string", "description": "Anything the technician should know" },
       "customerPhone":  { "type": "string", "description": "Only if different from the number they are calling from" }
     },
     "required": ["customerName", "serviceAddress", "serviceType", "preferredTime"]
   }
   ```
   The caller's number is taken from the call automatically. The old schema (`date`, `serviceType`) still works.
4. **System prompt:** add: *"Never tell the caller an appointment is booked or confirmed. After bookAppointment succeeds, say the request has been received and the team will confirm by phone or text. If a tool fails, take the caller's name and number and say the team will call back."*

## Configuration

All variables are optional except the secret.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LINEBACK_WEBHOOK_SECRET` | unset | Shared secret from Vapi. **Required** unless `LINEBACK_ALLOW_INSECURE=1` |
| `LINEBACK_ALLOW_INSECURE` | off | Dev only: accept webhooks with no secret |
| `LINEBACK_BUSINESS_NAME` | `Tundrel` | Shown in notification emails |
| `LINEBACK_DB_PATH` | `lineback.db` | SQLite file |
| `LINEBACK_OWNER_EMAIL` | unset | Where notifications go |
| `LINEBACK_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_FROM` | unset / `587` | SMTP account used to send |
| `LINEBACK_SMTP_SECURITY` | `starttls` | `starttls` (587), `ssl` (465) or `none` (local testing only) |
| `LINEBACK_LOG_LEVEL` | `INFO` | Logging level |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Used by `uv run lineback` |

Without `LINEBACK_SMTP_*` and `LINEBACK_OWNER_EMAIL`, requests are still saved but nobody is told. The server logs a warning at startup.

## Data

Requests live in SQLite. To see what is waiting for a human:

```bash
sqlite3 lineback.db "SELECT id, created_at, customer_name, customer_phone, service_type, preferred_time, notified_at FROM booking_requests WHERE status='pending'"
```

The reference callers hear is `LB-` plus the id (id 7 is `LB-0007`). The database file holds customer names, numbers and addresses: keep it out of git (already ignored) and back it up. **If you deploy somewhere with an ephemeral filesystem, mount a persistent volume or the data disappears on restart.**

## Tests

```bash
uv run pytest
```

## Roadmap

1. Connect a real calendar (Google Calendar or Cal.com first) so `checkAvailability` returns real slots.
2. Handle `end-of-call-report`: store summaries, duration and cost; send owner call summaries; count minutes against plan limits.
3. Per-business configuration keyed on the called number (hours, service area, escalation contact).
4. Emergency escalation (gas smell, carbon monoxide) with a live transfer.
5. Deploy to a stable HTTPS host with uptime alerts.
