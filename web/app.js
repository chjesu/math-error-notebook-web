const $ = selector => document.querySelector(selector);
const page = document.body.dataset.page;
const deviceId = (() => {
  try {
    const existing = localStorage.getItem("lzlm-device-id");
    if (existing) return existing;
    const created = crypto.randomUUID();
    localStorage.setItem("lzlm-device-id", created);
    return created;
  } catch {
    return crypto.randomUUID();
  }
})();

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      ...options,
      headers: {
        ...(options.body instanceof FormData ? {} : {"Content-Type": "application/json"}),
        "X-Device-ID": deviceId,
        ...(options.headers || {})
      }
    });
  } catch {
    const error = new Error("network_error");
    error.status = 0;
    throw error;
  }
  if (response.status === 204) return null;
  const value = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(value.error?.code || "temporarily_unavailable");
    error.status = response.status;
    throw error;
  }
  return value;
}

function authError(error) {
  if (error.status === 401) return "登录已失效，请重新登录。";
  return ({
    invalid_request: "请检查填写内容。",
    rate_limited: "操作过于频繁，请稍后重试。",
    export_expired: "导出已过期，请重新申请。",
    confirmation_required: "请输入正确的注销确认。",
    network_error: "网络异常，请检查网络后重试。"
  })[error.message] || "操作失败，请稍后重试。";
}

function status(target, message, error = false) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle("error", error);
}

function escapeHtml(value) {
  const span = document.createElement("span");
  span.textContent = value ?? "";
  return span.innerHTML;
}

async function requireSession() {
  try {
    await api("/v1/session");
    return true;
  } catch (error) {
    if (error.status === 401) location.replace("/login");
    else status($("#page-status"), authError(error), true);
    return false;
  }
}

$("#logout").addEventListener("click", async () => {
  try { await api("/v1/session", {method: "DELETE"}); } finally { location.replace("/login"); }
});

function bindWorkbench() {
  let activeIntake = null;
  let activeAttempt = null;
  let activeCandidate = null;

  async function loadWorkbench() {
    try {
      const workbench = await api("/v1/workbench");
      $("#error-count").textContent = workbench.error_count;
      $("#task-count").textContent = workbench.pending_task_count;
      $("#review-count").textContent = workbench.due_review_count;
      $("#gap-count").textContent = workbench.recommendation_gap_count;
      $("#error-list").innerHTML = workbench.recent_errors.length
        ? workbench.recent_errors.map(item => `<li><strong>${escapeHtml(item.question_text)}</strong><br><small>${escapeHtml(item.first_error || "待整理错因")}</small><button class="text-button" type="button" data-recommend-error="${item.error_id}">获取已验证推荐</button></li>`).join("")
        : '<li class="empty">还没有错题，上传第一道题吧。</li>';
    } catch (error) {
      status($("#upload-status"), authError(error), true);
    }
  }

  $("#upload-form").addEventListener("submit", async event => {
    event.preventDefault();
    const file = $("#file").files[0];
    if (!file) return;
    const form = new FormData();
    form.append("purpose", "question_image");
    form.append("file", file);
    const button = event.submitter;
    button.disabled = true;
    try {
      const uploaded = await api("/v1/files", {method: "POST", body: form, headers: {"Idempotency-Key": crypto.randomUUID()}});
      const task = await api("/v1/intakes", {method: "POST", body: JSON.stringify({file_id: uploaded.file_id}), headers: {"Idempotency-Key": crypto.randomUUID()}});
      activeIntake = {intakeId: task.resource_id, inputVersion: 1};
      activeAttempt = null;
      activeCandidate = null;
      $("#manual-flow").hidden = false;
      $("#manual-intake-form").hidden = false;
      $("#manual-grade-form").hidden = true;
      $("#grade-confirm").hidden = true;
      status($("#upload-status"), "文件已保存。请手工确认题干与作答；自动识别接入后也会保留确认步骤。");
      $("#question-text").focus();
      await loadWorkbench();
    } catch (error) {
      status($("#upload-status"), `${authError(error)} 文件未丢失时可重试。`, true);
    } finally {
      button.disabled = false;
    }
  });

  $("#manual-intake-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!activeIntake) return;
    const button = event.submitter;
    button.disabled = true;
    try {
      const intake = await api(`/v1/intakes/${activeIntake.intakeId}/manual-candidate`, {method: "POST", body: JSON.stringify({question_text: $("#question-text").value, answer_text: $("#answer-text").value})});
      const confirmed = await api(`/v1/intakes/${activeIntake.intakeId}/confirm`, {method: "POST", body: JSON.stringify({input_version: intake.input_version}), headers: {"Idempotency-Key": crypto.randomUUID()}});
      activeIntake.inputVersion = intake.input_version;
      activeAttempt = confirmed.resource_id;
      $("#manual-intake-form").hidden = true;
      $("#manual-grade-form").hidden = false;
      status($("#manual-status"), "题干与作答已确认，请记录判题候选。");
      $("#verdict").focus();
    } catch (error) {
      status($("#manual-status"), `确认失败：${authError(error)}`, true);
    } finally {
      button.disabled = false;
    }
  });

  $("#verdict").addEventListener("change", event => { $("#first-error").required = ["incorrect", "partial"].includes(event.target.value); });
  $("#manual-grade-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!activeAttempt || !activeIntake) return;
    const button = event.submitter;
    button.disabled = true;
    try {
      activeCandidate = await api(`/v1/attempts/${activeAttempt}/manual-grade`, {method: "POST", body: JSON.stringify({input_version: activeIntake.inputVersion, verdict: $("#verdict").value, first_error: $("#first-error").value, evidence: $("#grade-evidence").value})});
      const canCommit = ["incorrect", "partial"].includes(activeCandidate.verdict);
      $("#grade-summary").textContent = canCommit ? `候选结果：${activeCandidate.verdict === "incorrect" ? "错误" : "部分正确"}；首错：${activeCandidate.first_error}` : activeCandidate.verdict === "correct" ? "候选结果：本题正确，不写入错题本。" : "证据不足，不能写入错题本。";
      $("#grade-confirm").hidden = false;
      $("#commit-grade").hidden = !canCommit;
      status($("#manual-status"), canCommit ? "请核对候选后确认入本。" : "已安全停止，不会写入正式错题。");
    } catch (error) {
      status($("#manual-status"), `判题记录失败：${authError(error)}`, true);
    } finally {
      button.disabled = false;
    }
  });

  $("#commit-grade").addEventListener("click", async event => {
    if (!activeCandidate) return;
    event.currentTarget.disabled = true;
    try {
      await api(`/v1/grade-results/${activeCandidate.result_id}/commit`, {method: "POST", body: JSON.stringify({input_version: activeCandidate.input_version}), headers: {"Idempotency-Key": crypto.randomUUID()}});
      status($("#upload-status"), "已写入错题本，并安排首次复习。");
      $("#manual-flow").hidden = true;
      $("#upload-form").reset();
      activeIntake = activeAttempt = activeCandidate = null;
      await loadWorkbench();
    } catch (error) {
      status($("#manual-status"), `入本失败：${authError(error)}`, true);
    } finally {
      event.currentTarget.disabled = false;
    }
  });

  $("#error-list").addEventListener("click", async event => {
    const errorId = event.target.dataset.recommendError;
    if (!errorId) return;
    event.target.disabled = true;
    try {
      const result = await api(`/v1/errors/${errorId}/recommendations`, {method: "POST", body: "{}", headers: {"Idempotency-Key": crypto.randomUUID()}});
      event.target.textContent = result.gap ? `已分配 ${result.items.length} 题，仍有缺口` : `已分配 ${result.items.length} 题`;
      await loadWorkbench();
    } catch (error) {
      event.target.textContent = `推荐失败：${authError(error)}`;
    } finally {
      event.target.disabled = false;
    }
  });

  loadWorkbench();
}

function bindErrors() {
  async function loadErrors() {
    try {
      const result = await api("/v1/errors");
      $("#all-errors").innerHTML = result.items.map(item => `<li><button class="text-button" data-error-id="${item.error_id}">${escapeHtml(item.question_text)}</button><br><small>${escapeHtml(item.first_error || "待整理错因")}</small></li>`).join("") || '<li class="empty">还没有错题。</li>';
    } catch (error) {
      status($("#page-status"), authError(error), true);
    }
  }
  $("#refresh-errors").addEventListener("click", loadErrors);
  $("#all-errors").addEventListener("click", async event => {
    const id = event.target.dataset.errorId;
    if (!id) return;
    try {
      const item = await api(`/v1/errors/${id}`);
      $("#error-detail").hidden = false;
      $("#error-detail").innerHTML = `<h2>错题详情</h2><p>${escapeHtml(item.question_text)}</p><p><strong>首个错误步骤：</strong>${escapeHtml(item.first_error || "待整理")}</p>`;
    } catch (error) {
      status($("#page-status"), authError(error), true);
    }
  });
  loadErrors();
}

function bindReviews() {
  let dueReview = null;
  async function loadReviews() {
    try {
      const result = await api("/v1/reviews/today");
      dueReview = result.items[0] || null;
      $("#review-stage").textContent = dueReview ? `第 ${dueReview.stage} 阶段` : "暂无任务";
      $("#review-question").textContent = dueReview ? "请先遮住解析，独立重做原题，再如实记录结果。" : "今天没有到期复习。";
      $("#review-actions").hidden = !dueReview;
    } catch (error) {
      status($("#review-status"), authError(error), true);
    }
  }
  $("#review-actions").addEventListener("click", async event => {
    const result = event.target.dataset.reviewResult;
    if (!result || !dueReview) return;
    event.target.disabled = true;
    try {
      const completed = await api(`/v1/reviews/${dueReview.review_id}/complete`, {method: "POST", body: JSON.stringify({result}), headers: {"Idempotency-Key": crypto.randomUUID()}});
      status($("#review-status"), completed.mastered ? "已完成全部复习阶段。" : `已记录，下次为第 ${completed.next_review.stage} 阶段。`);
      await loadReviews();
    } catch (error) {
      status($("#review-status"), authError(error), true);
    } finally {
      event.target.disabled = false;
    }
  });
  loadReviews();
}

function bindPractice() {
  function selectedErrorIds() { return [...document.querySelectorAll('[name="practice-error"]:checked')].map(input => input.value); }
  function refreshCreateButton() { $("#create-pdf").disabled = selectedErrorIds().length === 0; }
  api("/v1/errors").then(result => {
    $("#practice-errors").innerHTML = result.items.length ? result.items.map(item => `<label class="check selection-item"><input name="practice-error" type="checkbox" value="${item.error_id}"> <span>${escapeHtml(item.question_text)}</span></label>`).join("") : '<p class="empty">还没有错题，请先在工作台录入。</p>';
    $("#practice-errors").addEventListener("change", event => {
      if (event.target.name !== "practice-error") return;
      const selected = selectedErrorIds();
      if (selected.length > 12) { event.target.checked = false; status($("#pdf-status"), "一次最多选择 12 道错题。", true); }
      refreshCreateButton();
    });
  }).catch(error => status($("#pdf-status"), authError(error), true));
  $("#create-pdf").addEventListener("click", async event => {
    const button = event.currentTarget;
    const errorIds = selectedErrorIds();
    if (!errorIds.length) return;
    button.disabled = true;
    try {
      const result = await api("/v1/practice-pdfs", {method: "POST", body: JSON.stringify({error_ids: errorIds, include_answers: $("#include-answers").checked}), headers: {"Idempotency-Key": crypto.randomUUID()}});
      if (result.download_url) {
        const link = document.createElement("a");
        link.href = result.download_url;
        link.textContent = "下载练习 PDF";
        link.setAttribute("download", "");
        $("#pdf-status").replaceChildren("已生成：", link);
      } else status($("#pdf-status"), `PDF 任务已受理：${result.task_id || result.resource_id || "处理中"}，请稍后刷新查看。`);
    } catch (error) {
      status($("#pdf-status"), authError(error), true);
    } finally {
      refreshCreateButton();
    }
  });
}

function bindProgress() {
  api("/v1/progress").then(result => {
    $("#progress-errors").textContent = result.error_count || 0;
    $("#progress-reviews").textContent = result.completed_review_count || 0;
    $("#progress-due").textContent = result.due_review_count || 0;
    $("#progress-gaps").textContent = result.recommendation_gap_count || 0;
  }).catch(error => status($("#page-status"), authError(error), true));
}

function bindSettings() {
  let sensitiveChallenge = null;
  let sensitiveAction = null;
  const settingsStatus = $("#settings-status");
  $("#logout-all").addEventListener("click", async () => {
    try { await api("/v1/sessions", {method: "DELETE"}); location.replace("/login"); }
    catch (error) { status(settingsStatus, authError(error), true); }
  });
  $("#sensitive-otp").addEventListener("click", async () => {
    try {
      sensitiveAction = $("#sensitive-action").value;
      const captchaToken = $("#sensitive-captcha-token").value.trim();
      const result = await api("/v1/auth/sensitive/otp/request", {method: "POST", body: JSON.stringify(captchaToken ? {phone: $("#sensitive-phone").value, action: sensitiveAction, captcha_token: captchaToken} : {phone: $("#sensitive-phone").value, action: sensitiveAction})});
      sensitiveChallenge = result.challenge_token;
      const localTestCode = /^\d{6}$/.test(result.local_test_code || "") ? result.local_test_code : "";
      if (localTestCode) $("#sensitive-code").value = localTestCode;
      status(settingsStatus, localTestCode ? "仅限本地测试：操作验证码已自动填入。" : `用于${sensitiveAction === "export" ? "导出" : "注销"}的验证码已发送，请在 5 分钟内使用。`);
    } catch (error) {
      if (error.message === "captcha_required") { $("#sensitive-captcha-fields").hidden = false; $("#sensitive-captcha-token").focus(); }
      status(settingsStatus, authError(error), true);
    }
  });
  function sensitivePayload() { return {phone: $("#sensitive-phone").value, challenge_token: sensitiveChallenge, code: $("#sensitive-code").value}; }
  $("#sensitive-action").addEventListener("change", () => { sensitiveChallenge = null; sensitiveAction = null; $("#sensitive-code").value = ""; status(settingsStatus, "用途已改变，请重新获取验证码。"); });
  $("#export-data").addEventListener("click", async () => {
    if (sensitiveAction !== "export" || !sensitiveChallenge) return status(settingsStatus, "请先选择导出并获取对应验证码。", true);
    try {
      const job = await api("/v1/exports", {method: "POST", body: JSON.stringify(sensitivePayload()), headers: {"Idempotency-Key": crypto.randomUUID()}});
      sensitiveChallenge = sensitiveAction = null;
      $("#sensitive-code").value = "";
      const link = document.createElement("a");
      link.href = job.download_url;
      link.textContent = "下载个人数据";
      link.setAttribute("download", "");
      settingsStatus.replaceChildren("导出已生成：", link);
    } catch (error) { status(settingsStatus, authError(error), true); }
  });
  $("#delete-account").addEventListener("click", async () => {
    if (sensitiveAction !== "delete" || !sensitiveChallenge) return status(settingsStatus, "请先选择注销并获取对应验证码。", true);
    if (!confirm("注销后所有设备立即退出，账号不能恢复。确认继续？")) return;
    try {
      await api("/v1/account", {method: "DELETE", body: JSON.stringify({...sensitivePayload(), confirmation: "DELETE"})});
      location.replace("/register");
    } catch (error) { status(settingsStatus, authError(error), true); }
  });
}

async function init() {
  if (!await requireSession()) return;
  ({workbench: bindWorkbench, errors: bindErrors, reviews: bindReviews, practice: bindPractice, progress: bindProgress, settings: bindSettings})[page]();
}

init();
