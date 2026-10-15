import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from relay.inbound import handle_update
from relay.store import DEFAULT_DONE_DELAY
from relay.telegram import TelegramClient
from tests import test_client
from tests.fake_telegram import TOKEN, FakeTelegram
from tests.test_images import JPEG, PNG, photo_update

tg = test_client.tg
CHAT = 4711


def stdin(payload):
    return mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")),
                                                            encoding="cp1252"))


class ClientPhase2Test(test_client.ClientTest):
    """Phase 2 client features; reuses ClientTest's fixtures. Its own tests are skipped here."""

    def hook(self, event, payload):
        with stdin(payload):
            return self.run_tg("hook", event)

    def pings(self, after):
        self.store.send_due_pings(time.time() + after)
        messages = (self.store.get_message_by_seq(i) for i in range(1, 30))
        return [m for m in messages if m and m["kind"] in ("done", "permission")]

    def write_cfg(self, **changes):
        with open(self.config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.update(changes)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return cfg

    def test_hook_stop_schedules_ping_with_summary(self):
        self.start_relay()
        self.hook("user-prompt", {"session_id": "s-hook", "prompt": "Bitte Login fixen", "session_title": "Löwe",
                                  "cwd": "C:\\Projekte\\WebApp"})
        code, out, _ = self.hook("stop", {"session_id": "s-hook", "cwd": "C:\\Projekte\\WebApp",
                                          "last_assistant_message": "Erledigt: Login ü " + "x" * 900,
                                          "background_tasks": []})
        self.assertEqual((code, out), (0, ""))
        [ping] = self.pings(DEFAULT_DONE_DELAY + 1)
        self.assertEqual((ping["kind"], ping["title"], ping["origin"]["project"]), ("done", "Löwe", "WebApp"))
        self.assertTrue(ping["text"].startswith("Erledigt: Login ü"))
        self.assertLessEqual(len(ping["text"]), 600)

    def test_hook_stop_with_running_background_task_sends_no_ping(self):
        self.start_relay()
        self.hook("stop", {"session_id": "s-bg", "last_assistant_message": "warte",
                           "background_tasks": [{"id": "b1", "status": "running"}]})
        self.assertEqual(self.pings(10 * DEFAULT_DONE_DELAY), [])

    def test_hook_secret_in_summary_is_replaced(self):
        self.start_relay()
        self.hook("stop", {"session_id": "s-sec", "last_assistant_message": "token=abcdefghijk123"})
        [ping] = self.pings(DEFAULT_DONE_DELAY + 1)
        self.assertEqual(ping["text"], "(Kurzfassung gesperrt: möglicher Secret)")

    def test_prompt_hook_tells_the_session_when_the_user_is_away(self):
        self.start_relay()
        code, out, _ = self.hook("user-prompt", {"session_id": "s-a", "prompt": "<task-notification>x"})
        self.assertEqual((code, out), (0, ""), "present: no extra context")
        self.store.set_state("presence", "away")
        code, out, _ = self.hook("user-prompt", {"session_id": "s-a", "prompt": "<task-notification>y"})
        self.assertEqual(code, 0)
        output = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        self.assertIn("`telegram` skill", output["additionalContext"])
        code, out, _ = self.hook("stop", {"session_id": "s-a", "last_assistant_message": "x"})
        self.assertEqual(out, "", "only the prompt hook adds context")

    def test_human_prompt_closes_question_but_automatic_prompt_does_not(self):
        self.start_relay()
        message_id = self.run_tg("ask", "Frage?")[1].split()[0]
        self.hook("user-prompt", {"session_id": "sess-1", "prompt": "<task-notification>fertig</task-notification>"})
        self.assertEqual(self.store.get_message(message_id)["status"], "queued")
        self.hook("user-prompt", {"session_id": "sess-1", "prompt": "Nimm B"})
        self.assertEqual(self.store.get_message(message_id)["closed_reason"], "answered_in_app")

    def test_permission_notification_and_other_notifications(self):
        self.start_relay()
        self.hook("notification", {"session_id": "s-n", "notification_type": "idle_prompt", "message": "idle"})
        self.assertEqual(self.pings(10 * DEFAULT_DONE_DELAY), [])
        self.hook("notification", {"session_id": "s-n", "notification_type": "permission_prompt",
                                   "message": "Claude needs your permission to use Bash"})
        [ping] = self.pings(DEFAULT_DONE_DELAY + 1)
        self.assertEqual(ping["kind"], "permission")
        self.assertRegex(ping["text"], r"^wartet seit \d\d:\d\d: Claude needs your permission to use Bash$")

    def test_hook_spools_while_relay_is_down_and_flushes_in_order(self):
        self.start_relay()
        good_url = self.write_cfg()["relay_url"]
        self.write_cfg(relay_url="http://127.0.0.1:9")
        self.assertEqual(self.hook("stop", {"session_id": "s-sp", "last_assistant_message": "eins"})[0], 0)
        spool = Path(self.config_path).parent / "spool"
        self.assertEqual(len(list(spool.glob("*.json"))), 1)
        self.write_cfg(relay_url=good_url)
        self.hook("stop", {"session_id": "s-other", "last_assistant_message": "zwei"})
        self.assertEqual(list(spool.glob("*.json")), [])
        self.assertEqual(sorted(p["text"] for p in self.pings(DEFAULT_DONE_DELAY + 1)), ["eins", "zwei"])

    def test_hook_never_fails_without_config(self):
        self.assertEqual(self.hook("stop", {"session_id": "x"})[0], 0)

    def test_session_done_delay_and_done_command(self):
        self.start_relay()
        self.assertEqual(self.run_tg("session", "--done-delay", "0")[0], 0)
        self.hook("stop", {"session_id": "sess-1", "last_assistant_message": "sofort"})
        self.assertEqual([p["text"] for p in self.pings(0)], ["sofort"])
        self.assertEqual(self.run_tg("session", "--done-delay", "off", "--session", "s2")[0], 0)
        self.assertEqual(self.run_tg("session", "--done-delay", "abc")[0], 1)
        code, out, _ = self.run_tg("done", "Codex-Runde fertig", "--agent", "Codex")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.get_message(out.strip())["kind"], "done")

    def test_notify_with_image_uploads_it(self):
        self.start_relay()
        image = Path(self.tmp.name) / "shot.png"
        image.write_bytes(PNG)
        code, out, _ = self.run_tg("notify", "Screenshot", "--image", str(image))
        self.assertEqual(code, 0)
        [media_id] = self.store.get_message(out.strip())["images"]
        self.assertEqual(self.store.get_media(media_id)["size"], len(PNG))
        (Path(self.tmp.name) / "doc.txt").write_text("kein Bild")
        self.assertEqual(self.run_tg("notify", "x", "--image", str(Path(self.tmp.name) / "doc.txt"))[0], 1)

    def test_wait_downloads_answer_images(self):
        fake = FakeTelegram()
        self.addCleanup(fake.stop)
        fake_url = fake.start()
        fake.files["F1"] = JPEG
        self.start_relay(TelegramClient(fake_url, TOKEN))
        message_id = self.run_tg("ask", "Wie sieht es aus?")[1].split()[0]
        self.store.mark_sent(message_id, 900)
        handle_update(self.store, photo_update("F1", caption="So", reply_to=900), CHAT, time.time())
        with mock.patch.dict(os.environ, {"TG_DOWNLOAD_DIR": os.path.join(self.tmp.name, "downloads")}):
            code, out, _ = self.run_tg("wait", message_id, "--timeout", "5")
        self.assertEqual(code, 0)
        answer = json.loads(out)["answer"]
        self.assertEqual(answer["text"], "So")
        [path] = answer["images"]
        self.assertEqual(Path(path).read_bytes(), JPEG)

    def test_register_sends_hash_for_approval(self):
        self.start_relay()
        self.assertEqual(self.run_tg("register")[0], 1, "desktop is already in tg-clients.json")
        self.write_cfg(name="laptop")
        code, out, _ = self.run_tg("register")
        self.assertEqual(code, 0)
        self.assertIn("Telegram", out)

    def test_install_sets_up_skill_hooks_and_claude_md(self):
        claude = Path(self.tmp.name) / "claude"
        claude.mkdir()
        spike = 'py -3 "C:\\x\\telegram-bridge\\spikes\\hooklog.py" Stop'
        (claude / "settings.json").write_text(json.dumps({"hooks": {
            "PreCompact": [{"hooks": [{"type": "command", "command": "echo keep"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": spike}]}],
        }, "autoMode": True}), encoding="utf-8")
        (claude / "CLAUDE.md").write_text("# Global rules\n\n- other rule\n", encoding="utf-8")
        self.start_relay()
        for _ in range(2):
            code, out, err = self.run_tg("install", "--claude-dir", str(claude))
            self.assertEqual(code, 0, err)
        skill = claude / "skills" / "telegram"
        self.assertTrue((skill / "tg.py").exists())
        skill_md = (skill / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: telegram", skill_md)
        self.assertIn((skill / "tg.py").as_posix(), skill_md)
        self.assertNotIn("{{TG}}", skill_md)
        self.assertTrue((skill / "AGENT-INSTRUCTIONS.md").exists())
        settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        self.assertTrue(settings["autoMode"])
        hooks = settings["hooks"]
        self.assertEqual(hooks["PreCompact"][0]["hooks"][0]["command"], "echo keep")
        commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
        self.assertFalse(any("hooklog" in c for c in commands))
        self.assertEqual(sum("hook stop" in c for c in commands), 1)
        self.assertEqual(hooks["Notification"][0]["matcher"], "permission_prompt")
        self.assertIn("hook user-prompt", hooks["UserPromptSubmit"][0]["hooks"][0]["command"])
        claude_md = (claude / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(claude_md.count("`telegram`"), 1)
        self.assertIn("- other rule", claude_md)
        self.assertFalse((claude / "settings.json").read_bytes().startswith(b"\xef\xbb\xbf"))


    def test_install_outside_the_repository_fetches_templates_from_the_relay(self):
        repo = Path(__file__).resolve().parent.parent
        files = Path(self.tmp.name) / "client-files"
        files.mkdir()
        for template in (repo / "integrations" / "claude" / "SKILL.md",
                         repo / "integrations" / "agents" / "AGENT-INSTRUCTIONS.md"):
            (files / template.name).write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        self.start_relay(client_dir=str(files))
        lone = Path(self.tmp.name) / "download" / "tg.py"
        lone.parent.mkdir()
        lone.write_text("# downloaded copy\n", encoding="utf-8")
        claude = Path(self.tmp.name) / "claude"
        with mock.patch.object(tg, "__file__", str(lone)):
            code, out, err = self.run_tg("install", "--claude-dir", str(claude))
        self.assertEqual(code, 0, err)
        skill = claude / "skills" / "telegram"
        self.assertEqual((skill / "tg.py").read_text(encoding="utf-8"), "# downloaded copy\n")
        self.assertIn("name: telegram", (skill / "SKILL.md").read_text(encoding="utf-8"))
        self.assertNotIn("{{TG}}", (skill / "AGENT-INSTRUCTIONS.md").read_text(encoding="utf-8"))


    def test_daily_reads_text_from_stdin_and_is_sent_once_per_day(self):
        self.start_relay()
        self.store.set_daily(True)
        with mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO("Bericht\nZeile 2 ü".encode()))):
            code, first, err = self.run_tg("daily", "-", "--date", "2026-10-16")
        self.assertEqual(code, 0, err)
        msg = self.store.get_message(first.strip())
        self.assertEqual((msg["kind"], msg["text"], msg["title"]), ("daily", "Bericht\nZeile 2 ü", "Fr 16.10."))
        code, second, _ = self.run_tg("daily", "nochmal", "--date", "2026-10-16")
        self.assertEqual(second, first, "same day = same message")

    def test_daily_switch_and_a_report_while_off(self):
        self.start_relay()
        code, out, err = self.run_tg("daily", "--on")
        self.assertEqual((code, out.strip()), (0, "daily report: on"), err)
        self.assertTrue(self.store.daily_enabled())
        code, out, err = self.run_tg("daily", "--off")
        self.assertEqual((code, out.strip()), (0, "daily report: off"), err)
        code, out, err = self.run_tg("daily", "Bericht", "--date", "2026-10-16")
        self.assertEqual((code, out), (0, ""), "switched off is not an error for the scheduled task")
        self.assertIn("switched off", err)
        code, _, err = self.run_tg("daily")
        self.assertEqual(code, 1, "text or --on/--off is required")


# Run only the tests defined above, not the inherited ClientTest ones a second time.
for _name in [n for n in dir(test_client.ClientTest) if n.startswith("test_")]:
    if _name not in ClientPhase2Test.__dict__:
        setattr(ClientPhase2Test, _name, None)

if __name__ == "__main__":
    unittest.main()
