"""Thin Telegram Bot API client with error classification."""
import json
import urllib.error
import uuid
import urllib.request
from typing import Any, Optional


class TelegramError(Exception):
    def __init__(self, message: str, retry_after: Optional[float] = None, permanent: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


class TelegramClient:
    def __init__(self, base_url: str, token: str):
        self._url = f"{base_url.rstrip('/')}/bot{token}/"
        self._file_url = f"{base_url.rstrip('/')}/file/bot{token}/"

    def _call(self, method: str, params: dict, timeout: float = 30, body: Optional[bytes] = None,
              content_type: str = "application/json") -> Any:
        data = body if body is not None else json.dumps(params).encode()
        request = urllib.request.Request(self._url + method, data=data, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as e:
            with e:
                try:
                    payload = json.load(e)
                except ValueError:
                    payload = {"error_code": e.code, "description": "invalid response"}
        except (urllib.error.URLError, OSError, ValueError) as e:
            # str(e) of a URLError may contain the URL, which contains the token.
            raise TelegramError(f"{method}: network error ({type(e).__name__})") from None
        if payload.get("ok"):
            return payload["result"]
        code = int(payload.get("error_code") or 0)
        description = str(payload.get("description", ""))
        retry_after = (payload.get("parameters") or {}).get("retry_after")
        message = f"{method}: {code} {description}"
        if code == 429:
            raise TelegramError(message, retry_after=float(retry_after or 5))
        raise TelegramError(message, permanent=400 <= code < 500)

    def get_updates(self, offset: int, timeout: int) -> list:
        return self._call("getUpdates", {"offset": offset, "timeout": timeout,
                                         "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 10)

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None,
                     reply_to: Optional[int] = None) -> int:
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}}
        if reply_markup:
            params["reply_markup"] = reply_markup
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self._call("sendMessage", params)["message_id"]

    def edit_message_text(self, chat_id: int, message_id: int, text: str,
                          reply_markup: Optional[dict] = None) -> None:
        params = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}}
        if reply_markup:
            params["reply_markup"] = reply_markup
        try:
            self._call("editMessageText", params)
        except TelegramError as e:
            if "message is not modified" not in str(e):
                raise

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> None:
        params = {"callback_query_id": callback_query_id}
        if text:
            params["text"] = text
        self._call("answerCallbackQuery", params)

    def set_my_commands(self, commands: list) -> None:
        self._call("setMyCommands", {"commands": commands})

    def get_webhook_info(self) -> dict:
        return self._call("getWebhookInfo", {})

    def _send_file(self, method: str, field: str, chat_id: int, filename: str, data: bytes, mime: str,
                   reply_to: Optional[int]) -> int:
        boundary = uuid.uuid4().hex
        fields = {"chat_id": str(chat_id)}
        if reply_to:
            fields["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                 for k, v in fields.items()]
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                     f"Content-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        result = self._call(method, {}, timeout=120, body=b"".join(parts),
                            content_type=f"multipart/form-data; boundary={boundary}")
        return result["message_id"]

    def send_photo(self, chat_id: int, filename: str, data: bytes, mime: str, reply_to: Optional[int] = None) -> int:
        return self._send_file("sendPhoto", "photo", chat_id, filename, data, mime, reply_to)

    def send_document(self, chat_id: int, filename: str, data: bytes, mime: str,
                      reply_to: Optional[int] = None) -> int:
        return self._send_file("sendDocument", "document", chat_id, filename, data, mime, reply_to)

    def get_file(self, file_id: str) -> dict:
        return self._call("getFile", {"file_id": file_id})

    def download_file(self, file_path: str, limit: int) -> bytes:
        try:
            with urllib.request.urlopen(self._file_url + file_path, timeout=60) as response:
                data = response.read(limit + 1)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TelegramError(f"file download: network error ({type(e).__name__})") from None
        if len(data) > limit:
            raise TelegramError("file download: too large", permanent=True)
        return data
