export const SUPERPROOFER_CHANNEL_ID = "C09TYQC1DEF";
export const HEYDONNA_DEV_CHANNEL_ID = "C0ALZJHGE49";
export const HEYDONNA_CI_BOT_USER_ID = "U0AJZTN7SM6";
export const HEYDONNA_CI_BOT_ID = "B0AJ3HSC2PQ";

export type SlackIngressCandidate = {
  channel?: string;
  channel_type?: string;
  thread_ts?: string;
  user?: string;
  bot_id?: string;
  text?: string;
  blocks?: unknown[];
};

export type SlackIngressEventType =
  | "message.im"
  | "message.group"
  | "message.channel"
  | "app_mention";

const CTO_USER_ID = "U0BNFGX2UAX";
const CTO_MENTION = new RegExp(`<@${CTO_USER_ID}(?:\\|[^>\\r\\n]*)?>`);

function renderedTextHasCtoMention(text: string): boolean {
  // Slack block quotes and code spans are rendered context, not a current
  // author mention. Ignore them so quoted history or pasted examples cannot
  // authorize a wake.
  const withoutQuotedLines = text
    .split(/\r?\n/)
    .filter((line) => !/^\s*>/.test(line))
    .join("\n");
  const withoutCode = withoutQuotedLines
    .replace(/```[\s\S]*?```/g, "")
    .replace(/`[^`\r\n]*`/g, "");
  return CTO_MENTION.test(withoutCode);
}

function blockHasCtoMention(block: unknown, quoted = false): boolean {
  if (!block || typeof block !== "object") return false;
  const record = block as Record<string, unknown>;
  const type = typeof record.type === "string" ? record.type : "";
  const style = record.style && typeof record.style === "object"
    ? record.style as Record<string, unknown>
    : null;
  if (
    quoted ||
    type === "rich_text_quote" ||
    type === "blockquote" ||
    type === "rich_text_preformatted" ||
    style?.code === true
  ) return false;
  if (type === "user" && record.user_id === CTO_USER_ID) return true;
  if (record.text && typeof record.text === "object" && blockHasCtoMention(record.text, false)) return true;
  // Only Slack's rendered mrkdwn surface can carry mention markup. Plain
  // rich-text/text elements are literal content and must not authorize ingress.
  if (type === "mrkdwn" && typeof record.text === "string" && renderedTextHasCtoMention(record.text)) {
    return true;
  }
  if (Array.isArray(record.elements)) {
    return record.elements.some((element) => blockHasCtoMention(element, false));
  }
  return false;
}

export function hasExplicitCtoMention(event: SlackIngressCandidate): boolean {
  if (typeof event.text === "string" && renderedTextHasCtoMention(event.text)) return true;
  return Array.isArray(event.blocks) && event.blocks.some((block) => blockHasCtoMention(block));
}

/**
 * Decide only whether the Socket Mode event belongs in the durable relay.
 * This is subscription policy, not final task routing.
 */
export function slackIngressEventType(
  event: SlackIngressCandidate,
  source: "message" | "app_mention",
): SlackIngressEventType | null {
  // #heydonna-dev is CTO Decisions mention-only. Verified CI/webhook sender
  // identity does not grant an exception; the current message must mention CTO.
  if (event.channel === HEYDONNA_DEV_CHANNEL_ID && !hasExplicitCtoMention(event)) return null;
  // Superproofer wakes its existing project task only for a current CTO mention,
  // including replies in threads whose earlier messages mentioned CTO.
  if (event.channel === SUPERPROOFER_CHANNEL_ID && !hasExplicitCtoMention(event)) return null;
  if (source === "app_mention") return "app_mention";
  if (event.channel === SUPERPROOFER_CHANNEL_ID) return "message.group";
  if (event.channel_type === "im") return "message.im";
  // Every otherwise-unmatched message visible to the CTO app enters the
  // durable router. Its default route is CTO Decisions; explicit channel/DM
  // routes still win. Origin policy separately excludes self/system events.
  if (event.channel) {
    return event.channel_type === "group" ? "message.group" : "message.channel";
  }
  return null;
}
