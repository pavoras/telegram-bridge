<p align="center">
  <img src="assets/avatar.jpg" alt="telegram-bridge bot avatar" width="128">
</p>

<h1 align="center">telegram-bridge</h1>

<p align="center">
  Your coding agents, on your phone.<br>
  Questions, "I'm done" pings and reports from Claude Code, Codex or any script, on Telegram.<br>
  Answers go back to exactly the agent that asked.
</p>

---

You start a long task, walk away, and the agent stops to ask a question that nobody sees.
**telegram-bridge** fixes that. It is a tiny self-hosted relay that owns a Telegram bot,
plus a single-file client that any agent can call:

```console
$ tg ask "Migration 0042 drops column legacy_id. Run it on production now?" --buttons "Yes,No,Later"
0b5e3c1a-… 482
$ tg wait 0b5e3c1a-…
{"status": "answered", "answer": {"text": "Later", "button": "Later"}, "reason": null}
```

On your phone:

```
❓ #482 · shop · DB migration
🖥 Laptop · Claude

Migration 0042 drops column legacy_id. Run it on production now?
[ Yes ] [ No ] [ Later ]
```

## Features

- **Questions with answers routed back.** Tap a button, reply to the message, or write
  `#482 later`. With several agents asking at once, every answer lands at the right one, and
  never at the wrong one: explicit targets are never rerouted, ambiguous messages get a picker.
- **"Done" pings for Claude Code.** Hooks send `✅ done` with a short summary 10 minutes after
  a session finishes its turn, unless you are back at the keyboard first. A permission prompt
  sends `⏸ waiting for approval`. The delay is adjustable per session ("ping me right away",
  "no pings").
- **Away mode.** Send `/away` to the bot and every Claude Code session learns at its next
  prompt that it should ask you on Telegram instead of in the app. `/back` ends it.
- **Images both ways.** Agents attach screenshots; you answer with photos or albums, which
  the agent gets as local files (or as images over MCP).
- **Any agent.** A plain CLI for Codex, scripts and CI; a Claude Code skill and hooks;
  an MCP server for agents without a shell, such as the Claude Desktop chat.
- **Daily report** (optional): a scheduled agent task sums up yesterday's commits, open
  questions and server status at 08:00; if it does not arrive, the relay sends a fallback at 08:15.
- **Server monitor hook.** Monitors can post alarms and a host status block with a token that
  can do nothing else. `/status` shows it.
- **Effortless setup.** Pair your chat with a one-time code, approve new machines with a tap
  (after comparing a 4-digit code), no config files to edit on the server.
- **Small and boring on purpose.** Python standard library only (relay and client), SQLite,
  long polling (no public webhook), about 3,000 lines with 160+ tests.
- **English or German** bot texts, any time zone.

## How it works

```
 your machines                                         your server
┌──────────────────────────────┐                     ┌──────────────────────────────┐
│ Claude Code ── hooks/skill ─┐│                     │ Caddy (HTTPS)                │
│ Codex, scripts, CI ─────────┼┼─ tg ── HTTPS ──────>│  └─> relay (Python + SQLite) │
│ Claude Desktop chat ── MCP ─┘│   (device token)    │       ├─ HTTP API /v1/*      │
└──────────────────────────────┘                     │       ├─ outbox + scheduler  │
                                                     │       └─ long polling ───────┼──> Telegram
 server monitor ── tg / HTTPS (report-only token) ──>│                              │
                                                     └──────────────────────────────┘
```

Only the relay talks to Telegram, so one bot serves all your machines and agents. Pings are
scheduled on the relay, not on your PC, so nothing has to keep running locally. More in
[docs/architecture.md](docs/architecture.md).

## Quick start

You need a server with Docker and a domain name (or any host that can run a small Python
service behind HTTPS), and Python 3.10+ on your computers.

**1. Create a bot.** Talk to [@BotFather](https://t.me/BotFather), send `/newbot`, keep the token.

**2. Run the relay.**

```sh
git clone https://github.com/pavoras/telegram-bridge.git
cd telegram-bridge/deploy
cp .env.example .env        # set DOMAIN, TG_BOT_TOKEN, TG_LANGUAGE, TG_TIMEZONE
chmod 600 .env
docker compose up -d --build
docker compose logs relay | grep /start
```

The log shows `/start 123456`. Send exactly that to your bot in a private chat. The bot
answers "Connected" and from now on serves only you.

**3. Install the client on each computer.**

```sh
python3 client/tg.py install --relay https://tg.example.com --name laptop --host-label Laptop
```

On Windows: `py -3 client\tg.py install …` or `.\install.ps1 -Relay https://tg.example.com`.
The bot asks you to approve the new device; tap **Approve** if the code matches the one
in your terminal. Done: `tg selftest` prints `ok`.

Or let your agent do it: tell Claude Code *"Set up Telegram following
https://tg.example.com/v1/client/SETUP.md"*.

Details, other reverse proxies and running without Docker: [docs/self-hosting.md](docs/self-hosting.md).

## Using it

| You want | Do |
|---|---|
| Agents to ask you on Telegram | send `/away` to the bot (`/back` when you return) |
| See what is open | `/open`, `/status` |
| Answer a question | tap a button, reply to it, or `#482 your answer` |
| Faster or no done pings in a session | tell the agent "ping me right away" / "no pings" |
| The daily report | `/daily on` and a scheduled task, see [docs/claude-code.md](docs/claude-code.md#daily-report) |

German commands work too (`/offen`, `/weg`, `/da`, `/hilfe`, `/daily an`).

For agents:

| Agent | How |
|---|---|
| Claude Code | `tg install` adds the `telegram` skill and hooks: [docs/claude-code.md](docs/claude-code.md) |
| Codex, other CLI agents, scripts | the `tg` command and [AGENT-INSTRUCTIONS.md](integrations/agents/AGENT-INSTRUCTIONS.md): [docs/clients.md](docs/clients.md) |
| Claude Desktop chat, other MCP clients | `tg mcp`: [docs/mcp.md](docs/mcp.md) |
| Anything that speaks HTTP | [docs/api.md](docs/api.md) |

## Security in short

- The bot serves exactly one private chat (yours). Everything else is ignored.
- Every machine has its own random token; the relay stores only its SHA-256. Rights per token
  (e.g. a monitor may only send reports).
- `tg` refuses to send text that looks like a secret (keys, tokens, private keys), and hooks
  withhold such summaries. This is a safety net, not a guarantee.
- No public webhook, no inbound port besides HTTPS, hardened container, metadata-only logs,
  messages deleted after 7 days.

Read [docs/security.md](docs/security.md) before you rely on it.

## Documentation

| | |
|---|---|
| [Self-hosting](docs/self-hosting.md) | relay deployment, configuration, pairing, monitors, updates, backups |
| [Clients](docs/clients.md) | installing `tg`, all commands, exit codes, config file |
| [Claude Code](docs/claude-code.md) | skill, hooks, done pings, away mode, daily report |
| [MCP](docs/mcp.md) | `tg mcp` for the Claude Desktop chat and other MCP clients |
| [HTTP API](docs/api.md) | endpoints, rights, limits |
| [Architecture](docs/architecture.md) | data model, answer routing, delivery guarantees, design decisions |
| [Security](docs/security.md) | threat model and what is (not) protected |

## Development

```sh
python -m unittest discover -s tests -t .
```

No dependencies. The tests include a fake Telegram Bot API and end-to-end runs of the relay
and several clients. CI runs them on Linux, macOS and Windows.

## License

[MIT](LICENSE)
