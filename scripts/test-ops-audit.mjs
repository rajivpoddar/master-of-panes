#!/usr/bin/env node
/**
 * test-ops-audit.mjs — Unit/integration tests for OpsAuditScheduler.
 *
 * Coverage:
 *   1. parseFirstLine: inject / skip / malformed / missing-reason variants.
 *   2. Scheduler lock: concurrent tick() does NOT overlap. Uses a stub bg-script
 *      injected via OPS_AUDIT_BG_SCRIPT_OVERRIDE — NEVER touches Codex or PM pane.
 *   3. Fixture output: stub bg-script INJECT_DECISION:inject path flows through
 *      relay.injectToPM as the freeform "MoP: hourly ops audit for <reason>" shape.
 *   4. parseRelayMessage routing (Fix 4 — option b): confirm hourly-audit payload
 *      DOES fall through parseRelayMessage and gets queued as freeform (slot=0),
 *      NOT as a slot-keyed event. This is intentional per R1 design.
 *   5. Prefix parsing: "MoP: hourly ops audit for <reason>" recognised by
 *      pm-context-injector.sh Shape 3.
 *   6. Bg-script fixture: synthetic stdout exercises inject path + decision shape.
 *
 * Usage: node scripts/test-ops-audit.mjs
 *
 * Exit code 0 on full pass. Prints pass/fail counts. Deterministic — no Codex,
 * no PM injection, no Slack/GitHub calls.
 */

import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { existsSync, writeFileSync, unlinkSync, mkdtempSync } from "node:fs";
import path from "node:path";
import os from "node:os";

const RESULTS = [];

function test(name, fn) {
  try {
    fn();
    RESULTS.push({ name, ok: true });
    console.log(`  ✓ ${name}`);
  } catch (err) {
    RESULTS.push({ name, ok: false, err: String(err?.message ?? err) });
    console.log(`  ✗ ${name}\n    ${err?.message ?? err}`);
  }
}

async function asyncTest(name, fn) {
  try {
    await fn();
    RESULTS.push({ name, ok: true });
    console.log(`  ✓ ${name}`);
  } catch (err) {
    RESULTS.push({ name, ok: false, err: String(err?.message ?? err) });
    console.log(`  ✗ ${name}\n    ${err?.message ?? err}`);
  }
}

console.log("=== ops-audit tests ===\n");

// ─── 0. Setup: build a stub bg-script that we will inject via env override ─
// This is the canonical determinism gate (R1 fix 3). Tests below import the
// scheduler module ONLY after OPS_AUDIT_BG_SCRIPT_OVERRIDE is set, so the
// module-load-time constant resolves to the stub.
const workDir = mkdtempSync(path.join(os.tmpdir(), "ops-audit-test-"));
const stubBgScript = path.join(workDir, "stub-bg-inject.sh");
const stubBgScriptSkip = path.join(workDir, "stub-bg-skip.sh");
const stubBgScriptSlow = path.join(workDir, "stub-bg-slow.sh");

writeFileSync(
  stubBgScript,
  `#!/bin/bash
# Stub bg-script — synthetic INJECT_DECISION:inject payload for tests.
# Deterministic. No Codex, no Slack, no GitHub, no PM injection.
cat <<EOF
INJECT_DECISION:inject REASON:test_inject_fixture
---
ACTION_ITEMS:
  - OWNER:dhruv EVIDENCE:test fixture TRANSITION:traced TARGET:misc
    ITEM: synthetic action item from stub-bg-inject.sh
---
-- META: codex_status=stub elapsed_s=0 reason=test_fixture
EOF
`,
  { mode: 0o755 }
);

writeFileSync(
  stubBgScriptSkip,
  `#!/bin/bash
echo "INJECT_DECISION:skip REASON:test_stub_healthy"
`,
  { mode: 0o755 }
);

writeFileSync(
  stubBgScriptSlow,
  `#!/bin/bash
# Sleeps long enough for the second concurrent tick() to hit the in-process lock.
sleep 1.5
echo "INJECT_DECISION:skip REASON:test_stub_slow_done"
`,
  { mode: 0o755 }
);

// ─── 1. parseFirstLine ────────────────────────────────────────
console.log("[1] parseFirstLine");
const opsAuditModulePath = path.join(
  os.homedir(),
  ".claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/dist/opsAudit.js"
);
const distExists = existsSync(opsAuditModulePath);

// Set env override BEFORE the first dynamic import — opsAudit.ts captures
// BG_SCRIPT_PATH at module-load via process.env.OPS_AUDIT_BG_SCRIPT_OVERRIDE.
process.env.OPS_AUDIT_BG_SCRIPT_OVERRIDE = stubBgScript;
// Tight timeout so a misbehaving stub can't hang tests beyond a few seconds.
process.env.MOP_OPS_AUDIT_BG_TIMEOUT_MS = "5000";

if (!distExists) {
  console.log(`  (SKIP) dist not built yet: ${opsAuditModulePath}`);
} else {
  const { parseFirstLine } = await import(opsAuditModulePath);

  test("parses INJECT_DECISION:inject + REASON", () => {
    const r = parseFirstLine("INJECT_DECISION:inject REASON:unassigned_status_todo\n---\nACTION_ITEMS:");
    assert.deepEqual(r, { decision: "inject", reason: "unassigned_status_todo" });
  });

  test("parses INJECT_DECISION:skip + REASON", () => {
    const r = parseFirstLine("INJECT_DECISION:skip REASON:healthy\n");
    assert.deepEqual(r, { decision: "skip", reason: "healthy" });
  });

  test("tolerates extra whitespace", () => {
    const r = parseFirstLine("INJECT_DECISION:  inject   REASON:  pm_punt");
    assert.equal(r.decision, "inject");
    assert.equal(r.reason, "pm_punt");
  });

  test("handles REASON with spaces in label", () => {
    const r = parseFirstLine("INJECT_DECISION:inject REASON:multi word reason here");
    assert.equal(r.decision, "inject");
    assert.equal(r.reason, "multi word reason here");
  });

  test("returns null for malformed first line", () => {
    assert.equal(parseFirstLine("HELLO WORLD"), null);
    assert.equal(parseFirstLine(""), null);
    assert.equal(parseFirstLine("inject\nREASON:foo"), null);
  });

  test("missing REASON defaults to 'unspecified'", () => {
    const r = parseFirstLine("INJECT_DECISION:inject");
    assert.equal(r?.decision, "inject");
    assert.equal(r?.reason, "unspecified");
  });
}

// ─── 2. Scheduler lock + bg-script invocation (deterministic stub) ────
console.log("\n[2] scheduler lock + stub bg-script invocation (deterministic)");
if (!distExists) {
  console.log("  (SKIP) dist not built yet");
} else {
  const { OpsAuditScheduler } = await import(opsAuditModulePath);

  // Minimal DB + relay stubs — assert never_called for relayInject in skip path,
  // and called_once with expected freeform message shape for inject path.
  function makeStubs() {
    const dbStub = {
      _kv: new Map(),
      _events: [],
      getConfig(k) { return this._kv.get(k) ?? null; },
      setConfig(k, v) { this._kv.set(k, v); },
      logEvent(slot, type, key, value, payload) {
        this._events.push({ slot, type, key, value, payload });
      },
    };
    const relayStub = {
      _injected: [],
      injectToPM(msg) {
        this._injected.push(msg);
        return true;
      },
    };
    return { dbStub, relayStub };
  }

  await asyncTest("paused scheduler skips on scheduled tick (no bg-script invoked)", async () => {
    const { dbStub, relayStub } = makeStubs();
    const scheduler = new OpsAuditScheduler(dbStub, relayStub);
    dbStub.setConfig("ops_audit_paused", "true");
    const r = await scheduler.tick("scheduled");
    assert.equal(r.decision, "skip");
    assert.equal(r.reason, "paused");
    assert.equal(relayStub._injected.length, 0, "paused tick must not inject");
  });

  await asyncTest("manual tick bypasses pause and routes inject payload via relay", async () => {
    const { dbStub, relayStub } = makeStubs();
    const scheduler = new OpsAuditScheduler(dbStub, relayStub);
    // Stub bg-script returns INJECT_DECISION:inject — manual tick bypasses pause.
    dbStub.setConfig("ops_audit_paused", "true");
    const r = await scheduler.tick("manual");
    assert.equal(r.decision, "inject", `expected inject, got ${r.reason}`);
    assert.equal(r.reason, "test_inject_fixture");
    assert.equal(relayStub._injected.length, 1, "inject path must call relay.injectToPM exactly once");
    assert.ok(
      relayStub._injected[0].startsWith("MoP: hourly ops audit for test_inject_fixture\n"),
      `expected MoP-prefixed freeform message; got: ${relayStub._injected[0].slice(0, 120)}`
    );
    assert.ok(
      relayStub._injected[0].includes("INJECT_DECISION:inject"),
      "payload must include the bg-script INJECT_DECISION line"
    );
  });

  await asyncTest("concurrent tick() coalesces — lock_held_prior_tick_running fires", async () => {
    const { dbStub, relayStub } = makeStubs();
    // Switch override to slow stub for THIS test only (in-process — affects module
    // constant only if we re-import; instead we exploit the lock via two parallel
    // manual ticks against the inject stub which completes fast). To force a real
    // race we point at the slow stub via env + re-import a fresh copy.
    const slowModuleUrl = opsAuditModulePath + `?cb=${Date.now()}`;
    process.env.OPS_AUDIT_BG_SCRIPT_OVERRIDE = stubBgScriptSlow;
    const slowMod = await import(slowModuleUrl);
    const scheduler2 = new slowMod.OpsAuditScheduler(dbStub, relayStub);
    const a = scheduler2.tick("manual");
    const b = scheduler2.tick("manual");
    const [resA, resB] = await Promise.all([a, b]);
    const lockSkips = [resA, resB].filter(
      (r) => r.decision === "skip" && r.reason === "lock_held_prior_tick_running"
    );
    assert.equal(lockSkips.length, 1, `expected exactly one lock-skip; got A=${JSON.stringify(resA)} B=${JSON.stringify(resB)}`);
    // Restore inject-stub override for downstream tests.
    process.env.OPS_AUDIT_BG_SCRIPT_OVERRIDE = stubBgScript;
  });

  await asyncTest("scheduled tick with skip stub does NOT inject", async () => {
    const { dbStub, relayStub } = makeStubs();
    const skipModuleUrl = opsAuditModulePath + `?cb=${Date.now()}-skip`;
    process.env.OPS_AUDIT_BG_SCRIPT_OVERRIDE = stubBgScriptSkip;
    const skipMod = await import(skipModuleUrl);
    const scheduler3 = new skipMod.OpsAuditScheduler(dbStub, relayStub);
    const r = await scheduler3.tick("scheduled");
    assert.equal(r.decision, "skip");
    assert.equal(r.reason, "test_stub_healthy");
    assert.equal(relayStub._injected.length, 0, "skip path must not inject");
    // Restore inject-stub override.
    process.env.OPS_AUDIT_BG_SCRIPT_OVERRIDE = stubBgScript;
  });
}

// ─── 3. parseRelayMessage routing for hourly-audit freeform (Fix 4 option b) ─
// Hourly-audit payload uses the "MoP: hourly ops audit for <reason>" prefix.
// parseRelayMessage in relay.ts handles Shapes 1/2a/2b (slot-keyed slash + check + idle/active).
// Shape 3 (hourly ops audit) is INTENTIONALLY not added to parseRelayMessage —
// it routes through the freeform branch of injectToPM, which enqueues with
// slot=0 + a synthetic event-type derived from the message hash, so multiple
// distinct hourly-audit payloads do NOT collapse onto each other when PM is busy.
console.log("\n[3] parseRelayMessage routing — hourly-audit IS freeform (option b)");
const relayModulePath = path.join(
  os.homedir(),
  ".claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/dist/relay.js"
);
if (existsSync(relayModulePath)) {
  // parseRelayMessage is NOT exported (file-internal). Verify via source-level grep
  // that no hourly-ops-audit branch was added — keeps option-b design honest.
  const relaySrc = path.join(
    os.homedir(),
    ".claude/plugins/cache/rajiv-plugins/master-of-panes/1.0.0/src/relay.ts"
  );
  if (existsSync(relaySrc)) {
    const { readFileSync } = await import("node:fs");
    const src = readFileSync(relaySrc, "utf8");
    test("parseRelayMessage does NOT have an hourly-ops-audit Shape 3 branch (option-b freeform routing)", () => {
      // Verify the function exists.
      assert.ok(src.includes("function parseRelayMessage"), "parseRelayMessage must exist");
      // Slice the function BODY only (between '{' after the signature and the
      // function's terminal '\n}\n'). Doc comments above the function are
      // permitted to mention hourly-ops-audit; what we forbid is an actual
      // regex / branch keying on the prefix inside the body.
      const fnStart = src.indexOf("function parseRelayMessage");
      const bodyStart = src.indexOf("{", fnStart);
      const bodyEnd = src.indexOf("\n}\n", bodyStart);
      assert.ok(bodyStart !== -1 && bodyEnd !== -1, "could not slice parseRelayMessage body");
      const fnBody = src.slice(bodyStart, bodyEnd);
      // Reject ONLY regex-like patterns keying on the hourly-ops-audit prefix.
      // The pass-through comment ("Shape 3 ... intentional pass-through") is allowed.
      const hourlyRegexBranch = /MoP:\\s\+hourly|hourly\\s\+ops\\s\+audit|new RegExp\([^)]*hourly[^)]*ops[^)]*audit/i;
      assert.ok(
        !hourlyRegexBranch.test(fnBody),
        "parseRelayMessage MUST NOT contain a regex keying on hourly-ops-audit prefix (option-b freeform routing). " +
          "If you want Shape 3 added (option-a), update opsAudit.ts comments + this test."
      );
    });
    test("relay.ts comments document freeform routing for hourly-audit", () => {
      // Confirm a free-form / hourly-audit explanatory comment exists somewhere in relay.ts.
      assert.ok(
        /free[\s-]?form|hourly[\s_-]*ops[\s_-]*audit/i.test(src),
        "expected a comment in relay.ts explaining freeform fall-through for hourly-audit; " +
          "add a comment near the freeform-branch of injectToPM citing R1 fix 4 option b."
      );
    });
  } else {
    console.log("  (SKIP) relay.ts source not present");
  }
} else {
  console.log("  (SKIP) relay.js dist not built");
}

// ─── 4. pm-context-injector.sh recognises Shape 3 ─
console.log("\n[4] pm-context-injector.sh Shape 3");
const injector = "/Users/rajiv/Downloads/projects/heydonna-app/.claude/hooks/pm-context-injector.sh";
if (!existsSync(injector)) {
  console.log("  (SKIP) injector not at " + injector);
} else {
  test("hook accepts hourly-ops-audit prefix payload", () => {
    const probe = `MoP: hourly ops audit for unassigned_status_todo\nINJECT_DECISION:inject REASON:unassigned_status_todo\n---\nACTION_ITEMS:\n  - OWNER:dhruv EVIDENCE:test TRANSITION:traced TARGET:misc\n    ITEM: probe\n---\n`;
    const json = JSON.stringify({ prompt: probe });
    const out = spawnSync("/bin/bash", [injector], {
      input: json,
      encoding: "utf8",
      env: { ...process.env, CLAUDE_PROJECT_DIR: "/Users/rajiv/Downloads/projects/heydonna-app" },
      timeout: 10000,
    });
    assert.ok(
      out.stdout.includes("MoP_SLOT_NOTIFICATION") && out.stdout.includes("hourly-ops-audit"),
      `expected MoP_SLOT_NOTIFICATION + hourly-ops-audit in stdout; got:\n${out.stdout.slice(0, 400)}`
    );
  });

  test("hook does NOT match for non-hourly MoP prefix", () => {
    const probe = `MoP: slot 2 idle\n`;
    const json = JSON.stringify({ prompt: probe });
    const out = spawnSync("/bin/bash", [injector], {
      input: json,
      encoding: "utf8",
      env: { ...process.env, CLAUDE_PROJECT_DIR: "/Users/rajiv/Downloads/projects/heydonna-app" },
      timeout: 10000,
    });
    assert.ok(
      !out.stdout.includes("hourly-ops-audit"),
      `expected no hourly-ops-audit match for slot-idle payload; got:\n${out.stdout.slice(0, 400)}`
    );
    assert.ok(
      out.stdout.includes("slot-idle"),
      `expected slot-idle skill hint; got:\n${out.stdout.slice(0, 400)}`
    );
  });
}

// ─── 5. Bg-script fixture sanity (file presence + executable, no real run) ─
console.log("\n[5] bg-script existence + executable");
const bgScript = path.join(os.homedir(), ".claude/scripts/hourly-ops-review-bg.sh");
test("bg-script file exists and is executable", () => {
  assert.ok(existsSync(bgScript), `missing bg-script at ${bgScript}`);
  const ok = spawnSync("/bin/bash", ["-c", `[[ -x ${bgScript} ]]`]).status === 0;
  assert.ok(ok, `bg-script not executable: ${bgScript}`);
});
test("bg-script prompt template file exists (parity source — R1 fix 2)", () => {
  const promptTpl = path.join(os.homedir(), ".claude/scripts/hourly-ops-review-prompt.txt");
  assert.ok(existsSync(promptTpl), `missing prompt template at ${promptTpl}`);
});
// Pre-load fs once for the synchronous bg-script assertions below.
const { readFileSync: _readFileSync } = await import("node:fs");
test("bg-script references SLACK_BRIDGE_DB env override (R1 fix 1)", () => {
  const src = _readFileSync(bgScript, "utf8");
  assert.ok(
    src.includes("SLACK_BRIDGE_DB:-/Users/rajiv/Downloads/projects/tmux-slack-bridge/bridge.db"),
    "bg-script must point SLACK_BRIDGE_DB at canonical tmux-slack-bridge/bridge.db with env override"
  );
  assert.ok(src.includes("Slack inputs (last 60 min — bridge DB)"), "bg-script must include Slack inputs section");
  assert.ok(src.includes("C0ALZJHGE49"), "bg-script must query #heydonna-dev channel");
  assert.ok(src.includes("D0AMF0XE6TS"), "bg-script must query Rajiv↔PM DM");
  assert.ok(src.includes("U0ALEAYCAUT"), "bg-script must filter Kanban posts by Dhruv user_id");
});

// Note: we DELIBERATELY do not invoke the real bg-script with --reason boot here.
// That would call Codex + slack-bridge DB + gh PR list, and on inject would
// emit a payload — but since this test never wires a relay, nothing gets
// injected to a PM pane. Even so, R1 directive forbids real Codex invocation
// during `npm test`. The stub-driven scheduler tests above (section 2) cover
// the full inject + skip paths with zero side effects.

// ─── Cleanup ─────────────────────────────────────────────────────────
try { unlinkSync(stubBgScript); } catch { /* ignore */ }
try { unlinkSync(stubBgScriptSkip); } catch { /* ignore */ }
try { unlinkSync(stubBgScriptSlow); } catch { /* ignore */ }

// ─── Summary ──────────────────────────────────────────────────
console.log("\n=== summary ===");
const pass = RESULTS.filter((r) => r.ok).length;
const fail = RESULTS.filter((r) => !r.ok).length;
console.log(`pass=${pass} fail=${fail}`);
if (fail > 0) {
  console.log("\nFailures:");
  for (const r of RESULTS.filter((r) => !r.ok)) {
    console.log(`  - ${r.name}: ${r.err}`);
  }
  process.exit(1);
}
process.exit(0);
