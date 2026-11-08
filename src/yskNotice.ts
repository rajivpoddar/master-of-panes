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
 * Typing 2 quotes the learn-more body as a side-agent block. Only then may
 * we append a blank line and "process this", after a short settle, and press
 * Enter. That append is the only non-empty-composer exception
 * (yskNoticeAppendException). Foreign text never matches.
 *
 * Rule 50: this never gates, holds, or delays PM input except for the brief
 * send lock it shares with every other PM keystroke. Keystrokes are sent
 * only while the notice is visible and the prompt is empty, including while
 * a turn is still running (Rajiv, same thread, ts 1794160185.336839: "it
 * won't work that way. pm turn can keep going for a long time."). The
 * submitted note is queued like any other mid-turn input. The final append
 * is the only non-empty-composer exception, and it runs only after this
 * same attempt verified the exact quoted note. Unknown pane activity sends
 * nothing. The first digit is claimed in the send-custody store before it
 * is typed, and every
 * later key re-checks the pinned pane, process, session and visible stage.
 * The session fence is synchronous after each awaited revalidate/capture and
 * again inside the literal and Enter writers, so a SessionStart during a
 * capture cannot authorize the next key. A consumed or uncertain claim is
 * never typed again, including after restart.
 *
 * submenu_missing with enter=0 is the only retry. The row stays started.
 * A later tick may type 1 again only when the same top note is visible, the
 * PM pane is idle, and the composer is empty. Three misses become terminal
 * `failed` and are logged once. Every abort stores the last 40 pane lines.
 *
 * Choice 2 does not insert the bare note. The 18:09Z composer was exactly
 * "Here is a note offered by a side agent:" plus a blockquote of the learn-more
 * body. Enter is still refused unless the composer is that exact quote and,
 * after the suffix, that quote plus the suffix and nothing else.
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
/** Custody scope for one note. Rows are never the capped config list. */
export function yskNoticeCustodyScope(noteHash: string): string {
  return `ysk-notice:${noteHash}`;
}

/** Recipient pinned before the first digit and rechecked before every later key. */
export type YskPin = {
  paneId: string;
  processId: string;
  sessionId: string;
  /** Idle is required only when retrying a submenu_missing miss. */
  activity: YskPaneActivity;
};
const CHOICES_TAIL_LINES = 6;
const NOTE_LOOKBACK_LINES = 8;
/** Learn-more body can be longer than the one-line summary (18:09Z quote was 8 lines). */
const SUBMENU_BODY_LOOKBACK_LINES = 40;
const MIN_NOTE_CHARS = 12;
export const YSK_ABORT_TAIL_LINES = 40;
export const YSK_SIDE_AGENT_PREFIX = "Here is a note offered by a side agent:\n\n";

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

export type YskPaneActivity = "active" | "idle" | "unknown";

/**
 * A running PM turn is not a deferral. Idle and active both may type, and
 * only when the capture also shows an empty composer. Unknown activity is
 * uncertainty and sends nothing.
 */
export function yskActivityRefusal(activity: YskPaneActivity): string | null {
  if (activity === "idle" || activity === "active") return null;
  return "pm_activity_unknown";
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
  /** Last 40 capture lines. Set on every abort. */
  paneTail?: string;
  /** False suppresses the PM-visible log. Terminal `failed` is logged once. */
  pmLog?: boolean;
};

export type YskNoticeCustody = {
  retryable: boolean;
  terminal: boolean;
  attempts: number;
};

/** Last lines of a capture, persisted with an abort. */
export function yskAbortPaneTail(snapshot: string | null | undefined): string {
  if (!snapshot) return "";
  const lines = snapshot.split("\n");
  return lines.slice(-YSK_ABORT_TAIL_LINES).join("\n");
}

/**
 * Lines of the open Learn-more submenu, from its Heads-up line through the
 * line above the choice cluster. Null when that cluster is not on screen.
 */
export function submenuBodyLines(snapshot: string | null | undefined): string[] | null {
  const frame = composerFrame(snapshot);
  if (!frame) return null;
  const lines = frame.above.split("\n");
  const tailStart = Math.max(0, lines.length - CHOICES_TAIL_LINES);
  for (let i = lines.length - 1; i >= tailStart; i--) {
    if (clusterAt(lines, i) !== "submenu") continue;
    const start = Math.max(0, i - SUBMENU_BODY_LOOKBACK_LINES);
    let head = -1;
    for (let j = i - 1; j >= start; j--) {
      if (HEADS_UP.test(lines[j].trim())) {
        head = j;
        break;
      }
    }
    if (head < 0) return null;
    const body = lines.slice(head, i).map((line) => line.replace(/\s+$/, ""));
    while (body.length > 0 && body[body.length - 1] === "") body.pop();
    return body.length ? body : null;
  }
  return null;
}

/** Exact composer text Choice 2 inserts for a learn-more body. */
export function yskSideAgentQuote(bodyLines: string[]): string | null {
  const lines = bodyLines.map((line) => line.replace(/\s+$/, ""));
  while (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  if (!lines.length) return null;
  const quoted = lines.map((line) => {
    const trimmed = line.trim().replace(/^✦\s*/, "");
    return trimmed ? `> ${trimmed}` : ">";
  }).join("\n");
  return YSK_SIDE_AGENT_PREFIX + quoted;
}

export type YskDriveDeps = {
  capture: (pin: YskPin) => Promise<string | null>;
  sendLiteral: (text: string, pin: YskPin) => Promise<void>;
  pressEnter: (pin: YskPin) => Promise<void>;
  sleep: (ms: number) => Promise<void>;
  /**
   * Custody before the first digit. "consumed" includes started, ambiguous,
   * accepted and queued rows. null means the store cannot be read: send nothing.
   */
  claimNotice: (noteHash: string, destination: string) => "execute" | "consumed" | "terminal" | null;
  /**
   * Read the row before claiming a retry. Absent on callers that only drive
   * a first attempt. A retryable row is submenu_missing, not yet terminal.
   */
  noticeCustody?: (noteHash: string) => YskNoticeCustody | null;
  /**
   * Durable outcome. `retryable` is only submenu_missing with enter=0; the
   * store returns `terminal` on the third miss. Every other typed attempt
   * stays consumed. `extra.paneTail` is the abort capture.
   */
  finishNotice: (
    noteHash: string,
    state: "ambiguous" | "accepted" | "retryable",
    reason: string,
    extra?: { paneTail?: string; enterPresses?: number },
  ) => "terminal" | "retryable" | "ambiguous" | "accepted" | void;
  ready: () => Promise<{ ok: true; pin: YskPin } | { ok: false; reason: string }>;
  /** Same pane id, process and session, still claude, idle or mid-turn, mode allowed. */
  revalidate: (pin: YskPin) => Promise<{ ok: true } | { ok: false; reason: string }>;
  /**
   * Synchronous session fence. Read the latest hook session, do not await.
   * Called after awaited observations and immediately inside the writers.
   * A reason stops the attempt; an already-claimed note stays consumed.
   */
  authority: (pin: YskPin) => string | null;
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
  pin: YskPin,
  timeoutMs: number,
  accept: (snapshot: string | null) => boolean,
): Promise<string | null> {
  const pollMs = deps.pollMs ?? 200;
  const start = Date.now();
  for (;;) {
    let snapshot: string | null;
    try {
      snapshot = await deps.capture(pin);
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

/**
 * One attempt. The caller must already hold the PM send lock so these
 * keystrokes cannot splice into another delivery. This function does not
 * acquire that lock and does not touch hooks.
 */
export async function driveYskNotice(deps: YskDriveDeps): Promise<YskDriveResult> {
  const keysSent: string[] = [];
  let claimedHash: string | null = null;
  let lastSnapshot: string | null = null;
  const captureSeen = async (pin: YskPin): Promise<string | null> => {
    const snapshot = await deps.capture(pin);
    if (typeof snapshot === "string") lastSnapshot = snapshot;
    return snapshot;
  };
  const tracked: YskDriveDeps = { ...deps, capture: captureSeen };
  const finish = (result: YskDriveResult): YskDriveResult => {
    const abort = result.outcome === "abort";
    const paneTail = abort ? yskAbortPaneTail(lastSnapshot) : undefined;
    let logged = abort ? { ...result, paneTail } : result;
    if (claimedHash && result.outcome !== "submitted") {
      const retryable = result.reason === "submenu_missing" && result.enterPresses === 0;
      try {
        const disposition = deps.finishNotice(
          claimedHash,
          retryable ? "retryable" : "ambiguous",
          result.reason,
          { paneTail },
        );
        if (disposition === "terminal") logged = { ...logged, reason: "failed" };
      } catch {
        /* a failed finish leaves the started row, which is still consumed */
      }
    }
    if (logged.pmLog === false) return logged;
    try {
      deps.log(logged);
    } catch {
      /* logging is not a reason to type again */
    }
    return logged;
  };
  try {
    const ready = await deps.ready();
    if (!ready.ok) return finish({ outcome: "noop", reason: ready.reason, keysSent, enterPresses: 0 });
    const pin = ready.pin;

    const first = await captureSeen(pin);
    const parsed = parseYskCapture(first);
    if (parsed.kind === "unreadable") return finish({ outcome: "noop", reason: "composer_unreadable", keysSent, enterPresses: 0 });
    if (parsed.kind === "none") return finish({ outcome: "noop", reason: "no_notice", keysSent, enterPresses: 0 });
    if (parsed.kind === "uncertain") return finish({ outcome: "noop", reason: parsed.reason, keysSent, enterPresses: 0 });
    if (parsed.kind === "draft") return finish({ outcome: "noop", reason: "draft_present", keysSent, enterPresses: 0 });
    if (parsed.kind === "submenu") return finish({ outcome: "noop", reason: "submenu_without_attempt", keysSent, enterPresses: 0 });

    const note = parsed.note;
    const noteHash = yskNoteHash(note);
    const prior = deps.noticeCustody?.(noteHash) ?? null;
    if (prior?.terminal) {
      return finish({ outcome: "noop", reason: "deduped", noteHash, keysSent, enterPresses: 0, pmLog: false });
    }
    // Later tick only. The first attempt may run during a live PM turn.
    if (prior?.retryable && pin.activity !== "idle") {
      return finish({ outcome: "noop", reason: "retry_waits_for_idle", noteHash, keysSent, enterPresses: 0 });
    }
    const sessionFence = (): string | null => deps.authority(pin);
    const blocked = async (expect: (snapshot: string | null) => boolean): Promise<string | null> => {
      const check = await deps.revalidate(pin);
      if (!check.ok) return check.reason;
      // revalidate awaited. A SessionStart can land before the capture starts.
      const afterRevalidate = sessionFence();
      if (afterRevalidate) return afterRevalidate;
      let snapshot: string | null;
      try {
        snapshot = await captureSeen(pin);
      } catch {
        return "capture_failed";
      }
      // The capture is the await a successor session can win. Do not accept
      // that screen, and do not send the key that follows this check.
      const afterCapture = sessionFence();
      if (afterCapture) return afterCapture;
      return expect(snapshot) ? null : "ui_stage_changed";
    };

    const beforeDigit = await blocked((snapshot) => {
      const screen = parseYskCapture(snapshot);
      return screen.kind === "top" && screen.note === note && screen.composer === "";
    });
    if (beforeDigit) return finish({ outcome: "noop", reason: beforeDigit, noteHash, keysSent, enterPresses: 0 });

    const claim = deps.claimNotice(noteHash, `${pin.paneId}|${pin.processId}|${pin.sessionId}`);
    if (claim === null) return finish({ outcome: "noop", reason: "custody_unreadable", noteHash, keysSent, enterPresses: 0 });
    if (claim === "terminal") {
      return finish({ outcome: "noop", reason: "deduped", noteHash, keysSent, enterPresses: 0, pmLog: false });
    }
    if (claim === "consumed") return finish({ outcome: "noop", reason: "deduped", noteHash, keysSent, enterPresses: 0 });
    claimedHash = noteHash;

    const stillTop = await blocked((snapshot) => {
      const screen = parseYskCapture(snapshot);
      return screen.kind === "top" && screen.note === note && screen.composer === "";
    });
    if (stillTop) return finish({ outcome: "abort", reason: stillTop, noteHash, keysSent, enterPresses: 0 });
    await deps.sendLiteral("1", pin);
    keysSent.push("1");
    const submenu = await pollCapture(tracked, pin, deps.submenuTimeoutMs ?? 2000, (snapshot) => {
      const screen = parseYskCapture(snapshot);
      if (screen.kind !== "submenu" || screen.composer !== "") return false;
      const frame = composerFrame(snapshot);
      return !!frame && squashHas(frame.above, note);
    });
    if (!submenu) {
      return finish({ outcome: "abort", reason: "submenu_missing", noteHash, keysSent, enterPresses: 0 });
    }

    const beforeChat = await blocked((snapshot) => {
      const screen = parseYskCapture(snapshot);
      const frame = composerFrame(snapshot);
      return screen.kind === "submenu" && screen.composer === "" && !!frame && squashHas(frame.above, note);
    });
    if (beforeChat) return finish({ outcome: "abort", reason: beforeChat, noteHash, keysSent, enterPresses: 0 });
    const ownedQuote = yskSideAgentQuote(submenuBodyLines(submenu) ?? []);
    if (!ownedQuote) {
      return finish({ outcome: "abort", reason: "quote_unreadable", noteHash, keysSent, enterPresses: 0 });
    }
    await deps.sendLiteral("2", pin);
    keysSent.push("2");
    const quoted = await pollCapture(tracked, pin, deps.quoteTimeoutMs ?? 2000, (snapshot) => {
      return composerOf(snapshot) === ownedQuote;
    });
    if (!quoted) {
      return finish({ outcome: "abort", reason: "quote_not_owned", noteHash, keysSent, enterPresses: 0 });
    }
    const beforeSuffix = await blocked((snapshot) => composerOf(snapshot) === ownedQuote);
    if (beforeSuffix) return finish({ outcome: "abort", reason: beforeSuffix, noteHash, keysSent, enterPresses: 0 });

    await deps.sendLiteral(YSK_NOTICE_PROCESS_SUFFIX, pin);
    keysSent.push(YSK_NOTICE_PROCESS_SUFFIX);
    const timing = deps.submitTiming ?? {};
    const expectedSubmit = ownedQuote + YSK_NOTICE_PROCESS_SUFFIX;
    const submit: SubmitCheckResult = await submitWithComposerCheck(expectedSubmit, {
      capture: () => captureSeen(pin),
      pressSubmit: async () => {
        const beforeEnter = await blocked((snapshot) => composerOf(snapshot) === expectedSubmit);
        if (beforeEnter) throw new Error(beforeEnter);
        await deps.pressEnter(pin);
      },
      sleep: deps.sleep,
      prePasteComposer: ownedQuote,
      noticeAppend: { verifiedQuote: ownedQuote },
      dwellMs: timing.dwellMs ?? 1500,
      payloadStableMs: timing.payloadStableMs ?? 200,
      payloadGraceMs: timing.payloadGraceMs ?? 1200,
      payloadStableGraceMs: timing.payloadGraceMs ?? 1500,
      clearGraceMs: timing.clearGraceMs ?? 1200,
      pollMs: timing.pollMs ?? 200,
      maxEnterPresses: 1,
    });
    if (submit.enterPresses !== 1 || (submit.cleared !== true && submit.queued !== true)) {
      return finish({
        outcome: "abort",
        reason: submit.enterPresses === 1 ? "submit_uncertain" : "suffix_not_confirmed",
        noteHash,
        keysSent,
        enterPresses: submit.enterPresses,
      });
    }
    try {
      deps.finishNotice(noteHash, "accepted", "process_this_notice", { enterPresses: submit.enterPresses });
    } catch {
      /* started/accepted either way: the row stays consumed */
    }
    claimedHash = null;
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
