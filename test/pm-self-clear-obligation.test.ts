import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { resolveAcceptedPmSelfClear } from "../src/pmSelfClearObligation.js";

for (const delivery of ["deferred_busy", "pending", "sent"]) {
  test(`accepted ${delivery} resolves canonical obligation before clear acknowledgement`, async () => {
    const calls: string[][] = [];
    assert.equal(await resolveAcceptedPmSelfClear(delivery, "2026-10-29T15:57:50Z", async (file, args) => {
      calls.push([file, ...args]);
    }), true);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0]!.slice(2, 10), ["obligation-resolve", "--kind", "pm-self-clear", "--target-type", "session", "--target-id", "pm-dhruva", "--reason"]);
    assert.match(calls[0]!.at(-1)!, /2026-10-29T15:57:50Z/);
    assert.match(calls[0]!.at(-1)!, /accepted POST/);
  });
}
test("rejected requests never resolve the obligation", async () => {
  for (const delivery of ["failed", "recent", "uncertain"]) {
    assert.equal(await resolveAcceptedPmSelfClear(delivery, null, async () => { throw new Error("must not call"); }), false);
  }
});
test("POST acceptance opens the save-handoff obligation and resolves the legacy one before responding", () => {
  // Rajiv D0AMF0XE6TS ts 1793732417.459339: save memory first, then MoP injects /clear at the next Stop.
  const source = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const accept = source.indexOf("const handoff = await requestPmHandoffClear(db, {");
  const resolve = source.indexOf("await resolveAcceptedPmSelfClear", accept);
  assert.ok(accept > 0);
  assert.ok(resolve > accept);
  assert.ok(resolve < source.indexOf("deferred (pm_save_handoff obligation opened", accept));
});
