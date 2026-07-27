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

import { open, stat, writeFile } from "node:fs/promises";
import { execShell } from "./asyncCommand.js";

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
  async tailLog(slot: number, bytes: number = 4096): Promise<string> {
    const logPath = this.getLogPath(slot);
    let handle: Awaited<ReturnType<typeof open>> | null = null;
    try {
      const fileStat = await stat(logPath);
      if (fileStat.size === 0) return "";

      const start = Math.max(0, fileStat.size - bytes);
      const readSize = Math.min(bytes, fileStat.size);
      handle = await open(logPath, "r");
      const buf = Buffer.alloc(readSize);
      await handle.read(buf, 0, readSize, start);

      // Strip ANSI escape codes for cleaner output
      return stripAnsi(buf.toString("utf-8"));
    } catch {
      return "";
    } finally {
      await handle?.close().catch(() => undefined);
    }
  }

  /**
   * Rotate a slot's log if it exceeds MAX_LOG_SIZE.
   * Keeps the last KEEP_SIZE bytes, discards the rest.
   */
  async rotateIfNeeded(slot: number): Promise<boolean> {
    const logPath = this.getLogPath(slot);
    try {
      const fileStat = await stat(logPath);
      if (fileStat.size > this.MAX_LOG_SIZE) {
        const tail = await this.tailLog(slot, this.KEEP_SIZE);
        await writeFile(logPath, tail);
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
  async getLogMtime(slot: number): Promise<Date | null> {
    try {
      return (await stat(this.getLogPath(slot))).mtime;
    } catch {
      return null;
    }
  }

  /**
   * Get a slot log's file size in bytes.
   */
  async getLogSize(slot: number): Promise<number> {
    try {
      return (await stat(this.getLogPath(slot))).size;
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
  async enableLogging(slotCount: number): Promise<void> {
    for (let i = 1; i <= slotCount; i++) {
      const logPath = this.getLogPath(i);
      try {
        // Touch log file so it exists for stat queries
        await writeFile(logPath, "", { flag: "a" });

        // Enable pipe-pane — streams all output to log file
        await execShell(`tmux pipe-pane -t "0:0.${i}" -o 'cat >> ${logPath}'`, {
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
  async disableLogging(slotCount: number): Promise<void> {
    for (let i = 1; i <= slotCount; i++) {
      try {
        await execShell(`tmux pipe-pane -t "0:0.${i}"`, { timeout: 5_000 });
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
