"""Relay process: Telegram poller, outbox worker, scheduler and HTTP API."""
import logging
import os
import signal
import threading
import time
from typing import Optional

from relay import i18n, outbound
from relay.api import Clients, make_server
from relay.config import Config, load_config
from relay.daily import check_daily
from relay.inbound import flush_albums, handle_update
from relay.store import Store
from relay.telegram import TelegramClient, TelegramError

POLL_TIMEOUT = 30
POLL_ERROR_PAUSE = 5
MAX_UPDATE_FAILURES = 3
SCHEDULER_TICK = 2
PURGE_INTERVAL = 3600
POLL_STALE = 600  # /health turns 503 (the monitor alarms by mail) when polling stopped this long ago

log = logging.getLogger("relay")


def poll_once(store: Store, tg: TelegramClient, allowed_chat_id: Optional[int], failures: dict,
              timeout: int = POLL_TIMEOUT, handler=handle_update) -> int:
    """Fetch and process one batch of updates in order. An update that fails three times is skipped."""
    offset = int(store.get_state("tg_offset") or 0) + 1
    processed = 0
    for update in tg.get_updates(offset, timeout):
        update_id = update["update_id"]
        try:
            if store.process_update(update_id, lambda: handler(store, update, allowed_chat_id, time.time())):
                processed += 1
            failures.pop(update_id, None)
        except Exception:
            failures[update_id] = failures.get(update_id, 0) + 1
            log.exception("update %s failed (%s/%s)", update_id, failures[update_id], MAX_UPDATE_FAILURES)
            if failures[update_id] < MAX_UPDATE_FAILURES:
                break
            store.process_update(update_id, lambda: None)
            failures.pop(update_id, None)
    return processed


def health_payload(last_poll_ok_at: Optional[float], started_at: float, now: float,
                   webhook_set: Optional[bool]) -> dict:
    since = last_poll_ok_at if last_poll_ok_at is not None else started_at
    return {"ok": now - since <= POLL_STALE,
            "last_poll_age_s": round(now - last_poll_ok_at, 1) if last_poll_ok_at is not None else None,
            "webhook_set": webhook_set}


def allowed_chat(configured: Optional[int], store: Store) -> Optional[int]:
    """TG_ALLOWED_CHAT_ID, else the chat paired with /start <code>; None = setup mode."""
    return configured if configured is not None else store.paired_chat()


def set_command_menu(tg: TelegramClient) -> bool:
    """The "/" menu in the bot chat. Best effort: the bot works without it."""
    try:
        tg.set_my_commands([{"command": c, "description": d} for c, d in i18n.commands()])
        return True
    except TelegramError as e:
        log.warning("setMyCommands failed: %s", e)
        return False


def run(config: Config) -> None:
    log.info("starting (language %s, time zone %s)", config.language, config.timezone)
    i18n.configure(config.language, config.timezone)
    store = Store(config.db_path)
    store.init_schema()
    tg = TelegramClient(config.telegram_base_url, config.bot_token)
    clients = Clients(config.clients_file, store)
    stop = threading.Event()
    status = {"last_poll_ok_at": None, "webhook_set": None}
    started_at = time.time()

    if allowed_chat(config.allowed_chat_id, store) is None:
        log.warning("setup mode: send this to your bot in a private chat:  /start %s", store.pairing_code())

    def poller():
        # Telegram calls run here, not before the API starts: a slow Telegram must not delay /v1/*.
        try:
            status["webhook_set"] = bool(tg.get_webhook_info().get("url"))
            if status["webhook_set"]:
                log.warning("a webhook is set for this bot; long polling will fail until it is removed")
        except TelegramError as e:
            log.warning("getWebhookInfo failed: %s", e)
        set_command_menu(tg)
        failures: dict = {}
        while not stop.is_set():
            try:
                poll_once(store, tg, allowed_chat(config.allowed_chat_id, store), failures)
                status["last_poll_ok_at"] = time.time()
            except Exception as e:
                log.warning("polling failed: %s", e)
                stop.wait(POLL_ERROR_PAUSE)

    def scheduler():
        next_purge = 0.0
        while not stop.wait(SCHEDULER_TICK):
            now = time.time()
            try:
                store.expire_due(now)
                store.send_due_pings(now)
                flush_albums(store, now)
                if allowed_chat(config.allowed_chat_id, store) is not None:
                    check_daily(store, now)
                if now >= next_purge:
                    store.purge_old(now)
                    store.purge_orphan_files(config.media_dir, now)
                    next_purge = now + PURGE_INTERVAL
            except Exception:
                log.exception("scheduler error")

    def health() -> dict:
        return health_payload(status["last_poll_ok_at"], started_at, time.time(), status["webhook_set"])

    os.makedirs(config.media_dir, exist_ok=True)
    server = make_server(store, clients, config.listen_host, config.listen_port, health, tg=tg,
                         media_dir=config.media_dir, client_dir=config.client_dir)
    chat = lambda: allowed_chat(config.allowed_chat_id, store)  # noqa: E731 (paired at runtime)
    threads = [threading.Thread(target=poller, daemon=True),
               threading.Thread(target=outbound.run, args=(store, tg, chat, stop), daemon=True),
               threading.Thread(target=scheduler, daemon=True)]
    for thread in threads:
        thread.start()

    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info("relay listening on %s:%s", config.listen_host, config.listen_port)
    server.serve_forever()
    server.server_close()
    # Let the outbox worker finish the delivery in progress so it is recorded and not resent.
    threads[1].join(timeout=10)
    store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run(load_config(os.environ))


if __name__ == "__main__":
    main()
