import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  clearComposerText,
  clearOwnedRefusedPasteComposer,
  composerHoldsPayload,
  composerText,
  shouldUseBracketedPaste,
  submitWithComposerCheck,
} from "../src/composer.js";

const RULE = "─".repeat(67);
const WIDTH = 65;
const WINDOW_ROWS = 19;

/**
 * Model of the slot 6 TUI (2026-10-19): 67 columns, composer capped at 19 rows.
 * Longer input scrolls the HEAD out of view, so only the tail is visible.
 * C-u deletes back to the start of the current (last) input line.
 */
class NarrowPane {
  text = "";
  paste(payload: string): void {
    this.text += payload;
  }
  ctrlU(): void {
    const at = this.text.lastIndexOf("\n");
    this.text = at < 0 ? "" : this.text.slice(0, at);
  }
  snapshot(): string {
    const rows: string[] = [];
    for (const line of this.text.split("\n")) {
      if (line.length === 0) rows.push("");
      for (let i = 0; i < line.length; i += WIDTH) rows.push(line.slice(i, i + WIDTH));
    }
    const visible = rows.slice(-WINDOW_ROWS);
    return [
      "✻ Brewed for 16s",
      "",
      RULE,
      ...visible.map((row, i) => (i === 0 ? `❯ ${row}` : `  ${row}`)),
      RULE,
      "   Pushya  gpt-6.1-sol · heydonna-app-3006",
      "",
    ].join("\n");
  }
}

const LONG = Array.from({ length: 18 }, (_, i) => `Q${i}: read-only prod check ${"x".repeat(50)} row ${i}`).join("\n");

test("incident shape: a long send in a narrow pane shows only its tail, so the full payload is never seen", () => {
  const pane = new NarrowPane();
  pane.paste(LONG);
  const composer = composerText(pane.snapshot());
  assert.ok(composer && composer.length > 0);
  assert.equal(composerHoldsPayload(composer, LONG), false);
});

test("a refused long paste is owned by its visible tail and C-u presses empty the whole composer", async () => {
  const pane = new NarrowPane();
  pane.paste(LONG);
  const capture = async (): Promise<string> => pane.snapshot();
  let presses = 0;
  const observed = composerText(await capture());
  const result = await clearOwnedRefusedPasteComposer(LONG, {
    prePasteComposer: "",
    lastObservedComposer: observed,
    enterPresses: 0,
    capture,
    clearComposer: async () => {
      presses++;
      pane.ctrlU();
      return true;
    },
    sleep: async () => undefined,
    stableMs: 250,
    timeoutMs: 250,
    pollMs: 250,
  });
  assert.deepEqual(result, { attempted: true, cleared: true, reason: "composer_cleared" });
  assert.ok(presses > 1, "multi-line paste needs one C-u per input line");
  assert.equal(pane.text, "");
});

test("cleanup stops pressing when the composer stops being attributable to the send", async () => {
  let composer = "operator typed this unrelated draft of reasonable length";
  let presses = 0;
  const result = await clearOwnedRefusedPasteComposer("completely different payload text here", {
    prePasteComposer: "",
    lastObservedComposer: composer,
    enterPresses: 0,
    capture: async () => `${RULE}\n❯ ${composer}\n${RULE}\n`,
    clearComposer: async () => {
      presses++;
      return true;
    },
    sleep: async () => undefined,
  });
  assert.equal(result.reason, "composer_not_owned_by_send");
  assert.equal(presses, 0);
  composer = "";
});

test("clearComposerText empties concatenated sends line by line and never presses Enter", async () => {
  const pane = new NarrowPane();
  pane.paste(`${LONG}\n${LONG}\n[03:10 AM] PM -> slot 6: one line`);
  let presses = 0;
  const result = await clearComposerText({
    capture: async () => pane.snapshot(),
    clearComposer: async () => {
      presses++;
      pane.ctrlU();
      return true;
    },
    sleep: async () => undefined,
    stableMs: 250,
    timeoutMs: 250,
    pollMs: 250,
  });
  assert.equal(result.cleared, true);
  assert.equal(result.reason, "composer_cleared");
  assert.equal(result.presses, presses);
  assert.ok(result.beforeChars > 0);
  assert.equal(pane.text, "");
});

test("clearComposerText reports already_empty, unreadable, pane-mode refusal and a stuck composer", async () => {
  const empty = `${RULE}\n❯ \n${RULE}\n`;
  const full = `${RULE}\n❯ stuck text that never clears\n${RULE}\n`;
  const noop = async () => undefined;
  assert.deepEqual(
    await clearComposerText({ capture: async () => empty, clearComposer: async () => true, sleep: noop }),
    { cleared: true, presses: 0, reason: "already_empty", beforeChars: 0 },
  );
  assert.equal(
    (await clearComposerText({ capture: async () => "no composer here", clearComposer: async () => true, sleep: noop })).reason,
    "composer_unreadable",
  );
  let keys = 0;
  const refused = await clearComposerText({
    capture: async () => full,
    clearComposer: async () => {
      keys++;
      return false;
    },
    sleep: noop,
  });
  assert.equal(refused.reason, "pane_mode_refused");
  assert.equal(keys, 1);
  const stuck = await clearComposerText({
    capture: async () => full,
    clearComposer: async () => true,
    sleep: noop,
    maxPresses: 3,
    timeoutMs: 250,
    pollMs: 250,
  });
  assert.equal(stuck.cleared, false);
  assert.equal(stuck.reason, "composer_empty_not_confirmed");
  assert.equal(stuck.presses, 3);
});

test("a send into a non-empty composer never presses Enter (so the paste must be refused up front)", async () => {
  let enters = 0;
  const result = await submitWithComposerCheck("short follow-up", {
    capture: async () => `${RULE}\n❯ earlier stuck text short follow-up\n${RULE}\n`,
    pressSubmit: async () => {
      enters++;
    },
    sleep: async () => undefined,
    prePasteComposer: "earlier stuck text",
    dwellMs: 0,
  });
  assert.equal(result.enterPresses, 0);
  assert.equal(enters, 0);
});

test("bracketed paste is chosen for long or many-line payloads only", () => {
  assert.equal(shouldUseBracketedPaste("PM: #10342 Q1/Q2 decided. Read /tmp/x.md and continue."), false);
  assert.equal(shouldUseBracketedPaste("a\nb\nc"), false);
  assert.equal(shouldUseBracketedPaste("x".repeat(599)), false);
  assert.equal(shouldUseBracketedPaste("x".repeat(600)), true);
  assert.equal(shouldUseBracketedPaste(Buffer.from(Array.from({ length: 7 }, () => "l").join("\n"))), true);
  assert.equal(shouldUseBracketedPaste(LONG), true);
});

test("send route wiring: refuse before paste, auto bracketed paste, sanctioned clear-composer route", () => {
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const start = server.indexOf("async function pastePayloadWithTmuxBuffer");
  const end = server.indexOf('app.post("/slots/:slotNum/clear-composer"', start);
  const paste = server.slice(start, end);
  const refuse = paste.indexOf('reason: "composer_not_empty"');
  const firstPaste = paste.indexOf("tmux paste-buffer");
  assert.ok(refuse > 0 && firstPaste > refuse, "non-empty composer is refused before any paste-buffer");
  assert.match(paste, /meta\.bracketedPaste \?\? shouldUseBracketedPaste\(payload\)/);
  assert.match(paste, /paste-buffer\$\{bracketedPaste \? " -p" : ""\}/);

  const route = server.slice(end, server.indexOf('app.post("/slots/:slotNum/send"', end));
  assert.match(route, /verifyPaneIdentity\(slotNum\)/);
  assert.match(route, /withSlotSendLock\(slotNum/);
  assert.match(route, /slot_active_force_required/);
  assert.match(route, /clearComposerText\(/);
  assert.doesNotMatch(route, /send-keys[^\n]*(Enter|Escape|C-c)/);
});
