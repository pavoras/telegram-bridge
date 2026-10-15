"""Relay configuration from environment variables and secret files."""
from dataclasses import dataclass
from typing import Mapping, Optional

from relay.i18n import LANGUAGES


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_chat_id: Optional[int]
    clients_file: Optional[str]
    db_path: str
    telegram_base_url: str
    listen_host: str
    listen_port: int
    media_dir: str = "/data/media"
    client_dir: str = "/app/client-files"
    language: str = "en"
    timezone: str = "UTC"


def _bot_token(env: Mapping[str, str]) -> str:
    """TG_BOT_TOKEN_FILE (preferred: a secret file) or TG_BOT_TOKEN."""
    token_file = env.get("TG_BOT_TOKEN_FILE", "").strip()
    if token_file:
        try:
            with open(token_file, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as e:
            raise ValueError(f"cannot read TG_BOT_TOKEN_FILE: {e.strerror}") from None
        if not token:
            raise ValueError("TG_BOT_TOKEN_FILE is empty")
        return token
    token = env.get("TG_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("set TG_BOT_TOKEN_FILE or TG_BOT_TOKEN")
    return token


def load_config(env: Mapping[str, str]) -> Config:
    chat = env.get("TG_ALLOWED_CHAT_ID", "").strip()
    host, _, port = env.get("TG_LISTEN", "0.0.0.0:8080").rpartition(":")
    language = env.get("TG_LANGUAGE", "en").strip().lower() or "en"
    if language not in LANGUAGES:
        raise ValueError(f"TG_LANGUAGE must be one of {', '.join(LANGUAGES)}")
    return Config(
        bot_token=_bot_token(env),
        allowed_chat_id=int(chat) if chat else None,
        clients_file=env.get("TG_CLIENTS_FILE", "").strip() or None,
        db_path=env.get("TG_DB_PATH", "/data/relay.db"),
        telegram_base_url=env.get("TG_API_BASE", "https://api.telegram.org").rstrip("/"),
        listen_host=host,
        listen_port=int(port),
        media_dir=env.get("TG_MEDIA_DIR", "/data/media"),
        client_dir=env.get("TG_CLIENT_DIR", "/app/client-files"),
        language=language,
        timezone=env.get("TG_TIMEZONE", "UTC").strip() or "UTC",
    )
