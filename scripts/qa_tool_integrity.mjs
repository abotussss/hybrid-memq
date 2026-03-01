import { repairToolIntegrity } from "../plugin/openclaw-memory-memq/dist/hooks/before_prompt_build.js";

const messages = [
  { role: "assistant", content: [{ type: "toolCall", id: "call_ok", name: "memory_search", input: "{}" }] },
  { role: "toolResult", toolCallId: "call_ok", content: "ok" },
  { role: "toolResult", toolCallId: "call_orphan", content: "orphan" },
];

const out = repairToolIntegrity(messages);
const keepIds = out.kept
  .filter((m) => m.role === "toolResult")
  .map((m) => String(m.toolCallId || ""))
  .join(",");

const ok = out.removed === 1 && keepIds === "call_ok";
if (!ok) {
  console.error(JSON.stringify({ ok: false, removed: out.removed, keepIds }, null, 2));
  process.exit(1);
}

console.log(JSON.stringify({ ok: true, removed: out.removed, keepIds }, null, 2));

