import { archivePrunedMessages } from "../lib/archive.js";
import { composeInjectedBlocks, ensureBudget } from "../lib/memctx_blocks.js";
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
    "rules.security.no_secrets=true",
    "rules.security.refuse_api_keys=true",
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

export function createBeforePromptBuild(api: any, sidecar: SidecarClient, rt: RuntimeState) {
  return async (event: any, hookCtx: any): Promise<any> => {
    const t0 = Date.now();
    const sessionKey = sessionKeyOf(event, hookCtx);
    const workspaceRoot = getCfg(api, "memq.workspaceRoot", defaults["memq.workspaceRoot"]);

    const budgets = {
      memctx: Math.max(32, getCfg(api, "memq.budgets.memctxTokens", defaults["memq.budgets.memctxTokens"])),
      rules: Math.max(24, getCfg(api, "memq.budgets.rulesTokens", defaults["memq.budgets.rulesTokens"])),
      style: Math.max(8, getCfg(api, "memq.budgets.styleTokens", defaults["memq.budgets.styleTokens"])),
    };

    const recentMax = Math.max(800, getCfg(api, "memq.recent.maxTokens", defaults["memq.recent.maxTokens"]));
    const minKeep = Math.max(2, getCfg(api, "memq.recent.minKeepMessages", defaults["memq.recent.minKeepMessages"]));

    const topK = Math.max(1, getCfg(api, "memq.retrieval.topK", defaults["memq.retrieval.topK"]));
    const surfaceThreshold = Number(getCfg(api, "memq.retrieval.surfaceThreshold", defaults["memq.retrieval.surfaceThreshold"]));
    const deepEnabled = Boolean(getCfg(api, "memq.retrieval.deepEnabled", defaults["memq.retrieval.deepEnabled"]));
    const styleEnabled = Boolean(getCfg(api, "memq.style.enabled", defaults["memq.style.enabled"]));

    const prompt = String(event?.prompt ?? "");
    rt.lastPromptBySession.set(sessionKey, prompt);

    try {
      await sidecar.idleTick(Math.floor(Date.now() / 1000));
    } catch {
      // best effort; degraded mode handles outage
    }

    const messages = Array.isArray(event?.messages) ? event.messages : [];
    const sliced = splitRecentByTokenBudget(messages, recentMax, minKeep);

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
        const prunedNorm = normalizeMessages(sliced.pruned);
        await sidecar.summarizeConversation(sessionKey, prunedNorm, "surface_only");
        await sidecar.summarizeConversation(sessionKey, prunedNorm, "deep");
      } catch {
        // degraded path keeps running
      }
    }

    // in-place prune for latest OpenClaw runtime behavior
    if (Array.isArray(event?.messages) && sliced.keepStart > 0) {
      event.messages.splice(0, event.messages.length, ...sliced.kept);
    }

    const recentMessages = normalizeMessages(sliced.kept);
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
      surfaceHit = Boolean(q.meta?.surfaceHit);
      deepCalled = Boolean(q.meta?.deepCalled);
    } catch (err) {
      const degradedEnabled = getCfg(api, "memq.degraded.enabled", defaults["memq.degraded.enabled"]);
      if (!degradedEnabled) throw err;
      const d = buildDegradedBlocks(prompt, budgets);
      memrules = d.memrules;
      memstyle = d.memstyle;
      memctx = d.memctx;
    }

    const prependContext = composeInjectedBlocks(memrules, memstyle, memctx);
    const injectedTokens = estimateTokens(prependContext);

    logInfo(
      api,
      `[memq-v2] before_prompt_build session=${sessionKey} kept_msgs=${sliced.kept.length} pruned_msgs=${sliced.pruned.length} kept_tokens=${sliced.keptTokens} recent_budget=${recentMax} injected_tokens=${injectedTokens} surface_hit=${surfaceHit ? 1 : 0} deep_called=${deepCalled ? 1 : 0} latency_ms=${Date.now() - t0}`
    );

    return { prependContext };
  };
}
