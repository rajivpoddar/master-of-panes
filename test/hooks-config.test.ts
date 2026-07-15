import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("Stop hooks relay PM and dev-pane terminal events to MoP", () => {
  const config = JSON.parse(
    readFileSync(new URL("../hooks/hooks.json", import.meta.url), "utf8")
  ) as {
    hooks: { Stop: Array<{ hooks: Array<{ command: string }> }> };
  };

  const commands = config.hooks.Stop.flatMap((entry) =>
    entry.hooks.map((hook) => hook.command)
  );

  assert.ok(
    commands.includes('bash "${CLAUDE_PLUGIN_ROOT}/scripts/hook-relay.sh" Stop'),
    "Stop must use the existing cwd-aware relay so the PM pane is recorded as slot 0"
  );
});
