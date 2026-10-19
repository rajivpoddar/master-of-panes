import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { composerClearFence, makeComposerClearKeyWriter } from "../src/clearComposerFence.js";
import { pinFrom, type SlotRowLike } from "../src/interruptBeforeClear.js";
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

test("an identical visible tail with a hidden foreign prefix is NOT owned: automatic clearing sends zero C-u", async () => {
  const pane = new NarrowPane();
  pane.paste(`operator draft that scrolled out of view\n${LONG}`);
  const capture = async (): Promise<string> => pane.snapshot();
  const observed = composerText(await capture());
  let presses = 0;
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
    timeoutMs: 250,
    pollMs: 250,
  });
  assert.equal(result.reason, "composer_not_owned_by_send");
  assert.equal(result.attempted, false);
  assert.equal(presses, 0);
  assert.ok(pane.text.startsWith("operator draft"), "operator text untouched");
});

test("a truncated prefix of the payload is not owned either", async () => {
  const half = "Q0: read-only prod check " + "x".repeat(60);
  let presses = 0;
  const result = await clearOwnedRefusedPasteComposer(LONG, {
    prePasteComposer: "",
    lastObservedComposer: half,
    enterPresses: 0,
    capture: async () => `${RULE}\n❯ ${half}\n${RULE}\n`,
    clearComposer: async () => {
      presses++;
      return true;
    },
    sleep: async () => undefined,
  });
  assert.equal(result.reason, "composer_not_owned_by_send");
  assert.equal(presses, 0);
});

test("a complete multi-line payload is cleared line by line; a foreign edit mid-clear stops it", async () => {
  const payload = "first line of the refused send\nsecond line of the refused send\nthird line of the refused send";
  const capturePane = (text: string): string => [RULE, ...text.split("\n").map((l, i) => (i === 0 ? `❯ ${l}` : `  ${l}`)), RULE, ""].join("\n");

  let text = payload;
  let presses = 0;
  const done = await clearOwnedRefusedPasteComposer(payload, {
    prePasteComposer: "",
    lastObservedComposer: composerText(capturePane(text)),
    enterPresses: 0,
    capture: async () => capturePane(text),
    clearComposer: async () => {
      presses++;
      const at = text.lastIndexOf("\n");
      text = at < 0 ? "" : text.slice(0, at);
      return true;
    },
    sleep: async () => undefined,
    timeoutMs: 250,
    pollMs: 250,
  });
  assert.deepEqual(done, { attempted: true, cleared: true, reason: "composer_cleared" });
  assert.equal(presses, 3);

  text = payload;
  presses = 0;
  const stopped = await clearOwnedRefusedPasteComposer(payload, {
    prePasteComposer: "",
    lastObservedComposer: composerText(capturePane(text)),
    enterPresses: 0,
    capture: async () => capturePane(text),
    clearComposer: async () => {
      presses++;
      text = "operator typed something else entirely in the meantime";
      return true;
    },
    sleep: async () => undefined,
    timeoutMs: 250,
    pollMs: 250,
  });
  assert.equal(stopped.cleared, false);
  assert.equal(presses, 1, "stops at the first remainder that is not the payload's own lines");
});

test("a complete payload and a bracketed-paste placeholder are still cleaned automatically", async () => {
  for (const shown of ["short refused payload that is long enough", "[Pasted text #7 +12 lines]"]) {
    let text = shown;
    const payload = shown.startsWith("[") ? "a long multi line payload\nwith lines" : shown;
    const result = await clearOwnedRefusedPasteComposer(payload, {
      prePasteComposer: "",
      lastObservedComposer: text,
      enterPresses: 0,
      capture: async () => `${RULE}\n❯ ${text}\n${RULE}\n`,
      clearComposer: async () => {
        text = "";
        return true;
      },
      sleep: async () => undefined,
      timeoutMs: 250,
      pollMs: 250,
    });
    assert.deepEqual(result, { attempted: true, cleared: true, reason: "composer_cleared" }, shown);
  }
});

// ---- generation fence -------------------------------------------------------------

function slotRow(over: Partial<SlotRowLike> = {}): SlotRowLike {
  return {
    occupied: true, repository_id: "github:o/r", issue: 10342, pr: null, handoff_id: "h1", branch: "b",
    assignment_epoch: 446, session_id: "77", active_turn_id: null, active_turn_state: "inactive", ...over,
  };
}

function fenceWorld(force: boolean) {
  const world = {
    row: slotRow() as SlotRowLike | null,
    pane: { paneId: "%7", checkout: "/p/3006" } as { paneId: string; checkout: string } | null,
    activity: "idle" as "idle" | "active" | "unknown",
  };
  const pin = pinFrom(world.pane!, world.row!);
  const deps = {
    pin,
    force,
    observePane: async () => world.pane,
    readRow: () => world.row,
    activity: async () => world.activity,
  };
  return { world, deps };
}

test("fence: epoch, session, owner, pane and checkout changes all refuse, with or without force", async () => {
  for (const force of [false, true]) {
    const changes: Array<[string, (w: ReturnType<typeof fenceWorld>["world"]) => void, string]> = [
      ["epoch", (w) => { w.row = slotRow({ assignment_epoch: 447 }); }, "drift:epoch"],
      ["session", (w) => { w.row = slotRow({ session_id: "78" }); }, "drift:session"],
      ["owner", (w) => { w.row = slotRow({ issue: 10999 }); }, "drift:owner"],
      ["pane id", (w) => { w.pane = { paneId: "%155", checkout: "/p/3006" }; }, "drift:pane"],
      ["checkout", (w) => { w.pane = { paneId: "%7", checkout: "/p/3007" }; }, "drift:checkout"],
      ["row gone", (w) => { w.row = null; }, "drift:row_missing"],
    ];
    for (const [name, mutate, reason] of changes) {
      const { world, deps } = fenceWorld(force);
      assert.equal(await composerClearFence(deps, "t"), null, `${name} baseline`);
      mutate(world);
      const got = await composerClearFence(deps, "t");
      assert.ok(got && got.startsWith(reason), `${name} force=${force}: ${got}`);
    }
  }
});

test("fence: idle->active refuses without force; force bypasses activity and turn, never identity", async () => {
  const strict = fenceWorld(false);
  strict.world.activity = "active";
  assert.match((await composerClearFence(strict.deps, "t")) ?? "", /drift:activity_active/);
  strict.world.activity = "idle";
  strict.world.row = slotRow({ active_turn_id: "t9", active_turn_state: "active" });
  assert.match((await composerClearFence(strict.deps, "t")) ?? "", /drift:turn/);

  const forced = fenceWorld(true);
  forced.world.activity = "active";
  forced.world.row = slotRow({ active_turn_id: "t9", active_turn_state: "active" });
  assert.equal(await composerClearFence(forced.deps, "t"), null);
  forced.world.row = slotRow({ active_turn_id: "t9", active_turn_state: "active", assignment_epoch: 500 });
  assert.match((await composerClearFence(forced.deps, "t")) ?? "", /drift:epoch/);
});

test("zero C-u reaches a newer target: queued-lock change, and a change between presses", async () => {
  const multi = Array.from({ length: 6 }, (_, i) => `line ${i} of a stale composer ${"y".repeat(20)}`).join("\n");
  for (const scenario of ["queued-lock epoch change", "idle->active between presses", "checkout change between presses"] as const) {
    const { world, deps } = fenceWorld(scenario !== "idle->active between presses" ? true : false);
    const pane = new NarrowPane();
    pane.paste(multi);
    if (scenario === "queued-lock epoch change") world.row = slotRow({ assignment_epoch: 447 });
    let keys = 0;
    const result = await clearComposerText({
      capture: async () => pane.snapshot(),
      clearComposer: async () => {
        const drift = await composerClearFence(deps, "clear_composer_key");
        if (drift) return drift;
        keys++;
        pane.ctrlU();
        if (keys === 2 && scenario === "idle->active between presses") world.activity = "active";
        if (keys === 2 && scenario === "checkout change between presses") world.pane = { paneId: "%7", checkout: "/p/3007" };
        return true;
      },
      sleep: async () => undefined,
      stableMs: 250,
      timeoutMs: 250,
      pollMs: 250,
    });
    assert.equal(result.cleared, false, scenario);
    assert.equal(result.reason, "generation_fenced", scenario);
    assert.ok(result.fenceReason, scenario);
    assert.equal(keys, scenario === "queued-lock epoch change" ? 0 : 2, `${scenario}: no key after the change`);
  }
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

// ---- placeholder ownership --------------------------------------------------------

function placeholderPane(text: string): string {
  return `${RULE}\n❯ ${text}\n${RULE}\n`;
}

test("a foreign placeholder after the first owned press gets no second key", async () => {
  let shown = "[Pasted text #47][Pasted text #48]";
  let presses = 0;
  const result = await clearOwnedRefusedPasteComposer("unrelated payload", {
    prePasteComposer: shown,
    lastObservedComposer: shown,
    enterPresses: 0,
    capture: async () => placeholderPane(shown),
    clearComposer: async () => {
      presses++;
      shown = "[Pasted text #47][Pasted text #99]"; // one owned left, a NEW placeholder appeared
      return true;
    },
    sleep: async () => undefined,
    timeoutMs: 250,
    pollMs: 250,
  });
  assert.equal(result.cleared, false);
  assert.equal(presses, 1, "no second key once a different placeholder shows");
});

test("the same owned placeholders are cleared one press at a time", async () => {
  let shown = "[Pasted text #47][Pasted text #48]";
  let presses = 0;
  const result = await clearOwnedRefusedPasteComposer("unrelated payload", {
    prePasteComposer: shown,
    lastObservedComposer: shown,
    enterPresses: 0,
    capture: async () => placeholderPane(shown),
    clearComposer: async () => {
      presses++;
      shown = presses === 1 ? "[Pasted text #47]" : "";
      return true;
    },
    sleep: async () => undefined,
    timeoutMs: 250,
    pollMs: 250,
  });
  assert.deepEqual(result, { attempted: true, cleared: true, reason: "composer_cleared" });
  assert.equal(presses, 2);
});

test("plaintext cleanup never becomes placeholder cleanup", async () => {
  const payload = "first line of the refused send\nsecond line of the refused send";
  let shown = payload;
  let presses = 0;
  const result = await clearOwnedRefusedPasteComposer(payload, {
    prePasteComposer: "",
    lastObservedComposer: composerText(placeholderPane(shown.replace(/\n/g, "\n  "))),
    enterPresses: 0,
    capture: async () => placeholderPane(shown.replace(/\n/g, "\n  ")),
    clearComposer: async () => {
      presses++;
      shown = "[Pasted text #5]"; // a foreign paste lands after the first press
      return true;
    },
    sleep: async () => undefined,
    timeoutMs: 250,
    pollMs: 250,
  });
  assert.equal(result.cleared, false);
  assert.equal(presses, 1);
});

// ---- key writer ordering ----------------------------------------------------------

function writerWorld(force: boolean, onModeQuery: (w: ReturnType<typeof fenceWorld>["world"]) => void) {
  const { world, deps } = fenceWorld(force);
  const calls: string[] = [];
  const write = makeComposerClearKeyWriter({
    ...deps,
    inputModeRefusal: async () => {
      calls.push("mode");
      onModeQuery(world);
      return null;
    },
    sendKey: async () => {
      calls.push("C-u");
    },
  });
  return { world, write, calls };
}

test("a generation or idle change inside the mode-query await sends zero C-u", async () => {
  const cases: Array<[string, boolean, (w: ReturnType<typeof fenceWorld>["world"]) => void, RegExp]> = [
    ["epoch change", false, (w) => { w.row = slotRow({ assignment_epoch: 447 }); }, /drift:epoch/],
    ["epoch change under force", true, (w) => { w.row = slotRow({ assignment_epoch: 447 }); }, /drift:epoch/],
    ["session change under force", true, (w) => { w.row = slotRow({ session_id: "78" }); }, /drift:session/],
    ["checkout change under force", true, (w) => { w.pane = { paneId: "%7", checkout: "/p/3007" }; }, /drift:checkout/],
    ["idle -> active", false, (w) => { w.activity = "active"; }, /drift:activity_active/],
    ["turn started", false, (w) => { w.row = slotRow({ active_turn_id: "t1", active_turn_state: "active" }); }, /drift:turn/],
  ];
  for (const [name, force, mutate, expected] of cases) {
    const { write, calls } = writerWorld(force, mutate);
    const got = await write();
    assert.match(String(got), expected, name);
    assert.deepEqual(calls, ["mode"], `${name}: mode was read first and no key followed`);
  }
});

test("force bypasses turn and activity only; the mode is read before the fences and the key is last", async () => {
  const { write, calls } = writerWorld(true, (w) => {
    w.activity = "active";
    w.row = slotRow({ active_turn_id: "t1", active_turn_state: "active" });
  });
  assert.equal(await write(), true);
  assert.deepEqual(calls, ["mode", "C-u"]);
});

test("a row change during the async fence (activity probe) is caught by the final row check", async () => {
  const { world, deps } = fenceWorld(false);
  const calls: string[] = [];
  const write = makeComposerClearKeyWriter({
    ...deps,
    activity: async () => {
      world.row = slotRow({ assignment_epoch: 999 });
      return "idle";
    },
    inputModeRefusal: async () => null,
    sendKey: async () => {
      calls.push("C-u");
    },
  });
  assert.match(String(await write()), /drift:epoch/);
  assert.deepEqual(calls, []);
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
  assert.match(route, /pinFrom\(/);
  assert.match(route, /withSlotSendLock\(slotNum/);
  const lock = route.indexOf("withSlotSendLock(slotNum");
  assert.ok(route.indexOf("composerClearFence(fenceDeps, \"clear_composer_lock\")") > lock, "revalidated inside the lock");
  assert.match(route, /makeComposerClearKeyWriter\(/, "every C-u goes through the ordered key writer");
  assert.doesNotMatch(route, /send-keys -t \$\{paneTarget\} C-u[^\n]*\n[^]*?send-keys -t \$\{paneTarget\} C-u/, "one C-u issuance site");
  assert.match(route, /slot_active_force_required/);
  assert.match(route, /clearComposerText\(/);
  assert.doesNotMatch(route, /send-keys[^\n]*(Enter|Escape|C-c)/);
});
