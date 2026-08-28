export const inject = ["workspaceRegistry", "tools", "attachments"];

function nextStepText(results) {
  const statuses = results.map((item) => item.receipt_status || item.status);
  if (results.some((item) => item.reference_review)) {
    return "下一步：系统将继续核对已验证题库解析，请等待本轮复核完成。";
  }
  if (statuses.includes("needs_review")) {
    return "下一步：请补充更清晰的题目或作答图片，也可以直接说明需要修正的题干、作答或解题过程。";
  }
  if (statuses.includes("saved") || statuses.includes("already_saved")) {
    return "下一步：可打开「错题本」查看错因和知识点并选择今日复习，也可以继续上传下一张题目图片。";
  }
  return "下一步：本轮题目无需计入错题本；可以继续上传下一张题目图片，或在当前会话追问解析。";
}

function receiptText(value) {
  const lines = [value.message];
  if (value.error_id) lines.push(`错题编号：${value.error_id}`);
  const referenceLabels = {consistent: "已与已验证题库解析核对一致", conflict: "与题库解析冲突，等待复核", not_found: "题库未匹配"};
  lines.push(`题库核验：${referenceLabels[value.reference_status]}`);
  lines.push(`知识点：${value.knowledge_point_count} 个`);
  lines.push(`复习任务：${value.review_status === "scheduled" ? "已安排" : "未安排"}`);
  lines.push(nextStepText([value]));
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
    if (item.reference_review) {
      lines.push(...referenceReviewText(item), "");
    }
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
    confidence: {type: "number"}
  };
  const resultProperties = {
    attachment_index: {type: "integer"}, item_no: {type: "integer"}, candidate_id: {type: "string"}, input_version: {type: "integer"},
    verdict: {type: "string", enum: ["correct", "partial", "incorrect", "unclear"]}, question_text: {type: "string"}, answer_text: {type: "string"},
    first_error: {type: "string"}, cause_code: {type: "string"}, cause_evidence: {type: "string"}, knowledge_points: {type: "array", items: {type: "string"}},
    correct_solution: {type: "string"}, final_answer: {type: "string"}, prevention_cue: {type: "string"},
    receipt_status: {type: "string", enum: ["saved", "already_saved", "not_saved_correct", "needs_review"]}, receipt_message: {type: "string"}, error_id: {type: "string"},
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
    description: "Required once when the latest user message contains math images. Submit every recognized question and judgment in image order. The tool reads the actual latest attachments, validates and stores them, freezes versions, cross-checks the verified bank, writes only incorrect or partial questions, schedules review, and returns authoritative receipts. If a result contains reference_review, compare its frozen independent answer with the protected reference answer and solution, then submit all such decisions once through adjudicate_error_notebook_reference_conflicts. Use empty strings for inapplicable diagnosis fields; never invent candidate ids.",
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

function adjudicateReferenceConflictsTool() {
  const origin = process.env.LZLM_PRODUCT_ORIGIN;
  const token = process.env.LZLM_HARNESS_INTERNAL_TOKEN;
  if (!origin || !token) throw new Error("Harness notebook bridge is not configured");
  return {
    name: "adjudicate_error_notebook_reference_conflicts",
    description: "Required once after process_error_notebook_attachments or recheck_error_notebook_reference_conflict returns one or more reference_review objects. For each frozen candidate, compare the independent answer with the verified reference answer and solution. Use consistent only when they are mathematically equivalent, conflict for a substantive difference, and uncertain when the evidence cannot decide. Submit every returned conflict in one call.",
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
              rationale: {type: "string", minLength: 20, maxLength: 4000}
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
                status: {type: "string", enum: ["saved", "already_saved", "not_saved_correct", "needs_review"]},
                receipt_message: {type: "string"}
              }
            }
          }
        }
      },
      render: (_args, value) => [{type: "text", text: [...value.results.map((item) => item.receipt_message), "", nextStepText(value.results)].join("\n")}]
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
            receipt_status: {type: "string", enum: ["saved", "already_saved", "not_saved_correct", "needs_review"]},
            receipt_message: {type: "string"}, reference_review: review
          }
        }}
      },
      render: (_args, value) => [{type: "text", text: [
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
  ctx.tools.register(receiptTool());
}
