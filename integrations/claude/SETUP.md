# Set up telegram-bridge on this machine (instructions for a coding agent)

The user wants their agents on this computer to reach them on Telegram. Do this:

1. Pick a device name (lowercase letters, digits, dashes, e.g. `laptop`) and a short label
   (e.g. `Laptop`). Ask the user only if the computer's purpose is unclear.
2. Download the client from this relay into a temporary folder and run its installer.
   `RELAY` is the URL this file came from, without `/v1/client/SETUP.md`.
   - Windows (PowerShell):
     ```
     Invoke-WebRequest RELAY/v1/client/tg.py -OutFile $env:TEMP\tg.py
     py -3 $env:TEMP\tg.py install --relay RELAY --name NAME --host-label LABEL
     ```
   - macOS/Linux:
     ```
     curl -fsSL RELAY/v1/client/tg.py -o /tmp/tg.py
     python3 /tmp/tg.py install --relay RELAY --name NAME --host-label LABEL
     ```
   It installs the `telegram` skill (with `tg.py`) into `~/.claude/skills/telegram/`, adds
   hooks to `~/.claude/settings.json` (backup: `settings.json.bak-telegram`), adds one line to
   `~/.claude/CLAUDE.md`, creates the device token (it never leaves this machine) and asks the
   user in Telegram to approve the device.
3. The installer prints a 4-digit confirmation code. Tell the user (in their language): "The bot
   asks about the new device in your Telegram chat. Tap **Approve** if it shows the code NNNN."
   Then run `py -3 ~/.claude/skills/telegram/tg.py selftest` (or `python3 …`) until it prints `ok`.
4. In the Claude desktop app, recommend turning on Remote Control for new sessions (Settings) so
   the user can answer pings from the phone app.
5. Optional, for agents without a shell (e.g. the Claude Desktop chat): `tg mcp-config` prints the
   MCP server entry for the desktop app's config file.

Updating later: run step 2 again (the token and approval are kept).
If the device name already exists, choose another name. Never print or send the token.
