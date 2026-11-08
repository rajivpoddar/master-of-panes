/**
 * PM-pane "You should know" notices (built-in mod cc-plugin-you-should-know).
 *
 * Rajiv DM D0AMF0XE6TS thread 1794143890.463219, ts 1794144815.513229:
 * "choose 1 (for learn more) and then 2 (chat about this) followed by
 * process this notice <enter>".
 *
 * The note sits just above an empty prompt:
 *   Heads up · <text>
 *   1: Learn more   2: Knew this already   0: Dismiss
 * Typing 1 opens Learn more (Understood / Chat in main session / Dismiss).
 * Typing 2 quotes the note into the prompt. Only then may we append
 * " process this notice" and press Enter. That append is the only
 * non-empty-composer exception (yskNoticeAppendException).
 *
 * Rule 50: this never gates, holds, or delays PM input except for the brief
 * send lock it shares with every other PM keystroke. Keystrokes are sent
 * only while the notice is visible, the pane is idle, and the prompt is
 * empty — except the final append, which runs only after this same attempt
 * verified the quote. Any uncertainty does nothing. A hash of the note is
 * remembered once we send the first digit, so a partial attempt is not retried.
 */

import { createHash } from "node:crypto";
import {
  QUEUED_HINT,
  YSK_NOTICE_PROCESS_SUFFIX,
  composerFrame,
  submitWithComposerCheck,
  type SubmitCheckResult,
} from "./composer.js";

export const YSK_SEEN_CONFIG_KEY = "ysk_notice_seen_v1";
const CHOICES_TAIL_LINES = 6;
const NOTE_LOOKBACK_LINES = 8;
const MIN_NOTE_CHARS = 12;

const TOP_LEARN = /1:\s*Learn more/i;
const TOP_KNEW = /2:\s*Knew this already/i;
const TOP_DISMISS = /0:\s*Dismiss/i;
const SUB_UNDERSTOOD = /1:\s*Understood/i;
const SUB_CHAT = /2:\s*Chat in main session/i;
const HEADS_UP = /Heads up\s*[·•]\s*(.*)$/i;

export function normalizeYskNote(note: string): string {
  return note.replace(/\s+/g, " ").trim();
}

export function yskNoteHash(note: string): string {
  return createHash("sha256").update(normalizeYskNote(note), "utf8").digest("hex");
}

export type YskParse =
  | { kind: "unreadable" }
  | { kind: "none" }
  | { kind: "uncertain"; reason: "queued_input" | "ambiguous" | "note_too_short" }
  | { kind: "draft"; note: string }
  | { kind: "top"; note: string; composer: string }
  | { kind: "submenu"; note: string | null; composer: string };

function clusterAt(lines: string[], index: number): "top" | "submenu" | "ambiguous" | null {
  const window = lines.slice(index, index + 2).join(" ");
  const top = TOP_LEARN.test(window) && TOP_KNEW.test(window) && TOP_DISMISS.test(window);
  const sub = SUB_UNDERSTOOD.test(window) && SUB_CHAT.test(window) && TOP_DISMISS.test(window);
  if (top && sub) return "ambiguous";
  if (sub) return "submenu";
  if (top) return "top";
  return null;
}

function noteBefore(lines: string[], choicesIndex: number): string | null | "ambiguous" {
  const start = Math.max(0, choicesIndex - NOTE_LOOKBACK_LINES);
  const found: string[] = [];
  for (let i = choicesIndex - 1; i >= start; i--) {
    const match = HEADS_UP.exec(lines[i].trim());
    if (!match) continue;
    const rest = lines.slice(i + 1, choicesIndex).map((line) => line.trim()).filter(Boolean);
    found.push(normalizeYskNote([match[1], ...rest].filter(Boolean).join(" ")));
  }
  if (found.length > 1) return "ambiguous";
  return found[0] ?? null;
}

/** Classify one visible PM capture. The notice must sit just above the prompt. */
export function parseYskCapture(snapshot: string | null | undefined): YskParse {
  const frame = composerFrame(snapshot);
  if (!frame) return { kind: "unreadable" };
  if (QUEUED_HINT.test(snapshot ?? "")) return { kind: "uncertain", reason: "queued_input" };
  const lines = frame.above.split("\n");
  const tailStart = Math.max(0, lines.length - CHOICES_TAIL_LINES);
  let found: { kind: "top" | "submenu"; index: number } | null = null;
  for (let i = lines.length - 1; i >= tailStart; i--) {
    const cluster = clusterAt(lines, i);
    if (cluster === "ambiguous") return { kind: "uncertain", reason: "ambiguous" };
    if (cluster) {
      found = { kind: cluster, index: i };
      break;
    }
  }
  if (!found) return { kind: "none" };
  const note = noteBefore(lines, found.index);
  if (note === "ambiguous") return { kind: "uncertain", reason: "ambiguous" };
  if (found.kind === "submenu") {
    return { kind: "submenu", note, composer: frame.composer };
  }
  if (!note) return { kind: "none" };
  if (note.length < MIN_NOTE_CHARS) return { kind: "uncertain", reason: "note_too_short" };
  if (frame.composer !== "") return { kind: "draft", note };
  return { kind: "top", note, composer: "" };
}

export type YskDriveResult = {
  outcome: "noop" | "abort" | "submitted" | "append_not_submitted";
  reason: string;
  noteHash?: string;
  keysSent: string[];
  enterPresses: number;
};

export type YskDriveDeps = {
  capture: () => Promise<string | null>;
  sendLiteral: (text: string) => Promise<void>;
  pressEnter: () => Promise<void>;
  sleep: (ms: number) => Promise<void>;
  /** Null means the dedupe store cannot be read. Fail open: send nothing. */
  loadSeen: () => string[] | null;
  saveSeen: (hashes: string[]) => void;
  ready: () => Promise<{ ok: true } | { ok: false; reason: string }>;
  log: (result: YskDriveResult) => void;
  submenuTimeoutMs?: number;
  quoteTimeoutMs?: number;
  pollMs?: number;
  submitTiming?: {
    dwellMs?: number;
    payloadStableMs?: number;
    payloadGraceMs?: number;
    clearGraceMs?: number;
    pollMs?: number;
  };
};

async function pollCapture(
  deps: YskDriveDeps,
  timeoutMs: number,
  accept: (snapshot: string | null) => boolean,
): Promise<string | null> {
  const pollMs = deps.pollMs ?? 200;
  const start = Date.now();
  for (;;) {
    let snapshot: string | null;
    try {
      snapshot = await deps.capture();
    } catch {
      return null;
    }
    if (accept(snapshot)) return snapshot;
    if (Date.now() - start >= timeoutMs) return null;
    await deps.sleep(pollMs);
  }
}

function composerOf(snapshot: string | null): string | null {
  const frame = composerFrame(snapshot);
  return frame ? frame.composer : null;
}

function remember(deps: YskDriveDeps, seen: string[], hash: string): void {
  if (!seen.includes(hash)) seen.push(hash);
  deps.saveSeen(seen);
}

/**
 * One attempt. The caller must already hold the PM send lock so these
 * keystrokes cannot splice into another delivery. This function does not
 * acquire that lock and does not touch hooks.
 */
export async function driveYskNotice(deps: YskDriveDeps): Promise<YskDriveResult> {
  const keysSent: string[] = [];
  const finish = (result: YskDriveResult): YskDriveResult => {
    try {
      deps.log(result);
    } catch {
      /* logging is not a reason to type again */
    }
    return result;
  };
  try {
    const ready = await deps.ready();
    if (!ready.ok) return finish({ outcome: "noop", reason: ready.reason, keysSent, enterPresses: 0 });
    const seen = deps.loadSeen();
    if (seen === null) return finish({ outcome: "noop", reason: "seen_unreadable", keysSent, enterPresses: 0 });

    const first = await deps.capture();
    const parsed = parseYskCapture(first);
    if (parsed.kind === "unreadable") return finish({ outcome: "noop", reason: "composer_unreadable", keysSent, enterPresses: 0 });
    if (parsed.kind === "none") return finish({ outcome: "noop", reason: "no_notice", keysSent, enterPresses: 0 });
    if (parsed.kind === "uncertain") return finish({ outcome: "noop", reason: parsed.reason, keysSent, enterPresses: 0 });
    if (parsed.kind === "draft") return finish({ outcome: "noop", reason: "draft_present", keysSent, enterPresses: 0 });
    if (parsed.kind === "submenu") return finish({ outcome: "noop", reason: "submenu_without_attempt", keysSent, enterPresses: 0 });

    const note = parsed.note;
    const noteHash = yskNoteHash(note);
    if (seen.includes(noteHash)) {
      return finish({ outcome: "noop", reason: "deduped", noteHash, keysSent, enterPresses: 0 });
    }
    // Remember before the first digit. A lost response must not type "1" again.
    remember(deps, seen, noteHash);

    await deps.sendLiteral("1");
    keysSent.push("1");
    const submenu = await pollCapture(deps, deps.submenuTimeoutMs ?? 2000, (snapshot) => {
      const screen = parseYskCapture(snapshot);
      if (screen.kind !== "submenu" || screen.composer !== "") return false;
      // The Learn-more explanation sits under the note, so the note text must
      // still be on screen, but lines between it and the choices are not the note.
      const frame = composerFrame(snapshot);
      return !!frame && squashHas(frame.above, note);
    });
    if (!submenu) {
      return finish({ outcome: "abort", reason: "submenu_missing", noteHash, keysSent, enterPresses: 0 });
    }

    await deps.sendLiteral("2");
    keysSent.push("2");
    const quoted = await pollCapture(deps, deps.quoteTimeoutMs ?? 2000, (snapshot) => {
      const composer = composerOf(snapshot);
      if (composer === null || composer === "") return false;
      return squashHas(composer, note) && !squashHas(composer, YSK_NOTICE_PROCESS_SUFFIX.trim());
    });
    if (!quoted) {
      return finish({ outcome: "abort", reason: "quote_missing", noteHash, keysSent, enterPresses: 0 });
    }
    const verifiedQuote = composerOf(quoted);
    if (!verifiedQuote) {
      return finish({ outcome: "abort", reason: "quote_missing", noteHash, keysSent, enterPresses: 0 });
    }
    // Re-read immediately. Step 4 may type only onto this exact quote.
    const again = composerOf(await deps.capture());
    if (again !== verifiedQuote) {
      return finish({ outcome: "abort", reason: "quote_changed", noteHash, keysSent, enterPresses: 0 });
    }

    await deps.sendLiteral(YSK_NOTICE_PROCESS_SUFFIX);
    keysSent.push(YSK_NOTICE_PROCESS_SUFFIX);
    const timing = deps.submitTiming ?? {};
    const submit: SubmitCheckResult = await submitWithComposerCheck(verifiedQuote + YSK_NOTICE_PROCESS_SUFFIX, {
      capture: deps.capture,
      pressSubmit: deps.pressEnter,
      sleep: deps.sleep,
      prePasteComposer: verifiedQuote,
      noticeAppend: { verifiedQuote },
      dwellMs: timing.dwellMs ?? 400,
      payloadStableMs: timing.payloadStableMs ?? 200,
      payloadGraceMs: timing.payloadGraceMs ?? 1200,
      payloadStableGraceMs: timing.payloadGraceMs ?? 1500,
      clearGraceMs: timing.clearGraceMs ?? 1200,
      pollMs: timing.pollMs ?? 200,
    });
    if (submit.enterPresses < 1) {
      return finish({
        outcome: "append_not_submitted",
        reason: "suffix_not_confirmed",
        noteHash,
        keysSent,
        enterPresses: 0,
      });
    }
    return finish({
      outcome: "submitted",
      reason: "process_this_notice",
      noteHash,
      keysSent,
      enterPresses: submit.enterPresses,
    });
  } catch {
    return finish({
      outcome: keysSent.length ? "abort" : "noop",
      reason: "uncertain",
      keysSent,
      enterPresses: 0,
    });
  }
}

function squashHas(composer: string, needle: string): boolean {
  const hay = composer.replace(/\s+/g, "");
  const pin = needle.replace(/\s+/g, "");
  return pin.length > 0 && hay.includes(pin);
}
