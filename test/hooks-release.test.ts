import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import type { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

test("a stale PM-direction Stop cannot reclaim a released slot", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-release-stop-test-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.assignSlot(3, "PR rework", "github:repo-1", 6245, "fix/6245", "turn-a", 6411, "a".repeat(40), 0);
    db.finishAgentTurn(3, "turn-a");
    db.releaseSlot(3, 1);
    assert.equal(db.getSlot(3)?.occupied, false);

    const processor = new HookProcessor(db, {} as TmuxRelay);
    await processor.process(3, {
      type: "Stop",
      session_id: "turn-a",
      transcript: "Work is complete. Need PM direction before the next step.",
    });

    const slot = db.getSlot(3);
    assert.equal(slot?.occupied, false);
    assert.equal(slot?.task, null);
    assert.equal(slot?.issue, null);
    assert.equal(slot?.pr, null);
    assert.equal(slot?.assignment_epoch, 2);
    const events = db.getEvents(3, 10, "stale_pm_direction_after_release");
    assert.equal(events.length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("hook events advance only to descendants and never overwrite with stale or divergent heads", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-checkout-sync-test-"));
  try {
    execFileSync("git", ["init", "-q", directory]);
    execFileSync("git", ["-C", directory, "config", "user.email", "test@example.com"]);
    execFileSync("git", ["-C", directory, "config", "user.name", "MoP Test"]);
    execFileSync("git", ["-C", directory, "switch", "-q", "-c", "fix/10-exact"]);
    writeFileSync(join(directory, "proof"), "one\n");
    execFileSync("git", ["-C", directory, "add", "proof"]);
    execFileSync("git", ["-C", directory, "commit", "-q", "-m", "first"]);
    const firstHead = execFileSync("git", ["-C", directory, "rev-parse", "HEAD"], {
      encoding: "utf8",
    }).trim();

    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.assignSlot(1, "issue", "github:repo-1", 10, "fix/10-exact", "turn-a", null, null, 0);
    const processor = new HookProcessor(db, {} as TmuxRelay);
    await processor.process(1, {
      type: "PostToolUse",
      session_id: "turn-a",
      cwd: directory,
      tool_name: "Bash",
    });
    assert.equal(db.getSlot(1)?.head_sha, firstHead);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);

    writeFileSync(join(directory, "proof"), "two\n");
    execFileSync("git", ["-C", directory, "add", "proof"]);
    execFileSync("git", ["-C", directory, "commit", "-q", "-m", "second"]);
    const secondHead = execFileSync("git", ["-C", directory, "rev-parse", "HEAD"], {
      encoding: "utf8",
    }).trim();
    await processor.process(1, {
      type: "PostToolUse",
      session_id: "turn-a",
      cwd: directory,
      tool_name: "Bash",
    });
    // A same-branch forward descendant is normal checkout observation.
    assert.equal(db.getSlot(1)?.head_sha, secondHead);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
    assert.equal(db.getEvents(1, 10, "slot_checkout_synced").length, 2);

    // A stale ancestor after the committed head cannot roll ownership back.
    execFileSync("git", ["-C", directory, "reset", "-q", "--hard", firstHead]);
    await processor.process(1, {
      type: "PostToolUse",
      session_id: "turn-a",
      cwd: directory,
      tool_name: "Read",
    });
    assert.equal(db.getSlot(1)?.head_sha, secondHead);

    // A divergent branch is likewise rebind-only and cannot overwrite the
    // registered tuple from observation.
    writeFileSync(join(directory, "divergent"), "three\n");
    execFileSync("git", ["-C", directory, "add", "divergent"]);
    execFileSync("git", ["-C", directory, "commit", "-q", "-m", "divergent"]);
    const divergentHead = execFileSync("git", ["-C", directory, "rev-parse", "HEAD"], {
      encoding: "utf8",
    }).trim();
    assert.notEqual(divergentHead, secondHead);
    await processor.process(1, {
      type: "PostToolUse",
      session_id: "turn-a",
      cwd: directory,
      tool_name: "Bash",
    });
    assert.equal(db.getSlot(1)?.head_sha, secondHead);
    assert.equal(db.getEvents(1, 10, "slot_checkout_synced").length, 2);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("hook checkout synchronization never adopts an unregistered branch", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-checkout-mismatch-test-"));
  try {
    execFileSync("git", ["init", "-q", directory]);
    execFileSync("git", ["-C", directory, "config", "user.email", "test@example.com"]);
    execFileSync("git", ["-C", directory, "config", "user.name", "MoP Test"]);
    execFileSync("git", ["-C", directory, "switch", "-q", "-c", "fix/10-wrong"]);
    writeFileSync(join(directory, "proof"), "one\n");
    execFileSync("git", ["-C", directory, "add", "proof"]);
    execFileSync("git", ["-C", directory, "commit", "-q", "-m", "first"]);

    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.assignSlot(1, "issue", "github:repo-1", 10, "fix/10-exact", "turn-a", null, null, 0);
    const processor = new HookProcessor(db, {} as TmuxRelay);
    await processor.process(1, {
      type: "PostToolUse",
      session_id: "turn-a",
      cwd: directory,
      tool_name: "Bash",
    });
    assert.equal(db.getSlot(1)?.branch, "fix/10-exact");
    assert.equal(db.getSlot(1)?.head_sha, null);
    assert.equal(db.getSlot(1)?.assignment_epoch, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("same-session idle_prompt finishes an active turn when Stop is missing", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-idle-prompt-turn-test-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.assignSlot(
      2,
      "PR rework",
      "github:repo-1",
      6847,
      "fix/6847-scheduler-indexed-measurement",
      "turn-a",
      6905,
      "a".repeat(40),
      0,
    );
    db.startAgentTurn(2, "turn-a");

    const processor = new HookProcessor(db, {} as TmuxRelay);
    await processor.process(2, {
      type: "Notification",
      notification_type: "idle_prompt",
      session_id: "turn-a",
    });

    const slot = db.getSlot(2);
    assert.equal(slot?.active_turn_state, "inactive");
    assert.equal(slot?.active_turn_id, null);
    assert.equal(slot?.active_turn_started_at, null);
    assert.equal(slot?.idle, true);
    assert.equal(db.getEvents(2, 10, "idle_prompt_turn_finished").length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("stale idle_prompt cannot finish a newer active turn", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-stale-idle-prompt-turn-test-"));
  try {
    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.assignSlot(
      2,
      "PR rework",
      "github:repo-1",
      6847,
      "fix/6847-scheduler-indexed-measurement",
      "turn-old",
      6905,
      "a".repeat(40),
      0,
    );
    db.startAgentTurn(2, "turn-new");

    const processor = new HookProcessor(db, {} as TmuxRelay);
    await processor.process(2, {
      type: "Notification",
      notification_type: "idle_prompt",
      session_id: "turn-old",
    });

    const slot = db.getSlot(2);
    assert.equal(slot?.active_turn_state, "indeterminate");
    assert.equal(slot?.active_turn_id, "turn-new");
    assert.equal(slot?.idle, false);
    assert.equal(db.getEvents(2, 10, "idle_prompt_turn_mismatch").length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("Stop debounce tolerates a four-second promised-action scanner under host pressure", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-promise-audit-timeout-test-"));
  const auditScript = join(directory, "slow-audit.py");
  const originalAuditScript = (HookProcessor as any).SLOT_PROMISE_AUDIT_SCRIPT;
  const originalIdleDebounce = (HookProcessor as any).IDLE_DEBOUNCE_MS;
  try {
    writeFileSync(
      auditScript,
      [
        "import time",
        "time.sleep(4)",
        'print("BLOCK\\t[SLOT_PROMISE_ACTION_REQUIRED] continue the promised work")',
        "",
      ].join("\n"),
    );

    const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
    db.assignSlot(
      1,
      "issue",
      "github:repo-1",
      10,
      "main",
      "turn-a",
      null,
      "a".repeat(40),
      0,
    );
    const sent: Array<{ slot: number; command: string; force: boolean }> = [];
    const relay = {
      sendToSlot(slot: number, command: string, force: boolean) {
        sent.push({ slot, command, force });
      },
    } as TmuxRelay;
    const processor = new HookProcessor(db, relay);
    (HookProcessor as any).SLOT_PROMISE_AUDIT_SCRIPT = auditScript;
    (HookProcessor as any).IDLE_DEBOUNCE_MS = 0;

    await processor.process(1, {
      type: "Stop",
      session_id: "turn-a",
      transcript_path: join(directory, "transcript.jsonl"),
      cwd: directory,
    });
    const deadline = Date.now() + 10_000;
    while (sent.length === 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }

    assert.deepEqual(sent, [{ slot: 1, command: "continue your work", force: true }]);
    assert.equal(db.getEvents(1, 10, "Stop").length, 1);
    assert.equal(db.getEvents(1, 10, "slot_idle_debounce_started").length, 1);
    assert.equal(db.getEvents(1, 10, "slot_promised_action_scan_failed").length, 0);
    assert.equal(db.getEvents(1, 10, "slot_promised_action_continue_injected").length, 1);
  } finally {
    (HookProcessor as any).SLOT_PROMISE_AUDIT_SCRIPT = originalAuditScript;
    (HookProcessor as any).IDLE_DEBOUNCE_MS = originalIdleDebounce;
    rmSync(directory, { recursive: true, force: true });
  }
});
