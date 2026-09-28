import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  composerText,
  composerHoldsPayload,
  clearOwnedRefusedPasteComposer,
  composerIsOnlyPastePlaceholders,
  composerShowsNewPastePlaceholder,
  maxPastePlaceholderNumber,
  INJECT_ENTER_DELAY_MS,
  resolveInjectEnterDelayMs,
  resolveAssignmentInjectEnterDelayMs,
  submitWithComposerCheck,
  waitForEmptyComposer,
} from "../src/composer.js";
import { PM_INJECT_ENTER_DELAY_MS, TmuxRelay } from "../src/relay.js";
import { withSlotSendLock } from "../src/slotSendLock.js";
import { paneInputModeRefusalReason } from "../src/paneInputMode.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const RULE = "─".repeat(103);
function pane(composer: string[]): string {
  return [
    "· Twisting… (2m 48s · ↓ 9.9k tokens)",
    "",
    RULE,
    ...composer,
    RULE,
    "   Hasta SD  Opus 5.5 (1M context) · heydonna-app-3002 · test/8317",
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
    "",
  ].join("\n");
}

test("slot and PM share one 1000 ms paste->Enter dwell", () => {
  assert.equal(INJECT_ENTER_DELAY_MS, 1000);
  assert.equal(resolveInjectEnterDelayMs("500"), 1000);
  assert.equal(resolveInjectEnterDelayMs("1500"), 1500);
  assert.equal(resolveInjectEnterDelayMs(undefined, "0"), 1000);
  assert.equal(PM_INJECT_ENTER_DELAY_MS, INJECT_ENTER_DELAY_MS);
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  assert.doesNotMatch(server, /bytes > chunkSize \? 1000 : 500/);
  assert.match(server, /return withSlotSendLock\(slotNum, async \(\) =>/);
  const relay = readFileSync(new URL("../src/relay.ts", import.meta.url), "utf8");
  const sendBody = relay.slice(relay.indexOf("async sendToSlotAsync"), relay.indexOf("async isSlotActive"));
  assert.doesNotMatch(sendBody, /sleep\(300\)/);
  assert.match(sendBody, /withSlotSendLock\(slotNum, async \(\) =>/);
});

test("assignment submit dwell is at least 2s while honoring larger configured dwell", () => {
  assert.equal(resolveAssignmentInjectEnterDelayMs(1000), 2000);
  assert.equal(resolveAssignmentInjectEnterDelayMs(1500), 2000);
  assert.equal(resolveAssignmentInjectEnterDelayMs(3000), 3000);
});

test("composerText reads the Claude input line between the rules", () => {
  assert.equal(composerText(pane(["❯ "])), "");
  assert.equal(composerText(pane(["❯ fix the flaky test", "  second line"])), "fix the flaky test\n  second line");
  assert.equal(composerText("plain shell $ "), null);
  assert.equal(composerText(null), null);
});

test("composerHoldsPayload requires the full visible payload and rejects hidden paste contents", () => {
  assert.equal(composerHoldsPayload("do X\nthen finish it", "do X\nthen finish it\n"), true);
  assert.equal(composerHoldsPayload("[Pasted text #1 +40 lines]", "long"), false);
  assert.equal(composerHoldsPayload("[Pasted text #13 +7 lines]\npacket line 9\npacket line 10", "packet line 1\npacket line 10\npacket line 13"), false);
  assert.equal(composerHoldsPayload("then finish it", "do X\nthen finish it"), false);
});

test("new [Pasted text #N] placeholder proves the paste landed and is submitted", async () => {
  const payload = `FILE_PACKET sha256=${"a".repeat(64)} ${"pointer".repeat(30)}`;
  let composer = "[Pasted text #68]";
  let enterPresses = 0;
  const result = await submitWithComposerCheck(payload, {
    capture: async () => pane([`❯ ${composer}`]),
    pressSubmit: async () => {
      enterPresses++;
      composer = "";
    },
    sleep: async () => undefined,
    prePasteComposer: "",
    dwellMs: 0,
    pollMs: 250,
  });
  assert.deepEqual(result, { payloadSeen: true, payloadStable: true, cleared: true, enterPresses: 1 });
  assert.equal(enterPresses, 1);
});

test("placeholder detection requires N greater than any pre-paste placeholder", () => {
  assert.equal(maxPastePlaceholderNumber("[Pasted text #47][Pasted text #48 +12 lines]"), 48);
  assert.equal(maxPastePlaceholderNumber("plain text"), 0);
  assert.equal(composerShowsNewPastePlaceholder("[Pasted text #49]", ""), true);
  assert.equal(composerShowsNewPastePlaceholder("[Pasted text #48][Pasted text #49 +3 lines]", "[Pasted text #48]"), true);
  assert.equal(composerShowsNewPastePlaceholder("[Pasted text #48]", "[Pasted text #48]"), false);
  assert.equal(composerShowsNewPastePlaceholder("hello", ""), false);
  assert.equal(composerIsOnlyPastePlaceholders("[Pasted text #47][Pasted text #48]"), true);
  assert.equal(composerIsOnlyPastePlaceholders("[Pasted text #47] hi"), false);
  assert.equal(composerIsOnlyPastePlaceholders(""), false);
});

test("refusal after two swallowed Enters clears the stale placeholder with C-u", async () => {
  const payload = "PM test packet body";
  let composer = "[Pasted text #50]";
  let lastObservedComposer: string | null = null;
  let enterPresses = 0;
  let clearKeys = 0;
  const capture = async (): Promise<string> => pane([`❯ ${composer}`]);
  const result = await submitWithComposerCheck(payload, {
    capture: async () => {
      const snap = await capture();
      lastObservedComposer = composerText(snap);
      return snap;
    },
    pressSubmit: async () => {
      enterPresses++;
    },
    sleep: async () => undefined,
    prePasteComposer: "",
    dwellMs: 0,
    clearGraceMs: 250,
    pollMs: 250,
  });
  assert.deepEqual(result, { payloadSeen: true, payloadStable: true, cleared: false, enterPresses: 2 });

  const cleanup = await clearOwnedRefusedPasteComposer(payload, {
    prePasteComposer: "",
    lastObservedComposer,
    enterPresses: 0,
    capture,
    clearComposer: async () => {
      clearKeys++;
      composer = "";
      return true;
    },
    sleep: async () => undefined,
    stableMs: 250,
    timeoutMs: 500,
    pollMs: 250,
  });
  assert.deepEqual(cleanup, { attempted: true, cleared: true, reason: "composer_cleared" });
  assert.equal(clearKeys, 1);
  assert.equal(enterPresses, 2, "retry Enter at most once");
});

test("stale placeholder-only pre-paste composer is owned by refusal cleanup", async () => {
  let composer = "[Pasted text #47][Pasted text #48]";
  const capture = async (): Promise<string> => pane([`❯ ${composer}`]);
  const cleanup = await clearOwnedRefusedPasteComposer("unrelated payload", {
    prePasteComposer: "[Pasted text #47][Pasted text #48]",
    lastObservedComposer: composer,
    enterPresses: 0,
    capture,
    clearComposer: async () => {
      composer = "";
      return true;
    },
    sleep: async () => undefined,
    stableMs: 250,
    timeoutMs: 500,
    pollMs: 250,
  });
  assert.deepEqual(cleanup, { attempted: true, cleared: true, reason: "composer_cleared" });
});

test("refused-paste cleanup preserves preexisting, changed, unreadable, and already-submitted composer state", async () => {
  const cases = [
    { name: "preexisting composer", pre: "user draft", observed: "[Pasted text #1]", current: "[Pasted text #1]", enters: 0, reason: "pre_paste_composer_not_empty" },
    { name: "operator edited after refusal", pre: "", observed: "[Pasted text #2]", current: "[Pasted text #2]\noperator text", enters: 0, reason: "composer_changed_after_refusal" },
    { name: "unreadable composer", pre: "", observed: null, current: null, enters: 0, reason: "composer_unreadable" },
    { name: "unrelated visible text", pre: "", observed: "operator-authored text", current: "operator-authored text", enters: 0, reason: "composer_not_owned_by_send" },
    { name: "Enter already sent", pre: "", observed: "[Pasted text #4]", current: "[Pasted text #4]", enters: 1, reason: "enter_already_sent" },
  ] as const;

  for (const scenario of cases) {
    let clearKeys = 0;
    const result = await clearOwnedRefusedPasteComposer("FILE_PACKET pointer", {
      prePasteComposer: scenario.pre,
      lastObservedComposer: scenario.observed,
      enterPresses: scenario.enters,
      capture: async () => scenario.current === null ? null : pane([`❯ ${scenario.current}`]),
      clearComposer: async () => {
        clearKeys++;
        return true;
      },
      sleep: async () => undefined,
      pollMs: 250,
    });
    assert.equal(result.attempted, false, scenario.name);
    assert.equal(result.cleared, false, scenario.name);
    assert.equal(result.reason, scenario.reason, scenario.name);
    assert.equal(clearKeys, 0, scenario.name);
  }
});

test("incidental FILE_PACKET words are not enough to clear a refused composer", async () => {
  const payload = `FILE_PACKET sha256=${"a".repeat(64)} payload contains a the gate pointer`;

  for (const fragment of ["a", "the", "gate"]) {
    let clearKeys = 0;
    const result = await clearOwnedRefusedPasteComposer(payload, {
      prePasteComposer: "",
      lastObservedComposer: fragment,
      enterPresses: 0,
      capture: async () => pane([`❯ ${fragment}`]),
      clearComposer: async () => {
        clearKeys++;
        return true;
      },
      sleep: async () => undefined,
    });

    assert.equal(result.reason, "composer_not_owned_by_send", fragment);
    assert.equal(result.attempted, false, fragment);
    assert.equal(clearKeys, 0, fragment);
  }
});

test("refusal cleanup is shared by file and text sends without changing the active-slot force gate", () => {
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const routeStart = server.indexOf('app.post("/slots/:slotNum/send"');
  const routeEnd = server.indexOf("// ─── Plan Approval", routeStart);
  const route = server.slice(routeStart, routeEnd);
  const activeGate = route.indexOf("if (!force && isValidDevSlot(slotNum, config.slotCount))");
  const fileSend = route.indexOf("pastePayloadWithTmuxBuffer(slotNum, paneTarget, filePayload");
  const textSend = route.indexOf("pastePayloadWithTmuxBuffer(slotNum, paneTarget, commandPayload");
  assert.ok(activeGate >= 0 && activeGate < fileSend && activeGate < textSend);
  assert.match(route, /reason: "slot_active_force_required"/);
  assert.ok(fileSend >= 0 && textSend >= 0, "both transports use the same serialized paste helper");
  assert.match(route.slice(fileSend, fileSend + 240), /clearOwnedComposerOnRefusal:\s*true/);
  assert.match(route.slice(textSend, textSend + 240), /clearOwnedComposerOnRefusal:\s*true/);

  const pasteStart = server.indexOf("async function pastePayloadWithTmuxBuffer");
  const pasteEnd = server.indexOf('app.post("/slots/:slotNum/send"', pasteStart);
  assert.match(server.slice(pasteStart, pasteEnd), /clearOwnedRefusedPasteComposer/);

  const refusedBlocks = route.split("if (!verify.ok) {").slice(1);
  assert.equal(refusedBlocks.length, 2, "file and text refusals both return through the unverified response path");
  for (const block of refusedBlocks) {
    assert.match(block.slice(0, 600), /success:\s*false/);
  }

  const assignmentStart = server.indexOf("async function deliverTaskFileForAssignment");
  const assignmentEnd = server.indexOf("registerAssignmentRoute(app, db, issueProjection)", assignmentStart);
  assert.doesNotMatch(server.slice(assignmentStart, assignmentEnd), /clearOwnedComposerOnRefusal/);
});

test("assignment packets wait for a stable empty composer before the first paste", () => {
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const assignmentStart = server.indexOf("async function deliverTaskFileForAssignment");
  const assignmentEnd = server.indexOf("registerAssignmentRoute(app, db, issueProjection)");
  const assignmentDelivery = server.slice(assignmentStart, assignmentEnd);
  assert.match(assignmentDelivery, /requireEmptyComposerBeforePaste:\s*true/);
  assert.match(assignmentDelivery, /bracketedPaste:\s*true/);

  const pasteStart = server.indexOf("async function pastePayloadWithTmuxBuffer");
  const pasteEnd = server.indexOf('app.post("/slots/:slotNum/send"', pasteStart);
  const pastePath = server.slice(pasteStart, pasteEnd);
  assert.ok(
    pastePath.indexOf("waitForEmptyComposer") >= 0
      && pastePath.indexOf("waitForEmptyComposer") < pastePath.indexOf("tmux load-buffer"),
    "the composer readiness check must run before any tmux paste",
  );
  assert.match(pastePath, /const submitDwellMs = meta\.requireEmptyComposerBeforePaste[\s\S]*?resolveAssignmentInjectEnterDelayMs\(\)[\s\S]*?: INJECT_ENTER_DELAY_MS/);
  assert.match(pastePath, /paste-buffer\$\{meta\.bracketedPaste \? " -p" : ""\}/);
});

test("post-clear assignment submits a collapsed placeholder packet once", async () => {
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const assignmentStart = server.indexOf("async function deliverTaskFileForAssignment");
  const assignmentEnd = server.indexOf("registerAssignmentRoute(app, db, issueProjection)");
  assert.match(server.slice(assignmentStart, assignmentEnd), /bracketedPaste:\s*true/);

  const payload = Array.from({ length: 13 }, (_, index) => `packet line ${index + 1}`).join("\n");
  let collapsed: string[] | null = [
    "❯ [Pasted text #13 +7 lines]",
    ...Array.from({ length: 5 }, (_, index) => `packet line ${index + 9}`),
  ];
  let enterPresses = 0;
  const result = await submitWithComposerCheck(payload, {
    capture: async () => pane(collapsed ?? ["❯ "]),
    pressSubmit: async () => {
      enterPresses++;
      collapsed = null;
    },
    sleep: async () => undefined,
    prePasteComposer: "",
    dwellMs: resolveAssignmentInjectEnterDelayMs(1000),
    payloadStableMs: 1000,
    clearGraceMs: 1000,
    pollMs: 250,
  });
  assert.equal(enterPresses, 1);
  assert.deepEqual(result, { payloadSeen: true, payloadStable: true, cleared: true, enterPresses: 1 });
});

test("slot sends check tmux pane mode before any normal input or paste", () => {
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const routeStart = server.indexOf('app.post("/slots/:slotNum/send"');
  const routeEnd = server.indexOf('\n});', routeStart);
  const route = server.slice(routeStart, routeEnd);
  const routeModeCheck = route.indexOf("readPaneInputModeRefusal(paneTarget)");
  const rawBranch = route.indexOf("if (raw) {");
  const typedModeRefusal = route.indexOf("reason: paneModeRefusal", routeModeCheck);
  const firstNormalKey = route.indexOf("tmux send-keys -t ${paneTarget} i");
  assert.ok(routeModeCheck > rawBranch, "raw keys remain available for safe operator recovery");
  assert.ok(routeModeCheck >= 0 && routeModeCheck < firstNormalKey, "mode is checked before editor-mode keystrokes");
  assert.ok(typedModeRefusal > routeModeCheck && typedModeRefusal < firstNormalKey, "copy mode is returned as a typed refusal before input");

  const pasteStart = server.indexOf("async function pastePayloadWithTmuxBuffer");
  const pasteEnd = server.indexOf('app.post("/slots/:slotNum/send"', pasteStart);
  const pastePath = server.slice(pasteStart, pasteEnd);
  const pasteModeCheck = pastePath.indexOf("readPaneInputModeRefusal(paneAddress)");
  const firstBufferWrite = pastePath.indexOf("tmux load-buffer");
  assert.ok(pasteModeCheck >= 0 && pasteModeCheck < firstBufferWrite, "pane mode is rechecked before paste-buffer writes");
});

test("tmux pane input mode check allows only readable normal mode", () => {
  assert.equal(paneInputModeRefusalReason("0\n"), null);
  assert.equal(paneInputModeRefusalReason("1\n"), "pane_in_copy_mode");
  assert.equal(paneInputModeRefusalReason(null), "pane_mode_unreadable");
  assert.equal(paneInputModeRefusalReason(""), "pane_mode_unreadable");
  assert.equal(paneInputModeRefusalReason("unexpected"), "pane_mode_unreadable");
});

test("clear-transition tail and unreadable pane must settle to an empty composer before paste", async () => {
  const snapshots = [
    pane(["❯ only the final two lines"]),
    "Claude Code is restarting after /clear",
    pane(["❯ "]),
    pane(["❯ "]),
    pane(["❯ "]),
    pane(["❯ "]),
    pane(["❯ "]),
  ];
  let captures = 0;
  let sleptMs = 0;
  const result = await waitForEmptyComposer({
    capture: async () => snapshots[Math.min(captures++, snapshots.length - 1)] ?? null,
    sleep: async (ms) => {
      sleptMs += ms;
    },
    stableMs: 1000,
    timeoutMs: 3000,
    pollMs: 250,
  });

  assert.equal(result.ready, true);
  assert.equal(result.snapshot, pane(["❯ "]));
  assert.ok(sleptMs >= 1500, `the stale tail and restart screen were not skipped (${sleptMs}ms)`);
});

test("assignment pre-paste readiness times out without entering or pasting into a stale composer", async () => {
  const result = await waitForEmptyComposer({
    capture: async () => pane(["❯ only the final two lines"]),
    sleep: async () => undefined,
    stableMs: 1000,
    timeoutMs: 500,
    pollMs: 250,
  });
  assert.deepEqual(result, {
    ready: false,
    snapshot: pane(["❯ only the final two lines"]),
    waitedMs: 500,
  });
});

test("buffered prompt after Enter gets one retry Enter and is reported uncleared", async () => {
  let enterPresses = 0;
  const result = await submitWithComposerCheck("task body", {
    capture: async () => pane(["❯ task body"]),
    pressSubmit: async () => {
      enterPresses++;
    },
    sleep: async () => undefined,
    dwellMs: 0,
    clearGraceMs: 250,
  });
  assert.deepEqual(result, { payloadSeen: true, payloadStable: true, cleared: false, enterPresses: 2 });
  assert.equal(enterPresses, 2, "one retry Enter, never more");
});

test("waits one second after the complete payload first appears before Enter", async () => {
  let elapsedMs = 0;
  let submitted = false;
  let enterAtMs: number | null = null;
  const result = await submitWithComposerCheck("complete task", {
    capture: async () => {
      if (submitted) return pane(["❯ "]);
      return pane(["❯ complete task"]);
    },
    pressSubmit: async () => {
      enterAtMs = elapsedMs;
      submitted = elapsedMs >= 2000;
    },
    sleep: async (ms) => {
      elapsedMs += ms;
    },
    prePasteComposer: "",
    dwellMs: 1000,
    clearGraceMs: 250,
    pollMs: 250,
  });

  assert.equal(enterAtMs, 2000, "the payload was first observed at 1000 ms and must remain stable for another second");
  assert.deepEqual(result, { payloadSeen: true, payloadStable: true, cleared: true, enterPresses: 1 });
});

test("assignment submit waits the extra dwell, then sends one Enter and verifies clear", async () => {
  let elapsedMs = 0;
  let submitted = false;
  let enters = 0;
  let enterAtMs: number | null = null;
  const result = await submitWithComposerCheck("short inline task", {
    capture: async () => submitted ? pane(["❯ "]) : pane(["❯ short inline task"]),
    pressSubmit: async () => {
      enters++;
      enterAtMs = elapsedMs;
      submitted = true;
    },
    sleep: async (ms) => {
      elapsedMs += ms;
    },
    prePasteComposer: "",
    dwellMs: resolveAssignmentInjectEnterDelayMs(1000),
    clearGraceMs: 250,
    pollMs: 250,
  });

  assert.equal(enterAtMs, 3000, "assignment waits 2s after paste plus 1s of stable full payload");
  assert.equal(enters, 1);
  assert.deepEqual(result, { payloadSeen: true, payloadStable: true, cleared: true, enterPresses: 1 });
});

test("partial paste is reported so the send is not marked verified", async () => {
  const result = await submitWithComposerCheck("line one\nline two", {
    capture: async () => pane(["❯ line one"]),
    pressSubmit: async () => undefined,
    sleep: async () => undefined,
    dwellMs: 0,
    payloadGraceMs: 500,
    clearGraceMs: 250,
  });
  assert.equal(result.payloadSeen, false);
  assert.equal(result.payloadStable, false);
  assert.equal(result.cleared, null);
  assert.equal(result.enterPresses, 0);
});

test("unreadable post-paste composer refuses without Enter", async () => {
  let enterPresses = 0;
  const result = await submitWithComposerCheck("complete packet", {
    capture: async () => null,
    pressSubmit: async () => {
      enterPresses++;
    },
    sleep: async () => undefined,
    prePasteComposer: "",
    dwellMs: 0,
  });
  assert.equal(enterPresses, 0);
  assert.deepEqual(result, { payloadSeen: null, payloadStable: null, cleared: null, enterPresses: 0 });
});

test("pre-existing or unreadable composer content is never submitted", async () => {
  for (const prePasteComposer of ["unrelated unsent prompt", null]) {
    let enterPresses = 0;
    const result = await submitWithComposerCheck("new payload", {
      capture: async () => pane(["❯ new payload"]),
      pressSubmit: async () => {
        enterPresses++;
      },
      sleep: async () => undefined,
      prePasteComposer,
    });
    assert.equal(result.enterPresses, 0);
    assert.notEqual(result.payloadStable, true);
    assert.equal(enterPresses, 0);
  }
});

test("same-slot pane sends run in order without blocking a different slot", async () => {
  const events: string[] = [];
  let releaseFirst!: () => void;
  const firstGate = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  const first = withSlotSendLock(2, async () => {
    events.push("first:start");
    await firstGate;
    events.push("first:end");
  });
  const second = withSlotSendLock(2, async () => {
    events.push("second:start");
    events.push("second:end");
  });
  await withSlotSendLock(3, async () => {
    events.push("other-slot");
  });
  assert.deepEqual(events, ["first:start", "other-slot"]);
  releaseFirst();
  await Promise.all([first, second]);
  assert.deepEqual(events, ["first:start", "other-slot", "first:end", "second:start", "second:end"]);
});

test("numbered-slot send dwells at least 1s and presses Enter once", async () => {
  const commands: string[] = [];
  let enters = 0;
  let pasteAt = 0;
  let firstEnterAt = 0;
  let pasted = false;
  const relay = new TmuxRelay(DEFAULT_CONFIG, {
    runShell: async (command) => {
      commands.push(command);
      if (command.startsWith("tmux display-message")) {
        return { stdout: "%42|/Users/rajiv/Downloads/projects/heydonna-app-3001\n", stderr: "" };
      }
      if (command.startsWith("git -C")) {
        return { stdout: "/Users/rajiv/Downloads/projects/heydonna-app-3001\n", stderr: "" };
      }
      if (command.includes("paste-buffer")) {
        pasteAt = Date.now();
        pasted = true;
      }
      if (command.endsWith(" Enter")) {
        enters++;
        if (enters === 1) firstEnterAt = Date.now();
      }
      if (command.startsWith("tmux capture-pane")) {
        return {
          stdout: !pasted ? pane(["❯ "]) : enters < 1 ? pane(["❯ continue your work"]) : pane(["❯ "]),
          stderr: "",
        };
      }
      return { stdout: "", stderr: "" };
    },
  });

  assert.equal(await relay.sendToSlotAsync(1, "continue your work"), true);
  assert.equal(enters, 1);
  assert.ok(firstEnterAt - pasteAt >= 950, `dwell was ${firstEnterAt - pasteAt}ms`);
  const mutations = commands.filter((command) => command.includes("send-keys") || command.includes("paste-buffer"));
  assert.ok(mutations.every((command) => command.includes("-t %42")));
});

// Incident mop-midturn-submit-20260927 (event 3284927): mid-turn pane reflowed
// the composer after Enter #1, so the exact-string retry guard skipped Enter #2.
const MIDTURN_PAYLOAD = "PM -> slot 4: split approved with one change: do NOT make S3 depend on your token; ship only the Origin branch.";
const MIDTURN_WIDE = [`❯ ${MIDTURN_PAYLOAD}`];
const MIDTURN_REFLOWED = [
  "❯ PM -> slot 4: split approved with one change: do NOT make S3",
  "  depend on your token; ship only the Origin branch.",
];

async function midturnSubmit(afterPress: (press: number) => string[] | string) {
  let presses = 0;
  let view: string[] | string = MIDTURN_WIDE;
  const result = await submitWithComposerCheck(MIDTURN_PAYLOAD, {
    capture: async () => typeof view === "string" ? view : pane(view),
    pressSubmit: async () => {
      presses++;
      view = afterPress(presses);
    },
    sleep: async () => undefined,
    prePasteComposer: "",
    dwellMs: 0,
    pollMs: 250,
    payloadStableMs: 250,
    clearGraceMs: 500,
  });
  return { result, presses };
}

test("mid-turn: reflowed composer after Enter #1 gets Enter #2 and clears", async () => {
  const { result, presses } = await midturnSubmit((p) => (p === 1 ? MIDTURN_REFLOWED : ["❯ "]));
  assert.deepEqual(result, { payloadSeen: true, payloadStable: true, cleared: true, enterPresses: 2 });
  assert.equal(presses, 2);
});

test("mid-turn: empty composer after Enter #1 is success with one press", async () => {
  const { result, presses } = await midturnSubmit(() => ["❯ "]);
  assert.equal(result.cleared, true);
  assert.equal(presses, 1);
});

test("mid-turn: unchanged prompt plus queued indicator is success without a second Enter", async () => {
  const queuedSnapshot = pane(MIDTURN_WIDE).replace(
    "· Twisting…",
    "Press up to edit queued messages\n· Twisting…",
  );
  const { result, presses } = await midturnSubmit(() => queuedSnapshot);
  assert.deepEqual(result, {
    payloadSeen: true,
    payloadStable: true,
    cleared: false,
    queued: true,
    enterPresses: 1,
  });
  assert.equal(presses, 1);
});

test("mid-turn: still held after two Enters is an explicit uncleared failure (no third press)", async () => {
  const { result, presses } = await midturnSubmit(() => MIDTURN_REFLOWED);
  assert.deepEqual(result, { payloadSeen: true, payloadStable: true, cleared: false, enterPresses: 2 });
  assert.equal(presses, 2);
});

test("mid-turn: exact prompt without queued indicator stays uncleared after at most two Enters", async () => {
  const { result, presses } = await midturnSubmit(() => MIDTURN_WIDE);
  assert.deepEqual(result, { payloadSeen: true, payloadStable: true, cleared: false, enterPresses: 2 });
  assert.equal(presses, 2, "retry once, never a third time");
});

test("mid-turn: foreign text after Enter #1 is never submitted again", async () => {
  const { result, presses } = await midturnSubmit(() => [`❯ ${MIDTURN_PAYLOAD} plus operator text`]);
  assert.equal(result.cleared, false);
  assert.equal(presses, 1);
});

// 2026-09-28 slot 1 / #8422 epoch 1081: MoP's own `/clear` sat in Claude
// Code's queued-message area above an EMPTY `❯` input line while a turn ran.
// The queued area must not count as a foreign draft.
function busyQueuedPane(input: string[]): string {
  return [
    "✽ Considering… (4m 49s · ↓ 2.0k tokens)",
    "",
    RULE,
    "❯ /clear",
    ...input,
    RULE,
    "  ctrl+x ctrl+s to send now · Press up to edit queued messages",
    "   Rohini SD  heydonna-app-3001",
    "",
  ].join("\n");
}

test("queued area: busy pane with queued /clear and empty input reads as empty", async () => {
  assert.equal(composerText(busyQueuedPane(["❯ "])), "");
  const ready = await waitForEmptyComposer({
    capture: async () => busyQueuedPane(["❯ "]),
    sleep: async () => undefined,
    stableMs: 500,
  });
  assert.equal(ready.ready, true, "busy + queued /clear + empty input must deliver");
});

test("queued area: foreign draft on the real input line still refuses", async () => {
  assert.equal(composerText(busyQueuedPane(["❯ half-typed operator note"])), "half-typed operator note");
  const ready = await waitForEmptyComposer({
    capture: async () => busyQueuedPane(["❯ half-typed operator note"]),
    sleep: async () => undefined,
    stableMs: 500,
    timeoutMs: 1000,
  });
  assert.equal(ready.ready, false);
});

test("queued area: idle empty pane still delivers", async () => {
  const ready = await waitForEmptyComposer({
    capture: async () => pane(["❯ "]),
    sleep: async () => undefined,
    stableMs: 500,
  });
  assert.equal(ready.ready, true);
});

test("queued area: payload moving into the queue after Enter counts as delivered", async () => {
  let view = pane(["❯ /clear"]);
  let presses = 0;
  const result = await submitWithComposerCheck("/clear", {
    capture: async () => view,
    pressSubmit: async () => {
      presses++;
      view = busyQueuedPane(["❯ "]);
    },
    sleep: async () => undefined,
    prePasteComposer: "",
    dwellMs: 0,
    payloadStableMs: 250,
  });
  assert.equal(result.cleared, true);
  assert.equal(presses, 1);
});
