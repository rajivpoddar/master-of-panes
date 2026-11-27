/** Durable state for a numbered slot's one Stop-authorized exit attempt. */
export type DevExitAttempt = {
  episode: string;
  paneId: string;
  originalClaudePid: number;
  originalStart: string;
  sessionId: string;
  inputEventId: number | null;
  assignmentEpoch: number;
  stopEventId: number | null;
  phase: "dispatching" | "delivered" | "unknown" | "blocked" | "verified";
  attemptCount: number;
  originalProcess: string;
  ownerPin: unknown;
  replacementProcess?: string;
  reported?: boolean;
  submittedAt: string;
  blockedReason: string | null;
};

/**
 * Whole budget of the live replacement probe a PM Stop may pay (CTO REVISE 3, C0ALZJHGE49 thread 1795798240.220189,
 * ts 1795801872.756899). The installed hook relay gives the whole Stop HTTP request 3 s (scripts/hook-relay.sh,
 * urlopen timeout=3); half of it is left for body parsing, selection and the downstream Stop handling.
 */
export const PM_STOP_REPLACEMENT_PROBE_BUDGET_MS = 1_500;

/** Select only an unambiguous foreground Claude process-group leader. */
export function parseForegroundClaude(ps: string): { pid: number; start: string } | null {
  const leaders = ps.split(/\r?\n/).flatMap(line => {
    const row = /^\s*(\d+)\s+(\d+)\s+(\S+)\s+((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\w+\s+\d+\s+\d\d:\d\d:\d\d\s+\d{4})\s+(.+?)\s*$/.exec(line);
    if (!row || row[1] !== row[2] || !row[3].includes("+") || !/(?:^|\/)claude$/.test(row[5])) return [];
    return [{ pid: Number(row[1]), start: row[4] }];
  });
  return leaders.length === 1 ? leaders[0] : null;
}

/**
 * The ONE binding test for an `exit_replacement_bound` receipt. It binds the replacement to this exact attempt
 * (episode, submittedAt, assignment epoch), a positive launch event, the single boot after that launch (the current
 * SessionStart id and its session), an owned "<pane>:<child>" process, and `process`, the replacement identity
 * "[pane, leaderPid, leaderStart]" whose leader pid is that owned child. Anything else is unbound: a newer boot or
 * Stop alone never earns cycle credit (CTO REVISE C0ALZJHGE49 thread 1795798240.220189, ts 1795798363.328589).
 */
export function exitReplacementReceiptBinds(
  receipt: any,
  boot: { id: number; payload: string | null } | undefined,
  attempt: DevExitAttempt,
  process: string,
): boolean {
  try {
    const bootPayload = JSON.parse(boot?.payload ?? "null");
    const replacement = JSON.parse(process);
    return receipt?.episode === attempt.episode && receipt?.submittedAt === attempt.submittedAt
      && receipt?.replacementProcess === process && receipt?.assignmentEpoch === attempt.assignmentEpoch
      && Number.isInteger(receipt.launchEventId) && receipt.launchEventId > 0
      && receipt.bootEventId > receipt.launchEventId && !!receipt.bootSession
      && /^\d+:\d+$/.test(receipt.ownedProcess ?? "")
      && boot?.id === receipt.bootEventId && bootPayload?.session_id === receipt.bootSession
      && String(replacement[1]) === receipt.ownedProcess.split(":")[1];
  } catch {
    return false;
  }
}

type ExitOwnerDb = {
  getConfig(key: string): string | null | undefined;
  isExitSlotRetained(slot: number): boolean;
  getSlot(slot: number): { assignment_epoch?: number | null } | null | undefined;
  getEvents(slot: number, limit: number, type?: string): Array<{ id: number; payload: string | null }>;
};

/**
 * The relay's live-owner predicate, side-effect free: the attempt still owns its slot. Same assignment epoch, owner
 * pin present, not retained (except the delayed-receipt retention), same assignment for dev slots, and the Stop-time
 * input unless the PM consumed its exit on a saved handoff. Shared by observeExitAttempts and the PM Stop backstop
 * (CTO REVISE C0ALZJHGE49 thread 1795798240.220189, ts 1795800748.304029, item 2: "reuse relay.ts's current-process and
 * ownership checks before crediting the cycle").
 */
export function exitOwnerMatches(db: ExitOwnerDb, slot: number, attempt: DevExitAttempt,
  assignmentOf: (row: any) => string): { matches: boolean; delayedReceiptRetention: boolean } {
  const pin = attempt.ownerPin as { assignment?: string; savedPmHandoff?: boolean } | null;
  let delayedReceiptRetention = false;
  try {
    const retained = JSON.parse(db.getConfig(`exit_retained_${slot}`) || "null");
    delayedReceiptRetention = retained?.episode === attempt.episode && retained.reason === "replacement_not_owned_by_restart";
  } catch { /* Other retention remains binding. */ }
  const savedPmHandoff = slot === 0 && pin?.savedPmHandoff === true;
  const row = db.getSlot(slot);
  const matches = !!pin && (!db.isExitSlotRetained(slot) || delayedReceiptRetention)
    && attempt.assignmentEpoch === (row?.assignment_epoch ?? 0)
    && (slot === 0 || (!!row && assignmentOf(row) === pin.assignment))
    && (savedPmHandoff || attempt.inputEventId === (db.getEvents(slot, 1, "UserPromptSubmit")[0]?.id ?? null));
  return { matches, delayedReceiptRetention };
}

/**
 * A PM-only episode's continuation is settled (no send can still be in flight) only on authoritative delivery or an
 * existing terminal disposition. "waiting" will still be sent by the health resume path, and "attempted" is written
 * BEFORE the guarded send, so it counts only with this episode's delivered pm_restart_continuation receipt
 * (CTO REVISE C0ALZJHGE49 ts 1795800748.304029, item 1).
 */
export function pmContinuationSettled(db: Pick<ExitOwnerDb, "getEvents">, episode: string | null,
  delivery: string | null | undefined): boolean {
  if (delivery === "delivered" || delivery === "exit_stalled" || delivery === "exit_refused" || !delivery) return true;
  if (delivery !== "attempted" || !episode) return false;
  return db.getEvents(0, 20, "pm_restart_continuation").some((row) => {
    try {
      const payload = JSON.parse(row.payload ?? "null");
      return payload?.episode === episode && payload.delivery === "delivered";
    } catch { return false; }
  });
}
