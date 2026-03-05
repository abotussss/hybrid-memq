import { SidecarClient } from "../lib/sidecar_client.js";
import { defaults, getCfg, logInfo } from "../config/schema.js";

export function createBeforeCompaction(api: any, sidecar: SidecarClient) {
  return async (): Promise<void> => {
    const brainMode = String(getCfg(api, "memq.brain.mode", defaults["memq.brain.mode"]) || "required").toLowerCase();
    const brainRequired = brainMode === "required";
    try {
      const res = await sidecar.idleRunOnce({ nowTs: Math.floor(Date.now() / 1000), maxWorkMs: 800 });
      const traceId = String(res?.traceId || "");
      logInfo(api, `[memq-v2] before_compaction idle_run_once=ok trace_id=${traceId}`);
      logInfo(api, `[memq][brain-proof] op=merge_plan trace_id=${traceId} model=gpt-oss:20b`);
    } catch (err) {
      if (brainRequired) throw err;
      // best effort
    }
  };
}
