import { defaults, getCfg, logInfo } from "../config/schema.js";
import { SidecarClient } from "../lib/sidecar_client.js";

export function createBeforeCompaction(api: any, sidecar: SidecarClient) {
  return async (): Promise<void> => {
    if (!getCfg(api, "memq.idle.enabled", defaults["memq.idle.enabled"])) return;
    try {
      const result = await sidecar.idleRunOnce({ nowTs: Math.floor(Date.now() / 1000), maxWorkMs: 1200 }, Number(getCfg(api, "memq.brain.timeoutMs", defaults["memq.brain.timeoutMs"])));
      const traceId = String(result?.traceId || "");
      logInfo(api, `[memq][brain-proof] turn=before_compaction trace_id=${traceId} op=merge_plan model=${getCfg(api, "memq.brain.model", defaults["memq.brain.model"])} ps_seen=${result?.psSeen ? 1 : 0}`);
    } catch (error) {
      const brainRequired = String(getCfg(api, "memq.brain.mode", defaults["memq.brain.mode"])).includes("required");
      if (brainRequired) throw error;
      logInfo(api, "[memq-v3] before_compaction degraded=1 reason=idle_failed");
    }
  };
}
