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
    description: "Confirm a real notebook/review receipt using the frozen candidate_id and input_version. For a review_unmatched result, once the student supplies printed PDF locating details, pass review to link the already-graded photo without re-uploading. For review_retryable or an early review reaching its due time, omit review and retry this confirmation. Never invent ids or claim success before this authoritative receipt.",
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
      const response = await fetch(`${origin}/v1/internal/harness/grade-results/${args.candidate_id}/commit`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, input_version: args.input_version, ...(Object.values(args.review || {}).some(Boolean) ? {review: args.review} : {})}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || !payload.receipt) throw new Error(`Notebook receipt failed (${response.status})`);
      exec.concludeTurn();
      return payload.receipt;
    },
    presentCall: () => ({card: "generic", title: "确认错题本记录", kind: "other", rawInput: null})
  };
}

function latestUserImages(agent) {
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
      if (!response.ok || !payload.receipt) throw new Error(`Notebook removal failed (${response.status})`);
      exec.concludeTurn();
      return payload.receipt;
    },
    presentCall: () => ({card: "generic", title: "移除错题", kind: "other", rawInput: null})
  };
}

function processResultText(value) {
  const lines = [];
  for (const item of value.results) {
    lines.push(
      `第 ${item.item_no} 题`,
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
      item.receipt_message,
      ""
    );
    if (item.reference_review) {
      lines.push(...referenceReviewText(item), "");
    }
    if (["review_unmatched", "review_waiting", "review_retryable"].includes(item.receipt_status)) {
      lines.push(`仅供后续确认工具使用：candidate_id=${item.candidate_id}, input_version=${item.input_version}。学生补充 PDF 定位后调用 confirm_error_notebook_entry 并附 review；尚未到期的结果可到期后直接确认，不要要求重传。`, "");
    }
  }
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
    description: "Required once when the latest user message contains one math image. Submit recognized questions and judgments in reading order with attachment_index=1. Keep question_text to the complete stem only. If the image visibly contains 复习码, 同类型推荐题, or 题库编号 Q-, review is mandatory: copy every visible PDF/review identifier exactly and never leave review empty. Do not submit unattempted originals marked reference-only. Ordinary new questions use empty review fields. The tool stores the image, freezes grades, cross-checks the bank, and either records new errors or accumulates PDF review results on their original tasks. A recognized review that cannot be linked must stay pending and must never become a new notebook error. Follow actual receipts, never infer stage completion. If reference_review is returned, submit the frozen independent/reference comparison through adjudicate_error_notebook_reference_conflicts. Never invent ids.",
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
      const images = latestUserImages(exec.agent);
      if (images.length === 0) throw new Error("The latest user message has no image attachments");
      if (images.length > 1) throw new Error("一条消息最多上传 1 张图片，请删除多余图片后重新发送");
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
            review_mode: /复习|推荐题|练习单|PDF/i.test(latestUserText(exec.agent)) || items.some((item) => Object.values(item.review || {}).some(Boolean)),
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
          throw new Error(`Notebook processing failed (${response.status})`);
        }
        usage = payload.usage || usage;
        results.push(...payload.results.map((item) => ({...item, attachment_index: attachmentIndex})));
      }
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
      render: (_args, value) => [{type: "text", text: [...value.results.map((item) => `${errorIdText(item)}\n${item.receipt_message}`), "", nextStepText(value.results)].join("\n")}]
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Reference adjudication requires an owning Harness session");
      const response = await fetch(`${origin}/v1/internal/harness/reference-conflicts/adjudicate`, {
        method: "POST",
        headers: {"authorization": `Bearer ${token}`, "content-type": "application/json"},
        body: JSON.stringify({session_id: exec.agent.id, items: args.items}),
        signal: exec.signal
      });
      const payload = await response.json();
      if (!response.ok || !Array.isArray(payload.results)) throw new Error(`Reference adjudication failed (${response.status})`);
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
      if (!response.ok || !payload.result) throw new Error(`Reference recheck failed (${response.status})`);
      return payload;
    },
    presentCall: () => ({card: "generic", title: "重新核对题库答案", kind: "other", rawInput: null})
  };
}

/** Register the single internal workspace used by the student product. */
export async function apply(ctx) {
  const workspacePath = process.env.LZLM_HARNESS_WORKSPACE_ROOT;
  if (!workspacePath) {
    throw new Error("LZLM_HARNESS_WORKSPACE_ROOT is required");
  }
  await ctx.workspaceRegistry.create(workspacePath, "错题会话");
  ctx.tools.register(processAttachmentsTool(ctx));
  ctx.tools.register(recheckReferenceConflictTool());
  ctx.tools.register(adjudicateReferenceConflictsTool());
  ctx.tools.register(removeErrorTool());
  ctx.tools.register(receiptTool());
}
