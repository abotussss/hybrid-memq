import { defaults, getCfg, logInfo } from "../config/schema.js";
import { SidecarClient } from "../lib/sidecar_client.js";

export function createGatewayStart(api: any, sidecar: SidecarClient) {
  return async (): Promise<void> => {
    const workspaceRoot = String(getCfg(api, "memq.workspaceRoot", defaults["memq.workspaceRoot"]));
    const env = {
      MEMQ_BRAIN_MODE: String(getCfg(api, "memq.brain.mode", defaults["memq.brain.mode"])),
      MEMQ_BRAIN_PROVIDER: String(getCfg(api, "memq.brain.provider", defaults["memq.brain.provider"])),
      MEMQ_BRAIN_BASE_URL: String(getCfg(api, "memq.brain.baseUrl", defaults["memq.brain.baseUrl"])),
      MEMQ_BRAIN_MODEL: String(getCfg(api, "memq.brain.model", defaults["memq.brain.model"])),
      MEMQ_BRAIN_KEEP_ALIVE: String(getCfg(api, "memq.brain.keepAlive", defaults["memq.brain.keepAlive"])),
      MEMQ_BRAIN_TIMEOUT_MS: String(getCfg(api, "memq.brain.timeoutMs", defaults["memq.brain.timeoutMs"])),
    };
    const ok = await sidecar.ensureUp(workspaceRoot, env);
    if (!ok) throw new Error("memq_sidecar_start_failed");
    await sidecar.bootstrapImportMd(workspaceRoot);
    logInfo(api, `[memq-v3] gateway_start sidecar_ready=1 brain_model=${env.MEMQ_BRAIN_MODEL}`);
  };
}
