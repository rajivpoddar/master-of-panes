import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { Hono } from "hono";
import type { MoPDatabase } from "./db.js";
import type { TmuxRelay } from "./relay.js";

const KEY = "pm_soft_boundary_v1";
export const PM_SOFT_MAX_DEFERRAL_MS = 45 * 60_000;
type State = {
  boot: number; session: string; tools: string[]; stopped: boolean;
  request: string | null; effect: boolean; fenced: boolean;
  failures?: Partial<Record<"clear" | "daily", { request: string; effect: boolean }>>;
};
type Pending = { key: string; requestedAt: number; boot: number; kind: "daily" | "clear" };

/** Session-bound hook accounting. Admission and Stop claiming are synchronous:
 * no await may separate the empty-tool check from closing tool admission.
 * The claim fences maintenance effects, never inbound message delivery.
 */
export class PMSoftBoundary {
  private state: State;
  private activeWriters = 0;
  constructor(private db: MoPDatabase, private pending: () => Pending | null,
    private now = Date.now, private onFailureReleased: () => void = () => {}) {
    try {
      const value = JSON.parse(db.getConfig(KEY) || "null");
      if (!value || !Number.isInteger(value.boot) || typeof value.session !== "string"
          || !Array.isArray(value.tools) || !value.tools.every((id: unknown) => typeof id === "string")
          || typeof value.stopped !== "boolean" || typeof value.effect !== "boolean"
          || !(value.request === null || typeof value.request === "string")) throw new Error("invalid state");
      this.state = { ...value, fenced: value.fenced === true };
    } catch {
      this.state = { boot: 0, session: "", tools: [], stopped: false, request: null, effect: false, fenced: false };
    }
  }
  private save(): void { this.db.setConfig(KEY, JSON.stringify(this.state)); }
  private boot(): { id: number; session: string } {
    const event = this.db.getEvents(0, 1, "SessionStart")[0];
    try { return { id: event?.id ?? 0, session: JSON.parse(event?.payload || "{}").session_id || "" }; }
    catch { return { id: 0, session: "" }; }
  }
  isHeld(): boolean { return this.state.request !== null; }
  isAdmissionFenced(): boolean { return this.isHeld() && this.state.fenced; }
  isDailyRestartReady(): boolean {
    return this.state.request?.startsWith("daily:") === true && this.isSafe();
  }
  /** Count the real writer/helper lifetime, including an HTTP handler that
   * outlives its caller's timeout. No failure releases input while keys may
   * still be issued by this process. Nested daily/respawn writers share it. */
  async withMaintenanceWriter<T>(run: () => Promise<T>): Promise<T> {
    this.activeWriters++;
    try { return await run(); }
    finally {
      this.activeWriters--;
      this.reconcileFailedMaintenance();
    }
  }
  failMaintenance(request: string): void {
    if (this.state.request !== request) return; // never release a successor
    const kind = request.startsWith("clear:") ? "clear" : "daily";
    this.state.failures = { ...this.state.failures, [kind]: { request, effect: this.state.effect } };
    this.save();
    this.reconcileFailedMaintenance();
  }
  /** Retry receipts and input admission are independent. Terminal failure
   * consumes the request but relinquishes its lock without touching the
   * composer, declaring idle, or sending/deleting any retained input. */
  reconcileFailedMaintenance(inputAfterWriter = false): boolean {
    const request = this.state.request;
    if (!request || this.activeWriters !== 0) return false;
    const kind = request.startsWith("clear:") ? "clear" : "daily";
    const clearState = this.db.getConfig("pm_clear_delivery_state");
    const failedClear = kind === "clear" && !this.state.effect
      && request === `clear:${Date.parse(this.db.getConfig("pm_clear_requested_at") || "")}`
      && (clearState === "failed" || clearState === "uncertain"
        || (inputAfterWriter && clearState === "awaiting_ack"));
    if (!failedClear && this.state.failures?.[kind]?.request !== request) return false;
    const failure = this.state.failures?.[kind]?.request === request
      ? this.state.failures[kind]! : { request, effect: this.state.effect };
    this.state = { ...this.state, request: null, effect: false, fenced: false, stopped: false,
      failures: { ...this.state.failures, [kind]: failure } };
    this.save();
    this.db.logEvent(0, "pm_maintenance_input_released", null, null, {
      request, attempted_effect: failure.effect,
      reason: clearState === "awaiting_ack" ? "input_after_completed_clear_write" : "failed_writer_settled",
      retry_receipt: "preserved",
    });
    this.onFailureReleased();
    return true;
  }
  deferCLIInput(): void {
    if (this.state.effect) return;
    this.state.fenced = false;
    this.state.stopped = false;
    this.save();
  }
  isSafe(): boolean {
    const boot = this.boot();
    return (!this.isHeld() || this.state.fenced) && this.state.stopped && this.state.tools.length === 0
      && this.state.boot === boot.id && !!boot.session && this.state.session === boot.session;
  }
  status() {
    return { held: this.isHeld(), admission_fenced: this.isAdmissionFenced(), stopped: this.isSafe(), tools_in_flight: this.state.tools.length,
      session_id: this.state.session, boot_id: this.state.boot, request: this.state.request };
  }
  /** Called before HookProcessor's first await, never for a dev slot. */
  observe(raw: Record<string, unknown>): string | null {
    const type = raw.hook_event_name;
    const session = typeof raw.session_id === "string" ? raw.session_id : "";
    if (type === "SessionStart") return null; // acknowledge only after it is logged
    if (!["PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop", "UserPromptSubmit"].includes(String(type))) return null;
    const boot = this.boot();
    if (!session || session !== boot.session || !boot.id) {
      return type === "PreToolUse" ? "PM tool session is not the current observed session." : null;
    }
    this.reconcileFailedMaintenance(type === "UserPromptSubmit" || type === "PreToolUse");
    if (type === "UserPromptSubmit") {
      // Human input takes priority over an unexecuted maintenance claim. The
      // native CLI owns busy-turn delivery; MoP must not retain/block it.
      this.deferCLIInput();
      return null;
    }
    if (this.state.boot !== boot.id) {
      if (this.state.session === session && !this.state.effect) {
        // Compaction keeps the session and its outstanding tool identities.
        this.state.boot = boot.id;
        this.state.stopped = false;
      } else {
        if (this.isHeld()) {
          return type === "PreToolUse"
            ? "PM maintenance is awaiting acknowledgement." : null;
        }
        this.state = { boot: boot.id, session, tools: [], stopped: false, request: null, effect: false, fenced: false, failures: this.state.failures };
      }
    }
    const id = typeof raw.tool_use_id === "string" ? raw.tool_use_id.trim() : "";
    if (type === "PreToolUse") {
      if (this.isAdmissionFenced()) return "PM maintenance owns this tool-free Stop; tool admission is fenced.";
      if (raw.mop_tool_tracking !== 1 || !id) return "PM tool tracking requires an identified admission hook.";
      this.state.stopped = false;
      if (!this.state.tools.includes(id)) this.state.tools.push(id);
    } else if (type === "PostToolUse" || type === "PostToolUseFailure") {
      if (id) this.state.tools = this.state.tools.filter(tool => tool !== id);
    } else if (type === "Stop" && raw.mop_tool_tracking === 1) {
      this.state.stopped = this.state.tools.length === 0;
      const pending = this.pending();
      if (!this.state.effect && this.activeWriters === 0 && this.state.stopped && pending && pending.boot === boot.id
          && pending.key !== this.state.failures?.[pending.kind]?.request
          && (this.db.getPendingPMEventCount() === 0
              || this.now() - pending.requestedAt >= PM_SOFT_MAX_DEFERRAL_MS)) {
        this.state.request = pending.key;
        this.state.fenced = true;
        this.db.logEvent(0, "pm_soft_boundary_claimed", "Stop", null, {
          request: pending.key, queued: this.db.getPendingPMEventCount(),
          deferred_ms: this.now() - pending.requestedAt,
        });
      }
    }
    this.save();
    return null;
  }
  sessionStarted(source: unknown): void {
    const boot = this.boot();
    if (!boot.id || boot.id === this.state.boot) return;
    if (source === "compact" && boot.session === this.state.session) {
      this.state.boot = boot.id;
      this.state.stopped = false;
      this.save();
      return;
    }
    // A fresh CLI boot has no composer from the abandoned writer. Release only
    // its admission reservation, never its consumed/uncertain clear receipt.
    // Active writes, compaction and attempted respawns still require their ack.
    const clearRequest = `clear:${Date.parse(this.db.getConfig("pm_clear_requested_at") || "")}`;
    if ((source === "startup" || source === "resume") && !this.state.effect
        && this.state.tools.length === 0 && this.state.request === clearRequest
        && this.db.getConfig("pm_clear_delivery_state") === "uncertain") {
      this.state = { boot: boot.id, session: boot.session, tools: [], stopped: false, request: null, effect: false, fenced: false, failures: this.state.failures };
      this.save();
      this.db.logEvent(0, "pm_maintenance_admission_recovered", "SessionStart", null, {
        request: clearRequest, source, reason: "fresh_boot_after_uncertain_clear", clear_receipt: "preserved",
      });
      return;
    }
    // A respawn keeps the fence through its full guarded response. A /clear
    // instead has a definitive SessionStart:clear acknowledgement.
    if (this.isHeld() && source !== "clear") return;
    this.state = { boot: boot.id, session: boot.session, tools: [], stopped: false, request: null, effect: false, fenced: false, failures: this.state.failures };
    this.save();
  }
  beginRespawn(request: unknown): boolean {
    if (!this.isHeld() || !this.isSafe() || this.state.effect || request !== this.state.request) return false;
    this.state.effect = true;
    this.save();
    return true;
  }
  respawnAcknowledged(): void {
    if (!this.state.effect) return;
    const request = this.state.request;
    const boot = this.boot();
    if (!boot.id || boot.id === this.state.boot) return; // response alone is not a boot receipt
    this.state = { boot: boot.id, session: boot.session, tools: [], stopped: false, request: null, effect: false, fenced: false, failures: this.state.failures };
    this.save();
    this.db.logEvent(0, "clear_pending_executed", "SessionStart", null, { via: "pm_soft_boundary_respawn", request });
    this.db.logEvent(0, "slot_cleared", "SessionStart", null, { via: "pm_soft_boundary_respawn", request });
  }
}

export function pendingPMMaintenance(db: MoPDatabase, now = new Date()): Pending | null {
  const boot = db.getEvents(0, 1, "SessionStart")[0]?.id ?? 0;
  if (db.hasPendingClear(0) && db.getConfig("pm_clear_delivery_state") === "deferred_busy") {
    const requestedAt = Date.parse(db.getConfig("pm_clear_requested_at") || "");
    return Number.isFinite(requestedAt) ? { key: `clear:${requestedAt}`, requestedAt, boot, kind: "clear" } : null;
  }
  const day = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
  const path = join(homedir(), ".claude/state", `daily-pm-restart-${day}.json`);
  try {
    const saved = JSON.parse(readFileSync(path, "utf8"));
    if (saved.handoff_status !== "pending" || saved.launch_intent) return null;
    const pin = saved.request_identity;
    const requestedAt = saved.requested_at ? Date.parse(saved.requested_at) : statSync(path).mtimeMs;
    if (!pin || !Number.isInteger(pin.boot?.[0]) || !Number.isFinite(requestedAt)) return null;
    const requestedSession = JSON.parse(pin.boot[1]).session_id;
    const currentBoot = db.getEvents(0, 1, "SessionStart")[0];
    const currentSession = JSON.parse(currentBoot?.payload || "{}").session_id;
    if (!requestedSession || currentSession !== requestedSession) return null;
    return { key: `daily:${day}:${pin.boot[0]}:${pin.work}`, requestedAt, boot, kind: "daily" };
  } catch { return null; }
}

export function registerPMSoftBoundary(app: Hono, db: MoPDatabase, relay: TmuxRelay): PMSoftBoundary {
  const boundary = new PMSoftBoundary(db, () => pendingPMMaintenance(db), Date.now, () => relay.setPMBusy(true));
  relay.setPMMaintenanceGate(boundary);
  app.use("/hooks/slot/0", async (c, next) => {
    let raw: Record<string, unknown>;
    try { raw = await c.req.raw.clone().json(); } catch { return next(); }
    const refusal = boundary.observe(raw);
    if (refusal) {
      return c.json({ hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: refusal } });
    }
    if (raw.hook_event_name === "PreToolUse") relay.setPMBusy(true);
    await next();
    if (raw.hook_event_name === "PreToolUse") {
      const response = await c.res.clone().json();
      if (response.blocked === true) {
        boundary.observe({ ...raw, hook_event_name: "PostToolUseFailure" });
        c.res = c.json({ hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "deny",
          permissionDecisionReason: response.reason || response.message || "PM tool refused by MoP." } });
      } else {
        c.res = c.json({ ...response, mop_tool_admitted: true });
      }
    }
    if (raw.hook_event_name === "SessionStart") {
      const wasHeld = boundary.isHeld();
      boundary.sessionStarted(raw.source);
      if (wasHeld && !boundary.isHeld()) relay.setPMBusy(raw.source !== "clear");
    }
    if (raw.hook_event_name === "Stop" && boundary.isHeld() && boundary.isSafe()) {
      if (await relay.isPMComposerQueueEmpty() && boundary.isSafe()) relay.setPMBusy(false);
      else { boundary.deferCLIInput(); relay.setPMBusy(true); }
    }
  });
  app.use("/pm-status", async (c, next) => {
    if (c.req.method === "POST") {
      let raw: { event?: string } = {};
      try { raw = await c.req.raw.clone().json(); } catch { return next(); }
      if ((boundary.isAdmissionFenced() && raw.event === "start")
          || (raw.event === "stop" && boundary.status().tools_in_flight > 0)) {
        return c.json({ success: true, pm_busy: relay.isPMBusy(), drained: 0, maintenance: true });
      }
    }
    await next();
    if (c.req.method === "GET") {
      const body = await c.res.json();
      const cliQueueEmpty = await relay.isPMComposerQueueEmpty();
      c.res = c.json({ ...body, soft_boundary: { ...boundary.status(), cli_queue_empty: cliQueueEmpty } });
    }
  });
  app.use("/slots/0/respawn", async (c, next) => {
    if (!boundary.isHeld()) return next();
    let body: { pm_soft_boundary_request?: string } = {};
    try { body = await c.req.raw.clone().json(); } catch { /* reject missing authority */ }
    if (!(await relay.isPMComposerQueueEmpty()) || !boundary.beginRespawn(body.pm_soft_boundary_request)) {
      return c.json({ success: false, reason: "pm_soft_boundary_refused", zero_effect: true }, 409);
    }
    const request = boundary.status().request!;
    await boundary.withMaintenanceWriter(async () => {
      try {
        await next();
        if (c.res.ok && (await c.res.clone().json()).success === true) {
          boundary.respawnAcknowledged();
          if (!boundary.isHeld()) relay.setPMBusy(false);
        }
      } finally {
        // A failed/uncertain response is terminal for this attempt. Keep its
        // consumed identity, not a permanent lock on the human's input.
        boundary.failMaintenance(request);
      }
    });
  });
  return boundary;
}
