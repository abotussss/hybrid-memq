import { defaults, getCfg, logInfo } from "../config/schema.js";
import { SidecarClient } from "../lib/sidecar_client.js";
import { messageText } from "../lib/token_estimate.js";
import type { RuntimeState } from "../types.js";

function collectMessages(event: any, hookCtx: any): any[] {
  const bags = [event, hookCtx, event?.result, hookCtx?.result, event?.response, hookCtx?.response];
  for (const bag of bags) {
    if (Array.isArray(bag?.messages) && bag.messages.length) return bag.messages;
  }
  return [];
}

function collectActionSummary(messages: any[]): string[] {
  const summaries: string[] = [];
  for (const message of messages.slice(-20)) {
    const role = String(message?.role ?? "").toLowerCase();
    if (role === "tool" || role === "toolresult" || role === "function") {
      const text = messageText(message);
      if (text) summaries.push(`${role}:${text.slice(0, 160)}`);
    }
  }
  return summaries.slice(0, 6);
}

export function createAgentEnd(api: any, sidecar: SidecarClient, runtime: RuntimeState) {
  return async (event: any, hookCtx: any): Promise<void> => {
    const sessionKey = String(hookCtx?.sessionKey ?? hookCtx?.sessionId ?? event?.sessionKey ?? "default");
    const messages = collectMessages(event, hookCtx);
    const lastAssistant = [...messages].reverse().find((message) => String(message?.role ?? "") === "assistant");
    const userText = runtime.lastUserBySession.get(sessionKey) || runtime.lastPromptBySession.get(sessionKey) || "";
    const assistantText = messageText(lastAssistant);
    if (!userText.trim() && !assistantText.trim()) return;
    const response = await sidecar.ingestTurn(
      {
        sessionKey,
        userText: userText.slice(0, 4000),
        assistantText: assistantText.slice(0, 4000),
        ts: Math.floor(Date.now() / 1000),
        metadata: { actions: collectActionSummary(messages) },
      },
      Number(getCfg(api, "memq.brain.timeoutMs", defaults["memq.brain.timeoutMs"]))
    );
    const traceId = String(response?.traceId || "");
    logInfo(api, `[memq][brain-proof] turn=agent_end session=${sessionKey} trace_id=${traceId} op=ingest_plan model=${getCfg(api, "memq.brain.model", defaults["memq.brain.model"])} ps_seen=1`);
  };
}
