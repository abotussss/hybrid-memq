import { SidecarClient } from "../lib/sidecar_client.js";
import { defaults, getCfg, logInfo } from "../config/schema.js";

export function createGatewayStart(api: any, sidecar: SidecarClient) {
  return async (): Promise<void> => {
    const workspaceRoot = getCfg(api, "memq.workspaceRoot", defaults["memq.workspaceRoot"]);
    let healthy = await sidecar.health();
    if (!healthy) {
      healthy = await sidecar.ensureUp(workspaceRoot);
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
