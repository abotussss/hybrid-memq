const SKIP_PATTERNS = [
  /^(hi|hello|hey|good\s*(morning|afternoon|evening|night)|greetings|yo|sup)\b/i,
  /^\//,
  /^(run|build|test|ls|cd|git|npm|pnpm|pip|docker|curl|cat|grep|find|make|sudo)\b/i,
  /^(yes|no|ok|okay|sure|fine|thanks|thank you|thx|got it|understood|cool|nice|great|good|perfect)\s*[.!]?$/i,
  /^(go ahead|continue|proceed|do it|start|begin|next|好的|可以|继续|繼續)\s*[.!]?$/i,
  /^[\p{Emoji}\s]+$/u,
  /heartbeat/i,
  /^\[system/i,
  /^(ping|pong|test|debug)\s*[.!?]?$/i,
];

const FORCE_RETRIEVE_PATTERNS = [
  /\b(remember|recall|forgot|memory|memories)\b/i,
  /\b(last time|before|previously|earlier|yesterday|ago)\b/i,
  /\b(my (name|email|phone|address|birthday|preference))\b/i,
  /\b(what did (i|we)|did i (tell|say|mention))\b/i,
  /(你记得|[你妳]記得|之前|上次|以前|还记得|還記得|提到过|提到過|说过|說過)/i,
];

export function normalizeQuery(query) {
  let s = String(query || "").trim();
  const metadataPattern = /^(Conversation info|Sender) \(untrusted metadata\):[\s\S]*?\n\s*\n/gim;
  s = s.replace(metadataPattern, "");
  s = s.trim().replace(/^\[cron:[^\]]+\]\s*/i, "");
  s = s.trim().replace(/^\[[A-Za-z]{3}\s\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}\s[^\]]+\]\s*/, "");
  return s.trim();
}

export function shouldSkipRetrieval(query, minLength) {
  const trimmed = normalizeQuery(query);
  if (FORCE_RETRIEVE_PATTERNS.some((p) => p.test(trimmed))) return false;
  if (trimmed.length < 5) return true;
  if (SKIP_PATTERNS.some((p) => p.test(trimmed))) return true;
  if (minLength !== undefined && minLength > 0) {
    if (trimmed.length < minLength && !trimmed.includes("?") && !trimmed.includes("？")) return true;
    return false;
  }
  const hasCJK = /[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]/.test(trimmed);
  const defaultMinLength = hasCJK ? 6 : 15;
  if (trimmed.length < defaultMinLength && !trimmed.includes("?") && !trimmed.includes("？")) return true;
  return false;
}
