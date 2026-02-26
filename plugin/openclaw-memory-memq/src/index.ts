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
  const rt: RuntimeState = {
    lastUserBySession: new Map(),
    lastPromptBySession: new Map(),
    lastKeptBySession: new Map(),
  };

  const beforePromptBuild = createBeforePromptBuild(api, sidecar, rt);
  const agentEnd = createAgentEnd(api, sidecar, rt);
  const beforeCompaction = createBeforeCompaction(api, sidecar);
  const gatewayStart = createGatewayStart(api, sidecar);
  const messageSending = createMessageSending(api, sidecar);

  const on = typeof api.on === "function" ? api.on.bind(api) : (typeof api.registerHook === "function" ? api.registerHook.bind(api) : null);
  if (on) {
    on("before_prompt_build", beforePromptBuild);
    on("agent_end", agentEnd);
    on("before_compaction", beforeCompaction);
    on("message_sending", messageSending);
    on("gateway_start", gatewayStart);
  }

  if (typeof api.registerService === "function") {
    api.registerService({
      id: "memq-v2-runtime",
      start: async () => {
        await gatewayStart();
      },
      stop: async () => {},
    });
  }
}
