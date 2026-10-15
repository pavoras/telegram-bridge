import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from relay import format as fmt
from relay.daily import check_daily
from relay.i18n import berlin_rule as berlin
from relay.store import Store

CEST = timezone(timedelta(hours=2))


def at(hour, minute, day=16):
    return datetime(2026, 10, day, hour, minute, tzinfo=CEST).timestamp()


class DailyFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()
        self.store.set_daily(True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def notices(self):
        return [e["payload"]["text"] for e in self.store.outbox_due(at(23, 0, 30), 100) if e["kind"] == "notice"]

    def test_no_fallback_before_0815_or_when_report_arrived(self):
        self.assertFalse(check_daily(self.store, at(8, 14)))
        self.assertFalse(check_daily(self.store, at(12, 0)), "not in the afternoon (e.g. first start)")
        self.store.create_message("desktop", "daily", "Bericht", {}, "daily-2026-10-16", at(8, 2))
        self.assertFalse(check_daily(self.store, at(8, 15)))
        self.assertEqual(self.notices(), [])

    def test_fallback_once_per_day_with_open_questions_and_host_status(self):
        self.store.create_message("desktop", "daily", "gestern", {}, "daily-2026-10-15", at(8, 0, 15))
        self.store.create_message("desktop", "question", "A?", {"project": "WebApp"}, "q1", at(7, 0),
                                  title="Login")
        self.store.set_host_status({"monitor": "ok", "disk": "41 %"}, at(7, 30))
        self.assertTrue(check_daily(self.store, at(8, 16)))
        self.assertFalse(check_daily(self.store, at(9, 0)), "only once a day")
        [text] = self.notices()
        self.assertTrue(text.startswith("☀️ Daily Report vom PC fehlt"))
        self.assertIn("WebApp · Login", text)
        self.assertIn("monitor: ok", text)
        self.assertIn("vor 46 min", text)
        self.assertTrue(check_daily(self.store, at(8, 20, 17)), "next day again")

    def test_no_fallback_while_the_daily_report_is_off(self):
        self.store.set_daily(False)
        self.assertFalse(check_daily(self.store, at(8, 16)))
        self.assertEqual(self.notices(), [])

    def test_daily_report_is_off_until_switched_on(self):
        fresh = Store(os.path.join(self.tmp.name, "fresh.db"))
        fresh.init_schema()
        self.assertFalse(fresh.daily_enabled())
        fresh.set_daily(True)
        self.assertTrue(fresh.daily_enabled())
        fresh.close()

    def test_fallback_without_host_status(self):
        check_daily(self.store, at(10, 0))
        self.assertIn("Server: kein Host-Status", self.notices()[0])

    def test_berlin_time_follows_summer_time(self):
        utc = timezone.utc
        self.assertEqual(berlin(datetime(2026, 7, 1, 6, 15, tzinfo=utc).timestamp()).hour, 8)
        self.assertEqual(berlin(datetime(2026, 12, 1, 7, 15, tzinfo=utc).timestamp()).hour, 8)
        self.assertEqual(berlin(datetime(2026, 10, 25, 0, 59, tzinfo=utc).timestamp()).utcoffset(), timedelta(hours=2))
        self.assertEqual(berlin(datetime(2026, 10, 25, 1, 0, tzinfo=utc).timestamp()).utcoffset(), timedelta(hours=1))
        self.assertEqual(berlin(datetime(2026, 3, 29, 1, 0, tzinfo=utc).timestamp()).utcoffset(), timedelta(hours=2))

    def test_daily_header(self):
        msg = {"kind": "daily", "code": None, "title": "Do 16.10.", "text": "Alles gut",
               "origin": {"host": "Desktop", "agent": "Claude"}, "buttons": None}
        self.assertTrue(fmt.render_message(msg)[0].startswith("☀️ Daily Report · Do 16.10.\n🖥 Desktop · Claude"))


if __name__ == "__main__":
    unittest.main()
