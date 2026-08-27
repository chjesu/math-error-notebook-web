export const inject = ["workspaceRegistry"];

/** Register the single internal workspace used by the student product. */
export async function apply(ctx) {
  const workspacePath = process.env.LZLM_HARNESS_WORKSPACE_ROOT;
  if (!workspacePath) {
    throw new Error("LZLM_HARNESS_WORKSPACE_ROOT is required");
  }
  await ctx.workspaceRegistry.create(workspacePath, "错题会话");
}
