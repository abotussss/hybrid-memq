export type Intent = "preference" | "constraint" | "plan" | "identity" | "general";

export function inferIntent(text: string): Intent {
  const s = text.toLowerCase();
  if (/(must|don't|do not|禁止|必須|してはいけない)/.test(s)) return "constraint";
  if (/(plan|deadline|schedule|todo|計画|期限)/.test(s)) return "plan";
  if (/(i am|my name|私は|自分は)/.test(s)) return "identity";
  if (/(prefer|like|tone|style|口調|好み)/.test(s)) return "preference";
  return "general";
}

export function requiredSlots(intent: Intent): string[] {
  if (intent === "preference") return ["tone", "formatting", "avoidance_rules"];
  if (intent === "constraint") return ["do", "dont", "safety", "budget"];
  if (intent === "plan") return ["goal", "deadline", "owner", "status"];
  if (intent === "identity") return ["name", "role", "stable_prefs"];
  return ["goal", "constraints"];
}
