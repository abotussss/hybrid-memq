import { logInfo } from "../config/schema.js";
import { SidecarClient } from "../lib/sidecar_client.js";

export function createBeforeCompaction(api: any, _sidecar: SidecarClient) {
  return async (): Promise<void> => {
    logInfo(api, "[memq-v3] before_compaction disabled=1");
  };
}
