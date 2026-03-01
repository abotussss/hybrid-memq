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

    try {
      const payload = {
        sessionKey,
        userText: userText.slice(0, 2400),
        assistantText: assistantText.slice(0, 2400),
        ts: Math.floor(Date.now() / 1000),
      };
      await sidecar.ingestTurn(payload);
      logInfo(api, `[memq-v2] agent_end session=${sessionKey} ingested=1`);
    } catch (err) {
      try {
        enqueueIngest(workspaceRoot, {
          sessionKey,
          userText: userText.slice(0, 2400),
          assistantText: assistantText.slice(0, 2400),
          ts: Math.floor(Date.now() / 1000),
        });
      } catch {
        // best effort queue
      }
      logInfo(api, `[memq-v2] agent_end session=${sessionKey} ingest_failed=${(err as Error).message}`);
    }
  };
}
