// Shared paste -> dwell -> Enter timing and composer inspection for every
// MoP tmux text delivery (PM pane and numbered slots).
//
// Rajiv 2026-09-25: prompts were left buffered in the Claude Code composer
// because Enter arrived before the TUI finished ingesting the paste. One
// shared dwell (default 1000 ms) now precedes every submit key, and delivery
// is only verified once the composer input line is empty again.

function parseDelay(raw: string | undefined): number | null {
  if (raw === undefined || raw.trim() === "") return null;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

export function resolveInjectEnterDelayMs(primary?: string, legacy?: string): number {
  const configured = parseDelay(primary) ?? parseDelay(legacy) ?? 1000;
  return Math.max(1000, configured);
}

export const INJECT_ENTER_DELAY_MS: number = resolveInjectEnterDelayMs(
  process.env.MOP_INJECT_ENTER_DELAY_MS,
  process.env.MOP_PM_INJECT_ENTER_DELAY_MS,
);

/** Assignment handoffs need one extra second for Claude to consume a pasted
 * composer before its single submit key. Keep larger operator dwell settings. */
export function resolveAssignmentInjectEnterDelayMs(configured = INJECT_ENTER_DELAY_MS): number {
  return Math.max(2000, configured);
}

const RULE_LINE = /^\s*─{20,}\s*$/;
/** Claude Code hints shown while prompts sit in the queued-message area. */
const QUEUED_INPUT_PLACEHOLDER = /^press\s+up\s+to\s+edit\s+queued\s+messages$/i;
export const QUEUED_HINT = /press\s+up\s+to\s+edit\s+queued\s+messages|ctrl\+x\s+ctrl\+s\s+to\s+send\s+now/i;

/**
 * Return the text currently sitting in the Claude Code composer, "" when the
 * input line is empty, or null when no composer box can be recognised (other
 * runtimes, dialogs, unexpected layouts). Callers treat null as "cannot
 * judge" and keep their previous verification behaviour.
 */
export function composerText(snapshot: string | null | undefined): string | null {
  if (!snapshot) return null;
  const lines = snapshot.replace(/\s+$/, "").split("\n");
  for (let bottom = lines.length - 1; bottom > 0; bottom--) {
    if (!RULE_LINE.test(lines[bottom])) continue;
    let top = bottom - 1;
    while (top >= 0 && !RULE_LINE.test(lines[top])) top--;
    if (top < 0) return null;
    const body = lines.slice(top + 1, bottom);
    if (body.length === 0 || !/^\s*❯/.test(body[0])) {
      bottom = top + 1;
      continue;
    }
    const text = body
      .map((line, index) => (index === 0 ? line.replace(/^\s*❯/, "") : line))
      .join("\n")
      .trim();
    // While a busy turn holds queued messages (rendered above the box),
    // Claude shows this placeholder on an otherwise EMPTY input line.
    return QUEUED_INPUT_PLACEHOLDER.test(text) ? "" : text;
  }
  return null;
}

function squash(text: string): string {
  return text.replace(/\s+/g, "");
}

/**
 * True only when the full payload is visible in the composer. Claude's
 * `[Pasted text ...]` placeholder hides its contents and cannot prove that a
 * truncated paste contains the expected head, middle, and tail.
 */
export function composerHoldsPayload(composer: string, payload: string): boolean {
  if (/\[Pasted text/i.test(composer)) return false;
  const expected = squash(payload.trim());
  if (!expected) return composer.trim() === "";
  return squash(composer).includes(expected);
}

const MIN_REFUSED_PASTE_PREFIX_LENGTH = 24;

/**
 * True only when the visible composer is attributable to this refused paste:
 * the pre-paste composer was empty, and the post-paste text is either a
 * collapsed paste placeholder (with any visible tail matching this payload)
 * or a sufficiently long visible prefix of this payload. This is cleanup
 * ownership, not proof that the whole payload arrived.
 */
export function composerIsRefusedPasteContent(composer: string, payload: string): boolean {
  const expected = squash(payload.trim());
  if (!expected || !composer.trim()) return false;

  const placeholder = composer.trim().match(
    /^\[Pasted text(?:\s+#\d+)?(?:\s+\+\d+\s+lines?)?\](?:\n([\s\S]*))?$/i,
  );
  if (placeholder) {
    const visibleTail = squash(placeholder[1] ?? "");
    return visibleTail === "" || expected.endsWith(visibleTail);
  }

  const visible = squash(composer.trim());
  return visible.length >= MIN_REFUSED_PASTE_PREFIX_LENGTH && expected.startsWith(visible);
}

const PLACEHOLDER_RE = /\[Pasted text(?:\s+#(\d+))?(?:\s+\+\d+\s+lines?)?\]/gi;

/** Highest `[Pasted text #N]` number in the composer, 0 when none. */
export function maxPastePlaceholderNumber(composer: string | null | undefined): number {
  let max = 0;
  for (const m of (composer ?? "").matchAll(PLACEHOLDER_RE)) {
    max = Math.max(max, m[1] ? Number(m[1]) : 1);
  }
  return max;
}

/** True when the composer holds only collapsed-paste placeholders (stale MoP pastes). */
export function composerIsOnlyPastePlaceholders(composer: string | null | undefined): boolean {
  if (!composer || !composer.trim()) return false;
  return composer.replace(PLACEHOLDER_RE, "").trim() === "";
}

/**
 * Claude Code collapses large pastes into `[Pasted text #N ...]`. A placeholder
 * whose N exceeds every placeholder present before the paste proves that this
 * paste landed in the composer.
 */
export function composerShowsNewPastePlaceholder(composer: string, prePasteComposer: string | null | undefined): boolean {
  const current = maxPastePlaceholderNumber(composer);
  return current > 0 && current > maxPastePlaceholderNumber(prePasteComposer);
}

export type EmptyComposerWaitDeps = {
  capture: () => Promise<string | null>;
  sleep: (ms: number) => Promise<void>;
  stableMs?: number;
  timeoutMs?: number;
  pollMs?: number;
};

export type EmptyComposerWaitResult =
  | { ready: true; snapshot: string; waitedMs: number }
  | { ready: false; snapshot: string | null; waitedMs: number };

/**
 * Wait for a readable empty Claude composer to remain empty before a paste.
 * A cleared input line alone is not enough immediately after `/clear`: the
 * new TUI can still be settling, and a paste during that transition can lose
 * its head. On timeout, callers must leave the pane untouched.
 */
export async function waitForEmptyComposer(deps: EmptyComposerWaitDeps): Promise<EmptyComposerWaitResult> {
  const stableMs = deps.stableMs ?? INJECT_ENTER_DELAY_MS;
  const timeoutMs = deps.timeoutMs ?? 3000;
  const pollMs = deps.pollMs ?? 250;
  let waitedMs = 0;
  let stableForMs = 0;
  let lastComposer: string | null = null;
  let lastSnapshot: string | null = null;

  for (;;) {
    const snapshot = await deps.capture();
    lastSnapshot = snapshot;
    const composer = composerText(snapshot);
    if (composer === "") {
      stableForMs = lastComposer === "" ? stableForMs + pollMs : 0;
      lastComposer = "";
      if (stableForMs >= stableMs && snapshot !== null) {
        return { ready: true, snapshot, waitedMs };
      }
    } else {
      lastComposer = composer;
      stableForMs = 0;
    }

    if (waitedMs >= timeoutMs) {
      return { ready: false, snapshot: lastSnapshot, waitedMs };
    }
    const sleepMs = Math.min(pollMs, timeoutMs - waitedMs);
    await deps.sleep(sleepMs);
    waitedMs += sleepMs;
  }
}

export type SubmitCheckResult = {
  /** true/false once the composer was readable before Enter; null if unrecognised. */
  payloadSeen: boolean | null;
  /** True only after the complete payload stays unchanged for the readiness interval. */
  payloadStable: boolean | null;
  /** true/false once the composer was readable after Enter; null if unrecognised. */
  cleared: boolean | null;
  /** Present only when Claude confirms this still-visible prompt is queued. */
  queued?: true;
  enterPresses: number;
};

export type RefusedPasteCleanupResult = {
  attempted: boolean;
  cleared: boolean;
  reason:
    | "pre_paste_composer_not_empty"
    | "enter_already_sent"
    | "composer_unreadable"
    | "composer_already_empty"
    | "composer_changed_after_refusal"
    | "composer_not_owned_by_send"
    | "pane_mode_refused"
    | "clear_key_outcome_unknown"
    | "composer_cleared"
    | "composer_empty_not_confirmed";
};

export type RefusedPasteCleanupDeps = {
  /** The composer contents captured immediately before this send pasted. */
  prePasteComposer: string | null;
  /** Last readable composer contents seen by submit verification. */
  lastObservedComposer: string | null;
  enterPresses: number;
  capture: () => Promise<string | null>;
  /** Return false when the pane mode changed and C-u must not be sent. */
  clearComposer: () => Promise<boolean>;
  sleep: (ms: number) => Promise<void>;
  stableMs?: number;
  timeoutMs?: number;
  pollMs?: number;
};

/**
 * Recover a refused paste only when the send began from a readable empty
 * composer, no Enter was sent, and a fresh read still matches the refused
 * paste observed by verification. C-u is sent once; the payload is never
 * resubmitted and a changed/unreadable composer is left untouched.
 */
export async function clearOwnedRefusedPasteComposer(
  payload: string,
  deps: RefusedPasteCleanupDeps,
): Promise<RefusedPasteCleanupResult> {
  if (deps.prePasteComposer !== "" && !composerIsOnlyPastePlaceholders(deps.prePasteComposer)) {
    return { attempted: false, cleared: false, reason: "pre_paste_composer_not_empty" };
  }
  if (deps.enterPresses !== 0) {
    return { attempted: false, cleared: false, reason: "enter_already_sent" };
  }

  const currentSnapshot = await deps.capture();
  const currentComposer = composerText(currentSnapshot);
  if (currentComposer === null) {
    return { attempted: false, cleared: false, reason: "composer_unreadable" };
  }
  if (currentComposer === "") {
    return { attempted: false, cleared: true, reason: "composer_already_empty" };
  }
  if (currentComposer !== deps.lastObservedComposer) {
    return { attempted: false, cleared: false, reason: "composer_changed_after_refusal" };
  }
  if (!composerIsRefusedPasteContent(currentComposer, payload) && !composerIsOnlyPastePlaceholders(currentComposer)) {
    return { attempted: false, cleared: false, reason: "composer_not_owned_by_send" };
  }

  let clearSent: boolean;
  try {
    clearSent = await deps.clearComposer();
  } catch {
    return { attempted: true, cleared: false, reason: "clear_key_outcome_unknown" };
  }
  if (!clearSent) {
    return { attempted: false, cleared: false, reason: "pane_mode_refused" };
  }

  const empty = await waitForEmptyComposer({
    capture: deps.capture,
    sleep: deps.sleep,
    stableMs: deps.stableMs ?? 250,
    timeoutMs: deps.timeoutMs ?? 1000,
    pollMs: deps.pollMs ?? 250,
  });
  return empty.ready
    ? { attempted: true, cleared: true, reason: "composer_cleared" }
    : { attempted: true, cleared: false, reason: "composer_empty_not_confirmed" };
}

export type SubmitCheckDeps = {
  capture: () => Promise<string | null>;
  pressSubmit: () => Promise<void>;
  sleep: (ms: number) => Promise<void>;
  /** Composer contents sampled before paste; non-empty or unknown blocks Enter. */
  prePasteComposer?: string | null;
  dwellMs?: number;
  payloadGraceMs?: number;
  /** Time the complete visible composer must remain unchanged before Enter. */
  payloadStableMs?: number;
  /** Bounded time to reach a stable complete composer after it first appears. */
  payloadStableGraceMs?: number;
  clearGraceMs?: number;
  pollMs?: number;
};

/**
 * Paste has already happened. Dwell, wait (bounded) until the composer shows
 * the payload, then press Enter and confirm either an empty input line or
 * Claude's queued-message indicator for the unchanged prompt. If the composer
 * is partial or unreadable, leave it untouched for an operator; never submit
 * unrelated text or automatically press Enter a second time.
 */
export async function submitWithComposerCheck(payload: string, deps: SubmitCheckDeps): Promise<SubmitCheckResult> {
  const dwellMs = deps.dwellMs ?? INJECT_ENTER_DELAY_MS;
  const payloadGraceMs = deps.payloadGraceMs ?? 5000;
  const payloadStableMs = deps.payloadStableMs ?? INJECT_ENTER_DELAY_MS;
  const payloadStableGraceMs = deps.payloadStableGraceMs ?? payloadGraceMs + payloadStableMs;
  const clearGraceMs = deps.clearGraceMs ?? 5000;
  const pollMs = deps.pollMs ?? 250;

  if (deps.prePasteComposer !== undefined && deps.prePasteComposer !== "") {
    return {
      payloadSeen: deps.prePasteComposer === null ? null : false,
      payloadStable: deps.prePasteComposer === null ? null : false,
      cleared: null,
      enterPresses: 0,
    };
  }

  await deps.sleep(dwellMs);

  let payloadSeen: boolean | null = null;
  let payloadDiscovered = false;
  let payloadWaited = 0;
  let payloadStableWaited = 0;
  let stableComposer: string | null = null;
  let stableForMs = 0;
  let payloadStable: boolean | null = false;
  for (;;) {
    const composer = composerText(await deps.capture());
    if (composer === null) {
      payloadStable = null;
      break;
    }
    payloadSeen = composerHoldsPayload(composer, payload)
      || composerShowsNewPastePlaceholder(composer, deps.prePasteComposer);
    if (payloadSeen) {
      payloadDiscovered = true;
      if (composer === stableComposer) {
        stableForMs += pollMs;
      } else {
        stableComposer = composer;
        stableForMs = 0;
      }
      if (stableForMs >= payloadStableMs) {
        payloadStable = true;
        break;
      }
      if (payloadStableWaited >= payloadStableGraceMs) break;
    } else {
      stableComposer = null;
      stableForMs = 0;
      if (!payloadDiscovered && payloadWaited >= payloadGraceMs) break;
      if (payloadDiscovered && payloadStableWaited >= payloadStableGraceMs) break;
    }
    await deps.sleep(pollMs);
    if (payloadDiscovered) payloadStableWaited += pollMs;
    else payloadWaited += pollMs;
  }

  if (payloadSeen !== true || payloadStable !== true) {
    return { payloadSeen, payloadStable, cleared: null, enterPresses: 0 };
  }

  const submittedComposer = stableComposer;
  let enterPresses = 0;
  let cleared: boolean | null = null;
  let queued = false;
  // One Enter, then verify; retry Enter once only if the exact pasted composer
  // is still sitting there (Enter was swallowed), never after it changed.
  for (let attempt = 0; attempt < 2; attempt++) {
    await deps.pressSubmit();
    enterPresses++;
    let lastComposer: string | null = null;
    for (let waited = 0; ; waited += pollMs) {
      await deps.sleep(pollMs);
      const snapshot = await deps.capture();
      lastComposer = composerText(snapshot);
      if (lastComposer === null) {
        cleared = null;
        break;
      }
      cleared = lastComposer === "";
      if (
        !cleared
        && QUEUED_HINT.test(snapshot ?? "")
        && submittedComposer !== null
        && squash(lastComposer) === squash(submittedComposer)
      ) {
        queued = true;
        break;
      }
      if (cleared || waited + pollMs >= clearGraceMs) break;
    }
    if (queued) break;
    // Mid-turn TUIs reflow the composer after Enter (wrap/indent changes), so
    // compare whitespace-normalized text: retry only when it is still exactly
    // our submitted composer, never after foreign text appeared.
    if (cleared !== false || lastComposer === null || submittedComposer === null) break;
    if (squash(lastComposer) !== squash(submittedComposer)) break;
  }

  return queued
    ? { payloadSeen, payloadStable, cleared, queued: true, enterPresses }
    : { payloadSeen, payloadStable, cleared, enterPresses };
}
