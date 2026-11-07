/**
 * Proof that the PM CLI actually took a pasted prompt.
 *
 * `tmux paste-buffer` + `send-keys Enter` returning success only proves tmux wrote
 * bytes to the pty. 2026-11-08 00:43 IST a CTO message was pasted this way, MoP
 * answered `verified: true`, and the CLI never recorded it (no UserPromptSubmit,
 * no transcript entry); only a bridge.db read revealed the loss.
 *
 * The CLI appends every prompt it accepts to the session transcript as one of
 *   - a `queue-operation` `enqueue` record (accepted while a turn is running),
 *   - a `user` record (accepted at idle),
 *   - an `attachment` `queued_command` record.
 * Acceptance is: such a record, written AFTER the paste began, whose text is the text
 * we sent. Matching is on the prompt record's own text, never on a substring of some
 * other record (a quoted copy inside a tool result or a model message is not a prompt).
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

export function normalizePrompt(text: string): string {
  return text.replace(/\r\n/g, "\n").trim().replace(PASTE_WRAPPER_OPEN, "").replace(PASTE_WRAPPER_CLOSE, "").trim();
}

export function promptMatchesSent(recordText: string, sentCommand: string): boolean {
  const a = normalizePrompt(recordText);
  const b = normalizePrompt(sentCommand);
  if (b.length === 0) return false;
  if (a === b) return true;
  // Long bodies may be re-wrapped by the CLI; require both ends to match, never a mere substring.
  return b.length > 400 && a.length > 400 && a.startsWith(b.slice(0, 200)) && a.endsWith(b.slice(-100));
}

/** Scan appended transcript text (jsonl) for an accepted prompt equal to `sentCommand`. */
export function chunkContainsAcceptedPrompt(chunk: string, sentCommand: string): boolean {
  // Cheap prefilter on the JSON-escaped form of one distinctive line, before parsing.
  const probeLine = normalizePrompt(sentCommand).split("\n").find((line) => line.trim().length >= 8)?.trim().slice(0, 24) ?? "";
  const probe = probeLine ? JSON.stringify(probeLine).slice(1, -1) : "";
  for (const line of chunk.split("\n")) {
    if (!line || (probe && !line.includes(probe))) continue;
    let record: TranscriptRecord;
    try {
      record = JSON.parse(line) as TranscriptRecord;
    } catch {
      continue;
    }
    if (promptTextsOf(record).some((text) => promptMatchesSent(text, sentCommand))) return true;
  }
  return false;
}

export type AcceptanceDeps = {
  /** Current transcript size; call BEFORE the paste and pass the result as `fromOffset`. */
  size: (path: string) => Promise<number>;
  read: (path: string, from: number, to: number) => Promise<string>;
  sleep: (ms: number) => Promise<void>;
  now: () => number;
};

export const defaultAcceptanceDeps: AcceptanceDeps = {
  size: async (path) => (await stat(path)).size,
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

export async function confirmPmAcceptance(options: {
  transcriptPath: string | null;
  /** Transcript size sampled before the paste. */
  fromOffset: number | null;
  command: string;
  timeoutMs: number;
  pollMs?: number;
  deps?: AcceptanceDeps;
}): Promise<{ accepted: boolean; reason: string }> {
  const deps = options.deps ?? defaultAcceptanceDeps;
  if (!options.transcriptPath || options.fromOffset === null) {
    return { accepted: false, reason: "transcript_unavailable" };
  }
  const deadline = deps.now() + options.timeoutMs;
  let offset = options.fromOffset;
  let carry = "";
  for (;;) {
    try {
      const size = await deps.size(options.transcriptPath);
      if (size > offset) {
        const text = carry + (await deps.read(options.transcriptPath, offset, size));
        offset = size;
        const lastNewline = text.lastIndexOf("\n");
        const complete = lastNewline >= 0 ? text.slice(0, lastNewline + 1) : "";
        carry = lastNewline >= 0 ? text.slice(lastNewline + 1) : text;
        if (chunkContainsAcceptedPrompt(complete, options.command)) return { accepted: true, reason: "transcript_prompt_record" };
      }
    } catch {
      // A transient read error is not evidence either way; keep polling until the deadline.
    }
    if (deps.now() >= deadline) return { accepted: false, reason: "pm_acceptance_unconfirmed" };
    await deps.sleep(options.pollMs ?? 200);
  }
}
