# Claude Code integration

`tg install` sets up everything for Claude Code. Run it once per computer (and operating
system user): the CLI, the desktop app and the IDE extensions share `~/.claude`, and all
Claude accounts you sign in with on that computer use the same setup.

## What gets installed

| Where | What |
|---|---|
| `~/.claude/skills/telegram/` | `SKILL.md` (the `telegram` skill), `tg.py`, `AGENT-INSTRUCTIONS.md` |
| `~/.claude/settings.json` | three hooks (below); your other settings stay as they are |
| `~/.claude/CLAUDE.md` | one line: use the `telegram` skill to reach the user when they are away |

The skill tells Claude when to use Telegram (only when you are away, when long autonomous work
needs a decision, or when you ask for it), how to ask (`tg ask`, then `tg wait` as a background
command, which wakes the session when you answer) and the rules (no secrets, short questions).

## Done pings

| Hook | Matcher | Effect |
|---|---|---|
| `Stop` | – | schedule "✅ done" with a summary of Claude's last message |
| `Notification` | `permission_prompt` | schedule "⏸ waiting for approval (since 14:02)" with the request |
| `UserPromptSubmit` | – | cancel the scheduled ping; a prompt typed by you also closes this session's open Telegram questions |

- The relay schedules the ping (default 10 minutes), so nothing runs on your computer in the
  meantime and a reboot loses nothing.
- No ping while the session still runs a background command or waits for a Telegram answer.
- Any new prompt cancels the pending ping. Automatic prompts (task notifications, scheduled
  tasks, messages from other sessions start with a tag like `<task-notification>`) cancel it
  too, but only a prompt you typed closes open Telegram questions (as "handled in the app").
- Change the timing by telling Claude, e.g. "ping me right away", "ping after 30 minutes",
  "no pings in this session". It runs `tg session --done-delay 0|30|off|default`.
- Hooks never block Claude: one attempt with a 3-second budget. If the relay is unreachable,
  the event goes to a local spool and is sent with the next hook call.
- Summaries that look like they contain a secret are withheld.

Replying to a done ping in Telegram does not reach the session (it is not waiting); the bot
tells you to answer in the app. With Remote Control (below) you can do that from the phone.

## Away mode

Send `/away` (or `/weg`) to the bot. From then on, every prompt in every Claude Code session
gets a short note: *The user is away … ask via the `telegram` skill*. Claude then asks you on
Telegram instead of in the app. `/back` (or `/da`) ends it. `/status` shows the current mode.

## Answering from the phone

- Tap a button, reply to the question (swipe), or write `#482 your answer`.
- With exactly one open question, any plain message is the answer.
- With several, the bot asks which question you mean.
- Photos and albums work as answers (with or without a caption).
- `/open` lists open questions with their codes.

## Remote Control

The Claude desktop app can connect new sessions to Remote Control (Settings). Then a done ping
on your phone is one tap away from the session itself in the Claude mobile app. telegram-bridge
does not switch Remote Control on its own.

## Daily report

An optional morning summary: commits of the last day in all your repositories (after
`git fetch`, so work pushed from other machines counts too), open Telegram questions, sessions
waiting for you, Claude and Codex quota, and the server status from your monitor.

1. Switch it on: `/daily on` in the bot or `tg daily --on`.
2. Create a scheduled task in Claude Code (desktop app: Scheduled → New task, daily 08:00) with
   the prompt from [`integrations/daily-report/prompt.md`](../integrations/daily-report/prompt.md),
   placeholders filled in.
3. Click "Run now" once and grant the permissions it asks for (they are stored with the task).

The task runs [`collect.py`](../integrations/daily-report/collect.py) for the facts and sends
the written report with `tg daily`, at most once per day. If no report arrived by 08:15 (in
`TG_TIMEZONE`), the relay sends a short fallback with open questions, failed deliveries and the
host status, so you notice when the computer was off. `/daily off` stops both.

Scheduled tasks run only while the desktop app is open; a missed run starts on the next launch.

## Uninstall

Remove the three `tg … hook` entries from `~/.claude/settings.json` (or restore
`settings.json.bak-telegram`), the `telegram` line from `~/.claude/CLAUDE.md` and the folder
`~/.claude/skills/telegram/`. The device config is in `%APPDATA%\telegram-bridge` or
`~/.config/telegram-bridge`.
