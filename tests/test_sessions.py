import os
import tempfile
import unittest

from relay import format as fmt
from relay.inbound import handle_update
from relay.store import DEFAULT_DONE_DELAY, Store
from tests.test_inbound import CHAT, text_update

T0 = 1_800_000_000.0
ORIGIN = {"host": "Desktop", "account": "Konto A", "project": "WebApp", "agent": "Claude"}


class SessionPingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def event(self, kind, ts, key="s1", **kw):
        return self.store.session_event("desktop", key, kind, ts, origin=ORIGIN, **kw)

    def pings(self, now):
        self.store.send_due_pings(now)
        return [m for m in self.messages() if m["kind"] in ("done", "permission")]

    def messages(self):
        return [self.store.get_message_by_seq(s) for s in range(1, 50) if self.store.get_message_by_seq(s)]

    def test_stop_schedules_ping_after_default_delay(self):
        self.event("prompt", T0, human=True, title="Login fixen")
        self.assertEqual(self.event("stop", T0 + 5, text="Alles erledigt."), "scheduled")
        self.assertEqual(self.pings(T0 + 5 + DEFAULT_DONE_DELAY - 1), [])
        [ping] = self.pings(T0 + 5 + DEFAULT_DONE_DELAY)
        self.assertEqual((ping["kind"], ping["text"], ping["title"], ping["session_key"]),
                         ("done", "Alles erledigt.", "Login fixen", "s1"))
        self.assertEqual(ping["origin"]["project"], "WebApp")
        self.assertEqual(len(self.pings(T0 + 10 * DEFAULT_DONE_DELAY)), 1, "a ping is sent once")

    def test_prompt_cancels_scheduled_ping(self):
        self.event("stop", T0, text="fertig")
        self.assertEqual(self.event("prompt", T0 + 60, human=False), "cancelled")
        self.assertEqual(self.pings(T0 + 10 * DEFAULT_DONE_DELAY), [])

    def test_new_stop_replaces_pending_ping(self):
        self.event("stop", T0, text="erste")
        self.event("stop", T0 + 300, text="zweite")
        self.assertEqual(self.pings(T0 + DEFAULT_DONE_DELAY + 1), [])
        [ping] = self.pings(T0 + 300 + DEFAULT_DONE_DELAY)
        self.assertEqual(ping["text"], "zweite")

    def test_busy_stop_or_open_question_schedules_nothing(self):
        self.event("stop", T0, text="fertig")
        self.assertEqual(self.event("stop", T0 + 1, text="wartet", busy=True), "cancelled")
        q = self.store.create_message("desktop", "question", "A?", ORIGIN, "k", T0, session_key="s2")
        self.assertEqual(self.event("stop", T0 + 2, key="s2", text="fragt"), "cancelled")
        self.assertEqual(self.pings(T0 + 10 * DEFAULT_DONE_DELAY), [])
        self.assertEqual(self.store.get_message(q["id"])["status"], "queued")

    def test_done_delay_per_session_and_off(self):
        self.store.set_done_delay("desktop", "s1", 0, T0)
        self.event("stop", T0, text="sofort")
        self.assertEqual(len(self.pings(T0)), 1)
        self.store.set_done_delay("desktop", "s2", None, T0)
        self.store.set_done_delay("desktop", "s2", -1, T0)
        self.assertEqual(self.event("stop", T0, key="s2", text="nie"), "cancelled")
        self.assertEqual(len(self.pings(T0 + 10 * DEFAULT_DONE_DELAY)), 1)

    def test_stale_event_is_ignored(self):
        self.event("prompt", T0 + 100, human=False)
        self.assertEqual(self.event("stop", T0 + 50, text="alt"), "stale")
        self.assertEqual(self.pings(T0 + 10 * DEFAULT_DONE_DELAY), [])

    def test_human_prompt_closes_open_questions_of_this_session_only(self):
        mine = self.store.create_message("desktop", "question", "A?", ORIGIN, "k1", T0, session_key="s1")
        other = self.store.create_message("desktop", "question", "B?", ORIGIN, "k2", T0, session_key="s9")
        self.event("prompt", T0 + 1, human=False)
        self.assertEqual(self.store.get_message(mine["id"])["status"], "queued")
        self.event("prompt", T0 + 2, human=True)
        closed = self.store.get_message(mine["id"])
        self.assertEqual((closed["status"], closed["closed_reason"]), ("closed", "answered_in_app"))
        self.assertEqual(self.store.get_message(other["id"])["status"], "queued")

    def test_permission_ping_uses_its_own_kind(self):
        from relay import i18n
        self.event("permission", T0, text="Bash ausführen?")
        [ping] = self.pings(T0 + DEFAULT_DONE_DELAY)
        self.assertEqual(ping["kind"], "permission")
        since = i18n.local_time(T0).strftime("%H:%M")
        self.assertEqual(ping["text"], f"wartet seit {since}: Bash ausführen?", "the relay words it")

    def test_origin_is_frozen_at_first_contact(self):
        self.event("prompt", T0, human=True)
        self.store.session_event("desktop", "s1", "stop", T0 + 1, origin=dict(ORIGIN, account="Konto B"),
                                 text="x")
        [ping] = self.pings(T0 + DEFAULT_DONE_DELAY + 1)
        self.assertEqual(ping["origin"]["account"], "Konto A")

    def test_format_of_pings(self):
        self.event("prompt", T0, human=True, title="Login <fix>")
        self.event("stop", T0, text="Fertig & gut")
        [ping] = self.pings(T0 + DEFAULT_DONE_DELAY)
        text, markup = fmt.render_message(ping)
        self.assertTrue(text.startswith("✅ fertig · WebApp · Login &lt;fix&gt;\n🖥 Desktop · Konto A · Claude"))
        self.assertIn("Fertig &amp; gut", text)
        self.assertIsNone(markup)
        self.assertTrue(fmt.render_message(dict(ping, kind="permission"))[0].startswith("⏸ wartet auf Freigabe"))

    def test_reply_to_ping_explains_that_session_is_not_waiting(self):
        self.event("stop", T0, text="fertig")
        [ping] = self.pings(T0 + DEFAULT_DONE_DELAY)
        self.store.mark_sent(ping["id"], 777)
        handle_update(self.store, text_update("weiter so", reply_to=777), CHAT, T0 + 700)
        notices = [e["payload"]["text"] for e in self.store.outbox_due(T0 + 10**6, 100) if e["kind"] == "notice"]
        self.assertEqual(notices, ["Diese Session wartet nicht auf Telegram – bitte in der Claude-App antworten."])


if __name__ == "__main__":
    unittest.main()
