import base64
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

from tests import test_client
from tests.test_images import PNG

tg = test_client.tg


class McpTest(test_client.ClientTest):
    """`tg mcp`: the client as a stdio MCP server for agents without a shell."""

    def setUp(self):
        super().setUp()
        self.start_relay()
        self.server_state = tg.McpServer()
        self.server_state.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-ai"}}})

    def rpc(self, method, params=None, id=1):
        return self.server_state.handle({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})

    def call(self, name, **arguments):
        return self.rpc("tools/call", {"name": name, "arguments": arguments})["result"]

    def payload(self, result):
        self.assertFalse(result.get("isError"), result)
        return json.loads(result["content"][0]["text"])

    def test_initialize_and_tool_list(self):
        reply = tg.McpServer().handle({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "x"}}})
        self.assertEqual(reply["id"], 7)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", reply["result"]["capabilities"])
        self.assertEqual(reply["result"]["serverInfo"]["name"], "telegram-bridge")
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(set(tools), {"telegram_notify", "telegram_ask", "telegram_wait", "telegram_close",
                                      "telegram_status"})
        self.assertEqual(tools["telegram_ask"]["inputSchema"]["required"], ["text"])
        self.assertIsNone(self.server_state.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.rpc("ping")["result"], {})
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)

    def test_ask_then_wait_for_the_answer(self):
        asked = self.payload(self.call("telegram_ask", text="A oder B?", buttons=["A", "B"], title="Wahl",
                                       wait_seconds=0))
        self.assertEqual(asked["status"], "open")
        msg = self.store.get_message(asked["id"])
        self.assertEqual((msg["kind"], msg["text"], msg["title"], msg["buttons"]), ("question", "A oder B?", "Wahl",
                                                                                  ["A", "B"]))
        self.assertEqual(msg["origin"]["agent"], "claude-ai")
        self.assertTrue(msg["session_key"].startswith("mcp-"))
        self.assertNotIn("project", msg["origin"], "the server's working folder says nothing about the chat")
        self.assertEqual(self.payload(self.call("telegram_wait", id=asked["id"], wait_seconds=0))["status"], "open")
        self.store.answer_question(asked["id"], {"text": "B", "button": "B"}, time.time())
        waited = self.payload(self.call("telegram_wait", id=asked["id"], wait_seconds=5))
        self.assertEqual((waited["status"], waited["answer"]["text"]), ("answered", "B"))

    def test_notify_status_close_and_project(self):
        sent = self.payload(self.call("telegram_notify", text="Fertig", project="Notizen"))
        self.assertEqual(self.store.get_message(sent["id"])["origin"]["project"], "Notizen")
        asked = self.payload(self.call("telegram_ask", text="Noch da?", wait_seconds=0))
        status = self.payload(self.call("telegram_status"))
        self.assertEqual([q["id"] for q in status["open"]], [asked["id"]])
        self.assertTrue(self.payload(self.call("telegram_close", id=asked["id"]))["closed"])
        waited = self.payload(self.call("telegram_wait", id=asked["id"], wait_seconds=0))
        self.assertEqual((waited["status"], waited["reason"]), ("closed", "closed_by_agent"))

    def test_errors_are_tool_results_not_crashes(self):
        blocked = self.call("telegram_notify", text="token = ghp_" + "a1B2" * 9)
        self.assertTrue(blocked["isError"])
        self.assertIn("secret", blocked["content"][0]["text"])
        self.assertEqual(self.store.open_questions(), [])
        self.assertTrue(self.call("telegram_notify", text="-")["isError"], "'-' would read the protocol stream")
        self.assertTrue(self.call("telegram_ask", text="")["isError"])
        self.assertTrue(self.call("telegram_wait", id="")["isError"])
        self.assertEqual(self.rpc("tools/call", {"name": "rm_rf", "arguments": {}})["result"]["isError"], True)

    def test_answer_images_are_returned_as_image_content(self):
        path = os.path.join(self.tmp.name, "a.png")
        with open(path, "wb") as f:
            f.write(PNG)
        blocks = tg.mcp_image_blocks([path])
        self.assertEqual(blocks, [{"type": "image", "mimeType": "image/png",
                                   "data": base64.b64encode(PNG).decode()}])

    def test_mcp_config_prints_the_desktop_entry(self):
        code, out, _ = self.run_tg("mcp-config")
        self.assertEqual(code, 0)
        entry = json.loads(out)["mcpServers"]["telegram"]
        self.assertEqual(entry["args"][-1], "mcp")
        self.assertTrue(entry["args"][-2].endswith("tg.py"))
        self.assertTrue(os.path.isabs(entry["args"][-2]))

    def test_stdio_loop_writes_one_json_line_per_reply(self):
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "telegram_notify", "arguments": {"text": "über stdio"}}}]
        data = b"".join(json.dumps(line, ensure_ascii=False).encode("utf-8") + b"\n" for line in lines) + b"not json\n"
        stdin = io.TextIOWrapper(io.BytesIO(data), encoding="cp1252")
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="\r\n")
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            self.assertEqual(tg.main(["mcp"]), 0)
            stdout.flush()
            raw = stdout.buffer.getvalue()
        self.assertNotIn(b"\r", raw, "plain \\n line ends, also on Windows")
        replies = {r.get("id"): r for r in map(json.loads, raw.decode("utf-8").splitlines())}
        self.assertEqual(set(replies), {1, 2, None})
        self.assertEqual(replies[None]["error"]["code"], -32700)
        sent = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual(self.store.get_message(sent["id"])["text"], "über stdio")


# Run only the tests defined above, not the inherited ClientTest ones a second time.
for _name in [n for n in dir(test_client.ClientTest) if n.startswith("test_")]:
    if _name not in McpTest.__dict__:
        setattr(McpTest, _name, None)


if __name__ == "__main__":
    unittest.main()
