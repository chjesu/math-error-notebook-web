export const inject = ["workspaceRegistry", "tools"];

function receiptText(value) {
  const lines = [value.message];
  if (value.error_id) lines.push(`错题编号：${value.error_id}`);
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
        required: ["schema", "status", "knowledge_point_count", "review_status", "message"],
        properties: {
          schema: {type: "string", const: "math-notebook-entry-receipt/v1"},
          status: {type: "string", enum: ["saved", "already_saved", "not_saved_correct", "needs_review"]},
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

/** Register the single internal workspace used by the student product. */
export async function apply(ctx) {
  const workspacePath = process.env.LZLM_HARNESS_WORKSPACE_ROOT;
  if (!workspacePath) {
    throw new Error("LZLM_HARNESS_WORKSPACE_ROOT is required");
  }
  await ctx.workspaceRegistry.create(workspacePath, "错题会话");
  ctx.tools.register(receiptTool());
}
