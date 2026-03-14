/**
 * MoP Log Manager — Persistent pane logging via `tmux pipe-pane`
 *
 * Replaces lossy `tmux capture-pane` snapshots with continuous output
 * streaming to per-slot log files. Provides:
 * - Tail reading (last N bytes of a slot's log)
 * - Log rotation (cap at 100KB, keep last 50KB)
 * - Mtime queries (for stuck detection)
 * - pipe-pane initialization on server startup
 */

import { execSync } from "node:child_process";
import {
  closeSync,
  openSync,
  readSync,
  statSync,
  writeFileSync,
} from "node:fs";

export class LogManager {
  private readonly MAX_LOG_SIZE = 100 * 1024; // 100KB per slot
  private readonly KEEP_SIZE = 50 * 1024; // Keep last 50KB on rotation
  private readonly LOG_DIR = "/tmp";

  getLogPath(slot: number): string {
    return `${this.LOG_DIR}/slot-${slot}.log`;
  }

  /**
   * Read last N bytes from a slot's log file.
   * More reliable than capture-pane — never loses content that scrolled past.
   */
  tailLog(slot: number, bytes: number = 4096): string {
    const logPath = this.getLogPath(slot);
    try {
      const stat = statSync(logPath);
      if (stat.size === 0) return "";

      const start = Math.max(0, stat.size - bytes);
      const readSize = Math.min(bytes, stat.size);
      const fd = openSync(logPath, "r");
      const buf = Buffer.alloc(readSize);
      readSync(fd, buf, 0, readSize, start);
      closeSync(fd);

      // Strip ANSI escape codes for cleaner output
      return stripAnsi(buf.toString("utf-8"));
    } catch {
      return "";
    }
  }

  /**
   * Rotate a slot's log if it exceeds MAX_LOG_SIZE.
   * Keeps the last KEEP_SIZE bytes, discards the rest.
   */
  rotateIfNeeded(slot: number): boolean {
    const logPath = this.getLogPath(slot);
    try {
      const stat = statSync(logPath);
      if (stat.size > this.MAX_LOG_SIZE) {
        const tail = this.tailLog(slot, this.KEEP_SIZE);
        writeFileSync(logPath, tail);
        return true; // Rotated
      }
      return false;
    } catch {
      return false;
    }
  }

  /**
   * Get a slot log's last-modified time.
   * Used by StuckDetector to determine if a slot is producing output.
   */
  getLogMtime(slot: number): Date | null {
    try {
      return statSync(this.getLogPath(slot)).mtime;
    } catch {
      return null;
    }
  }

  /**
   * Get a slot log's file size in bytes.
   */
  getLogSize(slot: number): number {
    try {
      return statSync(this.getLogPath(slot)).size;
    } catch {
      return 0;
    }
  }

  /**
   * Initialize tmux pipe-pane for all slots.
   * Each slot's output streams to /tmp/slot-N.log.
   *
   * The -o flag captures output only (not input keystrokes).
   * Idempotent — if already piping, pipe-pane replaces the existing pipe.
   */
  enableLogging(slotCount: number): void {
    for (let i = 1; i <= slotCount; i++) {
      const logPath = this.getLogPath(i);
      try {
        // Touch log file so it exists for stat queries
        writeFileSync(logPath, "", { flag: "a" });

        // Enable pipe-pane — streams all output to log file
        execSync(`tmux pipe-pane -t "0:0.${i}" -o 'cat >> ${logPath}'`, {
          timeout: 5_000,
        });
      } catch (err) {
        console.error(`[logs] Failed to enable pipe-pane for slot ${i}:`, err);
      }
    }
    console.log(`[logs] Pipe-pane enabled for ${slotCount} slots → ${this.LOG_DIR}/slot-N.log`);
  }

  /**
   * Disable pipe-pane for all slots (cleanup).
   */
  disableLogging(slotCount: number): void {
    for (let i = 1; i <= slotCount; i++) {
      try {
        execSync(`tmux pipe-pane -t "0:0.${i}"`, { timeout: 5_000 });
      } catch {
        // Ignore — pane may not exist
      }
    }
  }
}

// ─── Helpers ──────────────────────────────────────────────

/**
 * Strip ANSI escape codes from a string.
 * Covers: colors, cursor movement, erase sequences, OSC sequences.
 */
function stripAnsi(str: string): string {
  // eslint-disable-next-line no-control-regex
  return str.replace(/\x1B(?:\[[0-9;]*[A-Za-z]|\][^\x07]*\x07|\][^\x1B]*\x1B\\)/g, "");
}
