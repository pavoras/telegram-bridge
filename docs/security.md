# Security

telegram-bridge lets software on your computers send you messages and receive your answers.
Your answers are instructions to your agents, so the important questions are: who can send
answers, who can send messages, and what can leak.

## Who can answer

- The relay serves exactly one Telegram chat: the one paired with the one-time code or set in
  `TG_ALLOWED_CHAT_ID`. Updates from any other chat, from groups, or from a different sender
  are dropped without a reply.
- Buttons are checked against chat, sender, message and the current state of the question.
- Anyone who has your Telegram account (or an unlocked phone with it) can answer your agents.
  Protect the account (two-step verification, passcode).

## Who can send

- Every computer has its own random token (256 bits), created locally. The relay stores only
  its SHA-256 and compares in constant time. Rights per token: approved devices get `full`,
  extra tokens in the clients file can be limited (e.g. `report` + `host-status` for a monitor).
- New devices register without a token, but nothing happens until you tap **Approve** in your
  chat. The request shows a 4-digit code that the device also prints: approve only matching
  codes. One request per minute, at most three pending, they expire after a day.
- The token sits in a config file of your OS user. Every process running as that user can read
  it, including every agent. That is intended (any agent may reach you) but means a compromised
  account can message you and read answers to its own questions.
- The bot token lives only on the server (a secret file or `.env` with mode 600).

## What can leak

- Messages are visible to Telegram (bot chats are not end-to-end encrypted) and stored in the
  relay's database for 7 days.
- **Secret check:** `tg` refuses to send text that matches common secret formats (private keys,
  GitHub/Slack/OpenAI/AWS/Telegram tokens, JWTs, `password=…` assignments, long random or hex
  strings) and exits with code 5. Hook summaries that match are withheld. This catches
  mistakes, not a determined agent, and **images are not inspected**. The skill and agent
  instructions tell agents never to send secrets or personal data of other people.
- Logs contain metadata only: method, route, status and client name. No texts, no tokens, no ids.
- Done pings contain a summary of the agent's last message (≤ 600 characters). If your sessions
  handle sensitive material, turn pings off for them (`tg session --done-delay off`).

## Server hardening

The Compose file in `deploy/` runs the relay as an unprivileged user (UID 10010) in a
read-only container without capabilities, with `no-new-privileges`, memory, CPU and process
limits, a data volume as the only writable place and no published port. Caddy is the only
service reachable from outside; relay and proxy share an internal network.

In the relay:

- request bodies ≤ 64 KB (images ≤ 10 MB, checked by magic bytes), 15-second read timeout,
  at most 16 connections, 30 requests per minute per token
- incoming photos are fetched from Telegram by `file_id` only when an agent asks for them,
  size-limited and format-checked; uploads count against a 200 MB quota
- all text sent to Telegram is HTML-escaped
- error messages never contain the bot token (it is part of Telegram API URLs)

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security → Report a vulnerability) instead
of a public issue.
