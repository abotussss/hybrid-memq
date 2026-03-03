import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const modPath = path.resolve(here, "../../plugin/openclaw-memory-memq/dist/lib/token_estimate.js");
const hookPath = path.resolve(here, "../../plugin/openclaw-memory-memq/dist/hooks/before_prompt_build.js");
const mod = await import(pathToFileURL(modPath).href);
const hookMod = await import(pathToFileURL(hookPath).href);
const { messageText, splitRecentByTokenBudget, estimateTokens } = mod;
const { repairToolIntegrity } = hookMod;

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

function testRepairToolIntegrityRemovesOrphanToolResults() {
  const messages = [
    {
      role: "assistant",
      tool_calls: [
        {
          id: "call_ok_1",
          function: { name: "memory_search", arguments: "{\"q\":\"x\"}" },
        },
      ],
      content: [{ type: "text", text: "searching..." }],
    },
    {
      role: "tool",
      tool_call_id: "call_ok_1",
      content: [{ type: "toolResult", output: "ok" }],
    },
    {
      role: "tool",
      tool_call_id: "call_orphan",
      content: [{ type: "toolResult", output: "orphan" }],
    },
    {
      role: "assistant",
      content: [
        { type: "text", text: "done" },
        { type: "function_call_output", call_id: "call_orphan", output: "bad" },
      ],
    },
  ];

  const repaired = repairToolIntegrity(messages);
  const kept = repaired.kept;
  const orphanTool = kept.find((m) => String(m.role) === "tool" && String(m.tool_call_id) === "call_orphan");
  assert.equal(orphanTool, undefined, "orphan tool message must be removed");
  const outputBlockOrphan = kept.flatMap((m) => Array.isArray(m.content) ? m.content : [])
    .find((b) => String(b?.type) === "function_call_output" && String(b?.call_id) === "call_orphan");
  assert.equal(outputBlockOrphan, undefined, "orphan function_call_output block must be removed");
  assert.ok(repaired.removed >= 2, "should remove orphan tool artifacts");
}

testToolPayloadCounted();
testRecentSplitDoesNotPinHugeToolOutput();
testRepairToolIntegrityRemovesOrphanToolResults();
console.log("plugin_token_budget_regression: PASS");
