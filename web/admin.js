const $ = selector => document.querySelector(selector);
const roleLabels = {operations: "运营", reviewer: "内容审核", security: "安全审计", administrator: "管理员"};
const statusLabels = {
  failed_retryable: "可重试失败", failed_final: "最终失败", waiting_confirmation: "等待确认",
  candidate: "候选", verified: "已验证", rejected: "已拒绝", retired: "已停用",
  needs_review: "待复核", unreviewed: "未复核", pending: "处理中", completed: "已完成"
};

function escapeHtml(value) {
  const span = document.createElement("span");
  span.textContent = value ?? "";
  return span.innerHTML;
}

function label(value) { return statusLabels[value] || value || "—"; }
function time(value) { return value ? new Date(value).toLocaleString("zh-CN", {hour12: false}) : "—"; }
function compactId(value) { return value && value.length > 14 ? `${value.slice(0, 7)}…${value.slice(-5)}` : value || "—"; }
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
  $("#admin-metrics").innerHTML = [["活跃账号", overview.active_users || 0], ["需关注任务", overview.attention_tasks || 0], ["候选题", overview.candidate_questions || 0], ["待处理隐私工单", overview.pending_privacy_cases || 0]].map(([name, value]) => `<article><strong>${value}</strong><span>${name}</span></article>`).join("");
  if (allowed.has("tasks")) $("#admin-tasks").innerHTML = sections.tasks?.length ? sections.tasks.map(item => `<tr><td data-label="任务">${escapeHtml(compactId(item.task_id))}</td><td data-label="用户">${escapeHtml(item.user_ref)}</td><td data-label="类型">${escapeHtml(item.type)}</td><td data-label="状态"><span class="state-pill">${escapeHtml(label(item.status))}</span></td><td data-label="错误分类">${escapeHtml(item.error_code || "—")}</td><td data-label="更新时间">${escapeHtml(time(item.updated_at))}</td></tr>`).join("") : emptyRow(6);
  if (allowed.has("content")) $("#admin-content").innerHTML = sections.content?.length ? sections.content.map(item => `<tr><td data-label="题目">${escapeHtml(compactId(item.question_id))}</td><td data-label="状态"><span class="state-pill">${escapeHtml(label(item.status))}</span></td><td data-label="验证">${escapeHtml(label(item.verification))}</td><td data-label="版本">v${item.version}</td><td data-label="授权">${escapeHtml(item.license)}</td><td data-label="更新时间">${escapeHtml(time(item.updated_at))}</td></tr>`).join("") : emptyRow(6);
  if (allowed.has("risk")) {
    const risk = sections.risk || {};
    $("#risk-metrics").innerHTML = [["验证码申请", risk.sms_requested_today || 0], ["发送成功", risk.sms_sent_today || 0], ["发送失败", risk.sms_failed_today || 0], ["触发限流", risk.rate_limited_today || 0]].map(([name, value]) => `<article><strong>${value}</strong><span>${name}</span></article>`).join("");
  }
  if (allowed.has("privacy")) $("#admin-privacy").innerHTML = sections.privacy?.length ? sections.privacy.map(item => `<tr><td data-label="用户">${escapeHtml(item.user_ref)}</td><td data-label="状态"><span class="state-pill">${escapeHtml(label(item.status))}</span></td><td data-label="申请时间">${escapeHtml(time(item.requested_at))}</td><td data-label="更新时间">${escapeHtml(time(item.updated_at))}</td><td data-label="错误分类">${escapeHtml(item.error_code || "—")}</td></tr>`).join("") : emptyRow(5);
  if (allowed.has("audit")) $("#admin-audit").innerHTML = sections.audit?.length ? sections.audit.map(item => `<tr><td data-label="操作者">${escapeHtml(item.operator_ref)}</td><td data-label="角色">${escapeHtml(roleLabels[item.role] || item.role)}</td><td data-label="事件">${escapeHtml(item.event)}</td><td data-label="时间">${escapeHtml(time(item.occurred_at))}</td></tr>`).join("") : emptyRow(4, "还没有后台访问记录");
}

$("#refresh-admin").addEventListener("click", loadDashboard);
loadDashboard();
