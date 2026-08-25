from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import tempfile
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


if __name__ == "__main__":
    unittest.main()
