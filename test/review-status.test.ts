import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { MoPDatabase } from "../src/db.js";
import type { MoPConfig } from "../src/types.js";

function withDatabase(run: (db: MoPDatabase) => void): void {
  const directory = mkdtempSync(join(tmpdir(), "mop-review-status-"));
  const config: MoPConfig = {
    httpPort: 0,
    mcpTransport: "stdio",
    dbPath: join(directory, "mop.db"),
    slotCount: 4,
    pmPaneAddress: "0:0.0",
    legacyRepositoryId: null,
  };
  const db = new MoPDatabase(config);
  try {
    run(db);
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
}

test("review status accepts an exact-issue codex reviewer Agent event", () => {
  withDatabase((db) => {
    db.logEvent(3, "PostToolUse", "PostToolUse", "Agent", {
      type: "PostToolUse",
      tool_name: "Agent",
      tool_input: {
        subagent_type: "codex-code-reviewer",
        description: "Code review for issue #7450",
        prompt: "Review issue #7450 and return a functionality-first verdict.",
      },
    });

    const result = db.findReviewEvent(7450, 240);
    assert.equal(result.found, true);
    assert.equal(result.method, "codex-agent");
    assert.equal(typeof result.timestamp, "string");
    assert.equal(result.slot, 3);
    assert.equal(result.details, "tool=Agent, matched in events DB");
  });
});

test("review status accepts the equivalent Task event shape", () => {
  withDatabase((db) => {
    db.logEvent(2, "PostToolUse", "PostToolUse", "Task", {
      type: "PostToolUse",
      tool_name: "Task",
      tool_input: {
        subagent_type: "codex-code-reviewer",
        description: "Code review for issue 7450",
        prompt: "Review #7450.",
      },
    });

    const result = db.findReviewEvent(7450, 240);
    assert.equal(result.found, true);
    assert.equal(result.method, "codex-agent");
    assert.equal(result.slot, 2);
  });
});

test("review status rejects unrelated issues and arbitrary Agent or Task prose", () => {
  withDatabase((db) => {
    db.logEvent(1, "PostToolUse", "PostToolUse", "Agent", {
      tool_input: {
        subagent_type: "codex-code-reviewer",
        description: "Code review for issue #17450",
        prompt: "Review issue #17450.",
      },
    });
    db.logEvent(2, "PostToolUse", "PostToolUse", "Task", {
      tool_input: {
        subagent_type: "general-purpose",
        description: "Investigate issue #7450",
        prompt: "Mention codex review for #7450 in arbitrary prose.",
      },
    });
    db.logEvent(3, "PostToolUse", "PostToolUse", "Agent", {
      tool_input: {
        subagent_type: "codex-code-reviewer-extra",
        description: "Code review for issue #7450",
        prompt: "Review issue #7450.",
      },
    });

    assert.equal(db.findReviewEvent(7450, 240).found, false);
  });
});

test("review status preserves existing Skill and Bash review paths", () => {
  withDatabase((db) => {
    db.logEvent(1, "PostToolUse", "PostToolUse", "Skill", {
      skill: "codex-app-code-review",
      issue: 7450,
    });
    const skillResult = db.findReviewEvent(7450, 240);
    assert.equal(skillResult.found, true);
    assert.equal(skillResult.method, "codex-app");

    db.logEvent(2, "PostToolUse", "PostToolUse", "Bash", {
      command: "codex exec review issue 7451",
    });
    const bashResult = db.findReviewEvent(7451, 240);
    assert.equal(bashResult.found, true);
    assert.equal(bashResult.method, "codex-cli");
  });
});
