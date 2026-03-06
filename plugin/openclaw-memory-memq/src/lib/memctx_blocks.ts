export function wrapBlock(tag: string, body: string): string {
  const clean = String(body || "").trim();
  if (!clean) return "";
  return `<${tag}>\n${clean}\n</${tag}>`;
}

export function composeInjectedBlocks(memrules: string, memstyle: string, memctx: string): string {
  return [
    wrapBlock("MEMRULES v1", memrules),
    wrapBlock("MEMSTYLE v1", memstyle),
    wrapBlock("MEMCTX v1", memctx),
  ]
    .filter(Boolean)
    .join("\n\n");
}
