import assert from "node:assert/strict";
import test from "node:test";

import type { PasteGuard } from "../src/pasteGuard.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

type Shell = ConstructorParameters<typeof TmuxRelay>[1] extends infer D ? D extends { runShell?: infer R } ? R : never : never;

function relayWith(commands: string[], events: string[] = []) {
  const runShell = (async (command: string) => {
    commands.push(command);
    events.push(command.replace(/ -b \S+/, "").split(" ").slice(0, 2).join(" "));
    return { stdout: "", stderr: "" };
  }) as unknown as Shell;
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell, pmRuntime: "claude", pmPaneReady: async () => true });
  relay.drainPMQueue = async () => 0;
  return relay;
}

const TEXT = "# slack-channel C0A\n[slack-event C0A/1.1]\nhello";

test("a guarded paste targets the pinned immutable pane id and the guard runs between load-buffer and paste-buffer", async () => {
  const commands: string[] = [];
  const relay = relayWith(commands);
  const seen: string[] = [];
  const guard: PasteGuard = {
    paneId: "%7",
    beforePaste: async (text) => {
      seen.push(text);
      seen.push(`loaded=${commands.some((c) => c.includes("load-buffer"))} pasted=${commands.some((c) => c.includes("paste-buffer"))}`);
      return { ok: true };
    },
    beforeSubmit: async () => {
      seen.push(`submit-check pasted=${commands.some((c) => c.includes("paste-buffer"))} enters=${commands.filter((c) => c.includes("send-keys")).length}`);
      return { ok: true };
    },
  };
  const result = await relay.submitToPM(TEXT, undefined, guard);
  assert.equal(result.ok, true);
  assert.deepEqual(seen, [TEXT, "loaded=true pasted=false", "submit-check pasted=true enters=0"]);
  const paste = commands.find((c) => c.includes("paste-buffer"))!;
  const enter = commands.find((c) => c.includes("send-keys"))!;
  assert.match(paste, /-t %7 /);
  assert.match(enter, /-t %7 Enter/);
  assert.ok(!commands.some((c) => (c.includes("paste-buffer") || c.includes("send-keys")) && c.includes(DEFAULT_CONFIG.pmPaneAddress)), "never the mutable PM address");
});

test("a guard refusal means nothing was pasted and the result says so", async () => {
  const commands: string[] = [];
  const relay = relayWith(commands);
  const result = await relay.submitToPM(TEXT, undefined, { paneId: "%7", beforePaste: async () => ({ ok: false, reason: "recipient_changed" }), beforeSubmit: async () => ({ ok: true }) });
  assert.deepEqual([result.ok, result.ambiguous, result.refused], [false, false, "recipient_changed"]);
  assert.ok(!commands.some((c) => c.includes("paste-buffer") || c.includes("send-keys")));
  assert.ok(commands.some((c) => c.includes("delete-buffer")), "the loaded buffer is discarded");
});

test("a guard that throws is a refusal before any write, never an ambiguous paste", async () => {
  const commands: string[] = [];
  const relay = relayWith(commands);
  const result = await relay.submitToPM(TEXT, undefined, { paneId: "%7", beforePaste: async () => { throw new Error("tmux probe hung"); }, beforeSubmit: async () => ({ ok: true }) });
  assert.deepEqual([result.ok, result.ambiguous, result.refused], [false, false, "guard_error"]);
  assert.ok(!commands.some((c) => c.includes("paste-buffer")));
});

test("the guard runs inside the serialized writer: the second paste's guard starts only after the first paste finished", async () => {
  const order: string[] = [];
  const commands: string[] = [];
  const relay = relayWith(commands, []);
  const guardFor = (name: string): PasteGuard => ({
    paneId: "%7",
    beforePaste: async () => { order.push(`${name}:guard after_enters=${commands.filter((c) => c.includes("send-keys")).length}`); return { ok: true }; },
    beforeSubmit: async () => ({ ok: true }),
  });
  await Promise.all([relay.submitToPM(TEXT, undefined, guardFor("A")), relay.submitToPM(TEXT + "2", undefined, guardFor("B"))]);
  assert.deepEqual(order, ["A:guard after_enters=0", "B:guard after_enters=1"]);
});

test("an unguarded send keeps the existing behaviour: the mutable PM address, no guard", async () => {
  const commands: string[] = [];
  const relay = relayWith(commands);
  const result = await relay.submitToPM(TEXT);
  assert.equal(result.ok, true);
  assert.match(commands.find((c) => c.includes("paste-buffer"))!, new RegExp(`-t ${DEFAULT_CONFIG.pmPaneAddress.replace(/[.]/g, "\\.")} `));
});

test("a guard that refuses before Enter: the paste may have landed, so NO key is sent and the result is ambiguous, never zero-effect", async () => {
  const commands: string[] = [];
  const relay = relayWith(commands);
  const result = await relay.submitToPM(TEXT, undefined, {
    paneId: "%7",
    beforePaste: async () => ({ ok: true }),
    beforeSubmit: async () => ({ ok: false, reason: "recipient_changed" }),
  });
  assert.deepEqual([result.ok, result.ambiguous, result.refused, result.withheld], [false, true, undefined, "recipient_changed"]);
  assert.equal(commands.filter((c) => c.includes("paste-buffer")).length, 1, "the paste was already invoked");
  assert.equal(commands.filter((c) => c.includes("send-keys")).length, 0, "no Enter and no other key reaches the pane");
});

test("a guard that throws before Enter withholds Enter and keeps the send ambiguous", async () => {
  const commands: string[] = [];
  const relay = relayWith(commands);
  const result = await relay.submitToPM(TEXT, undefined, {
    paneId: "%7",
    beforePaste: async () => ({ ok: true }),
    beforeSubmit: async () => { throw new Error("tmux probe hung"); },
  });
  assert.deepEqual([result.ok, result.ambiguous, result.withheld], [false, true, "guard_error"]);
  assert.equal(commands.filter((c) => c.includes("send-keys")).length, 0);
});

test("the pre-Enter check runs inside the serialized writer: the second send's paste and check start only after the first send finished", async () => {
  const order: string[] = [];
  const commands: string[] = [];
  const relay = relayWith(commands);
  const guardFor = (name: string): PasteGuard => ({
    paneId: "%7",
    beforePaste: async () => { order.push(`${name}:paste-check`); return { ok: true }; },
    beforeSubmit: async () => { order.push(`${name}:enter-check enters=${commands.filter((c) => c.includes("send-keys")).length}`); return { ok: true }; },
  });
  await Promise.all([relay.submitToPM(TEXT, undefined, guardFor("A")), relay.submitToPM(TEXT + "2", undefined, guardFor("B"))]);
  assert.deepEqual(order, ["A:paste-check", "A:enter-check enters=0", "B:paste-check", "B:enter-check enters=1"]);
});
