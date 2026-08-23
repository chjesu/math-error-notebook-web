const $ = selector => document.querySelector(selector);
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
let challenge = null;
let phone = null;
let dueReview = null;
let recentErrorIds = [];
let activeIntake = null;
let activeAttempt = null;
let activeCandidate = null;
let authMode = "login";
let countdown = 0;
let countdownTimer = null;
let otpRequesting = false;
let authSubmitting = false;
let phoneTouched = false;
let authRevision = 0;
let sensitiveChallenge = null;
let sensitiveAction = null;

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
  if (response.headers.get("content-type")?.startsWith("application/pdf")) return response.blob();
  const value = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(value.error?.code || "temporarily_unavailable"); error.status = response.status; error.retryAfter = Number(value.error?.retry_after_seconds || response.headers.get("retry-after") || 0); throw error; }
  return value;
}

function show(authenticated) {
  const legalActive = location.pathname.startsWith("/legal/");
  document.body.classList.toggle("is-authenticated", authenticated && !legalActive);
  $("#legal-view").hidden = !legalActive;
  $("#auth-view").hidden = authenticated || legalActive;
  $("#workbench-view").hidden = !authenticated || legalActive;
  document.querySelector(".sidebar").hidden = !authenticated || legalActive;
  document.querySelector(".bottom-nav").hidden = !authenticated || legalActive;
}

function authError(error) {
  if (error.status === 401) return "登录已失效，请重新登录。";
  return ({phone_not_registered:"该手机号尚未注册，请前往注册。", phone_already_registered:"该手机号已注册，请前往登录。", invalid_code:"验证码不正确，请重新输入。", code_expired:"验证码已失效，请重新获取。", too_many_attempts:"验证次数过多，请重新获取验证码。", agreement_required:"请先同意用户协议和隐私政策。", weak_password:"密码需为8—20位，并同时包含字母和数字。", invalid_request:"请检查填写内容。", rate_limited:"操作过于频繁，请稍后重试。", export_expired:"导出已过期，请重新申请。", confirmation_required:"请输入正确的注销确认。", network_error:"网络异常，请检查网络后重试。"})[error.message] || "操作失败，请稍后重试。";
}

function validPhone() { return /^1[3-9]\d{9}$/.test($("#phone").value); }
function validCode() { return /^\d{6}$/.test($("#code").value); }
function validPassword() { const value = $("#password").value; return value.length >= 8 && value.length <= 20 && /[A-Za-z]/.test(value) && /[0-9]/.test(value) && !/[\s\x00-\x1f\x7f]/.test(value); }
function refreshAuthControls() {
  const phoneIsValid = validPhone();
  const passwordIsValid = authMode !== "register" || validPassword();
  const busy = otpRequesting || authSubmitting;
  $("#phone").setAttribute("aria-invalid", String(phoneTouched && !phoneIsValid));
  $("#phone-error").hidden = !phoneTouched || phoneIsValid;
  $("#phone").disabled = busy;
  $("#login-tab").disabled = busy;
  $("#register-tab").disabled = busy;
  $("#switch-auth").disabled = busy;
  $("#code").disabled = !challenge || authSubmitting;
  $("#password").disabled = authSubmitting;
  $("#agreement").disabled = authSubmitting;
  $("#toggle-password").disabled = authSubmitting;
  $("#otp-button").disabled = !phoneIsValid || otpRequesting || countdown > 0;
  $("#auth-submit").disabled = !challenge || !phoneIsValid || !validCode() || !$("#agreement").checked || !passwordIsValid || authSubmitting;
  $("#password").setAttribute("aria-invalid", String(authMode === "register" && $("#password").value && !passwordIsValid));
  $("#password-error").hidden = authMode !== "register" || !$("#password").value || passwordIsValid;
  if (!$("#password-error").hidden) $("#password-error").textContent = "密码需为 8—20 位，并同时包含字母和数字。";
}

function setAuthMode(mode, keepPhone = false, updateHistory = true) {
  authRevision += 1;
  otpRequesting = false;
  authSubmitting = false;
  $("#legal-view").hidden = true; $("#auth-view").hidden = false;
  const modeChanged = authMode !== mode;
  authMode = mode; $("#auth-title").textContent = mode === "login" ? "验证码登录" : "手机号注册";
  $("#auth-help").textContent = mode === "login" ? "已有账号使用手机号验证码登录。" : "设置密码并确认协议，注册后直接进入你的错题本。";
  $("#register-fields").hidden = mode !== "register"; $("#agreement-fields").hidden = false; $("#password").required = mode === "register"; $("#auth-submit").textContent = mode === "login" ? "验证并进入" : "注册并进入";
  $("#switch-auth").textContent = mode === "login" ? "没有账号？立即注册" : "已有账号？去登录";
  $("#login-tab").classList.toggle("ghost", mode !== "login"); $("#register-tab").classList.toggle("ghost", mode !== "register");
  $("#login-tab").setAttribute("aria-pressed", String(mode === "login")); $("#register-tab").setAttribute("aria-pressed", String(mode === "register"));
  $("#password").type = "password"; $("#toggle-password").textContent = "显示"; $("#toggle-password").setAttribute("aria-label", "显示密码"); $("#toggle-password").setAttribute("aria-pressed", "false");
  if (!keepPhone || modeChanged) { if (!keepPhone) { $("#phone").value = ""; phoneTouched = false; } $("#code").value = ""; $("#code-error").hidden = true; $("#password").value = ""; $("#agreement").checked = false; }
  resetOtp();
  refreshAuthControls();
  if (!updateHistory) $("#phone").focus();
  if (updateHistory) history.pushState({authMode: mode}, "", mode === "login" ? "/login" : "/register");
}

function resetOtp() { challenge = null; clearInterval(countdownTimer); countdown = 0; $("#otp-button").textContent = "获取验证码"; refreshAuthControls(); }
function startCountdown(seconds = 60) { countdown = Math.max(1, Math.ceil(seconds || 60)); clearInterval(countdownTimer); const tick = () => { $("#otp-button").textContent = countdown ? `${countdown}s 后重试` : "重新发送"; refreshAuthControls(); if (!countdown) clearInterval(countdownTimer); }; tick(); countdownTimer = setInterval(() => { countdown -= 1; tick(); }, 1000); }

function showPanel(id) { ["errors","reviews","practice","progress","settings"].forEach(x => { const el = document.getElementById(x); if (el) el.hidden = x !== id && id !== "workbench"; }); document.querySelectorAll('a[href^="#"]').forEach(link => link.classList.toggle("active", link.getAttribute("href") === `#${id}`)); }

function status(target, message, error = false) {
  target.textContent = message;
  target.classList.toggle("error", error);
}

function escapeHtml(value) {
  const span = document.createElement("span");
  span.textContent = value;
  return span.innerHTML;
}

async function refresh() {
  try {
    await api("/v1/session");
    show(true);
    if (location.pathname.startsWith("/legal/")) return;
    if (location.pathname !== "/") history.replaceState({}, "", `/${location.hash || "#workbench"}`);
    const [workbench, reviews] = await Promise.all([api("/v1/workbench"), api("/v1/reviews/today")]);
    $("#error-count").textContent = workbench.error_count;
    $("#task-count").textContent = workbench.pending_task_count;
    $("#review-count").textContent = workbench.due_review_count;
    $("#gap-count").textContent = workbench.recommendation_gap_count;
    recentErrorIds = workbench.recent_errors.map(item => item.error_id);
    $("#error-list").innerHTML = workbench.recent_errors.length
      ? workbench.recent_errors.map(item => `<li><strong>${escapeHtml(item.question_text)}</strong><br><small>${escapeHtml(item.first_error || "待整理错因")}</small><button class="text-button" type="button" data-recommend-error="${item.error_id}">获取已验证推荐</button></li>`).join("")
      : '<li class="empty">还没有错题，上传第一道题吧。</li>';
    dueReview = reviews.items[0] || null;
    $("#review-stage").textContent = dueReview ? `第 ${dueReview.stage} 阶段` : "暂无任务";
    $("#review-question").textContent = dueReview ? "请先遮住解析，独立重做原题，再如实记录结果。" : "今天没有到期复习。";
    $("#review-actions").hidden = !dueReview;
    $("#create-pdf").disabled = recentErrorIds.length === 0;
  } catch (error) {
    if (error?.status !== 401) status($("#auth-status"), "网络异常，请检查网络后重试。", true);
    show(false);
  }
}

$("#phone-form").addEventListener("submit", async event => {
  event.preventDefault();
  phoneTouched = true;
  if (!validPhone()) return refreshAuthControls();
  phone = $("#phone").value;
  const requestRevision = authRevision;
  const requestMode = authMode;
  const requestPhone = phone;
  const button = event.submitter;
  otpRequesting = true;
  button.textContent = "发送中…";
  refreshAuthControls();
  try {
    const captcha_token = $("#captcha-token").value.trim();
    const result = await api(`/v1/auth/${requestMode}/otp/request`, {method: "POST", body: JSON.stringify(captcha_token ? {phone: requestPhone, captcha_token} : {phone: requestPhone})});
    if (requestRevision !== authRevision || requestMode !== authMode || requestPhone !== $("#phone").value) return;
    challenge = result.challenge_token;
    const localTestCode = /^\d{6}$/.test(result.local_test_code || "") ? result.local_test_code : "";
    $("#code").value = localTestCode;
    $("#code-error").hidden = true;
    refreshAuthControls();
    status($("#auth-status"), localTestCode ? "仅限本地测试：模拟验证码已自动填入。" : result.message);
    $("#code").focus();
    startCountdown(result.retry_after_seconds);
  } catch (error) {
    if (requestRevision !== authRevision || requestMode !== authMode || requestPhone !== $("#phone").value) return;
    if (error.message === "captcha_required") { $("#captcha-fields").hidden = false; $("#captcha-token").focus(); }
    if (error.status === 429 && error.retryAfter) startCountdown(error.retryAfter);
    status($("#auth-status"), authError(error), true);
    if (["phone_not_registered", "phone_already_registered"].includes(error.message)) $("#switch-auth").focus();
  } finally {
    if (requestRevision === authRevision) {
      otpRequesting = false;
      if (!countdown) button.textContent = "获取验证码";
      refreshAuthControls();
    }
  }
});

$("#phone").addEventListener("input", () => {
  const normalized = $("#phone").value.replace(/\D/g, "").slice(0, 11);
  if ($("#phone").value !== normalized) $("#phone").value = normalized;
  if (phone && $("#phone").value !== phone) {
    authRevision += 1;
    $("#code").value = "";
    resetOtp();
    status($("#auth-status"), "手机号已变更，请重新获取验证码。");
  }
  refreshAuthControls();
});
$("#phone").addEventListener("blur", () => { phoneTouched = true; refreshAuthControls(); });
$("#code").addEventListener("input", () => { $("#code").value = $("#code").value.replace(/\D/g, "").slice(0, 6); $("#code-error").hidden = true; refreshAuthControls(); });
$("#password").addEventListener("input", refreshAuthControls);
$("#agreement").addEventListener("change", refreshAuthControls);

$("#code-form").addEventListener("submit", async event => {
  event.preventDefault();
  if ($("#auth-submit").disabled) return refreshAuthControls();
  const button = event.submitter;
  const requestRevision = authRevision;
  const requestMode = authMode;
  const requestPhone = phone;
  const requestChallenge = challenge;
  authSubmitting = true;
  button.textContent = authMode === "login" ? "登录中" : "注册中";
  refreshAuthControls();
  try {
    const body = {phone: requestPhone, challenge_token: requestChallenge, code: $("#code").value, terms_version: "2026-08-23", privacy_version: "2026-08-23"};
    if (requestMode === "register") body.password = $("#password").value;
    await api(requestMode === "login" ? "/v1/auth/login/otp/verify" : "/v1/auth/register/complete", {method: "POST", body: JSON.stringify(body)});
    if (requestRevision !== authRevision || requestMode !== authMode || requestPhone !== phone || requestChallenge !== challenge) return;
    await refresh();
  } catch (error) {
    if (requestRevision !== authRevision || requestMode !== authMode || requestPhone !== phone || requestChallenge !== challenge) return;
    if (["invalid_code", "code_expired", "too_many_attempts"].includes(error.message)) {
      $("#code").value = "";
      $("#code-error").textContent = authError(error);
      $("#code-error").hidden = false;
      if (error.message !== "invalid_code") resetOtp();
      $("#code").focus();
    }
    status($("#auth-status"), authError(error), true);
  } finally {
    if (requestRevision === authRevision) {
      authSubmitting = false;
      button.textContent = authMode === "login" ? "验证并进入" : "注册并进入";
      refreshAuthControls();
    }
  }
});

$("#login-tab").onclick = () => { if (authMode !== "login") setAuthMode("login", false); }; $("#register-tab").onclick = () => { if (authMode !== "register") setAuthMode("register", true); };
$("#switch-auth").onclick = () => setAuthMode(authMode === "login" ? "register" : "login", authMode === "login");
$("#toggle-password").onclick = () => { const input = $("#password"); input.type = input.type === "password" ? "text" : "password"; const visible = input.type === "text"; $("#toggle-password").textContent = visible ? "隐藏" : "显示"; $("#toggle-password").setAttribute("aria-label", visible ? "隐藏密码" : "显示密码"); $("#toggle-password").setAttribute("aria-pressed", String(visible)); };
function showLegal(kind, updateHistory = true) { document.body.classList.remove("is-authenticated"); $("#password").type = "password"; $("#toggle-password").textContent = "显示"; $("#toggle-password").setAttribute("aria-label", "显示密码"); $("#toggle-password").setAttribute("aria-pressed", "false"); $("#auth-view").hidden = true; $("#workbench-view").hidden = true; document.querySelector(".sidebar").hidden = true; document.querySelector(".bottom-nav").hidden = true; $("#legal-view").hidden = false; const terms = kind === "terms"; $("#legal-title").textContent = terms ? "用户协议" : "隐私政策"; $("#legal-text").textContent = terms ? "请仅上传你有权处理的学习资料，并对手工确认的题干、作答和判题结果负责。" : "本地测试版只处理你主动提交的手机号和学习数据；认证秘密不进入日志或模型，个人数据可导出并可注销账号。"; if (updateHistory) history.pushState({legal: kind}, "", terms ? "/legal/terms" : "/legal/privacy"); }
document.querySelectorAll("[data-legal]").forEach(link => link.onclick = event => { event.preventDefault(); if (!otpRequesting && !authSubmitting) showLegal(link.dataset.legal); });
$("#legal-back").onclick = () => history.back();

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
    status($("#upload-status"), `文件已保存。请手工确认题干与作答；自动识别 Worker 接入后也会保留确认步骤。`);
    $("#question-text").focus();
    await refresh();
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
    const intake = await api(`/v1/intakes/${activeIntake.intakeId}/manual-candidate`, {
      method: "POST",
      body: JSON.stringify({question_text: $("#question-text").value, answer_text: $("#answer-text").value})
    });
    const confirmed = await api(`/v1/intakes/${activeIntake.intakeId}/confirm`, {
      method: "POST",
      body: JSON.stringify({input_version: intake.input_version}),
      headers: {"Idempotency-Key": crypto.randomUUID()}
    });
    activeIntake.inputVersion = intake.input_version;
    activeAttempt = confirmed.resource_id;
    $("#manual-intake-form").hidden = true;
    $("#manual-grade-form").hidden = false;
    status($("#manual-status"), "题干与作答已确认，请记录判题候选。");
    $("#verdict").focus();
  } catch (error) {
    status($("#manual-status"), `确认失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
});

$("#verdict").addEventListener("change", event => {
  $("#first-error").required = ["incorrect", "partial"].includes(event.target.value);
});

$("#manual-grade-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!activeAttempt || !activeIntake) return;
  const button = event.submitter;
  button.disabled = true;
  try {
    activeCandidate = await api(`/v1/attempts/${activeAttempt}/manual-grade`, {
      method: "POST",
      body: JSON.stringify({
        input_version: activeIntake.inputVersion,
        verdict: $("#verdict").value,
        first_error: $("#first-error").value,
        evidence: $("#grade-evidence").value
      })
    });
    const canCommit = ["incorrect", "partial"].includes(activeCandidate.verdict);
    $("#grade-summary").textContent = canCommit
      ? `候选结果：${activeCandidate.verdict === "incorrect" ? "错误" : "部分正确"}；首错：${activeCandidate.first_error}`
      : activeCandidate.verdict === "correct" ? "候选结果：本题正确，不写入错题本。" : "证据不足，不能写入错题本。";
    $("#grade-confirm").hidden = false;
    $("#commit-grade").hidden = !canCommit;
    status($("#manual-status"), canCommit ? "请核对候选后确认入本。" : "已安全停止，不会写入正式错题。");
  } catch (error) {
    status($("#manual-status"), `判题记录失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
});

$("#commit-grade").addEventListener("click", async event => {
  if (!activeCandidate) return;
  event.currentTarget.disabled = true;
  try {
    await api(`/v1/grade-results/${activeCandidate.result_id}/commit`, {
      method: "POST",
      body: JSON.stringify({input_version: activeCandidate.input_version}),
      headers: {"Idempotency-Key": crypto.randomUUID()}
    });
    status($("#upload-status"), "已写入错题本，并安排首次复习。");
    $("#manual-flow").hidden = true;
    $("#upload-form").reset();
    activeIntake = activeAttempt = activeCandidate = null;
    await refresh();
  } catch (error) {
    status($("#manual-status"), `入本失败：${error.message}`, true);
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
    await refresh();
  } catch (error) {
    event.target.textContent = `推荐失败：${error.message}`;
  } finally {
    event.target.disabled = false;
  }
});

$("#review-actions").addEventListener("click", async event => {
  const result = event.target.dataset.reviewResult;
  if (!result || !dueReview) return;
  event.target.disabled = true;
  try {
    const completed = await api(`/v1/reviews/${dueReview.review_id}/complete`, {method: "POST", body: JSON.stringify({result}), headers: {"Idempotency-Key": crypto.randomUUID()}});
    status($("#review-status"), completed.mastered ? "已完成全部复习阶段。" : `已记录，下次为第 ${completed.next_review.stage} 阶段。`);
    await refresh();
  } catch (error) {
    status($("#review-status"), `记录失败：${error.message}`, true);
  } finally {
    event.target.disabled = false;
  }
});

$("#create-pdf").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await api("/v1/practice-pdfs", {method: "POST", body: JSON.stringify({error_ids: recentErrorIds, include_answers: $("#include-answers").checked}), headers: {"Idempotency-Key": crypto.randomUUID()}});
    if (result.download_url) { const link = document.createElement("a"); link.href = result.download_url; link.textContent = "下载练习 PDF"; link.setAttribute("download", ""); $("#pdf-status").replaceChildren("已生成：", link); }
    else status($("#pdf-status"), `PDF 任务已受理：${result.task_id || result.resource_id || "处理中"}，请稍后刷新查看。`);
  } catch (error) {
    status($("#pdf-status"), `${authError(error)}`, true);
  } finally {
    button.disabled = recentErrorIds.length === 0;
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("/v1/session", {method: "DELETE"}); } finally { show(false); }
});

$("#logout-all").onclick = async () => { try { await api("/v1/sessions", {method: "DELETE"}); show(false); } catch (e) { status($("#settings-status"), authError(e), true); } };
$("#sensitive-otp").onclick = async () => { try { sensitiveAction = $("#sensitive-action").value; const captcha_token = $("#sensitive-captcha-token").value.trim(); const result = await api("/v1/auth/sensitive/otp/request", {method:"POST", body:JSON.stringify(captcha_token ? {phone:$("#sensitive-phone").value, action:sensitiveAction, captcha_token} : {phone:$("#sensitive-phone").value, action:sensitiveAction})}); sensitiveChallenge = result.challenge_token; const localTestCode = /^\d{6}$/.test(result.local_test_code || "") ? result.local_test_code : ""; if (localTestCode) $("#sensitive-code").value = localTestCode; status($("#settings-status"), localTestCode ? "仅限本地测试：操作验证码已自动填入。" : `用于${sensitiveAction === "export" ? "导出" : "注销"}的验证码已发送，请在 5 分钟内使用。`); } catch(e) { if (e.message === "captcha_required") { $("#sensitive-captcha-fields").hidden = false; $("#sensitive-captcha-token").focus(); } status($("#settings-status"), authError(e), true); } };
function sensitivePayload() { return {phone:$("#sensitive-phone").value, challenge_token:sensitiveChallenge, code:$("#sensitive-code").value}; }
$("#sensitive-action").onchange = () => { sensitiveChallenge = null; sensitiveAction = null; $("#sensitive-code").value = ""; status($("#settings-status"), "用途已改变，请重新获取验证码。"); };
$("#export-data").onclick = async () => { if (sensitiveAction !== "export" || !sensitiveChallenge) return status($("#settings-status"), "请先选择导出并获取对应验证码。", true); try { const job = await api("/v1/exports", {method:"POST", body:JSON.stringify(sensitivePayload()), headers:{"Idempotency-Key":crypto.randomUUID()}}); sensitiveChallenge = sensitiveAction = null; $("#sensitive-code").value = ""; if (job.download_url) { const link = document.createElement("a"); link.href = job.download_url; link.textContent = "下载个人数据"; link.setAttribute("download", ""); $("#settings-status").replaceChildren("导出已生成：", link); link.click(); } else status($("#settings-status"), `导出任务已创建：${job.job_id || job.task_id || "处理中"}`); } catch(e) { status($("#settings-status"), authError(e), true); } };
$("#delete-account").onclick = async () => { if (sensitiveAction !== "delete" || !sensitiveChallenge) return status($("#settings-status"), "请先选择注销并获取对应验证码。", true); if (!confirm("注销后所有设备立即退出，账号不能恢复。确认继续？")) return; try { await api("/v1/account", {method:"DELETE", body:JSON.stringify({...sensitivePayload(), confirmation:"DELETE"})}); sensitiveChallenge = sensitiveAction = null; show(false); setAuthMode("register", false); status($("#auth-status"), "账号已注销。", false); } catch(e) { status($("#settings-status"), authError(e), true); } };
$("#refresh-errors").onclick = async () => { try { const result = await api("/v1/errors"); $("#all-errors").innerHTML = result.items.map(item => `<li><button class="text-button" data-error-id="${item.error_id}">${escapeHtml(item.question_text)}</button><br><small>${escapeHtml(item.first_error || "待整理错因")}</small></li>`).join("") || '<li class="empty">还没有错题。</li>'; } catch(e) { status($("#settings-status"), authError(e), true); } };
$("#all-errors").onclick = async event => { const id = event.target.dataset.errorId; if (!id) return; try { const item = await api(`/v1/errors/${id}`); $("#error-detail").hidden = false; $("#error-detail").textContent = `${item.question_text}\n${item.first_error || ""}`; } catch(e) { status($("#settings-status"), authError(e), true); } };
function routeHash() { const hash = location.hash.slice(1); showPanel(["errors","reviews","practice","progress","settings"].includes(hash) ? hash : "workbench"); if (hash === "errors") $("#refresh-errors").click(); if (hash === "progress") api("/v1/progress").then(x => $("#progress-text").textContent = `错题 ${x.error_count || 0} 道，已完成复习 ${x.completed_review_count || 0} 次。`).catch(e => $("#progress-text").textContent = authError(e)); }
window.addEventListener("hashchange", routeHash);
function routePage() { if (location.pathname === "/legal/terms") return showLegal("terms", false); if (location.pathname === "/legal/privacy") return showLegal("privacy", false); setAuthMode(location.pathname === "/register" ? "register" : "login", true, false); }
window.addEventListener("popstate", () => { routePage(); refresh(); });

routePage();
routeHash();
refresh();
