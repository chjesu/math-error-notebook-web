from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest import mock

from scripts import codex_app_server


class _Input(StringIO):
    def close(self) -> None:
        pass


class _Process:
    def __init__(self, lines: list[dict]) -> None:
        self.stdin = _Input()
        self.stdout = StringIO("".join(json.dumps(item) + "\n" for item in lines))
        self.stderr = StringIO("")
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -1


class CodexAppServerTests(unittest.TestCase):
    def test_turn_uses_official_thread_protocol_and_forwards_safe_events(self) -> None:
        result = {"action": "ready", "assistant_message": "可以确认"}
        process = _Process([
            {"id": 0, "result": {"userAgent": "test"}},
            {"id": 1, "result": {"thread": {"id": "thread-123"}}},
            {"id": 2, "result": {"turn": {"id": "turn-1"}}},
            {"method": "turn/started", "params": {"turn": {"status": "inProgress"}}},
            {"method": "item/agentMessage/delta", "params": {"delta": "{", "threadId": "thread-123", "turnId": "turn-1", "itemId": "item-1"}},
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": json.dumps(result)}, "threadId": "thread-123", "turnId": "turn-1", "completedAtMs": 1}},
            {"method": "turn/completed", "params": {"threadId": "thread-123", "turn": {"id": "turn-1", "items": [], "status": "completed"}}},
        ])
        events = []
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            output = Path(temporary) / "result.json"
            image = Path(temporary) / "question.png"
            image.write_bytes(b"synthetic")
            route = {"model": "test", "reasoning_effort": "low", "schema": str(Path(__file__).resolve().parents[1] / "schemas" / "math-loop-turn.schema.json")}
            with mock.patch.object(codex_app_server.shutil, "which", return_value="codex"), mock.patch.object(
                codex_app_server.subprocess, "Popen", return_value=process,
            ), mock.patch.object(codex_app_server, "_codex_environment", return_value={}):
                value = codex_app_server.run_turn(route=route, prompt="synthetic", output_path=output, images=[image], event_callback=events.append)
        sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(value, {"thread_id": "thread-123", "result": result})
        self.assertEqual([item["method"] for item in sent[:4]], ["initialize", "initialized", "thread/start", "turn/start"])
        self.assertEqual(sent[2]["params"]["sandbox"], "read-only")
        self.assertEqual(sent[2]["params"]["approvalPolicy"], "never")
        self.assertEqual(sent[3]["params"]["input"], [
            {"type": "text", "text": "synthetic"},
            {"type": "localImage", "path": str(image)},
        ])
        self.assertEqual([event["type"] for event in events], ["turn_started", "agent_message_delta", "item_completed", "turn_completed"])

    def test_history_uses_official_cursor_protocol(self) -> None:
        entries = [{"turnId": "turn-1", "item": {"id": "item-1", "type": "agentMessage", "text": "{}"}}]
        process = _Process([
            {"id": 0, "result": {"userAgent": "test"}},
            {"id": 1, "result": {"data": entries, "nextCursor": "older-page"}},
        ])
        with mock.patch.object(codex_app_server.shutil, "which", return_value="codex"), mock.patch.object(
            codex_app_server.subprocess, "Popen", return_value=process,
        ), mock.patch.object(codex_app_server, "_codex_environment", return_value={}):
            value = codex_app_server.list_thread_items(thread_id="thread-123", cursor="page-1", limit=20)
        sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(value, {"items": entries, "next_cursor": "older-page"})
        self.assertEqual([item["method"] for item in sent], ["initialize", "initialized", "thread/items/list"])
        self.assertTrue(sent[0]["params"]["capabilities"]["experimentalApi"])
        self.assertEqual(sent[2]["params"], {"threadId": "thread-123", "limit": 20, "sortDirection": "desc", "cursor": "page-1"})

    def test_turn_interrupt_uses_the_official_turn_method(self) -> None:
        process = _Process([
            {"id": 0, "result": {}},
            {"id": 1, "result": {"thread": {"id": "thread-123"}}},
            {"id": 2, "result": {"turn": {"id": "turn-1"}}},
            {"id": 3, "result": {}},
            {"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "interrupted"}}},
        ])
        cancellation = Event()
        cancellation.set()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            output = Path(temporary) / "result.json"
            route = {"model": "test", "reasoning_effort": "low", "schema": str(Path(__file__).resolve().parents[1] / "schemas" / "math-loop-turn.schema.json")}
            with mock.patch.object(codex_app_server.shutil, "which", return_value="codex"), mock.patch.object(
                codex_app_server.subprocess, "Popen", return_value=process,
            ), mock.patch.object(codex_app_server, "_codex_environment", return_value={}):
                with self.assertRaises(codex_app_server.AppServerError) as raised:
                    codex_app_server.run_turn(route=route, prompt="synthetic", output_path=output, cancel_event=cancellation)
        sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(raised.exception.public_code, "model_interrupted")
        self.assertIn({"method": "turn/interrupt", "id": 3, "params": {"threadId": "thread-123", "turnId": "turn-1"}}, sent)

    def test_compaction_uses_the_official_thread_method(self) -> None:
        process = _Process([
            {"id": 0, "result": {}},
            {"id": 1, "result": {"thread": {"id": "thread-123"}}},
            {"id": 2, "result": {}},
            {"method": "thread/compacted", "params": {"threadId": "thread-123"}},
        ])
        with mock.patch.object(codex_app_server.shutil, "which", return_value="codex"), mock.patch.object(
            codex_app_server.subprocess, "Popen", return_value=process,
        ), mock.patch.object(codex_app_server, "_codex_environment", return_value={}):
            value = codex_app_server.compact_thread(thread_id="thread-123")
        sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        self.assertEqual(value, {"thread_id": "thread-123", "status": "completed"})
        self.assertEqual([item["method"] for item in sent], ["initialize", "initialized", "thread/resume", "thread/compact/start"])


if __name__ == "__main__":
    unittest.main()
