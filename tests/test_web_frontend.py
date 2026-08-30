import json
from pathlib import Path
import unittest
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class FrontendContractTests(unittest.TestCase):
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
    addEventListener(event, fn) { this.handlers[event] = fn; }, scrollIntoView() {}, querySelectorAll() {return [];} });
  return nodes.get(id);
}
const errors = Array.from({length:13}, (_,i) => ({error_id:String(i),status:'open',question_text:'题目',first_error:'错因',
  diagnosis:{knowledge_points:['知识点']}, review:{due_at:'2026-07-01T00:00:00Z',status:'pending',stage:2}}));
const history = {days:[{date:'2026-08-24',items:[{type:'due',error_id:'historic',stage:1}],due_review_count:1}],
  summary:{due_review_count:1},total_error_count:13};
const before = JSON.stringify(history);
const context = {Intl, $, Date:class extends Date {constructor(...args) {super(...(args.length ? args : ['2026-08-30T16:30:00Z']));}},
  sessionStorage:{getItem:k=>storage.get(k)??null, setItem:(k,v)=>storage.set(k,v)},
  escapeHtml:s=>String(s??''), renderMath:()=>{}, authError:e=>String(e), status:(node,text,error=false)=>{node.textContent=text;node.error=error;},
  api:async path=>path==='/v1/errors' ? {items:errors,selection_scope:'a'.repeat(24)} : path==='/v1/progress' ? {today_completed_review_count:0} : history};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function chinaDate('), source.indexOf('function bindPractice(')), context);
const tick = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
  context.bindProgress(); await tick();
  assert.equal($('#progress-status').textContent,'数据已更新。');
  const grid = $('#review-calendar').innerHTML;
  assert.ok(grid.includes('今日到期<strong>0</strong>'));
  assert.ok(grid.includes('历史逾期<strong>13</strong>'));
  assert.ok(grid.includes('今日已完成<strong>0</strong>'));
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
  await $('#refresh-progress').handlers.click();
  assert.equal(($('#calendar-day-items').innerHTML.match(/ checked/g)||[]).length,12);
  assert.equal(JSON.stringify(history),before);
  context.sessionStorage.setItem=()=>{throw new Error('blocked');}; change('12',false);
  assert.equal($('#calendar-selection-status').error,true);
  assert.ok($('#calendar-selection-status').textContent.includes('无法保存选题'));
  // Historical details use the server's day-end snapshot, not today's list.
  const recorded = id => ({type:'due',error_id:id,stage:2,question_text:'题目',first_error:'错因',knowledge_points:['知识点'],original_due_date:'2026-07-01'});
  history.backlog_items = [recorded('0'),recorded('finished')];
  history.days[0].backlog_indices = [0,1];
  history.days[0].history_complete = false;
  await $('#refresh-progress').handlers.click();
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
  // Move to a future month: only real pending plans, with no completion/overdue prediction.
  errors.push({error_id:'future',status:'open',review:{stage:3,status:'pending',due_at:'2026-09-02T00:00:00Z'}});
  history.days = [{date:'2026-09-02',items:[{...recorded('future'),stage:3,original_due_date:'2026-09-02'}],stage_counts:{'3':1}}];
  $('#calendar-next').handlers.click(); await tick();
  const futureGrid = $('#review-calendar').innerHTML;
  assert.ok(futureGrid.includes('计划复习<strong>1</strong>'));
  assert.ok(futureGrid.includes('第3阶段×1'));
  assert.ok(!futureGrid.includes('当日未完成') && !futureGrid.includes('当日已完成'));
  clickDay('2026-09-02','due');
  assert.equal(($('#calendar-day-items').innerHTML.match(/name="calendar-error"/g)||[]).length,1);
  assert.ok($('#calendar-history-note').textContent.includes('选题不改变原定复习日期'));
  $('#calendar-day-close').handlers.click();
  assert.equal($('#calendar-day-detail').hidden,true);
  $('#calendar-prev').handlers.click(); await tick();
  assert.equal($('#calendar-month').textContent,'2026 年 8 月');
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

    def test_operations_dashboard_is_separate_read_only_and_responsive(self) -> None:
        html = (WEB / "admin.html").read_text(encoding="utf-8")
        script = (WEB / "admin.js").read_text(encoding="utf-8")
        style = (WEB / "app.css").read_text(encoding="utf-8")
        for text in ("后台管理", "用户管理", "用户行为分析", "模型 Token 消耗", "失败与等待任务", "候选题与待复核内容", "短信与风控", "注销工单", "后台访问审计"):
            self.assertIn(text, html)
        self.assertIn('fetch("/v1/admin/dashboard?limit=50"', script)
        self.assertIn("本视图不支持按手机号查询", html)
        self.assertNotIn('href="/errors"', html)
        self.assertNotIn("修改判题", html)
        self.assertIn(".admin-metrics", style)
        self.assertIn(".admin-table-wrap", style)
        self.assertIn('data-label="状态"', script)
        self.assertIn('data-label="题库内容"', script)
        self.assertIn('unreviewed: "待复核"', script)
        self.assertIn("/v1/harness/sessions/usage", (ROOT / "extensions" / "dsh-math-notebook-ui" / "lib" / "client.js").read_text(encoding="utf-8"))
        self.assertIn('data-label="Token"', script)
        self.assertIn("content: attr(data-label)", style)

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
        for text in ("今日的复习计划", "全部错题"):
            self.assertIn(text, html)
        for text in ("六阶段复习规则", "主动提取", "间隔效应", "即时反馈", "各复习阶段"):
            self.assertNotIn(text, html)
        for marker in ("generate-review-pdf", "selected-error-count"):
            self.assertIn(f'id="{marker}"', html)
        self.assertNotIn('id="today-review-items"', html)
        self.assertNotIn("renderDueReviews", script)
        for contract in ('api("/v1/errors")', 'api("/v1/reviews/today")', 'api("/v1/progress")', 'api("/v1/practice-pdfs"'):
            self.assertIn(contract, script)
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
        for text in ("六阶段复习规则", "主动提取", "间隔效应", "即时反馈", "错题与复习日历", "新增错题", "应复习", "需改错", "逾期", "复习正确率"):
            self.assertIn(text, html)
        self.assertNotIn("各复习阶段", html)
        for marker in ("review-calendar", "calendar-month", "calendar-prev", "calendar-next", "calendar-summary", "calendar-stats", "calendar-filters", "calendar-day-detail", "calendar-day-items", "refresh-progress"):
            self.assertIn(f'id="{marker}"', html)
        self.assertNotIn('id="stage-count-1"', html)
        self.assertIn('function bindProgress()', script)
        self.assertIn('api(`/v1/progress/calendar?month=${monthKey()}`)', script)
        self.assertIn('data-calendar-filter', html)
        self.assertIn('data-calendar-date', script)
        self.assertIn('knowledge_points', script)

    def test_workbench_is_the_official_deepseek_harness_surface(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        plugin = (ROOT / "extensions" / "dsh-math-notebook-ui" / "lib" / "client.js").read_text(encoding="utf-8")
        host_plugin = (ROOT / "extensions" / "dsh-math-notebook-ui" / "lib" / "index.js").read_text(encoding="utf-8")
        patch = (ROOT / "config" / "deepseek-harness" / "web-product.patch.yml").read_text(encoding="utf-8")
        runtime_config = (ROOT / "config" / "deepseek-harness" / "cordis.yml").read_text(encoding="utf-8")
        preset = (ROOT / "config" / "deepseek-harness" / "agent-presets" / "math-notebook" / "agent.cordis.yml").read_text(encoding="utf-8")
        dependencies = package["dependencies"]
        self.assertEqual(dependencies["@deepseek-ai/dsh"], "0.1.1-rc.2")
        self.assertEqual(dependencies["@deepseek-ai/dsh-web-frontend"], "0.1.1-rc.2")
        self.assertEqual(dependencies["@lizhaolin/dsh-math-notebook-ui"], "file:extensions/dsh-math-notebook-ui")
        self.assertIn('data-page="harness-workbench"', html)
        self.assertIn('id="harness-frame"', html)
        self.assertIn('allow="clipboard-read; clipboard-write"', html)
        self.assertIn('fetch("/v1/session"', html)
        self.assertIn('location.replace("/login")', html)
        self.assertIn('frame.src = "http://127.0.0.1:3080/"', html)
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
        self.assertIn('element.textContent === "探索未至之境"', plugin)
        self.assertIn('element.textContent = "今天要整理哪道错题?"', plugin)
        self.assertIn('textarea[placeholder="描述你想要构建的内容"]', plugin)
        self.assertIn('element.placeholder = "学习不是熊瞎子掰棒子"', plugin)
        self.assertIn('customizeStudentCopy(ctx)', plugin)
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
        for plugin_id in ("ui-settings-models", "ui-model-selection", "ui-settings-plugin-inventory", "ui-settings-plugins"):
            self.assertRegex(patch, rf"- id: {plugin_id}\s+disabled: true")
        self.assertRegex(patch, r"- id: attachment-local\s+config:\s+maxImagesPerMessage: 1")
        self.assertRegex(runtime_config, r"- id: attachment-local\s+name: '@deepseek-ai/dsh-attachment-local'\s+config:\s+dshHome: .*\s+maxImagesPerMessage: 1")
        self.assertIn('ctx.workspaceRegistry.create(workspacePath, "错题会话")', host_plugin)
        self.assertIn('LZLM_HARNESS_WORKSPACE_ROOT', host_plugin)
        self.assertIn('name: "confirm_error_notebook_entry"', host_plugin)
        self.assertIn('name: "process_error_notebook_attachments"', host_plugin)
        self.assertIn('name: "recheck_error_notebook_reference_conflict"', host_plugin)
        self.assertIn('name: "adjudicate_error_notebook_reference_conflicts"', host_plugin)
        self.assertIn('/v1/internal/harness/reference-conflicts/recheck', host_plugin)
        self.assertIn('/v1/internal/harness/reference-conflicts/adjudicate', host_plugin)
        self.assertNotIn('anyOf:', host_plugin)
        self.assertIn('ctx.attachments.readImage', host_plugin)
        self.assertIn('/v1/internal/harness/intakes/process', host_plugin)
        self.assertIn('latestUserImages(exec.agent)', host_plugin)
        self.assertIn('if (images.length > 1)', host_plugin)
        self.assertIn('一条消息最多上传 1 张图片', host_plugin)
        self.assertIn('exec.concludeTurn()', host_plugin)
        self.assertIn('/v1/internal/harness/grade-results/', host_plugin)
        self.assertIn('/v1/harness/sessions/bind', plugin)
        self.assertIn('credentials: "include"', plugin)
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
        self.assertIn("confirm_error_notebook_entry", preset)
        for prompt in (patch, preset, runtime_config):
            self.assertIn('process_error_notebook_attachments', prompt)
            self.assertIn('recheck_error_notebook_reference_conflict', prompt)
            self.assertIn('adjudicate_error_notebook_reference_conflicts', prompt)
            self.assertIn('receipt_message', prompt)
            self.assertNotIn('未收到判题流程返回', prompt)
            self.assertIn('最终答案', prompt)
            self.assertIn('*（小建议：……）*', prompt)
            self.assertIn('“## 下一步”', prompt)
            self.assertIn('只给出一个最优先', prompt)
            self.assertIn('固定为 1 的 attachment_index', prompt)
            self.assertNotIn('最终答案及小建议', prompt)

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
