from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class HarnessGradingContractTests(unittest.TestCase):
    def test_partial_failure_reports_exact_pending_batch_and_preserves_recovery_state(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is needed for Harness contract checks")
        module_uri = (ROOT / "extensions/dsh-math-notebook-ui/lib/index.js").resolve().as_uri()
        script = r"""
process.env.LZLM_PRODUCT_ORIGIN='http://product';
process.env.LZLM_HARNESS_INTERNAL_TOKEN='internal-test-token';
process.env.LZLM_HARNESS_WORKSPACE_ROOT='C:/workspace';
const assert=await import('node:assert/strict');
const extension=await import(MODULE_URI);
const tools=[], writes=[], requests=[];
let reads=0, references=0, failure='conflict', failedImage=2;
const review={code:'',pdf_id:'',error_id:'',question_id:'',stage:0,kind:''};
const key=item=>`${item.attachment_index}:${item.item_no}`;
const textItem=(attachment_index,item_no)=>({attachment_index,item_no,
  question_text:`question-${attachment_index}-${item_no}`,answer_text:`answer-${attachment_index}-${item_no}`,review});
const grade=item=>({attachment_index:item.attachment_index,item_no:item.item_no,
  verdict:'correct',first_error:'',cause_code:'',cause_evidence:'checked',knowledge_points:['algebra'],
  correct_solution:`solution-${key(item)}`,final_answer:`answer-${key(item)}`,prevention_cue:'check',confidence:0.9});
globalThis.fetch=async(url,options)=>{
  const body=JSON.parse(options.body);
  if(url.endsWith('/grading-references')){
    references++;
    return {ok:true,json:async()=>({items:body.items.map(item=>({item_no:item.item_no,grading_strategy:'independent',reference:null}))})};
  }
  assert.ok(url.endsWith('/intakes/process'));
  const image=Number(body.attachment.name.split('.')[0]);
  requests.push(image);
  if(image===failedImage && failure){
    if(failure==='network') throw new Error('network unavailable');
    if(failure==='json') return {ok:true,json:async()=>{throw new Error('invalid JSON')}};
    return {ok:false,status:409,json:async()=>({error:{code:'conflict'}})};
  }
  for(const item of body.items){
    const identity=`${image}:${item.item_no}`;
    assert.equal(item.question_text,`question-${image}-${item.item_no}`);
    assert.equal(item.answer_text,`answer-${image}-${item.item_no}`);
    assert.equal(item.correct_solution,`solution-${identity}`);
    writes.push(identity);
  }
  return {ok:true,json:async()=>({results:body.items.map(item=>({...item,candidate_id:`candidate-${image}-${item.item_no}`,
    input_version:1,receipt_status:'not_saved_correct',receipt_message:'confirmed',error_id:'',
    review_match_candidates:[],reference_review:null}))})};
};
await extension.apply({workspaceRegistry:{create:async()=>undefined},tools:{register:t=>tools.push(t)},
  attachments:{readImage:async ref=>{reads++;return {ref:{attachmentId:`id-${ref}`,name:`${ref}.png`,mediaType:'image/png'},data:new Uint8Array([1])}}}});
const transcribe=tools.find(t=>t.name==='transcribe_error_notebook_attachments');
const processTool=tools.find(t=>t.name==='process_error_notebook_attachments');
let messages=[{role:'user',content:[1,2,3].map(n=>({type:'image',attachment:n}))},
  {role:'user',source:{kind:'tool'},content:[{type:'tool-result',content:[{type:'text',text:'unrelated tool receipt'}]}]}];
const agent={id:'batch-recovery',session:{events:[{type:'turn/start',data:{turn:1}}],deriveMessages:()=>messages}};
const exec={agent,signal:new AbortController().signal,concludeTurn:()=>undefined};
const errorFrom=async args=>{
  try {await processTool.execute(args,exec);assert.fail('expected rejection');}
  catch(error){assert.ok(error.message.includes('当前权威批次状态：'),error.message);return error;}
};
const state=error=>JSON.parse(error.message.split('当前权威批次状态：')[1].split('\n')[0]);
// Three photos, five questions. Interleaved OCR input becomes canonical image order.
const frozen=await transcribe.execute({items:[textItem(1,1),textItem(2,1),textItem(1,2),textItem(2,2),textItem(3,1)]},exec);
assert.deepEqual(frozen.items.map(key),['1:1','1:2','2:1','2:2','3:1']);
assert.match(transcribe.output.render({},frozen)[0].text,/1:1, 1:2, 2:1, 2:2, 3:1/);
const full={batch_ref:frozen.batch_ref,items:frozen.items.map(grade)};
const error=await errorFrom(full);
assert.match(error.message,/409: conflict/);
assert.deepEqual(state(error),{batch_ref:frozen.batch_ref,pending_item_keys:['2:1','2:2','3:1'],completed_item_keys:['1:1','1:2']});
assert.deepEqual(writes,['1:1','1:2']);
const beforeRequests=requests.length;
for(const items of [full.items, [grade(textItem(3,1))], [grade(textItem(2,1)),grade(textItem(2,1)),grade(textItem(3,1))],
  [grade(textItem(3,1)),grade(textItem(2,1)),grade(textItem(2,2))],
  [grade(textItem(2,1)),grade(textItem(2,2)),grade(textItem(4,1))]]){
  const rejected=await errorFrom({batch_ref:frozen.batch_ref,items});
  assert.deepEqual(state(rejected).pending_item_keys,['2:1','2:2','3:1']);
  assert.deepEqual(state(rejected).received_item_keys,items.map(key));
  assert.match(rejected.message,/本次调用尚未提交任何题目/);
}
assert.equal(requests.length,beforeRequests,'invalid guesses must never write');
// Recovery returns the exact original frozen text; not even an OCR rewrite resets completion.
const beforeReads=reads,beforeReferences=references;
for(const items of [[],[textItem(1,1)]]){
  const recovered=await transcribe.execute({items},exec);
  assert.equal(recovered.batch_ref,frozen.batch_ref);
  assert.deepEqual(recovered.items,frozen.items.slice(2));
}
assert.equal(reads,beforeReads);
assert.equal(references,beforeReferences);
failure='';
const finished=await processTool.execute({batch_ref:state(error).batch_ref,
  items:state(error).pending_item_keys.map(k=>full.items.find(item=>key(item)===k))},exec);
assert.deepEqual(finished.results.map(key),['1:1','1:2','2:1','2:2','3:1']);
assert.deepEqual(writes,['1:1','1:2','2:1','2:2','3:1'],'each confirmed question written once');
// Network/JSON failures also disclose the authoritative pending set, without pretending rollback.
for(const mode of ['network','json']){
  agent.session.events=[{type:'turn/start',data:{turn:mode}}];
  messages=[{role:'user',content:[{type:'image',attachment:1}]},
    {role:'user',content:[{type:'tool-result',content:[]}]}];
  failedImage=1;failure=mode;
  const batch=await transcribe.execute({items:[textItem(1,1)]},exec);
  const failed=await errorFrom({batch_ref:batch.batch_ref,items:[grade(textItem(1,1))]});
  assert.deepEqual(state(failed).pending_item_keys,['1:1']);
  assert.deepEqual(state(failed).completed_item_keys,[]);
  assert.match(failed.message,/结果未确认不等于未写入/);
  failure='';
  assert.equal((await processTool.execute({batch_ref:batch.batch_ref,items:[grade(textItem(1,1))]},exec)).results.length,1);
}
// Never reuse an old upload when the genuine latest user message has no images.
agent.session.events=[{type:'turn/start',data:{turn:99}}];
messages.push({role:'user',content:[{type:'text',text:'new message without an image'}]});
const empty=await transcribe.execute({items:[]},exec);
assert.deepEqual(empty.items,[]);
assert.equal(empty.batch_ref,'');
messages=[{role:'user',content:[{type:'image',attachment:1}]}];
await assert.rejects(()=>transcribe.execute({items:[]},exec),/每张图片/);
console.log('batch recovery contracts passed');
""".replace("MODULE_URI", json.dumps(module_uri))
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
const unclear=await processTool.execute({{batch_ref:unclearFrozen.batch_ref,items:[{{attachment_index:1,item_no:1,verdict:'unclear',first_error:'题干不足',cause_code:'unclear',cause_evidence:'关键公式无法辨认',knowledge_points:[],correct_solution:'',final_answer:'',prevention_cue:'补充清晰图片',confidence:0.1}}]}},unclearExec);
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
// Missing independent solutions in a later image must be repairable before any write.
const itemSchema=processTool.parameters.properties.items.items;
for(const field of ['correct_solution','final_answer']){{
  assert.ok(itemSchema.required.includes(field),'model schema must require '+field);
  assert.match(itemSchema.properties[field].description,/independent/);
  assert.match(itemSchema.properties[field].description,/verified_reference/);
}}
const repairAgent={{id:'repair-session',session:agent.session}};
const repairExec={{agent:repairAgent,signal:exec.signal}};
const repairFrozen=await transcribe.execute({{items:[
  {{attachment_index:1,item_no:1,question_text:'题干一：若 x²=4，求 x。',answer_text:'x=2',review}},
  {{attachment_index:2,item_no:1,question_text:'题干二：解方程 y+1=3。',answer_text:'y=1',review}}
]}},repairExec);
assert.deepEqual(repairFrozen.items.map(item=>item.grading_strategy),['verified_reference','independent']);
const repairText=transcribe.output.render({{}},repairFrozen)[0].text;
assert.match(repairText,/correct_solution/);
assert.match(repairText,/final_answer/);
const writesBeforeRepair=processCalls;
const imageOneBeforeRepair=imageOneWrites;
for(const verdict of ['correct','partial','incorrect']){{
  for(const field of ['correct_solution','final_answer']){{
    for(const value of [undefined,'','   ',null,12]){{
      const incomplete={{...grade(2,'由 y+1=3，得 y=2。'),verdict,[field]:value}};
      if(value===undefined) delete incomplete[field];
      await assert.rejects(()=>processTool.execute({{batch_ref:repairFrozen.batch_ref,items:[grade(1),incomplete]}},repairExec),error=>{{
        assert.match(error.message,/图片 2 第 1 题缺少/);
        assert.ok(error.message.includes(field));
        assert.ok(error.message.includes(repairFrozen.batch_ref));
        assert.match(error.message,/全部未完成题目：1:1, 2:1/);
        assert.match(error.message,/无需重传图片或重新转写/);
        assert.deepEqual(JSON.parse(error.message.split('LZLM_PROTECTED_V1 ').at(-1)),[{{key:'batch:'+repairFrozen.batch_ref,status:'active'}}]);
        return true;
      }});
      assert.equal(processCalls,writesBeforeRepair,'validation of all items must precede every write');
    }}
  }}
}}
const repaired=await processTool.execute({{batch_ref:repairFrozen.batch_ref,items:[grade(1),grade(2,'由 y+1=3，得 y=2。')]}},repairExec);
assert.equal(repaired.results.length,2);
assert.equal(repaired.batch_ref,repairFrozen.batch_ref,'repair must keep the exact frozen batch');
assert.equal(imageOneWrites,imageOneBeforeRepair+1,'repair writes the first image exactly once');
assert.equal(processCalls,writesBeforeRepair+2,'repair processes both pending images exactly once');
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
