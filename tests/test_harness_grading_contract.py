from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HarnessGradingContractTests(unittest.TestCase):
    def test_lean_batch_rehydrates_and_recovers_without_replaying_confirmed_image(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is needed for Harness contract checks")
        module_uri = (ROOT / "extensions" / "dsh-math-notebook-ui" / "lib" / "index.js").resolve().as_uri()
        script = f"""
process.env.LZLM_PRODUCT_ORIGIN='http://product';
process.env.LZLM_HARNESS_INTERNAL_TOKEN='internal-test-token';
process.env.LZLM_HARNESS_WORKSPACE_ROOT='C:/workspace';
const assert = await import('node:assert/strict');
const extension = await import({json.dumps(module_uri)});
const tools=[];
const review={{code:'Rabcdef123456-01',pdf_id:'pdf',error_id:'',question_id:'qid',stage:1,kind:'original'}};
const reference={{question_id:'qid',version_no:7,question_text:'已验证题干：若 x²=4，求 x。',reference_answer:'x=±2',reference_solution:'由 x²=4，得 x=±2。',source_title:'授权题库'}};
let referenceCalls=0, imageOneWrites=0, processCalls=0, inspectBody=null, malformedRefresh=false, malformedProcessCalls=0, failSupersedingTranscription=false;
const option={{code:'CaseSensitive-A',pdf_id:'pdf',pdf_name:'练习一',error_id:'e'.repeat(32),question_id:'qid',kind:'original',stage:1,
  stem_text:'若 x²=4，求 x。A.2 B.-2 C.±2 D.0',match_score:0.99,candidate_source:'verified_question',generated_at:null,started:true}};
const receipt=(item_no,attachment_index,extra={{}})=>({{item_no,candidate_id:String(attachment_index).repeat(32),input_version:1,verdict:'incorrect',
  question_text:attachment_index===1?'题干一：若 x²=4，求 x。':'题干二：解方程 y+1=3。',answer_text:attachment_index===1?'x=2':'y=1',
  first_error:'漏解',cause_code:'incomplete_cases',cause_evidence:'只写一个根',knowledge_points:['方程'],correct_solution:'完整解析',final_answer:'答案',prevention_cue:'验根',
  receipt_status:'saved',receipt_message:'权威回执',error_id:'a'.repeat(32),review_association:{{status:'not_review',pdf_id:'',review_code:'',error_id:'',question_id:'',stage:0,kind:''}},
  review_match_candidates:[],reference_review:null,...extra}});
globalThis.fetch=async(url,options)=>{{
  const body=JSON.parse(options.body);
  if(url.endsWith('/grading-references')){{
    if(body.session_id==='supersede-session' && failSupersedingTranscription) return {{ok:false,status:503,json:async()=>({{error:{{code:'temporary'}}}})}};
    if(body.session_id==='malformed-session') return {{ok:true,status:200,json:async()=>({{items:body.items.map(item=>malformedRefresh
      ? {{item_no:item.item_no,grading_strategy:'verified_reference',reference:null}}
      : {{item_no:item.item_no,grading_strategy:'independent',reference:null}})}})}};
    referenceCalls++;
    const first=body.items[0].question_text.startsWith('题干一');
    if(first) return {{ok:true,status:200,json:async()=>({{items:[{{item_no:1,grading_strategy:'verified_reference',reference}}]}})}};
    return {{ok:true,status:200,json:async()=>({{items:[{{item_no:1,grading_strategy:referenceCalls===2?'verified_reference':'independent',reference:referenceCalls===2?reference:null}}]}})}};
  }}
  if(url.endsWith('/intakes/process')){{
    if(body.session_id==='malformed-session'){{
      malformedProcessCalls++;
      if(malformedProcessCalls===1){{malformedRefresh=true;return {{ok:false,status:409,json:async()=>({{error:{{code:'reference_changed'}}}})}};}}
      return {{ok:true,status:200,json:async()=>({{results:[receipt(1,1)]}})}};
    }}
    processCalls++;
    const first=body.items[0].question_text.startsWith('题干一');
    if(first){{
      imageOneWrites++;
      assert.equal(body.items[0].question_text,'题干一：若 x²=4，求 x。');
      assert.equal(body.items[0].answer_text,'x=2');
      assert.deepEqual(body.items[0].review,review);
      assert.equal(body.items[0].grading_strategy,'verified_reference');
      assert.equal(body.items[0].final_answer,reference.reference_answer);
      assert.equal(body.items[0].correct_solution,reference.reference_solution);
      return {{ok:true,status:200,json:async()=>({{results:[receipt(1,1)]}})}};
    }}
    if(processCalls===2) return {{ok:false,status:409,json:async()=>({{error:{{code:'reference_changed'}}}})}};
    const unclear=body.items[0].question_text.startsWith('字迹不足');
    assert.equal(body.items[0].question_text,unclear?'字迹不足，无法恢复完整题干':'题干二：解方程 y+1=3。');
    assert.equal(body.items[0].answer_text,unclear?'':'y=1');
    assert.equal(body.items[0].grading_strategy,'independent');
    assert.equal(body.items[0].correct_solution,unclear?'':'由 y+1=3，得 y=2。');
    return {{ok:true,status:200,json:async()=>({{results:[receipt(1,2,{{receipt_status:'review_unmatched',review_match_candidates:[option]}})]}})}};
  }}
  if(url.endsWith('/context')){{
    inspectBody=body;
    return {{ok:true,status:200,json:async()=>({{context_json:JSON.stringify({{scope:'exact',current_bound_account:true,query:{{mode:'exact'}},error:{{error_id:'a'.repeat(32)}}}})}})}};
  }}
  if(url.endsWith('/practice-reviews/adjudicate')) return {{ok:true,status:200,json:async()=>({{results:body.items.map(item=>({{candidate_id:item.candidate_id,input_version:item.input_version,status:'review_waiting',receipt_message:'已保留并处理队列',error_id:''}}))}})}};
  throw new Error('unexpected endpoint '+url);
}};
await extension.apply({{workspaceRegistry:{{create:async()=>undefined}},tools:{{register:t=>tools.push(t)}},attachments:{{readImage:async(ref)=>({{ref:{{attachmentId:'sha256:'+(ref==='one'?'1':'2').repeat(64),mediaType:'image/png',name:ref+'.png'}},data:new Uint8Array([1,2])}})}}}});
const agent={{id:'session',session:{{events:[{{type:'turn/start',data:{{turn:1}}}}],deriveMessages:()=>[{{role:'user',content:[{{type:'image',attachment:'one'}},{{type:'image',attachment:'two'}}]}}]}}}};
const exec={{agent,signal:new AbortController().signal}};
const transcribe=tools.find(t=>t.name==='transcribe_error_notebook_attachments');
const frozen=await transcribe.execute({{items:[
  {{attachment_index:1,item_no:1,question_text:'题干一：若 x²=4，求 x。',answer_text:'x=2',review}},
  {{attachment_index:2,item_no:1,question_text:'题干二：解方程 y+1=3。',answer_text:'y=1',review}}
]}},exec);
const transcriptionText=transcribe.output.render({{}},frozen)[0].text;
assert.ok(transcriptionText.split('\\n').at(-1).startsWith('LZLM_PROTECTED_V1 ['));
const grade=(attachment_index,solution='')=>({{attachment_index,item_no:1,verdict:'incorrect',first_error:'错误',cause_code:'calculation',cause_evidence:'计算不等价',knowledge_points:['方程'],correct_solution:solution,final_answer:solution?'y=2':'',prevention_cue:'验算',confidence:0.9}});
const oldPayload={{items:frozen.items.map((item,index)=>({{...item,...grade(index+1,index?'由 y+1=3，得 y=2。':''),final_answer:index?'y=2':reference.reference_answer,correct_solution:index?'由 y+1=3，得 y=2。':reference.reference_solution}}))}};
const leanPayload={{batch_ref:frozen.batch_ref,items:[grade(1),grade(2,'由 y+1=3，得 y=2。')]}};
const oldJson=JSON.stringify(oldPayload), leanJson=JSON.stringify(leanPayload);
assert.ok(Buffer.byteLength(leanJson,'utf8')<Buffer.byteLength(oldJson,'utf8'));
const processTool=tools.find(t=>t.name==='process_error_notebook_attachments');
const writesBeforeInvalid=processCalls;
await assert.rejects(()=>processTool.execute({{batch_ref:'stale',items:leanPayload.items}},exec),/批次引用已过期/);
await assert.rejects(()=>processTool.execute({{batch_ref:frozen.batch_ref,items:[leanPayload.items[0]]}},exec),/不得遗漏/);
await assert.rejects(()=>processTool.execute({{batch_ref:frozen.batch_ref,items:[leanPayload.items[0],leanPayload.items[0]]}},exec),/不得遗漏/);
await assert.rejects(()=>processTool.execute({{batch_ref:frozen.batch_ref,items:[leanPayload.items[1],leanPayload.items[0]]}},exec),/不得遗漏/);
const otherAgent={{id:'other',session:agent.session}};
await assert.rejects(()=>processTool.execute(leanPayload,{{agent:otherAgent,signal:exec.signal}}),/请先调用 transcribe/);
agent.session.events=[{{type:'turn/start',data:{{turn:2}}}}];
await assert.rejects(()=>processTool.execute(leanPayload,exec),/请先调用 transcribe/);
agent.session.events=[{{type:'turn/start',data:{{turn:1}}}}];
assert.equal(processCalls,writesBeforeInvalid,'invalid batches must be rejected before writes');
let recovery;
try{{await processTool.execute(leanPayload,exec)}}catch(error){{recovery=String(error.message)}}
assert.match(recovery,/新的冻结版本/);
assert.ok(recovery.split('\\n').at(-1).startsWith('LZLM_PROTECTED_V1 ['));
assert.equal(imageOneWrites,1,'confirmed image write count after recovery');
const refreshed=JSON.parse(recovery.slice(recovery.indexOf('{{"batch_ref"'),recovery.lastIndexOf('\\nLZLM_PROTECTED_V1')));
assert.equal(refreshed.items.length,1);
assert.equal(refreshed.items[0].grading_strategy,'independent');
await assert.rejects(()=>processTool.execute({{batch_ref:frozen.batch_ref,items:[grade(2,'由 y+1=3，得 y=2。')]}},exec),/批次引用已过期/);
const completed=await processTool.execute({{batch_ref:refreshed.batch_ref,items:[grade(2,'由 y+1=3，得 y=2。')]}},exec);
assert.equal(imageOneWrites,1,'confirmed image write count after retry');
assert.equal(completed.results.length,2);
const rendered=processTool.output.render({{}},completed)[0].text;
assert.ok(rendered.split('\\n').at(-1).startsWith('LZLM_PROTECTED_V1 ['));
assert.equal(rendered.split('CaseSensitive-A').length-1,1);
assert.ok(rendered.includes(option.stem_text));
const inspect=tools.find(t=>t.name==='inspect_math_notebook');
const noOption={{candidate_id:'3'.repeat(32),input_version:2,question_text:'缺少 PDF 定位的完整题干',options:[]}};
const inspectRendered=inspect.output.render({{}},{{context_json:JSON.stringify({{scope:'pending_review_links',pending_review_links:[{{candidate_id:'2'.repeat(32),input_version:1,question_text:'题干二',options:[option]}},noOption]}}),next_review_batch_json:JSON.stringify([{{candidate_id:'2'.repeat(32),input_version:1,question_text:'题干二',options:[option]}}])}})[0].text;
assert.equal(inspectRendered.split('CaseSensitive-A').length-1,1);
assert.ok(inspectRendered.includes('缺少 PDF 定位的完整题干') && inspectRendered.includes('needs_review_locator'));
const exact=await inspect.execute({{scope:'exact',error_id:'a'.repeat(32)}},exec);
assert.deepEqual(inspectBody,{{session_id:'session',scope:'exact',error_id:'a'.repeat(32)}});
assert.equal(exact.next_review_batch_json,'[]');
const practiceTool=tools.find(t=>t.name==='adjudicate_practice_review_associations');
const afterExact=await practiceTool.execute({{items:[{{candidate_id:'2'.repeat(32),input_version:1,status:'matched',code:option.code,rationale:'全部数学条件、数字、选项和所求量逐项一致。'}}]}},exec);
assert.equal(afterExact.results[0].status,'review_waiting','exact inspection must not destroy the internal association queue');
const unclearAgent={{id:'unclear-session',session:{{events:[{{type:'turn/start',data:{{turn:2}}}}],deriveMessages:()=>[{{role:'user',content:[{{type:'image',attachment:'two'}}]}}]}}}};
const unclearExec={{agent:unclearAgent,signal:new AbortController().signal}};
const unclearFrozen=await transcribe.execute({{items:[{{attachment_index:1,item_no:1,question_text:'字迹不足，无法恢复完整题干',answer_text:'',review}}]}},unclearExec);
const unclear=await processTool.execute({{batch_ref:unclearFrozen.batch_ref,items:[{{attachment_index:1,item_no:1,verdict:'unclear',first_error:'题干不足',cause_code:'unclear',cause_evidence:'关键公式无法辨认',knowledge_points:[],prevention_cue:'补充清晰图片',confidence:0.1}}]}},unclearExec);
assert.equal(unclear.results[0].verdict,'incorrect');
const referenceTool=tools.find(t=>t.name==='adjudicate_error_notebook_reference_conflicts');
const same={{candidate_id:'4'.repeat(32),input_version:1,status:'review_unmatched',receipt_message:'仍待关联',question_text:'q',review_match_candidates:[option]}};
const sameRendered=referenceTool.output.render({{items:[same]}},{{results:[same],review_pending:true,next_review_batch_json:'[]'}})[0].text;
const sameTransitions=JSON.parse(sameRendered.split('LZLM_PROTECTED_V1 ').at(-1));
assert.equal(sameTransitions.at(-1).status,'active','same candidate replacement must remain protected');
const retryableRef={{candidate_id:'6'.repeat(32),input_version:2,status:'review_retryable',receipt_message:'可重试'}};
const retryableRefText=referenceTool.output.render({{items:[same]}},{{results:[retryableRef],review_pending:false,next_review_batch_json:'[]'}})[0].text;
const retryableRefTransitions=JSON.parse(retryableRefText.split('LZLM_PROTECTED_V1 ').at(-1));
assert.deepEqual(retryableRefTransitions.map(item=>item.status),['resolved','active']);
assert.equal(retryableRefTransitions.at(-1).key,'candidate:'+retryableRef.candidate_id+':2');
const confirmTool=tools.find(t=>t.name==='confirm_error_notebook_entry');
for(const status of ['review_unmatched','review_retryable']){{
  const text=confirmTool.output.render({{candidate_id:'5'.repeat(32),input_version:1}},{{status,reference_status:'not_found',knowledge_point_count:0,review_status:'pending',message:'仍可恢复'}})[0].text;
  assert.equal(JSON.parse(text.split('LZLM_PROTECTED_V1 ').at(-1))[0].status,'active');
}}
const terminalText=confirmTool.output.render({{candidate_id:'5'.repeat(32),input_version:1}},{{status:'saved',reference_status:'not_found',knowledge_point_count:1,review_status:'scheduled',message:'已保存'}})[0].text;
assert.equal(JSON.parse(terminalText.split('LZLM_PROTECTED_V1 ').at(-1))[0].status,'resolved');
const practiceRenderer=practiceTool.output.render;
for(const status of ['review_unmatched','review_retryable','saved']){{
  const result={{candidate_id:'7'.repeat(32),input_version:1,status,receipt_message:'状态回执',error_id:''}};
  const text=practiceRenderer({{items:[{{candidate_id:'7'.repeat(32),input_version:1}}]}},{{results:[result],reference_pending:false,next_review_batch_json:'[]'}})[0].text;
  const transitions=JSON.parse(text.split('LZLM_PROTECTED_V1 ').at(-1));
  assert.equal(transitions.at(-1).status,status==='saved'?'resolved':'active');
}}
const retryTool=tools.find(t=>t.name==='retry_practice_review_confirmation');
for(const status of ['review_retryable','review_completed']){{
  const receipt={{candidate_id:'8'.repeat(32),input_version:3,status}};
  const text=retryTool.output.render({{}},{{result_json:JSON.stringify({{receipt,review_item:{{recommended_action:'none'}}}})}})[0].text;
  const transition=JSON.parse(text.split('LZLM_PROTECTED_V1 ').at(-1))[0];
  assert.equal(transition.status,status==='review_retryable'?'active':'resolved');
}}
const malformedAgent={{id:'malformed-session',session:{{events:[{{type:'turn/start',data:{{turn:3}}}}],deriveMessages:()=>[{{role:'user',content:[{{type:'image',attachment:'one'}}]}}]}}}};
const malformedExec={{agent:malformedAgent,signal:new AbortController().signal}};
const malformedFrozen=await transcribe.execute({{items:[{{attachment_index:1,item_no:1,question_text:'原冻结题干',answer_text:'作答',review}}]}},malformedExec);
const malformedGrade={{attachment_index:1,item_no:1,verdict:'incorrect',first_error:'错',cause_code:'calculation',cause_evidence:'证据',knowledge_points:['方程'],correct_solution:'解析',final_answer:'答案',prevention_cue:'检查',confidence:0.8}};
await assert.rejects(()=>processTool.execute({{batch_ref:malformedFrozen.batch_ref,items:[malformedGrade]}},malformedExec),/invalid item/);
malformedRefresh=false;
const afterMalformed=await processTool.execute({{batch_ref:malformedFrozen.batch_ref,items:[malformedGrade]}},malformedExec);
assert.equal(afterMalformed.results.length,1,'malformed refresh must not mutate the frozen batch reference or items');
let supersedeTurn=4;
const supersedeAgent={{id:'supersede-session',session:{{events:[{{type:'turn/start',data:{{turn:supersedeTurn}}}}],deriveMessages:()=>[{{role:'user',content:[{{type:'image',attachment:'two'}}]}}]}}}};
const supersedeExec={{agent:supersedeAgent,signal:new AbortController().signal}};
const firstBatch=await transcribe.execute({{items:[{{attachment_index:1,item_no:1,question_text:'题干二：解方程 y+1=3。',answer_text:'y=1',review}}]}},supersedeExec);
failSupersedingTranscription=true;
supersedeTurn=5; supersedeAgent.session.events=[{{type:'turn/start',data:{{turn:supersedeTurn}}}}];
await assert.rejects(()=>transcribe.execute({{items:[{{attachment_index:1,item_no:1,question_text:'失败的新题干',answer_text:'',review}}]}},supersedeExec),/Grading reference lookup failed/);
failSupersedingTranscription=false;
supersedeTurn=6; supersedeAgent.session.events=[{{type:'turn/start',data:{{turn:supersedeTurn}}}}];
const replacementBatch=await transcribe.execute({{items:[{{attachment_index:1,item_no:1,question_text:'题干二：解方程 y+1=3。',answer_text:'y=1',review}}]}},supersedeExec);
assert.equal(replacementBatch.superseded_batch_ref,firstBatch.batch_ref,'failed transcription must preserve the prior active batch');
const replacementTransitions=JSON.parse(transcribe.output.render({{}},replacementBatch)[0].text.split('LZLM_PROTECTED_V1 ').at(-1));
assert.deepEqual(replacementTransitions,[{{key:'batch:'+replacementBatch.batch_ref,status:'active'}},{{key:'batch:'+firstBatch.batch_ref,status:'resolved'}}]);
console.log(JSON.stringify({{old_chars:oldJson.length,new_chars:leanJson.length,old_utf8_bytes:Buffer.byteLength(oldJson,'utf8'),new_utf8_bytes:Buffer.byteLength(leanJson,'utf8')}}));
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sizes = json.loads(result.stdout.strip().splitlines()[-1])
        print("synthetic grading payload sizes:", sizes)
        self.assertLess(sizes["new_chars"], sizes["old_chars"])
        self.assertLess(sizes["new_utf8_bytes"], sizes["old_utf8_bytes"])


if __name__ == "__main__":
    unittest.main()
