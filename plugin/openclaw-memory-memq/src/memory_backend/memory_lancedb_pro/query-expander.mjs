const MAX_EXPANSION_TERMS = 6;

const SYNONYM_MAP = [
  {
    ja: ["記憶", "会話", "前に話した", "前に言った", "覚えて"],
    en: ["memory", "remember", "recall", "previously", "earlier"],
    expansions: ["記憶", "会話", "前回", "以前", "memory", "recall", "context"],
  },
  {
    ja: ["昨日", "最近", "前回", "さっき"],
    en: ["yesterday", "recently", "last time", "earlier"],
    expansions: ["昨日", "最近", "前回", "timeline", "history", "event"],
  },
  {
    ja: ["名前", "呼び方", "口調", "人格", "スタイル"],
    en: ["name", "nickname", "tone", "persona", "style"],
    expansions: ["名前", "呼び方", "口調", "人格", "スタイル", "profile", "identity"],
  },
  {
    ja: ["ルール", "禁止", "秘密", "トークン", "apiキー"],
    en: ["rule", "policy", "secret", "token", "api key"],
    expansions: ["ルール", "ポリシー", "秘密", "トークン", "APIキー", "security", "policy"],
  },
  {
    ja: ["テレビ", "tv", "grp", "視聴", "広告"],
    en: ["tv", "grp", "reach", "frequency", "adstock"],
    expansions: ["テレビ", "TV", "GRP", "視聴", "reach", "frequency", "adstock", "MMM", "DLM"],
  },
];

function escapeRegex(text) {
  return String(text || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function hasEnglishTrigger(query, term) {
  return new RegExp(`\\b${escapeRegex(term)}\\b`, "i").test(query);
}

export function expandQuery(query) {
  const raw = String(query || "").trim();
  if (raw.length < 2) return raw;
  const lower = raw.toLowerCase();
  const additions = new Set();

  for (const entry of SYNONYM_MAP) {
    const jaMatch = (entry.ja || []).some((token) => lower.includes(String(token).toLowerCase()));
    const enMatch = (entry.en || []).some((token) => hasEnglishTrigger(raw, token));
    if (!jaMatch && !enMatch) continue;
    for (const extra of entry.expansions || []) {
      if (!lower.includes(String(extra).toLowerCase())) additions.add(String(extra));
      if (additions.size >= MAX_EXPANSION_TERMS) break;
    }
    if (additions.size >= MAX_EXPANSION_TERMS) break;
  }

  if (!additions.size) return raw;
  return `${raw} ${[...additions].join(" ")}`.trim();
}
