/**
 * Proof that the PM CLI took the exact prompt a specific paste wrote.
 *
 * `tmux paste-buffer` + `send-keys Enter` returning success only proves tmux wrote bytes to
 * the pty. 2026-11-08 00:43 IST a CTO message was pasted, MoP answered `verified: true`, and
 * the CLI never recorded it. The CLI appends every prompt it accepts to the session transcript
 * as a `queue-operation` `enqueue`, a `user`, or an `attachment` `queued_command` record.
 *
 * Acceptance is such a record that
 *   1. carries EXACTLY the pasted text (after supported structural normalization only:
 *      line endings, outer whitespace, the CLI's `<pasted_content>` wrapper);
 *   2. belongs to THIS paste: it starts at or after the cursor sampled inside the serialized
 *      writer immediately before this paste, before the next paste's cursor, and its own
 *      timestamp is not earlier than the paste;
 *   3. is credited at most once (a record can never satisfy two sends);
 *   4. lives in the same file generation the cursor was taken in (replacement, rotation or
 *      truncation of the transcript is UNKNOWN);
 *   5. is observed while the recipient is still the pinned pane and session.
 * Anything else, including a later manual copy of the text, leaves the send UNKNOWN. Quoted
 * copies in tool results, model text or request blobs are not prompt records and never count.
 */

import { open, stat } from "node:fs/promises";

export type TranscriptRecord = Record<string, unknown>;

function asText(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) {
    return value.flatMap((block) =>
      block && typeof block === "object" && (block as { type?: unknown }).type === "text" &&
      typeof (block as { text?: unknown }).text === "string"
        ? [(block as { text: string }).text]
        : [],
    );
  }
  return [];
}

/** Prompt texts a transcript record represents, or [] when it is not an accepted prompt. */
export function promptTextsOf(record: TranscriptRecord): string[] {
  if (record.type === "queue-operation") {
    return record.operation === "enqueue" ? asText(record.content) : [];
  }
  if (record.type === "user") {
    const message = record.message as { content?: unknown } | undefined;
    return asText(message?.content);
  }
  if (record.type === "attachment") {
    const attachment = record.attachment as { type?: unknown; prompt?: unknown } | undefined;
    return attachment?.type === "queued_command" ? asText(attachment.prompt) : [];
  }
  return [];
}

const PASTE_WRAPPER_OPEN = /^<pasted_content id="[^"]*">\n/;
const PASTE_WRAPPER_CLOSE = /\n<\/pasted_content id="[^"]*">$/;

/** The only normalization applied to either side: line endings, outer whitespace, paste wrapper. */
export function normalizePrompt(text: string): string {
  return text.replace(/\r\n/g, "\n").trim().replace(PASTE_WRAPPER_OPEN, "").replace(PASTE_WRAPPER_CLOSE, "").trim();
}

/** Exact equality after normalization. A prompt that merely shares its ends is a different prompt. */
export function promptMatchesSent(recordText: string, sentText: string): boolean {
  const sent = normalizePrompt(sentText);
  return sent.length > 0 && normalizePrompt(recordText) === sent;
}

export type TranscriptIdentity = { dev: number; ino: number; size: number };

/** What one paste is bound to. Taken inside the serialized writer, per paste. */
export type PasteCursor = {
  path: string;
  dev: number;
  ino: number;
  /** Transcript size (a line boundary) immediately before this paste. */
  size: number;
  startedAtMs: number;
  text: string;
};

export type PasteWindow = {
  cursor: PasteCursor;
  textKey: string;
  /** Start of the NEXT paste into the same transcript, once there is one; this paste's records end before it. */
  upper: number | null;
  settled: boolean;
};

/** Process-local bookkeeping that makes record credit unique and per-paste. */
export class AcceptanceLedger {
  private readonly claimed = new Map<string, number>();
  private readonly last = new Map<string, PasteWindow>();

  constructor(private readonly now: () => number = Date.now) {}

  /** Open this paste's window and close the previous one on the same transcript. */
  open(cursor: PasteCursor): { window: PasteWindow; overlapsIdentical: boolean } {
    const key = `${cursor.dev}:${cursor.ino}:${cursor.path}`;
    const previous = this.last.get(key);
    const textKey = normalizePrompt(cursor.text);
    if (previous && previous.upper === null) previous.upper = cursor.size;
    // The same text pasted again while the earlier identical paste is still awaiting its own
    // record cannot be told apart from it by content: the later one stays UNKNOWN.
    const overlapsIdentical = previous !== undefined && !previous.settled && previous.textKey === textKey;
    const window: PasteWindow = { cursor, textKey, upper: null, settled: false };
    this.last.set(key, window);
    return { window, overlapsIdentical };
  }

  isClaimed(path: string, offset: number): boolean {
    return this.claimed.has(`${path}:${offset}`);
  }

  claim(path: string, offset: number): boolean {
    const now = this.now();
    for (const [claim, at] of this.claimed) if (now - at > 60 * 60 * 1000) this.claimed.delete(claim);
    const key = `${path}:${offset}`;
    if (this.claimed.has(key)) return false;
    this.claimed.set(key, now);
    return true;
  }
}

export type AcceptanceDeps = {
  identity: (path: string) => Promise<TranscriptIdentity>;
  /** Bytes [from, to) decoded as UTF-8. */
  read: (path: string, from: number, to: number) => Promise<string>;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
};

export const defaultAcceptanceDeps: AcceptanceDeps = {
  identity: async (path) => {
    const info = await stat(path);
    return { dev: info.dev, ino: info.ino, size: info.size };
  },
  read: async (path, from, to) => {
    const handle = await open(path, "r");
    try {
      const length = Math.max(0, Math.min(to - from, 16 * 1024 * 1024));
      const buffer = Buffer.alloc(length);
      const { bytesRead } = await handle.read(buffer, 0, length, from);
      return buffer.subarray(0, bytesRead).toString("utf8");
    } finally {
      await handle.close();
    }
  },
  sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now: Date.now,
};

/** Complete jsonl lines in `text` with their absolute byte offsets; a trailing partial line is dropped. */
export function* completeLines(text: string, baseOffset: number): Generator<{ offset: number; line: string; end: number }> {
  let offset = baseOffset;
  let start = 0;
  for (;;) {
    const newline = text.indexOf("\n", start);
    if (newline < 0) return;
    const line = text.slice(start, newline);
    const end = offset + Buffer.byteLength(line, "utf8") + 1;
    yield { offset, line, end };
    offset = end;
    start = newline + 1;
  }
}

/** Offset of the first record in `text` that is exactly the pasted prompt, written at/after the paste. */
export function findAcceptedPrompt(
  text: string,
  baseOffset: number,
  sentText: string,
  notBeforeMs: number,
  isClaimed: (offset: number) => boolean,
): { offset: number; end: number } | null {
  for (const { offset, line, end } of completeLines(text, baseOffset)) {
    if (!line || isClaimed(offset)) continue;
    let record: TranscriptRecord;
    try {
      record = JSON.parse(line) as TranscriptRecord;
    } catch {
      continue;
    }
    if (!promptTextsOf(record).some((candidate) => promptMatchesSent(candidate, sentText))) continue;
    const written = typeof record.timestamp === "string" ? Date.parse(record.timestamp) : Number.NaN;
    // A record without a usable timestamp, or older than the paste, is not this paste's prompt.
    if (!Number.isFinite(written) || written < notBeforeMs) continue;
    return { offset, end };
  }
  return null;
}

/** Clock slack between the CLI's record timestamp and MoP's paste time, same host. */
const TIMESTAMP_SLACK_MS = 1_000;

export async function confirmPmAcceptance(options: {
  ledger: AcceptanceLedger;
  cursor: PasteCursor | null;
  window: PasteWindow | null;
  overlapsIdentical?: boolean;
  /** True while the recipient is still the pane and session the send was pinned to. */
  recipientIntact: () => Promise<boolean>;
  timeoutMs: number;
  pollMs?: number;
  deps?: AcceptanceDeps;
}): Promise<{ accepted: boolean; reason: string }> {
  const deps = options.deps ?? defaultAcceptanceDeps;
  const { cursor, window, ledger } = options;
  if (!cursor || !window) return { accepted: false, reason: "no_paste_cursor" };
  if (options.overlapsIdentical) {
    window.settled = true;
    return { accepted: false, reason: "identical_prompt_in_flight" };
  }
  try {
    const deadline = deps.now() + options.timeoutMs;
    let offset = cursor.size;
    for (;;) {
      try {
        const identity = await deps.identity(cursor.path);
        if (identity.dev !== cursor.dev || identity.ino !== cursor.ino) {
          return { accepted: false, reason: "transcript_replaced" };
        }
        if (identity.size < offset) return { accepted: false, reason: "transcript_truncated" };
        const limit = window.upper === null ? identity.size : Math.min(identity.size, window.upper);
        if (limit > offset) {
          const text = await deps.read(cursor.path, offset, limit);
          const hit = findAcceptedPrompt(
            text,
            offset,
            cursor.text,
            cursor.startedAtMs - TIMESTAMP_SLACK_MS,
            (candidate) => ledger.isClaimed(cursor.path, candidate),
          );
          if (hit) {
            if (!(await options.recipientIntact())) return { accepted: false, reason: "recipient_changed_during_send" };
            if (!ledger.claim(cursor.path, hit.offset)) return { accepted: false, reason: "record_already_credited" };
            return { accepted: true, reason: "transcript_prompt_record" };
          }
          // Advance only past complete lines so a record split across reads is not lost.
          const lastNewline = text.lastIndexOf("\n");
          if (lastNewline >= 0) offset += Buffer.byteLength(text.slice(0, lastNewline + 1), "utf8");
        }
      } catch {
        // A transient read error is not evidence either way; keep polling until the deadline.
      }
      if (deps.now() >= deadline) return { accepted: false, reason: "pm_acceptance_unconfirmed" };
      await deps.sleep(options.pollMs ?? 200);
    }
  } finally {
    window.settled = true;
  }
}
