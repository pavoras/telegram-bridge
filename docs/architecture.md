# Architecture

## Components

```
 computer                                  server
┌──────────────────────────┐             ┌───────────────────────────────────────────┐
│ agent ── tg (CLI / MCP) ─┼── HTTPS ───>│ relay                                     │
│ Claude Code hooks ── tg ─┘             │  ├─ HTTP API (ThreadingHTTPServer)        │
└──────────────────────────┘             │  ├─ poller ── getUpdates (long polling) ──┼─> Telegram
                                         │  ├─ outbox worker ── sendMessage/Photo ───┼─>
                                         │  ├─ scheduler (pings, expiry, 08:15, purge)│
                                         │  └─ SQLite (WAL)                          │
                                         └───────────────────────────────────────────┘
```

- **Relay** (`relay/`): the only process that talks to Telegram, because a bot can have only
  one `getUpdates` consumer. Long polling instead of a webhook: no public route for Telegram,
  works behind any NAT. Four threads: HTTP API, poller, outbox worker, scheduler.
- **Client** (`client/tg.py`): one file without dependencies, so it can be downloaded from the
  relay and run anywhere with Python 3.10+. CLI, Claude Code hooks, installer and MCP server.
- **Integrations** (`integrations/`): the Claude Code skill and setup guide, instructions for
  other agents, the daily report task.

## Data model (SQLite)

| Table | Holds |
|---|---|
| `messages` | reports, questions, pings, daily reports: kind, status, text, buttons, origin, answer |
| `codes` | the short code of each question; a code is not reused for 7 days |
| `tg_links` | Telegram message id → message (main message and its photos) |
| `outbox` | everything to send or edit in Telegram, with attempts and next try |
| `inbox_assign` | an incoming answer waiting for "which question do you mean?" (10 min) |
| `albums`, `albums_done` | photo albums being collected (3 s) and where they went |
| `media` | uploaded images (file on disk) and incoming photos (Telegram `file_id` only) |
| `sessions` | per agent session: frozen origin, done delay, scheduled ping |
| `devices` | registration requests and approved devices (token hashes) |
| `idempotency` | client idempotency keys → message |
| `state` | Telegram offset, presence, paired chat, host status, daily switch, pairing code |

Everything older than 7 days is deleted hourly; questions expire after 24 hours.

## Answer routing

An incoming text, photo or album is assigned in this order:

1. **Reply** to a bot message → that question (via `tg_links`).
2. **`#482 …`** at the start → the question with this code, if it is still open.
3. **Exactly one open question** → that one; the bot confirms with code and origin.
4. **Several open questions** → the bot offers one button per question, bound to this incoming
   message only (valid 10 minutes).
5. **No open question** → the bot says so; nothing is stored.

An explicit target (reply or code) that is no longer open is never redirected to another
question; the bot says why ("already answered", "expired", "handled in the app"). Every state
change of a question is one transaction conditioned on the old state: the first valid change
wins (answer, button, expiry, closing by the agent or by activity in the app).

## Delivery

- Every send or edit goes through the outbox: API calls only write to the database and return
  immediately, so a Telegram outage never blocks agents.
- The worker sends at most about one message per second, honours `retry_after` on 429 and
  retries with exponential backoff (up to 10 attempts); 4xx errors other than 429 are permanent
  and counted in `/status` and the daily fallback.
- Delivery is **at least once**: if Telegram's success response is lost, or the relay dies
  between sending and recording, the message can arrive twice. A delivery error never
  overwrites a question that was answered meanwhile.
- Processing an update and storing the new Telegram offset happen in one transaction; an
  update that fails three times is skipped so it cannot block the chat.
- A question with images is a main message (text, buttons) followed by the photos as replies
  to it, so replying to a photo also answers the question.

## Done pings

Hooks report events (`stop`, `permission`, `prompt`) with a timestamp. The relay keeps one
scheduled ping per session, replaces it on every `stop`, cancels it on every `prompt`, and
sends it when due unless the session asked a Telegram question meanwhile. Events older than
the last one are ignored, so a spooled event that arrives late does no harm. Scheduling on the
server means nothing has to keep running on the computer, and reboots lose nothing.

The session's origin (computer, account, project) is frozen at first contact, so a later
account switch on the computer does not relabel an existing session.

## Design decisions

- **One person per relay.** One bot serves exactly one private chat. A second person gets a
  second bot and instance. This keeps authorization trivial: whoever writes in that chat is
  the user.
- **Standard library only.** No dependencies to update, audit or install; the client can be
  copied as one file.
- **Tokens per device, approved in Telegram.** No server access needed to add a computer;
  the 4-digit code prevents approving a stranger's request by mistake.
- **No forwarding into finished sessions.** A reply to a done ping does not reach the session:
  Claude Code has no supported way to inject a message into an idle session from outside.
  Remote Control in the Claude apps covers that case.
- **Text and images only.** No files, voice or arbitrary attachments.
