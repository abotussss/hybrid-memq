import { createAgentEnd } from "./hooks/agent_end.js";
import { createBeforeCompaction } from "./hooks/before_compaction.js";
import { createBeforePromptBuild } from "./hooks/before_prompt_build.js";
import { createGatewayStart } from "./hooks/gateway_start.js";
import { createMessageSending } from "./hooks/message_sending.js";
import { defaults, getCfg } from "./config/schema.js";
import { SidecarClient } from "./lib/sidecar_client.js";
import type { RuntimeState } from "./types.js";

export default function register(api: any): void {
  const sidecar = new SidecarClient(getCfg(api, "memq.sidecarUrl", defaults["memq.sidecarUrl"]));
  const runtime: RuntimeState = {
    lastUserBySession: new Map(),
    lastPromptBySession: new Map(),
    lastMemstyleBySession: new Map(),
    lastSessionSanitizeAtMs: 0,
  };
  const on = typeof api.on === "function"
    ? api.on.bind(api)
    : typeof api.registerHook === "function"
      ? api.registerHook.bind(api)
      : null;
  if (!on) return;
  on("before_prompt_build", createBeforePromptBuild(api, sidecar, runtime));
  on("agent_end", createAgentEnd(api, sidecar, runtime));
  on("before_compaction", createBeforeCompaction(api, sidecar));
  on("message_sending", createMessageSending(api, sidecar, runtime));
  on("gateway_start", createGatewayStart(api, sidecar));
}
