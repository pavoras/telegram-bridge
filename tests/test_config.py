import os
import tempfile
import unittest

from relay.config import load_config


class LoadConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token_file = os.path.join(self.tmp.name, "bot-token")
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write("123:abc\n")
        self.clients_file = os.path.join(self.tmp.name, "clients.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_config_reads_token_file_and_defaults(self):
        cfg = load_config({"TG_BOT_TOKEN_FILE": self.token_file, "TG_CLIENTS_FILE": self.clients_file,
                           "TG_ALLOWED_CHAT_ID": "4711"})
        self.assertEqual(cfg.bot_token, "123:abc")
        self.assertEqual(cfg.allowed_chat_id, 4711)
        self.assertEqual(cfg.clients_file, self.clients_file)
        self.assertEqual(cfg.db_path, "/data/relay.db")
        self.assertEqual(cfg.telegram_base_url, "https://api.telegram.org")
        self.assertEqual((cfg.listen_host, cfg.listen_port), ("0.0.0.0", 8080))

    def test_load_config_missing_token_file_raises(self):
        with self.assertRaises(ValueError):
            load_config({"TG_BOT_TOKEN_FILE": os.path.join(self.tmp.name, "nope"),
                         "TG_CLIENTS_FILE": self.clients_file})

    def test_empty_chat_id_is_none(self):
        cfg = load_config({"TG_BOT_TOKEN_FILE": self.token_file, "TG_CLIENTS_FILE": self.clients_file,
                           "TG_ALLOWED_CHAT_ID": ""})
        self.assertIsNone(cfg.allowed_chat_id)

    def test_effortless_defaults(self):
        cfg = load_config({"TG_BOT_TOKEN": "123:abc"})
        self.assertEqual(cfg.bot_token, "123:abc")
        self.assertIsNone(cfg.clients_file, "devices are approved in Telegram; the file is optional")
        self.assertEqual((cfg.language, cfg.timezone), ("en", "UTC"))
        cfg = load_config({"TG_BOT_TOKEN": "123:abc", "TG_LANGUAGE": "de", "TG_TIMEZONE": "Europe/Berlin"})
        self.assertEqual((cfg.language, cfg.timezone), ("de", "Europe/Berlin"))
        with self.assertRaises(ValueError):
            load_config({"TG_BOT_TOKEN": "123:abc", "TG_LANGUAGE": "fr"})
        with self.assertRaises(ValueError):
            load_config({})

    def test_listen_and_overrides(self):
        cfg = load_config({"TG_BOT_TOKEN_FILE": self.token_file, "TG_CLIENTS_FILE": self.clients_file,
                           "TG_DB_PATH": "x.db", "TG_API_BASE": "http://127.0.0.1:9/",
                           "TG_LISTEN": "127.0.0.1:9000"})
        self.assertEqual(cfg.db_path, "x.db")
        self.assertEqual(cfg.telegram_base_url, "http://127.0.0.1:9")
        self.assertEqual((cfg.listen_host, cfg.listen_port), ("127.0.0.1", 9000))


if __name__ == "__main__":
    unittest.main()
