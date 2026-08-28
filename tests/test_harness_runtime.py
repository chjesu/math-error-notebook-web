from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from services.web_app.harness_runtime import HarnessRuntimeAdapter, HarnessRuntimeConfig


class HarnessRuntimeTests(unittest.TestCase):
    def config(self, root: Path) -> HarnessRuntimeConfig:
        return HarnessRuntimeConfig(
            project_root=root,
            cordis_config=root / "cordis.yml",
            runtime_entry=root / "runtime.js",
            image_admission_entry=root / "admit.mjs",
            session_root=root / "sessions",
            attachment_home=root / "attachments",
            projection_root=root / "projection",
        )

    def test_environment_selects_one_provider_without_changing_business_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "HARNESS_PROVIDER": "qwen-compatible",
            "HARNESS_MODEL": "qwen-vl-test",
            "HARNESS_MAX_TOKENS": "4096",
        }, clear=False):
            value = HarnessRuntimeConfig.from_environment(Path(directory))
        self.assertEqual((value.provider, value.model, value.max_tokens), ("qwen-compatible", "qwen-vl-test", 4096))

    def test_history_projection_is_durable_and_cursor_paginated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = HarnessRuntimeAdapter(self.config(Path(directory)))
            adapter._append_projection("session-test", "user", '{"message":"第一轮"}')
            adapter._append_projection("session-test", "assistant", '{"reply":"收到"}')
            page = adapter.read_history("session-test", limit=1)
            self.assertEqual(page["items"][0]["item"]["type"], "agentMessage")
            self.assertEqual(page["next_cursor"], "1")
            earlier = adapter.read_history("session-test", cursor=page["next_cursor"], limit=1)
            self.assertEqual(earlier["items"][0]["item"]["type"], "userMessage")

    def test_image_admission_returns_only_durable_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = HarnessRuntimeAdapter(self.config(Path(directory)))
            completed = subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=json.dumps([{
                    "attachmentId": "sha256:" + "a" * 64,
                    "mediaType": "image/png", "bytes": 4, "width": 1, "height": 1,
                }]), stderr="",
            )
            with patch("services.web_app.harness_runtime.shutil.which", return_value="node"), patch(
                "services.web_app.harness_runtime.subprocess.run", return_value=completed,
            ):
                values = adapter._admit_images([{"mediaType": "image/png", "data": "AAAA", "name": "q.png"}])
            self.assertEqual(values[0]["attachmentId"], "sha256:" + "a" * 64)

    def test_finish_failure_keeps_provider_code_out_of_public_classification(self) -> None:
        events = [{"type": "turn/end", "data": {"reason": {
            "kind": "error", "error": {"code": "INVALID_REQUEST", "message": "400 bad image"},
        }}}]
        kind, provider_code, diagnostic = HarnessRuntimeAdapter._finish_details(events)
        self.assertEqual((kind, provider_code), ("error", "INVALID_REQUEST"))
        self.assertIn("bad image", diagnostic)
        self.assertEqual(HarnessRuntimeAdapter._failure_code(diagnostic), "model_unavailable")

    def test_receipt_tool_returns_server_event_and_ends_turn(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required by the Harness runtime")
        module_uri = (Path(__file__).parents[1] / "extensions" / "dsh-math-notebook-ui" / "lib" / "index.js").as_uri()
        script = f"""
globalThis.fetch = async (url, options) => {{
  const body = JSON.parse(options.body);
  if (!url.endsWith('/' + 'a'.repeat(32) + '/commit')) throw new Error('wrong endpoint');
  if (body.session_id !== 'session-test' || body.input_version !== 3) throw new Error('wrong payload');
  return {{ok: true, status: 200, json: async () => ({{receipt: {{
    schema: 'math-notebook-entry-receipt/v1', status: 'saved', error_id: 'b'.repeat(32), reference_status: 'consistent',
    knowledge_point_count: 2, review_status: 'scheduled', message: '已计入错题本'
  }}}})}};
}};
const extension = await import({json.dumps(module_uri)});
let tool;
await extension.apply({{
  workspaceRegistry: {{create: async () => undefined}},
  tools: {{register: (value) => {{ tool = value; }}}}
}});
let concluded = 0;
const result = await tool.execute({{candidate_id: 'a'.repeat(32), input_version: 3}}, {{
  agent: {{id: 'session-test'}}, signal: new AbortController().signal,
  concludeTurn: () => {{ concluded += 1; }}
}});
if (result.status !== 'saved' || concluded !== 1) throw new Error('receipt did not conclude the turn');
const rendered = tool.output.render({{}}, result)[0].text;
if (!rendered.includes('错题编号：' + 'b'.repeat(32)) || !rendered.includes('知识点：2 个') || !rendered.includes('复习任务：已安排')) throw new Error('receipt details were not rendered');
"""
        environment = dict(os.environ)
        environment.update({
            "LZLM_PRODUCT_ORIGIN": "http://127.0.0.1:8000",
            "LZLM_HARNESS_INTERNAL_TOKEN": "synthetic-test-token",
            "LZLM_HARNESS_WORKSPACE_ROOT": str(Path.cwd()),
        })
        completed = subprocess.run(
            [node, "--input-type=module", "-e", script],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
