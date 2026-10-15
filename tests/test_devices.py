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
from relay.inbound import handle_update
from relay.store import Store
from tests.test_inbound import CHAT, callback_update

T0 = 1_800_000_000.0
HASH = hashlib.sha256(b"laptop-token").hexdigest()
CODE = r"^\d{4}$"  # registration returns the confirmation code


class DeviceStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def notices(self):
        return [e["payload"] for e in self.store.outbox_due(T0 + 10**6, 100) if e["kind"] == "notice"]

    def test_registration_asks_the_user_and_approval_activates_device(self):
        self.assertRegex(self.store.register_device("laptop", HASH, "Laptop", T0), CODE)
        [prompt] = self.notices()
        self.assertIn("laptop", prompt["text"])
        approve = prompt["buttons"][0][1]
        self.assertEqual(self.store.approved_devices(), {})
        handle_update(self.store, callback_update(approve), CHAT, T0 + 5)
        self.assertEqual(self.store.approved_devices(), {"laptop": HASH})
        handle_update(self.store, callback_update(approve), CHAT, T0 + 6)
        answers = [e["payload"]["text"] for e in self.store.outbox_due(T0 + 10**6, 100)
                   if e["kind"] == "callback_answer"]
        self.assertEqual(answers[0], "✔ Gerät laptop zugelassen")
        self.assertEqual(answers[1], "Diese Anfrage ist schon erledigt.")

    def test_rejection_and_limits(self):
        self.assertRegex(self.store.register_device("a", HASH, None, T0), CODE)
        self.assertEqual(self.store.register_device("b", HASH, None, T0 + 30), "busy")
        self.assertEqual(self.store.register_device("a", HASH, None, T0 + 61), "exists")
        reject = self.notices()[0]["buttons"][1][1]
        handle_update(self.store, callback_update(reject), CHAT, T0 + 62)
        self.assertEqual(self.store.approved_devices(), {})
        self.assertRegex(self.store.register_device("a", HASH, None, T0 + 130), CODE)
        self.assertRegex(self.store.register_device("c", HASH, None, T0 + 200), CODE)
        self.assertRegex(self.store.register_device("d", HASH, None, T0 + 300), CODE)
        self.assertEqual(self.store.register_device("e", HASH, None, T0 + 400), "busy", "at most 3 pending")


class DeviceApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()
        path = os.path.join(self.tmp.name, "clients.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"desktop": {"sha256": hashlib.sha256(b"tok-desktop").hexdigest(), "rights": ["full"]}}, f)
        self.server = make_server(self.store, Clients(path, self.store), "127.0.0.1", 0, lambda: {"ok": True})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=headers,
                                                               method=method), timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def test_register_then_token_works_after_approval(self):
        self.assertEqual(self.call("GET", "/v1/status", token="laptop-token")[0], 401)
        status, body = self.call("POST", "/v1/register", {"name": "laptop", "sha256": HASH, "host": "Laptop"})
        self.assertEqual((status, body["status"]), (202, "pending"))
        self.assertRegex(body["code"], CODE)
        seq = int(self.store.outbox_due(T0 * 2, 10)[0]["payload"]["buttons"][0][1].split(":")[1])
        self.assertIsNotNone(self.store.decide_device(seq, True, time.time()))
        self.assertEqual(self.call("GET", "/v1/status", token="laptop-token")[0], 200)

    def test_register_validation_and_reserved_names(self):
        self.assertEqual(self.call("POST", "/v1/register", {"name": "Bad Name", "sha256": HASH})[0], 400)
        self.assertEqual(self.call("POST", "/v1/register", {"name": "x", "sha256": "abc"})[0], 400)
        self.assertEqual(self.call("POST", "/v1/register", {"name": "desktop", "sha256": HASH})[0], 409)


if __name__ == "__main__":
    unittest.main()
