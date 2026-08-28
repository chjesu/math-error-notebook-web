export const inject = ["workspaceRegistry", "tools", "attachments"];

function receiptText(value) {
  const lines = [value.message];
  if (value.error_id) lines.push(`错题编号：${value.error_id}`);
  const referenceLabels = {consistent: "已与已验证题库解析核对一致", conflict: "与题库解析冲突，等待复核", not_found: "题库未匹配"};
  lines.push(`题库核验：${referenceLabels[value.reference_status]}`);
  lines.push(`知识点：${value.knowledge_point_count} 个`);
  lines.push(`复习任务：${value.review_status === "scheduled" ? "已安排" : "未安排"}`);
  return [{type: "text", text: lines.join("\n")}];
}

function receiptTool() {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness receipt bridge is not configured");
  return {
    name: "confirm_error_notebook_entry",
    description: "After the product grading pipeline supplies a frozen candidate_id and input_version, call this exactly once to confirm the real notebook write. Never claim that a question was saved without this tool's receipt. Call it for correct and unclear results too so the product can return an authoritative not-saved receipt.",
    parameters: {
      type: "object",
      additionalProperties: false,
      required: ["candidate_id", "input_version"],
      properties: {
        candidate_id: {type: "string", description: "Frozen product grade candidate id."},
        input_version: {type: "integer", description: "Frozen input version paired with the candidate."}
      }
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        required: ["schema", "status", "reference_status", "knowledge_point_count", "review_status", "message"],
        properties: {
          schema: {type: "string", const: "math-notebook-entry-receipt/v1"},
          status: {type: "string", enum: ["saved", "already_saved", "not_saved_correct", "needs_review"]},
          reference_status: {type: "string", enum: ["consistent", "conflict", "not_found"]},
          reference_question_id: {type: "string"},
          reference_version_no: {type: "integer"},
          error_id: {type: "string"},
          knowledge_point_count: {type: "integer"},
          review_status: {type: "string", enum: ["scheduled", "not_scheduled"]},
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
        body: JSON.stringify({session_id: exec.agent.id, input_version: args.input_version}),
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

function processResultText(value) {
  const lines = [];
  for (const item of value.results) {
    lines.push(
      `第 ${item.item_no} 题`,
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
  }
  return [{type: "text", text: lines.join("\n").trim()}];
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
    confidence: {type: "number"}
  };
  const resultProperties = {
    attachment_index: {type: "integer"}, item_no: {type: "integer"}, candidate_id: {type: "string"}, input_version: {type: "integer"},
    verdict: {type: "string", enum: ["correct", "partial", "incorrect", "unclear"]}, question_text: {type: "string"}, answer_text: {type: "string"},
    first_error: {type: "string"}, cause_code: {type: "string"}, cause_evidence: {type: "string"}, knowledge_points: {type: "array", items: {type: "string"}},
    correct_solution: {type: "string"}, final_answer: {type: "string"}, prevention_cue: {type: "string"},
    receipt_status: {type: "string", enum: ["saved", "already_saved", "not_saved_correct", "needs_review"]}, receipt_message: {type: "string"}, error_id: {type: "string"}
  };
  return {
    name: "process_error_notebook_attachments",
    description: "Required once when the latest user message contains math images. Submit every recognized question and judgment in image order. The tool reads the actual latest attachments, validates and stores them, freezes versions, cross-checks the verified bank, writes only incorrect or partial questions, schedules review, and returns authoritative receipts. Use empty strings for inapplicable diagnosis fields; never invent candidate ids.",
    parameters: {
      type: "object", additionalProperties: false, required: ["items"],
      properties: {items: {type: "array", items: {type: "object", additionalProperties: false, required: Object.keys(itemProperties), properties: itemProperties}}}
    },
    output: {
      schema: {
        type: "object", additionalProperties: false, required: ["schema", "results"],
        properties: {
          schema: {type: "string", const: "math-notebook-process-result/v1"},
          results: {type: "array", items: {type: "object", additionalProperties: false, required: Object.keys(resultProperties), properties: resultProperties}}
        }
      },
      render: (_args, value) => processResultText(value)
    },
    async execute(args, exec) {
      if (!exec.agent) throw new Error("Notebook processing requires an owning Harness session");
      const images = latestUserImages(exec.agent);
      if (images.length === 0) throw new Error("The latest user message has no image attachments");
      if (args.items.some((item) => item.attachment_index < 1 || item.attachment_index > images.length)) {
        throw new Error("A result refers to an attachment outside the latest user message");
      }
      const results = [];
      for (let attachmentIndex = 1; attachmentIndex <= images.length; attachmentIndex += 1) {
        const items = args.items.filter((item) => item.attachment_index === attachmentIndex);
        if (items.length === 0) throw new Error(`No result supplied for attachment ${attachmentIndex}`);
        const stored = await ctx.attachments.readImage(images[attachmentIndex - 1].attachment, exec.signal);
        const response = await fetch(`${origin}/v1/internal/harness/intakes/process`, {
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
            items: items.map(({attachment_index: _attachmentIndex, ...item}) => item)
          }),
          signal: exec.signal
        });
        const payload = await response.json();
        if (!response.ok || !Array.isArray(payload.results)) throw new Error(`Notebook processing failed (${response.status})`);
        results.push(...payload.results.map((item) => ({...item, attachment_index: attachmentIndex})));
      }
      return {schema: "math-notebook-process-result/v1", results};
    },
    presentCall: () => ({card: "generic", title: "整理并记录错题", kind: "other", rawInput: null})
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
  ctx.tools.register(receiptTool());
}
