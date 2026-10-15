# Daily report task prompt (template)

Use this as the prompt of a daily scheduled task in Claude Code (e.g. Claude Desktop → Scheduled,
every day at 08:00). Replace the two placeholders first:

- `{{TG}}`: how to run `tg`, as printed by `tg install` (e.g. `py -3 "C:/Users/you/.claude/skills/telegram/tg.py"`
  or `python3 "/home/you/.claude/skills/telegram/tg.py"`)
- `{{PROJECTS}}`: the folder that contains your git repositories

Switch the report on once with `tg daily --on` (or `/daily on` in the bot). If it does not arrive by
08:15, the relay sends a short fallback report itself.

---

You write the user's daily report and send it on Telegram. Read and report only: no project work,
no file changes, no commits.

`TG` = `{{TG}}`

0. Run `TG status`. If it shows `"daily": "off"`, the report is switched off: stop right away and
   send nothing.
1. Turn off your own done pings: `TG session --done-delay off`
2. Collect the facts (takes about 10–60 s):
   `python3 "{{PROJECTS}}/telegram-bridge/integrations/daily-report/collect.py" --projects "{{PROJECTS}}"`
   (on Windows `py -3` instead of `python3`; adjust the path if telegram-bridge lives elsewhere)
3. If the tools are available (otherwise leave the point out, do not guess):
   - The Claude usage of this account (the app's usage tool); name the account.
   - The app's sessions (list_sessions): which ones wait for the user or ran since the last report
     (title, short).
4. Write the report in the user's language, for a phone, at most ~25 lines, plain text without tables:
   - First anything unusual (⚠️), if there is: host status older than 2 h or not ok, quota above
     80 %, a failed git fetch, open questions older than 12 h.
   - Work: one line per project with commits: count + gist in keywords (no hashes).
   - Open: open Telegram questions (#code, project), sessions waiting for the user.
   - Quotas: Claude (account), Codex (% of the week, reset).
   - Server: host status in one line.
   Missing values as "unknown". No secrets, no personal data of other people.
5. Send it (text on stdin):
   ```
   TG daily - <<'EOF'
   …report…
   EOF
   ```
   Exit 0 = sent (also when tg says the report is switched off). On exit 4 (relay unreachable)
   retry once after 2 minutes; otherwise the relay sends its fallback report at 08:15.
