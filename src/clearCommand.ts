/**
 * Direct pane sends must never be an alternate session-clear authority.
 * Recognize command-shaped clear/reset forms, including legacy separator and
 * whitespace variants, while leaving ordinary prose untouched.
 */
export function isClearBearingCommand(command: string): boolean {
  const normalized = command.trim().toLowerCase().replace(/^[\\/\s]+/, "");
  const rawTokens = normalized.split(/\s+/).filter(Boolean);
  const tokens = rawTokens
    .slice(0, 2)
    .map((token) => token.replace(/[\\/._:-]/g, ""))
    .filter(Boolean);
  const first = tokens[0] ?? "";
  const joined = tokens.join("");
  const aliases = new Set(["clear", "reset", "sessionclear", "clearsession", "sessionreset", "resetsession"]);
  const onlyOptions = (from: number): boolean => rawTokens.slice(from).every((token) => token.startsWith("-"));
  return aliases.has(first) && onlyOptions(1)
    || aliases.has(joined) && rawTokens.length >= 2 && onlyOptions(2);
}
