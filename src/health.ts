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
 * PM auto-revival: When the PM process (pane 0:0.0) dies, MoP detects it
 * and restarts with `claude-pm --continue`. This is the most critical case —
 * PM coordinates all slots, so downtime must be minimized.
 */

import { execSync } from "node:child_process";
import type { MoPDatabase } from "./db.js";
import type { TmuxRelay } from "./relay.js";

// ─── Restart Commands ──────────────────────────────────────
// These are shell aliases defined in ~/.zshrc. Since tmux panes
// run interactive zsh shells, aliases are available after Claude exits.

const RESTART_COMMANDS: Record<number, string> = {
  0: "claude-pm --continue",
  1: "claude-dev-1 --continue",
  2: "claude-qa-2 --continue",
  3: "claude-dev-3 --continue",
  4: "claude-qa --continue",
};

/** Shell command names that indicate Claude Code has exited back to the shell */
const SHELL_COMMANDS = new Set(["zsh", "bash", "sh", "fish"]);

// ─── Health Checker ────────────────────────────────────────

export class ProcessHealthChecker {
  private readonly CHECK_INTERVAL_MS = 30 * 1000;       // Check every 30s
  private readonly RESTART_COOLDOWN_MS = 120 * 1000;    // 2min cooldown per slot
  private readonly MAX_RESTARTS_PER_HOUR = 3;           // Prevent crash loops
  private timer: NodeJS.Timeout | null = null;
  private readonly startTime = Date.now();        // Startup grace period anchor

  /** slot -> timestamp of last restart */
  private lastRestart = new Map<number, number>();
  /** slot -> restart count in current hour window */
  private restartCounts = new Map<number, { count: number; windowStart: number }>();

  constructor(
    private db: MoPDatabase,
    private relay: TmuxRelay,
  ) {}

  // ─── Detection ─────────────────────────────────────────

  /**
   * Get the current command running in a tmux pane.
   * Returns "claude" when Claude Code is running, "zsh" when it's dead.
   */
  private getPaneCommand(slotNum: number): string | null {
    const paneAddress = `0:0.${slotNum}`;
    try {
      const result = execSync(
        `tmux display-message -t ${paneAddress} -p '#{pane_current_command}'`,
        { timeout: 5_000, stdio: ["pipe", "pipe", "pipe"] },
      ).toString().trim();
      return result || null;
    } catch {
      return null; // Pane doesn't exist or tmux error
    }
  }

  /**
   * Check if a slot's Claude Code process is dead.
   * Dead = pane shows a shell (zsh/bash) instead of "claude".
   */
  isProcessDead(slotNum: number): boolean {
    const cmd = this.getPaneCommand(slotNum);
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
  private restartSlot(slotNum: number): boolean {
    const restartCmd = RESTART_COMMANDS[slotNum];
    if (!restartCmd) return false;

    const paneAddress = `0:0.${slotNum}`;
    try {
      // Send restart command to the pane's shell
      // The pane should be at a zsh prompt after Claude Code died
      execSync(
        `tmux send-keys -t ${paneAddress} '${restartCmd}' Enter`,
        { timeout: 10_000, stdio: ["pipe", "pipe", "pipe"] },
      );
      return true;
    } catch (err) {
      console.error(`[health] Failed to restart slot ${slotNum}:`, err);
      return false;
    }
  }

  // ─── Main Check Loop ──────────────────────────────────

  /**
   * Check all slots (0-4) for dead processes and restart if needed.
   *
   * Slot 0 = PM (claude-pm)
   * Slots 1-3 = Dev (claude-dev-N)
   * Slot 4 = QA (claude-qa)
   */
  checkAll(): void {
    const now = Date.now();

    // Startup grace period — skip checks for first 30s after MoP starts
    // Prevents false "process dead" alerts during MoP restart
    if (now - this.startTime < 30_000) return;

    for (let slot = 0; slot <= 4; slot++) {
      // Skip if on cooldown (recently restarted)
      const lastTime = this.lastRestart.get(slot);
      if (lastTime && now - lastTime < this.RESTART_COOLDOWN_MS) continue;

      // DND does NOT skip health checks — Claude must run on all slots always.
      // DND only means "don't assign new work" — NOT "don't keep alive."
      // Rajiv directive (2026-03-16): "heartbeat should ignore dnd. claude has to run on all ports all the time."

      // Check if the Claude Code process is dead
      if (!this.isProcessDead(slot)) continue;

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

      // ─── Process is dead — restart it ───────────────────
      const slotName = slot === 0 ? "PM" : `slot ${slot}`;
      console.log(`[health] ${slotName} process DEAD — restarting with --continue`);

      // Log the death event
      this.db.logEvent(slot, "process_dead", null, null, {
        detected_at: new Date().toISOString(),
        pane_command: this.getPaneCommand(slot),
      });

      const restarted = this.restartSlot(slot);
      if (restarted) {
        this.lastRestart.set(slot, now);
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
          // PM died — can't notify PM. Just log.
          console.log("[health] PM process restarted — PM will resume via --continue");
        }

        // Post-restart continue injection: wait one check cycle (30s),
        // verify Claude is running, then send "continue" to resume session.
        this.scheduleContinueInjection(slot);
      }
    }

    // After dead-process checks, scan for exit_pending on idle slots
    this.checkExitPending();
  }

  // ─── Exit Pending: Scan for Already-Idle Slots ─────────

  /**
   * When exit_pending is set, slots that go idle AFTER the flag
   * trigger /exit via handleStop() in hooks.ts. But slots that
   * were ALREADY idle when the flag was set would never trigger.
   *
   * This method runs every 30s in checkAll() and catches those
   * already-idle slots: if exit_pending is true, process is alive
   * (cmd=claude), slot is idle, and hasn't cycled yet → send /exit.
   */
  private checkExitPending(): void {
    if (!this.db.getExitPending()) return;

    const status = this.db.getExitStatus();

    // Check all slots including PM (slot 0) — MoP triggers exit for all panes
    for (let slot = 0; slot <= 4; slot++) {
      // Already cycled — skip
      if (status.cycled[slot]) continue;

      // Skip DND slots (slot 0/PM has no DB entry — never DND)
      if (slot > 0) {
        try {
          const slotState = this.db.getSlot(slot);
          if (slotState?.dnd) continue;
        } catch {
          continue;
        }
      }

      // Process must be alive (cmd=claude) — if dead, the main
      // checkAll loop handles restart, not /exit
      const cmd = this.getPaneCommand(slot);
      if (!cmd || SHELL_COMMANDS.has(cmd)) continue;

      // Slot must be idle (not actively processing)
      if (this.relay.isSlotActive(slot)) continue;

      // ─── Idle + alive + exit_pending + not cycled → send /exit ──
      const exitLabel = slot === 0 ? "PM" : `slot ${slot}`;
      console.log(`[health] exit_pending: ${exitLabel} already idle — sending /exit`);
      this.relay.sendToSlot(slot, "/exit", true);
      this.db.markSlotExitCycled(slot);
      this.db.logEvent(slot, "exit_pending_triggered", null, null, {
        source: "health_check",
        reason: `${exitLabel} was already idle when exit_pending was set`,
      });
      // Can't inject to PM when PM itself is the target — just log
      if (slot !== 0) {
        this.relay.injectToPM(
          `# 🔄 slot ${slot} sent /exit (exit_pending — was already idle) — watchdog will restart`,
        );
      }
    }

    // Check if all slots have now cycled
    const updatedStatus = this.db.getExitStatus();
    const allCycled = Object.entries(updatedStatus.cycled).every(([, v]) => v);
    if (allCycled) {
      this.db.setExitPending(false);
      this.relay.injectToPM("# ✅ All slots have cycled — exit_pending auto-cleared");
      this.db.logEvent(0, "exit_pending_complete", null, null, {
        source: "health_check",
        reason: "All slots (0-4) have cycled — flag cleared",
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
    const timer = setTimeout(() => {
      const cmd = this.getPaneCommand(slotNum);
      if (cmd !== "claude") {
        console.log(
          `[health] Slot ${slotNum} not yet at claude prompt (cmd: ${cmd}) — skipping continue injection`,
        );
        return;
      }

      const paneAddress = `0:0.${slotNum}`;
      try {
        execSync(
          `tmux send-keys -t ${paneAddress} 'continue' Enter`,
          { timeout: 10_000, stdio: ["pipe", "pipe", "pipe"] },
        );
        console.log(`[health] Sent "continue" to slot ${slotNum} after restart`);
        this.db.logEvent(slotNum, "continue_injected", null, null, {
          reason: "Post-restart continue injection — session resuming",
        });
      } catch (err) {
        console.error(`[health] Failed to send continue to slot ${slotNum}:`, err);
      }
    }, this.CHECK_INTERVAL_MS); // Wait one check cycle (30s)

    // Don't prevent process exit
    if (timer.unref) timer.unref();
  }

  // ─── Lifecycle ─────────────────────────────────────────

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => {
      try {
        this.checkAll();
      } catch (err) {
        console.error("[health] Check failed:", err);
      }
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
