export type SlackEventOrigin = {
  user?: string;
  bot_id?: string;
  subtype?: string;
};

/**
 * Return the origin-level reason an ingress event must be ignored.
 *
 * Bot-authored messages are valid CTO ingress. Slack may represent them with
 * `bot_id`, the legacy `bot_message` subtype, or both. A human `file_share`
 * subtype is also a real user message; other subtypes describe lifecycle or
 * system events and must not create fresh CTO wakes.
 */
export function eventOriginIgnoreReason(
  event: SlackEventOrigin,
  appUserId?: string | null,
  contextBotUserId?: string,
): "unsupported-subtype" | "app-self" | null {
  if (event.subtype && event.subtype !== "bot_message" && event.subtype !== "file_share") {
    return "unsupported-subtype";
  }
  if (event.subtype === "file_share" && event.bot_id) return "unsupported-subtype";
  if (event.user && (event.user === appUserId || event.user === contextBotUserId)) {
    return "app-self";
  }
  return null;
}
