import { SidecarClient } from "../lib/sidecar_client.js";
import { getCfg, defaults, logInfo } from "../config/schema.js";
import type { RuntimeState } from "../types.js";

function extractTextTargets(event: any, hookCtx: any): Array<{ obj: any; key: string; text: string }> {
  const out: Array<{ obj: any; key: string; text: string }> = [];
  const bag = [event, hookCtx, event?.result, hookCtx?.result, event?.response, hookCtx?.response];
  for (const obj of bag) {
    if (!obj || typeof obj !== "object") continue;
    if (typeof obj.text === "string" && obj.text.trim()) out.push({ obj, key: "text", text: obj.text });
    if (Array.isArray(obj.payloads)) {
      for (const p of obj.payloads) {
        if (p && typeof p === "object" && typeof p.text === "string" && p.text.trim()) {
          out.push({ obj: p, key: "text", text: p.text });
        }
      }
    }
    if (Array.isArray(obj.messages)) {
      for (const m of obj.messages) {
        if (m && typeof m === "object" && m.role === "assistant" && typeof m.content === "string" && m.content.trim()) {
          out.push({ obj: m, key: "content", text: m.content });
        }
      }
    }
  }
  return out;
}

function parseKvLines(memstyle: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const raw of String(memstyle || "").split("\n")) {
    const ln = raw.trim();
    if (!ln || ln.startsWith("budget_tokens=")) continue;
    const idx = ln.indexOf("=");
    if (idx <= 0) continue;
    const k = ln.slice(0, idx).trim();
    const v = ln.slice(idx + 1).trim();
    if (!k || !v) continue;
    out.set(k, v);
  }
  return out;
}

function enforceIdentityStyle(text: string, memstyle: string): { text: string; changed: boolean } {
  let out = String(text || "");
  if (!out.trim() || !memstyle.trim()) return { text: out, changed: false };

  const kv = parseKvLines(memstyle);
  const persona = kv.get("persona") || "";
  const first = kv.get("firstPerson") || kv.get("mustFirstPerson") || "僕";
  const callUser = kv.get("callUser") || kv.get("mustCallUser") || "";
  const prefix = kv.get("prefix") || kv.get("mustPrefix") || (callUser ? `${callUser}、` : "");
  if (!persona) return { text: out, changed: false };

  const jpAssistantClaim = /(?:僕|私|わたし|俺)\s*は[^。!\n]{0,48}(?:OpenClaw|assistant|アシスタント)[^。!\n]{0,48}[。.!！?？]?/giu;
  const enAssistantClaim = /I\s*am[^.\n]{0,80}\bassistant\b[^.\n]{0,40}[.!?]?/giu;
  const hadClaim = jpAssistantClaim.test(out) || enAssistantClaim.test(out);
  if (!hadClaim) return { text: out, changed: false };

  out = out.replace(jpAssistantClaim, "").replace(enAssistantClaim, "").trim();
  const identityLine = `${prefix}${first}は${persona}として応答するよ。`.trim();
  out = out ? `${identityLine}\n${out}` : identityLine;
  return { text: out, changed: true };
}

export function createMessageSending(api: any, sidecar: SidecarClient, rt: RuntimeState) {
  return async (event: any, hookCtx: any): Promise<void> => {
    const enabled = getCfg(api, "memq.security.primaryRulesEnabled", defaults["memq.security.primaryRulesEnabled"]);
    if (!enabled) return;

    const sessionKey = String(hookCtx?.sessionKey ?? hookCtx?.sessionId ?? event?.sessionKey ?? "default");
    const mode = getCfg(api, "memq.security.llmAuditEnabled", defaults["memq.security.llmAuditEnabled"]) ? "dual" : "primary";
    const targets = extractTextTargets(event, hookCtx);
    if (!targets.length) return;

    // patch last assistant-ish text only
    const t = targets[targets.length - 1];
    try {
      const res = await sidecar.auditOutput({
        sessionKey,
        text: t.text,
        mode,
        thresholds: {
          llmAuditThreshold: getCfg(api, "memq.security.llmAuditThreshold", defaults["memq.security.llmAuditThreshold"]),
          blockThreshold: getCfg(api, "memq.security.blockThreshold", defaults["memq.security.blockThreshold"]),
        },
      });
      if (res.block && typeof res.redactedText === "string") {
        t.obj[t.key] = res.redactedText;
        logInfo(api, `[memq-v2] message_sending session=${sessionKey} redacted=1 risk=${res.risk.toFixed(2)}`);
      }
    } catch (err) {
      logInfo(api, `[memq-v2] message_sending session=${sessionKey} audit_failed=${(err as Error).message}`);
    }

    const memstyle = String(rt.lastMemstyleBySession.get(sessionKey) || "");
    if (memstyle) {
      const fixed = enforceIdentityStyle(String(t.obj[t.key] || ""), memstyle);
      if (fixed.changed) {
        t.obj[t.key] = fixed.text;
        logInfo(api, `[memq-v2] message_sending session=${sessionKey} style_identity_repair=1`);
      }
    }
  };
}
