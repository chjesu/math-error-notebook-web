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
  const pendingIntakes = [];
  const conversationTurns = [];
  const renderedCandidates = new Set();
  const CHAT_PAGE_SIZE = 10;
  let visibleTurnCount = CHAT_PAGE_SIZE;
  const uploadInput = $("#file");
  const sendButton = $("#upload-button");
  const chatInput = $("#chat-input");
  const dropZone = $("#drop-zone");
  const uploadSurface = $(".chat-main");
  const allowedExtensions = new Set(["pdf", "png", "jpg", "jpeg", "docx"]);
  const maxFileBytes = 25 * 1024 * 1024;
  const confirmIntakeCommands = new Set(["确认并判题", "确认题干与作答", "开始判题"]);
  const commitCommands = new Set(["确认入本", "确认写入错题本", "加入错题本"]);
  const nextCommands = new Set(["下一题", "处理下一个", "跳过"]);
  const previewDialog = $("#image-preview-dialog");
  const previewContent = $("#image-preview-content");

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
    button.dataset.previewName = item.file.name;
    button.setAttribute("aria-label", `查看 ${item.file.name} 原图`);
    image.src = item.previewUrl;
    image.alt = "";
    button.append(image);
    return button;
  }

  function scrollChatToEnd() {
    requestAnimationFrame(() => { $("#chat-thread").scrollTop = $("#chat-thread").scrollHeight; });
  }

  function renderConversationWindow() {
    const start = Math.max(0, conversationTurns.length - visibleTurnCount);
    $("#chat-stream").replaceChildren(...conversationTurns.slice(start));
    $("#history-pagination").hidden = start === 0;
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
    heading.textContent = `判题候选 · ${verdict}`;
    for (const [label, value] of [["第一处错误", candidate.first_error], ["主要错因", causeLabels[diagnosis.cause_code] || diagnosis.cause_code], ["判断依据", diagnosis.cause_evidence], ["正确过程", diagnosis.correct_solution], ["最终答案", diagnosis.final_answer], ["防错提示", diagnosis.prevention_cue]]) {
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
    const turn = document.createElement("div");
    const avatar = document.createElement("img");
    const response = document.createElement("div");
    const heading = document.createElement("strong");
    const question = document.createElement("p");
    const answer = document.createElement("p");
    turn.className = "chat-turn assistant-turn";
    avatar.className = "chat-avatar";
    avatar.src = "/assets/branding/logo-symbol-color-64-v1.png";
    avatar.alt = "";
    response.className = "chat-response chat-intake-candidate";
    heading.textContent = `题干与作答候选 · 进度 ${intake.queueIndex || 1}/${intake.queueTotal || 1}`;
    question.textContent = `题干：${intake.questionText || "尚未识别，请直接告诉我题干或需要修正的内容。"}`;
    answer.textContent = `作答：${intake.answerText || "未识别或未作答"}`;
    renderMath(question);
    renderMath(answer);
    response.append(heading, question, answer);
    turn.append(avatar, response);
    appendTurn(turn);
  }

  function setComposerState() {
    const retryable = uploadFiles.some(item => !item.submitted && ["queued", "failed"].includes(item.state));
    chatInput.disabled = false;
    chatInput.placeholder = stage === "upload"
      ? "输入消息，或添加图片、PDF、DOCX"
      : stage === "intake"
        ? "输入修正或补充；确认无误请发送“确认并判题”"
        : "继续追问或修正；确认无误请发送“确认入本”";
    sendButton.disabled = busy || (!retryable && !chatInput.value.trim());
    sendButton.textContent = retryable && uploadFiles.some(item => item.state === "failed") ? "↻" : "↑";
  }

  function renderUploadFiles() {
    const labels = {queued: "等待上传", uploading: "上传中", processing: "处理中", done: "上传完成", failed: "上传失败"};
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
      if (["queued", "failed"].includes(item.state)) {
        const remove = document.createElement("button"); remove.type = "button"; remove.dataset.removeFile = item.id; remove.textContent = "×"; preview.append(remove);
      }
      name.textContent = item.file.name;
      card.append(preview, name);
      return card;
    });
    $("#upload-file-list").replaceChildren(...cards);
    setComposerState();
  }

  function addUploadFiles(files) {
    const rejected = [];
    let duplicates = 0;
    for (const file of files) {
      const extension = file.name.split(".").pop().toLowerCase();
      if (!allowedExtensions.has(extension)) rejected.push(`${file.name}：格式不支持`);
      else if (file.size > maxFileBytes) rejected.push(`${file.name}：超过 25 MB`);
      else if (uploadFiles.some(item => item.file.name.toLowerCase() === file.name.toLowerCase() && item.file.size === file.size && item.file.lastModified === file.lastModified)) duplicates += 1;
      else uploadFiles.push({id: crypto.randomUUID(), file, extension, previewUrl: ["png", "jpg", "jpeg"].includes(extension) ? URL.createObjectURL(file) : "", state: "queued", progress: 0, error: "", submitted: false});
    }
    renderUploadFiles();
    const notice = [rejected.join("；"), duplicates ? `已忽略 ${duplicates} 个重复文件` : ""].filter(Boolean).join("；");
    status($("#upload-status"), notice, Boolean(rejected.length));
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
        let value = {}; try { value = JSON.parse(xhr.responseText); } catch (_) {}
        if (xhr.status >= 200 && xhr.status < 300) resolve(value);
        else { const error = new Error(value.error?.code || "temporarily_unavailable"); error.status = xhr.status; reject(error); }
      });
      xhr.addEventListener("error", () => reject(new Error("network_error")));
      xhr.send(form);
    });
  }

  function appendUserUpload(files) {
    const turn = document.createElement("div");
    const bubble = document.createElement("div");
    const grid = document.createElement("div");
    const label = document.createElement("p");
    turn.className = "chat-turn user-turn";
    bubble.className = "chat-upload-bubble";
    grid.className = "chat-upload-grid";
    for (const item of files) {
      const card = document.createElement("div");
      const preview = document.createElement("div");
      const name = document.createElement("small");
      card.className = "chat-upload-thumbnail";
      preview.className = "chat-upload-preview";
      if (item.previewUrl) preview.append(imagePreviewButton(item));
      else { const type = document.createElement("strong"); type.textContent = item.extension.toUpperCase(); preview.append(type); }
      name.textContent = item.file.name; card.append(preview, name); grid.append(card);
    }
    label.textContent = `请整理这 ${files.length} 个文件`;
    bubble.append(grid, label); turn.append(bubble); appendTurn(turn);
  }

  function activateNextIntake(message = "") {
    if (message) assistantTurn(message);
    activeIntake = pendingIntakes.shift() || null;
    activeAttempt = null;
    activeCandidate = null;
    stage = activeIntake ? "intake" : "upload";
    if (activeIntake) {
      appendIntake(activeIntake);
      assistantTurn(`你可以直接输入修正或追问。内容无误时发送“确认并判题”${pendingIntakes.length ? `；后面还有 ${pendingIntakes.length} 道题` : ""}。`);
    }
    setComposerState();
    if (!chatInput.disabled) chatInput.focus();
  }

  async function uploadQueued() {
    const files = uploadFiles.filter(item => !item.submitted && ["queued", "failed"].includes(item.state));
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
      setProgress(progress, "正在上传附件", `${index + 1}/${files.length} · ${item.file.name} · 0%`);
      try {
        if (!item.intakeId) {
          const uploaded = await uploadFile(item, percent => setProgress(progress, "正在上传附件", `${index + 1}/${files.length} · ${item.file.name} · ${percent}%`));
          item.state = "processing";
          setProgress(progress, "正在建立错题会话", `${index + 1}/${files.length} · ${item.file.name}`);
          const task = await api("/v1/intakes", {method: "POST", body: JSON.stringify({file_id: uploaded.file_id}), headers: {"Idempotency-Key": crypto.randomUUID()}});
          item.intakeId = task.resource_id;
        }
        setProgress(progress, "正在识别题目与作答", `${index + 1}/${files.length} · ${item.file.name}`);
        const candidate = await api(`/v1/intakes/${item.intakeId}/model-candidate`, {method: "POST", body: JSON.stringify({refresh: true})});
        const extractedItems = Array.isArray(candidate.items) && candidate.items.length ? candidate.items : [candidate];
        for (const extracted of extractedItems) {
          pendingIntakes.push({
            intakeId: extracted.intake_id || item.intakeId,
            itemNo: extracted.item_no || 1,
            inputVersion: extracted.input_version || 1,
            status: extracted.status || "extracting",
            fileName: extractedItems.length > 1 ? `${item.file.name} · 第 ${extracted.item_no || 1} 题` : item.file.name,
            questionText: extracted.question_text || "",
            answerText: extracted.answer_text || "",
          });
        }
        recognized += extractedItems.filter(extracted => extracted.question_text).length;
        item.state = "done";
        completed += 1;
      } catch (error) {
        item.state = "failed";
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
    } else setProgress(progress, "附件发送失败", "文件仍保留在输入框中，可以重试。", "error");
  }

  async function restorePendingIntakes() {
    try {
      const result = await api("/v1/intakes");
      if (activeIntake || pendingIntakes.length || !Array.isArray(result.items)) return;
      pendingIntakes = result.items.map(item => ({
        intakeId: item.intake_id, itemNo: item.item_no, inputVersion: item.input_version,
        status: item.status, fileName: `待确认题目 ${item.item_no}`,
        questionText: item.question_text || "", answerText: item.answer_text || "",
      }));
      pendingIntakes.forEach((intake, index) => {
        intake.queueIndex = index + 1;
        intake.queueTotal = pendingIntakes.length;
      });
      if (pendingIntakes.length) activateNextIntake("已恢复上次尚未处理完的题目。" );
    } catch (_) {}
  }

  async function confirmAndGrade() {
    if (!activeIntake?.questionText || activeIntake.status !== "waiting_confirmation") {
      assistantTurn("当前还没有可确认的完整题干。请直接告诉我题干、作答或需要修正的内容。", true);
      return;
    }
    const progress = progressTurn();
    setProgress(progress, "正在确认题干与作答", "锁定当前版本，准备判题。" );
    const confirmed = await api(`/v1/intakes/${activeIntake.intakeId}/confirm`, {method: "POST", body: JSON.stringify({input_version: activeIntake.inputVersion}), headers: {"Idempotency-Key": crypto.randomUUID()}});
    activeAttempt = confirmed.resource_id;
    stage = "grade";
    setComposerState();
    setProgress(progress, "正在判题", "定位第一处实质错误并生成完整解法。" );
    activeCandidate = await api(`/v1/attempts/${activeAttempt}/model-grade`, {method: "POST", body: JSON.stringify({input_version: activeIntake.inputVersion})});
    appendCandidate(activeCandidate);
    if (activeCandidate.verdict === "correct") {
      setProgress(progress, "判题候选已生成", "本题正确，无需入本，正在继续下一题。", "complete");
      activateNextIntake("本题判定正确，无需写入错题本。" );
      return;
    }
    const canCommit = ["incorrect", "partial"].includes(activeCandidate.verdict);
    setProgress(progress, "判题候选已生成", canCommit ? "可继续追问或修正；确认后发送“确认入本”。" : "本题不会自动入本；可以继续追问或发送“下一题”。", "complete");
  }

  async function commitCurrent() {
    if (!activeCandidate || !["incorrect", "partial"].includes(activeCandidate.verdict)) {
      assistantTurn("当前结果不能写入错题本。可以继续追问，或发送“下一题”。", true);
      return;
    }
    const entry = await api(`/v1/grade-results/${activeCandidate.result_id}/commit`, {method: "POST", body: JSON.stringify({input_version: activeCandidate.input_version}), headers: {"Idempotency-Key": crypto.randomUUID()}});
    let message = "已写入错题本并安排首次复习。";
    try {
      const recommendations = await api(`/v1/errors/${entry.error_id}/recommendations`, {method: "POST", headers: {"Idempotency-Key": crypto.randomUUID()}});
      message = `已写入错题本并安排首次复习，已匹配 ${recommendations.items.length} 道已验证练习。`;
    } catch (_) {}
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
  }

  async function sendMessage() {
    const message = chatInput.value.trim();
    if (!message || busy) return;
    chatInput.value = "";
    chatInput.style.height = "auto";
    userTurn(message);
    if (!activeIntake) {
      assistantTurn("请先添加题目图片、PDF 或 DOCX，我才能结合题目继续处理。");
      setComposerState();
      chatInput.focus();
      return;
    }
    busy = true;
    setComposerState();
    try {
      if (stage === "intake" && confirmIntakeCommands.has(message)) await confirmAndGrade();
      else if (stage === "grade" && commitCommands.has(message)) await commitCurrent();
      else if (stage === "grade" && nextCommands.has(message)) activateNextIntake("本题未写入错题本，继续处理下一份。" );
      else {
        const progress = progressTurn();
        setProgress(progress, "正在思考", stage === "intake" ? "结合图片和当前题干理解你的修正。" : "结合当前判题候选理解你的问题。" );
        await chatTurn(message, progress);
        setProgress(progress, "本轮已完成", "会话上下文已保留，可以继续输入。", "complete");
      }
    } catch (error) {
      assistantTurn(`本轮未完成：${authError(error)}。你的消息没有触发写库，可以重试。`, true);
    } finally {
      busy = false;
      setComposerState();
      if (!chatInput.disabled) chatInput.focus();
    }
  }

  $("#load-older").addEventListener("click", () => {
    const thread = $("#chat-thread");
    const height = thread.scrollHeight;
    visibleTurnCount += CHAT_PAGE_SIZE;
    renderConversationWindow();
    requestAnimationFrame(() => { thread.scrollTop += thread.scrollHeight - height; });
  });
  uploadInput.addEventListener("change", () => { addUploadFiles(uploadInput.files); uploadInput.value = ""; });
  $("#file-picker").addEventListener("click", () => uploadInput.click());
  dropZone.addEventListener("click", event => { if (!event.target.closest("button, textarea")) chatInput.focus(); });
  chatInput.addEventListener("input", () => { chatInput.style.height = "auto"; chatInput.style.height = `${Math.min(chatInput.scrollHeight, 150)}px`; setComposerState(); });
  chatInput.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#upload-form").requestSubmit(); } });
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
    if (opener) openImagePreview(opener.dataset.previewUrl, opener.dataset.previewName);
  });
  previewDialog?.addEventListener("click", event => { if (event.target === previewDialog) previewDialog.close(); });
  previewDialog?.addEventListener("close", () => { previewContent.removeAttribute("src"); });
  uploadSurface.addEventListener("dragover", event => { if (Array.from(event.dataTransfer?.types || []).includes("Files")) { event.preventDefault(); uploadSurface.classList.add("drag-active"); } });
  uploadSurface.addEventListener("dragleave", event => { if (!event.relatedTarget || !uploadSurface.contains(event.relatedTarget)) uploadSurface.classList.remove("drag-active"); });
  uploadSurface.addEventListener("drop", event => { if (event.dataTransfer?.files?.length) { event.preventDefault(); uploadSurface.classList.remove("drag-active"); addUploadFiles(event.dataTransfer.files); } });
  document.addEventListener("paste", event => { const files = event.clipboardData?.files; if (files?.length) { event.preventDefault(); addUploadFiles(files); } });
  $("#upload-form").addEventListener("submit", event => { event.preventDefault(); uploadFiles.some(item => !item.submitted && ["queued", "failed"].includes(item.state)) ? uploadQueued() : sendMessage(); });
  window.addEventListener("beforeunload", () => uploadFiles.forEach(item => item.previewUrl && URL.revokeObjectURL(item.previewUrl)));
  setComposerState();
  restorePendingIntakes();
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
