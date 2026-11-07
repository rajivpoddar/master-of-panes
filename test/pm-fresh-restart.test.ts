// PM restart contract (Rajiv, DM D0AMF0XE6TS thread 1793979986.975849, ts 1793988738.009889 /
// 1793989226.288339): /exit -> wait for the zsh prompt (pane read, bounded; respawn-pane is the
// fallback) -> FRESH configured-model launcher (no --continue) -> wait for the Claude input prompt
// -> ONE continuation through the guarded writer. Crash recovery uses the same path.
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

type PaneState = "busy" | "idle" | "exiting" | "printing" | "shell" | "booting" | "fresh";

function harness(options: { state?: PaneState; exitAfterPolls?: number; bootSessionStart?: boolean; neverBoots?: boolean } = {}) {
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  const pane = { state: options.state ?? "exiting" as PaneState, polls: 0, typed: [] as string[], shell: [] as string[], sends: [] as string[],
    pid: "500", child: "501", onPoll: undefined as undefined | ((polls: number) => void) };
  const order: string[] = [];
  const runShell = async (command: string) => {
    pane.shell.push(command);
    let stdout = "";
    if (command.startsWith("tmux display-message") && command.includes("#{pane_pid}")) stdout = `${pane.pid}\n`;
    else if (command.startsWith("tmux display-message")) stdout = `%7|${PM_PATH}\n`;
    else if (command.startsWith("pgrep -P")) {
      const alive = !["shell", "printing", "booting"].includes(pane.state);
      if (!alive || !pane.child) throw new Error("no child");
      stdout = `${pane.child}\n`;
    }
    else if (command.startsWith("git -C")) stdout = `${PM_PATH}\n`;
    else if (command.startsWith("tmux respawn-pane")) { pane.state = "shell"; pane.pid = "600"; order.push("respawn"); }
    else if (command.startsWith("tmux send-keys -l")) { pane.typed.push(command); order.push("type"); }
    else if (command.startsWith("tmux send-keys") && command.endsWith(" Enter")) {
      order.push("enter");
      if (pane.state === "shell" && pane.typed.length) {
        pane.state = options.neverBoots ? "booting" : "fresh";
        if (!options.neverBoots && options.bootSessionStart !== false) {
          db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "fresh-session", cwd: PM_PATH });
        }
      }
    } else throw new Error(`Unexpected shell command: ${command}`);
    return { stdout, stderr: "" };
  };
  const relay = {
    observeSlotPane: async () => ({
      paneId: "%7",
      text: pane.state === "shell" ? SHELL_TEXT : pane.state === "printing" ? "Resuming...\n" : COMPOSER_TEXT,
    }),
    getSlotActivityState: async () => pane.state === "idle" || pane.state === "fresh" ? "idle"
      : pane.state === "busy" ? "active" : "unknown",
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
  health.PM_BOOT_EVENT_TIMEOUT_MS = 50;
  health.getPaneCommand = async () => {
    pane.polls++;
    pane.onPoll?.(pane.polls);
    if (pane.state === "exiting" && pane.polls >= (options.exitAfterPolls ?? 3)) pane.state = "shell";
    if (pane.state === "printing" && pane.polls >= (options.exitAfterPolls ?? 3)) pane.state = "shell";
    return pane.state === "shell" || pane.state === "printing" ? "zsh" : pane.state === "booting" ? "zsh" : "claude";
  };
  return { db, pane, order, health, relay };
}

function armPmOnlyEpisode(db: MoPDatabase, prompt?: string, delivery = "delivered") {
  db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "old-session", cwd: PM_PATH });
  db.setExitPending(true, false, true, prompt);
  db.markSlotExitCycled(0);
  const episode = db.getExitStatus().episode;
  db.logEvent(0, "exit_pending_triggered", "Stop", null, { source: "pm_only_stop", delivery, episode });
  return episode;
}

const launchTyped = (pane: { typed: string[] }) => pane.typed.map((cmd) => cmd.replace(/^tmux send-keys -l -t %7 /, ""));

test("launcher contract: fresh start, configured model, default opus, never --continue", () => {
  assert.equal(pmFreshLaunchCommand("opus"), "bash /Users/rajiv/.claude/scripts/launch-pm-cliproxy.sh --model claude-opus-5-5");
  assert.equal(pmFreshLaunchCommand("deepseek"), "bash /Users/rajiv/.claude/scripts/launch-pm-cliproxy.sh --model deepseek");
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

for (const prompt of [undefined, "Read /saved/resume.md and resume."]) {
  test(`/exit episode: waits for the zsh prompt, launches fresh, injects ${prompt ? "the saved" : "the standard"} continuation exactly once`, async () => {
    const h = harness({ state: "printing", exitAfterPolls: 4 });
    try {
      const episode = armPmOnlyEpisode(h.db, prompt);
      const result = await h.health.restartPMFresh("exit", episode);
      assert.equal(result.success, true, result.reason);
      // Nothing was typed before the pane showed the shell prompt.
      assert.ok(h.pane.polls >= 4);
      assert.deepEqual(launchTyped(h.pane), [`'cd '\\''${PM_PATH}'\\'' && bash /Users/rajiv/.claude/scripts/launch-pm-cliproxy.sh --model claude-opus-5-5'`]);
      assert.equal(h.pane.shell.some((cmd) => cmd.includes("--continue")), false);
      assert.deepEqual(h.pane.sends, [prompt ?? PM_RESTART_CONTINUATION_PROMPT]);
      assert.deepEqual(h.order, ["type", "enter", "continuation", "drain"]);
      const status = h.db.getExitStatus();
      assert.equal(status.continuation_delivery, "delivered");
      assert.equal(status.dev_only, true, "devs armed only after the PM continuation");
      // A later health tick or duplicate trigger never resends.
      await h.health.reconcilePMRestartContinuation();
      await h.health.restartPMFresh("exit", episode);
      assert.equal(h.pane.sends.length, 1);
    } finally { h.db.close(); }
  });
}

test("configured deepseek model is passed to the launcher", async () => {
  const h = harness({ state: "shell" });
  try {
    h.db.setConfig(PM_MODEL_CONFIG_KEY, "deepseek");
    const result = await h.health.restartPMFresh("crash");
    assert.equal(result.success, true, result.reason);
    assert.match(launchTyped(h.pane)[0], /launch-pm-cliproxy\.sh --model deepseek'$/);
  } finally { h.db.close(); }
});

test("crash recovery (no exit episode) uses the same fresh launch and one standard continuation per boot", async () => {
  const h = harness({ state: "shell" });
  try {
    h.db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "old-session", cwd: PM_PATH });
    const result = await h.health.restartPMFresh("crash");
    assert.equal(result.success, true, result.reason);
    assert.deepEqual(h.pane.sends, [PM_RESTART_CONTINUATION_PROMPT]);
    // Same boot, second attempt: the per-boot claim refuses a resend.
    assert.equal(await h.health.deliverPMFreshContinuation("%7", h.db.getEvents(0, 1, "SessionStart")[0].id, "crash"), "already_claimed");
    assert.equal(h.pane.sends.length, 1);
    assert.equal(h.db.getEvents(0, 1, "process_restarted").length, 1);
  } finally { h.db.close(); }
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
  const h = harness({ state: "exiting", exitAfterPolls: 3 });
  try {
    const episode = armPmOnlyEpisode(h.db);
    const [a, b] = await Promise.all([h.health.restartPMFresh("exit", episode), h.health.restartPMFresh("crash")]);
    assert.equal(a, b);
    assert.equal(launchTyped(h.pane).length, 1);
    assert.equal(h.pane.sends.length, 1);
  } finally { h.db.close(); }
});

test("zsh prompt timeout with an idle PM takes the force path: respawn-pane to a shell, then fresh launch + continuation", async () => {
  const h = harness({ state: "idle" });
  try {
    const episode = armPmOnlyEpisode(h.db, undefined, "uncertain");
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, true, result.reason);
    const respawn = h.pane.shell.find((cmd) => cmd.startsWith("tmux respawn-pane"));
    assert.equal(respawn, `tmux respawn-pane -k -t %7 -c '${PM_PATH}'`, "fallback respawns a shell, not a --continue launcher");
    assert.deepEqual(h.order, ["respawn", "type", "enter", "continuation", "drain"]);
    assert.equal(h.db.getEvents(0, 1, "pm_restart_force_respawn").length, 1);
    assert.deepEqual(h.pane.sends, [PM_RESTART_CONTINUATION_PROMPT]);
  } finally { h.db.close(); }
});

test("zsh prompt timeout with a BUSY PM never kills the turn: no force, no launcher, no continuation", async () => {
  const h = harness({ state: "busy" });
  try {
    const episode = armPmOnlyEpisode(h.db);
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, false);
    assert.equal(result.reason, "shell_prompt_timeout_pm_busy_no_force");
    assert.deepEqual(h.order, []);
    assert.equal(h.pane.shell.some((cmd) => cmd.startsWith("tmux respawn-pane")), false);
    // The fence is released so the health tick owns the later exit.
    assert.equal(h.health.isPmInitiatedRespawn(0), false);
  } finally { h.db.close(); }
});

test("Claude prompt never appears: bounded wait, no continuation typed into the shell", async () => {
  const h = harness({ state: "shell", neverBoots: true });
  try {
    const result = await h.health.restartPMFresh("crash");
    assert.equal(result.success, false);
    assert.match(result.reason, /claude prompt not observed/);
    assert.deepEqual(h.pane.sends, []);
  } finally { h.db.close(); }
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

test("crash restart never force-kills an agent that is running when the prompt wait expires", async () => {
  const h = harness({ state: "idle" });
  try {
    const result = await h.health.restartPMFresh("crash");
    assert.equal(result.reason, "shell_prompt_timeout_agent_running_no_force");
    assert.deepEqual(h.order, []);
  } finally { h.db.close(); }
});

test("04:00 with_devs episode: the fresh PM restart delivers the continuation and ends the episode when devs are done", async () => {
  const h = harness({ state: "shell" });
  try {
    h.db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "old-session", cwd: PM_PATH });
    h.db.setExitPending(true, false, true, "Read /saved/resume.md.", true, true);
    const episode = h.db.getExitStatus().episode;
    for (let i = 0; i <= 8; i++) h.db.markSlotExitCycled(i);
    h.db.logEvent(0, "exit_pending_triggered", "Stop", null, { source: "pm_only_stop", delivery: "delivered", episode });
    (h.relay as any).hasPendingExitConfirmation = () => false;
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, true, result.reason);
    assert.deepEqual(h.pane.sends, ["Read /saved/resume.md."]);
    assert.deepEqual(h.order, ["type", "enter", "continuation", "drain"]);
    assert.equal(h.db.getExitStatus().pending, false, "single episode complete; no second dev phase");
  } finally { h.db.close(); }
});

test("pm_launcher=direct: the same fresh restart types launch-pm.sh, then one continuation", async () => {
  const h = harness({ state: "shell" });
  try {
    h.db.setConfig("pm_launcher", "direct");
    const result = await h.health.restartPMFresh("crash");
    assert.equal(result.success, true, result.reason);
    assert.match(launchTyped(h.pane)[0], /&& bash \/Users\/rajiv\/\.claude\/scripts\/launch-pm\.sh --model claude-opus-5-5'$/);
    assert.deepEqual(h.pane.sends, [PM_RESTART_CONTINUATION_PROMPT]);
    assert.equal(JSON.parse(h.db.getEvents(0, 1, "pm_restart_started")[0].payload).launcher, "direct");
  } finally { h.db.close(); }
});

// CTO REVISE ts 1794018596.287469: ownership and busy protection for EVERY trigger.
test("unresponsive trigger never kills a BUSY PM (an unchanged screen is not proof of idle)", async () => {
  const h = harness({ state: "busy" });
  try {
    const result = await h.health.restartPMFresh("unresponsive");
    assert.equal(result.reason, "shell_prompt_timeout_pm_busy_no_force");
    assert.equal(h.pane.shell.some((cmd) => cmd.startsWith("tmux respawn-pane")), false);
    assert.deepEqual(h.order, []);
  } finally { h.db.close(); }
});

test("a different PM process than the one pinned at the trigger is never killed", async () => {
  const h = harness({ state: "idle" });
  try {
    const episode = armPmOnlyEpisode(h.db);
    h.pane.onPoll = (polls) => { if (polls === 2) h.pane.child = "777"; };
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.reason, "no_force:pm_process_changed");
    assert.equal(h.pane.shell.some((cmd) => cmd.startsWith("tmux respawn-pane")), false);
  } finally { h.db.close(); }
});

test("a successor PM boot during the wait stops the restart: no kill, no launcher, no continuation", async () => {
  const h = harness({ state: "idle" });
  try {
    const episode = armPmOnlyEpisode(h.db);
    h.pane.onPoll = (polls) => {
      if (polls === 2) h.db.logEvent(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "successor", cwd: PM_PATH });
    };
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.reason, "no_force:pm_session_changed");
    assert.deepEqual(h.order, []);
    assert.deepEqual(h.pane.sends, []);
  } finally { h.db.close(); }
});

test("only the FIRST boot after the launch is this launch; a successor boot gets no continuation", async () => {
  const h = harness({ state: "shell" });
  try {
    const episode = armPmOnlyEpisode(h.db);
    const origLog = h.db.logEvent.bind(h.db);
    // The launch boots, then a second (successor) SessionStart lands before delivery.
    (h.db as any).logEvent = (slot: number, type: string, hook: any, tool: any, payload: any) => {
      const id = origLog(slot, type, hook, tool, payload);
      if (type === "SessionStart" && payload.session_id === "fresh-session") {
        origLog(0, "SessionStart", "SessionStart", null, { source: "startup", session_id: "successor", cwd: PM_PATH });
      }
      return id;
    };
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.reason, "successor_boot_after_launch");
    assert.deepEqual(h.pane.sends, []);
    assert.equal(h.db.getExitStatus().continuation_delivery, "waiting");
  } finally { h.db.close(); }
});

test("an exit restart writes the durable launch record that binds its boot", async () => {
  const h = harness({ state: "exiting" });
  try {
    const episode = armPmOnlyEpisode(h.db);
    const result = await h.health.restartPMFresh("exit", episode);
    assert.equal(result.success, true, result.reason);
    const record = JSON.parse(h.db.getConfig("pm_restart_launch")!);
    assert.equal(record.episode, episode);
    assert.equal(record.pane_id, "%7");
    assert.ok(record.launch_event_id > 0);
  } finally { h.db.close(); }
});
