#!/usr/bin/env node
// codex-review-companion.mjs
// Custom Codex review invocation for HeyDonna. Replaces upstream
// codex-companion.mjs adversarial-review for our app-* review skills.
//
// Why this exists: the upstream companion's branch-scope diff failed on PR #4165
// + #4168 because preview-seed.zip blew the 1MB diff cap. This companion:
//   - reads our own extracted prompt templates (~/.claude/skills/codex-app-{type}-review/templates/prompt.txt)
//   - computes diff with explicit binary-fixture exclusions
//   - drives `codex app-server` over JSON-RPC stdio (v1.1)
//   - parses verdict, writes marker file, exits with structured code
//
// v1.1 (2026-05-08): switched from `codex exec` (stdin-piped, intermittent
// upstream block) to `codex app-server` (app-server protocol). Mirrors the
// upstream `~/.claude/plugins/marketplaces/openai-codex/plugins/codex/scripts/
// codex-companion.mjs` wire format:
//   1. spawn `codex app-server` with line-delimited JSON-RPC over stdio
//   2. request("initialize", {clientInfo, capabilities}) + notify("initialized")
//   3. request("thread/start", {cwd, model, approvalPolicy:"never",
//      sandbox:"read-only", serviceName, ephemeral:true, experimentalRawEvents:false})
//   4. request("turn/start", {threadId, input:[{type:"text",text,text_elements:[]}],
//      model, effort, outputSchema:null})
//   5. listen for notifications until turn/completed (or final_answer agentMessage
//      with subagent drain → inferred completion)
//   6. capture final agentMessage text → resolve as finalText
//   7. proc.stdin.end() + close
// Rajiv directive 2026-05-08 13:19 IST: *"note that codex exec sometime
// randomly blocks. codex app is fine."* (thread 1778225422.613069).
//
// Hook exemption: ~/.claude/hooks/block-codex-exec.sh exempts this script.
// After v1.1 the companion no longer calls raw `codex exec` — exemption stays
// as defensive backstop.
//
// Usage:
//   # Pre-PR plan-review — branch on origin, no PR yet (use --branch, NOT --pr):
//   node scripts/codex-review-companion.mjs --review-type plan --branch fix/4170-foo --plan-file docs/plans/issue-4170-foo.md
//   # Post-PR reviews (PR exists, use --pr):
//   node scripts/codex-review-companion.mjs --review-type code --pr 4170
//   node scripts/codex-review-companion.mjs --review-type plan --pr 4170 --plan-file docs/plans/issue-4170-foo.md
//   node scripts/codex-review-companion.mjs --review-type qa --pr 4170 --qa-report /tmp/qa-report-4170.md
//   # Pre-implementation architecture review — issue contract, no fake diff source:
//   node scripts/codex-review-companion.mjs --review-type arch --issue 4170 --focus-text "root cause: ..."
//
// Anti-pattern: passing the ISSUE number as --pr (issue !== PR) → companion errors
// with "Could not resolve to a PullRequest" → DO NOT fall back to raw `codex exec`
// (blocked by hook) → DO NOT self-review (CP #1 violation). Use --branch for pre-PR.
//
// Exit codes: 0=APPROVE/CONFIRMED/VERIFIED, 1=REQUEST_CHANGES/REVISE/NEEDS_REVISION/NEEDS_DEEPER_INVESTIGATION,
//             2=REJECT/MISDIAGNOSED, 3=error, 42=terminal review control decision

import { spawn, spawnSync } from "node:child_process";
import crypto from "node:crypto";
import readline from "node:readline";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";

const HOME = os.homedir();
const REVIEW_TYPES = new Set(["arch", "plan", "code", "qa"]);
export const DEFAULT_MODEL = "gpt-5.6-luna";
export const DEFAULT_REVIEW_EFFORT = "high";
const DIFF_TRUNCATE_AT = 800_000; // chars
const CODEX_BIN =
  process.env.CODEX_BIN || "/Users/rajiv/.nvm/versions/node/v22.13.1/bin/codex";
const CODEX_TIMEOUT_MS = parseInt(
  process.env.CODEX_TIMEOUT_MS || `${9 * 60 * 1000}`,
  10,
); // 9min internal deadline; callers must allow the cleanup grace below.
const CALLER_TIMEOUT_GRACE_MS = 60 * 1000;
const HEARTBEAT_INTERVAL_MS = 30 * 1000;
const CODEX_SERVICE_TIER =
  process.env.CODEX_SERVICE_TIER === "inherit"
    ? null
    : process.env.CODEX_SERVICE_TIER || null;

// ---------- arg parsing ----------

function parseArgs(argv) {
  const out = {
    reviewType: null,
    pr: null,
    branch: null,
    scope: [],
    exclude: [],
    issue: null,
    reworkItems: null,
    previousHead: null,
    rebaseBaseline: null,
    reviewOrdinal: null,
    planFile: null,
    qaReport: null,
    qaBriefFile: null,
    focusText: null,
    model: DEFAULT_MODEL,
    effort: null,
    markerFile: null,
    outputFormat: "text",
    repoRoot: process.cwd(),
    verbose: false,
  };

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    switch (a) {
      case "--review-type":
        out.reviewType = next();
        break;
      case "--pr":
        out.pr = next();
        break;
      case "--branch":
        out.branch = next();
        break;
      case "--scope":
        // collect remaining non-flag args as scope paths
        while (i + 1 < argv.length && !argv[i + 1].startsWith("--")) {
          out.scope.push(argv[++i]);
        }
        break;
      case "--exclude":
        out.exclude.push(next());
        break;
      case "--issue":
        out.issue = next();
        break;
      case "--rework-items":
        out.reworkItems = next();
        break;
      case "--previous-head":
        out.previousHead = next();
        break;
      case "--rebase-baseline":
        out.rebaseBaseline = next();
        break;
      case "--review-ordinal":
        out.reviewOrdinal = next();
        break;
      case "--plan-file":
        out.planFile = next();
        break;
      case "--qa-report":
        out.qaReport = next();
        break;
      case "--qa-brief":
        out.qaBriefFile = next();
        break;
      case "--focus-text":
        out.focusText = next();
        break;
      case "--model":
        out.model = next();
        break;
      case "--effort":
        out.effort = next();
        break;
      case "--marker-file":
        out.markerFile = next();
        break;
      case "--output-format":
        out.outputFormat = next();
        break;
      case "--repo-root":
        out.repoRoot = next();
        break;
      case "--verbose":
      case "-v":
        out.verbose = true;
        break;
      case "--help":
      case "-h":
        printUsage();
        process.exit(0);
      default:
        // ignore unknowns
        break;
    }
  }
  return out;
}

function printUsage() {
  console.error(`Usage: codex-review-companion.mjs [options]

Required:
  --review-type <arch|plan|code|qa>   review type, picks prompt template
  --pr <N> | --branch <name>          required except for issue-only arch review

Optional:
  --scope <paths...>                  restrict diff to specific paths
  --exclude <glob>                    exclude paths (repeatable)
  --issue <N>                         override auto-resolved issue number
  --rework-items <text>               optional plan/code re-review scope
  --previous-head <sha>               required baseline for explicit re-review
  --rebase-baseline <sha>             explicit baseline for an intentional rebase lineage
  --review-ordinal <N>                explicit review ordinal for a rebase lineage
  --plan-file <path>                  for plan review (default: docs/plans/issue-<N>-*.md)
  --qa-report <path>                  for qa review (default: /tmp/qa-report-<ISSUE>.md)
  --qa-brief <path>                   for qa review (extracted PR comment)
  --focus-text <text>                 for arch review (root cause / proposal)
  --model <name>                      default: ${DEFAULT_MODEL}
  --effort <level>                    default: ${DEFAULT_REVIEW_EFFORT}
  --marker-file <path>                default: /tmp/codex-app-<type>-review-<N>.txt
  --output-format <text|json>         default: text
  --repo-root <path>                  default: cwd
  --verbose, -v                       print extra logs

Exit codes: 0 APPROVE, 1 REQUEST_CHANGES, 2 REJECT, 3 error,
            42 review-cap/same-head/history terminal (do not retry)`);
}

// ---------- shell helpers ----------

function sh(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, {
    encoding: "utf8",
    maxBuffer: 50 * 1024 * 1024,
    cwd: opts.cwd || process.cwd(),
    env: { ...process.env, ...(opts.env || {}) },
    input: opts.input,
  });
  return {
    code: r.status,
    stdout: (r.stdout || "").toString(),
    stderr: (r.stderr || "").toString(),
  };
}

function reviewRescueCommand(args) {
  return [
    "PM_RESCUE_REQUIRED:",
    "freeze the exact source; release/park the owning slot so it is refillable;",
    "PM must run Skill(pm-pr-rescue) off-slot via claude -p",
    `issue=${args.issue}`,
    `pr=${args.pr || "none"}`,
    `reason=${args.reviewType}-review-cap`,
    "CTO escalation requires a MoP-validated Fable FAILED packet",
  ].join(" ");
}

function reviewBudgetCommandArgs(args, { publish = false } = {}) {
  const lifecycleId = args.pr || args.issue;
  const commandArgs = [
    "review-budget",
    "--pr",
    String(lifecycleId),
    "--issue",
    String(args.issue),
    "--json",
  ];
  if (args._currentHead) {
    commandArgs.push("--head", String(args._currentHead));
  }
  if (args.pr) commandArgs.push("--live-pr");
  if (publish && args.pr) commandArgs.push("--publish");
  return commandArgs;
}

const PLAN_REVIEW_CAP = 3;
const PLAN_HISTORY_DEFAULT_DIR = path.join(
  HOME,
  ".claude",
  "state",
  "codex-review-companion",
);

function planHistoryRoot() {
  return process.env.CODEX_REVIEW_HISTORY_DIR || PLAN_HISTORY_DEFAULT_DIR;
}

function stableRepositoryIdentity(args, runner = sh) {
  const root = runner("git", ["rev-parse", "--show-toplevel"], {
    cwd: args.repoRoot,
  });
  if (root.code !== 0 || !root.stdout.trim()) {
    throw new Error("PLAN_REVIEW_HISTORY_REPOSITORY_UNRESOLVED");
  }
  const remote = runner("git", ["config", "--get", "remote.origin.url"], {
    cwd: args.repoRoot,
  });
  const identity = remote.code === 0 && remote.stdout.trim()
    ? remote.stdout.trim()
    : path.resolve(root.stdout.trim());
  return identity.replace(/\.git$/, "");
}

function planHistoryKey(args, runner = sh) {
  const identity = stableRepositoryIdentity(args, runner);
  return crypto
    .createHash("sha256")
    .update(`${identity}\0${args.issue}\0plan`)
    .digest("hex");
}

function fsyncDirectory(directory) {
  const descriptor = fs.openSync(directory, "r");
  try {
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function atomicWrite(filePath, content) {
  const directory = path.dirname(filePath);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const temporary = path.join(
    directory,
    `.${path.basename(filePath)}.${process.pid}.${crypto.randomBytes(6).toString("hex")}.tmp`,
  );
  const descriptor = fs.openSync(temporary, "wx", 0o600);
  try {
    fs.writeFileSync(descriptor, content, "utf8");
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  fs.renameSync(temporary, filePath);
  fsyncDirectory(directory);
}

function withPlanHistoryLock(callback) {
  const root = planHistoryRoot();
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  const lockPath = path.join(root, ".lock");
  let descriptor;
  let acquired = false;
  try {
    descriptor = fs.openSync(lockPath, "wx", 0o600);
    acquired = true;
    fs.writeFileSync(descriptor, `${process.pid}\n${Date.now()}\n`, "utf8");
    fs.fsyncSync(descriptor);
    return callback(root);
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    if (acquired) {
      try {
        fs.unlinkSync(lockPath);
        fsyncDirectory(root);
      } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
    }
  }
}

function markerFieldFromText(text, field) {
  const match = String(text).match(new RegExp(`^${field}:\\s*(.*?)\\s*$`, "m"));
  return match ? match[1].trim() : "";
}

function validPlanMarker(text, issue) {
  return (
    markerFieldFromText(text, "MARKER_PROVENANCE") === "codex-review-companion" &&
    markerFieldFromText(text, "TYPE") === "plan-review" &&
    markerFieldFromText(text, "ISSUE") === `#${issue}` &&
    /^[0-9a-f]{7,40}$/i.test(markerFieldFromText(text, "HEAD_SHA")) &&
    /^\d+$/.test(markerFieldFromText(text, "TIMESTAMP")) &&
    markerFieldFromText(text, "VERDICT") !== "" &&
    !text.includes("REVIEW_PROVENANCE_MODE: carry-forward")
  );
}

function historyFiles(directory, issue) {
  if (!fs.existsSync(directory)) return [];
  return fs
    .readdirSync(directory, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".md"))
    .map((entry) => path.join(directory, entry.name));
}

function legacyPlanMarkerMatchesScope(source, args, text) {
  const name = path.basename(source);
  const issue = String(args.issue);
  const escapedIssue = issue.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const filenameMatches = new RegExp(
    `^plan-(?:pr-[0-9]+-)?issue-${escapedIssue}(?:-|\\.|$)`,
  ).test(name);
  if (!filenameMatches) return false;

  // Filename identity selects the bounded legacy record set before strict
  // validation. Explicit type/issue fields can exclude a mismatched review;
  // missing fields stay relevant and therefore fail closed below.
  const type = markerFieldFromText(text, "TYPE");
  if (type && type !== "plan-review") return false;
  const markerIssue = markerFieldFromText(text, "ISSUE");
  if (markerIssue && markerIssue !== `#${issue}`) return false;
  return true;
}

function importRetainedPlanMarkers(args, repositoryDirectory) {
  const legacyRoot =
    process.env.CODEX_REVIEW_LEGACY_DIR || "/tmp/codex-review-companion";
  const candidates = historyFiles(legacyRoot, args.issue);
  for (const source of candidates) {
    const text = fs.readFileSync(source, "utf8");
    if (!legacyPlanMarkerMatchesScope(source, args, text)) continue;
    if (!validPlanMarker(text, args.issue)) {
      throw new Error(`PLAN_REVIEW_HISTORY_MARKER_INVALID: ${source}`);
    }
    const digest = crypto.createHash("sha256").update(text).digest("hex");
    const target = path.join(repositoryDirectory, `${digest}.md`);
    if (!fs.existsSync(target)) atomicWrite(target, text);
  }
}

function openBlockerClasses(text) {
  const classes = [];
  let currentClass = null;
  for (const line of String(text).split("\n")) {
    if (line.startsWith("BLOCKER_CLASS:")) {
      currentClass = line.slice("BLOCKER_CLASS:".length).trim();
    } else if (line.startsWith("BLOCKER_STATUS:")) {
      if (currentClass && line.slice("BLOCKER_STATUS:".length).trim() === "OPEN") {
        classes.push(currentClass);
      }
      currentClass = null;
    }
  }
  return classes;
}

function repeatedOpenBlockerClass(records, issue) {
  const counts = new Map();
  for (const file of records) {
    const text = fs.readFileSync(file, "utf8");
    if (!validPlanMarker(text, issue)) {
      throw new Error(`PLAN_REVIEW_HISTORY_RECORD_INVALID: ${file}`);
    }
    for (const blockerClass of new Set(openBlockerClasses(text))) {
      counts.set(blockerClass, (counts.get(blockerClass) || 0) + 1);
    }
  }
  return [...counts.entries()].find(([, count]) => count >= 2)?.[0] || null;
}

function completedPlanRecords(directory, issue) {
  return historyFiles(directory, issue).filter((file) => {
    const text = fs.readFileSync(file, "utf8");
    if (!validPlanMarker(text, issue)) {
      throw new Error(`PLAN_REVIEW_HISTORY_RECORD_INVALID: ${file}`);
    }
    return true;
  });
}

function liveReservationFiles(directory) {
  if (!fs.existsSync(directory)) return [];
  const reservations = fs
    .readdirSync(directory, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".reservation.json"))
    .map((entry) => path.join(directory, entry.name));
  const live = [];
  for (const file of reservations) {
    try {
      const record = JSON.parse(fs.readFileSync(file, "utf8"));
      const age = Date.now() - Date.parse(record.started_at || "");
      let running = false;
      if (Number.isInteger(record.pid) && record.pid > 0) {
        try {
          process.kill(record.pid, 0);
          running = true;
        } catch {
          running = false;
        }
      }
      if (!running && Number.isFinite(age) && age > 30 * 60 * 1000) {
        fs.unlinkSync(file);
        continue;
      }
      live.push(file);
    } catch {
      // A malformed reservation is fail-closed and remains counted.
      live.push(file);
    }
  }
  return live;
}

function releasePlanReviewReservation(args) {
  const reservation = args._planReviewReservation;
  if (!reservation) return;
  try {
    withPlanHistoryLock(() => {
      if (fs.existsSync(reservation)) fs.unlinkSync(reservation);
    });
  } catch {
    // The original admission remains fail-closed; never convert a transport
    // failure into a completed round when cleanup itself is unavailable.
  }
  delete args._planReviewReservation;
}

function reservePlanReviewOverride(args, runner = sh) {
  try {
    const key = planHistoryKey(args, runner);
    return withPlanHistoryLock((root) => {
      const directory = path.join(root, key);
      fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
      importRetainedPlanMarkers(args, directory);
      const completed = completedPlanRecords(directory, args.issue);
      const reservations = liveReservationFiles(directory);
      if (reservations.length) {
        return {
          ok: false,
          message: "PLAN_REVIEW_HISTORY_OVERRIDE_RESERVATION_CONFLICT",
        };
      }
      const reservation = path.join(
        directory,
        `${process.pid}-${Date.now()}-${crypto.randomBytes(6).toString("hex")}.reservation.json`,
      );
      atomicWrite(
        reservation,
        JSON.stringify(
          {
            issue: String(args.issue),
            review_type: "plan",
            head: args._currentHead || null,
            pid: process.pid,
            started_at: new Date().toISOString(),
            authorized_override: true,
            completed_rounds: completed.length,
          },
          null,
          2,
        ) + "\n",
      );
      args._planReviewReservation = reservation;
      return { ok: true };
    });
  } catch (error) {
    return {
      ok: false,
      message: `PLAN_REVIEW_HISTORY_UNAVAILABLE: ${error.message}`,
    };
  }
}

function runReviewBudget(args, runner = sh, { publish = false } = {}) {
  if (args.reviewType !== "plan") {
    return {
      ok: true,
      budget: {
        decision: "allowed",
        current_head_unreviewed_types: [],
        cap_consumed_by_current_head_pass: [],
        review_type_caps: [],
        hard_cap_types: [],
        blocking_round_counts_48h: {},
        cap_reasons: [],
      },
    };
  }
  try {
    const key = planHistoryKey(args, runner);
    return withPlanHistoryLock((root) => {
      const directory = path.join(root, key);
      fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
      importRetainedPlanMarkers(args, directory);
      const completed = completedPlanRecords(directory, args.issue);
      const repeatedClass = repeatedOpenBlockerClass(completed, args.issue);
      if (publish) {
        if (
          !args._planReviewReservation ||
          !fs.existsSync(args._planReviewReservation)
        ) {
          return { ok: false, message: "PLAN_REVIEW_HISTORY_RESERVATION_MISSING" };
        }
        const marker = args._canonicalMarkerPath;
        if (!marker || !fs.existsSync(marker)) {
          return { ok: false, message: "PLAN_REVIEW_HISTORY_MARKER_MISSING" };
        }
        const text = fs.readFileSync(marker, "utf8");
        if (!validPlanMarker(text, args.issue)) {
          return { ok: false, message: "PLAN_REVIEW_HISTORY_MARKER_INVALID" };
        }
        const digest = crypto.createHash("sha256").update(text).digest("hex");
        atomicWrite(path.join(directory, `${digest}.md`), text);
        if (args._planReviewReservation && fs.existsSync(args._planReviewReservation)) {
          fs.unlinkSync(args._planReviewReservation);
          fsyncDirectory(directory);
          delete args._planReviewReservation;
        }
        return {
          ok: true,
          budget: {
            decision: "allowed",
            blocking_round_counts_48h: { plan: completed.length + 1 },
            review_type_caps: [],
            hard_cap_types: [],
            cap_reasons: [],
          },
        };
      }
      const reservations = liveReservationFiles(directory);
      const rounds = completed.length;
      const reserved = reservations.length;
      const repeatedBlocker = Boolean(repeatedClass);
      const capped = repeatedBlocker || rounds + reserved >= PLAN_REVIEW_CAP;
      const baseBudget = {
        decision: capped ? "rescue_required" : "allowed",
        current_head_unreviewed_types: [],
        cap_consumed_by_current_head_pass: [],
        review_type_caps: capped ? ["plan"] : [],
        hard_cap_types: capped ? ["plan"] : [],
        blocking_round_counts_48h: { plan: rounds },
        cap_reasons: repeatedBlocker
          ? [`same_blocker_class:${repeatedClass}`]
          : capped
            ? ["plan_review_round_cap"]
            : [],
      };
      if (capped) {
        return { ok: true, budget: baseBudget };
      }
      const reservation = path.join(
        directory,
        `${process.pid}-${Date.now()}-${crypto.randomBytes(6).toString("hex")}.reservation.json`,
      );
      atomicWrite(
        reservation,
        JSON.stringify(
          {
            issue: String(args.issue),
            review_type: "plan",
            head: args._currentHead || null,
            pid: process.pid,
            started_at: new Date().toISOString(),
          },
          null,
          2,
        ) + "\n",
      );
      args._planReviewReservation = reservation;
      return { ok: true, budget: baseBudget };
    });
  } catch (error) {
    return {
      ok: false,
      message: `PLAN_REVIEW_HISTORY_UNAVAILABLE: ${error.message}`,
    };
  }
}

function exactHeadOverrideReviewAdmission(args, runner = sh) {
  if (!args.pr || !args.markerFile) return { allowed: false };

  const stateDir = process.env.HEYDONNA_PM_STATE_DIR || "/tmp";
  const rescopePath = path.join(stateDir, `pm-rescope-pr-${args.pr}.json`);
  const reviewPath = path.join(stateDir, `pm-review-pending-${args.pr}.json`);
  let rescope;
  let review;
  try {
    rescope = JSON.parse(fs.readFileSync(rescopePath, "utf8"));
    review = JSON.parse(fs.readFileSync(reviewPath, "utf8"));
  } catch {
    return { allowed: false };
  }

  const headResult = runner("git", ["rev-parse", "HEAD"], {
    cwd: args.repoRoot,
  });
  if (headResult.code !== 0) return { allowed: false };
  const head = headResult.stdout.trim();
  const expectedMarker = String(review.expected_marker || "");
  const proof = String(rescope.terminal_decision_proof || "");
  const matches =
    rescope.status === "resolved" &&
    rescope.terminal_decision === "override_with_evidence" &&
    String(rescope.pr) === String(args.pr) &&
    rescope.headRefOid === head &&
    review.status === "pending" &&
    review.scope === "phase-a" &&
    String(review.pr) === String(args.pr) &&
    review.headRefOid === head &&
    expectedMarker.length > 0 &&
    path.resolve(args.markerFile) === path.resolve(expectedMarker) &&
    !fs.existsSync(expectedMarker) &&
    proof.length > 0 &&
    fs.existsSync(proof);

  return matches
    ? { allowed: true, head, proof, expectedMarker }
    : { allowed: false };
}

function reviewBudgetPreflight(args, runner = sh) {
  if (!new Set(["plan", "code"]).has(args.reviewType)) return { allowed: true };

  if (!args.issue && args.branch) {
    args.issue = issueFromBranchName(args.branch);
  }
  if (!args.issue) {
    return {
      allowed: false,
      message:
        `${args.reviewType.toUpperCase()}_REVIEW_BUDGET_UNRESOLVED: ` +
        "--issue is required for fail-closed review admission",
    };
  }

  const checked = runReviewBudget(args, runner, { publish: false });
  if (!checked.ok) return { allowed: false, message: checked.message };
  const budget = checked.budget;
  const caps = budget.review_type_caps || budget.hard_cap_types || [];
  if (caps.includes(args.reviewType)) {
    const overrideAdmission = exactHeadOverrideReviewAdmission(args, runner);
    if (overrideAdmission.allowed) {
      const reservation = reservePlanReviewOverride(args, runner);
      if (!reservation.ok) {
        return { allowed: false, message: reservation.message };
      }
      return {
        allowed: true,
        budget,
        overrideAdmission,
      };
    }
    const rounds = budget.blocking_round_counts_48h?.[args.reviewType] || 0;
    const reasons = Array.isArray(budget.cap_reasons)
      ? budget.cap_reasons.join(",")
      : "unknown";
    const kind = args.reviewType.toUpperCase();
    return {
      allowed: false,
      budget,
      message:
        `${kind}_REVIEW_CAP_REACHED issue=${args.issue} pr=${args.pr || "pre-pr"} ` +
        `negative_${args.reviewType}_rounds=${rounds} cap_reasons=${reasons || "unknown"}. ` +
        "Ordinary next review is blocked before Codex invocation. " +
        `CTO escalation command: ${reviewRescueCommand(args)}`,
    };
  }

  return { allowed: true, budget };
}

function writeReviewCapPacket(args, admission) {
  const kind = String(args.reviewType || "review").toUpperCase();
  const identity =
    args.reviewType === "plan" ? args.issue : args.pr || args.issue;
  const packetPath = path.join(
    process.env.HEYDONNA_PM_STATE_DIR || "/tmp",
    `${args.reviewType}-review-cap-${identity}.txt`,
  );
  const budget = admission.budget || {};
  const rounds = budget.blocking_round_counts_48h?.[args.reviewType] || 0;
  const body = [
    `${kind}_REVIEW_CAP_REACHED`,
    "REVIEW_CAP_MARKER=pm-review-cap:v1",
    `issue=${args.issue || "unknown"}`,
    `pr=${args.pr || "pre-pr"}`,
    `slot=${process.env.HEYDONNA_SLOT || process.env.CLAUDE_SLOT || "unknown"}`,
    `branch=${args._currentBranch || args.branch || "unknown"}`,
    `head=${args._currentHead || budget.headRefOid || "unknown"}`,
    `plan_sha=${args._currentHead || budget.headRefOid || "unknown"}`,
    `marker=${args.markerFile || `/tmp/codex-app-${args.reviewType}-review-${identity}.txt`}`,
    `round_count=${rounds}`,
    `cap_reasons=${(budget.cap_reasons || []).join(",") || "unknown"}`,
    `next=${reviewRescueCommand(args)}`,
    "ordinary_review_blocked=true",
    "reviewer_invoked=false",
  ].join("\n");
  fs.mkdirSync(path.dirname(packetPath), { recursive: true });
  fs.writeFileSync(packetPath, `${body}\n`, { mode: 0o600 });
  return packetPath;
}

function reviewCapTerminal(args, admission) {
  const packetPath = writeReviewCapPacket(args, admission);
  console.error(
    `[codex-review-companion] REVIEW_CAP_TERMINAL packet=${packetPath} ${admission.message}`,
  );
  process.exit(42);
}

function planReviewBudgetPreflight(args, runner = sh) {
  return reviewBudgetPreflight(args, runner);
}

function publishReviewHistory(args, runner = sh) {
  if (
    (args.reviewType !== "plan" && !args.pr) ||
    !new Set(["plan", "code", "qa"]).has(args.reviewType)
  ) {
    return { ok: true };
  }
  return runReviewBudget(args, runner, { publish: true });
}

function fail(msg, code = 3) {
  console.error(`[codex-review-companion] ERROR: ${msg}`);
  process.exit(code);
}

function isReReview(args) {
  return (
    new Set(["plan", "code"]).has(args.reviewType) && Boolean(args.reworkItems)
  );
}

// Detect a main-merge descendant head: a commit with more than one parent.
function isMergeHead(args, currentHead, runner = sh) {
  const parents = runner(
    "git",
    ["rev-list", "--parents", "-n", "1", currentHead],
    { cwd: args.repoRoot },
  );
  if (parents.code !== 0 || !parents.stdout.trim()) return false;
  const fields = parents.stdout.trim().split(/\s+/);
  // fields[0] is the commit itself; the rest are its parents.
  return fields.length > 2;
}

// Authoritative PR-owned diff base for a main-merge descendant head.
//
// When a branch merges origin/<base> into itself, the review range must be
// the PR's OWN three-dot delta: merge-base(origin/<base>, HEAD)...HEAD. The
// prior reviewed head is usually the merge's FIRST parent, and
// `git diff <first-parent>...HEAD` degrades to a two-dot span that contains
// every main advance merged in — main-drift scope noise that CHECK 435/CHECK
// 458 forbid from blocking. Returns the true merge base or null when the head
// is not a merge (ordinary rework keeps the previous-head baseline).
function prOwnedDiffBase(args, currentHead, runner = sh) {
  if (!isMergeHead(args, currentHead, runner)) return null;
  const baseName = String(args._prData?.baseRefName || "main");
  const mb = runner(
    "git",
    ["merge-base", `origin/${baseName}`, currentHead],
    { cwd: args.repoRoot },
  );
  if (mb.code !== 0 || !mb.stdout.trim()) return null;
  return mb.stdout.trim();
}

function bindReReviewBaseline(args, currentHead, runner = sh) {
  if (!isReReview(args)) return null;
  if (!args.previousHead) {
    fail(
      `${args.reviewType === "plan" ? "Plan" : "Code"} re-review requires ` +
        "--previous-head <last-reviewed-head>; HEAD~1 is not a safe multi-commit baseline.",
    );
  }
  const previous = runner(
    "git",
    ["rev-parse", "--verify", `${args.previousHead}^{commit}`],
    { cwd: args.repoRoot },
  );
  if (previous.code !== 0 || !previous.stdout.trim()) {
    fail(`Could not resolve --previous-head ${args.previousHead}`);
  }
  const previousHead = previous.stdout.trim();
  if (args.rebaseBaseline) {
    if (args.reviewType !== "plan" || !args.issue || !args.branch) {
      fail(
        "Intentional rebase binding requires --review-type plan, --issue, and --branch.",
      );
    }
    if (!/^[1-9]\d*$/.test(String(args.reviewOrdinal || ""))) {
      fail("Intentional rebase binding requires a positive --review-ordinal.");
    }
    const baseline = runner(
      "git",
      ["rev-parse", "--verify", `${args.rebaseBaseline}^{commit}`],
      { cwd: args.repoRoot },
    );
    if (baseline.code !== 0 || !baseline.stdout.trim()) {
      fail(`Could not resolve --rebase-baseline ${args.rebaseBaseline}`);
    }
    const baselineHead = baseline.stdout.trim();
    const baselineAncestor = runner(
      "git",
      ["merge-base", "--is-ancestor", baselineHead, currentHead],
      { cwd: args.repoRoot },
    );
    if (baselineAncestor.code !== 0) {
      fail(
        `--rebase-baseline ${baselineHead.slice(0, 12)} is not an ancestor of ` +
          `current head ${currentHead.slice(0, 12)}; refuse unrelated lineage.`,
      );
    }
    const parentList = (head) => {
      const result = runner(
        "git",
        ["rev-list", "--parents", "-n", "1", head],
        { cwd: args.repoRoot },
      );
      if (result.code !== 0 || !result.stdout.trim()) return null;
      return result.stdout.trim().split(/\s+/).slice(1);
    };
    const priorParents = parentList(previousHead);
    const currentParents = parentList(currentHead);
    if (
      !priorParents ||
      !currentParents ||
      priorParents.length !== 1 ||
      currentParents.length !== 1
    ) {
      fail(
        "Intentional rebase binding requires prior and current review heads " +
          "to be non-merge commits with exactly one parent.",
      );
    }
    const priorParent = priorParents[0];
    const currentParent = currentParents[0];
    const priorParentToBaseline = runner(
      "git",
      ["merge-base", "--is-ancestor", priorParent, baselineHead],
      { cwd: args.repoRoot },
    );
    if (
      priorParent === baselineHead ||
      priorParentToBaseline.code !== 0 ||
      currentParent !== baselineHead
    ) {
      fail(
        "Intentional rebase binding requires the declared baseline to be a " +
          "strict descendant of the prior head parent and the current head's " +
          "sole parent.",
      );
    }
    args._reviewBaselineHead = previousHead;
    args._rebaseBaseline = baselineHead;
    args._prOwnedBase = null;
    return baselineHead;
  }
  const ancestor = runner(
    "git",
    ["merge-base", "--is-ancestor", previousHead, currentHead],
    { cwd: args.repoRoot },
  );
  if (ancestor.code !== 0) {
    fail(
      `--previous-head ${previousHead.slice(0, 12)} is not an ancestor of ` +
        `current head ${currentHead.slice(0, 12)}; require PM adjudication ` +
        "instead of guessing a rework delta.",
    );
  }
  args._reviewBaselineHead = previousHead;
  const prOwnedBase = prOwnedDiffBase(args, currentHead, runner);
  args._prOwnedBase = prOwnedBase || null;
  // A main-merge descendant's PR-owned delta is merge-base(origin/<base>,
  // HEAD)...HEAD, not the first-parent span; the first parent is the prior
  // reviewed head and its two-dot span contains every merged main advance.
  return prOwnedBase || previousHead;
}

function info(args, msg) {
  if (args.verbose) console.error(`[codex-review-companion] ${msg}`);
}

function requiredCallerTimeoutMs(internalTimeoutMs = CODEX_TIMEOUT_MS) {
  return internalTimeoutMs + CALLER_TIMEOUT_GRACE_MS;
}

function terminationDiagnostic({
  signal,
  phase,
  elapsedMs,
  internalTimeoutMs = CODEX_TIMEOUT_MS,
}) {
  return [
    "classification=CALLER_TIMEOUT_OR_TERMINATION",
    `signal=${signal}`,
    `phase=${phase}`,
    `elapsed_ms=${elapsedMs}`,
    `internal_timeout_ms=${internalTimeoutMs}`,
    `required_outer_timeout_ms=${requiredCallerTimeoutMs(internalTimeoutMs)}`,
  ].join(" ");
}

function isIssueOnlyArchReview(args) {
  return (
    args.reviewType === "arch" &&
    Boolean(args.issue) &&
    !args.pr &&
    !args.branch
  );
}

function sourceRequirementError(args) {
  if (args.pr || args.branch || isIssueOnlyArchReview(args)) return null;
  if (args.reviewType === "arch") {
    return "Architecture review requires --issue <N>, --pr <N>, or --branch <name>";
  }
  return "--pr <N> or --branch <name> required";
}

// ---------- diff resolution ----------

const AUTO_EXCLUDE = [
  ":!tests/e2e/fixtures/*.zip",
  ":!tests/e2e/fixtures/*.docx",
  ":!tests/e2e/fixtures/*.mp3",
  ":!tests/e2e/fixtures/*.png",
  ":!tests/e2e/fixtures/*.jpg",
  ":!tests/e2e/fixtures/*.pdf",
  ":!**/*.zip",
  ":!**/*.mp3",
  ":!**/*.docx",
  ":!**/*.pdf",
  ":!**/node_modules/**",
  ":!.next/**",
  ":!dist/**",
  ":!build/**",
];

function currentBranchName(repoRoot) {
  const r = sh("git", ["branch", "--show-current"], { cwd: repoRoot });
  return r.code === 0 ? r.stdout.trim() : "";
}

function currentHeadSha(repoRoot) {
  const r = sh("git", ["rev-parse", "HEAD"], { cwd: repoRoot });
  return r.code === 0 ? r.stdout.trim() : "";
}

function issueFromBranchName(branch) {
  const m = String(branch || "").match(/(?:^|[/_-])(\d{3,6})(?:[/_-]|$)/);
  return m ? m[1] : null;
}

function resolveIssueFromPrContext(prData, currentBranch) {
  const branchIssue =
    issueFromBranchName(currentBranch) ||
    issueFromBranchName(prData.headRefName);
  if (branchIssue) return branchIssue;

  const title = prData.title || "";
  const titleIssue = title.match(/#(\d{3,6})\b/);
  if (titleIssue) return titleIssue[1];

  const body = prData.body || "";
  // Only closing keywords are authoritative enough for auto-resolution.
  // Do not use refs/issue/first-# fallback: those commonly point at parents,
  // siblings, CP notes, or superseded issues and caused false #3568/#11 reviews.
  const closing = body.match(
    /(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#?(\d{3,6})\b/i,
  );
  if (closing) return closing[1];

  return String(prData.number || "");
}

function exactQaPrHeadBinding(reviewType, prData, resolvedHead) {
  if (reviewType !== "qa") return null;

  const branch = String(prData?.headRefName || "").trim();
  const head = String(prData?.headRefOid || "").trim();
  const fetchedHead = String(resolvedHead || "").trim();
  if (!branch) {
    throw new Error("QA review requires a PR head branch");
  }
  if (!/^[0-9a-f]{40}$/.test(head)) {
    throw new Error("QA review requires an exact 40-character PR headRefOid");
  }
  if (fetchedHead !== head) {
    throw new Error(
      `QA review fetched head ${fetchedHead || "missing"} but PR headRefOid is ${head}; ` +
        "refusing stale or cross-wired QA evidence",
    );
  }
  return { branch, head };
}

function resolveDiff(args) {
  let baseRef, headRef;
  if (args.pr) {
    const r = sh("gh", [
      "pr",
      "view",
      args.pr,
      "--json",
      "baseRefName,baseRefOid,headRefName,headRefOid,title,body,number,commits",
    ]);
    if (r.code !== 0) fail(`gh pr view ${args.pr} failed: ${r.stderr}`);
    const j = JSON.parse(r.stdout);
    baseRef = `origin/${j.baseRefName}`;
    // Fetch the base ref to avoid "bad revision" failures.
    sh("git", ["fetch", "origin", j.baseRefName], { cwd: args.repoRoot });
    args._prData = j;

    if (args.reviewType === "code") {
      const currentBranch = currentBranchName(args.repoRoot);
      const currentHead = currentHeadSha(args.repoRoot);
      if (!currentHead)
        fail(`Could not resolve current HEAD in ${args.repoRoot}`);
      if (j.headRefOid && currentHead !== j.headRefOid) {
        fail(
          `Code review must run from the current PR branch head. cwd=${args.repoRoot} ` +
            `HEAD=${currentHead.slice(0, 12)} PR#${args.pr} head=${String(j.headRefOid).slice(0, 12)} ` +
            `branch=${j.headRefName}. Checkout/pull the PR branch first.`,
        );
      }
      if (currentBranch && j.headRefName && currentBranch !== j.headRefName) {
        fail(
          `Code review must run on the PR branch. current=${currentBranch} ` +
            `PR#${args.pr} branch=${j.headRefName}. Checkout the PR branch first.`,
        );
      }
      if (
        args.branch &&
        args.branch !== currentBranch &&
        args.branch !== j.headRefName
      ) {
        fail(
          `--branch ${args.branch} does not match current/PR branch ` +
            `${currentBranch || j.headRefName}; refusing cross-wired code review.`,
        );
      }
      headRef = "HEAD";
      args._currentBranch = currentBranch;
      args._currentHead = currentHead;
    } else {
      headRef = `origin/${j.headRefName}`;
      const fetch = sh("git", ["fetch", "origin", j.headRefName], {
        cwd: args.repoRoot,
      });
      if (new Set(["plan", "qa"]).has(args.reviewType) && fetch.code !== 0) {
        fail(
          `git fetch origin ${j.headRefName} failed for ${args.reviewType} review: ${fetch.stderr}`,
        );
      }
      if (new Set(["plan", "qa"]).has(args.reviewType)) {
        const resolved = sh(
          "git",
          ["rev-parse", "--verify", `${headRef}^{commit}`],
          { cwd: args.repoRoot },
        );
        if (resolved.code !== 0 || !resolved.stdout.trim()) {
          fail(`Could not resolve fetched ${args.reviewType} head ${headRef}`);
        }
        const resolvedHead = resolved.stdout.trim();
        if (resolvedHead !== j.headRefOid) {
          fail(
            `${args.reviewType} review fetched head ${resolvedHead.slice(0, 12)} ` +
              `does not match PR#${args.pr} head ${String(j.headRefOid).slice(0, 12)}`,
          );
        }
        if (args.reviewType === "qa") {
          try {
            const binding = exactQaPrHeadBinding(
              args.reviewType,
              j,
              resolvedHead,
            );
            args._currentBranch = binding.branch;
            args._currentHead = binding.head;
          } catch (error) {
            fail(error instanceof Error ? error.message : String(error));
          }
        } else {
          args._currentBranch = j.headRefName;
          args._currentHead = resolvedHead;
        }
      }
    }

    if (!args.issue) {
      args.issue = resolveIssueFromPrContext(j, args._currentBranch || "");
    }
  } else if (args.branch) {
    // Detect whether the branch exists on origin. Plan review on a local-only
    // feature branch is valid — `main...<local-branch>` is sufficient and
    // avoids forcing a premature push. Rajiv directive 2026-05-08 19:03 IST.
    const remoteCheck = sh(
      "git",
      ["ls-remote", "--heads", "origin", args.branch],
      { cwd: args.repoRoot },
    );
    const branchOnRemote =
      remoteCheck.code === 0 &&
      remoteCheck.stdout &&
      remoteCheck.stdout.trim().length > 0;

    if (branchOnRemote) {
      sh("git", ["fetch", "origin", args.branch], { cwd: args.repoRoot });
      sh("git", ["fetch", "origin", "main"], { cwd: args.repoRoot });
      baseRef = "origin/main";
      headRef = `origin/${args.branch}`;
    } else {
      // Local-only branch (e.g., plan review before push). Use local refs.
      const localCheck = sh("git", ["rev-parse", "--verify", args.branch], {
        cwd: args.repoRoot,
      });
      if (localCheck.code !== 0) {
        fail(`Branch '${args.branch}' not found locally or on origin`);
      }
      // Refresh local main so the diff is computed against an up-to-date base.
      sh("git", ["fetch", "origin", "main"], { cwd: args.repoRoot });
      baseRef = "main";
      headRef = args.branch;
    }
    const headCheck = sh("git", ["rev-parse", "--verify", headRef], {
      cwd: args.repoRoot,
    });
    if (headCheck.code !== 0 || !headCheck.stdout.trim()) {
      fail(
        `Could not resolve exact plan head for ${headRef}: ${headCheck.stderr}`,
      );
    }
    args._currentBranch = args.branch;
    args._currentHead = headCheck.stdout.trim();
  } else {
    fail("Must provide --pr or --branch");
  }

  if (isReReview(args)) {
    baseRef = bindReReviewBaseline(args, args._currentHead);
  }

  const excludes = AUTO_EXCLUDE.concat(args.exclude.map((e) => `:!${e}`));
  const diffArgs = [
    "diff",
    `${baseRef}...${headRef}`,
    "--",
    ...(args.scope.length ? args.scope : []),
    ...excludes,
  ];
  const r = sh("git", diffArgs, { cwd: args.repoRoot });
  if (r.code !== 0)
    fail(`git diff ${baseRef}...${headRef} failed: ${r.stderr}`);

  args._diffBaseRef = baseRef;
  args._diffHeadRef = headRef;

  let diff = r.stdout;
  if (diff.length > DIFF_TRUNCATE_AT) {
    diff =
      diff.slice(0, DIFF_TRUNCATE_AT) +
      `\n\n[diff truncated at ${DIFF_TRUNCATE_AT} chars — review the listed files manually]\n`;
  }
  return { diff, baseRef, headRef };
}

function resolveIssueOnlyArchBaseline(args) {
  const fetch = sh("git", ["fetch", "origin", "main"], { cwd: args.repoRoot });
  if (fetch.code !== 0) {
    fail(
      `git fetch origin main failed for issue-only arch review: ${fetch.stderr}`,
    );
  }
  const rev = sh("git", ["rev-parse", "origin/main"], { cwd: args.repoRoot });
  if (rev.code !== 0 || !rev.stdout.trim()) {
    fail(
      `Could not resolve origin/main for issue-only arch review: ${rev.stderr}`,
    );
  }
  args._sourceMode = "issue-only";
  args._baselineRef = "origin/main";
  args._baselineSha = rev.stdout.trim();
  return {
    mode: "issue-only",
    baselineRef: args._baselineRef,
    baselineSha: args._baselineSha,
  };
}

function composeReviewPrompt(promptBody, source) {
  if (source.mode === "issue-only") {
    return `${promptBody}\n\n== REVIEW SOURCE (ISSUE-ONLY ARCHITECTURE REVIEW) ==\n\nNo implementation branch or PR exists yet, so no implementation diff is expected. Review the issue contract and focus text against the current main baseline. Use read-only repository tools to inspect the relevant runtime control points and alternative paths; do not treat the absence of a diff as missing evidence.\n\nBASELINE_REF: ${source.baselineRef}\nBASELINE_SHA: ${source.baselineSha}`;
  }
  return `${promptBody}\n\n== DIFF (${source.baseRef}...${source.headRef}) ==\n\n${source.diff}`;
}

function appendTransitionMarkerContract(prompt, args) {
  if (!new Set(["plan", "code"]).has(args.reviewType)) return prompt;
  const transitionFields =
    args.reviewType === "code" && args.pr
      ? "\nRUNTIME_CONTROL_POINT: <the production control point actually reviewed>" +
        "\nPASS_SCOPE: phase-a if the verdict approves this exact head; otherwise blocked" +
        "\nREADINESS_CEILING: qa-passed-awaiting-ci if approved; otherwise blocked"
      : "";
  return `${prompt}\n\n== REVIEW CONVERGENCE MARKER CONTRACT ==\n\nFor every blocking finding, emit one complete repeated record at the end of the review:\nBLOCKER_ID: <stable CLASS-NNN identifier reused on re-review>\nBLOCKER_CLASS: DIRECTIVE | CONTROL_POINT | REACHABILITY | SCOPE | IMPLEMENTATION | PROOF\nBLOCKER_FINGERPRINT: <sha256 of the invariant + owning control point, stable across wording changes>\nBLOCKER_STATUS: OPEN | RESOLVED | SUPERSEDED\nBLOCKER_ORIGIN: prior | new_revision | newly_inspectable\nBLOCKER_REASON: <one-line invariant and evidence>\n\nOn re-review, emit a terminal status for every prior blocker before introducing a new one. A prior blocker recorded RESOLVED or SUPERSEDED is TERMINAL: do not reopen it on a descendant head unless the new delta touches its runtime control point or supplies a NEW concrete counterexample, and then emit BLOCKER_ORIGIN: new_revision with the new evidence (never prior). P2 findings are follow-ups and cannot support a negative verdict. If PM explicitly upgrades a P2 because of evidenced runtime or release impact, also emit:\nSEVERITY_OVERRIDE: P1\nSEVERITY_OVERRIDE_REASON: <durable evidence>${transitionFields}\nDo not claim merge-ready here. Current-head CI/E2E remains a separate gate.`;
}

function validatePriorBlockerCarryForward(parsed, budget, currentHead) {
  // A resolved or superseded prior blocker must NOT be re-opened as
  // BLOCKER_ORIGIN=prior on a descendant head unless the new delta touches
  // its runtime control point or supplies a NEW concrete counterexample.
  // Relitigating a terminal blocker as prior-origin OPEN without new evidence
  // is a contract violation and must fail closed (no marker written).
  const prior = priorBlockerLedger(budget, currentHead);
  const terminalByKey = new Map();
  for (const entry of prior) {
    const status = String(entry.status).toUpperCase();
    if (!["RESOLVED", "SUPERSEDED"].includes(status)) continue;
    const key =
      entry.blocker_fingerprint ||
      `${entry.blocker_class}:${entry.blocker_id}`;
    terminalByKey.set(key, { status, blockerId: entry.blocker_id, head: entry.head });
  }
  const violations = [];
  for (const blocker of parsed?.blockers || []) {
    if (String(blocker.status).toUpperCase() !== "OPEN") continue;
    if (String(blocker.origin || "").toLowerCase() !== "prior") continue;
    const key =
      blocker.fingerprint || `${blocker.blockerClass}:${blocker.blockerId}`;
    const terminal = terminalByKey.get(key);
    if (!terminal) continue;
    violations.push(
      `${blocker.blockerId} (${terminal.status} at ${terminal.head || "prior"})`,
    );
  }
  if (!violations.length) return null;
  return (
    "PRIOR_TERMINAL_BLOCKER_REOPENED: " +
    violations.join(", ") +
    " was previously RESOLVED/SUPERSEDED and cannot be re-opened with " +
    "BLOCKER_ORIGIN=prior without a new counterexample or a delta that " +
    "touches its runtime control point."
  );
}

function priorBlockerLedger(budget, currentHead) {
  // Consume the FULL event history, not just blocking/OPEN rows. A resolved
  // or superseded prior blocker must remain terminal on a descendant head
  // unless the new delta touches its runtime control point or supplies a new
  // concrete counterexample. blocking_class_sequence_48h filters to OPEN
  // blocking rows, silently dropping terminal statuses and letting the
  // reviewer relitigate (REACHABILITY-001 family: #7255 CONTROL_POINT-001 /
  // IMPLEMENTATION-003 / PROOF-003 reopened after RESOLVED/SUPERSEDED).
  const events = Array.isArray(budget?.events) ? budget.events : [];
  const latest = new Map();
  for (const event of events) {
    const fingerprint = String(event.blocker_fingerprint || "");
    const blockerId = String(event.blocker_id || "");
    const head = String(event.headRefOid || "");
    if ((!fingerprint && !blockerId) || !head) continue;
    // The current head's own marker (when already published, e.g. during a
    // replay) is not a PRIOR blocker.
    if (currentHead && head === String(currentHead)) continue;
    const key =
      fingerprint || `${event.layer}:${event.review_type}:${blockerId}`;
    const mtime = Number(event.mtime || 0);
    const existing = latest.get(key);
    if (existing && mtime < Number(existing.mtime || 0)) continue;
    latest.set(key, {
      mtime,
      blocker_id: blockerId || "unknown",
      blocker_class: event.blocker_class || event.class || "unknown",
      blocker_fingerprint: fingerprint || "legacy-missing",
      status: event.blocker_status || "OPEN",
      head,
      reason: event.blocker_reason || null,
    });
  }
  return [...latest.values()];
}

function appendPriorBlockerLedger(prompt, budget, currentHead) {
  const prior = priorBlockerLedger(budget, currentHead);
  const open = prior.filter(
    (entry) => String(entry.status).toUpperCase() === "OPEN",
  );
  const terminal = prior.filter(
    (entry) => !["OPEN", ""].includes(String(entry.status).toUpperCase()),
  );
  const ledger = open.length ? JSON.stringify(open, null, 2) : "[]";
  const terminalLedger = terminal.length
    ? JSON.stringify(terminal, null, 2)
    : "[]";
  return (
    `${prompt}\n\n== PRIOR BLOCKER LEDGER ==\n\n${ledger}\n\n` +
    `== TERMINAL PRIOR BLOCKERS (RESOLVED/SUPERSEDED) ==\n\n${terminalLedger}\n\n` +
    "This ledger is authoritative review context. Resolve or preserve each " +
    "OPEN blocker by its stable ID and fingerprint. Do not rename a prior " +
    "blocker.\n" +
    "CARRY-FORWARD SEMANTICS: a prior blocker recorded RESOLVED or SUPERSEDED " +
    "is TERMINAL — do not reopen it on a descendant head unless the new delta " +
    "touches its runtime control point or supplies a NEW concrete " +
    "counterexample. Reopening requires BLOCKER_ORIGIN: new_revision with the " +
    "new evidence; never re-emit a terminal blocker as OPEN with " +
    "BLOCKER_ORIGIN: prior. A ship-time/release gate (for example " +
    "CONTROL_POINT-001 post-#7207 live-path gate) stays a release gate, not a " +
    "repeated phase-a blocker. Introduce a new blocker only when the current " +
    "revision caused it or the necessary evidence was genuinely unavailable " +
    "to the prior review."
  );
}

function latestReviewedHead(budget, reviewType) {
  const events = Array.isArray(budget?.events) ? budget.events : [];
  const candidates = events
    .filter(
      (event) =>
        event.review_type === reviewType &&
        event.layer === "slot_codex" &&
        // Only COMPLETED reviews count as the latest reviewed head
        // (b2e016a5): an incomplete live-head cap terminal (the reviewer
        // never ran) must not suppress the one bounded current-head review
        // with SAME_HEAD_REVIEW_SUPPRESSED. Refused/uninvoked/malformed/
        // transport-failed/stale-head and UNKNOWN markers are telemetry.
        event.completed === true &&
        /^[0-9a-f]{7,40}$/i.test(String(event.headRefOid || "")),
    )
    .sort((a, b) => Number(b.mtime || 0) - Number(a.mtime || 0));
  return candidates.length ? String(candidates[0].headRefOid) : null;
}

const CARRY_FORWARD_APPROVALS = new Set([
  "APPROVE",
  "APPROVED",
  "APPROVE_PENDING_CI",
  "APPROVED_PENDING_CI",
  "PASS",
]);

function markerField(text, name) {
  const match = String(text || "").match(
    new RegExp(`^${name}:\\s*(.*?)\\s*$`, "m"),
  );
  return match ? match[1].trim() : "";
}

function latestApprovedReviewEvent(budget, reviewType = "code") {
  const events = Array.isArray(budget?.events) ? budget.events : [];
  const completed = events
    .filter(
      (event) =>
        event.review_type === reviewType &&
        event.layer === "slot_codex" &&
        event.completed === true &&
        /^[0-9a-f]{40}$/.test(String(event.headRefOid || "")) &&
        String(event.path || ""),
    )
    .sort((a, b) => Number(b.mtime || 0) - Number(a.mtime || 0));
  const latest = completed[0];
  if (
    !latest ||
    latest.blocking === true ||
    !CARRY_FORWARD_APPROVALS.has(String(latest.verdict || "").toUpperCase())
  ) {
    return null;
  }
  return latest;
}

function productPaths(scope) {
  const ownership = scope?.ownership || {};
  return [...new Set(scope?.changed_files || [])]
    .filter((file) => ownership[file] && ownership[file] !== "control_plane")
    .sort();
}

function stablePatchId(args, base, head, paths, runner = sh) {
  if (!paths.length) return null;
  const diff = runner(
    "git",
    [
      "diff",
      "--binary",
      "--full-index",
      "--no-ext-diff",
      "--no-renames",
      base,
      head,
      "--",
      ...paths,
    ],
    { cwd: args.repoRoot },
  );
  if (diff.code !== 0) return null;
  const patch = runner("git", ["patch-id", "--stable"], {
    cwd: args.repoRoot,
    input: diff.stdout,
  });
  if (patch.code !== 0) return null;
  const match = patch.stdout.trim().match(/^([0-9a-f]{40})\s/);
  return match ? match[1] : null;
}

function liveReviewThreadState(args, runner = sh) {
  const query = `query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){
    repository(owner:$owner,name:$name){pullRequest(number:$pr){
      headRefOid
      reviewThreads(first:100,after:$endCursor){
        nodes{id isResolved}
        pageInfo{hasNextPage endCursor}
      }
    }}
  }`;
  const response = runner(
    "gh",
    [
      "api",
      "graphql",
      "--paginate",
      "--slurp",
      "-f",
      "owner=heydonna-app",
      "-f",
      "name=heydonna-app",
      "-F",
      `pr=${args.pr}`,
      "-f",
      `query=${query}`,
    ],
    { cwd: args.repoRoot },
  );
  if (response.code !== 0) {
    return { ok: false, reason: "review_threads_unreadable" };
  }
  try {
    const pages = JSON.parse(response.stdout);
    if (!Array.isArray(pages) || !pages.length)
      throw new Error("missing pages");
    const unresolved = [];
    let head = "";
    for (const page of pages) {
      const pull = page?.data?.repository?.pullRequest;
      const threads = pull?.reviewThreads;
      if (!pull || !Array.isArray(threads?.nodes)) throw new Error("bad page");
      if (!head) head = String(pull.headRefOid || "");
      if (head !== String(pull.headRefOid || "")) throw new Error("head drift");
      for (const node of threads.nodes) {
        if (
          !node ||
          typeof node.id !== "string" ||
          typeof node.isResolved !== "boolean"
        ) {
          throw new Error("bad thread");
        }
        if (!node.isResolved) unresolved.push(node.id);
      }
    }
    const finalInfo =
      pages.at(-1)?.data?.repository?.pullRequest?.reviewThreads?.pageInfo;
    if (!finalInfo || finalInfo.hasNextPage !== false)
      throw new Error("incomplete pages");
    return { ok: true, head, unresolved };
  } catch (error) {
    return { ok: false, reason: `review_threads_invalid:${error.message}` };
  }
}

function reviewCarryForwardAdmission(
  args,
  budget,
  { runner = sh, classify = classifyDelta, threadState = null } = {},
) {
  if (args.reviewType !== "code" || !args.pr || args.reworkItems) {
    return { status: "not_applicable", reason: "not_implicit_pr_code_review" };
  }
  const event = latestApprovedReviewEvent(budget, "code");
  if (!event) return { status: "not_applicable", reason: "no_prior_approval" };
  const approvedHead = String(event.headRefOid);
  const currentHead = String(args._currentHead || "");
  if (!currentHead || approvedHead === currentHead) {
    return { status: "not_applicable", reason: "same_or_missing_head" };
  }

  let markerText;
  try {
    markerText = fs.readFileSync(String(event.path), "utf8");
  } catch {
    return { status: "not_applicable", reason: "source_marker_unreadable" };
  }
  const markerVerdict = markerField(
    markerText,
    "FINAL_REVIEWER_VERDICT",
  ).toUpperCase();
  const markerPr = markerField(markerText, "PR").replace(/^#/, "");
  if (
    markerField(markerText, "MARKER_PROVENANCE") !== "codex-review-companion" ||
    markerField(markerText, "TYPE") !== "code-review" ||
    !CARRY_FORWARD_APPROVALS.has(markerVerdict) ||
    markerPr !== String(args.pr) ||
    markerField(markerText, "HEAD_SHA") !== approvedHead
  ) {
    return { status: "not_applicable", reason: "source_marker_invalid" };
  }

  const parents = runner(
    "git",
    ["rev-list", "--parents", "-n", "1", currentHead],
    { cwd: args.repoRoot },
  );
  const fields = parents.stdout.trim().split(/\s+/);
  if (parents.code !== 0 || fields.length !== 3 || fields[0] !== currentHead) {
    return {
      status: "not_applicable",
      reason: "current_head_not_two_parent_merge",
    };
  }
  const [, branchPreMergeHead, mainParentHead] = fields;
  if (mainParentHead !== String(args._prData?.baseRefOid || "")) {
    return { status: "not_applicable", reason: "main_parent_mismatch" };
  }
  const firstParentHistory = runner(
    "git",
    ["rev-list", "--first-parent", branchPreMergeHead],
    { cwd: args.repoRoot },
  );
  const firstParentHeads = firstParentHistory.stdout.trim().split(/\s+/);
  if (
    firstParentHistory.code !== 0 ||
    !firstParentHeads.includes(approvedHead)
  ) {
    return {
      status: "not_applicable",
      reason: "approved_head_not_first_parent_ancestor",
    };
  }

  if (approvedHead !== branchPreMergeHead) {
    const postReviewScope = classify(
      args,
      approvedHead,
      branchPreMergeHead,
      runner,
    );
    if (
      postReviewScope?.product_changed !== false ||
      postReviewScope?.control_plane_only !== true
    ) {
      return { status: "not_applicable", reason: "post_review_product_delta" };
    }
  }

  const commits = Array.isArray(args._prData?.commits)
    ? args._prData.commits
    : [];
  const firstPrCommit = String(commits[0]?.oid || "");
  if (!/^[0-9a-f]{40}$/.test(firstPrCommit)) {
    return { status: "not_applicable", reason: "pr_commit_base_unavailable" };
  }
  const firstParents = runner(
    "git",
    ["rev-list", "--parents", "-n", "1", firstPrCommit],
    { cwd: args.repoRoot },
  );
  const firstFields = firstParents.stdout.trim().split(/\s+/);
  if (
    firstParents.code !== 0 ||
    firstFields.length !== 2 ||
    firstFields[0] !== firstPrCommit
  ) {
    return { status: "not_applicable", reason: "pr_commit_base_invalid" };
  }
  const reviewedBaseHead = firstFields[1];
  const baseAncestor = runner(
    "git",
    ["merge-base", "--is-ancestor", reviewedBaseHead, approvedHead],
    { cwd: args.repoRoot },
  );
  if (baseAncestor.code !== 0) {
    return {
      status: "not_applicable",
      reason: "reviewed_base_not_approved_ancestor",
    };
  }

  const approvedScope = classify(args, reviewedBaseHead, approvedHead, runner);
  const currentScope = classify(args, mainParentHead, currentHead, runner);
  const approvedProductPaths = productPaths(approvedScope);
  const currentProductPaths = productPaths(currentScope);
  if (
    !approvedProductPaths.length ||
    JSON.stringify(approvedProductPaths) !== JSON.stringify(currentProductPaths)
  ) {
    return { status: "not_applicable", reason: "product_path_set_changed" };
  }
  const approvedPatchId = stablePatchId(
    args,
    reviewedBaseHead,
    approvedHead,
    approvedProductPaths,
    runner,
  );
  const currentPatchId = stablePatchId(
    args,
    mainParentHead,
    currentHead,
    currentProductPaths,
    runner,
  );
  if (!approvedPatchId || approvedPatchId !== currentPatchId) {
    return {
      status: "not_applicable",
      reason: "product_patch_identity_changed",
    };
  }

  const liveThreads = threadState || liveReviewThreadState(args, runner);
  if (!liveThreads.ok) {
    return {
      status: "blocked",
      reason: liveThreads.reason || "review_threads_unreadable",
    };
  }
  if (liveThreads.head !== currentHead) {
    return { status: "blocked", reason: "head_drift" };
  }
  if (liveThreads.unresolved.length) {
    return {
      status: "blocked",
      reason: `unresolved_review_threads:${liveThreads.unresolved.join(",")}`,
    };
  }

  return {
    status: "carry_forward",
    reason: "verified_pure_main_descendant",
    sourceMarker: String(event.path),
    sourceMarkerSha256: crypto
      .createHash("sha256")
      .update(markerText)
      .digest("hex"),
    approvedHead,
    reviewedBaseHead,
    branchPreMergeHead,
    mainParentHead,
    productPatchId: approvedPatchId,
    productPaths: approvedProductPaths,
    rulesSha256:
      currentScope?.rules_sha256 || approvedScope?.rules_sha256 || "unknown",
    runtimeControlPoint:
      markerField(markerText, "runtime_control_point") || "review-provenance",
    passScope: markerField(markerText, "pass_scope") || "phase-a",
    readinessCeiling:
      markerField(markerText, "readiness_ceiling") || "qa-passed-awaiting-ci",
  };
}

function classifyDelta(args, baseline, headRef, runner = sh) {
  const classifier = path.join(
    args.repoRoot,
    "scripts",
    "ci",
    "change_scope.py",
  );
  const result = runner(
    "python3",
    [
      classifier,
      "--repo-root",
      args.repoRoot,
      "--base",
      baseline,
      "--head",
      headRef,
    ],
    { cwd: args.repoRoot },
  );
  if (result.code !== 0) {
    return {
      scope: "unknown",
      product_changed: true,
      error: result.stderr.trim(),
    };
  }
  try {
    return JSON.parse(result.stdout);
  } catch (error) {
    return { scope: "unknown", product_changed: true, error: error.message };
  }
}

function automaticDeltaReview(args, source, budget, runner = sh) {
  if (!new Set(["plan", "code"]).has(args.reviewType) || isReReview(args)) {
    return { source, mode: isReReview(args) ? "explicit_delta" : "full" };
  }
  const previous = latestReviewedHead(budget, args.reviewType);
  if (!previous) return { source, mode: "full" };
  if (previous === args._currentHead) {
    return { source, mode: "same_head", previousHead: previous };
  }

  const resolved = runner(
    "git",
    ["rev-parse", "--verify", `${previous}^{commit}`],
    {
      cwd: args.repoRoot,
    },
  );
  if (resolved.code !== 0 || !resolved.stdout.trim()) {
    fail(
      `Could not resolve durable prior ${args.reviewType} review head ${previous}`,
    );
  }
  const previousHead = resolved.stdout.trim();
  const ancestor = runner(
    "git",
    ["merge-base", "--is-ancestor", previousHead, args._currentHead],
    { cwd: args.repoRoot },
  );
  if (ancestor.code !== 0) {
    fail(
      `REVIEW_HISTORY_DIVERGED previous=${previousHead} current=${args._currentHead}. ` +
        "Do not restart a full review after rewritten history; PM must bind the intended baseline.",
      42,
    );
  }

  args.previousHead = previousHead;
  args._reviewBaselineHead = previousHead;
  const prOwnedBase = prOwnedDiffBase(args, args._currentHead, runner);
  args._prOwnedBase = prOwnedBase || null;
  const deltaBase = prOwnedBase || previousHead;
  args.reworkItems =
    "Automatic delta re-review: terminalize every prior blocker and inspect only " +
    "the new revision plus the minimum downstream seams needed to validate it.";
  const excludes = AUTO_EXCLUDE.concat(
    args.exclude.map((entry) => `:!${entry}`),
  );
  const diffArgs = [
    "diff",
    `${deltaBase}...${source.headRef}`,
    "--",
    ...(args.scope.length ? args.scope : []),
    ...excludes,
  ];
  const diffResult = runner("git", diffArgs, { cwd: args.repoRoot });
  if (diffResult.code !== 0) {
    fail(`git delta review diff failed: ${diffResult.stderr}`);
  }
  const deltaScope = classifyDelta(args, deltaBase, source.headRef, runner);
  args._deltaScope = deltaScope;
  args._diffBaseRef = deltaBase;
  let diff = diffResult.stdout;
  if (diff.length > DIFF_TRUNCATE_AT) {
    diff =
      diff.slice(0, DIFF_TRUNCATE_AT) +
      `\n\n[delta diff truncated at ${DIFF_TRUNCATE_AT} chars]\n`;
  }
  return {
    mode: "automatic_delta",
    previousHead,
    source: { ...source, diff, baseRef: previousHead },
  };
}

// ---------- issue body fetch ----------

function fetchIssueBody(issue) {
  if (!issue) return "";
  const r = sh("gh", [
    "issue",
    "view",
    issue,
    "--json",
    "title,body",
    "--jq",
    '.title + "\\n\\n" + .body',
  ]);
  if (r.code !== 0) {
    console.error(
      `[codex-review-companion] WARN: gh issue view ${issue} failed; continuing without issue body`,
    );
    return "";
  }
  return r.stdout;
}

// ---------- prompt loading ----------

function loadPromptTemplate(args) {
  const skillName = `codex-app-${args.reviewType}-review`;
  const dir = path.join(HOME, ".claude", "skills", skillName, "templates");
  const promptFile = isReReview(args)
    ? path.join(dir, "prompt-rework.txt")
    : path.join(dir, "prompt.txt");
  if (!fs.existsSync(promptFile)) {
    fail(`Prompt template not found: ${promptFile}`);
  }
  return fs.readFileSync(promptFile, "utf8");
}

// ---------- placeholder substitution ----------

function findPlanFile(args) {
  if (args.planFile) return args.planFile;
  if (!args.issue) return null;
  const planDir = path.join(args.repoRoot, "docs", "plans");
  if (!fs.existsSync(planDir)) return null;
  const candidates = fs
    .readdirSync(planDir)
    .filter(
      (f) =>
        f.includes(`-${args.issue}-`) || f.startsWith(`issue-${args.issue}`),
    );
  if (candidates.length === 0) return null;
  return path.join(planDir, candidates[0]);
}

function readSafe(p) {
  try {
    return fs.readFileSync(p, "utf8");
  } catch (e) {
    return null;
  }
}

function substitute(template, args, ctx) {
  let s = template;
  const issueBody = ctx.issueBody || "[issue body unavailable]";
  const planFileForReview = findPlanFile(args);
  const planContentForReview = planFileForReview
    ? readSafe(planFileForReview) ||
      `[plan file ${planFileForReview} unreadable]`
    : "[no plan file found in docs/plans/]";
  // Common substitutions
  s = s.replaceAll("#ISSUE", args.issue ? `#${args.issue}` : "#UNKNOWN");
  s = s.replaceAll("#PRNUMBER", args.pr ? `#${args.pr}` : "#UNKNOWN");
  s = s.replaceAll("[paste issue body]", issueBody);
  s = s.replaceAll("[paste plan content if present]", planContentForReview);

  // plan-review specific
  if (args.reviewType === "plan") {
    const planFile = findPlanFile(args);
    const planContent = planFile
      ? readSafe(planFile) || `[plan file ${planFile} unreadable]`
      : "[no plan file found in docs/plans/]";
    s = s.replaceAll("[paste plan content]", planContent);
  }

  // plan/code re-review specific
  if (isReReview(args)) {
    s = s.replaceAll("[paste rework items]", args.reworkItems);
    const baseline = args._reviewBaselineHead || args.previousHead;
    const r = sh("git", ["diff", `${baseline}...HEAD`, "--name-only"], {
      cwd: args.repoRoot,
    });
    const reworkFiles = r.code === 0 ? r.stdout.trim() : "(unknown)";
    s = s.replaceAll("[REWORK_FILES]", reworkFiles);
    s = s.replaceAll("[PREVIOUS_REVIEW_HEAD]", baseline || "(missing)");
    s = s.replaceAll("[DELTA_SCOPE]", args._deltaScope?.scope || "unknown");
    s = s.replaceAll(
      "[DELTA_SCOPE_DETAIL]",
      args._deltaScope?.control_plane_only
        ? "non-product-only: validate the named proof/process delta; do not reopen unchanged product behavior"
        : "product or unknown: inspect the changed runtime path and only the minimum directly affected seams",
    );
  }

  // qa-review specific
  if (args.reviewType === "qa") {
    const qaPath =
      args.qaReport || (args.issue ? `/tmp/qa-report-${args.issue}.md` : null);
    let qa = qaPath && fs.existsSync(qaPath) ? readSafe(qaPath) : null;
    // Issue #4208: also pull ALL QA-report comments from the PR (in
    // chronological order) so supplemental reports posted after the initial
    // qa-tester output are visible to Codex. Previously this code only
    // honored the local qaPath file, so PR-posted supplements were dropped.
    let prQaReports = [];
    if (args.pr) {
      const r = sh("gh", [
        "pr",
        "view",
        args.pr,
        "--json",
        "comments",
        "--jq",
        // Match QA report markers (broader than the brief regex below). Keep
        // chronological order — earlier reports first, supplements later.
        '.comments | map(select(.body | test("QA Report|QA report|qa-report|Health Score|Verdict:|VERDICT:|## QA|Browser QA|Manual QA|Supplement"; "i"))) | map(.body) | join("\n\n---\n\n")',
      ]);
      if (r.code === 0 && r.stdout.trim()) {
        const joined = r.stdout.trim();
        if (joined) prQaReports.push(joined);
      }
    }
    const qaParts = [];
    if (qa) qaParts.push(`# Local QA report (${qaPath})\n\n${qa}`);
    if (prQaReports.length) {
      qaParts.push(
        `# PR-posted QA reports (chronological, includes supplements)\n\n${prQaReports.join("\n\n")}`,
      );
    }
    const qaCombined = qaParts.length
      ? qaParts.join("\n\n===\n\n")
      : "[QA report not found]";
    info(
      args,
      `QA inputs: local=${qa ? qa.length : 0}chars pr_reports=${prQaReports.length ? prQaReports[0].length : 0}chars combined=${qaCombined.length}chars`,
    );
    s = s.replaceAll(
      "[paste full contents of /tmp/qa-report-ISSUE.md]",
      qaCombined,
    );
    const briefPath = args.qaBriefFile;
    let brief = "[QA brief not provided]";
    if (briefPath && fs.existsSync(briefPath))
      brief = readSafe(briefPath) || brief;
    else if (args.pr) {
      // try to fetch most recent PM-authored brief from PR comments
      const r = sh("gh", [
        "pr",
        "view",
        args.pr,
        "--json",
        "comments",
        "--jq",
        '.comments | reverse | map(select(.body | test("QA Brief|qa brief|Test scenarios|Mandatory checks"; "i"))) | first | .body // ""',
      ]);
      if (r.code === 0 && r.stdout.trim()) brief = r.stdout.trim();
    }
    s = s.replaceAll("[paste QA brief from PR comments]", brief);
    // file list
    if (args.pr) {
      const r = sh("gh", ["pr", "diff", args.pr, "--name-only"]);
      if (r.code === 0)
        s = s.replaceAll(
          "[paste file list from gh pr diff --name-only — NOT full diff]",
          r.stdout.trim(),
        );
    }
  }

  // arch-review specific
  if (args.reviewType === "arch" && args.focusText) {
    // arch prompt has free-form structure; append focus-text via a marker the prompt uses
    if (s.includes("[paste root cause analysis]"))
      s = s.replaceAll("[paste root cause analysis]", args.focusText);
  }

  return s;
}

// ---------- codex invocation (v1.1 — app-server JSON-RPC over stdio) ----------
//
// Mirrors upstream `~/.claude/plugins/marketplaces/openai-codex/plugins/codex/
// scripts/lib/{app-server,codex}.mjs` wire format. Self-contained: does not
// import upstream lib paths (they may not exist on every host).
//
// Protocol summary:
//   stdin/stdout = line-delimited JSON-RPC (one message per line, terminated \n).
//   request shape:  {id, method, params}
//   response shape: {id, result} or {id, error:{code,message}}
//   notification:   {method, params}  (no id)
//
// Sequence:
//   → request initialize {clientInfo, capabilities}
//   → notify initialized {}
//   → request thread/start {cwd, model, approvalPolicy:"never",
//                           sandbox:"read-only", serviceName,
//                           ephemeral:true, experimentalRawEvents:false}
//                                                  → {thread:{id}}
//   → request turn/start  {threadId, input:[{type:"text",text,text_elements:[]}],
//                          model, effort, outputSchema:null}
//                                                  → {turn:{id, status}}
//   ← notifications: thread/started, turn/started, item/started, item/completed,
//                    turn/completed, error
//
// Final message: the `agentMessage` thread item with `phase:"final_answer"` is
// the canonical review output. Some turns also emit a `turn/completed`
// notification with the same text. We capture the last completed agentMessage
// text on the root thread and return it as finalText.

const APP_SERVER_SERVICE_NAME = "heydonna_codex_review_companion";
const APP_SERVER_CLIENT_INFO = {
  title: "HeyDonna Codex Review Companion",
  name: "codex-review-companion",
  version: "1.1.0",
};
const APP_SERVER_CAPABILITIES = {
  experimentalApi: false,
  // Suppress streaming-delta notifications we don't need; cuts stdout chatter.
  optOutNotificationMethods: [
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/reasoning/textDelta",
  ],
};

class CodexAppServerClient {
  constructor(args) {
    this.args = args;
    this.proc = null;
    this.rl = null;
    this.pending = new Map(); // id -> {resolve, reject, method}
    this.nextId = 1;
    this.stderr = "";
    this.closed = false;
    this.exitResolved = false;
    this.exitError = null;
    this.notificationHandler = null;
    this.exitPromise = new Promise((resolve) => {
      this._resolveExit = resolve;
    });
  }

  spawn() {
    const codexArgs = CODEX_SERVICE_TIER
      ? ["-c", `service_tier="${CODEX_SERVICE_TIER}"`, "app-server"]
      : ["app-server"];
    info(this.args, `spawning ${CODEX_BIN} ${codexArgs.join(" ")}`);
    this.proc = spawn(CODEX_BIN, codexArgs, {
      cwd: this.args.repoRoot,
      env: { ...process.env, NO_COLOR: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc.stdout.setEncoding("utf8");
    this.proc.stderr.setEncoding("utf8");
    this.proc.stderr.on("data", (chunk) => {
      this.stderr += chunk;
    });
    this.proc.on("error", (err) => this._handleExit(err));
    this.proc.on("exit", (code, signal) => {
      const detail =
        code === 0
          ? null
          : new Error(
              `codex app-server exited unexpectedly (${signal ? `signal ${signal}` : `exit ${code}`}). STDERR:\n${this.stderr.slice(-2000)}`,
            );
      this._handleExit(detail);
    });
    this.rl = readline.createInterface({ input: this.proc.stdout });
    this.rl.on("line", (line) => this._handleLine(line));
  }

  _handleLine(line) {
    if (!line.trim()) return;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch (e) {
      // Non-JSON lines (rare) — log to stderr trail and ignore.
      this.stderr += `[non-json stdout line] ${line}\n`;
      return;
    }
    if (
      msg.id !== undefined &&
      (msg.result !== undefined || msg.error !== undefined)
    ) {
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      if (msg.error) {
        const err = new Error(
          msg.error.message || `app-server ${p.method} failed`,
        );
        err.rpcCode = msg.error.code;
        err.data = msg.error.data;
        p.reject(err);
      } else {
        p.resolve(msg.result || {});
      }
      return;
    }
    // Server-initiated request — reply method-not-found per upstream behavior.
    if (msg.id !== undefined && msg.method) {
      this._send({
        id: msg.id,
        error: {
          code: -32601,
          message: `Unsupported server request: ${msg.method}`,
        },
      });
      return;
    }
    // Notification.
    if (msg.method && this.notificationHandler) {
      try {
        this.notificationHandler(msg);
      } catch (e) {
        info(this.args, `notification handler error: ${e.message}`);
      }
    }
  }

  _handleExit(err) {
    if (this.exitResolved) return;
    this.exitResolved = true;
    this.exitError = err || null;
    for (const p of this.pending.values()) {
      p.reject(
        this.exitError || new Error("codex app-server connection closed"),
      );
    }
    this.pending.clear();
    this._resolveExit();
  }

  _send(message) {
    if (!this.proc || !this.proc.stdin || this.proc.stdin.destroyed) {
      throw new Error("codex app-server stdin is not available");
    }
    this.proc.stdin.write(`${JSON.stringify(message)}\n`);
  }

  request(method, params) {
    if (this.closed) {
      return Promise.reject(new Error("codex app-server client is closed"));
    }
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, method });
      try {
        this._send({ id, method, params });
      } catch (e) {
        this.pending.delete(id);
        reject(e);
      }
    });
  }

  notify(method, params = {}) {
    if (this.closed) return;
    try {
      this._send({ method, params });
    } catch {}
  }

  setNotificationHandler(h) {
    this.notificationHandler = h;
  }

  async close() {
    if (this.closed) {
      // Already closing; still wait for proc exit (with hard fallback).
      await Promise.race([
        this.exitPromise,
        new Promise((r) => setTimeout(r, 3000)),
      ]);
      return;
    }
    this.closed = true;
    try {
      if (this.rl) this.rl.close();
    } catch {}
    try {
      if (this.proc && !this.proc.killed) {
        try {
          this.proc.stdin.end();
        } catch {}
        // Escalate to SIGTERM after 200ms, SIGKILL after 1500ms.
        setTimeout(() => {
          if (this.proc && !this.proc.killed) {
            try {
              this.proc.kill("SIGTERM");
            } catch {}
          }
        }, 200);
        setTimeout(() => {
          if (this.proc && !this.proc.killed) {
            try {
              this.proc.kill("SIGKILL");
            } catch {}
          }
        }, 1500);
      }
    } catch {}
    // Hard fallback: never block forever waiting for exitPromise.
    await Promise.race([
      this.exitPromise,
      new Promise((r) => setTimeout(r, 3000)),
    ]);
    // If exit still hasn't resolved (extremely rare), force resolve so callers
    // who await exitPromise don't hang.
    if (!this.exitResolved) {
      this.exitResolved = true;
      this._resolveExit();
    }
  }
}

function invokeCodex(prompt, args) {
  const client = new CodexAppServerClient(args);
  return new Promise(async (resolve, reject) => {
    const startedAt = Date.now();
    let phase = "starting";
    let heartbeat = null;
    let signalHandler = null;
    const setPhase = (nextPhase) => {
      phase = nextPhase;
      console.error(
        `[codex-review-companion] phase=${phase} elapsed_ms=${Date.now() - startedAt} internal_timeout_ms=${CODEX_TIMEOUT_MS} required_outer_timeout_ms=${requiredCallerTimeoutMs()}`,
      );
    };
    const cleanupLifecycle = () => {
      if (heartbeat) clearInterval(heartbeat);
      if (signalHandler) {
        process.removeListener("SIGTERM", signalHandler);
        process.removeListener("SIGINT", signalHandler);
      }
    };

    signalHandler = async (signal) => {
      console.error(
        `[codex-review-companion] ${terminationDiagnostic({ signal, phase, elapsedMs: Date.now() - startedAt })}`,
      );
      cleanupLifecycle();
      try {
        await client.close();
      } catch {}
      process.exit(signal === "SIGTERM" ? 143 : 130);
    };
    process.once("SIGTERM", signalHandler);
    process.once("SIGINT", signalHandler);
    heartbeat = setInterval(() => {
      console.error(
        `[codex-review-companion] heartbeat phase=${phase} elapsed_ms=${Date.now() - startedAt}`,
      );
    }, HEARTBEAT_INTERVAL_MS);
    heartbeat.unref?.();

    let timer = setTimeout(async () => {
      phase = "internal_timeout";
      try {
        await client.close();
      } catch {}
      cleanupLifecycle();
      reject(
        new Error(
          `CODEX_REVIEW_STALLED: Codex app-server produced no terminal review within ${CODEX_TIMEOUT_MS}ms; do not retry automatically`,
        ),
      );
    }, CODEX_TIMEOUT_MS);

    try {
      setPhase("spawn");
      client.spawn();

      // 1. initialize
      setPhase("initialize");
      await client.request("initialize", {
        clientInfo: APP_SERVER_CLIENT_INFO,
        capabilities: APP_SERVER_CAPABILITIES,
      });
      client.notify("initialized", {});
      info(args, `app-server initialized`);

      // 2. thread/start
      setPhase("thread_start");
      const threadResp = await client.request("thread/start", {
        cwd: args.repoRoot,
        model: args.model || null,
        approvalPolicy: "never",
        sandbox: "read-only",
        serviceName: APP_SERVER_SERVICE_NAME,
        ephemeral: true,
        experimentalRawEvents: false,
      });
      const threadId = threadResp?.thread?.id;
      if (!threadId) {
        throw new Error(
          `thread/start returned no thread id: ${JSON.stringify(threadResp).slice(0, 500)}`,
        );
      }
      info(args, `thread/start id=${threadId} (prompt ${prompt.length} chars)`);

      // 3. listen for notifications, capture final agentMessage text
      let lastFinalAnswer = "";
      let lastAgentMessage = "";
      let finalAnswerSeen = false;
      let turnCompleted = false;
      let turnError = null;
      const activeSubagentTurns = new Set();
      let pendingCollab = new Set();
      let inferredCompletionTimer = null;
      const onComplete = () => {
        if (turnCompleted) return;
        turnCompleted = true;
        // Resolve via the closure below — we set a flag the turn-loop checks.
        if (resolveTurn) resolveTurn();
      };
      const scheduleInferred = () => {
        if (turnCompleted || !finalAnswerSeen) return;
        if (pendingCollab.size > 0 || activeSubagentTurns.size > 0) return;
        if (inferredCompletionTimer) clearTimeout(inferredCompletionTimer);
        inferredCompletionTimer = setTimeout(() => {
          if (
            !turnCompleted &&
            finalAnswerSeen &&
            pendingCollab.size === 0 &&
            activeSubagentTurns.size === 0
          ) {
            info(args, `turn completion inferred (final_answer + drain)`);
            onComplete();
          }
        }, 500);
        inferredCompletionTimer.unref?.();
      };

      let resolveTurn;
      const turnDone = new Promise((r) => {
        resolveTurn = r;
      });

      client.setNotificationHandler((msg) => {
        const m = msg.method;
        const p = msg.params || {};
        const itemThreadId = p.threadId ?? null;
        if (m === "turn/started") {
          if (itemThreadId && itemThreadId !== threadId) {
            activeSubagentTurns.add(itemThreadId);
          }
        } else if (m === "turn/completed") {
          if (itemThreadId && itemThreadId !== threadId) {
            activeSubagentTurns.delete(itemThreadId);
            scheduleInferred();
            return;
          }
          // Root turn complete.
          if (p.turn && p.turn.status && p.turn.status !== "completed") {
            turnError = new Error(`turn ended with status=${p.turn.status}`);
          }
          onComplete();
        } else if (m === "item/started" || m === "item/completed") {
          const item = p.item || {};
          const lifecycle = m === "item/started" ? "started" : "completed";
          if (item.type === "collabAgentToolCall") {
            if (lifecycle === "started" || item.status === "inProgress") {
              pendingCollab.add(item.id);
            } else if (lifecycle === "completed") {
              pendingCollab.delete(item.id);
              scheduleInferred();
            }
          }
          if (
            item.type === "agentMessage" &&
            lifecycle === "completed" &&
            (!itemThreadId || itemThreadId === threadId)
          ) {
            const text = item.text || "";
            if (text) {
              lastAgentMessage = text;
              if (item.phase === "final_answer") {
                lastFinalAnswer = text;
                finalAnswerSeen = true;
                scheduleInferred();
              }
            }
          }
        } else if (m === "error") {
          turnError = new Error(
            p?.error?.message || "codex app-server error notification",
          );
        }
      });

      // 4. turn/start
      setPhase("turn_start");
      const turnResp = await client.request("turn/start", {
        threadId,
        input: [{ type: "text", text: prompt, text_elements: [] }],
        model: args.model || null,
        effort: args.effort || null,
        outputSchema: null,
      });
      info(args, `turn/start id=${turnResp?.turn?.id ?? "?"}`);
      // If the server already returned a non-inProgress status synchronously,
      // mark complete immediately.
      if (turnResp?.turn?.status && turnResp.turn.status !== "inProgress") {
        if (turnResp.turn.status !== "completed") {
          turnError = new Error(`turn status=${turnResp.turn.status}`);
        }
        onComplete();
      }

      // 5. wait for turn done (or error / exit)
      setPhase("reviewing");
      const exitGuard = client.exitPromise.then(() => {
        if (!turnCompleted) {
          throw (
            client.exitError ||
            new Error("app-server exited before turn completed")
          );
        }
      });
      // Swallow exitGuard rejection if turnDone wins — prevents unhandled rejection.
      exitGuard.catch(() => {});
      await Promise.race([turnDone, exitGuard]);

      clearTimeout(timer);
      if (inferredCompletionTimer) clearTimeout(inferredCompletionTimer);
      setPhase("finalizing");

      info(
        args,
        `turn loop exited: turnCompleted=${turnCompleted} finalAnswerSeen=${finalAnswerSeen} lastFinalLen=${lastFinalAnswer.length} lastAgentLen=${lastAgentMessage.length}`,
      );

      const finalText = (lastFinalAnswer || lastAgentMessage || "").trim();
      if (turnError && !finalText) {
        await client.close();
        cleanupLifecycle();
        reject(turnError);
        return;
      }
      if (!finalText) {
        await client.close();
        cleanupLifecycle();
        reject(
          new Error(
            `Codex app-server returned empty final message. STDERR:\n${client.stderr.slice(-2000)}`,
          ),
        );
        return;
      }

      await client.close();
      cleanupLifecycle();
      resolve({ finalText, stdout: "", stderr: client.stderr });
    } catch (e) {
      clearTimeout(timer);
      try {
        await client.close();
      } catch {}
      cleanupLifecycle();
      reject(e);
    }
  });
}

// ---------- verdict parsing ----------

const BLOCKER_CLASSES = new Set([
  "DIRECTIVE",
  "CONTROL_POINT",
  "REACHABILITY",
  "SCOPE",
  "IMPLEMENTATION",
  "PROOF",
]);

const BLOCKING_REVIEW_VERDICTS = new Set([
  "REJECT",
  "MISDIAGNOSED",
  "REQUEST_CHANGES",
  "REVISE",
  "NEEDS_REVISION",
  "NEEDS_DEEPER_INVESTIGATION",
]);

function blockerClassFor(description) {
  const text = String(description || "").toLowerCase();
  if (/directive|requirement|fidelity/.test(text)) return "DIRECTIVE";
  if (/control point|runtime owner|owning boundary/.test(text))
    return "CONTROL_POINT";
  if (/reachab|production path|entrypoint|writer/.test(text))
    return "REACHABILITY";
  if (/scope|split|rescope|issue contract/.test(text)) return "SCOPE";
  if (/proof|test|assert|red.on.revert|fixture|harness/.test(text))
    return "PROOF";
  return "IMPLEMENTATION";
}

function blockerFingerprint(blocker) {
  const canonical = [
    blocker.blockerClass,
    blocker.blockerId,
    blocker.description || "",
  ]
    .join("|")
    .toLowerCase()
    .replace(/\b[0-9a-f]{7,40}\b/g, "<sha>")
    .replace(/\b\d+\b/g, "<n>")
    .replace(/\s+/g, " ")
    .trim();
  return crypto.createHash("sha256").update(canonical).digest("hex");
}

function parseBlockers(text, findings, verdict) {
  const blockers = [];
  let current = null;
  const flush = () => {
    if (!current?.blockerId) return;
    current.blockerClass = BLOCKER_CLASSES.has(current.blockerClass)
      ? current.blockerClass
      : blockerClassFor(current.description);
    current.status = ["OPEN", "RESOLVED", "SUPERSEDED"].includes(current.status)
      ? current.status
      : "OPEN";
    current.fingerprint = /^[0-9a-f]{64}$/.test(current.fingerprint || "")
      ? current.fingerprint
      : blockerFingerprint(current);
    blockers.push(current);
    current = null;
  };

  for (const raw of String(text || "").split(/\r?\n/)) {
    const match = raw.match(
      /^\s*(BLOCKER_ID|BLOCKER_CLASS|BLOCKER_FINGERPRINT|BLOCKER_STATUS|BLOCKER_ORIGIN|BLOCKER_REASON)\s*:\s*(.+?)\s*$/i,
    );
    if (!match) continue;
    const key = match[1].toUpperCase();
    const value = match[2].trim().replace(/^`|`$/g, "");
    if (key === "BLOCKER_ID") {
      flush();
      current = {
        blockerId: value.toUpperCase(),
        blockerClass: "",
        fingerprint: "",
        status: "",
        origin: "",
        description: "",
      };
      continue;
    }
    if (!current) continue;
    if (key === "BLOCKER_CLASS") current.blockerClass = value.toUpperCase();
    if (key === "BLOCKER_FINGERPRINT")
      current.fingerprint = value.toLowerCase();
    if (key === "BLOCKER_STATUS") current.status = value.toUpperCase();
    if (key === "BLOCKER_ORIGIN") current.origin = value.toLowerCase();
    if (key === "BLOCKER_REASON") current.description = value;
  }
  flush();

  if (blockers.length || !BLOCKING_REVIEW_VERDICTS.has(verdict))
    return blockers;

  // Legacy fallback. It keeps old reviewers observable, but the origin makes
  // clear that this is not a mechanically stable reviewer-emitted identity.
  return findings.map((finding, index) => {
    const blockerClass = blockerClassFor(finding.description);
    const blocker = {
      blockerId: `${blockerClass}-${String(index + 1).padStart(3, "0")}`,
      blockerClass,
      fingerprint: "",
      status: "OPEN",
      origin: "legacy_synthesized",
      description: finding.description,
    };
    blocker.fingerprint = blockerFingerprint(blocker);
    return blocker;
  });
}

function parseVerdict(reviewType, text) {
  const findings = [];
  let verdict = null;
  let effort = null;
  let admissionDisposition = null;
  let admissionReason = null;
  let runtimeControlPoint = null;
  let passScope = null;
  let readinessCeiling = null;
  let severityOverride = null;
  let severityOverrideReason = null;

  // Precedence (most-restrictive wins on collision)
  const order = {
    REJECT: 0,
    MISDIAGNOSED: 0,
    REQUEST_CHANGES: 1,
    REVISE: 1,
    NEEDS_REVISION: 1,
    NEEDS_DEEPER_INVESTIGATION: 1,
    APPROVE_PENDING_CI: 2,
    APPROVE: 2,
    CONFIRMED: 2,
    VERIFIED: 2,
  };
  const verdicts = Object.keys(order);
  // Look for a verdict heading first. Codex commonly emits variants such as
  // "VERDICT: REVISE", "**Verdict: REVISE**", and "1. Verdict: revise".
  const explicit = text.match(
    new RegExp(
      "^[\\s>*#_`-]*(?:\\d{1,2}[.)]\\s*)?(?:\\*\\*)?VERDICT(?:\\*\\*)?\\s*[:\\-]\\s*(?:\\*\\*|`)?(" +
        verdicts.join("|") +
        ")\\b",
      "im",
    ),
  );
  if (explicit) verdict = explicit[1].toUpperCase();
  // Otherwise scan all words and pick the most-restrictive
  if (!verdict) {
    for (const v of verdicts) {
      const re = new RegExp(`(^|\\W)${v}(\\W|$)`, "gi");
      if (re.test(text)) {
        if (verdict === null || order[v] < order[verdict]) {
          verdict = v;
        }
      }
    }
  }
  if (!verdict) verdict = "UNKNOWN";

  const effortM = text.match(
    /EFFORT[\s:]*\b(S|M|L|XL|none|minimal|low|medium|high|xhigh)\b/i,
  );
  if (effortM) effort = effortM[1].toUpperCase();

  // Ready Pool architecture reviews have a second decision axis. The general
  // architecture verdict answers whether the diagnosis is sound; admission
  // disposition answers what PM should do with the issue. Keep these separate
  // so MISDIAGNOSED does not accidentally become a CTO escalation.
  const admissionDispositions = [
    "ADMIT",
    "REPAIR_CONTRACT",
    "PARK_TRACKING",
    "PARK_DEPENDENCY",
    "DUPLICATE_OR_SUPERSEDED",
    "NEEDS_INVESTIGATION",
    "NEEDS_CTO",
  ];
  const admissionM = text.match(
    new RegExp(
      "^[\\s>*#_`-]*(?:\\*\\*)?ADMISSION_DISPOSITION(?:\\*\\*)?\\s*[:\\-]\\s*(?:\\*\\*|`)?(" +
        admissionDispositions.join("|") +
        ")\\b",
      "im",
    ),
  );
  if (admissionM) admissionDisposition = admissionM[1].toUpperCase();
  const admissionReasonM = text.match(
    /^[\s>*#_`-]*(?:\*\*)?ADMISSION_REASON(?:\*\*)?\s*[:\-]\s*(.+)$/im,
  );
  if (admissionReasonM) {
    admissionReason = admissionReasonM[1]
      .trim()
      .replace(/^\*\*|\*\*$/g, "")
      .slice(0, 1000);
  }

  const runtimeControlPointM = text.match(
    /^[\s>*#_`-]*(?:\*\*)?RUNTIME_CONTROL_POINT(?:\*\*)?\s*:\s*(.+)$/im,
  );
  if (runtimeControlPointM) {
    runtimeControlPoint = runtimeControlPointM[1]
      .trim()
      .replace(/^`|`$/g, "")
      .slice(0, 2000);
  }
  const passScopeM = text.match(
    /^[\s>*#_`-]*(?:\*\*)?PASS_SCOPE(?:\*\*)?\s*:\s*(phase-a|blocked)\b/im,
  );
  if (passScopeM) passScope = passScopeM[1].toLowerCase();
  const readinessCeilingM = text.match(
    /^[\s>*#_`-]*(?:\*\*)?READINESS_CEILING(?:\*\*)?\s*:\s*(.+)$/im,
  );
  if (readinessCeilingM) {
    readinessCeiling = readinessCeilingM[1]
      .trim()
      .replace(/^`|`$/g, "")
      .slice(0, 500);
  }
  const severityOverrideM = text.match(
    /^[\s>*#_`-]*(?:\*\*)?SEVERITY_OVERRIDE(?:\*\*)?\s*:\s*(P[01])\b/im,
  );
  if (severityOverrideM) severityOverride = severityOverrideM[1].toUpperCase();
  const severityOverrideReasonM = text.match(
    /^[\s>*#_`-]*(?:\*\*)?SEVERITY_OVERRIDE_REASON(?:\*\*)?\s*:\s*(.+)$/im,
  );
  if (severityOverrideReasonM) {
    severityOverrideReason = severityOverrideReasonM[1].trim().slice(0, 1000);
  }

  // Findings: P0/P1/P2 markers (code-review) — primary format
  const findingRe = /^[\s>*\-]*(P[012])[\s:.\-]+([^\n]+)$/gm;
  let m;
  while ((m = findingRe.exec(text)) !== null) {
    findings.push({
      priority: m[1].toUpperCase(),
      description: m[2].trim().slice(0, 500),
    });
  }

  // Plan reviews and some code reviews emit numbered "Required revisions" /
  // "Required changes" / "Issues" sections instead of P0/P1/P2 markers. Without
  // this fallback the JSON shows `findings: []` even when Codex returned a
  // detailed revision list — slots then misinterpret that as a companion
  // parsing failure and fall back to /zen-plan-review (Rajiv directive
  // 2026-05-12 16:24 IST thread `1778583286.581679`).
  if (
    findings.length === 0 &&
    (verdict === "REVISE" ||
      verdict === "REQUEST_CHANGES" ||
      verdict === "NEEDS_REVISION" ||
      verdict === "REJECT" ||
      verdict === "NEEDS_DEEPER_INVESTIGATION")
  ) {
    // Match lines like "1. Rename the test." or "1) Add the explicit ..." —
    // optionally preceded by markdown bullet/quote chars. Capture the first
    // line of the numbered item (description), trim trailing dots.
    const numberedRe = /^[\s>*\-]*(\d{1,2})[.)][\s]+(.+?)$/gm;
    let nm;
    while ((nm = numberedRe.exec(text)) !== null) {
      const desc = nm[2].trim().replace(/[.:]+$/, "");
      // Skip noise: very short items, citations like "10. " in a sentence,
      // and lines that look like list-item continuation (start with lowercase
      // and no verb-like first word). Heuristic — keep simple.
      if (desc.length < 6) continue;
      findings.push({
        priority: "P1",
        description: desc.slice(0, 500),
      });
    }
    // Cap defensively — Codex shouldn't emit >20 numbered items.
    if (findings.length > 20) findings.length = 20;
  }

  const blockers = parseBlockers(text, findings, verdict);
  const p2OnlyBlocking =
    reviewType === "code" &&
    BLOCKING_REVIEW_VERDICTS.has(verdict) &&
    findings.length > 0 &&
    findings.every((finding) => finding.priority === "P2") &&
    severityOverride !== "P1";

  // Exit code mapping
  const exitCode = {
    APPROVE: 0,
    CONFIRMED: 0,
    VERIFIED: 0,
    APPROVE_PENDING_CI: 0,
    REQUEST_CHANGES: 1,
    REVISE: 1,
    NEEDS_REVISION: 1,
    NEEDS_DEEPER_INVESTIGATION: 1,
    REJECT: 2,
    MISDIAGNOSED: 2,
    UNKNOWN: 3,
  }[verdict];

  return {
    verdict,
    effort,
    findings,
    exitCode,
    admissionDisposition,
    admissionReason,
    runtimeControlPoint,
    passScope,
    readinessCeiling,
    blockers,
    severityOverride,
    severityOverrideReason,
    p2OnlyBlocking,
  };
}

function verdictContractError(parsed) {
  if (!parsed?.p2OnlyBlocking) return null;
  return (
    "P2_ONLY_BLOCKING_VERDICT_INVALID: P2 findings are non-blocking. " +
    "Approve with bounded follow-ups, or emit SEVERITY_OVERRIDE: P1 and " +
    "SEVERITY_OVERRIDE_REASON with concrete runtime/release evidence."
  );
}

// ---------- marker file ----------

function writeMarker(args, parsed, codexText) {
  const ident = args.pr || args.issue || "unknown";
  const markerPath =
    args.markerFile || `/tmp/codex-app-${args.reviewType}-review-${ident}.txt`;
  const canonicalDir = "/tmp/codex-review-companion";
  const ts = Math.floor(Date.now() / 1000);
  const lines = [
    `VERDICT: ${parsed.verdict}`,
    `COMPANION_VERDICT: ${parsed.verdict}`,
    `FINAL_REVIEWER_VERDICT: ${parsed.verdict}`,
    `MARKER_PROVENANCE: codex-review-companion`,
    `TYPE: ${args.reviewType}-review`,
    `TIMESTAMP: ${ts}`,
    `ISSUE: ${args.issue ? "#" + args.issue : "-"}`,
    `PR: ${args.pr ? "#" + args.pr : "-"}`,
    args._currentBranch ? `BRANCH: ${args._currentBranch}` : null,
    args._currentHead ? `HEAD_SHA: ${args._currentHead}` : null,
    args._currentHead ? `headRefOid: ${args._currentHead}` : null,
    args._diffBaseRef ? `DIFF_BASE: ${args._diffBaseRef}` : null,
    args._prOwnedBase ? `PR_OWNED_BASE: ${args._prOwnedBase}` : null,
    args._diffHeadRef ? `DIFF_HEAD: ${args._diffHeadRef}` : null,
    args._reviewBaselineHead
      ? `PREVIOUS_REVIEW_HEAD: ${args._reviewBaselineHead}`
      : null,
    args._carryForwardProof ? "REVIEW_PROVENANCE_MODE: carry-forward" : null,
    args._carryForwardProof ? "MODEL_REVIEW_INVOKED: false" : null,
    args._carryForwardProof
      ? `SOURCE_REVIEW_MARKER: ${args._carryForwardProof.sourceMarker}`
      : null,
    args._carryForwardProof
      ? `SOURCE_REVIEW_SHA256: ${args._carryForwardProof.sourceMarkerSha256}`
      : null,
    args._carryForwardProof
      ? `SOURCE_REVIEW_HEAD: ${args._carryForwardProof.approvedHead}`
      : null,
    args._carryForwardProof
      ? `REVIEWED_BASE_HEAD: ${args._carryForwardProof.reviewedBaseHead}`
      : null,
    args._carryForwardProof
      ? `BRANCH_PRE_MERGE_HEAD: ${args._carryForwardProof.branchPreMergeHead}`
      : null,
    args._carryForwardProof
      ? `MAIN_PARENT_HEAD: ${args._carryForwardProof.mainParentHead}`
      : null,
    args._carryForwardProof
      ? `PRODUCT_PATCH_ID: ${args._carryForwardProof.productPatchId}`
      : null,
    args._carryForwardProof
      ? `PRODUCT_PATHS: ${args._carryForwardProof.productPaths.join(",")}`
      : null,
    args._carryForwardProof
      ? `CHANGE_SCOPE_RULES_SHA256: ${args._carryForwardProof.rulesSha256}`
      : null,
    args._rebaseBaseline ? `REBASE_BASELINE: ${args._rebaseBaseline}` : null,
    args.reviewOrdinal ? `REVIEW_ORDINAL: ${args.reviewOrdinal}` : null,
    args._sourceMode ? `SOURCE_MODE: ${args._sourceMode}` : null,
    args._baselineRef ? `BASELINE_REF: ${args._baselineRef}` : null,
    args._baselineSha ? `BASELINE_SHA: ${args._baselineSha}` : null,
    args.effort ? `REQUESTED_EFFORT: ${args.effort}` : null,
    parsed.effort ? `EFFORT: ${parsed.effort}` : null,
    parsed.admissionDisposition
      ? `ADMISSION_DISPOSITION: ${parsed.admissionDisposition}`
      : null,
    parsed.admissionReason
      ? `ADMISSION_REASON: ${parsed.admissionReason}`
      : null,
    parsed.runtimeControlPoint
      ? `runtime_control_point: ${parsed.runtimeControlPoint}`
      : null,
    parsed.passScope ? `pass_scope: ${parsed.passScope}` : null,
    parsed.readinessCeiling
      ? `readiness_ceiling: ${parsed.readinessCeiling}`
      : null,
    parsed.severityOverride
      ? `SEVERITY_OVERRIDE: ${parsed.severityOverride}`
      : null,
    parsed.severityOverrideReason
      ? `SEVERITY_OVERRIDE_REASON: ${parsed.severityOverrideReason}`
      : null,
    `--- Blockers (${parsed.blockers.length}) ---`,
    ...parsed.blockers.flatMap((blocker) => [
      `BLOCKER_ID: ${blocker.blockerId}`,
      `BLOCKER_CLASS: ${blocker.blockerClass}`,
      `BLOCKER_FINGERPRINT: ${blocker.fingerprint}`,
      `BLOCKER_STATUS: ${blocker.status}`,
      `BLOCKER_ORIGIN: ${blocker.origin || "reviewer"}`,
      `BLOCKER_REASON: ${blocker.description || "see review output"}`,
    ]),
    `--- Findings (${parsed.findings.length}) ---`,
    ...parsed.findings.map((f) => `${f.priority}: ${f.description}`),
    `--- Review Output ---`,
    codexText,
  ].filter(Boolean);
  fs.writeFileSync(markerPath, lines.join("\n"));
  try {
    fs.mkdirSync(canonicalDir, { recursive: true });
    const scopeIdent = [
      args.reviewType,
      args.pr ? `pr-${args.pr}` : null,
      args.issue ? `issue-${args.issue}` : null,
      args._currentHead ? args._currentHead.slice(0, 12) : null,
      String(ts),
    ]
      .filter(Boolean)
      .join("-");
    const canonicalPath = path.join(canonicalDir, `${scopeIdent}.md`);
    args._canonicalMarkerPath = canonicalPath;
    fs.writeFileSync(
      canonicalPath,
      [
        `CANONICAL_MARKER: ${canonicalPath}`,
        `LEGACY_MARKER: ${markerPath}`,
        ...lines,
        "",
      ].join("\n"),
    );
  } catch (err) {
    console.error(
      `[codex-review-companion] WARN: failed to write canonical marker: ${err?.message || err}`,
    );
  }
  return markerPath;
}

// ---------- main ----------

async function main() {
  const args = parseArgs(process.argv.slice(2));

  if (!args.reviewType || !REVIEW_TYPES.has(args.reviewType)) {
    fail(`--review-type required, one of: ${[...REVIEW_TYPES].join(", ")}`);
  }
  const sourceError = sourceRequirementError(args);
  if (sourceError) fail(sourceError);
  if (!args.effort) {
    args.effort = DEFAULT_REVIEW_EFFORT;
  }

  let source;
  if (isIssueOnlyArchReview(args)) {
    info(
      args,
      `resolving issue-only architecture baseline for issue #${args.issue}`,
    );
    source = resolveIssueOnlyArchBaseline(args);
    info(
      args,
      `issue-only baseline: ${source.baselineRef}@${source.baselineSha.slice(0, 12)}`,
    );
  } else {
    info(
      args,
      `resolving diff for ${args.pr ? `PR #${args.pr}` : `branch ${args.branch}`}`,
    );
    const { diff, baseRef, headRef } = resolveDiff(args);
    source = { mode: "diff", diff, baseRef, headRef };
    info(args, `diff: ${diff.length} chars, base=${baseRef} head=${headRef}`);
  }

  const budgetAdmission = reviewBudgetPreflight(args);
  if (!budgetAdmission.allowed) {
    if (budgetAdmission.budget?.decision === "rescue_required") {
      reviewCapTerminal(args, budgetAdmission);
    }
    fail(budgetAdmission.message);
  }

  // A functionality-approved product delta does not need another model pass
  // merely because the branch later absorbed current main. Admit the prior
  // approval mechanically only when the branch-only descendant is non-product,
  // the live merge has the exact current-main parent, the product path set and
  // stable patch identity are unchanged, every review thread is resolved, and
  // the live head survives a final read-back. Any mismatch falls through to a
  // normal delta review; thread-read/head-drift failures stop before model use.
  const carryForward = reviewCarryForwardAdmission(
    args,
    budgetAdmission.budget,
  );
  if (carryForward.status === "blocked") {
    fail(
      `REVIEW_CARRY_FORWARD_BLOCKED head=${args._currentHead} reason=${carryForward.reason}; ` +
        "no model review was started",
      42,
    );
  }
  if (carryForward.status === "carry_forward") {
    args._carryForwardProof = carryForward;
    args._reviewBaselineHead = carryForward.approvedHead;
    args._prOwnedBase = carryForward.mainParentHead;
    args._diffBaseRef = carryForward.mainParentHead;
    const parsed = {
      verdict: "APPROVE",
      exitCode: 0,
      effort: null,
      admissionDisposition: "CARRY_FORWARD",
      admissionReason:
        "verified pure-main descendant preserves reviewed product delta",
      runtimeControlPoint: carryForward.runtimeControlPoint,
      passScope: carryForward.passScope,
      readinessCeiling: carryForward.readinessCeiling,
      blockers: [],
      findings: [],
      severityOverride: null,
      severityOverrideReason: null,
    };
    const markerPath = writeMarker(
      args,
      parsed,
      [
        "MECHANICAL_REVIEW_CARRY_FORWARD: PASS",
        `source_review_head=${carryForward.approvedHead}`,
        `current_head=${args._currentHead}`,
        `product_patch_id=${carryForward.productPatchId}`,
        "model_review_invoked=false",
      ].join("\n"),
    );
    const historyPublication = publishReviewHistory(args);
    if (!historyPublication.ok) fail(historyPublication.message);
    if (args.outputFormat === "json") {
      console.log(
        JSON.stringify(
          {
            verdict: "APPROVE",
            final_reviewer_verdict: "APPROVE",
            marker_provenance: "codex-review-companion",
            review_provenance_mode: "carry-forward",
            model_review_invoked: false,
            marker_file: markerPath,
            pr: args.pr,
            head_sha: args._currentHead,
            source_review_head: carryForward.approvedHead,
            product_patch_id: carryForward.productPatchId,
          },
          null,
          2,
        ),
      );
    } else {
      console.log("VERDICT: APPROVE");
      console.log("REVIEW_PROVENANCE_MODE: carry-forward");
      console.log("MODEL_REVIEW_INVOKED: false");
      console.log(`MARKER: ${markerPath}`);
    }
    return;
  }

  const reviewScope = automaticDeltaReview(
    args,
    source,
    budgetAdmission.budget,
  );
  if (reviewScope.mode === "same_head") {
    fail(
      `SAME_HEAD_REVIEW_SUPPRESSED type=${args.reviewType} head=${args._currentHead}. ` +
        "Reuse the durable exact-head marker; no Codex invocation was started.",
      42,
    );
  }
  source = reviewScope.source;
  if (isReReview(args) && !args._deltaScope) {
    args._deltaScope = classifyDelta(
      args,
      args._reviewBaselineHead,
      source.headRef,
    );
  }
  info(
    args,
    `review mode=${reviewScope.mode} delta_scope=${args._deltaScope?.scope || "not_applicable"}`,
  );

  // Fetch issue body
  const issueBody = fetchIssueBody(args.issue);

  // Load + substitute prompt
  const template = loadPromptTemplate(args);
  const promptBody = substitute(template, args, { issueBody });

  const fullPrompt = appendPriorBlockerLedger(
    appendTransitionMarkerContract(
      composeReviewPrompt(promptBody, source),
      args,
    ),
    budgetAdmission.budget,
    args._currentHead,
  );
  info(args, `prompt size: ${fullPrompt.length} chars`);

  // Invoke Codex
  let codexResult;
  try {
    codexResult = await invokeCodex(fullPrompt, args);
  } catch (e) {
    releasePlanReviewReservation(args);
    fail(`Codex invocation failed: ${e.message}`);
  }

  // Parse verdict
  const parsed = parseVerdict(args.reviewType, codexResult.finalText);
  const contractError = verdictContractError(parsed);
  if (contractError) {
    releasePlanReviewReservation(args);
    fail(contractError);
  }
  const carryForwardError = validatePriorBlockerCarryForward(
    parsed,
    budgetAdmission.budget,
    args._currentHead,
  );
  if (carryForwardError) {
    releasePlanReviewReservation(args);
    fail(carryForwardError);
  }

  // Write marker
  const markerPath = writeMarker(args, parsed, codexResult.finalText);
  const historyPublication = publishReviewHistory(args);
  if (!historyPublication.ok) fail(historyPublication.message);

  // Output
  if (args.outputFormat === "json") {
    console.log(
      JSON.stringify(
        {
          verdict: parsed.verdict,
          companion_verdict: parsed.verdict,
          final_reviewer_verdict: parsed.verdict,
          marker_provenance: "codex-review-companion",
          effort: parsed.effort,
          requested_effort: args.effort || null,
          admission_disposition: parsed.admissionDisposition,
          admission_reason: parsed.admissionReason,
          runtime_control_point: parsed.runtimeControlPoint,
          pass_scope: parsed.passScope,
          readiness_ceiling: parsed.readinessCeiling,
          findings: parsed.findings,
          blockers: parsed.blockers,
          severity_override: parsed.severityOverride,
          severity_override_reason: parsed.severityOverrideReason,
          marker_file: markerPath,
          review_type: args.reviewType,
          pr: args.pr,
          issue: args.issue,
          branch: args._currentBranch || args.branch || null,
          head_sha: args._currentHead || null,
          diff_base: args._diffBaseRef || null,
          pr_owned_base: args._prOwnedBase || null,
          diff_head: args._diffHeadRef || null,
          previous_review_head: args._reviewBaselineHead || null,
          source_mode: args._sourceMode || "diff",
          baseline_ref: args._baselineRef || null,
          baseline_sha: args._baselineSha || null,
        },
        null,
        2,
      ),
    );
  } else {
    console.log(`VERDICT: ${parsed.verdict}`);
    if (args.effort) console.log(`REQUESTED_EFFORT: ${args.effort}`);
    if (parsed.effort) console.log(`EFFORT: ${parsed.effort}`);
    if (parsed.admissionDisposition)
      console.log(`ADMISSION_DISPOSITION: ${parsed.admissionDisposition}`);
    if (parsed.admissionReason)
      console.log(`ADMISSION_REASON: ${parsed.admissionReason}`);
    console.log(`MARKER: ${markerPath}`);
    if (parsed.findings.length) {
      console.log(`FINDINGS (${parsed.findings.length}):`);
      for (const f of parsed.findings)
        console.log(`  ${f.priority}: ${f.description}`);
    }
    if (parsed.blockers.length) {
      console.log(`BLOCKERS (${parsed.blockers.length}):`);
      for (const blocker of parsed.blockers)
        console.log(
          `  ${blocker.blockerId} ${blocker.status} ${blocker.blockerClass}: ${blocker.description}`,
        );
    }
    console.log("");
    console.log("--- Codex output ---");
    console.log(codexResult.finalText);
  }

  process.exit(parsed.exitCode);
}

process.on("unhandledRejection", (reason) => {
  const msg =
    reason instanceof Error ? reason.stack || reason.message : String(reason);
  console.error(`[codex-review-companion] UNHANDLED REJECTION: ${msg}`);
  process.exit(3);
});
process.on("uncaughtException", (err) => {
  console.error(
    `[codex-review-companion] UNCAUGHT: ${err.stack || err.message}`,
  );
  process.exit(3);
});

// Export parseVerdict for contract tests. Tests import this function to
// guarantee the verdict↔findings invariant (REVISE/REJECT/REQUEST_CHANGES/
// NEEDS_REVISION/NEEDS_DEEPER_INVESTIGATION with numbered "Required revisions"
// produces findings.length >= 1) holds across edits. Without an exported
// hook, contract tests would have to spawn the CLI and parse stdout, which
// is slow + brittle.
export {
  automaticDeltaReview,
  appendPriorBlockerLedger,
  appendTransitionMarkerContract,
  bindReReviewBaseline,
  classifyDelta,
  composeReviewPrompt,
  exactHeadOverrideReviewAdmission,
  exactQaPrHeadBinding,
  isIssueOnlyArchReview,
  isReReview,
  issueFromBranchName,
  latestReviewedHead,
  latestApprovedReviewEvent,
  liveReviewThreadState,
  parseVerdict,
  planReviewBudgetPreflight,
  priorBlockerLedger,
  prOwnedDiffBase,
  publishReviewHistory,
  reviewCarryForwardAdmission,
  reviewBudgetPreflight,
  runReviewBudget,
  releasePlanReviewReservation,
  requiredCallerTimeoutMs,
  resolveIssueFromPrContext,
  sourceRequirementError,
  terminationDiagnostic,
  validatePriorBlockerCarryForward,
  verdictContractError,
  writeReviewCapPacket,
  writeMarker,
};

// Only run main() when invoked directly as a CLI (not when imported by tests).
import { fileURLToPath } from "node:url";
const __isCli =
  process.argv[1] && process.argv[1] === fileURLToPath(import.meta.url);
if (__isCli) {
  main().catch((e) => {
    console.error(`[codex-review-companion] FATAL: ${e.stack || e.message}`);
    process.exit(3);
  });
}
