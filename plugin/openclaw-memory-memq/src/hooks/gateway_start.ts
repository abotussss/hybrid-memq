import { ingestMarkdownMemory } from "../services/ingest.js";
import { SidecarClient } from "../services/sidecar.js";
import { startBackgroundConsolidation } from "../services/background_consolidation.js";
import { getCfg, logInfo } from "../services/config.js";

export function createGatewayStart(api: any, sidecar: SidecarClient) {
  return async (): Promise<void> => {
    const ok = await sidecar.health();
    if (!ok) {
      logInfo(api, "[memq] sidecar unhealthy: start sidecar on 127.0.0.1:7781");
      return;
    }

    const workspaceRoot = getCfg<string>(api, "memq.workspaceRoot", process.cwd());
    const added = await ingestMarkdownMemory(sidecar, {
      workspaceRoot,
      writeThresholdLow: getCfg<number>(api, "memq.writeGate.low", 0.45),
      writeThresholdHigh: getCfg<number>(api, "memq.writeGate.high", 0.65)
    });

    logInfo(api, `[memq] sidecar healthy ingest_added=${added}`);
    startBackgroundConsolidation(api, sidecar);
  };
}
