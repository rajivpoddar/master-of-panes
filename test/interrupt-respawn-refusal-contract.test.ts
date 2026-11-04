import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { Hono } from "hono";
import ts from "typescript";
import { z } from "zod";
import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { respawnSessionAt, respawnWorkToken } from "../src/respawnWhenIdle.js";
import {
  idlePromptStaleTurnRemediation,
  registerWedgeInterruptRoute,
  WEDGED_BUSY_REMEDIATION,
} from "../src/wedgeInterruptRoute.js";

// 2026-11-04 live run (S1 ep 1732, turn 4755b715...): the PM's shell-quoted
// body brace-expanded into four non-object posts, the route parsed {} and said
// "identity moved"; respawn then refused "busy" on an Escape-ended turn whose
// row was never settled (no Stop). Both refusals now name the real cause and
// the exact contract. Reason codes are unchanged.
const database = () => new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });

function busyRow(db: MoPDatabase) {
  db.updateSlot(1, { occupied: true, idle: false, active_turn_id: "4755b715", active_turn_state: "active" } as any);
  (db as any).db.prepare("UPDATE slots SET assignment_epoch = 1732, occupied = 1, idle = 0 WHERE slot = 1").run();
}

test("interrupt-turn: malformed body vs moved identity are typed, with the body contract; zero effects", async () => {
  const db = database();
  const sends: number[] = [];
  try {
    busyRow(db);
    const app = new Hono();
    registerWedgeInterruptRoute(app, {
      db,
      verifyPaneIdentity: async () => ({ ok: true, snapshot: { paneId: "%1", currentPath: "/x" } }),
      captureSnapshot: async () => null,
      observeCheckout: async () => null,
      sendInterruptKey: async (slot) => { sends.push(slot); return true; },
      nowMs: () => Date.now(),
    });
    const post = (body: string) => app.request("/slots/1/interrupt-turn", {
      method: "POST", headers: { "content-type": "application/json" }, body,
    });
    for (const fragment of ['"slot":1', '"expected_assignment_epoch":1732', "", '{"slot":1,"expected_assignment_epoch":"1732","expected_active_turn_id":"4755b715"}']) {
      const res = await post(fragment);
      const json = await res.json() as Record<string, string>;
      assert.equal(res.status, 409);
      assert.equal(json.reason, "stale_identity");
      assert.equal(json.identity_detail, "pins_missing_or_malformed", fragment);
      assert.match(json.error, /"expected_assignment_epoch": <integer>, "expected_active_turn_id": "<string>"/);
      assert.doesNotMatch(json.error, /identity moved/);
    }
    const moved = await post(JSON.stringify({ expected_assignment_epoch: 1731, expected_active_turn_id: "4755b715" }));
    const movedJson = await moved.json() as Record<string, string>;
    assert.equal(movedJson.identity_detail, "pins_mismatch");
    assert.match(movedJson.error, /current assignment_epoch=1732 active_turn_id=4755b715/);
    assert.match(movedJson.error, /abandon-turn/);
    // Correct pins pass the identity leg (refused later only for pane evidence).
    const ok = await post(JSON.stringify({ expected_assignment_epoch: 1732, expected_active_turn_id: "4755b715" }));
    assert.notEqual(((await ok.json()) as Record<string, string>).reason, "stale_identity");
    assert.deepEqual(sends, []);
  } finally { db.close(); }
});

test("respawn busy refusal names the unsettled turn and the abandon-turn / dev_only-exit contract; zero effects", async () => {
  const source = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const tree = ts.createSourceFile("server.ts", source, ts.ScriptTarget.Latest, true);
  let handler: ts.Node | undefined;
  const visit = (node: ts.Node) => {
    if (ts.isCallExpression(node) && node.expression.getText(tree) === "app.post"
        && node.arguments[0]?.getText(tree) === '"/slots/:slotNum/respawn"') handler = node.arguments[1];
    ts.forEachChild(node, visit);
  };
  visit(tree);
  assert.ok(handler);
  const js = ts.transpileModule(`const handler = ${handler!.getText(tree)};`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  const db = database();
  const effects: string[] = [];
  try {
    busyRow(db);
    const run = new Function("db", "slotParamSchema", "ALLOWED_MODELS", "RESTART_COMMANDS", "verifyPaneIdentity", "healthChecker",
      "respawnSessionAt", "respawnWorkToken", "pmSoftBoundary", "WEDGED_BUSY_REMEDIATION", "idlePromptStaleTurnRemediation",
      "deliverRespawnExit", `${js}; return handler;`)(
      db, z.coerce.number().int().min(0).max(8), new Set(["sonnet"]), { 1: "launch-slot-1.sh" },
      async () => ({ ok: true, snapshot: { paneId: "%1", currentPath: "/x" } }),
      { isPmInitiatedRespawn: () => false, getPaneCommandPublic: async () => { effects.push("probe"); return "claude"; } },
      respawnSessionAt, respawnWorkToken, { isHeld: () => false, isSafe: () => false }, WEDGED_BUSY_REMEDIATION,
      idlePromptStaleTurnRemediation, async () => { effects.push("exit"); return {}; },
    );
    let status = 0; let out: Record<string, unknown> = {};
    await run({
      req: { param: () => "1", json: async () => ({}) },
      json: (body: Record<string, unknown>, code = 200) => { out = body; status = code; return body; },
    });
    assert.equal(status, 409);
    assert.equal(out.reason, "slot_busy_respawn_refused", "reason code unchanged");
    assert.equal(out.active_turn_id, "4755b715");
    assert.equal(out.active_turn_state, "active");
    assert.match(String(out.error), /abandon-turn/);
    assert.match(String(out.stale_turn_remediation), /POST \/slots\/1\/abandon-turn/);
    assert.match(String(out.stale_turn_remediation), /"dev_only":true/);
    assert.deepEqual(effects, [], "no /exit or probe before refusal");
  } finally { db.close(); }
});
