import { defaults, getCfg, logInfo } from "../config/schema.js";
import { composeInjectedBlocks } from "../lib/memctx_blocks.js";
import { SidecarClient } from "../lib/sidecar_client.js";
import { enforceTotalInputCap, trimRecentToBudget } from "../lib/token_budget.js";
import { normalizeMessages } from "../lib/token_estimate.js";
import type { RuntimeState } from "../types.js";

function sessionKeyOf(event: any, hookCtx: any): string {
  return String(hookCtx?.sessionKey ?? hookCtx?.sessionId ?? event?.sessionKey ?? event?.sessionId ?? "default");
}

export function createBeforePromptBuild(api: any, sidecar: SidecarClient, runtime: RuntimeState) {
  return async (event: any, hookCtx: any): Promise<any> => {
    const sessionKey = sessionKeyOf(event, hookCtx);
    const prompt = String(event?.prompt ?? "");
    runtime.lastPromptBySession.set(sessionKey, prompt);
    const messages = normalizeMessages(Array.isArray(event?.messages) ? event.messages : []);
    const lastUser = [...messages].reverse().find((message) => message.role === "user");
    if (lastUser?.text) runtime.lastUserBySession.set(sessionKey, lastUser.text);

    const workspaceRoot = String(getCfg(api, "memq.workspaceRoot", defaults["memq.workspaceRoot"]));
    const env = {
      MEMQ_BRAIN_MODE: String(getCfg(api, "memq.brain.mode", defaults["memq.brain.mode"])),
      MEMQ_BRAIN_PROVIDER: String(getCfg(api, "memq.brain.provider", defaults["memq.brain.provider"])),
      MEMQ_BRAIN_BASE_URL: String(getCfg(api, "memq.brain.baseUrl", defaults["memq.brain.baseUrl"])),
      MEMQ_BRAIN_MODEL: String(getCfg(api, "memq.brain.model", defaults["memq.brain.model"])),
      MEMQ_BRAIN_KEEP_ALIVE: String(getCfg(api, "memq.brain.keepAlive", defaults["memq.brain.keepAlive"])),
      MEMQ_BRAIN_TIMEOUT_MS: String(getCfg(api, "memq.brain.timeoutMs", defaults["memq.brain.timeoutMs"])),
      MEMQ_STYLE_TOKENS: String(getCfg(api, "memq.budgets.styleTokens", defaults["memq.budgets.styleTokens"])),
      MEMQ_RULES_TOKENS: String(getCfg(api, "memq.budgets.rulesTokens", defaults["memq.budgets.rulesTokens"])),
      MEMQ_MEMCTX_TOKENS: String(getCfg(api, "memq.budgets.memctxTokens", defaults["memq.budgets.memctxTokens"])),
      MEMQ_RECENT_TOKENS: String(getCfg(api, "memq.recent.maxTokens", defaults["memq.recent.maxTokens"])),
      MEMQ_RECENT_MIN_KEEP_MESSAGES: String(getCfg(api, "memq.recent.minKeepMessages", defaults["memq.recent.minKeepMessages"])),
      MEMQ_TOTAL_MAX_INPUT_TOKENS: String(getCfg(api, "memq.total.maxInputTokens", defaults["memq.total.maxInputTokens"])),
      MEMQ_TOTAL_RESERVE_TOKENS: String(getCfg(api, "memq.total.reserveTokens", defaults["memq.total.reserveTokens"])),
    };
    const brainRequired = String(env.MEMQ_BRAIN_MODE).includes("required");
    const degradedEnabled = Boolean(getCfg(api, "memq.degraded.enabled", defaults["memq.degraded.enabled"]));
    const trim = trimRecentToBudget(messages, {
      totalMaxInputTokens: Number(env.MEMQ_TOTAL_MAX_INPUT_TOKENS),
      totalReserveTokens: Number(env.MEMQ_TOTAL_RESERVE_TOKENS),
      memctxTokens: Number(env.MEMQ_MEMCTX_TOKENS),
      rulesTokens: Number(env.MEMQ_RULES_TOKENS),
      styleTokens: Number(env.MEMQ_STYLE_TOKENS),
      recentMaxTokens: Number(env.MEMQ_RECENT_TOKENS),
      recentMinKeepMessages: Number(env.MEMQ_RECENT_MIN_KEEP_MESSAGES),
    }, prompt);
    const ensured = await sidecar.ensureUp(workspaceRoot, env);
    if (!ensured) {
      if (brainRequired || !degradedEnabled) throw new Error("memq_sidecar_required_unavailable");
      if (Array.isArray(event?.messages)) {
        const keptRaw = trim.kept.map((message) => message.raw).filter(Boolean);
        event.messages.splice(0, event.messages.length, ...keptRaw);
      }
      logInfo(api, `[memq-v3] session=${sessionKey} degraded=1 reason=sidecar_unavailable`);
      return {};
    }

    await sidecar.idleTick(Math.floor(Date.now() / 1000));
    let response;
    try {
      response = await sidecar.memctxQuery(
        {
          sessionKey,
          prompt,
          recentMessages: trim.kept.map((message) => ({ role: message.role, text: message.text, ts: message.ts })),
          budgets: {
            memctxTokens: Number(env.MEMQ_MEMCTX_TOKENS),
            rulesTokens: Number(env.MEMQ_RULES_TOKENS),
            styleTokens: Number(env.MEMQ_STYLE_TOKENS),
          },
          topK: Number(getCfg(api, "memq.retrieval.topK", defaults["memq.retrieval.topK"])),
        },
        Number(env.MEMQ_BRAIN_TIMEOUT_MS)
      );
    } catch (error) {
      if (brainRequired || !degradedEnabled) throw error;
      if (Array.isArray(event?.messages)) {
        const keptRaw = trim.kept.map((message) => message.raw).filter(Boolean);
        event.messages.splice(0, event.messages.length, ...keptRaw);
      }
      logInfo(api, `[memq-v3] session=${sessionKey} degraded=1 reason=memctx_query_failed`);
      return {};
    }

    const enforced = enforceTotalInputCap(
      {
        prompt,
        recent: trim.kept,
        memrules: response.memrules || "",
        memstyle: response.memstyle || "",
        memctx: response.memctx || "",
      },
      {
        totalMaxInputTokens: Number(env.MEMQ_TOTAL_MAX_INPUT_TOKENS),
        totalReserveTokens: Number(env.MEMQ_TOTAL_RESERVE_TOKENS),
        memctxTokens: Number(env.MEMQ_MEMCTX_TOKENS),
        rulesTokens: Number(env.MEMQ_RULES_TOKENS),
        styleTokens: Number(env.MEMQ_STYLE_TOKENS),
        recentMaxTokens: Number(env.MEMQ_RECENT_TOKENS),
        recentMinKeepMessages: Number(env.MEMQ_RECENT_MIN_KEEP_MESSAGES),
      }
    );

    const finalKept = enforced.recent;
    const finalMemctx = enforced.memctx;
    if (Array.isArray(event?.messages)) {
      const keptRaw = finalKept.map((message) => message.raw).filter(Boolean);
      event.messages.splice(0, event.messages.length, ...keptRaw);
    }

    runtime.lastMemstyleBySession.set(sessionKey, response.memstyle || "");
    const prependContext = composeInjectedBlocks(response.memrules, response.memstyle, finalMemctx);
    const traceId = String(response.meta?.debug?.trace_id ?? "");
    logInfo(api, `[memq][brain-proof] turn=before_prompt_build session=${sessionKey} trace_id=${traceId} op=recall_plan model=${env.MEMQ_BRAIN_MODEL} ps_seen=${response.meta?.debug?.ps_seen ? 1 : 0}`);
    logInfo(
      api,
      `[memq-v3] session=${sessionKey} tokens.system=${enforced.breakdown.system} tokens.rules=${enforced.breakdown.rules} tokens.style=${enforced.breakdown.style} tokens.ctx=${enforced.breakdown.ctx} tokens.recent=${enforced.breakdown.recent} tokens.total=${enforced.breakdown.total} tokens.cap=${enforced.breakdown.cap} recent_kept=${finalKept.length}`
    );
    return { prependContext };
  };
}
