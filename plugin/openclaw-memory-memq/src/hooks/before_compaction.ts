import { SidecarClient } from "../services/sidecar.js";
import { logInfo } from "../services/config.js";

export function createBeforeCompaction(api: any, sidecar: SidecarClient) {
  return async (): Promise<void> => {
    logInfo(api, "[memq] before_compaction: rebuilding index");
    await sidecar.rebuild();
  };
}
