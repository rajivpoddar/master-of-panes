/**
 * Core MoP hook processor.
 *
 * Hooks update the authoritative activity/session observation used by the
 * slot registry. PM notifications, idle nudges, plan approval, check-slot,
 * and self-reactivation automation are retired with the PM control plane.
 */

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { MoPDatabase } from "./db.js";
import type { TmuxRelay } from "./relay.js";
import type { HookPayload, HookResponse } from "./types.js";

const execFileAsync = promisify(execFile);

function classifyBashCommand(command: string): string | null {
  if (/vitest\s+run|bun\s+run\s+test|npx\s+vitest/.test(command)) return "testing";
  if (/tsc\s+--noEmit/.test(command)) return "type_checking";
  if (/bun\s+lint|eslint/.test(command)) return "linting";
  if (/git\s+commit/.test(command)) return "committing";
  if (/git\s+push/.test(command)) return "pushing";
  if (/git\s+(checkout|branch|switch)/.test(command)) return "branching";
  if (/modal\s+deploy/.test(command)) return "deploying_modal";
  if (/npx\s+convex\s+deploy/.test(command)) return "deploying_convex";
  if (/sg\s+--lang/.test(command)) return "exploring";
  return null;
}

export class HookProcessor {
  constructor(
    private readonly db: MoPDatabase,
    // Kept in the constructor for the core server wiring and future direct
    // observation use; this class deliberately performs no relay side effects.
    _relay: TmuxRelay,
  ) {}

  private async syncObservedCheckout(slotNum: number, payload: HookPayload): Promise<void> {
    if (!payload.cwd || !["PostToolUse", "SessionStart", "Stop"].includes(payload.type)) return;
    const slot = this.db.getSlot(slotNum);
    if (!slot?.occupied || !slot.branch) return;
    if (slot.active_turn_id && payload.session_id && payload.session_id !== slot.active_turn_id) return;

    try {
      const branchResult = await execFileAsync(
        "git",
        ["-C", payload.cwd, "symbolic-ref", "--quiet", "--short", "HEAD"],
        { encoding: "utf8", timeout: 1000 },
      );
      const branch = String(branchResult.stdout).trim();
      if (!branch || branch !== slot.branch) return;

      const headResult = await execFileAsync(
        "git",
        ["-C", payload.cwd, "rev-parse", "--verify", "HEAD^{commit}"],
        { encoding: "utf8", timeout: 1000 },
      );
      const head = String(headResult.stdout).trim().toLowerCase();
      if (!/^[0-9a-f]{40}$/.test(head)) return;
      if (slot.head_sha && slot.head_sha !== head) {
        await execFileAsync(
          "git",
          ["-C", payload.cwd, "merge-base", "--is-ancestor", slot.head_sha, head],
          { encoding: "utf8", timeout: 1000 },
        ).catch(() => { throw new Error("checkout head is not a descendant"); });
      }
      this.db.syncSlotCheckout(slotNum, branch, head, slot.assignment_epoch);
    } catch {
      // Observation never overwrites a committed assignment tuple.
    }
  }

  async process(slotNum: number, payload: HookPayload): Promise<HookResponse> {
    this.db.logEvent(
      slotNum,
      payload.type,
      payload.type,
      payload.tool_name ?? null,
      payload as unknown as Record<string, unknown>,
    );
    await this.syncObservedCheckout(slotNum, payload);

    const idle =
      payload.type === "Stop" ||
      payload.type === "SessionEnd" ||
      (payload.type === "Notification" &&
        (payload.notification_type === "idle_prompt" ||
          payload.notification_type === "permission_prompt"));

    const updates: Record<string, unknown> = {
      last_activity: new Date().toISOString(),
    };
    if (!["SessionStart", "PreCompact", "PostCompact"].includes(payload.type)) {
      updates.idle = idle;
    }
    if (payload.type === "Stop" || payload.type === "SessionEnd") {
      updates.activity = null;
    } else if (payload.type === "PostToolUse" || payload.type === "UserPromptSubmit") {
      const command =
        payload.tool_name === "Bash" && typeof payload.tool_input?.command === "string"
          ? payload.tool_input.command
          : "";
      const activity = classifyBashCommand(command);
      if (activity) updates.activity = activity;
    }
    this.db.updateSlot(slotNum, updates);
    return {};
  }

}
