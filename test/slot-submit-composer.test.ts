import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { composerText, composerHoldsPayload, INJECT_ENTER_DELAY_MS, submitWithComposerCheck } from "../src/composer.js";
import { PM_INJECT_ENTER_DELAY_MS, TmuxRelay } from "../src/relay.js";
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
  assert.equal(PM_INJECT_ENTER_DELAY_MS, INJECT_ENTER_DELAY_MS);
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  assert.doesNotMatch(server, /bytes > chunkSize \? 1000 : 500/);
  const relay = readFileSync(new URL("../src/relay.ts", import.meta.url), "utf8");
  const sendBody = relay.slice(relay.indexOf("async sendToSlotAsync"), relay.indexOf("async isSlotActive"));
  assert.doesNotMatch(sendBody, /sleep\(300\)/);
});

test("composerText reads the Claude input line between the rules", () => {
  assert.equal(composerText(pane(["❯ "])), "");
  assert.equal(composerText(pane(["❯ fix the flaky test", "  second line"])), "fix the flaky test\n  second line");
  assert.equal(composerText("plain shell $ "), null);
  assert.equal(composerText(null), null);
});

test("composerHoldsPayload matches the final line or the paste placeholder", () => {
  assert.equal(composerHoldsPayload("do X\nthen finish it", "do X\nthen finish it\n"), true);
  assert.equal(composerHoldsPayload("[Pasted text #1 +40 lines]", "long"), true);
  assert.equal(composerHoldsPayload("do X", "do X\nthen finish it"), false);
});

test("buffered prompt after first Enter gets exactly one more Enter and is then verified cleared", async () => {
  const screens = [pane(["❯ task body"]), pane(["❯ task body"]), pane(["❯ "])];
  const result = await submitWithComposerCheck("task body", {
    capture: async () => screens[0] ?? null,
    pressSubmit: async () => {
      screens.shift();
    },
    sleep: async () => undefined,
    dwellMs: 0,
    clearGraceMs: 250,
  });
  assert.deepEqual(result, { payloadSeen: true, cleared: true, enterPresses: 2 });
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
  assert.equal(result.cleared, false);
  assert.equal(result.enterPresses, 1);
});

test("numbered-slot send dwells 1s before Enter and re-presses once when the prompt stays buffered", async () => {
  const commands: string[] = [];
  let enters = 0;
  let pasteAt = 0;
  let firstEnterAt = 0;
  const relay = new TmuxRelay(DEFAULT_CONFIG, {
    runShell: async (command) => {
      commands.push(command);
      if (command.startsWith("tmux display-message")) {
        return { stdout: "%42|/Users/rajiv/Downloads/projects/heydonna-app-3001\n", stderr: "" };
      }
      if (command.startsWith("git -C")) {
        return { stdout: "/Users/rajiv/Downloads/projects/heydonna-app-3001\n", stderr: "" };
      }
      if (command.includes("paste-buffer")) pasteAt = Date.now();
      if (command.endsWith(" Enter")) {
        enters++;
        if (enters === 1) firstEnterAt = Date.now();
      }
      if (command.startsWith("tmux capture-pane")) {
        return { stdout: enters < 2 ? pane(["❯ continue your work"]) : pane(["❯ "]), stderr: "" };
      }
      return { stdout: "", stderr: "" };
    },
  });

  assert.equal(await relay.sendToSlotAsync(1, "continue your work"), true);
  assert.equal(enters, 2);
  assert.ok(firstEnterAt - pasteAt >= 950, `dwell was ${firstEnterAt - pasteAt}ms`);
  const mutations = commands.filter((command) => command.includes("send-keys") || command.includes("paste-buffer"));
  assert.ok(mutations.every((command) => command.includes("-t %42")));
});
