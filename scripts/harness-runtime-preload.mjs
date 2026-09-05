import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";

const EXPECTED_PI_AI_VERSION = "0.82.1";
const SOURCE_ANCHOR =
  "const cacheWriteTokens = rawUsage.prompt_tokens_details?.cache_write_tokens || 0;";
const SOURCE_REPLACEMENT =
  "const cacheWriteTokens = rawUsage.prompt_tokens_details?.cache_write_tokens ?? rawUsage.prompt_tokens_details?.cache_creation_input_tokens ?? 0;";

const targetUrl = import.meta.resolve("@earendil-works/pi-ai/api/openai-completions");
const packageUrl = new URL("../../package.json", targetUrl).href;
const packageMetadata = JSON.parse(readFileSync(new URL(packageUrl), "utf8"));
if (packageMetadata.version !== EXPECTED_PI_AI_VERSION) {
  throw new Error(
    `Harness runtime preload requires @earendil-works/pi-ai ${EXPECTED_PI_AI_VERSION}; found ${String(packageMetadata.version)}`,
  );
}

function transformTargetSource(source) {
  const text = typeof source === "string" ? source : Buffer.from(source).toString("utf8");
  const occurrences = text.split(SOURCE_ANCHOR).length - 1;
  if (occurrences !== 1) {
    throw new Error(
      `Harness runtime preload expected one pi-ai cache-usage source anchor; found ${occurrences}`,
    );
  }
  return text.replace(SOURCE_ANCHOR, SOURCE_REPLACEMENT);
}

// Validate before the server starts rather than waiting for the first model call.
transformTargetSource(readFileSync(new URL(targetUrl)));

registerHooks({
  load(url, context, nextLoad) {
    const loaded = nextLoad(url, context);
    if (url !== targetUrl) return loaded;
    return { ...loaded, source: transformTargetSource(loaded.source) };
  },
});

export {
  EXPECTED_PI_AI_VERSION,
  SOURCE_ANCHOR,
  SOURCE_REPLACEMENT,
  targetUrl,
  transformTargetSource,
};
