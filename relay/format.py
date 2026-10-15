"""Telegram HTML rendering for relay messages."""
import html
from typing import Optional

from relay.i18n import t

TEXT_LIMIT = 3500
BUTTONS_PER_ROW = 3


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def truncate(text: str, limit: int = TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(t("truncated"))] + t("truncated")


def _join(parts) -> str:
    return " · ".join(escape(str(p)) for p in parts if p)


def _header(msg: dict) -> str:
    origin = msg.get("origin") or {}
    title = msg.get("title") or origin.get("title")
    if msg["kind"] == "question":
        first = "❓ " + _join([f"#{msg['code']}", origin.get("project"), title])
    elif msg["kind"] == "done":
        first = "✅ " + _join([t("hdr_done"), origin.get("project"), title])
    elif msg["kind"] == "daily":
        first = "☀️ " + _join([t("hdr_daily"), title])
    elif msg["kind"] == "permission":
        first = "⏸ " + _join([t("hdr_permission"), origin.get("project"), title])
    else:
        first = "📋 " + _join([origin.get("project"), title])
    second = _join([origin.get("host"), origin.get("account"), origin.get("agent")])
    return first + ("\n🖥 " + second if second else "")


def render_message(msg: dict) -> tuple[str, Optional[dict]]:
    body = (msg.get("text") or "").strip()
    text = _header(msg) + ("\n\n" + escape(truncate(body)) if body else "")
    buttons = msg.get("buttons")
    if msg["kind"] != "question" or not buttons:
        return text, None
    keys = [{"text": label, "callback_data": f"b:{msg['seq']}:{i}"} for i, label in enumerate(buttons)]
    rows = [keys[i:i + BUTTONS_PER_ROW] for i in range(0, len(keys), BUTTONS_PER_ROW)]
    return text, {"inline_keyboard": rows}


def render_final(msg: dict) -> str:
    text, _ = render_message(msg)
    status, reason = msg["status"], msg.get("closed_reason")
    if status == "answered":
        answer = msg.get("answer") or {}
        parts = [escape(truncate(answer.get("text") or "", 100))]
        count = len(answer.get("images") or [])
        if count:
            parts.append(t("images_count" if count > 1 else "image_count", n=count))
        line = t("final_answered", answer=" ".join(p for p in parts if p))
    elif status == "expired":
        line = t("final_expired")
    elif reason == "answered_in_app":
        line = t("final_in_app")
    else:
        line = t("final_closed")
    return text + "\n\n" + line
