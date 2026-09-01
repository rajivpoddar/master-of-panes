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
