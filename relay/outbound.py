"""Outbox worker: delivers queued Telegram actions with retries and throttling."""
import logging
import os
import threading
import time
from typing import Optional

from relay import format as fmt
from relay.store import OPEN, Store
from relay.telegram import TelegramClient, TelegramError

MAX_ATTEMPTS = 10
MAX_BACKOFF = 300
SEND_INTERVAL = 1.0

log = logging.getLogger("relay.outbound")


def _perform(store: Store, tg: TelegramClient, chat_id: int, entry: dict) -> None:
    """Deliver one outbox entry and mark it done."""
    kind, payload = entry["kind"], entry["payload"]
    if kind == "send":
        msg = store.get_message(payload["message_id"])
        if msg is not None:
            if msg["status"] in OPEN:
                text, markup = fmt.render_message(msg)
            else:
                text, markup = fmt.render_final(msg), None
            store.complete_send(entry["id"], msg["id"], tg.send_message(chat_id, text, markup))
            return
    elif kind == "edit":
        msg = store.get_message(payload["message_id"])
        if msg and msg["tg_message_id"]:
            tg.edit_message_text(chat_id, msg["tg_message_id"], fmt.render_final(msg))
    elif kind == "notice":
        buttons = payload.get("buttons")
        markup = {"inline_keyboard": [[{"text": b[0], "callback_data": b[1]}] for b in buttons]} if buttons else None
        tg.send_message(chat_id, fmt.escape(payload["text"]), markup, reply_to=payload.get("reply_to"))
    elif kind == "photo":
        media = store.get_media(payload["media_id"])
        try:
            with open(media["path"], "rb") as f:
                data = f.read()
        except (TypeError, OSError):
            data = None  # purged or missing: nothing left to send
        if data is not None:
            name = os.path.basename(media["path"])
            try:
                tg_id = tg.send_photo(chat_id, name, data, media["mime"], payload["reply_to"])
            except TelegramError as e:
                if not e.permanent:
                    raise
                tg_id = tg.send_document(chat_id, name, data, media["mime"], payload["reply_to"])
            store.link_tg(tg_id, payload["message_id"])
    elif kind == "notice_delete":
        tg.delete_message(chat_id, payload["tg_message_id"])
    elif kind == "callback_answer":
        tg.answer_callback_query(payload["callback_query_id"], payload.get("text"))
    store.outbox_done(entry["id"])


SETUP_RETRY = 60


def process_once(store: Store, tg: TelegramClient, chat_id: Optional[int], now: float) -> int:
    """Deliver at most one due outbox entry. Without a chat (setup mode) only entries that name
    their own chat are delivered; the rest wait."""
    due = store.outbox_due(now, 1)
    if not due:
        return 0
    entry = due[0]
    target = entry["payload"].get("chat_id", chat_id)
    if target is None:
        store.outbox_retry(entry["id"], now + SETUP_RETRY)
        return 1
    try:
        _perform(store, tg, target, entry)
    except TelegramError as e:
        attempts = entry["attempts"] + 1
        if e.permanent or attempts >= MAX_ATTEMPTS:
            log.warning("outbox %s %s failed permanently: %s", entry["id"], entry["kind"], e)
            store.outbox_fail(entry["id"])
        else:
            delay = e.retry_after if e.retry_after is not None else min(MAX_BACKOFF, 2 ** attempts)
            store.outbox_retry(entry["id"], now + delay)
    return 1


def run(store: Store, tg: TelegramClient, chat_id, stop: threading.Event) -> None:
    """chat_id: the chat, or a function returning it (None until a chat is paired)."""
    while not stop.is_set():
        try:
            sent = process_once(store, tg, chat_id() if callable(chat_id) else chat_id, time.time())
        except Exception:
            log.exception("outbox worker error")
            sent = 0
        stop.wait(SEND_INTERVAL if sent else 0.5)
