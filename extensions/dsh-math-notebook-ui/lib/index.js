export const inject = ["workspaceRegistry", "tools", "attachments"];

const receiptStatuses = ["saved", "already_saved", "not_saved_correct", "needs_review", "review_unmatched", "review_waiting", "review_completed", "review_needs_correction", "review_reference_only", "review_stale", "review_retryable"];
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

function rememberPendingAdjudication(agent, results) {
  const items = results.filter((item) => item.reference_review).map((item) => ({candidate_id: item.candidate_id, input_version: item.input_version}));
  if (items.length) pendingAdjudicationByAgent.set(agent, items);
  else pendingAdjudicationByAgent.delete(agent);
  return items.length > 0;
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
  if (statuses.includes("review_unmatched")) return "下一步：请补充 PDF 名称、错题编号与阶段或复习码；直接在会话补充即可，无需再次上传图片。";
  if (statuses.includes("review_waiting")) return "下一步：按复习回执补齐该组尚未上传的必做题；若提示尚未到期，则到期后再确认。";
  if (statuses.includes("review_needs_correction")) return "下一步：先依据错因与解析订正本组题目，再按回执中的日期复习。";
  if (statuses.includes("review_stale")) return "下一步：打开错题本查看当前复习计划，这份旧练习单不会改变新的阶段。";
  if (statuses.includes("review_completed")) return "下一步：本组复习已完成，请按回执的下次日期复习，也可继续提交其他组的作答。";
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
      render: (_args, value) => receiptText(value)
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
    if (item.reference_review) {
      lines.push(...referenceReviewText(item), "");
    }
    if (item.receipt_status === "review_unmatched") {
      lines.push(`仅供补充关联使用：candidate_id=${item.candidate_id}, input_version=${item.input_version}。学生补充 PDF 定位后调用 confirm_error_notebook_entry 并附 review，不要要求重传。`, "");
    } else if (item.receipt_status === "review_retryable") {
      lines.push(`仅供重试确认使用：candidate_id=${item.candidate_id}, input_version=${item.input_version}。调用 confirm_error_notebook_entry 时不要附 review，也不要要求重传。`, "");
    }
  }
  lines.push(value.results.some((item) => item.reference_review)
    ? "Agent 下一动作：只调用 adjudicate_error_notebook_reference_conflicts，一次提交上述全部待复核候选。"
    : "Agent 下一动作：直接生成最终回复；禁止再调用 confirm_error_notebook_entry。", "");
  if (value.usage) {
    lines.push(`今日学习负荷：已判题 ${value.usage.grade.count}/${value.usage.grade.limit}（建议 ${value.usage.grade.target}）；已生成推荐题 ${value.usage.recommendation.count}/${value.usage.recommendation.limit}（建议 ${value.usage.recommendation.target}）。`, "");
  }
  lines.push(nextStepText(value.results));
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
  const itemProperties = {
    attachment_index: {type: "integer"},
    item_no: {type: "integer"},
    question_text: {type: "string"},
    answer_text: {type: "string"},
    review: reviewLocatorSchema
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
        type: "object", additionalProperties: false, required: ["schema", "items"],
        properties: {
          schema: {type: "string", const: "math-notebook-transcription/v1"},
          items: {type: "array", items: {type: "object", additionalProperties: false, required: Object.keys(itemProperties), properties: itemProperties}}
        }
      },
      render: (_args, value) => value.items.length === 0 ? [{type: "text", text: missingImageMessage}] : [{type: "text", text: [
        "题干与作答已经冻结。下一步只能基于以下文字独立解题和判题，不得重新识别或改写：",
        ...value.items.flatMap((item) => [
          `图片 ${item.attachment_index} · 第 ${item.item_no} 题题干：${item.question_text}`,
          `图片 ${item.attachment_index} · 第 ${item.item_no} 题学生作答：${item.answer_text || "未作答"}`
        ])
      ].join("\n")}]
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Notebook transcription requires an owning Harness session");
      const images = latestUserImages(exec.agent);
      if (images.length === 0) {
        exec.concludeTurn();
        return {schema: "math-notebook-transcription/v1", items: []};
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
      transcriptionByAgent.set(exec.agent, {images, items, userText: latestUserText(exec.agent), turn: currentTurn(exec.agent)});
      return {schema: "math-notebook-transcription/v1", items};
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
    question_text: {type: "string"},
    answer_text: {type: "string"},
    verdict: {type: "string", enum: ["correct", "partial", "incorrect", "unclear"]},
    first_error: {type: "string"},
    cause_code: {type: "string", enum: ["", "knowledge_gap", "concept_confusion", "formula_condition", "method_choice", "reasoning_gap", "algebra_transform", "calculation", "misreading", "incomplete_cases", "expression", "careless", "unclear"]},
    cause_evidence: {type: "string"},
    knowledge_points: {type: "array", items: {type: "string"}},
    correct_solution: {type: "string"},
    final_answer: {type: "string"},
    prevention_cue: {type: "string"},
    confidence: {type: "number"},
    review: reviewLocatorSchema
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
    description: "Required once after transcribe_error_notebook_attachments freezes the latest 1-10 image transcription. Independently solve and grade only from that frozen question_text and answer_text, then submit all judgments in image order using the frozen attachment_index and per-image item_no. The two frozen text fields and review locator must be copied exactly. The tool queries the current account's frozen PDF records and returns the authoritative review_association for every item, then stores every image separately, freezes grades, cross-checks the bank, and either records new errors or accumulates PDF review results on their original tasks. A recognized review that cannot be linked must stay pending and must never become a new notebook error. Follow review_association and actual receipts; never infer PDF identity or stage completion. If reference_review is returned, submit the frozen independent/reference comparison through adjudicate_error_notebook_reference_conflicts. Never invent ids.",
    parameters: {
      type: "object", additionalProperties: false, required: ["items"],
      properties: {items: {type: "array", items: {type: "object", additionalProperties: false, required: Object.keys(itemProperties), properties: itemProperties}}}
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["schema", "results"],
        properties: {
          schema: {type: "string", const: "math-notebook-process-result/v1"},
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
      if (args.items.length !== frozen.items.length || args.items.some((item, index) => {
        const expected = frozen.items[index];
        return item.attachment_index !== expected.attachment_index
          || item.item_no !== expected.item_no
          || item.question_text.trim() !== expected.question_text
          || item.answer_text.trim() !== expected.answer_text
          || Object.keys(reviewLocatorSchema.properties).some((key) => item.review?.[key] !== expected.review?.[key]);
      })) throw new Error("判题提交必须原样使用已冻结的题干、作答和复习定位信息");
      if (args.items.some((item) => item.attachment_index < 1 || item.attachment_index > images.length)) {
        throw new Error("A result refers to an attachment outside the latest user message");
      }
      const results = [];
      let usage = null;
      for (let attachmentIndex = 1; attachmentIndex <= images.length; attachmentIndex += 1) {
        const items = args.items.filter((item) => item.attachment_index === attachmentIndex);
        if (items.length === 0) throw new Error(`No result supplied for attachment ${attachmentIndex}`);
        const stored = await ctx.attachments.readImage(images[attachmentIndex - 1].attachment, exec.signal);
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
          throw toolRequestError("Notebook processing failed", response, payload);
        }
        usage = payload.usage || usage;
        results.push(...payload.results.map((item) => ({...item, attachment_index: attachmentIndex})));
      }
      transcriptionByAgent.delete(exec.agent);
      rememberPendingAdjudication(exec.agent, results);
      return {schema: "math-notebook-process-result/v1", results, ...(usage ? {usage} : {})};
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
        type: "object", additionalProperties: false, required: ["results"],
        properties: {
          results: {
            type: "array",
            items: {
              type: "object", additionalProperties: false,
              required: ["candidate_id", "input_version", "status", "receipt_message"],
              properties: {
                candidate_id: {type: "string"}, input_version: {type: "integer"},
                status: {type: "string", enum: receiptStatuses},
                receipt_message: {type: "string"}, error_id: {type: "string"}
              }
            }
          }
        }
      },
      render: (_args, value) => [{type: "text", text: [...value.results.map((item) => `${errorIdText(item)}\n${item.receipt_message}`), "", "Agent 下一动作：直接生成最终回复；禁止调用 confirm_error_notebook_entry。", nextStepText(value.results)].join("\n")}]
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
      return payload;
    },
    presentCall: () => ({card: "generic", title: "复核题库答案", kind: "other", rawInput: null})
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
        nextStepText([value.result])
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
    description: "Read authoritative current-account notebook context. Use before answering questions about existing error_id records, review codes, due reviews, today's plan, learning progress, generated PDFs, or pending PDF-review links. Pass review_code whenever the user gives one; review_item then provides the canonical inherited status, group counts, pending items and recommended_action. Never override that action with a guess. This is read-only and cannot inspect another account.",
    parameters: {
      type: "object", additionalProperties: false,
      properties: {
        error_id: {type: "string", pattern: "^[0-9a-f]{32}$"},
        review_code: {type: "string", pattern: "^R[0-9a-fA-F]{12}-[0-9]{2}(?:-[0-9A-Fa-f]{6})?$"}
      }
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["context_json"],
        properties: {context_json: {type: "string"}}
      },
      render: (_args, value) => [{type: "text", text: `当前账号业务上下文（只读）\n${JSON.stringify(JSON.parse(value.context_json), null, 2)}`}]
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Notebook inspection requires an owning Harness session");
      const response = await fetch(`${origin}/v1/internal/harness/context`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, ...(args.error_id ? {error_id: args.error_id} : {}), ...(args.review_code ? {review_code: args.review_code} : {})}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || typeof payload.context_json !== "string") throw toolRequestError("Notebook inspection failed", response, payload);
      return payload;
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
      render: (_args, value) => [{type: "text", text: `复习记录重试结果（权威回执）\n${JSON.stringify(JSON.parse(value.result_json), null, 2)}`}]
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
  ctx.tools.register(reflowPracticePdfTool());
  ctx.tools.register(lookupQuestionBankReferenceTool());
  ctx.tools.register(recheckReferenceConflictTool());
  ctx.tools.register(adjudicateReferenceConflictsTool());
  ctx.tools.register(removeErrorTool());
  ctx.tools.register(receiptTool());
}
