# MCP server

`tg mcp` runs the client as a [Model Context Protocol](https://modelcontextprotocol.io) server
over stdio. Use it for agents that cannot run shell commands or background waits, e.g. the
chat in Claude Desktop or other MCP clients. Claude Code does not need it (it has the skill and
hooks). It uses the same device config as `tg`, so install and approve the device first
([clients.md](clients.md)).

## Set up Claude Desktop

```sh
tg mcp-config
```

prints the entry with the right paths for this computer, e.g.

```json
{"mcpServers": {"telegram": {"command": "py", "args": ["-3", "C:/Users/you/.claude/skills/telegram/tg.py", "mcp"]}}}
```

Open Claude Desktop → Settings → Developer → Edit Config, merge the `telegram` entry into
`mcpServers` (keep everything else in that file), save and restart the app.

## Tools

| Tool | Arguments | Result |
|---|---|---|
| `telegram_notify` | `text`, optional `title`, `project`, `images` (local paths) | `{"id", "status": "sent"}` |
| `telegram_ask` | `text`, optional `buttons` (≤ 8), `wait_seconds` (0–50, default 50), `title`, `project`, `images` | `{"id", "code", "status", "answer", "reason"}` |
| `telegram_wait` | `id`, optional `wait_seconds` (0–50, default 50) | `{"id", "status", "answer", "reason"}` |
| `telegram_close` | `id` | `{"closed": true\|false}` |
| `telegram_status` | – | open questions, presence, host status |

`status` is `answered` (with `answer.text`, maybe `answer.button` and `answer.images`), `open`
(call `telegram_wait` again), `closed` or `expired` (with `reason`, e.g. `answered_in_app`).
Photos in answers come back as image content the model can look at (up to 8 MB in total per
result) and as local file paths.

Tool calls never take longer than about 50 seconds, which stays below typical MCP client
timeouts. To wait longer, the model calls `telegram_wait` repeatedly.

Messages show the MCP client's name as the agent (e.g. `claude-ai`). There is no project
folder, so pass `project` if you want one in the header.

Errors (relay unreachable, secret check, invalid arguments) come back as tool results with
`isError: true`, never as a crash of the server.

## Protocol details

- Transport: stdio, one JSON-RPC 2.0 message per line (UTF-8, `\n`).
- Methods: `initialize` (echoes the client's protocol version), `ping`, `tools/list`,
  `tools/call`; notifications are accepted and ignored.
- Tool calls run in their own threads, so a waiting call does not block pings or other calls.
- Each server process uses its own session key (`mcp-…`), so its questions are grouped.
