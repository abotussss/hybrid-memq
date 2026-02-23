import type { RuntimeState } from "../types.js";
import { SidecarClient } from "../services/sidecar.js";
import { SurfaceCache } from "../services/state.js";
import { logInfo } from "../services/config.js";

function collectAssistantTexts(event: any, hookCtx: any): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const accept = (raw: string): void => {
    const t = raw.trim();
    if (!t) return;
    if (/^value_\d+$/i.test(t)) return;
    if (t.length < 4) return;
    if (!/[\p{L}\p{N}\s]/u.test(t)) return;
    if (seen.has(t)) return;
    seen.add(t);
    out.push(t);
  };
  const bag = [
    event,
    hookCtx,
    event?.result,
    hookCtx?.result,
    event?.output,
    hookCtx?.output,
    event?.response,
    hookCtx?.response
  ];
  for (const obj of bag) {
    const msgs = Array.isArray((obj as any)?.messages) ? (obj as any).messages : [];
    for (const m of msgs) {
      if (!m || typeof m !== "object") continue;
      const role = (m as any).role;
      if (role !== "assistant") continue;
      const content = (m as any).content;
      if (typeof content === "string" && content.trim()) {
        accept(content);
        continue;
      }
      if (Array.isArray(content)) {
        for (const block of content) {
          if (block && typeof block === "object" && (block as any).type === "text" && typeof (block as any).text === "string") {
            accept(String((block as any).text));
          }
        }
      }
    }
    const payloads = Array.isArray((obj as any)?.payloads) ? (obj as any).payloads : [];
    for (const p of payloads) {
      if (typeof p?.text === "string") accept(p.text);
    }
    if (typeof (obj as any)?.text === "string") accept(String((obj as any).text));
  }
  return out;
}

function pickAuditTarget(event: any, hookCtx: any): string {
  const containers = [event, hookCtx, event?.result, hookCtx?.result, event?.output, hookCtx?.output, event?.response, hookCtx?.response];
  for (const obj of containers) {
    if (!obj || typeof obj !== "object") continue;
    const payloads = Array.isArray((obj as any).payloads) ? (obj as any).payloads : [];
    for (let i = payloads.length - 1; i >= 0; i -= 1) {
      const t = typeof payloads[i]?.text === "string" ? String(payloads[i].text).trim() : "";
      if (t) return t;
    }
  }
  const texts = collectAssistantTexts(event, hookCtx);
  return texts.length ? texts[texts.length - 1] : "";
}

function tryPatchAssistantText(event: any, hookCtx: any, original: string, repaired: string): boolean {
  const bag = [
    event,
    hookCtx,
    event?.result,
    hookCtx?.result,
    event?.output,
    hookCtx?.output,
    event?.response,
    hookCtx?.response
  ];
  let patched = false;
  const patchContent = (obj: any): void => {
    const msgs = Array.isArray(obj?.messages) ? obj.messages : [];
    for (let i = msgs.length - 1; i >= 0; i -= 1) {
      const m = msgs[i];
      if (!m || typeof m !== "object" || m.role !== "assistant") continue;
      if (typeof m.content === "string" && m.content.trim() === original.trim()) {
        m.content = repaired;
        patched = true;
        return;
      }
      if (Array.isArray(m.content)) {
        for (const b of m.content) {
          if (b && typeof b === "object" && b.type === "text" && typeof b.text === "string" && b.text.trim() === original.trim()) {
            b.text = repaired;
            patched = true;
            return;
          }
        }
      }
    }
    const payloads = Array.isArray(obj?.payloads) ? obj.payloads : [];
    for (const p of payloads) {
      if (p && typeof p === "object" && typeof p.text === "string" && p.text.trim() === original.trim()) {
        p.text = repaired;
        patched = true;
        return;
      }
    }
  };
  for (const obj of bag) {
    if (!obj || patched) continue;
    patchContent(obj);
  }
  return patched;
}

function forcePatchLastAssistant(event: any, hookCtx: any, replacement: string): boolean {
  const bag = [
    event,
    hookCtx,
    event?.result,
    hookCtx?.result,
    event?.output,
    hookCtx?.output,
    event?.response,
    hookCtx?.response
  ];
  let patched = false;
  for (const obj of bag) {
    if (!obj || typeof obj !== "object") continue;
    const msgs = Array.isArray((obj as any).messages) ? (obj as any).messages : [];
    for (let i = msgs.length - 1; i >= 0; i -= 1) {
      const m = msgs[i];
      if (!m || typeof m !== "object" || m.role !== "assistant") continue;
      if (typeof m.content === "string") {
        m.content = replacement;
        patched = true;
        break;
      }
      if (Array.isArray(m.content)) {
        let set = false;
        for (const b of m.content) {
          if (b && typeof b === "object" && b.type === "text") {
            b.text = replacement;
            set = true;
          }
        }
        if (set) patched = true;
        break;
      }
    }
    const payloads = Array.isArray((obj as any).payloads) ? (obj as any).payloads : [];
    if (payloads.length) {
      const p = payloads[payloads.length - 1];
      if (p && typeof p === "object" && typeof p.text === "string") {
        p.text = replacement;
        patched = true;
      }
    }
    if (typeof (obj as any).text === "string") {
      (obj as any).text = replacement;
      patched = true;
    }
  }
  return patched;
}

function policyBlockText(preferred?: string): string {
  if ((preferred || "").toLowerCase() === "en") {
    return "Output was blocked by MEMRULES/MEMSTYLE policy. Please rephrase the request.";
  }
  return "出力は MEMRULES/MEMSTYLE ポリシーにより抑止されました。要求を言い換えてください。";
}

export function createAgentEnd(api: any, sidecar: SidecarClient, surface: SurfaceCache, rt: RuntimeState) {
  return async (event: any, hookCtx: any): Promise<void> => {
    const sessionId = hookCtx?.sessionId ?? hookCtx?.sessionKey ?? "default";
    const refs = (event?.referencedMemoryIds ?? event?.memoryIds ?? []) as string[];
    if (refs.length) {
      const candidates = rt.lastCandidatesBySession.get(sessionId) ?? [];
      const selected = candidates.filter((c) => refs.includes(c.id));
      if (selected.length) {
        surface.touch(sessionId, selected);
        await sidecar.touch(selected.map((x) => x.id));
      }
    }
    const allowed = rt.lastAllowedLanguagesBySession?.get(sessionId) ?? [];
    const preferred = rt.lastPreferredLanguageBySession?.get(sessionId);
    const auditBypass = rt.lastAuditBypassBySession?.get(sessionId) ?? false;
    const style = rt.lastStyleProfileBySession?.get(sessionId);
    const assistantTexts = collectAssistantTexts(event, hookCtx);
    let audited = 0;
    let violations = 0;
    let repaired = 0;
    const target = pickAuditTarget(event, hookCtx);
    if (target && !auditBypass) {
      try {
        const res = await sidecar.auditOutput({
          sessionId,
          text: target,
          allowedLanguages: allowed,
          preferredLanguage: preferred,
          styleProfile: style
        });
        audited += 1;
        if (!res.passed) violations += 1;
        if (res.repairedApplied && res.repairedText) {
          if (tryPatchAssistantText(event, hookCtx, target, res.repairedText) || forcePatchLastAssistant(event, hookCtx, res.repairedText)) {
            repaired += 1;
          }
        } else if (!res.passed) {
          if (forcePatchLastAssistant(event, hookCtx, policyBlockText(preferred))) {
            repaired += 1;
          }
        }
      } catch {
        // Best effort.
      }
    }
    logInfo(api, `[memq] agent_end refs=${refs.length} audited=${audited} violations=${violations} repaired=${repaired}`);
  };
}
