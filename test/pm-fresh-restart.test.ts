// PM restart = Rajiv's exit flow (C0ALZJHGE49 thread 1794998965.861819, ts 1794999568.152419): "MoP should inject /exit at
// stop, inject 1 if the exit option comes up, then inject the launch pm command, then inject the continuation prompt
// without any gates." The Stop hook injects /exit; ProcessHealthChecker.runPMFreshRestart does the other three steps.
// No pane/boot ownership pin, launch record or replacement binding: a probe failure only delays a bounded wait.
import assert from "node:assert/strict";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import {
  isShellPromptVisible, pmConfiguredModel, pmFreshLaunchCommand, PM_MODEL_CONFIG_KEY,
  PM_RESTART_CONTINUATION_PROMPT, ProcessHealthChecker, RESTART_COMMANDS,
} from "../src/health.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const PM_PATH = "/Users/rajiv/Downloads/projects/heydonna-app";
const SHELL_TEXT = "some earlier output\n\x1b[32mrajiv@heydonna\x1b[39m ~ \x1b[31m»\x1b[39m\n$ \n\n";
const COMPOSER_TEXT = `${"─".repeat(40)}\n❯ \n${"─".repeat(40)}\n  ? for shortcuts`;
// The "Background work is running" dialog (shape of test/exit-confirmation.test.ts), option 1 highlighted.
const EXIT_MENU_TEXT = "Background work is running\n❯ 1. Exit and stop tasks\n  2. Move to background and exit\n  3. Stay\nEnter to confirm · Esc to cancel";

// menu: Claude is up showing the exit dialog; exiting: Claude is still up, no dialog; shell: back at the zsh prompt;
// booting: launcher typed, no Claude yet; fresh: the new Claude session is up at its input prompt.
type PaneState = "menu" | "exiting" | "idle" | "busy" | "shell" | "booting" | "fresh";

// failIdentityProbe: how many `tmux display-message` probes fail first (Infinity = always). noSessionStart: the new Claude
// boots but its SessionStart hook never arrives (a late or missing hook).
function harness(options: { state?: PaneState; neverBoots?: boolean; failIdentityProbe?: number; noSessionStart?: boolean } = {}) {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  const pane = { state: options.state ?? "exiting" as PaneState, polls: 0, typed: [] as string[], shell: [] as string[], sends: [] as string[],
    onPoll: undefined as undefined | ((polls: number) => void),
    // The VISIBLE screen (capture-pane) when it differs from the scrollback the other fakes show, and text a user appends
    // to the typed launcher after it was typed.
    visible: undefined as undefined | string, appended: "", probeFailures: options.failIdentityProbe ?? 0 };
  const order: string[] = [];
  const typedRaw = () => (pane.typed.at(-1) ?? "").replace(/^tmux send-keys -l -t %7 /, "").slice(1, -1).replace(/'\\''/g, "'");
  const runShell = async (command: string) => {
    pane.shell.push(command);
    let stdout = "";
    if (command.startsWith("tmux display-message")) {
      if (pane.probeFailures > 0) { pane.probeFailures--; throw new Error("Command failed: tmux display-message"); }
      stdout = `%7|${PM_PATH}\n`;
    } else if (command.startsWith("git -C ") && command.endsWith("rev-parse --show-toplevel")) stdout = `${PM_PATH}\n`;
    else if (command.startsWith("tmux list-panes")) stdout = `%7|${PM_PATH}\n`;
    else if (command === "tmux capture-pane -t %7 -p -J") {
      stdout = pane.visible ?? (pane.state === "shell" ? (pane.typed.length ? `earlier output\n$ ${typedRaw()}${pane.appended}\n` : SHELL_TEXT)
        : pane.state === "menu" ? EXIT_MENU_TEXT : COMPOSER_TEXT);
    } else if (command.startsWith("tmux respawn-pane")) { pane.state = "shell"; order.push("respawn"); }
    else if (command === "tmux send-keys -t %7 1") order.push("1");
    else if (command.startsWith("tmux send-keys -l")) { pane.typed.push(command); order.push("type"); }
    else if (command.startsWith("tmux send-keys") && command.endsWith(" Enter")) {
      if (pane.state === "menu") { order.push("menu-enter"); pane.state = "exiting"; }
      else {
        order.push("enter");
        if (pane.state === "shell" && pane.typed.length) {
          pane.state = options.neverBoots ? "booting" : "fresh";
          if (!options.neverBoots && !options.noSessionStart) db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "fresh-session", cwd: PM_PATH });
        }
      }
    } else throw new Error(`Unexpected shell command: ${command}`);
    return { stdout, stderr: "" };
  };
  const relay = {
    observeSlotPane: async () => ({
      paneId: "%7",
      text: pane.state === "shell" ? SHELL_TEXT : pane.state === "menu" ? EXIT_MENU_TEXT : COMPOSER_TEXT,
    }),
    getSlotActivityState: async () => pane.state === "idle" || pane.state === "fresh" ? "idle" : pane.state === "busy" ? "active" : "unknown",
    deliverGuardedToSlot: async (slot: number, text: string, guard: any) => {
      assert.equal(slot, 0);
      assert.equal(guard.expectedPaneId, "%7");
      assert.equal(guard.bracketedPaste, true);
      if (await guard.recheck("pre_paste", async () => null) || guard.finalCheck?.("pre_enter")) {
        return { outcome: "refused_pre_effect", reason: "fence", paneId: "%7" };
      }
      order.push("continuation");
      pane.sends.push(text);
      return { outcome: "delivered", paneId: "%7" };
    },
    drainPMQueue: async () => { order.push("drain"); },
    injectToPM: () => true,
  };
  const health = new ProcessHealthChecker(db, relay as any) as any;
  health.runShell = runShell;
  health.PM_RESTART_POLL_MS = 2;
  health.PM_EXIT_SHELL_TIMEOUT_MS = 200;
  health.PM_FORCE_SHELL_TIMEOUT_MS = 100;
  health.PM_PROMPT_TIMEOUT_MS = 100;
  health.pmAgentProcess = async () => "4000:4001";
  health.getPaneCommand = async () => {
    pane.polls++;
    pane.onPoll?.(pane.polls);
    // Claude takes a few polls to finish exiting once nothing is blocking it (the menu was answered, or none appeared).
    if (pane.state === "exiting" && pane.polls >= 3) pane.state = "shell";
    return pane.state === "shell" || pane.state === "booting" ? "zsh" : "claude";
  };
  return { db, pane, order, health, relay };
}

function armPmOnlyEpisode(db: MoPDatabase, prompt?: string): string {
  db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "old-session", cwd: PM_PATH });
  db.setExitPending(true, false, true, prompt);
  const episode = db.getExitStatus().episode!;
  db.logEvent(0, "exit_pending_triggered", "Stop", null, { source: "pm_only_stop", delivery: "delivered", episode });
  return episode;
}

const launchTyped = (pane: { typed: string[] }) => pane.typed.map((cmd) => cmd.replace(/^tmux send-keys -l -t %7 /, ""));
const LAUNCH = `'cd '\\''${PM_PATH}'\\'' && bash /Users/rajiv/.claude/scripts/launch-pm.sh --via cliproxy --model claude-opus-5-5'`;

test("launcher contract: fresh start, configured model, default opus, never --continue", () => {
  assert.equal(pmFreshLaunchCommand("opus"), "bash /Users/rajiv/.claude/scripts/launch-pm.sh --via cliproxy --model claude-opus-5-5");
  assert.equal(pmFreshLaunchCommand("deepseek"), "bash /Users/rajiv/.claude/scripts/launch-pm.sh --via cliproxy --model deepseek");
  assert.equal(pmFreshLaunchCommand("bad; rm -rf /"), pmFreshLaunchCommand("opus"), "malformed model never reaches the shell");
  assert.equal(RESTART_COMMANDS[0], pmFreshLaunchCommand("opus"));
  assert.doesNotMatch(RESTART_COMMANDS[0], /--continue/);
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  try {
    assert.equal(pmConfiguredModel(db), "opus");
    db.setConfig(PM_MODEL_CONFIG_KEY, "deepseek");
    assert.equal(pmConfiguredModel(db), "deepseek");
    db.setConfig(PM_MODEL_CONFIG_KEY, "x y");
    assert.equal(pmConfiguredModel(db), "opus");
  } finally { db.close(); }
});

test("zsh prompt detection reads the pane: jreese prompt yes; Claude composer, output, dead pane no", () => {
  assert.equal(isShellPromptVisible(SHELL_TEXT), true);
  assert.equal(isShellPromptVisible("rajiv@heydonna ~/Downloads/projects/heydonna-app ‹main› »"), true);
  assert.equal(isShellPromptVisible(COMPOSER_TEXT), false);
  assert.equal(isShellPromptVisible("Resuming...\n"), false);
  assert.equal(isShellPromptVisible("Pane is dead (status 0, Tue Oct 13 22:40:33 2026)"), false);
  assert.equal(isShellPromptVisible("$ bash launch-pm-cliproxy.sh"), false, "typed input is not an empty prompt");
  assert.equal(isShellPromptVisible(""), false);
});

// The whole contract, in order: exit menu -> 1 -> Enter (selection confirmed) -> shell -> launch command -> continuation.
for (const prompt of [undefined, "Read /saved/resume.md and resume."]) {
  test(`exit flow step order with the exit menu: 1, Enter, launch, ${prompt ? "saved" : "standard"} continuation, once`, async () => {
    const h = harness({ state: "menu" });
    try {
      const episode = armPmOnlyEpisode(h.db, prompt);
      const result = await h.health.restartPMFresh("exit", episode);
      assert.equal(result.success, true, result.reason);
      assert.deepEqual(h.order, ["1", "menu-enter", "type", "enter", "continuation", "drain"]);
      assert.deepEqual(launchTyped(h.pane), [LAUNCH]);
      assert.equal(h.pane.shell.some((cmd) => cmd.includes("--continue")), false);
      assert.deepEqual(h.pane.sends, [prompt ?? PM_RESTART_CONTINUATION_PROMPT]);
      const status = h.db.getExitStatus();
      assert.equal(status.continuation_delivery, "delivered");
      assert.equal(status.pending, false, "the PM-only episode ends with its continuation");
      assert.equal(status.dev_only, false, "dev slots are never armed by a PM-only restart");
      assert.equal(h.db.getEvents(0, 1, "pm_restart_failed").length, 0);
      // A duplicate trigger never resends.
      await h.health.restartPMFresh("exit", episode);
      assert.equal(h.pane.sends.length, 1);
    } finally { h.db.close(); }
  });
}

test("exit flow without an exit menu: nothing is typed until the shell prompt, then launch and continuation", async () => {
  const h = harness({ state: "exiting" });
  try {
    const episode = armPmOnlyEpisode(h.db);
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, true, result.reason);
    assert.ok(h.pane.polls >= 3);
    assert.deepEqual(h.order, ["type", "enter", "continuation", "drain"], "no 1 or Enter without a menu");
    assert.deepEqual(h.pane.sends, [PM_RESTART_CONTINUATION_PROMPT]);
  } finally { h.db.close(); }
});

// Cause of the 16:21 IST 18 Nov failure: a single-shot pane identity probe aborted the restart before it launched anything.
// A transient probe failure only delays the next key; a probe that never answers sends nothing blind (finding 2).
test("a transient tmux identity probe failure never aborts the exit flow", async () => {
  const h = harness({ state: "menu", failIdentityProbe: 2 });
  try {
    const episode = armPmOnlyEpisode(h.db);
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, true, result.reason);
    assert.deepEqual(h.order, ["1", "menu-enter", "type", "enter", "continuation", "drain"]);
  } finally { h.db.close(); }
});

test("a pane that cannot be identified gets no menu key and no launcher: bounded wait, nothing sent", async () => {
  const h = harness({ state: "menu", failIdentityProbe: Infinity });
  try {
    const episode = armPmOnlyEpisode(h.db);
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, false);
    assert.deepEqual(h.order, []);
    assert.deepEqual(h.pane.sends, []);
  } finally { h.db.close(); }
});

// CTO REVISE ts 1795004139.496649, finding 2: the menu is read from the visible screen inside the send lock.
test("a menu that is only in scrollback is never answered: no key is sent when the visible screen has no menu", async () => {
  const h = harness({ state: "menu" });
  try {
    h.pane.visible = COMPOSER_TEXT;
    const episode = armPmOnlyEpisode(h.db);
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, false);
    assert.deepEqual(h.order, [], "no 1, no Enter");
    assert.equal(h.db.getConfig("exit_pending_restart_steps") ?? "", "");
  } finally { h.db.close(); }
});

test("a menu key is recorded durably once: the same episode never answers the menu twice", async () => {
  // neverBoots keeps the episode open: a completed episode resets its step list with the rest of the exit state.
  const h = harness({ state: "menu", neverBoots: true });
  try {
    const episode = armPmOnlyEpisode(h.db);
    await h.health.restartPMFresh("exit", episode);
    assert.equal(h.db.getConfig("exit_pending_restart_steps"), "menu_1,menu_Enter,launch,launched");
    // A restarted MoP process (empty in-memory state) sees the menu again: the recorded keys are not repeated.
    h.pane.state = "menu";
    const before = h.order.length;
    await h.health.answerPMExitMenu("%7", episode, new Set());
    assert.equal(h.order.length, before);
    // A new episode starts clean.
    h.db.setExitPending(true, false, true);
    assert.equal(h.db.getConfig("exit_pending_restart_steps") ?? "", "");
  } finally { h.db.close(); }
});

test("launcher: a half-typed draft at the prompt is never joined to the launcher", async () => {
  const h = harness({ state: "shell" });
  try {
    h.pane.visible = "$ half typed draft\n";
    const result = await h.health.restartPMFresh("crash");
    assert.equal(result.success, false);
    assert.match(result.reason, /pm_launch_prompt_not_empty/);
    assert.deepEqual(h.order, [], "nothing typed, nothing submitted");
  } finally { h.db.close(); }
});

test("launcher: a draft appended after the launcher was typed is never submitted", async () => {
  const h = harness({ state: "shell" });
  try {
    h.pane.appended = " && rm draft";
    const result = await h.health.restartPMFresh("crash");
    assert.equal(result.success, false);
    assert.match(result.reason, /pm_launch_text_not_owned/);
    assert.deepEqual(h.order, ["type"], "typed once, Enter withheld");
    assert.deepEqual(h.pane.sends, []);
  } finally { h.db.close(); }
});

test("launcher steps are recorded durably before they happen: a restarted MoP never retypes or resubmits", async () => {
  const h = harness({ state: "shell", neverBoots: true });
  try {
    const episode = armPmOnlyEpisode(h.db);
    await h.health.restartPMFresh("exit", episode);
    assert.equal(h.db.getConfig("exit_pending_restart_steps"), "launch,launched");
    h.pane.state = "shell";
    const before = h.order.length;
    assert.equal(await h.health.sendPMLaunch("%7", "cd /x && y", episode), "launch_already_attempted");
    assert.equal(h.order.length, before, "no second type, no second Enter");
  } finally { h.db.close(); }
});

test("exit flow times out waiting for the shell: one pm_restart_failed, nothing typed, no force, episode still pending", async () => {
  const h = harness({ state: "idle" });
  try {
    const episode = armPmOnlyEpisode(h.db);
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, false);
    assert.equal(result.reason, "exit_blocked_shell_not_observed_no_force");
    assert.deepEqual(h.order, []);
    assert.equal(h.db.getEvents(0, 5, "pm_restart_failed").length, 1);
    assert.equal(h.db.getExitStatus().pending, true);
    assert.deepEqual(h.pane.sends, []);
  } finally { h.db.close(); }
});

test("the launched Claude never appears: bounded wait, one pm_restart_failed, no continuation typed into the shell", async () => {
  const h = harness({ state: "shell", neverBoots: true });
  try {
    const result = await h.health.restartPMFresh("crash");
    assert.equal(result.success, false);
    assert.match(result.reason, /claude process not observed/);
    assert.deepEqual(h.pane.sends, []);
    assert.equal(h.db.getEvents(0, 5, "pm_restart_failed").length, 1);
  } finally { h.db.close(); }
});

test("crash recovery (no exit episode) uses the same fresh launch and one standard continuation per boot", async () => {
  const h = harness({ state: "shell" });
  try {
    h.db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "old-session", cwd: PM_PATH });
    const result = await h.health.restartPMFresh("crash");
    assert.equal(result.success, true, result.reason);
    assert.deepEqual(h.order, ["type", "enter", "continuation", "drain"]);
    assert.deepEqual(h.pane.sends, [PM_RESTART_CONTINUATION_PROMPT]);
    // Same boot and same live process, second attempt: the per-boot claim refuses a resend.
    const boot = h.db.getEvents(0, 1, "SessionStart")[0].id;
    assert.equal(await h.health.deliverPMRestartContinuation("%7", boot, "crash", null, "4000:4001"), "already_claimed");
    assert.equal(h.pane.sends.length, 1);
  } finally { h.db.close(); }
});

// CTO REVISE ts 1795004139.496649, finding 1: the destination is the live replacement Claude process in the launched pane
// plus the guarded composer. SessionStart is evidence only; it neither gates nor satisfies the delivery.
test("a late or missing SessionStart never delays or drops the continuation", async () => {
  const h = harness({ state: "shell", noSessionStart: true });
  try {
    const episode = armPmOnlyEpisode(h.db, "saved prompt");
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, true, result.reason);
    assert.deepEqual(h.order, ["type", "enter", "continuation", "drain"]);
    assert.deepEqual(h.pane.sends, ["saved prompt"]);
    assert.equal(h.db.getExitStatus().pending, false);
  } finally { h.db.close(); }
});

test("a different live Claude process in the pane is never the destination: nothing is pasted, the claim stays consumed", async () => {
  const h = harness({ state: "shell" });
  try {
    const episode = armPmOnlyEpisode(h.db, "saved prompt");
    let probes = 0;
    h.health.pmAgentProcess = async () => (++probes === 1 ? "4000:4001" : "4000:4999");
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, false);
    assert.match(result.reason, /continuation_refused_pre_effect/);
    assert.deepEqual(h.pane.sends, []);
    assert.equal(h.db.getExitStatus().continuation_delivery, "attempted", "no automatic resend into another process");
  } finally { h.db.close(); }
});

test("a process probe that cannot name the Claude yet waits within the bound, then delivers once", async () => {
  const h = harness({ state: "shell" });
  try {
    const episode = armPmOnlyEpisode(h.db, "saved prompt");
    let probes = 0;
    h.health.pmAgentProcess = async () => (++probes === 1 || probes > 3 ? "4000:4001" : null);
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, true, result.reason);
    assert.deepEqual(h.pane.sends, ["saved prompt"]);
    assert.equal(h.db.getExitStatus().pending, false);
  } finally { h.db.close(); }
});

// Finding 3: the first health tick after a MoP restart settles or resumes the PM-only episode (slot 0 only).
const confirmReceipt = (db: MoPDatabase, episode: string) =>
  db.logEvent(0, "pm_restart_continuation", null, null, { episode, trigger: "exit", boot_event_id: 1, delivery: "delivered" });

test("resume tick: a confirmed continuation receipt settles the episode through the PM-only completion, nothing is resent", async () => {
  const h = harness({ state: "fresh" });
  try {
    const episode = armPmOnlyEpisode(h.db, "saved prompt");
    h.db.setConfig("exit_pending_continuation_delivery", "attempted");
    confirmReceipt(h.db, episode);
    await h.health.resumePMExitEpisode(h.db.getExitStatus());
    assert.equal(h.db.getExitStatus().pending, false);
    assert.equal(h.db.getEvents(0, 5, "exit_pending_complete").length, 1);
    assert.deepEqual(h.pane.sends, []);
    assert.deepEqual(h.order, []);
  } finally { h.db.close(); }
});

test("resume tick: a receipt for another episode never settles this one", async () => {
  const h = harness({ state: "fresh" });
  try {
    const episode = armPmOnlyEpisode(h.db, "saved prompt");
    h.db.setConfig("exit_pending_continuation_delivery", "attempted");
    confirmReceipt(h.db, `${episode}-other`);
    await h.health.resumePMExitEpisode(h.db.getExitStatus());
    assert.equal(h.db.getExitStatus().pending, true);
    assert.deepEqual(h.pane.sends, []);
  } finally { h.db.close(); }
});

test("resume tick: an attempted send with no confirmed receipt stays consumed", async () => {
  const h = harness({ state: "fresh" });
  try {
    const episode = armPmOnlyEpisode(h.db, "saved prompt");
    h.db.setConfig("exit_pending_restart_steps", "launch,launched");
    h.db.setConfig("exit_pending_continuation_delivery", "attempted");
    await h.health.resumePMExitEpisode(h.db.getExitStatus());
    const status = h.db.getExitStatus();
    assert.equal(status.pending, true);
    assert.equal(status.continuation_delivery, "attempted");
    assert.equal(status.episode, episode);
    assert.deepEqual(h.pane.sends, []);
  } finally { h.db.close(); }
});

test("resume tick: a recorded launch whose continuation was never attempted resumes the unsent step on the live Claude", async () => {
  const h = harness({ state: "fresh" });
  try {
    armPmOnlyEpisode(h.db, "saved prompt");
    h.db.setConfig("exit_pending_restart_steps", "launch,launched");
    await h.health.resumePMExitEpisode(h.db.getExitStatus());
    assert.deepEqual(h.pane.sends, ["saved prompt"]);
    assert.equal(h.db.getExitStatus().pending, false);
    // A further tick finds a settled episode: nothing more is sent.
    await h.health.resumePMExitEpisode(h.db.getExitStatus());
    assert.equal(h.pane.sends.length, 1);
  } finally { h.db.close(); }
});

test("resume tick: no recorded launch, or no live Claude in the pane, resumes nothing", async () => {
  for (const [steps, process] of [["launch", "4000:4001"], ["launch,launched", "4000:"], ["launch,launched", null]] as const) {
    const h = harness({ state: "fresh" });
    try {
      armPmOnlyEpisode(h.db, "saved prompt");
      h.db.setConfig("exit_pending_restart_steps", steps);
      h.health.pmAgentProcess = async () => process;
      await h.health.resumePMExitEpisode(h.db.getExitStatus());
      assert.deepEqual(h.pane.sends, [], `${steps} / ${process}`);
      assert.equal(h.db.getExitStatus().continuation_delivery, "waiting");
    } finally { h.db.close(); }
  }
});

test("resume tick: skipped while this process runs the restart flow itself", async () => {
  const h = harness({ state: "fresh" });
  try {
    armPmOnlyEpisode(h.db, "saved prompt");
    h.db.setConfig("exit_pending_restart_steps", "launch,launched");
    h.health.pmRestartInFlight = Promise.resolve();
    await h.health.resumePMExitEpisode(h.db.getExitStatus());
    assert.deepEqual(h.pane.sends, []);
  } finally { h.db.close(); }
});

test("configured model and launcher reach the typed command", async () => {
  for (const [config, pattern] of [
    [[PM_MODEL_CONFIG_KEY, "deepseek"], /launch-pm\.sh --via cliproxy --model deepseek'$/],
    [["pm_launcher", "direct"], /launch-pm\.sh --via direct --model claude-opus-5-5'$/],
  ] as const) {
    const h = harness({ state: "shell" });
    try {
      h.db.setConfig(config[0], config[1]);
      const result = await h.health.restartPMFresh("crash");
      assert.equal(result.success, true, result.reason);
      assert.match(launchTyped(h.pane)[0], pattern);
    } finally { h.db.close(); }
  }
});

test("health tick on a dead PM pane starts the fresh restart, never the --continue launcher", async () => {
  const h = harness({ state: "shell" });
  try {
    (h.health as any).startTime = 0;
    for (let slot = 1; slot <= 8; slot++) h.db.updateSlot(slot, { dnd: true });
    h.health.isSlotProvisioned = (slot: number) => slot === 0;
    await h.health.checkAll();
    await h.health.pmRestartInFlight;
    assert.deepEqual(h.pane.sends, [PM_RESTART_CONTINUATION_PROMPT]);
    assert.equal(launchTyped(h.pane).length, 1);
    assert.equal(h.pane.shell.some((cmd) => cmd.includes("--continue")), false);
    // The completed restart seeds the cooldown: the next tick launches nothing.
    await h.health.checkAll();
    assert.equal(launchTyped(h.pane).length, 1);
  } finally { h.db.close(); }
});

test("concurrent triggers join one restart: one launcher, one continuation", async () => {
  const h = harness({ state: "menu" });
  try {
    const episode = armPmOnlyEpisode(h.db);
    const [a, b] = await Promise.all([h.health.restartPMFresh("exit", episode), h.health.restartPMFresh("crash")]);
    assert.equal(a, b);
    assert.equal(launchTyped(h.pane).length, 1);
    assert.equal(h.pane.sends.length, 1);
  } finally { h.db.close(); }
});

test("04:00 with_devs episode: the restart delivers the continuation and ends the episode when devs are done", async () => {
  const h = harness({ state: "shell" });
  try {
    h.db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "old-session", cwd: PM_PATH });
    h.db.setExitPending(true, false, true, "Read /saved/resume.md.", true, true);
    const episode = h.db.getExitStatus().episode!;
    h.db.logEvent(0, "exit_pending_triggered", "Stop", null, { source: "pm_only_stop", delivery: "delivered", episode });
    for (let i = 1; i <= 8; i++) h.db.markSlotExitCycled(i);
    (h.relay as any).hasPendingExitConfirmation = () => false;
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, true, result.reason);
    assert.deepEqual(h.pane.sends, ["Read /saved/resume.md."]);
    assert.equal(h.db.getExitStatus().pending, false, "single episode complete; no second dev phase");
  } finally { h.db.close(); }
});

test("crash/unresponsive never kills a running agent without proof: busy PM keeps its turn, idle PM is respawned", async () => {
  const busy = harness({ state: "busy" });
  try {
    const result = await busy.health.restartPMFresh("unresponsive");
    assert.equal(result.reason, "shell_prompt_timeout_pm_busy_no_force");
    assert.equal(busy.pane.shell.some((cmd) => cmd.startsWith("tmux respawn-pane")), false);
    assert.deepEqual(busy.order, []);
  } finally { busy.db.close(); }
  const crashed = harness({ state: "idle" });
  try {
    const result = await crashed.health.restartPMFresh("crash");
    assert.equal(result.reason, "shell_prompt_timeout_agent_running_no_force");
    assert.deepEqual(crashed.order, []);
  } finally { crashed.db.close(); }
  const idle = harness({ state: "idle" });
  try {
    const result = await idle.health.restartPMFresh("unresponsive");
    assert.equal(result.success, true, result.reason);
    assert.deepEqual(idle.order, ["respawn", "type", "enter", "continuation", "drain"]);
    assert.equal(idle.db.getEvents(0, 1, "pm_restart_force_respawn").length, 1);
  } finally { idle.db.close(); }
});

test("PM-only Stop hands its consumed /exit to the fresh-restart handler exactly once", async () => {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  const calls: Array<[string | null, string]> = [];
  try {
    db.setExitPending(true, false, true);
    const relay = {
      observeSlotPane: async () => ({ paneId: "%7" }),
      deliverGuardedToSlot: async () => ({ outcome: "delivered" }),
      injectToPM() {}, sendToSlot() { throw Error("must not send to a dev"); },
    };
    const processor = new HookProcessor(db, relay as any) as any;
    processor.pmExitRestartHandler = (episode: string | null, delivery: string) => calls.push([episode, delivery]);
    db.logEvent(0, "SessionStart", "SessionStart", null, { session_id: "pm" });
    await processor.process(0, { type: "Stop", session_id: "pm" });
    await processor.process(0, { type: "Stop", session_id: "pm" });
    assert.deepEqual(calls, [[db.getExitStatus().episode, "delivered"]]);
  } finally { db.close(); }
});

test("a refused /exit never starts a restart", () => {
  const h = harness({ state: "shell" });
  try {
    h.health.onPmExitDispatched("ep", "refused_pre_effect");
    assert.equal(h.health.pmRestartInFlight, null);
  } finally { h.db.close(); }
});
