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
 * continuation pompt at the claude prompt" ... "MoP should run the right launcher and then inject the
 * continuation prompt or point to the saved handoff file." The PM is relaunched with
 * the configured model plus `--continue` (Rajiv C0ALZJHGE49 thread 1795715075.690009,
 * ts 1795716529.803219: "enter should be triggered by MoP after a delay. note that
 * restart should use --continue."), and still receives ONE continuation prompt once
 * its Claude input prompt is up. PM crash recovery uses the same path. Dev slots keep
 * `--continue` + "continue".
 */

import { createHash, randomUUID } from "node:crypto";
import { existsSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { execShell } from "./asyncCommand.js";
import type { MoPDatabase } from "./db.js";
import { recentJsonlActivity } from "./jsonlActivity.js";
import { parsePMExitMenu, type TmuxRelay } from "./relay.js";
import { DEV_SLOT_NUMBERS, RUNTIME_SLOT_NUMBERS, SLOT_RUNTIME_IDENTITIES, isDecommissionedSlot } from "./slotConfig.js";
import { paneAddress, paneAddress as resolvePaneAddress, verifyPaneIdentity, type PaneCommandRunner } from "./paneIdentity.js";
import { pinFrom, pinDrift } from "./interruptBeforeClear.js";
import { latestSessionStartEventId } from "./sessionStartClearWait.js";
import { composerText } from "./composer.js";
import { withSlotSendLock } from "./slotSendLock.js";
import { pmExitStallReason } from "./pmHandoffExit.js";

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
/** Prints the OS-reported executable path of argv[1]'s pid (libproc proc_pidpath), or nothing when unreadable. */
const PROC_PIDPATH_SCRIPT =
  "import ctypes, sys; b = ctypes.create_string_buffer(4096); "
  + "n = ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidpath(int(sys.argv[1]), b, 4096); "
  + "sys.stdout.write(b.value.decode() if n > 0 else '')";
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
export type PmLauncher = "cliproxy" | "direct";
export const PM_DEFAULT_LAUNCHER: PmLauncher = "cliproxy";

/** The configured PM backend; an absent or unknown value keeps cliproxy. */
export function pmConfiguredLauncher(db: Pick<MoPDatabase, "getConfig">): PmLauncher {
  let value = "";
  try { value = (db.getConfig(PM_LAUNCHER_CONFIG_KEY) ?? "").trim(); } catch { value = ""; }
  return value === "direct" ? "direct" : PM_DEFAULT_LAUNCHER;
}

/** PM launcher command for a model and backend; runPMFreshRestart appends --continue. */
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
 * True when the pane's bottom line LOOKS like a shell prompt (zsh jreese theme renders `user@host <cwd> »` then `$ `).
 * Readiness only: the restart flow uses it to notice that the shell is back. It is NOT evidence that the shell input
 * is empty (a draft ending in a spaced `#`, `$`, `%` or `>` passes it), so it never authorizes a send; the launcher's
 * final guard is shellInputProvablyEmpty below (CTO REVISE C0ALZJHGE49 thread 1795715075.690009, ts 1795719337.686639).
 */
export function isShellPromptVisible(text: string | null | undefined): boolean {
  if (!text) return false;
  const lines = text.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").split(/\r?\n/).map((line) => line.trimEnd())
    .filter((line) => line.trim() !== "");
  const last = lines.at(-1)?.trim();
  if (!last || /^Pane is dead/.test(last)) return false;
  return /^(?:\S.*\s)?[$%#»]$/.test(last);
}

/** The PM shell's input line: ~/.zshrc appends a second prompt line `$ ` to the jreese theme. */
export const PM_SHELL_PROMPT_TAIL = "$ ";
/** The jreese prompt's first line, ANSI stripped: `user@host <cwd> [±branch] »` (`#` as root). */
const PM_PROMPT_HEAD = /^[^\s@]+@\S+ .*[»#]$/;

/** Cursor and screen state of a pane, read in ONE tmux call (see ProcessHealthChecker.pmShellInputEvidence). */
export type ShellInputEvidence = {
  cursorX: number;
  cursorY: number;
  historySize: number;
  alternateOn: boolean;
  inMode: boolean;
  /** Physical screen rows (capture-pane -p): index === cursor_y for the cursor row. */
  rows: string[];
  /** The same screen with wrapped rows joined (capture-pane -p -J). */
  joined: string[];
};

/**
 * Parses `display-message -p "<x> <y> <history> <alt> <mode> <height>" ; capture-pane -p ; display-message -p <nonce> ;
 * capture-pane -p -J`. The physical block is sliced by the pane height and the nonce line must sit right after it, so
 * pane text can never shift the split. Anything malformed is null (unreadable), never a guess.
 */
export function parseShellInputEvidence(stdout: string, nonce: string): ShellInputEvidence | null {
  const lines = stdout.split("\n");
  const head = /^(\d+) (\d+) (\d+) ([01]) ([01]) (\d+)$/.exec(lines[0] ?? "");
  if (!head) return null;
  const height = Number(head[6]);
  if (height < 1 || lines[1 + height] !== nonce) return null;
  const joined = lines.slice(2 + height);
  if (joined.at(-1) === "") joined.pop();
  return {
    cursorX: Number(head[1]), cursorY: Number(head[2]), historySize: Number(head[3]),
    alternateOn: head[4] === "1", inMode: head[5] === "1",
    rows: lines.slice(1, 1 + height), joined,
  };
}

/**
 * Evidence that the PM shell's input buffer is EMPTY, from cursor geometry rather than from what the text ends with
 * (CTO REVISE ts 1795719337.686639: "replace the prompt-suffix heuristic at the final send with evidence that the shell
 * input is actually empty"). All must hold:
 *  - normal screen, not copy mode;
 *  - the cursor sits exactly at the end of the `$ ` input prompt (cursor_x === 2), and that physical row is exactly `$`
 *    plus spaces: any typed character moves the cursor right, and a draft with the cursor moved home still shows its
 *    text after it on the row;
 *  - every physical row below the cursor is blank (no later buffer lines);
 *  - with wrapped rows joined, the last non-blank logical line is exactly `$` (the cursor row is not the wrapped tail of
 *    a long draft) and the logical line right above it is the jreese prompt head `user@host <cwd> »`. A draft line
 *    always follows the real `$ ` on its row, so it can never be that line.
 * Draft content alone cannot satisfy this; the caller also pins the cursor position across the launch delay and
 * re-checks it atomically at the send. A right prompt (RPS1 `N ↵` after a failed command) fails closed, as before.
 */
export function shellInputProvablyEmpty(evidence: ShellInputEvidence | null): boolean {
  if (!evidence || evidence.alternateOn || evidence.inMode) return false;
  const clean = (line: string) => line.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").trimEnd();
  const rows = evidence.rows.map(clean);
  if (evidence.cursorX !== PM_SHELL_PROMPT_TAIL.length) return false;
  if (rows[evidence.cursorY] !== PM_SHELL_PROMPT_TAIL.trimEnd()) return false;
  if (rows.slice(evidence.cursorY + 1).some((row) => row !== "")) return false;
  const logical = evidence.joined.map(clean);
  while (logical.length && logical.at(-1) === "") logical.pop();
  if (logical.at(-1) !== PM_SHELL_PROMPT_TAIL.trimEnd()) return false;
  return PM_PROMPT_HEAD.test(logical.at(-2) ?? "");
}

/** Same prompt cursor (same absolute row and column): nothing was typed, pasted, scrolled or cleared in between. */
export function samePromptCursor(a: ShellInputEvidence, b: ShellInputEvidence): boolean {
  return a.cursorX === b.cursorX && a.cursorY === b.cursorY && a.historySize === b.historySize;
}

/** tmux command-parser double quoting: inside "..." only `\`, `"` and `$` are special. */
function tmuxQuote(value: string): string {
  return `"${value.replace(/[\\"$]/g, (ch) => `\\${ch}`)}"`;
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

type PMRestartTrigger = "exit" | "crash" | "unresponsive" | "recover";
type PMRecoveryTarget = "session" | "window" | `%${number}` | `before:%${number}`;

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
  /** launcher typed -> the agent process runs in the pane (no idle prompt required; also bounds a non-episode continuation's pre-effect retries). */
  private PM_PROMPT_TIMEOUT_MS = 90_000;
  /**
   * Shell prompt seen -> the one guarded launcher submission (Rajiv ts 1795716529.803219: "enter should be triggered by MoP
   * after a delay"). The delay comes BEFORE the final empty-shell check, never between the text and its Enter (CTO REVISE
   * C0ALZJHGE49 thread 1795715075.690009, ts 1795717248.453549).
   */
  private PM_LAUNCH_DELAY_MS = 1_000;
  /** Submission sent -> the launcher (or the agent) must show up as a process; confirmation only, never a resend. */
  private PM_LAUNCH_CONFIRM_MS = 10_000;
  /** tmux runner for the PM restart effects (tests substitute a fake pane). */
  private runShell: PaneCommandRunner = execShell;
  /** Single owner of a PM restart; concurrent triggers join it. */
  private pmRestartInFlight: Promise<{ success: boolean; reason: string }> | null = null;
  private pmResumeInFlight = false;
  onPmExitStall?: (
    terminal: NonNullable<ReturnType<MoPDatabase["getPmExitStall"]>>,
    replacedHandoff: { requestedAt: string; obligationId: string | null } | null,
  ) => void;

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
  private async getPaneCommand(slotNum: number, exactPM = false, expectedPaneId?: string): Promise<string | null> {
    const identity = await verifyPaneIdentity(slotNum, this.runShell);
    if (!identity.ok) {
      console.warn(`[health] pane identity unavailable for slot ${slotNum}: ${identity.detail}`);
      return null;
    }
    const paneTarget = identity.snapshot.paneId;
    try {
      if (slotNum === 0) {
        const result = await this.runShell(
          `tmux display-message -t ${paneTarget} -p '#{pane_id}|#{pane_dead}|#{pane_current_command}|#{session_name}:#{window_index}.#{pane_index}'`,
          { timeout: 5_000 },
        );
        const [id, dead, command, address] = result.stdout.trim().split("|");
        if (id !== paneTarget || (expectedPaneId && id !== expectedPaneId)
            || (exactPM && address !== identity.snapshot.address) || dead !== "0" || !command) return null;
        if (this.isAgentCommand(0, command)) return command;
        const process = await this.pmAgentProcess(paneTarget);
        return ProcessHealthChecker.ownedAgent(process) ? "claude" : process === null ? null : command;
      }
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

  /**
   * Wait (bounded) for the PM pane to be back at an empty shell prompt after /exit, answering Claude Code's exit menu on
   * the way: when the "Background work is running" option menu is up, inject 1, then Enter once option 1 shows as
   * selected (Rajiv ts 1794999568.152419: "inject 1 if the exit option comes up"). The menu is only read while the pane
   * runs the agent, so scrollback left over after the exit never counts as a live menu. This is the only sender of that 1
   * for slot 0 (relay.ts leaves the PM menu to this flow). Probe failures just retry on the next poll.
   */
  private async waitForPMShellPrompt(timeoutMs: number, episode: string | null = null, allowExitMenu = true): Promise<{ paneId: string } | null> {
    const deadline = Date.now() + timeoutMs;
    const sent = new Set<string>();
    for (;;) {
      const command = await this.getPaneCommand(0, !allowExitMenu);
      const observed = command ? await this.relay.observeSlotPane(0) : null;
      if (observed && command) {
        if (SHELL_COMMANDS.has(command)) {
          if (isShellPromptVisible(observed.text)) return { paneId: observed.paneId };
        } else if (this.isAgentCommand(0, command)) {
          if (!allowExitMenu) return null; // A manual launch won recovery; do not touch it.
          if (parsePMExitMenu(observed.text)) await this.answerPMExitMenu(observed.paneId, episode, sent);
        }
      }
      if (Date.now() >= deadline) return null;
      await this.sleepMs(Math.min(this.PM_RESTART_POLL_MS, deadline - Date.now()));
    }
  }

  /** Durable per-episode record (db.setExitPending resets it) of a restart step whose effect was attempted. */
  private restartStepDone(episode: string | null, step: string): boolean {
    return !!episode && (this.db.getConfig("exit_pending_restart_steps") ?? "").split(",").includes(step);
  }

  /** Written BEFORE the step's effect: an uncertain or failed effect stays consumed, across a MoP restart too. */
  private markRestartStep(episode: string | null, ...step: string[]): void {
    if (!episode) return;
    const steps = (this.db.getConfig("exit_pending_restart_steps") ?? "").split(",").filter(Boolean);
    this.db.setConfig("exit_pending_restart_steps", [...steps, ...step].join(","));
  }

  /** True for a crash/unresponsive restart (no episode) or while the same exit episode is still pending. */
  private sameEpisode(episode: string | null): boolean {
    if (!episode) return true;
    const status = this.db.getExitStatus();
    return status.pending && status.episode === episode;
  }

  /** The VISIBLE screen of a pane (no scrollback, wrapped lines joined); null when it cannot be read. */
  private async visiblePane(paneId: string): Promise<string | null> {
    try { return (await this.runShell(`tmux capture-pane -t ${paneId} -p -J`, { timeout: 10_000 })).stdout; } catch { return null; }
  }

  /** Cursor + screen evidence for the PM shell input, in ONE tmux call; null when unreadable or malformed. */
  private async pmShellInputEvidence(paneId: string): Promise<ShellInputEvidence | null> {
    const nonce = `@@mop-evidence-${randomUUID()}@@`;
    const format = "#{cursor_x} #{cursor_y} #{history_size} #{alternate_on} #{pane_in_mode} #{pane_height}";
    try {
      const { stdout } = await this.runShell(`tmux display-message -p -t ${paneId} ${shellEscape(format)} \\; `
        + `capture-pane -p -t ${paneId} \\; display-message -p -t ${paneId} ${shellEscape(nonce)} \\; `
        + `capture-pane -p -J -t ${paneId}`, { timeout: 10_000 });
      return parseShellInputEvidence(stdout, nonce);
    } catch { return null; }
  }

  /**
   * ONE exit-menu key, inside the slot send lock. Right before the key it rechecks the same pane, the menu on the visible
   * screen (older scrollback never counts) and the same episode, then records the key durably first (CTO REVISE
   * ts 1795004139.496649, finding 2). An unreadable pane just waits for the next poll within the caller's bound.
   */
  private async answerPMExitMenu(paneId: string, episode: string | null, sent: Set<string>): Promise<void> {
    await withSlotSendLock(0, async () => {
      const done = (key: string) => sent.has(key) || this.restartStepDone(episode, `menu_${key}`);
      const key = !done("1") ? "1" : !done("Enter") ? "Enter" : null;
      if (!key) return;
      const identity = await verifyPaneIdentity(0, this.runShell);
      if (!identity.ok || identity.snapshot.paneId !== paneId) return;
      const screen = await this.visiblePane(paneId);
      const menu = screen === null ? null : parsePMExitMenu(screen);
      if (!menu || (key === "Enter" && menu.selected !== 1) || !this.sameEpisode(episode)) return;
      sent.add(key);
      this.markRestartStep(episode, `menu_${key}`);
      await this.runShell(`tmux send-keys -t ${paneId} ${key}`, { timeout: 10_000 });
    }).catch(() => undefined);
  }

  /**
   * Poll until the PM pane runs the agent, bounded. NOT until it is idle: a replacement that is busy (booting, working a
   * resumed turn, handling an inbound) is still the replacement, and its boot and replacement receipts must be written
   * without waiting for a quiet prompt (CTO REVISE C0ALZJHGE49 thread 1794970059.617269, ts 1794973351.556359, item 1).
   * The guarded writer decides when the composer can safely take the continuation.
   */
  private async waitForPMAgentProcess(timeoutMs: number, recoveryPaneId?: string): Promise<string | null> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const command = await this.getPaneCommand(0, recoveryPaneId !== undefined, recoveryPaneId);
      if (this.isAgentCommand(0, command)) return command;
      if (Date.now() >= deadline) return null;
      await this.sleepMs(Math.min(this.PM_RESTART_POLL_MS, deadline - Date.now()));
    }
  }

  /**
   * The PM restart contract (see runPMFreshRestart): after /exit, wait for the zsh prompt (answering the exit menu with 1),
   * type the configured-model launcher, then wait for the agent process. Planned restarts stay fresh with one
   * continuation prompt; PM availability recovery resumes the previous conversation without sending another prompt.
   * Every wait is bounded and none gates PM input. Recovery never force-kills a pane.
   */
  restartPMFresh(trigger: PMRestartTrigger, episode: string | null = null, target: PMRecoveryTarget | null = null): Promise<{ success: boolean; reason: string }> {
    if (this.pmRestartInFlight) return this.pmRestartInFlight;
    if (this.pmInitiatedRespawns.has(0)) return Promise.resolve({ success: false, reason: "controlled_respawn_owns_pm" });
    // A just-completed restart owns the cooldown: a late duplicate trigger must
    // never relaunch (or force-respawn) the freshly booted PM.
    if (this.isRestartSuppressed(0)) return Promise.resolve({ success: false, reason: "restart_cooldown" });
    this.pmInitiatedRespawns.add(0);
    const run = (async () => {
      let launched = false;
      try {
        const result = await this.runPMFreshRestart(trigger, episode, target);
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
   * A bare "claude" argv[0] (launch-pm.sh does `exec claude`) is owned only when
   * the process's resolved executable image is one of the trusted paths below.
   * 06f1d4b dropped bare names entirely, so every launch-pm.sh restart failed
   * with launch_boot_process_unowned (boot_process "<pane>:") from 2026-11-11.
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
    const children: string[] = [];
    for (const row of rows) {
      if (row[2] !== pane) continue;
      const argv0 = row[3].split(/\s/, 1)[0];
      if (executables.has(argv0)) { children.push(row[1]); continue; }
      // `exec claude` (launch-pm.sh) leaves a bare "claude" argv[0]: the name the
      // launcher typed, not a path, so it proves nothing by itself. Resolve the
      // process's real executable image and apply the SAME trusted-path test.
      // An unresolvable image is UNKNOWN (null), never "no Claude child".
      if (argv0 === "claude") {
        const image = await this.processImagePath(row[1]);
        if (image === null) return null;
        if (executables.has(image)) children.push(row[1]);
      }
    }
    if (children.length > 1) return null;
    return `${pane}:${children[0] ?? ""}`;
  }

  /**
   * The executable image the OS reports for a live pid (libproc proc_pidpath), or null when it cannot be read as
   * exactly one absolute path. lsof's txt mappings list the executable AND every mapped library in no guaranteed
   * order, so no position in them identifies the main image (CTO REVISE ts 1794949061.287419).
   */
  private async processImagePath(pid: string): Promise<string | null> {
    if (!/^\d+$/.test(pid)) return null;
    try {
      const out = (await this.runShell(`python3 -I -c ${shellEscape(PROC_PIDPATH_SCRIPT)} ${pid}`, { timeout: 5_000 })).stdout;
      const lines = out.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
      return lines.length === 1 && lines[0].startsWith("/") ? lines[0] : null;
    } catch {
      return null;
    }
  }

  /** A known identity with exactly one live Claude child. */
  private static ownedAgent(identity: string | null): boolean {
    return identity !== null && /^\d+:\d+$/.test(identity);
  }

  /**
   * The PM restart is exactly Rajiv's exit flow (C0ALZJHGE49 thread 1794998965.861819, ts 1794999568.152419: "MoP should
   * inject /exit at stop, inject 1 if the exit option comes up, then inject the launch pm command, then inject the
   * continuation prompt without any gates"). The Stop hook injects /exit (hooks.ts handleStop); this runs from there:
   *   1. wait for the shell prompt, answering the exit menu with 1 on the way (waitForPMShellPrompt);
   *   2. type the launch command (pmLaunchPlan: the configured launcher and model);
   *   3. wait for the replacement Claude process in the launched pane (SessionStart is evidence only);
   *   4. inject the continuation prompt (deliverPMRestartContinuation).
   * Every wait is bounded; a timeout logs pm_restart_failed once and stops. Nothing here holds PM input or tool calls
   * (rule 50). There is no pane/boot ownership pin, launch record or replacement binding: a transient tmux probe failure
   * only delays a wait, it never aborts the flow (the 16:21 IST 18 Nov restart aborted at a single-shot identity probe
   * before it launched anything).
   */
  private async runPMFreshRestart(trigger: PMRestartTrigger, episode: string | null, target: PMRecoveryTarget | null):
    Promise<{ success: boolean; launched: boolean; reason: string }> {
    const { model, launcher, command } = pmLaunchPlan(this.db);
    const recover = trigger === "recover";
    // Every PM restart resumes the previous conversation (Rajiv ts 1795716529.803219: "restart should use --continue").
    const launchCommand = `${command} --continue`;
    const cwd = SLOT_CWDS[0];
    const fail = (reason: string, launched = false, extra: Record<string, unknown> = {}) => {
      this.db.logEvent(0, "pm_restart_failed", null, null, { trigger, episode, model, reason, ...extra });
      console.error(`[health][pm-restart] ${trigger}: ${reason}`);
      return { success: false, launched, reason };
    };
    this.db.logEvent(0, "pm_restart_started", null, null, { trigger, episode, model, launcher, launch_command: launchCommand });
    if (recover && target) {
      try {
        const prepared = await withSlotSendLock(0, async () => {
          if (await this.probeMissingPMPane() !== target) return false;
          const startDir = shellEscape(cwd);
          if (target.startsWith("before:")) {
            const sibling = target.slice("before:".length);
            // tmux indices start at pane-base-index. In the same server turn,
            // require the same first pane in 0:0 with a positive base, split
            // before it, then restore base 0. Never replace a sibling process.
            const stillMissing = "#{&&:#{==:#{session_name}:#{window_index},0:0},#{&&:#{>:#{pane_index},0},#{==:#{pane_index},#{pane-base-index}}}}";
            const split = `split-window -bfd -t ${sibling} -P -F created -c ${startDir} ; set-option -w -t ${sibling} pane-base-index 0`;
            const result = await this.runShell(`tmux if-shell -F -t ${sibling} ${shellEscape(stillMissing)} ${shellEscape(split)}`, { timeout: 10_000 });
            return result.stdout.trim() === "created";
          }
          const create = target === "session" ? `tmux new-session -d -s 0 -n pm -c ${startDir}`
            : target === "window" ? `tmux new-window -d -t '=0:0' -n pm -c ${startDir}`
              : `tmux respawn-pane -t ${target} -c ${startDir}`;
          // No -k: concurrent creation or a live pane must never be replaced.
          await this.runShell(create, { timeout: 10_000 });
          return true;
        });
        if (!prepared) return fail("pm_recovery_target_changed");
      } catch (err) {
        return fail(`pm_recovery_pane_create_failed: ${String(err).slice(0, 200)}`);
      }
    }

    // 1. Wait for a visibly empty shell. Availability recovery cannot kill a process or answer a live Claude's exit menu.
    let shell = await this.waitForPMShellPrompt(trigger === "unresponsive" ? 0 : this.PM_EXIT_SHELL_TIMEOUT_MS, episode, !recover);
    if (!shell) {
      if (trigger === "exit") return fail("exit_blocked_shell_not_observed_no_force");
      if (recover) return fail("pm_recovery_shell_not_observed_no_force");
      const command = await this.getPaneCommand(0);
      if (!command) return fail("shell_prompt_timeout_pane_unknown");
      const identity = await verifyPaneIdentity(0, this.runShell);
      if (!identity.ok) return fail(`force respawn refused: ${identity.detail}`);
      const respawnPane = identity.snapshot.paneId;
      if (this.isAgentCommand(0, command)) {
        if (trigger === "crash") return fail("shell_prompt_timeout_agent_running_no_force", false, { pane_command: command });
        if ((await this.relay.getSlotActivityState(0, respawnPane)) !== "idle") {
          return fail("shell_prompt_timeout_pm_busy_no_force", false, { pane_command: command });
        }
      }
      this.db.logEvent(0, "pm_restart_force_respawn", null, null, {
        trigger, episode, pane_command: command, reason: "shell prompt not observed within bound",
      });
      try {
        await this.runShell(`tmux respawn-pane -k -t ${respawnPane} -c ${shellEscape(cwd)}`, { timeout: 10_000 });
        this.lastForceRespawn.set(0, Date.now());
      } catch (err) {
        return fail(`force respawn failed: ${String(err).slice(0, 200)}`);
      }
      shell = await this.waitForPMShellPrompt(this.PM_FORCE_SHELL_TIMEOUT_MS, episode);
      if (!shell) return fail("shell_prompt_not_observed_after_force_respawn");
    }
    this.unresponsiveStates.delete(0);
    const launchEventId = this.db.logEvent(0, "process_dead", null, null, {
      detected_at: new Date().toISOString(), pane_command: "shell_prompt", trigger, episode,
    });

    // 2. The launch command, typed at the shell prompt.
    const paneId = shell.paneId;
    const refusal = await this.sendPMLaunch(paneId, `cd ${shellEscape(cwd)} && ${launchCommand}`, episode, recover);
    if (refusal) return fail(refusal);
    this.db.logEvent(0, "pm_restart_launched", null, null, { trigger, episode, model, launcher, launch_command: launchCommand });

    // 3. The replacement Claude process in the launched pane. The destination is that live process plus the guarded
    //    composer below, not a SessionStart row (CTO REVISE finding 1): the SessionStart id is evidence only, and a late
    //    or missing hook delays nothing. Busy or idle, the guarded writer decides when the composer can take the paste.
    const bootCommand = await this.waitForPMAgentProcess(this.PM_PROMPT_TIMEOUT_MS, recover ? paneId : undefined);
    if (!bootCommand) return fail(`claude process not observed within ${this.PM_PROMPT_TIMEOUT_MS}ms`, true);
    this.db.logEvent(0, "agent_booted", null, null, {
      command: bootCommand, launch_command: launchCommand, observed_at: new Date().toISOString(),
    });
    this.db.logEvent(0, "process_restarted", null, null, {
      command: launchCommand, restarted_at: new Date().toISOString(), trigger, model,
    });
    // Planned /exit: bind the replacement MoP launched to this exact attempt. Detached and best effort; it never delays
    // the continuation below or gates PM input.
    if (trigger === "exit" && episode) {
      void this.bindPMExitReplacement(paneId, episode, launchEventId)
        .catch((err) => console.error("[health][pm-restart] exit replacement receipt failed:", err));
    }

    // --continue already restored the session. Do not add a synthetic PM turn on recovery.
    if (recover) {
      void this.relay.drainPMQueue().catch((err) => console.error("[health] PM queue recovery drain failed:", err));
      return { success: true, launched: true, reason: "previous session resumed" };
    }

    // 4. The planned-restart continuation prompt.
    const identity = await this.pmAgentProcess(paneId);
    const outcome = await this.deliverPMRestartContinuation(paneId, latestSessionStartEventId(this.db, 0), trigger, episode,
      ProcessHealthChecker.ownedAgent(identity) ? identity : null);
    if (outcome !== "delivered") return fail(`continuation_${outcome}`, true);
    void this.relay.drainPMQueue();
    return { success: true, launched: true, reason: "continuation delivered" };
  }

  /**
   * The PM restart's `exit_replacement_bound` receipt, in the shape restartSlot writes for dev slots. Without it a PM
   * /exit replacement stayed UNKNOWN (replacement_not_owned_by_restart) and held exit custody (request 45610), and the
   * only way out was crediting a newer boot alone, which the CTO refused (C0ALZJHGE49 thread 1795798240.220189, ts
   * 1795798363.328589: "Restore proof-bound exit custody"). The receipt binds the launch (the process_dead row logged
   * before the launcher was typed), the single boot after it, the owned Claude child and its replacement identity, for
   * the exact attempt and assignment epoch. Bounded poll; anything ambiguous writes no receipt and the attempt stays
   * UNKNOWN. Never waits on idleness, never types, never gates input.
   */
  private async bindPMExitReplacement(paneId: string, episode: string, launchEventId: number): Promise<void> {
    const bootsAfterLaunch = () => this.db.getEvents(0, 50, "SessionStart").filter(e => e.id > launchEventId).sort((a, b) => a.id - b.id);
    const deadline = Date.now() + this.AGENT_BOOT_TIMEOUT_MS;
    let boots = bootsAfterLaunch();
    while (boots.length === 0 && Date.now() < deadline) {
      await this.sleepMs(Math.min(this.AGENT_BOOT_POLL_MS, deadline - Date.now()));
      boots = bootsAfterLaunch();
    }
    const unbound = (reason: string) => {
      this.db.logEvent(0, "pm_exit_replacement_unbound", null, null, { episode, launchEventId, reason });
    };
    const boot = boots[0];
    let payload: any = null;
    try { payload = JSON.parse(boot?.payload ?? "null"); } catch { /* UNKNOWN */ }
    if (boots.length !== 1 || !payload?.session_id || payload.cwd !== SLOT_CWDS[0] || !["startup", "resume"].includes(payload.source)) {
      return unbound(`boot_not_single_owned_launch (${boots.length})`);
    }
    const ownedProcess = await this.pmAgentProcess(paneId);
    const replacementProcess = await this.relay.exitProcessIdentity(0, paneId);
    const ownedAgain = await this.pmAgentProcess(paneId);
    if (!ProcessHealthChecker.ownedAgent(ownedProcess) || ownedAgain !== ownedProcess || !replacementProcess
        || latestSessionStartEventId(this.db, 0) !== boot.id) {
      return unbound("replacement_process_not_owned");
    }
    const attempt = this.db.getExitAttempt(0);
    const epoch = this.db.getSlot(0)?.assignment_epoch ?? 0;
    if (!attempt || attempt.episode !== episode || attempt.assignmentEpoch !== epoch || this.db.getExitStatus().episode !== episode
        || replacementProcess === attempt.originalProcess) {
      return unbound("attempt_moved");
    }
    this.db.logEvent(0, "exit_replacement_bound", null, null, {
      episode, submittedAt: attempt.submittedAt, assignmentEpoch: epoch, launchEventId,
      bootEventId: boot.id, bootSession: payload.session_id, ownedProcess, replacementProcess,
    });
  }

  /**
   * ONE owned launcher submission (CTO REVISE C0ALZJHGE49 thread 1795715075.690009, ts 1795717248.453549): "Make the
   * delayed action one owned launcher submission. Delay before the final guarded empty-shell check, then send the exact
   * --continue command and its terminator together through the existing pane owner. Use the process probe for
   * confirmation only. Preserve uncertain submissions as consumed."
   *
   * Final guard rework (CTO REVISE C0ALZJHGE49 thread 1795715075.690009, ts 1795719337.686639): "Keep the one combined
   * submission and replace the prompt-suffix heuristic at the final send with evidence that the shell input is actually
   * empty." A draft ending in a spaced `#`/`$`/`%`/`>` passed the old suffix check and would have been submitted.
   *
   * 1. Anchor: read the shell-input evidence (cursor + screen, one tmux call) and require shellInputProvablyEmpty. A
   *    pane that stays unreadable until the deadline, or a non-empty input, refuses and is consumed.
   * 2. Wait PM_LAUNCH_DELAY_MS (the 26 Nov restart left the typed launcher buffered; Rajiv ts 1795716529.803219).
   * 3. Inside the slot send lock, the final guard: same episode, the pane foreground is still a shell, same pane, the
   *    input is still provably empty AND the prompt cursor has not moved since the anchor (same absolute row and
   *    column). Anything a person typed, pasted or cleared meanwhile refuses at once, before any key, and that refusal is
   *    recorded ("launch") so later ticks of the same episode never retry it. Only an unreadable pane waits, bounded.
   * 4. The step is recorded durably BEFORE the effect. The send is ONE atomic tmux `if-shell -F`: the tmux server
   *    re-checks the same cursor position, normal screen and no copy mode, and only then runs `send-keys -l <text> ;
   *    send-keys Enter ; display-message -p sent` as one unit with no key from another client in between. Output other
   *    than `sent` means the condition changed (nothing typed) or the effect is uncertain: consumed, no retry. MoP never
   *    sends a bare Enter, so it can never submit text it did not type.
   * 5. The process probe only confirms. Unconfirmed or failed sends stay consumed: there is no second submission.
   * Returns a refusal or null.
   */
  private async sendPMLaunch(paneId: string, typed: string, episode: string | null, exactPM = false): Promise<string | null> {
    if (this.restartStepDone(episode, "launch") || this.restartStepDone(episode, "launched")) return "launch_already_attempted";
    const deadline = Date.now() + this.PM_LAUNCH_DELAY_MS + this.PM_PROMPT_TIMEOUT_MS;
    let anchor: ShellInputEvidence | null = null;
    for (;;) {
      anchor = await this.pmShellInputEvidence(paneId);
      if (anchor) break;
      if (Date.now() >= deadline) {
        this.markRestartStep(episode, "launch");
        return "pm_launch_screen_unreadable";
      }
      await this.sleepMs(Math.min(this.PM_RESTART_POLL_MS, deadline - Date.now()));
    }
    if (!shellInputProvablyEmpty(anchor)) {
      this.markRestartStep(episode, "launch");
      return "pm_launch_prompt_not_empty";
    }
    await this.sleepMs(this.PM_LAUNCH_DELAY_MS);
    for (;;) {
      const outcome = await withSlotSendLock(0, async () => {
        if (this.restartStepDone(episode, "launch") || this.restartStepDone(episode, "launched")) return "launch_already_attempted";
        if (!this.sameEpisode(episode)) return "exit_pending_changed";
        const command = await this.getPaneCommand(0, exactPM, exactPM ? paneId : undefined);
        if (!command || !SHELL_COMMANDS.has(command)) return "pm_shell_changed";
        const identity = await verifyPaneIdentity(0, this.runShell);
        if (!identity.ok) return "wait";
        if (identity.snapshot.paneId !== paneId) return "pm_pane_changed";
        const evidence = await this.pmShellInputEvidence(paneId);
        if (evidence === null) return "wait";
        if (!shellInputProvablyEmpty(evidence) || !samePromptCursor(anchor, evidence)) {
          // A refused submission is consumed too: later ticks of this episode never retry it behind a person's input.
          this.markRestartStep(episode, "launch");
          return "pm_launch_prompt_not_empty";
        }
        this.markRestartStep(episode, "launch", "launched");
        const unchanged = `#{&&:#{==:#{cursor_x},${evidence.cursorX}},#{&&:#{==:#{cursor_y},${evidence.cursorY}},`
          + `#{&&:#{==:#{history_size},${evidence.historySize}},#{&&:#{==:#{alternate_on},0},#{==:#{pane_in_mode},0}}}}}`;
        const submit = `send-keys -t ${paneId} -l ${tmuxQuote(typed)} ; send-keys -t ${paneId} Enter ; display-message -p sent`;
        const { stdout } = await this.runShell(`tmux if-shell -F -t ${paneId} ${shellEscape(unchanged)} ${shellEscape(submit)}`,
          { timeout: 10_000 });
        return stdout.trim() === "sent" ? "sent" : "pm_launch_prompt_changed";
      }).catch((err) => `launcher send failed: ${String(err).slice(0, 200)}`);
      if (outcome === "sent") break;
      if (outcome !== "wait") return outcome;
      if (Date.now() >= deadline) {
        this.markRestartStep(episode, "launch");
        return "pm_launch_screen_unreadable";
      }
      await this.sleepMs(Math.min(this.PM_RESTART_POLL_MS, deadline - Date.now()));
    }
    // Confirmation only. An UNKNOWN listing is left to the bounded agent-process wait that follows; a listing that
    // provably has neither the launcher nor the agent for the whole window (for example a fast launcher failure) fails
    // the restart once. Nothing is resent either way.
    const confirmBy = Date.now() + this.PM_LAUNCH_CONFIRM_MS;
    let submitted: boolean | null = null;
    for (;;) {
      submitted = await this.pmLaunchSubmitted(paneId, exactPM);
      if (submitted === true || Date.now() >= confirmBy) break;
      await this.sleepMs(Math.min(this.PM_RESTART_POLL_MS, confirmBy - Date.now()));
    }
    this.db.logEvent(0, "pm_launch_submitted", null, null, { episode, pane_id: paneId, confirmed: submitted });
    return submitted === false ? "pm_launch_not_confirmed" : null;
  }

  /**
   * Whether the typed launcher was submitted, read from processes only: true when the pane runs the agent, or the pane
   * shell has a direct child running launch-pm.sh or claude; false when the pane's terminal listing provably has none;
   * null when UNKNOWN (probe error, unreadable listing, listing without the pane shell). Other shell children, such as
   * prompt helpers (gitstatusd, async workers), never count as the launcher.
   */
  private async pmLaunchSubmitted(paneId: string, exactPM = false): Promise<boolean | null> {
    const command = await this.getPaneCommand(0, exactPM, exactPM ? paneId : undefined);
    if (this.isAgentCommand(0, command)) return true;
    return this.paneShellRunsLauncher(paneId);
  }

  private async paneShellRunsLauncher(paneId: string): Promise<boolean | null> {
    let pane = "";
    let tty = "";
    try {
      [pane, tty] = (await this.runShell(`tmux display-message -t ${paneId} -p '#{pane_pid}|#{pane_tty}'`, { timeout: 5_000 }))
        .stdout.trim().split("|");
    } catch {
      return null;
    }
    if (!/^\d+$/.test(pane ?? "") || !/^\/dev\/[\w/.-]+$/.test(tty ?? "")) return null;
    let listing = "";
    try {
      listing = (await this.runShell(`ps -t ${shellEscape(tty.replace(/^\/dev\//, ""))} -o pid=,ppid=,command=`, { timeout: 5_000 })).stdout;
    } catch {
      return null;
    }
    const rows = listing.split(/\r?\n/).map((line) => /^\s*(\d+)\s+(\d+)\s+(.*)$/.exec(line)).filter(Boolean) as RegExpExecArray[];
    if (!rows.some((row) => row[1] === pane)) return null;
    return rows.some((row) => row[2] === pane
      && (/(^|[\s/])launch-pm\.sh(\s|$)/.test(row[3]) || (row[3].split(/\s/, 1)[0].split("/").pop() ?? "") === "claude"));
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

  /** A failed checkout probe isn't proof of absence. Enumerate the exact PM window; display-message may fall back to another pane. */
  private async probeMissingPMPane(): Promise<PMRecoveryTarget | null> {
    try {
      const { stdout } = await this.runShell(
        "tmux list-panes -t '=0:0' -F '#{pane_index}|#{pane_id}|#{pane_dead}'", { timeout: 5_000 });
      const panes = stdout.trim().split(/\r?\n/).map(line => /^(\d+)\|(%\d+)\|([01])$/.exec(line));
      if (panes.some(pane => !pane)) return null;
      const pm = panes.find(pane => pane?.[1] === "0");
      if (pm) return pm[3] === "1" ? pm[2] as `%${number}` : null;
      const first = panes[0];
      return first ? `before:${first[2] as `%${number}`}` : null;
    } catch (err) {
      const message = String(err);
      if (/^(?:can't find session: 0|no server running on .+|error connecting to .+ \(No such file or directory\))$/m.test(message)) return "session";
      if (/^can't find window: 0$/m.test(message)) return "window";
      return null; // Unreadable is not a missing pane.
    }
  }

  /**
   * Check all slots (0-8) for dead processes and restart if needed.
   *
   * Slot 0 = PM (claude-pm)
   * Slots 1-8 = isolated dev runtimes.
   */
  async checkAll(boot = false): Promise<void> {
    const now = Date.now();
    const pmOnly = boot || now - this.startTime < 30_000;
    if (!pmOnly) this.reconcileStuckPmExit(now);

    for (const slot of RUNTIME_SLOT_NUMBERS) {
      // Keep the startup grace for dev slots, never for PM availability.
      if ((pmOnly && slot !== 0) || isDecommissionedSlot(slot)) continue;
      // Skip controlled or recently completed respawns.
      if (slot !== 0 && this.isRestartSuppressed(slot, now)) continue;

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

      const paneCommand = await this.getPaneCommand(slot, slot === 0);

      if (this.isAgentCommand(slot, paneCommand)) {
        if (slot === 0 && this.db.getPendingPMEventCount() > 0) {
          void this.relay.drainPMQueue().catch((err) => console.error("[health] PM queue drain failed:", err));
        }
        if (boot && slot === 0) console.log("[health] PM boot check: already running; no launch");
        if (!pmOnly && !this.isRestartSuppressed(slot)) await this.checkClaudeResponsiveness(slot);
        continue;
      }

      if ((!paneCommand || !SHELL_COMMANDS.has(paneCommand)) && slot !== 0) {
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

      // Run under the same single restart owner at boot and on later ticks.
      if (slot === 0) {
        const target = paneCommand ? null : await this.probeMissingPMPane();
        if ((!paneCommand && !target) || this.isRestartSuppressed(0)) continue;
        const status = this.db.getExitStatus();
        const planned = !target && status.pending && status.pm_only && this.db.hasExitAttemptSubmitted(0);
        const trigger = planned ? "exit" : "recover";
        console.log(`[health] PM unavailable (${target ?? paneCommand}); starting ${trigger} from ${boot ? "boot" : "tick"}`);
        void this.restartPMFresh(trigger, planned ? status.episode : null, target).catch((err) => console.error("[health] PM restart failed:", err));
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

    // After periodic dead-process checks, scan for exit_pending on idle slots.
    if (!pmOnly) await this.checkExitPending();
  }

  // ─── Exit Pending: process receipts only, never timer-driven /exit ───

  private reconcileStuckPmExit(now: number): void {
    if (this.pmRestartInFlight || this.pmResumeInFlight) return;
    const reason = pmExitStallReason(this.db, now);
    if (!reason) return;
    const status = this.db.getExitStatus();
    if (!status.episode || this.relay.hasPendingExitConfirmation(status.episode)) return;
    if (status.continuation_delivery === "attempted" && this.continuationDeliveryRecorded(status.episode)) return;
    const terminal = this.db.settleStuckPmExit(status.episode, reason, now);
    if (terminal) this.onPmExitStall?.(terminal, status.handoff_requested_at ? {
      requestedAt: status.handoff_requested_at, obligationId: status.handoff_obligation_id,
    } : null);
  }

  private async checkExitPending(): Promise<void> {
    await this.relay.observeExitAttempts();
    const status = this.db.getExitStatus();
    // A PM-only episode whose continuation was delivered while its /exit dialog confirmation was still open ends here.
    if (status.pending && status.pm_only && !status.with_devs && status.continuation_delivery === "delivered") {
      this.completePMOnlyEpisode(status.episode, "health_observer");
      return;
    }
    // A MoP restart mid-restart-flow: pick the PM-only episode up where it stopped (never blocks the tick or PM input).
    if (status.pending && status.pm_only) void this.resumePMExitEpisode(status).catch((err) => {
      console.error("[health] PM exit episode resume failed:", err);
    });
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

  /**
   * Next tick after a MoP restart (CTO REVISE ts 1795004139.496649, finding 3), only while no restart flow of this process
   * is running. An episode left at `attempted` whose continuation has an exact confirmed receipt settles through the same
   * atomic PM-only completion. A launch recorded durably (`launched`) whose continuation was never attempted (`waiting`)
   * and whose pane now runs exactly one live Claude resumes the unsent step. An `attempted` send with no receipt stays
   * consumed, and a launch with no live agent is left alone. Only slot 0 is read or written; dev slots are untouched.
   */
  private async resumePMExitEpisode(status: ReturnType<MoPDatabase["getExitStatus"]>): Promise<void> {
    if (this.pmRestartInFlight || this.pmResumeInFlight) return;
    if (status.continuation_delivery === "attempted") {
      if (this.continuationDeliveryRecorded(status.episode)) this.finishPMContinuation(status);
      return;
    }
    if (status.continuation_delivery !== "waiting" || !this.restartStepDone(status.episode, "launched")) return;
    this.pmResumeInFlight = true;
    try {
      const identity = await verifyPaneIdentity(0, this.runShell);
      if (!identity.ok) return;
      const process = await this.pmAgentProcess(identity.snapshot.paneId);
      if (!ProcessHealthChecker.ownedAgent(process)) return;
      await this.deliverPMRestartContinuation(identity.snapshot.paneId, latestSessionStartEventId(this.db, 0), "exit", status.episode, process);
    } finally {
      this.pmResumeInFlight = false;
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
        // A PM-only exit episode's continuation is injected by the restart flow (runPMFreshRestart), not here.
        if (slotNum === 0 && this.db.getExitStatus().pm_only) return;
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

  /** A delivered receipt for THIS episode is already recorded: its continuation is never injected a second time. */
  private continuationDeliveryRecorded(episode: string | null): boolean {
    if (!episode) return false;  // a crash restart's receipt carries episode null; it never settles an episode
    return this.db.getEvents(0, 20, "pm_restart_continuation").some((row) => {
      try {
        const payload = JSON.parse(row.payload);
        return payload.episode === episode && payload.delivery === "delivered";
      } catch { return false; }
    });
  }

  /**
   * A PM-only episode (pm_only, not with_devs) ends the moment its continuation is delivered: pending clears, the episode
   * reads `delivered` and `exit_pending_complete` is logged. Dev slots are never armed (Rajiv C0ALZJHGE49 thread
   * 1794970059.617269, ts 1794971912.636829; the RSS arm and pm-respawn.sh both promise a PM-only restart, and the
   * 2026-11-09 global exit-pending restarted a dev slot mid-task). setExitPending(false) resets the delivery marker, so the
   * final marker is written after it, and the clear, the marker and the completion record commit as one transaction
   * (db.completePMOnlyExitEpisode). An /exit dialog confirmation still open for the episode defers the clear to the next health tick
   * (checkExitPending); the episode already reads `delivered`, so the continuation is never resent.
   */
  private completePMOnlyEpisode(episode: string | null, source: string): void {
    if (this.relay.hasPendingExitConfirmation?.(episode)) {
      this.db.setConfig("exit_pending_continuation_delivery", "delivered");
      return;
    }
    // One transaction (CTO REVISE ts 1794975890.713959): a crash inside the terminal clear leaves the episode as it was.
    this.db.completePMOnlyExitEpisode(episode, source, "PM-only episode: continuation delivered, dev slots untouched");
    this.unresponsiveStates.delete(0);
  }

  /** The continuation is delivered (now, or by a receipt already on record): finish the episode. */
  private finishPMContinuation(status: ReturnType<MoPDatabase["getExitStatus"]>): void {
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
    this.completePMOnlyEpisode(status.episode, "pm_restart_continuation");
  }

  /**
   * Step 4 of the PM restart: inject the continuation into the new PM's input, with no idle timer, Stop wait or input
   * guard (Rajiv C0ALZJHGE49 thread 1794970059.617269, ts 1794971912.636829: "continuation delivery is supposed to be
   * injected right after the process is back up. it does not have to wait for stop or any other gate"). It is one
   * serialized paste on the same chain as every PM write, and a busy PM queues it behind its turn
   * (allowNativeQueuedInput). A PM-only exit episode sends its saved prompt under the durable waiting -> attempted claim
   * and ends when the delivery is confirmed; every other restart sends the standard handoff continuation once per boot.
   * A PROVEN pre-effect refusal (composer not ready on a fresh boot) wrote nothing and is retried within the bound; an
   * uncertain outcome stays consumed and is never resent. Returns the delivery outcome (or why nothing was sent).
   */
  private async deliverPMRestartContinuation(paneId: string, bootId: number, trigger: string,
    episode: string | null, processIdentity: string | null = null): Promise<string> {
    const status = this.db.getExitStatus();
    const owned = trigger === "exit" && !!episode && status.pending && status.pm_only && status.episode === episode;
    if (trigger === "exit" && episode && !owned) return "episode_changed";
    if (owned) {
      if (this.continuationDeliveryRecorded(episode)) {
        this.finishPMContinuation(status);
        return "delivered";
      }
      if (status.continuation_delivery !== "waiting") return `episode_${status.continuation_delivery ?? "unarmed"}`;
      this.db.setConfig("exit_pending_continuation_delivery", "attempted");
    } else {
      // One claim per boot: the SessionStart id may not exist yet (a late hook), so the launched process joins the token.
      const claimKey = "pm_restart_continuation_boot";
      const token = processIdentity ? `${bootId}|${processIdentity}` : String(bootId);
      if (this.db.getConfig(claimKey) === token) return "already_claimed";
      this.db.setConfig(claimKey, token);
    }
    const prompt = owned ? (status.continuation_prompt ?? PM_RESTART_CONTINUATION_PROMPT) : PM_RESTART_CONTINUATION_PROMPT;
    // Destination = the live replacement Claude process in the launched pane plus the guarded composer, never a newer
    // SessionStart row (CTO REVISE finding 1; SessionStart stays evidence in the receipt). `replaced` turns true only when a
    // DIFFERENT live process owns the pane (permanent); a probe that cannot name the process is a proven pre-effect refusal
    // that waits within the bound. The native queue behind ordinary inbound is unchanged (allowNativeQueuedInput).
    let replaced = false;
    const live = async (): Promise<boolean> => {
      const id = await this.pmAgentProcess(paneId);
      const agent = ProcessHealthChecker.ownedAgent(id);
      if (agent && processIdentity && id !== processIdentity) replaced = true;
      return agent && !replaced;
    };
    const fence = (): string | null => {
      if (owned) {
        const current = this.db.getExitStatus();
        if (!current.pending || !current.pm_only || current.episode !== episode
            || current.continuation_prompt !== status.continuation_prompt) return "exit_pending_changed";
      }
      return replaced ? "pm_process_changed" : null;
    };
    // Physical ownership of OUR unsubmitted paste, for the relay's draft cleanup inside its send lock.
    const continuationDraft = { sameBoot: () => !replaced, owns: live };
    const send = () => this.relay.deliverGuardedToSlot(0, prompt, {
      expectedPaneId: paneId, bracketedPaste: true, allowNativeQueuedInput: true, continuationDraft, finalCheck: fence,
      recheck: async () => { const refused = fence(); return refused ?? ((await live()) ? fence() : "pm_process_unavailable"); },
    });
    const deadline = Date.now() + this.PM_PROMPT_TIMEOUT_MS;
    let result = await send();
    while (result.outcome === "refused_pre_effect" && !fence() && Date.now() < deadline) {
      await this.sleepMs(Math.min(this.PM_RESTART_POLL_MS * 5, Math.max(0, deadline - Date.now())));
      result = await send();
    }
    this.db.logEvent(0, "pm_restart_continuation", null, null, {
      episode: owned ? episode : null, trigger, boot_event_id: bootId, delivery: result.outcome,
    });
    if (owned) {
      if (result.outcome === "refused_pre_effect") {
        // Proven zero effect: hand this episode's own claim back.
        if (!fence() && this.db.getExitStatus().continuation_delivery === "attempted") {
          this.db.setConfig("exit_pending_continuation_delivery", "waiting");
        }
      } else if (result.outcome === "delivered" && !fence()) {
        this.finishPMContinuation(status);
      }
    }
    return result.outcome;
  }

  // ─── Lifecycle ─────────────────────────────────────────

  private async runCheck(boot = false): Promise<void> {
    if (this.checkInFlight) {
      console.warn("[health] Skipping overlapping health check");
      return;
    }
    this.checkInFlight = true;
    try {
      await this.checkAll(boot);
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
    void this.runCheck(true);
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
