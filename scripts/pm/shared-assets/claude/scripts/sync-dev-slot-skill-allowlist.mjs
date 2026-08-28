#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

const allowlist = new Set([
  "message-pm",
  "pm-wait-nudge",
  "respawn",
  "slot-boot",
  "codex-app-plan-review",
  "codex-plan-review",
  "codex-app-code-review",
  "codex-code-review",
  "codex-review-cap-marker-handling",
  "producer-consumer-contract-verification",
  "codex-app-qa-review",
  "codex-qa-review",
  "qa-brief",
  "proofshot",
  "heydonna-agent-browser",
  "playwright-testmatch-override-qa-only",
  "agent-browser",
  "agent-browser-auth-redirect-selector",
  "agent-browser-blob-download-capture",
  "agent-browser-console-error-tracker",
  "agent-browser-daemon-stale-eagain-recovery",
  "agent-browser-eval-iife-return",
  "agent-browser-hidden-file-input",
  "agent-browser-indexeddb-eval",
  "agent-browser-inline-popover-detection",
  "agent-browser-login",
  "agent-browser-multi-slot-isolation",
  "agent-browser-persistent-session",
  "agent-browser-project-creation",
  "agent-browser-proofreading",
  "agent-browser-prosemirror-selection",
  "agent-browser-prosemirror-typing",
  "agent-browser-radix-context-menu-trigger",
  "agent-browser-radix-select",
  "agent-browser-react-form-validation",
  "agent-browser-recharts-tooltip-hover",
  "agent-browser-ref-click-eagain",
  "agent-browser-spa-link-navigation",
  "agent-browser-window-open-headless",
]);

const defaultSkillOverride = "off";
const devPluginOverrides = {
  "codex@openai-codex": false,
  "context-mode@context-mode": false,
  "explanatory-output-style@claude-plugins-official": false,
  "master-of-panes@rajiv-plugins": true,
};

const defaultSlotRoots = [1, 2, 3, 4, 5, 6].map(
  (slot) => `/Users/rajiv/Downloads/projects/heydonna-app-300${slot}`,
);
const slotRoots = process.env.HEYDONNA_DEV_SLOT_ROOTS
  ? process.env.HEYDONNA_DEV_SLOT_ROOTS.split(path.delimiter).filter(Boolean)
  : defaultSlotRoots;
const userSkillsRoot =
  process.env.CLAUDE_USER_SKILLS_DIR ?? "/Users/rajiv/.claude/skills";

function skillName(skillFile) {
  const body = fs.readFileSync(skillFile, "utf8");
  const frontmatter = body.match(/^---\s*\n([\s\S]*?)\n---(?:\s*\n|$)/);
  const name = frontmatter?.[1].match(/^name:\s*["']?([^"'\n]+?)["']?\s*$/m)?.[1];
  return name?.trim() || path.basename(path.dirname(skillFile));
}

function discoverSkills(root, names, visited = new Set()) {
  if (!fs.existsSync(root)) return;
  const realRoot = fs.realpathSync(root);
  if (visited.has(realRoot)) return;
  visited.add(realRoot);

  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const fullPath = path.join(root, entry.name);
    if (entry.isDirectory() || entry.isSymbolicLink()) {
      try {
        if (fs.statSync(fullPath).isDirectory()) discoverSkills(fullPath, names, visited);
      } catch {
        // Ignore broken skill symlinks; Claude cannot discover them either.
      }
    } else if (entry.isFile() && entry.name === "SKILL.md") {
      names.add(skillName(fullPath));
    }
  }
}

function desiredOverrides() {
  const names = new Set(allowlist);
  discoverSkills(userSkillsRoot, names);
  for (const slotRoot of slotRoots) {
    discoverSkills(path.join(slotRoot, ".claude", "skills"), names);
  }

  return Object.fromEntries(
    [...names]
      .sort((a, b) => a.localeCompare(b))
      .map((name) => [name, allowlist.has(name) ? "on" : defaultSkillOverride]),
  );
}

function updateSettings(slotRoot, overrides, checkOnly) {
  const settingsPath = path.join(slotRoot, ".claude", "settings.local.json");
  const current = fs.existsSync(settingsPath)
    ? JSON.parse(fs.readFileSync(settingsPath, "utf8"))
    : {};
  const next = {
    ...current,
    enabledPlugins: {
      ...(current.enabledPlugins ?? {}),
      ...devPluginOverrides,
    },
    skillOverrides: overrides,
  };
  const serialized = `${JSON.stringify(next, null, 2)}\n`;
  const existing = fs.existsSync(settingsPath)
    ? fs.readFileSync(settingsPath, "utf8")
    : "";
  const changed = serialized !== existing;

  if (changed && !checkOnly) {
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
    const temporaryPath = `${settingsPath}.tmp-${process.pid}`;
    fs.writeFileSync(temporaryPath, serialized, { mode: 0o600 });
    fs.renameSync(temporaryPath, settingsPath);
  }

  process.stdout.write(
    `${changed ? (checkOnly ? "DRIFT" : "UPDATED") : "OK"} ${settingsPath}\n`,
  );
  return changed;
}

const checkOnly = process.argv.includes("--check");
const overrides = desiredOverrides();
let drifted = false;
for (const slotRoot of slotRoots) {
  drifted = updateSettings(slotRoot, overrides, checkOnly) || drifted;
}

const fullyDescribed = Object.entries(overrides)
  .filter(([, value]) => value === "on")
  .map(([name]) => name);
const nameOnly = Object.entries(overrides)
  .filter(([, value]) => value === "name-only")
  .map(([name]) => name);
process.stdout.write(
  `DEV_SKILL_ALLOWLIST_OK fully_described=${fullyDescribed.length} name_only=${nameOnly.length}\n`,
);
if (checkOnly && drifted) process.exitCode = 1;
