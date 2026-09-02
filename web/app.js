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
    daily_grade_limit: "今天已完成 40 道判题，请先复习和订正；新图片可明日继续处理。",
    daily_recommendation_limit: "今天已生成 24 道推荐题，请先完成已有练习。",
    network_error: "网络异常，请检查网络后重试。"
  })[error.message] || "操作失败，请稍后重试。";
}

async function loadLearningUsage() {
  if (!["errors", "practice", "progress"].includes(page)) return;
  let strip = $("#learning-usage");
  if (!strip) {
    strip = document.createElement("section");
    strip.id = "learning-usage";
    strip.className = "learning-usage-strip";
    strip.setAttribute("aria-label", "今日学习负荷");
    $(".page-header").insertAdjacentElement("afterend", strip);
  }
  try {
    const usage = await api("/v1/learning-usage");
    const grade = usage.grade;
    const recommendation = usage.recommendation;
    strip.innerHTML = `<strong>今日学习负荷</strong><span>判题 <b>${grade.count}/${grade.limit}</b><small>建议 ${grade.target}</small></span><span>推荐题 <b>${recommendation.count}/${recommendation.limit}</b><small>建议 ${recommendation.target}</small></span>`;
    strip.classList.toggle("is-limit", grade.limit_reached || recommendation.limit_reached);
  } catch {
    strip.remove();
  }
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
  let errors = [];
  let dueReviews = [];
  let progress = {};
  let selectionInitialized = false;
  let selectionMode = "auto";
  let fixedPlan = null;
  let pendingReviewLinks = [];
  let pendingReviewLinksPromise = null;
  let generatingPdf = false;
  let dashboardPromise = null;
  const selectedErrorIds = new Set();
  let selectionScope = "";

  function reviewMap() {
    return new Map(errors.filter(item => item.status === "open" && item.review).map(item => [item.error_id, item.review]));
  }

  function fixedPlanCounts() {
    const items = fixedPlan?.items || [];
    return {
      total: items.length,
      pending: items.filter(item => item.status === "pending").length,
      completed: items.filter(item => item.status === "completed").length,
      correction: items.filter(item => item.status === "needs_correction").length,
      unavailable: items.filter(item => item.status === "unavailable").length,
    };
  }

  function stageLabel(item) {
    if (item.status === "mastered") return "已掌握";
    if (!item.review) return "待安排";
    return `第 ${item.review.stage} 阶段`;
  }

  function setPlanStep(selector, done) {
    $(selector).classList.toggle("is-done", done);
  }

  function renderPlan() {
    const selected = selectedErrorIds.size;
    if (selectionMode === "fixed") {
      const counts = fixedPlanCounts();
      const unfinished = counts.pending + counts.correction + counts.unavailable;
      const link = fixedPlan?.download_url ? `<a href="${escapeHtml(fixedPlan.download_url)}" download>下载今日复习推荐题 PDF</a>` : "";
      $("#selected-error-count").textContent = fixedPlan?.available ? `今日计划固定 ${counts.total} 道` : "今日 PDF 的选题清单不可还原";
      $("#pdf-step-state").textContent = "今日已生成，计划不再自动换题";
      $("#correction-step-state").textContent = counts.correction ? `已完成 ${counts.completed} 道，${counts.correction} 道需继续改错` : `已完成 ${counts.completed} 道，待完成 ${counts.pending} 道`;
      const paperProgress = fixedPlan?.progress;
      if (paperProgress?.available) $("#correction-step-state").textContent = `必做题已答 ${paperProgress.answered_count}/${paperProgress.required_count} · 待答 ${paperProgress.pending_count} · 需订正 ${paperProgress.needs_correction_count}`;
      $("#completion-step-state").textContent = counts.unavailable ? `${counts.unavailable} 道历史状态无法核对` : unfinished ? `还有 ${counts.pending} 道待完成` : "今日固定计划已完成";
      $("#today-plan-state").textContent = fixedPlan?.available ? `固定计划 ${counts.total} 道 · 完成 ${counts.completed} · 待做 ${counts.pending}${counts.correction ? ` · 需改错 ${counts.correction}` : ""}` : "今日已生成 PDF，但旧文件缺少可核对的冻结清单";
      $("#generate-review-pdf").disabled = true;
      $("#today-pdf-status").innerHTML = link ? `今日计划已固定：${link}` : "今日计划已固定。";
      setPlanStep("#plan-step-select", counts.total > 0);
      setPlanStep("#plan-step-pdf", true);
      setPlanStep("#plan-step-correct", counts.completed > 0 || counts.correction > 0);
      setPlanStep("#plan-step-done", unfinished === 0 && counts.total > 0);
      return;
    }
    const completedToday = progress.today_completed_review_count || 0;
    const needsCorrection = progress.today_needs_correction_count || 0;
    const due = dueReviews.length;
    $("#selected-error-count").textContent = selected ? `${selectionMode === "manual" ? "手动" : "自动"}已选 ${selected} 道` : due ? `有 ${due} 道到期，等待选择` : "今日无到期题";
    $("#pdf-step-state").textContent = selected ? "可以生成" : "待选择题目";
    $("#correction-step-state").textContent = needsCorrection ? `已答 ${completedToday} 道，${needsCorrection} 道需继续改错` : completedToday ? `已核对 ${completedToday} 道，无待改错` : due ? "待完成重做" : "今日无到期题";
    $("#completion-step-state").textContent = due ? `还有 ${due} 道待完成` : "今日任务已完成";
    $("#today-plan-state").textContent = due ? `完成 ${completedToday} 道 · 待复习 ${due} 道` : "今日已完成";
    $("#generate-review-pdf").disabled = selected === 0 || generatingPdf;
    setPlanStep("#plan-step-select", selected > 0 || due === 0);
    setPlanStep("#plan-step-pdf", due === 0);
    setPlanStep("#plan-step-correct", completedToday > 0 || due === 0);
    setPlanStep("#plan-step-done", due === 0);
  }

  function renderErrors() {
    $("#all-errors").innerHTML = errors.length ? errors.map(item => {
      const diagnosis = item.diagnosis || {};
      const points = Array.isArray(diagnosis.knowledge_points) && diagnosis.knowledge_points.length ? diagnosis.knowledge_points.map(point => `<span>${escapeHtml(point)}</span>`).join("") : '<span>知识点待整理</span>';
      const checked = selectedErrorIds.has(item.error_id) ? " checked" : "";
      const disabled = selectionMode === "fixed" ? " disabled" : "";
      const detailId = `error-detail-${escapeHtml(item.error_id)}`;
      const title = selectionMode === "fixed" ? "今日 PDF 已生成，计划已固定" : "加入今日复习";
      return `<li class="error-card"><label class="error-select" title="${title}"><input name="today-error" type="checkbox" value="${escapeHtml(item.error_id)}"${checked}${disabled}><span class="sr-only">选择这道错题</span></label><article><div class="error-card-heading"><span class="badge">${escapeHtml(stageLabel(item))}</span><time datetime="${escapeHtml(item.created_at)}">${escapeHtml(new Date(item.created_at).toLocaleDateString("zh-CN"))}</time></div><p class="error-record-id">错题编号（error_id）：<code>${escapeHtml(item.error_id)}</code></p><h3>${escapeHtml(item.question_text)}</h3><dl><div><dt>错误原因</dt><dd><strong>${escapeHtml(causeLabels[diagnosis.cause_code] || "待整理")}</strong>${escapeHtml(diagnosis.cause_evidence || item.first_error || "尚未记录")}</dd></div><div><dt>涉及知识点</dt><dd class="knowledge-tags">${points}</dd></div></dl><button class="text-button error-detail-trigger" type="button" data-error-id="${escapeHtml(item.error_id)}" aria-expanded="false" aria-controls="${detailId}">查看完整解析与操作</button><section id="${detailId}" class="error-detail" data-error-detail="${escapeHtml(item.error_id)}" hidden></section></article></li>`;
    }).join("") : '<li class="empty">还没有错题。</li>';
    renderMath($("#all-errors"));
  }

  function renderPendingReviewLinks() {
    const panel = $("#pending-review-links-panel");
    panel.hidden = pendingReviewLinks.length === 0;
    if (!pendingReviewLinks.length) return;
    $("#pending-review-links-count").textContent = `${pendingReviewLinks.length} 条`;
    $("#pending-review-links").innerHTML = pendingReviewLinks.map(item => {
      const verdict = {correct: "正确", partial: "部分正确", incorrect: "错误", unclear: "待核对"}[item.verdict] || "已判题";
      const options = item.options.length ? item.options.map(option => `<button type="button" class="ghost" data-review-link-candidate="${escapeHtml(item.candidate_id)}" data-review-link-version="${item.input_version}" data-review-link-code="${escapeHtml(option.code)}">关联到 ${escapeHtml(option.pdf_name)} · 第 ${option.stage} 阶段 · ${option.kind === "recommendation" ? "推荐题" : "原题"}</button>`).join("") : '<small>当前 PDF 清单中没有可靠候选，请在会话中补充图片上的复习码。</small>';
      return `<li><div><span class="badge">${verdict}</span><p>${escapeHtml(item.question_text)}</p></div><div class="pending-review-options">${options}</div></li>`;
    }).join("");
    renderMath($("#pending-review-links"));
  }

  function loadPendingReviewLinks() {
    if (pendingReviewLinksPromise) return pendingReviewLinksPromise;
    pendingReviewLinksPromise = api("/v1/practice-review-links").then(result => {
      pendingReviewLinks = result.items;
      renderPendingReviewLinks();
    }).catch(() => {
      pendingReviewLinks = [];
      renderPendingReviewLinks();
    }).finally(() => { pendingReviewLinksPromise = null; });
    return pendingReviewLinksPromise;
  }

  async function showError(id) {
    const [item, recommendations] = await Promise.all([api(`/v1/errors/${id}`), api(`/v1/errors/${id}/recommendations`)]);
    const detail = $(`[data-error-detail="${CSS.escape(id)}"]`);
    if (!detail) return;
    document.querySelectorAll('[data-error-detail]').forEach(panel => { panel.hidden = true; });
    document.querySelectorAll('.error-detail-trigger').forEach(button => { button.textContent = "查看完整解析与操作"; button.setAttribute("aria-expanded", "false"); });
    const diagnosis = item.diagnosis || {};
    const recommendationHtml = recommendations.items.length ? recommendations.items.map((recommendation, index) => {
      const optionsHtml = Array.isArray(recommendation.options) && recommendation.options.length
        ? `<p class="recommendation-options">${recommendation.options.map(escapeHtml).join("　　")}</p>` : "";
      return `<li><strong>练习 ${index + 1}</strong><p>${escapeHtml(recommendation.stem_text)}</p>${optionsHtml}<small>${escapeHtml(recommendation.source)} · ${escapeHtml(recommendation.reason)}</small></li>`;
    }).join("") : '<li class="empty">还没有匹配练习。</li>';
    detail.hidden = false;
    detail.innerHTML = `<h2>错题详情</h2><dl class="diagnosis-list"><dt>原题</dt><dd>${escapeHtml(item.question_text)}</dd><dt>你的作答</dt><dd>${escapeHtml(item.answer_text || "未填写")}</dd><dt>第一处实质错误</dt><dd>${escapeHtml(item.first_error || "待整理")}</dd><dt>主要错因</dt><dd>${escapeHtml(causeLabels[diagnosis.cause_code] || "待整理")}</dd><dt>判断依据</dt><dd>${escapeHtml(diagnosis.cause_evidence || "待整理")}</dd><dt>知识点梳理</dt><dd>${escapeHtml(diagnosis.knowledge_points?.join("\n") || "待整理")}</dd><dt>完整正确过程</dt><dd>${escapeHtml(diagnosis.correct_solution || "待整理")}</dd><dt>最终答案</dt><dd>${escapeHtml(diagnosis.final_answer || "待整理")}</dd><dt>防错提示</dt><dd>${escapeHtml(diagnosis.prevention_cue || "待整理")}</dd></dl><h3>已验证练习</h3><ol class="recommendation-list">${recommendationHtml}</ol><div class="actions"><button type="button" data-error-action="recommend" class="ghost">匹配练习</button><button type="button" data-error-action="master" class="ghost">标记已掌握</button><button type="button" data-error-action="remove" class="danger">移除错题</button></div>`;
    renderMath(detail);
    const trigger = $(`[data-error-id="${CSS.escape(id)}"]`);
    trigger.textContent = "收起完整解析与操作";
    trigger.setAttribute("aria-expanded", "true");
  }
  function loadDashboard() {
    if (dashboardPromise) return dashboardPromise;
    dashboardPromise = (async () => {
      try {
        const [errorResult, reviewResult, progressResult, pdfResult] = await Promise.all([api("/v1/errors"), api("/v1/reviews/today"), api("/v1/progress"), api("/v1/practice-pdfs")]);
        errors = errorResult.items;
        selectionScope = errorResult.selection_scope;
        dueReviews = reviewResult.items;
        progress = progressResult;
        fixedPlan = pdfResult.today_plan || null;
        const available = reviewMap();
        const saved = readReviewSelection(selectionScope, available);
        const resolved = resolveReviewSelection({fixedPlan, dueReviews, saved, mode: selectionInitialized ? selectionMode : "auto", currentIds: selectedErrorIds});
        selectionMode = resolved.mode;
        selectedErrorIds.clear();
        resolved.ids.forEach(id => selectedErrorIds.add(id));
        selectionInitialized = true;
        renderPlan();
        renderErrors();
        loadPendingReviewLinks();
        return true;
      } catch (error) {
        status($("#page-status"), authError(error), true);
        return false;
      } finally {
        dashboardPromise = null;
      }
    })();
    return dashboardPromise;
  }
  $("#refresh-errors").addEventListener("click", loadDashboard);
  $("#all-errors").addEventListener("change", event => {
    if (event.target.name !== "today-error") return;
    if (selectionMode === "fixed") return;
    if (event.target.checked && selectedErrorIds.size >= 12) {
      event.target.checked = false;
      return status($("#page-status"), "今日复习一次最多选择 12 道错题。", true);
    }
    selectionMode = "manual";
    if (event.target.checked) selectedErrorIds.add(event.target.value);
    else selectedErrorIds.delete(event.target.value);
    if (!writeReviewSelection(selectionScope, selectedErrorIds, reviewMap())) status($("#page-status"), "当前浏览器无法保存选题，离开页面后需重新选择。", true);
    status($("#today-pdf-status"), "题目选择已变化，请重新生成 PDF。", false);
    renderPlan();
  });
  $("#pending-review-links").addEventListener("click", async event => {
    const button = event.target.closest("[data-review-link-candidate]");
    if (!button) return;
    button.disabled = true;
    try {
      const result = await api(`/v1/practice-review-links/${button.dataset.reviewLinkCandidate}`, {
        method: "POST",
        body: JSON.stringify({input_version: Number(button.dataset.reviewLinkVersion), code: button.dataset.reviewLinkCode}),
      });
      status($("#page-status"), result.receipt?.message || "关联完成。", false);
      await loadDashboard();
    } catch (error) {
      status($("#page-status"), authError(error), true);
      button.disabled = false;
    }
  });
  $("#all-errors").addEventListener("click", async event => {
    const trigger = event.target.closest("[data-error-id]");
    const id = trigger?.dataset.errorId;
    if (!id) return;
    const detail = $(`[data-error-detail="${CSS.escape(id)}"]`);
    if (detail && !detail.hidden) {
      detail.hidden = true;
      trigger.textContent = "查看完整解析与操作";
      trigger.setAttribute("aria-expanded", "false");
      return;
    }
    try {
      await showError(id);
    } catch (error) {
      status($("#page-status"), authError(error), true);
    }
  });
  $("#all-errors").addEventListener("click", async event => {
    const action = event.target.dataset.errorAction;
    const detail = event.target.closest("[data-error-detail]");
    if (!action || !detail) return;
    const errorId = detail.dataset.errorDetail;
    event.target.disabled = true;
    try {
      if (action === "recommend") await api(`/v1/errors/${errorId}/recommendations`, {method: "POST", headers: {"Idempotency-Key": crypto.randomUUID()}});
      else if (action === "master") await api(`/v1/errors/${errorId}/master`, {method: "POST"});
      else if (action === "remove") {
        if (!confirm("移除后将取消这道错题的待复习和未完成推荐，确认继续？")) return;
        await api(`/v1/errors/${errorId}`, {method: "DELETE"});
        await loadDashboard();
        return;
      }
      await Promise.all([loadDashboard(), loadLearningUsage()]);
      await showError(errorId);
    } catch (error) {
      status($("#page-status"), authError(error), true);
    } finally {
      event.target.disabled = false;
    }
  });
  $("#generate-review-pdf").addEventListener("click", async event => {
    if (generatingPdf) return;
    const button = event.currentTarget;
    generatingPdf = true;
    button.disabled = true;
    status($("#today-pdf-status"), "正在匹配已验证推荐题并生成 PDF…");
    try {
      if (!(await loadDashboard()) || fixedPlan) return;
      let ids = [...selectedErrorIds];
      if (!ids.length) return status($("#today-pdf-status"), "当前没有可生成的复习题。", true);
      await ensureReviewRecommendations(ids);
      const prepared = new Set(ids);
      if (!(await loadDashboard()) || fixedPlan) return;
      ids = [...selectedErrorIds];
      if (!ids.length) return status($("#today-pdf-status"), "复习任务刚刚完成，当前没有可生成的题目。", false);
      await ensureReviewRecommendations(ids.filter(id => !prepared.has(id)));
      const result = await createDailyReviewPdf(ids, $("#review-pdf-answers").checked);
      showPracticePdfResult(result, $("#today-pdf-status"));
      await Promise.all([loadDashboard(), loadLearningUsage()]);
    } catch (error) {
      status($("#today-pdf-status"), authError(error), true);
    } finally {
      generatingPdf = false;
      renderPlan();
    }
  });
  const refreshWhenVisible = () => { if (!document.hidden && !generatingPdf) loadDashboard(); };
  window.addEventListener("pageshow", refreshWhenVisible);
  window.addEventListener("focus", refreshWhenVisible);
  document.addEventListener("visibilitychange", refreshWhenVisible);
  window.setInterval(refreshWhenVisible, 30000);
  loadDashboard();
}

function chinaDate(value = new Date()) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"}).formatToParts(new Date(value)).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function todayReviewSnapshot(errors, progress, now = new Date()) {
  const date = chinaDate(now);
  const pending = errors.filter(item => item.status === "open" && ["pending", "ready"].includes(item.review?.status) && Number.isFinite(Date.parse(item.review.due_at)));
  const overdue = pending.filter(item => chinaDate(item.review.due_at) < date).sort((a, b) => Date.parse(a.review.due_at) - Date.parse(b.review.due_at));
  return {
    date, due_count: pending.filter(item => chinaDate(item.review.due_at) === date).length,
    completed_count: progress.today_completed_review_count || 0,
    overdue_items: overdue.map(item => ({type: "due", error_id: item.error_id, question_text: item.question_text, first_error: item.first_error,
      knowledge_points: item.diagnosis?.knowledge_points || [], stage: item.review.stage, status: item.review.status, overdue: true, original_due_date: chinaDate(item.review.due_at)}))
  };
}

function reviewSelectionKey(scope, now = new Date()) {
  if (!/^[0-9a-f]{24}$/.test(scope || "")) throw new Error("selection_scope_unavailable");
  return `lzlm-review-selection:${scope}:${chinaDate(now)}`;
}

function readReviewSelection(scope, available, now = new Date()) {
  try {
    const raw = sessionStorage.getItem(reviewSelectionKey(scope, now));
    if (raw === null) return null;
    const value = JSON.parse(raw);
    const items = Array.isArray(value) ? value.map(error_id => ({error_id, review_id: null})) : value?.mode === "manual" && Array.isArray(value.items) ? value.items : null;
    if (!items) return null;
    return [...new Set(items.filter(item => {
      const review = available.get(item?.error_id);
      const reviewId = typeof review === "string" ? review : review?.review_id;
      return typeof item?.error_id === "string" && available.has(item.error_id) && (!item.review_id || reviewId === item.review_id);
    }).map(item => item.error_id))].slice(0, 12);
  } catch { return null; }
}

function writeReviewSelection(scope, ids, reviews = new Map(), now = new Date()) {
  try {
    const items = [...ids].slice(0, 12).map(error_id => ({error_id, review_id: reviews.get(error_id)?.review_id || null}));
    sessionStorage.setItem(reviewSelectionKey(scope, now), JSON.stringify({mode: "manual", items}));
    return true;
  } catch { return false; }
}

function resolveReviewSelection({fixedPlan, dueReviews, saved, mode, currentIds}) {
  if (fixedPlan) return {mode: "fixed", ids: fixedPlan.available ? [...new Set(fixedPlan.items.map(item => item.error_id))].slice(0, 12) : []};
  if (mode === "manual" || saved !== null) return {mode: "manual", ids: saved === null ? [...currentIds] : saved};
  return {mode: "auto", ids: dueReviews.slice(0, 12).map(item => item.error_id)};
}

function ensureReviewRecommendations(ids) {
  return Promise.all(ids.map(id => api(`/v1/errors/${id}/recommendations?limit=1`, {method: "POST", headers: {"Idempotency-Key": crypto.randomUUID()}})));
}

function createDailyReviewPdf(ids, includeAnswers = false) {
  return api("/v1/practice-pdfs", {method: "POST", body: JSON.stringify({error_ids: ids, include_answers: includeAnswers, plan_kind: "daily_review"}), headers: {"Idempotency-Key": crypto.randomUUID()}});
}

function showPracticePdfResult(result, target) {
  if (!result.download_url) return status(target, "PDF 正在生成，请稍后刷新。");
  const link = document.createElement("a");
  link.href = result.download_url;
  link.textContent = "下载今日复习推荐题 PDF";
  link.setAttribute("download", "");
  target.replaceChildren("已生成：", link);
}

function bindProgress() {
  let monthCursor = new Date(`${chinaDate()}T12:00:00`);
  monthCursor = new Date(monthCursor.getFullYear(), monthCursor.getMonth(), 1);
  let calendar = {days: [], summary: {}, total_error_count: 0};
  let activeFilter = "all";
  let selectedDate = "";
  let selectedMetric = "";
  let todaySnapshot = {date: chinaDate(), due_count: 0, completed_count: 0, overdue_items: []};
  let selectionScope = "";
  let selectedErrorIds = new Set();
  let currentReviews = new Map();
  let fixedPlan = null;
  let generatingPdf = false;
  const filterLabels = {all: "全部活动", new: "新增错题", due: "待复习", completed: "作答与完成", correction: "需改错", papers: "PDF 当前进度", answered: "当日已答题"};

  function monthKey() {
    return `${monthCursor.getFullYear()}-${String(monthCursor.getMonth() + 1).padStart(2, "0")}`;
  }

  function filteredItems(day) {
    if (activeFilter === "new") return day.items.filter(item => item.type === "new");
    if (activeFilter === "due") return day.items.filter(item => item.type === "due");
    if (activeFilter === "completed") return day.items.filter(item => item.type === "completed");
    if (activeFilter === "correction") return day.items.filter(item => item.needs_correction);
    return day.items;
  }

  function metric(date, kind, label, value, className, incomplete = false) {
    const count = incomplete ? value ? `≥${value}` : "待核实" : value;
    return `<button type="button" class="calendar-metric ${className}" data-calendar-date="${date}" data-calendar-kind="${kind}" aria-label="${date} ${label} ${count}">${label}<strong>${count}</strong></button>`;
  }

  function backlogFor(day) {
    if (day.date === todaySnapshot.date) return todaySnapshot.overdue_items;
    return (day.backlog_indices || []).map(index => calendar.backlog_items[index]);
  }

  function selectable(item) {
    const review = currentReviews.get(item.error_id);
    return item.type === "due" && review && item.stage === review.stage && item.original_due_date === chinaDate(review.due_at);
  }

  function detailItems(day) {
    const future = day.date > todaySnapshot.date;
    const kind = selectedMetric || activeFilter;
    const due = day.items.filter(item => item.type === "due" && (day.date < todaySnapshot.date || selectable(item)));
    const backlog = backlogFor(day);
    if (["papers", "answered"].includes(kind)) return [];
    if (kind === "backlog") return backlog;
    if (kind === "due") return selectedMetric ? due : [...due, ...backlog];
    if (kind === "new") return day.items.filter(item => item.type === "new");
    if (kind === "completed") return day.items.filter(item => item.type === "completed");
    if (kind === "correction") return day.items.filter(item => item.needs_correction);
    return future ? due : [...day.items, ...backlog];
  }

  function renderStats() {
    const summary = calendar.summary || {};
    $("#calendar-stat-new").textContent = summary.new_error_count || 0;
    $("#calendar-stat-due").textContent = summary.due_review_count || 0;
    $("#calendar-stat-completed").textContent = summary.completed_review_count || 0;
    $("#calendar-stat-answered").textContent = summary.submitted_question_count || 0;
    $("#calendar-stat-correction").textContent = summary.needs_correction_count || 0;
    $("#calendar-stat-overdue").textContent = summary.overdue_review_count || 0;
    $("#calendar-stat-rate").textContent = `${summary.planned_completion_percent || 0}%`;
    $("#calendar-stat-accuracy").textContent = `${summary.review_accuracy_percent || 0}%`;
  }

  function renderDayDetail() {
    const detail = $("#calendar-day-detail");
    const day = calendar.days.find(item => item.date === selectedDate) || {date: selectedDate, items: []};
    const items = detailItems(day);
    if (!selectedDate) {
      detail.hidden = true;
      return;
    }
    const groups = new Map();
    items.forEach(item => {
      if (!groups.has(item.error_id)) groups.set(item.error_id, {item, labels: new Set(), knowledge: new Set(), selectable: false});
      const group = groups.get(item.error_id);
      group.selectable ||= Boolean(selectable(item));
      (item.knowledge_points || []).forEach(point => group.knowledge.add(point));
      if (item.type === "new") group.labels.add("新增错题");
      else if (item.type === "due") group.labels.add(`第 ${item.stage} 阶段 · 原定 ${item.original_due_date || selectedDate}`);
      else {
        const result = {correct: "正确", partial: "部分掌握", wrong: "错误"}[item.result] || "已完成";
        group.labels.add(`第 ${item.stage} 阶段 · 复习${result}`);
      }
    });
    const label = selectedMetric === "backlog" ? selectedDate === todaySnapshot.date ? "历史逾期" : "截至当天未完成" : selectedMetric === "due" ? selectedDate > todaySnapshot.date ? "计划复习" : "当日到期" : filterLabels[selectedMetric || activeFilter];
    $("#calendar-day-title").textContent = `${selectedDate} · ${label}`;
    $("#calendar-history-note").textContent = day.history_complete === false ? "历史记录不完整：部分旧到期日期已被覆盖或任务已取消，以下只展示可核实的记录，不能视为完整统计。" : selectedDate < todaySnapshot.date ? "未完成数量截至所选日期当天结束（中国时间）；已在之后完成的题仍保留在当时的记录中。" : selectedDate > todaySnapshot.date ? "仅展示已经安排的复习计划，后续阶段按实际完成情况生成；选题不改变原定复习日期。" : "历史逾期包含以前月份，不包含今天到期的题目。";
    $("#calendar-history-note").textContent += " 需改错与 PDF 显示截至现在的逐题状态；首次成绩和提交日期保留，订正不额外推进阶段。重印共享作答，不重复计数。";
    const hasSelectable = [...groups.values()].some(group => group.selectable);
    $("#calendar-selection-status").hidden = !hasSelectable;
    status($("#calendar-selection-status"), `已选 ${selectedErrorIds.size}/12 道。仅仍在待复习任务中的题目可加入今日选题。`);
    $("#calendar-pdf-actions").hidden = !hasSelectable;
    $("#calendar-generate-pdf").disabled = !selectedErrorIds.size || generatingPdf || Boolean(fixedPlan);
    if (fixedPlan) $("#calendar-pdf-status").innerHTML = fixedPlan.download_url ? `今日计划已固定：<a href="${escapeHtml(fixedPlan.download_url)}" download>下载 PDF</a>` : "今日计划已固定。";
    const kind = selectedMetric || activeFilter;
    const paperHtml = ["all", "due", "completed", "papers"].includes(kind) ? (day.practice_plans || []).map(paper => `<article class="calendar-detail-item"><h4>${escapeHtml(paper.filename)}</h4>${practiceProgressMarkup(paper)}<a href="/v1/practice-pdfs/${encodeURIComponent(paper.task_id)}/download" download>下载 PDF</a></article>`).join("") : "";
    const activityHtml = ["all", "completed", "answered", "correction"].includes(kind) ? (day.practice_activity || []).filter(row => kind !== "correction" || row.status === "needs_correction").map(row => `<article class="calendar-detail-item"><div class="calendar-event-labels"><span>${row.kind === "original" ? "原题重做" : "推荐训练"} · ${practiceVerdictLabel(row)}</span><span>实际提交 ${escapeHtml(new Date(row.submitted_at).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai"}))}</span></div><p data-math-text>${escapeHtml(row.question_text)}</p><small>来源：${escapeHtml(row.filename)} · 错题 ${escapeHtml(row.error_id.slice(0, 8))}</small></article>`).join("") : "";
    $("#calendar-day-items").innerHTML = paperHtml + activityHtml + [...groups.values()].map(group => {
      const labels = [...group.labels].map(label => `<span>${escapeHtml(label)}</span>`).join("");
      const cause = group.item.first_error ? `<p data-math-text><strong>错误原因：</strong>${escapeHtml(group.item.first_error)}</p>` : "";
      const knowledge = group.knowledge.size ? `<p data-math-text><strong>知识点：</strong>${[...group.knowledge].map(escapeHtml).join("、")}</p>` : "";
      const checkbox = group.selectable ? `<label class="calendar-backlog-select"><input type="checkbox" name="calendar-error" value="${escapeHtml(group.item.error_id)}"${selectedErrorIds.has(group.item.error_id) ? " checked" : ""}${fixedPlan || generatingPdf ? " disabled" : ""}><span>加入今日复习</span></label>` : "";
      return `<article class="calendar-detail-item">${checkbox}<div class="calendar-event-labels">${labels}</div><h4 data-math-text>${escapeHtml(group.item.question_text)}</h4>${cause}${knowledge}</article>`;
    }).join("") || '<p class="empty">当前没有符合筛选条件的题目。</p>';
    detail.hidden = false;
    $("#calendar-day-items").querySelectorAll("[data-math-text]").forEach(renderMath);
  }

  function renderCalendar() {
    const year = monthCursor.getFullYear();
    const month = monthCursor.getMonth();
    const firstWeekday = (new Date(year, month, 1).getDay() + 6) % 7;
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const byDay = new Map(calendar.days.map(item => [item.date, item]));
    const cells = Array.from({length: firstWeekday}, () => '<div class="calendar-day is-empty" aria-hidden="true"></div>');
    for (let day = 1; day <= daysInMonth; day += 1) {
      const dateKey = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
      const record = byDay.get(dateKey) || {date: dateKey, items: [], stage_counts: {}};
      const isToday = dateKey === todaySnapshot.date;
      const isFuture = dateKey > todaySnapshot.date;
      const classes = `calendar-day${isToday ? " is-today" : ""}${selectedDate === dateKey ? " is-selected" : ""}`;
      const plans = record.items.filter(item => item.type === "due" && (!isFuture || selectable(item)));
      const incomplete = !isToday && !isFuture && record.history_complete === false;
      const metrics = [
        ["all", "due"].includes(activeFilter) ? metric(dateKey, "due", isFuture ? "计划复习" : isToday ? "今日到期" : "当日到期", isToday ? todaySnapshot.due_count : plans.length, "is-due", incomplete) : "",
        !isFuture && ["all", "due"].includes(activeFilter) ? metric(dateKey, "backlog", isToday ? "历史逾期" : "当日未完成", backlogFor(record).length, "is-overdue", incomplete) : "",
        !isFuture && ["all", "completed"].includes(activeFilter) ? metric(dateKey, "completed", "完成复习组", isToday ? todaySnapshot.completed_count : record.completed_review_count || 0, "is-completed") : "",
        !isFuture && ["all", "completed"].includes(activeFilter) && record.submitted_question_count ? metric(dateKey, "answered", "当日已答题", record.submitted_question_count, "is-completed") : "",
        ["all", "due", "completed"].includes(activeFilter) && record.practice_plans?.length ? metric(dateKey, "papers", "PDF已答", record.paper_required_count ? `${record.paper_answered_count}/${record.paper_required_count}` : "待核对", "is-completed") : "",
        !isFuture && (activeFilter === "new" || activeFilter === "all" && record.new_error_count) ? metric(dateKey, "new", "新增错题", record.new_error_count || 0, "is-new") : "",
        !isFuture && (activeFilter === "correction" || activeFilter === "all" && record.needs_correction_count) ? metric(dateKey, "correction", "需改错", record.needs_correction_count || 0, "is-correction") : "",
      ].join("");
      const stageCounts = {};
      plans.forEach(item => {stageCounts[item.stage] = (stageCounts[item.stage] || 0) + 1;});
      const stages = ["all", "due"].includes(activeFilter) ? Object.entries(stageCounts).map(([stage, count]) => `<span>第${stage}阶段×${count}</span>`).join("") : "";
      cells.push(`<div class="${classes}"><button type="button" class="calendar-date-open" data-calendar-date="${dateKey}" aria-label="查看 ${dateKey} 复习详情"><strong>${day}</strong><span>${isToday ? "今天" : isFuture ? "计划" : ""}</span></button><div class="calendar-day-metrics">${metrics}</div>${stages ? `<div class="calendar-stages">${stages}</div>` : ""}${incomplete ? '<span class="calendar-history-warning">记录不完整</span>' : ""}</div>`);
    }
    while (cells.length % 7) cells.push('<div class="calendar-day is-empty" aria-hidden="true"></div>');
    $("#calendar-month").textContent = `${year} 年 ${month + 1} 月`;
    const activeDays = calendar.days.filter(day => filteredItems(day).length || ["all", "completed"].includes(activeFilter) && day.submitted_question_count || ["all", "due", "completed"].includes(activeFilter) && day.practice_plans?.length).length;
    $("#calendar-summary").textContent = `${filterLabels[activeFilter]}：${activeDays} 天有记录；累计 ${calendar.total_error_count || 0} 道错题。`;
    $("#review-calendar").innerHTML = cells.join("");
    renderDayDetail();
  }

  async function loadProgress() {
    status($("#progress-status"), "正在读取学习记录…");
    try {
      const [history, errorResult, progress, pdfResult] = await Promise.all([api(`/v1/progress/calendar?month=${monthKey()}`), api("/v1/errors"), api("/v1/progress"), api("/v1/practice-pdfs")]);
      calendar = history;
      todaySnapshot = todayReviewSnapshot(errorResult.items, progress);
      selectionScope = errorResult.selection_scope;
      currentReviews = new Map(errorResult.items.filter(item => item.status === "open" && ["pending", "ready"].includes(item.review?.status)).map(item => [item.error_id, item.review]));
      fixedPlan = pdfResult.today_plan || null;
      selectedErrorIds = new Set(fixedPlan?.available ? fixedPlan.items.map(item => item.error_id).slice(0, 12) : readReviewSelection(selectionScope, currentReviews) || []);
      renderStats();
      renderCalendar();
      status($("#progress-status"), "数据已更新。");
      return true;
    } catch (error) {
      status($("#progress-status"), authError(error), true);
      return false;
    }
  }
  $("#calendar-prev").addEventListener("click", () => { monthCursor = new Date(monthCursor.getFullYear(), monthCursor.getMonth() - 1, 1); selectedDate = ""; loadProgress(); });
  $("#calendar-next").addEventListener("click", () => { monthCursor = new Date(monthCursor.getFullYear(), monthCursor.getMonth() + 1, 1); selectedDate = ""; loadProgress(); });
  $("#calendar-filters").addEventListener("click", event => {
    const button = event.target.closest("[data-calendar-filter]");
    if (!button) return;
    activeFilter = button.dataset.calendarFilter;
    selectedMetric = "";
    $("#calendar-filters").querySelectorAll("button").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
    renderCalendar();
  });
  $("#review-calendar").addEventListener("click", event => {
    const button = event.target.closest("[data-calendar-date]");
    if (!button) return;
    selectedDate = button.dataset.calendarDate;
    selectedMetric = button.dataset.calendarKind || "";
    renderCalendar();
    $("#calendar-day-detail").scrollIntoView({block: "nearest"});
  });
  $("#calendar-day-items").addEventListener("change", event => {
    const input = event.target;
    const day = calendar.days.find(item => item.date === selectedDate) || {date: selectedDate, items: []};
    if (fixedPlan || generatingPdf || input.name !== "calendar-error" || !detailItems(day).some(item => item.error_id === input.value && selectable(item))) return;
    if (input.checked && selectedErrorIds.size >= 12) {
      input.checked = false;
      return status($("#calendar-selection-status"), "今日复习一次最多选择 12 道，请先取消其他题目。", true);
    }
    if (input.checked) selectedErrorIds.add(input.value);
    else selectedErrorIds.delete(input.value);
    const saved = writeReviewSelection(selectionScope, selectedErrorIds, currentReviews);
    status($("#calendar-selection-status"), saved ? `已选 ${selectedErrorIds.size}/12 道，已同步选题，可在下方直接生成 PDF。` : "当前浏览器无法保存选题，请重新选择。", !saved);
    $("#calendar-generate-pdf").disabled = !selectedErrorIds.size || generatingPdf;
  });
  $("#calendar-generate-pdf").addEventListener("click", async event => {
    if (generatingPdf || fixedPlan || !selectedErrorIds.size) return;
    const button = event.currentTarget;
    let ids = [...selectedErrorIds];
    generatingPdf = true;
    button.disabled = true;
    renderDayDetail();
    status($("#calendar-pdf-status"), "正在匹配已验证推荐题并生成 PDF…");
    try {
      if (!(await loadProgress())) return status($("#calendar-pdf-status"), "未能核对最新复习计划，请稍后重试。", true);
      if (fixedPlan) return;
      ids = ids.filter(id => selectedErrorIds.has(id));
      if (!ids.length) return status($("#calendar-pdf-status"), "所选复习任务已变化，请重新选择。", true);
      await ensureReviewRecommendations(ids);
      if (!(await loadProgress())) return status($("#calendar-pdf-status"), "未能核对最新复习计划，请稍后重试。", true);
      if (fixedPlan) return;
      ids = ids.filter(id => selectedErrorIds.has(id));
      if (!ids.length) return status($("#calendar-pdf-status"), "所选复习任务已变化，请重新选择。", true);
      const result = await createDailyReviewPdf(ids);
      showPracticePdfResult(result, $("#calendar-pdf-status"));
      await loadProgress();
    } catch (error) {
      status($("#calendar-pdf-status"), authError(error), true);
    } finally {
      generatingPdf = false;
      renderDayDetail();
    }
  });
  $("#calendar-day-close").addEventListener("click", () => { selectedDate = ""; renderCalendar(); });
  $("#refresh-progress").addEventListener("click", loadProgress);
  loadProgress();
}

function practiceVerdictLabel(row) {
  return {correct: "已答正确", needs_correction: "已答待订正", pending: "待作答", reference_only: "仅作推荐依据"}[row.status] || "待核对";
}

function practiceProgressMarkup(paper) {
  const progress = paper.progress;
  if (!progress?.available) return '<p class="pdf-progress-note">旧 PDF 的题目清单尚未完整核对，暂不推测完成数量。</p>';
  const groups = progress.groups.filter(group => group.answered_count > 0).map(group => `<span>错题 ${escapeHtml(group.error_id.slice(0, 8))} · 第 ${group.stage} 阶段 · 已答 ${group.answered_count}/${group.required_count}${group.completed ? " · 本组已记录" : ""}</span>`).join("");
  const items = progress.items.map(row => `<li><strong>${row.kind === "original" ? "原题" : "推荐题"} · ${practiceVerdictLabel(row)}</strong><p data-math-text>${escapeHtml(row.question_text)}</p>${row.submitted_at ? `<small>提交于 ${escapeHtml(new Date(row.submitted_at).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai"}))}</small>` : ""}</li>`).join("");
  return `<div class="pdf-progress"><p>必做题已答 <strong>${progress.answered_count}/${progress.required_count}</strong> · 待答 ${progress.pending_count} · 需订正 ${progress.needs_correction_count}</p><div class="pdf-progress-groups">${groups}</div><details><summary>查看逐题作答状态</summary><ol>${items}</ol></details><small>截至当前的进度，重印共享作答；全部必做题完成后才汇总复习阶段。</small></div>`;
}

function bindPractice() {
  api("/v1/practice-pdfs").then(result => {
    $("#practice-pdf-history").innerHTML = result.items.length ? result.items.map(item => {
      const generated = item.generated_at ? new Date(item.generated_at).toLocaleString("zh-CN") : "已生成";
      const dateParts = item.generated_at ? Object.fromEntries(new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai", year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
      }).formatToParts(new Date(item.generated_at)).map(part => [part.type, part.value])) : {};
      const displayName = item.source === "generated" && item.generated_at
        ? `每日复习练习-${dateParts.year}年${dateParts.month}月${dateParts.day}日-${dateParts.hour}时${dateParts.minute}分.pdf`
        : item.filename;
      const details = item.source === "desktop_skill" ? `${generated} · Skill 历史文件` : `${generated} · ${item.question_count || 0} 道题${item.include_answers ? " · 含答案" : ""}`;
      return `<article class="pdf-history-item"><div><strong>${escapeHtml(displayName)}</strong><small>${escapeHtml(details)}</small>${practiceProgressMarkup(item)}</div><a class="pdf-download" href="${escapeHtml(item.download_url)}" download>下载</a></article>`;
    }).join("") : '<p class="empty">还没有生成过练习 PDF。</p>';
    $("#practice-pdf-history").querySelectorAll("[data-math-text]").forEach(renderMath);
  }).catch(error => { $("#practice-pdf-history").innerHTML = `<p class="status error">${escapeHtml(authError(error))}</p>`; });
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
  loadLearningUsage();
  ({workbench: bindWorkbench, errors: bindErrors, practice: bindPractice, progress: bindProgress, settings: bindSettings})[page]();
}

init();
