import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import {
  expectedCheckoutPath,
  paneAddress,
  validatePaneIdentity,
  validatePaneLayout,
  verifyPaneIdentity,
} from "../src/paneIdentity.js";

test("authoritative six-pane layout is one-to-one and rejects aliases/missing panes", () => {
  assert.deepEqual(
    validatePaneLayout(["0:0.0", "0:0.1", "0:0.2", "0:0.3", "0:0.4", "0:0.5", "0:0.6"]),
    { ok: true },
  );
  assert.equal(validatePaneLayout(["0:0.0", "0:0.1", "0:0.2", "0:0.3", "0:0.4", "0:0.4", "0:0.6"]).ok, false);
  assert.equal(validatePaneLayout(["0:0.0", "0:0.1", "0:0.2", "0:0.3", "0:0.4", "0:0.5"]).ok, false);
  assert.equal(paneAddress(4), "0:0.4");
});

test("pane identity accepts the exact slot checkout and refuses S4/S5 aliasing", async () => {
  assert.equal(expectedCheckoutPath(4), "/Users/rajiv/Downloads/projects/heydonna-app-3004");
  assert.equal(expectedCheckoutPath(5), "/Users/rajiv/Downloads/projects/heydonna-app-3005");
  assert.equal(
    validatePaneIdentity(4, "/Users/rajiv/Downloads/projects/heydonna-app-3004", "%42").ok,
    true,
  );
  const mismatch = validatePaneIdentity(4, "/Users/rajiv/Downloads/projects/heydonna-app-3005", "%42");
  assert.equal(mismatch.ok, false);
  if (!mismatch.ok) assert.equal(mismatch.reason, "checkout_mismatch");

  const seen: string[] = [];
  const exact = await verifyPaneIdentity(4, async (command) => {
    seen.push(command);
    if (command.startsWith("tmux display-message")) {
      return { stdout: "%42\t/Users/rajiv/Downloads/projects/heydonna-app-3004\n", stderr: "" };
    }
    return { stdout: "/Users/rajiv/Downloads/projects/heydonna-app-3004\n", stderr: "" };
  });
  assert.equal(exact.ok, true);
  assert.equal(seen.length, 2);
  assert.match(seen[0], /0:0\.4/);
  if (exact.ok) assert.equal(exact.snapshot.paneId, "%42");

  const refused = await verifyPaneIdentity(4, async () => ({
    stdout: "%43\t/Users/rajiv/Downloads/projects/heydonna-app-3005\n",
    stderr: "",
  }));
  assert.equal(refused.ok, false);
  if (!refused.ok) assert.equal(refused.reason, "checkout_mismatch");
});

test("pane identity fails closed when tmux is unavailable or slot is unknown", async () => {
  const unavailable = await verifyPaneIdentity(5, async () => {
    throw new Error("no-client-found");
  });
  assert.equal(unavailable.ok, false);
  if (!unavailable.ok) assert.equal(unavailable.reason, "pane_unavailable");
  const unknown = await verifyPaneIdentity(7, async () => ({ stdout: "", stderr: "" }));
  assert.equal(unknown.ok, false);
  if (!unknown.ok) assert.equal(unknown.reason, "unknown_slot");
});

test("relay refuses a mismatched S4 target before any pane delivery", async () => {
  const commands: string[] = [];
  const relay = new TmuxRelay(DEFAULT_CONFIG, {
    runShell: async (command) => {
      commands.push(command);
      if (command.startsWith("tmux display-message")) {
        return { stdout: "%42\t/Users/rajiv/Downloads/projects/heydonna-app-3005\n", stderr: "" };
      }
      return { stdout: "", stderr: "" };
    },
  });
  assert.equal(await relay.sendToSlotAsync(4, "safe fixture"), false);
  assert.equal(commands.some((command) => command.includes("send-keys") || command.includes("paste-buffer")), false);
});

test("pane delivery pins retries to the verified immutable pane id", async () => {
  const commands: string[] = [];
  const relay = new TmuxRelay(DEFAULT_CONFIG, {
    runShell: async (command) => {
      commands.push(command);
      if (command.startsWith("tmux display-message")) {
        return { stdout: "%42\t/Users/rajiv/Downloads/projects/heydonna-app-3004\n", stderr: "" };
      }
      if (command.startsWith("git -C")) {
        return { stdout: "/Users/rajiv/Downloads/projects/heydonna-app-3004\n", stderr: "" };
      }
      return { stdout: "", stderr: "" };
    },
  });

  assert.equal(await relay.sendToSlotAsync(4, "Escape", false, true), true);
  const mutations = commands.filter((command) => command.includes("send-keys") || command.includes("paste-buffer"));
  assert.ok(mutations.length > 0);
  assert.ok(mutations.every((command) => command.includes("-t %42")));
  assert.equal(mutations.some((command) => command.includes("0:0.4")), false);
});

test("destroyed verified pane fails without numeric-address fallback or retry retarget", async () => {
  const commands: string[] = [];
  const relay = new TmuxRelay(DEFAULT_CONFIG, {
    runShell: async (command) => {
      commands.push(command);
      if (command.startsWith("tmux display-message")) {
        return { stdout: "%42\t/Users/rajiv/Downloads/projects/heydonna-app-3004\n", stderr: "" };
      }
      if (command.startsWith("git -C")) {
        return { stdout: "/Users/rajiv/Downloads/projects/heydonna-app-3004\n", stderr: "" };
      }
      throw new Error("pane %42 destroyed");
    },
  });

  assert.equal(await relay.sendToSlotAsync(4, "Escape", false, true), false);
  const mutations = commands.filter((command) => command.includes("send-keys") || command.includes("paste-buffer"));
  assert.ok(mutations.length > 0);
  assert.ok(mutations.every((command) => command.includes("-t %42")));
  assert.equal(mutations.some((command) => command.includes("0:0.4")), false);
});

test("pane mutation surfaces use pane ids after verification", () => {
  const sources = ["../src/server.ts", "../src/health.ts", "../src/relay.ts"]
    .map((file) => readFileSync(new URL(file, import.meta.url), "utf8"));
  const combined = sources.join("\n");
  assert.doesNotMatch(combined, /const paneTarget = paneAddress\(slotNum\)/);
  assert.doesNotMatch(combined, /identity\.snapshot\.address/);
  assert.match(combined, /identity\.snapshot\.paneId/);
});
