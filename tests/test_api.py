import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from relay.api import Clients, make_server
from relay.store import Store

TOKENS = {"desktop": "tok-desktop", "laptop": "tok-laptop", "server-monitor": "tok-monitor"}


def write_clients(path):
    rights = {"desktop": ["full"], "laptop": ["full"], "server-monitor": ["report", "host-status"]}
    data = {name: {"sha256": hashlib.sha256(tok.encode()).hexdigest(), "rights": rights[name]}
            for name, tok in TOKENS.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()
        clients_file = os.path.join(self.tmp.name, "clients.json")
        write_clients(clients_file)
        self.health = {"ok": True, "last_poll_age_s": 1.0, "webhook_set": False}
        self.server = make_server(self.store, Clients(clients_file), "127.0.0.1", 0, lambda: self.health)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, client="desktop", token=None):
        headers = {"Content-Type": "application/json"}
        token = token if token is not None else TOKENS.get(client)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def question(self, key="k1", client="desktop", **kw):
        body = {"kind": "question", "text": "A oder B?", "idempotency_key": key,
                "origin": {"project": "WebApp"}, "buttons": ["A", "B"]}
        body.update(kw)
        return self.call("POST", "/v1/messages", body, client=client)

    def test_create_question_and_long_poll_returns_answer(self):
        status, created = self.question()
        self.assertEqual(status, 201)
        self.assertTrue(100 <= created["code"] <= 999)
        threading.Timer(0.3, lambda: self.store.answer_question(created["id"], {"text": "B"}, time.time())).start()
        started = time.time()
        status, result = self.call("GET", f"/v1/messages/{created['id']}?wait=5")
        self.assertEqual(status, 200)
        self.assertEqual((result["status"], result["answer"]["text"]), ("answered", "B"))
        self.assertLess(time.time() - started, 3)

    def test_wait_times_out_with_open_status(self):
        _, created = self.question()
        started = time.time()
        status, result = self.call("GET", f"/v1/messages/{created['id']}?wait=1")
        self.assertEqual((status, result["status"], result["answer"]), (200, "queued", None))
        self.assertGreaterEqual(time.time() - started, 0.9)

    def test_close_own_question(self):
        _, created = self.question()
        self.assertEqual(self.call("POST", f"/v1/messages/{created['id']}/close", {})[1], {"closed": True})
        result = self.call("GET", f"/v1/messages/{created['id']}")[1]
        self.assertEqual((result["status"], result["closed_reason"]), ("closed", "closed_by_agent"))

    def test_other_clients_message_is_404(self):
        _, created = self.question()
        self.assertEqual(self.call("GET", f"/v1/messages/{created['id']}", client="laptop")[0], 404)
        self.assertEqual(self.call("POST", f"/v1/messages/{created['id']}/close", {}, client="laptop")[0], 404)

    def test_monitor_token_can_only_send_reports(self):
        self.assertEqual(self.question(client="server-monitor")[0], 403)
        status, _ = self.call("POST", "/v1/messages", {"kind": "report", "text": "Alarm", "idempotency_key": "m1"},
                              client="server-monitor")
        self.assertEqual(status, 201)
        self.assertEqual(self.call("GET", "/v1/status", client="server-monitor")[0], 403)

    def test_validation_errors(self):
        self.assertEqual(self.call("POST", "/v1/messages", {"kind": "question", "text": "x"}),
                         (400, {"error": "idempotency_key"}))
        self.assertEqual(self.question(kind="spam")[1], {"error": "kind"})
        self.assertEqual(self.question(buttons=["x" * 33])[1], {"error": "buttons"})
        self.assertEqual(self.question(text="")[1], {"error": "text"})

    def test_rate_limit_returns_429_after_30_requests(self):
        statuses = [self.call("GET", "/v1/status")[0] for _ in range(31)]
        self.assertEqual(statuses[:30], [200] * 30)
        self.assertEqual(statuses[30], 429)
        self.assertEqual(self.call("GET", "/v1/status", client="laptop")[0], 200)

    def test_invalid_token_401_and_health_without_token(self):
        self.assertEqual(self.call("GET", "/v1/status", token="wrong")[0], 401)
        self.assertEqual(self.call("GET", "/v1/status", client="nobody")[0], 401)
        self.assertEqual(self.call("GET", "/v1/health", client="nobody"),
                         (200, {"ok": True, "last_poll_age_s": 1.0, "webhook_set": False}))

    def test_relay_words_blocked_summaries_and_empty_done_messages(self):
        status, _ = self.call("POST", "/v1/sessions/s1/events", {"type": "stop", "ts": 1.0, "summary_blocked": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.store._query("SELECT ping_text FROM sessions WHERE session_key = 's1'")[0][0],
                         "(Kurzfassung gesperrt: möglicher Secret)")
        status, created = self.call("POST", "/v1/messages", {"kind": "done", "idempotency_key": "d1"})
        self.assertEqual((status, self.store.get_message(created["id"])["text"]), (201, ""))
        self.assertEqual(self.call("POST", "/v1/messages", {"kind": "report", "idempotency_key": "r1"})[0], 400)

    def test_without_a_clients_file_only_approved_devices_get_in(self):
        import hashlib
        store_clients = Clients(None, self.store)
        self.assertIsNone(store_clients.authenticate("Bearer x"))
        self.store.register_device("laptop", hashlib.sha256(b"tok-l").hexdigest(), None, 1.0)
        [device] = [r for r in self.store._query("SELECT seq FROM devices")]
        self.store.decide_device(device[0], True, 2.0)
        self.assertEqual(store_clients.authenticate("Bearer tok-l"), ("laptop", ["full"]))

    def test_unhealthy_relay_answers_503(self):
        self.health = {"ok": False, "last_poll_age_s": 900.0, "webhook_set": False}
        self.assertEqual(self.call("GET", "/v1/health", client="nobody"), (503, self.health))

    def test_idempotent_post_returns_same_id(self):
        first = self.question(key="same")[1]
        second = self.question(key="same")[1]
        self.assertEqual(first, second)

    def test_status_lists_open_questions_and_presence(self):
        _, created = self.question()
        self.store.set_state("presence", "away")
        status, result = self.call("GET", "/v1/status", client="laptop")
        self.assertEqual(result["presence"], "away")
        self.assertEqual([(q["id"], q["code"], q["project"]) for q in result["open"]],
                         [(created["id"], created["code"], "WebApp")])

    def test_unknown_path_404_and_large_body_413(self):
        self.assertEqual(self.call("GET", "/v1/nothing")[0], 404)
        self.assertEqual(self.question(text="x" * 70000)[0], 413)

    def test_session_events_and_done_delay(self):
        status, body = self.call("POST", "/v1/sessions/abc-1/events",
                                 {"type": "stop", "ts": time.time(), "text": "fertig", "origin": {"project": "P"}})
        self.assertEqual((status, body), (200, {"result": "scheduled", "presence": "present"}))
        self.assertEqual(self.call("POST", "/v1/sessions/abc-1", {"done_delay": "off"}), (200, {"done_delay": -1}))
        self.assertEqual(self.call("POST", "/v1/sessions/abc-1", {"done_delay": 1800}), (200, {"done_delay": 1800}))
        self.assertEqual(self.call("POST", "/v1/sessions/abc-1", {"done_delay": None}), (200, {"done_delay": None}))
        self.assertEqual(self.call("POST", "/v1/sessions/abc-1", {"done_delay": -5})[0], 400)
        self.assertEqual(self.call("POST", "/v1/sessions/abc-1/events", {"type": "nap", "ts": 1})[0], 400)
        self.assertEqual(self.call("POST", "/v1/sessions/a%20b/events", {"type": "stop", "ts": 1})[0], 404)
        self.assertEqual(self.call("POST", "/v1/sessions/abc-1/events", {"type": "stop", "ts": 1},
                                   client="server-monitor")[0], 403)

    def test_done_message_kind_is_accepted(self):
        status, created = self.call("POST", "/v1/messages", {"kind": "done", "text": "Codex fertig",
                                                             "idempotency_key": "d1"})
        self.assertEqual(status, 201)
        self.assertEqual(self.store.get_message(created["id"])["kind"], "done")

    def test_client_files_are_served_without_token(self):
        folder = os.path.join(self.tmp.name, "client-files")
        os.makedirs(folder)
        with open(os.path.join(folder, "SETUP.md"), "w", encoding="utf-8") as f:
            f.write("# Setup")
        server = make_server(self.store, Clients(os.path.join(self.tmp.name, "clients.json")), "127.0.0.1", 0,
                             lambda: {"ok": True}, client_dir=folder)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/v1/client/SETUP.md", timeout=5) as r:
            self.assertEqual((r.status, r.read(), r.headers.get_content_type()), (200, b"# Setup", "text/plain"))
        for name in ("tg.py", "../clients.json", "relay.db"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/v1/client/" + name, timeout=5)
            caught.exception.close()
            self.assertIn(caught.exception.code, (401, 404))

    def test_host_status_by_monitor_and_in_status(self):
        body = {"items": {"monitor": "ok", "disk": "41 %"}}
        self.assertEqual(self.call("POST", "/v1/host-status", body, client="server-monitor"), (200, {"ok": True}))
        self.assertEqual(self.call("POST", "/v1/host-status", {"items": {"x": 1}}, client="server-monitor")[0], 400)
        self.assertEqual(self.call("POST", "/v1/messages", {"kind": "daily", "text": "x", "idempotency_key": "d"},
                                   client="server-monitor")[0], 403)
        status, result = self.call("GET", "/v1/status")
        self.assertEqual(result["host_status"]["items"], {"monitor": "ok", "disk": "41 %"})
        self.assertLess(result["host_status"]["age_s"], 5)

    def test_daily_switch(self):
        daily = {"kind": "daily", "text": "Bericht", "idempotency_key": "daily-2026-10-16"}
        self.assertEqual(self.call("GET", "/v1/status")[1]["daily"], "off")
        self.assertEqual(self.call("POST", "/v1/messages", daily), (409, {"error": "daily_off"}))
        self.assertEqual(self.call("POST", "/v1/daily", {"enabled": True}), (200, {"daily": "on"}))
        self.assertEqual(self.call("GET", "/v1/status")[1]["daily"], "on")
        self.assertEqual(self.call("POST", "/v1/messages", daily)[0], 201)
        self.assertEqual(self.call("POST", "/v1/daily", {"enabled": "ja"})[0], 400)
        self.assertEqual(self.call("POST", "/v1/daily", {"enabled": False}, client="server-monitor")[0], 403)
        self.assertEqual(self.call("POST", "/v1/daily", {"enabled": False}), (200, {"daily": "off"}))

    def test_log_lines_contain_no_text_or_token(self):
        with self.assertLogs("relay.api", level="INFO") as logs:
            _, created = self.question(text="GEHEIMER-INHALT")
            self.call("GET", f"/v1/messages/{created['id']}")
        joined = "\n".join(logs.output)
        self.assertIn("POST /v1/messages 201 desktop", joined)
        self.assertIn("GET /v1/messages/{id} 200 desktop", joined)
        for forbidden in ("GEHEIMER-INHALT", TOKENS["desktop"], created["id"]):
            self.assertNotIn(forbidden, joined)


if __name__ == "__main__":
    unittest.main()
