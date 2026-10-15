"""Regression tests for the findings of review round 2 (Astra, phase 1+2)."""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from relay import api, inbound
from relay.api import Clients, make_server
from relay.inbound import flush_albums, handle_update
from relay.store import DEFAULT_DONE_DELAY, Store
from tests import test_client
from tests import test_client_hooks
from tests.test_images import photo_update
from tests.test_inbound import CHAT

T0 = 1_800_000_000.0
tg = test_client.tg


class StoreFindingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ask(self, key, tg_id, session=None, now=T0):
        m = self.store.create_message("desktop", "question", "Frage?", {"project": "P"}, key, now,
                                      session_key=session)
        self.store.mark_sent(m["id"], tg_id)
        return m["id"]

    def notices(self):
        return [e["payload"]["text"] for e in self.store.outbox_due(T0 + 10**6, 100) if e["kind"] == "notice"]

    def test_1_late_album_part_never_answers_another_question(self):
        a = self.ask("a", 900)
        b = self.ask("b", 901)
        handle_update(self.store, photo_update("F1", reply_to=900, group="G"), CHAT, T0)
        flush_albums(self.store, T0 + 3)
        handle_update(self.store, photo_update("F2", group="G"), CHAT, T0 + 5)
        flush_albums(self.store, T0 + 10)
        self.assertEqual(self.store.get_message(a)["status"], "answered")
        self.assertEqual(self.store.get_message(b)["status"], "sent")
        self.assertIn("Album", self.notices()[-1])

    def test_7_old_prompt_does_not_close_a_younger_question(self):
        q = self.ask("q", 900, session="s1", now=T0 + 100)
        self.store.session_event("desktop", "s1", "prompt", T0 + 50, human=True)
        self.assertEqual(self.store.get_message(q)["status"], "sent")

    def test_8_off_clears_planned_ping_and_new_question_suppresses_it(self):
        self.store.session_event("desktop", "s1", "stop", T0, origin={"project": "P"}, text="x")
        self.store.set_done_delay("desktop", "s1", -1, T0 + 1)
        self.assertEqual(self.store.send_due_pings(T0 + 10 * DEFAULT_DONE_DELAY), 0)
        self.store.session_event("desktop", "s2", "stop", T0, origin={"project": "P"}, text="x")
        self.ask("q", 900, session="s2", now=T0 + 5)
        self.assertEqual(self.store.send_due_pings(T0 + 10 * DEFAULT_DONE_DELAY), 0)

    def test_9_album_survives_a_failed_routing(self):
        q = self.ask("a", 900)
        handle_update(self.store, photo_update("F1", reply_to=900, group="G"), CHAT, T0)
        with mock.patch.object(inbound, "route", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                flush_albums(self.store, T0 + 3)
        flush_albums(self.store, T0 + 4)
        self.assertEqual(self.store.get_message(q)["status"], "answered")

    def test_11_late_delivery_failure_keeps_the_answer(self):
        q = self.store.create_message("desktop", "question", "A?", {}, "k", T0)
        self.store.answer_question(q["id"], {"text": "ja"}, T0 + 1)
        send = [e for e in self.store.outbox_due(T0 + 10, 10) if e["kind"] == "send"][0]
        self.store.outbox_fail(send["id"])
        self.assertEqual(self.store.get_message(q["id"])["status"], "answered")

    def test_14_settings_before_first_event_do_not_freeze_an_empty_origin(self):
        self.store.set_done_delay("desktop", "s1", 0, T0)
        self.store.session_event("desktop", "s1", "stop", T0 + 1, origin={"project": "WebApp"}, text="x")
        self.store.send_due_pings(T0 + 2)
        ping = self.store.get_message_by_seq(1)
        self.assertEqual(ping["origin"], {"project": "WebApp"})

    def test_3_registration_shows_a_confirmation_code(self):
        code = self.store.register_device("laptop", "a" * 64, "Laptop", T0)
        self.assertRegex(code, r"^\d{4}$")
        self.assertIn(code, self.notices()[0])


class ServerFindingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()
        path = os.path.join(self.tmp.name, "clients.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({}, f)
        self.server = make_server(self.store, Clients(path, self.store), "127.0.0.1", 0, lambda: {"ok": True})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def test_2_stalled_request_is_dropped(self):
        with mock.patch.object(api, "SOCKET_TIMEOUT", 1):
            sock = socket.create_connection(self.server.server_address, timeout=5)
            sock.sendall(b"POST /v1/register HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{")
            started = time.time()
            try:
                data = sock.recv(4096)
            except OSError:
                data = b""
            sock.close()
        self.assertLess(time.time() - started, 4, "the server must not wait forever for the body")
        self.assertFalse(data.startswith(b"HTTP/1.1 202"))


class ClientFindingsTest(test_client_hooks.ClientPhase2Test):
    def test_4_secret_in_origin_is_not_sent_by_hooks(self):
        self.start_relay()
        self.write_cfg(host_label="ghp_" + "a" * 30)
        self.hook("stop", {"session_id": "s-o", "last_assistant_message": "fertig"})
        [ping] = self.pings(DEFAULT_DONE_DELAY + 1)
        self.assertNotIn("ghp_", json.dumps(ping["origin"]))

    def test_5_parallel_failed_hooks_keep_every_event(self):
        self.start_relay()
        good_url = self.write_cfg()["relay_url"]
        self.write_cfg(relay_url="http://127.0.0.1:9")
        cfg = tg.load_config()
        events = [tg._hook_event("stop", {"session_id": f"s{i}", "last_assistant_message": f"m{i}"}, cfg)
                  for i in range(6)]
        threads = [threading.Thread(target=tg._deliver_events, args=(cfg, [e])) for e in events]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.write_cfg(relay_url=good_url)
        self.hook("user-prompt", {"session_id": "other", "prompt": "<x>"})
        self.assertEqual(sorted(p["text"] for p in self.pings(DEFAULT_DONE_DELAY + 1)), [f"m{i}" for i in range(6)])

    def test_6_hook_budget_is_one_deadline_for_all_requests(self):
        self.start_relay()
        timeouts = []

        def slow(client, method, path, body):
            timeouts.append(client.timeout)
            time.sleep(1.2)
            return {}

        cfg = tg.load_config()
        events = [tg._hook_event("stop", {"session_id": f"s{i}"}, cfg) for i in range(4)]
        with mock.patch.object(tg.RelayClient, "_send_once", slow):
            started = time.time()
            tg._deliver_events(cfg, events)
        self.assertLess(time.time() - started, tg.HOOK_BUDGET + 1.3)
        self.assertLessEqual(timeouts[-1], tg.HOOK_BUDGET - 1.2 + 0.1)

    def test_13_install_keeps_a_backup_of_claude_md(self):
        claude = Path(self.tmp.name) / "claude"
        claude.mkdir()
        (claude / "CLAUDE.md").write_text("# Global rules\n- mine\n", encoding="utf-8")
        self.start_relay()
        self.assertEqual(self.run_tg("install", "--claude-dir", str(claude))[0], 0)
        self.assertEqual((claude / "CLAUDE.md.bak-telegram").read_text(encoding="utf-8"), "# Global rules\n- mine\n")
        self.assertFalse(list(claude.glob("*.tmp")))


for _name in [n for n in dir(test_client_hooks.ClientPhase2Test) if n.startswith("test_")]:
    if _name not in ClientFindingsTest.__dict__:
        setattr(ClientFindingsTest, _name, None)

if __name__ == "__main__":
    unittest.main()
