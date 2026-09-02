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
    def test_product_tool_schemas_match_installed_harness_runtime(self) -> None:
        node = shutil.which("node")
        root = Path(__file__).parents[1]
        if node is None or not (root / "node_modules/@deepseek-ai/dsh-tools/lib/index.js").is_file():
            self.skipTest("Installed Harness is needed for native schema validation")
        script = """
import {assertSupportedJsonSchema, validateJsonSchemaValue} from '@deepseek-ai/dsh-tools';
import {apply} from './extensions/dsh-math-notebook-ui/lib/index.js';
const definitions = [];
await apply({workspaceRegistry: {create: async () => {}}, attachments: {}, tools: {register: tool => {
  // Registration restricts output schemas; parameter constraints are handled separately.
  assertSupportedJsonSchema(tool.output.schema);
  definitions.push(tool);
}}});
const tool = definitions.find(tool => tool.name === 'confirm_error_notebook_entry');
let imageTurnBlocked = false;
try {
  await tool.execute({candidate_id:'a'.repeat(32),input_version:1}, {agent:{id:'s',session:{deriveMessages:()=>[{role:'user',content:[{type:'image'}]}]}},signal:new AbortController().signal});
} catch (error) {
  imageTurnBlocked = String(error.message).includes('本轮禁止再次调用确认工具');
}
if (!imageTurnBlocked) throw new Error('image turn must not call the follow-up confirmation tool');
for (const status of ['review_waiting', 'review_completed', 'review_corrected', 'review_needs_correction', 'review_unmatched', 'review_stale', 'review_retryable']) {
  const value = {schema:'math-notebook-entry-receipt/v1', status, reference_status:'not_found',
    knowledge_point_count:1, review_status:'completed', message:'已记录',
    completed_question_count:2, required_question_count:2, next_stage:null, next_due_at:null, replayed:true};
  const issues = validateJsonSchemaValue(tool.output.schema, value);
  if (issues.length) throw new Error(issues.join('; '));
  const text = tool.output.render({},value)[0].text;
  if (!text.includes('下一步：') || text.includes('undefined')) throw new Error('invalid review receipt rendering');
}
"""
        environment = dict(os.environ, LZLM_PRODUCT_ORIGIN="http://127.0.0.1:8000", LZLM_HARNESS_INTERNAL_TOKEN="synthetic-test-token", LZLM_HARNESS_WORKSPACE_ROOT=str(root))
        result = subprocess.run([node, "--input-type=module", "-e", script], cwd=root, env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hybrid_review_tools_refresh_pending_batch_and_return_server_receipts(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required by the Harness runtime")
        root = Path(__file__).parents[1]
        module_uri = (root / "extensions" / "dsh-math-notebook-ui" / "lib" / "index.js").as_uri()
        script = """
const extension = await import(__MODULE__);
const ids = {old:'a'.repeat(32), other:'b'.repeat(32), fresh:'c'.repeat(32)};
const codes = {
  old:'R111111111111-01-ABCDEF', other:'R222222222222-02-ABCDEF', fresh:'R333333333333-03-ABCDEF'
};
const option = (code, stem) => ({code, pdf_id:'d'.repeat(32), pdf_name:'practice.pdf', error_id:'e'.repeat(32),
  question_id:'f'.repeat(32), kind:'recommendation', stage:1, stem_text:stem, match_score:0.98,
  candidate_source:'semantic_candidate', generated_at:null, started:false});
const result = (candidate_id, question_text, code, reference_review=null) => ({
  item_no: candidate_id === ids.old ? 1 : 2, candidate_id, input_version:1, verdict:'correct', question_text,
  answer_text:'x=1', first_error:'', cause_code:'', cause_evidence:'', knowledge_points:['方程'],
  correct_solution:'解答', final_answer:'x=1', prevention_cue:'检查', receipt_status:'review_unmatched',
  receipt_message:'待关联', error_id:'', review_association:{status:'unmatched',pdf_id:'',review_code:'',
    error_id:'',question_id:'',stage:0,kind:''}, review_match_candidates:[option(code, question_text)], reference_review
});
let practiceCalls = 0;
globalThis.fetch = async (url, options) => {
  const body = JSON.parse(options.body);
  if (url.endsWith('/v1/internal/harness/intakes/process')) return {ok:true,status:200,json:async()=>({results:[
    result(ids.old, 'q-old', codes.old, {source_title:'题库',version_no:1,independent_answer:'1',reference_answer:'1',reference_solution:'解'}),
    result(ids.other, 'q-other', codes.other)
  ]})};
  if (url.endsWith('/v1/internal/harness/reference-conflicts/adjudicate')) {
    if (body.items.length !== 1 || body.items[0].candidate_id !== ids.old) throw new Error('wrong reference batch');
    return {ok:true,status:200,json:async()=>({results:[{candidate_id:ids.fresh,input_version:1,
      question_text:'q-old',status:'review_unmatched',receipt_message:'题库复核完成，仍待关联',error_id:'',
      review_match_candidates:[option(codes.fresh,'q-old')]}]})};
  }
  if (url.endsWith('/v1/internal/harness/practice-reviews/adjudicate')) {
    practiceCalls += 1;
    const submitted = new Map(body.items.map(item => [item.candidate_id, item.code]));
    if (submitted.get(ids.other) !== codes.other || submitted.get(ids.fresh) !== codes.fresh) throw new Error('wrong refreshed batch');
    return {ok:true,status:200,json:async()=>({results:[
      {candidate_id:ids.other,input_version:1,status:'review_waiting',receipt_message:'服务端已保存第一题',error_id:'e'.repeat(32)},
      {candidate_id:ids.fresh,input_version:1,status:'review_completed',receipt_message:'服务端已完成本组',error_id:'e'.repeat(32)}
    ]})};
  }
  throw new Error('wrong endpoint');
};
const tools = [];
await extension.apply({workspaceRegistry:{create:async()=>undefined},tools:{register:value=>tools.push(value)},
  attachments:{readImage:async()=>({ref:{attachmentId:'sha256:'+'1'.repeat(64),mediaType:'image/png',name:'q.png'},data:new Uint8Array([1])})}});
const agent = {id:'session-hybrid',session:{events:[{type:'turn/start',data:{turn:1}}],deriveMessages:()=>[
  {role:'user',content:[{type:'text',text:'复习 PDF'},{type:'image',attachment:'image-ref'}]}
]}};
const exec = {agent,signal:new AbortController().signal};
const review = {code:'',pdf_id:'',error_id:'',question_id:'',stage:0,kind:''};
await tools.find(tool=>tool.name==='transcribe_error_notebook_attachments').execute({items:[
  {attachment_index:1,item_no:1,question_text:'q-old',answer_text:'x=1',review},
  {attachment_index:1,item_no:2,question_text:'q-other',answer_text:'x=1',review}
]},exec);
const processTool = tools.find(tool=>tool.name==='process_error_notebook_attachments');
const grade = (attachment_index,item_no,question_text) => ({attachment_index,item_no,question_text,answer_text:'x=1',
  verdict:'correct',first_error:'',cause_code:'',cause_evidence:'',knowledge_points:['方程'],correct_solution:'解答',
  final_answer:'x=1',prevention_cue:'检查',confidence:0.99,review});
const processed = await processTool.execute({items:[grade(1,1,'q-old'),grade(1,2,'q-other')]},exec);
if (processed.results[0].review_match_candidates[0].stem_text !== 'q-old') throw new Error('candidate evidence lost');
const referenceTool = tools.find(tool=>tool.name==='adjudicate_error_notebook_reference_conflicts');
const reference = await referenceTool.execute({items:[{candidate_id:ids.old,input_version:1,status:'consistent',
  rationale:'独立答案与题库答案在数学上完全一致，可以继续复习关联。'}]},exec);
if (!reference.review_pending || reference.results[0].candidate_id !== ids.fresh ||
    reference.results[0].review_match_candidates[0].code !== codes.fresh) throw new Error('fresh pending evidence lost');
const practiceTool = tools.find(tool=>tool.name==='adjudicate_practice_review_associations');
const item = (candidate_id,code) => ({candidate_id,input_version:1,status:'matched',code,
  rationale:'题干的全部条件、数值、选项与所求量均一致，可以唯一确认。'});
for (const invalid of [
  [item(ids.old,codes.old),item(ids.other,codes.other)],
  [item(ids.fresh,codes.fresh)],
  [item(ids.fresh,codes.fresh.toLowerCase()),item(ids.other,codes.other)]
]) {
  let blocked = false;
  try { await practiceTool.execute({items:invalid},exec); } catch (error) { blocked = true; }
  if (!blocked) throw new Error('invalid pending batch was accepted');
}
if (practiceCalls !== 0) throw new Error('invalid batch reached server');
const committed = await practiceTool.execute({items:[item(ids.other,codes.other),item(ids.fresh,codes.fresh)]},exec);
if (practiceCalls !== 1 || committed.results[0].receipt_message !== '服务端已保存第一题' ||
    committed.results[1].status !== 'review_completed') throw new Error('server receipts were not returned');
const rendered = practiceTool.output.render({},committed)[0].text;
if (!rendered.includes('服务端已完成本组')) throw new Error('server receipt was not rendered');
""".replace("__MODULE__", json.dumps(module_uri))
        environment = dict(os.environ, LZLM_PRODUCT_ORIGIN="http://127.0.0.1:8000",
                           LZLM_HARNESS_INTERNAL_TOKEN="synthetic-test-token",
                           LZLM_HARNESS_WORKSPACE_ROOT=str(root))
        completed = subprocess.run([node, "--input-type=module", "-e", script], cwd=root,
                                   env=environment, capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_inspect_restores_pending_review_batch_and_empty_context_clears_it(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required by the Harness runtime")
        root = Path(__file__).parents[1]
        module_uri = (root / "extensions" / "dsh-math-notebook-ui" / "lib" / "index.js").as_uri()
        script = """
const extension = await import(__MODULE__);
const ids = ['a'.repeat(32), 'b'.repeat(32)];
const codes = ['R111111111111-01-ABCDEF', 'R222222222222-02-ABCDEF'];
const pending = {pending_review_links: ids.map((candidate_id, index) => ({
  candidate_id, input_version:index + 1, question_text:`private-question-${index + 1}`,
  options:[{code:codes[index],pdf_id:'c'.repeat(32),pdf_name:'private.pdf',error_id:'d'.repeat(32),
    question_id:'e'.repeat(32),kind:'recommendation',stage:index + 1,stem_text:`private-stem-${index + 1}`,
    match_score:0.99,candidate_source:'semantic_candidate',generated_at:null,started:false}]
}))};
let contextCalls = 0;
let practiceCalls = 0;
globalThis.fetch = async (url, options) => {
  const body = JSON.parse(options.body);
  if (url.endsWith('/v1/internal/harness/context')) {
    contextCalls += 1;
    const context = contextCalls === 3 ? {pending_review_links:[]} : pending;
    return {ok:true,status:200,json:async()=>({context_json:JSON.stringify(context)})};
  }
  if (url.endsWith('/v1/internal/harness/practice-reviews/adjudicate')) {
    practiceCalls += 1;
    if (body.session_id !== 'session-inspect' || body.items.length !== 2 ||
        body.items.some((item, index) => item.candidate_id !== ids[index] || item.input_version !== index + 1 || item.code !== codes[index])) {
      throw new Error('inspect-restored batch was not submitted exactly');
    }
    return {ok:true,status:200,json:async()=>({results:ids.map((candidate_id, index) => ({
      candidate_id,input_version:index + 1,status:'review_completed',receipt_message:`saved-${index + 1}`,error_id:'d'.repeat(32)
    }))})};
  }
  throw new Error('unexpected endpoint (re-upload/process must not be needed)');
};
const tools = [];
await extension.apply({workspaceRegistry:{create:async()=>undefined},tools:{register:value=>tools.push(value)},attachments:{}});
const inspectTool = tools.find(tool=>tool.name==='inspect_math_notebook');
const practiceTool = tools.find(tool=>tool.name==='adjudicate_practice_review_associations');
const agent = {id:'session-inspect'};
const exec = {agent,signal:new AbortController().signal};
const item = (candidate_id,input_version,code) => ({candidate_id,input_version,status:'matched',code,
  rationale:'题干的全部条件、数值、选项与所求量均一致，可以唯一确认。'});
const batch = ids.map((candidate_id, index) => item(candidate_id,index + 1,codes[index]));
const inspected = await inspectTool.execute({},exec);
const inspectText = inspectTool.output.render({},inspected)[0].text;
if (!inspectText.includes(codes[0]) || !inspectText.includes('候选元数据仅供内部复习关联工具调用，不得向学生复述')) {
  throw new Error('inspect did not retain internal candidate evidence and disclosure guard');
}
const committed = await practiceTool.execute({items:batch},exec);
if (practiceCalls !== 1 || committed.results.length !== 2) throw new Error('restored exact batch was not committed');
const publicReceipt = JSON.stringify(committed) + practiceTool.output.render({},committed)[0].text;
if (codes.some(code => publicReceipt.includes(code)) || publicReceipt.includes('private.pdf') || publicReceipt.includes('private-stem')) {
  throw new Error('candidate evidence leaked into the adjudication receipt');
}
await inspectTool.execute({},exec);
await inspectTool.execute({},exec);
let cleared = false;
try { await practiceTool.execute({items:batch},exec); } catch (error) {
  cleared = String(error.message).includes('全部待关联 candidate_id');
}
if (!cleared || practiceCalls !== 1) throw new Error('empty inspect context did not clear stale pending state');
""".replace("__MODULE__", json.dumps(module_uri))
        environment = dict(os.environ, LZLM_PRODUCT_ORIGIN="http://127.0.0.1:8000",
                           LZLM_HARNESS_INTERNAL_TOKEN="synthetic-test-token",
                           LZLM_HARNESS_WORKSPACE_ROOT=str(root))
        completed = subprocess.run([node, "--input-type=module", "-e", script], cwd=root,
                                   env=environment, capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)

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
if (!rendered.startsWith('错题编号（error_id）：' + 'b'.repeat(32)) || !rendered.includes('知识点：2 个') || !rendered.includes('复习任务：已安排')) throw new Error('receipt details were not rendered');
if (!rendered.includes('下一步：') || !rendered.includes('打开「错题本」')) throw new Error('receipt next step missing');
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

    def test_attachment_tool_reads_latest_image_and_returns_durable_business_result(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required by the Harness runtime")
        module_uri = (Path(__file__).parents[1] / "extensions" / "dsh-math-notebook-ui" / "lib" / "index.js").as_uri()
        script = f"""
globalThis.fetch = async (url, options) => {{
  const body = JSON.parse(options.body);
  if (url.endsWith('/v1/internal/harness/reference-conflicts/recheck')) {{
    if (body.session_id !== 'session-process' || body.question_text !== 'historical q') throw new Error('wrong recheck');
    return {{ok: true, status: 200, json: async () => ({{result: {{
      candidate_id: 'd'.repeat(32), input_version: 2, question_text: 'historical q', receipt_status: 'needs_review',
      receipt_message: '等待第二阶段复核', reference_review: {{source_title: '题库', version_no: 3,
        independent_answer: 'x=1', reference_answer: 'x=1', reference_solution: '移项得 x=1'}}
    }}}})}};
  }}
  if (url.endsWith('/v1/internal/harness/reference-conflicts/adjudicate')) {{
    if (body.session_id !== 'session-process' || body.items[0].status !== 'consistent') throw new Error('wrong adjudication');
    return {{ok: true, status: 200, json: async () => ({{results: [{{
      candidate_id: 'a'.repeat(32), input_version: 1, status: 'saved', error_id: 'b'.repeat(32), receipt_message: '第二阶段复核一致，已计入错题本'
    }}]}})}};
  }}
  if (url.endsWith('/v1/internal/harness/errors/remove')) {{
    if (body.session_id !== 'session-process' || body.error_id !== 'b'.repeat(32)) throw new Error('wrong removal');
    if (body.confirmation_text !== '确认移除错题 ' + 'b'.repeat(32)) throw new Error('wrong confirmation');
    return {{ok: true, status: 200, json: async () => ({{receipt: {{
      schema: 'math-notebook-removal-receipt/v1', status: 'removed', error_id: 'b'.repeat(32), message: '已移除'
    }}}})}};
  }}
  if (!url.endsWith('/v1/internal/harness/intakes/process')) throw new Error('wrong endpoint');
  if (body.session_id !== 'session-process' || !['c','d'].includes(body.attachment.attachment_id.slice(7, 8))) throw new Error('wrong attachment');
  if (body.items.length !== 1 || body.items[0].item_no !== 1 || 'attachment_index' in body.items[0]) throw new Error('wrong items');
  if (body.correction_mode !== true) throw new Error('correction mode missing');
  const second = body.attachment.attachment_id === 'sha256:' + 'd'.repeat(64);
  return {{ok: true, status: 200, json: async () => ({{results: [{{
    item_no: 1, candidate_id: (second ? 'f' : 'a').repeat(32), input_version: 1, verdict: 'incorrect', question_text: second ? 'q2' : 'q', answer_text: 'a',
    first_error: 'e', cause_code: 'calculation', cause_evidence: 'because', knowledge_points: ['point'],
    correct_solution: 'solution', final_answer: 'answer', prevention_cue: 'check', receipt_status: 'saved',
    receipt_message: '已计入错题本', error_id: (second ? 'e' : 'b').repeat(32),
    reference_review: second ? null : {{source_title: '题库', version_no: 1, independent_answer: 'x=1', reference_answer: 'x=1', reference_solution: '解得 x=1'}}
  }}]}})}};
}};
const extension = await import({json.dumps(module_uri)});
const registered = [];
await extension.apply({{
  workspaceRegistry: {{create: async () => undefined}},
  tools: {{register: (value) => registered.push(value)}},
  attachments: {{readImage: async (ref) => {{
    if (!['image-ref', 'image-ref-2'].includes(ref)) throw new Error('wrong image ref');
    const second = ref === 'image-ref-2';
    return {{ref: {{attachmentId: 'sha256:' + (second ? 'd' : 'c').repeat(64), mediaType: 'image/png', name: second ? 'q2.png' : 'q.png'}}, data: new Uint8Array([1, 2, 3])}};
  }}}}
}});
let afterTranscription = false;
const messages = [
  {{role: 'user', content: [{{type: 'image', attachment: 'old-ref'}}]}},
  {{role: 'assistant', content: [{{type: 'text', text: 'old'}}]}},
  {{role: 'user', content: [{{type: 'text', text: '请重新判并改错'}}, {{type: 'image', attachment: 'image-ref'}}, {{type: 'image', attachment: 'image-ref-2'}}]}}
];
const agent = {{id: 'session-process', session: {{
  events: [{{type: 'turn/start', data: {{turn: 7}}}}],
  deriveMessages: () => afterTranscription
    ? [...messages, {{role: 'user', content: [{{type: 'tool_result', text: 'OCR frozen'}}]}}]
    : messages
}}}};
const review = {{code: '', pdf_id: '', error_id: '', question_id: '', stage: 0, kind: ''}};
const transcriber = registered.find((value) => value.name === 'transcribe_error_notebook_attachments');
const transcription = await transcriber.execute({{items: [{{
  attachment_index: 1, item_no: 1, question_text: 'q', answer_text: 'a', review
}}, {{
  attachment_index: 2, item_no: 1, question_text: 'q2', answer_text: 'a', review
}}]}}, {{agent, signal: new AbortController().signal}});
if (transcription.schema !== 'math-notebook-transcription/v1' || transcription.items.length !== 2) throw new Error('wrong transcription');
afterTranscription = true;
const tool = registered.find((value) => value.name === 'process_error_notebook_attachments');
const result = await tool.execute({{items: [{{
  attachment_index: 1, item_no: 1, question_text: 'q', answer_text: 'a', verdict: 'incorrect', first_error: 'e',
  cause_code: 'calculation', cause_evidence: 'because', knowledge_points: ['point'], correct_solution: 'solution',
  final_answer: 'answer', prevention_cue: 'check', confidence: 0.9, review
}}, {{
  attachment_index: 2, item_no: 1, question_text: 'q2', answer_text: 'a', verdict: 'incorrect', first_error: 'e',
  cause_code: 'calculation', cause_evidence: 'because', knowledge_points: ['point'], correct_solution: 'solution',
  final_answer: 'answer', prevention_cue: 'check', confidence: 0.9, review
}}]}}, {{
  agent, signal: new AbortController().signal
}});
if (result.schema !== 'math-notebook-process-result/v1' || result.results.length !== 2 || result.results[1].attachment_index !== 2) throw new Error('wrong result');
const rendered = tool.output.render({{}}, result)[0].text;
if (!rendered.includes('图片 1 · 第 1 题') || !rendered.includes('图片 2 · 第 1 题') || !rendered.includes('已计入错题本')) throw new Error('result not rendered');
if (!rendered.startsWith('图片 1 · 第 1 题\\n错题编号（error_id）：' + 'b'.repeat(32) + '\\n题目：')) throw new Error('full error id must precede question');
for (const [receipt_status,error_id,label] of [
  ['already_saved','b'.repeat(32),'b'.repeat(32)],
  ['review_completed','b'.repeat(32),'b'.repeat(32)],
  ['not_saved_correct','','无（本题正确，未计入错题本）'],
  ['review_unmatched','','待确认（尚未关联原错题）'],
  ['needs_review','','暂不可用（等待入本或关联确认）'],
  ['needs_review','ERR-12345678','暂不可用（等待入本或关联确认）'],
  ['needs_review','b'.repeat(8),'暂不可用（等待入本或关联确认）'],
]) {{
  const text = tool.output.render({{}}, {{results:[{{...result.results[0],receipt_status,error_id}}]}})[0].text;
  if (!text.startsWith('图片 1 · 第 1 题\\n错题编号（error_id）：' + label + '\\n题目：')) throw new Error('incorrect id display for ' + receipt_status);
}}
if (!rendered.includes('下一步：') || !rendered.includes('等待本轮复核完成')) throw new Error('processing next step missing');
const waitingText = tool.output.render({{}}, {{results:[{{...result.results[0], receipt_status:'review_waiting', reference_review:null, review_association:{{status:'matched',pdf_id:'p',review_code:'r',error_id:'e',stage:1}}}}]}})[0].text;
if (waitingText.includes('candidate_id=') || !waitingText.includes('禁止再调用 confirm_error_notebook_entry')) throw new Error('matched review must not request another confirmation');
const unmatchedText = tool.output.render({{}}, {{results:[{{...result.results[0], receipt_status:'review_unmatched', reference_review:null, review_association:{{status:'unmatched'}}}}]}})[0].text;
if (!unmatchedText.includes('confirm_error_notebook_entry') || !unmatchedText.includes('candidate_id=')) throw new Error('unmatched review must retain its linking receipt');
const adjudicator = registered.find((value) => value.name === 'adjudicate_error_notebook_reference_conflicts');
let wrongCandidateBlocked = false;
try {{
  await adjudicator.execute({{items: [{{candidate_id:'c'.repeat(32),input_version:1,status:'consistent',rationale:'该候选并非本轮 process 返回的待复核题目。'}}]}}, {{agent,signal:new AbortController().signal}});
}} catch (error) {{
  wrongCandidateBlocked = String(error.message).includes('不得遗漏或串题');
}}
if (!wrongCandidateBlocked) throw new Error('wrong adjudication candidate must be blocked');
const rechecker = registered.find((value) => value.name === 'recheck_error_notebook_reference_conflict');
const rechecked = await rechecker.execute({{question_text: 'historical q'}}, {{agent: {{id: 'session-process'}}, signal: new AbortController().signal}});
if (rechecked.result.reference_review.reference_answer !== 'x=1') throw new Error('reference conflict not reloaded');
const recheckRendered = rechecker.output.render({{}}, rechecked)[0].text;
if (!recheckRendered.includes('candidate_id=' + 'd'.repeat(32)) || !recheckRendered.includes('题库参考解析')) throw new Error('recheck evidence hidden from model');
if (!recheckRendered.includes('下一步：') || !recheckRendered.includes('等待本轮复核完成')) throw new Error('recheck next step missing');
const adjudicated = await adjudicator.execute({{items: [{{
  candidate_id: 'a'.repeat(32), input_version: 1, status: 'consistent',
  rationale: '独立答案与题库答案的数学结论完全一致。'
}}]}}, {{agent, signal: new AbortController().signal}});
if (adjudicated.results[0].status !== 'saved') throw new Error('reference conflict not adjudicated');
const adjudicatedRendered = adjudicator.output.render({{}}, adjudicated)[0].text;
if (!adjudicatedRendered.startsWith('错题编号（error_id）：' + 'b'.repeat(32))) throw new Error('adjudication error id missing');
if (!adjudicator.output.schema.properties.results.items.properties.error_id) throw new Error('adjudication id missing from schema');
const resolvedRecheck = {{result:{{...rechecked.result, receipt_status:'saved', error_id:'b'.repeat(32),reference_review:null}}}};
if (!rechecker.output.render({{}}, resolvedRecheck)[0].text.startsWith('错题编号（error_id）：' + 'b'.repeat(32))) throw new Error('resolved recheck id missing');
if (!rechecker.output.schema.properties.result.properties.error_id) throw new Error('recheck id missing from schema');
if (!adjudicatedRendered.includes('下一步：') || !adjudicatedRendered.includes('打开「错题本」')) throw new Error('adjudication next step missing');
if (!rendered.includes('只调用 adjudicate_error_notebook_reference_conflicts') || !adjudicatedRendered.includes('直接生成最终回复')) throw new Error('agent action must be explicit');
const remover = registered.find((value) => value.name === 'remove_error_notebook_entry');
let concluded = false;
const removed = await remover.execute({{error_id: 'b'.repeat(32)}}, {{
  agent: {{id: 'session-process', session: {{deriveMessages: () => [
    {{role: 'assistant', content: [{{type: 'text', text: '请确认'}}]}},
    {{role: 'user', content: [{{type: 'text', text: '确认移除错题 ' + 'b'.repeat(32)}}]}}
  ]}}}}, signal: new AbortController().signal, concludeTurn: () => {{ concluded = true; }}
}});
if (removed.status !== 'removed' || !concluded) throw new Error('error not removed');
const removalRendered = remover.output.render({{}}, removed)[0].text;
if (!removalRendered.includes('已移除') || !removalRendered.includes('b'.repeat(32))) throw new Error('removal receipt not rendered');
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
