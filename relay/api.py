"""HTTP API for clients: authentication, rights, rate limiting and message endpoints."""
import hashlib
import hmac
import json
import logging
import os
import re
import socketserver
import uuid
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, NamedTuple, Optional
from urllib.parse import parse_qs, urlparse

from relay.i18n import t
from relay.store import OPEN, Store
from relay.telegram import TelegramClient, TelegramError

MAX_BODY = 64 * 1024
DRAIN_LIMIT = 1024 * 1024
MAX_CONNECTIONS = 16
SOCKET_TIMEOUT = 15  # a client that stops sending mid-request is dropped instead of holding a slot
RATE_LIMIT = 30
RATE_WINDOW = 60.0
MAX_WAIT = 50.0
POLL_INTERVAL = 0.5
MAX_BUTTONS = 8
MAX_BUTTON_LEN = 32

MAX_EVENT_TEXT = 4000
MAX_MEDIA = 10 * 1024 * 1024
MEDIA_QUOTA = 200 * 1024 * 1024
MAX_IMAGES = 10
MEDIA_PATH = re.compile(r"^/v1/media/([0-9a-f]{32})$")
# Public files for setting up a new machine (the client is open source, nothing secret in them).
CLIENT_PATH = re.compile(r"^/v1/client/(tg\.py|SKILL\.md|AGENT-INSTRUCTIONS\.md|SETUP\.md)$")
MAX_DONE_DELAY = 7 * 86400
EVENT_TYPES = ("stop", "permission", "prompt")

MESSAGE_PATH = re.compile(r"^/v1/messages/([0-9a-f-]{36})(/close)?$")
DEVICE_NAME = re.compile(r"^[a-z0-9-]{1,32}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
SESSION_PATH = re.compile(r"^/v1/sessions/([A-Za-z0-9_.:-]{1,100})(/events)?$")

log = logging.getLogger("relay.api")


class Clients:
    """Device tokens, stored as SHA-256 hashes: {"<client_id>": {"sha256": "...", "rights": [...]}}.
    The file is optional (monitors with limited rights live there); devices approved in Telegram
    (store) are added with full rights."""

    def __init__(self, path: Optional[str], store: Optional[Store] = None):
        self._clients = {}
        if path:
            with open(path, encoding="utf-8") as f:
                self._clients = json.load(f)
        self._store = store

    def names(self) -> set:
        return set(self._clients)

    def authenticate(self, authorization_header: Optional[str]):
        if not authorization_header or not authorization_header.startswith("Bearer "):
            return None
        digest = hashlib.sha256(authorization_header[7:].strip().encode()).hexdigest()
        entries = [(c, str(e.get("sha256", "")), list(e.get("rights", []))) for c, e in self._clients.items()]
        if self._store is not None:
            entries += [(name, sha, ["full"]) for name, sha in self._store.approved_devices().items()
                        if name not in self._clients]
        for client_id, sha, rights in entries:
            if hmac.compare_digest(digest, sha):
                return client_id, rights
        return None


class Raw(NamedTuple):
    content_type: str
    data: bytes


def sniff_image(data: bytes) -> Optional[str]:
    """MIME type from the magic bytes; None if not PNG, JPEG or WEBP."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class ApiError(Exception):
    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status = status
        self.error = error


class _RateLimiter:
    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, client_id: str, now: float) -> bool:
        with self._lock:
            hits = self._hits[client_id]
            while hits and hits[0] <= now - RATE_WINDOW:
                hits.popleft()
            if len(hits) >= RATE_LIMIT:
                return False
            hits.append(now)
            return True


def _optional_str(body: dict, field: str, limit: int) -> Optional[str]:
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ApiError(400, field)
    return value


def _validate_origin(body: dict) -> dict:
    origin = body.get("origin") or {}
    if not isinstance(origin, dict) or len(origin) > 10 or not all(
            isinstance(k, str) and isinstance(v, str) and len(v) <= 200 for k, v in origin.items()):
        raise ApiError(400, "origin")
    return origin


def _validate_event(body: dict) -> dict:
    if body.get("type") not in EVENT_TYPES:
        raise ApiError(400, "type")
    ts = body.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        raise ApiError(400, "ts")
    text = _optional_str(body, "text", MAX_EVENT_TEXT)
    if body.get("summary_blocked") is True:
        text = t("summary_blocked")  # the client found a possible secret and sent no summary
    return {"kind": body["type"], "ts": float(ts), "origin": _validate_origin(body),
            "text": text, "title": _optional_str(body, "title", 200),
            "human": body.get("human") is True, "busy": body.get("busy") is True}


def _validate_done_delay(body: dict) -> Optional[int]:
    value = body.get("done_delay")
    if value == "off":
        return -1
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_DONE_DELAY:
        raise ApiError(400, "done_delay")
    return value


def _validate_message(body: dict, rights: list) -> dict:
    kind = body.get("kind")
    if kind not in ("report", "question", "done", "daily"):
        raise ApiError(400, "kind")
    if "full" not in rights and kind not in rights:
        raise ApiError(403, "forbidden")
    text = body.get("text", "" if kind == "done" else None)  # a done ping may be just its header
    if not isinstance(text, str) or (kind != "done" and not text.strip()):
        raise ApiError(400, "text")
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not 0 < len(key) <= 100:
        raise ApiError(400, "idempotency_key")
    origin = _validate_origin(body)
    buttons = body.get("buttons")
    if buttons is not None:
        if (kind != "question" or not isinstance(buttons, list) or len(buttons) > MAX_BUTTONS or not all(
                isinstance(b, str) and 0 < len(b) <= MAX_BUTTON_LEN for b in buttons)):
            raise ApiError(400, "buttons")
    images = body.get("images")
    if images is not None and (not isinstance(images, list) or len(images) > MAX_IMAGES
                               or not all(isinstance(i, str) for i in images)):
        raise ApiError(400, "images")
    return {"kind": kind, "text": text, "idempotency_key": key, "origin": origin, "images": images or None,
            "title": _optional_str(body, "title", 200), "session_key": _optional_str(body, "session_key", 100),
            "buttons": buttons or None}


def _public(msg: dict) -> dict:
    return {"id": msg["id"], "code": msg["code"], "status": msg["status"], "answer": msg["answer"],
            "closed_reason": msg["closed_reason"]}


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind resolves the host name (socket.getfqdn), which can block for half a
        # minute without working reverse DNS (seen on macOS). The name is only used for CGI.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_server(store: Store, clients: Clients, host: str, port: int, health: Callable[[], dict],
                tg: Optional[TelegramClient] = None, media_dir: Optional[str] = None,
                client_dir: Optional[str] = None) -> ThreadingHTTPServer:
    limiter = _RateLimiter()
    upload_lock = threading.Lock()  # quota check, file and row as one step
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            self.timeout = SOCKET_TIMEOUT
            super().setup()

        def log_message(self, *args):
            pass

        def _send(self, status: int, payload) -> None:
            raw = payload if isinstance(payload, Raw) else Raw("application/json", json.dumps(payload).encode())
            body = raw.data
            self.send_response(status)
            self.send_header("Content-Type", raw.content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _raw_body(self, limit: int) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                if length <= limit + DRAIN_LIMIT:
                    while length > 0:
                        length -= len(self.rfile.read(min(length, 65536)) or b"x" * length)
                else:
                    self.close_connection = True
                raise ApiError(413, "too_large")
            return self.rfile.read(length) if length else b""

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                if length <= DRAIN_LIMIT:
                    self.rfile.read(length)
                else:
                    self.close_connection = True
                raise ApiError(413, "too_large")
            if not length:
                return {}
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                raise ApiError(400, "json") from None
            if not isinstance(body, dict):
                raise ApiError(400, "json")
            return body

        def _handle(self, method: str) -> None:
            url = urlparse(self.path)
            template, client_id, status = url.path, "-", 500
            if not slots.acquire(blocking=False):
                self.close_connection = True
                log.info("%s %s %s %s", method, template, 503, client_id)
                self._send(503, {"error": "busy"})
                return
            try:
                match = MESSAGE_PATH.match(url.path)
                if match:
                    template = "/v1/messages/{id}" + (match.group(2) or "")
                session = SESSION_PATH.match(url.path)
                if session:
                    template = "/v1/sessions/{key}" + (session.group(2) or "")
                media = MEDIA_PATH.match(url.path)
                if media:
                    template = "/v1/media/{id}"
                if method == "GET" and url.path == "/v1/health":
                    payload = health()
                    status = 200 if payload.get("ok") else 503
                elif method == "GET" and CLIENT_PATH.match(url.path):
                    status, payload = self._client_file(CLIENT_PATH.match(url.path).group(1))
                elif method == "POST" and url.path == "/v1/register":
                    status, payload = self._register()
                else:
                    auth = clients.authenticate(self.headers.get("Authorization"))
                    if auth is None:
                        raise ApiError(401, "unauthorized")
                    client_id, rights = auth
                    if not limiter.allow(client_id, time.time()):
                        raise ApiError(429, "rate_limited")
                    if session and method == "POST":
                        status, payload = self._session(session, client_id, rights)
                    elif method == "POST" and url.path == "/v1/media":
                        status, payload = self._upload(client_id, rights)
                    elif media and method == "GET":
                        status, payload = self._download(media.group(1), client_id)
                    else:
                        status, payload = self._route(method, url, match, client_id, rights)
            except ApiError as e:
                status, payload = e.status, {"error": e.error}
            except TimeoutError:
                status, payload = 408, None  # client stalled: drop the connection without an answer
                self.close_connection = True
            except Exception:
                log.exception("unhandled error")
                status, payload = 500, {"error": "internal"}
            finally:
                slots.release()
            log.info("%s %s %s %s", method, template, status, client_id)
            if payload is not None:
                self._send(status, payload)

        def _route(self, method, url, match, client_id, rights):
            if method == "POST" and url.path == "/v1/messages":
                fields = _validate_message(self._body(), rights)
                if fields["kind"] == "daily" and not store.daily_enabled():
                    raise ApiError(409, "daily_off")
                for media_id in fields["images"] or []:
                    owned = store.get_media(media_id)
                    if owned is None or owned["direction"] != "out" or owned["client_id"] != client_id:
                        raise ApiError(400, "images")
                created = store.create_message(client_id, fields["kind"], fields["text"], fields["origin"],
                                               fields["idempotency_key"], time.time(), title=fields["title"],
                                               session_key=fields["session_key"], buttons=fields["buttons"],
                                               images=fields["images"])
                return 201, created
            if match:
                msg = store.get_message(match.group(1))
                if msg is None or msg["client_id"] != client_id:
                    raise ApiError(404, "not_found")
                if method == "POST" and match.group(2):
                    self._body()
                    return 200, {"closed": store.close_question(msg["id"], "closed_by_agent", time.time())}
                if method == "GET" and not match.group(2):
                    return 200, _public(self._wait(msg, url))
            if method == "POST" and url.path == "/v1/host-status":
                if "full" not in rights and "host-status" not in rights:
                    raise ApiError(403, "forbidden")
                items = self._body().get("items")
                if not isinstance(items, dict) or len(items) > 20 or not all(
                        isinstance(k, str) and len(k) <= 40 and isinstance(v, str) and len(v) <= 200
                        for k, v in items.items()):
                    raise ApiError(400, "items")
                store.set_host_status(items, time.time())
                return 200, {"ok": True}
            if method == "POST" and url.path == "/v1/daily":
                if "full" not in rights:
                    raise ApiError(403, "forbidden")
                enabled = self._body().get("enabled")
                if not isinstance(enabled, bool):
                    raise ApiError(400, "enabled")
                store.set_daily(enabled)
                return 200, {"daily": "on" if enabled else "off"}
            if method == "GET" and url.path == "/v1/status":
                if "full" not in rights:
                    raise ApiError(403, "forbidden")
                open_questions = [{"id": q["id"], "code": q["code"], "client_id": q["client_id"],
                                   "project": (q["origin"] or {}).get("project"), "title": q["title"],
                                   "created_at": q["created_at"]} for q in store.open_questions()]
                host = store.get_host_status()
                if host is not None:
                    host = {"items": host["items"], "age_s": round(time.time() - host["received_at"])}
                return 200, {"open": open_questions, "presence": store.get_state("presence") or "present",
                             "host_status": host, "last_daily_at": store.last_message_at("daily"),
                             "daily": "on" if store.daily_enabled() else "off"}
            raise ApiError(404, "not_found")

        def _upload(self, client_id, rights):
            if "full" not in rights or media_dir is None:
                raise ApiError(403, "forbidden")
            data = self._raw_body(MAX_MEDIA)
            mime = sniff_image(data)
            if mime is None:
                raise ApiError(415, "not_an_image")
            with upload_lock:
                if store.media_usage() + len(data) > MEDIA_QUOTA:
                    raise ApiError(507, "media_quota")
                media_id = uuid.uuid4().hex
                os.makedirs(media_dir, exist_ok=True)
                path = os.path.join(media_dir, f"{media_id}.{EXTENSIONS[mime]}")
                with open(path, "wb") as f:
                    f.write(data)
                store.add_outbound_media(media_id, client_id, path, mime, len(data), time.time())
            return 201, {"id": media_id}

        def _download(self, media_id, client_id):
            media = store.get_media(media_id)
            owner = None
            if media and media["direction"] == "in" and media["message_id"]:
                owner = (store.get_message(media["message_id"]) or {}).get("client_id")
            elif media and media["direction"] == "out":
                owner = media["client_id"]
            if owner != client_id:
                raise ApiError(404, "not_found")
            if media["direction"] == "out":
                with open(media["path"], "rb") as f:
                    return 200, Raw(media["mime"], f.read())
            if tg is None:
                raise ApiError(503, "telegram")
            try:
                info = tg.get_file(media["file_id"])
                if int(info.get("file_size") or 0) > MAX_MEDIA:
                    raise ApiError(413, "too_large")
                data = tg.download_file(info["file_path"], MAX_MEDIA)
            except TelegramError:
                raise ApiError(502, "telegram") from None
            mime = sniff_image(data)
            if mime is None:
                raise ApiError(415, "not_an_image")
            return 200, Raw(mime, data)

        @staticmethod
        def _client_file(name):
            path = os.path.join(client_dir, name) if client_dir else None
            if not path or not os.path.isfile(path):
                raise ApiError(404, "not_found")
            with open(path, "rb") as f:
                return 200, Raw("text/plain; charset=utf-8", f.read())

        def _register(self):
            body = self._body()
            name, sha = body.get("name"), body.get("sha256")
            if not isinstance(name, str) or not DEVICE_NAME.match(name):
                raise ApiError(400, "name")
            if not isinstance(sha, str) or not SHA256_HEX.match(sha):
                raise ApiError(400, "sha256")
            if name in clients.names():
                raise ApiError(409, "exists")
            result = store.register_device(name, sha, _optional_str(body, "host", 60), time.time())
            if result == "exists":
                raise ApiError(409, "exists")
            if result == "busy":
                raise ApiError(429, "busy")
            return 202, {"status": "pending", "code": result}

        def _session(self, match, client_id, rights):
            if "full" not in rights:
                raise ApiError(403, "forbidden")
            key, body = match.group(1), self._body()
            if match.group(2):
                event = _validate_event(body)
                return 200, {"result": store.session_event(client_id, key, **event),
                             "presence": store.get_state("presence") or "present"}
            delay = _validate_done_delay(body)
            store.set_done_delay(client_id, key, delay, time.time())
            return 200, {"done_delay": delay}

        @staticmethod
        def _wait(msg: dict, url) -> dict:
            try:
                wait = min(MAX_WAIT, max(0.0, float(parse_qs(url.query).get("wait", ["0"])[0])))
            except ValueError:
                raise ApiError(400, "wait") from None
            deadline = time.time() + wait
            while msg["status"] in OPEN and time.time() < deadline:
                time.sleep(min(POLL_INTERVAL, max(0.0, deadline - time.time())))
                msg = store.get_message(msg["id"]) or msg
            return msg

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return _Server((host, port), Handler)
