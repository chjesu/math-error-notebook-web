import { HarnessSdkJsonRpcServer } from "@deepseek-ai/dsh-sdk-jsonrpc-server";
import { JsonRpcLineTransport } from "@deepseek-ai/dsh-sdk-protocol";

export const name = "notebook-sdk-jsonrpc-server";
export const inject = ["agents", "compaction"];

const sessionId = (params) => {
  const value = params?.sessionId;
  if (typeof value !== "string" || !/^[A-Za-z0-9._-]{1,128}$/.test(value)) {
    throw new TypeError("sessionId must be a valid Harness session id");
  }
  return value;
};

class NotebookJsonRpcServer extends HarnessSdkJsonRpcServer {
  async handleRequest(method, params) {
    if (method === "session/compact") {
      const rec = await this.getOrCreateSession(sessionId(params));
      const timeoutMs = Number.isSafeInteger(params?.timeoutMs) ? params.timeoutMs : 170_000;
      if (timeoutMs < 1 || timeoutMs > 180_000) throw new TypeError("timeoutMs must be between 1 and 180000");
      const result = await this.ctx.compaction.compactNow(
        rec.handle.agent, AbortSignal.timeout(timeoutMs),
      );
      return {
        status: "completed",
        compacted: result !== null,
        ...(result === null ? {} : {
          historyItems: result.shadowedSeqs.length,
          estimatedTokens: result.shadowedTokenCount,
        }),
      };
    }
    if (method === "session/cancel") {
      const rec = this.sessions.get(sessionId(params));
      if (rec !== undefined) rec.handle.agent.cancel({kind: "user"}, {keepInbox: true});
      return {accepted: rec !== undefined};
    }
    return super.handleRequest(method, params);
  }
}

export function apply(ctx, config = {}) {
  const transport = new JsonRpcLineTransport(process.stdin, process.stdout);
  const server = new NotebookJsonRpcServer(ctx, transport, {
    maxTokensAsSuccess: config.maxTokensAsSuccess === true,
  });
  const rootFiber = ctx.root.fiber;
  let exitTask;
  const disposeAndExit = () => {
    exitTask ??= (async () => {
      await Promise.allSettled([Promise.resolve().then(() => transport.flush())]);
      await Promise.allSettled([Promise.resolve().then(() => rootFiber.dispose())]);
      process.exit(0);
    })();
    return exitTask;
  };
  transport.onRequest(async (method, params) => {
    if (method === "initialize") await ctx.get("loader")?.await();
    const result = await server.handleRequest(method, params);
    if (method === "shutdown") setImmediate(disposeAndExit);
    return result;
  });
  ctx.effect(() => {
    transport.start();
    return async () => {
      await server.shutdown();
      transport.close();
    };
  }, "notebook-jsonrpc.serve");
}
