const $ = selector => document.querySelector(selector);
const authMode = document.body.dataset.authMode;
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
let requestedPhone = null;
let countdown = 0;
let countdownTimer = null;
let otpRequesting = false;
let authSubmitting = false;
let phoneTouched = false;
let authRevision = 0;

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      ...options,
      headers: {"Content-Type": "application/json", "X-Device-ID": deviceId, ...(options.headers || {})}
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
    error.retryAfter = Number(value.error?.retry_after_seconds || response.headers.get("retry-after") || 0);
    throw error;
  }
  return value;
}

function authError(error) {
  return ({
    phone_not_registered: "该手机号尚未注册，请前往注册。",
    phone_already_registered: "该手机号已注册，请前往登录。",
    invalid_code: "验证码不正确，请重新输入。",
    code_expired: "验证码已失效，请重新获取。",
    too_many_attempts: "验证次数过多，请重新获取验证码。",
    agreement_required: "请先同意用户协议和隐私政策。",
    weak_password: "密码需为 8—20 位，并同时包含字母和数字。",
    invalid_request: "请检查填写内容。",
    rate_limited: "操作过于频繁，请稍后重试。",
    network_error: "网络异常，请检查网络后重试。"
  })[error.message] || "操作失败，请稍后重试。";
}

function status(message, error = false) {
  $("#auth-status").textContent = message;
  $("#auth-status").classList.toggle("error", error);
}

function validPhone() { return /^1[3-9]\d{9}$/.test($("#phone").value); }
function validCode() { return /^\d{6}$/.test($("#code").value); }
function validPassword() {
  if (authMode !== "register") return true;
  const value = $("#password").value;
  return value.length >= 8 && value.length <= 20 && /[A-Za-z]/.test(value) && /[0-9]/.test(value) && !/[\s\x00-\x1f\x7f]/.test(value);
}

function refreshAuthControls() {
  const phoneIsValid = validPhone();
  const passwordIsValid = validPassword();
  const busy = otpRequesting || authSubmitting;
  $("#phone").setAttribute("aria-invalid", String(phoneTouched && !phoneIsValid));
  $("#phone-error").hidden = !phoneTouched || phoneIsValid;
  $("#phone").disabled = busy;
  $("#code").disabled = !challenge || authSubmitting;
  $("#agreement").disabled = authSubmitting;
  $("#otp-button").disabled = !phoneIsValid || otpRequesting || countdown > 0;
  $("#auth-submit").disabled = !challenge || !phoneIsValid || !validCode() || !passwordIsValid || !$("#agreement").checked || authSubmitting;
  if (authMode === "register") {
    $("#password").disabled = authSubmitting;
    $("#toggle-password").disabled = authSubmitting;
    $("#password").setAttribute("aria-invalid", String(Boolean($("#password").value) && !passwordIsValid));
    $("#password-error").hidden = !$("#password").value || passwordIsValid;
    if (!$("#password-error").hidden) $("#password-error").textContent = "密码需为 8—20 位，并同时包含字母和数字。";
  }
}

function resetOtp() {
  challenge = null;
  clearInterval(countdownTimer);
  countdown = 0;
  $("#otp-button").textContent = "获取验证码";
  refreshAuthControls();
}

function startCountdown(seconds = 60) {
  countdown = Math.max(1, Math.ceil(seconds || 60));
  clearInterval(countdownTimer);
  const tick = () => {
    $("#otp-button").textContent = countdown ? `${countdown}s 后重试` : "重新发送";
    refreshAuthControls();
    if (!countdown) clearInterval(countdownTimer);
  };
  tick();
  countdownTimer = setInterval(() => { countdown -= 1; tick(); }, 1000);
}

$("#phone-form").addEventListener("submit", async event => {
  event.preventDefault();
  phoneTouched = true;
  if (!validPhone()) return refreshAuthControls();
  const requestRevision = authRevision;
  const phone = $("#phone").value;
  const button = event.submitter;
  otpRequesting = true;
  button.textContent = "发送中…";
  refreshAuthControls();
  try {
    const captchaToken = $("#captcha-token").value.trim();
    const result = await api(`/v1/auth/${authMode}/otp/request`, {method: "POST", body: JSON.stringify(captchaToken ? {phone, captcha_token: captchaToken} : {phone})});
    if (requestRevision !== authRevision || phone !== $("#phone").value) return;
    challenge = result.challenge_token;
    requestedPhone = phone;
    const localTestCode = /^\d{6}$/.test(result.local_test_code || "") ? result.local_test_code : "";
    $("#code").value = localTestCode;
    $("#code-error").hidden = true;
    status(localTestCode ? "仅限本地测试：模拟验证码已自动填入。" : result.message);
    refreshAuthControls();
    $("#code").focus();
    startCountdown(result.retry_after_seconds);
  } catch (error) {
    if (requestRevision !== authRevision) return;
    if (error.message === "captcha_required") { $("#captcha-fields").hidden = false; $("#captcha-token").focus(); }
    if (error.status === 429 && error.retryAfter) startCountdown(error.retryAfter);
    status(authError(error), true);
  } finally {
    if (requestRevision === authRevision) {
      otpRequesting = false;
      if (!countdown) button.textContent = "获取验证码";
      refreshAuthControls();
    }
  }
});

$("#code-form").addEventListener("submit", async event => {
  event.preventDefault();
  if ($("#auth-submit").disabled) return refreshAuthControls();
  const button = event.submitter;
  const requestRevision = authRevision;
  const requestChallenge = challenge;
  const phone = requestedPhone;
  authSubmitting = true;
  button.textContent = authMode === "login" ? "登录中…" : "注册中…";
  refreshAuthControls();
  try {
    const body = {phone, challenge_token: requestChallenge, code: $("#code").value, terms_version: "2026-08-23", privacy_version: "2026-08-23"};
    if (authMode === "register") body.password = $("#password").value;
    await api(authMode === "login" ? "/v1/auth/login/otp/verify" : "/v1/auth/register/complete", {method: "POST", body: JSON.stringify(body)});
    location.replace("/");
  } catch (error) {
    if (requestRevision !== authRevision || requestChallenge !== challenge) return;
    if (["invalid_code", "code_expired", "too_many_attempts"].includes(error.message)) {
      $("#code").value = "";
      $("#code-error").textContent = authError(error);
      $("#code-error").hidden = false;
      if (error.message !== "invalid_code") resetOtp();
      $("#code").focus();
    }
    status(authError(error), true);
  } finally {
    if (requestRevision === authRevision) {
      authSubmitting = false;
      button.textContent = authMode === "login" ? "验证并进入" : "注册并进入";
      refreshAuthControls();
    }
  }
});

$("#phone").addEventListener("input", () => {
  const normalized = $("#phone").value.replace(/\D/g, "").slice(0, 11);
  if ($("#phone").value !== normalized) $("#phone").value = normalized;
  if (requestedPhone && $("#phone").value !== requestedPhone) {
    authRevision += 1;
    requestedPhone = null;
    $("#code").value = "";
    resetOtp();
    status("手机号已变更，请重新获取验证码。");
  }
  refreshAuthControls();
});
$("#phone").addEventListener("blur", () => { phoneTouched = true; refreshAuthControls(); });
$("#code").addEventListener("input", () => { $("#code").value = $("#code").value.replace(/\D/g, "").slice(0, 6); $("#code-error").hidden = true; refreshAuthControls(); });
$("#agreement").addEventListener("change", refreshAuthControls);

if (authMode === "register") {
  $("#password").addEventListener("input", refreshAuthControls);
  $("#toggle-password").addEventListener("click", () => {
    const input = $("#password");
    input.type = input.type === "password" ? "text" : "password";
    const visible = input.type === "text";
    $("#toggle-password").textContent = visible ? "隐藏" : "显示";
    $("#toggle-password").setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
    $("#toggle-password").setAttribute("aria-pressed", String(visible));
  });
}

refreshAuthControls();
