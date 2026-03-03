import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const modPath = path.resolve(here, "../../plugin/openclaw-memory-memq/dist/lib/token_estimate.js");
const mod = await import(pathToFileURL(modPath).href);
const { messageText, splitRecentByTokenBudget, estimateTokens } = mod;

function testToolPayloadCounted() {
  const huge = "X".repeat(9000);
  const msg = {
    role: "toolResult",
    content: [
      {
        type: "toolResult",
        output: {
          result: huge,
          status: "ok",
        },
      },
    ],
  };
  const txt = messageText(msg);
  assert.ok(txt.length > 200, "tool result text should be extracted");
  assert.ok(estimateTokens(txt) > 120, "tool result should contribute tokens");
}

function testRecentSplitDoesNotPinHugeToolOutput() {
  const huge = "Z".repeat(12000);
  const messages = [
    { role: "user", content: "short user question" },
    { role: "assistant", content: "short assistant reply" },
    { role: "toolResult", content: [{ type: "toolResult", output: huge }] },
  ];
  const res = splitRecentByTokenBudget(messages, 220, 2);
  const keptRoles = res.kept.map((m) => String(m.role));
  assert.ok(keptRoles.includes("user"), "must keep recent user message");
  assert.ok(keptRoles.includes("assistant"), "must keep recent assistant message");
  assert.ok(!keptRoles.includes("toolResult"), "huge tool payload should be pruned");
}

testToolPayloadCounted();
testRecentSplitDoesNotPinHugeToolOutput();
console.log("plugin_token_budget_regression: PASS");

