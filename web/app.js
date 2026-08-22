const $ = selector => document.querySelector(selector);
let challenge = null;
let phone = null;
let dueReview = null;
let recentErrorIds = [];

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body instanceof FormData ? {} : {"Content-Type": "application/json"}),
      ...(options.headers || {})
    }
  });
  if (response.status === 204) return null;
  if (response.headers.get("content-type")?.startsWith("application/pdf")) return response.blob();
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.code || "temporarily_unavailable");
  return value;
}

function show(authenticated) {
  $("#auth-view").hidden = authenticated;
  $("#workbench-view").hidden = !authenticated;
  document.querySelector(".sidebar").hidden = !authenticated;
  document.querySelector(".bottom-nav").hidden = !authenticated;
}

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
  } catch {
    show(false);
  }
}

$("#phone-form").addEventListener("submit", async event => {
  event.preventDefault();
  phone = $("#phone").value;
  const button = event.submitter;
  button.disabled = true;
  try {
    const result = await api("/v1/auth/otp/request", {method: "POST", body: JSON.stringify({phone})});
    challenge = result.challenge_token;
    $("#code-form").hidden = false;
    status($("#auth-status"), result.message);
    $("#code").focus();
  } catch (error) {
    status($("#auth-status"), `请求失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
});

$("#code-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("/v1/auth/otp/verify", {method: "POST", body: JSON.stringify({phone, challenge_token: challenge, code: $("#code").value})});
    await refresh();
  } catch {
    status($("#auth-status"), "验证码无效或已过期，请重试。", true);
  } finally {
    button.disabled = false;
  }
});

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
    status($("#upload-status"), `已保存，识别任务 ${task.task_id.slice(0, 8)} 正在处理。`);
    await refresh();
  } catch (error) {
    status($("#upload-status"), `上传失败：${error.message}。文件未丢失时可重试。`, true);
  } finally {
    button.disabled = false;
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
    const link = document.createElement("a");
    link.href = result.download_url;
    link.textContent = "下载练习 PDF";
    link.setAttribute("download", "");
    $("#pdf-status").replaceChildren("已生成：", link);
  } catch (error) {
    status($("#pdf-status"), `生成失败：${error.message}`, true);
  } finally {
    button.disabled = recentErrorIds.length === 0;
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("/v1/session", {method: "DELETE"}); } finally { show(false); }
});

refresh();
