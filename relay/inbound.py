"""Handling of incoming Telegram updates: commands, answer routing, button callbacks."""
import re
from typing import Optional

from relay import i18n
from relay.daily import host_status_lines
from relay.i18n import t
from relay.store import OPEN, Store

CODE_PREFIX = re.compile(r"^#(\d{3,4})\b\s*(.*)$", re.DOTALL)
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")
ALBUM_WAIT = 3


def help_text() -> str:
    return t("help", commands="\n".join(f"/{name} – {description}" for name, description in i18n.commands()))


def reason_text(msg: dict) -> str:
    code = f"#{msg['code']}"
    if msg["status"] == "answered":
        return t("reason_answered", code=code)
    if msg["status"] == "expired":
        return t("reason_expired", code=code)
    if msg["status"] == "failed":
        return t("reason_failed", code=code)
    if msg.get("closed_reason") == "answered_in_app":
        return t("reason_in_app", code=code)
    return t("reason_closed", code=code)


def _label(msg: dict, separator: str = " ") -> str:
    origin = msg.get("origin") or {}
    parts = [p for p in (origin.get("project"), msg.get("title") or origin.get("title")) if p]
    return f"#{msg['code']}" + (separator + " · ".join(parts) if parts else "")


def handle_update(store: Store, update: dict, allowed_chat_id: Optional[int], now: float) -> None:
    if "message" in update:
        _handle_message(store, update["message"], allowed_chat_id, now)
    elif "callback_query" in update:
        _handle_callback(store, update["callback_query"], allowed_chat_id, now)


def _handle_message(store: Store, message: dict, allowed: Optional[int], now: float) -> None:
    chat_id = (message.get("chat") or {}).get("id")
    sender = (message.get("from") or {}).get("id")
    text = message.get("text")
    if allowed is None:  # setup mode: wait for /start <pairing code> from a private chat
        words = (text or "").split()
        if words and words[0].split("@")[0].lower() == "/start" and chat_id == sender:
            if len(words) > 1 and store.try_pair(chat_id, words[1]):
                store.enqueue_notice(t("paired"), now, chat_id=chat_id)
            else:
                store.enqueue_notice(t("pair_hint"), now, chat_id=chat_id)
        return
    if chat_id != allowed or sender != chat_id:
        return
    message_id = message["message_id"]

    def notice(body: str, buttons=None) -> None:
        store.enqueue_notice(body, now, reply_to_tg_id=message_id, buttons=buttons)

    file_id = _image_file_id(message)
    if text is None and file_id is None:
        notice(t("unsupported"))
        return
    if text is not None and text.startswith("/"):
        _handle_command(store, text, now, notice)
        return
    incoming = {"message_id": message_id, "reply_to": (message.get("reply_to_message") or {}).get("message_id"),
                "text": text if text is not None else message.get("caption") or "",
                "images": [store.add_inbound_media(file_id, now)] if file_id else []}
    if file_id and message.get("media_group_id"):
        if not store.add_album_part(str(message["media_group_id"]), message_id, incoming["reply_to"],
                                    message.get("caption"), incoming["images"][0], now):
            notice(t("late_album"))
        return
    route(store, incoming, now)


def _image_file_id(message: dict) -> Optional[str]:
    if message.get("photo"):
        return message["photo"][-1]["file_id"]
    document = message.get("document") or {}
    return document.get("file_id") if document.get("mime_type") in IMAGE_TYPES else None


def flush_albums(store: Store, now: float) -> None:
    """Route albums whose parts have had ALBUM_WAIT seconds to arrive."""
    with store.tx():  # taking and routing succeed or fail together
        for incoming in store.take_due_albums(now - ALBUM_WAIT):
            route(store, incoming, now)


def route(store: Store, incoming: dict, now: float) -> None:
    """Assign an incoming answer (text and/or images) to a question, in the order of spec 4.3."""
    message_id, text, images = incoming["message_id"], incoming["text"], incoming["images"]

    def notice(body: str, buttons=None) -> None:
        store.enqueue_notice(body, now, reply_to_tg_id=message_id, buttons=buttons)

    def payload(answer_text: str) -> dict:
        return {"text": answer_text, "images": images} if images else {"text": answer_text}

    def answer(target: dict, answer_text: str) -> None:
        if target["status"] not in OPEN:
            notice(reason_text(target))
        elif store.answer_question(target["id"], payload(answer_text), now):
            notice(t("delivered", label=_label(target)))
        else:
            notice(reason_text(store.get_message(target["id"])))

    reply = incoming["reply_to"]
    if reply:
        target = store.message_for_tg(reply)
        if target is not None and target["kind"] in ("done", "permission"):
            notice(t("not_waiting"))
        elif target is None or target["kind"] != "question":
            notice(t("cannot_reply"))
        else:
            answer(target, text)
        return

    match = CODE_PREFIX.match(text)
    if match:
        code, rest = int(match.group(1)), match.group(2).strip()
        target = store.find_by_code(code)
        if target is None:
            notice(t("code_unknown", code=code))
        elif target["status"] in OPEN and not rest and not images:
            notice(t("answer_missing"))
        else:
            answer(target, rest)
        return

    open_questions = store.open_questions()
    if len(open_questions) == 1:
        answer(open_questions[0], text)
    elif open_questions:
        seq = store.create_assignment(message_id, payload(text), now)
        notice(t("selection_prompt"),
               buttons=[(_label(q)[:60], f"a:{seq}:{q['seq']}") for q in open_questions])
    else:
        notice(t("no_open_question"))


def _handle_command(store: Store, text: str, now: float, notice) -> None:
    name = text.split()[0].split("@")[0].lower().lstrip("/")
    command = "start" if name == "start" else i18n.command_key(name)
    if command == "open":
        open_questions = store.open_questions()
        if not open_questions:
            notice(t("no_open_questions"))
            return
        notice("\n".join(f"{_label(q, ' · ')} · {t('minutes_ago', n=int((now - q['created_at']) // 60))}"
                         for q in open_questions))
    elif command == "away":
        store.set_state("presence", "away")
        notice(t("now_away"))
    elif command == "back":
        store.set_state("presence", "present")
        notice(t("now_present"))
    elif command == "status":
        notice(status_text(store, now))
    elif command == "daily":
        argument = text.split()[1].lower() if len(text.split()) > 1 else ""
        if argument and argument not in i18n.ON_WORDS + i18n.OFF_WORDS:
            notice(t("daily_usage"))
            return
        if argument:
            store.set_daily(argument in i18n.ON_WORDS)
        notice(t("daily_on") if store.daily_enabled() else t("daily_off"))
    elif command == "start":
        notice(t("ready"))
    else:
        notice(help_text())


def status_text(store: Store, now: float) -> str:
    presence = t("presence_away") if store.get_state("presence") == "away" else t("presence_present")
    lines = [t("status_presence", presence=presence), t("status_open", n=len(store.open_questions()))]
    failed = store.failed_since(now - 86400)
    if failed:
        lines.append(t("failed_24h", n=failed))
    if store.daily_enabled():
        last = store.last_message_at("daily")
        lines.append(t("last_daily", when=i18n.format_time(last) if last else t("never")))
    else:
        lines.append(t("daily_is_off"))
    host = store.get_host_status()
    if host is not None:
        lines += [""] + host_status_lines(store, now)
    return "\n".join(lines)


def _handle_callback(store: Store, query: dict, allowed: Optional[int], now: float) -> None:
    chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
    sender = (query.get("from") or {}).get("id")
    if allowed is None or chat_id != allowed or sender != allowed:
        return

    def reply(text: str) -> None:
        store.enqueue_callback_answer(query["id"], text, now)

    parts = str(query.get("data", "")).split(":")
    try:
        kind, first, second = parts[0], int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        reply(t("invalid"))
        return

    if kind == "b":
        msg = store.get_message_by_seq(first)
        buttons = (msg or {}).get("buttons") or []
        if msg is None or not 0 <= second < len(buttons):
            reply(t("invalid"))
        elif store.answer_question(msg["id"], {"text": buttons[second], "button": buttons[second]}, now):
            reply("✔ " + buttons[second])
        else:
            reply(reason_text(store.get_message(msg["id"])))
    elif kind == "r":
        device = store.decide_device(first, second == 1, now)
        if device is None:
            reply(t("request_done"))
        else:
            reply(t("device_approved" if second == 1 else "device_rejected", name=device["name"]))
        prompt_id = (query.get("message") or {}).get("message_id")
        if prompt_id:
            store.enqueue_notice_delete(prompt_id, now)
    elif kind == "a":
        msg = store.get_message_by_seq(second)
        if msg is None:
            reply(t("invalid"))
            return
        chosen = store.take_assignment(first, now)
        selection_id = (query.get("message") or {}).get("message_id")
        if chosen is None:
            reply(t("selection_stale"))
        elif store.answer_question(msg["id"], chosen, now):
            reply(t("assigned", label=_label(msg)))
        else:
            reply(reason_text(store.get_message(msg["id"])))
        # The selection prompt has done its job either way; remove it from the chat.
        if selection_id:
            store.enqueue_notice_delete(selection_id, now)
    else:
        reply(t("invalid"))
