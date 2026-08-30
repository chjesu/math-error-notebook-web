const $ = selector => document.querySelector(selector);
const roleLabels = {operations: "运营", reviewer: "内容审核", security: "安全审计", administrator: "管理员"};
const statusLabels = {
  failed_retryable: "可重试失败", failed_final: "最终失败", waiting_confirmation: "等待确认",
  candidate: "候选", verified: "已验证", rejected: "已拒绝", retired: "已停用",
  needs_review: "待复核", unreviewed: "待复核", pending: "处理中", completed: "已完成",
  active: "正常", restricted: "受限", pending_delete: "待注销", deleted: "已注销"
};

function escapeHtml(value) {
  const span = document.createElement("span");
  span.textContent = value ?? "";
  return span.innerHTML;
}

function label(value) { return statusLabels[value] || value || "—"; }
function time(value) { return value ? new Date(value).toLocaleString("zh-CN", {hour12: false}) : "—"; }
function compactId(value) { return value && value.length > 14 ? `${value.slice(0, 7)}…${value.slice(-5)}` : value || "—"; }
function number(value) { return Number(value || 0).toLocaleString("zh-CN"); }
function emptyRow(columns, message = "当前没有待处理项") { return `<tr><td colspan="${columns}" class="admin-empty">${message}</td></tr>`; }

async function loadDashboard() {
  const button = $("#refresh-admin");
  button.disabled = true;
  try {
    const response = await fetch("/v1/admin/dashboard?limit=50", {credentials: "same-origin", headers: {Accept: "application/json"}});
    if (response.status === 401) return location.replace("/login");
    if (response.status === 403) throw new Error("forbidden");
    if (!response.ok) throw new Error("unavailable");
    renderDashboard(await response.json());
    $("#admin-status").textContent = "数据已更新；本次访问已记录审计。";
    $("#admin-status").classList.remove("error");
  } catch (error) {
    $("#admin-status").textContent = error.message === "forbidden" ? "当前账号没有运营后台权限。" : "运营数据暂时无法加载，请稍后重试。";
    $("#admin-status").classList.add("error");
  } finally {
    button.disabled = false;
  }
}

function renderDashboard(result) {
  const sections = result.sections || {};
  const allowed = new Set(result.operator?.sections || []);
  $("#operator-role").textContent = roleLabels[result.operator?.role] || "运营角色";
  $("#generated-at").textContent = `更新于 ${time(result.generated_at)}`;
  document.querySelectorAll("[data-admin-section]").forEach(element => { element.hidden = !allowed.has(element.dataset.adminSection); });
  document.querySelectorAll("[data-section-link]").forEach(element => { element.hidden = !allowed.has(element.dataset.sectionLink); });
  const overview = sections.overview || {};
  $("#admin-metrics").innerHTML = [["活跃账号", overview.active_users || 0], ["需关注任务", overview.attention_tasks || 0], ["候选题", overview.candidate_questions || 0], ["待处理隐私工单", overview.pending_privacy_cases || 0]].map(([name, value]) => `<article><strong>${number(value)}</strong><span>${name}</span></article>`).join("");
  if (allowed.has("users")) $("#admin-users").innerHTML = sections.users?.length ? sections.users.map(item => `<tr><td data-label="用户">${escapeHtml(item.user_ref)}</td><td data-label="状态"><span class="state-pill">${escapeHtml(label(item.status))}</span></td><td data-label="注册时间">${escapeHtml(time(item.created_at))}</td><td data-label="最近活跃">${escapeHtml(time(item.last_active_at))}</td><td data-label="有效会话">${number(item.active_sessions)}</td><td data-label="错题">${number(item.error_count)}</td><td data-label="复习">${number(item.review_count)}</td><td data-label="PDF">${number(item.pdf_count)}</td><td data-label="Token">${number(item.total_tokens)}</td></tr>`).join("") : emptyRow(9, "当前没有用户记录");
  if (allowed.has("behavior")) {
    const behavior = sections.behavior || {totals: {}, daily: []};
    const totals = behavior.totals || {};
    $("#behavior-metrics").innerHTML = [["注册", totals.registrations], ["上传", totals.uploads], ["判题", totals.grades], ["新增错题", totals.errors_added], ["完成复习", totals.reviews_completed], ["生成 PDF", totals.pdfs_generated]].map(([name, value]) => `<article><strong>${number(value)}</strong><span>${name}</span></article>`).join("");
    $("#admin-behavior").innerHTML = behavior.daily?.length ? behavior.daily.map(item => `<tr><td data-label="日期">${escapeHtml(item.date)}</td><td data-label="活跃用户">${number(item.active_users)}</td><td data-label="注册">${number(item.registrations)}</td><td data-label="上传">${number(item.uploads)}</td><td data-label="识别题目">${number(item.intakes)}</td><td data-label="判题">${number(item.grades)}</td><td data-label="新增错题">${number(item.errors_added)}</td><td data-label="完成复习">${number(item.reviews_completed)}</td><td data-label="生成 PDF">${number(item.pdfs_generated)}</td></tr>`).join("") : emptyRow(9, "最近 7 天没有业务行为记录");
  }
  if (allowed.has("usage")) {
    const usage = sections.usage || {summary: {}, users: []};
    const summary = usage.summary || {};
    $("#usage-metrics").innerHTML = [["计量用户", summary.user_count], ["模型会话", summary.session_count], ["非缓存输入", summary.uncached_input_tokens], ["输出", summary.output_tokens], ["缓存读取", summary.cache_read_tokens], ["Token 合计", summary.total_tokens]].map(([name, value]) => `<article><strong>${number(value)}</strong><span>${name}</span></article>`).join("");
    $("#admin-usage").innerHTML = usage.users?.length ? usage.users.map(item => `<tr><td data-label="用户">${escapeHtml(item.user_ref)}</td><td data-label="会话">${number(item.session_count)}</td><td data-label="非缓存输入">${number(item.uncached_input_tokens)}</td><td data-label="输出">${number(item.output_tokens)}</td><td data-label="缓存读取">${number(item.cache_read_tokens)}</td><td data-label="缓存写入">${number(item.cache_write_tokens)}</td><td data-label="合计">${number(item.total_tokens)}</td><td data-label="更新时间">${escapeHtml(time(item.updated_at))}</td></tr>`).join("") : emptyRow(8, "尚未收到 Harness Token 计量");
  }
  if (allowed.has("tasks")) $("#admin-tasks").innerHTML = sections.tasks?.length ? sections.tasks.map(item => `<tr><td data-label="任务">${escapeHtml(compactId(item.task_id))}</td><td data-label="用户">${escapeHtml(item.user_ref)}</td><td data-label="类型">${escapeHtml(item.type)}</td><td data-label="状态"><span class="state-pill">${escapeHtml(label(item.status))}</span></td><td data-label="错误分类">${escapeHtml(item.error_code || "—")}</td><td data-label="更新时间">${escapeHtml(time(item.updated_at))}</td></tr>`).join("") : emptyRow(6);
  if (allowed.has("content")) $("#admin-content").innerHTML = sections.content?.length ? sections.content.map(item => `<tr><td data-label="题目">${escapeHtml(compactId(item.question_id))}</td><td data-label="状态"><span class="state-pill">${escapeHtml(label(item.status))}</span></td><td data-label="题库内容">${escapeHtml(label(item.verification))}</td><td data-label="版本">v${item.version}</td><td data-label="授权">${escapeHtml(item.license)}</td><td data-label="更新时间">${escapeHtml(time(item.updated_at))}</td></tr>`).join("") : emptyRow(6);
  if (allowed.has("risk")) {
    const risk = sections.risk || {};
    $("#risk-metrics").innerHTML = [["验证码申请", risk.sms_requested_today || 0], ["发送成功", risk.sms_sent_today || 0], ["发送失败", risk.sms_failed_today || 0], ["触发限流", risk.rate_limited_today || 0]].map(([name, value]) => `<article><strong>${number(value)}</strong><span>${name}</span></article>`).join("");
  }
  if (allowed.has("privacy")) $("#admin-privacy").innerHTML = sections.privacy?.length ? sections.privacy.map(item => `<tr><td data-label="用户">${escapeHtml(item.user_ref)}</td><td data-label="状态"><span class="state-pill">${escapeHtml(label(item.status))}</span></td><td data-label="申请时间">${escapeHtml(time(item.requested_at))}</td><td data-label="更新时间">${escapeHtml(time(item.updated_at))}</td><td data-label="错误分类">${escapeHtml(item.error_code || "—")}</td></tr>`).join("") : emptyRow(5);
  if (allowed.has("audit")) $("#admin-audit").innerHTML = sections.audit?.length ? sections.audit.map(item => `<tr><td data-label="操作者">${escapeHtml(item.operator_ref)}</td><td data-label="角色">${escapeHtml(roleLabels[item.role] || item.role)}</td><td data-label="事件">${escapeHtml(item.event)}</td><td data-label="时间">${escapeHtml(time(item.occurred_at))}</td></tr>`).join("") : emptyRow(4, "还没有后台访问记录");
}

$("#refresh-admin").addEventListener("click", loadDashboard);
loadDashboard();
