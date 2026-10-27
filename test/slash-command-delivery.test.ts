// Claude Code 2.1.300 opens an autocomplete menu when a slash command such as
// `/exit` is typed; an Enter sent immediately is swallowed by the menu and the
// text stays buffered (POST /slots/N/respawn -> 500 respawn_exit_uncertain).
// Slash-command delivery must: Escape + C-u, type the command, wait, send Enter
// separately, verify the composer emptied and retry Enter once if still buffered.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { isSlashCommand, resolveSlashCommandEnterDelayMs, slashComposerClearable } from "../src/composer.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

process.env.MOP_SLASH_COMMAND_ENTER_DELAY_MS = "0";
process.env.MOP_INJECT_ENTER_DELAY_MS = "1000";

const CHECKOUT = "/Users/rajiv/Downloads/projects/heydonna-app-3006";
const RULE = "──────────────────────────";

type Pane = { composer: string; buffer: string; keys: string[]; swallowEnters: number };

function shell(p: Pane) {
  return async (command: string) => {
    if (command.startsWith("tmux display-message")) return { stdout: `%16|${CHECKOUT}\n`, stderr: "" };
    if (command.startsWith("git -C")) return { stdout: `${CHECKOUT}\n`, stderr: "" };
    if (command.startsWith("tmux capture-pane")) {
      return { stdout: ["⏺ done", "", RULE, `❯ ${p.composer}`, RULE, "  ⏵⏵ bypass permissions on"].join("\n"), stderr: "" };
    }
    if (command.startsWith("tmux load-buffer")) {
      p.buffer = await readFile(command.match(/'([^']+)'\s*$/)?.[1] ?? "", "utf8");
      return { stdout: "", stderr: "" };
    }
    if (command.startsWith("tmux paste-buffer")) {
      p.keys.push("paste");
      p.composer = p.buffer;
      return { stdout: "", stderr: "" };
    }
    const key = command.match(/^tmux send-keys -t %16 (\S+)$/)?.[1];
    if (key) {
      p.keys.push(key);
      if (key === "Escape") return { stdout: "", stderr: "" };
      if (key === "C-u") { p.composer = ""; return { stdout: "", stderr: "" }; }
      if (key === "Enter") {
        // The autocomplete menu swallows Enter while a slash command is buffered.
        if (p.composer.startsWith("/") && p.swallowEnters > 0) { p.swallowEnters -= 1; return { stdout: "", stderr: "" }; }
        p.composer = "";
        return { stdout: "", stderr: "" };
      }
    }
    throw new Error(`unexpected command: ${command}`);
  };
}

function relayFor(p: Pane) {
  return new TmuxRelay(DEFAULT_CONFIG, { runShell: shell(p) as never });
}

const guarded = (relay: TmuxRelay, command: string) => relay.deliverGuardedToSlot(6, command, {
  expectedPaneId: "%16",
  recheck: async () => null,
  timing: { pollMs: 1, clearGraceMs: 20, payloadGraceMs: 20 },
});

test("slash detection and configurable Enter delay", () => {
  assert.equal(isSlashCommand("/exit"), true);
  assert.equal(isSlashCommand("/clear"), true);
  assert.equal(isSlashCommand("/model opus"), true);
  assert.equal(isSlashCommand("please /exit"), false);
  assert.equal(isSlashCommand("/exit\nmore"), false);
  assert.equal(isSlashCommand("/Users/rajiv/x.txt"), false);
  assert.equal(resolveSlashCommandEnterDelayMs(""), 1000);
  assert.equal(resolveSlashCommandEnterDelayMs("1500"), 1500);
  assert.equal(slashComposerClearable("", "/exit"), true);
  assert.equal(slashComposerClearable("/exit", "/exit"), true);
  assert.equal(slashComposerClearable("my draft", "/exit"), false);
  assert.equal(slashComposerClearable(null, "/exit"), false);
});

test("guarded /exit: Escape, C-u, type, then a separate Enter; swallowed Enter retried once", async () => {
  const p: Pane = { composer: "", buffer: "", keys: [], swallowEnters: 1 };
  const result = await guarded(relayFor(p), "/exit");
  assert.equal(result.outcome, "delivered");
  assert.deepEqual(p.keys, ["Escape", "C-u", "paste", "Enter", "Enter"]);
  assert.equal(p.composer, "");
});

test("guarded /exit with a leftover buffered /exit is cleared first and delivered", async () => {
  const p: Pane = { composer: "/exit", buffer: "", keys: [], swallowEnters: 0 };
  const result = await guarded(relayFor(p), "/exit");
  assert.equal(result.outcome, "delivered");
  assert.deepEqual(p.keys, ["Escape", "C-u", "paste", "Enter"]);
});

test("guarded /exit never clears a foreign draft", async () => {
  const p: Pane = { composer: "half-typed operator note", buffer: "", keys: [], swallowEnters: 0 };
  const result = await guarded(relayFor(p), "/exit");
  assert.equal(result.outcome, "refused_pre_effect");
  assert.equal(result.reason, "composer_not_empty");
  assert.deepEqual(p.keys, []);
});

test("guarded /exit still buffered after the one retry is reported uncertain (two Enters max)", async () => {
  const p: Pane = { composer: "", buffer: "", keys: [], swallowEnters: 5 };
  const result = await guarded(relayFor(p), "/exit");
  assert.equal(result.outcome, "uncertain");
  assert.equal(p.keys.filter(k => k === "Enter").length, 2);
});

test("guarded plain text keeps the single-Enter contract and sends no Escape/C-u", async () => {
  const p: Pane = { composer: "", buffer: "", keys: [], swallowEnters: 0 };
  const result = await guarded(relayFor(p), "Resume the task.");
  assert.equal(result.outcome, "delivered");
  assert.deepEqual(p.keys, ["paste", "Enter"]);
});

test("sendToSlot /clear: clear composer, then separate Enter retried once when swallowed", async () => {
  const p: Pane = { composer: "", buffer: "", keys: [], swallowEnters: 1 };
  assert.equal(await relayFor(p).sendToSlotAsync(6, "/clear"), true);
  assert.deepEqual(p.keys, ["Escape", "C-u", "paste", "Enter", "Enter"]);
});

test("sendToSlot plain text is unchanged: no Escape/C-u", async () => {
  const p: Pane = { composer: "", buffer: "", keys: [], swallowEnters: 0 };
  assert.equal(await relayFor(p).sendToSlotAsync(6, "hello slot"), true);
  assert.deepEqual(p.keys, ["paste", "Enter"]);
});

test("guarded /exit whose retry is refused after the first Enter (TUI exiting) reports submit_unconfirmed", async () => {
  const p: Pane = { composer: "", buffer: "", keys: [], swallowEnters: 1 };
  let enters = 0;
  const relay = relayFor(p);
  const result = await relay.deliverGuardedToSlot(6, "/exit", {
    expectedPaneId: "%16",
    recheck: async (stage) => (stage === "pre_enter" && enters++ > 0 ? "pane_not_idle" : null),
    timing: { pollMs: 1, clearGraceMs: 20, payloadGraceMs: 20 },
  });
  assert.equal(result.outcome, "uncertain");
  assert.equal(result.reason, "submit_unconfirmed");
  assert.equal(p.keys.filter(k => k === "Enter").length, 1, "never a second Enter into a non-idle pane");
});

test("activity probe ignores gray slash-menu chevron below idle composer", async () => {
  const p: Pane = { composer: "/exit", buffer: "", keys: [], swallowEnters: 0 };
  const run = shell(p);
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: async command => {
    const result = await run(command);
    if (command.startsWith("tmux capture-pane")) result.stdout = result.stdout.replace("  ⏵⏵ bypass permissions on", "  \x1b[38;2;153;153;153m❯ /exit Exit the CLI\x1b[0m");
    return result;
  } });
  assert.equal(await relay.getPaneActivityState("%16"), "idle");
});
test("activity probe retains busy composer state despite a white menu chevron", async () => {
  const p: Pane = { composer: "/exit", buffer: "", keys: [], swallowEnters: 0 };
  const run = shell(p);
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: async command => {
    const result = await run(command);
    if (command.startsWith("tmux capture-pane")) {
      result.stdout = result.stdout.replace("❯ /exit", "\x1b[38;2;153;153;153m❯\x1b[0m /exit").replace("  ⏵⏵ bypass permissions on", "  ❯ /exit Exit the CLI");
    }
    return result;
  } });
  assert.equal(await relay.getPaneActivityState("%16"), "active");
});

test("slash clear waits for Escape rendering before C-u and preserves a changed draft", async () => {
  const p: Pane = { composer: "/exit", buffer: "", keys: [], swallowEnters: 0 };
  const run = shell(p);
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: async command => {
    if (command.startsWith("tmux capture-pane") && p.keys.includes("Escape")) p.composer = "operator draft";
    return run(command);
  } });
  const result = await guarded(relay, "/exit");
  assert.equal(result.outcome, "refused_pre_effect");
  assert.equal(result.reason, "composer_not_empty");
  assert.deepEqual(p.keys, ["Escape"]);
  assert.equal(p.composer, "operator draft");
});
