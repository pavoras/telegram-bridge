import itertools
import os
import tempfile
import unittest

from relay.inbound import handle_update
from relay.store import Store

T0 = 1_800_000_000.0
CHAT = 4711
_ids = itertools.count(1)


def text_update(text, chat=CHAT, sender=CHAT, reply_to=None, **extra):
    message = {"message_id": 5000 + next(_ids), "chat": {"id": chat, "type": "private"}, "from": {"id": sender},
               "text": text}
    if reply_to:
        message["reply_to_message"] = {"message_id": reply_to}
    message.update(extra)
    return {"update_id": next(_ids), "message": message}


def callback_update(data, chat=CHAT, sender=CHAT):
    return {"update_id": next(_ids), "callback_query": {"id": f"cb{next(_ids)}", "from": {"id": sender},
                                                        "message": {"message_id": 1, "chat": {"id": chat}},
                                                        "data": data}}


class InboundTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()
        self.tg_ids = itertools.count(900)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ask(self, key, project="WebApp", buttons=None, sent=True):
        m = self.store.create_message("desktop", "question", "Frage?", {"project": project}, key, T0,
                                      title="Titel " + key, buttons=buttons)
        if sent:
            self.store.mark_sent(m["id"], next(self.tg_ids))
        return self.store.get_message(m["id"])

    def handle(self, update, allowed=CHAT, now=T0 + 10):
        handle_update(self.store, update, allowed, now)

    def outbox(self, kind):
        return [e["payload"] for e in self.store.outbox_due(T0 + 10**6, 1000) if e["kind"] == kind]

    def notices(self):
        return [p["text"] for p in self.outbox("notice")]

    def status(self, msg):
        return self.store.get_message(msg["id"])["status"]

    def test_foreign_chat_is_ignored(self):
        q = self.ask("a")
        self.handle(text_update("hallo", chat=999, sender=999))
        self.handle(text_update("hallo", chat=CHAT, sender=999))
        self.assertEqual(self.status(q), "sent")
        self.assertEqual(self.notices(), [])

    def test_pairing_binds_the_chat_that_sends_the_code(self):
        code = self.store.pairing_code()
        self.assertRegex(code, r"^\d{6}$")
        self.assertEqual(self.store.pairing_code(), code, "stable until used")
        self.handle(text_update("/start", chat=12345, sender=12345), allowed=None)
        self.handle(text_update("irgendwas", chat=12345, sender=12345), allowed=None)
        self.handle(text_update(f"/start {code}", chat=-100, sender=12345), allowed=None)  # a group: ignored
        self.assertIsNone(self.store.paired_chat())
        self.handle(text_update(f"/start {code}", chat=12345, sender=12345), allowed=None)
        self.assertEqual(self.store.paired_chat(), 12345)
        notices = self.outbox("notice")
        self.assertEqual([n["chat_id"] for n in notices], [12345, 12345])
        self.assertIn("Kopplungscode", notices[0]["text"])
        self.assertTrue(notices[1]["text"].startswith("Verbunden."))
        self.handle(text_update(f"/start {code}", chat=999, sender=999), allowed=None)
        self.assertEqual(self.store.paired_chat(), 12345, "the first chat wins, the code is used up")

    def test_wrong_pairing_codes_replace_the_code(self):
        code = self.store.pairing_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.handle(text_update(f"/start {wrong}", chat=666, sender=666), allowed=None)
        self.assertNotEqual(self.store.pairing_code(), code, "five wrong tries: a new code (in the log)")
        self.assertIsNone(self.store.paired_chat())

    def test_english_texts_and_commands_in_both_languages(self):
        from relay import i18n
        i18n.configure("en", "UTC")
        self.addCleanup(i18n.configure, "de", "Europe/Berlin")
        self.handle(text_update("/help"))
        self.assertIn("/open – open questions", self.notices()[0])
        self.handle(text_update("/offen"))
        self.assertEqual(self.notices()[1], "No open questions.")
        self.handle(text_update("/away"))
        self.assertEqual(self.store.get_state("presence"), "away")
        self.handle(text_update("/daily on"))
        self.assertTrue(self.store.daily_enabled())
        self.handle(text_update("/daily aus"))
        self.assertFalse(self.store.daily_enabled())
        self.assertEqual(self.notices()[-1], "Daily report: off. /daily on switches it on.")

    def test_reply_to_question_answers_it(self):
        q = self.ask("a")
        self.ask("b")
        self.handle(text_update("Nimm B", reply_to=q["tg_message_id"]))
        msg = self.store.get_message(q["id"])
        self.assertEqual((msg["status"], msg["answer"]["text"]), ("answered", "Nimm B"))
        self.assertTrue(self.notices()[0].startswith(f"✔ an #{q['code']}"))

    def test_reply_to_non_question_is_rejected(self):
        self.ask("a")
        self.handle(text_update("x", reply_to=424242))
        self.assertEqual(self.notices(), ["Auf diese Nachricht kann man nicht antworten."])

    def test_hash_code_answers_open_question_and_strips_prefix(self):
        self.ask("a")
        q = self.ask("b")
        self.handle(text_update(f"#{q['code']} nimm Variante B"))
        self.assertEqual(self.store.get_message(q["id"])["answer"]["text"], "nimm Variante B")

    def test_hash_code_of_answered_question_is_not_redirected(self):
        a = self.ask("a")
        b = self.ask("b")
        self.store.answer_question(a["id"], {"text": "erste"}, T0 + 1)
        self.handle(text_update(f"#{a['code']} neue Antwort"))
        self.assertEqual(self.status(b), "sent")
        self.assertEqual(self.store.get_message(a["id"])["answer"]["text"], "erste")
        self.assertIn(f"#{a['code']} ist bereits beantwortet.", self.notices())

    def test_unknown_code_and_empty_answer(self):
        q = self.ask("a")
        self.handle(text_update("#1234 hallo"))
        self.handle(text_update(f"#{q['code']}"))
        self.assertEqual(self.notices(), ["#1234 gibt es nicht (mehr).", "Antworttext fehlt."])
        self.assertEqual(self.status(q), "sent")

    def test_single_open_question_gets_plain_text(self):
        q = self.ask("a")
        self.handle(text_update("ja, mach das"))
        self.assertEqual(self.store.get_message(q["id"])["answer"]["text"], "ja, mach das")

    def test_multiple_open_questions_offer_assignment_buttons_then_assign(self):
        a = self.ask("a", project="WebApp")
        b = self.ask("b", project="Wingbot")
        self.handle(text_update("Variante B bitte"))
        offer = self.outbox("notice")[0]
        self.assertEqual(offer["text"], "Welche Frage ist gemeint?")
        labels = [btn[0] for btn in offer["buttons"]]
        self.assertTrue(labels[0].startswith(f"#{a['code']} WebApp"))
        data_b = offer["buttons"][1][1]
        self.assertRegex(data_b, r"^a:\d+:%d$" % b["seq"])
        self.handle(callback_update(data_b))
        self.assertEqual(self.store.get_message(b["id"])["answer"]["text"], "Variante B bitte")
        self.assertEqual(self.status(a), "sent")
        self.assertEqual(len(self.outbox("callback_answer")), 1)

    def test_no_open_question_notice(self):
        self.handle(text_update("hallo?"))
        self.assertEqual(self.notices(), ["Keine offene Frage."])

    def test_button_double_tap_only_first_counts(self):
        q = self.ask("a", buttons=["A", "B"])
        self.handle(callback_update(f"b:{q['seq']}:1"))
        self.handle(callback_update(f"b:{q['seq']}:0"))
        msg = self.store.get_message(q["id"])
        self.assertEqual((msg["answer"]["text"], msg["answer"]["button"]), ("B", "B"))
        answers = [p["text"] for p in self.outbox("callback_answer")]
        self.assertEqual(answers, ["✔ B", f"#{q['code']} ist bereits beantwortet."])

    def test_expired_assignment_button_has_no_effect(self):
        self.ask("a")
        b = self.ask("b")
        self.handle(text_update("spät"), now=T0)
        data = self.outbox("notice")[0]["buttons"][1][1]
        self.handle(callback_update(data), now=T0 + 601)
        self.assertEqual(self.status(b), "sent")
        self.assertEqual(self.outbox("callback_answer")[0]["text"], "Diese Auswahl gilt nicht mehr (schon zugeordnet oder abgelaufen). Bitte neu senden.")

    def test_selection_message_is_deleted_after_choice(self):
        self.ask("a", project="WebApp")
        b = self.ask("b", project="Wingbot")
        self.handle(text_update("Variante B"))
        data_b = self.outbox("notice")[0]["buttons"][1][1]
        self.handle(callback_update(data_b))
        self.assertEqual(self.outbox("notice_delete"), [{"tg_message_id": 1}])
        self.assertEqual(self.store.get_message(b["id"])["status"], "answered")

    def test_used_or_expired_selection_is_deleted_too(self):
        self.ask("a")
        self.ask("b")
        self.handle(text_update("spät"), now=T0)
        data = self.outbox("notice")[0]["buttons"][0][1]
        self.handle(callback_update(data), now=T0 + 601)
        self.assertEqual(self.outbox("notice_delete"), [{"tg_message_id": 1}])

    def test_second_tap_on_used_assignment_has_no_effect(self):
        a = self.ask("a")
        b = self.ask("b")
        self.handle(text_update("nur einmal"))
        buttons = self.outbox("notice")[0]["buttons"]
        self.handle(callback_update(buttons[1][1]))
        self.handle(callback_update(buttons[0][1]))
        self.assertEqual(self.status(a), "sent")
        self.assertEqual(self.store.get_message(b["id"])["answer"]["text"], "nur einmal")
        self.assertEqual(self.outbox("callback_answer")[1]["text"],
                         "Diese Auswahl gilt nicht mehr (schon zugeordnet oder abgelaufen). Bitte neu senden.")

    def test_invalid_callback_data_is_answered(self):
        self.handle(callback_update("b:999:0"))
        self.handle(callback_update("kaputt"))
        self.assertEqual([p["text"] for p in self.outbox("callback_answer")], ["Ungültig.", "Ungültig."])

    def test_offen_lists_open_questions(self):
        a = self.ask("a")
        self.handle(text_update("/offen"), now=T0 + 600)
        self.assertEqual(self.notices(), [f"#{a['code']} · WebApp · Titel a · vor 10 min"])
        self.store.answer_question(a["id"], {"text": "x"}, T0 + 601)
        self.handle(text_update("/offen"))
        self.assertEqual(self.notices()[-1], "Keine offenen Fragen.")

    def test_presence_commands(self):
        self.handle(text_update("/weg"))
        self.assertEqual(self.store.get_state("presence"), "away")
        self.handle(text_update("/da@example_bot"))
        self.assertEqual(self.store.get_state("presence"), "present")

    def test_status_without_anything(self):
        self.store.set_daily(True)
        self.handle(text_update("/status"))
        self.assertEqual(self.notices(), ["Status: am Rechner\nOffene Fragen: 0\nLetzter Daily Report: noch keiner"],
                         "no server section on a relay that never got a host status (e.g. a second person's)")

    def test_status_with_questions_daily_and_host_status(self):
        self.ask("a")
        self.store.set_daily(True)
        self.store.create_message("desktop", "daily", "Bericht", {}, "daily-1", T0)
        self.store.set_host_status({"monitor": "ok", "disk": "15 %"}, T0 - 300)
        self.handle(text_update("/weg"))
        self.handle(text_update("/status"), now=T0 + 60)
        self.assertEqual(self.notices()[-1], "Status: unterwegs\nOffene Fragen: 1\n"
                                             "Letzter Daily Report: 15.01. 09:00\n\n"
                                             "Server (Stand vor 6 min):\n• monitor: ok\n• disk: 15 %")

    def test_daily_command_switches_the_report(self):
        self.handle(text_update("/daily"))
        self.assertEqual(self.notices()[-1], "Daily Report: aus. /daily an schaltet ihn ein.")
        self.handle(text_update("/daily an"))
        self.assertTrue(self.store.daily_enabled())
        self.assertEqual(self.notices()[-1], "Daily Report: an (08:00, Ersatzbericht um 08:15).")
        self.handle(text_update("/daily aus"))
        self.assertFalse(self.store.daily_enabled())
        self.assertEqual(self.notices()[-1], "Daily Report: aus. /daily an schaltet ihn ein.")
        self.handle(text_update("/daily vielleicht"))
        self.assertEqual(self.notices()[-1], "/daily an oder /daily aus")

    def test_status_shows_the_daily_switch(self):
        self.store.set_daily(True)
        self.handle(text_update("/status"))
        self.assertTrue(self.notices()[0].endswith("\nLetzter Daily Report: noch keiner"))
        self.store.set_daily(False)
        self.handle(text_update("/status"))
        self.assertTrue(self.notices()[1].endswith("\nDaily Report: aus"))

    def test_help_lists_every_menu_command(self):
        from relay.i18n import commands
        self.handle(text_update("/hilfe"))
        for command, _ in commands():
            self.assertIn(f"/{command} ", self.notices()[0])


if __name__ == "__main__":
    unittest.main()
