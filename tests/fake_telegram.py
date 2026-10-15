"""In-process fake of the Telegram Bot API for tests."""
import email
import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

TOKEN = "123:TEST"


class FakeTelegram:
    def __init__(self, token: str = TOKEN):
        self.token = token
        self.sent: list = []
        self._updates: list = []
        self._next_update_id = 1
        self._next_message_id = 1000
        self._failures: dict = {}
        self.files: dict = {}  # file_id -> bytes, served by getFile and the file download URL
        self._lock = threading.Condition()
        self._server = None

    # --- test controls -------------------------------------------------
    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self):
                url = urlparse(self.path)
                parts = url.path.strip("/").split("/")
                params = dict(parse_qsl(url.query))
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if len(parts) >= 3 and parts[0] == "file" and parts[1] == "bot" + fake.token:
                    data = fake.files.get(parts[-1].split(".")[0])
                    if data is None:
                        return self._reply(404, {"ok": False})
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                content_type = self.headers.get("Content-Type", "")
                if content_type.startswith("multipart/form-data"):
                    message = email.message_from_bytes(
                        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw)
                    for part in message.get_payload():
                        name = part.get_param("name", header="content-disposition")
                        payload = part.get_payload(decode=True)
                        if part.get_filename():
                            params[name] = {"filename": part.get_filename(), "size": len(payload),
                                            "content_type": part.get_content_type()}
                        else:
                            params[name] = payload.decode()
                elif raw:
                    params.update(json.loads(raw))
                if len(parts) != 2 or parts[0] != "bot" + fake.token:
                    return self._reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                status, payload = fake._call(parts[1], params)
                self._reply(status, payload)

            do_GET = _handle
            do_POST = _handle

        class QuietServer(ThreadingHTTPServer):
            daemon_threads = True

            def server_bind(self):  # skip the reverse DNS lookup of HTTPServer.server_bind
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = self.server_address[:2]

            def handle_error(self, request, client_address):
                pass  # clients (e.g. a killed relay) may drop connections mid-request

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def push_update(self, update: dict) -> dict:
        with self._lock:
            if "update_id" not in update:
                update = dict(update, update_id=self._next_update_id)
            self._next_update_id = max(self._next_update_id, update["update_id"] + 1)
            self._updates.append(update)
            self._lock.notify_all()
        return update

    def fail_next(self, method: str, status: int, retry_after=None) -> None:
        with self._lock:
            self._failures[method] = (status, retry_after)

    def calls(self, method: str) -> list:
        with self._lock:
            return [c["params"] for c in self.sent if c["method"] == method]

    # --- API -------------------------------------------------------------
    def _call(self, method: str, params: dict):
        with self._lock:
            failure = self._failures.pop(method, None)
        if failure:
            status, retry_after = failure
            payload = {"ok": False, "error_code": status, "description": f"fake error {status}"}
            if retry_after is not None:
                payload["parameters"] = {"retry_after": retry_after}
            return status, payload
        if method == "getUpdates":
            return 200, {"ok": True, "result": self._get_updates(params)}
        with self._lock:
            entry = {"method": method, "params": params}
            self.sent.append(entry)
            if method in ("sendMessage", "sendPhoto", "sendDocument"):
                self._next_message_id += 1
                entry["message_id"] = self._next_message_id
                result = {"message_id": self._next_message_id, "chat": {"id": int(params["chat_id"])},
                          "text": params.get("text", "")}
            elif method == "getFile":
                data = self.files.get(params["file_id"])
                if data is None:
                    return 400, {"ok": False, "error_code": 400, "description": "Bad Request: invalid file_id"}
                result = {"file_id": params["file_id"], "file_size": len(data),
                          "file_path": f"photos/{params['file_id']}.jpg"}
            elif method == "getWebhookInfo":
                result = {"url": ""}
            else:
                result = True
        return 200, {"ok": True, "result": result}

    def _get_updates(self, params: dict) -> list:
        offset = int(params.get("offset", 0))
        deadline = time.time() + min(float(params.get("timeout", 0)), 1.0)
        with self._lock:
            while True:
                result = [u for u in self._updates if u["update_id"] >= offset]
                if result or time.time() >= deadline:
                    return result
                self._lock.wait(max(0.0, deadline - time.time()))
