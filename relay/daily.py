"""08:15 safety net: if the PC's daily report did not arrive, the relay sends a short one itself."""
from datetime import datetime, time as clock

from relay.i18n import local_time, t
from relay.store import Store

FALLBACK_AT = clock(8, 15)
FALLBACK_UNTIL = clock(12, 0)  # a missing morning report is not worth a message in the afternoon


def host_status_lines(store: Store, now: float) -> list:
    status = store.get_host_status()
    if status is None:
        return [t("server_none")]
    age = int((now - status["received_at"]) // 60)
    lines = [t("server_header", n=age)]
    lines += [f"• {key}: {value}" for key, value in status["items"].items()]
    return lines


def check_daily(store: Store, now: float) -> bool:
    """Run once per day from FALLBACK_AT; True if the fallback report was queued."""
    local = local_time(now)
    today = local.date().isoformat()
    if (not store.daily_enabled() or not FALLBACK_AT <= local.time() < FALLBACK_UNTIL
            or store.get_state("daily_checked") == today):
        return False
    store.set_state("daily_checked", today)
    midnight = datetime.combine(local.date(), clock(0), local.tzinfo).timestamp()
    if store.has_message_since("daily", midnight):
        return False
    questions = store.open_questions()
    lines = [t("fallback_title"), ""]
    if questions:
        lines.append(t("fallback_open", n=len(questions)))
        for q in questions:
            origin = q.get("origin") or {}
            label = " · ".join(p for p in (origin.get("project"), q.get("title")) if p)
            lines.append(f"• #{q['code']} {label}".rstrip())
    else:
        lines.append(t("no_open_questions"))
    failed = store.failed_since(now - 86400)
    if failed:
        lines.append(t("failed_24h", n=failed))
    lines += [""] + host_status_lines(store, now)
    store.enqueue_notice("\n".join(lines), now)
    return True
