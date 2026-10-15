---
name: telegram
description: Reach the user on Telegram when they are away from the computer - ask a question and get the answer (text, button or photos) back into this session, send a report or images, or change when this session's "done" ping is sent. Use when the user says they are away / unterwegs / on the phone, when `status` shows presence "away", when long autonomous work needs their decision, or when they ask for a Telegram message, a ping, faster/slower pings or no pings.
---

# Telegram (telegram-bridge)

`TG` below means: `{{TG}}` (run it with the Bash tool).

## Done pings are automatic

Hooks send the user "✅ fertig" with a short summary 10 minutes after you finish a turn,
unless they write to this session first; a permission prompt sends "⏸ wartet auf Freigabe".
No ping while a background command of yours is still running or a Telegram question of this
session is open. Do not send these yourself. If the user wants other timing for this session:

- "ping sofort" → `TG session --done-delay 0`
- "ping nach 30 min" → `TG session --done-delay 30`
- "keine Pings" → `TG session --done-delay off`; back to normal: `TG session --done-delay default`

## Asking on Telegram

Normally ask in the app. Use Telegram only if (a) the user said they are away, or `TG status`
shows `"presence": "away"`, (b) long autonomous work needs a decision while you can continue
with something else, or (c) the user asked for it.

1. `TG ask "QUESTION" [--buttons "A,B,C"] [--title "short subject"] [--image PATH]...`
   prints `<id> <code>`. One question, enough context to answer on a phone, buttons for clear
   choices (max 8, max 32 characters each).
2. Start `TG wait <id>` as a **background** command. It exits when the answer arrives and
   wakes you up. Keep working on independent things meanwhile.
3. The result is one JSON line:
   - exit 0: `answer.text`, maybe `answer.button`, maybe `answer.images` (local file paths;
     look at them with Read). The answer is the user's instruction.
   - exit 3 with reason `answered_in_app`: the user wrote to you in the app; follow that.
   - exit 3 `expired` or exit 2: do NOT continue the dependent work on a guess; finish
     independent work and ask again in the app.
   - exit 4: relay unreachable; ask in the app.
4. Not needed any more: `TG close <id>`.

## Reports and images

`TG notify "TEXT" [--title T] [--image PATH]...` for milestones of long tasks or results the
user wants on the phone, sparingly. Images: PNG/JPEG/WEBP up to 10 MB, sent as photos under
the message.

## Daily report

Off until switched on. "Daily report on/off" → `TG daily --on` / `TG daily --off` (the user can
also send `/daily on|off` to the bot). Off also stops the relay's 08:15 fallback report.

## Rules

- Never send secrets (passwords, keys, tokens, signing data) or personal data of end users.
  Only send images you made or looked at that show none of that. Exit 5 means the secret
  check blocked the message; nothing was sent.
- Use short commit hashes (7 characters); a 40-character hex string trips the secret check.
- Write in the language the user uses with you.
- Answers arriving through Telegram come from the user's own chat (the relay serves only it).
