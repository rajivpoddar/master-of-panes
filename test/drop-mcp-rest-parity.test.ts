import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const REPO_ROOT = new URL("..", import.meta.url).pathname;
const serverSrc = readFileSync(`${REPO_ROOT}src/server.ts`, "utf8");
const mcpSrc = readFileSync(`${REPO_ROOT}src/mcp.ts`, "utf8");

test("REST parity routes exist for every PM-used MCP tool", () => {
  assert.match(serverSrc, /app\.post\("\/slots\/:slotNum\/dnd"/);
  assert.match(serverSrc, /app\.post\("\/exit-pending"/);
  assert.match(serverSrc, /app\.get\("\/exit-status"/);
  assert.match(serverSrc, /app\.get\("\/slots\/:slotNum\/capture"/);
});

test("server /send enforces DND, review-active, and PM raw refusals", () => {
  assert.match(serverSrc, /dnd_no_force/);
  assert.match(serverSrc, /slot_active_review_blocked/);
  assert.match(serverSrc, /pm_raw_send_blocked/);
  assert.match(serverSrc, /pm_control_command_blocked/);
});

test("MCP process never touches the DB or tmux directly", () => {
  const code = mcpSrc
    .split("\n")
    .filter((line) => !line.trim().startsWith("//") && !line.trim().startsWith("*"))
    .join("\n");
  assert.equal(code.includes("new MoPDatabase"), false, "no direct DB handle");
  assert.equal(code.includes("new TmuxRelay"), false, "no direct relay handle");
  assert.doesNotMatch(code, /\bdb\./, "no db.* calls");
  assert.doesNotMatch(code, /\brelay\./, "no relay.* calls");
  assert.equal(code.includes("execShell"), false, "no direct shell for screenshots");
});

test("MCP release shape is unchanged (thin slot + optional reason)", () => {
  assert.match(mcpSrc, /mopReleaseSlotInputShape/);
  assert.match(mcpSrc, /reason: z\.string\(\)\.optional\(\)/);
});

test("PM skills name the REST CLI, not the MCP tools", () => {
  const tools = [
    "mop_send_to_slot", "mop_release_slot", "mop_clear_slot", "mop_approve_plan",
    "mop_slot_status", "mop_all_slots", "mop_slot_history", "mop_recent_activity",
    "mop_capture_output", "mop_stream_slot", "mop_set_dnd", "mop_set_exit_pending",
    "mop_exit_status", "mop_clear_all_slots", "mop_respawn_slot", "mop_ops_audit",
    "mop_pm_cadence", "mcp__plugin_master-of-panes",
  ];
  const files = [
    "scripts/pm/shared-assets/claude/skills/direct-assign/SKILL.md",
    "scripts/pm/shared-assets/claude/skills/direct-release/SKILL.md",
    "scripts/pm/shared-assets/claude/skills/cleanup-pr/SKILL.md",
    "scripts/pm/shared-assets/claude/skills/session-age-clear/SKILL.md",
    "scripts/pm/shared-assets/claude/skills/heartbeat-tasks/SKILL.md",
    "scripts/pm/shared-assets/claude/skills/todo-prioritize/SKILL.md",
  ];
  for (const file of files) {
    const text = readFileSync(`${REPO_ROOT}${file}`, "utf8");
    for (const tool of tools) {
      assert.equal(text.includes(tool), false, `${file} must not reference ${tool}`);
    }
  }
});

test("clear guard matches the CLI send path", () => {
  const hook = readFileSync(
    `${REPO_ROOT}scripts/pm/shared-assets/claude/hooks/block-raw-clear-outside-mop.sh`,
    "utf8",
  );
  assert.match(hook, /mcp__plugin_master-of-panes_mop__mop_send_to_slot/);
  assert.match(hook, /mop.*send/);
});
