import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import {
  PMCadenceScheduler,
  type HeartbeatPreflightResult,
  type PMCadenceRunResult,
} from "../src/pmCadence.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const PRODUCER = join(
  process.cwd(),
  "scripts/pm/shared-assets/claude/scripts/pm/control-plane/sakshi-heartbeat.py",
);
const PRODUCER_IMPORT_ROOT = join(
  process.cwd(),
  "scripts/pm/shared-assets/claude",
);
const FORBIDDEN = /heartbeat-tasks|sakshi-heartbeat\.sh|scheduled-heartbeat\.sh|heartbeat-session-age-clear\.py|backlog-triage\.py|mop-clear-slot\.sh|pm-operator|slot\s+(?:dispatch|release|clear)|Slack/i;

function heartbeatDb(directory: string): MoPDatabase {
  return new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
}

function preflightFailure(stage: HeartbeatPreflightResult["stage"]): HeartbeatPreflightResult {
  return { ok: false, stage, returncode: 2, output: "", error: `${stage} unavailable` };
}

function preflightSuccess(): HeartbeatPreflightResult & { launch_prompt: string } {
  return {
    ok: true,
    stage: "ready-pool-audit",
    returncode: 0,
    output: "",
    error: null,
    launch_prompt: "canonical heartbeat prompt",
  };
}

test("the installed producer emits a self-contained read-only launch prompt", () => {
  const prompt = execFileSync("python3", [PRODUCER, "--launch-prompt"], {
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: PRODUCER_IMPORT_ROOT },
  });
  assert.match(prompt, /sakshi-heartbeat\.py --dry-run/);
  assert.match(prompt, /sakshi-heartbeat\.py --ready-pool-audit/);
  assert.match(prompt, /session-age due state is\s+report-only/i);
  assert.doesNotMatch(prompt, FORBIDDEN);
  assert.doesNotMatch(readFileSync(PRODUCER, "utf8"), /HEARTBEAT_SKILL|LEGACY_READY_POOL_COMMAND/);
});

test("the scheduler status describes the canonical fail-closed preflight", () => {
  const cadenceSource = readFileSync("src/pmCadence.ts", "utf8");
  assert.match(cadenceSource, /execFile\(\s*"python3"/);
  assert.match(cadenceSource, /CANONICAL_HEARTBEAT_PRODUCER/);
  assert.ok(cadenceSource.indexOf('args: ["--dry-run"]') < cadenceSource.indexOf('args: ["--launch-prompt"]'));
  assert.ok(cadenceSource.indexOf('args: ["--launch-prompt"]') < cadenceSource.indexOf('args: ["--ready-pool-audit"]'));

  const scheduler = new PMCadenceScheduler({
    getConfig: () => null,
    setConfig: () => undefined,
    hasPMQueueDelivery: () => false,
    logEvent: () => undefined,
  } as never, {} as never);
  const heartbeat = scheduler.getStatus().tasks.find((task) => task.task === "heartbeat");
  assert.ok(heartbeat);
  assert.match(heartbeat.command, /canonical read-only Sakshi producer preflight/);
  assert.match(heartbeat.command, /nonzero producer result.*stops this wake/);
  assert.doesNotMatch(heartbeat.command, /(?:heartbeat-tasks|pm-operator|backlog-triage\.py|scheduled-heartbeat\.sh|heartbeat-session-age-clear\.py|mop-clear-slot\.sh)/i);
});

test("the producer manifest entry matches the candidate source and executable mode", () => {
  const manifest = JSON.parse(readFileSync("scripts/pm/shared-assets/manifest.json", "utf8")) as {
    entries?: Array<{ source_path?: string; canonical_target?: string; sha256?: string; mode?: number }>;
  };
  const entry = manifest.entries?.find((candidate) =>
    candidate.source_path === "claude/scripts/pm/control-plane/sakshi-heartbeat.py",
  );
  assert.ok(entry);
  assert.equal(entry.canonical_target, "/Users/rajiv/.claude/scripts/sakshi-heartbeat.py");
  assert.equal(entry.mode, 0o755);
  const digest = createHash("sha256").update(readFileSync(PRODUCER)).digest("hex");
  assert.equal(entry.sha256, digest);
  assert.equal(statSync(PRODUCER).mode & 0o777, 0o755);
});

for (const stage of ["dry-run", "launch-prompt", "ready-pool-audit"] as const) {
  test(`a ${stage} producer failure stops the scheduled heartbeat before delivery`, async () => {
    const directory = mkdtempSync(join(tmpdir(), "mop-heartbeat-preflight-"));
    try {
      const db = heartbeatDb(directory);
      let deliveries = 0;
      const scheduler = new PMCadenceScheduler(db, {
        submitToPM: async () => {
          deliveries += 1;
          return { ok: true };
        },
      } as never, { heartbeatPreflight: async () => preflightFailure(stage) });

      const result = await scheduler.runManual("heartbeat");
      assert.equal(result.triggered, false);
      assert.equal(result.injected, false);
      assert.equal(result.preflight, "failed");
      assert.equal(result.preflight_stage, stage);
      assert.equal(deliveries, 0);
      assert.equal(db.getConfig("pm_cadence_heartbeat_last_due_key"), null);
      assert.equal(db.getEvents(0, 10, "pm_cadence_preflight_failed").length, 1);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
}

test("a successful canonical preflight delivers the exact emitted prompt once", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-heartbeat-preflight-success-"));
  try {
    const db = heartbeatDb(directory);
    const deliveries: string[] = [];
    const scheduler = new PMCadenceScheduler(db, {
      submitToPM: async (message: string) => {
        deliveries.push(message);
        return { ok: true };
      },
    } as never, { heartbeatPreflight: async () => preflightSuccess() });

    const result = await scheduler.runManual("heartbeat");
    assert.equal(result.triggered, true);
    assert.equal(result.injected, true);
    assert.equal(result.preflight, "passed");
    assert.equal(deliveries.length, 1);
    assert.match(deliveries[0] ?? "", /canonical heartbeat prompt$/);
    assert.notEqual(db.getConfig("pm_cadence_heartbeat_last_due_key"), null);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("restart reconciles one delivered heartbeat occurrence without rerunning the producer", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-heartbeat-restart-reconcile-"));
  try {
    const db = heartbeatDb(directory);
    const launchPrompt = "canonical composed launch prompt";
    let producerCalls = 0;
    let submissions = 0;
    const first = new PMCadenceScheduler(db, {
      submitToPM: async (message: string, eventType?: string) => {
        submissions += 1;
        db.logEvent(0, "pm_queue_delivered", null, null, {
          event_type: eventType,
          message,
        });
        return { ok: false, ambiguous: true };
      },
    } as never, {
      heartbeatPreflight: async () => {
        producerCalls += 1;
        return { ok: true, stage: "ready-pool-audit", returncode: 0, output: "", error: null, launch_prompt: launchPrompt };
      },
    });

    const firstResult = await first.runManual("heartbeat");
    assert.equal(firstResult.queued, true);
    assert.equal(producerCalls, 1);
    assert.equal(submissions, 1);
    assert.equal(db.getConfig("pm_cadence_heartbeat_last_due_key"), null);
    const prepared = db.getEvents(0, 20, "pm_cadence_occurrence_prepared")[0];
    assert.ok(prepared);
    const preparedPayload = JSON.parse(prepared.payload) as { message?: string; message_sha256?: string };
    assert.equal(preparedPayload.message, firstResult.message);
    assert.equal(preparedPayload.message_sha256, createHash("sha256").update(firstResult.message, "utf8").digest("hex"));

    let restartProducerCalls = 0;
    let restartSubmissions = 0;
    const fresh = new PMCadenceScheduler(db, {
      submitToPM: async () => {
        restartSubmissions += 1;
        return { ok: true };
      },
    } as never, {
      heartbeatPreflight: async () => {
        restartProducerCalls += 1;
        return { ok: true, stage: "ready-pool-audit", returncode: 0, output: "", error: null, launch_prompt: "must not regenerate" };
      },
    });
    db.setConfig("pm_cadence_morning_brief_paused", "true");
    const reconcile = await fresh.tick("scheduled");

    assert.deepEqual(reconcile, []);
    assert.equal(restartProducerCalls, 0);
    assert.equal(restartSubmissions, 0);
    assert.equal(db.getConfig("pm_cadence_heartbeat_last_due_key"), firstResult.due_key);
    assert.equal(db.getEvents(0, 20, "pm_cadence_delivery_reconciled").length, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

for (const receipt of [
  { name: "missing", write: (_db: MoPDatabase, _eventType: string) => undefined },
  { name: "payload mismatch", write: (db: MoPDatabase, eventType: string) => db.logEvent(0, "pm_queue_delivered", null, null, { event_type: eventType, message: "wrong composed payload" }) },
  { name: "malformed", write: (db: MoPDatabase, eventType: string) => db.logEvent(0, "pm_queue_delivered", null, null, { event_type: eventType, message: 42 }) },
] as const) {
  test(`cadence ${receipt.name} receipt does not reconcile or resubmit`, async () => {
    const directory = mkdtempSync(join(tmpdir(), `mop-heartbeat-${receipt.name.replace(/\s+/g, "-")}-`));
    try {
      const db = heartbeatDb(directory);
      let submissions = 0;
      const first = new PMCadenceScheduler(db, {
        submitToPM: async () => {
          submissions += 1;
          return { ok: false, ambiguous: true };
        },
      } as never, {
        heartbeatPreflight: async () => ({ ok: true, stage: "ready-pool-audit", returncode: 0, output: "", error: null, launch_prompt: "prepared" }),
      });
      const firstResult = await first.runManual("heartbeat");
      assert.equal(submissions, 1);
      receipt.write(db, `cadence-heartbeat-${firstResult.due_key}`);

      let restartProducerCalls = 0;
      const fresh = new PMCadenceScheduler(db, {
        submitToPM: async () => {
          submissions += 1;
          return { ok: true };
        },
      } as never, {
        heartbeatPreflight: async () => {
          restartProducerCalls += 1;
          return { ok: true, stage: "ready-pool-audit", returncode: 0, output: "", error: null, launch_prompt: "must not run" };
        },
      });
      const result = await (fresh as unknown as {
        runIfDue: (task: "heartbeat", reason: "scheduled") => Promise<PMCadenceRunResult | null>;
      }).runIfDue("heartbeat", "scheduled");
      assert.ok(result);
      assert.equal(result.triggered, false);
      assert.match(result.message, /RECONCILIATION_FAILED/);
      assert.equal(restartProducerCalls, 0);
      assert.equal(submissions, 1);
      assert.equal(db.getConfig("pm_cadence_heartbeat_last_due_key"), null);
      assert.equal(db.getEvents(0, 20, "pm_cadence_delivery_reconciled").length, 0);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
}

test("different cadence and unrelated PM deliveries cannot reconcile the current bucket", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-heartbeat-unrelated-receipts-"));
  try {
    const db = heartbeatDb(directory);
    let producerCalls = 0;
    let submissions = 0;
    const scheduler = new PMCadenceScheduler(db, {
      submitToPM: async () => {
        submissions += 1;
        return { ok: true };
      },
    } as never, {
      heartbeatPreflight: async () => {
        producerCalls += 1;
        return { ok: true, stage: "ready-pool-audit", returncode: 0, output: "", error: null, launch_prompt: "must not run on boot seed" };
      },
    });
    const currentDueKey = (scheduler as unknown as { currentDueKey: (task: "heartbeat") => string }).currentDueKey("heartbeat");
    db.logEvent(0, "pm_queue_delivered", null, null, {
      event_type: "cadence-heartbeat-previous-bucket",
      message: "a different cadence occurrence",
    });
    db.logEvent(0, "pm_queue_delivered", null, null, {
      event_type: "freeform-unrelated",
      message: "unrelated PM delivery",
    });

    const result = await scheduler.tick("scheduled");
    assert.deepEqual(result, []);
    assert.equal(producerCalls, 0);
    assert.equal(submissions, 0);
    assert.equal(db.getConfig("pm_cadence_heartbeat_last_due_key"), currentDueKey);
    assert.equal(db.getEvents(0, 20, "pm_cadence_delivery_reconciled").length, 0);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
