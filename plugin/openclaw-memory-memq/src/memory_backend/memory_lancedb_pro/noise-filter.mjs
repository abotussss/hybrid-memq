const DENIAL_PATTERNS = [
  /i don'?t have (any )?(information|data|memory|record)/i,
  /i'?m not sure about/i,
  /i don'?t recall/i,
  /i don'?t remember/i,
  /no (relevant )?memories found/i,
];

const META_QUESTION_PATTERNS = [
  /\bdo you (remember|recall|know about)\b/i,
  /\bcan you (remember|recall)\b/i,
  /\bdid i (tell|mention|say|share)\b/i,
  /\bwhat did i (tell|say|mention)\b/i,
];

const BOILERPLATE_PATTERNS = [
  /budget_tokens=/i,
  /<memrules/i,
  /<memstyle/i,
  /<memctx/i,
  /^fresh session/i,
  /^new session/i,
  /^heartbeat/i,
];

export function isNoise(text) {
  const trimmed = String(text || "").trim();
  if (trimmed.length < 5) return true;
  if (DENIAL_PATTERNS.some((p) => p.test(trimmed))) return true;
  if (META_QUESTION_PATTERNS.some((p) => p.test(trimmed))) return true;
  if (BOILERPLATE_PATTERNS.some((p) => p.test(trimmed))) return true;
  return false;
}

export function filterNoise(items, getText) {
  return items.filter((item) => !isNoise(getText(item)));
}
