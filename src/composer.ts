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

const RULE_LINE = /^\s*─{20,}\s*$/;

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
    return body
      .map((line, index) => (index === 0 ? line.replace(/^\s*❯/, "") : line))
      .join("\n")
      .trim();
  }
  return null;
}

function squash(text: string): string {
  return text.replace(/\s+/g, "");
}

/**
 * True when the full visible payload is in the composer, or Claude has
 * collapsed a multiline paste into its paste placeholder. Matching only the
 * last line is unsafe: a truncated packet can end with the expected tail.
 */
export function composerHoldsPayload(composer: string, payload: string): boolean {
  if (/\[Pasted text/i.test(composer)) return true;
  const expected = squash(payload.trim());
  if (!expected) return composer.trim() === "";
  return squash(composer).includes(expected);
}

export type SubmitCheckResult = {
  /** true/false once the composer was readable before Enter; null if unrecognised. */
  payloadSeen: boolean | null;
  /** true/false once the composer was readable after Enter; null if unrecognised. */
  cleared: boolean | null;
  enterPresses: number;
};

export type SubmitCheckDeps = {
  capture: () => Promise<string | null>;
  pressSubmit: () => Promise<void>;
  sleep: (ms: number) => Promise<void>;
  /** Composer contents sampled before paste; non-empty or unknown blocks Enter. */
  prePasteComposer?: string | null;
  dwellMs?: number;
  payloadGraceMs?: number;
  clearGraceMs?: number;
  pollMs?: number;
};

/**
 * Paste has already happened. Dwell, wait (bounded) until the composer shows
 * the payload, then press Enter once and confirm the input line is empty. If
 * the composer is partial or unreadable, leave it untouched for an operator;
 * never submit unrelated text or automatically press Enter a second time.
 */
export async function submitWithComposerCheck(payload: string, deps: SubmitCheckDeps): Promise<SubmitCheckResult> {
  const dwellMs = deps.dwellMs ?? INJECT_ENTER_DELAY_MS;
  const payloadGraceMs = deps.payloadGraceMs ?? 3000;
  const clearGraceMs = deps.clearGraceMs ?? 2000;
  const pollMs = deps.pollMs ?? 250;

  if (deps.prePasteComposer !== undefined && deps.prePasteComposer !== "") {
    return {
      payloadSeen: deps.prePasteComposer === null ? null : false,
      cleared: null,
      enterPresses: 0,
    };
  }

  await deps.sleep(dwellMs);

  let payloadSeen: boolean | null = null;
  for (let waited = 0; ; waited += pollMs) {
    const composer = composerText(await deps.capture());
    if (composer === null) break;
    payloadSeen = composerHoldsPayload(composer, payload);
    if (payloadSeen || waited >= payloadGraceMs) break;
    await deps.sleep(pollMs);
  }

  if (payloadSeen !== true) {
    return { payloadSeen, cleared: null, enterPresses: 0 };
  }

  await deps.pressSubmit();
  let cleared: boolean | null = null;
  for (let waited = 0; ; waited += pollMs) {
    await deps.sleep(pollMs);
    const composer = composerText(await deps.capture());
    if (composer === null) {
      cleared = null;
      break;
    }
    cleared = composer === "";
    if (cleared || waited + pollMs >= clearGraceMs) break;
  }

  return { payloadSeen, cleared, enterPresses: 1 };
}
