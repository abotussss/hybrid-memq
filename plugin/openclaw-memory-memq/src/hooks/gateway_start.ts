import { SidecarClient } from "../lib/sidecar_client.js";
import { defaults, getCfg, logInfo } from "../config/schema.js";

export function createGatewayStart(api: any, sidecar: SidecarClient) {
  return async (): Promise<void> => {
    const workspaceRoot = getCfg(api, "memq.workspaceRoot", defaults["memq.workspaceRoot"]);
    const brainMode = String(getCfg(api, "memq.brain.mode", defaults["memq.brain.mode"]) || "best_effort").toLowerCase();
    const brainProvider = String(getCfg(api, "memq.brain.provider", defaults["memq.brain.provider"]));
    const brainBaseUrl = String(getCfg(api, "memq.brain.baseUrl", defaults["memq.brain.baseUrl"]));
    const brainModel = String(getCfg(api, "memq.brain.model", defaults["memq.brain.model"]));
    const brainKeepAlive = String(getCfg(api, "memq.brain.keepAlive", defaults["memq.brain.keepAlive"]));
    const brainTimeoutMs = Number(getCfg(api, "memq.brain.timeoutMs", defaults["memq.brain.timeoutMs"]));
    const brainMaxTokens = Number(getCfg(api, "memq.brain.maxTokens", defaults["memq.brain.maxTokens"]));
    const brainAutoRestart = getCfg(api, "memq.brain.autoRestart", defaults["memq.brain.autoRestart"]);
    const brainRestartCooldownSec = Number(
      getCfg(api, "memq.brain.restartCooldownSec", defaults["memq.brain.restartCooldownSec"])
    );
    const brainRestartWaitMs = Number(getCfg(api, "memq.brain.restartWaitMs", defaults["memq.brain.restartWaitMs"]));
    logInfo(api, `[memq-v2] gateway_start workspace_root=${workspaceRoot}`);
    let healthy = await sidecar.health();
    if (!healthy) {
      healthy = await sidecar.ensureUp(workspaceRoot, {
        brainMode,
        brainProvider,
        brainBaseUrl,
        brainModel,
        brainKeepAlive,
        brainTimeoutMs,
        brainMaxTokens,
        brainAutoRestart,
        brainRestartCooldownSec,
        brainRestartWaitMs,
      });
    }
    if (!healthy) {
      logInfo(api, "[memq-v2] gateway_start sidecar_unhealthy");
      return;
    }
    try {
      await sidecar.bootstrapImportMd(workspaceRoot);
      logInfo(api, "[memq-v2] gateway_start bootstrap_import_md=ok");
    } catch {
      logInfo(api, "[memq-v2] gateway_start bootstrap_import_md=skip");
    }
  };
}
