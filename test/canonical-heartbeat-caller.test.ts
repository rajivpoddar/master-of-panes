import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { PMCadenceScheduler } from "../src/pmCadence.js";

test("the scheduled heartbeat caller uses only the canonical Python producer", () => {
  const prompt = readFileSync(
    "scripts/pm/shared-assets/codex/automations/heydonna-3h-ready-pool-reconciliation/prompt.template",
    "utf8",
  );
  assert.match(prompt, /\/Users\/rajiv\/\.claude\/scripts\/sakshi-heartbeat\.py --ready-pool-audit/);
  assert.doesNotMatch(prompt, /(?:pm-operator|backlog-triage\.py|scheduled-heartbeat\.sh|heartbeat-session-age-clear\.py|mop-clear-slot\.sh)/i);

  const scheduler = new PMCadenceScheduler({
    getConfig: () => null,
    setConfig: () => undefined,
    hasPMQueueDelivery: () => false,
    logEvent: () => undefined,
  } as never, {} as never);
  const heartbeat = scheduler.getStatus().tasks.find((task) => task.task === "heartbeat");
  assert.ok(heartbeat);
  assert.match(heartbeat.command, /sakshi-heartbeat\.py --dry-run/);
  assert.match(heartbeat.command, /sakshi-heartbeat\.py --launch-prompt/);
  assert.match(heartbeat.command, /sakshi-heartbeat\.py --ready-pool-audit/);
  assert.doesNotMatch(heartbeat.command, /(?:heartbeat-tasks|pm-operator|backlog-triage\.py|scheduled-heartbeat\.sh|heartbeat-session-age-clear\.py|mop-clear-slot\.sh)/i);
});
