# Self-hosting the relay

The relay is one small Python process (standard library + SQLite). It owns your Telegram bot,
stores messages for 7 days and offers an HTTP API to your clients. It needs:

- outgoing HTTPS to `api.telegram.org` (long polling, no webhook, no inbound connection from Telegram)
- an HTTPS endpoint your computers can reach (a reverse proxy in front of port 8080)
- a persistent directory for `relay.db` and uploaded images (a few MB)

One relay serves one person: one bot, one private chat. A second person runs a second instance
with their own bot (same image, different token, database and URL).

## 1. Create the bot

1. Open [@BotFather](https://t.me/BotFather), send `/newbot`, choose a name and a username.
2. Keep the token (`123456789:AA…`) private: not in chats, not in repositories.
3. Optional: `/setuserpic` (an avatar is in `assets/avatar.jpg`), `/setdescription`.

The relay sets the bot's command menu itself on start.

## 2. Run it with Docker Compose (recommended)

`deploy/` contains a ready setup: the relay plus [Caddy](https://caddyserver.com/), which gets
a Let's Encrypt certificate automatically.

```sh
git clone https://github.com/pavoras/telegram-bridge.git
cd telegram-bridge/deploy
cp .env.example .env
chmod 600 .env
$EDITOR .env                 # DOMAIN, TG_BOT_TOKEN, TG_LANGUAGE, TG_TIMEZONE
docker compose up -d --build
```

Point the DNS name in `DOMAIN` at the server before starting, so Caddy can get the certificate.
Check: `curl https://tg.example.com/v1/health` → `{"ok": true, …}`.

### Behind your own reverse proxy

Start only the relay (`docker compose up -d --build relay`) and route your proxy to port 8080.
Under a path prefix the proxy must strip the prefix, e.g. Caddy:

```
example.com {
	handle_path /tg/* {
		reverse_proxy relay:8080
	}
}
```

nginx:

```nginx
location /tg/ {
    proxy_pass http://127.0.0.1:8080/;
    client_max_body_size 11m;
    proxy_read_timeout 70s;   # tg wait long-polls up to 50 s
}
```

The relay URL for clients is then `https://example.com/tg`.

### Without Docker

```sh
cd telegram-bridge
mkdir -p data/client-files
cp client/tg.py integrations/claude/SKILL.md integrations/claude/SETUP.md \
   integrations/agents/AGENT-INSTRUCTIONS.md data/client-files/
TG_BOT_TOKEN=123:abc TG_DB_PATH=./data/relay.db TG_MEDIA_DIR=./data/media \
TG_LISTEN=127.0.0.1:8080 TG_CLIENT_DIR=./data/client-files python3 -m relay.main
```

Python 3.10+ is enough. Run it as a systemd service or similar and put HTTPS in front.
`TG_CLIENT_DIR` holds the files the relay serves to new machines under `/v1/client/`;
the Docker image contains them already.

## 3. Pair your chat

On first start, without `TG_ALLOWED_CHAT_ID`, the relay is in setup mode and logs a pairing code:

```
WARNING relay setup mode: send this to your bot in a private chat:  /start 123456
```

Send exactly that to your bot. It answers "Connected" and serves only this chat from now on
(stored in the database; no restart needed). Five wrong codes replace the code (see the log
again). To pin the chat in configuration instead, set `TG_ALLOWED_CHAT_ID` to your numeric
chat id; it takes precedence over pairing.

## 4. Add your computers

On each computer, from a clone of the repository:

```sh
python3 client/tg.py install --relay https://tg.example.com --name laptop --host-label Laptop
```

or without cloning: tell your coding agent *"Set up Telegram following
https://tg.example.com/v1/client/SETUP.md"*. The relay serves the client and instructions.

The client creates a random token locally and sends only its SHA-256 to the relay. The bot
asks you: *New device "laptop (Laptop)" wants to send you messages. Confirmation code: 4821*.
Tap **Approve** only if your terminal shows the same code. See [clients.md](clients.md).

## Configuration

All settings are environment variables.

| Variable | Default | Meaning |
|---|---|---|
| `TG_BOT_TOKEN_FILE` | – | file containing the bot token (preferred, e.g. a Docker secret) |
| `TG_BOT_TOKEN` | – | the bot token itself (if no file is given) |
| `TG_ALLOWED_CHAT_ID` | empty | the only chat served; empty = paired chat or setup mode |
| `TG_LANGUAGE` | `en` | language of the bot's texts: `en`, `de` |
| `TG_TIMEZONE` | `UTC` | IANA time zone for shown times and the daily report (`Europe/Berlin`) |
| `TG_CLIENTS_FILE` | empty | optional JSON file with extra tokens and rights (below) |
| `TG_DB_PATH` | `/data/relay.db` | SQLite database |
| `TG_MEDIA_DIR` | `/data/media` | uploaded images (deleted after 7 days) |
| `TG_LISTEN` | `0.0.0.0:8080` | address of the HTTP API |
| `TG_CLIENT_DIR` | `/app/client-files` | files served under `/v1/client/` |
| `TG_API_BASE` | `https://api.telegram.org` | Telegram Bot API base URL (tests, local Bot API server) |

### Extra tokens with limited rights

Devices approved in Telegram get full rights. For anything that should only report, for
example a server monitor, add an entry to a clients file:

```json
{
  "server-monitor": {"sha256": "<sha256 hex of its token>", "rights": ["report", "host-status"]}
}
```

Rights: `full` (everything), or any of `report`, `question`, `done`, `daily` (message kinds
it may send) and `host-status`. Create a token with
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"` and its hash with
`printf %s "$TOKEN" | sha256sum`. The file is read at start; restart the relay after changes.
With Docker secrets (bind mounts), edit the file in place (`tee`), not with an editor that
replaces it.

### Server monitor

A monitor can send alarms as reports and a host status block that `/status` and the 08:15
fallback show:

```sh
curl -fsS https://tg.example.com/v1/host-status \
  -H "Authorization: Bearer $MONITOR_TOKEN" -H "Content-Type: application/json" \
  -d '{"items": {"monitor": "ok", "disk /": "41 %", "backup": "3 h ago"}}'
```

See [api.md](api.md). Send alarms by e-mail as well: if Telegram is the thing that is down,
a Telegram-only alarm cannot reach you.

## Operations

- **Health:** `GET /v1/health` returns 200 `{"ok": true, "last_poll_age_s": …}` while the relay
  polls Telegram, and **503** when the last successful poll is older than 10 minutes. Watch it
  with your monitoring (the Compose file uses it as the container healthcheck).
- **Logs:** metadata only (method, route, status, client name), never message texts or tokens.
- **Backups:** the database holds at most 7 days of messages; backing it up is optional. Without
  it you lose open questions, device approvals and the paired chat (pair again, re-run
  `tg register` on each computer).
- **Updates:** `git pull && docker compose up -d --build`. The database schema migrates itself.
  Re-run `tg install` on your computers afterwards to update the client and skill.
- **Pin the base image** for reproducible builds:
  `docker build -f relay/Dockerfile --build-arg PYTHON_IMAGE=python:3.13-slim@sha256:<digest> .`
- **Resources:** the Compose file limits the relay to 128 MB RAM and 0.25 CPU; it uses far less.

## Limits

| | |
|---|---|
| Text per message | 3,500 characters (longer is cut with a note) |
| Images | PNG, JPEG, WEBP; 10 MB each; 10 per message; 200 MB stored in total |
| Buttons | 8 per question, 32 characters each |
| Requests | 30 per minute per token, 16 connections at once |
| Questions | expire after 24 h |
| Retention | messages, images and links 7 days |
| Device registrations | 1 per minute, at most 3 pending |
