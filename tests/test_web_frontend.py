import json
from pathlib import Path
import unittest
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class FrontendContractTests(unittest.TestCase):
    def test_question_markup_renders_only_content_addressed_diagrams(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is needed for frontend behavior checks")
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
const escape = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const context = {document:{createElement:()=>{let value=''; return {set textContent(v){value=v},get innerHTML(){return escape(value)}}}}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function escapeHtml('), source.indexOf('function wrapPlainMath(')), context);
const digest = 'a'.repeat(64);
const html = context.questionMarkup(`题目<script>坏</script>\n![示意图](bank-assets/${digest}.png)`);
assert.ok(html.includes('题目&lt;script&gt;坏&lt;/script&gt;'));
assert.ok(html.includes(`/v1/question-assets/${digest}.png`));
assert.ok(html.includes('class="question-diagram"'));
const rejected = context.questionMarkup('![图](bank-assets/../../secret.png)');
assert.ok(!rejected.includes('<img'));
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        diagram_rule = (WEB / "app.css").read_text(encoding="utf-8").split(".question-diagram {", 1)[1].split("}", 1)[0]
        self.assertIn("max-width: min(100%, 520px)", diagram_rule)
        self.assertIn("max-height: 360px", diagram_rule)

    def test_notebook_cards_show_full_error_ids(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is needed for frontend behavior checks")
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8'), list = {innerHTML:''};
const ids = ['0123456789abcdef0123456789abcdef', 'fedcba9876543210fedcba9876543210'];
const context = {$:()=>list, errors:ids.map((error_id,i)=>({error_id,status:i?'mastered':'open',
  created_at:'2026-08-31T00:00:00Z', question_text:'题目', first_error:'错因'})),
  selectedErrorIds:new Set([ids[0]]), selectionMode:'auto', causeLabels:{},
  stageLabel:item=>item.status==='mastered'?'已掌握':'待安排', escapeHtml:s=>String(s??''),
  questionMarkup:s=>String(s??''), renderMath:()=>{}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  function renderErrors()'), source.indexOf('  async function showError(')),context);
for (const mode of ['auto','manual','fixed']) {
  context.selectionMode = mode;
  context.renderErrors();
  const displayed = [...list.innerHTML.matchAll(/<p class="error-record-id">错题编号（error_id）：<code>([0-9a-f]{32})<\/code><\/p>/g)].map(m=>m[1]);
  assert.deepEqual(displayed,ids);
  for (const id of ids) assert.ok(list.innerHTML.includes(`data-error-detail="${id}"`));
}
context.errors = [];
context.renderErrors();
assert.ok(list.innerHTML.includes('还没有错题。'));
assert.ok(!list.innerHTML.includes('error-record-id'));
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn("overflow-wrap: anywhere", css.split(".error-record-id {", 1)[1].split("}", 1)[0])
        self.assertIn("user-select: text", css.split(".error-record-id code {", 1)[1].split("}", 1)[0])

    def test_calendar_controls_are_borderless_accessible_icons(self) -> None:
        html = (WEB / "progress.html").read_text(encoding="utf-8")
        css = (WEB / "app.css").read_text(encoding="utf-8")
        for control, label in (("calendar-day-close", "关闭日期详情"), ("calendar-prev", "上个月"), ("calendar-next", "下个月")):
            button = html.split(f'<button id="{control}"', 1)[1].split('</button>', 1)[0]
            self.assertIn(f'aria-label="{label}"', button)
            self.assertIn('<svg ', button)
            self.assertIn('aria-hidden="true"', button)
            self.assertNotIn('class="ghost"', button)
            self.assertIn(f'#{control}:hover', css)
        self.assertIn('#calendar-prev, #calendar-next, #calendar-day-close {', css)
        rule = css.split('#calendar-day-close {', 1)[1].split('}', 1)[0]
        for style in ('width: 32px', 'height: 32px', 'border: 0', 'border-radius: 6px', 'background: transparent'):
            self.assertIn(style, rule)
        self.assertIn('#calendar-day-close:hover', css)
        self.assertIn('button:focus-visible', css)

    def test_calendar_backlog_events_preserve_history_and_limit_selection(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is needed for frontend behavior checks")
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
const nodes = new Map(), storage = new Map();
function $(id) {
  if (!nodes.has(id)) nodes.set(id, {textContent:'', innerHTML:'', hidden:true, handlers:{},
    addEventListener(event, fn) { this.handlers[event] = fn; }, replaceChildren(...children) { this.children=children; }, scrollIntoView() {}, querySelectorAll() {return [];} });
  return nodes.get(id);
}
const errors = Array.from({length:13}, (_,i) => ({error_id:String(i),status:'open',question_text:'题目',first_error:'错因',
  diagnosis:{knowledge_points:['知识点']}, review:{due_at:'2026-07-01T00:00:00Z',status:'pending',stage:2}}));
const history = {days:[{date:'2026-08-24',items:[{type:'due',error_id:'historic',stage:1}],due_review_count:1}],
  summary:{due_review_count:1},total_error_count:13};
const before = JSON.stringify(history);
const requests = [];
let todayPlan = null, failPlanRead = false, beforeRecommendations = null;
const context = {Intl, $, Date:class extends Date {constructor(...args) {super(...(args.length ? args : ['2026-08-30T16:30:00Z']));}},
  crypto:{randomUUID:()=>`key-${requests.length}`}, document:{createElement:tag=>({tag,setAttribute(){}})},
  sessionStorage:{getItem:k=>storage.get(k)??null, setItem:(k,v)=>storage.set(k,v)},
  escapeHtml:s=>String(s??''), questionMarkup:s=>String(s??''), renderMath:()=>{}, authError:e=>String(e), status:(node,text,error=false)=>{node.textContent=text;node.error=error;},
  api:async (path,options={})=>{requests.push({path,options}); if(path==='/v1/errors') return {items:errors,selection_scope:'a'.repeat(24)};
    if(path==='/v1/progress') return {today_completed_review_count:0};
    if(path==='/v1/practice-pdfs') {if(failPlanRead) throw new Error('unavailable');
      return options.method==='POST' ? {download_url:'/v1/practice-pdfs/paper/download'} : {items:[],today_plan:todayPlan};}
    if(path.includes('/recommendations?')) {beforeRecommendations?.(); return {items:[],gap:false};} return history;}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function chinaDate('), source.indexOf('function bindPractice(')), context);
const tick = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const reload = context.bindProgress(); await tick();
  assert.equal($('#progress-status').textContent,'数据已更新。');
  const grid = $('#review-calendar').innerHTML;
  assert.ok(grid.includes('今日到期<strong>0</strong>'));
  assert.ok(grid.includes('历史逾期<strong>13</strong>'));
  assert.ok(grid.includes('完成复习组<strong>0</strong>'));
  assert.ok(grid.includes('data-calendar-date="2026-08-24"'));
  assert.equal($('#calendar-stat-due').textContent,1);
  $('#review-calendar').handlers.click({target:{closest:()=>({dataset:{calendarDate:'2026-08-31',calendarKind:'backlog'}})}});
  assert.equal($('#calendar-day-detail').hidden,false);
  assert.equal(($('#calendar-day-items').innerHTML.match(/name="calendar-error"/g)||[]).length,13);
  assert.ok($('#calendar-day-items').innerHTML.includes('原定 2026-07-01'));
  assert.ok($('#calendar-day-items').innerHTML.includes('错误原因：'));
  const change = (value,checked) => {const target={name:'calendar-error',value,checked};$('#calendar-day-items').handlers.change({target});return target;};
  for(let i=0;i<12;i++) change(String(i),true);
  assert.equal(change('12',true).checked,false);
  assert.ok($('#calendar-selection-status').textContent.includes('最多选择 12 道'));
  assert.equal(context.readReviewSelection('a'.repeat(24),new Map(errors.map(x=>[x.error_id,x.review.review_id||null]))).length,12);
  change('0',false); change('12',true);
  assert.equal(context.readReviewSelection('a'.repeat(24),new Map(errors.map(x=>[x.error_id,x.review.review_id||null]))).includes('12'),true);
  assert.equal($('#calendar-generate-pdf').disabled,false);
  const generate = () => $('#calendar-generate-pdf').handlers.click({currentTarget:$('#calendar-generate-pdf')});
  const pending = generate();
  assert.equal($('#calendar-generate-pdf').disabled,true);
  assert.equal(($('#calendar-day-items').innerHTML.match(/ disabled/g)||[]).length,13);
  change('12',false); // Cannot edit the print selection while generating.
  await Promise.all([pending,generate()]);
  const generated = requests.find(row=>row.path==='/v1/practice-pdfs'&&row.options.method==='POST');
  assert.equal(JSON.parse(generated.options.body).error_ids.length,12);
  assert.equal(requests.filter(row=>row.path==='/v1/practice-pdfs'&&row.options.method==='POST').length,1);
  assert.equal($('#calendar-pdf-status').children[1].href,'/v1/practice-pdfs/paper/download');
  assert.equal($('#calendar-pdf-status').children[1].target,'_top');
  await reload();
  assert.equal(($('#calendar-day-items').innerHTML.match(/ checked/g)||[]).length,12);
  assert.equal(JSON.stringify(history),before);
  // A plan created by another page after loading must be reused without writes.
  const fixed = {available:true,items:errors.slice(1),download_url:'/v1/practice-pdfs/existing/download'};
  todayPlan = fixed;
  const writes = () => requests.filter(row=>row.options.method==='POST').length;
  const writesBefore = writes();
  await generate();
  assert.equal(writes(),writesBefore);
  assert.equal($('#calendar-generate-pdf').disabled,true);
  assert.ok($('#calendar-pdf-status').innerHTML.includes(fixed.download_url));
  change('12',false);
  await generate();
  assert.equal(writes(),writesBefore);
  todayPlan = null; await reload();
  failPlanRead = true; await generate();
  assert.equal(writes(),writesBefore);
  assert.equal($('#calendar-pdf-status').error,true);
  assert.equal($('#calendar-generate-pdf').disabled,false);
  failPlanRead = false;
  // Check again after recommendation matching, before submitting the PDF job.
  beforeRecommendations = () => {todayPlan=fixed;};
  await generate();
  assert.equal(requests.filter(row=>row.path==='/v1/practice-pdfs'&&row.options.method==='POST').length,1);
  assert.equal($('#calendar-generate-pdf').disabled,true);
  todayPlan = null; beforeRecommendations = null; await reload();
  context.sessionStorage.setItem=()=>{throw new Error('blocked');}; change('12',false);
  assert.equal($('#calendar-selection-status').error,true);
  assert.ok($('#calendar-selection-status').textContent.includes('无法保存选题'));
  // Historical details use the server's day-end snapshot, not today's list.
  const recorded = id => ({type:'due',error_id:id,stage:2,question_text:'题目',first_error:'错因',knowledge_points:['知识点'],original_due_date:'2026-07-01'});
  history.backlog_items = [recorded('0'),recorded('finished')];
  history.days[0].backlog_indices = [0,1];
  history.days[0].history_complete = false;
  await reload();
  const clickDay = (date,kind='') => $('#review-calendar').handlers.click({target:{closest:()=>({dataset:{calendarDate:date,calendarKind:kind}})}});
  clickDay('2026-08-24','backlog');
  assert.ok($('#calendar-history-note').textContent.includes('记录不完整'));
  assert.equal(($('#calendar-day-items').innerHTML.match(/<article/g)||[]).length,2);
  assert.equal(($('#calendar-day-items').innerHTML.match(/name="calendar-error"/g)||[]).length,1);
  const storedBefore = JSON.stringify([...storage]);
  change('finished',true);
  assert.equal(JSON.stringify([...storage]),storedBefore);
  clickDay('2026-08-23');
  assert.equal($('#calendar-day-detail').hidden,false);
  assert.ok($('#calendar-day-items').innerHTML.includes('没有符合筛选条件'));
  // Paper progress belongs to the printed day; submitted activity belongs to
  // the real day. Partial work must remain visible while group count is zero.
  const row = {item_id:'q1',error_id:'historic',question_text:'推荐题内容',kind:'recommendation',stage:1,
    status:'correct',required:true,submitted_at:'2026-08-30T16:10:00Z'};
  const paper = {task_id:'paper1',filename:'29日练习.pdf',progress:{available:true,answered_count:1,required_count:3,
    pending_count:2,needs_correction_count:0,groups:[{error_id:'historic',stage:1,answered_count:1,required_count:3}],items:[row]}};
  history.days.push({date:'2026-08-29',items:[],practice_plans:[paper],paper_answered_count:1,paper_required_count:3},
    {date:'2026-08-31',items:[],practice_activity:[{...row,filename:paper.filename}],submitted_question_count:1});
  history.summary.submitted_question_count=1;
  await reload();
  assert.ok($('#review-calendar').innerHTML.includes('PDF已答<strong>1/3</strong>'));
  assert.ok($('#review-calendar').innerHTML.includes('当日已答题<strong>1</strong>'));
  assert.equal($('#calendar-stat-answered').textContent,1);
  clickDay('2026-08-29','papers');
  assert.ok($('#calendar-day-items').innerHTML.includes('已答 1/3'));
  assert.ok($('#calendar-day-items').innerHTML.includes('重印共享作答'));
  assert.ok($('#calendar-day-items').innerHTML.includes('待答 2'));
  clickDay('2026-08-31','answered');
  assert.ok($('#calendar-day-items').innerHTML.includes('实际提交'));
  assert.ok($('#calendar-day-items').innerHTML.includes('已答正确'));
  // Move to a future month: only real pending plans, with no completion/overdue prediction.
  errors.push({error_id:'future',status:'open',review:{stage:3,status:'pending',due_at:'2026-09-02T00:00:00Z'}});
  history.days = [{date:'2026-09-02',items:[{...recorded('future'),stage:3,original_due_date:'2026-09-02'}],stage_counts:{'3':1}}];
  $('#calendar-next').handlers.click(); await tick();
  const futureGrid = $('#review-calendar').innerHTML;
  assert.ok(futureGrid.includes('计划复习<strong>1</strong>'));
  assert.ok(futureGrid.includes('第3阶段×1'));
  assert.ok(!futureGrid.includes('当日未完成') && !futureGrid.includes('完成复习组'));
  clickDay('2026-09-02','due');
  assert.equal(($('#calendar-day-items').innerHTML.match(/name="calendar-error"/g)||[]).length,1);
  assert.ok($('#calendar-history-note').textContent.includes('选题不改变原定复习日期'));
  $('#calendar-day-close').handlers.click();
  assert.equal($('#calendar-day-detail').hidden,true);
  $('#calendar-prev').handlers.click(); await tick();
  assert.equal($('#calendar-month').textContent,'2026 年 8 月');
  // Opening today's due list selects only eligible items, once, up to 12.
  context.sessionStorage.setItem=(k,v)=>storage.set(k,v);
  storage.clear();
  errors.splice(0,errors.length,...Array.from({length:13},(_,i)=>({error_id:String(i),status:'open',
    review:{review_id:`today-${i}`,stage:2,status:'pending',due_at:'2026-08-31T00:00:00Z'}})));
  history.days=[{date:'2026-08-31',items:[
    ...errors.map(error=>({...recorded(error.error_id),original_due_date:'2026-08-31'})),
    {...recorded('0'),original_due_date:'2026-08-31'},
    {...recorded('finished'),original_due_date:'2026-08-31'}]}];
  todayPlan=null; await reload();
  const writesBeforeDefaults=writes();
  clickDay('2026-08-31','due');
  assert.equal(($('#calendar-day-items').innerHTML.match(/ checked/g)||[]).length,12);
  assert.equal(($('#calendar-day-items').innerHTML.match(/name="calendar-error"/g)||[]).length,13);
  assert.equal($('#calendar-generate-pdf').disabled,false);
  const reviews=new Map(errors.map(error=>[error.error_id,error.review]));
  assert.deepEqual([...context.readReviewSelection('a'.repeat(24),reviews)],errors.slice(0,12).map(error=>error.error_id));
  assert.equal(writes(),writesBeforeDefaults); // Selecting never generates a PDF.
  change('0',false);
  clickDay('2026-08-31','due');
  assert.equal(($('#calendar-day-items').innerHTML.match(/ checked/g)||[]).length,11);
  await reload();
  assert.equal(($('#calendar-day-items').innerHTML.match(/ checked/g)||[]).length,11);
  for(let i=1;i<12;i++) change(String(i),false);
  clickDay('2026-08-31','due');
  assert.equal(($('#calendar-day-items').innerHTML.match(/ checked/g)||[]).length,0);
  assert.equal($('#calendar-generate-pdf').disabled,true); // Keep deliberate deselect-all.
  todayPlan={available:true,items:errors.slice(0,2),download_url:'/fixed'};
  storage.clear(); await reload();
  clickDay('2026-08-31','due');
  assert.equal(($('#calendar-day-items').innerHTML.match(/ checked/g)||[]).length,2);
  assert.equal(($('#calendar-day-items').innerHTML.match(/ disabled/g)||[]).length,13);
  assert.equal(storage.size,0); // Fixed plans are not rewritten by defaults.
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_today_summary_and_selection_are_date_and_account_scoped(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is needed for frontend behavior checks")
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
const helpers = source.slice(source.indexOf('function chinaDate('), source.indexOf('function bindProgress('));
const data = new Map();
const context = {Intl, Date, Set, sessionStorage: {getItem: k => data.get(k) ?? null, setItem: (k,v) => data.set(k,v)}};
vm.createContext(context); vm.runInContext(helpers, context);
const now = new Date('2026-08-30T16:30:00Z');
assert.equal(context.chinaDate(now), '2026-08-31');
function error(id,due,status='open',taskStatus='pending') {
  return {error_id:id, status, question_text:'题目', first_error:'错因', diagnosis:{knowledge_points:['知识点']}, review:{due_at:due, status:taskStatus,stage:2}};
}
const errors = [error('old','2026-07-01T00:00:00Z'), error('yesterday','2026-08-30T15:59:59Z'),
  error('today','2026-08-30T16:00:00Z'), error('later','2026-08-31T15:59:59Z'),
  error('future','2026-08-31T16:00:00Z'), error('removed','2026-07-01T00:00:00Z','removed'),
  error('mastered','2026-07-01T00:00:00Z','mastered'), error('done','2026-07-01T00:00:00Z','open','completed'), error('bad','invalid')];
const before = JSON.stringify(errors);
const today = context.todayReviewSnapshot(errors, {today_completed_review_count:3}, now);
assert.equal(today.date, '2026-08-31'); assert.equal(today.due_count,2); assert.equal(today.completed_count,3);
assert.equal(JSON.stringify(today.overdue_items.map(x=>x.error_id)), JSON.stringify(['old','yesterday']));
assert.equal(today.overdue_items[0].original_due_date,'2026-07-01');
assert.equal(JSON.stringify(errors), before);
const empty = context.todayReviewSnapshot([],{},now);
assert.equal(empty.due_count,0); assert.equal(empty.completed_count,0); assert.equal(empty.overdue_items.length,0);
const ids = new Map([['old',{review_id:'r-old'}],['yesterday',{review_id:'r-yesterday'}]]);
assert.equal(context.readReviewSelection('a'.repeat(24),ids,now), null);
assert.equal(context.writeReviewSelection('a'.repeat(24),new Set(['old','stale']),ids,now),true);
assert.equal(JSON.stringify(context.readReviewSelection('a'.repeat(24),ids,now)), '["old"]');
assert.equal(context.readReviewSelection('b'.repeat(24),ids,now),null);
assert.equal(context.readReviewSelection('a'.repeat(24),ids,new Date('2026-08-31T16:00:00Z')),null);
context.writeReviewSelection('a'.repeat(24),new Set(),ids,now);
assert.equal(JSON.stringify(context.readReviewSelection('a'.repeat(24),ids,now)), '[]');
const many = new Map(Array.from({length:20},(_,i)=>[String(i),{review_id:`r-${i}`}]))
context.writeReviewSelection('a'.repeat(24),new Set(many.keys()),many,now);
assert.equal(context.readReviewSelection('a'.repeat(24),many,now).length,12);
data.clear();
context.writeReviewSelection('a'.repeat(24),new Set(['old']),ids,now);
assert.equal(context.readReviewSelection('a'.repeat(24),new Map([['old',{review_id:'new-round'}]]),now).length,0);
const due = Array.from({length:14},(_,i)=>({error_id:`e${i}`}));
assert.equal(JSON.stringify(context.resolveReviewSelection({fixedPlan:null,dueReviews:due,saved:null,mode:'auto',currentIds:new Set()}).ids),JSON.stringify(due.slice(0,12).map(x=>x.error_id)));
assert.equal(JSON.stringify(context.resolveReviewSelection({fixedPlan:null,dueReviews:due.slice(2),saved:null,mode:'auto',currentIds:new Set()}).ids),JSON.stringify(due.slice(2,14).map(x=>x.error_id)));
assert.equal(JSON.stringify(context.resolveReviewSelection({fixedPlan:null,dueReviews:due,saved:['e7'],mode:'manual',currentIds:new Set()}).ids),'["e7"]');
const fixed = {available:true,items:[{error_id:'e1',status:'completed'},{error_id:'e2',status:'pending'}]};
assert.equal(JSON.stringify(context.resolveReviewSelection({fixedPlan:fixed,dueReviews:due.slice(5),saved:null,mode:'auto',currentIds:new Set()}).ids),'["e1","e2"]');
context.sessionStorage.setItem = () => {throw new Error('blocked');};
assert.equal(context.writeReviewSelection('a'.repeat(24),new Set(ids.keys()),ids,now),false);
assert.equal(context.writeReviewSelection('',new Set(ids.keys()),ids,now),false);
assert.ok(source.includes('data-calendar-kind="${kind}"'));
assert.ok(source.includes('plan_kind: "daily_review"'));
assert.ok(!source.includes('plan_kind: "practice"'));
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_admin_code_removed_without_removing_shared_model_usage(self) -> None:
        self.assertFalse((WEB / "admin.html").exists())
        self.assertFalse((WEB / "admin.js").exists())
        self.assertFalse(list((ROOT / "services" / "web_ops").glob("*.py")))
        style = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertNotIn(".admin-", style)
        launcher = (ROOT / "scripts" / "local_env.py").read_text(encoding="utf-8")
        self.assertNotIn("grant-admin", launcher)
        self.assertNotIn("web_ops", launcher)
        self.assertIn("/v1/harness/sessions/usage", (ROOT / "extensions" / "dsh-math-notebook-ui" / "lib" / "client.js").read_text(encoding="utf-8"))

    def test_product_pages_remain_independent_documents(self) -> None:
        pages = {
            "errors.html": ('data-page="errors"', 'id="all-errors"'),
            "practice.html": ('data-page="practice"', 'id="practice-pdf-history"'),
            "progress.html": ('data-page="progress"', 'id="review-rule-heading"'),
            "settings.html": ('data-page="settings"', 'id="sensitive-form"'),
        }
        unique_markers = [marker for _, marker in pages.values()]
        for filename, (page_marker, own_marker) in pages.items():
            html = (WEB / filename).read_text(encoding="utf-8")
            self.assertIn(page_marker, html)
            self.assertIn(own_marker, html)
            self.assertIn('/assets/branding/logo-symbol-color-64-v1.png', html)
            self.assertIn('/web/vendor/katex/katex.min.js', html)
            self.assertIn('/web/vendor/katex/auto-render.min.js', html)
            self.assertIn('李兆霖数学错题本', html)
            for route in ('href="/"', 'href="/errors"', 'href="/practice"', 'href="/progress"', 'href="/settings"'):
                self.assertIn(route, html)
            for icon in ("errors", "practice", "progress", "settings"):
                self.assertIn(f'/web/nav-icons.svg#{icon}', html)
            self.assertNotIn('href="/reviews"', html)
            self.assertNotIn('/web/nav-icons.svg#workbench', html)
            self.assertIn('aria-label="返回工作台"', html)
            self.assertIn('<span>设置</span>', html)
            self.assertNotIn('<span>设置与隐私</span>', html)
            self.assertNotIn('href="#', html)
            for other_marker in unique_markers:
                if other_marker != own_marker:
                    self.assertNotIn(other_marker, html)
        self.assertFalse((WEB / "reviews.html").exists())
        self.assertTrue((WEB / "progress.html").exists())

    def test_error_notebook_focuses_on_today_and_error_records(self) -> None:
        html = (WEB / "errors.html").read_text(encoding="utf-8")
        script = (WEB / "app.js").read_text(encoding="utf-8")
        for text in ("今日的复习计划", "待关联判题", "全部错题"):
            self.assertIn(text, html)
        for text in ("六阶段复习规则", "主动提取", "间隔效应", "即时反馈", "各复习阶段"):
            self.assertNotIn(text, html)
        for marker in ("generate-review-pdf", "selected-error-count"):
            self.assertIn(f'id="{marker}"', html)
        self.assertNotIn('id="today-review-items"', html)
        self.assertNotIn("renderDueReviews", script)
        for contract in ('api("/v1/errors")', 'api("/v1/reviews/today")', 'api("/v1/progress")', 'api("/v1/practice-pdfs"', 'api("/v1/practice-review-links")'):
            self.assertIn(contract, script)
        self.assertIn("data-review-link-candidate", script)
        self.assertIn('name="today-error"', script)
        self.assertIn('data-error-detail=', script)
        self.assertNotIn('id="error-detail"', html)
        self.assertNotIn("$$('", script)
        self.assertIn("today_needs_correction_count", script)
        self.assertIn('timeZone: "Asia/Shanghai"', script)
        self.assertIn('fixedPlan = pdfResult.today_plan', script)
        self.assertIn('"今日已生成，计划不再自动换题"', script)
        self.assertIn('/recommendations?limit=1', script)

    def test_product_pages_show_deterministic_daily_learning_usage(self) -> None:
        script = (WEB / "app.js").read_text(encoding="utf-8")
        style = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn('api("/v1/learning-usage")', script)
        self.assertIn("今日学习负荷", script)
        self.assertIn("grade.count", script)
        self.assertIn("recommendation.count", script)
        self.assertIn(".learning-usage-strip", style)

    def test_learning_progress_owns_review_rules_and_activity_calendar(self) -> None:
        html = (WEB / "progress.html").read_text(encoding="utf-8")
        script = (WEB / "app.js").read_text(encoding="utf-8")
        style = (WEB / "app.css").read_text(encoding="utf-8")
        for text in ("六阶段复习规则", "主动提取", "间隔效应", "即时反馈", "错题与复习日历", "新增错题", "应复习", "需改错", "待补知识", "逾期", "复习正确率"):
            self.assertIn(text, html)
        self.assertNotIn("各复习阶段", html)
        for marker in ("review-calendar", "calendar-month", "calendar-prev", "calendar-next", "calendar-summary", "calendar-stats", "calendar-filters", "calendar-day-detail", "calendar-day-items", "calendar-generate-pdf", "calendar-pdf-status"):
            self.assertIn(f'id="{marker}"', html)
        self.assertNotIn('id="refresh-progress"', html)
        self.assertNotIn('$("#refresh-progress")', script)
        self.assertNotIn('id="stage-count-1"', html)
        self.assertIn('function bindProgress()', script)
        self.assertIn('api(`/v1/progress/calendar?month=${monthKey()}`)', script)
        self.assertIn('data-calendar-filter', html)
        self.assertIn('data-calendar-date', script)
        self.assertIn('knowledge_points', script)
        self.assertGreater(html.index('id="review-rule-heading"'), html.index('id="progress-status"'))
        self.assertIn('.calendar-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));', style)
        self.assertIn('.calendar-filters { display: inline-flex; gap: 2px; padding: 4px;', style)
        self.assertIn('.calendar-filters button[aria-pressed="true"] { background: var(--paper); color: var(--brand);', style)
        self.assertIn('/defer`', script)
        self.assertIn('/resume`', script)

    def test_workbench_is_the_official_deepseek_harness_surface(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        plugin = (ROOT / "extensions" / "dsh-math-notebook-ui" / "lib" / "client.js").read_text(encoding="utf-8")
        host_plugin = (ROOT / "extensions" / "dsh-math-notebook-ui" / "lib" / "index.js").read_text(encoding="utf-8")
        patch = (ROOT / "config" / "deepseek-harness" / "web-product.patch.yml").read_text(encoding="utf-8")
        runtime_config = (ROOT / "config" / "deepseek-harness" / "cordis.yml").read_text(encoding="utf-8")
        preset = (ROOT / "config" / "deepseek-harness" / "agent-presets" / "math-notebook" / "agent.cordis.yml").read_text(encoding="utf-8")
        dependencies = package["dependencies"]
        self.assertEqual(dependencies["@deepseek-ai/cordis-plugin-group"], "1.0.2")
        self.assertEqual(dependencies["@deepseek-ai/dsh"], "0.1.1-rc.2")
        self.assertEqual(dependencies["@deepseek-ai/dsh-web-frontend"], "0.1.1-rc.2")
        self.assertEqual(dependencies["@deepseek-ai/dsh-compaction-tool-result-pruner"], "0.1.1-rc.2")
        self.assertEqual(dependencies["@deepseek-ai/dsh-sdk-protocol"], "0.1.1-rc.2")
        self.assertEqual(dependencies["@lizhaolin/dsh-math-notebook-ui"], "file:extensions/dsh-math-notebook-ui")
        self.assertIn('data-page="harness-workbench"', html)
        self.assertIn('id="harness-frame"', html)
        self.assertIn('allow="clipboard-read; clipboard-write"', html)
        self.assertIn('fetch("/v1/session"', html)
        self.assertIn('location.replace("/login")', html)
        self.assertIn('frame.src = "/harness/"', html)
        self.assertNotIn("3080", html)
        self.assertNotIn('/web/app.js', html)
        self.assertNotIn('id="upload-form"', html)
        self.assertIn('李兆霖数学错题本', plugin)
        self.assertIn('id: "math-notebook-navigation"', plugin)
        self.assertIn('sidebar.footer.action', plugin)
        for label, route in (("错题本", "/errors"), ("练习 PDF", "/practice"), ("学习进度", "/progress")):
            self.assertIn(f'path: "{route}", label: "{label}"', plugin)
        self.assertNotIn('path: "/reviews"', plugin)
        self.assertNotIn("今日复习", plugin)
        self.assertNotIn('path: "/", label: "工作台"', plugin)
        self.assertIn('ctx.slots.register({name: "conversation", priority: -1}, ProductSurface)', plugin)
        self.assertIn('data-lzlm-product-surface', plugin)
        self.assertIn('?embedded=1', plugin)
        self.assertIn('data-lzlm-product-path', plugin)
        self.assertIn('/v1/harness/navigation-status', plugin)
        self.assertIn('installProductNavigationStatus(ctx)', plugin)
        self.assertIn('markProductNavigationSeen(path)', plugin)
        self.assertIn('setInterval(refresh, 15000)', plugin)
        self.assertIn('data-lzlm-nav-status', plugin)
        self.assertIn('kind: "pending"', plugin)
        self.assertIn('待复习 ${payload.progress.due_count} 道，需改错 ${payload.progress.needs_correction_count} 道', plugin)
        self.assertIn('element.textContent === "探索未至之境"', plugin)
        self.assertIn('element.textContent = "今天要整理哪道错题?"', plugin)
        self.assertIn('textarea[placeholder="描述你想要构建的内容"]', plugin)
        self.assertIn('element.placeholder = "学习不是熊瞎子掰棒子"', plugin)
        self.assertIn('customizeStudentCopy(ctx)', plugin)
        self.assertIn('[data-variant="think"] [class*="_leading"]', plugin)
        self.assertIn('logo-symbol-color-64-v1.png', plugin)
        self.assertIn('[data-variant="think"] [class*="_leading"] svg', plugin)
        self.assertIn("closeProductOnSessionClick(ctx)", plugin)
        self.assertIn("event.target.closest('[role=\"treeitem\"]')", plugin)
        self.assertNotIn('target: "_top",\n          title: item.label', plugin)
        self.assertNotIn('path: "/settings", label: "设置与隐私"', plugin)
        self.assertIn('id: "account-privacy"', plugin)
        self.assertIn('name: "settings.section"', plugin)
        self.assertIn('label: "账号与隐私"', plugin)
        self.assertIn('src: `${productOrigin}/settings?embedded=1`', plugin)
        self.assertIn('title: "账号与隐私"', plugin)
        self.assertIn('order: -10', plugin)
        self.assertIn('restrictStudentSettings(ctx)', plugin)
        self.assertIn('button.toggleAttribute("data-lzlm-student-hidden", button.textContent.trim() !== "账号与隐私")', plugin)
        self.assertIn('["打开配置文件", "Open configuration file"]', plugin)
        self.assertIn('[data-lzlm-student-hidden]', plugin)
        self.assertNotIn('href: `${productOrigin}/settings`', plugin)
        self.assertIn('aria-label": "错题本功能导航"', plugin)
        self.assertIn('button[aria-label="选择工作区"]', plugin)
        self.assertIn('button[aria-label="添加工作区"]', plugin)
        self.assertIn('button[aria-label="新建会话"]:has(svg)', plugin)
        self.assertIn('button[aria-label="New session"]:has(svg)', plugin)
        self.assertIn('item.title === productWorkspaceTitle', plugin)
        self.assertIn('ctx.workspaces.connectWorkspace(workspace.workspaceId)', plugin)
        self.assertIn('ctx.sessions.open(sessionId)', plugin)
        for plugin_id in ("permission", "command-feedback", "ui-settings-models", "ui-model-selection", "ui-settings-plugin-inventory", "ui-settings-plugins"):
            self.assertRegex(patch, rf"- id: {plugin_id}\s+disabled: true")
        self.assertRegex(patch, r"- id: attachment-local\s+config:\s+maxImagesPerMessage: 10")
        self.assertRegex(runtime_config, r"- id: attachment-local\s+name: '@deepseek-ai/dsh-attachment-local'\s+config:\s+dshHome: .*\s+maxImagesPerMessage: 10")
        self.assertIn('ctx.workspaceRegistry.create(workspacePath, "错题会话")', host_plugin)
        self.assertIn('LZLM_HARNESS_WORKSPACE_ROOT', host_plugin)
        self.assertIn('name: "transcribe_error_notebook_attachments"', host_plugin)
        self.assertIn('name: "confirm_error_notebook_entry"', host_plugin)
        self.assertIn('name: "process_error_notebook_attachments"', host_plugin)
        self.assertIn('review_association', host_plugin)
        self.assertIn('name: "recheck_error_notebook_reference_conflict"', host_plugin)
        self.assertIn('name: "lookup_question_bank_reference"', host_plugin)
        self.assertIn('name: "inspect_math_notebook"', host_plugin)
        self.assertIn('name: "reflow_practice_pdf"', host_plugin)
        self.assertIn('name: "adjudicate_error_notebook_reference_conflicts"', host_plugin)
        self.assertIn('authoritative_grade', host_plugin)
        self.assertIn('/v1/internal/harness/reference-conflicts/recheck', host_plugin)
        self.assertIn('/v1/internal/harness/question-bank/reference', host_plugin)
        self.assertIn('/v1/internal/harness/grading-references', host_plugin)
        self.assertIn('/v1/internal/harness/context', host_plugin)
        self.assertIn('/v1/internal/harness/practice-reviews/retry', host_plugin)
        self.assertIn('retry_practice_review_confirmation', host_plugin)
        self.assertIn('/v1/internal/harness/practice-pdfs/', host_plugin)
        self.assertIn('payload?.error?.code', host_plugin)
        self.assertIn('/v1/internal/harness/reference-conflicts/adjudicate', host_plugin)
        self.assertNotIn('anyOf:', host_plugin)
        self.assertIn('ctx.attachments.readImage', host_plugin)
        self.assertIn('transcriptionByAgent', host_plugin)
        self.assertIn('不得重新看图改写题干或作答', patch)
        self.assertIn('/v1/internal/harness/intakes/process', host_plugin)
        self.assertIn('latestUserImages(exec.agent)', host_plugin)
        self.assertNotIn('The latest user message has no image attachments', host_plugin)
        self.assertIn('当前消息没有可读取的图片附件', host_plugin)
        self.assertIn('return {schema: "math-notebook-transcription/v1", items: []}', host_plugin)
        self.assertIn('const images = frozen.images', host_plugin)
        self.assertIn('currentTurn(exec.agent) !== frozen.turn', host_plugin)
        self.assertIn('if (images.length > 10)', host_plugin)
        self.assertIn('一条消息最多上传 10 张图片', host_plugin)
        self.assertIn('correction_mode:', host_plugin)
        self.assertIn('review_code:', host_plugin)
        self.assertIn('exec.concludeTurn()', host_plugin)
        self.assertIn('/v1/internal/harness/grade-results/', host_plugin)
        self.assertIn('/v1/harness/sessions/bind', plugin)
        self.assertIn('const modelEpoch = "qwen3.8-flash-v2"', plugin)
        self.assertIn('ctx.workspaces.startSession(workspace.workspaceId)', plugin)
        self.assertIn('credentials: "include"', plugin)
        self.assertIn('const productOrigin = window.location.origin;', plugin)
        self.assertNotIn('http://127.0.0.1:8000', plugin)
        self.assertNotIn('/v1/internal/harness/intake-batches', host_plugin)
        self.assertIn('const items = args.items.filter', host_plugin)
        self.assertIn('required: ["items"]', host_plugin)
        self.assertIn('dataset.lzlmSelectionActions', plugin)
        self.assertIn('textContent = "添加到对话"', plugin)
        self.assertIn('ancestor.closest("[data-chat-flow]")', plugin)
        self.assertIn('conversation.input.for(sessionContext)', plugin)
        self.assertIn('input.setDraft(', plugin)
        self.assertIn('installSelectionToConversation(ctx)', plugin)
        self.assertIn("ui-brand-official", patch)
        self.assertIn("ui-math-notebook", patch)
        self.assertIn("tool-bash", patch)
        self.assertIn("tool-pwsh", patch)
        self.assertIn("default: math-notebook", patch)
        self.assertIn("includeUserRoot: true", patch)
        self.assertIn("@deepseek-ai/dsh-persona", preset)
        self.assertNotIn("dsh-tool-", preset)
        self.assertIn("@deepseek-ai/dsh-compaction-basic", preset)
        self.assertIn("@deepseek-ai/dsh-command-compact", preset)
        self.assertIn("@deepseek-ai/dsh-compaction-tool-result-pruner", preset)
        self.assertIn("confirm_error_notebook_entry", preset)
        for prompt in (patch, preset, runtime_config):
            self.assertIn('第一行必须是“错题编号（error_id）：<完整编号>”', prompt)
            self.assertIn('放在“题目整理”之前', prompt)
            self.assertIn('32 位小写十六进制 error_id', prompt)
            self.assertIn('在工具返回前不得猜测编号', prompt)
            self.assertIn('不得补零或截断', prompt)
            self.assertIn('process_error_notebook_attachments', prompt)
            self.assertIn('transcribe_error_notebook_attachments', prompt)
            self.assertIn('grading_strategy', prompt)
            self.assertIn('verified_reference', prompt)
            self.assertIn('recheck_error_notebook_reference_conflict', prompt)
            self.assertIn('lookup_question_bank_reference', prompt)
            self.assertIn('inspect_math_notebook', prompt)
            self.assertIn('reflow_practice_pdf', prompt)
            self.assertIn('不得把题库编号当作 question_text', prompt)
            self.assertIn('adjudicate_error_notebook_reference_conflicts', prompt)
            self.assertIn('以该题库答案与解析为准', prompt)
            self.assertIn('authoritative_grade', prompt)
            self.assertIn('印刷题干与手写作答必须分区识别', prompt)
            self.assertIn('要求“一个取值”', prompt)
            self.assertIn('receipt_message', prompt)
            self.assertNotIn('未收到判题流程返回', prompt)
            self.assertIn('最终答案', prompt)
            self.assertIn('*（小建议：……）*', prompt)
            self.assertIn('“## 下一步”', prompt)
            self.assertIn('只给出一个最优先', prompt)
            self.assertIn('attachment_index', prompt)
            self.assertIn('每张图片的 item_no 从 1 连续编号', prompt)
            self.assertIn('review_item.recommended_action', prompt)
            self.assertNotIn('最终答案及小建议', prompt)
        self.assertIn('defer_math_review', preset)
        self.assertIn('resume_math_review', preset)
        self.assertIn("apiKeyEnv: !!js process.env.HARNESS_API_KEY_ENV ?? 'DASHSCOPE_API_KEY'", runtime_config)
        self.assertIn("model: !!js process.env.HARNESS_MODEL ?? 'qwen3.8-flash'", runtime_config)
        for model_config in (runtime_config, patch):
            self.assertIn("cacheRetention: short", model_config)
            self.assertIn("cacheControlFormat: anthropic", model_config)
        for model_config in (runtime_config, patch):
            self.assertIn("process.env.HARNESS_CONTEXT_WINDOW ?? '1000000'", model_config)
            self.assertIn("process.env.HARNESS_MAX_TOKENS ?? '32768'", model_config)
            self.assertIn("defaultContextWindow: !!js Number.parseInt(process.env.HARNESS_CONTEXT_WINDOW ?? '1000000', 10)", model_config)
            self.assertIn("defaultMaxTokens: !!js Number.parseInt(process.env.HARNESS_MAX_TOKENS ?? '32768', 10)", model_config)
        for compaction_config in (runtime_config, preset):
            self.assertIn("thresholdRatio: 0.25", compaction_config)
            self.assertIn("retainRatio: 0.08", compaction_config)
            self.assertIn("maxTokens: 4096", compaction_config)
            self.assertIn("compactionRetries: 1", compaction_config)
            self.assertIn("maxOverflowRetries: 1", compaction_config)
        self.assertIn("name: './notebook-jsonrpc-server.mjs'", runtime_config)
        self.assertIn("@deepseek-ai/dsh-compaction-tool-result-pruner", runtime_config)
        self.assertIn("requestImagePixelBudget: 4194304", runtime_config)
        self.assertIn("requestImageMaxBytes: 4194304", runtime_config)
        self.assertRegex(patch, r"- id: llm-pi-ai\s+config:\s+providers:\s+notebook-provider:")
        self.assertIn("apiKeyEnv: !!js process.env.HARNESS_API_KEY_ENV ?? 'DASHSCOPE_API_KEY'", patch)
        self.assertRegex(patch, r"- id: agent-default-model\s+config:\s+provider: notebook-provider")
        self.assertEqual(patch.count("- id: agent-default-model\n"), 1)
        self.assertEqual(patch.count("- id: llm-pi-ai\n"), 1)

    def test_downloads_escape_the_nested_harness_frame(self) -> None:
        script = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('setAttribute("download", "")', script)
        self.assertNotRegex(script, r'<a[^>]+\sdownload(?:[\s=>])')
        self.assertGreaterEqual(script.count('target="_top"'), 4)
        self.assertGreaterEqual(script.count('link.target = "_top"'), 2)

    def test_navigation_status_uses_view_and_completion_clear_rules(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is needed for frontend behavior checks")
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('extensions/dsh-math-notebook-ui/lib/client.js', 'utf8');
const indicators = new Map(), buttons = ['/errors','/practice','/progress'].map(path => ({dataset:{lzlmProductPath:path},
  title:'', attrs:{}, setAttribute(k,v){this.attrs[k]=v;}, removeAttribute(k){delete this.attrs[k];},
  querySelector(){if(!indicators.has(path)) indicators.set(path,{hidden:true,dataset:{}}); return indicators.get(path);}}));
const storage = new Map();
let payload = {scope:'account-a',errors:{count:1,revision:'errors-1'},practice:{count:1,revision:'pdf-1'},
  progress:{due_count:2,needs_correction_count:1}};
const context = {productOrigin:'http://127.0.0.1:8000', navigationItems:[
  {path:'/errors',label:'错题本'},{path:'/practice',label:'练习 PDF'},{path:'/progress',label:'学习进度'}],
  document:{hidden:false,querySelectorAll:()=>buttons,addEventListener(){},removeEventListener(){}},
  window:{addEventListener(){},removeEventListener(){}}, console, setInterval, clearInterval,
  localStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v)},
  fetch:async()=>({ok:true,json:async()=>payload})};
const start = source.indexOf('    let activeProductPath');
const end = source.indexOf('    function closeProductSurface');
vm.createContext(context); vm.runInContext(source.slice(start,end),context);
(async()=>{
  await context.refreshProductNavigationStatus();
  assert.equal(indicators.get('/errors').hidden,false);
  assert.equal(indicators.get('/practice').hidden,false);
  assert.equal(indicators.get('/progress').hidden,false);
  assert.equal(indicators.get('/progress').dataset.kind,'pending');
  context.markProductNavigationSeen('/errors');
  assert.equal(indicators.get('/errors').hidden,true);
  await context.refreshProductNavigationStatus();
  assert.equal(indicators.get('/errors').hidden,true);
  payload={...payload,errors:{count:1,revision:'errors-2'}};
  await context.refreshProductNavigationStatus();
  assert.equal(indicators.get('/errors').hidden,false);
  context.markProductNavigationSeen('/progress');
  assert.equal(indicators.get('/progress').hidden,false);
  payload={...payload,progress:{due_count:0,needs_correction_count:0}};
  await context.refreshProductNavigationStatus();
  assert.equal(indicators.get('/progress').hidden,true);
  payload={...payload,scope:'account-b'};
  await context.refreshProductNavigationStatus();
  assert.equal(indicators.get('/practice').hidden,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_harness_product_views_hide_the_legacy_sidebar(self) -> None:
        script = (WEB / "app.js").read_text(encoding="utf-8")
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn('new URLSearchParams(location.search).get("embedded") === "1"', script)
        self.assertIn("body.is-embedded .sidebar", css)
        self.assertIn("body.is-embedded main {", css)
        self.assertIn("margin-left: 0;", css)

    def test_harness_product_views_share_the_harness_visual_language(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn("body.is-embedded {", css)
        self.assertIn("--ink: #0f1115;", css)
        self.assertIn("body.is-embedded .page-header", css)
        self.assertIn("body.is-embedded .panel {", css)
        self.assertIn("body.is-embedded button {", css)
        self.assertIn("body.is-embedded .stats div {", css)

    def test_brand_and_learning_contract_survive_harness_adoption(self) -> None:
        html = "".join(path.read_text(encoding="utf-8") for path in WEB.glob("*.html"))
        script = (WEB / "app.js").read_text(encoding="utf-8")
        for removed in ("昵称", "年级", "出生日期", "监护人", "家庭"):
            self.assertNotIn(removed, html + script)
        self.assertIn("next_action", (ROOT / "openapi" / "web-v1.json").read_text(encoding="utf-8"))
        self.assertIn("练习 PDF", html)
        self.assertIn("/v1/reviews/today", script)
        self.assertIn("/v1/practice-pdfs", script)
        self.assertIn('id="practice-pdf-history"', html)
        self.assertIn('item.source === "desktop_skill"', script)
        self.assertIn('Skill 历史文件', script)
        self.assertIn('item.source === "generated"', script)
        self.assertIn('每日复习练习-${dateParts.year}年', script)
        self.assertIn("已生成的 PDF", html)
        self.assertNotIn("生成新练习", html)
        self.assertNotIn('id="practice-errors"', html)
        self.assertNotIn('id="create-pdf"', html)
        self.assertIn("/chat-turn", script)
        self.assertIn("await commitCurrent()", script)
        self.assertIn('localStorage.getItem("lzlm-device-id")', script)
        self.assertIn('"X-Device-ID": deviceId', script)
        for heading in ("题目整理", "学生作答还原", "错因分析与点评", "知识点梳理", "详细解析", "最终答案", "错题本记录检查"):
            self.assertIn(heading, script)
        self.assertIn('（小建议：${diagnosis.prevention_cue}）', script)

    def test_daily_pdf_button_locks_before_first_await(self) -> None:
        script = (WEB / "app.js").read_text(encoding="utf-8")
        handler = script.split('$("#generate-review-pdf").addEventListener("click", async event => {', 1)[1].split("  const refreshWhenVisible", 1)[0]
        self.assertLess(handler.index("generatingPdf = true;"), handler.index("await loadDashboard()"))
        self.assertIn("const button = event.currentTarget;", handler)
        self.assertIn("button.disabled = true;", handler)
        self.assertNotIn("event.currentTarget.disabled", handler)
        self.assertNotIn('6. 最终答案及小建议', script)

    def test_mobile_layout_and_keyboard_focus_are_defined_for_product_pages(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:720px)", css)
        self.assertIn(":focus-visible", css)
        self.assertNotIn("min-width:720px", css)
        self.assertIn(".sidebar nav a:last-child { margin-top: auto; }", css)

    def test_logout_only_appears_in_settings(self) -> None:
        settings = (WEB / "settings.html").read_text(encoding="utf-8")
        self.assertIn('id="logout"', settings)
        self.assertIn("退出当前账号", settings)
        self.assertIn('id="logout-all"', settings)
        for filename in ("index.html", "errors.html", "practice.html", "progress.html"):
            self.assertNotIn('id="logout"', (WEB / filename).read_text(encoding="utf-8"))

    def test_login_and_register_are_separate_documents(self) -> None:
        login = (WEB / "login.html").read_text(encoding="utf-8")
        register = (WEB / "register.html").read_text(encoding="utf-8")
        auth_script = (WEB / "auth.js").read_text(encoding="utf-8")
        self.assertIn('data-auth-mode="login"', login)
        self.assertIn('data-auth-mode="register"', register)
        self.assertNotIn('id="password"', login)
        self.assertIn('id="password" type="password"', register)
        for html in (login, register):
            self.assertIn('<form id="code-form">', html)
            self.assertIn('id="captcha-token"', html)
            self.assertIn('id="code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required disabled', html)
            self.assertIn('id="otp-button" disabled', html)
            self.assertIn('id="auth-submit" disabled', html)
            self.assertNotIn('href="#', html)
        self.assertIn('minlength="8" maxlength="20"', register)
        self.assertIn('/[A-Za-z]/.test(value)', auth_script)
        self.assertIn('/[0-9]/.test(value)', auth_script)
        self.assertIn("captcha_required", auth_script)
        self.assertIn("error.retryAfter", auth_script)
        self.assertIn("countdown > 0", auth_script)
        self.assertIn('$("#code").value = localTestCode;', auth_script)
        self.assertIn("仅限本地测试：模拟验证码已自动填入。", auth_script)
        self.assertIn('!validCode() || !$("#agreement").checked || authSubmitting', auth_script)
        self.assertNotIn('!passwordIsValid || !$("#agreement").checked', auth_script)
        self.assertIn('location.replace("/")', auth_script)

    def test_visual_tokens_remain_stable(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")
        for token in ("#002060", "#F6F5F1", "#182230", "#586474", "#D9DEE7"):
            self.assertIn(token, css)


if __name__ == "__main__":
    unittest.main()
