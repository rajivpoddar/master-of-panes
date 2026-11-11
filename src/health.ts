/**
 * MoP Process Health Checker — Auto-revival for dead Claude Code processes
 *
 * Checks all panes (PM + dev + QA) every 30 seconds.
 * If a pane's current command is a shell (zsh/bash) instead of "claude",
 * the Claude Code process has died. MoP restarts it with `--continue`.
 *
 * Detection: `tmux display-message -t PANE -p '#{pane_current_command}'`
 * Revival: `tmux send-keys -t PANE 'claude-xxx --continue' Enter`
 *
 * The --continue flag resumes the most recent session in the pane's cwd,
 * restoring conversation context including any in-progress work.
 *
 * PM restart (Rajiv, DM D0AMF0XE6TS thread 1793979986.975849, ts 1793988738.009889
 * and 1793989226.288339): "MoP injects /exit, waits for the zsh prompt, then injects
 * launch-pm.sh or launch-pm-cliproxy.sh with opus/deepseek. then injects the
 * continuation pompt at the claude prompt" ... "switching models with continue is not
 * probably a good idea. MoP should run the right launcher and then inject the
 * continuation prompt or point to the saved handoff file." The PM is therefore
 * relaunched FRESH (no --continue) with the configured model, and receives ONE
 * continuation prompt once its Claude input prompt is up. PM crash recovery uses
 * the same path. Dev slots keep `--continue` + "continue".
 */

import { createHash } from "node:crypto";
import { existsSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { execShell } from "./asyncCommand.js";
import type { MoPDatabase } from "./db.js";
import { recentJsonlActivity } from "./jsonlActivity.js";
import type { TmuxRelay } from "./relay.js";
import { DEV_SLOT_NUMBERS, RUNTIME_SLOT_NUMBERS, SLOT_RUNTIME_IDENTITIES } from "./slotConfig.js";
import { paneAddress, paneAddress as resolvePaneAddress, verifyPaneIdentity, type PaneCommandRunner } from "./paneIdentity.js";
import { pinFrom, pinDrift } from "./interruptBeforeClear.js";
import { latestSessionStartEventId } from "./sessionStartClearWait.js";
import { composerText } from "./composer.js";
import { withSlotSendLock } from "./slotSendLock.js";

// ─── Restart Commands ──────────────────────────────────────
// These are shell aliases defined in ~/.zshrc. Since tmux panes
// run interactive zsh shells, aliases are available after Claude exits.

// Use standalone scripts instead of shell aliases — eliminates .zshrc/OMZ dependency.
// Scripts are bash, not zsh, so no alias resolution issues.
// (Rajiv directive 2026-04-03: "use scripts to launch claude on the slots instead of aliases")
// PM and numbered slots use user-local Claude Code launchers. PM keeps CLIProxy
// routing during both crash recovery and explicit respawn; numbered wrappers
// remain independent of tracked OMP launcher cutovers.
export const SCRIPTS_DIR = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/scripts";
export const CLAUDE_SLOT_SCRIPTS_DIR = "/Users/rajiv/.claude/scripts";
export const CLAUDE_SLOT_RUNTIME_ENV = [
  "DEV_SLOT_SPARK_MODEL=ornith-1.5-35b-a3b",
  "DEV_SLOT_SPARK_MAX_CONTEXT_TOKENS=240000",
  "DEV_SLOT_SPARK_MAX_OUTPUT_TOKENS=32000",
  "DEV_SLOT_SPARK_MAX_THINKING_TOKENS=2048",
  "MAX_THINKING_TOKENS=2048",
].join(" ");
// ─── PM fresh-launch contract ───────────────────────────
/** MoP config key holding the PM model passed to the PM launcher. */
export const PM_MODEL_CONFIG_KEY = "pm_model";
/** Rajiv switched the PM pane to Opus (DM D0AMF0XE6TS ts 1794015590.986679). */
export const PM_DEFAULT_MODEL = "opus";
/**
 * Config value -> launcher `--model` argument. launch-pm.sh accepts
 * `--model <alias|id>`; the cliproxy route maps `deepseek` itself. `opus` is
 * pinned to the launcher's default id so Claude Code never resolves a bare
 * alias the CLIProxy route does not serve.
 */
const PM_MODEL_LAUNCH_ARGS: Record<string, string> = {
  opus: "claude-opus-5-5",
  deepseek: "deepseek",
};
export const PM_RESTART_CONTINUATION_PROMPT =
  "Session restarted. Continue PM work: read /Users/rajiv/Downloads/projects/heydonna-app/data/pm-continuation-note.md "
  + "and memory MEMORY-session-handoff-current.md, reconcile live pm-ops.db, MoP slots and open PRs, then resume the next actions.";

/** The configured PM model; an absent or malformed value falls back to the default. */
export function pmConfiguredModel(db: Pick<MoPDatabase, "getConfig">): string {
  let value = "";
  try { value = (db.getConfig(PM_MODEL_CONFIG_KEY) ?? "").trim(); } catch { value = ""; }
  return /^[A-Za-z0-9._-]{1,64}$/.test(value) ? value : PM_DEFAULT_MODEL;
}

/**
 * PM backend. One launcher, `launch-pm.sh --via cliproxy|direct` (Rajiv DM
 * D0AMF0XE6TS thread 1794015303.639509, ts 1794018254.090169: "for direct
 * claude, it has to use launch-pm.sh"; ts 1794019433.856239: "can we combine
 * the launcher script into one and make cliproxy or direct a param").
 * MoP config `pm_launcher`:
 * - `cliproxy` (default): the shared CLIProxyAPI route;
 * - `direct`: direct Claude (Max subscription, no proxy).
 * `--via` is ALWAYS passed explicitly, so no restart path can go direct by
 * omission. The model stays a separate key (`pm_model`). A non-Claude model
 * (deepseek) exists only on the proxy, so `direct` with it keeps cliproxy.
 * The fresh-start + continuation contract is identical for both.
 */
export const PM_LAUNCHER_CONFIG_KEY = "pm_launcher";
/** Durable record of the last MoP PM launch: binds the ONE boot it produced. */
export const PM_RESTART_LAUNCH_KEY = "pm_restart_launch";
export type PmLauncher = "cliproxy" | "direct";
export const PM_DEFAULT_LAUNCHER: PmLauncher = "cliproxy";

/** The configured PM backend; an absent or unknown value keeps cliproxy. */
export function pmConfiguredLauncher(db: Pick<MoPDatabase, "getConfig">): PmLauncher {
  let value = "";
  try { value = (db.getConfig(PM_LAUNCHER_CONFIG_KEY) ?? "").trim(); } catch { value = ""; }
  return value === "direct" ? "direct" : PM_DEFAULT_LAUNCHER;
}

/** Fresh PM launcher command (never --continue) for a model and backend. */
export function pmFreshLaunchCommand(model: string = PM_DEFAULT_MODEL, launcher: PmLauncher = PM_DEFAULT_LAUNCHER): string {
  const safe = /^[A-Za-z0-9._-]{1,64}$/.test(model) ? model : PM_DEFAULT_MODEL;
  const arg = PM_MODEL_LAUNCH_ARGS[safe] ?? safe;
  const effective: PmLauncher = launcher === "direct" && arg.startsWith("claude-") ? "direct" : "cliproxy";
  return `bash ${CLAUDE_SLOT_SCRIPTS_DIR}/launch-pm.sh --via ${effective} --model ${arg}`;
}

/** The launch the next PM restart will use, from MoP config. */
export function pmLaunchPlan(db: Pick<MoPDatabase, "getConfig">): { launcher: PmLauncher; model: string; command: string } {
  const launcher = pmConfiguredLauncher(db);
  const model = pmConfiguredModel(db);
  return { launcher, model, command: pmFreshLaunchCommand(model, launcher) };
}

/**
 * True when the pane's bottom line is an empty interactive shell prompt (zsh
 * jreese theme renders `user@host <cwd> »` then `$ `). Read from the pane, never
 * inferred from elapsed time. Claude's composer chevron is not a shell prompt.
 */
export function isShellPromptVisible(text: string | null | undefined): boolean {
  if (!text) return false;
  const lines = text.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").split(/\r?\n/).map((line) => line.trimEnd())
    .filter((line) => line.trim() !== "");
  const last = lines.at(-1)?.trim();
  if (!last || /^Pane is dead/.test(last)) return false;
  return /^(?:\S.*\s)?[$%#»]$/.test(last);
}

// Dev slots: always --continue (crash recovery preserves session/work).
// PM: fresh launch with the configured model, then one continuation prompt.
// Respawn skill: --continue is optional arg (fresh start for atma updates).
export const RESTART_COMMANDS: Record<number, string> = {
  0: pmFreshLaunchCommand(PM_DEFAULT_MODEL),
  ...Object.fromEntries(DEV_SLOT_NUMBERS.map((slot) => [
    slot,
    `env ${CLAUDE_SLOT_RUNTIME_ENV} bash ${SLOT_RUNTIME_IDENTITIES[slot].launchScript} --continue`,
  ])),
};

/** Shell command names that indicate Claude Code has exited back to the shell */
export const SHELL_COMMANDS = new Set(["zsh", "bash", "sh", "fish"]);

// Pane current-commands that indicate an agent TUI is booted (Claude Code or
// omp/oh-my-pi). The respawn endpoint uses this to know when a launch succeeded.
export const AGENT_COMMANDS = new Set(["claude", "omp"]);

const SLOT_JSONL_DIRS: Record<number, string> = {
  0: "/Users/rajiv/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app",
  ...Object.fromEntries(DEV_SLOT_NUMBERS.map((slot) => [slot, SLOT_RUNTIME_IDENTITIES[slot].jsonlPath])),
};

const SLOT_CWDS: Record<number, string> = {
  0: "/Users/rajiv/Downloads/projects/heydonna-app",
  ...Object.fromEntries(DEV_SLOT_NUMBERS.map((slot) => [slot, SLOT_RUNTIME_IDENTITIES[slot].checkoutPath])),
};

type PaneProbe = {
  fingerprint: string;
  jsonlActivityToken: string;
  lastEventId: number;
};

type UnresponsiveState = PaneProbe & {
  unchangedChecks: number;
  probeSentAt?: number;
};

function shellEscape(value: string): string {
  // Inside a template literal a bare `'\''` evaluates to `'''`; escape the
  // backslash so the shell receives the standard '\'' quote break.
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function typedLaunchCommandForPane(slotNum: number): string | null {
  const restartCmd = RESTART_COMMANDS[slotNum];
  const cwd = SLOT_CWDS[slotNum];
  if (!restartCmd || !cwd) return null;

  // This string is typed into an existing shell pane. Do not wrap it in
  // `/bin/zsh -lc ...`: tmux send-keys sends text, not an argv vector, and the
  // extra shell layer collapses quoting into broken pane input.
  return `cd ${shellEscape(cwd)} && ${restartCmd}`;
}

// ─── Health Checker ────────────────────────────────────────

export class ProcessHealthChecker {
  private readonly CHECK_INTERVAL_MS = 30 * 1000;       // Check every 30s
  private readonly RESTART_COOLDOWN_MS = 120 * 1000;    // 2min cooldown per slot
  private readonly MAX_RESTARTS_PER_HOUR = 3;           // Prevent crash loops
  private readonly AGENT_BOOT_TIMEOUT_MS = 60_000;
  private readonly AGENT_BOOT_POLL_MS = 1_000;
  private readonly UNRESPONSIVE_PROBE_CHECKS = 6;       // 6 unchanged 30s ticks (~3m) before redraw probe
  private readonly UNRESPONSIVE_RESPAWN_CHECKS = 8;     // 8 unchanged ticks (~4m) before force respawn
  private readonly UNRESPONSIVE_FORCE_RESPAWN_COOLDOWN_MS = 15 * 60 * 1000;
  private timer: NodeJS.Timeout | null = null;
  private readonly startTime = Date.now();        // Startup grace period anchor
  private checkInFlight = false;

  /** slot -> timestamp of last restart */
  private lastRestart = new Map<number, number>();
  /** slot -> restart count in current hour window */
  private restartCounts = new Map<number, { count: number; windowStart: number }>();
  /** Slots currently being respawned by PM via /respawn endpoint.
   * While a slot is in this set, the health checker skips both auto-restart
   * and the "process died — auto-restarted" notification to PM. Prevents
   * intentional respawn from looking like a crash. */
  private pmInitiatedRespawns = new Set<number>();
  /** slot -> last stable pane/jsonl/event probe. Used to catch cmd=claude but TUI-wedged panes. */
  private unresponsiveStates = new Map<number, UnresponsiveState>();
  /** slot -> timestamp of last force respawn for unresponsive cmd=claude pane. */
  private lastForceRespawn = new Map<number, number>();

  // PM fresh restart bounds. Every wait reads the pane; none is a fixed sleep.
  private PM_RESTART_POLL_MS = 1_000;
  /** /exit (plus any exit-confirmation dialog) -> zsh prompt. */
  private PM_EXIT_SHELL_TIMEOUT_MS = 120_000;
  /** respawn-pane (force path) -> zsh prompt. */
  private PM_FORCE_SHELL_TIMEOUT_MS = 30_000;
  /** launcher typed -> idle Claude input prompt. */
  private PM_PROMPT_TIMEOUT_MS = 90_000;
  /** idle prompt -> this launch's SessionStart hook row. */
  private PM_BOOT_EVENT_TIMEOUT_MS = 20_000;
  /** tmux runner for the PM restart effects (tests substitute a fake pane). */
  private runShell: PaneCommandRunner = execShell;
  /** Single owner of a PM restart; concurrent triggers join it. */
  private pmRestartInFlight: Promise<{ success: boolean; reason: string }> | null = null;

  constructor(
    private db: MoPDatabase,
    private relay: TmuxRelay,
  ) {}

  /** PM is always provisioned; a dev slot is provisioned once its checkout exists. */
  private isSlotProvisioned(slot: number): boolean {
    const identity = SLOT_RUNTIME_IDENTITIES[slot];
    return slot === 0 || (identity !== undefined && existsSync(identity.checkoutPath));
  }

  // ─── PM-Initiated Respawn Tracking ─────────────────────

  /** Mark slot as being respawned by PM — suppress crash notification. */
  markPmInitiatedRespawn(slotNum: number): void {
    this.pmInitiatedRespawns.add(slotNum);
  }

  /** Clear PM-initiated respawn flag — restore normal crash detection. */
  clearPmInitiatedRespawn(slotNum: number): void {
    this.pmInitiatedRespawns.delete(slotNum);
  }

  /**
   * Finish a successful PM-initiated respawn.
   *
   * The controlled route has already launched Claude through the configured
   * slot launcher at this point. Seed the normal restart cooldown before
   * removing its fence so the next health tick cannot mistake a transient
   * shell/readback state for a second crash and launch the slot again.
   */
  completePmInitiatedRespawn(slotNum: number): void {
    this.lastRestart.set(slotNum, Date.now());
    this.unresponsiveStates.delete(slotNum);
    this.pmInitiatedRespawns.delete(slotNum);
  }

  /** Check if a slot is currently being respawned by PM. */
  isPmInitiatedRespawn(slotNum: number): boolean {
    return this.pmInitiatedRespawns.has(slotNum);
  }

  /** True when a controlled respawn owns the slot or just completed. */
  private isRestartSuppressed(slotNum: number, now = Date.now()): boolean {
    if (this.pmInitiatedRespawns.has(slotNum)) return true;
    const lastTime = this.lastRestart.get(slotNum);
    return lastTime !== undefined && now - lastTime < this.RESTART_COOLDOWN_MS;
  }

  /** Public getter for pane's current command (used by respawn orchestration). */
  getPaneCommandPublic(slotNum: number): Promise<string | null> {
    return this.getPaneCommand(slotNum);
  }

  // ─── Detection ─────────────────────────────────────────

  /**
   * Get the current command running in a tmux pane.
   * Returns "claude" when Claude Code is running, "zsh" when it's dead.
   */
  private async getPaneCommand(slotNum: number): Promise<string | null> {
    const identity = await verifyPaneIdentity(slotNum);
    if (!identity.ok) {
      console.warn(`[health] pane identity unavailable for slot ${slotNum}: ${identity.detail}`);
      return null;
    }
    const paneTarget = identity.snapshot.paneId;
    try {
      const result = await execShell(
        `tmux display-message -t ${paneTarget} -p '#{pane_current_command}'`,
        { timeout: 5_000 },
      );
      const stdout = result.stdout.trim();
      if (!stdout) return null;
      if (stdout === "claude") return stdout;

      if (SHELL_COMMANDS.has(stdout)) {
        const pidResult = await execShell(
          `tmux display-message -t ${paneTarget} -p '#{pane_pid}'`,
          { timeout: 5_000 },
        );
        const panePid = pidResult.stdout.trim();
        if (panePid && /^[0-9]+$/.test(panePid)) {
          try {
            const childResult = await execShell(
              `pgrep -P ${panePid} -fl '^claude( |$)'`,
              { timeout: 5_000 },
            );
            if (childResult.stdout.trim()) return "claude";
          } catch {
            // No direct Claude child; fall through to the pane command.
          }
        }
      }

      return stdout;
    } catch {
      return null; // Pane doesn't exist or tmux error
    }
  }

  /**
   * Check if a slot's Claude Code process is dead.
   * Dead = pane shows a shell (zsh/bash) instead of "claude".
   */
  async isProcessDead(slotNum: number): Promise<boolean> {
    const cmd = await this.getPaneCommand(slotNum);
    if (!cmd) return false; // Can't determine — don't restart blindly
    return SHELL_COMMANDS.has(cmd);
  }

  // ─── Crash Loop Protection ─────────────────────────────

  /**
   * Check if a slot has exceeded the restart limit for this hour.
   * Prevents infinite restart loops when Claude keeps crashing.
   */
  private isInCrashLoop(slotNum: number): boolean {
    const now = Date.now();
    const entry = this.restartCounts.get(slotNum);

    if (!entry || now - entry.windowStart > 3600_000) {
      // New hour window
      this.restartCounts.set(slotNum, { count: 0, windowStart: now });
      return false;
    }

    return entry.count >= this.MAX_RESTARTS_PER_HOUR;
  }

  private recordRestart(slotNum: number): void {
    const now = Date.now();
    const entry = this.restartCounts.get(slotNum);

    if (!entry || now - entry.windowStart > 3600_000) {
      this.restartCounts.set(slotNum, { count: 1, windowStart: now });
    } else {
      entry.count++;
    }
  }

  // ─── Revival ───────────────────────────────────────────

  /**
   * Restart a slot's Claude Code process by sending the alias command.
   */
  private async restartSlot(slotNum: number): Promise<{ success: boolean; reason: string; provenance?: { launchEventId: number; bootEventId: number; bootSession: string; ownedProcess: string; replacementProcess: string } }> {
    const launchCmd = typedLaunchCommandForPane(slotNum);
    if (!launchCmd) return { success: false, reason: "launcher command is not configured" };

    const identity = await verifyPaneIdentity(slotNum);
    if (!identity.ok) {
      console.warn(`[health] refusing restart for slot ${slotNum}: ${identity.detail}`);
      return { success: false, reason: `pane identity unavailable: ${identity.detail}` };
    }
    const paneTarget = identity.snapshot.paneId;
    // Effect-edge fence: verifyPaneIdentity() yields. A controlled respawn can
    // begin or finish while that lookup is in flight, so re-check immediately
    // before the launcher command is sent to the pane.
    if (this.isRestartSuppressed(slotNum)) return { success: false, reason: "restart suppressed by an active fence" };
    try {
      // Send restart command to the pane's shell.
      // Uses standalone bash scripts (not aliases) — no .zshrc/OMZ dependency.
      // Scripts resolve the atma template vars directly via sed.
      const shellProcess = await this.pmAgentProcess(paneTarget);
      if (!shellProcess?.endsWith(":")) return { success: false, reason: "launch shell ownership unknown" };
      const launchEventId = this.db.logEvent(slotNum, "agent_restart_launch", null, null, { paneId: paneTarget, shellProcess });
      await execShell(
        `tmux send-keys -t ${paneTarget} ${shellEscape(launchCmd)} Enter`,
        { timeout: 10_000 },
      );
      const bootCommand = await this.waitForAgentBoot(slotNum);
      const bootDeadline = Date.now() + this.AGENT_BOOT_TIMEOUT_MS;
      let boots = this.db.getEvents(slotNum, 50, "SessionStart").filter(e => e.id > launchEventId).sort((a, b) => a.id - b.id);
      while (bootCommand && boots.length === 0 && Date.now() < bootDeadline) {
        await this.sleepMs(Math.min(this.AGENT_BOOT_POLL_MS, bootDeadline - Date.now()));
        boots = this.db.getEvents(slotNum, 50, "SessionStart").filter(e => e.id > launchEventId).sort((a, b) => a.id - b.id);
      }
      const boot = boots[0];
      let payload: any = null;
      try { payload = JSON.parse(boot?.payload ?? "null"); } catch { /* UNKNOWN */ }
      const ownedProcess = await this.pmAgentProcess(paneTarget);
      const replacementProcess = await this.relay.exitProcessIdentity(slotNum, paneTarget);
      const ownedAgain = await this.pmAgentProcess(paneTarget);
      if (!bootCommand || boots.length !== 1 || !payload?.session_id || payload.cwd !== SLOT_CWDS[slotNum]
          || !["startup", "resume"].includes(payload.source) || !ProcessHealthChecker.ownedAgent(ownedProcess)
          || !ownedProcess!.startsWith(shellProcess) || ownedAgain !== ownedProcess || !replacementProcess
          || latestSessionStartEventId(this.db, slotNum) !== boot.id) {
        return { success: false, reason: "launch boot ownership unknown" };
      }
      return { success: true, reason: `agent boot verified (${bootCommand})`, provenance: {
        launchEventId, bootEventId: boot.id, bootSession: payload.session_id, ownedProcess: ownedProcess!, replacementProcess,
      } };
    } catch (err) {
      console.error(`[health] Failed to restart slot ${slotNum}:`, err);
      return { success: false, reason: `launcher failed: ${String(err)}` };
    }
  }

  // ─── PM fresh restart (Rajiv ts 1793988738.009889 / 1793989226.288339) ───

  /** Hook-side entry: the PM-only Stop consumed its single /exit attempt. */
  onPmExitDispatched(episode: string | null, delivery: string): void {
    if (delivery === "refused_pre_effect") return;
    void this.restartPMFresh("exit", episode).catch((err) => {
      console.error("[health] PM fresh restart after /exit failed:", err);
    });
  }

  private sleepMs(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
  }

  /** Poll the PM pane until an empty shell prompt is visible, bounded. */
  private async waitForPMShellPrompt(timeoutMs: number): Promise<{ paneId: string } | null> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const command = await this.getPaneCommand(0);
      if (command && SHELL_COMMANDS.has(command)) {
        const observed = await this.relay.observeSlotPane(0);
        if (observed && isShellPromptVisible(observed.text)) return { paneId: observed.paneId };
      }
      if (Date.now() >= deadline) return null;
      await this.sleepMs(Math.min(this.PM_RESTART_POLL_MS, deadline - Date.now()));
    }
  }

  /** Poll until the PM pane runs the agent AND shows an idle Claude input prompt, bounded. */
  private async waitForPMClaudePrompt(paneId: string, timeoutMs: number): Promise<string | null> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const command = await this.getPaneCommand(0);
      if (this.isAgentCommand(0, command)
          && (await this.relay.getSlotActivityState(0, paneId)) === "idle") return command;
      if (Date.now() >= deadline) return null;
      await this.sleepMs(Math.min(this.PM_RESTART_POLL_MS, deadline - Date.now()));
    }
  }

  /**
   * The PM restart contract: wait for the zsh prompt after /exit (pane read,
   * bounded; respawn-pane to a fresh shell is the fallback), type the
   * configured-model launcher as a FRESH start, wait for the Claude input
   * prompt (bounded), then deliver ONE continuation through the guarded writer.
   * Never force-kills a busy PM turn. Used for /exit episodes and PM crashes.
   */
  restartPMFresh(trigger: "exit" | "crash" | "unresponsive", episode: string | null = null): Promise<{ success: boolean; reason: string }> {
    if (this.pmRestartInFlight) return this.pmRestartInFlight;
    if (this.pmInitiatedRespawns.has(0)) return Promise.resolve({ success: false, reason: "controlled_respawn_owns_pm" });
    // A just-completed restart owns the cooldown: a late duplicate trigger must
    // never relaunch (or force-respawn) the freshly booted PM.
    if (this.isRestartSuppressed(0)) return Promise.resolve({ success: false, reason: "restart_cooldown" });
    this.pmInitiatedRespawns.add(0);
    const run = (async () => {
      let launched = false;
      try {
        const result = await this.runPMFreshRestart(trigger, episode);
        launched = result.launched;
        return { success: result.success, reason: result.reason };
      } finally {
        if (launched) {
          this.recordRestart(0);
          this.completePmInitiatedRespawn(0);
        } else {
          this.lastRestart.set(0, Date.now());
          this.pmInitiatedRespawns.delete(0);
        }
        this.pmRestartInFlight = null;
      }
    })();
    this.pmRestartInFlight = run;
    return run;
  }

  /**
   * Pane pid + direct Claude child pid of the PM pane (read-only process
   * identity): "<pane>:<child>", "<pane>:" when the pane's terminal listing
   * provably has no Claude child of the pane shell, or null when the identity is
   * UNKNOWN or ambiguous (a probe error, a listing without the pane shell, a
   * non-numeric pane pid, or more than one Claude child).
   * Callers must refuse on null, never compare two nulls as a match
   * (CTO REVISE ts 1794019803.549589).
   */
  private async pmAgentProcess(paneId: string): Promise<string | null> {
    let pane = "";
    let tty = "";
    try {
      [pane, tty] = (await this.runShell(`tmux display-message -t ${paneId} -p '#{pane_pid}|#{pane_tty}'`, { timeout: 5_000 }))
        .stdout.trim().split("|");
    } catch {
      return null;
    }
    if (!/^\d+$/.test(pane ?? "") || !/^\/dev\/[\w/.-]+$/.test(tty ?? "")) return null;
    // The pane's own terminal (the same `ps -t` read the exit-dialog identity
    // uses); a probe error or an unreadable listing is UNKNOWN, never "none".
    let listing = "";
    try {
      listing = (await this.runShell(`ps -t ${shellEscape(tty.replace(/^\/dev\//, ""))} -o pid=,ppid=,command=`, { timeout: 5_000 })).stdout;
    } catch {
      return null;
    }
    const rows = listing.split(/\r?\n/).map((line) => /^\s*(\d+)\s+(\d+)\s+(.*)$/.exec(line)).filter(Boolean) as RegExpExecArray[];
    if (!rows.some((row) => row[1] === pane)) return null;
    // Only the stable executable and explicitly named aliases (including their
    // resolved targets) establish ownership; an arbitrary */claude does not.
    const paths = [join(homedir(), ".local/opt/claude-code/claude"), "/opt/homebrew/bin/claude", join(homedir(), ".local/bin/claude")];
    const executables = new Set(paths);
    for (const path of paths) {
      try { executables.add(realpathSync(path)); } catch { /* unavailable alias */ }
    }
    const children = rows.filter((row) => row[2] === pane && executables.has(row[3].split(/\s/, 1)[0])).map((row) => row[1]);
    if (children.length > 1) return null;
    return `${pane}:${children[0] ?? ""}`;
  }

  /** A known identity with exactly one live Claude child. */
  private static ownedAgent(identity: string | null): boolean {
    return identity !== null && /^\d+:\d+$/.test(identity);
  }

  /** The durable MoP launch record of the last PM restart (survives a MoP restart). */
  private pmLaunchRecord(): {
    episode: string | null; launch_event_id: number; pane_id: string; command: string;
    status?: "planned" | "refused" | "unknown" | "issued" | "booted"; pane_pid?: string; boot_event_id?: number; process?: string;
  } | null {
    try {
      const record = JSON.parse(this.db.getConfig(PM_RESTART_LAUNCH_KEY) || "null");
      return record && typeof record.launch_event_id === "number" ? record : null;
    } catch {
      return null;
    }
  }

  /**
   * The ONE boot a MoP launch produced: the first PM SessionStart after the
   * launch keystroke, from the PM checkout, with a session id. Any later
   * SessionStart is a successor and never "this launch" (CTO REVISE ts
   * 1794018596.287469).
   */
  private launchBoot(launchEventId: number): { id: number; session: string } | null {
    const after = this.db.getEvents(0, 50, "SessionStart").filter((event) => event.id > launchEventId)
      .sort((a, b) => a.id - b.id);
    const first = after[0];
    if (!first) return null;
    try {
      const payload = JSON.parse(first.payload);
      if (!["startup", "resume"].includes(String(payload.source)) || payload.cwd !== SLOT_CWDS[0]
          || typeof payload.session_id !== "string" || !payload.session_id.trim()) return null;
      return { id: first.id, session: payload.session_id };
    } catch {
      return null;
    }
  }

  private async runPMFreshRestart(trigger: "exit" | "crash" | "unresponsive", episode: string | null):
    Promise<{ success: boolean; launched: boolean; reason: string }> {
    const { model, launcher, command: launchCommand } = pmLaunchPlan(this.db);
    const cwd = SLOT_CWDS[0];
    const fail = (reason: string, launched = false, extra: Record<string, unknown> = {}) => {
      // A restart failure does not cancel an issued or uncertain /exit or launcher.
      // The separate refused_pre_effect exit terminal owns proven zero-effect cleanup.
      this.db.logEvent(0, "pm_restart_failed", null, null, { trigger, episode, model, reason, ...extra });
      console.error(`[health][pm-restart] ${trigger}: ${reason}`);
      return { success: false, launched, reason };
    };
    this.db.logEvent(0, "pm_restart_started", null, null, { trigger, episode, model, launcher, launch_command: launchCommand });

    // Ownership pin (CTO REVISE ts 1794018596.287469): the pane, the PM boot,
    // the agent process and the exit episode observed at the trigger. Every
    // effect below re-checks it, so a recovered or successor PM is never
    // killed, typed into, or handed this restart's continuation.
    const pinned = await verifyPaneIdentity(0, this.runShell);
    if (!pinned.ok) return fail(`pane identity unavailable: ${pinned.detail}`);
    const pin = {
      paneId: pinned.snapshot.paneId,
      boot: latestSessionStartEventId(this.db, 0),
      process: await this.pmAgentProcess(pinned.snapshot.paneId),
      episode: trigger === "exit" ? episode : null,
    };
    const ownerRefusal = async (): Promise<string | null> => {
      const identity = await verifyPaneIdentity(0, this.runShell);
      if (!identity.ok || identity.snapshot.paneId !== pin.paneId) return "pane_identity_changed";
      if (latestSessionStartEventId(this.db, 0) !== pin.boot) return "pm_session_changed";
      if (pin.episode) {
        const current = this.db.getExitStatus();
        if (!current.pending || current.episode !== pin.episode) return "exit_episode_changed";
      }
      return null;
    };

    // Never resend an uncertain launcher (CTO REVISE ts 1794019803.549589 and
    // 1794021484.838249). An unresolved (planned/unknown/issued) launch of a
    // still-pending exit episode keeps that episode's launch custody CONSUMED,
    // across ANY newer boot (e.g. an operator's manual PM that then exits): its
    // record is never overwritten and no launcher is sent again for that
    // episode. Custody retires only by reconciliation (the record becomes
    // `booted`) or an explicitly superseding request (a new or disabled
    // exit-pending episode). An episode-less (crash) launch blocks only until a
    // PM boot follows it in this pane.
    const prior = this.pmLaunchRecord();
    if (prior && ["planned", "unknown", "issued"].includes(String(prior.status))) {
      const current = this.db.getExitStatus();
      const episodeCustody = prior.episode !== null && current.pending && current.episode === prior.episode;
      const paneUnresolved = prior.pane_id === pin.paneId && latestSessionStartEventId(this.db, 0) <= prior.launch_event_id;
      if (episodeCustody || paneUnresolved) {
        return fail("uncertain_launch_no_resend", false, {
          prior_status: prior.status, prior_launch_event_id: prior.launch_event_id, prior_episode: prior.episode,
          custody: episodeCustody ? "episode" : "pane",
        });
      }
    }
    // The kill guard: same pane/boot/episode AND the same KNOWN live process.
    const killRefusal = async (): Promise<string | null> => {
      const owner = await ownerRefusal();
      if (owner) return owner;
      const current = await this.pmAgentProcess(pin.paneId);
      if (!ProcessHealthChecker.ownedAgent(pin.process) || !ProcessHealthChecker.ownedAgent(current)) return "pm_process_unknown";
      return current === pin.process ? null : "pm_process_changed";
    };

    // 1. zsh prompt (pane read). Fallback: respawn-pane to a fresh login shell,
    //    only for the SAME pinned process and only when it is gone or shows an
    //    idle prompt, for EVERY trigger; a busy PM turn is never killed.
    let shell = await this.waitForPMShellPrompt(trigger === "unresponsive" ? 0 : this.PM_EXIT_SHELL_TIMEOUT_MS);
    if (shell && shell.paneId !== pin.paneId) return fail("pane_identity_changed");
    if (!shell) {
      // /exit is graceful only. A blocked dialog or background agent must
      // never turn a timeout into respawn-pane -k, even at an idle prompt.
      if (trigger === "exit") return fail("exit_blocked_shell_not_observed_no_force");
      const command = await this.getPaneCommand(0);
      if (!command) return fail("shell_prompt_timeout_pane_unknown");
      const agentAlive = this.isAgentCommand(0, command);
      if (agentAlive && trigger === "crash") {
        // A crash restart started from an observed shell; an agent now running
        // was launched by someone else and is never killed.
        return fail("shell_prompt_timeout_agent_running_no_force", false, { pane_command: command });
      }
      // A live agent may be killed only if it is the SAME, KNOWN process pinned
      // at the trigger; an unknown or ambiguous identity refuses. With no agent
      // left the pane must provably be a bare shell (known, no Claude child).
      const guard = agentAlive ? killRefusal : async () => {
        const owner = await ownerRefusal();
        if (owner) return owner;
        const current = await this.pmAgentProcess(pin.paneId);
        return current !== null && current.endsWith(":") ? null : "pm_process_unknown";
      };
      const refusal = await guard();
      if (refusal) return fail(`no_force:${refusal}`, false, { pane_command: command });
      if (agentAlive && (await this.relay.getSlotActivityState(0, pin.paneId)) !== "idle") {
        // An unchanged screen/JSONL/event counter is not proof of idle.
        return fail("shell_prompt_timeout_pm_busy_no_force", false, { pane_command: command });
      }
      // Last pre-effect recheck INCLUDING the process: nothing yields between it and the kill.
      const finalRefusal = await guard();
      if (finalRefusal) return fail(`no_force:${finalRefusal}`, false, { pane_command: command });
      this.db.logEvent(0, "pm_restart_force_respawn", null, null, {
        trigger, episode, pane_command: command, process: pin.process, reason: "shell prompt not observed within bound",
      });
      try {
        await this.runShell(`tmux respawn-pane -k -t ${pin.paneId} -c ${shellEscape(cwd)}`, { timeout: 10_000 });
        this.lastForceRespawn.set(0, Date.now());
      } catch (err) {
        return fail(`force respawn failed: ${String(err).slice(0, 200)}`);
      }
      shell = await this.waitForPMShellPrompt(this.PM_FORCE_SHELL_TIMEOUT_MS);
      if (!shell) return fail("shell_prompt_not_observed_after_force_respawn");
      if (shell.paneId !== pin.paneId) return fail("pane_identity_changed");
    }
    this.unresponsiveStates.delete(0);
    this.db.logEvent(0, "process_dead", null, null, {
      detected_at: new Date().toISOString(), pane_command: "shell_prompt", trigger, episode,
    });

    // 2. Fresh launcher with the configured model, typed at the verified prompt.
    //    The durable launch record binds the ONE boot this launch produces.
    //    The record carries the ACTUAL disposition (CTO REVISE ts
    //    1794019803.549589): planned -> refused (proven zero effect) | unknown
    //    (a key may have landed) | issued (both keys sent) -> booted (owned
    //    process + its SessionStart). Only `booted` can take a continuation.
    const launchEventId = this.db.getEvents(0, 1)[0]?.id ?? 0;
    const typed = `cd ${shellEscape(cwd)} && ${launchCommand}`;
    const shellProcess = await this.pmAgentProcess(pin.paneId);
    if (shellProcess === null || !shellProcess.endsWith(":")) return fail("launch_refused:pm_shell_process_unknown");
    const record = {
      episode: pin.episode, launch_event_id: launchEventId, pane_id: pin.paneId, command: launchCommand,
      pane_pid: shellProcess.slice(0, -1), at: new Date().toISOString(), status: "planned",
    } as Record<string, unknown>;
    const setRecord = (fields: Record<string, unknown>) => {
      Object.assign(record, fields);
      this.db.setConfig(PM_RESTART_LAUNCH_KEY, JSON.stringify(record));
    };
    setRecord({});
    let effect = false;
    const refusal = await withSlotSendLock(0, async () => {
      for (const key of ["text", "Enter"] as const) {
        const owner = await ownerRefusal();
        if (owner) return owner;
        const command = await this.getPaneCommand(0);
        if (!command || !SHELL_COMMANDS.has(command)) return "pm_shell_changed";
        effect = true;
        await this.runShell(key === "text"
          ? `tmux send-keys -l -t ${pin.paneId} ${shellEscape(typed)}`
          : `tmux send-keys -t ${pin.paneId} Enter`, { timeout: 10_000 });
      }
      return null;
    }).catch((err) => `launcher send failed: ${String(err).slice(0, 200)}`);
    if (refusal) {
      setRecord({ status: effect ? "unknown" : "refused", reason: refusal });
      return fail(refusal, false, { launch_disposition: record.status });
    }
    setRecord({ status: "issued", issued_at: new Date().toISOString() });
    this.db.logEvent(0, "pm_restart_launched", null, null, { trigger, episode, model, launcher, launch_command: launchCommand });

    // 3. Claude input prompt (pane read, bounded).
    const exitCandidate = this.db.getExitAttempt(0);
    const bootCommand = await this.waitForPMClaudePrompt(pin.paneId, this.PM_PROMPT_TIMEOUT_MS);
    if (!bootCommand) return fail(`claude prompt not observed within ${this.PM_PROMPT_TIMEOUT_MS}ms`, true);
    this.db.logEvent(0, "agent_booted", null, null, {
      command: bootCommand, launch_command: launchCommand, observed_at: new Date().toISOString(),
    });
    this.db.logEvent(0, "process_restarted", null, null, {
      command: launchCommand, restarted_at: new Date().toISOString(), trigger, model,
    });

    // This launch's FIRST SessionStart binds the continuation to this boot; a
    // later (successor) boot never inherits it.
    const bootDeadline = Date.now() + this.PM_BOOT_EVENT_TIMEOUT_MS;
    let boot = this.launchBoot(launchEventId);
    while (!boot && Date.now() < bootDeadline) {
      await this.sleepMs(Math.min(this.PM_RESTART_POLL_MS, bootDeadline - Date.now()));
      boot = this.launchBoot(launchEventId);
    }
    if (!boot) return fail("session_start_not_observed_for_launch", true);
    if (latestSessionStartEventId(this.db, 0) !== boot.id) return fail("successor_boot_after_launch", true);
    // First-in-time is not ownership: the boot must be a Claude child of the
    // SAME shell this launch typed into, and the only boot since the launch.
    const bootProcess = await this.pmAgentProcess(pin.paneId);
    if (!ProcessHealthChecker.ownedAgent(bootProcess) || !bootProcess!.startsWith(`${record.pane_pid}:`)) {
      return fail("launch_boot_process_unowned", true, { boot_process: bootProcess });
    }
    setRecord({ status: "booted", boot_event_id: boot.id, process: bootProcess });
    if (trigger === "exit" && exitCandidate?.episode === episode) {
      const replacementProcess = await this.relay.exitProcessIdentity(0, pin.paneId);
      if (replacementProcess && await this.pmAgentProcess(pin.paneId) === bootProcess
          && latestSessionStartEventId(this.db, 0) === boot.id) {
        this.db.logEvent(0, "exit_replacement_bound", null, null, { episode,
          submittedAt: exitCandidate.submittedAt, assignmentEpoch: exitCandidate.assignmentEpoch, replacementProcess,
          launchEventId, bootEventId: boot.id, bootSession: boot.session, ownedProcess: bootProcess });
      }
    }
    const processGuard = async () => (await this.pmAgentProcess(pin.paneId)) === bootProcess ? null : "pm_process_changed";

    // 4. ONE continuation. A PM-only exit episode keeps its durable
    //    waiting -> attempted claim (and its saved continuation); every other
    //    restart claims this boot once.
    const status = this.db.getExitStatus();
    if (pin.episode && status.pending && status.pm_only && this.db.hasExitAttemptSubmitted(0) && status.episode === pin.episode) {
      await this.reconcilePMRestartContinuation(boot.id);  // binds the booted record + process inside
      // Read this episode's continuation receipt: a with_devs episode whose devs
      // all cycled ends (and resets its flags) as soon as the PM part lands.
      let delivery: string | null = null;
      const receipt = this.db.getEvents(0, 1, "pm_restart_continuation")[0];
      try {
        const payload = receipt ? JSON.parse(receipt.payload) : null;
        if (payload && payload.episode === status.episode && receipt!.id > launchEventId) delivery = String(payload.delivery);
      } catch { delivery = null; }
      const delivered = delivery === "delivered";
      if (delivered) void this.relay.drainPMQueue();
      return { success: delivered, launched: true, reason: `continuation ${delivery ?? "unknown"}` };
    }
    const outcome = await this.deliverPMFreshContinuation(pin.paneId, boot.id, trigger, processGuard);
    if (outcome === "delivered") void this.relay.drainPMQueue();
    return { success: outcome === "delivered", launched: true, reason: `continuation ${outcome}` };
  }

  /** Exactly-once continuation for a non-episode PM restart, claimed per boot before the send. */
  private async deliverPMFreshContinuation(paneId: string, bootId: number, trigger: string,
    processGuard: () => Promise<string | null>): Promise<string> {
    const claimKey = "pm_restart_continuation_boot";
    if (this.db.getConfig(claimKey) === String(bootId)) return "already_claimed";
    this.db.setConfig(claimKey, String(bootId));
    const fence = () => latestSessionStartEventId(this.db, 0) !== bootId ? "pm_session_changed" : null;
    const result = await this.relay.deliverGuardedToSlot(0, PM_RESTART_CONTINUATION_PROMPT, {
      expectedPaneId: paneId,
      recheck: async () => fence() ?? await processGuard() ?? fence(),
      finalCheck: fence,
      bracketedPaste: true,
    });
    this.db.logEvent(0, "pm_restart_continuation", null, null, {
      episode: null, trigger, boot_event_id: bootId, delivery: result.outcome,
    });
    return result.outcome;
  }

  private isAgentCommand(slotNum: number, command: string | null): boolean {
    return command !== null && (AGENT_COMMANDS.has(command)
      || (slotNum === 0 && /^\d+\.\d+\.\d+$/.test(command)));
  }

  private async waitForAgentBoot(slotNum: number, timeoutMs = this.AGENT_BOOT_TIMEOUT_MS): Promise<string | null> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() <= deadline) {
      const command = await this.getPaneCommand(slotNum);
      if (this.isAgentCommand(slotNum, command)) {
        this.db.logEvent(slotNum, "agent_booted", null, null, {
          command,
          launch_command: RESTART_COMMANDS[slotNum],
          observed_at: new Date().toISOString(),
        });
        return command;
      }
      if (Date.now() >= deadline) break;
      await new Promise((resolve) => setTimeout(resolve, Math.min(this.AGENT_BOOT_POLL_MS, Math.max(0, deadline - Date.now()))));
    }
    return null;
  }

  private async forceRespawnSlot(slotNum: number, reason: string): Promise<boolean> {
    const launchCmd = RESTART_COMMANDS[slotNum];
    const cwd = SLOT_CWDS[slotNum];
    if (!launchCmd || !cwd) return false;

    const identity = await verifyPaneIdentity(slotNum);
    if (!identity.ok) {
      console.warn(`[health] refusing force-respawn for slot ${slotNum}: ${identity.detail}`);
      return false;
    }
    const paneTarget = identity.snapshot.paneId;
    try {
      await execShell(
        `tmux respawn-pane -k -t ${paneTarget} -c ${shellEscape(cwd)} ${shellEscape(launchCmd)}`,
        { timeout: 10_000 },
      );
      this.lastForceRespawn.set(slotNum, Date.now());
      this.unresponsiveStates.delete(slotNum);
      console.log(`[health] Force-respawned slot ${slotNum} after unresponsive probe (${reason})`);
      return true;
    } catch (err) {
      console.error(`[health] Failed to force-respawn slot ${slotNum}:`, err);
      return false;
    }
  }

  private async capturePaneFingerprint(slotNum: number): Promise<string | null> {
    const paneAddress = resolvePaneAddress(slotNum);
    try {
      const result = await execShell(
        `tmux capture-pane -t ${paneAddress} -p -S -80`,
        { timeout: 5_000, maxBuffer: 256 * 1024 },
      );
      return createHash("sha256").update(result.stdout).digest("hex");
    } catch {
      return null;
    }
  }

  private latestEventId(slotNum: number): number {
    try {
      const event = this.db.getEvents(slotNum, 1)[0];
      return event?.id ?? 0;
    } catch {
      return 0;
    }
  }

  /**
   * Detect panes where tmux still reports `claude` but the TUI no longer
   * reacts to keyboard input. This avoids activity-only false positives by
   * requiring all three signals to stay unchanged across multiple health ticks:
   * visible pane fingerprint, Claude JSONL mtime, and MoP hook/event id.
   */
  private async checkClaudeResponsiveness(slotNum: number): Promise<void> {
    const fingerprint = await this.capturePaneFingerprint(slotNum);
    if (!fingerprint) {
      this.unresponsiveStates.delete(slotNum);
      return;
    }

    const probe: PaneProbe = {
      fingerprint,
      jsonlActivityToken: (
        await recentJsonlActivity.latestActivity(SLOT_JSONL_DIRS[slotNum])
      ).token,
      lastEventId: this.latestEventId(slotNum),
    };
    const prev = this.unresponsiveStates.get(slotNum);

    if (!prev ||
        prev.fingerprint !== probe.fingerprint ||
        prev.jsonlActivityToken !== probe.jsonlActivityToken ||
        prev.lastEventId !== probe.lastEventId) {
      this.unresponsiveStates.set(slotNum, { ...probe, unchangedChecks: 1 });
      return;
    }

    const state: UnresponsiveState = {
      ...probe,
      unchangedChecks: prev.unchangedChecks + 1,
      probeSentAt: prev.probeSentAt,
    };
    this.unresponsiveStates.set(slotNum, state);

    if (state.unchangedChecks === this.UNRESPONSIVE_PROBE_CHECKS) {
      try {
        const identity = await verifyPaneIdentity(slotNum);
        if (!identity.ok) {
          this.db.logEvent(slotNum, "pane_unresponsive_probe_failed", null, null, {
            unchanged_checks: state.unchangedChecks,
            reason: "pane_identity_mismatch",
            detail: identity.detail,
          });
          return;
        }
        await execShell(`tmux send-keys -t ${identity.snapshot.paneId} C-l`, { timeout: 5_000 });
        state.probeSentAt = Date.now();
        this.unresponsiveStates.set(slotNum, state);
        this.db.logEvent(slotNum, "pane_unresponsive_probe_sent", null, null, {
          unchanged_checks: state.unchangedChecks,
          probe: "C-l",
          jsonl_activity_token: state.jsonlActivityToken,
          last_event_id: state.lastEventId,
        });
      } catch (err) {
        this.db.logEvent(slotNum, "pane_unresponsive_probe_failed", null, null, {
          unchanged_checks: state.unchangedChecks,
          error: String(err).slice(0, 300),
        });
      }
      return;
    }

    if (state.unchangedChecks < this.UNRESPONSIVE_RESPAWN_CHECKS) return;

    const lastForce = this.lastForceRespawn.get(slotNum);
    if (lastForce && Date.now() - lastForce < this.UNRESPONSIVE_FORCE_RESPAWN_COOLDOWN_MS) {
      this.db.logEvent(slotNum, "pane_unresponsive_respawn_suppressed", null, null, {
        unchanged_checks: state.unchangedChecks,
        cooldown_ms: this.UNRESPONSIVE_FORCE_RESPAWN_COOLDOWN_MS,
        last_force_respawn_at: new Date(lastForce).toISOString(),
      });
      return;
    }

    if (this.isInCrashLoop(slotNum)) {
      this.db.logEvent(slotNum, "pane_unresponsive_crash_loop_suppressed", null, null, {
        unchanged_checks: state.unchangedChecks,
        max_restarts_per_hour: this.MAX_RESTARTS_PER_HOUR,
      });
      if (slotNum !== 0) {
        this.relay.injectToPM(
          `# 🔴 slot ${slotNum} appears unresponsive but is in restart limit — manual intervention needed.`,
        );
      }
      return;
    }

    this.db.logEvent(slotNum, "pane_unresponsive_force_respawn", null, null, {
      unchanged_checks: state.unchangedChecks,
      probe_sent_at: state.probeSentAt ? new Date(state.probeSentAt).toISOString() : null,
      command: slotNum === 0 ? pmLaunchPlan(this.db).command : RESTART_COMMANDS[slotNum],
    });

    if (slotNum === 0) {
      // PM crash recovery uses the fresh-launch + continuation contract.
      this.unresponsiveStates.delete(0);
      void this.restartPMFresh("unresponsive").catch((err) => console.error("[health] PM unresponsive restart failed:", err));
      return;
    }

    const restarted = await this.forceRespawnSlot(slotNum, `unchanged_checks=${state.unchangedChecks}`);
    if (restarted) {
      this.lastRestart.set(slotNum, Date.now());
      this.recordRestart(slotNum);
      if (slotNum !== 0) {
        this.relay.injectToPM(
          `# 🔄 slot ${slotNum} pane was unresponsive — force-respawned with --continue`,
        );
      }
      this.scheduleContinueInjection(slotNum);
    }
  }

  // ─── Main Check Loop ──────────────────────────────────

  /**
   * Check all slots (0-8) for dead processes and restart if needed.
   *
   * Slot 0 = PM (claude-pm)
   * Slots 1-8 = isolated dev runtimes.
   */
  async checkAll(): Promise<void> {
    const now = Date.now();

    // Startup grace period — skip checks for first 30s after MoP starts
    // Prevents false "process dead" alerts during MoP restart
    if (now - this.startTime < 30_000) return;

    for (const slot of RUNTIME_SLOT_NUMBERS) {
      // Skip controlled or recently completed respawns.
      if (this.isRestartSuppressed(slot, now)) continue;

      // DND does NOT skip health checks — Claude must run on all slots always.
      // DND only means "don't assign new work" — NOT "don't keep alive."
      // Rajiv directive (2026-03-16): "heartbeat should ignore dnd. claude has to run on all ports all the time."

      // PM-initiated respawn in progress — the shell state is expected, not a crash.
      // Skip both auto-restart AND notification while flag is set.
      // (Rajiv directive 2026-04-05: "not send slot crash events to pm" during respawn)
      if (this.pmInitiatedRespawns.has(slot)) continue;

      // A configured slot whose checkout is not provisioned yet (S7/S8 before
      // their clone exists) has an empty shell pane by design: never treat it
      // as a dead process, launch into it, or alert PM about it.
      if (!this.isSlotProvisioned(slot)) continue;

      const paneCommand = await this.getPaneCommand(slot);

      if (this.isAgentCommand(slot, paneCommand)) {
        if (slot === 0 && this.db.getExitPending()) await this.reconcilePMRestartContinuation();
        await this.checkClaudeResponsiveness(slot);
        continue;
      }

      if (!paneCommand || !SHELL_COMMANDS.has(paneCommand)) {
        this.unresponsiveStates.delete(slot);
        continue;
      }

      // getPaneCommand() yields. Re-check before emitting death/restart effects
      // so an in-flight health iteration cannot race a controlled /exit.
      if (this.isRestartSuppressed(slot)) continue;

      this.unresponsiveStates.delete(slot);

      // Crash loop protection
      if (this.isInCrashLoop(slot)) {
        const slotName = slot === 0 ? "PM" : `slot ${slot}`;
        console.warn(
          `[health] ${slotName} in crash loop (${this.MAX_RESTARTS_PER_HOUR}+ restarts/hr) — skipping auto-restart`,
        );

        // Notify PM about the crash loop (unless PM itself is looping)
        if (slot !== 0) {
          this.relay.injectToPM(
            `# 🔴 slot ${slot} in crash loop — ${this.MAX_RESTARTS_PER_HOUR}+ restarts in the last hour. Manual intervention needed.`,
          );
        }
        continue;
      }

      // ─── PM is dead — fresh relaunch + one continuation ──
      // Runs detached under its own fence so dev-slot health ticks continue.
      if (slot === 0) {
        console.log("[health] PM process DEAD — fresh relaunch with configured model + continuation");
        const status = this.db.getExitStatus();
        const trigger = status.pending && status.pm_only && this.db.hasExitAttemptSubmitted(0) ? "exit" : "crash";
        void this.restartPMFresh(trigger, status.episode).catch((err) => console.error("[health] PM restart failed:", err));
        continue;
      }

      // ─── Process is dead — restart it ───────────────────
      const slotName = slot === 0 ? "PM" : `slot ${slot}`;
      console.log(`[health] ${slotName} process DEAD — restarting with --continue`);

      // Log the death event
      this.db.logEvent(slot, "process_dead", null, null, {
        detected_at: new Date().toISOString(),
        pane_command: paneCommand,
      });

      const exitCandidate = this.db.getExitAttempt(slot);
      const exitEpoch = this.db.getSlot(slot)?.assignment_epoch;
      const restartResult = await this.restartSlot(slot);
      if (restartResult.success) {
        if (restartResult.provenance && exitCandidate && exitCandidate.assignmentEpoch === exitEpoch
            && this.db.getSlot(slot)?.assignment_epoch === exitEpoch && !this.db.isExitSlotRetained(slot)) {
          const { replacementProcess } = restartResult.provenance;
          this.db.logEvent(slot, "exit_replacement_bound", null, null, { episode: exitCandidate.episode,
            submittedAt: exitCandidate.submittedAt, assignmentEpoch: exitEpoch, ...restartResult.provenance, replacementProcess });
        }
        this.lastRestart.set(slot, Date.now());
        this.recordRestart(slot);

        this.db.logEvent(slot, "process_restarted", null, null, {
          command: RESTART_COMMANDS[slot],
          restarted_at: new Date().toISOString(),
        });

        // Notify PM about slot death + restart (unless PM itself died)
        if (slot !== 0) {
          const taskInfo = (() => {
            try {
              const s = this.db.getSlot(slot);
              return s?.task ? ` | task: ${s.task}` : "";
            } catch {
              return "";
            }
          })();

          this.relay.injectToPM(
            `# 🔄 ${slotName} process died — auto-restarted with --continue${taskInfo}`,
          );
      } else {
          // PM died — keep the operator alert in the durable PM queue. Relay
          // delivery is gated on a booted agent, so this can never type into
          // the shell while recovery is incomplete.
          console.log("[health] PM process restarted — PM will resume via --continue");
        }

        // Post-restart continue injection: wait one check cycle (30s),
        // verify Claude is running, then send "continue" to resume session.
        this.scheduleContinueInjection(slot);
        if (slot === 0) void this.relay.drainPMQueue();
      } else {
        // Failed attempts get the ordinary cooldown to avoid a 30s launcher
        // storm, but they do not count as successful restarts for the hourly
        // crash-loop limit.
        this.lastRestart.set(slot, Date.now());
        const failure = {
          command: RESTART_COMMANDS[slot],
          reason: restartResult.reason,
          failed_at: new Date().toISOString(),
          max_restarts_per_hour: this.MAX_RESTARTS_PER_HOUR,
          restart_count_incremented: false,
        };
        this.db.logEvent(slot, "process_restart_failed", null, null, failure);
        console.error(`[health][restart-failed] ${slotName}: ${restartResult.reason}`);
        this.relay.injectToPM(
          `# 🔴 ${slotName} restart failed — ${restartResult.reason}. Manual intervention may be needed.`,
        );
      }
    }

    // After dead-process checks, scan for exit_pending on idle slots
    await this.checkExitPending();
  }

  // ─── Exit Pending: process receipts only, never timer-driven /exit ───

  private async checkExitPending(): Promise<void> {
    await this.relay.observeExitAttempts();
    const status = this.db.getExitStatus();
    if (!status.pending || status.pm_only) return;
    const settled = Object.entries(status.cycled).every(([slot, verified]) =>
      (status.dev_only && Number(slot) === 0) || verified || this.db.isExitSlotRetained(Number(slot)));
    if (settled && !this.relay.hasPendingExitConfirmation(status.episode)) {
      this.db.setExitPending(false);
      this.db.logEvent(0, "exit_pending_complete", null, null, {
        source: "health_observer", reason: "Every targeted slot has a verified process replacement or explicit cohort retention",
      });
    }
  }

  // ─── Post-Restart Continue Injection ───────────────────

  /**
   * After a restart, wait 30s then send "continue" to the pane.
   * The --continue flag on the restart command resumes the session,
   * but the slot lands at the Claude prompt. Sending "continue"
   * triggers the session to pick up where it left off.
   *
   * Guard: only sends if pane_current_command is "claude" (process alive
   * and at the prompt). If still showing "zsh", the restart didn't work
   * and next checkAll() cycle will handle it.
   */
  private scheduleContinueInjection(slotNum: number): void {
    const restartEpisode = slotNum === 0 ? this.db.getExitStatus().episode : null;
    const timer = setTimeout(async () => {
      const cmd = await this.getPaneCommand(slotNum);
      // Native Claude's executable name is its installed version (for example
      // 2.1.312). The daily PM identity probe already accepts that live shape.
      if (cmd !== "claude" && !(slotNum === 0 && this.isAgentCommand(slotNum, cmd))) {
        console.log(
          `[health] Slot ${slotNum} not yet at claude prompt (cmd: ${cmd}) — skipping continue injection`,
        );
        return;
      }

      try {
        const identity = await verifyPaneIdentity(slotNum);
        if (!identity.ok) {
          console.warn(`[health] refusing continue injection for slot ${slotNum}: ${identity.detail}`);
          return;
        }
        if (slotNum === 0 && this.db.getExitStatus().pm_only) {
          if (this.db.getExitStatus().episode !== restartEpisode) return;
          await this.reconcilePMRestartContinuation();
          return;
        }
        await execShell(
          `tmux send-keys -t ${identity.snapshot.paneId} 'continue' Enter`,
          { timeout: 10_000 },
        );
        console.log(`[health] Sent "continue" to slot ${slotNum} after restart`);
        this.db.logEvent(slotNum, "continue_injected", null, null, {
          reason: "Post-restart continue injection — session resuming",
        });
        this.unresponsiveStates.delete(slotNum);
      } catch (err) {
        console.error(`[health] Failed to send continue to slot ${slotNum}:`, err);
      }
    }, this.CHECK_INTERVAL_MS); // Wait one check cycle (30s)

    // Don't prevent process exit
    if (timer.unref) timer.unref();
  }

  /**
   * Recover only an unattempted episode whose exit is followed by a PM boot.
   *
   * CTO correction (C0ALZJHGE49 thread 1793754459.969419, ts 1793761597.648009):
   * an `uncertain` /exit receipt stays uncertain forever and a boot/session id
   * is never delivery proof (the real --continue boot reuses the same UUID).
   * The continuation settles independently of exit-submit certainty only when
   * the watchdog itself observed the PM process die and restarted it AFTER the
   * attempt (process_dead -> process_restarted, both newer than the receipt),
   * the latest boot follows that death, and the pane is still owned. The send
   * goes through the guarded continuation writer behind the durable
   * waiting -> attempted claim; only its delivered receipt arms dev_only and
   * releases custody. Anything less stays typed uncertain.
   */
  private async reconcilePMRestartContinuation(expectedBoot?: number): Promise<void> {
    const status = this.db.getExitStatus();
    if (!status.pending || !status.pm_only || !status.cycled[0]
        || status.continuation_delivery !== "waiting") return;
    const exit = this.db.getEvents(0, 1, "exit_pending_triggered")[0];
    const boot = this.db.getEvents(0, 1, "SessionStart")[0];
    if (!exit || !boot || boot.id <= exit.id) return;
    if (expectedBoot !== undefined && boot.id !== expectedBoot) return;
    // Only the boot MoP itself launched for THIS episode receives its
    // continuation (CTO REVISE ts 1794018596.287469). A recovered or successor
    // PM started outside that launch never inherits an older restart's prompt.
    // CTO REVISE ts 1794019803.549589: a planned/issued record is not a
    // launch; only a `booted` record (owned Claude process + its SessionStart)
    // binds the boot, and that same process must still own the pane.
    let launch = this.pmLaunchRecord();
    // A successfully issued launcher may become healthy after the bounded wait.
    // Bind only its first boot in the same pane and shell; planned/unknown sends
    // cannot establish that ownership and remain in custody without replay.
    if (launch?.status === "issued" && launch.episode === status.episode && launch.launch_event_id >= exit.id) {
      const first = this.launchBoot(launch.launch_event_id);
      const identity = await verifyPaneIdentity(0, this.runShell);
      const process = identity.ok && identity.snapshot.paneId === launch.pane_id
        ? await this.pmAgentProcess(launch.pane_id) : null;
      const command = await this.getPaneCommand(0);
      const idle = await this.relay.getSlotActivityState(0, launch.pane_id);
      const processAgain = await this.pmAgentProcess(launch.pane_id);
      const current = this.db.getExitStatus();
      const latest = this.pmLaunchRecord();
      if (first && first.id === boot.id && latestSessionStartEventId(this.db, 0) === boot.id
          && ProcessHealthChecker.ownedAgent(process) && process!.startsWith(`${launch.pane_pid}:`)
          && processAgain === process && this.isAgentCommand(0, command) && idle === "idle"
          && current.pending && current.episode === status.episode && current.continuation_delivery === "waiting"
          && latest?.status === "issued" && latest.launch_event_id === launch.launch_event_id
          && latest.episode === launch.episode && latest.pane_id === launch.pane_id) {
        launch = { ...latest, status: "booted", boot_event_id: boot.id, process: process! };
        this.db.setConfig(PM_RESTART_LAUNCH_KEY, JSON.stringify(launch));
        this.db.logEvent(0, "process_restarted", null, null, { trigger: "exit", episode: status.episode,
          command: launch.command, reason: "late_owned_boot" });
      }
    }
    if (!launch || launch.status !== "booted" || launch.episode !== status.episode || launch.launch_event_id < exit.id
        || launch.boot_event_id !== boot.id || !ProcessHealthChecker.ownedAgent(launch.process ?? null)) {
      this.noteUncertainContinuation(status.episode, "boot_not_bound_to_mop_launch", exit.id, boot.id);
      return;
    }
    const ownedProcess = launch.process!;
    if ((await this.pmAgentProcess(launch.pane_id)) !== ownedProcess) {
      this.noteUncertainContinuation(status.episode, "boot_process_not_owned", exit.id, boot.id);
      return;
    }
    const processGuard = async () => (await this.pmAgentProcess(launch.pane_id)) === ownedProcess ? null : "pm_process_changed";
    let payload: Record<string, unknown>;
    let receipt: Record<string, unknown>;
    try { payload = JSON.parse(boot.payload); receipt = JSON.parse(exit.payload); }
    catch { return; }
    if (receipt.episode !== status.episode
        || !["startup", "resume"].includes(String(payload.source))
        || typeof payload.session_id !== "string" || !payload.session_id.trim()) return;
    let restart: { dead: number; restarted: number } | null = null;
    if (receipt.delivery !== "delivered") {
      if (receipt.delivery !== "uncertain") return;
      const dead = this.db.getEvents(0, 1, "process_dead")[0];
      const restarted = this.db.getEvents(0, 1, "process_restarted")[0];
      if (!dead || !restarted || dead.id <= exit.id || restarted.id <= dead.id || boot.id <= dead.id) {
        this.noteUncertainContinuation(status.episode, "no_watchdog_restart_after_attempt", exit.id, boot.id);
        return;
      }
      restart = { dead: dead.id, restarted: restarted.id };
    }
    const identity = await verifyPaneIdentity(0, this.runShell);
    if (!identity.ok || payload.cwd !== identity.snapshot.currentPath) {
      if (restart) this.noteUncertainContinuation(status.episode, "pane_ownership_unverified", exit.id, boot.id);
      return;
    }
    if (latestSessionStartEventId(this.db, 0) !== boot.id
        || this.db.getExitStatus().episode !== status.episode) return;
    if (identity.snapshot.paneId !== launch.pane_id) return;
    await this.deliverPMRestartContinuation(identity.snapshot.paneId, boot.id, restart ? {
      exit_event_id: exit.id, exit_delivery: "uncertain",
      process_dead_event_id: restart.dead, process_restarted_event_id: restart.restarted, boot_event_id: boot.id,
    } : undefined, processGuard);
  }

  private readonly uncertainContinuationNoted = new Set<string>();

  /** Typed, once per episode+reason: the continuation stays waiting; the exit receipt stays uncertain. */
  private noteUncertainContinuation(episode: string | null, reason: string, exitId: number, bootId: number): void {
    const key = `${episode}:${reason}`;
    if (this.uncertainContinuationNoted.has(key)) return;
    this.uncertainContinuationNoted.add(key);
    this.db.logEvent(0, "pm_restart_continuation_uncertain", null, null, {
      episode, reason, exit_event_id: exitId, boot_event_id: bootId,
    });
  }

  private async deliverPMRestartContinuation(paneId: string, expectedBoot?: number, claim?: Record<string, unknown>,
    processGuard?: () => Promise<string | null>): Promise<void> {
    const status = this.db.getExitStatus();
    if (!status.pending || !status.pm_only || !status.cycled[0]
        || status.continuation_delivery !== "waiting") return;
    // The fresh PM has no resumed session: a saved episode continuation wins,
    // otherwise the standard handoff-pointing continuation (never bare "continue").
    const prompt = status.continuation_prompt ?? PM_RESTART_CONTINUATION_PROMPT;
    const boot = latestSessionStartEventId(this.db, 0);
    if (expectedBoot !== undefined && boot !== expectedBoot) return;
    this.db.setConfig("exit_pending_continuation_delivery", "attempted");
    // Durable attempt-before-send claim bound to the restart evidence (uncertain-exit path).
    if (claim) this.db.logEvent(0, "pm_restart_continuation_claim", null, null, { episode: status.episode, ...claim });
    const fence = () => {
      const current = this.db.getExitStatus();
      return !current.pending || !current.pm_only || current.episode !== status.episode || current.continuation_prompt !== status.continuation_prompt
        ? "exit_pending_changed" : latestSessionStartEventId(this.db, 0) !== boot ? "pm_session_changed" : null;
    };
    const result = await this.relay.deliverGuardedToSlot(0, prompt, {
      expectedPaneId: paneId,
      recheck: async () => fence() ?? (processGuard ? await processGuard() : null) ?? fence(), finalCheck: fence, bracketedPaste: true,
    });
    this.db.logEvent(0, "pm_restart_continuation", null, null, {
      episode: status.episode, boot_event_id: boot, delivery: result.outcome,
    });
    if (result.outcome !== "delivered" || fence()) return;
    if (status.with_devs) {
      // 04:00 single occurrence (obligation 30559): the devs were armed in THIS
      // episode already. Keep the episode and every dev cycle flag (no second
      // dev phase); the PM part is done. Clear when every dev has cycled.
      // with_devs stays set: it marks this episode's arm-time dev cohort.
      this.db.setConfig("exit_pending_pm_only", "false");
      this.db.setConfig("exit_pending_dev_only", "true");
      this.db.setConfig("exit_pending_stop_only", "true");
      this.db.setConfig("exit_pending_continuation_delivery", "delivered");
      const after = this.db.getExitStatus();
      const devsDone = Object.entries(after.cycled).every(([slot, v]) => Number(slot) === 0 || v || this.db.isExitSlotRetained(Number(slot)));
      this.db.logEvent(0, "pm_restart_continuation_with_devs", null, null, {
        episode: status.episode, devs_all_cycled: devsDone,
        reason: "PM continuation delivered; dev slots of the same episode keep their cycle flags",
      });
      if (devsDone && !this.relay.hasPendingExitConfirmation?.(after.episode)) {
        this.db.setExitPending(false);
        this.db.logEvent(0, "exit_pending_complete", null, null, {
          source: "pm_restart_continuation", reason: "PM continuation delivered and every dev slot already cycled",
        });
      }
      this.unresponsiveStates.delete(0);
      return;
    }
    // Rajiv 1793502605.714159: only AFTER PM has booted and received its
    // continuation do we reset dev flags. DND and normal Stop handling remain.
    this.db.setExitPending(true, true);
    this.db.setConfig("exit_pending_stop_only", "true");
    this.db.setConfig("exit_pending_continuation_delivery", "delivered");
    this.db.logEvent(0, "dev_exit_pending_after_pm_restart", null, null, {
      reason: "PM continuation delivered; dev slots exit at their next Stop, DND skipped",
    });
    this.unresponsiveStates.delete(0);
  }

  // ─── Lifecycle ─────────────────────────────────────────

  private async runCheck(): Promise<void> {
    if (this.checkInFlight) {
      console.warn("[health] Skipping overlapping health check");
      return;
    }
    this.checkInFlight = true;
    try {
      await this.checkAll();
    } catch (err) {
      console.error("[health] Check failed:", err);
    } finally {
      this.checkInFlight = false;
    }
  }

  start(): void {
    if (this.timer) return;
    for (const dir of Object.values(SLOT_JSONL_DIRS)) {
      recentJsonlActivity.watchDirectory(dir);
    }
    this.timer = setInterval(() => {
      void this.runCheck();
    }, this.CHECK_INTERVAL_MS);
    console.log(
      `[health] Process health checker started — ` +
      `checking every ${this.CHECK_INTERVAL_MS / 1000}s, ` +
      `${this.RESTART_COOLDOWN_MS / 1000}s cooldown, ` +
      `max ${this.MAX_RESTARTS_PER_HOUR} restarts/hr`,
    );
  }

  stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
