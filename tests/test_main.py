import os
import tempfile
import unittest

from relay.main import poll_once
from relay.store import Store
from relay.telegram import TelegramClient
from tests.fake_telegram import TOKEN, FakeTelegram

CHAT = 4711


def text(update_id, body):
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": {"id": CHAT}, "from": {"id": CHAT},
                                                "text": body}}


class PollerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()
        self.fake = FakeTelegram()
        self.tg = TelegramClient(self.fake.start(), TOKEN)

    def tearDown(self):
        self.fake.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_allowed_chat_from_config_or_pairing(self):
        from relay.main import allowed_chat
        self.assertIsNone(allowed_chat(None, self.store))
        self.store.pair(4242)
        self.assertEqual(allowed_chat(None, self.store), 4242)
        self.assertEqual(allowed_chat(CHAT, self.store), CHAT, "TG_ALLOWED_CHAT_ID wins")

    def test_health_turns_bad_when_polling_stops(self):
        from relay.main import health_payload
        started = 1000.0
        self.assertTrue(health_payload(None, started, started + 30, False)["ok"], "start-up grace")
        self.assertFalse(health_payload(None, started, started + 601, False)["ok"], "never polled")
        self.assertEqual(health_payload(started + 100, started, started + 160, False),
                         {"ok": True, "last_poll_age_s": 60.0, "webhook_set": False})
        self.assertFalse(health_payload(started + 100, started, started + 701, False)["ok"], "stale poll")

    def test_command_menu_is_sent_to_telegram(self):
        from relay.i18n import commands
        from relay.main import set_command_menu
        self.assertTrue(set_command_menu(self.tg))
        (call,) = self.fake.calls("setMyCommands")
        self.assertEqual(call["commands"], [{"command": c, "description": d} for c, d in commands()])
        self.fake.fail_next("setMyCommands", 500)
        self.assertFalse(set_command_menu(self.tg))  # a failure is logged, never fatal

    def test_poll_once_processes_updates_and_advances_offset(self):
        self.fake.push_update(text(10, "/weg"))
        self.assertEqual(poll_once(self.store, self.tg, CHAT, {}, timeout=0), 1)
        self.assertEqual(self.store.get_state("presence"), "away")
        self.assertEqual(self.store.get_state("tg_offset"), "10")
        self.assertEqual(poll_once(self.store, self.tg, CHAT, {}, timeout=0), 0)

    def test_poison_update_is_skipped_after_three_failures(self):
        self.fake.push_update(text(20, "kaputt"))
        self.fake.push_update(text(21, "/weg"))
        calls = []

        def handler(store, update, allowed, now):
            calls.append(update["update_id"])
            if update["update_id"] == 20:
                raise RuntimeError("boom")
            store.set_state("presence", "away")

        failures = {}
        with self.assertLogs("relay", level="ERROR") as logs:
            for _ in range(3):
                poll_once(self.store, self.tg, CHAT, failures, timeout=0, handler=handler)
        self.assertEqual(len(logs.records), 3)
        self.assertEqual(calls, [20, 20, 20, 21])
        self.assertEqual(self.store.get_state("tg_offset"), "21")
        self.assertEqual(self.store.get_state("presence"), "away")


if __name__ == "__main__":
    unittest.main()
