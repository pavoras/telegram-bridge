import unittest

from relay import i18n

from relay import format as fmt

ORIGIN = {"project": "WebApp", "host": "Desktop", "account": "Konto A", "agent": "Claude"}


def message(**kw):
    base = {"id": "m1", "seq": 42, "code": 482, "kind": "question", "status": "sent", "title": "Login-Guard fixen",
            "session_key": "s1", "text": "Variante A oder B?", "buttons": None, "origin": ORIGIN,
            "answer": None, "closed_reason": None}
    base.update(kw)
    return base


class FormatTest(unittest.TestCase):
    def test_render_escapes_html_and_truncates_long_text(self):
        text, _ = fmt.render_message(message(text="<b>&" + "x" * 5000))
        body = text.split("\n\n", 1)[1]
        self.assertTrue(body.startswith("&lt;b&gt;&amp;"))
        self.assertTrue(body.endswith("… (gekürzt)"))
        self.assertLessEqual(len(fmt.truncate("y" * 5000)), 3500)
        self.assertEqual(fmt.truncate("kurz"), "kurz")

    def test_render_question_header_and_buttons(self):
        text, markup = fmt.render_message(message(buttons=["A", "B", "C", "Später"]))
        lines = text.split("\n")
        self.assertEqual(lines[0], "❓ #482 · WebApp · Login-Guard fixen")
        self.assertEqual(lines[1], "🖥 Desktop · Konto A · Claude")
        rows = markup["inline_keyboard"]
        self.assertEqual([len(r) for r in rows], [3, 1])
        self.assertEqual(rows[1][0], {"text": "Später", "callback_data": "b:42:3"})
        for row in rows:
            for button in row:
                self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_render_report_has_no_code_and_no_buttons(self):
        text, markup = fmt.render_message(message(kind="report", code=None, title="Build fertig"))
        self.assertTrue(text.startswith("📋 WebApp · Build fertig\n"))
        self.assertIsNone(markup)

    def test_render_omits_missing_origin_fields(self):
        text, _ = fmt.render_message(message(origin={"project": "WebApp"}, title=None, session_key=None))
        lines = text.split("\n")
        self.assertEqual(lines[0], "❓ #482 · WebApp")
        self.assertEqual(lines[1], "")
        self.assertNotIn("None", text)

    def test_render_final_status_lines(self):
        answered = fmt.render_final(message(status="answered", answer={"text": "<B>"}))
        self.assertTrue(answered.endswith("✔ beantwortet: &lt;B&gt;"))
        self.assertTrue(fmt.render_final(message(status="closed", closed_reason="answered_in_app"))
                        .endswith("✔ in der App erledigt"))
        self.assertTrue(fmt.render_final(message(status="expired", closed_reason="expired")).endswith("⌛ abgelaufen"))
        self.assertTrue(fmt.render_final(message(status="closed", closed_reason="closed_by_agent"))
                        .endswith("✖ geschlossen"))



class EnglishFormatTest(unittest.TestCase):
    def setUp(self):
        i18n.configure("en", "UTC")
        self.addCleanup(i18n.configure, "de", "Europe/Berlin")

    def test_headers_and_final_states_in_english(self):
        done = {"kind": "done", "code": None, "title": "Build", "text": "", "origin": {"project": "App"},
                "buttons": None}
        self.assertEqual(fmt.render_message(done)[0], "✅ done · App · Build")
        question = {"kind": "question", "code": 482, "title": None, "text": "A?", "origin": {}, "buttons": None,
                    "status": "expired", "closed_reason": "expired"}
        self.assertTrue(fmt.render_final(question).endswith("⌛ expired"))


if __name__ == "__main__":
    unittest.main()
