import { resolve } from "node:path";

import type { ExecShellResult } from "./asyncCommand.js";
import { execShell } from "./asyncCommand.js";
import { runtimeIdentity } from "./slotConfig.js";

export type PaneIdentitySnapshot = {
  slot: number;
  address: string;
  /** Immutable tmux pane identity captured by the same probe as currentPath. */
  paneId: string;
  currentPath: string;
  expectedPath: string;
};

export type PaneIdentityResult =
  | { ok: true; snapshot: PaneIdentitySnapshot }
  | { ok: false; reason: "unknown_slot" | "pane_unavailable" | "checkout_mismatch"; detail: string };

export type PaneCommandRunner = (command: string, options?: { timeout?: number }) => Promise<ExecShellResult>;

export function paneAddress(slot: number): string {
  return `0:0.${slot}`;
}

function shellEscape(value: string): string {
  return `'${value.replace(/'/g, "'\\\\''")}'`;
}

export function expectedCheckoutPath(slot: number): string | null {
  if (slot === 0) return "/Users/rajiv/Downloads/projects/heydonna-app";
  return runtimeIdentity(slot)?.checkoutPath ?? null;
}

export function validatePaneIdentity(
  slot: number,
  currentPath: string,
  paneId: string,
): PaneIdentityResult {
  if (!/^%\d+$/.test(paneId)) {
    return { ok: false, reason: "pane_unavailable", detail: `pane ${paneAddress(slot)} returned no immutable pane id` };
  }
  const expectedPath = expectedCheckoutPath(slot);
  if (!expectedPath) {
    return { ok: false, reason: "unknown_slot", detail: `no runtime identity is configured for slot ${slot}` };
  }
  const trimmedCurrent = currentPath.trim();
  const normalizedCurrent = trimmedCurrent ? resolve(trimmedCurrent) : "";
  const normalizedExpected = resolve(expectedPath);
  if (!normalizedCurrent || normalizedCurrent !== normalizedExpected) {
    return {
      ok: false,
      reason: "checkout_mismatch",
      detail: `pane ${paneAddress(slot)} is ${normalizedCurrent || "empty"}; expected ${normalizedExpected}`,
    };
  }
  return {
    ok: true,
    snapshot: {
      slot,
      address: paneAddress(slot),
      paneId,
      currentPath: normalizedCurrent,
      expectedPath: normalizedExpected,
    },
  };
}

/**
 * Verify the pane's actual checkout before any pane-targeted mutation.
 * A mismatch is deliberately fail-closed: numeric tmux addresses are not
 * sufficient identity when a pane was inserted, reused, or manually launched.
 */
export async function verifyPaneIdentity(
  slot: number,
  runShell: PaneCommandRunner = execShell,
): Promise<PaneIdentityResult> {
  if (!expectedCheckoutPath(slot)) {
    return { ok: false, reason: "unknown_slot", detail: `no runtime identity is configured for slot ${slot}` };
  }
  const address = paneAddress(slot);
  try {
    const result = await runShell(
      // A literal tab is rendered as an underscore by tmux under the
      // launchd environment used by the service.  Use a stable delimiter
      // so the immutable pane id/path probe is environment-independent.
      `tmux display-message -t ${address} -p '#{pane_id}|#{pane_current_path}'`,
      { timeout: 3_000 },
    );
    const identityFields = result.stdout.trimEnd().split(/\r?\n/, 1)[0]?.split("|", 2) ?? [];
    const paneId = identityFields[0]?.trim() ?? "";
    const panePath = identityFields[1]?.trim() ?? "";
    if (!panePath || !/^%\d+$/.test(paneId)) {
      return { ok: false, reason: "pane_unavailable", detail: `pane ${address} returned an invalid immutable identity` };
    }
    const checkout = await runShell(
      `git -C ${shellEscape(panePath)} rev-parse --show-toplevel`,
      { timeout: 3_000 },
    );
    return validatePaneIdentity(slot, checkout.stdout, paneId);
  } catch (error) {
    return {
      ok: false,
      reason: "pane_unavailable",
      detail: `pane ${address} identity probe failed: ${error instanceof Error ? error.message : String(error)}`,
    };
  }
}

export function validatePaneLayout(
  paneAddresses: readonly string[],
  slotCount = 6,
): { ok: true } | { ok: false; reason: string } {
  const expected = Array.from({ length: slotCount + 1 }, (_, index) => `0:0.${index}`);
  const actual = [...paneAddresses];
  if (actual.length !== expected.length || new Set(actual).size !== actual.length) {
    return { ok: false, reason: `expected ${expected.length} unique panes, got ${actual.length}` };
  }
  const sortedExpected = [...expected].sort();
  const sortedActual = actual.sort();
  if (sortedActual.some((address, index) => address !== sortedExpected[index])) {
    return { ok: false, reason: `pane addresses must be exactly ${expected.join(", ")}` };
  }
  return { ok: true };
}
