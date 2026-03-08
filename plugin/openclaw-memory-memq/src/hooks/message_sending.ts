import { logInfo } from "../config/schema.js";
import { SidecarClient } from "../lib/sidecar_client.js";
import type { RuntimeState } from "../types.js";

export function createMessageSending(api: any, _sidecar: SidecarClient, _runtime: RuntimeState) {
  return async (_event: any, hookCtx: any): Promise<void> => {
    const sessionKey = String(hookCtx?.sessionKey ?? hookCtx?.sessionId ?? "default");
    logInfo(api, `[memq-v3] message_sending disabled=1 session=${sessionKey}`);
  };
}
