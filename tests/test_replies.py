"""Replies to reports reach the session's listener; done pings only for away mode, long runs and Telegram turns."""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from relay.api import Clients, make_server
from relay.i18n import t
from relay.inbound import flush_albums, handle_update
from relay.store import DEFAULT_DONE_DELAY, LISTEN_FRESH, LONG_RUN, REPLY_TTL, Store
from tests.test_api import TOKENS, write_clients
from tests.test_images import photo_update
from tests.test_inbound import CHAT, text_update

T0 = 1_800_000_000.0
ORIGIN = {"host": "Desktop", "project": "WebApp", "agent": "Claude"}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def event(self, kind, ts, key="s1", **kw):
        return self.store.session_event("desktop", key, kind, ts, origin=ORIGIN, **kw)

    def pings(self, now):
        self.store.send_due_pings(now)
        return [m for m in self.messages() if m["kind"] in ("done", "permission")]

    def messages(self):
        return [m for m in (self.store.get_message_by_seq(s) for s in range(1, 50)) if m]

    def notices(self):
        return [e["payload"] for e in self.store.outbox_due(T0 + 10**6, 1000) if e["kind"] == "notice"]


class PingRuleTest(StoreCase):
    def test_short_work_at_the_computer_gets_no_ping(self):
        self.event("prompt", T0, human=True)
        self.assertEqual(self.event("stop", T0 + LONG_RUN - 1, text="fertig"), "scheduled")
        self.assertEqual(self.pings(T0 + 10 * LONG_RUN), [])

    def test_long_run_gets_a_ping_while_present(self):
        self.event("prompt", T0, human=True)
        self.event("prompt", T0 + 100, human=False)  # automatic prompts do not count as the user's
        self.event("stop", T0 + LONG_RUN, text="lange fertig")
        [ping] = self.pings(T0 + LONG_RUN + DEFAULT_DONE_DELAY)
        self.assertEqual(ping["text"], "lange fertig")

    def test_session_without_a_human_prompt_counts_as_long(self):
        self.event("stop", T0, text="fertig")
        self.assertEqual(len(self.pings(T0 + DEFAULT_DONE_DELAY)), 1)

    def test_away_is_checked_when_the_ping_is_due(self):
        self.event("prompt", T0, human=True)
        self.event("stop", T0 + 60, text="a")
        self.store.set_state("presence", "away")
        self.assertEqual(len(self.pings(T0 + 60 + DEFAULT_DONE_DELAY)), 1)
        self.event("prompt", T0 + 2000, human=True)
        self.event("stop", T0 + 2060, text="b")
        self.store.set_state("presence", "present")
        self.assertEqual(len(self.pings(T0 + 2060 + DEFAULT_DONE_DELAY)), 1, "no second ping")


class ListenerCase(StoreCase):
    def ping(self, tg_id=777, key="s1", ts=T0, kind="stop", text="Alles erledigt.\nDetails"):
        self.store.set_state("presence", "away")
        self.event(kind, ts, key=key, text=text, title="Login fixen")
        self.store.send_due_pings(ts + DEFAULT_DONE_DELAY)
        ping = [m for m in self.messages() if m["kind"] in ("done", "permission") and m["session_key"] == key][-1]
        self.store.mark_sent(ping["id"], tg_id)
        return ping

    def reply(self, text, tg_id=777, now=T0 + 700):
        update = text_update(text, reply_to=tg_id)
        handle_update(self.store, update, CHAT, now)
        return update["message"]["message_id"]

    def listen(self, listener="listener-a", started=T0, now=T0 + 690, key="s1"):
        return self.store.listen("desktop", key, listener, started, now)


class ReplyRoutingTest(ListenerCase):
    def test_reply_to_a_ping_reaches_the_listening_session(self):
        self.ping()
        self.assertEqual(self.listen()["result"], "idle")
        inbound = self.reply("Bitte noch Tests", now=T0 + 700)
        self.assertEqual(self.notices(), [], "confirmed when the session takes it")
        self.assertEqual(self.store.listen_state("desktop", "s1", "listener-a"), "pending")
        result = self.listen(now=T0 + 701)
        self.assertEqual(result["result"], "reply")
        self.assertEqual(result["reply"]["text"], "Bitte noch Tests")
        self.assertEqual(result["reply"]["report"], {"kind": "done", "title": "Login fixen", "text": "Alles erledigt."})
        [notice] = self.notices()
        self.assertEqual((notice["text"], notice["reply_to"]), (t("reply_passed", label="WebApp · Login fixen"), inbound))
        self.assertEqual(self.listen(now=T0 + 702)["result"], "idle", "handed over once")

    def test_reply_to_a_report_of_a_session(self):
        m = self.store.create_message("desktop", "report", "Zwischenstand", ORIGIN, "r1", T0, session_key="s1")
        self.store.mark_sent(m["id"], 555)
        self.listen()
        self.reply("weiter", tg_id=555)
        self.assertEqual(self.listen(now=T0 + 701)["reply"]["text"], "weiter")

    def test_not_listening_is_said_at_once_and_nothing_is_kept(self):
        self.ping()
        self.listen(now=T0 + 700 - LISTEN_FRESH - 1)  # the listener went quiet
        self.reply("hallo?")
        [notice] = self.notices()
        self.assertEqual(notice["text"], t("not_listening"))
        self.assertEqual(self.listen(now=T0 + 701)["result"], "idle")

    def test_report_without_session_and_daily_report(self):
        report = self.store.create_message("desktop", "report", "Codex fertig", ORIGIN, "r1", T0)
        daily = self.store.create_message("desktop", "daily", "Tag", ORIGIN, "d1", T0)
        self.store.mark_sent(report["id"], 601)
        self.store.mark_sent(daily["id"], 602)
        self.reply("x", tg_id=601)
        self.reply("y", tg_id=602)
        self.assertEqual([n["text"] for n in self.notices()], [t("not_listening"), t("cannot_reply")])

    def test_reply_to_permission_ping_says_approval_stays_in_the_app(self):
        self.ping(kind="permission", text="Bash?")
        self.listen()
        self.reply("ja, aber vorsichtig")
        self.assertEqual([n["text"] for n in self.notices()], [t("reply_permission")])
        self.assertEqual(self.listen(now=T0 + 701)["reply"]["text"], "ja, aber vorsichtig")

    def test_empty_reply_is_refused(self):
        self.ping()
        self.listen()
        self.reply("   ")
        self.assertEqual([n["text"] for n in self.notices()], [t("answer_missing")])

    def test_photo_album_reply_is_passed_with_its_images(self):
        ping = self.ping()
        self.listen()
        handle_update(self.store, photo_update("F1", caption="so", reply_to=777, group="g"), CHAT, T0 + 700)
        handle_update(self.store, photo_update("F2", reply_to=777, group="g"), CHAT, T0 + 700)
        flush_albums(self.store, T0 + 710)
        reply = self.listen(now=T0 + 711)["reply"]
        self.assertEqual(reply["text"], "so")
        self.assertEqual(len(reply["images"]), 2)
        for media_id in reply["images"]:
            self.assertEqual(self.store.get_media(media_id)["message_id"], ping["id"], "downloadable by its client")


class ListenerLifecycleTest(ListenerCase):
    def test_newer_listener_supersedes_older_one(self):
        self.assertEqual(self.listen("listener-a", started=T0)["result"], "idle")
        self.assertEqual(self.listen("listener-b", started=T0 + 5)["result"], "idle")
        self.assertEqual(self.store.listen_state("desktop", "s1", "listener-a"), "superseded")
        self.assertEqual(self.listen("listener-a", started=T0)["result"], "superseded", "the old one cannot come back")
        self.assertEqual(self.store.listen_state("desktop", "s1", "listener-b"), "idle")

    def test_session_end_stops_the_listener_and_drops_replies(self):
        self.ping()
        self.listen()
        inbound = self.reply("noch da?")
        self.assertEqual(self.event("end", T0 + 705), "ended")
        [notice] = self.notices()
        self.assertEqual((notice["text"], notice["reply_to"]), (t("reply_dropped"), inbound))
        self.assertEqual(self.listen(now=T0 + 706)["result"], "superseded")
        self.assertFalse(self.store.listening("desktop", "s1", T0 + 706))
        self.assertEqual(self.listen("listener-c", started=T0 + 800, now=T0 + 800)["result"], "idle",
                         "a resumed session listens again")

    def test_reply_nobody_takes_is_dropped(self):
        self.ping()
        self.listen()
        inbound = self.reply("hallo", now=T0 + 700)
        self.assertEqual(self.store.drop_stale_replies(T0 + 700 + REPLY_TTL - 1), 0)
        self.assertEqual(self.store.drop_stale_replies(T0 + 700 + REPLY_TTL), 1)
        self.assertEqual([(n["text"], n["reply_to"]) for n in self.notices()], [(t("reply_dropped"), inbound)])

    def test_turn_started_from_telegram_is_answered_there_at_once(self):
        self.ping()
        self.listen()
        self.reply("mach weiter")
        self.listen(now=T0 + 701)
        self.store.set_state("presence", "present")
        self.event("prompt", T0 + 702, human=False)  # the wake-up turn
        self.event("stop", T0 + 760, text="weiter gemacht")
        self.assertEqual([p["text"] for p in self.pings(T0 + 760)], ["Alles erledigt.\nDetails", "weiter gemacht"])
        self.event("prompt", T0 + 800, human=True)  # back in the app: short work stays quiet again
        self.event("stop", T0 + 900, text="still")
        self.assertEqual(len(self.pings(T0 + 900 + DEFAULT_DONE_DELAY)), 2)

    def test_reply_cancels_a_planned_ping(self):
        self.ping()
        self.listen()
        self.event("stop", T0 + 650, text="noch eine")  # planned for T0 + 650 + delay
        self.reply("stopp")
        self.listen(now=T0 + 701)
        self.assertEqual(len(self.pings(T0 + 10 * DEFAULT_DONE_DELAY)), 1)


class MigrationTest(unittest.TestCase):
    def test_old_sessions_table_gets_the_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "relay.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE sessions (client_id TEXT NOT NULL, session_key TEXT NOT NULL, origin TEXT,"
                         " title TEXT, done_delay INTEGER, ping_due REAL, ping_kind TEXT, ping_text TEXT,"
                         " last_event_at REAL, PRIMARY KEY (client_id, session_key))")
            conn.execute("INSERT INTO sessions (client_id, session_key, origin) VALUES ('desktop', 's1', '{}')")
            conn.commit()
            conn.close()
            store = Store(path)
            try:
                store.init_schema()
                self.assertEqual(store.session_event("desktop", "s1", "stop", T0, text="x"), "scheduled")
                self.assertEqual(store.listen("desktop", "s1", "listener-a", T0, T0)["result"], "idle")
            finally:
                store.close()


class ListenApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()
        clients_file = os.path.join(self.tmp.name, "clients.json")
        write_clients(clients_file)
        self.server = make_server(self.store, Clients(clients_file), "127.0.0.1", 0, lambda: {"ok": True})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def get(self, path, client="desktop"):
        request = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {TOKENS[client]}"})
        try:
            with urllib.request.urlopen(request, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def test_long_poll_returns_a_reply_as_soon_as_it_arrives(self):
        now = time.time()
        self.assertEqual(self.get(f"/v1/sessions/s1/listen?listener=listener-a&started={now}&wait=0"),
                         (200, {"result": "idle"}))
        report = self.store.create_message("desktop", "report", "Stand", ORIGIN, "r1", now, session_key="s1")
        self.store.mark_sent(report["id"], 900)
        threading.Timer(0.3, lambda: handle_update(self.store, text_update("los", reply_to=900), CHAT,
                                                   time.time())).start()
        started = time.time()
        status, body = self.get(f"/v1/sessions/s1/listen?listener=listener-a&started={now}&wait=5")
        self.assertEqual((status, body["result"], body["reply"]["text"]), (200, "reply", "los"))
        self.assertLess(time.time() - started, 3)

    def test_superseded_while_waiting(self):
        now = time.time()
        self.get(f"/v1/sessions/s1/listen?listener=listener-a&started={now}&wait=0")
        threading.Timer(0.3, lambda: self.store.listen("desktop", "s1", "listener-b", now + 1, time.time())).start()
        started = time.time()
        self.assertEqual(self.get(f"/v1/sessions/s1/listen?listener=listener-a&started={now}&wait=5"),
                         (200, {"result": "superseded"}))
        self.assertLess(time.time() - started, 3)

    def test_rights_and_validation(self):
        self.assertEqual(self.get("/v1/sessions/s1/listen?listener=listener-a&wait=0", client="server-monitor")[0],
                         403)
        self.assertEqual(self.get("/v1/sessions/s1/listen?listener=x&wait=0")[0], 400)
        self.assertEqual(self.get("/v1/sessions/s1/listen?listener=listener-a&wait=abc")[0], 400)

    def test_listeners_do_not_use_the_general_rate_limit(self):
        for _ in range(35):
            self.assertEqual(self.get("/v1/sessions/s1/listen?listener=listener-a&wait=0")[0], 200)
        self.assertEqual(self.get("/v1/status")[0], 200)

    def test_end_event_is_accepted(self):
        request = urllib.request.Request(self.base + "/v1/sessions/s1/events", method="POST",
                                         data=json.dumps({"type": "end", "ts": time.time()}).encode(),
                                         headers={"Authorization": f"Bearer {TOKENS['desktop']}",
                                                  "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as r:
            self.assertEqual(json.load(r)["result"], "ended")


if __name__ == "__main__":
    unittest.main()
