import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { MoPDatabase } from "../src/db.js";
import { HookProcessor } from "../src/hooks.js";
import type { TmuxRelay } from "../src/relay.js";
import { DEFAULT_CONFIG } from "../src/types.js";

test("PM slot-0 completion is persisted as PostToolUse and duplicate delivery has no notification side effect", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-pm-posttooluse-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const notices: string[] = [];
  const processor = new HookProcessor(db, {
    injectToPM: (message: string) => { notices.push(message); return true; },
    notifySubagentComplete: () => { notices.push("subagent-complete"); },
  } as unknown as TmuxRelay);
  const event = {
    type: "PostToolUse" as const,
    session_id: "pm-parent-session-1",
    cwd: "/Users/rajiv/Downloads/projects/heydonna-app",
    tool_name: "Agent",
    tool_input: {
      subagent_type: "codex-arch-reviewer",
      prompt: "codex-arch-reviewer issue pmf-shortcut-ctrl-k baseline_sha 9e90b464c7e7e9f561aefeb4ec00accb8d35eff3; codex-app-arch-review validated marker=/tmp/codex-review-companion/arch-issue-pmf-shortcut-ctrl-k-1790799143.md timestamp=1790799143",
    },
  };
  try {
    await processor.process(0, event);
    await processor.process(0, event);
    const events = db.getEvents(0, 10, "PostToolUse");
    assert.equal(events.length, 2, "the append-only observation log records both deliveries");
    assert.equal(events[0]?.tool_name, "Agent");
    const persisted = JSON.parse(events[0]!.payload) as typeof event;
    assert.equal(persisted.type, "PostToolUse");
    assert.equal(persisted.session_id, event.session_id);
    assert.equal(persisted.cwd, event.cwd);
    assert.equal(persisted.tool_input.subagent_type, "codex-arch-reviewer");
    assert.equal(notices.length, 0, "no tool_output means no duplicate subagent completion injection");
    const auditMatches = events.filter((row) => /codex-app-arch-review/i.test(row.payload));
    assert.equal(auditMatches.length, 2);
    assert.equal(auditMatches.length > 0, true, "duplicate delivery leaves the gate's allow/deny result unchanged");

    await processor.process(5, {
      type: "PostToolUse",
      session_id: "slot-5-session",
      cwd: "/Users/rajiv/Downloads/projects/heydonna-app-3005",
      tool_name: "Read",
    });
    assert.equal(db.getEvents(5, 10, "PostToolUse").length, 1, "numbered slot-5 hook ingestion remains intact");
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
