const pendingBySlot = new Map<number, Promise<void>>();

/** Serialize a complete pane delivery so another send cannot splice into it. */
export async function withSlotSendLock<T>(slot: number, operation: () => Promise<T>): Promise<T> {
  const previous = pendingBySlot.get(slot) ?? Promise.resolve();
  let release!: () => void;
  const hold = new Promise<void>((resolve) => {
    release = resolve;
  });
  const current = previous.catch(() => undefined).then(() => hold);
  pendingBySlot.set(slot, current);

  await previous.catch(() => undefined);
  try {
    return await operation();
  } finally {
    release();
    if (pendingBySlot.get(slot) === current) pendingBySlot.delete(slot);
  }
}
