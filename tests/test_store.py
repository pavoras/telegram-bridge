import os
import tempfile
import unittest

from relay.store import Store

T0 = 1_800_000_000.0
DAY = 86400
ORIGIN = {"project": "WebApp", "host": "Desktop"}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "relay.db")
        self.store = Store(self.path)
        self.store.init_schema()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ask(self, key, now=T0, client="desktop", **kw):
        return self.store.create_message(client, "question", "Frage?", ORIGIN, key, now, **kw)

    def outbox_kinds(self, now=T0 + 10):
        return [e["kind"] for e in self.store.outbox_due(now, 1000)]

    def test_question_gets_code_in_range_and_report_has_none(self):
        q = self.ask("k1")
        r = self.store.create_message("desktop", "report", "Info", ORIGIN, "k2", T0)
        self.assertTrue(100 <= q["code"] <= 999)
        self.assertIsNone(r["code"])
        msg = self.store.get_message(q["id"])
        self.assertEqual(msg["status"], "queued")
        self.assertEqual(msg["expires_at"], T0 + DAY)
        self.assertEqual(msg["origin"], ORIGIN)

    def test_idempotent_create_returns_same_message_and_one_outbox_entry(self):
        a = self.ask("same")
        b = self.ask("same")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.outbox_kinds(), ["send"])
        other_client = self.ask("same", client="laptop")
        self.assertNotEqual(other_client["id"], a["id"])

    def test_code_not_reused_within_7_days(self):
        codes = set()
        for i in range(900):
            m = self.ask(f"k{i}", now=T0 + i)
            codes.add(m["code"])
            self.assertTrue(self.store.answer_question(m["id"], {"text": "ok"}, T0 + i))
        self.assertEqual(codes, set(range(100, 1000)))
        overflow = self.ask("overflow", now=T0 + 1000)
        self.assertTrue(1000 <= overflow["code"] <= 9999)
        later = self.ask("later", now=T0 + 7 * DAY + 1000)
        self.assertTrue(100 <= later["code"] <= 999)

    def test_first_state_change_wins(self):
        m = self.ask("k")
        self.assertTrue(self.store.answer_question(m["id"], {"text": "A"}, T0 + 1))
        self.assertFalse(self.store.answer_question(m["id"], {"text": "B"}, T0 + 2))
        self.assertFalse(self.store.close_question(m["id"], "answered_in_app", T0 + 3))
        msg = self.store.get_message(m["id"])
        self.assertEqual((msg["status"], msg["answer"]["text"]), ("answered", "A"))
        self.assertEqual(self.outbox_kinds(), ["send", "edit"])

    def test_expire_due_only_affects_open_questions_older_than_24h(self):
        old = self.ask("old", now=T0)
        answered = self.ask("answered", now=T0)
        self.store.answer_question(answered["id"], {"text": "x"}, T0 + 5)
        young = self.ask("young", now=T0 + DAY - 10)
        self.assertEqual(self.store.expire_due(T0 + DAY), 1)
        self.assertEqual(self.store.get_message(old["id"])["status"], "expired")
        self.assertEqual(self.store.get_message(answered["id"])["status"], "answered")
        self.assertEqual(self.store.get_message(young["id"])["status"], "queued")
        self.assertEqual([q["id"] for q in self.store.open_questions()], [young["id"]])

    def test_process_update_skips_duplicates_and_rolls_back_on_error(self):
        m = self.ask("k")

        def failing():
            self.store.answer_question(m["id"], {"text": "A"}, T0 + 1)
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.store.process_update(7, failing)
        self.assertEqual(self.store.get_message(m["id"])["status"], "queued")
        self.assertIsNone(self.store.get_state("tg_offset"))

        def ok():
            self.store.answer_question(m["id"], {"text": "A"}, T0 + 1)

        self.assertTrue(self.store.process_update(7, ok))
        self.assertEqual(self.store.get_message(m["id"])["status"], "answered")
        self.assertFalse(self.store.process_update(7, ok))
        self.assertFalse(self.store.process_update(6, ok))
        self.assertEqual(self.store.get_state("tg_offset"), "7")

    def test_assignment_can_be_taken_once_and_expires_after_600s(self):
        seq = self.store.create_assignment(555, {"text": "Antwort"}, T0)
        self.assertEqual(self.store.take_assignment(seq, T0 + 10), {"text": "Antwort"})
        self.assertIsNone(self.store.take_assignment(seq, T0 + 20))
        seq2 = self.store.create_assignment(556, {"text": "spät"}, T0)
        self.assertIsNone(self.store.take_assignment(seq2, T0 + 601))

    def test_find_by_code_returns_closed_message_with_status(self):
        m = self.ask("k")
        self.store.close_question(m["id"], "answered_in_app", T0 + 1)
        found = self.store.find_by_code(m["code"])
        self.assertEqual((found["id"], found["status"], found["closed_reason"]),
                         (m["id"], "closed", "answered_in_app"))
        self.assertIsNone(self.store.find_by_code(99))

    def test_outbox_fail_marks_message_failed(self):
        m = self.ask("k")
        entry = self.store.outbox_due(T0 + 1, 10)[0]
        self.store.outbox_fail(entry["id"])
        self.assertEqual(self.store.get_message(m["id"])["status"], "failed")
        self.assertEqual(self.store.outbox_due(T0 + 1, 10), [])

    def test_mark_sent_links_telegram_message_and_seq_lookup(self):
        m = self.ask("k", buttons=["A", "B"], title="Login")
        self.store.mark_sent(m["id"], 1234)
        msg = self.store.message_for_tg(1234)
        self.assertEqual((msg["id"], msg["status"], msg["buttons"], msg["title"]),
                         (m["id"], "sent", ["A", "B"], "Login"))
        self.assertEqual(self.store.get_message_by_seq(msg["seq"])["id"], m["id"])

    def test_complete_send_marks_message_and_outbox_in_one_step(self):
        m = self.ask("k")
        entry = self.store.outbox_due(T0, 10)[0]
        self.store.complete_send(entry["id"], m["id"], 4321)
        self.assertEqual(self.store.get_message(m["id"])["status"], "sent")
        self.assertEqual(self.store.message_for_tg(4321)["id"], m["id"])
        self.assertEqual(self.store.outbox_due(T0 + 10, 10), [])

    def test_outbox_retry_postpones_entry(self):
        self.ask("k")
        entry = self.store.outbox_due(T0, 10)[0]
        self.store.outbox_retry(entry["id"], T0 + 60)
        self.assertEqual(self.store.outbox_due(T0 + 59, 10), [])
        self.assertEqual(self.store.outbox_due(T0 + 60, 10)[0]["attempts"], 1)

    def test_notice_and_callback_answer_are_queued(self):
        self.store.enqueue_notice("Hallo", T0, reply_to_tg_id=5, buttons=[("#123 X", "a:1:2")])
        self.store.enqueue_callback_answer("cb1", "ok", T0)
        entries = self.store.outbox_due(T0, 10)
        self.assertEqual([e["kind"] for e in entries], ["notice", "callback_answer"])
        self.assertEqual(entries[0]["payload"], {"text": "Hallo", "reply_to": 5, "buttons": [["#123 X", "a:1:2"]]})

    def test_purge_old_removes_rows_older_than_7_days(self):
        old = self.ask("old", now=T0)
        new = self.ask("new", now=T0 + 7 * DAY)
        self.assertGreaterEqual(self.store.purge_old(T0 + 7 * DAY + 1), 1)
        self.assertIsNone(self.store.get_message(old["id"]))
        self.assertIsNotNone(self.store.get_message(new["id"]))

    def test_orphan_media_files_are_removed(self):
        media_dir = os.path.join(self.tmp.name, "media")
        os.makedirs(media_dir)

        def file(name, age):
            path = os.path.join(media_dir, name)
            with open(path, "wb") as f:
                f.write(b"x")
            os.utime(path, (T0 - age, T0 - age))
            return path

        known = file("known.png", 2 * DAY)
        self.store.add_outbound_media("known", "desktop", known, "image/png", 1, T0 - 2 * DAY)
        orphan = file("orphan.png", 2 * 3600)  # e.g. the relay died between file and row
        fresh = file("fresh.png", 60)  # an upload may be between file and row right now
        self.assertEqual(self.store.purge_orphan_files(media_dir, T0), 1)
        self.assertEqual(sorted(os.listdir(media_dir)), ["fresh.png", "known.png"])
        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(os.path.exists(fresh))
        self.assertEqual(self.store.purge_orphan_files(os.path.join(self.tmp.name, "missing"), T0), 0)

    def test_data_survives_reopen(self):
        m = self.ask("k")
        self.store.close()
        self.store = Store(self.path)
        self.store.init_schema()
        self.assertEqual(self.store.get_message(m["id"])["code"], m["code"])


if __name__ == "__main__":
    unittest.main()
