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
