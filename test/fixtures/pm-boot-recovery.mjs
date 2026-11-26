// Isolated operational fixture: build dist, then run this file with MoP's Node.
// Uses real tmux pane lookup/split on a private socket and an in-memory DB.
// A simulated shell and Claude composer record writes; no real PM launcher runs.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { MoPDatabase } from "../../dist/db.js";
import { ProcessHealthChecker } from "../../dist/health.js";
import { expectedCheckoutPath } from "../../dist/paneIdentity.js";
import { TmuxRelay } from "../../dist/relay.js";
import { DEFAULT_CONFIG } from "../../dist/types.js";
import { execShell } from "../../dist/asyncCommand.js";

const home = mkdtempSync("/tmp/mop-pm-boot-fixture-");
const cwd = expectedCheckoutPath(0);
const escape = (value) => `'${value.replace(/'/g, "'\\''")}'`;
const tmux = (args, options) => execShell(`HOME=${escape(home)} TMUX= tmux -S ${escape(join(home, "socket"))} -f /dev/null ${args}`, options);
const panes = async () => (await tmux("list-panes -t '=0:0' -F '#{pane_index}|#{pane_id}|#{pane_pid}|#{pane_dead}'"))
  .stdout.trim().split("\n").map((line) => line.split("|"));
const config = { ...DEFAULT_CONFIG, dbPath: ":memory:", pmPaneAddress: "0:0.0" };
const db = new MoPDatabase(config);
const message = "isolated PM recovery canary";
const event = "freeform-pm-boot-fixture";
const rule = "─".repeat(40);
const screen = (stdout) => ({ stdout, stderr: "", code: 0 });
let sibling, pm, typed, loaded, composer;
let phase = "missing", launches = 0, pastes = 0, submissions = 0;

const runShell = async (command, options) => {
  if (command.startsWith("git -C ")) return execShell(command, options);
  assert.match(command, /^tmux /);
  if (command.startsWith("tmux capture-pane")) {
    assert.ok(pm && command.includes(`-t ${pm} `), "all composer reads must target the new PM only");
    if (phase === "shell") return screen(typed ? `$ ${typed}\n` : "$ \n");
    return screen(phase === "ready" ? `${rule}\n❯ ${composer ?? ""}\n${rule}\n` : "Resuming…\n");
  }
  if (command.startsWith("tmux send-keys -l")) {
    assert.equal(phase, "shell");
    const prefix = `tmux send-keys -l -t ${pm} `;
    assert.ok(command.startsWith(prefix));
    typed = command.slice(prefix.length + 1, -1).replace(/'\\''/g, "'");
    assert.match(typed, /launch-pm\.sh .* --continue$/);
    return screen("");
  }
  if (command.startsWith("tmux load-buffer")) {
    assert.equal(phase, "ready");
    loaded = readFileSync(/^tmux load-buffer -b \S+ '([^']+)'$/.exec(command)[1], "utf8");
    return screen("");
  }
  if (command.startsWith("tmux paste-buffer")) {
    assert.equal(phase, "ready");
    assert.match(command, new RegExp(`-t ${pm} -d$`));
    composer = loaded;
    pastes++;
    return screen("");
  }
  if (command.startsWith("tmux send-keys")) {
    assert.equal(command, `tmux send-keys -t ${pm} Enter`);
    if (phase === "shell") {
      launches++;
      phase = "starting";
      return tmux(`set-option -p -t ${pm} @fixture_command claude`);
    }
    assert.equal(phase, "ready");
    assert.equal(composer, message);
    composer = "";
    submissions++;
    return screen("");
  }
  const args = command.slice(5).replaceAll("#{pane_current_command}", "#{@fixture_command}");
  const result = await tmux(args, options);
  if (command.startsWith("tmux if-shell")) {
    [[, pm]] = await panes();
    assert.notEqual(pm, sibling[1]);
    await tmux(`set-option -p -t ${pm} @fixture_command zsh`);
    phase = "shell";
  }
  return result;
};

try {
  await tmux(`new-session -d -s 0 -n pm -x 100 -y 40 -c ${escape(cwd)} 'sleep 60'`);
  await tmux("set-option -g default-command 'sleep 60'");
  await tmux("set-option -w -t '=0:0' pane-base-index 1");
  [sibling] = await panes();
  assert.equal(sibling[0], "1");
  await tmux(`set-option -p -t ${sibling[1]} @fixture_command claude`);

  const relay = new TmuxRelay(config, { pmRuntime: "claude", runShell });
  relay.setDatabase(db);
  const health = new ProcessHealthChecker(db, relay);
  health.runShell = runShell;
  health.pmAgentProcess = async () => "1:"; // fake shell, no Claude child yet
  health.PM_EXIT_SHELL_TIMEOUT_MS = 2000;
  health.PM_PROMPT_TIMEOUT_MS = 2000;
  db.enqueuePendingPMEvent(0, event, message);
  const original = db.peekPendingPMEvents();

  await health.checkAll(true);
  assert.ok(health.pmRestartInFlight, "boot must recover the missing PM, not accept the same-checkout sibling");
  assert.equal((await health.pmRestartInFlight).success, true);
  assert.equal(phase, "starting");
  assert.equal(await relay.drainPMQueue(), 0);
  assert.deepEqual(db.peekPendingPMEvents(), original);
  assert.equal(db.getPMQueueDeliveryState(0, event, message, original[0].enqueued_at), "none");
  assert.deepEqual([launches, pastes, submissions], [1, 0, 0]);

  phase = "ready";
  await health.checkAll(true);
  assert.equal(await relay.pmDrainChain, 1);
  await health.checkAll(true);
  assert.equal(await relay.drainPMQueue(), 0);
  assert.equal(db.getPendingPMEventCount(), 0);
  assert.equal(db.getPMQueueDeliveryState(0, event, message, original[0].enqueued_at), "delivered");
  assert.deepEqual([launches, pastes, submissions], [1, 1, 1]);
  const [recovered, intact] = await panes();
  assert.deepEqual(recovered.slice(0, 2), ["0", pm]);
  assert.deepEqual(intact, sibling, "the sibling stays at .1 with its original pane and PID");
  console.log("PASS: sibling preserved; 1 --continue launch; 0 delivery attempts while starting; 1 paste, Enter and durable delivery after readiness; no duplicates.");
} finally {
  db.close();
  await tmux("kill-server").catch(() => undefined);
}
