// Offline proof: actual compiled relay/health and extracted compiled HTTP writer.
// Every shell/HTTP boundary is fake. Never import/start dist/server.js.
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile, writeFile, mkdtemp, mkdir, chmod, rm, unlink } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import ts from 'typescript';

const root = resolve(process.env.MOP_PROOF_STAGE || '.');
const load = name => import(pathToFileURL(join(root, 'dist', `${name}.js`)));
const { TmuxRelay } = await load('relay');
const { ProcessHealthChecker } = await load('health');
const { DEFAULT_CONFIG } = await load('types');
const { verifyPaneIdentity } = await load('paneIdentity');
const { pinFrom, pinDrift } = await load('interruptBeforeClear');
const { withSlotSendLock } = await load('slotSendLock');
const { isValidDevSlot } = await load('slotConfig');
const composer = await load('composer');
const { pinnedGuardedSend } = await load('pinnedWriters');
const { ASSIGNMENT_INLINE_TASK_MAX_BYTES, buildAssignmentTaskPacket } = await load('assignmentTaskPacket');
const checkout = slot => `/Users/rajiv/Downloads/projects/heydonna-app${slot ? `-300${slot}` : ''}`;
const rule = '──────────────────────────';
const render = p => `${p.prefix ?? ''}${rule}\n${p.active ? '\x1b[38;2;153;153;153m' : ''}❯\x1b[0m ${p.composer}\n${rule}\n  ⏵⏵ bypass permissions on`;
const parse = path => ts.createSourceFile(path, readFileSync(join(root, path), 'utf8'), ts.ScriptTarget.Latest, true);
function find(node, predicate) {
  if (predicate(node)) return node;
  let result; ts.forEachChild(node, child => { result ??= find(child, predicate); }); return result;
}
function extractFunction(name, deps) {
  const node = find(parse('dist/server.js'), n => ts.isFunctionDeclaration(n) && n.name?.text === name);
  assert.ok(node, `compiled ${name} exists`);
  return new Function(...Object.keys(deps), `return (${node.getText()});`)(...Object.values(deps));
}

async function world() {
  const dir = await mkdtemp(join(tmpdir(), 'mop-pane-activity-'));
  const w = { dir, address: { '0:0.7': '%7', '0:0.6': '%6' }, panes: {
    '%7': { slot: 6, active: false, composer: '' }, '%6': { slot: 7, active: false, composer: '' },
  }, effects: [], loads: 0, buffer: '', session: 1, pending: true, cycled: {}, events: [],
  row: { occupied: true, repository_id: 'repo', issue: 10, pr: null, handoff_id: null,
    branch: 'work', assignment_epoch: 1, session_id: '1', active_turn_id: null, active_turn_state: 'inactive', dnd: false },
  };
  for (let n = 0; n <= 8; n++) w.cycled[n] = n !== 6;
  w.shell = async command => {
    w.onCommand?.(command);
    if (command.startsWith('tmux display-message')) {
      const target = command.match(/-t (\S+)/)?.[1];
      const id = w.address[target] ?? target; const pane = w.panes[id];
      if (!pane) throw new Error('missing pane');
      return { stdout: `${id}|${checkout(pane.slot)}\n`, stderr: '' };
    }
    if (command.startsWith('tmux list-panes')) return { stdout: Object.entries(w.panes).map(([id,p]) => `${id}|${checkout(p.slot)}`).join('\n'), stderr: '' };
    if (command.startsWith('git -C')) return { stdout: `${command.match(/'([^']+)'/)?.[1]}\n`, stderr: '' };
    if (command.startsWith('tmux capture-pane')) {
      const target = command.match(/-t (\S+)/)?.[1]; const p = w.panes[w.address[target] ?? target];
      if (!p) throw new Error('missing pane');
      const output = w.planPrompt && command.includes('-S -20') ? 'Would you like to proceed?' : p.unreadable ? '' : command.includes(' -e ') ? render(p) : render(p).replace(/\x1b\[[0-9;]*m/g,'');
      await Promise.resolve(); w.afterCapture?.(command);
      return { stdout: output, stderr: '' };
    }
    if (command.startsWith('tmux load-buffer')) {
      w.loads++;
      w.buffer = await readFile(command.match(/'([^']+)'\s*$/)?.[1], 'utf8');
      w.afterLoad?.(); return { stdout: '', stderr: '' };
    }
    if (command.startsWith('tmux paste-buffer')) {
      const target = command.match(/-t (\S+)/)?.[1]; const id = w.address[target] ?? target;
      w.effects.push(`${id}:paste:${w.buffer}`); w.panes[id].composer += w.buffer;
      w.afterPaste?.(); return { stdout: '', stderr: '' };
    }
    if (command.startsWith('tmux send-keys')) {
      const target = command.match(/-t (\S+)/)?.[1]; const id = w.address[target] ?? target; w.effects.push(`${id}:Enter`);
      w.panes[id].composer = ''; w.afterEnter?.(); return { stdout: '', stderr: '' };
    }
    if (command.includes('/is-active.sh')) {
      const result = await probeResult(w,6);
      if (result.status !== 0) throw Object.assign(new Error('probe exit'), {code:result.status});
      return {stdout:result.stdout,stderr:''};
    }
    throw new Error(`unexpected fake command: ${command}`);
  };
  w.db = { getExitPending: () => w.pending, getExitStatus: () => ({ cycled: { ...w.cycled } }),
    updateSlot: (slot, changes) => Object.assign(w.row,changes),
    getSlot: slot => slot === 6 ? { ...w.row } : null,
    getEvents: (slot, limit, type) => type === 'SessionStart' ? [{ id: w.session }] : [],
    markSlotExitCycled: slot => { w.cycled[slot] = true; }, setExitPending: value => { w.pending = value; },
    logEvent: (...args) => w.events.push(args),
  };
  w.relay = new TmuxRelay(DEFAULT_CONFIG, { runShell: w.shell, pmRuntime:'claude', pmPaneReady:async()=>true });
  w.relay.setDatabase(w.db); w.relay.injectToPM = () => true;
  // Rehydrate the EXACT compiled activity method only to inject its legacy
  // execShell dependency. The baseline runs the installed shell-probe bytes;
  // the candidate's verified native capture uses the real runShell method.
  const method = find(parse('dist/relay.js'), n => ts.isMethodDeclaration(n) && n.name?.getText() === 'getSlotActivityState');
  const body = method.body.getText();
  const probe = async () => {
    const result = await probeResult(w, 6);
    if (result.status !== 0) throw Object.assign(new Error('probe exit'), { code: result.status });
    return { stdout: result.stdout, stderr: '' };
  };
  w.relay.getSlotActivityState = new Function('verifyPaneIdentity', 'execShell', `return async function(slotNum, expectedPaneId) ${body}`)(verifyPaneIdentity, probe);
  const pendingSends = [];
  w.relay.sendToSlot = (...args) => { pendingSends.push(w.relay.sendToSlotAsync(...args)); return true; };
  w.health = new ProcessHealthChecker(w.db, w.relay);
  w.health.getPaneCommand = async () => 'claude'; // read-only process probe boundary
  w.tick = async () => { await w.health.checkExitPending(); await Promise.all(pendingSends); };
  w.cleanup = () => rm(dir, { recursive: true, force: true });
  return w;
}

async function probeResult(w, slot, fast = false) {
  const bin = join(w.dir, 'bin'); await mkdir(bin, { recursive: true });
  // Fake tmux/git/sleep are child-process boundary fixtures, never host tmux.
  const state = join(w.dir, 'state.json'); await writeFile(state, JSON.stringify(w)); await rm(state + '.captures', { force: true });
  const fixture = `#!/usr/bin/env node\nconst fs=require('fs');const s=JSON.parse(fs.readFileSync(process.env.MOP_FAKE_STATE));const args=process.argv.slice(2);const cmd=args[0];const target=args[args.indexOf('-t')+1];const id=s.address[target]||target;const p=s.panes[id];if(cmd==='display-message'){if(!p)process.exit(2);console.log(id+'|'+(${checkout.toString()})(p.slot));}else if(cmd==='list-panes'){for(const [id,p] of Object.entries(s.panes))console.log(id+'|'+(${checkout.toString()})(p.slot));}else if(cmd==='capture-pane'){if(!p)process.exit(2);const cf=process.env.MOP_FAKE_STATE+'.captures';const n=(fs.existsSync(cf)?Number(fs.readFileSync(cf,'utf8')):0)+1;fs.writeFileSync(cf,String(n));const lf=(s.laterCaptureFail||{})[n];if(lf==='exit')process.exit(1);if(lf==='empty')process.exit(0);if(!p.unreadable)console.log((${render.toString()})(p));}else process.exit(2);`;
  // render's rule constant is serialized as a literal in this fake executable.
  await writeFile(join(bin, 'tmux'), fixture.replace('const fs=require', `const rule=${JSON.stringify(rule)};const fs=require`));
  await writeFile(join(bin, 'git'), '#!/bin/bash\nprintf "%s\\n" "$2"\n');
  await writeFile(join(bin, 'sleep'), '#!/bin/bash\nexit 0\n');
  await writeFile(join(bin, 'md5'), '#!/bin/bash\n/usr/bin/shasum\n');
  for (const name of ['tmux','git','sleep','md5']) await chmod(join(bin,name), 0o755);
  const canonical = join(root, 'scripts/pm/shared-assets/claude/skills/tmux-slot-command/scripts/is-active.sh');
  const script = process.env.MOP_BASELINE_PROBE || canonical;
  return spawnSync('/bin/bash', [script, String(slot), ...(fast ? ['--fast'] : [])], { encoding: 'utf8', env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, MOP_FAKE_STATE: state, MOP_TMUX_BIN: join(bin, "tmux") } });
}

test('S6 active / numeric index6 S7 idle: actual health writer emits no /exit or Enter', async () => {
  const w = await world(); try { w.panes['%7'].active = true; await w.tick(); assert.deepEqual(w.effects, []); assert.equal(w.cycled[6], false); } finally { await w.cleanup(); }
});
test('inverse activity: idle S6 / active S7 permits exactly the pinned S6 writer', async () => {
  const w = await world(); try { w.panes['%6'].active = true; await w.tick(); assert.deepEqual(w.effects, ['%7:paste:/exit', '%7:Enter']); assert.equal(w.cycled[6], true); } finally { await w.cleanup(); }
});
for (const failure of ['missing','unreadable','rebound']) test(`${failure} pane activity is unknown and legacy boolean refuses idle`, async () => {
  const w = await world(); try {
    if (failure === 'missing') delete w.panes['%7'];
    if (failure === 'unreadable') w.panes['%7'].unreadable = true;
    if (failure === 'rebound') { w.panes['%60'] = { ...w.panes['%7'] }; w.address['0:0.7'] = '%60'; delete w.panes['%7']; }
    assert.equal(await w.relay.getSlotActivityState(6, '%7'), 'unknown');
    if (failure !== 'rebound') assert.equal(await w.relay.isSlotActive(6), true);
    if (failure !== 'rebound') { await w.tick(); assert.deepEqual(w.effects, []); }
  } finally { await w.cleanup(); }
});
for (const drift of ['active','pane','epoch','session','turn']) test(`health ${drift} during buffer load refuses before actual paste`, async () => {
  const w = await world(); try {
    w.afterLoad = () => {
      if (drift === 'active') w.panes['%7'].active = true;
      if (drift === 'pane') { w.panes['%60'] = { ...w.panes['%7'] }; w.address['0:0.7'] = '%60'; }
      if (drift === 'epoch') w.row.assignment_epoch++;
      if (drift === 'session') w.session++;
      if (drift === 'turn') { w.row.active_turn_id = 'new'; w.row.active_turn_state = 'active'; }
    };
    await w.tick(); assert.equal(w.loads,1); assert.deepEqual(w.effects, []); assert.equal(w.cycled[6], false);
  } finally { await w.cleanup(); }
});
test('health post-paste unknown consumes the episode without Enter or retry', async () => {
  const w = await world(); try {
    w.afterPaste = () => { w.panes['%7'].unreadable = true; };
    await w.tick(); assert.deepEqual(w.effects, ['%7:paste:/exit']); assert.equal(w.cycled[6], true);
    w.pending = true; await w.tick(); assert.deepEqual(w.effects, ['%7:paste:/exit']);
  } finally { await w.cleanup(); }
});
test('health late draft after buffer load refuses paste and preserves draft', async () => {
  const w = await world(); try {
    w.afterLoad = () => { w.panes['%7'].composer = 'SYNTHETIC_PENDING_DRAFT'; };
    await w.tick();
    assert.deepEqual(w.effects, []); assert.equal(w.cycled[6], false);
    assert.equal(w.panes['%7'].composer, 'SYNTHETIC_PENDING_DRAFT');
  } finally { await w.cleanup(); }
});
test('health unreadable composer after buffer load refuses before paste', async () => {
  const w = await world(); try {
    w.afterLoad = () => { w.panes['%7'].unreadable = true; };
    await w.tick(); assert.deepEqual(w.effects, []); assert.equal(w.cycled[6], false);
  } finally { await w.cleanup(); }
});
for (const arrival of ['after_paste', 'pre_enter_recheck']) test(`health foreign draft ${arrival} refuses Enter and consumes uncertainty once`, async () => {
  const w = await world(); try {
    let pasted = false;
    w.afterPaste = () => {
      pasted = true;
      if (arrival === 'after_paste') w.panes['%7'].composer = 'SYNTHETIC_PENDING_DRAFT' + w.panes['%7'].composer;
    };
    w.onCommand = command => {
      if (arrival === 'pre_enter_recheck' && pasted && command.startsWith('tmux capture-pane -e ')) {
        w.panes['%7'].composer = 'SYNTHETIC_PENDING_DRAFT/exit';
      }
    };
    await w.tick(); assert.deepEqual(w.effects, ['%7:paste:/exit']);
    assert.equal(w.panes['%7'].composer, 'SYNTHETIC_PENDING_DRAFT/exit');
    assert.equal(w.cycled[6], true);
    w.pending = true; await w.tick(); assert.deepEqual(w.effects, ['%7:paste:/exit']);
    assert.equal(w.panes['%7'].composer, 'SYNTHETIC_PENDING_DRAFT/exit');
  } finally { await w.cleanup(); }
});
test('configured numeric address rebound resolves the owning checkout, not the neighboring pane', async () => {
  const w = await world(); try {
    w.address = { '0:0.7': '%6', '0:0.6': '%7' }; w.panes['%7'].active = true;
    assert.equal(await w.relay.getSlotActivityState(6), 'active'); await w.tick(); assert.deepEqual(w.effects, []);
  } finally { await w.cleanup(); }
});
test('pane rebinding during activity capture invalidates that observation',async()=>{
  const w=await world();try{
    w.afterCapture=()=>{w.panes['%60']={...w.panes['%7']};w.address['0:0.7']='%60';};
    assert.equal(await w.relay.getSlotActivityState(6,'%7'),'unknown');
  }finally{await w.cleanup();}
});
test('exit-pending lock wait rechecks activity before any paste',async()=>{
  const w=await world();try{
    let release;const hold=withSlotSendLock(6,()=>new Promise(r=>{release=r;}));
    while(!release) await Promise.resolve();
    const tick=w.tick();
    // Wait only for the offline activity read boundary, then change the fake pane.
    let observed=false;w.afterCapture=()=>{observed=true;};
    for(let i=0;i<10&&!observed;i++) await new Promise(r=>setImmediate(r));
    w.panes['%7'].active=true;release();await hold;await tick;
    assert.deepEqual(w.effects,[]);assert.equal(w.cycled[6],false);
  }finally{await w.cleanup();}
});
for(const preserve of ['active_turn','dnd']) test(`exit-pending preserves ${preserve} occupied work`,async()=>{
  const w=await world();try{
    if(preserve==='active_turn'){w.row.active_turn_id='accepted';w.row.active_turn_state='active';}else w.row.dnd=true;
    await w.tick();assert.deepEqual(w.effects,[]);assert.equal(w.cycled[6],false);
  }finally{await w.cleanup();}
});
test('idle verified PM retains the existing exit-pending target and never uses a numbered pane',async()=>{
  const w=await world();try{
    w.address['0:0.0']='%0';w.panes['%0']={slot:0,active:false,composer:''};w.cycled[6]=true;w.cycled[0]=false;
    await w.tick();assert.deepEqual(w.effects,['%0:paste:/exit','%0:Enter']);assert.equal(w.cycled[0],true);
  }finally{await w.cleanup();}
});

for (const foreign of ['', '\nSYNTHETIC_PENDING_DRAFT', '\n[Pasted text #2 +12 lines]']) test(`actual pinned assignment owned collapsed packet ${foreign ? 'with foreign content ' + JSON.stringify(foreign) + ' refuses' : 'submits once'}`, async () => {
  const w = await world(); try {
    const file = join(w.dir, 'assignment.md');
    const payload = Array.from({length:12}, (_, i) => `Task line ${i + 1}: fixture`).join('\n');
    await writeFile(file, payload);
    const pin = pinFrom({paneId:'%7',checkout:checkout(6)},w.row);
    const placeholder = '[Pasted text #1 +12 lines]';
    w.afterPaste = () => { w.panes['%7'].composer = placeholder + foreign; };
    const send = extractFunction('deliverTaskFileForAssignment', {
      verifyPaneIdentity: slot => verifyPaneIdentity(slot,w.shell), readFile,
      ASSIGNMENT_INLINE_TASK_MAX_BYTES, buildAssignmentTaskPacket, pinnedGuardedSend,
      relay: w.relay, slotOperationFence: () => () => pinDrift(pin,w.row), sleep: async () => {},
    });
    const result = await send(6,file,pin);
    assert.equal(result.verified, !foreign);
    assert.deepEqual(w.effects, [`%7:paste:${payload}`, ...(!foreign ? ['%7:Enter'] : [])]);
    assert.equal(w.panes['%7'].composer, foreign ? placeholder + foreign : '');
    if (foreign) {
      assert.match(result.reason, /uncertain:pre_enter_refused:composer_not_owned_by_send/);
      assert.equal(result.receipt.outcome, 'uncertain');
      // The existing one-shot writer does not repaste or send a second Enter.
      assert.equal(w.loads,1);
    }
  } finally { await w.cleanup(); }
});
test('single-line recovery refuses a collapsed placeholder as unowned and never retries', async () => {
  const w=await world();try {
    w.afterPaste=()=>{w.panes['%7'].composer='[Pasted text #1 +12 lines]';};
    await w.tick();assert.deepEqual(w.effects,['%7:paste:/exit']);assert.equal(w.cycled[6],true);
    w.pending=true;await w.tick();assert.deepEqual(w.effects,['%7:paste:/exit']);
    assert.equal(w.panes['%7'].composer,'[Pasted text #1 +12 lines]');
  } finally { await w.cleanup(); }
});

function writer(w) {
  const row = () => ({ ...w.row, session_id: String(w.session) });
  const deps = { withSlotSendLock, relay: w.relay, db: w.db, sendChunkSizeBytes: () => 4000,
    fenceSlotOperation: async (slot,pin) => {
      const id = await verifyPaneIdentity(slot,w.shell);
      return pinDrift(pin,row(),id.ok ? { paneId:id.snapshot.paneId,checkout:id.snapshot.currentPath } : null);
    }, slotOperationFence: (slot,pin) => () => pinDrift(pin,row()), readPaneInputModeRefusal: async () => null,
    INJECT_ENTER_DELAY_MS: 0, resolveAssignmentInjectEnterDelayMs: () => 0,
    capturePaneSnapshot: async id => (await w.shell(`tmux capture-pane -t ${id} -p`)).stdout,
    ...composer, execShell: w.shell, sleep: async () => {}, writeFile, unlink,
    shellEscape: value => `'${value}'`, verifyComposerAwareSend: async () => ({ok:true}),
  };
  return extractFunction('pastePayloadWithTmuxBuffer',deps);
}
for (const drift of ['active','pane','epoch']) test(`actual HTTP buffer writer ${drift} after load refuses before paste`, async () => {
  const w = await world(); try {
    const pin = pinFrom({paneId:'%7',checkout:checkout(6)},w.row);
    w.afterLoad = () => { if(drift==='active')w.panes['%7'].active=true; if(drift==='epoch')w.row.assignment_epoch++; if(drift==='pane'){w.panes['%60']={...w.panes['%7']};w.address['0:0.7']='%60';} };
    await assert.rejects(writer(w)(6,'%7',Buffer.from('fixture'),{source:'command',label:'fixture',activityPin:pin}));
    assert.deepEqual(w.effects,[]);
  } finally { await w.cleanup(); }
});
test('actual HTTP buffer writer loses activity evidence after paste: no Enter',async()=>{
  const w=await world();try{
    const pin=pinFrom({paneId:'%7',checkout:checkout(6)},w.row);w.afterPaste=()=>{w.panes['%7'].unreadable=true;};
    await assert.rejects(writer(w)(6,'%7',Buffer.from('fixture'),{source:'command',label:'fixture',activityPin:pin}));
    assert.deepEqual(w.effects,['%7:paste:fixture']);
  }finally{await w.cleanup();}
});
test('retained shell probe uses the mapped immutable S6 target, refuses missing capture and rebind', async () => {
  const w = await world(); try {
    w.panes['%7'].active=true; assert.equal((await probeResult(w,6,true)).status,0);
    w.panes['%7'].active=false; w.panes['%6'].active=true; assert.equal((await probeResult(w,6,true)).status,1);
    w.panes['%7'].unreadable=true; assert.equal((await probeResult(w,6,true)).status,2);
  } finally { await w.cleanup(); }
});
for(const [color,state,exit] of [['153;153;153','active',0],['10;20;30','unknown',2]]) test(`native and retained probe honor inherited ${state} foreground`,async()=>{
  const w=await world();try{
    w.panes['%7'].prefix=`\x1b[38;2;${color}m`;
    assert.equal(await w.relay.getSlotActivityState(6),state);assert.equal((await probeResult(w,6,true)).status,exit);
  }finally{await w.cleanup();}
});

function route(w,path,gatesOnly=false) {
  const node=find(parse('dist/server.js'),n=>ts.isCallExpression(n)&&n.expression.getText()==='app.post'&&n.arguments[0]?.text===path);
  assert.ok(node,'actual compiled route exists');
  let code=node.arguments[1].getText();
  if(gatesOnly) { const stop=code.indexOf('// ── GATE: If command is "2"'); assert.ok(stop>0); code=code.slice(0,stop)+'return c.json({success:true},200);\n}'; }
  const row=()=>({...w.row,session_id:String(w.session)});
  const deps={slotParamSchema:{safeParse:()=>({success:true,data:6})},verifyPaneIdentity:slot=>verifyPaneIdentity(slot,w.shell),
    db:w.db,relay:w.relay,config:{...DEFAULT_CONFIG,slotCount:8},paneAddress:()=> '0:0.7',resolvePaneAddress:()=> '0:0.7',
    parseMessageSlotWrapper:()=>null,isPmControlCommand:()=>false,paneExists:async()=>true,isValidDevSlot,
    slotRowWithSession:row,pinFrom,withSlotSendLock,execShell:w.shell,sleep:async()=>{},
    pinDrift,
    fenceSlotOperation:async(slot,pin)=>{const id=await verifyPaneIdentity(slot,w.shell);return pinDrift(pin,row(),id.ok?{paneId:id.snapshot.paneId,checkout:id.snapshot.currentPath}:null);},
    slotOperationFence:(slot,pin)=>()=>pinDrift(pin,row()),processor:{clearPlanApprovalTimer:()=>{}},
  };
  return new Function(...Object.keys(deps),`return (${code});`)(...Object.values(deps));
}
const context=body=>({req:{param:()=> '6',json:async()=>body},json:(body,status=200)=>({body,status})});
for(const command of ['/review-and-pr','fixture']) for(const activity of ['active','unknown']) test(`actual ${command==='fixture'?'non-force':'review'} gate refuses same-pane ${activity}`,async()=>{
  const w=await world();try{
    w.panes['%7'].active=activity==='active';w.panes['%7'].unreadable=activity==='unknown';
    const result=await route(w,'/slots/:slotNum/send',true)(context({command,force:command!=='fixture'}));
    assert.equal(result.status,409);assert.deepEqual(w.effects,[]);
  }finally{await w.cleanup();}
});
test('actual plan approval reads success from the pinned S6, never idle S7',async()=>{
  const w=await world();try{
    w.planPrompt=true;w.row.activity='awaiting_plan_approval';w.afterEnter=()=>{w.panes['%7'].active=true;};
    const result=await route(w,'/slots/:slotNum/approve-plan')(context({option:'4'}));
    assert.equal(result.body.success,true);assert.equal(result.body.status,'active');assert.deepEqual(w.effects,['%7:Enter']);
  }finally{await w.cleanup();}
});
test('actual plan approval post-effect unknown is unconfirmed without a second key',async()=>{
  const w=await world();try{
    w.planPrompt=true;w.row.activity='awaiting_plan_approval';w.afterEnter=()=>{w.panes['%7'].unreadable=true;};
    const result=await route(w,'/slots/:slotNum/approve-plan')(context({option:'4'}));
    assert.equal(result.body.success,false);assert.equal(result.body.status,'unconfirmed');assert.deepEqual(w.effects,['%7:Enter']);
  }finally{await w.cleanup();}
});
test('actual plan choice refuses an epoch changed while awaiting the prompt',async()=>{
  const w=await world();try{
    w.planPrompt=true;w.row.activity='awaiting_plan_approval';w.afterCapture=()=>{w.row.assignment_epoch++;};
    const result=await route(w,'/slots/:slotNum/approve-plan')(context({option:'4'}));
    assert.equal(result.body.success,false);assert.deepEqual(w.effects,[]);
  }finally{await w.cleanup();}
});
test('actual plan success cannot update a later assignment after the activity read',async()=>{
  const w=await world();try{
    w.planPrompt=true;w.row.activity='awaiting_plan_approval';w.afterEnter=()=>{w.panes['%7'].active=true;w.row.assignment_epoch++;};
    const result=await route(w,'/slots/:slotNum/approve-plan')(context({option:'4'}));
    assert.equal(result.body.success,false);assert.equal(result.body.status,'unconfirmed');assert.equal(w.row.activity,'awaiting_plan_approval');assert.deepEqual(w.effects,['%7:Enter']);
  }finally{await w.cleanup();}
});

// CTO P1 (C0ALZJHGE49/1791372028.387449 ts 1791375065.803519): a readable first
// capture followed by failed/empty later captures must be UNKNOWN, never IDLE.
for (const mode of ['exit','empty']) for (const fail of [[2],[3],[2,3]]) test(`later capture ${fail.join('+')} ${mode} is unknown: no paste, Enter or cycle`, async () => {
  const w = await world(); try {
    w.laterCaptureFail = Object.fromEntries(fail.map(n => [n, mode]));
    assert.equal((await probeResult(w, 6)).status, 2);
    w.relay.getSlotActivityState = async () => { const r = await probeResult(w, 6); return r.status === 0 ? 'active' : r.status === 1 ? 'idle' : 'unknown'; };
    await w.tick();
    assert.deepEqual(w.effects, []); assert.equal(w.loads, 0); assert.equal(w.cycled[6], false);
  } finally { await w.cleanup(); }
});
test('readable later captures on an idle pane still prove idle', async () => {
  const w = await world(); try { assert.equal((await probeResult(w, 6)).status, 1); } finally { await w.cleanup(); }
});

// CTO REVISE on 71912da (C0ALZJHGE49/1791372028.387449 ts 1791375601.182929):
// the guarded recovery writer re-reads the composer after every await.
test('late draft typed during buffer load stays intact: zero paste, zero Enter', async () => {
  const w = await world(); try {
    w.afterLoad = () => { w.panes['%7'].composer = 'half typed draft'; };
    await w.tick();
    assert.equal(w.loads, 1); assert.deepEqual(w.effects, []);
    assert.equal(w.panes['%7'].composer, 'half typed draft');
  } finally { await w.cleanup(); }
});
test('foreign composer content after paste is never submitted', async () => {
  const w = await world(); try {
    w.afterPaste = () => { w.panes['%7'].composer = 'draft text /exit'; };
    await w.tick();
    assert.deepEqual(w.effects, ['%7:paste:/exit']); assert.equal(w.cycled[6], true);
  } finally { await w.cleanup(); }
});
test('positive idle S6 delivery still pastes /exit and presses one Enter', async () => {
  const w = await world(); try {
    await w.tick();
    assert.deepEqual(w.effects, ['%7:paste:/exit', '%7:Enter']); assert.equal(w.cycled[6], true);
  } finally { await w.cleanup(); }
});

// CTO P2 on 249813f (ts 1791376219.975109): owned collapsed multiline packets still submit once.
const packet = Array.from({ length: 13 }, (_, i) => `packet line ${i + 1}`).join('\n');
const guarded = w => w.relay.deliverGuardedToSlot(6, packet, { expectedPaneId: '%7', recheck: async () => null, bracketedPaste: true,
  timing: { dwellMs: 0, pollMs: 0, payloadGraceMs: 0, payloadStableMs: 0, clearGraceMs: 0 } });
test('newly owned placeholder-only multiline packet submits exactly once', async () => {
  const w = await world(); try {
    w.afterPaste = () => { w.panes['%7'].composer = '[Pasted text #1 +12 lines]'; };
    const r = await guarded(w);
    assert.equal(w.effects.filter(e => e.endsWith(':Enter')).length, 1); assert.notEqual(r.outcome, 'refused_pre_effect');
  } finally { await w.cleanup(); }
});
test('placeholder plus foreign visible text refuses Enter, keeps content, no retry', async () => {
  const w = await world(); try {
    w.afterPaste = () => { w.panes['%7'].composer = 'my draft [Pasted text #1 +12 lines]'; };
    const r = await guarded(w);
    assert.equal(w.effects.filter(e => e.endsWith(':Enter')).length, 0);
    assert.equal(w.effects.filter(e => e.includes(':paste:')).length, 1);
    assert.equal(w.panes['%7'].composer, 'my draft [Pasted text #1 +12 lines]'); assert.equal(r.outcome, 'uncertain');
  } finally { await w.cleanup(); }
});

// CTO REVISE at edbc899 (ts 1791377258.369439): a short /exit never owns an opaque placeholder.
test('/exit send + foreign 12-line human paste: zero Enter, content preserved, no retry', async () => {
  const w = await world(); try {
    w.afterPaste = () => { w.panes['%7'].composer = '[Pasted text #1 +12 lines]'; };
    await w.tick();
    assert.deepEqual(w.effects, ['%7:paste:/exit']);
    assert.equal(w.panes['%7'].composer, '[Pasted text #1 +12 lines]'); assert.equal(w.cycled[6], true);
    w.afterPaste = undefined; await w.tick(); assert.deepEqual(w.effects, ['%7:paste:/exit']);
  } finally { await w.cleanup(); }
});
test('multiline packet with two placeholders refuses Enter', async () => {
  const w = await world(); try {
    w.afterPaste = () => { w.panes['%7'].composer = '[Pasted text #1 +12 lines] [Pasted text #2 +12 lines]'; };
    await guarded(w);
    assert.equal(w.effects.filter(e => e.endsWith(':Enter')).length, 0);
  } finally { await w.cleanup(); }
});
