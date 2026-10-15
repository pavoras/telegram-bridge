import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from relay.api import Clients, make_server
from relay.store import Store

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))
import tg  # noqa: E402


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "config.json")
        self.env = mock.patch.dict(os.environ, {"TG_CONFIG": self.config_path, "CLAUDE_CODE_SESSION_ID": "sess-1",
                                                "CLAUDE_CODE_USER_EMAIL": "you@example.com"})
        self.env.start()
        self.delays = mock.patch.object(tg, "RETRY_DELAYS", (0, 0))
        self.delays.start()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()
        self.clients_file = os.path.join(self.tmp.name, "clients.json")
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.delays.stop()
        self.env.stop()
        self.store.close()
        self.tmp.cleanup()

    def run_tg(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tg.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def start_relay(self, telegram=None, client_dir=None):
        """Run `tg setup`, register the printed hash with an in-process relay."""
        code, out, _ = self.run_tg("setup", "--relay", "http://127.0.0.1:1", "--name", "desktop",
                                   "--host-label", "Desktop")
        self.assertEqual(code, 0)
        name, digest = out.split()
        with open(self.clients_file, "w", encoding="utf-8") as f:
            json.dump({name: {"sha256": digest.split(":", 1)[1], "rights": ["full"]}}, f)
        self.server = make_server(self.store, Clients(self.clients_file), "127.0.0.1", 0,
                                  lambda: {"ok": True, "last_poll_age_s": 0.5, "webhook_set": False},
                                  tg=telegram, media_dir=os.path.join(self.tmp.name, "media"),
                                  client_dir=client_dir)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        with open(self.config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["relay_url"] = f"http://127.0.0.1:{self.server.server_address[1]}"
        cfg["account_labels"] = {"you@example.com": "Konto A"}
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def test_setup_prints_only_hash_and_writes_config(self):
        code, out, _ = self.run_tg("setup", "--relay", "https://relay.example/tg", "--name", "desktop")
        self.assertEqual(code, 0)
        with open(self.config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertNotIn(cfg["token"], out)
        self.assertEqual(out.strip(), "desktop sha256:" + hashlib.sha256(cfg["token"].encode()).hexdigest())
        self.assertEqual(cfg["relay_url"], "https://relay.example/tg")
        self.assertEqual(self.run_tg("setup", "--relay", "x", "--name", "desktop")[0], 1)

    def test_secret_patterns_block_and_name_pattern(self):
        self.start_relay()
        samples = {
            "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----",
            "github_token": "ghp_" + "a" * 30,
            "github_pat": "github_pat_" + "a1" * 15,
            "openai_key": "sk-" + "A1b2" * 6,
            "slack_token": "xoxb-1234567890-abc",
            "aws_key": "AKIA" + "A" * 16,
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "assignment": "password = hunter2secret",
            "telegram_token": "1234567890:" + "A" * 35,
            "hex": "a" * 40,
            "random": "Xy7" * 14,
        }
        for name, sample in samples.items():
            code, _, err = self.run_tg("notify", f"Bitte prüfen: {sample}")
            self.assertEqual(code, 5, name)
            self.assertIn(name, err)
            self.assertNotIn(sample, err)
        code, _, _ = self.run_tg("ask", "Frage?", "--buttons", "ok," + "ghp_" + "b" * 30)
        self.assertEqual(code, 5)
        self.assertEqual(self.store.open_questions(), [])
        self.assertEqual(self.store.outbox_due(time.time() + 10, 100), [])

    def test_normal_text_with_commit_hash_short_is_allowed(self):
        self.assertIsNone(tg.find_secret([
            "Commit 8c00887 ist gepusht, siehe https://github.com/example/project/blob/main/docs/specs/"
            "2026-10-15-design.md und C:\\Users\\you\\Projects\\telegram-bridge\\relay",
            "Token-Datei liegt unter %APPDATA%", "password reset flow fertig"]))

    def test_ask_wait_returns_answer_json_exit_0(self):
        self.start_relay()
        code, out, _ = self.run_tg("ask", "A oder B?", "--buttons", "A,B", "--title", "Login")
        self.assertEqual(code, 0)
        message_id, question_code = out.split()
        msg = self.store.get_message(message_id)
        self.assertEqual((msg["code"], msg["buttons"], msg["title"], msg["session_key"]),
                         (int(question_code), ["A", "B"], "Login", "sess-1"))
        threading.Timer(0.3, lambda: self.store.answer_question(message_id, {"text": "B", "button": "B"},
                                                                time.time())).start()
        code, out, _ = self.run_tg("wait", message_id, "--timeout", "10")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual((result["status"], result["answer"]["text"], result["reason"]), ("answered", "B", None))

    def test_wait_timeout_exit_2_and_closed_exit_3_with_reason(self):
        self.start_relay()
        message_id = self.run_tg("ask", "Frage?")[1].split()[0]
        self.assertEqual(self.run_tg("wait", message_id, "--timeout", "1")[0], 2)
        self.store.close_question(message_id, "answered_in_app", time.time())
        code, out, _ = self.run_tg("wait", message_id, "--timeout", "5")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["reason"], "answered_in_app")

    def test_wait_survives_short_relay_outage(self):
        self.start_relay()
        answered = {"id": "x", "code": 123, "status": "answered", "answer": {"text": "ja"}, "closed_reason": None}
        outcomes = [tg.TgError("relay unreachable", tg.EXIT_RELAY), tg.TgError("relay unreachable", tg.EXIT_RELAY),
                    answered]

        def flaky(client, method, path, body=None):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(tg.RelayClient, "request", flaky), mock.patch.object(tg, "OUTAGE_PAUSE", 0):
            code, out, _ = self.run_tg("wait", "x", "--timeout", "60")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["answer"]["text"], "ja")

    def test_close_and_status(self):
        self.start_relay()
        message_id = self.run_tg("ask", "Frage?")[1].split()[0]
        code, out, _ = self.run_tg("status")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["open"][0]["id"], message_id)
        self.assertEqual(self.run_tg("close", message_id)[0], 0)
        self.assertEqual(self.store.get_message(message_id)["closed_reason"], "closed_by_agent")

    def test_relay_down_exit_4_after_three_attempts(self):
        import socket
        with socket.socket() as probe:  # a port that was free a moment ago: nothing listens there
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.run_tg("setup", "--relay", f"http://127.0.0.1:{port}", "--name", "desktop")
        with mock.patch.object(tg.RelayClient, "_send_once", wraps=tg.RelayClient._send_once, autospec=True) as send:
            code, _, err = self.run_tg("notify", "hallo")
        self.assertEqual(code, 4)
        self.assertEqual(send.call_count, 3)
        self.assertIn("relay", err.lower())

    def test_retry_after_network_error_does_not_duplicate_question(self):
        self.start_relay()
        original = tg.RelayClient._send_once
        calls = []

        def lossy(client, method, path, body):
            result = original(client, method, path, body)
            calls.append(path)
            if len(calls) == 1:
                raise ConnectionResetError("response lost")
            return result

        with mock.patch.object(tg.RelayClient, "_send_once", lossy):
            code, out, _ = self.run_tg("ask", "Nur einmal?")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.store.open_questions()), 1)
        self.assertEqual(out.split()[0], self.store.open_questions()[0]["id"])

    def test_origin_uses_account_label_and_project_folder(self):
        self.start_relay()
        with mock.patch("os.getcwd", return_value=os.path.join("C:\\", "Projekte", "WebApp")):
            message_id = self.run_tg("notify", "Build fertig", "--title", "Release")[1].split()[0]
        origin = self.store.get_message(message_id)["origin"]
        self.assertEqual(origin, {"host": "Desktop", "account": "Konto A", "project": "WebApp",
                                  "agent": "Claude" if os.environ.get("CLAUDECODE") == "1" else "Agent",
                                  "session": "sess-1"})

    def test_selftest(self):
        self.start_relay()
        self.assertEqual(self.run_tg("selftest")[0], 0)
        with open(self.config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["token"] = "falsch"
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        self.assertEqual(self.run_tg("selftest")[0], 1)


if __name__ == "__main__":
    unittest.main()
