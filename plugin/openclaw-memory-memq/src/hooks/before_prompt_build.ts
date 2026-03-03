import { archivePrunedMessages } from "../lib/archive.js";
import { composeInjectedBlocks, ensureBudget } from "../lib/memctx_blocks.js";
import { flushIngestQueue } from "../lib/ingest_queue.js";
import { readStyleCache, writeStyleCache } from "../lib/style_cache.js";
import { SidecarClient } from "../lib/sidecar_client.js";
import { estimateTokens, messageText, normalizeMessages, splitRecentByTokenBudget } from "../lib/token_estimate.js";
import { defaults, getCfg, logInfo } from "../config/schema.js";
import type { RuntimeState } from "../types.js";

function sessionKeyOf(event: any, hookCtx: any): string {
  return String(hookCtx?.sessionKey ?? hookCtx?.sessionId ?? event?.sessionKey ?? event?.sessionId ?? "default");
}

function buildDegradedBlocks(prompt: string, budgets: { memctx: number; rules: number; style: number }): {
  memrules: string;
  memstyle: string;
  memctx: string;
} {
  const memrules = ensureBudget([
    "rules.security.never_output_secrets=true",
    "rules.operation.allow_user_requested_local_config=true",
  ].join("\n"), budgets.rules);
  const memstyle = "";
  const memctx = ensureBudget([
    `ctx.mode=degraded`,
    `ctx.prompt_digest=${prompt.slice(0, 96).replace(/\s+/g, " ")}`,
    "ctx.memory=surface_only_minimal",
  ].join("\n"), budgets.memctx);
  return { memrules, memstyle, memctx };
}

function parseImmediateStyleOverrides(prompt: string): Record<string, string> {
  const s = String(prompt || "");
  const out: Record<string, string> = {};
  const normalizeCallUser = (raw: string): string => {
    return raw
      .trim()
      .replace(/^["「]|["」]$/g, "")
      .replace(/\s+/g, " ")
      .replace(/(?:って)$/u, "")
      .replace(/[。.!！?？]+$/u, "")
      .trim();
  };

  const mCallQuoted = s.match(
    /(?:呼び方は|ユーザー呼称は|あなたの呼び方は|call me(?: as)?|refer to me as)\s*[「"]?([^」"\n。]{1,24})[」"]?/i
  );
  if (mCallQuoted) out.callUser = normalizeCallUser(mCallQuoted[1]);

  const mCallJp = s.match(
    /(?:俺|ぼく|僕|私|わたし|オレ)のことは\s*[「"]?([^」"\n。]{1,24}?)(?:[」"]?\s*(?:って|と)?\s*呼(?:んで|べ|んでね)?)/i
  );
  if (mCallJp) out.callUser = normalizeCallUser(mCallJp[1]);

  const mPrefix = s.match(/文頭(?:は|を)?\s*[「"]([^」"\n]{1,24})[」"]/i);
  if (mPrefix) out.prefix = mPrefix[1].trim();

  if (out.callUser && !out.prefix) out.prefix = `${out.callUser}、`;
  return out;
}

function mergeMemstyle(base: string, overrides: Record<string, string>, budgetTokens: number): string {
  const lines = String(base || "")
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);

  let budgetLine = `budget_tokens=${budgetTokens}`;
  const order: string[] = [];
  const kv = new Map<string, string>();
  for (const ln of lines) {
    if (ln.startsWith("budget_tokens=")) {
      budgetLine = ln;
      continue;
    }
    const i = ln.indexOf("=");
    if (i <= 0) continue;
    const k = ln.slice(0, i).trim();
    const v = ln.slice(i + 1).trim();
    if (!k) continue;
    if (!kv.has(k)) order.push(k);
    kv.set(k, v);
  }

  for (const [k, v] of Object.entries(overrides)) {
    if (!v) continue;
    if (!kv.has(k)) order.push(k);
    kv.set(k, v);
  }

  const merged = [budgetLine, ...order.map((k) => `${k}=${kv.get(k) || ""}`)].join("\n");
  return ensureBudget(merged, budgetTokens);
}

function toolCallIdsInMessage(m: any): string[] {
  const ids = new Set<string>();
  if (!m || typeof m !== "object") return [];

  if (String(m.role ?? "") === "assistant") {
    const blocks = Array.isArray(m.content) ? m.content : [];
    for (const b of blocks) {
      if (!b || typeof b !== "object") continue;
      const t = String((b as any).type ?? "");
      if (t === "toolCall" || t === "tool_call" || t === "tool-use" || t === "function_call") {
        const id = String((b as any).id ?? "").trim();
        if (id) ids.add(id);
      }
    }

    const toolCalls = Array.isArray((m as any).tool_calls) ? (m as any).tool_calls : [];
    for (const tc of toolCalls) {
      const id = String(tc?.id ?? "").trim();
      if (id) ids.add(id);
    }
  }
  return [...ids];
}

function toolResultIdInMessage(m: any): string {
  if (!m || typeof m !== "object") return "";
  return String(
    (m as any).toolCallId ??
      (m as any).tool_call_id ??
      (m as any).toolCallID ??
      (m as any).call_id ??
      ""
  ).trim();
}

function toolResultIdsInContentBlocks(m: any): string[] {
  const ids = new Set<string>();
  if (!m || typeof m !== "object") return [];
  const blocks = Array.isArray((m as any).content) ? (m as any).content : [];
  for (const b of blocks) {
    if (!b || typeof b !== "object") continue;
    const t = String((b as any).type ?? "");
    if (t !== "toolResult" && t !== "tool_result" && t !== "function_call_output" && t !== "tool-output") continue;
    const id = String(
      (b as any).toolCallId ??
        (b as any).tool_call_id ??
        (b as any).call_id ??
        (b as any).id ??
        ""
    ).trim();
    if (id) ids.add(id);
  }
  return [...ids];
}

function filterOrphanToolResultBlocks(m: any, callIds: Set<string>): { msg: any; removed: number } {
  const blocks = Array.isArray(m?.content) ? m.content : null;
  if (!blocks) return { msg: m, removed: 0 };
  let removed = 0;
  const keptBlocks = blocks.filter((b: any) => {
    if (!b || typeof b !== "object") return true;
    const t = String((b as any).type ?? "");
    if (t !== "toolResult" && t !== "tool_result" && t !== "function_call_output" && t !== "tool-output") {
      return true;
    }
    const id = String(
      (b as any).toolCallId ??
        (b as any).tool_call_id ??
        (b as any).call_id ??
        (b as any).id ??
        ""
    ).trim();
    if (id && callIds.has(id)) return true;
    removed += 1;
    return false;
  });
  if (removed === 0) return { msg: m, removed: 0 };
  return { msg: { ...m, content: keptBlocks }, removed };
}

export function repairToolIntegrity(messages: any[]): { kept: any[]; removed: number } {
  if (!Array.isArray(messages) || messages.length === 0) return { kept: [], removed: 0 };

  const callIds = new Set<string>();
  for (const m of messages) {
    for (const id of toolCallIdsInMessage(m)) callIds.add(id);
  }

  const kept: any[] = [];
  let removed = 0;
  for (const m of messages) {
    const role = String(m?.role ?? "");
    const isToolResult = role === "toolResult" || role === "tool" || role === "function";
    if (!isToolResult) {
      kept.push(m);
      continue;
    }
    const rid = toolResultIdInMessage(m);
    if (rid && callIds.has(rid)) {
      kept.push(m);
      continue;
    }
    removed += 1;
  }

  const repaired: any[] = [];
  for (const m of kept) {
    const { msg, removed: r } = filterOrphanToolResultBlocks(m, callIds);
    removed += r;
    // if message became empty content array, keep it only when it still carries text content
    if (Array.isArray(msg?.content) && msg.content.length === 0) {
      const rid = toolResultIdInMessage(msg);
      const blockIds = toolResultIdsInContentBlocks(msg);
      if (!rid && blockIds.length === 0) continue;
    }
    repaired.push(msg);
  }
  return { kept: repaired, removed };
}

export function createBeforePromptBuild(api: any, sidecar: SidecarClient, rt: RuntimeState) {
  return async (event: any, hookCtx: any): Promise<any> => {
    const t0 = Date.now();
    const sessionKey = sessionKeyOf(event, hookCtx);
    const workspaceRoot = getCfg(api, "memq.workspaceRoot", defaults["memq.workspaceRoot"]);
    const prompt = String(event?.prompt ?? "");
    rt.lastPromptBySession.set(sessionKey, prompt);
    const cachedStyle = readStyleCache(workspaceRoot, sessionKey);
    if (cachedStyle && !rt.lastMemstyleBySession.get(sessionKey)) {
      rt.lastMemstyleBySession.set(sessionKey, cachedStyle);
    }

    const styleBase = Math.max(8, getCfg(api, "memq.budgets.styleTokens", defaults["memq.budgets.styleTokens"]));
    const styleMax = Math.max(styleBase, getCfg(api, "memq.style.maxBudgetTokens", defaults["memq.style.maxBudgetTokens"]));
    let styleBudget = styleBase;
    const styleCue = /(口調|性格|キャラ|人格|style|persona|なりき|呼び方|一人称)/i.test(prompt);
    const prevStyle = String(rt.lastMemstyleBySession.get(sessionKey) || "");
    const prevNeed = Math.min(styleMax, Math.max(72, estimateTokens(prevStyle) + 12));
    if (styleCue) styleBudget = Math.max(styleBudget, 120);
    if (prevNeed > styleBudget) styleBudget = Math.max(styleBudget, prevNeed);

    const budgets = {
      memctx: Math.max(32, getCfg(api, "memq.budgets.memctxTokens", defaults["memq.budgets.memctxTokens"])),
      rules: Math.max(24, getCfg(api, "memq.budgets.rulesTokens", defaults["memq.budgets.rulesTokens"])),
      style: Math.min(styleMax, styleBudget),
    };

    const recentMax = Math.max(800, getCfg(api, "memq.recent.maxTokens", defaults["memq.recent.maxTokens"]));
    const minKeep = Math.max(2, getCfg(api, "memq.recent.minKeepMessages", defaults["memq.recent.minKeepMessages"]));
    const recallLikePrompt = /(覚えて|記憶|これまで|君は誰|あなたは誰|家族|呼称|一人称|最近|昨日|一昨日|先週|先月|summary|recap|who are you|yesterday|recent)/i.test(
      prompt
    );
    const codingLikePrompt = /(コード|code|bug|error|stack|trace|diff|patch|test|build|compile|実装|修正)/i.test(prompt);
    const recentBudget = recallLikePrompt && !codingLikePrompt ? Math.max(1200, Math.min(recentMax, Math.floor(recentMax * 0.45))) : recentMax;

    const topK = Math.max(1, getCfg(api, "memq.retrieval.topK", defaults["memq.retrieval.topK"]));
    const surfaceThreshold = Number(getCfg(api, "memq.retrieval.surfaceThreshold", defaults["memq.retrieval.surfaceThreshold"]));
    const deepEnabled = Boolean(getCfg(api, "memq.retrieval.deepEnabled", defaults["memq.retrieval.deepEnabled"]));
    const styleEnabled = Boolean(getCfg(api, "memq.style.enabled", defaults["memq.style.enabled"]));

    const ensured = await sidecar.ensureUp(workspaceRoot);
    if (!ensured) {
      logInfo(api, `[memq-v2] before_prompt_build sidecar_unavailable session=${sessionKey}`);
    } else {
      try {
        const q = await flushIngestQueue(workspaceRoot, sidecar, 64);
        if (q.sent > 0 || q.remain > 0) {
          logInfo(api, `[memq-v2] before_prompt_build ingest_queue_flush sent=${q.sent} remain=${q.remain}`);
        }
      } catch {
        // best effort
      }
    }

    try {
      await sidecar.idleTick(Math.floor(Date.now() / 1000));
    } catch {
      // best effort; degraded mode handles outage
    }

    const messages = Array.isArray(event?.messages) ? event.messages : [];
    const sliced = splitRecentByTokenBudget(messages, recentBudget, minKeep);

    if (sliced.pruned.length > 0) {
      if (getCfg(api, "memq.archive.enabled", defaults["memq.archive.enabled"])) {
        archivePrunedMessages({
          workspaceRoot,
          sessionKey,
          pruned: sliced.pruned,
          maxFileBytes: Math.max(2048, getCfg(api, "memq.archive.maxFileBytes", defaults["memq.archive.maxFileBytes"])),
          maxFiles: Math.max(1, getCfg(api, "memq.archive.maxFiles", defaults["memq.archive.maxFiles"])),
        });
      }

      try {
        const prunedNorm = normalizeMessages(sliced.pruned).filter((m) => m.role === "user" || m.role === "assistant");
        await sidecar.summarizeConversation(sessionKey, prunedNorm, "surface_only");
        await sidecar.summarizeConversation(sessionKey, prunedNorm, "deep");
      } catch {
        // degraded path keeps running
      }
    }

    const repaired = repairToolIntegrity(sliced.kept);
    if (repaired.removed > 0) {
      logInfo(api, `[memq-v2] before_prompt_build session=${sessionKey} tool_orphan_removed=${repaired.removed}`);
    }
    const keptForPrompt = repaired.kept;

    // in-place rewrite for latest OpenClaw runtime behavior
    if (Array.isArray(event?.messages) && (sliced.keepStart > 0 || repaired.removed > 0)) {
      event.messages.splice(0, event.messages.length, ...keptForPrompt);
    }

    const recentMessages = normalizeMessages(keptForPrompt).filter((m) => m.role === "user" || m.role === "assistant");
    rt.lastKeptBySession.set(sessionKey, recentMessages);
    const lastUser = [...recentMessages].reverse().find((m) => m.role === "user")?.text ?? prompt;
    rt.lastUserBySession.set(sessionKey, lastUser);

    let memrules = "";
    let memstyle = "";
    let memctx = "";
    let surfaceHit = false;
    let deepCalled = false;

    try {
      const q = await sidecar.memctxQuery({
        sessionKey,
        prompt,
        recentMessages,
        budgets: {
          memctxTokens: budgets.memctx,
          rulesTokens: budgets.rules,
          styleTokens: budgets.style,
        },
        topK,
        surfaceThreshold,
        deepEnabled,
      });
      memrules = ensureBudget(q.memrules || "", budgets.rules);
      memstyle = styleEnabled ? ensureBudget(q.memstyle || "", budgets.style) : "";
      memctx = ensureBudget(q.memctx || "", budgets.memctx);
      if (styleEnabled) {
        const overrides = parseImmediateStyleOverrides(prompt);
        if (Object.keys(overrides).length > 0) {
          memstyle = mergeMemstyle(memstyle, overrides, budgets.style);
        }
      }
      rt.lastMemstyleBySession.set(sessionKey, memstyle);
      writeStyleCache(workspaceRoot, sessionKey, memstyle);
      surfaceHit = Boolean(q.meta?.surfaceHit);
      deepCalled = Boolean(q.meta?.deepCalled);
    } catch (err) {
      const degradedEnabled = getCfg(api, "memq.degraded.enabled", defaults["memq.degraded.enabled"]);
      if (!degradedEnabled) throw err;
      const d = buildDegradedBlocks(prompt, budgets);
      memrules = d.memrules;
      memstyle =
        (styleEnabled ? rt.lastMemstyleBySession.get(sessionKey) || cachedStyle : "") || d.memstyle;
      if (styleEnabled && memstyle) {
        memstyle = ensureBudget(memstyle, budgets.style);
      }
      memctx = d.memctx;
      rt.lastMemstyleBySession.set(sessionKey, memstyle);
    }

    const prependContext = composeInjectedBlocks(memrules, memstyle, memctx);
    const injectedTokens = estimateTokens(prependContext);

    logInfo(
      api,
      `[memq-v2] before_prompt_build session=${sessionKey} kept_msgs=${keptForPrompt.length} pruned_msgs=${sliced.pruned.length} kept_tokens=${sliced.keptTokens} recent_budget=${recentBudget} recent_cfg=${recentMax} injected_tokens=${injectedTokens} surface_hit=${surfaceHit ? 1 : 0} deep_called=${deepCalled ? 1 : 0} latency_ms=${Date.now() - t0}`
    );

    return { prependContext };
  };
}
