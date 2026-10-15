"""Language and time zone of the bot's own texts (TG_LANGUAGE, TG_TIMEZONE)."""
import logging
from datetime import datetime, timedelta, timezone, tzinfo

LANGUAGES = ("en", "de")
_settings = {"language": "en", "zone": timezone.utc, "zone_name": "UTC"}

TEXTS = {
    # message headers and final states
    "truncated": {"en": "… (truncated)", "de": "… (gekürzt)"},
    "hdr_done": {"en": "done", "de": "fertig"},
    "hdr_daily": {"en": "Daily report", "de": "Daily Report"},
    "hdr_permission": {"en": "waiting for approval", "de": "wartet auf Freigabe"},
    "final_answered": {"en": "✔ answered: {answer}", "de": "✔ beantwortet: {answer}"},
    "image_count": {"en": "(+{n} image)", "de": "(+{n} Bild)"},
    "images_count": {"en": "(+{n} images)", "de": "(+{n} Bilder)"},
    "final_expired": {"en": "⌛ expired", "de": "⌛ abgelaufen"},
    "final_in_app": {"en": "✔ handled in the app", "de": "✔ in der App erledigt"},
    "final_closed": {"en": "✖ closed", "de": "✖ geschlossen"},
    "waiting_since": {"en": "waiting since {time}: {message}", "de": "wartet seit {time}: {message}"},
    "approval_needed": {"en": "Approval needed", "de": "Freigabe nötig"},
    "summary_blocked": {"en": "(summary withheld: possible secret)", "de": "(Kurzfassung gesperrt: möglicher Secret)"},
    # why an answer did not reach a question
    "reason_answered": {"en": "{code} is already answered.", "de": "{code} ist bereits beantwortet."},
    "reason_expired": {"en": "{code} has expired.", "de": "{code} ist abgelaufen."},
    "reason_failed": {"en": "{code} could not be delivered.", "de": "{code} konnte nicht zugestellt werden."},
    "reason_in_app": {"en": "{code} was handled in the app.", "de": "{code} wurde in der App erledigt."},
    "reason_closed": {"en": "{code} was closed.", "de": "{code} wurde geschlossen."},
    # routing of incoming messages
    "selection_prompt": {"en": "Which question do you mean?", "de": "Welche Frage ist gemeint?"},
    "not_waiting": {"en": "This session is not waiting on Telegram – please answer in the app.",
                    "de": "Diese Session wartet nicht auf Telegram – bitte in der Claude-App antworten."},
    "unsupported": {"en": "Only text and images (PNG, JPEG, WEBP) are supported.",
                    "de": "Nur Text und Bilder (PNG, JPEG, WEBP) werden unterstützt."},
    "late_album": {"en": "This image belongs to an album that was already assigned and was not taken over. "
                         "Please send it again as a reply to the question.",
                   "de": "Dieses Bild gehört zu einem Album, das schon zugeordnet ist, und wurde nicht übernommen. "
                         "Bitte als Antwort auf die Frage erneut senden."},
    "delivered": {"en": "✔ passed to {label}", "de": "✔ an {label} übergeben"},
    "cannot_reply": {"en": "This message cannot be answered.", "de": "Auf diese Nachricht kann man nicht antworten."},
    "code_unknown": {"en": "#{code} does not exist (any more).", "de": "#{code} gibt es nicht (mehr)."},
    "answer_missing": {"en": "The answer text is missing.", "de": "Antworttext fehlt."},
    "no_open_question": {"en": "No open question.", "de": "Keine offene Frage."},
    "no_open_questions": {"en": "No open questions.", "de": "Keine offenen Fragen."},
    "minutes_ago": {"en": "{n} min ago", "de": "vor {n} min"},
    # commands
    "help": {"en": "How to answer questions:\n"
                   "• reply to the question in Telegram (swipe)\n"
                   "• or start your message with #123\n"
                   "• or tap a button\n"
                   "With only one open question, plain text is enough.\n\n{commands}",
             "de": "So antwortest du auf Fragen:\n"
                   "• In Telegram auf die Frage antworten (wischen)\n"
                   "• oder #123 am Anfang deiner Nachricht\n"
                   "• oder einen Button antippen\n"
                   "Ist nur eine Frage offen, reicht einfacher Text.\n\n{commands}"},
    "now_away": {"en": "Status: away. Agents ask you here now.",
                 "de": "Status: unterwegs. Agenten fragen dich jetzt hier."},
    "now_present": {"en": "Status: at the computer.", "de": "Status: am Rechner."},
    "ready": {"en": "Ready. /help shows how it works.", "de": "Bereit. /hilfe zeigt die Bedienung."},
    "daily_on": {"en": "Daily report: on (08:00, fallback report at 08:15).",
                 "de": "Daily Report: an (08:00, Ersatzbericht um 08:15)."},
    "daily_off": {"en": "Daily report: off. /daily on switches it on.",
                  "de": "Daily Report: aus. /daily an schaltet ihn ein."},
    "daily_usage": {"en": "/daily on or /daily off", "de": "/daily an oder /daily aus"},
    "status_presence": {"en": "Status: {presence}", "de": "Status: {presence}"},
    "presence_away": {"en": "away", "de": "unterwegs"},
    "presence_present": {"en": "at the computer", "de": "am Rechner"},
    "status_open": {"en": "Open questions: {n}", "de": "Offene Fragen: {n}"},
    "failed_24h": {"en": "Failed deliveries (24 h): {n}", "de": "Fehlgeschlagene Zustellungen (24 h): {n}"},
    "last_daily": {"en": "Last daily report: {when}", "de": "Letzter Daily Report: {when}"},
    "never": {"en": "none yet", "de": "noch keiner"},
    "daily_is_off": {"en": "Daily report: off", "de": "Daily Report: aus"},
    "date_format": {"en": "%b %d, %H:%M", "de": "%d.%m. %H:%M"},
    # buttons and devices
    "invalid": {"en": "Invalid.", "de": "Ungültig."},
    "request_done": {"en": "This request was already handled.", "de": "Diese Anfrage ist schon erledigt."},
    "device_approved": {"en": "✔ Device {name} approved", "de": "✔ Gerät {name} zugelassen"},
    "device_rejected": {"en": "Device {name} rejected", "de": "Gerät {name} abgelehnt"},
    "selection_stale": {"en": "This choice is no longer valid (already assigned or expired). Please send again.",
                        "de": "Diese Auswahl gilt nicht mehr (schon zugeordnet oder abgelaufen). Bitte neu senden."},
    "assigned": {"en": "✔ to {label}", "de": "✔ an {label}"},
    "device_request": {"en": "New device “{label}” wants to send you messages. Confirmation code: {code}\n"
                             "Only approve if the device shows the same code.",
                       "de": "Neues Gerät „{label}“ möchte dir Nachrichten schicken. Bestätigungscode: {code}\n"
                             "Nur zulassen, wenn das Gerät denselben Code zeigt."},
    "approve": {"en": "Approve", "de": "Zulassen"},
    "reject": {"en": "Reject", "de": "Ablehnen"},
    # pairing (setup mode)
    "pair_hint": {"en": "Send /start followed by the pairing code from the relay log.",
                  "de": "Sende /start und dahinter den Kopplungscode aus dem Relay-Log."},
    "paired": {"en": "Connected. The bot now serves only this chat. /help shows how it works.",
               "de": "Verbunden. Der Bot bedient jetzt nur diesen Chat. /hilfe zeigt die Bedienung."},
    # server status and the daily fallback
    "server_none": {"en": "Server: no host status", "de": "Server: kein Host-Status"},
    "server_header": {"en": "Server (as of {n} min ago):", "de": "Server (Stand vor {n} min):"},
    "fallback_title": {"en": "☀️ The daily report from the PC is missing (the PC or the scheduled task did not run).",
                       "de": "☀️ Daily Report vom PC fehlt (der PC oder der geplante Task lief nicht)."},
    "fallback_open": {"en": "Open questions ({n}):", "de": "Offene Fragen ({n}):"},
}

# Command key → names per language. Every name works in every language; the menu shows the
# configured language's names.
COMMAND_NAMES = {"open": {"en": "open", "de": "offen"}, "status": {"en": "status", "de": "status"},
                 "away": {"en": "away", "de": "weg"}, "back": {"en": "back", "de": "da"},
                 "daily": {"en": "daily", "de": "daily"}, "help": {"en": "help", "de": "hilfe"}}
COMMAND_HELP = {"open": {"en": "open questions", "de": "offene Fragen"},
                "status": {"en": "questions, daily report, server", "de": "Fragen, Daily Report, Server"},
                "away": {"en": "you are away", "de": "du bist unterwegs"},
                "back": {"en": "you are at the computer", "de": "du bist am Rechner"},
                "daily": {"en": "daily report on/off", "de": "Daily Report an/aus"},
                "help": {"en": "how it works", "de": "Bedienung"}}
ON_WORDS, OFF_WORDS = ("on", "an"), ("off", "aus")


def t(key: str, **values) -> str:
    text = TEXTS[key][_settings["language"]]
    return text.format(**values) if values else text


def language() -> str:
    return _settings["language"]


def commands() -> list:
    """[(name, description)] for the bot menu and /help, in the configured language."""
    lang = _settings["language"]
    return [(COMMAND_NAMES[k][lang], COMMAND_HELP[k][lang]) for k in COMMAND_NAMES]


def command_key(name: str):
    for key, names in COMMAND_NAMES.items():
        if name in names.values():
            return key
    return None


def berlin_rule(ts: float) -> datetime:
    """Central European time without a tz database (EU rule: summer time from the last Sunday of
    March to the last Sunday of October, 01:00 UTC). Used where tzdata is missing, e.g. Windows."""
    utc = datetime.fromtimestamp(ts, timezone.utc)

    def last_sunday(month: int) -> datetime:
        day = datetime(utc.year, month, 31, 1, tzinfo=timezone.utc)
        return day - timedelta(days=(day.weekday() + 1) % 7)

    summer = last_sunday(3) <= utc < last_sunday(10)
    return utc.astimezone(timezone(timedelta(hours=2 if summer else 1)))


def _zone(name: str):
    if name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # no tz database (Windows without tzdata) or an unknown name
        if name == "Europe/Berlin":
            return None  # berlin_rule
        logging.getLogger("relay").warning("unknown time zone %r, using UTC", name)
        return timezone.utc


def configure(language: str = "en", zone: str = "UTC") -> None:
    if language not in LANGUAGES:
        raise ValueError(f"TG_LANGUAGE must be one of {', '.join(LANGUAGES)}")
    _settings.update(language=language, zone=_zone(zone), zone_name=zone)


def local_time(ts: float) -> datetime:
    zone = _settings["zone"]
    return berlin_rule(ts) if zone is None else datetime.fromtimestamp(ts, zone)


def format_time(ts: float) -> str:
    return local_time(ts).strftime(t("date_format"))
