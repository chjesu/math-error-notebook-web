export const inject = ["workspaceRegistry", "tools", "attachments"];

const receiptStatuses = ["saved", "already_saved", "not_saved_correct", "needs_review", "review_unmatched", "review_waiting", "review_completed", "review_corrected", "review_needs_correction", "review_reference_only", "review_stale", "review_retryable"];
const reviewLocatorSchema = {
  type: "object", additionalProperties: false,
  required: ["code", "pdf_id", "error_id", "question_id", "stage", "kind"],
  properties: {
    code: {type: "string"}, pdf_id: {type: "string"}, error_id: {type: "string"}, question_id: {type: "string"},
    stage: {type: "integer", enum: [0, 1, 2, 3, 4, 5, 6]}, kind: {type: "string", enum: ["", "original", "recommendation"]}
  },
  description: "Copy only visible review code, PDF name/id, printed error/question id, stage and original/recommendation kind. Unknown strings are empty and stage is 0. Never guess. Ordinary new questions use all empty values."
};
const transcriptionByAgent = new WeakMap();
const pendingAdjudicationByAgent = new WeakMap();
const pendingReviewAssociationByAgent = new WeakMap();
const reviewAssociationReceiptsByAgent = new WeakMap();

function newBatchReference() {
  return globalThis.crypto.randomUUID();
}

function itemKey(item) {
  return `${item.attachment_index}:${item.item_no}`;
}

function protectedMarker(kind, id, status) {
  return {key: `${kind}:${id}`, status};
}

function candidateMarkers(items, status) {
  return items
    .filter((item) => item?.candidate_id && Number.isInteger(item.input_version))
    .map((item) => protectedMarker("candidate", `${item.candidate_id}:${item.input_version}`, status));
}

function candidateNeedsProtection(item) {
  return Boolean(item?.reference_review || item?.review_match_candidates?.length
    || ["review_unmatched", "review_retryable"].includes(item?.receipt_status || item?.status));
}

function protectedTrailer(markers) {
  return markers.length ? `LZLM_PROTECTED_V1 ${JSON.stringify(markers)}` : "";
}

function replacementCandidateMarkers(newItems, oldItems) {
  return [
    ...candidateMarkers(oldItems, "resolved"),
    ...candidateMarkers(newItems, "active")
  ];
}

function saveReviewQueue(agent, items, resetReceipts = false) {
  const queue = [...new Map(items.map((item) => [`${item.candidate_id}:${item.input_version}`, item])).values()];
  if (queue.length) pendingReviewAssociationByAgent.set(agent, queue);
  else pendingReviewAssociationByAgent.delete(agent);
  if (resetReceipts) reviewAssociationReceiptsByAgent.delete(agent);
  return queue.length > 0;
}

function reviewQueuePage(agent) {
  return JSON.stringify((pendingReviewAssociationByAgent.get(agent) || []).slice(0, 20).map(({codes: _codes, ...item}) => item));
}

function gradingBatchText(frozen, items = frozen.items) {
  return JSON.stringify({
    batch_ref: frozen.batchRef,
    items: items.map((item) => ({
      attachment_index: item.attachment_index,
      item_no: item.item_no,
      question_text: item.question_text,
      answer_text: item.answer_text,
      review: item.review,
      grading_strategy: item.grading_strategy,
      ...(item.reference ? {reference: item.reference} : {})
    }))
  });
}

function candidatePayload(value) {
  const referenceConflicts = value.results
    .filter((item) => item.reference_review)
    .map((item) => ({candidate_id: item.candidate_id, input_version: item.input_version, ...item.reference_review}));
  const reviewLinks = value.next_review_batch_json ? JSON.parse(value.next_review_batch_json) : [];
  return referenceConflicts.length || reviewLinks.length
    ? JSON.stringify({reference_conflicts: referenceConflicts, review_links: reviewLinks})
    : "";
}

function rememberPendingAdjudication(agent, results) {
  const items = results.filter((item) => item.reference_review).map((item) => ({candidate_id: item.candidate_id, input_version: item.input_version}));
  if (items.length) pendingAdjudicationByAgent.set(agent, items);
  else pendingAdjudicationByAgent.delete(agent);
  return items.length > 0;
}

function rememberPendingReviewAssociations(agent, results) {
  const items = results
    .filter((item) => item.receipt_status === "review_unmatched" && Array.isArray(item.review_match_candidates) && item.review_match_candidates.length)
    .map((item) => ({
      candidate_id: item.candidate_id,
      input_version: item.input_version,
      question_text: item.question_text,
      options: item.review_match_candidates,
      codes: item.review_match_candidates.map((candidate) => candidate.code)
    }));
  return saveReviewQueue(agent, items, true);
}

function refreshPendingReviewAssociations(agent, adjudicated, results) {
  const replaced = new Set(adjudicated.map((item) => `${item.candidate_id}:${item.input_version}`));
  const refreshed = new Map(
    (pendingReviewAssociationByAgent.get(agent) || [])
      .filter((item) => !replaced.has(`${item.candidate_id}:${item.input_version}`))
      .map((item) => [`${item.candidate_id}:${item.input_version}`, item])
  );
  for (const item of results) {
    const key = `${item.candidate_id}:${item.input_version}`;
    refreshed.delete(key);
    if (item.status === "review_unmatched" && Array.isArray(item.review_match_candidates) && item.review_match_candidates.length) {
      refreshed.set(key, {
        candidate_id: item.candidate_id,
        input_version: item.input_version,
        question_text: item.question_text,
        options: item.review_match_candidates,
        codes: item.review_match_candidates.map((candidate) => candidate.code)
      });
    }
  }
  const items = [...refreshed.values()];
  return saveReviewQueue(agent, items);
}

function rememberInspectedPendingReviewAssociations(agent, context) {
  const items = (Array.isArray(context?.pending_review_links) ? context.pending_review_links : [])
    .filter((item) => typeof item?.candidate_id === "string" && Number.isInteger(item.input_version) && Array.isArray(item.options) && item.options.length)
    .map((item) => ({
      candidate_id: item.candidate_id,
      input_version: item.input_version,
      question_text: item.question_text,
      options: item.options,
      codes: item.options.map((candidate) => candidate.code)
    }));
  return saveReviewQueue(agent, items, true);
}

function toolRequestError(label, response, payload) {
  const code = payload?.error?.code;
  return new Error(`${label} (${response.status}${code ? `: ${code}` : ""})`);
}

function nextStepText(results) {
  const statuses = results.map((item) => item.receipt_status || item.status);
  if (statuses.includes("review_retryable")) return "下一步：直接在会话重试确认复习记录，无需重新上传图片。";
  if (results.some((item) => item.reference_review)) {
    return "下一步：系统将继续核对已验证题库解析，请等待本轮复核完成。";
  }
  if (results.some((item) => item.reference_status === "conflict")) return "下一步：继续核对已保存的题库复核结果，无需重新上传图片或反复确认题干。";
  if (results.some((item) => (item.receipt_status || item.status) === "review_unmatched" && item.review_match_candidates?.length)) {
    return "下一步：系统将继续核对当前账号下的 PDF 候选；请等待本轮语义关联完成。";
  }
  if (statuses.includes("review_unmatched")) return "下一步：请补充 PDF 名称、错题编号与阶段或复习码；直接在会话补充即可，无需再次上传图片。";
  if (statuses.includes("review_waiting")) return "下一步：按复习回执补齐该组尚未上传的必做题；若提示尚未到期，则到期后再确认。";
  if (statuses.includes("review_needs_correction")) return "下一步：先依据错因与解析订正本组题目，再按回执中的日期复习。";
  if (statuses.includes("review_stale")) return "下一步：打开错题本查看当前复习计划，这份旧练习单不会改变新的阶段。";
  if (statuses.includes("review_completed")) return "下一步：本组复习已完成，请按回执的下次日期复习，也可继续提交其他组的作答。";
  if (statuses.includes("review_corrected")) return "下一步：本组订正已完成，复习阶段与日期不变；可继续订正其他题目。";
  if (statuses.includes("review_reference_only")) return "下一步：提交同组标记为必做的原题或推荐训练题。";
  if (statuses.includes("needs_review")) {
    return "下一步：请补充更清晰的题目或作答图片，也可以直接说明需要修正的题干、作答或解题过程。";
  }
  if (statuses.includes("saved") || statuses.includes("already_saved")) {
    return "下一步：可打开「错题本」查看错因和知识点并选择今日复习，也可以继续上传下一张题目图片。";
  }
  return "下一步：本轮题目无需计入错题本；可以继续上传下一张题目图片，或在当前会话追问解析。";
}

function errorIdText(value) {
  const prefix = "错题编号（error_id）：";
  if (typeof value.error_id === "string" && /^[0-9a-f]{32}$/.test(value.error_id)) return prefix + value.error_id;
  const status = value.receipt_status || value.status;
  if (status === "not_saved_correct") return prefix + "无（本题正确，未计入错题本）";
  if (status === "review_unmatched") return prefix + "待确认（尚未关联原错题）";
  return prefix + "暂不可用（等待入本或关联确认）";
}

function receiptText(value) {
  const lines = [errorIdText(value), value.message];
  const referenceLabels = {consistent: "已与已验证题库解析核对一致", conflict: "与题库解析冲突，等待复核", not_found: "题库未匹配"};
  lines.push(`题库核验：${referenceLabels[value.reference_status]}`);
  lines.push(`知识点：${value.knowledge_point_count} 个`);
  if (!value.status.startsWith("review_")) lines.push(`复习任务：${value.review_status === "scheduled" ? "已安排" : "未安排"}`);
  lines.push(nextStepText([value]));
  return [{type: "text", text: lines.join("\n")}];
}

function receiptTool() {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness receipt bridge is not configured");
  return {
    name: "confirm_error_notebook_entry",
    description: "Confirm a frozen notebook/review result only when the latest authoritative process receipt is review_unmatched or review_retryable. For review_unmatched, once the student supplies printed PDF locating details, pass review to link the already-graded photo without re-uploading. For review_retryable, omit review. Never call this tool for review_waiting or any already matched process result; those results are already saved. Never invent ids or claim success before this authoritative receipt.",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["candidate_id", "input_version"],
      properties: {
        candidate_id: {type: "string", description: "Frozen product grade candidate id."},
        input_version: {type: "integer", description: "Frozen input version paired with the candidate."},
        review: reviewLocatorSchema
      }
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        required: ["schema", "status", "reference_status", "knowledge_point_count", "review_status", "message"],
        properties: {
          schema: {type: "string", const: "math-notebook-entry-receipt/v1"},
          candidate_id: {type: "string"}, input_version: {type: "integer"},
          status: {type: "string", enum: receiptStatuses},
          reference_status: {type: "string", enum: ["consistent", "conflict", "not_found"]},
          reference_question_id: {type: "string"},
          reference_version_no: {type: "integer"},
          error_id: {type: "string"},
          knowledge_point_count: {type: "integer"},
          review_status: {type: "string"},
          completed_question_count: {type: "integer"}, required_question_count: {type: "integer"},
          review_result: {type: "string"}, completed_at: {type: "string"}, replayed: {type: "boolean"},
          next_stage: {oneOf: [{type: "integer"}, {type: "null"}]}, next_due_at: {oneOf: [{type: "string"}, {type: "null"}]},
          message: {type: "string"}
        }
      },
      render: (args, value) => {
        const rendered = receiptText(value);
        rendered[0].text += `\n${protectedTrailer(candidateMarkers([args], candidateNeedsProtection(value) ? "active" : "resolved"))}`;
        return rendered;
      }
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Notebook receipt requires an owning Harness session");
      if (latestUserImages(exec.agent).length) {
        throw new Error("当前图片已由 process_error_notebook_attachments 处理；本轮禁止再次调用确认工具，请直接采用处理或复核回执完成回复。");
      }
      const response = await fetch(`${origin}/v1/internal/harness/grade-results/${args.candidate_id}/commit`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, input_version: args.input_version, ...(Object.values(args.review || {}).some(Boolean) ? {review: args.review} : {})}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || !payload.receipt) throw toolRequestError("Notebook receipt failed", response, payload);
      exec.concludeTurn();
      return payload.receipt;
    },
    presentCall: () => ({card: "generic", title: "确认错题本记录", kind: "other", rawInput: null})
  };
}

function latestUserImages(agent) {
  if (!agent.session?.deriveMessages) return [];
  const messages = agent.session.deriveMessages();
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== "user") continue;
    return Array.isArray(message.content) ? message.content.filter((block) => block.type === "image") : [];
  }
  return [];
}

function latestUserText(agent) {
  const messages = agent.session.deriveMessages();
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== "user") continue;
    if (typeof message.content === "string") return message.content;
    return Array.isArray(message.content)
      ? message.content.filter((block) => block.type === "text" && typeof block.text === "string").map((block) => block.text).join("\n")
      : "";
  }
  return "";
}

function currentTurn(agent) {
  const events = agent.session.events;
  if (!Array.isArray(events)) return null;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index]?.type === "turn/start") return events[index].data?.turn ?? null;
  }
  return null;
}

const missingImageMessage = "当前消息没有可读取的图片附件。请重新添加图片，确认缩略图出现并完成上传后，再发送判题请求。";

function removeErrorTool() {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  return {
    name: "remove_error_notebook_entry",
    description: "Remove one existing error-notebook entry without asking the student to upload its image again. First identify the exact error_id from an authoritative receipt in this conversation. If the latest user message does not contain both the exact error_id and the words 确认移除错题, ask once for: 确认移除错题 <error_id>. Call this tool exactly once only after that explicit confirmation.",
    parameters: {
      type: "object", additionalProperties: false, required: ["error_id"],
      properties: {error_id: {type: "string", pattern: "^[0-9a-f]{32}$", description: "Authoritative error id previously returned by the product."}}
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["schema", "status", "error_id", "message"],
        properties: {
          schema: {type: "string", const: "math-notebook-removal-receipt/v1"},
          status: {type: "string", const: "removed"},
          error_id: {type: "string"},
          message: {type: "string"}
        }
      },
      render: (_args, value) => [{type: "text", text: `${value.message}\n错题编号：${value.error_id}`}]
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Notebook removal requires an owning Harness session");
      const confirmationText = latestUserText(exec.agent);
      const compact = confirmationText.replace(/\s+/g, "").toLowerCase();
      if (!compact.includes("确认移除错题") || !compact.includes(args.error_id.toLowerCase())) {
        throw new Error(`请让学生回复“确认移除错题 ${args.error_id}”后再执行。`);
      }
      const response = await fetch(`${origin}/v1/internal/harness/errors/remove`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, error_id: args.error_id, confirmation_text: confirmationText}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || !payload.receipt) throw toolRequestError("Notebook removal failed", response, payload);
      exec.concludeTurn();
      return payload.receipt;
    },
    presentCall: () => ({card: "generic", title: "移除错题", kind: "other", rawInput: null})
  };
}

function processResultText(value) {
  if (value.results.length === 0) return [{type: "text", text: missingImageMessage}];
  const lines = [];
  for (const item of value.results) {
    const association = item.review_association || {status: "not_review"};
    lines.push(
      `图片 ${item.attachment_index} · 第 ${item.item_no} 题`,
      errorIdText(item),
      `题目：${item.question_text}`,
      `学生作答：${item.answer_text || "未作答"}`,
      `判定：${item.verdict}`,
      `第一处实质错误：${item.first_error || "无"}`,
      `错因分析：${item.cause_evidence || "无"}`,
      `知识点：${item.knowledge_points.join("、") || "无"}`,
      `详细解析：${item.correct_solution || "无"}`,
      `最终答案：${item.final_answer || "无"}`,
      `小建议：${item.prevention_cue || "无"}`,
      association.status === "matched"
        ? `复习关联：已命中 PDF ${association.pdf_id}，复习码 ${association.review_code}，错题 ${association.error_id}，第 ${association.stage} 阶段`
        : association.status === "unmatched" ? "复习关联：未找到唯一 PDF 记录" : "复习关联：普通判题，无需关联 PDF",
      item.receipt_message,
      ""
    );
    if (item.receipt_status === "review_unmatched" && !item.review_match_candidates?.length) {
      lines.push(`仅供补充关联使用：candidate_id=${item.candidate_id}, input_version=${item.input_version}。学生补充 PDF 定位后调用 confirm_error_notebook_entry 并附 review，不要要求重传。`, "");
    } else if (item.receipt_status === "review_retryable") {
      lines.push(`仅供重试确认使用：candidate_id=${item.candidate_id}, input_version=${item.input_version}。调用 confirm_error_notebook_entry 时不要附 review，也不要要求重传。`, "");
    }
  }
  lines.push(value.results.some((item) => item.reference_review)
    ? "Agent 下一动作：只调用 adjudicate_error_notebook_reference_conflicts，一次提交上述全部待复核候选。"
    : value.results.some((item) => item.receipt_status === "review_unmatched" && item.review_match_candidates?.length)
      ? "Agent 下一动作：只调用 adjudicate_practice_review_associations，按当前队列顺序提交前 20 项；之后按 next_review_batch_json 分批继续。"
      : "Agent 下一动作：直接生成最终回复；禁止再调用 confirm_error_notebook_entry。", "");
  if (value.usage) {
    lines.push(`今日学习负荷：已判题 ${value.usage.grade.count}/${value.usage.grade.limit}（建议 ${value.usage.grade.target}）；已生成推荐题 ${value.usage.recommendation.count}/${value.usage.recommendation.limit}（建议 ${value.usage.recommendation.target}）。`, "");
  }
  const candidates = candidatePayload(value);
  if (candidates) lines.push("当前批次完整候选（唯一副本，仅供内部工具调用，不得向学生复述）：", candidates);
  lines.push(nextStepText(value.results));
  const trailer = protectedTrailer([
    ...candidateMarkers(value.results.filter(candidateNeedsProtection), "active"),
    ...(value.batch_ref ? [protectedMarker("batch", value.batch_ref, "resolved")] : [])
  ]);
  if (trailer) lines.push(trailer);
  return [{type: "text", text: lines.join("\n").trim()}];
}

function referenceReviewText(item) {
  return [
    "以下字段仅供复核工具调用，不得向用户展示：",
    `candidate_id=${item.candidate_id}`,
    `input_version=${item.input_version}`,
    `题库来源：${item.reference_review.source_title}（第 ${item.reference_review.version_no} 版）`,
    `冻结的独立答案：${item.reference_review.independent_answer}`,
    `题库参考答案：${item.reference_review.reference_answer}`,
    `题库参考解析：${item.reference_review.reference_solution || "无"}`
  ];
}

function transcribeAttachmentsTool(ctx) {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  const itemProperties = {
    attachment_index: {type: "integer"},
    item_no: {type: "integer"},
    question_text: {type: "string"},
    answer_text: {type: "string"},
    review: reviewLocatorSchema
  };
  const referenceProperties = {
    question_id: {type: "string"}, version_no: {type: "integer"}, question_text: {type: "string"},
    reference_answer: {type: "string"}, reference_solution: {type: "string"}, source_title: {type: "string"}
  };
  const resolvedItemProperties = {
    ...itemProperties,
    grading_strategy: {type: "string", enum: ["verified_reference", "independent"]},
    reference: {oneOf: [
      {type: "null"},
      {type: "object", additionalProperties: false, required: Object.keys(referenceProperties), properties: referenceProperties}
    ]}
  };
  return {
    name: "transcribe_error_notebook_attachments",
    description: "Required first when the latest user message contains 1-10 math images. Do OCR and segmentation only: for each image copy every printed question stem separately from the student's final handwritten answer, in image order. Do not solve or grade yet. Use one-based attachment_index and restart item_no at 1 for every image. The returned transcription is frozen for the later processing call in this same turn.",
    parameters: {
      type: "object", additionalProperties: false, required: ["items"],
      properties: {items: {type: "array", minItems: 1, maxItems: 200, items: {type: "object", additionalProperties: false, required: Object.keys(itemProperties), properties: itemProperties}}}
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["schema", "batch_ref", "items"],
        properties: {
          schema: {type: "string", const: "math-notebook-transcription/v1"},
          batch_ref: {type: "string"},
          superseded_batch_ref: {type: "string"},
          items: {type: "array", items: {type: "object", additionalProperties: false, required: Object.keys(resolvedItemProperties), properties: resolvedItemProperties}}
        }
      },
      render: (_args, value) => value.items.length === 0 ? [{type: "text", text: missingImageMessage}] : [{type: "text", text: [
        "题干与作答已经冻结，不得重新识别或改写。严格按每题 grading_strategy 判题：",
        ...value.items.flatMap((item) => item.grading_strategy === "verified_reference" ? [
          `图片 ${item.attachment_index} · 第 ${item.item_no} 题题干：${item.question_text}`,
          `图片 ${item.attachment_index} · 第 ${item.item_no} 题学生作答：${item.answer_text || "未作答"}`,
          "判题策略：verified_reference。二维码或精确题库编号已关联到已验证当前版本；禁止重新完整解题，只核对学生作答与以下参考答案是否等价。处理时宿主会使用该授权版本的答案与解析，不要复制它们。",
          `当前题库题干：${item.reference.question_text}`,
          `当前题库参考答案：${item.reference.reference_answer}`,
          `当前题库参考解析：${item.reference.reference_solution || "暂无解析"}`,
          `题库版本：${item.reference.version_no}；来源：${item.reference.source_title}`
        ] : [
          `图片 ${item.attachment_index} · 第 ${item.item_no} 题题干：${item.question_text}`,
          `图片 ${item.attachment_index} · 第 ${item.item_no} 题学生作答：${item.answer_text || "未作答"}`,
          "判题策略：independent。没有取得精确且已验证的当前参考，必须独立解题并提交完整解析与最终答案。"
        ]),
        `处理批次引用：${value.batch_ref}。调用 process_error_notebook_attachments 时只提交该引用、题目位置和新生成的判定字段。`,
        protectedTrailer([
          protectedMarker("batch", value.batch_ref, "active"),
          ...(value.superseded_batch_ref ? [protectedMarker("batch", value.superseded_batch_ref, "resolved")] : [])
        ])
      ].join("\n")}]
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Notebook transcription requires an owning Harness session");
      const images = latestUserImages(exec.agent);
      if (images.length === 0) {
        exec.concludeTurn();
        return {schema: "math-notebook-transcription/v1", batch_ref: "", items: []};
      }
      if (images.length > 10) throw new Error("一条消息最多上传 10 张图片，请分批发送");
      if (images.some((_image, imageIndex) => {
        const items = args.items.filter((item) => item.attachment_index === imageIndex + 1);
        return items.length === 0 || items.some((item, itemIndex) => item.item_no !== itemIndex + 1);
      }) || args.items.some((item) => item.attachment_index < 1 || item.attachment_index > images.length)) {
        throw new Error("每张图片都必须使用对应 attachment_index，且 item_no 从 1 连续编号");
      }
      const items = args.items.map((item) => ({
        ...item,
        question_text: item.question_text.trim(),
        answer_text: item.answer_text.trim()
      }));
      if (items.some((item) => item.question_text === "")) throw new Error("Every transcription item requires a question stem");
      const storedImages = [];
      const references = new Map();
      for (let attachmentIndex = 1; attachmentIndex <= images.length; attachmentIndex += 1) {
        const stored = await ctx.attachments.readImage(images[attachmentIndex - 1].attachment, exec.signal);
        storedImages.push(stored);
        const attachmentItems = items.filter((item) => item.attachment_index === attachmentIndex);
        const response = await fetch(`${origin}/v1/internal/harness/grading-references`, {
          method: "POST",
          headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
          body: JSON.stringify({
            session_id: exec.agent.id,
            attachment: {
              attachment_id: String(stored.ref.attachmentId),
              name: stored.ref.name || `question-${attachmentIndex}.${stored.ref.mediaType === "image/png" ? "png" : "jpg"}`,
              media_type: stored.ref.mediaType,
              data: Buffer.from(stored.data).toString("base64")
            },
            items: attachmentItems.map((item) => ({item_no: item.item_no, question_text: item.question_text, review: item.review}))
          }),
          signal: exec.signal
        });
        const payload = await response.json();
        if (!response.ok || !Array.isArray(payload.items) || payload.items.length !== attachmentItems.length) {
          throw toolRequestError("Grading reference lookup failed", response, payload);
        }
        for (const item of payload.items) references.set(`${attachmentIndex}:${item.item_no}`, item);
      }
      const resolvedItems = items.map((item) => {
        const resolved = references.get(`${item.attachment_index}:${item.item_no}`);
        if (!resolved || !["verified_reference", "independent"].includes(resolved.grading_strategy)) {
          throw new Error("Grading reference lookup returned an invalid item");
        }
        return {...item, grading_strategy: resolved.grading_strategy, reference: resolved.reference};
      });
      const superseded = transcriptionByAgent.get(exec.agent);
      const frozen = {batchRef: newBatchReference(), images, storedImages, items: resolvedItems, completed: new Map(), recoveryAttempts: new Map(), userText: latestUserText(exec.agent), turn: currentTurn(exec.agent)};
      transcriptionByAgent.set(exec.agent, frozen);
      return {schema: "math-notebook-transcription/v1", batch_ref: frozen.batchRef, ...(superseded ? {superseded_batch_ref: superseded.batchRef} : {}), items: resolvedItems};
    },
    presentCall: () => ({card: "generic", title: "识别题干与作答", kind: "other", rawInput: null})
  };
}

function processAttachmentsTool(ctx) {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  const itemProperties = {
    attachment_index: {type: "integer", description: "One-based image index in the latest user message."},
    item_no: {type: "integer", description: "One-based question order within that image."},
    verdict: {type: "string", enum: ["correct", "partial", "incorrect", "unclear"]},
    first_error: {type: "string"},
    cause_code: {type: "string", enum: ["", "knowledge_gap", "concept_confusion", "formula_condition", "method_choice", "reasoning_gap", "algebra_transform", "calculation", "misreading", "incomplete_cases", "expression", "careless", "unclear"]},
    cause_evidence: {type: "string"},
    knowledge_points: {type: "array", items: {type: "string"}},
    correct_solution: {type: "string"},
    final_answer: {type: "string"},
    prevention_cue: {type: "string"},
    confidence: {type: "number"},
  };
  const resultProperties = {
    attachment_index: {type: "integer"}, item_no: {type: "integer"}, candidate_id: {type: "string"}, input_version: {type: "integer"},
    verdict: {type: "string", enum: ["correct", "partial", "incorrect", "unclear"]}, question_text: {type: "string"}, answer_text: {type: "string"},
    first_error: {type: "string"}, cause_code: {type: "string"}, cause_evidence: {type: "string"}, knowledge_points: {type: "array", items: {type: "string"}},
    correct_solution: {type: "string"}, final_answer: {type: "string"}, prevention_cue: {type: "string"},
    receipt_status: {type: "string", enum: receiptStatuses}, receipt_message: {type: "string"}, error_id: {type: "string"},
    review_association: {
      type: "object", additionalProperties: false,
      required: ["status", "pdf_id", "review_code", "error_id", "question_id", "stage", "kind"],
      properties: {
        status: {type: "string", enum: ["not_review", "matched", "unmatched"]},
        pdf_id: {type: "string"}, review_code: {type: "string"}, error_id: {type: "string"}, question_id: {type: "string"},
        stage: {type: "integer", enum: [0, 1, 2, 3, 4, 5, 6]}, kind: {type: "string", enum: ["", "original", "recommendation"]}
      }
    },
    review_match_candidates: {
      type: "array",
      items: {
        type: "object", additionalProperties: false,
        required: ["code", "pdf_id", "pdf_name", "error_id", "question_id", "kind", "stage", "stem_text", "match_score", "candidate_source", "generated_at", "started"],
        properties: {
          code: {type: "string"}, pdf_id: {type: "string"}, pdf_name: {type: "string"}, error_id: {type: "string"},
          question_id: {type: "string"}, kind: {type: "string", enum: ["original", "recommendation"]}, stage: {type: "integer"},
          stem_text: {type: "string"}, match_score: {type: "number"},
          candidate_source: {type: "string", enum: ["visible_identity", "verified_question", "semantic_candidate"]},
          generated_at: {oneOf: [{type: "string"}, {type: "null"}]}, started: {type: "boolean"}
        }
      }
    },
    reference_review: {
      oneOf: [
        {type: "null"},
        {
          type: "object", additionalProperties: false,
          required: ["source_title", "version_no", "independent_answer", "reference_answer", "reference_solution"],
          properties: {
            source_title: {type: "string"}, version_no: {type: "integer"}, independent_answer: {type: "string"},
            reference_answer: {type: "string"}, reference_solution: {type: "string"}
          }
        }
      ]
    }
  };
  return {
    name: "process_error_notebook_attachments",
    description: "Required after transcribe_error_notebook_attachments. Copy its opaque batch_ref and submit each still-pending item once in frozen image order. Do not copy question_text, answer_text, review, grading_strategy, or verified reference answer/solution: the host rehydrates those immutable fields from this session's exact frozen batch. For independent items, provide complete correct_solution and final_answer; for verified_reference items, only grade equivalence and the host supplies the authorized current answer and solution. The host rejects stale, reordered, duplicate, missing, or out-of-batch positions, rechecks reference freshness and preserves confirmed images across a bounded recovery. Never invent ids.",
    parameters: {
      type: "object", additionalProperties: false, required: ["batch_ref", "items"],
      properties: {
        batch_ref: {type: "string", description: "Opaque current batch reference returned by transcription or reference recovery."},
        items: {type: "array", items: {type: "object", additionalProperties: false, required: ["attachment_index", "item_no", "verdict", "first_error", "cause_code", "cause_evidence", "knowledge_points", "prevention_cue", "confidence"], properties: itemProperties}}
      }
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["schema", "results"],
        properties: {
          schema: {type: "string", const: "math-notebook-process-result/v1"},
          batch_ref: {type: "string"},
          next_review_batch_json: {type: "string"},
          results: {type: "array", items: {type: "object", additionalProperties: false, required: Object.keys(resultProperties), properties: resultProperties}},
          usage: {type: "object"}
        }
      },
      render: (_args, value) => processResultText(value)
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Notebook processing requires an owning Harness session");
      const frozen = transcriptionByAgent.get(exec.agent);
      if (!frozen || (frozen.turn !== null && currentTurn(exec.agent) !== frozen.turn)) throw new Error("请先调用 transcribe_error_notebook_attachments 冻结本批图片的题干与作答");
      const images = frozen.images;
      if (args.batch_ref !== frozen.batchRef) throw new Error("判题批次引用已过期；请使用最近一次工具返回的 batch_ref");
      const pending = frozen.items.filter((item) => !frozen.completed.has(itemKey(item)));
      if (args.items.length !== pending.length || args.items.some((item, index) => itemKey(item) !== itemKey(pending[index])) || new Set(args.items.map(itemKey)).size !== args.items.length) {
        throw new Error("判题提交必须按顺序且恰好包含当前冻结批次的全部未完成题目，不得遗漏、重复、倒置或串题");
      }
      if (args.items.some((item) => item.attachment_index < 1 || item.attachment_index > images.length)) {
        throw new Error("A result refers to an attachment outside the latest user message");
      }
      const submitted = new Map(args.items.map((item) => [itemKey(item), item]));
      let usage = null;
      for (let attachmentIndex = 1; attachmentIndex <= images.length; attachmentIndex += 1) {
        const frozenItems = pending.filter((item) => item.attachment_index === attachmentIndex);
        if (frozenItems.length === 0) continue;
        const items = frozenItems.map((expected) => {
          const grade = submitted.get(itemKey(expected));
          if (expected.grading_strategy === "independent" && grade.verdict !== "unclear" && (!grade.correct_solution?.trim() || !grade.final_answer?.trim())) {
            throw new Error("independent 判题必须提交完整解析与最终答案");
          }
          return {
            ...grade,
            correct_solution: grade.correct_solution || "",
            final_answer: grade.final_answer || "",
            question_text: expected.question_text,
            answer_text: expected.answer_text,
            review: expected.review,
            grading_strategy: expected.grading_strategy,
            ...(expected.grading_strategy === "verified_reference" ? {
              correct_solution: expected.reference.reference_solution || "",
              final_answer: expected.reference.reference_answer
            } : {})
          };
        });
        const stored = frozen.storedImages[attachmentIndex - 1];
        const response = await fetch(`${origin}/v1/internal/harness/intakes/process`, {
          method: "POST",
          headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
          body: JSON.stringify({
            session_id: exec.agent.id,
            review_mode: /复习|推荐题|练习单|PDF/i.test(frozen.userText) || items.some((item) => Object.values(item.review || {}).some(Boolean)),
            correction_mode: /改错|订正|纠正|重新判|改判/.test(frozen.userText),
            attachment: {
              attachment_id: String(stored.ref.attachmentId),
              name: stored.ref.name || `question-${attachmentIndex}.${stored.ref.mediaType === "image/png" ? "png" : "jpg"}`,
              media_type: stored.ref.mediaType,
              data: Buffer.from(stored.data).toString("base64")
            },
            items: items.map(({attachment_index: _attachmentIndex, ...item}) => item)
          }),
          signal: exec.signal
        });
        const payload = await response.json();
        if (!response.ok || !Array.isArray(payload.results)) {
          const code = payload.error?.code;
          if (code === "daily_grade_limit") throw new Error("今天已完成 40 道判题，请先复习和订正；新图片可明日继续处理。");
          if (code === "reference_changed") {
            const attempts = frozen.recoveryAttempts.get(attachmentIndex) || 0;
            if (attempts >= 1) throw new Error("题库参考在本轮恢复后再次变化；为避免使用不稳定参考，本轮停止且不会重写已确认结果。请重新发起判题");
            frozen.recoveryAttempts.set(attachmentIndex, attempts + 1);
            const refresh = await fetch(`${origin}/v1/internal/harness/grading-references`, {
              method: "POST",
              headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
              body: JSON.stringify({
                session_id: exec.agent.id,
                attachment: {
                  attachment_id: String(stored.ref.attachmentId), name: stored.ref.name,
                  media_type: stored.ref.mediaType, data: Buffer.from(stored.data).toString("base64")
                },
                items: frozenItems.map((item) => ({item_no: item.item_no, question_text: item.question_text, review: item.review}))
              }),
              signal: exec.signal
            });
            const refreshed = await refresh.json();
            if (!refresh.ok || !Array.isArray(refreshed.items) || refreshed.items.length !== frozenItems.length) {
              throw toolRequestError("Reference recovery failed", refresh, refreshed);
            }
            const byNumber = new Map(refreshed.items.map((item) => [item.item_no, item]));
            const refreshedItems = frozen.items.map((item) => item.attachment_index !== attachmentIndex ? item : {
              ...item,
              grading_strategy: byNumber.get(item.item_no)?.grading_strategy,
              reference: byNumber.get(item.item_no)?.reference
            });
            if (refreshedItems.some((item) => !["verified_reference", "independent"].includes(item.grading_strategy)
              || (item.grading_strategy === "verified_reference" && !item.reference))) throw new Error("Reference recovery returned an invalid item");
            frozen.items = refreshedItems;
            frozen.batchRef = newBatchReference();
            const stillPending = frozen.items.filter((item) => !frozen.completed.has(itemKey(item)));
            throw new Error(`题库参考已刷新为新的冻结版本；已确认题目不会重写。请按以下唯一当前批次重新判定未完成题目，无需重新上传：${gradingBatchText(frozen, stillPending)}\n${protectedTrailer([protectedMarker("batch", frozen.batchRef, "active"), protectedMarker("batch", args.batch_ref, "resolved")])}`);
          }
          throw toolRequestError("Notebook processing failed", response, payload);
        }
        if (payload.results.length !== frozenItems.length || frozenItems.some((expected) => payload.results.filter((item) => item.item_no === expected.item_no).length !== 1)) {
          throw new Error("Notebook processing returned missing, duplicate, reordered, or out-of-batch results");
        }
        usage = payload.usage || usage;
        for (const item of payload.results) frozen.completed.set(`${attachmentIndex}:${item.item_no}`, {...item, attachment_index: attachmentIndex});
      }
      transcriptionByAgent.delete(exec.agent);
      const results = frozen.items.map((item) => frozen.completed.get(itemKey(item)));
      rememberPendingAdjudication(exec.agent, results);
      rememberPendingReviewAssociations(exec.agent, results);
      return {schema: "math-notebook-process-result/v1", batch_ref: frozen.batchRef, results, next_review_batch_json: reviewQueuePage(exec.agent), ...(usage ? {usage} : {})};
    },
    presentCall: () => ({card: "generic", title: "整理并记录错题", kind: "other", rawInput: null})
  };
}

function adjudicateReferenceConflictsTool() {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  return {
    name: "adjudicate_error_notebook_reference_conflicts",
    description: "Required once after process_error_notebook_attachments or recheck_error_notebook_reference_conflict returns one or more reference_review objects. For each frozen candidate, compare the independent answer with the verified reference answer and solution. Use consistent only when they are mathematically equivalent. Use conflict for a substantive difference; in that case the verified current reference is authoritative, so regrade the student's answer against it and submit authoritative_grade. Use uncertain only when the evidence cannot decide. Submit every returned conflict in one call.",
    parameters: {
      type: "object", additionalProperties: false, required: ["items"],
      properties: {
        items: {
          type: "array", minItems: 1, maxItems: 20,
          items: {
            type: "object", additionalProperties: false,
            required: ["candidate_id", "input_version", "status", "rationale"],
            properties: {
              candidate_id: {type: "string"}, input_version: {type: "integer"},
              status: {type: "string", enum: ["consistent", "conflict", "uncertain"]},
              rationale: {type: "string", minLength: 20, maxLength: 4000},
              authoritative_grade: {
                type: "object", additionalProperties: false,
                required: ["verdict", "first_error", "cause_code", "cause_evidence", "knowledge_points", "prevention_cue", "confidence"],
                properties: {
                  verdict: {type: "string", enum: ["correct", "partial", "incorrect", "unclear"]},
                  first_error: {type: "string"},
                  cause_code: {type: "string", enum: ["", "knowledge_gap", "concept_confusion", "formula_condition", "method_choice", "reasoning_gap", "algebra_transform", "calculation", "misreading", "incomplete_cases", "expression", "careless", "unclear"]},
                  cause_evidence: {type: "string"},
                  knowledge_points: {type: "array", items: {type: "string"}},
                  prevention_cue: {type: "string"}, confidence: {type: "number"}
                }
              }
            }
          }
        }
      }
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["results", "review_pending", "next_review_batch_json"],
        properties: {
          review_pending: {type: "boolean"},
          next_review_batch_json: {type: "string"},
          results: {
            type: "array",
            items: {
              type: "object", additionalProperties: false,
              required: ["candidate_id", "input_version", "status", "receipt_message"],
              properties: {
                candidate_id: {type: "string"}, input_version: {type: "integer"},
                status: {type: "string", enum: receiptStatuses},
                receipt_message: {type: "string"}, error_id: {type: "string"}, question_text: {type: "string"},
                review_match_candidates: {
                  type: "array",
                  items: {
                    type: "object", additionalProperties: false,
                    required: ["code", "pdf_id", "pdf_name", "error_id", "question_id", "kind", "stage", "stem_text", "match_score", "candidate_source", "generated_at", "started"],
                    properties: {
                      code: {type: "string"}, pdf_id: {type: "string"}, pdf_name: {type: "string"}, error_id: {type: "string"},
                      question_id: {type: "string"}, kind: {type: "string", enum: ["original", "recommendation"]}, stage: {type: "integer"},
                      stem_text: {type: "string"}, match_score: {type: "number"},
                      candidate_source: {type: "string", enum: ["visible_identity", "verified_question", "semantic_candidate"]},
                      generated_at: {oneOf: [{type: "string"}, {type: "null"}]}, started: {type: "boolean"}
                    }
                  }
                }
              }
            }
          }
        }
      },
      render: (args, value) => [{type: "text", text: [
        ...value.results.map((item) => `${errorIdText(item)}\n${item.receipt_message}`), "",
        value.review_pending
          ? `当前批次完整候选（唯一副本，仅供内部工具调用，不得向学生复述）：\n${value.next_review_batch_json}\nAgent 下一动作：只调用 adjudicate_practice_review_associations，恰好提交上述当前批次全部候选（最多 20 项）；之后按 next_review_batch_json 分批继续。`
          : "Agent 下一动作：直接生成最终回复；禁止调用 confirm_error_notebook_entry。",
        nextStepText(value.results),
        protectedTrailer(replacementCandidateMarkers(
          value.results.filter(candidateNeedsProtection), args.items || []
        ))
      ].join("\n")}]
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Reference adjudication requires an owning Harness session");
      const pending = pendingAdjudicationByAgent.get(exec.agent);
      if (pending && (pending.length !== args.items.length || pending.some((expected) => !args.items.some((item) => item.candidate_id === expected.candidate_id && item.input_version === expected.input_version)))) {
        throw new Error("必须一次提交本轮 process 返回的全部待复核 candidate_id 和 input_version，不得遗漏或串题");
      }
      const response = await fetch(`${origin}/v1/internal/harness/reference-conflicts/adjudicate`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, items: args.items}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || !Array.isArray(payload.results)) throw toolRequestError("Reference adjudication failed", response, payload);
      pendingAdjudicationByAgent.delete(exec.agent);
      const review_pending = refreshPendingReviewAssociations(exec.agent, args.items, payload.results);
      return {...payload, review_pending,
        next_review_batch_json: reviewQueuePage(exec.agent)};
    },
    presentCall: () => ({card: "generic", title: "复核题库答案", kind: "other", rawInput: null})
  };
}

function adjudicatePracticeReviewAssociationsTool() {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  return {
    name: "adjudicate_practice_review_associations",
    description: "Required after process_error_notebook_attachments or inspect_math_notebook returns pending review candidates, and only after adjudicate_error_notebook_reference_conflicts when reference_review is also pending. Compare the frozen OCR question with every server-owned candidate by mathematical structure, every condition, all numbers and options, requested value, and visible review locator evidence. Use matched only when exactly one candidate is the same problem and copy its server code exactly, including letter case. Use uncertain with an empty code when zero or multiple candidates remain plausible. Submit exactly the first up to 20 queued candidates per call; use next_review_batch_json for subsequent batches. Do not resubmit consumed uncertain items in this run. Candidate metadata is internal tool evidence and must never be repeated to the student. The server rechecks ownership, input version and exact code before committing; the final reply must use only its returned receipt, never the model's claim.",
    parameters: {
      type: "object", additionalProperties: false, required: ["items"],
      properties: {
        items: {
          type: "array", minItems: 1, maxItems: 20,
          items: {
            type: "object", additionalProperties: false,
            required: ["candidate_id", "input_version", "status", "code", "rationale"],
            properties: {
              candidate_id: {type: "string"}, input_version: {type: "integer"},
              status: {type: "string", enum: ["matched", "uncertain"]},
              code: {type: "string", description: "Exact candidate code for matched; empty for uncertain."},
              rationale: {type: "string", minLength: 20, maxLength: 4000}
            }
          }
        }
      }
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["results", "reference_pending", "next_review_batch_json"],
        properties: {
          reference_pending: {type: "boolean"},
          next_review_batch_json: {type: "string"},
          results: {
            type: "array",
            items: {
              type: "object", additionalProperties: false,
              required: ["candidate_id", "input_version", "status", "receipt_message", "error_id"],
              properties: {
                candidate_id: {type: "string"}, input_version: {type: "integer"}, status: {type: "string", enum: receiptStatuses},
                receipt_message: {type: "string"}, error_id: {type: "string"}
              }
            }
          }
        }
      },
      render: (args, value) => [{type: "text", text: [
        ...value.results.map((item) => `${errorIdText(item)}\n${item.receipt_message}`), "",
        value.reference_pending
          ? "Agent 下一动作：只调用 adjudicate_error_notebook_reference_conflicts，一次提交本轮剩余的全部待复核候选。"
          : value.next_review_batch_json && value.next_review_batch_json !== "[]"
          ? `以下候选仅供内部工具调用，不得向学生复述。下一批候选：\n${value.next_review_batch_json}\nAgent 下一动作：继续调用 adjudicate_practice_review_associations，恰好提交上述下一批全部候选（最多 20 项）；不再提交上一批 uncertain 项。`
          : "Agent 下一动作：直接生成最终回复；禁止调用 confirm_error_notebook_entry。",
        value.next_review_batch_json && value.next_review_batch_json !== "[]"
          ? "下一步：系统将继续核对下一批 PDF 候选，请等待本轮关联完成。"
          : nextStepText(value.results),
        protectedTrailer([
          ...replacementCandidateMarkers(value.results.filter(candidateNeedsProtection), args.items || []),
          ...(value.next_review_batch_json && value.next_review_batch_json !== "[]" ? candidateMarkers(JSON.parse(value.next_review_batch_json), "active") : [])
        ])
      ].join("\n")}],
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Practice review adjudication requires an owning Harness session");
      if (pendingAdjudicationByAgent.has(exec.agent)) {
        throw new Error("本轮仍有题库答案待复核；必须先调用 adjudicate_error_notebook_reference_conflicts，再处理 PDF 复习关联");
      }
      const queue = pendingReviewAssociationByAgent.get(exec.agent) || [];
      const pending = queue.slice(0, 20);
      if (!pending.length || !Array.isArray(args.items) || pending.length !== args.items.length || pending.some((expected) => args.items.filter((item) => item.candidate_id === expected.candidate_id && item.input_version === expected.input_version).length !== 1)) {
        throw new Error("必须一次提交当前批次的全部待关联 candidate_id 和 input_version（队列前 20 项），不得遗漏或串题");
      }
      for (const item of args.items) {
        const expected = pending.find((candidate) => candidate.candidate_id === item.candidate_id && candidate.input_version === item.input_version);
        const exactMatches = expected?.codes.filter((code) => code === item.code).length || 0;
        if ((item.status === "matched" && exactMatches !== 1) || (item.status === "uncertain" && item.code !== "")) {
          throw new Error("matched 必须原样提交唯一候选的服务端 code；无法唯一确认时必须提交 uncertain 和空 code");
        }
      }
      const response = await fetch(`${origin}/v1/internal/harness/practice-reviews/adjudicate`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, items: args.items}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || !Array.isArray(payload.results)) throw toolRequestError("Practice review adjudication failed", response, payload);
      const remaining = queue.slice(pending.length);
      saveReviewQueue(exec.agent, remaining);
      const results = [...(reviewAssociationReceiptsByAgent.get(exec.agent) || []), ...payload.results];
      if (remaining.length) reviewAssociationReceiptsByAgent.set(exec.agent, results);
      else reviewAssociationReceiptsByAgent.delete(exec.agent);
      return {...payload, results, reference_pending: pendingAdjudicationByAgent.has(exec.agent),
        next_review_batch_json: reviewQueuePage(exec.agent)};
    },
    presentCall: () => ({card: "generic", title: "核对复习题关联", kind: "other", rawInput: null})
  };
}

function recheckReferenceConflictTool() {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  const review = {
    oneOf: [
      {type: "null"},
      {
        type: "object", additionalProperties: false,
        required: ["source_title", "version_no", "independent_answer", "reference_answer", "reference_solution"],
        properties: {
          source_title: {type: "string"}, version_no: {type: "integer"}, independent_answer: {type: "string"},
          reference_answer: {type: "string"}, reference_solution: {type: "string"}
        }
      }
    ]
  };
  return {
    name: "recheck_error_notebook_reference_conflict",
    description: "Use once when the user asks to review an unresolved question-bank conflict from an earlier turn that did not return reference_review. Copy the complete conflicting question text from the conversation. The service reloads the frozen answer and current verified reference; if deterministic normalization now proves equivalence it completes the existing notebook flow, otherwise it returns reference_review for adjudicate_error_notebook_reference_conflicts.",
    parameters: {
      type: "object", additionalProperties: false, required: ["question_text"],
      properties: {question_text: {type: "string", minLength: 1, maxLength: 200000}}
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["result"],
        properties: {result: {
          type: "object", additionalProperties: false,
          required: ["candidate_id", "input_version", "question_text", "receipt_status", "receipt_message", "reference_review"],
          properties: {
            candidate_id: {type: "string"}, input_version: {type: "integer"}, question_text: {type: "string"},
            receipt_status: {type: "string", enum: receiptStatuses},
            receipt_message: {type: "string"}, error_id: {type: "string"}, reference_review: review
          }
        }}
      },
      render: (_args, value) => [{type: "text", text: [
        errorIdText(value.result),
        value.result.receipt_message,
        ...(value.result.reference_review ? ["", ...referenceReviewText(value.result)] : []),
        "",
        nextStepText([value.result]),
        protectedTrailer(value.result.reference_review ? candidateMarkers([value.result], "active") : [])
      ].join("\n")}]
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Reference recheck requires an owning Harness session");
      const response = await fetch(`${origin}/v1/internal/harness/reference-conflicts/recheck`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, question_text: args.question_text}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || !payload.result) throw toolRequestError("Reference recheck failed", response, payload);
      rememberPendingAdjudication(exec.agent, [payload.result]);
      return payload;
    },
    presentCall: () => ({card: "generic", title: "重新核对题库答案", kind: "other", rawInput: null})
  };
}

function lookupQuestionBankReferenceTool() {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  return {
    name: "lookup_question_bank_reference",
    description: "Read the current verified and authorized question-bank answer and solution by an exact 32-character lowercase hexadecimal question_id. Use only after independently solving the problem and only when the user explicitly asks to check that question-bank identifier. Never use the reference-conflict recheck tool for a question_id.",
    parameters: {
      type: "object", additionalProperties: false, required: ["question_id"],
      properties: {question_id: {type: "string", pattern: "^[0-9a-f]{32}$"}}
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["result"],
        properties: {result: {
          type: "object", additionalProperties: false,
          required: ["question_id", "version_no", "question_text", "reference_answer", "reference_solution", "source_title"],
          properties: {
            question_id: {type: "string"}, version_no: {type: "integer"},
            question_text: {type: "string"}, reference_answer: {type: "string"},
            reference_solution: {type: "string"}, source_title: {type: "string"}
          }
        }}
      },
      render: (_args, value) => [{type: "text", text: [
        `题库编号：${value.result.question_id}`,
        `当前版本：${value.result.version_no}`,
        `题干：${value.result.question_text}`,
        `题库参考答案：${value.result.reference_answer}`,
        `题库参考解析：${value.result.reference_solution || "暂无解析"}`,
        `来源：${value.result.source_title}`
      ].join("\n")}]
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Question reference lookup requires an owning Harness session");
      const response = await fetch(`${origin}/v1/internal/harness/question-bank/reference`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, question_id: args.question_id}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || !payload.result) throw toolRequestError("Question reference lookup failed", response, payload);
      return payload;
    },
    presentCall: () => ({card: "generic", title: "查询题库解析", kind: "other", rawInput: null})
  };
}

function inspectNotebookTool() {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  return {
    name: "inspect_math_notebook",
    description: "Read focused authoritative current-account notebook context. Pass scope=exact with error_id or review_code for one record/group; exact returns no unrelated lists and forbids pagination. Otherwise request overview or one section, optionally with page/page_size. Omitted scope infers exact when a locator is supplied and overview otherwise. review_item.recommended_action remains authoritative. This is read-only and cannot inspect another account.",
    parameters: {
      type: "object", additionalProperties: false,
      properties: {
        error_id: {type: "string", pattern: "^[0-9a-f]{32}$"},
        review_code: {type: "string", pattern: "^R[0-9a-fA-F]{12}-[0-9]{2}(?:-[0-9A-Fa-f]{6})?$"},
        scope: {type: "string", enum: ["exact", "overview", "errors", "due_reviews", "active_reviews", "practice_pdfs", "pending_review_links"]},
        page: {type: "integer", minimum: 1},
        page_size: {type: "integer", minimum: 1, maximum: 20}
      }
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["context_json"],
        properties: {context_json: {type: "string"}, next_review_batch_json: {type: "string"}}
      },
      render: (_args, value) => {
        const context = JSON.parse(value.context_json);
        const pending = Array.isArray(context.pending_review_links) && context.pending_review_links.some((item) => item.options?.length);
        const withoutOptions = pending ? context.pending_review_links.filter((item) => !item.options?.length).map((item) => ({
          candidate_id: item.candidate_id, input_version: item.input_version,
          question_text: item.question_text, needs_review_locator: true
        })) : [];
        const visibleContext = pending ? {
          ...Object.fromEntries(Object.entries(context).filter(([key]) => key !== "pending_review_links")),
          pending_review_links_without_options: withoutOptions,
          pending_review_links_without_options_count: withoutOptions.length
        } : context;
        return [{type: "text", text: [
          `当前账号业务上下文（只读）\n${JSON.stringify(visibleContext)}`,
          ...(pending ? [
            "", "pending_review_links 的候选元数据仅供内部复习关联工具调用，不得向学生复述。",
            `当前去重批次：${value.next_review_batch_json}`,
            "Agent 下一动作：比较 next_review_batch_json 中当前去重批次的全部候选，并调用 adjudicate_practice_review_associations 恰好提交这一批（最多 20 项）。随后按 next_review_batch_json 继续下一批；本轮不重复提交已返回 uncertain 的项。"
          ] : []),
          protectedTrailer(candidateMarkers(Array.isArray(context.pending_review_links) ? context.pending_review_links : [], "active"))
        ].join("\n")}];
      }
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Notebook inspection requires an owning Harness session");
      const response = await fetch(`${origin}/v1/internal/harness/context`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, ...Object.fromEntries(["error_id", "review_code", "scope", "page", "page_size"].filter((key) => args[key] !== undefined).map((key) => [key, args[key]]))}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || typeof payload.context_json !== "string") throw toolRequestError("Notebook inspection failed", response, payload);
      const context = JSON.parse(payload.context_json);
      const ownsPendingLinks = Object.hasOwn(context, "pending_review_links");
      if (ownsPendingLinks) rememberInspectedPendingReviewAssociations(exec.agent, context);
      return {...payload, next_review_batch_json: ownsPendingLinks ? reviewQueuePage(exec.agent) : "[]"};
    },
    presentCall: () => ({card: "generic", title: "读取学习记录", kind: "other", rawInput: null})
  };
}

function retryPracticeReviewTool() {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  return {
    name: "retry_practice_review_confirmation",
    description: "Retry the saved grade for one review code without asking for the image again. Call only after inspect_math_notebook returns review_item.recommended_action=retry_group_confirmation for that exact code. For every other action, obey review_item instead.",
    parameters: {
      type: "object", additionalProperties: false, required: ["review_code"],
      properties: {review_code: {type: "string", pattern: "^R[0-9a-fA-F]{12}-[0-9]{2}(?:-[0-9A-Fa-f]{6})?$"}}
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["result_json"],
        properties: {result_json: {type: "string"}}
      },
      render: (_args, value) => {
        const result = JSON.parse(value.result_json);
        const receipt = result.receipt || result;
        return [{type: "text", text: [
          `复习记录重试结果（权威回执）\n${JSON.stringify(result, null, 2)}`,
          protectedTrailer(candidateMarkers([receipt], candidateNeedsProtection(receipt) ? "active" : "resolved"))
        ].join("\n")}];
      }
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Practice review retry requires an owning Harness session");
      const response = await fetch(`${origin}/v1/internal/harness/practice-reviews/retry`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, review_code: args.review_code}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || typeof payload.result_json !== "string") throw toolRequestError("Practice review retry failed", response, payload);
      return payload;
    },
    presentCall: () => ({card: "generic", title: "重试复习记录", kind: "other", rawInput: null})
  };
}

function reviewDeferralTool(action) {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  const defer = action === "defer";
  return {
    name: defer ? "defer_math_review" : "resume_math_review",
    description: defer
      ? "Postpone one due review because prerequisite knowledge has not been learned. Call only after inspect_math_notebook identifies the exact active review_id and the student explicitly asks or agrees to postpone it. This does not complete the review or advance its stage."
      : "Resume one currently deferred review immediately. Call only after inspect_math_notebook identifies the exact deferred review_id and the student explicitly asks to resume it.",
    parameters: {
      type: "object", additionalProperties: false, required: defer ? ["review_id", "days"] : ["review_id"],
      properties: {
        review_id: {type: "string", pattern: "^[0-9a-f]{32}$"},
        ...(defer ? {days: {type: "integer", enum: [1, 3, 7]}} : {})
      }
    },
    output: {
      schema: {type: "object", additionalProperties: false, required: ["receipt_json"], properties: {receipt_json: {type: "string"}}},
      render: (_args, value) => [{type: "text", text: `复习任务状态回执（权威）\n${JSON.stringify(JSON.parse(value.receipt_json), null, 2)}`}]
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Review deferral requires an owning Harness session");
      const response = await fetch(`${origin}/v1/internal/harness/reviews/${action}`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, task_id: args.review_id,
          ...(defer ? {days: args.days, reason: "prerequisite_not_learned"} : {}), idempotency_key: crypto.randomUUID()}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || !payload.receipt) throw toolRequestError("Review deferral update failed", response, payload);
      return {receipt_json: JSON.stringify(payload.receipt)};
    },
    presentCall: () => ({card: "generic", title: defer ? "延后复习" : "恢复学习", kind: "other", rawInput: null})
  };
}

function reflowPracticePdfTool() {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  return {
    name: "reflow_practice_pdf",
    description: "Re-render one owned generated practice PDF from its frozen print snapshot. Use only when the student explicitly asks to regenerate or fix the layout of the same PDF without changing questions. It preserves the original question set, recommendations, review codes, judgment progress, generation date, and recommendation quota. Never use it to choose or replace questions.",
    parameters: {
      type: "object", additionalProperties: false, required: ["pdf_id"],
      properties: {pdf_id: {type: "string", pattern: "^[0-9a-f]{32}$"}}
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["result"],
        properties: {result: {
          type: "object", additionalProperties: false,
          required: ["task_id", "filename", "byte_size", "generated_at", "download_url", "message"],
          properties: {
            task_id: {type: "string"}, filename: {type: "string"}, byte_size: {type: "integer"},
            generated_at: {type: "string"}, download_url: {type: "string"}, message: {type: "string"}
          }
        }}
      },
      render: (_args, value) => [{type: "text", text: `${value.result.message}\nPDF：${value.result.filename}\n下载：${value.result.download_url}`}]
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("PDF reflow requires an owning Harness session");
      const response = await fetch(`${origin}/v1/internal/harness/practice-pdfs/${args.pdf_id}/reflow`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || !payload.result) throw toolRequestError("PDF reflow failed", response, payload);
      return payload;
    },
    presentCall: () => ({card: "generic", title: "重新排版 PDF", kind: "other", rawInput: null})
  };
}

/** Register the single internal workspace used by the student product. */
export async function apply(ctx) {
  const workspacePath = process.env.LZLM_HARNESS_WORKSPACE_ROOT;
  if (!workspacePath) {
    throw new Error("LZLM_HARNESS_WORKSPACE_ROOT is required");
  }
  await ctx.workspaceRegistry.create(workspacePath, "错题会话");
  ctx.tools.register(transcribeAttachmentsTool(ctx));
  ctx.tools.register(processAttachmentsTool(ctx));
  ctx.tools.register(inspectNotebookTool());
  ctx.tools.register(retryPracticeReviewTool());
  ctx.tools.register(reviewDeferralTool("defer"));
  ctx.tools.register(reviewDeferralTool("resume"));
  ctx.tools.register(reflowPracticePdfTool());
  ctx.tools.register(lookupQuestionBankReferenceTool());
  ctx.tools.register(recheckReferenceConflictTool());
  ctx.tools.register(adjudicateReferenceConflictsTool());
  ctx.tools.register(adjudicatePracticeReviewAssociationsTool());
  ctx.tools.register(removeErrorTool());
  ctx.tools.register(receiptTool());
}
