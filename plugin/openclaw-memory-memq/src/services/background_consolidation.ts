import { SidecarClient } from "./sidecar.js";
import { getCfg, logInfo } from "./config.js";

export function startBackgroundConsolidation(api: any, sidecar: SidecarClient): () => void {
  const hour = getCfg<number>(api, "memq.consolidation.intervalSec", 3600);
  const timer = setInterval(async () => {
    try {
      await sidecar.consolidate(Math.floor(Date.now() / 1000));
      logInfo(api, "[memq] background consolidation complete");
    } catch (err) {
      logInfo(api, `[memq] consolidation error: ${(err as Error).message}`);
    }
  }, Math.max(30_000, hour * 1000));

  return () => clearInterval(timer);
}
