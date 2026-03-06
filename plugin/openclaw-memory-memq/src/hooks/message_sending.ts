import { defaults, getCfg, logInfo } from "../config/schema.js";
import { SidecarClient } from "../lib/sidecar_client.js";
import type { RuntimeState } from "../types.js";

function extractTargets(event: any, hookCtx: any): Array<{ obj: any; key: string; text: string }> {
  const out: Array<{ obj: any; key: string; text: string }> = [];
  for (const bag of [event, hookCtx, event?.result, hookCtx?.result, event?.response, hookCtx?.response]) {
    if (!bag || typeof bag !== "object") continue;
    if (typeof bag.text === "string" && bag.text.trim()) out.push({ obj: bag, key: "text", text: bag.text });
    if (Array.isArray(bag.messages)) {
      for (const message of bag.messages) {
        if (message?.role === "assistant" && typeof message?.content === "string" && message.content.trim()) {
          out.push({ obj: message, key: "content", text: message.content });
        }
      }
    }
  }
  return out;
}

export function createMessageSending(api: any, sidecar: SidecarClient, _runtime: RuntimeState) {
  return async (event: any, hookCtx: any): Promise<void> => {
    if (!getCfg(api, "memq.security.primaryRulesEnabled", defaults["memq.security.primaryRulesEnabled"])) return;
    const targets = extractTargets(event, hookCtx);
    if (!targets.length) return;
    const target = targets[targets.length - 1];
    const sessionKey = String(hookCtx?.sessionKey ?? hookCtx?.sessionId ?? event?.sessionKey ?? "default");
    const result = await sidecar.auditOutput(
      {
        sessionKey,
        text: target.text,
        mode: getCfg(api, "memq.security.llmAuditEnabled", defaults["memq.security.llmAuditEnabled"]) ? "dual" : "primary",
      },
      30000
    );
    if (typeof result?.redactedText === "string" && result.redactedText.trim()) {
      target.obj[target.key] = result.redactedText;
    }
    if (result?.block && !result?.redactedText) {
      target.obj[target.key] = "[BLOCKED_BY_MEMRULES]";
    }
    logInfo(api, `[memq-v3] message_sending session=${sessionKey} risk=${Number(result?.risk || 0).toFixed(2)} blocked=${result?.block ? 1 : 0}`);
  };
}
