/** Canonical numbered-slot configuration shared by every MoP control surface. */
export const PM_SLOT = 0;
export const DEFAULT_DEV_SLOT_COUNT = 6;
export const DEV_SLOT_NUMBERS: readonly number[] = Object.freeze(
  Array.from({ length: DEFAULT_DEV_SLOT_COUNT }, (_, index) => index + 1),
);
export const RUNTIME_SLOT_NUMBERS: readonly number[] = Object.freeze([
  PM_SLOT,
  ...DEV_SLOT_NUMBERS,
]);

export function isValidDevSlot(slot: number, slotCount = DEFAULT_DEV_SLOT_COUNT): boolean {
  return Number.isInteger(slot) && slot >= 1 && slot <= slotCount;
}

export function isValidRuntimeSlot(slot: number, slotCount = DEFAULT_DEV_SLOT_COUNT): boolean {
  return slot === PM_SLOT || isValidDevSlot(slot, slotCount);
}

export function devSlots(slotCount = DEFAULT_DEV_SLOT_COUNT): number[] {
  if (!Number.isInteger(slotCount) || slotCount < 1 || slotCount > DEFAULT_DEV_SLOT_COUNT) {
    throw new Error(`invalid configured slot count: ${slotCount}`);
  }
  return Array.from({ length: slotCount }, (_, index) => index + 1);
}

export type SlotRuntimeIdentity = {
  slot: number;
  checkoutPath: string;
  jsonlPath: string;
  launchScript: string;
  /** Read-only verified legacy bindings are preserved; only S5/S6 are provisioned. */
  legacyConvexDeployment?: string;
  legacyConvexProject?: string;
  convexDeployment?: string;
  convexProject?: string;
  provisioning: "preserve-live" | "create-isolated";
  appPort: number;
  browserSession: string;
  browserProfile: string;
  modalSuffix: string;
  envPath: string;
};

/** Explicit per-slot identities; values are configuration, not a state store. */
export const SLOT_RUNTIME_IDENTITIES: Readonly<Record<number, SlotRuntimeIdentity>> =
  Object.freeze(Object.fromEntries(
    DEV_SLOT_NUMBERS.map((slot) => [slot, {
      slot,
      checkoutPath: `/Users/rajiv/Downloads/projects/heydonna-app-300${slot}`,
      jsonlPath: `/Users/rajiv/.claude/projects/-Users-rajiv-Downloads-projects-heydonna-app-300${slot}`,
      launchScript: `/Users/rajiv/.claude/scripts/launch-slot-${slot}.sh`,
      ...(slot === 1 ? { legacyConvexDeployment: "dev:uncommon-buffalo-66", legacyConvexProject: "heydonna-slot-1", provisioning: "preserve-live" as const } :
        slot === 2 ? { legacyConvexDeployment: "dev:optimistic-camel-445", legacyConvexProject: "heydonna", provisioning: "preserve-live" as const } :
        slot === 3 ? { legacyConvexDeployment: "dev:handsome-finch-141", legacyConvexProject: "heydonna-slot-3", provisioning: "preserve-live" as const } :
        slot === 4 ? { legacyConvexDeployment: "dev:knowing-orca-670", legacyConvexProject: "heydonna-slot-4", provisioning: "preserve-live" as const } :
        { convexDeployment: `heydonna-slot-${slot}`, convexProject: `heydonna-slot-${slot}`, provisioning: "create-isolated" as const }),
      appPort: 3000 + slot,
      browserSession: `slot${slot}`,
      browserProfile: `/Users/rajiv/.agent-browser/profiles/admin-slot${slot}`,
      modalSuffix: `-slot${slot}`,
      envPath: `/Users/rajiv/Downloads/projects/heydonna-app-300${slot}/.env.local`,
    } satisfies SlotRuntimeIdentity])) as Record<number, SlotRuntimeIdentity>);

export function runtimeIdentity(slot: number): SlotRuntimeIdentity | null {
  return SLOT_RUNTIME_IDENTITIES[slot] ?? null;
}
