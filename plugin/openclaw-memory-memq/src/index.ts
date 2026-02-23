import type { RuntimeState } from "./types.js";
import { createAgentEnd } from "./hooks/agent_end.js";
import { createBeforeCompaction } from "./hooks/before_compaction.js";
import { createBeforePromptBuild } from "./hooks/before_prompt_build.js";
import { createGatewayStart } from "./hooks/gateway_start.js";
import { SidecarClient } from "./services/sidecar.js";
import { RuntimeMetrics, SurfaceCache } from "./services/state.js";
import { getCfg, logInfo } from "./services/config.js";

export default function register(api: any): void {
  const sidecar = new SidecarClient(getCfg(api, "memq.sidecarUrl", "http://127.0.0.1:7781"));
  const surface = new SurfaceCache(getCfg(api, "memq.surface.max", 120));
  const metrics = new RuntimeMetrics();
  const rt: RuntimeState = {
    lastCandidatesBySession: new Map(),
    lastAllowedLanguagesBySession: new Map(),
    lastPreferredLanguageBySession: new Map(),
    lastAuditBypassBySession: new Map(),
    lastStyleProfileBySession: new Map()
  };

  const before = createBeforePromptBuild(api, sidecar, surface, rt, metrics);
  const onEnd = createAgentEnd(api, sidecar, surface, rt);
  const onCompaction = createBeforeCompaction(api, sidecar);
  const onStart = createGatewayStart(api, sidecar);

  // Prefer modern lifecycle API (`api.on`) and keep compatibility.
  if (typeof api.on === "function") {
    api.on("before_prompt_build", before);
    // Compatibility for runtimes that expose only before_agent_start.
    api.on("before_agent_start", before);
    api.on("agent_end", onEnd);
    api.on("before_compaction", onCompaction);
  } else if (typeof api.registerHook === "function") {
    api.registerHook("before_prompt_build", before);
    api.registerHook("agent_end", onEnd);
    api.registerHook("before_compaction", onCompaction);
  }

  if (typeof api.registerService === "function") {
    api.registerService({
      id: "memq-runtime",
      start: async () => {
        await onStart();
      },
      stop: async () => {}
    });
  }

  const metricsHook = async () => {
    const s = metrics.summary();
    logInfo(
      api,
      `[memq] metrics turns=${s.turns} deep_call_rate=${s.deepCallRate.toFixed(2)} surface_hit_rate=${s.surfaceHitRate.toFixed(2)} fallback_rate=${s.fallbackRate.toFixed(2)}`
    );
  };

  if (typeof api.on === "function") {
    api.on("agent_end", metricsHook);
  } else if (typeof api.registerHook === "function") {
    api.registerHook("agent_end", metricsHook);
  }
}
