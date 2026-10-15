#!/usr/bin/env python3
"""tg - let an agent reach its human on Telegram through a telegram-bridge relay.

Exit codes: 0 ok, 1 usage/config/request error, 2 wait timed out (question still open),
3 question closed or expired (JSON with "reason"), 4 relay unreachable, 5 blocked: possible secret.
"""
import argparse
import base64
import datetime
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

RETRY_DELAYS = (1, 2)
MAX_WAIT_CHUNK = 50
OUTAGE_PAUSE = 30
DEFAULT_WAIT_TIMEOUT = 24 * 3600
MAX_IMAGE = 10 * 1024 * 1024
DOWNLOAD_RETENTION = 7 * 24 * 3600
HOOK_BUDGET = 3
SPOOL_MAX = 200
SPOOL_MAX_AGE = 24 * 3600
SUMMARY_LIMIT = 600
BLOCKED = object()  # summary withheld because it may contain a secret; the relay says so in its language
# Prompts that start with a tag (<task-notification>, <scheduled-task …>, <agent-message …>, …) come from
# the system, not from the human (spike S3). Unknown tags count as automatic too.
AUTO_PROMPT = re.compile(r"^\s*<[A-Za-z][\w-]*")
OUR_HOOK = re.compile(r"telegram-bridge[\\/]+spikes[\\/]+hooklog\.py|skills[\\/]+telegram[\\/]+tg\.py")
CLAUDE_MD_LINE = ("- Telegram: to reach the user when they are away from the computer (questions, reports, images, "
                  "done-ping settings), use the `telegram` skill.")

SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"github_pat_\w{20,}")),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("jwt", re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")),
    ("telegram_token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("assignment", re.compile(r"(?i)(password|passwort|secret|token|api[_-]?key)\s*[:=]\s*\S{6,}")),
    ("hex", re.compile(r"\b[A-Fa-f0-9]{40,}\b")),
]
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9+=_/.-])[A-Za-z0-9+=_-]{40,}(?![A-Za-z0-9+=_/.-])")

EXIT_USAGE, EXIT_TIMEOUT, EXIT_CLOSED, EXIT_RELAY, EXIT_SECRET = 1, 2, 3, 4, 5


class TgError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_USAGE):
        super().__init__(message)
        self.exit_code = exit_code


class RelayHttpError(Exception):
    def __init__(self, status: int, error: str):
        super().__init__(f"{status} {error}")
        self.status = status
        self.error = error


def find_secret(texts: list) -> Optional[str]:
    """Return the name of the first secret pattern found in any text, never the match itself."""
    for text in texts:
        if not text:
            continue
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                return name
        for match in _LONG_TOKEN.finditer(text):
            token = match.group(0)
            if re.search(r"[a-z]", token) and re.search(r"[A-Z]", token) and re.search(r"\d", token):
                return "random"
    return None


def config_path() -> Path:
    if os.environ.get("TG_CONFIG"):
        return Path(os.environ["TG_CONFIG"])
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "telegram-bridge" / "config.json"
    return Path.home() / ".config" / "telegram-bridge" / "config.json"


def load_config() -> dict:
    path = config_path()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise TgError(f"no config at {path}; run: tg setup --relay URL --name NAME") from None
    except ValueError:
        raise TgError(f"config at {path} is not valid JSON") from None


def _claude_email() -> Optional[str]:
    email = os.environ.get("CLAUDE_CODE_USER_EMAIL")
    if email:
        return email
    try:
        with open(Path.home() / ".claude.json", encoding="utf-8") as f:
            return (json.load(f).get("oauthAccount") or {}).get("emailAddress")
    except (OSError, ValueError):
        return None


def detect_origin(cfg: dict, args) -> dict:
    email = _claude_email()
    account = None
    if email:
        account = (cfg.get("account_labels") or {}).get(email) or email.split("@")[0]
    origin = {
        "host": cfg.get("host_label") or platform.node(),
        "account": account,
        "project": getattr(args, "project", None) or (
            None if getattr(args, "no_project", False) else os.path.basename(os.getcwd().rstrip("\\/"))),
        "agent": getattr(args, "agent", None) or ("Claude" if os.environ.get("CLAUDECODE") == "1" else "Agent"),
        "session": getattr(args, "session", None) or os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "title": getattr(args, "origin_title", None),
    }
    return {k: v for k, v in origin.items() if v}


def sniff_image(data: bytes) -> Optional[tuple]:
    """(mime, extension) from the magic bytes; None if not PNG, JPEG or WEBP."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


class RelayClient:
    def __init__(self, url: str, token: str, timeout: Optional[float] = None):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _open(self, method: str, path: str, data: Optional[bytes], content_type: str, timeout: float):
        request = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": content_type})
        try:
            return urllib.request.urlopen(request, timeout=self.timeout or timeout)
        except urllib.error.HTTPError as e:
            with e:
                try:
                    error = json.load(e).get("error", "")
                except ValueError:
                    error = ""
            raise RelayHttpError(e.code, error) from None

    def _send_once(self, method: str, path: str, body: Optional[dict]) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        timeout = MAX_WAIT_CHUNK + 15 if "wait=" in path else 20
        with self._open(method, path, data, "application/json", timeout) as response:
            return json.load(response)

    def _raw_once(self, method: str, path: str, data: Optional[bytes], content_type: str) -> tuple:
        with self._open(method, path, data, content_type, 120) as response:
            return response.headers.get_content_type(), response.read()

    def request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        return self._retry(lambda: self._send_once(method, path, body))

    def raw(self, method: str, path: str, data: Optional[bytes] = None,
            content_type: str = "application/octet-stream") -> tuple:
        return self._retry(lambda: self._raw_once(method, path, data, content_type))

    def _retry(self, call):
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                return call()
            except RelayHttpError as e:
                if e.status not in (429, 500, 502, 503, 504):
                    raise TgError(f"relay rejected the request: {e.status} {e.error}") from None
                problem = f"relay error {e.status}"
            except (urllib.error.URLError, OSError, ValueError) as e:
                problem = f"relay unreachable ({type(e).__name__})"
            if attempt < attempts - 1:
                time.sleep(RETRY_DELAYS[attempt])
        raise TgError(f"{problem} after {attempts} attempts", EXIT_RELAY)


def _client(cfg: dict) -> RelayClient:
    if not cfg.get("relay_url") or not cfg.get("token"):
        raise TgError("config is missing relay_url or token")
    return RelayClient(cfg["relay_url"], cfg["token"])


def _check_secrets(texts: list) -> None:
    found = find_secret(texts)
    if found:
        raise TgError(f"blocked: the message looks like it contains a secret (pattern: {found}). "
                      "Nothing was sent. Remove it and try again.", EXIT_SECRET)


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def cmd_setup(args) -> int:
    path = config_path()
    if path.exists() and not args.force:
        raise TgError(f"config already exists at {path}; use --force to replace it (the old token stops working)")
    token = secrets.token_urlsafe(32)
    cfg = {"relay_url": args.relay.rstrip("/"), "token": token, "name": args.name,
           "host_label": args.host_label or platform.node(), "account_labels": {}}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    if os.name != "nt":
        os.chmod(path, 0o600)
    print(f"{args.name} sha256:{hashlib.sha256(token.encode()).hexdigest()}")
    return 0


def _read_images(paths: list) -> list:
    """Load and check the images before anything is sent: [(data, mime), ...]."""
    images = []
    for path in paths or []:
        try:
            size = os.path.getsize(path)
            if size > MAX_IMAGE:
                raise TgError(f"image too large (max 10 MB): {path}")
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            raise TgError(f"cannot read image {path}: {e.strerror}") from None
        kind = sniff_image(data)
        if kind is None:
            raise TgError(f"not a PNG, JPEG or WEBP image: {path}")
        images.append((data, kind[0]))
    return images


def _send(args, kind: str, key: Optional[str] = None) -> dict:
    if args.text == "-":
        args.text = sys.stdin.buffer.read().decode("utf-8").strip()
    cfg = load_config()
    origin = detect_origin(cfg, args)
    buttons = [b.strip() for b in args.buttons.split(",") if b.strip()] if getattr(args, "buttons", None) else None
    _check_secrets([args.text, args.title, *(buttons or []), *origin.values()])
    images = _read_images(getattr(args, "image", None))
    client = _client(cfg)
    media_ids = [json.loads(client.raw("POST", "/v1/media", data, mime)[1])["id"] for data, mime in images]
    body = {"kind": kind, "text": args.text, "idempotency_key": key or str(uuid.uuid4()), "origin": origin,
            "title": args.title, "session_key": origin.get("session"), "buttons": buttons,
            "images": media_ids or None}
    return client.request("POST", "/v1/messages", {k: v for k, v in body.items() if v is not None})


def cmd_notify(args) -> int:
    created = _send(args, "report")
    print(created["id"])
    return 0


def cmd_ask(args) -> int:
    created = _send(args, "question")
    print(f"{created['id']} {created['code']}")
    if args.wait is not None:
        sys.stdout.flush()
        return _wait(created["id"], args.wait)
    return 0


def _wait(message_id: str, timeout: float) -> int:
    code, payload = _wait_result(message_id, timeout)
    _print_json(payload)
    return code


def _wait_result(message_id: str, timeout: float) -> tuple:
    """(exit code, result) of waiting up to timeout seconds for the answer."""
    client = _client(load_config())
    deadline = time.time() + timeout
    while True:
        # Whole seconds, at least 1: a 0 s poll near the deadline would spin into the rate limit.
        chunk = min(MAX_WAIT_CHUNK, max(1, math.ceil(deadline - time.time())))
        try:
            result = client.request("GET", f"/v1/messages/{message_id}?wait={chunk}")
        except TgError as e:
            # A long background wait outlives relay restarts and short network outages.
            if e.exit_code != EXIT_RELAY or time.time() >= deadline:
                raise
            time.sleep(min(OUTAGE_PAUSE, max(0.0, deadline - time.time())))
            continue
        status = result["status"]
        if status == "answered":
            answer = dict(result["answer"])
            if answer.get("images"):
                answer["images"] = _download_images(client, message_id, answer["images"])
            return 0, {"status": status, "answer": answer, "reason": None}
        if status not in ("queued", "sent"):
            return EXIT_CLOSED, {"status": status, "answer": None, "reason": result.get("closed_reason") or status}
        if time.time() >= deadline:
            return EXIT_TIMEOUT, {"status": "open", "answer": None, "reason": "timeout"}


def download_dir() -> Path:
    return Path(os.environ.get("TG_DOWNLOAD_DIR") or Path(tempfile.gettempdir()) / "telegram-bridge")


def _download_images(client: RelayClient, message_id: str, media_ids: list) -> list:
    """Save the answer's images locally (kept 7 days) and return their paths."""
    folder = download_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        try:
            if old.is_file() and time.time() - old.stat().st_mtime > DOWNLOAD_RETENTION:
                old.unlink()
        except OSError:
            pass
    paths = []
    for index, media_id in enumerate(media_ids, 1):
        _, data = client.raw("GET", f"/v1/media/{media_id}")
        kind = sniff_image(data)
        path = folder / f"{message_id[:8]}-{index}.{kind[1] if kind else 'bin'}"
        path.write_bytes(data)
        paths.append(str(path))
    return paths


def cmd_wait(args) -> int:
    return _wait(args.id, args.timeout)


def cmd_close(args) -> int:
    _print_json(_client(load_config()).request("POST", f"/v1/messages/{args.id}/close", {}))
    return 0


def cmd_status(args) -> int:
    _print_json(_client(load_config()).request("GET", "/v1/status"))
    return 0


def cmd_selftest(args) -> int:
    cfg = load_config()
    client = _client(cfg)
    client.request("GET", "/v1/status")
    print(f"ok: {cfg['name'] if cfg.get('name') else 'client'} -> {cfg['relay_url']}")
    return 0


def cmd_done(args) -> int:
    print(_send(args, "done")["id"])
    return 0


WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def cmd_daily(args) -> int:
    """The daily report: one per day; a second run on the same day returns the first message.
    --on/--off switch the report (and the relay's 08:15 fallback); while off, a report is dropped."""
    if args.on or args.off:
        print("daily report: " + _client(load_config()).request("POST", "/v1/daily", {"enabled": args.on})["daily"])
        return 0
    if args.text is None:
        raise TgError("daily needs the report text ('-' for stdin) or --on/--off")
    day = datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    args.title = args.title or f"{WEEKDAYS[day.weekday()]} {day.day:02d}.{day.month:02d}."
    try:
        print(_send(args, "daily", key=f"daily-{day.isoformat()}")["id"])
    except TgError as e:
        if "409 daily_off" not in str(e):
            raise
        print("tg: the daily report is switched off (tg daily --on); nothing sent", file=sys.stderr)
    return 0


def cmd_session(args) -> int:
    key = args.session or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not key:
        raise TgError("no session id: pass --session")
    value = args.done_delay.strip().lower()
    if value in ("off", "default"):
        delay = "off" if value == "off" else None
    else:
        try:
            delay = round(float(value) * 60)
        except ValueError:
            raise TgError("--done-delay takes minutes, 'off' or 'default'") from None
    _print_json(_client(load_config()).request("POST", f"/v1/sessions/{urllib.parse.quote(key, safe='')}",
                                               {"done_delay": delay}))
    return 0


def cmd_register(args) -> int:
    cfg = load_config()
    body = {"name": cfg.get("name"), "sha256": hashlib.sha256(cfg.get("token", "").encode()).hexdigest(),
            "host": cfg.get("host_label")}
    code = _client(cfg).request("POST", "/v1/register", body).get("code")
    print(f"Request sent. Confirmation code: {code}\n"
          "Tap \"Approve\" in your Telegram chat with the bot only if it shows the same code, then run: tg selftest")
    return 0


# --- Claude Code hooks ------------------------------------------------------------
def _summary(text: Optional[str]) -> Optional[str]:
    if not text or not text.strip():
        return None
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if find_secret([text]):
        return BLOCKED
    return text if len(text) <= SUMMARY_LIMIT else text[:SUMMARY_LIMIT - 1] + "…"


def _hook_event(event: str, payload: dict, cfg: dict) -> Optional[dict]:
    key = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not key:
        return None
    cwd = str(payload.get("cwd") or "").rstrip("\\/")
    project = re.split(r"[\\/]", cwd)[-1] if cwd else None
    origin = detect_origin(cfg, argparse.Namespace(project=project, agent="Claude", session=key))
    origin = {k: ("?" if find_secret([v]) else v) for k, v in origin.items()}
    body = {"ts": time.time(), "origin": origin}
    if event == "stop":
        tasks = payload.get("background_tasks") or []
        body.update(type="stop", text=_summary(payload.get("last_assistant_message")),
                    busy=any(isinstance(t, dict) and t.get("status") == "running" for t in tasks))
        if body["text"] is BLOCKED:
            body.update(text=None, summary_blocked=True)
    elif event == "notification":
        if payload.get("notification_type", "permission_prompt") != "permission_prompt":
            return None
        body.update(type="permission", text=_summary(payload.get("message")))  # the relay adds "waiting since"
        if body["text"] is BLOCKED:
            body.update(text=None, summary_blocked=True)
    elif event == "user-prompt":
        prompt = str(payload.get("prompt") or "")
        title = payload.get("session_title")
        body.update(type="prompt", human=bool(prompt.strip()) and not AUTO_PROMPT.match(prompt),
                    title=title if isinstance(title, str) and title and not find_secret([title]) else None)
    else:
        return None
    return {"session": key, "body": {k: v for k, v in body.items() if v is not None}}


def _spooled() -> list:
    """Undelivered hook events, oldest first, as (file, event). One file per event, so parallel hooks
    never overwrite each other; at worst two hooks send the same event twice, which the relay tolerates."""
    folder = config_path().parent / "spool"
    entries = []
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if time.time() - event["body"].get("ts", 0) < SPOOL_MAX_AGE:
            entries.append((path, event))
        else:
            path.unlink(missing_ok=True)
    return entries[-SPOOL_MAX:]


def _spool(event: dict) -> None:
    folder = config_path().parent / "spool"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{event['body'].get('ts', time.time()):017.6f}-{uuid.uuid4().hex[:8]}.json"
    (folder / name).write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")


AWAY_CONTEXT = ("The user is away (they set it in the Telegram bot). If you need their decision or feedback, ask via the "
                "`telegram` skill (tg ask, then tg wait in the background) instead of only here in the app.")


def _deliver_events(cfg: dict, new: list) -> Optional[dict]:
    """Send spooled events first, then the new ones, all within one HOOK_BUDGET; spool what is left."""
    pending = _spooled() + [(None, event) for event in new]
    client = RelayClient(cfg["relay_url"], cfg["token"])
    deadline = time.monotonic() + HOOK_BUDGET
    last = None
    for index, (path, event) in enumerate(pending):
        client.timeout = deadline - time.monotonic()
        if client.timeout < 0.2:
            break
        try:
            last = client._send_once(
                "POST", f"/v1/sessions/{urllib.parse.quote(event['session'], safe='')}/events", event["body"])
        except RelayHttpError as e:
            if e.status == 429 or e.status >= 500:
                break  # relay trouble: try again with the next hook
        except (urllib.error.URLError, OSError, ValueError):
            break
        if path is not None:  # delivered, or rejected for good (4xx)
            path.unlink(missing_ok=True)
    else:
        return last
    for path, event in pending[index:]:
        if path is None:
            _spool(event)
    return None


def cmd_hook(args) -> int:
    """Claude Code hook: never fails; prints only the away notice for the prompt hook."""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        payload = json.loads(raw) if raw.strip() else {}
        cfg = load_config()
        event = _hook_event(args.event, payload if isinstance(payload, dict) else {}, cfg)
        response = _deliver_events(cfg, [event] if event else [])
        if args.event == "user-prompt" and event and (response or {}).get("presence") == "away":
            # Tells every working session, old ones included, that the user is reachable on Telegram only.
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                     "additionalContext": AWAY_CONTEXT}}))
    except Exception:
        pass
    return 0


# --- installation ----------------------------------------------------------------
def _install_hooks(path: Path, runner: str) -> None:
    settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    hooks = settings.setdefault("hooks", {})
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if not OUR_HOOK.search(str(h.get("command", "")))]
            if kept:
                groups.append(dict(group, hooks=kept))
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    for event, argument, matcher in (("Stop", "stop", None), ("Notification", "notification", "permission_prompt"),
                                     ("UserPromptSubmit", "user-prompt", None)):
        group = {"hooks": [{"type": "command", "command": f"{runner} hook {argument}", "timeout": 10}]}
        hooks.setdefault(event, []).append({"matcher": matcher, **group} if matcher else group)
    _write_atomic(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")


def _write_atomic(path: Path, text: str) -> None:
    """Back up the original once, then replace the file in one step (never a half-written file)."""
    backup = path.with_name(path.name + ".bak-telegram")
    if path.exists() and not backup.exists():
        shutil.copyfile(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _install_claude_md(path: Path) -> None:
    text = path.read_text(encoding="utf-8-sig") if path.exists() else "# Global rules\n"
    if "`telegram`" in text:
        return
    _write_atomic(path, text.rstrip("\n") + "\n" + CLAUDE_MD_LINE + "\n")


# --- MCP server (`tg mcp`) -------------------------------------------------------
# For agents without a shell or background commands (e.g. a desktop chat): the same relay calls as
# tools over stdio (JSON-RPC 2.0, one message per line). Waiting happens in short tool calls.

MCP_WAIT_MAX = 50
MCP_IMAGE_BUDGET = 8 * 1024 * 1024  # answer images sent inline as image content, in total
_ORIGIN_ARGS = {"title": {"type": "string", "description": "short subject shown in the header"},
                "project": {"type": "string", "description": "project or topic shown as the origin"},
                "images": {"type": "array", "items": {"type": "string"}, "maxItems": 10,
                           "description": "local PNG/JPEG/WEBP file paths, max 10 MB each"}}
MCP_TOOLS = [
    {"name": "telegram_notify",
     "description": "Send the user a short report on Telegram (no answer expected). Use sparingly. "
                    "Never include secrets or personal data of other people.",
     "inputSchema": {"type": "object", "required": ["text"],
                     "properties": {"text": {"type": "string"}, **_ORIGIN_ARGS}}},
    {"name": "telegram_ask",
     "description": "Ask the user a question on Telegram; the answer comes back to you. Give enough context to "
                    "answer on a phone; buttons for clear choices. Waits up to wait_seconds (default 50) for the "
                    "answer; if it is still open, call telegram_wait with the returned id.",
     "inputSchema": {"type": "object", "required": ["text"],
                     "properties": {"text": {"type": "string"},
                                    "buttons": {"type": "array", "items": {"type": "string", "maxLength": 32},
                                                "maxItems": 8},
                                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MCP_WAIT_MAX},
                                    **_ORIGIN_ARGS}}},
    {"name": "telegram_wait",
     "description": "Wait up to wait_seconds (default 50) for the answer to a telegram_ask question. Result "
                    "status: answered (answer.text, answer.button, images), open (call again), closed or "
                    "expired (do not continue the dependent work on a guess).",
     "inputSchema": {"type": "object", "required": ["id"],
                     "properties": {"id": {"type": "string"},
                                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MCP_WAIT_MAX}}}},
    {"name": "telegram_close",
     "description": "Close one of your open Telegram questions that is no longer needed.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}},
    {"name": "telegram_status",
     "description": "Open Telegram questions and the user's presence (\"away\" = ask on Telegram).",
     "inputSchema": {"type": "object", "properties": {}}},
]


def mcp_image_blocks(paths: list) -> list:
    blocks, budget = [], MCP_IMAGE_BUDGET
    for path in paths:
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue
        kind = sniff_image(data)
        if kind is None or len(data) > budget:
            continue
        budget -= len(data)
        blocks.append({"type": "image", "mimeType": kind[0], "data": base64.b64encode(data).decode()})
    return blocks


class McpServer:
    def __init__(self):
        self.agent = "MCP"
        self.session = f"mcp-{uuid.uuid4().hex[:12]}"

    def handle(self, request: dict) -> Optional[dict]:
        """One JSON-RPC message in, the reply out (None for notifications)."""
        method, rid = request.get("method"), request.get("id")
        params = request.get("params") or {}
        if "id" not in request:
            return None
        if method == "initialize":
            self.agent = str((params.get("clientInfo") or {}).get("name") or "MCP")[:40]
            version = params.get("protocolVersion")
            result = {"protocolVersion": version if isinstance(version, str) else "2025-06-18",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "telegram-bridge", "version": "1"},
                      "instructions": "Reach the user on Telegram when they are away from the computer."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": MCP_TOOLS}
        elif method == "tools/call":
            result = self._call(params.get("name"), params.get("arguments") or {})
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _call(self, name, arguments: dict) -> dict:
        try:
            payload, images = self._tool(name, arguments)
        except TgError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # never let a tool error end the server
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
                + mcp_image_blocks(images)}

    @staticmethod
    def _seconds(arguments: dict) -> int:
        value = arguments.get("wait_seconds", MCP_WAIT_MAX)
        if not isinstance(value, int) or isinstance(value, bool):
            raise TgError("wait_seconds must be a whole number")
        return max(0, min(MCP_WAIT_MAX, value))

    def _tool(self, name, arguments: dict) -> tuple:
        if name in ("telegram_notify", "telegram_ask"):
            text = arguments.get("text")
            if not isinstance(text, str) or not text.strip() or text.strip() == "-":
                raise TgError("text is required")
            buttons = arguments.get("buttons")
            if buttons is not None and (not isinstance(buttons, list) or not all(isinstance(b, str) for b in buttons)):
                raise TgError("buttons must be a list of strings")
            args = argparse.Namespace(text=text, title=arguments.get("title"), project=arguments.get("project"),
                                      agent=self.agent, session=self.session, image=arguments.get("images"),
                                      buttons=",".join(b.replace(",", " ") for b in buttons) if buttons else None,
                                      no_project=not arguments.get("project"))
            if name == "telegram_notify":
                return {"id": _send(args, "report")["id"], "status": "sent"}, []
            seconds = self._seconds(arguments)
            created = _send(args, "question")
            _, result = _wait_result(created["id"], seconds)
            return dict(result, id=created["id"], code=created["code"]), (result.get("answer") or {}).get("images", [])
        if name in ("telegram_wait", "telegram_close"):
            message_id = arguments.get("id")
            if not isinstance(message_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", message_id):
                raise TgError("id must be the id returned by telegram_ask")
            if name == "telegram_close":
                return _client(load_config()).request("POST", f"/v1/messages/{message_id}/close", {}), []
            _, result = _wait_result(message_id, self._seconds(arguments))
            return dict(result, id=message_id), (result.get("answer") or {}).get("images", [])
        if name == "telegram_status":
            return _client(load_config()).request("GET", "/v1/status"), []
        raise TgError(f"unknown tool {name}")


def cmd_mcp(args) -> int:
    server = McpServer()
    out = sys.stdout.buffer  # bytes: plain \n line ends on Windows too
    lock = threading.Lock()
    workers = []

    def write(reply: Optional[dict]) -> None:
        if reply is not None:
            with lock:
                out.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
                out.flush()

    sys.stdout.flush()
    for line in iter(sys.stdin.buffer.readline, b""):
        if not line.strip():
            continue
        try:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError
        except ValueError:
            write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if request.get("method") == "tools/call":
            # A waiting tool call must not block pings and other calls.
            worker = threading.Thread(target=lambda r=request: write(server.handle(r)), daemon=True)
            worker.start()
            workers.append(worker)
        else:
            write(server.handle(request))
    for worker in workers:
        worker.join()
    return 0


def cmd_mcp_config(args) -> int:
    """The entry for an MCP client's config (Claude Desktop: Settings > Developer > Edit Config)."""
    command = ["py", "-3"] if os.name == "nt" else [sys.executable or "python3"]
    entry = {"command": command[0], "args": command[1:] + [Path(__file__).resolve().as_posix(), "mcp"]}
    _print_json({"mcpServers": {"telegram": entry}})
    return 0


def cmd_install(args) -> int:
    claude = Path(args.claude_dir) if args.claude_dir else Path.home() / ".claude"
    skill = claude / "skills" / "telegram"
    skill.mkdir(parents=True, exist_ok=True)
    source, target = Path(__file__).resolve(), skill / "tg.py"
    if source != target.resolve():
        shutil.copyfile(source, target)
    runner = f'{"py -3" if os.name == "nt" else "python3"} "{target.as_posix()}"'
    repo = source.parent.parent
    relay = args.relay or (load_config().get("relay_url") if config_path().exists() else None)
    for template, name in ((repo / "integrations" / "claude" / "SKILL.md", "SKILL.md"),
                           (repo / "integrations" / "agents" / "AGENT-INSTRUCTIONS.md", "AGENT-INSTRUCTIONS.md")):
        if template.exists():
            text = template.read_text(encoding="utf-8")
        elif relay:  # installed from a single downloaded tg.py: the relay serves the templates
            try:
                with urllib.request.urlopen(f"{relay.rstrip('/')}/v1/client/{name}", timeout=20) as response:
                    text = response.read().decode("utf-8")
            except (urllib.error.URLError, OSError) as e:
                raise TgError(f"cannot download {name} from the relay ({type(e).__name__})") from None
        else:
            raise TgError(f"template missing: {template}; pass --relay URL")
        (skill / name).write_text(text.replace("{{TG}}", runner), encoding="utf-8", newline="\n")
    _install_hooks(claude / "settings.json", runner)
    _install_claude_md(claude / "CLAUDE.md")
    print(f"skill: {skill}\nhooks: {claude / 'settings.json'}\nrun tg as: {runner}\n"
          f"MCP (agents without a shell): {runner} mcp-config")
    if not config_path().exists():
        if not (args.relay and args.name):
            print("next: tg setup --relay URL --name NAME, then tg register")
            return 0
        cmd_setup(argparse.Namespace(relay=args.relay, name=args.name, host_label=args.host_label, force=False))
        cmd_register(args)
        return 0
    try:
        _client(load_config()).request("GET", "/v1/status")
        print("device: ok")
    except TgError as e:
        print(f"device: not working yet ({e}); if new, run: tg register")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tg", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="create a device token and config")
    p.add_argument("--relay", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--host-label")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_setup)

    for name, func, help_text in (("notify", cmd_notify, "send a report (no answer expected)"),
                                  ("ask", cmd_ask, "ask a question; prints '<id> <code>'"),
                                  ("done", cmd_done, "send a 'finished' ping (agents without hooks)"),
                                  ("daily", cmd_daily, "send the daily report (once per day)")):
        p = sub.add_parser(name, help=help_text)
        if name == "done":
            p.add_argument("text", nargs="?", default="")
        elif name == "daily":
            p.add_argument("text", nargs="?", help="'-' reads the text from stdin")
        else:
            p.add_argument("text", help="'-' reads the text from stdin")
        if name == "daily":
            p.add_argument("--date", help="YYYY-MM-DD, default today")
            switch = p.add_mutually_exclusive_group()
            switch.add_argument("--on", action="store_true", help="switch the daily report on")
            switch.add_argument("--off", action="store_true", help="switch it off (no report, no 08:15 fallback)")
        p.add_argument("--title")
        p.add_argument("--session")
        p.add_argument("--agent")
        p.add_argument("--project")
        p.add_argument("--image", action="append", metavar="PATH", help="PNG/JPEG/WEBP, max 10 MB, repeatable")
        if name == "ask":
            p.add_argument("--buttons", help="comma separated, at most 8")
            p.add_argument("--wait", type=float, metavar="SECONDS")
        p.set_defaults(func=func)

    p = sub.add_parser("wait", help="wait for the answer to a question")
    p.add_argument("id")
    p.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("close", help="close one of your questions")
    p.add_argument("id")
    p.set_defaults(func=cmd_close)

    sub.add_parser("status", help="open questions and presence").set_defaults(func=cmd_status)
    sub.add_parser("selftest", help="check config, relay and token").set_defaults(func=cmd_selftest)
    sub.add_parser("register", help="ask for approval of this device in Telegram").set_defaults(func=cmd_register)

    p = sub.add_parser("session", help="when this session's done ping is sent")
    p.add_argument("--done-delay", required=True, metavar="MIN|off|default")
    p.add_argument("--session")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("hook", help="Claude Code hook (reads the hook JSON from stdin)")
    p.add_argument("event", choices=["stop", "notification", "user-prompt"])
    p.set_defaults(func=cmd_hook)

    sub.add_parser("mcp", help="run as an MCP server over stdio (for agents without a shell)").set_defaults(
        func=cmd_mcp)
    sub.add_parser("mcp-config", help="print the MCP server entry for Claude Desktop's config").set_defaults(
        func=cmd_mcp_config)

    p = sub.add_parser("install", help="install skill, hooks and CLAUDE.md line for Claude Code")
    p.add_argument("--claude-dir")
    p.add_argument("--relay")
    p.add_argument("--name")
    p.add_argument("--host-label")
    p.set_defaults(func=cmd_install)
    return parser


def main(argv: Optional[list] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TgError as e:
        print(f"tg: {e}", file=sys.stderr)
        return e.exit_code


if __name__ == "__main__":
    sys.exit(main())
