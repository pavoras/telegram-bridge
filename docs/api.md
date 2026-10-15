# HTTP API

Everything the `tg` client does is plain HTTPS + JSON, so any language can talk to the relay.
All paths start with `/v1`. Authenticated endpoints need `Authorization: Bearer <device token>`;
the relay identifies the client from the token (never from the body).

Errors are `{"error": "<code>"}` with a fitting status: 400 (invalid field, the code names it),
401 (`unauthorized`), 403 (`forbidden`: missing right), 404 (`not_found`), 409, 413
(`too_large`), 415 (`not_an_image`), 429 (`rate_limited`, `busy`), 502/503 (`telegram`:
fetching a photo from Telegram failed), 507 (`media_quota`).

## Without a token

### `GET /v1/health`

`200 {"ok": true, "last_poll_age_s": 12.3, "webhook_set": false}`, or `503` with `"ok": false`
when the relay has not polled Telegram successfully for 10 minutes. `webhook_set: true` means a
webhook is configured for the bot, which blocks long polling (remove it with `deleteWebhook`).

### `GET /v1/client/{file}`

`tg.py`, `SKILL.md`, `SETUP.md`, `AGENT-INSTRUCTIONS.md`: the client and its instructions,
so a new machine can install from the relay alone.

### `POST /v1/register`

```json
{"name": "laptop", "sha256": "<sha256 hex of the device token>", "host": "Laptop"}
```

`202 {"status": "pending", "code": "4821"}`: the bot asks the user to approve the device and
shows the same code. `409 exists` (name taken), `429 busy` (one request per minute, at most 3
pending). Names: `[a-z0-9-]{1,32}`.

## Messages

### `POST /v1/messages`

```json
{
  "kind": "question",
  "text": "Deploy now?",
  "idempotency_key": "6f1c…",
  "title": "Release 2.3",
  "buttons": ["Yes", "No"],
  "origin": {"host": "Laptop", "project": "shop", "agent": "Codex", "account": "work"},
  "session_key": "codex-shop-2026-10-15",
  "images": ["<media id>"]
}
```

| Field | |
|---|---|
| `kind` | `report`, `question`, `done`, `daily` (needs right `full` or the kind itself) |
| `text` | required except for `done`; longer than 3,500 characters is cut |
| `idempotency_key` | required, ≤ 100 characters; repeating it returns the first message |
| `title` | optional, shown in the header |
| `buttons` | questions only, ≤ 8, each ≤ 32 characters |
| `origin` | optional, ≤ 10 string fields (≤ 200 characters); shown, never used for authorization |
| `session_key` | optional; groups questions and done pings of one agent session |
| `images` | optional, ≤ 10 media ids from `POST /v1/media` of the same client |

`201 {"id": "<uuid>", "code": 482}` (`code` for questions, otherwise null).
`409 daily_off` for `daily` while the daily report is switched off.

### `GET /v1/messages/{id}?wait=SECONDS`

Status of your own message; with `wait` (≤ 50) the request blocks until the question is
answered or closed, or the time is up.

```json
{"id": "…", "code": 482, "status": "answered",
 "answer": {"text": "Yes", "button": "Yes", "images": ["<media id>"], "answered_at": 1792…},
 "closed_reason": null}
```

`status`: `queued`, `sent` (open), `answered`, `closed`, `expired`, `failed`.
`closed_reason`: `answered_in_app`, `closed_by_agent`, `expired`, …

### `POST /v1/messages/{id}/close`

Withdraws an open question; the Telegram message is edited. `200 {"closed": true}`.

## Images

### `POST /v1/media`

Raw image bytes as the body (`Content-Type: image/png`, `image/jpeg` or `image/webp`; the
relay checks the magic bytes), ≤ 10 MB. `201 {"id": "<32 hex>"}`. Right: `full`.
Stored for 7 days; 200 MB in total.

### `GET /v1/media/{id}`

The bytes of an image you uploaded, or of a photo the user sent as the answer to your
question (the relay downloads it from Telegram on demand).

## Sessions (done pings)

### `POST /v1/sessions/{key}/events`

What the Claude Code hooks send. Right: `full`.

```json
{"type": "stop", "ts": 1792000000.5, "text": "summary", "title": "session title",
 "origin": {…}, "busy": false, "human": false, "summary_blocked": false}
```

- `stop`: schedule a "done" ping (unless `busy` or a question of the session is open)
- `permission`: schedule a "waiting for approval" ping; `text` is the request
- `prompt`: cancel the ping; with `human: true` also close the session's open questions
  created before `ts` (as `answered_in_app`)

Events older than the session's last event are ignored (`"result": "stale"`).
`summary_blocked: true` replaces the text with "(summary withheld: possible secret)".
Response: `{"result": "scheduled" | "cancelled" | "stale", "presence": "away" | "present"}`.

### `POST /v1/sessions/{key}`

`{"done_delay": 0}` seconds until the ping (≤ 7 days), `"off"`, or `null` for the default
(600). `200 {"done_delay": …}`.

## Status and settings

### `GET /v1/status`

Right: `full`.

```json
{"open": [{"id": "…", "code": 482, "client_id": "laptop", "project": "shop", "title": "…", "created_at": 1792…}],
 "presence": "away",
 "host_status": {"items": {"monitor": "ok"}, "age_s": 300},
 "last_daily_at": 1792…,
 "daily": "on"}
```

### `POST /v1/host-status`

`{"items": {"disk /": "41 %", "backup": "3 h ago"}}`: ≤ 20 items, keys ≤ 40 and values ≤ 200
characters. Replaces the previous block. Right: `full` or `host-status`.

### `POST /v1/daily`

`{"enabled": true}` switches the daily report (and the 08:15 fallback) on or off.
`200 {"daily": "on"}`. Right: `full`.

## Limits

30 requests per minute per token (`429 rate_limited`), 16 connections at once, request bodies
≤ 64 KB (images ≤ 10 MB), 15 seconds to send a request.
