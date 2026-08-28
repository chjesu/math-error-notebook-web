const $ = selector => document.querySelector(selector);
if (new URLSearchParams(location.search).get("embedded") === "1") document.body.classList.add("is-embedded");
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
    model_unavailable: "本地智能处理暂时不可用，请稍后重试。",
    model_network_error: "智能处理网络连接失败，系统已自动重试；请稍后再次发送。",
    model_rate_limited: "智能处理请求较多，请稍后重试。",
    model_authentication_error: "智能处理登录状态失效，请重新启动本地服务。",
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

function wrapPlainMath(value) {
  if (/\\[([]|\$/.test(value)) return value;
  const runs = /[A-Za-z0-9π√|([][\sA-Za-z0-9π√+\-−*/=^_(),.|\[\]{}⊥∥≠≤≥·×²³₀-₉]*[A-Za-z0-9π√|)\]²³₀-₉]/g;
  return value.replace(runs, raw => {
    const expression = raw.trim();
    if (/^\d+[.)．、]?$/.test(expression) || !/[=+\-−*/^_|√π⊥∥≠≤≥·×²³₀-₉]|[A-Za-z]\([^)]*\)/.test(expression)) return raw;
    const leading = raw.slice(0, raw.indexOf(expression));
    const trailing = raw.slice(raw.indexOf(expression) + expression.length);
    const latex = expression
      .replace(/([A-Za-z])([₀-₉]+)/g, (_, letter, digits) => `${letter}_{${digits.replace(/[₀-₉]/g, digit => "0123456789"["₀₁₂₃₄₅₆₇₈₉".indexOf(digit)])}}`)
      .replace(/²/g, "^{2}")
      .replace(/³/g, "^{3}")
      .replace(/\^(-?\d+)/g, "^{$1}")
      .replace(/√\s*(\([^()]*\)|[A-Za-z0-9]+)/g, "\\sqrt{$1}")
      .replace(/π\s*\/\s*(\d+)/g, "\\frac{\\pi}{$1}")
      .replace(/(\d+)\s*\/\s*(\d+)/g, "\\frac{$1}{$2}")
      .replace(/π/g, "\\pi ")
      .replace(/_{3,}/g, "\\underline{\\qquad}")
      .replace(/≠/g, "\\ne ")
      .replace(/≤/g, "\\le ")
      .replace(/≥/g, "\\ge ")
      .replace(/⊥/g, "\\perp ")
      .replace(/∥/g, "\\parallel ")
      .replace(/·/g, "\\cdot ")
      .replace(/×/g, "\\times ")
      .replace(/−/g, "-");
    return `${leading}\\(${latex}\\)${trailing}`;
  });
}

function renderMath(target) {
  if (!target) return;
  const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    node.nodeValue = node.nodeValue
      .replace(/\\\\([()[\]])/g, "\\$1")
      .replace(/\\\\(?=[A-Za-z])/g, "\\")
      .replace(/^(\s*[A-D][.、．]\s*)\\+\s*$/gm, "$1");
    node.nodeValue = wrapPlainMath(node.nodeValue);
  }
  target.classList.add("math-content");
  if (typeof window.renderMathInElement !== "function") return;
  window.renderMathInElement(target, {
    delimiters: [
      {left: "\\[", right: "\\]", display: true},
      {left: "\\(", right: "\\)", display: false},
      {left: "$$", right: "$$", display: true},
      {left: "$", right: "$", display: false}
    ],
    output: "mathml",
    throwOnError: false,
    strict: "ignore",
    trust: false,
    errorColor: "currentColor"
  });
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
  let stage = "upload";
  let busy = false;
  let uploadFiles = [];
  let pendingIntakes = [];
  let intakeBatch = [];
  let intakeBatchTurn = null;
  let holdScroll = false;
  let historyCursor = null;
  let historyLoading = false;
  let stoppable = false;
  let stopRequested = false;
  let activeProgress = null;
  const conversationTurns = [];
  const renderedCandidates = new Set();
  const renderedAttachmentIds = new Set();
  const uploadInput = $("#file");
  const sendButton = $("#upload-button");
  const chatInput = $("#chat-input");
  const compactButton = $("#compact-conversation");
  const clearConversationButton = $("#clear-conversation");
  const actionGroup = $("#composer-actions");
  const dropZone = $("#drop-zone");
  const uploadSurface = $(".chat-main");
  const allowedExtensions = new Set(["pdf", "png", "jpg", "jpeg", "docx"]);
  const mimeExtensions = new Map([
    ["application/pdf", "pdf"],
    ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"],
    ["image/jpeg", "jpg"],
    ["image/png", "png"],
  ]);
  const retryableUploadStates = new Set(["queued", "failed", "processing_failed", "recognition_failed"]);
  const maxFileBytes = 25 * 1024 * 1024;
  const nextCommands = new Set(["下一题", "处理下一个", "跳过"]);
  const previewDialog = $("#image-preview-dialog");
  const previewContent = $("#image-preview-content");
  let composerAction = "ask";

  function selectedComposerAction() {
    return composerAction;
  }

  function selectComposerAction(value) {
    composerAction = value;
  }

  function openImagePreview(url, name) {
    if (!url || !previewDialog || !previewContent) return;
    previewContent.src = url;
    previewContent.alt = name ? `${name} 原图` : "上传原图";
    previewDialog.showModal();
  }

  function imagePreviewButton(item) {
    const button = document.createElement("button");
    const image = document.createElement("img");
    button.type = "button";
    button.className = "image-preview-trigger";
    button.dataset.previewUrl = item.previewUrl;
    button.dataset.previewName = item.name;
    button.setAttribute("aria-label", `查看 ${item.name} 原图`);
    image.src = item.previewUrl;
    image.alt = "";
    button.append(image);
    return button;
  }

  function scrollChatToEnd() {
    if (holdScroll) return;
    requestAnimationFrame(() => { $("#chat-thread").scrollTop = $("#chat-thread").scrollHeight; });
  }

  function focusChatInput() {
    chatInput.focus({preventScroll: holdScroll});
  }

  function renderConversationWindow() {
    $("#chat-stream").replaceChildren(...conversationTurns);
    $("#history-pagination").hidden = !historyCursor;
    $("#load-older").disabled = historyLoading;
    $("#load-older").textContent = historyLoading ? "正在加载" : "加载更早";
  }

  function appendTurn(turn) {
    conversationTurns.push(turn);
    renderConversationWindow();
    $(".chat-welcome").hidden = true;
    scrollChatToEnd();
  }

  function assistantTurn(message, error = false) {
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
    renderMath(response);
    turn.append(avatar, response);
    appendTurn(turn);
    return turn;
  }

  function userTurn(message) {
    const turn = document.createElement("div");
    const bubble = document.createElement("div");
    turn.className = "chat-turn user-turn";
    bubble.className = "chat-user-message";
    bubble.textContent = message;
    renderMath(bubble);
    turn.append(bubble);
    appendTurn(turn);
    return turn;
  }

  function progressTurn() {
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
    disclosure.append(summary, steps);
    response.append(disclosure);
    turn.append(avatar, response);
    appendTurn(turn);
    return {turn, disclosure, summary, steps, title: "", current: null, detail: null};
  }

  function setProgress(progress, title, detail, state = "running") {
    progress.turn.className = `chat-turn assistant-turn chat-progress is-${state}`;
    if (progress.title !== title) {
      progress.current?.classList.replace("is-active", "is-done");
      const step = document.createElement("li");
      const dot = document.createElement("span");
      const content = document.createElement("div");
      const heading = document.createElement("strong");
      const description = document.createElement("p");
      step.className = "is-active";
      dot.className = "progress-indicator";
      heading.textContent = title;
      content.append(heading, description);
      step.append(dot, content);
      progress.steps.append(step);
      progress.title = title;
      progress.current = step;
      progress.detail = description;
    }
    progress.detail.textContent = detail;
    progress.summary.textContent = state === "running" ? title : `${title} · ${detail}`;
    if (state !== "running") {
      progress.current.className = `is-${state}`;
      progress.disclosure.open = state === "error";
    }
    scrollChatToEnd();
  }

  function appendCandidate(candidate) {
    if (!candidate?.result_id || renderedCandidates.has(candidate.result_id)) return;
    renderedCandidates.add(candidate.result_id);
    const diagnosis = candidate.diagnosis || {};
    const turn = document.createElement("div");
    const avatar = document.createElement("img");
    const response = document.createElement("div");
    const heading = document.createElement("strong");
    const list = document.createElement("dl");
    const verdict = {incorrect: "错误", partial: "部分正确", correct: "正确", unclear: "证据不足"}[candidate.verdict] || candidate.verdict;
    turn.className = "chat-turn assistant-turn";
    avatar.className = "chat-avatar";
    avatar.src = "/assets/branding/logo-symbol-color-64-v1.png";
    avatar.alt = "";
    response.className = "chat-response chat-candidate";
    heading.textContent = `错题解析 · ${verdict}`;
    const cause = [
      candidate.first_error && `第一处实质错误：${candidate.first_error}`,
      diagnosis.cause_code && `主要错因：${causeLabels[diagnosis.cause_code] || diagnosis.cause_code}`,
      diagnosis.cause_evidence && `分析与点评：${diagnosis.cause_evidence}`,
    ].filter(Boolean).join("\n");
    const final = [diagnosis.final_answer, diagnosis.prevention_cue && `（小建议：${diagnosis.prevention_cue}）`].filter(Boolean).join("\n\n");
    for (const [label, value] of [
      ["1. 题目整理", activeIntake?.questionText],
      ["2. 学生作答还原", activeIntake?.answerText || "未识别或未作答"],
      ["3. 错因分析与点评", cause],
      ["4. 知识点梳理", diagnosis.knowledge_points?.join("\n")],
      ["5. 详细解析", diagnosis.correct_solution],
      ["6. 最终答案", final],
    ]) {
      if (!value) continue;
      const term = document.createElement("dt");
      const detail = document.createElement("dd");
      term.textContent = label;
      detail.textContent = value;
      renderMath(detail);
      list.append(term, detail);
    }
    response.append(heading, list);
    turn.append(avatar, response);
    appendTurn(turn);
  }

  function appendIntake(intake) {
    if (!intakeBatch.includes(intake)) intakeBatch.push(intake);
    renderIntakeBatch();
  }

  function renderIntakeBatch() {
    if (!intakeBatch.length) return;
    const initialRender = !intakeBatchTurn;
    if (!intakeBatchTurn) {
      intakeBatchTurn = document.createElement("div");
      const avatar = document.createElement("img");
      const response = document.createElement("div");
      intakeBatchTurn.className = "chat-turn assistant-turn intake-batch-turn";
      avatar.className = "chat-avatar";
      avatar.src = "/assets/branding/logo-symbol-color-64-v1.png";
      avatar.alt = "";
      response.className = "chat-response intake-batch";
      intakeBatchTurn.append(avatar, response);
      appendTurn(intakeBatchTurn);
    }
    const response = intakeBatchTurn.querySelector(".intake-batch");
    const heading = document.createElement("div");
    const title = document.createElement("strong");
    const hint = document.createElement("p");
    const list = document.createElement("div");
    heading.className = "intake-batch-heading";
    title.textContent = `识别结果 · 共 ${intakeBatch.length} 道题`;
    hint.textContent = "系统会按顺序自动判题并整理错题；识别不清时可在输入框直接补充或修正。";
    list.className = "recognized-question-list";
    heading.append(title, hint);
    let activeCard = null;
    for (const [index, intake] of intakeBatch.entries()) {
      const card = document.createElement("article");
      const cardHeading = document.createElement("header");
      const cardTitle = document.createElement("strong");
      const badge = document.createElement("span");
      const question = document.createElement("section");
      const answer = document.createElement("section");
      const stateLabels = {current: "等待自动处理", grading: "正在判题", needs_input: "需要补充", correct: "答案正确", saved: "已收入错题本", skipped: "已跳过"};
      const state = intake === activeIntake ? (intake.uiState || (stage === "grade" ? "graded" : "current")) : (intake.uiState || "waiting");
      card.className = `recognized-question-card is-${state}`;
      card.dataset.intakeId = intake.intakeId;
      if (intake === activeIntake) activeCard = card;
      cardTitle.textContent = `题目 ${index + 1}`;
      badge.className = "question-state";
      badge.textContent = stateLabels[state] || "待处理";
      cardHeading.append(cardTitle, badge);
      question.innerHTML = `<strong>题干</strong><p></p>`;
      question.querySelector("p").textContent = intake.questionText || "尚未识别，请在输入框补充题干。";
      answer.innerHTML = `<strong>识别作答</strong><p></p>`;
      answer.querySelector("p").textContent = intake.answerText || "未识别或未作答";
      card.append(cardHeading, question, answer);
      renderMath(card);
      list.append(card);
    }
    response.replaceChildren(heading, list);
    if (initialRender) requestAnimationFrame(() => activeCard?.scrollIntoView({block: "nearest"}));
  }

  function setComposerState() {
    const retryable = uploadFiles.some(item => !item.submitted && retryableUploadStates.has(item.state));
    const actions = stage === "grade" && activeCandidate?.verdict === "unclear" ? [["next", "跳过本题"]] : [];
    const signature = actions.map(([value, label]) => `${value}:${label}`).join("|");
    if (actionGroup.dataset.signature !== signature) {
      actionGroup.replaceChildren(...actions.map(([value, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `composer-action${value === "commit" ? " is-primary" : ""}`;
        button.dataset.composerAction = value;
        button.textContent = label;
        return button;
      }));
      actionGroup.dataset.signature = signature;
    }
    chatInput.disabled = false;
    actionGroup.querySelectorAll("button").forEach(button => { button.disabled = busy; });
    compactButton.hidden = !activeIntake;
    compactButton.disabled = busy;
    chatInput.placeholder = stage === "upload"
      ? "输入消息，或添加图片、PDF、DOCX"
      : stage === "intake"
        ? "系统正在自动处理；也可以输入补充或修正"
        : "继续追问判题依据，或输入需要修正的内容";
    sendButton.classList.toggle("is-stop", stoppable);
    sendButton.setAttribute("aria-label", stoppable ? "停止处理" : "发送");
    if (stoppable) {
      sendButton.disabled = stopRequested;
      sendButton.textContent = stopRequested ? "…" : "■";
    } else {
      sendButton.disabled = busy || (!retryable && !chatInput.value.trim());
      sendButton.textContent = retryable && uploadFiles.some(item => ["failed", "processing_failed", "recognition_failed"].includes(item.state)) ? "↻" : "↑";
    }
  }

  function renderUploadFiles() {
    const labels = {queued: "等待上传", uploading: "上传中", processing: "处理中", done: "处理完成", failed: "上传失败", processing_failed: "建会话失败", recognition_failed: "识别失败"};
    const cards = uploadFiles.filter(item => !item.submitted).map(item => {
      const card = document.createElement("li");
      const preview = document.createElement("div");
      const name = document.createElement("small");
      const state = document.createElement("span");
      card.className = `upload-thumbnail is-${item.state}`;
      preview.className = "upload-preview";
      if (item.previewUrl) {
        preview.append(imagePreviewButton(item));
      } else {
        const type = document.createElement("strong"); type.textContent = item.extension.toUpperCase(); preview.append(type);
      }
      state.className = "upload-state";
      state.textContent = item.state === "uploading" ? `${item.progress}%` : labels[item.state];
      preview.append(state);
      if (retryableUploadStates.has(item.state)) {
        const remove = document.createElement("button"); remove.type = "button"; remove.dataset.removeFile = item.id; remove.textContent = "×"; preview.append(remove);
      }
      name.textContent = item.name;
      card.append(preview, name);
      return card;
    });
    $("#upload-file-list").replaceChildren(...cards);
    setComposerState();
  }

  function addUploadFiles(files) {
    const rejected = [];
    let duplicates = 0;
    for (const [index, file] of Array.from(files).entries()) {
      const originalName = String(file.name || "").trim();
      const nameExtension = originalName.includes(".") ? originalName.split(".").pop().toLowerCase() : "";
      const extension = nameExtension || mimeExtensions.get(String(file.type || "").toLowerCase()) || "";
      const name = originalName || `粘贴图片-${index + 1}.${extension || "bin"}`;
      if (!allowedExtensions.has(extension)) rejected.push(`${name}：格式不支持`);
      else if (file.size > maxFileBytes) rejected.push(`${name}：超过 25 MB`);
      else if (uploadFiles.some(item => item.name.toLowerCase() === name.toLowerCase() && item.file.size === file.size && item.file.lastModified === file.lastModified)) duplicates += 1;
      else uploadFiles.push({id: crypto.randomUUID(), file, name, extension, previewUrl: ["png", "jpg", "jpeg"].includes(extension) ? URL.createObjectURL(file) : "", state: "queued", progress: 0, error: "", submitted: false});
    }
    renderUploadFiles();
    const notice = [rejected.join("；"), duplicates ? `已忽略 ${duplicates} 个重复文件` : ""].filter(Boolean).join("；");
    status($("#upload-status"), notice, Boolean(rejected.length));
  }

  function uploadFile(item, onProgress) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("purpose", "question_image");
      form.append("file", item.file, item.name);
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
        let value = {}; try { value = JSON.parse(xhr.responseText); } catch (_) {}
        if (xhr.status >= 200 && xhr.status < 300) resolve(value);
        else { const error = new Error(value.error?.code || "temporarily_unavailable"); error.status = xhr.status; reject(error); }
      });
      xhr.addEventListener("error", () => reject(new Error("network_error")));
      xhr.send(form);
    });
  }

  function appendUserUpload(files) {
    const freshFiles = files.map(item => {
      const name = String(item.name || "附件");
      const extension = String(item.extension || (name.includes(".") ? name.split(".").pop() : "file")).toLowerCase();
      return {
        ...item,
        name,
        extension,
        previewUrl: item.previewUrl || item.preview_url || "",
        attachmentId: item.attachmentId || item.attachment_id || "",
      };
    }).filter(item => !item.attachmentId || !renderedAttachmentIds.has(item.attachmentId));
    if (!freshFiles.length) return;
    freshFiles.forEach(item => { if (item.attachmentId) renderedAttachmentIds.add(item.attachmentId); });
    const turn = document.createElement("div");
    const bubble = document.createElement("div");
    const grid = document.createElement("div");
    const label = document.createElement("p");
    turn.className = "chat-turn user-turn";
    bubble.className = "chat-upload-bubble";
    grid.className = "chat-upload-grid";
    for (const item of freshFiles) {
      const card = document.createElement("div");
      const preview = document.createElement("div");
      const name = document.createElement("small");
      card.className = "chat-upload-thumbnail";
      preview.className = "chat-upload-preview";
      if (item.previewUrl) preview.append(imagePreviewButton(item));
      else { const type = document.createElement("strong"); type.textContent = item.extension.toUpperCase(); preview.append(type); }
      name.textContent = item.name; card.append(preview, name); grid.append(card);
    }
    label.textContent = `请整理这 ${freshFiles.length} 个文件`;
    bubble.append(grid, label); turn.append(bubble); appendTurn(turn);
  }

  function activateNextIntake(message = "") {
    if (message) assistantTurn(message);
    activeIntake = pendingIntakes.shift() || null;
    activeAttempt = null;
    activeCandidate = null;
    stage = activeIntake ? "intake" : "upload";
    if (activeIntake) {
      activeIntake.uiState = "current";
      appendIntake(activeIntake);
      setTimeout(processActiveIntake, 0);
    } else renderIntakeBatch();
    setComposerState();
    if (!chatInput.disabled) focusChatInput();
  }

  async function uploadQueued() {
    const files = uploadFiles.filter(item => !item.submitted && retryableUploadStates.has(item.state));
    if (!files.length || busy) return;
    busy = true;
    files.forEach(item => { item.submitted = true; });
    renderUploadFiles();
    appendUserUpload(files);
    const progress = progressTurn();
    let completed = 0;
    let recognized = 0;
    let failed = 0;
    for (const [index, item] of files.entries()) {
      item.state = "uploading";
      setProgress(progress, "正在上传附件", `${index + 1}/${files.length} · ${item.name} · 0%`);
      try {
        if (!item.fileId) {
          const uploaded = await uploadFile(item, percent => setProgress(progress, "正在上传附件", `${index + 1}/${files.length} · ${item.name} · ${percent}%`));
          item.fileId = uploaded.file_id;
          item.progress = 100;
        }
        if (!item.intakeId) {
          item.state = "processing";
          setProgress(progress, "正在建立错题会话", `${index + 1}/${files.length} · ${item.name}`);
          const task = await api("/v1/intakes", {method: "POST", body: JSON.stringify({file_id: item.fileId}), headers: {"Idempotency-Key": crypto.randomUUID()}});
          item.intakeId = task.resource_id;
        }
        setProgress(progress, "正在识别题目与作答", `${index + 1}/${files.length} · ${item.name}`);
        const candidate = await api(`/v1/intakes/${item.intakeId}/model-candidate`, {method: "POST", body: JSON.stringify({refresh: true})});
        const extractedItems = Array.isArray(candidate.items) && candidate.items.length ? candidate.items : [candidate];
        for (const extracted of extractedItems) {
          const intake = {
            intakeId: extracted.intake_id || item.intakeId,
            itemNo: extracted.item_no || 1,
            inputVersion: extracted.input_version || 1,
            status: extracted.status || "extracting",
            fileName: extractedItems.length > 1 ? `${item.name} · 第 ${extracted.item_no || 1} 题` : item.name,
            questionText: extracted.question_text || "",
            answerText: extracted.answer_text || "",
          };
          pendingIntakes.push(intake);
          intakeBatch.push(intake);
        }
        recognized += extractedItems.filter(extracted => extracted.question_text).length;
        item.state = "done";
        completed += 1;
      } catch (error) {
        item.state = item.intakeId ? "recognition_failed" : item.fileId ? "processing_failed" : "failed";
        item.error = authError(error);
        item.submitted = false;
        failed += 1;
      }
    }
    busy = false;
    pendingIntakes.forEach((intake, index) => {
      intake.queueIndex = index + 1;
      intake.queueTotal = pendingIntakes.length;
    });
    renderUploadFiles();
    if (completed) {
      setProgress(progress, "文件已准备好", `已从 ${completed} 个文件识别 ${recognized} 道题${failed ? `，${failed} 个文件可重试` : ""}。`, failed ? "warning" : "complete");
      if (!activeIntake) activateNextIntake();
    } else {
      const firstFailure = files.find(item => ["failed", "processing_failed", "recognition_failed"].includes(item.state));
      if (firstFailure?.state === "recognition_failed") {
        setProgress(progress, "题目识别未完成", `${firstFailure.error} 文件已经保存，可直接重试识别，不会重复上传。`, "error");
      } else if (firstFailure?.state === "processing_failed") {
        setProgress(progress, "错题会话创建失败", `${firstFailure.error} 文件已经上传，可直接重试。`, "error");
      } else {
        setProgress(progress, "附件上传失败", `${firstFailure?.error || "请稍后重试。"} 文件仍保留在输入框中。`, "error");
      }
    }
  }

  async function restorePendingIntakes() {
    try {
      const result = await api("/v1/intakes");
      if (activeIntake || pendingIntakes.length || !Array.isArray(result.items)) return;
      appendUserUpload(result.items.map(item => item.attachment).filter(Boolean));
      const interrupted = result.items.filter(item => item.status === "extracting" && item.attachment);
      for (const item of interrupted) {
        if (uploadFiles.some(file => file.fileId === item.attachment.attachment_id)) continue;
        const name = item.attachment.name;
        uploadFiles.push({
          id: crypto.randomUUID(), file: null, fileId: item.attachment.attachment_id, intakeId: item.intake_id,
          name, extension: name.includes(".") ? name.split(".").pop().toLowerCase() : "file",
          previewUrl: item.attachment.preview_url, state: "recognition_failed", progress: 100,
          error: "上次识别未完成", submitted: false,
        });
      }
      pendingIntakes = result.items.filter(item => item.status !== "extracting").map(item => ({
        intakeId: item.intake_id, itemNo: item.item_no, inputVersion: item.input_version,
        status: item.status, fileName: `待确认题目 ${item.item_no}`,
        questionText: item.question_text || "", answerText: item.answer_text || "",
      }));
      intakeBatch = [...pendingIntakes];
      pendingIntakes.forEach((intake, index) => {
        intake.queueIndex = index + 1;
        intake.queueTotal = pendingIntakes.length;
      });
      renderUploadFiles();
      if (interrupted.length) assistantTurn("已恢复上次未完成的图片，点击重试即可继续识别。", true);
      if (pendingIntakes.length) activateNextIntake("已恢复上次尚未处理完的题目。" );
    } catch (_) {}
  }

  async function restoreConversationHistory() {
    try {
      const result = await api("/v1/conversations/latest/messages");
      historyCursor = typeof result.next_cursor === "string" ? result.next_cursor : null;
      appendHistoryItems(result.items);
    } catch (error) {
      status($("#upload-status"), `历史会话暂时无法加载：${authError(error)}`, true);
    }
  }

  function appendHistoryItems(items, prepend = false) {
    if (!Array.isArray(items)) return;
    const before = conversationTurns.length;
    for (const item of items) {
      if (!item) continue;
      const attachments = Array.isArray(item.attachments) ? item.attachments : [];
      if (attachments.length) appendUserUpload(attachments);
      if (attachments.length && item.role === "user") continue;
      if (typeof item.text !== "string" || !item.text.trim()) continue;
      if (item.role === "user") userTurn(item.text);
      else if (item.role === "assistant") assistantTurn(item.text);
    }
    if (prepend) {
      const added = conversationTurns.splice(before);
      conversationTurns.unshift(...added);
      renderConversationWindow();
    }
  }

  async function loadOlderHistory() {
    if (!historyCursor || historyLoading) return;
    const thread = $("#chat-thread");
    const height = thread.scrollHeight;
    historyLoading = true;
    renderConversationWindow();
    try {
      const result = await api(`/v1/conversations/latest/messages?cursor=${encodeURIComponent(historyCursor)}`);
      historyCursor = typeof result.next_cursor === "string" ? result.next_cursor : null;
      appendHistoryItems(result.items, true);
      requestAnimationFrame(() => { thread.scrollTop += thread.scrollHeight - height; });
    } catch (error) {
      status($("#upload-status"), `更早的会话暂时无法加载：${authError(error)}`, true);
    } finally {
      historyLoading = false;
      renderConversationWindow();
    }
  }

  async function restoreWorkbench() {
    await restoreConversationHistory();
    await restorePendingIntakes();
  }

  clearConversationButton?.addEventListener("click", async () => {
    if (!window.confirm("确定清空当前会话吗？未完成的题目和上传附件会从工作台移除；已经入本的错题不受影响。")) return;
    clearConversationButton.disabled = true;
    try {
      await api("/v1/conversations/latest", {method: "DELETE"});
      location.reload();
    } catch (error) {
      clearConversationButton.disabled = false;
      status($("#upload-status"), `清空会话失败：${authError(error)}`, true);
    }
  });

  async function confirmAndGrade() {
    if (!activeAttempt && (!activeIntake?.questionText || !["waiting_confirmation", "confirmed"].includes(activeIntake.status))) {
      activeIntake.uiState = "needs_input";
      assistantTurn("这道题的题干还不完整，请直接补充题干、作答或需要修正的内容。", true);
      return;
    }
    const progress = progressTurn();
    activeIntake.uiState = "grading";
    renderIntakeBatch();
    if (!activeAttempt) {
      setProgress(progress, "正在固定识别结果", "锁定当前版本，准备自动判题。" );
      const confirmed = await api(`/v1/intakes/${activeIntake.intakeId}/confirm`, {method: "POST", body: JSON.stringify({input_version: activeIntake.inputVersion}), headers: {"Idempotency-Key": crypto.randomUUID()}});
      activeAttempt = confirmed.resource_id;
    }
    stage = "grade";
    setComposerState();
    setProgress(progress, "正在判题", "定位第一处实质错误并生成完整解法。" );
    activeCandidate = await api(`/v1/attempts/${activeAttempt}/model-grade`, {method: "POST", body: JSON.stringify({input_version: activeIntake.inputVersion})});
    activeIntake.automaticRetries = 0;
    appendCandidate(activeCandidate);
    if (activeCandidate.verdict === "correct") {
      activeIntake.uiState = "correct";
      renderIntakeBatch();
      setProgress(progress, "判题候选已生成", "本题正确，无需入本，正在继续下一题。", "complete");
      activateNextIntake("错题本记录检查：本题判定正确，未计入错题本。" );
      return;
    }
    if (["incorrect", "partial"].includes(activeCandidate.verdict)) {
      setProgress(progress, "正在整理错题", "判题已完成，正在写入错题本并安排复习。" );
      await commitCurrent();
      setProgress(progress, "本题已处理", "已自动写入错题本，并继续处理下一题。", "complete");
      return;
    }
    activeIntake.uiState = "needs_input";
    renderIntakeBatch();
    setProgress(progress, "需要补充信息", "当前证据不足，未写入错题本。请在输入框补充或修正，也可以跳过本题。", "warning");
  }

  async function processActiveIntake() {
    if (busy || !activeIntake || (stage !== "intake" && !(stage === "grade" && activeAttempt && !activeCandidate))) return;
    busy = true;
    setComposerState();
    let retryAutomatically = false;
    try {
      await confirmAndGrade();
    } catch (error) {
      retryAutomatically = ["model_network_error", "model_rate_limited"].includes(error.message)
        && Boolean(activeAttempt) && (activeIntake.automaticRetries || 0) < 1;
      if (retryAutomatically) {
        activeIntake.automaticRetries = (activeIntake.automaticRetries || 0) + 1;
        activeIntake.uiState = "grading";
        assistantTurn("网络连接出现波动，题目已保留，3 秒后自动续跑。", true);
      } else {
        activeIntake.uiState = "needs_input";
        assistantTurn(`本题自动处理未完成：${authError(error)} 题目已保留，可补充信息后重试。`, true);
      }
    } finally {
      busy = false;
      renderIntakeBatch();
      setComposerState();
      if (!chatInput.disabled) focusChatInput();
      if (retryAutomatically) setTimeout(processActiveIntake, 3000);
    }
  }

  async function commitCurrent() {
    if (!activeCandidate || !["incorrect", "partial"].includes(activeCandidate.verdict)) {
      assistantTurn("当前结果不能写入错题本。可以继续追问，或发送“下一题”。", true);
      return;
    }
    const entry = await api(`/v1/grade-results/${activeCandidate.result_id}/commit`, {method: "POST", body: JSON.stringify({input_version: activeCandidate.input_version}), headers: {"Idempotency-Key": crypto.randomUUID()}});
    const knowledgeCount = entry.diagnosis?.knowledge_points?.length || 0;
    let message = `错题本记录检查：已计入错题本，错因分析和 ${knowledgeCount} 个知识点已保存，并已安排首次复习。`;
    try {
      const recommendations = await api(`/v1/errors/${entry.error_id}/recommendations`, {method: "POST", headers: {"Idempotency-Key": crypto.randomUUID()}});
      message = `错题本记录检查：已计入错题本，错因分析和 ${knowledgeCount} 个知识点已保存；已安排首次复习，并匹配 ${recommendations.items.length} 道已验证练习。`;
    } catch (_) {}
    activeIntake.uiState = "saved";
    renderIntakeBatch();
    activateNextIntake(message);
  }

  async function chatTurn(message, progress) {
    let response;
    try {
      response = await fetch(`/v1/intakes/${activeIntake.intakeId}/chat-turn-stream`, {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-Device-ID": deviceId},
        body: JSON.stringify({message, stage, input_version: activeIntake.inputVersion, attempt_id: activeAttempt, candidate_id: activeCandidate?.result_id || null}),
      });
    } catch {
      throw new Error("network_error");
    }
    if (!response.ok || !response.body) throw new Error("model_unavailable");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result = null;
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line) continue;
        const event = JSON.parse(line);
        if (event.type === "error") throw new Error(event.error?.code || "model_unavailable");
        if (event.type === "result") result = event.data;
        if (event.type !== "runtime") continue;
        const type = event.event?.type;
        if (type === "request_started") setProgress(progress, "正在连接会话", "正在恢复这道题的完整上下文。");
        else if (type === "turn_started") setProgress(progress, "正在理解你的消息", "已进入同一个错题会话回合。");
        else if (type === "item_started") setProgress(progress, "正在分析题目", "正在结合题干、作答和已有判题信息推理。");
        else if (type === "agent_message_delta") setProgress(progress, "正在组织回复", "模型正在生成本轮候选结果。");
        else if (type === "thread_compacted") setProgress(progress, "正在整理上下文", "会话已压缩，关键题目信息会继续保留。");
      }
      if (done) break;
    }
    if (!result) throw new Error("model_unavailable");
    assistantTurn(result.assistant_message);
    if (result.intake) {
      activeIntake.inputVersion = result.intake.input_version;
      activeIntake.status = result.intake.status;
      activeIntake.questionText = result.intake.question_text || "";
      activeIntake.answerText = result.intake.answer_text || "";
      if (result.action === "revise_intake") appendIntake(activeIntake);
    }
    if (result.candidate) {
      activeCandidate = result.candidate;
      appendCandidate(activeCandidate);
    }
    return result;
  }

  async function stopActiveTurn() {
    if (!stoppable || stopRequested || !activeIntake) return;
    stopRequested = true;
    setComposerState();
    if (activeProgress) setProgress(activeProgress, "正在停止", "已向当前会话发送停止请求。" );
    try {
      await api(`/v1/intakes/${activeIntake.intakeId}/conversation/stop`, {method: "POST", body: "{}"});
    } catch (error) {
      stopRequested = false;
      if (activeProgress) setProgress(activeProgress, "停止请求未送达", authError(error), "error");
      setComposerState();
    }
  }

  async function compactConversation() {
    if (!activeIntake || busy) return;
    busy = true;
    setComposerState();
    const progress = progressTurn();
    setProgress(progress, "正在整理上下文", "保留题目、作答和关键结论，压缩较早的会话内容。" );
    try {
      await api(`/v1/intakes/${activeIntake.intakeId}/conversation/compact`, {method: "POST", body: "{}"});
      setProgress(progress, "上下文已整理", "后续追问会继续沿用当前错题会话。", "complete");
    } catch (error) {
      setProgress(progress, "上下文整理失败", authError(error), "error");
    } finally {
      busy = false;
      setComposerState();
    }
  }

  async function sendMessage(preserveScroll = false) {
    const selectedAction = selectedComposerAction();
    const fixedMessages = {next: "下一题"};
    const message = fixedMessages[selectedAction] || chatInput.value.trim();
    if (!message || busy) return;
    const previousHoldScroll = holdScroll;
    if (preserveScroll) holdScroll = true;
    if (selectedAction === "ask") {
      chatInput.value = "";
      chatInput.style.height = "auto";
    }
    userTurn(message);
    if (!activeIntake) {
      assistantTurn("请先添加题目图片、PDF 或 DOCX，我才能结合题目继续处理。");
      setComposerState();
      chatInput.focus();
      return;
    }
    busy = true;
    setComposerState();
    let progress = null;
    let resumeAutomaticIntake = false;
    try {
      if (stage === "intake" && nextCommands.has(message)) { activeIntake.uiState = "skipped"; renderIntakeBatch(); activateNextIntake("已跳过本题。" ); }
      else if (stage === "grade" && nextCommands.has(message)) { activeIntake.uiState = "skipped"; renderIntakeBatch(); activateNextIntake("本题未写入错题本，继续处理下一份。" ); }
      else {
        progress = progressTurn();
        activeProgress = progress;
        setProgress(progress, "正在思考", stage === "intake" ? "结合图片和当前题干理解你的修正。" : "结合当前判题候选理解你的问题。" );
        stoppable = true;
        stopRequested = false;
        setComposerState();
        const result = await chatTurn(message, progress);
        resumeAutomaticIntake = stage === "intake" && result.intake?.status === "waiting_confirmation";
        setProgress(progress, "本轮已完成", "会话上下文已保留，可以继续输入。", "complete");
      }
      selectComposerAction("ask");
    } catch (error) {
      if (error.message === "model_interrupted") {
        if (progress) setProgress(progress, "本轮已停止", "没有生成候选，也没有触发写库。", "warning");
      } else {
        if (progress) setProgress(progress, "本轮未完成", authError(error), "error");
        assistantTurn(`本轮未完成：${authError(error)}。你的消息没有触发写库，可以重试。`, true);
      }
    } finally {
      stoppable = false;
      stopRequested = false;
      activeProgress = null;
      busy = false;
      renderIntakeBatch();
      setComposerState();
      if (!chatInput.disabled) focusChatInput();
      holdScroll = previousHoldScroll;
      if (resumeAutomaticIntake) setTimeout(processActiveIntake, 0);
    }
  }

  $("#load-older").addEventListener("click", loadOlderHistory);
  compactButton.addEventListener("click", compactConversation);
  uploadInput.addEventListener("change", () => { addUploadFiles(uploadInput.files); uploadInput.value = ""; });
  dropZone.addEventListener("click", event => { if (!event.target.closest("button, textarea, input, label")) chatInput.focus(); });
  actionGroup.addEventListener("click", event => {
    const button = event.target.closest("[data-composer-action]");
    if (!button || busy) return;
    selectComposerAction(button.dataset.composerAction);
    sendMessage(true);
  });
  chatInput.addEventListener("input", () => {
    selectComposerAction("ask");
    chatInput.style.height = "auto";
    chatInput.style.height = `${Math.min(chatInput.scrollHeight, 150)}px`;
    setComposerState();
  });
  chatInput.addEventListener("keydown", event => {
    if (event.isComposing || event.keyCode === 229) return;
    if (stoppable) return;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!event.repeat) $("#upload-form").requestSubmit();
    }
  });
  $("#upload-file-list").addEventListener("click", event => {
    const opener = event.target.closest("[data-preview-url]");
    if (opener) {
      openImagePreview(opener.dataset.previewUrl, opener.dataset.previewName);
      return;
    }
    const id = event.target.dataset.removeFile;
    if (!id) return;
    const item = uploadFiles.find(value => value.id === id);
    if (item?.previewUrl) URL.revokeObjectURL(item.previewUrl);
    uploadFiles = uploadFiles.filter(value => value.id !== id);
    renderUploadFiles();
  });
  $("#chat-stream").addEventListener("click", event => {
    const opener = event.target.closest("[data-preview-url]");
    if (opener) return openImagePreview(opener.dataset.previewUrl, opener.dataset.previewName);
  });
  previewDialog?.addEventListener("click", event => { if (event.target === previewDialog) previewDialog.close(); });
  previewDialog?.addEventListener("close", () => { previewContent.removeAttribute("src"); });
  function hasTransferredFiles(transfer) {
    return Array.from(transfer?.types || []).includes("Files") || Array.from(transfer?.items || []).some(item => item.kind === "file");
  }

  function transferredFiles(transfer) {
    const files = Array.from(transfer?.files || []);
    if (files.length) return files;
    return Array.from(transfer?.items || []).filter(item => item.kind === "file").map(item => item.getAsFile?.()).filter(Boolean);
  }

  function isInsideUploadSurface(event) {
    return event.target instanceof Node && uploadSurface.contains(event.target);
  }

  async function clipboardFiles() {
    if (!navigator.clipboard?.read) return [];
    const files = [];
    try {
      for (const item of await navigator.clipboard.read()) {
        for (const type of item.types) {
          const extension = mimeExtensions.get(type.toLowerCase());
          if (!extension) continue;
          const blob = await item.getType(type);
          files.push(new File([blob], `粘贴图片-${files.length + 1}.${extension}`, {type}));
        }
      }
    } catch (_error) {
      return [];
    }
    return files;
  }

  let pasteHandledAt = 0;
  let clipboardReadPending = false;
  async function addClipboardFiles() {
    if (clipboardReadPending) return false;
    clipboardReadPending = true;
    try {
      const files = await clipboardFiles();
      if (!files.length) return false;
      pasteHandledAt = performance.now();
      addUploadFiles(files);
      return true;
    } finally {
      clipboardReadPending = false;
    }
  }

  window.addEventListener("dragenter", event => { if (isInsideUploadSurface(event) && hasTransferredFiles(event.dataTransfer)) { event.preventDefault(); uploadSurface.classList.add("drag-active"); } }, true);
  window.addEventListener("dragover", event => { if (isInsideUploadSurface(event) && hasTransferredFiles(event.dataTransfer)) { event.preventDefault(); uploadSurface.classList.add("drag-active"); } }, true);
  window.addEventListener("dragleave", event => { if (!event.relatedTarget || !uploadSurface.contains(event.relatedTarget)) uploadSurface.classList.remove("drag-active"); }, true);
  window.addEventListener("drop", event => {
    if (!isInsideUploadSurface(event) || !hasTransferredFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.stopPropagation();
    uploadSurface.classList.remove("drag-active");
    addUploadFiles(transferredFiles(event.dataTransfer));
  }, true);
  window.addEventListener("paste", async event => {
    const files = transferredFiles(event.clipboardData);
    if (files.length) {
      event.preventDefault();
      pasteHandledAt = performance.now();
      addUploadFiles(files);
      return;
    }
    if (await addClipboardFiles()) event.preventDefault();
  }, true);
  window.addEventListener("keydown", event => {
    if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "v") return;
    setTimeout(async () => {
      if (performance.now() - pasteHandledAt < 250) return;
      await addClipboardFiles();
    }, 0);
  }, true);
  $("#upload-form").addEventListener("submit", event => {
    event.preventDefault();
    if (stoppable) stopActiveTurn();
    else uploadFiles.some(item => !item.submitted && retryableUploadStates.has(item.state)) ? uploadQueued() : sendMessage();
  });
  window.addEventListener("beforeunload", () => uploadFiles.forEach(item => item.previewUrl && URL.revokeObjectURL(item.previewUrl)));
  setComposerState();
  restoreWorkbench();
}

function bindErrors() {
  let currentErrorId = null;
  async function showError(id) {
    const [item, recommendations] = await Promise.all([api(`/v1/errors/${id}`), api(`/v1/errors/${id}/recommendations`)]);
    currentErrorId = id;
    const diagnosis = item.diagnosis || {};
    const recommendationHtml = recommendations.items.length ? recommendations.items.map((recommendation, index) => `<li><strong>练习 ${index + 1}</strong><p>${escapeHtml(recommendation.stem_text)}</p><small>${escapeHtml(recommendation.source)} · ${escapeHtml(recommendation.reason)}</small></li>`).join("") : '<li class="empty">还没有匹配练习。</li>';
    $("#error-detail").hidden = false;
    $("#error-detail").innerHTML = `<h2>错题详情</h2><dl class="diagnosis-list"><dt>原题</dt><dd>${escapeHtml(item.question_text)}</dd><dt>你的作答</dt><dd>${escapeHtml(item.answer_text || "未填写")}</dd><dt>第一处实质错误</dt><dd>${escapeHtml(item.first_error || "待整理")}</dd><dt>主要错因</dt><dd>${escapeHtml(causeLabels[diagnosis.cause_code] || "待整理")}</dd><dt>判断依据</dt><dd>${escapeHtml(diagnosis.cause_evidence || "待整理")}</dd><dt>知识点梳理</dt><dd>${escapeHtml(diagnosis.knowledge_points?.join("\n") || "待整理")}</dd><dt>完整正确过程</dt><dd>${escapeHtml(diagnosis.correct_solution || "待整理")}</dd><dt>最终答案</dt><dd>${escapeHtml(diagnosis.final_answer || "待整理")}</dd><dt>防错提示</dt><dd>${escapeHtml(diagnosis.prevention_cue || "待整理")}</dd></dl><h3>已验证练习</h3><ol class="recommendation-list">${recommendationHtml}</ol><div class="actions"><button type="button" data-error-action="recommend" class="ghost">匹配练习</button><button type="button" data-error-action="master" class="ghost">标记已掌握</button><button type="button" data-error-action="remove" class="danger">移除错题</button></div>`;
    renderMath($("#error-detail"));
  }
  async function loadErrors() {
    try {
      const result = await api("/v1/errors");
      $("#all-errors").innerHTML = result.items.map(item => `<li><button class="text-button" data-error-id="${item.error_id}">${escapeHtml(item.question_text)}</button><br><small>${escapeHtml(item.first_error || "待整理错因")} · ${item.status === "mastered" ? "已掌握" : "复习中"}</small></li>`).join("") || '<li class="empty">还没有错题。</li>';
      renderMath($("#all-errors"));
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
        renderMath($("#review-question"));
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
    renderMath($("#practice-errors"));
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
