import { readFileSync } from "node:fs";
import { freezeMessage } from "@deepseek-ai/dsh-llm";
import {
  PRUNE_MARKER,
  ToolResultPruner,
  codePointLength,
} from "@deepseek-ai/dsh-compaction-tool-result-pruner";

const EXPECTED_PRUNER_VERSION = "0.1.1-rc.2";
const MARKER_PREFIX = "LZLM_PROTECTED_V1 ";
const SNAPSHOT_PREFIX = "LZLM_PROTECTED_SNAPSHOT_V1\n";
const KEY_PATTERN = /^(?:batch|candidate):[\x21-\x7e]{1,246}$/;
const MAX_TRAILER_TRANSITIONS = 512;
const TRUSTED_TOOL_NAMES = new Set([
  "transcribe_error_notebook_attachments",
  "process_error_notebook_attachments",
  "recheck_error_notebook_reference_conflict",
  "inspect_math_notebook",
  "adjudicate_error_notebook_reference_conflicts",
  "adjudicate_practice_review_associations",
  "confirm_error_notebook_entry",
  "retry_practice_review_confirmation",
]);

const packageMetadata = JSON.parse(
  readFileSync(new URL(import.meta.resolve("@deepseek-ai/dsh-compaction-tool-result-pruner/package.json")), "utf8"),
);
if (packageMetadata.version !== EXPECTED_PRUNER_VERSION) {
  throw new Error(
    `Notebook protected pruner requires @deepseek-ai/dsh-compaction-tool-result-pruner ${EXPECTED_PRUNER_VERSION}; found ${String(packageMetadata.version)}`,
  );
}

function markersInBlocks(blocks) {
  const finalText = [...blocks].reverse().find((block) => block.type === "text")?.text;
  if (finalText === undefined) return [];
  const finalLine = finalText.split(/\r?\n/).filter((line) => line.length > 0).at(-1);
  if (finalLine === undefined || !finalLine.startsWith(MARKER_PREFIX)) return [];
  let markers;
  try {
    markers = JSON.parse(finalLine.slice(MARKER_PREFIX.length));
  } catch {
    throw new Error("invalid LZLM_PROTECTED_V1 JSON trailer");
  }
  if (!Array.isArray(markers) || markers.length < 1 || markers.length > MAX_TRAILER_TRANSITIONS) {
    throw new Error("invalid LZLM_PROTECTED_V1 trailer manifest");
  }
  for (const marker of markers) {
    if (
      marker === null ||
      typeof marker !== "object" ||
      Array.isArray(marker) ||
      Object.keys(marker).sort().join(",") !== "key,status" ||
      typeof marker.key !== "string" ||
      !KEY_PATTERN.test(marker.key) ||
      (marker.status !== "active" && marker.status !== "resolved")
    ) {
      throw new Error("invalid LZLM_PROTECTED_V1 trailer fields");
    }
  }
  return markers;
}

function trailerLengthInBlocks(blocks) {
  const finalText = [...blocks].reverse().find((block) => block.type === "text")?.text;
  if (finalText === undefined || markersInBlocks(blocks).length === 0) return 0;
  const finalLine = finalText.split(/\r?\n/).filter((line) => line.length > 0).at(-1);
  return finalLine === undefined ? 0 : codePointLength(finalLine);
}

function resultBlocks(event) {
  if (event?.type !== "tool/result") return null;
  const result = event.data?.message?.content?.[0];
  return result?.type === "tool-result" && Array.isArray(result.content) ? result.content : null;
}

function trustedToolCallIds(session) {
  const ids = new Set();
  for (const seq of session.surface.nodes) {
    const message = session.events[seq]?.data?.message;
    if (message?.role !== "assistant" || !Array.isArray(message.content)) continue;
    for (const block of message.content) {
      if (block.type === "tool-call" && TRUSTED_TOOL_NAMES.has(block.name)) ids.add(block.id);
    }
  }
  return ids;
}

function isTrustedToolResult(event, trustedIds) {
  return resultBlocks(event) !== null && trustedIds.has(event.data.message.source?.callId);
}

function latestProtectedStates(session) {
  const states = new Map();
  const trustedIds = trustedToolCallIds(session);
  for (const seq of session.surface.nodes) {
    const event = session.events[seq];
    const blocks = resultBlocks(event);
    if (blocks !== null && isTrustedToolResult(event, trustedIds)) {
      for (const marker of markersInBlocks(blocks)) states.set(marker.key, marker.status);
      continue;
    }
    const message = event?.data?.message;
    if (message?.source?.kind === "plugin" && message.source.plugin === "compact") {
      for (const block of message.content ?? []) {
        if (block.type !== "text" || !block.text.startsWith(SNAPSHOT_PREFIX)) continue;
        const payload = { type: "text", text: block.text.slice(SNAPSHOT_PREFIX.length) };
        for (const marker of markersInBlocks([payload])) states.set(marker.key, marker.status);
      }
    }
  }
  return states;
}

function eventHasActiveMarker(event, states, trustedIds) {
  if (!isTrustedToolResult(event, trustedIds)) return false;
  const blocks = resultBlocks(event);
  return blocks !== null && markersInBlocks(blocks).some(
    (marker) => marker.status === "active" && states.get(marker.key) === "active",
  );
}

/** Stock replay-safe replacement transaction, filtered only for currently active notebook results. */
class NotebookProtectedToolResultPruner extends ToolResultPruner {
  pruneContent(blocks) {
    const trailerChars = trailerLengthInBlocks(blocks);
    if (trailerChars === 0) return super.pruneContent(blocks);
    const requiredTail = trailerChars + 1;
    if (requiredTail + codePointLength(PRUNE_MARKER) >= this.config.thresholdChars) return null;
    const config = {
      ...this.config,
      headChars: Math.min(
        this.config.headChars,
        this.config.thresholdChars - requiredTail - codePointLength(PRUNE_MARKER),
      ),
      tailChars: Math.max(this.config.tailChars, requiredTail),
    };
    return ToolResultPruner.prototype.pruneContent.call({
      config,
      measureContent: this.measureContent.bind(this),
    }, blocks);
  }

  pruneSession(session) {
    const states = latestProtectedStates(session);
    const trustedIds = trustedToolCallIds(session);
    const candidates = [];
    for (const seq of [...session.surface.nodes]) {
      const event = session.events[seq];
      if (resultBlocks(event) !== null && !eventHasActiveMarker(event, states, trustedIds)) candidates.push({ seq, event });
    }
    const pruned = [];
    let charsRemoved = 0;
    for (const { seq, event } of candidates) {
      const result = event.data.message.content[0];
      const content = this.pruneContent(result.content);
      if (content === null) continue;
      const charsBefore = this.measureContent(result.content);
      const charsAfter = this.measureContent(content);
      const message = freezeMessage({
        ...event.data.message,
        content: [{ ...result, content }],
      });
      session.append("compaction/prune", {
        shadowedRange: { start: seq, end: seq },
        shadowedSeqs: [seq],
        shadowedTokenCount: this.ctx.tokenMeter.estimateMessage(event.data.message),
      });
      const replacement = session.append(
        "tool/result",
        { ...event.data, message },
        {
          surfaceOp: { op: "replace", start: seq, end: seq },
          sourceEventSeqs: [seq],
        },
      );
      pruned.push({
        originalSeq: seq,
        replacementSeq: replacement.seq,
        callId: event.data.message.source.callId,
        charsBefore,
        charsAfter,
      });
      charsRemoved += charsBefore - charsAfter;
    }
    return { pruned, charsRemoved };
  }
}

export {
  EXPECTED_PRUNER_VERSION,
  MAX_TRAILER_TRANSITIONS,
  MARKER_PREFIX,
  TRUSTED_TOOL_NAMES,
  NotebookProtectedToolResultPruner,
  eventHasActiveMarker,
  latestProtectedStates,
  markersInBlocks,
  resultBlocks,
  trailerLengthInBlocks,
  trustedToolCallIds,
};
export default NotebookProtectedToolResultPruner;
