import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";
import ts from "typescript";
import { MoPDatabase } from "../src/db.js";
import { ProcessHealthChecker } from "../src/health.js";
import { DEFAULT_CONFIG } from "../src/types.js";

function database() { return new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" }); }
const source = (name: string) => readFileSync(new URL(`../src/${name}.ts`, import.meta.url), "utf8");
function find(node: ts.Node, predicate: (n: ts.Node) => boolean): ts.Node | undefined {
  if (predicate(node)) return node;
  let result: ts.Node | undefined;
  ts.forEachChild(node, child => { result ??= find(child, predicate); });
  return result;
}
function route(path: string, method: string, db: MoPDatabase) {
  const tree = ts.createSourceFile("server.ts", source("server"), ts.ScriptTarget.Latest, true);
  const call = find(tree, n => ts.isCallExpression(n) && n.expression.getText(tree) === `app.${method}` && n.arguments[0]?.getText(tree) === JSON.stringify(path)) as ts.CallExpression;
  assert.ok(call);
  const js = ts.transpileModule(`const handler = ${call.arguments[1].getText(tree)};`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return new Function("db", `${js}; return handler;`)(db);
}
function stopExit(db: MoPDatabase, sent: number[]) {
  const tree = ts.createSourceFile("hooks.ts", source("hooks"), ts.ScriptTarget.Latest, true);
  const method = find(tree, n => ts.isMethodDeclaration(n) && n.name.getText(tree) === "handleStop")!;
  const exit = find(method, n => ts.isIfStatement(n) && n.expression.getText(tree).startsWith("this.db.getExitPending()"))!;
  const dnd = find(method, n => ts.isIfStatement(n) && n.expression.getText(tree) === "slot.dnd")!;
  const js = ts.transpileModule(`function run(slotNum: number) { const slot = this.db.getSlot(slotNum) || {}; ${dnd.getText(tree)} ${exit.getText(tree)} }`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return new Function(`${js}; return run;`)().bind({ db, relay: { sendToSlot: (slot: number) => sent.push(slot), injectToPM() {} } });
}

test("REST accepts and persists dev-only scope; omission restores legacy global scope", async () => {
  const db = database();
  try {
    const post = route("/exit-pending", "post", db);
    const get = route("/exit-status", "get", db);
    const context = (body: unknown) => ({ req: { json: async () => body }, json: (body: unknown, status = 200) => ({ body, status }) });
    assert.equal((await post(context({ enabled: true, dev_only: true }))).status, 200);
    assert.equal(db.getConfig("exit_pending_dev_only"), "true");
    assert.equal(get(context(null)).body.dev_only, true);
    assert.equal((await post(context({ enabled: true, dev_only: "true" }))).status, 400);
    assert.equal(db.getExitStatus().dev_only, true, "invalid request cannot mutate the scope");
    await post(context({ enabled: true }));
    assert.equal(get(context(null)).body.dev_only, false);
    await post(context({ enabled: false, dev_only: true }));
    assert.equal(db.getExitStatus().pending, false);
    assert.equal(db.getExitStatus().dev_only, false);
  } finally { db.close(); }
});

test("dev-only scope and cycled state survive reopening the database", () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-exit-scope-test-"));
  const config = { ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") };
  let db = new MoPDatabase(config);
  try {
    db.setExitPending(true, true);
    db.markSlotExitCycled(1);
    db.close();
    db = new MoPDatabase(config);
    assert.equal(db.getExitStatus().dev_only, true);
    assert.equal(db.getExitStatus().pending, true);
    assert.equal(db.getExitStatus().cycled[1], true);
    assert.equal(db.getExitStatus().cycled[0], false);
  } finally { db.close(); rmSync(directory, { recursive: true, force: true }); }
});

for (const devOnly of [false, true]) {
  test(`Stop exit scope dev_only=${devOnly}: PM guard, DND, and completion`, () => {
    const db = database();
    try {
      const sent: number[] = [];
      const stop = stopExit(db, sent);
      db.setExitPending(true, devOnly);
      if (devOnly) {
        stop(0);
        assert.deepEqual(sent, [], "PM Stop must not exit while a dev-only episode is pending");
        assert.equal(db.getExitStatus().cycled[0], false);
      }
      db.updateSlot(1, { dnd: true });
      stop(1);
      assert.deepEqual(sent, []);
      db.updateSlot(1, { dnd: false });
      for (const slot of Object.keys(db.getExitStatus().cycled).map(Number).filter(n => n > 0)) stop(slot);
      assert.equal(db.getExitStatus().pending, !devOnly, "dev-only completes without PM; legacy waits for PM");
      stop(0);
      assert.equal(sent.includes(0), !devOnly);
      assert.equal(db.getExitPending(), false);
    } finally { db.close(); }
  });

  test(`health exit scope dev_only=${devOnly}: PM guard, DND, and completion`, async () => {
    const db = database();
    const sent: number[] = [];
    const observed: number[] = [];
    try {
      db.setExitPending(true, devOnly);
      const slots = Object.keys(db.getExitStatus().cycled).map(Number).filter(n => n > 0);
      for (const slot of slots) db.markSlotExitCycled(slot);
      db.setConfig("exit_cycled_1", "false");
      db.updateSlot(1, { dnd: true, active_turn_state: "inactive" });
      const relay = {
        observeSlotPane: async (slot: number) => { observed.push(slot); return { paneId: `%${slot}` }; },
        getSlotActivityState: async () => "idle",
        deliverGuardedToSlot: async (slot: number, _command: string, guard: any) => {
          assert.equal(await guard.recheck(), null);
          assert.equal(guard.finalCheck(), null);
          sent.push(slot);
          return { outcome: "delivered" };
        },
        injectToPM() {},
      };
      const health = new ProcessHealthChecker(db, relay as any) as any;
      health.getPaneCommand = async () => "claude";
      await health.checkExitPending();
      assert.equal(sent.includes(1), false, "DND is preserved");
      assert.equal(observed.includes(0), !devOnly);
      assert.equal(db.getExitPending(), true, "DND slot still needs to cycle");
      db.updateSlot(1, { dnd: false });
      await health.checkExitPending();
      assert.equal(sent.includes(1), true);
      assert.equal(sent.includes(0), !devOnly);
      assert.equal(db.getExitPending(), false);
    } finally { db.close(); }
  });
}
