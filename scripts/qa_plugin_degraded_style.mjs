import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createBeforePromptBuild } from "../plugin/openclaw-memory-memq/dist/hooks/before_prompt_build.js";

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "memq-style-cache-"));
const memqDir = path.join(tmp, ".memq");
fs.mkdirSync(memqDir, { recursive: true });
const cached = [
  "budget_tokens=120",
  "persona=ロックマン",
  "callUser=ヒロ",
  "prefix=ヒロ、",
  "firstPerson=僕",
].join("\n");
fs.writeFileSync(path.join(memqDir, "style_cache.json"), JSON.stringify({ qa: cached, __last__: cached }, null, 2));

const api = {
  pluginConfig: {
    "memq.workspaceRoot": tmp,
    "memq.style.enabled": true,
    "memq.degraded.enabled": true,
    "memq.budgets.memctxTokens": 120,
    "memq.budgets.rulesTokens": 80,
    "memq.budgets.styleTokens": 120,
    "memq.recent.maxTokens": 5000,
    "memq.recent.minKeepMessages": 6,
    "memq.retrieval.topK": 5,
    "memq.retrieval.surfaceThreshold": 0.85,
    "memq.retrieval.deepEnabled": true,
  },
  logger: { info: () => {} },
};

const sidecar = {
  ensureUp: async () => false,
  idleTick: async () => {},
  summarizeConversation: async () => {},
  memctxQuery: async () => {
    throw new Error("down");
  },
};

const rt = {
  lastUserBySession: new Map(),
  lastPromptBySession: new Map(),
  lastKeptBySession: new Map(),
  lastMemstyleBySession: new Map(),
};

const hook = createBeforePromptBuild(api, sidecar, rt);
const res = await hook(
  {
    prompt: "普通に会話しよう",
    messages: [{ role: "user", content: "こんにちは" }],
  },
  { sessionKey: "qa" }
);

const prepend = String(res?.prependContext || "");
const ok = prepend.includes("<MEMSTYLE v1>") && prepend.includes("persona=ロックマン") && prepend.includes("callUser=ヒロ");
if (!ok) {
  console.error(JSON.stringify({ ok: false, prepend }, null, 2));
  process.exit(1);
}
console.log(JSON.stringify({ ok: true }, null, 2));

