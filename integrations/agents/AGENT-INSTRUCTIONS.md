# Reaching your human on Telegram (telegram-bridge)

You can send your human short messages on Telegram and receive their answers with the
`tg` command. Use it when they are away from the computer and you need a decision, or to
report that a long task is finished. Write in the language your human uses with you.

`tg` below means: `{{TG}}` (filled in by `tg install`; otherwise run `client/tg.py` from the
telegram-bridge repository). It needs a config created once with `tg setup` + `tg register`;
`tg selftest` checks it.

## Commands

| Command | What it does | Output |
|---|---|---|
| `tg notify "TEXT" [--title T] [--image PATH]...` | Report, no answer expected | message id |
| `tg ask "TEXT" [--buttons "A,B"] [--title T] [--session K] [--image PATH]...` | Question | `<id> <code>` |
| `tg done ["TEXT"]` | "Finished" ping (agents without Claude Code hooks) | message id |
| `tg wait ID [--timeout SECONDS]` | Wait for the answer (default 24 h) | one JSON line |
| `tg ask ... --wait SECONDS` | Ask and wait in one call | `<id> <code>`, then the JSON line |
| `tg close ID` | Withdraw your question (no longer needed) | JSON |
| `tg status` | Open questions and whether your human is away (`presence`) | JSON |

Common options: `--title` (short subject, shown in the header), `--project` (default: name
of the current folder), `--agent` (e.g. `Codex`), `--session` (see below).

Exit codes:

| Code | Meaning | What to do |
|---|---|---|
| 0 | OK / answered | `tg wait` prints `{"status": "answered", "answer": {"text": ..., "button": ..., "images": [...]}}` |
| 1 | Usage, config or request error | Read the message on stderr |
| 2 | Wait timed out, question still open | Wait again later with the same id, or ask in your own UI |
| 3 | Question closed or expired | JSON `reason`: `answered_in_app` (they answered you elsewhere, carry on), `expired` (24 h passed), `closed_by_agent`, `failed` |
| 4 | Relay unreachable | Fall back to asking in your own UI |
| 5 | Blocked: text looks like a secret | Nothing was sent. Remove the secret and send again |

## Rules

1. **Ask in your normal UI first.** Use `tg ask` only when your human said they are away,
   `tg status` shows `"presence": "away"`, they asked you to use Telegram, or you are in the
   middle of long autonomous work and need a decision while you continue elsewhere.
2. **Keep it short.** One question per message, enough context to answer from a phone.
   Offer buttons for clear choices (max 8, max 32 characters each).
3. **Never send secrets:** passwords, keys, tokens, signing data, personal data of end users.
   `tg` blocks common secret patterns (exit 5), but that is only a safety net.
4. **Pick a session key and keep it.** `--session` identifies you across calls (Claude Code
   sets it automatically). Use something stable, e.g. `codex-<project>-<date>`.
5. **Wait without blocking yourself.** If your tool can run a command in the background and
   resume you when it exits, start `tg wait ID` that way. Otherwise use `tg ask ... --wait 600`
   in chunks while you have nothing else to do.
6. **On exit 2 or `expired`, do not guess the answer.** Do independent work, then ask again
   in your own UI. On `answered_in_app`, continue with what your human told you there.
7. **Report sparingly.** `tg notify` for milestones or "finished", not for every step.
   Use short commit hashes (7 characters); a full 40-character hash trips the secret check.
8. Images: PNG/JPEG/WEBP up to 10 MB with `--image` (repeatable). Only send images you made
   or looked at that show no secrets or personal data. Images in answers are downloaded and
   listed as local paths in `answer.images`.

## Example

```
$ tg ask "Migration 0042 drops column legacy_id. Run it on production now?" --buttons "Yes,No,Later" --title "DB migration"
0b5e…-…-… 482
$ tg wait 0b5e…-…-…          # in the background
{"status": "answered", "answer": {"text": "Later", "button": "Later", "answered_at": 1792...}, "reason": null}
```

Your human answers on Telegram by tapping a button, replying to the message, or writing
`#482 <answer>`.
