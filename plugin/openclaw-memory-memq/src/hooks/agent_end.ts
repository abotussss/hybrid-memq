import { SidecarClient } from "../lib/sidecar_client.js";
import { enqueueIngest } from "../lib/ingest_queue.js";
import { messageText } from "../lib/token_estimate.js";
import { defaults, getCfg, logInfo } from "../config/schema.js";
import type { RuntimeState } from "../types.js";

function collectMessages(event: any, hookCtx: any): any[] {
  const bag = [event, hookCtx, event?.result, hookCtx?.result, event?.response, hookCtx?.response];
  let best: any[] = [];
  for (const x of bag) {
    const arr = Array.isArray(x?.messages) ? x.messages : [];
    if (arr.length > best.length) best = arr;
  }
  return best;
}

function collectActionSummaries(messages: any[]): string[] {
  const out: string[] = [];
  const push = (s: string) => {
    const v = String(s || "").replace(/\s+/g, " ").trim();
    if (!v) return;
    out.push(v.slice(0, 200));
  };
  const recent = messages.slice(-24);
  for (const m of recent) {
    const role = String(m?.role ?? "").toLowerCase();
    if (role === "tool" || role === "toolresult" || role === "function") {
      const t = messageText(m);
      if (t) push(`${role}:${t}`);
    }
    if (role === "assistant") {
      const toolCalls = Array.isArray((m as any)?.tool_calls) ? (m as any).tool_calls : [];
      for (const tc of toolCalls) {
        const nm = String(tc?.function?.name ?? tc?.name ?? "").trim();
        if (nm) push(`tool_call:${nm}`);
      }
      const blocks = Array.isArray((m as any)?.content) ? (m as any).content : [];
      for (const b of blocks) {
        const bt = String((b as any)?.type ?? "");
        if (bt === "toolCall" || bt === "tool_call" || bt === "function_call") {
          const nm = String((b as any)?.name ?? (b as any)?.function?.name ?? "").trim();
          if (nm) push(`tool_call:${nm}`);
        }
      }
    }
  }
  const uniq: string[] = [];
  const seen = new Set<string>();
  for (const x of out) {
    const k = x.toLowerCase();
    if (seen.has(k)) continue;
    seen.add(k);
    uniq.push(x);
  }
  return uniq.slice(0, 8);
}

export function createAgentEnd(api: any, sidecar: SidecarClient, rt: RuntimeState) {
  return async (event: any, hookCtx: any): Promise<void> => {
    const sessionKey = String(hookCtx?.sessionKey ?? hookCtx?.sessionId ?? event?.sessionKey ?? "default");
    const workspaceRoot = getCfg(api, "memq.workspaceRoot", defaults["memq.workspaceRoot"]);
    const messages = collectMessages(event, hookCtx);

    const lastUser = [...messages].reverse().find((m) => String(m?.role ?? "") === "user");
    const lastAssistant = [...messages].reverse().find((m) => String(m?.role ?? "") === "assistant");

    // Prefer the pre-injection prompt captured in before_prompt_build.
    const userText = rt.lastUserBySession.get(sessionKey) || messageText(lastUser) || rt.lastPromptBySession.get(sessionKey) || "";
    const assistantText = messageText(lastAssistant);

    if (!userText && !assistantText) return;
    const actionSummaries = collectActionSummaries(messages);

    try {
      const payload = {
        sessionKey,
        userText: userText.slice(0, 2400),
        assistantText: assistantText.slice(0, 2400),
        ts: Math.floor(Date.now() / 1000),
        metadata: actionSummaries.length > 0 ? { actionSummaries } : undefined,
      };
      const res = await sidecar.ingestTurn(payload);
      const traceId = String(res?.traceId || "");
      const wrote = JSON.stringify(res?.wrote || {});
      logInfo(api, `[memq-v2] agent_end session=${sessionKey} ingested=1 trace_id=${traceId} wrote=${wrote}`);
      logInfo(api, `[memq][brain-proof] session=${sessionKey} op=ingest_plan trace_id=${traceId} model=gpt-oss:20b`);
    } catch (err) {
      const em = String((err as Error)?.message || err || "unknown_error").replace(/\s+/g, " ").slice(0, 280);
      try {
        enqueueIngest(workspaceRoot, {
          sessionKey,
          userText: userText.slice(0, 2400),
          assistantText: assistantText.slice(0, 2400),
          ts: Math.floor(Date.now() / 1000),
          metadata: actionSummaries.length > 0 ? { actionSummaries } : undefined,
        });
      } catch {
        // best effort queue
      }
      logInfo(api, `[memq-v2] agent_end session=${sessionKey} ingest_failed=${(err as Error).message}`);
      logInfo(api, `[memq][brain-proof] session=${sessionKey} op=ingest_plan trace_id= err=1 model=gpt-oss:20b error=${em}`);
    }
  };
}
