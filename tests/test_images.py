import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from relay import format as fmt
from relay import outbound
from relay.api import Clients, make_server
from relay.inbound import flush_albums, handle_update
from relay.store import Store
from relay.telegram import TelegramClient
from tests.fake_telegram import TOKEN, FakeTelegram
from tests.test_inbound import CHAT, text_update

T0 = 1_800_000_000.0
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100


def photo_update(file_id, caption=None, reply_to=None, group=None):
    update = text_update("", reply_to=reply_to)
    message = update["message"]
    del message["text"]
    message["photo"] = [{"file_id": file_id + "_small", "width": 90, "height": 90},
                        {"file_id": file_id, "width": 1280, "height": 1280}]
    if caption is not None:
        message["caption"] = caption
    if group:
        message["media_group_id"] = group
    return update


class InboundImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ask(self, key, tg_id):
        m = self.store.create_message("desktop", "question", "Wie sieht es aus?", {"project": "P"}, key, T0)
        self.store.mark_sent(m["id"], tg_id)
        return m["id"]

    def notices(self):
        return [e["payload"]["text"] for e in self.store.outbox_due(T0 + 10**6, 100) if e["kind"] == "notice"]

    def test_photo_reply_answers_with_caption_and_largest_image(self):
        qid = self.ask("a", 900)
        self.ask("b", 901)
        handle_update(self.store, photo_update("F1", caption="So!", reply_to=900), CHAT, T0 + 5)
        answer = self.store.get_message(qid)["answer"]
        self.assertEqual(answer["text"], "So!")
        [media_id] = answer["images"]
        self.assertEqual(self.store.get_media(media_id)["file_id"], "F1")
        self.assertEqual(self.store.get_media(media_id)["message_id"], qid)

    def test_photo_without_caption_goes_to_the_only_open_question(self):
        qid = self.ask("a", 900)
        handle_update(self.store, photo_update("F1"), CHAT, T0 + 5)
        answer = self.store.get_message(qid)["answer"]
        self.assertEqual((answer["text"], len(answer["images"])), ("", 1))

    def test_album_parts_are_collected_into_one_answer(self):
        qid = self.ask("a", 900)
        handle_update(self.store, photo_update("F1", caption="#{} zwei".format(
            self.store.get_message(qid)["code"]), group="G"), CHAT, T0)
        handle_update(self.store, photo_update("F2", group="G"), CHAT, T0 + 1)
        flush_albums(self.store, T0 + 2)
        self.assertEqual(self.store.get_message(qid)["status"], "sent", "album still collecting")
        flush_albums(self.store, T0 + 3)
        answer = self.store.get_message(qid)["answer"]
        self.assertEqual((answer["text"], len(answer["images"])), ("zwei", 2))
        flush_albums(self.store, T0 + 10)
        self.assertEqual(len(self.notices()), 1)

    def test_unsupported_attachment_is_refused(self):
        self.ask("a", 900)
        update = text_update("")
        del update["message"]["text"]
        update["message"]["document"] = {"file_id": "D1", "mime_type": "application/pdf"}
        handle_update(self.store, update, CHAT, T0)
        self.assertEqual(self.notices(), ["Nur Text und Bilder (PNG, JPEG, WEBP) werden unterstützt."])

    def test_final_render_mentions_images(self):
        qid = self.ask("a", 900)
        handle_update(self.store, photo_update("F1", caption="Da", reply_to=900), CHAT, T0)
        self.assertTrue(fmt.render_final(self.store.get_message(qid)).endswith("✔ beantwortet: Da (+1 Bild)"))


class MediaApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "relay.db"))
        self.store.init_schema()
        self.media_dir = os.path.join(self.tmp.name, "media")
        clients = os.path.join(self.tmp.name, "clients.json")
        with open(clients, "w", encoding="utf-8") as f:
            json.dump({name: {"sha256": hashlib.sha256(f"tok-{name}".encode()).hexdigest(), "rights": ["full"]}
                       for name in ("desktop", "laptop")}, f)
        self.fake = FakeTelegram()
        self.tg = TelegramClient(self.fake.start(), TOKEN)
        self.server = make_server(self.store, Clients(clients), "127.0.0.1", 0, lambda: {"ok": True},
                                  tg=self.tg, media_dir=self.media_dir)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.fake.stop()
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, client="desktop", raw=None, content_type="application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer tok-{client}", "Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=10) as r:
                payload = r.read()
                return r.status, (json.loads(payload) if r.headers.get_content_type() == "application/json"
                                  else (r.headers.get_content_type(), payload))
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.load(e)

    def upload(self, data=PNG, client="desktop"):
        return self.call("POST", "/v1/media", raw=data, client=client, content_type="application/octet-stream")

    def test_upload_and_send_message_with_images(self):
        status, media = self.upload()
        self.assertEqual(status, 201)
        status, created = self.call("POST", "/v1/messages", {"kind": "report", "text": "Screenshot",
                                                             "idempotency_key": "r1", "images": [media["id"]]})
        self.assertEqual(status, 201)
        for _ in range(3):
            outbound.process_once(self.store, self.tg, CHAT, time.time())
        [main] = self.fake.calls("sendMessage")
        [photo] = self.fake.calls("sendPhoto")
        self.assertEqual(photo["photo"]["size"], len(PNG))
        self.assertEqual(json.loads(photo["reply_parameters"])["message_id"],
                         self.store.get_message(created["id"])["tg_message_id"])
        self.assertIsNotNone(main)

    def test_photo_falls_back_to_document_on_permanent_error(self):
        _, media = self.upload(JPEG)
        self.call("POST", "/v1/messages", {"kind": "report", "text": "x", "idempotency_key": "r1",
                                           "images": [media["id"]]})
        outbound.process_once(self.store, self.tg, CHAT, time.time())
        self.fake.fail_next("sendPhoto", 400)
        outbound.process_once(self.store, self.tg, CHAT, time.time())
        self.assertEqual(len(self.fake.calls("sendDocument")), 1)

    def test_upload_validation(self):
        self.assertEqual(self.upload(b"%PDF-1.4 nope")[0], 415)
        self.assertEqual(self.upload(PNG + b"\x00" * (10 * 1024 * 1024))[0], 413)
        _, media = self.upload(client="laptop")
        status, _ = self.call("POST", "/v1/messages", {"kind": "report", "text": "x", "idempotency_key": "r1",
                                                       "images": [media["id"]]})
        self.assertEqual(status, 400, "images of another client are refused")

    def test_download_of_an_inbound_image(self):
        self.fake.files["F1"] = JPEG
        m = self.store.create_message("desktop", "question", "Bild?", {}, "k", T0)
        self.store.mark_sent(m["id"], 900)
        handle_update(self.store, photo_update("F1", reply_to=900), CHAT, T0)
        [media_id] = self.store.get_message(m["id"])["answer"]["images"]
        self.assertEqual(self.call("GET", f"/v1/media/{media_id}"), (200, ("image/jpeg", JPEG)))
        self.assertEqual(self.call("GET", f"/v1/media/{media_id}", client="laptop")[0], 404)


if __name__ == "__main__":
    unittest.main()
