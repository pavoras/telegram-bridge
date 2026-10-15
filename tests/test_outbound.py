import os
import tempfile
import unittest

from relay import outbound
from relay.store import Store
from relay.telegram import TelegramClient
from tests.fake_telegram import TOKEN, FakeTelegram

T0 = 1_800_000_000.0
CHAT = 4711


class OutboundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "relay.db")
        self.store = Store(self.path)
        self.store.init_schema()
        self.fake = FakeTelegram()
        self.tg = TelegramClient(self.fake.start(), TOKEN)

    def tearDown(self):
        self.fake.stop()
        self.store.close()
        self.tmp.cleanup()

    def question(self, key="k"):
        return self.store.create_message("desktop", "question", "A oder B?", {"project": "WebApp"}, key, T0,
                                         buttons=["A", "B"])

    def test_outbox_send_marks_sent_and_links_tg_id(self):
        m = self.question()
        self.assertEqual(outbound.process_once(self.store, self.tg, CHAT, T0), 1)
        sent = self.fake.calls("sendMessage")[0]
        self.assertEqual((sent["chat_id"], sent["parse_mode"]), (CHAT, "HTML"))
        self.assertIn("inline_keyboard", sent["reply_markup"])
        msg = self.store.get_message(m["id"])
        self.assertEqual(msg["status"], "sent")
        self.assertEqual(self.store.message_for_tg(msg["tg_message_id"])["id"], m["id"])
        self.assertEqual(outbound.process_once(self.store, self.tg, CHAT, T0), 0)

    def test_edit_after_answer_removes_buttons(self):
        m = self.question()
        outbound.process_once(self.store, self.tg, CHAT, T0)
        self.store.answer_question(m["id"], {"text": "B"}, T0 + 1)
        outbound.process_once(self.store, self.tg, CHAT, T0 + 1)
        edit = self.fake.calls("editMessageText")[0]
        self.assertEqual(edit["message_id"], self.store.get_message(m["id"])["tg_message_id"])
        self.assertTrue(edit["text"].endswith("✔ beantwortet: B"))
        self.assertNotIn("reply_markup", edit)

    def test_outbox_429_retries_after_retry_after(self):
        self.question()
        self.fake.fail_next("sendMessage", 429, retry_after=3)
        outbound.process_once(self.store, self.tg, CHAT, T0)
        self.assertEqual(self.store.outbox_due(T0 + 2, 10), [])
        self.assertEqual(len(self.store.outbox_due(T0 + 3, 10)), 1)
        outbound.process_once(self.store, self.tg, CHAT, T0 + 3)
        self.assertEqual(len(self.fake.calls("sendMessage")), 1)

    def test_outbox_400_is_permanent_and_marks_failed(self):
        m = self.question()
        self.fake.fail_next("sendMessage", 400)
        outbound.process_once(self.store, self.tg, CHAT, T0)
        self.assertEqual(self.store.get_message(m["id"])["status"], "failed")
        self.assertEqual(self.store.outbox_due(T0 + 999, 10), [])

    def test_transient_errors_back_off_and_give_up_after_10_attempts(self):
        m = self.question()
        now = T0
        for attempt in range(10):
            self.fake.fail_next("sendMessage", 502)
            due = self.store.outbox_due(now, 10)
            self.assertEqual(len(due), 1, attempt)
            outbound.process_once(self.store, self.tg, CHAT, now)
            now += 301
        self.assertEqual(self.store.get_message(m["id"])["status"], "failed")

    def test_notice_is_escaped_and_replies(self):
        self.store.enqueue_notice("#123 <ist> erledigt", T0, reply_to_tg_id=77, buttons=[("#5 X", "a:1:2")])
        outbound.process_once(self.store, self.tg, CHAT, T0)
        sent = self.fake.calls("sendMessage")[0]
        self.assertEqual(sent["text"], "#123 &lt;ist&gt; erledigt")
        self.assertEqual(sent["reply_parameters"]["message_id"], 77)
        self.assertEqual(sent["reply_markup"]["inline_keyboard"], [[{"text": "#5 X", "callback_data": "a:1:2"}]])

    def test_setup_mode_only_delivers_notices_with_explicit_chat(self):
        self.question()
        self.store.enqueue_notice("Chat-ID: 12345", T0, chat_id=12345)
        self.assertEqual(outbound.process_once(self.store, self.tg, None, T0), 1)
        self.assertEqual(self.fake.calls("sendMessage"), [])
        self.assertEqual(outbound.process_once(self.store, self.tg, None, T0), 1)
        self.assertEqual([c["chat_id"] for c in self.fake.calls("sendMessage")], [12345])
        self.assertEqual(len(self.store.outbox_due(T0 + 60, 10)), 1)

    def test_notice_delete_removes_the_message(self):
        self.store.enqueue_notice_delete(55, T0)
        outbound.process_once(self.store, self.tg, CHAT, T0)
        self.assertEqual(self.fake.calls("deleteMessage"), [{"chat_id": CHAT, "message_id": 55}])

    def test_callback_answer_is_sent(self):
        self.store.enqueue_callback_answer("cb9", "ok", T0)
        outbound.process_once(self.store, self.tg, CHAT, T0)
        self.assertEqual(self.fake.calls("answerCallbackQuery")[0]["callback_query_id"], "cb9")

    def test_outbox_survives_restart_without_double_send(self):
        self.question()
        outbound.process_once(self.store, self.tg, CHAT, T0)
        self.store.close()
        self.store = Store(self.path)
        self.store.init_schema()
        self.assertEqual(outbound.process_once(self.store, self.tg, CHAT, T0 + 5), 0)
        self.assertEqual(len(self.fake.calls("sendMessage")), 1)

    def test_telegram_error_text_never_contains_token(self):
        from relay.telegram import TelegramError
        self.fake.fail_next("sendMessage", 400)
        with self.assertRaises(TelegramError) as ctx:
            self.tg.send_message(CHAT, "x")
        self.assertTrue(ctx.exception.permanent)
        self.assertNotIn(TOKEN, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
