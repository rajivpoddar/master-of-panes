// CTO REVISE a4ebc0f4 (C0ALZJHGE49 ts 1794276606.286049): checkout sync yields
// after the Stop owner is captured, so an older Stop can finish after a newer
// one has already created the pending dispatch. Settlement must keep S2.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

const realGit = execFileSync("which", ["git"], { encoding: "utf8" }).trim();

function waitForFile(path: string): Promise<void> {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const timer = setInterval(() => {
      if (existsSync(path)) {
        clearInterval(timer);
        resolve();
      } else if (Date.now() - started > 2_000) {
        clearInterval(timer);
        reject(new Error(`timed out waiting for ${path}`));
      }
    }, 10);
  });
}

test("an older Stop that finishes after checkout sync does not replace the newer pending Stop", async () => {
  const root = mkdtempSync(join(tmpdir(), "mop-newest-stop-"));
  const holdDir = join(root, "older");
  const fastDir = join(root, "newer");
  const heldFile = join(root, "held");
  const releaseFile = join(root, "release");
  const gitBin = join(root, "bin");
  const git = join(gitBin, "git");
  mkdirSync(holdDir, { recursive: true });
  mkdirSync(fastDir, { recursive: true });
  mkdirSync(gitBin, { recursive: true });
  writeFileSync(git, `#!/bin/bash
orig=("$@")
cwd=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-C" ]]; then cwd="$2"; shift 2; continue; fi
  break
done
cmd="\${1:-}"
if [[ -n "\${HOLD_CWD:-}" && "$cwd" == "$HOLD_CWD" && ! -f "\${RELEASE_FILE:-/nonexistent}" ]]; then
  : > "$HELD_FILE"
  while [[ ! -f "$RELEASE_FILE" ]]; do sleep 0.02; done
fi
case "$cmd" in
  symbolic-ref) printf 'main\\n' ;;
  rev-parse) printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n' ;;
  merge-base) exit 0 ;;
  *) exec "$REAL_GIT" "\${orig[@]}" ;;
esac
`);
  chmodSync(git, 0o755);
  const previousPath = process.env.PATH;
  const previousHold = process.env.HOLD_CWD;
  const previousHeld = process.env.HELD_FILE;
  const previousRelease = process.env.RELEASE_FILE;
  const previousReal = process.env.REAL_GIT;
  process.env.PATH = `${gitBin}:${previousPath ?? ""}`;
  process.env.HOLD_CWD = holdDir;
  process.env.HELD_FILE = heldFile;
  process.env.RELEASE_FILE = releaseFile;
  process.env.REAL_GIT = realGit;

  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: ":memory:" });
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: async () => ({ stdout: "", stderr: "" }) });
  relay.setDatabase(db);
  const seen: number[] = [];
  relay.sendToSlotAsync = async (_slot, _command, _force, _raw, _episode, owner) => {
    seen.push(owner?.stop ?? -1);
    return true;
  };
  relay.injectToPM = () => true;
  let releaseSettle = (): void => {};
  const settleHeld = new Promise<void>((resolve) => { releaseSettle = resolve; });
  let markEntered = (): void => {};
  const settleEntered = new Promise<void>((resolve) => { markEntered = resolve; });
  (relay as unknown as { exitTurnContinued: () => Promise<null> }).exitTurnContinued = async () => {
    markEntered();
    await settleHeld;
    return null;
  };
  const processor = new HookProcessor(db, relay);
  const releaseOlder = () => { if (!existsSync(releaseFile)) writeFileSync(releaseFile, "go\n"); };
  try {
    db.updateSlot(5, { occupied: true, branch: "main", dnd: false, active_turn_id: null, active_turn_state: "inactive" });
    db.logEvent(5, "SessionStart", "SessionStart", null, { session_id: "sess-5" });
    db.setExitPending(true);
    const older = processor.process(5, { type: "Stop", session_id: "sess-5", cwd: holdDir });
    await waitForFile(heldFile);
    const olderStop = db.getEvents(5, 1, "Stop")[0]?.id;
    const newer = processor.process(5, { type: "Stop", session_id: "sess-5", cwd: fastDir });
    await newer;
    const newerStop = db.getEvents(5, 1, "Stop")[0]?.id;
    await settleEntered;
    releaseOlder();
    await older;
    releaseSettle();
    const deadline = Date.now() + 1_000;
    while (seen.length === 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    assert.ok(typeof olderStop === "number" && typeof newerStop === "number");
    assert.ok(newerStop > olderStop);
    assert.deepEqual(seen, [newerStop]);
  } finally {
    releaseOlder();
    releaseSettle();
    process.env.PATH = previousPath;
    if (previousHold === undefined) delete process.env.HOLD_CWD; else process.env.HOLD_CWD = previousHold;
    if (previousHeld === undefined) delete process.env.HELD_FILE; else process.env.HELD_FILE = previousHeld;
    if (previousRelease === undefined) delete process.env.RELEASE_FILE; else process.env.RELEASE_FILE = previousRelease;
    if (previousReal === undefined) delete process.env.REAL_GIT; else process.env.REAL_GIT = previousReal;
    db.close();
  }
});
