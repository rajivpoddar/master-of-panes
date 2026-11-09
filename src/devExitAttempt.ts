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
