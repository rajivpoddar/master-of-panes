// Offline proof of actual compiled writers and Hono /send. No server import/listener/live tmux.
import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { readFile, writeFile, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ts from 'typescript';
import { Hono } from 'hono';
import { z } from 'zod';
import { MoPDatabase } from '../dist/db.js';
import { DEFAULT_CONFIG } from '../dist/types.js';
import { TmuxRelay } from '../dist/relay.js';
import * as interrupt from '../dist/interruptBeforeClear.js';
import * as composer from '../dist/composer.js';
import * as clearWait from '../dist/sessionStartClearWait.js';
import * as packet from '../dist/assignmentTaskPacket.js';
import { paneInputModeRefusalReason } from '../dist/paneInputMode.js';
import { expectedCheckoutPath, paneAddress, verifyPaneIdentity } from '../dist/paneIdentity.js';
import { isValidRuntimeSlot, DEFAULT_DEV_SLOT_COUNT } from '../dist/slotConfig.js';
import { withSlotSendLock } from '../dist/slotSendLock.js';
import { registerAssignmentEffectRoutes } from '../dist/assignmentEffectRoutes.js';

const server = ts.createSourceFile('server.js', readFileSync(new URL('../dist/server.js', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true);
const names = ['observeSlotPane', 'slotSessionToken', 'slotRowWithSession', 'fenceSlotOperation', 'interruptSlotBeforeClear', 'clearSlotsThroughMopHttp', 'clearComposerResidueAfterReleaseInterrupt', 'capturePaneSnapshot', 'readPaneInputModeRefusal', 'pastePayloadWithTmuxBuffer', 'sendChunkSizeBytes', 'shellEscape', 'deliveryConfirmed', 'sendClearViaMopSendPath', 'clearSlotForAssignment', 'deliverTaskFileForAssignment', 'parseMessageSlotWrapper', 'shellWords', 'paneExists'];
const declarations = names.map(name => {
  const node = server.statements.find(n => ts.isFunctionDeclaration(n) && n.name?.text === name);
  assert.ok(node, `compiled ${name}`); return node.getText(server);
}).join('\n');
const schema = server.statements.find(n => ts.isVariableStatement(n) && n.declarationList.declarations.some(d => d.name.getText(server) === 'slotParamSchema')).getText(server);
const sendRoute = server.statements.find(n => ts.isExpressionStatement(n) && ts.isCallExpression(n.expression) && n.expression.expression.getText(server) === 'app.post' && n.expression.arguments[0]?.text === '/slots/:slotNum/send').getText(server);
const REPO = 'github:heydonna-app/heydonna-app', HEAD = 'b'.repeat(40);
function harness() {
  const dir = mkdtempSync(join(tmpdir(), 'mop-ibc-writer-')), slot = 5, checkout = expectedCheckoutPath(slot);
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(dir, 'db.sqlite') });
  let pane = '%50', session = 'A', text = '', activity = 'idle', loaded = '', screen = 0;
  let onSleep = null, onCapture = null, onLoad = null, onPaste = null, onKey = null, onHttp = null, onFile = null, onIdentity = null;
  const effects = [];
  const tuple = issue => ({ repository_id: REPO, issue, pr: null, branch: `fix/${issue}`, head_sha: HEAD, work_kind: 'implementation', handoff_id: String(issue), claimed_at: null });
  assert.equal(db.assignSlotSimple(slot, tuple(8900), 'owner A').ok, true);
  function hook(source = 'startup') { db.logEvent(slot, 'SessionStart', 'SessionStart', null, { session_id: session, cwd: checkout, source }); }
  hook();
  function drift(kind) {
    if (kind === 'pane') pane = '%51';
    if (kind === 'session') { session = 'B'; hook(); }
    if (kind === 'epoch') assert.equal(db.assignSlotSimple(slot, tuple(8901), 'owner B').ok, true);
    if (kind === 'turn') { db.startAgentTurn(slot, 'turn-B'); activity = 'active'; }
  }
  const runShell = async command => {
    if (command.includes('pane_in_mode')) return { stdout: '0', stderr: '' };
    if (command.includes('display-message') || command.includes('list-panes')) { await onIdentity?.(); return { stdout: `${pane}|${checkout}\n`, stderr: '' }; }
    if (command.startsWith('git ')) return { stdout: checkout + '\n', stderr: '' };
    if (command.includes('capture-pane')) {
      await onCapture?.();
      const prompt = activity === 'active' && command.includes(' -e ') ? '\x1b[38;2;153;153;153m❯ \x1b[0m' : '❯ ' + text;
      return { stdout: activity === 'unknown' ? 'unreadable' : `screen ${screen}\n────────────────────────\n${prompt}\n────────────────────────\n`, stderr: '' };
    }
    if (command.includes('load-buffer')) { loaded = readFileSync(command.match(/'([^']+)'$/)[1], 'utf8'); await onLoad?.(); return { stdout: '', stderr: '' }; }
    if (command.includes('paste-buffer') || command.includes('send-keys')) {
      const target = command.match(/-t\s+'?([^'\s]+)'?/)[1];
      effects.push({ command, target, session, epoch: db.getSlot(slot).assignment_epoch, turn: db.getSlot(slot).active_turn_id });
      if (command.includes('paste-buffer')) { text += loaded; await onPaste?.(); }
      else {
        screen++; text = ''; activity = 'idle';
        if (command.endsWith(' Enter') && loaded === '/clear') { session = 'cleared'; hook('clear'); }
        await onKey?.(command);
      }
      return { stdout: '', stderr: '' };
    }
    throw Error('unexpected fake command: ' + command);
  };
  const relay = new TmuxRelay(DEFAULT_CONFIG, { runShell }); relay.setDatabase(db);
  // Legacy numeric activity is also read through the injected fake runner:
  // baseline probes must never invoke the installed is-active.sh.
  const activityProbe = relay.getSlotActivityState.bind(relay);
  relay.getSlotActivityState = (number, expected) => activityProbe(number, expected ?? pane);
  const app = new Hono();
  const context = { app, db, relay, config: DEFAULT_CONFIG, z, ...interrupt, ...composer, ...clearWait, ...packet,
    withSlotSendLock, paneAddress, paneInputModeRefusalReason, isValidRuntimeSlot, DEFAULT_DEV_SLOT_COUNT,
    verifyPaneIdentity: slot => verifyPaneIdentity(slot, runShell), execShell: runShell,
    execShellOk: async command => { await runShell(command); return true; },
    sleep: async ms => { await onSleep?.(ms); }, writeFile, unlink,
    readFile: async path => { await onFile?.(); return readFile(path); },
    fetch: async (url, init) => { await onHttp?.(); return app.request(url, init); },
    requestPmClearOnce: async () => ({ kind: 'pending' }),
    PM_CLEAR_STALE_ACK_REPAIR_MS: 1, PM_CLEAR_RECENT_SUPPRESS_MS: 1, PM_CLEAR_REQUESTED_AT_KEY: 'pm_clear_requested_at',
    hasRecentClearEvent: () => false, findLaterPmLifecycleEvent: () => null,
    ASSIGNMENT_CLEAR_READY_TIMEOUT_MS: 0, ASSIGNMENT_CLEAR_READY_POLL_MS: 1,
  };
  const rt = new Function(...Object.keys(context), schema + '\n' + declarations + '\n' + sendRoute + '\nreturn {' + names.join(',') + '};')(...Object.values(context));
  const pin = () => interrupt.pinFrom({ paneId: pane, checkout }, rt.slotRowWithSession(slot));
  return { db, slot, checkout, dir, effects, rt, app, pin, drift,
    set onSleep(v) { onSleep = v; }, set onCapture(v) { onCapture = v; }, set onLoad(v) { onLoad = v; }, set onPaste(v) { onPaste = v; }, set onKey(v) { onKey = v; }, set onHttp(v) { onHttp = v; }, set onFile(v) { onFile = v; }, set onIdentity(v) { onIdentity = v; },
    set text(v) { text = v; }, set activity(v) { activity = v; },
    close() { db.close(); rmSync(dir, { recursive: true, force: true }); }
  };
}

for (const kind of ['pane', 'session', 'epoch', 'turn']) {
  test(`actual cleanup writer refuses ${kind} drift during restore wait, no C-u`, async () => {
    const h = harness();
    try {
      h.text = 'restored prompt'; const pin = h.pin(); let done = false;
      h.onSleep = () => { if (!done) { done = true; h.drift(kind); } };
      const r = await h.rt.clearComposerResidueAfterReleaseInterrupt(h.slot, pin.paneId, pin);
      assert.equal(r, 'not_cleared'); assert.deepEqual(h.effects, []);
    } finally { h.close(); }
  });
  test(`actual HTTP clear transition refuses ${kind} drift after caller fence, zero pane effects`, async () => {
    const h = harness();
    try {
      const pin = h.pin(); h.onHttp = () => h.drift(kind);
      const r = await h.rt.sendClearViaMopSendPath(h.slot, 'offline', true, pin);
      assert.equal(r.success, false); assert.deepEqual(h.effects, []);
    } finally { h.close(); }
  });
  test(`actual task writer refuses ${kind} drift during buffer loading before paste`, async () => {
    const h = harness();
    try {
      const pin = h.pin(), file = join(h.dir, 'task.md'); await writeFile(file, 'Implement the task.');
      h.onLoad = () => h.drift(kind);
      const r = await h.rt.deliverTaskFileForAssignment(h.slot, file, pin);
      assert.equal(r.verified, false); assert.deepEqual(h.effects, []);
    } finally { h.close(); }
  });
  test(`actual /clear writer refuses ${kind} drift after paste, no Enter on the new generation`, async () => {
    const h = harness();
    try {
      const pin = h.pin(); h.onPaste = () => h.drift(kind);
      const r = await h.rt.sendClearViaMopSendPath(h.slot, 'offline', true, pin);
      assert.equal(r.success, false); assert.equal(h.effects.length, 1); assert.match(h.effects[0].command, /paste-buffer/);
      assert.equal(h.effects[0].target, '%50'); assert.equal(h.effects[0].session, 'A');
    } finally { h.close(); }
  });
}

test('terminal_only refuses a turn starting after its initial idle observation; never interrupts', async () => {
  const h = harness();
  try {
    h.db.updateAssignmentState(h.slot, { occupied: false }); // temporary SQLite fixture only
    let observed = 0; h.onIdentity = () => { if (++observed === 1) h.drift('turn'); };
    const r = await h.rt.clearSlotsThroughMopHttp([h.slot], { clearExistingPendingForTargets: false, source: 'offline', terminalOnly: true });
    assert.ok(!r[0].status.startsWith('cleared')); assert.deepEqual(h.effects, []); assert.equal(h.db.getSlot(h.slot).active_turn_id, 'turn-B');
  } finally { h.close(); }
});

test('actual writers preserve positive interrupt -> cleanup -> clear -> bracketed task order', async () => {
  const h = harness();
  try {
    h.db.startAgentTurn(h.slot, 'turn-A'); h.activity = 'active'; h.text = 'restored prompt';
    h.onKey = command => { if (command.endsWith(' Escape')) h.text = 'restored prompt'; };
    const op = await h.rt.interruptSlotBeforeClear(h.slot, 'offline-positive');
    assert.equal(op.ok, true, JSON.stringify(op));
    const cleared = await h.rt.clearSlotForAssignment(h.slot, op.pin); assert.equal(cleared.ok, true, JSON.stringify(cleared));
    const pin = h.pin(), file = join(h.dir, 'task.md'); await writeFile(file, 'Implement the task.');
    const delivered = await h.rt.deliverTaskFileForAssignment(h.slot, file, pin); assert.equal(delivered.verified, true, JSON.stringify(delivered));
    assert.match(h.effects[0].command, /Escape$/); assert.match(h.effects[1].command, /C-u$/);
    assert.match(h.effects[2].command, /paste-buffer/); assert.match(h.effects[3].command, /Enter$/);
    assert.match(h.effects[4].command, /paste-buffer -p/); assert.match(h.effects[5].command, /Enter$/);
    assert.ok(h.effects.every(effect => effect.target === '%50'));
  } finally { h.close(); }
});

test('actual guarded writer sends at most one Enter after lost acknowledgment', async () => {
  const h = harness();
  try {
    h.onKey = () => { throw Error('lost acknowledgment after effect'); };
    const r = await h.rt.sendClearViaMopSendPath(h.slot, 'offline', true, h.pin());
    assert.equal(r.success, false); assert.equal(h.effects.filter(e => e.command.endsWith(' Enter')).length, 1);
  } finally { h.close(); }
});

test('standalone occupied refusal preserves assignment and pending clear; PM retains self-clear latch', async () => {
  const h = harness();
  try {
    const before = h.db.getSlot(h.slot); h.db.setPendingClear(h.slot);
    const r = await h.rt.clearSlotsThroughMopHttp([h.slot], { clearExistingPendingForTargets: true, source: 'offline', terminalOnly: false });
    assert.match(r[0].status, /refused/); assert.equal(h.db.hasPendingClear(h.slot), true); assert.deepEqual(h.db.getSlot(h.slot), before);
    const pm = await h.rt.clearSlotsThroughMopHttp([0], { clearExistingPendingForTargets: false, source: 'offline', terminalOnly: false });
    assert.match(pm[0].status, /already requested/); assert.deepEqual(h.effects, []);
  } finally { h.close(); }
});

test('actual Hono assignment carries the same pin through interrupt, clear and committed task writer', async () => {
  const h = harness();
  try {
    h.db.startAgentTurn(h.slot, 'turn-A'); h.activity = 'active';
    const file = join(h.dir, 'task.md'); await writeFile(file, 'Implement the task.');
    registerAssignmentEffectRoutes(h.app, { db: h.db,
      interruptTurn: slot => h.rt.interruptSlotBeforeClear(slot, 'offline-assignment'),
      observePane: slot => h.rt.observeSlotPane(slot), readSession: slot => h.rt.slotSessionToken(slot),
      clearSlot: (slot, pin) => h.rt.clearSlotForAssignment(slot, pin),
      deliverTaskFile: (slot, path, pin) => h.rt.deliverTaskFileForAssignment(slot, path, pin),
      observeWorktree: async () => ({ clean: true, detail: 'offline fixture' }) });
    const r = await h.app.request(`http://mop/slots/${h.slot}/assign-effect`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({
      effect_id: 'offline-writer-chain', selection_class: 'new_issue', expected_epoch: h.db.getSlot(h.slot).assignment_epoch,
      repository_id: REPO, issue: 8902, pr: null, branch: 'fix/8902', head_sha: HEAD,
      work_kind: 'implementation', handoff_id: '8902', claimed_at: null, task: 'Implement the task.', task_file: file }) });
    const body = await r.json(); assert.equal(r.status, 200, JSON.stringify(body)); assert.equal(body.status, 'assigned');
    assert.equal(h.db.getSlot(h.slot).issue, 8902); assert.equal(h.db.getSlot(h.slot).assignment_epoch, 2);
    assert.equal(h.effects.length, 5); assert.match(h.effects[0].command, /Escape$/);
    assert.match(h.effects[1].command, /paste-buffer/); assert.match(h.effects[2].command, /Enter$/);
    assert.match(h.effects[3].command, /paste-buffer -p/); assert.match(h.effects[4].command, /Enter$/);
  } finally { h.close(); }
});

test('actual task writer refuses a turn appearing during file read, no buffer load or pane effect', async () => {
  const h = harness();
  try {
    const pin = h.pin(), file = join(h.dir, 'task.md'); await writeFile(file, 'Implement the task.');
    h.onFile = () => h.drift('turn');
    const r = await h.rt.deliverTaskFileForAssignment(h.slot, file, pin);
    assert.equal(r.verified, false); assert.deepEqual(h.effects, []); assert.equal(h.db.getSlot(h.slot).active_turn_id, 'turn-B');
  } finally { h.close(); }
});

for (const activity of ['unknown', 'active']) {
  test(`actual clear writer refuses fresh ${activity} activity before paste`, async () => {
    const h = harness();
    try {
      const pin = h.pin(); h.onLoad = () => { h.activity = activity; };
      const r = await h.rt.sendClearViaMopSendPath(h.slot, 'offline', true, pin);
      assert.equal(r.success, false); assert.deepEqual(h.effects, []);
    } finally { h.close(); }
  });
}

test('writer waiting on the existing send lock rechecks before paste, refusing a later turn', async () => {
  const h = harness();
  try {
    const pin = h.pin(); let release, entered;
    const started = new Promise(resolve => { entered = resolve; });
    const hold = withSlotSendLock(h.slot, async () => { entered(); await new Promise(resolve => { release = resolve; }); });
    await started;
    const send = h.rt.pastePayloadWithTmuxBuffer(h.slot, pin.paneId, Buffer.from('/clear'), { source: 'command', label: '/clear', operationPin: pin });
    h.drift('turn'); release(); await hold;
    const result = await send; assert.equal(result.verify.ok, false); assert.deepEqual(h.effects, []);
  } finally { h.close(); }
});

test('new input appearing during buffer load is retained and not pasted over', async () => {
  const h = harness();
  try {
    const pin = h.pin(); h.onLoad = () => { h.text = 'user input'; };
    const result = await h.rt.sendClearViaMopSendPath(h.slot, 'offline', true, pin);
    assert.equal(result.success, false); assert.deepEqual(h.effects, []);
  } finally { h.close(); }
});
