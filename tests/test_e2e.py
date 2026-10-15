"""End to end: relay as a real process, two tg clients, fake Telegram, relay restart in between."""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from tests.fake_telegram import TOKEN, FakeTelegram

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TG = os.path.join(ROOT, "client", "tg.py")
CHAT = 4711


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout=20.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeTelegram()
        self.api_base = self.fake.start()
        self.port = free_port()
        self.relay = None
        token_file = os.path.join(self.tmp.name, "bot-token")
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(TOKEN)
        self.clients_file = os.path.join(self.tmp.name, "clients.json")
        self.relay_env = dict(os.environ, TG_BOT_TOKEN_FILE=token_file, TG_CLIENTS_FILE=self.clients_file,
                              TG_ALLOWED_CHAT_ID=str(CHAT), TG_DB_PATH=os.path.join(self.tmp.name, "relay.db"),
                              TG_API_BASE=self.api_base, TG_LISTEN=f"127.0.0.1:{self.port}", PYTHONUNBUFFERED="1",
                              TG_LANGUAGE="de", TG_TIMEZONE="Europe/Berlin",
                              TG_MEDIA_DIR=os.path.join(self.tmp.name, "media"))
        self.configs = {}
        clients = {}
        for name in ("desktop", "laptop"):
            self.configs[name] = os.path.join(self.tmp.name, f"{name}.json")
            out = self.tg(name, "setup", "--relay", f"http://127.0.0.1:{self.port}", "--name", name,
                          "--host-label", name.title()).stdout
            client, digest = out.split()
            clients[client] = {"sha256": digest.split(":", 1)[1], "rights": ["full"]}
        with open(self.clients_file, "w", encoding="utf-8") as f:
            json.dump(clients, f)

    def tearDown(self):
        self.stop_relay()
        self.fake.stop()
        self.tmp.cleanup()

    def tg_env(self, name):
        return dict(os.environ, TG_CONFIG=self.configs[name], CLAUDE_CODE_SESSION_ID=f"session-{name}")

    def tg(self, name, *args):
        result = subprocess.run([sys.executable, TG, *args], env=self.tg_env(name), capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def start_relay(self):
        self.relay_log = open(os.path.join(self.tmp.name, "relay.log"), "w+", encoding="utf-8")
        self.relay = subprocess.Popen([sys.executable, "-m", "relay.main"], cwd=ROOT, env=self.relay_env,
                                      stdout=self.relay_log, stderr=subprocess.STDOUT)

        def healthy():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/health", timeout=1) as r:
                    return r.status == 200
            except OSError:
                return False

        try:
            wait_for(healthy, what="relay health")
        except AssertionError:
            self.relay_log.seek(0)
            raise AssertionError("relay did not start:\n" + self.relay_log.read()[-3000:]) from None

    def stop_relay(self):
        if self.relay and self.relay.poll() is None:
            self.relay.terminate()
            self.relay.wait(10)
        if getattr(self, "relay_log", None):
            self.relay_log.close()

    def relay_status(self, name, message_id):
        with open(self.configs[name], encoding="utf-8") as f:
            token = json.load(f)["token"]
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/messages/{message_id}",
                                         headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=5) as r:
            return json.load(r)["status"]

    def sent_questions(self):
        return [c for c in self.fake.sent if c["method"] == "sendMessage" and c["params"]["text"].startswith("❓")]

    def test_e2e_two_agents_parallel_questions(self):
        self.start_relay()
        id_a, code_a = self.tg("desktop", "ask", "Frage A?", "--title", "A").stdout.split()
        id_b, code_b = self.tg("laptop", "ask", "Frage B?", "--title", "B").stdout.split()
        # Restart only after the relay has recorded both deliveries (not merely after Telegram got them).
        wait_for(lambda: self.relay_status("desktop", id_a) == "sent" and self.relay_status("laptop", id_b) == "sent",
                 what="both questions recorded as sent")
        self.assertEqual(len(self.sent_questions()), 2)

        self.stop_relay()
        self.start_relay()
        time.sleep(1.5)
        self.assertEqual(len(self.sent_questions()), 2, "no double send after restart")

        waiters = {name: subprocess.Popen([sys.executable, TG, "wait", message_id, "--timeout", "30"],
                                          env=self.tg_env(name), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True, encoding="utf-8")
                   for name, message_id in (("desktop", id_a), ("laptop", id_b))}

        question_b = next(c for c in self.sent_questions() if f"#{code_b}" in c["params"]["text"])
        self.assertIn("🖥 Laptop", question_b["params"]["text"])
        self.fake.push_update({"message": {"message_id": 1, "chat": {"id": CHAT}, "from": {"id": CHAT},
                                           "text": "Antwort für B", "reply_to_message": {
                                               "message_id": question_b["message_id"]}}})
        self.fake.push_update({"message": {"message_id": 2, "chat": {"id": CHAT}, "from": {"id": CHAT},
                                           "text": f"#{code_a} Antwort für A"}})

        results = {}
        for name, proc in waiters.items():
            out, err = proc.communicate(timeout=40)
            self.assertEqual(proc.returncode, 0, err)
            results[name] = json.loads(out)["answer"]["text"]
        self.assertEqual(results, {"desktop": "Antwort für A", "laptop": "Antwort für B"})

        self.fake.push_update({"message": {"message_id": 3, "chat": {"id": CHAT}, "from": {"id": CHAT},
                                           "text": "/offen"}})
        wait_for(lambda: any(c["method"] == "sendMessage" and c["params"]["text"] == "Keine offenen Fragen."
                             for c in self.fake.sent), what="/offen reply")
        edits = [c["params"]["text"] for c in self.fake.sent if c["method"] == "editMessageText"]
        self.assertEqual(sorted(bool(re.search("✔ beantwortet", t)) for t in edits), [True, True])


if __name__ == "__main__":
    unittest.main()
