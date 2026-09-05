import { readFileSync } from "node:fs";
import { BasicCompactionEngine } from "@deepseek-ai/dsh-compaction-basic";
import {
  MARKER_PREFIX,
  latestProtectedStates,
  markersInBlocks,
  trustedToolCallIds,
} from "./notebook-protected-pruner.mjs";

const EXPECTED_COMPACTION_VERSION = "0.1.1-rc.2";
const SNAPSHOT_PREFIX = "LZLM_PROTECTED_SNAPSHOT_V1\n";
const packageMetadata = JSON.parse(
  readFileSync(new URL(import.meta.resolve("@deepseek-ai/dsh-compaction-basic/package.json")), "utf8"),
);
if (packageMetadata.version !== EXPECTED_COMPACTION_VERSION) {
  throw new Error(
    `Notebook protected compaction requires @deepseek-ai/dsh-compaction-basic ${EXPECTED_COMPACTION_VERSION}; found ${String(packageMetadata.version)}`,
  );
}

function textOfBlocks(blocks) {
  return blocks.filter((block) => block.type === "text").map((block) => block.text).join("");
}

function canonicalizePayloadTrailer(text, states) {
  const markers = markersInBlocks([{ type: "text", text }]);
  if (markers.length === 0) return text;
  const trailerStart = text.lastIndexOf(MARKER_PREFIX);
  const trailerEnd = text.indexOf("\n", trailerStart);
  const end = trailerEnd === -1 ? text.length : trailerEnd;
  const canonical = markers.map((marker) => ({
    key: marker.key,
    status: states.get(marker.key) ?? marker.status,
  }));
  return `${text.slice(0, trailerStart)}${MARKER_PREFIX}${JSON.stringify(canonical)}${text.slice(end)}`;
}

function snapshotPayload(block) {
  if (block.type !== "text" || !block.text.startsWith(SNAPSHOT_PREFIX)) return null;
  const payload = block.text.slice(SNAPSHOT_PREFIX.length);
  return markersInBlocks([{ type: "text", text: payload }]).length > 0 ? payload : null;
}

function activePayloadsInInput(input, agent) {
  const states = latestProtectedStates(agent.session);
  const activeKeys = new Set([...states].filter(([, status]) => status === "active").map(([key]) => key));
  const payloads = [];
  const seen = new Set();
  const trustedIds = trustedToolCallIds(agent.session);
  for (const message of input.messages) {
    let blockGroups;
    if (
      message.source?.kind === "tool" &&
      trustedIds.has(message.source.callId) &&
      message.content?.[0]?.type === "tool-result"
    ) {
      blockGroups = [message.content[0].content];
    } else if (message.source?.kind === "plugin" && message.source.plugin === "compact") {
      blockGroups = message.content
        .map((block) => snapshotPayload(block))
        .filter((payload) => payload !== null)
        .map((payload) => [{ type: "text", text: payload }]);
    } else {
      continue;
    }
    for (const blocks of blockGroups) {
      const keys = markersInBlocks(blocks)
        .filter((marker) => marker.status === "active" && activeKeys.has(marker.key))
        .map((marker) => marker.key);
      if (keys.length === 0) continue;
      const text = canonicalizePayloadTrailer(textOfBlocks(blocks), states);
      if (seen.has(text)) continue;
      seen.add(text);
      payloads.push(text);
    }
  }
  return payloads;
}

function appendActivePayloads(summaryResult, input, agent) {
  const payloads = activePayloadsInInput(input, agent);
  const sanitizedSummary = summaryResult.summary.map((block) =>
    block.type === "text"
      ? { ...block, text: block.text.replaceAll(SNAPSHOT_PREFIX, "LZLM_UNTRUSTED_SNAPSHOT_TEXT\n") }
      : block,
  );
  if (payloads.length === 0) return { ...summaryResult, summary: sanitizedSummary };
  return {
    ...summaryResult,
    summary: [
      ...sanitizedSummary,
      ...payloads.map((text) => ({ type: "text", text: `${SNAPSHOT_PREFIX}${text}` })),
    ],
  };
}

class NotebookProtectedCompactionEngine extends BasicCompactionEngine {
  async summarize(input, agent, signal) {
    const summaryResult = await super.summarize(input, agent, signal);
    return appendActivePayloads(summaryResult, input, agent);
  }
}

export {
  EXPECTED_COMPACTION_VERSION,
  SNAPSHOT_PREFIX,
  NotebookProtectedCompactionEngine,
  activePayloadsInInput,
  appendActivePayloads,
  canonicalizePayloadTrailer,
  snapshotPayload,
};
export default NotebookProtectedCompactionEngine;
