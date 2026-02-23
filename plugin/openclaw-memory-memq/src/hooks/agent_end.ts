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
  const bag = [event, hookCtx, event?.result, hookCtx?.result, event?.output, hookCtx?.output];
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
  }
  return out;
}

export function createAgentEnd(api: any, sidecar: SidecarClient, surface: SurfaceCache, rt: RuntimeState) {
  return async (event: any, hookCtx: any): Promise<void> => {
    const sessionId = hookCtx?.sessionKey ?? hookCtx?.sessionId ?? "default";
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
    const assistantTexts = collectAssistantTexts(event, hookCtx);
    let audited = 0;
    let violations = 0;
    for (const text of assistantTexts.slice(-2)) {
      try {
        const res = await sidecar.auditOutput({
          sessionId,
          text,
          allowedLanguages: allowed
        });
        audited += 1;
        if (!res.passed) violations += 1;
      } catch {
        // Best effort.
      }
    }
    logInfo(api, `[memq] agent_end refs=${refs.length} audited=${audited} violations=${violations}`);
  };
}
