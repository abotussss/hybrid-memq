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
    const brainMode = String(getCfg(api, "memq.brain.mode", defaults["memq.brain.mode"]) || "best_effort").toLowerCase();
    const brainProvider = String(getCfg(api, "memq.brain.provider", defaults["memq.brain.provider"]));
    const brainBaseUrl = String(getCfg(api, "memq.brain.baseUrl", defaults["memq.brain.baseUrl"]));
    const brainModel = String(getCfg(api, "memq.brain.model", defaults["memq.brain.model"]));
    const brainKeepAlive = String(getCfg(api, "memq.brain.keepAlive", defaults["memq.brain.keepAlive"]));
    const brainTimeoutMs = Number(getCfg(api, "memq.brain.timeoutMs", defaults["memq.brain.timeoutMs"]));
    const brainMaxTokens = Number(getCfg(api, "memq.brain.maxTokens", defaults["memq.brain.maxTokens"]));
    const brainAutoRestart = getCfg(api, "memq.brain.autoRestart", defaults["memq.brain.autoRestart"]);
    const brainRestartCooldownSec = Number(
      getCfg(api, "memq.brain.restartCooldownSec", defaults["memq.brain.restartCooldownSec"])
    );
    const brainRestartWaitMs = Number(getCfg(api, "memq.brain.restartWaitMs", defaults["memq.brain.restartWaitMs"]));
    const brainRequired = brainMode === "required";

    const recentMax = Math.max(800, getCfg(api, "memq.recent.maxTokens", defaults["memq.recent.maxTokens"]));
    const minKeep = Math.max(2, getCfg(api, "memq.recent.minKeepMessages", defaults["memq.recent.minKeepMessages"]));
    const totalMaxInput = Math.max(1200, getCfg(api, "memq.total.maxInputTokens", defaults["memq.total.maxInputTokens"]));
    const totalReserve = Math.max(0, getCfg(api, "memq.total.reserveTokens", defaults["memq.total.reserveTokens"]));
    const capSafetyRatio = Math.max(
      0.55,
      Math.min(0.95, Number(getCfg(api, "memq.total.capSafetyRatio", defaults["memq.total.capSafetyRatio"])))
    );
    const promptTokens = estimateTokens(prompt);
    const styleEnabled = Boolean(getCfg(api, "memq.style.enabled", defaults["memq.style.enabled"]));
    const recallLikePrompt = /(覚えて|記憶|これまで|君は誰|あなたは誰|家族|呼称|一人称|最近|昨日|一昨日|先週|先月|summary|recap|who are you|yesterday|recent)/i.test(
      prompt
    );
    const codingLikePrompt = /(コード|code|bug|error|stack|trace|diff|patch|test|build|compile|実装|修正)/i.test(prompt);
    const leanMode = codingLikePrompt && !recallLikePrompt;
    if (leanMode) {
      budgets.memctx = Math.max(48, Math.min(72, budgets.memctx));
    }
    let recentBudget = recallLikePrompt && !codingLikePrompt ? Math.max(1200, Math.min(recentMax, Math.floor(recentMax * 0.45))) : recentMax;
    const fixedEstimate = promptTokens + budgets.memctx + budgets.rules + (styleEnabled ? budgets.style : 0) + totalReserve;
    const recentCapByTotal = Math.max(220, Math.floor((totalMaxInput - fixedEstimate) * capSafetyRatio));
    recentBudget = Math.max(220, Math.min(recentBudget, recentCapByTotal));

    let topK = Math.max(1, getCfg(api, "memq.retrieval.topK", defaults["memq.retrieval.topK"]));
    if (leanMode) topK = Math.min(topK, 3);
    const surfaceThreshold = Number(getCfg(api, "memq.retrieval.surfaceThreshold", defaults["memq.retrieval.surfaceThreshold"]));
    const deepEnabled = Boolean(getCfg(api, "memq.retrieval.deepEnabled", defaults["memq.retrieval.deepEnabled"]));

    const ensured = await sidecar.ensureUp(workspaceRoot, {
      brainMode,
      brainProvider,
      brainBaseUrl,
      brainModel,
      brainKeepAlive,
      brainTimeoutMs,
      brainMaxTokens,
      brainAutoRestart,
      brainRestartCooldownSec,
      brainRestartWaitMs,
    });
    if (!ensured) {
      logInfo(api, `[memq-v2] before_prompt_build sidecar_unavailable session=${sessionKey}`);
      if (brainRequired) {
        throw new Error("memq_required_sidecar_unavailable_or_mismatched");
      }
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
    let sliced = splitRecentByTokenBudget(messages, recentBudget, minKeep);
    // Enforce total-cap pre-trim with pessimistic fixed estimate.
    let trimLoops = 0;
    while (sliced.keptTokens + fixedEstimate > totalMaxInput && trimLoops < 4) {
      recentBudget = Math.max(180, Math.floor(recentBudget * 0.78));
      const minKeepNow = recentBudget < 700 ? 2 : minKeep;
      sliced = splitRecentByTokenBudget(messages, recentBudget, minKeepNow);
      trimLoops += 1;
    }

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
    let keptForPrompt = repaired.kept;

    // in-place rewrite for latest OpenClaw runtime behavior
    if (
      Array.isArray(event?.messages) &&
      (sliced.pruned.length > 0 || repaired.removed > 0 || keptForPrompt.length !== messages.length)
    ) {
      event.messages.splice(0, event.messages.length, ...keptForPrompt);
    }

    let recentMessages = normalizeMessages(keptForPrompt).filter((m) => m.role === "user" || m.role === "assistant");
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
      rt.lastMemstyleBySession.set(sessionKey, memstyle);
      writeStyleCache(workspaceRoot, sessionKey, memstyle);
      surfaceHit = Boolean(q.meta?.surfaceHit);
      deepCalled = Boolean(q.meta?.deepCalled);
      const traceId = String((q as any)?.traceId || (q as any)?.meta?.traceId || (q as any)?.meta?.debug?.trace_id || "");
      const psSeen = Number((q as any)?.meta?.debug?.ps_seen || 0) > 0 ? 1 : 0;
      logInfo(api, `[memq][brain-proof] session=${sessionKey} op=recall_plan trace_id=${traceId} model=gpt-oss:20b ps_seen=${psSeen} latency_ms=${Date.now() - t0}`);
      if (brainRequired && psSeen !== 1) {
        throw new Error("brain_proof_missing_ps_seen");
      }
    } catch (err) {
      const em = String((err as Error)?.message || err || "unknown_error").replace(/\s+/g, " ").slice(0, 280);
      logInfo(
        api,
        `[memq][brain-proof] session=${sessionKey} op=recall_plan trace_id= err=1 model=gpt-oss:20b ps_seen=0 error=${em}`
      );
      if (brainRequired) throw err;
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

    let prependContext = composeInjectedBlocks(memrules, memstyle, memctx);
    let injectedTokens = estimateTokens(prependContext);
    let keptEstimated = splitRecentByTokenBudget(keptForPrompt, Number.MAX_SAFE_INTEGER, 1).keptTokens;
    let totalEstimatedInput = keptEstimated + injectedTokens + promptTokens + totalReserve;
    // Enforce total-cap post-trim as final safety net.
    if (totalEstimatedInput > totalMaxInput) {
      const emergencyRecentBudget = Math.max(140, totalMaxInput - (injectedTokens + promptTokens + totalReserve));
      const emergencyMinKeep = emergencyRecentBudget < 700 ? 2 : minKeep;
      const emergencySlice = splitRecentByTokenBudget(keptForPrompt, emergencyRecentBudget, emergencyMinKeep);
      if (emergencySlice.pruned.length > 0) {
        if (getCfg(api, "memq.archive.enabled", defaults["memq.archive.enabled"])) {
          archivePrunedMessages({
            workspaceRoot,
            sessionKey,
            pruned: emergencySlice.pruned,
            maxFileBytes: Math.max(2048, getCfg(api, "memq.archive.maxFileBytes", defaults["memq.archive.maxFileBytes"])),
            maxFiles: Math.max(1, getCfg(api, "memq.archive.maxFiles", defaults["memq.archive.maxFiles"])),
          });
        }
        try {
          const prunedNorm = normalizeMessages(emergencySlice.pruned).filter((m) => m.role === "user" || m.role === "assistant");
          await sidecar.summarizeConversation(sessionKey, prunedNorm, "surface_only");
          await sidecar.summarizeConversation(sessionKey, prunedNorm, "deep");
        } catch {
          // best effort
        }
      }
      const emergencyRepair = repairToolIntegrity(emergencySlice.kept);
      keptForPrompt = emergencyRepair.kept;
      if (Array.isArray(event?.messages)) {
        event.messages.splice(0, event.messages.length, ...keptForPrompt);
      }
      recentMessages = normalizeMessages(keptForPrompt).filter((m) => m.role === "user" || m.role === "assistant");
      rt.lastKeptBySession.set(sessionKey, recentMessages);
      const finalUser = [...recentMessages].reverse().find((m) => m.role === "user")?.text ?? prompt;
      rt.lastUserBySession.set(sessionKey, finalUser);

      keptEstimated = splitRecentByTokenBudget(keptForPrompt, Number.MAX_SAFE_INTEGER, 1).keptTokens;
      totalEstimatedInput = keptEstimated + injectedTokens + promptTokens + totalReserve;
    }
    const overCap = totalEstimatedInput > totalMaxInput ? 1 : 0;

    logInfo(
      api,
      `[memq-v2] before_prompt_build session=${sessionKey} kept_msgs=${keptForPrompt.length} pruned_msgs=${sliced.pruned.length} kept_tokens=${keptEstimated} recent_budget=${recentBudget} recent_cfg=${recentMax} trim_loops=${trimLoops} lean_mode=${leanMode ? 1 : 0} injected_tokens=${injectedTokens} total_est=${totalEstimatedInput} total_cap=${totalMaxInput} total_over=${overCap} surface_hit=${surfaceHit ? 1 : 0} deep_called=${deepCalled ? 1 : 0} latency_ms=${Date.now() - t0}`
    );

    return { prependContext };
  };
}
