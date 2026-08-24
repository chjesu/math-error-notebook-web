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
    model_unavailable: "本地智能处理暂时不可用，已切换为人工确认。",
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

const causeLabels = {
  knowledge_gap: "知识点未掌握", concept_confusion: "概念理解不准确", formula_condition: "公式或定理使用条件遗漏",
  method_choice: "解题思路选择错误", reasoning_gap: "推理或步骤跳跃", algebra_transform: "代数变形错误",
  calculation: "计算错误", misreading: "审题错误", incomplete_cases: "漏解或分类不完整",
  expression: "表达或书写不规范", careless: "有直接证据的粗心错误", unclear: "信息不足"
};

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

function bindWorkbench() {
  let activeIntake = null;
  let activeAttempt = null;
  let activeCandidate = null;
  let uploadFiles = [];
  let uploadRunning = false;
  const CHAT_PAGE_SIZE = 10;
  const conversationTurns = [];
  const renderedCandidates = new Set();
  let visibleTurnCount = CHAT_PAGE_SIZE;
  const pendingIntakes = [];
  const uploadInput = $("#file");
  const uploadButton = $("#upload-button");
  const dropZone = $("#drop-zone");
  const uploadSurface = $(".chat-main");
  const allowedExtensions = new Set(["pdf", "png", "jpg", "jpeg", "docx"]);
  const maxFileBytes = 25 * 1024 * 1024;

  function renderUploadFiles() {
    const stateLabels = {queued: "等待上传", uploading: "上传中", processing: "处理中", done: "上传完成", failed: "上传失败"};
    const composerFiles = uploadFiles.filter(item => !item.submitted);
    $("#upload-file-list").replaceChildren(...composerFiles.map(item => {
      const card = document.createElement("li");
      const preview = document.createElement("div");
      const name = document.createElement("small");
      const state = document.createElement("span");
      const remove = document.createElement("button");
      card.className = `upload-thumbnail is-${item.state}`;
      card.title = item.error ? `${item.file.name}：${item.error}` : item.file.name;
      card.setAttribute("aria-label", `${item.file.name}，${stateLabels[item.state]}`);
      preview.className = "upload-preview";
      if (item.previewUrl) {
        const image = document.createElement("img");
        image.src = item.previewUrl;
        image.alt = "";
        preview.append(image);
      } else {
        const type = document.createElement("strong");
        type.textContent = item.extension.toUpperCase();
        preview.append(type);
      }
      state.className = "upload-state";
      state.textContent = item.state === "uploading" ? `${item.progress}%` : stateLabels[item.state];
      preview.append(state);
      if (item.state === "uploading") {
        const progress = document.createElement("i");
        progress.className = "upload-progress";
        progress.style.width = `${item.progress}%`;
        preview.append(progress);
      }
      if (["queued", "failed"].includes(item.state)) {
        remove.type = "button";
        remove.dataset.removeFile = item.id;
        remove.setAttribute("aria-label", `移除 ${item.file.name}`);
        remove.textContent = "×";
        preview.append(remove);
      }
      name.textContent = item.file.name;
      card.append(preview, name);
      return card;
    }));
    const retryable = composerFiles.filter(item => ["queued", "failed"].includes(item.state));
    uploadButton.disabled = uploadRunning || retryable.length === 0;
    const retrying = retryable.some(item => item.state === "failed");
    uploadButton.textContent = retrying ? "↻" : "↑";
    uploadButton.setAttribute("aria-label", retrying ? "重试失败文件" : "上传并录入");
  }

  function addUploadFiles(files) {
    const rejected = [];
    const duplicates = [];
    for (const file of files) {
      const extension = file.name.split(".").pop().toLowerCase();
      if (!allowedExtensions.has(extension)) rejected.push(`${file.name}：格式不支持`);
      else if (file.size > maxFileBytes) rejected.push(`${file.name}：超过 25 MB`);
      else if (uploadFiles.some(item => item.file.name.toLowerCase() === file.name.toLowerCase() && item.file.size === file.size && item.file.lastModified === file.lastModified)) duplicates.push(file.name);
      else uploadFiles.push({id: crypto.randomUUID(), file, extension, previewUrl: ["png", "jpg", "jpeg"].includes(extension) ? URL.createObjectURL(file) : "", state: "queued", progress: 0, error: "", submitted: false});
    }
    renderUploadFiles();
    const waiting = uploadFiles.filter(item => item.state === "queued").length;
    const notice = [rejected.join("；"), duplicates.length ? `已忽略 ${duplicates.length} 个重复文件` : ""].filter(Boolean).join("；");
    status($("#upload-status"), notice || `已添加 ${waiting} 个待上传文件。`, rejected.length > 0);
  }

  function scrollChatToEnd() {
    requestAnimationFrame(() => { $("#chat-thread").scrollTop = $("#chat-thread").scrollHeight; });
  }

  function renderConversationWindow() {
    const start = Math.max(0, conversationTurns.length - visibleTurnCount);
    $("#chat-stream").replaceChildren(...conversationTurns.slice(start));
    $("#history-pagination").hidden = start === 0;
  }

  $("#load-older").addEventListener("click", () => {
    const thread = $("#chat-thread");
    const previousHeight = thread.scrollHeight;
    visibleTurnCount += CHAT_PAGE_SIZE;
    renderConversationWindow();
    requestAnimationFrame(() => { thread.scrollTop += thread.scrollHeight - previousHeight; });
  });

  function createConversationPreview(item) {
    const card = document.createElement("div");
    const preview = document.createElement("div");
    const name = document.createElement("small");
    card.className = "chat-upload-thumbnail";
    preview.className = "chat-upload-preview";
    if (item.previewUrl) {
      const image = document.createElement("img");
      image.src = item.previewUrl;
      image.alt = "";
      preview.append(image);
    } else {
      const type = document.createElement("strong");
      type.textContent = item.extension.toUpperCase();
      preview.append(type);
    }
    name.textContent = item.file.name;
    card.title = item.file.name;
    card.append(preview, name);
    return card;
  }

  function appendTurn(turn) {
    conversationTurns.push(turn);
    renderConversationWindow();
    $(".chat-welcome").hidden = true;
    scrollChatToEnd();
  }

  function appendUserUpload(files) {
    const turn = document.createElement("div");
    const bubble = document.createElement("div");
    const grid = document.createElement("div");
    const label = document.createElement("p");
    turn.className = "chat-turn user-turn";
    bubble.className = "chat-upload-bubble";
    grid.className = "chat-upload-grid";
    label.textContent = `请整理这 ${files.length} 个文件`;
    grid.append(...files.map(createConversationPreview));
    bubble.append(grid, label);
    turn.append(bubble);
    appendTurn(turn);
  }

  function appendAssistantProgress() {
    const turn = document.createElement("div");
    const avatar = document.createElement("img");
    const response = document.createElement("div");
    const disclosure = document.createElement("details");
    const summary = document.createElement("summary");
    const steps = document.createElement("ol");
    turn.className = "chat-turn assistant-turn chat-progress is-running";
    avatar.className = "chat-avatar";
    avatar.src = "/assets/branding/logo-symbol-color-64-v1.png";
    avatar.alt = "";
    response.className = "chat-response";
    disclosure.className = "chat-disclosure";
    disclosure.open = true;
    summary.className = "chat-disclosure-summary";
    summary.textContent = "正在处理";
    steps.className = "chat-progress-steps";
    steps.setAttribute("role", "status");
    steps.setAttribute("aria-live", "polite");
    disclosure.append(summary, steps);
    response.append(disclosure);
    turn.append(avatar, response);
    appendTurn(turn);
    return {turn, disclosure, summary, steps, currentTitle: "", currentStep: null, currentDetail: null};
  }

  function setAssistantProgress(progress, title, detail, state = "running") {
    progress.turn.className = `chat-turn assistant-turn chat-progress is-${state}`;
    if (progress.currentTitle !== title) {
      progress.currentStep?.classList.replace("is-active", "is-done");
      const step = document.createElement("li");
      const indicator = document.createElement("span");
      const content = document.createElement("div");
      const heading = document.createElement("strong");
      const description = document.createElement("p");
      step.className = "is-active";
      indicator.className = "progress-indicator";
      indicator.setAttribute("aria-hidden", "true");
      heading.textContent = title;
      content.append(heading, description);
      step.append(indicator, content);
      progress.steps.append(step);
      progress.currentTitle = title;
      progress.currentStep = step;
      progress.currentDetail = description;
    }
    progress.currentDetail.textContent = detail;
    progress.summary.textContent = state === "running" ? title : `${title} · ${detail}`;
    if (state === "running") progress.disclosure.open = true;
    if (state !== "running") {
      progress.currentStep.className = `is-${state}`;
      progress.disclosure.open = state === "error";
    }
    scrollChatToEnd();
  }

  function appendAssistantNote(message, error = false) {
    if (!message) return;
    const turn = document.createElement("div");
    const avatar = document.createElement("img");
    const response = document.createElement("div");
    turn.className = `chat-turn assistant-turn${error ? " is-error" : ""}`;
    avatar.className = "chat-avatar";
    avatar.src = "/assets/branding/logo-symbol-color-64-v1.png";
    avatar.alt = "";
    response.className = "chat-response";
    response.textContent = message;
    turn.append(avatar, response);
    appendTurn(turn);
  }

  function appendUserConfirmation(questionText, answerText) {
    const turn = document.createElement("div");
    const content = document.createElement("div");
    const heading = document.createElement("strong");
    const question = document.createElement("p");
    const answer = document.createElement("p");
    turn.className = "chat-turn user-turn";
    content.className = "chat-user-confirmation";
    heading.textContent = "已确认题干与作答";
    question.textContent = `题干：${questionText}`;
    answer.textContent = `作答：${answerText || "未填写"}`;
    content.append(heading, question, answer);
    turn.append(content);
    appendTurn(turn);
  }

  function appendGradeCandidate(candidate) {
    if (!candidate.result_id || renderedCandidates.has(candidate.result_id)) return;
    renderedCandidates.add(candidate.result_id);
    const diagnosis = candidate.diagnosis || {};
    const turn = document.createElement("div");
    const avatar = document.createElement("img");
    const response = document.createElement("div");
    const heading = document.createElement("strong");
    const list = document.createElement("dl");
    const verdict = candidate.verdict === "incorrect" ? "错误" : candidate.verdict === "partial" ? "部分正确" : candidate.verdict === "correct" ? "正确" : "证据不足";
    turn.className = "chat-turn assistant-turn";
    avatar.className = "chat-avatar";
    avatar.src = "/assets/branding/logo-symbol-color-64-v1.png";
    avatar.alt = "";
    response.className = "chat-response chat-candidate";
    heading.textContent = `判题候选 · ${verdict}`;
    for (const [label, value] of [["第一处错误", candidate.first_error], ["主要错因", causeLabels[diagnosis.cause_code] || diagnosis.cause_code], ["判断依据", diagnosis.cause_evidence], ["正确过程", diagnosis.correct_solution], ["最终答案", diagnosis.final_answer], ["防错提示", diagnosis.prevention_cue]]) {
      if (!value) continue;
      const term = document.createElement("dt");
      const detail = document.createElement("dd");
      term.textContent = label;
      detail.textContent = value;
      list.append(term, detail);
    }
    response.append(heading, list);
    turn.append(avatar, response);
    appendTurn(turn);
  }

  function showManualComposer(show) {
    $("#manual-flow").hidden = !show;
    $("#upload-form").hidden = show;
    $("#composer-note").hidden = show;
  }

  function activateNextIntake(message = "", error = false) {
    appendAssistantNote(message, error);
    activeIntake = pendingIntakes.shift() || null;
    activeAttempt = null;
    activeCandidate = null;
    $("#manual-intake-form").reset();
    $("#manual-grade-form").reset();
    $("#grade-confirm").hidden = true;
    $("#commit-grade").hidden = false;
    $("#next-intake").hidden = true;
    status($("#manual-status"), "");
    if (!activeIntake) {
      showManualComposer(false);
      return;
    }
    showManualComposer(true);
    $("#manual-intake-form").hidden = false;
    $("#manual-grade-form").hidden = true;
    $("#intake-context").textContent = `请确认“${activeIntake.fileName}”的题干与作答${pendingIntakes.length ? `，后面还有 ${pendingIntakes.length} 个文件` : ""}。`;
    $("#question-text").value = activeIntake.questionText || "";
    $("#answer-text").value = activeIntake.answerText || "";
    scrollChatToEnd();
    $("#question-text").focus();
  }

  function uploadFile(item, onProgress) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("purpose", "question_image");
      form.append("file", item.file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/v1/files");
      xhr.withCredentials = true;
      xhr.setRequestHeader("X-Device-ID", deviceId);
      xhr.setRequestHeader("Idempotency-Key", crypto.randomUUID());
      xhr.upload.addEventListener("progress", event => {
        if (!event.lengthComputable) return;
        item.progress = Math.min(99, Math.round(event.loaded / event.total * 100));
        onProgress(item.progress);
      });
      xhr.addEventListener("load", () => {
        const value = (() => { try { return JSON.parse(xhr.responseText); } catch { return {}; } })();
        if (xhr.status >= 200 && xhr.status < 300) return resolve(value);
        const error = new Error(value.error?.code || "temporarily_unavailable");
        error.status = xhr.status;
        reject(error);
      });
      xhr.addEventListener("error", () => {
        const error = new Error("network_error");
        error.status = 0;
        reject(error);
      });
      xhr.send(form);
    });
  }

  uploadInput.addEventListener("change", () => {
    addUploadFiles(uploadInput.files);
    uploadInput.value = "";
  });
  dropZone.addEventListener("click", event => {
    if (!event.target.closest("button")) uploadInput.click();
  });
  $("#file-picker").addEventListener("click", () => uploadInput.click());
  uploadSurface.addEventListener("dragover", event => {
    if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    uploadSurface.classList.add("drag-active");
  });
  uploadSurface.addEventListener("dragleave", event => {
    if (event.relatedTarget && uploadSurface.contains(event.relatedTarget)) return;
    uploadSurface.classList.remove("drag-active");
  });
  uploadSurface.addEventListener("drop", event => {
    if (!event.dataTransfer?.files?.length) return;
    event.preventDefault();
    uploadSurface.classList.remove("drag-active");
    addUploadFiles(event.dataTransfer.files);
  });
  document.addEventListener("paste", event => {
    if (event.target.closest?.("input, textarea, [contenteditable]")) return;
    const files = event.clipboardData?.files;
    if (files?.length) { event.preventDefault(); addUploadFiles(files); }
  });
  $("#upload-file-list").addEventListener("click", event => {
    const id = event.target.dataset.removeFile;
    if (!id) return;
    const removed = uploadFiles.find(item => item.id === id);
    if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
    uploadFiles = uploadFiles.filter(item => item.id !== id);
    renderUploadFiles();
  });

  $("#upload-form").addEventListener("submit", async event => {
    event.preventDefault();
    const files = uploadFiles.filter(item => ["queued", "failed"].includes(item.state));
    if (!files.length || uploadRunning) return;
    uploadRunning = true;
    files.forEach(item => { item.submitted = true; });
    renderUploadFiles();
    appendUserUpload(files);
    const progress = appendAssistantProgress();
    status($("#upload-status"), "");
    setAssistantProgress(progress, "准备发送附件", `共 ${files.length} 个文件，正在开始上传。`);
    let completed = 0;
    let failed = 0;
    for (const [index, item] of files.entries()) {
      item.state = "uploading";
      item.progress = 0;
      item.error = "";
      setAssistantProgress(progress, "正在上传附件", `${index + 1}/${files.length} · ${item.file.name} · 0%`);
      try {
        const uploaded = await uploadFile(item, percent => setAssistantProgress(progress, "正在上传附件", `${index + 1}/${files.length} · ${item.file.name} · ${percent}%`));
        item.state = "processing";
        item.progress = 100;
        setAssistantProgress(progress, "附件已安全保存", `${index + 1}/${files.length} · ${item.file.name} · 格式、大小和重复内容校验已完成`);
        setAssistantProgress(progress, "正在创建录入任务", `${index + 1}/${files.length} · ${item.file.name}`);
        const task = await api("/v1/intakes", {method: "POST", body: JSON.stringify({file_id: uploaded.file_id}), headers: {"Idempotency-Key": crypto.randomUUID()}});
        let modelCandidate = {input_version: 1, question_text: "", answer_text: "", model_status: "manual"};
        try {
          setAssistantProgress(progress, "正在识别题目与作答", `${index + 1}/${files.length} · ${item.file.name} · Codex CLI 正在读取图片`);
          modelCandidate = await api(`/v1/intakes/${task.resource_id}/model-candidate`, {method: "POST", body: "{}"});
          const recognized = modelCandidate.model_status === "complete" || modelCandidate.model_status === "existing";
          setAssistantProgress(progress, recognized ? "识别候选已生成" : "图片内容不够清晰", recognized ? `${index + 1}/${files.length} · ${item.file.name} · 请核对后再判题` : `${index + 1}/${files.length} · ${item.file.name} · 已切换为人工录入`, recognized ? "running" : "warning");
        } catch (modelError) {
          setAssistantProgress(progress, "自动识别未完成", `${index + 1}/${files.length} · ${item.file.name} · ${authError(modelError)}`, "warning");
        }
        pendingIntakes.push({intakeId: task.resource_id, inputVersion: modelCandidate.input_version || 1, fileName: item.file.name, questionText: modelCandidate.question_text || "", answerText: modelCandidate.answer_text || "", modelStatus: modelCandidate.model_status || "manual"});
        item.state = "done";
        completed += 1;
        setAssistantProgress(progress, "已加入确认队列", `${index + 1}/${files.length} · ${item.file.name} · 接下来由你核对题干与作答`);
      } catch (error) {
        item.state = "failed";
        item.error = authError(error);
        item.submitted = false;
        failed += 1;
        setAssistantProgress(progress, "当前文件处理失败", `${index + 1}/${files.length} · ${item.file.name} · ${item.error}`, "warning");
      }
      renderUploadFiles();
    }
    uploadRunning = false;
    renderUploadFiles();
    const summary = `${completed ? `已保存 ${completed} 个文件` : "没有文件上传成功"}${failed ? `，${failed} 个失败，可重试` : ""}。`;
    if (completed) {
      const nextStep = activeIntake ? "已成功文件已加入待确认队列。" : "请从第一份开始核对题干与作答。";
      setAssistantProgress(progress, "文件已准备好", `${summary} ${nextStep}`, failed ? "warning" : "complete");
      if (!activeIntake && pendingIntakes.length) activateNextIntake();
    } else {
      setAssistantProgress(progress, "附件发送失败", `${summary} 文件仍保留在输入框中，你可以重试。`, "error");
    }
  });

  window.addEventListener("beforeunload", () => uploadFiles.forEach(item => item.previewUrl && URL.revokeObjectURL(item.previewUrl)));

  $("#manual-intake-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!activeIntake) return;
    const button = event.submitter;
    button.disabled = true;
    try {
      const questionText = $("#question-text").value;
      const answerText = $("#answer-text").value;
      let intake;
      if (["complete", "existing"].includes(activeIntake.modelStatus)) {
        const changed = questionText !== activeIntake.questionText || answerText !== activeIntake.answerText;
        intake = changed ? await api(`/v1/intakes/${activeIntake.intakeId}`, {method: "PATCH", body: JSON.stringify({input_version: activeIntake.inputVersion, question_text: questionText, answer_text: answerText})}) : {input_version: activeIntake.inputVersion};
      } else {
        intake = await api(`/v1/intakes/${activeIntake.intakeId}/manual-candidate`, {method: "POST", body: JSON.stringify({question_text: questionText, answer_text: answerText})});
      }
      const confirmed = await api(`/v1/intakes/${activeIntake.intakeId}/confirm`, {method: "POST", body: JSON.stringify({input_version: intake.input_version}), headers: {"Idempotency-Key": crypto.randomUUID()}});
      activeIntake.inputVersion = intake.input_version;
      activeAttempt = confirmed.resource_id;
      appendUserConfirmation(questionText, answerText);
      $("#manual-intake-form").hidden = true;
      $("#manual-grade-form").hidden = false;
      const gradeProgress = appendAssistantProgress();
      setAssistantProgress(gradeProgress, "题干与作答已确认", "正在把确认后的内容交给 Codex CLI 判题。");
      setAssistantProgress(gradeProgress, "正在定位第一处错误", "模型只生成候选，不会自动写入错题本。");
      try {
        activeCandidate = await api(`/v1/attempts/${activeAttempt}/model-grade`, {method: "POST", body: JSON.stringify({input_version: activeIntake.inputVersion})});
        showGradeCandidate(activeCandidate);
        setAssistantProgress(gradeProgress, "判题候选已生成", "请核对首错、错因和完整解法后，再确认是否入本。", "complete");
      } catch (modelError) {
        setAssistantProgress(gradeProgress, "自动判题未完成", `${authError(modelError)} 你仍可在下方人工填写。`, "warning");
        status($("#manual-status"), "请人工记录判题候选。");
        $("#verdict").focus();
      }
    } catch (error) {
      status($("#manual-status"), `确认失败：${authError(error)}`, true);
    } finally {
      button.disabled = false;
    }
  });

  function refreshDiagnosisFields() {
    const required = ["incorrect", "partial"].includes($("#verdict").value);
    $("#diagnosis-fields").hidden = !required;
    for (const id of ["first-error", "cause-code", "grade-evidence", "correct-solution", "final-answer"]) $(`#${id}`).required = required;
  }
  $("#verdict").addEventListener("change", refreshDiagnosisFields);
  refreshDiagnosisFields();

  function showGradeCandidate(candidate) {
    const diagnosis = candidate.diagnosis || {};
    $("#verdict").value = candidate.verdict;
    $("#first-error").value = candidate.first_error || "";
    $("#cause-code").value = diagnosis.cause_code || "unclear";
    $("#grade-evidence").value = diagnosis.cause_evidence || "";
    $("#correct-solution").value = diagnosis.correct_solution || "";
    $("#final-answer").value = diagnosis.final_answer || "";
    $("#prevention-cue").value = diagnosis.prevention_cue || "";
    refreshDiagnosisFields();
    appendGradeCandidate(candidate);
    const canCommit = ["incorrect", "partial"].includes(candidate.verdict);
    $("#grade-summary").textContent = canCommit ? `候选结果：${candidate.verdict === "incorrect" ? "错误" : "部分正确"}；首错：${candidate.first_error}` : candidate.verdict === "correct" ? "候选结果：本题正确，不写入错题本。" : "证据不足，不能写入错题本。";
    $("#grade-confirm").hidden = false;
    $("#commit-grade").hidden = !canCommit;
    $("#next-intake").hidden = canCommit;
    status($("#manual-status"), canCommit ? "请核对候选后确认入本。" : "已安全停止，不会写入正式错题。");
  }

  $("#manual-grade-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!activeAttempt || !activeIntake) return;
    const button = event.submitter;
    button.disabled = true;
    try {
      activeCandidate = await api(`/v1/attempts/${activeAttempt}/manual-grade`, {method: "POST", body: JSON.stringify({input_version: activeIntake.inputVersion, verdict: $("#verdict").value, first_error: $("#first-error").value, cause_code: $("#cause-code").value, evidence: $("#grade-evidence").value, correct_solution: $("#correct-solution").value, final_answer: $("#final-answer").value, prevention_cue: $("#prevention-cue").value})});
      showGradeCandidate(activeCandidate);
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
      const entry = await api(`/v1/grade-results/${activeCandidate.result_id}/commit`, {method: "POST", body: JSON.stringify({input_version: activeCandidate.input_version}), headers: {"Idempotency-Key": crypto.randomUUID()}});
      let message = "已写入错题本并安排首次复习。";
      try {
        const recommendations = await api(`/v1/errors/${entry.error_id}/recommendations`, {method: "POST", headers: {"Idempotency-Key": crypto.randomUUID()}});
        message = `已写入错题本并安排首次复习，已匹配 ${recommendations.items.length} 道已验证练习${recommendations.gap ? "，题量不足部分已标记" : ""}。`;
      } catch (_) {
        message += "练习暂未匹配，可稍后在错题详情中重试。";
      }
      activateNextIntake(message);
    } catch (error) {
      status($("#manual-status"), `入本失败：${authError(error)}`, true);
    } finally {
      event.currentTarget.disabled = false;
    }
  });

  $("#next-intake").addEventListener("click", () => activateNextIntake("本题不会写入错题本。"));

}

function bindErrors() {
  let currentErrorId = null;
  async function showError(id) {
    const [item, recommendations] = await Promise.all([api(`/v1/errors/${id}`), api(`/v1/errors/${id}/recommendations`)]);
    currentErrorId = id;
    const diagnosis = item.diagnosis || {};
    const recommendationHtml = recommendations.items.length ? recommendations.items.map((recommendation, index) => `<li><strong>练习 ${index + 1}</strong><p>${escapeHtml(recommendation.stem_text)}</p><small>${escapeHtml(recommendation.source)} · ${escapeHtml(recommendation.reason)}</small></li>`).join("") : '<li class="empty">还没有匹配练习。</li>';
    $("#error-detail").hidden = false;
    $("#error-detail").innerHTML = `<h2>错题详情</h2><dl class="diagnosis-list"><dt>原题</dt><dd>${escapeHtml(item.question_text)}</dd><dt>你的作答</dt><dd>${escapeHtml(item.answer_text || "未填写")}</dd><dt>第一处实质错误</dt><dd>${escapeHtml(item.first_error || "待整理")}</dd><dt>主要错因</dt><dd>${escapeHtml(causeLabels[diagnosis.cause_code] || "待整理")}</dd><dt>判断依据</dt><dd>${escapeHtml(diagnosis.cause_evidence || "待整理")}</dd><dt>完整正确过程</dt><dd>${escapeHtml(diagnosis.correct_solution || "待整理")}</dd><dt>最终答案</dt><dd>${escapeHtml(diagnosis.final_answer || "待整理")}</dd><dt>防错提示</dt><dd>${escapeHtml(diagnosis.prevention_cue || "待整理")}</dd></dl><h3>已验证练习</h3><ol class="recommendation-list">${recommendationHtml}</ol><div class="actions"><button type="button" data-error-action="recommend" class="ghost">匹配练习</button><button type="button" data-error-action="master" class="ghost">标记已掌握</button><button type="button" data-error-action="remove" class="danger">移除错题</button></div>`;
  }
  async function loadErrors() {
    try {
      const result = await api("/v1/errors");
      $("#all-errors").innerHTML = result.items.map(item => `<li><button class="text-button" data-error-id="${item.error_id}">${escapeHtml(item.question_text)}</button><br><small>${escapeHtml(item.first_error || "待整理错因")} · ${item.status === "mastered" ? "已掌握" : "复习中"}</small></li>`).join("") || '<li class="empty">还没有错题。</li>';
    } catch (error) {
      status($("#page-status"), authError(error), true);
    }
  }
  $("#refresh-errors").addEventListener("click", loadErrors);
  $("#all-errors").addEventListener("click", async event => {
    const id = event.target.dataset.errorId;
    if (!id) return;
    try {
      await showError(id);
    } catch (error) {
      status($("#page-status"), authError(error), true);
    }
  });
  $("#error-detail").addEventListener("click", async event => {
    const action = event.target.dataset.errorAction;
    if (!action || !currentErrorId) return;
    event.target.disabled = true;
    try {
      if (action === "recommend") await api(`/v1/errors/${currentErrorId}/recommendations`, {method: "POST", headers: {"Idempotency-Key": crypto.randomUUID()}});
      else if (action === "master") await api(`/v1/errors/${currentErrorId}/master`, {method: "POST"});
      else if (action === "remove") {
        if (!confirm("移除后将取消这道错题的待复习和未完成推荐，确认继续？")) return;
        await api(`/v1/errors/${currentErrorId}`, {method: "DELETE"});
        currentErrorId = null;
        $("#error-detail").hidden = true;
        await loadErrors();
        return;
      }
      await showError(currentErrorId);
      await loadErrors();
    } catch (error) {
      status($("#page-status"), authError(error), true);
    } finally {
      event.target.disabled = false;
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
      if (dueReview) {
        const practice = dueReview.recommendations.length ? `<h3>同类型练习</h3><ol>${dueReview.recommendations.map(item => `<li>${escapeHtml(item.stem_text)}<br><small>${escapeHtml(item.source)} · ${escapeHtml(item.reason)}</small></li>`).join("")}</ol>` : "";
        $("#review-question").innerHTML = `<p><strong>先遮住解析，独立重做：</strong></p><div class="review-stem">${escapeHtml(dueReview.question_text)}</div><details><summary>需要时查看上次首错</summary><p>${escapeHtml(dueReview.first_error || "待整理")}</p></details>${practice}`;
      } else $("#review-question").textContent = "今天没有到期复习。";
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
  Promise.all([api("/v1/progress"), api("/v1/bank/status")]).then(([result, bank]) => {
    $("#progress-errors").textContent = result.error_count || 0;
    $("#progress-mastered").textContent = result.mastered_count || 0;
    $("#progress-reviews").textContent = result.completed_review_count || 0;
    $("#progress-accuracy").textContent = `${result.review_accuracy_percent || 0}%`;
    $("#progress-due").textContent = result.due_review_count || 0;
    $("#progress-gaps").textContent = result.recommendation_gap_count || 0;
    $("#bank-questions").textContent = bank.question_count || 0;
    $("#bank-recommendable").textContent = bank.recommendable_count || 0;
    $("#bank-candidates").textContent = bank.candidate_count || 0;
  }).catch(error => status($("#page-status"), authError(error), true));
}

function bindSettings() {
  let sensitiveChallenge = null;
  let sensitiveAction = null;
  const settingsStatus = $("#settings-status");
  $("#logout").addEventListener("click", async () => {
    try { await api("/v1/session", {method: "DELETE"}); } finally { location.replace("/login"); }
  });
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
