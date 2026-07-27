import { watch, type FSWatcher } from "node:fs";
import { stat } from "node:fs/promises";
import { join } from "node:path";

export type JsonlActivitySignal =
  | {
      kind: "unknown";
      token: string;
      observedAtMs: null;
      reason: "no-event" | "watch-failed";
    }
  | {
      kind: "observed";
      token: string;
      observedAtMs: number;
    };

interface WatchHandle {
  on(event: "error", listener: (error: Error) => void): unknown;
  close(): void;
}

type WatchFactory = (
  dir: string,
  listener: (eventType: string, filename: string | Buffer | null) => void,
) => WatchHandle;

type StatFactory = (path: string) => Promise<{ mtimeMs: number }>;

interface DirectoryActivity {
  watcher: WatchHandle | null;
  epoch: number;
  sequence: number;
  lastActivityMs: number | null;
  recentFiles: Map<string, number>;
  failed: boolean;
  nextWatchAttemptMs: number;
}

interface RecentJsonlActivityOptions {
  maxRecentFiles?: number;
  now?: () => number;
  watchFactory?: WatchFactory;
  statFactory?: StatFactory;
  statTimeoutMs?: number;
  watchRetryMs?: number;
}

export interface JsonlActivitySnapshot {
  failed: boolean;
  signal: JsonlActivitySignal;
  recentFiles: string[];
}

let nextEpoch = 1;

const defaultWatchFactory: WatchFactory = (dir, listener): FSWatcher =>
  watch(dir, { persistent: false }, listener);
const defaultStatFactory: StatFactory = async (path) => stat(path);

/**
 * Tracks only JSONL writes observed after startup. Historical Claude session
 * filenames are random UUIDs, so directory order cannot identify recent files,
 * and statting every file blocks the Node event loop.
 */
export class RecentJsonlActivity {
  private readonly states = new Map<string, DirectoryActivity>();
  private readonly maxRecentFiles: number;
  private readonly now: () => number;
  private readonly watchFactory: WatchFactory;
  private readonly statFactory: StatFactory;
  private readonly statTimeoutMs: number;
  private readonly watchRetryMs: number;

  constructor(options: RecentJsonlActivityOptions = {}) {
    this.maxRecentFiles = options.maxRecentFiles ?? 16;
    this.now = options.now ?? Date.now;
    this.watchFactory = options.watchFactory ?? defaultWatchFactory;
    this.statFactory = options.statFactory ?? defaultStatFactory;
    this.statTimeoutMs = options.statTimeoutMs ?? 2_000;
    this.watchRetryMs = options.watchRetryMs ?? 30_000;
  }

  watchDirectory(dir: string): void {
    if (this.states.has(dir)) return;

    const state: DirectoryActivity = {
      watcher: null,
      epoch: nextEpoch++,
      sequence: 0,
      lastActivityMs: null,
      recentFiles: new Map(),
      failed: false,
      nextWatchAttemptMs: 0,
    };
    this.states.set(dir, state);
    this.startWatcher(dir, state);
  }

  private startWatcher(dir: string, state: DirectoryActivity): void {
    const wasFailed = state.failed;
    try {
      const watcher = this.watchFactory(dir, (_eventType, filename) => {
        const name = filename?.toString();
        if (name && !name.endsWith(".jsonl")) return;

        const observedAt = this.now();
        state.failed = false;
        state.sequence += 1;
        state.lastActivityMs = observedAt;
        if (name) {
          state.recentFiles.delete(name);
          state.recentFiles.set(name, observedAt);
          while (state.recentFiles.size > this.maxRecentFiles) {
            const oldest = state.recentFiles.keys().next().value;
            if (oldest === undefined) break;
            state.recentFiles.delete(oldest);
          }
        }
      });
      state.watcher = watcher;
      // A replacement watcher is probationary until it observes a real write.
      // Keeping the failed state here prevents relay from draining based on an
      // old timestamp and keeps health's unknown token stable across retries.
      state.failed = wasFailed;
      state.nextWatchAttemptMs = 0;
      watcher.on("error", (error) => {
        if (state.watcher !== watcher) return;
        watcher.close();
        state.watcher = null;
        if (!state.failed) state.sequence += 1;
        state.failed = true;
        state.nextWatchAttemptMs = this.now() + this.watchRetryMs;
        console.warn(`[jsonl-activity] watcher failed for ${dir}: ${String(error)}`);
      });
    } catch (error) {
      state.watcher = null;
      if (!wasFailed) state.sequence += 1;
      state.failed = true;
      state.nextWatchAttemptMs = this.now() + this.watchRetryMs;
      console.warn(`[jsonl-activity] unable to watch ${dir}: ${String(error)}`);
    }
  }

  async latestActivity(dir: string): Promise<JsonlActivitySignal> {
    this.watchDirectory(dir);
    const state = this.states.get(dir);
    if (!state) {
      return {
        kind: "unknown",
        token: "unknown:missing-state",
        observedAtMs: null,
        reason: "watch-failed",
      };
    }

    if (
      state.failed &&
      state.watcher === null &&
      this.now() >= state.nextWatchAttemptMs
    ) {
      this.startWatcher(dir, state);
    }

    if (!state.failed && state.recentFiles.size > 0) {
      const recentStats = await Promise.allSettled(
        [...state.recentFiles.keys()].map(async (name) => ({
          name,
          mtimeMs: (await this.statWithTimeout(join(dir, name))).mtimeMs,
        })),
      );
      let newestMtimeMs = state.lastActivityMs ?? 0;
      for (const result of recentStats) {
        if (result.status === "fulfilled") {
          newestMtimeMs = Math.max(newestMtimeMs, result.value.mtimeMs);
        }
      }
      if (state.lastActivityMs === null || newestMtimeMs > state.lastActivityMs) {
        state.sequence += 1;
        state.lastActivityMs = newestMtimeMs;
      }
    }

    if (state.failed || state.lastActivityMs === null) {
      return {
        kind: "unknown",
        token: `unknown:${state.epoch}:${state.sequence}`,
        observedAtMs: null,
        reason: state.failed ? "watch-failed" : "no-event",
      };
    }

    return {
      kind: "observed",
      token: `observed:${state.epoch}:${state.sequence}`,
      observedAtMs: state.lastActivityMs,
    };
  }

  private async statWithTimeout(path: string): Promise<{ mtimeMs: number }> {
    let timer: NodeJS.Timeout | undefined;
    try {
      return await Promise.race([
        this.statFactory(path),
        new Promise<never>((_resolve, reject) => {
          timer = setTimeout(
            () => reject(new Error(`stat timeout after ${this.statTimeoutMs}ms`)),
            this.statTimeoutMs,
          );
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  async snapshot(dir: string): Promise<JsonlActivitySnapshot | null> {
    const state = this.states.get(dir);
    if (!state) return null;
    return {
      failed: state.failed,
      signal: await this.latestActivity(dir),
      recentFiles: [...state.recentFiles.keys()],
    };
  }

  close(): void {
    for (const state of this.states.values()) {
      state.watcher?.close();
    }
    this.states.clear();
  }
}

export const recentJsonlActivity = new RecentJsonlActivity();
