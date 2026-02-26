import { SidecarClient } from "../lib/sidecar_client.js";
import { logInfo } from "../config/schema.js";

export function createBeforeCompaction(api: any, sidecar: SidecarClient) {
  return async (): Promise<void> => {
    try {
      await sidecar.idleRunOnce({ nowTs: Math.floor(Date.now() / 1000), maxWorkMs: 800 });
      logInfo(api, "[memq-v2] before_compaction idle_run_once=ok");
    } catch {
      // best effort
    }
  };
}
