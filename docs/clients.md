# The `tg` client

`client/tg.py` is a single Python file (3.10+, standard library only). Agents, hooks, scripts
and the MCP server all use it. It works the same on Windows, macOS and Linux.

## Install

```sh
python3 client/tg.py install --relay https://tg.example.com --name laptop --host-label Laptop
```

Windows: `py -3 client\tg.py install …`, or `.\install.ps1 -Relay https://tg.example.com`.

`install` does everything at once and is safe to run again (also to update):

1. copies `tg.py` to `~/.claude/skills/telegram/tg.py` and writes the Claude Code skill
   (`SKILL.md`) and `AGENT-INSTRUCTIONS.md` next to it, with the exact command to run `tg`
2. adds three hooks to `~/.claude/settings.json` (backup: `settings.json.bak-telegram`,
   your other settings stay untouched), see [claude-code.md](claude-code.md)
3. adds one line to `~/.claude/CLAUDE.md` pointing to the skill
4. first run only: creates the device config and asks you in Telegram to approve the device

Without Claude Code, `install` is still the easiest way; the Claude parts just sit unused.
Manual alternative:

```sh
python3 tg.py setup --relay https://tg.example.com --name laptop --host-label Laptop
python3 tg.py register      # prints a 4-digit code; approve it in Telegram
python3 tg.py selftest      # ok: laptop -> https://tg.example.com
```

### Device approval

`tg register` (also run by `install`) sends the device name and the SHA-256 of its token to
the relay. The bot shows *New device "laptop (Laptop)" … Confirmation code: 4821* with
**Approve** / **Reject**. Approve only if your terminal shows the same code: anyone can send
a registration request, the code is how you recognise yours. Requests expire after a day.

### Config file

`%APPDATA%\telegram-bridge\config.json` (Windows) or `~/.config/telegram-bridge/config.json`;
override with `TG_CONFIG`.

```json
{
  "relay_url": "https://tg.example.com",
  "token": "…",
  "name": "laptop",
  "host_label": "Laptop",
  "account_labels": {"you@example.com": "work"}
}
```

`account_labels` maps Claude account e-mails to short labels shown in message headers (useful
with several Claude accounts). The token never leaves the machine except as a bearer token to
your relay; any process of your user can read it, which is intended (every agent may reach you).

## Commands

`tg` below means `python3 ~/.claude/skills/telegram/tg.py` (Windows: `py -3 …`), or the
path in your clone.

| Command | Purpose | Output |
|---|---|---|
| `tg notify TEXT` | report, no answer expected | message id |
| `tg ask TEXT [--buttons "A,B,C"] [--wait S]` | question | `<id> <code>`; with `--wait` then the result |
| `tg wait ID [--timeout S]` | wait for the answer (default 24 h) | one JSON line |
| `tg close ID` | withdraw your question | JSON |
| `tg done [TEXT]` | "finished" ping, for agents without hooks | message id |
| `tg status` | open questions, presence (`away`/`present`), host status, daily switch | JSON |
| `tg daily TEXT \| --on \| --off` | send the daily report (once per day) / switch it | message id |
| `tg session --done-delay MIN\|off\|default` | done ping timing for this Claude session | JSON |
| `tg selftest` | check config, relay and token | `ok: …` |
| `tg register` | ask for approval of this device again | code |
| `tg setup --relay URL --name NAME` | create token and config (`--force` replaces) | name + hash |
| `tg install [--relay URL --name N --host-label L]` | install / update everything | summary |
| `tg mcp` | run as MCP server over stdio, see [mcp.md](mcp.md) | – |
| `tg mcp-config` | print the MCP entry for Claude Desktop | JSON |
| `tg hook stop\|notification\|user-prompt` | Claude Code hooks (JSON on stdin) | – |

Text arguments accept `-` to read from stdin (`tg notify - < report.txt`).

Common options for `notify`, `ask`, `done`, `daily`:

| Option | Meaning |
|---|---|
| `--title T` | short subject in the header |
| `--project P` | project shown in the header (default: name of the current folder) |
| `--agent A` | agent name shown in the header (`Claude` inside Claude Code, else `Agent`) |
| `--session K` | stable session key (Claude Code sets it); groups questions and pings |
| `--image PATH` | attach a PNG/JPEG/WEBP (≤ 10 MB), repeatable, up to 10 |

### Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | ok / answered | `tg wait` prints `{"status": "answered", "answer": {"text", "button", "images"}}` |
| 1 | usage, config or request error | read stderr |
| 2 | wait timed out, question still open | wait again later with the same id |
| 3 | question closed or expired | JSON `reason`: `answered_in_app`, `expired`, `closed_by_agent`, `failed` |
| 4 | relay unreachable (after 3 tries) | fall back to asking in your own UI |
| 5 | blocked: the text looks like a secret | nothing was sent; remove it and retry |

Images in answers are downloaded to a temp folder (`telegram-bridge`, kept 7 days) and listed
as local paths in `answer.images`.

### Waiting without blocking

`tg wait` long-polls the relay in 50-second chunks and survives relay restarts and short
outages until its timeout. Agents that can run background commands (Claude Code) start it in
the background and are woken when it exits. Others use `tg ask … --wait 600` in chunks, or the
MCP tools.

## Other agents (Codex & co.)

Give the agent [`AGENT-INSTRUCTIONS.md`](../integrations/agents/AGENT-INSTRUCTIONS.md) (installed
next to `tg.py` with the right command filled in). It explains when to ask, how to wait, the
exit codes and the rules (no secrets, short questions, stable `--session`). Agents without
hooks send `tg done "summary"` when they finish.

## The secret check

Before sending, `tg` checks all outgoing text (message, title, buttons, origin) for private
key blocks, GitHub/Slack/OpenAI-style tokens, AWS keys, JWTs, Telegram bot tokens,
`password=`/`token=`/`api_key=` assignments, hex strings of 40+ characters and long random
strings of mixed case and digits. A hit means exit 5 and nothing is sent. Hooks replace the
summary with "(summary withheld: possible secret)". Use 7-character commit hashes: a full
40-character hash looks like a token. Images are not inspected. See [security.md](security.md).
