"""Actual snapshot helper, synthetic state and fake transport; no live effects."""
import importlib.util
import json
import subprocess
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
ASSETS = ROOT / "scripts/pm/shared-assets"
HELPER = ASSETS / "claude/scripts/cto-relay-snapshot.py"
SOP = ASSETS / "codex/monitors/cto-slack-relay/WAKE_SOP.md"
SOCIAL = "01a11e3d-d8bb-73c1-8593-8ff56019d842"
DECISIONS = "01a09112-a09c-7361-9a2a-0ada6a4e9dfb"
GROWTH = "C0C7U35LBKR"
SPEC = importlib.util.spec_from_file_location("growth_relay", HELPER)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def configured_sop(tmp_path):
    text = SOP.read_text()
    start, rest = text.split(MOD.ROUTE_TABLE_BEGIN, 1)
    data, end = rest.split(MOD.ROUTE_TABLE_END, 1)
    table = json.loads(data)
    table["mention_only_channels"][GROWTH] = "U0BNFGX2UAX"
    table["exact_channel_routes"][GROWTH] = {
        "project": "heydonna",
        "destination_thread_id": SOCIAL,
        "consumer_sop_path": "/Users/rajiv/.codex/monitors/heydonna-social-media/WAKE_SOP.md",
        "admitted_after_epoch": 1000,
    }
    path = tmp_path / "SOP.md"
    path.write_text(start + MOD.ROUTE_TABLE_BEGIN + "\n" + json.dumps(table) + "\n" + MOD.ROUTE_TABLE_END + end)
    return path


def event(sop, channel=GROWTH, ts="1001.000001", queued_at="1970-01-01T00:16:41Z", text="<@U0BNFGX2UAX> synthetic full message\nsecond line", kind="app_mention"):
    return {
        "dedup_key": f"{channel}:{ts}", "fingerprint": f"cto-slack-relay:slack_socket_mode:{channel}:{ts}",
        "queued_at": queued_at, "sop_path": str(sop),
        "exact_tuple": {"channel": channel, "ts": ts, "thread_ts": "1000.1", "user": "UEQTTB97A", "text": text},
        "source_evidence": {"slack_event_type": kind, "explicit_cto_mention": None},
        "live_verification": {"source_message_found": True},
        "attachments": [{"slack_file_id": "Fsynthetic", "local_path": "/tmp/synthetic.png"}],
        "previous_thread_message": {"ts": "1000.1", "text": "synthetic parent"},
        "wake_text": f"SOP path: {sop}\nSender: UEQTTB97A\nMessage:\n{text}",
    }


def snapshot(tmp_path, sop, events, handled=(), claims=None, receipts=(), in_process=False):
    for name, obj in [("events", {"events": events}), ("ack", {"handled": list(handled)}), ("claims", claims or {})]:
        (tmp_path / f"{name}.json").write_text(json.dumps(obj))
    receipt_file = tmp_path / "receipts.jsonl"
    receipt_file.write_text("".join(json.dumps(r) + "\n" for r in receipts))
    args = [sys.executable, str(HELPER), "--route-sop", str(sop), "--limit", "1"]
    for option, filename in [("--events-file", "events.json"), ("--ack-file", "ack.json"), ("--claims-file", "claims.json"), ("--output", "output.json"), ("--heartbeat-file", "heartbeat"), ("--lock-file", "lock"), ("--delivery-receipts-file", "receipts.jsonl")]:
        args += [option, str(tmp_path / filename)]
    if in_process:
        original = sys.argv
        try:
            sys.argv = args[1:]
            assert MOD.main() == 0
        finally:
            sys.argv = original
    else:
        run = subprocess.run(args, capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
    return json.loads((tmp_path / "output.json").read_text())


def test_growth_once_preserves_full_envelope(tmp_path):
    sop = configured_sop(tmp_path)
    source = event(sop)
    delivered = []  # Fake supported delivery boundary, never a real task send.
    first = snapshot(tmp_path, sop, [source])
    for frozen in first:
        delivered.append((frozen["relay_route"]["destination_thread_id"], frozen["routed_wake_text"]))
    assert [dest for dest, _ in delivered] == [SOCIAL]
    assert DECISIONS not in [dest for dest, _ in delivered]
    assert first[0]["exact_tuple"] == source["exact_tuple"]
    assert first[0]["attachments"] == source["attachments"]
    assert json.dumps(source, ensure_ascii=False) in delivered[0][1]
    assert snapshot(tmp_path, sop, [source], handled=[source["dedup_key"]]) == []


@pytest.mark.parametrize("channel,kind,dest", [
    ("C0ALZJHGE49", "app_mention", DECISIONS),
    ("COTHER", "app_mention", DECISIONS),
    ("Dsynthetic", "message.im", "01a0911a-a718-7743-b37e-e785f24f3708"),
    ("C09TYQC1DEF", "app_mention", "01a02002-6986-7953-ab5a-cc8087476873"),
])
def test_other_routes_unchanged(tmp_path, channel, kind, dest):
    sop = configured_sop(tmp_path)
    assert snapshot(tmp_path, sop, [event(sop, channel=channel, kind=kind)])[0]["relay_route"]["destination_thread_id"] == dest


@pytest.mark.parametrize("text", ["status only", "> <@U0BNFGX2UAX> quoted", "`<@U0BNFGX2UAX>`", "```\n<@U0BNFGX2UAX>\n```", ""])
def test_no_inferred_growth_addressing(tmp_path, text):
    sop = configured_sop(tmp_path)
    source = event(sop, text=text)
    source["previous_thread_message"]["text"] = "<@U0BNFGX2UAX> parent only"
    assert snapshot(tmp_path, sop, [source]) == []


@pytest.mark.parametrize("ts,queued", [("999.9", "1970-01-01T00:16:41Z"), ("1001.0", "1970-01-01T00:16:39Z"), ("999.9", None)])
def test_old_admission_never_changes_owner(tmp_path, ts, queued):
    sop = configured_sop(tmp_path)
    source = event(sop, ts=ts, queued_at=queued)
    assert snapshot(tmp_path, sop, [source])[0]["relay_route"]["destination_thread_id"] == DECISIONS


@pytest.mark.parametrize("status", ["delivered", "queued", "uncertain"])
def test_prior_receipt_never_delivers_to_second_owner(tmp_path, status):
    sop = configured_sop(tmp_path)
    source = event(sop)
    receipt = {"receipt_key": source["dedup_key"], "status": status, "destination_thread_id": DECISIONS}
    assert snapshot(tmp_path, sop, [source], receipts=[receipt]) == []
    assert json.loads((tmp_path / "receipts.jsonl").read_text()) == receipt


def test_claim_remains_with_original_owner(tmp_path):
    sop = configured_sop(tmp_path)
    source = event(sop)
    claim = {source["dedup_key"]: {"expires_at_epoch": 9999999999, "owner": "original-decisions-claim"}}
    assert snapshot(tmp_path, sop, [source], claims=claim) == []
    assert json.loads((tmp_path / "claims.json").read_text()) == claim


def test_rollback_keeps_admitted_social_events_on_social(tmp_path):
    sop = configured_sop(tmp_path)
    sop.write_text(sop.read_text().replace('"admitted_after_epoch": 1000', '"admitted_after_epoch": 1000, "admitted_before_epoch": 1002'))
    assert snapshot(tmp_path, sop, [event(sop)])[0]["relay_route"]["destination_thread_id"] == SOCIAL
    later = event(sop, ts="1003.0", queued_at="1970-01-01T00:16:43Z")
    assert snapshot(tmp_path, sop, [later])[0]["relay_route"]["destination_thread_id"] == DECISIONS


def test_managed_route_declares_existing_social_owner():
    table = MOD.extract_route_table(SOP)
    route = table["exact_channel_routes"][GROWTH]
    assert route["destination_thread_id"] == SOCIAL
    # Runtime cutover is stamped live (synced from the deployed SOP); unstamped is also valid.
    assert route["admitted_after_epoch"] is None or float(route["admitted_after_epoch"]) > 0


def test_actual_envelope_writer_retains_current_growth_blocks(tmp_path):
    # Execute only the actual tuple-construction statements, never bridge startup.
    bridge = ASSETS / "bridge/cto-bridge.ts"
    code = r"""
const fs = require('node:fs');
const {transformSync} = require('esbuild');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('  const tuple: EventTuple = {');
const end = source.indexOf('  const attachments:', start);
if (start < 0 || end < 0) throw new Error('actual tuple boundary missing');
const js = transformSync(source.slice(start, end) + '\nreturn tuple;', {loader:'ts'}).code;
const construct = new Function('event','channel','ts','user','botId','text',js);
const blocks = [{type:'rich_text',elements:[{type:'rich_text_section',elements:[{type:'user',user_id:'U0BNFGX2UAX'}]}]}];
console.log(JSON.stringify(construct({blocks,thread_ts:'1000.1'}, 'C0C7U35LBKR','1001.000001','UEQTTB97A',null,'')));
"""
    result = subprocess.run([shutil.which("node") or "node", "-e", code, str(bridge)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    sop = configured_sop(tmp_path)
    source = event(sop, text="")
    source["exact_tuple"] = json.loads(result.stdout)
    frozen = snapshot(tmp_path, sop, [source])
    assert frozen[0]["relay_route"]["destination_thread_id"] == SOCIAL
    assert frozen[0]["exact_tuple"] == source["exact_tuple"]


def test_unknown_receipt_preserves_active_claim(tmp_path):
    sop = configured_sop(tmp_path)
    source = event(sop)
    claim = {source["dedup_key"]: {"expires_at_epoch": 9999999999, "owner": "original-decisions-claim"}}
    assert snapshot(tmp_path, sop, [source], claims=claim, receipts=[{"receipt_key": source["dedup_key"], "status": "uncertain"}]) == []
    assert json.loads((tmp_path / "claims.json").read_text()) == claim


def test_frozen_ingress_origin_and_loop_exclusions():
    code = r"""
const fs = require('node:fs'), assert = require('node:assert/strict');
const {transformSync} = require('esbuild');
function load(name) {
 const source = fs.readFileSync(require('node:path').join(process.argv[1],name),'utf8');
 const js = transformSync(source,{loader:'ts',format:'cjs'}).code;
 const module = {exports:{}}; new Function('module','exports',js)(module,module.exports); return module.exports;
}
const {eventOriginIgnoreReason} = load('cto-event-policy.ts');
const {slackIngressEventType} = load('cto-ingress-policy.ts');
let delivered=0;
for (const event of [{user:'U0BNFGX2UAX'},{user:'synthetic-context-bot'}, {subtype:'message_changed'},{subtype:'message_deleted'}]) {
 if (!eventOriginIgnoreReason(event,'U0BNFGX2UAX','synthetic-context-bot')) delivered++;
}
assert.equal(delivered,0);
assert.equal(slackIngressEventType({channel:'C0ALZJHGE49',text:'status only'},'message'),null);
assert.equal(slackIngressEventType({channel:'C09TYQC1DEF',text:'> <@U0BNFGX2UAX> quoted'},'message'),null);
console.log('6 actual ingress/origin discriminators PASS; fake delivery=0');
"""
    run = subprocess.run([shutil.which("node") or "node", "-e", code, str(ROOT / "test/fixtures/bridge")], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_actual_growth_ingress_dedup_and_self_echo_without_side_effects():
    code = r"""
const fs = require('node:fs'), assert = require('node:assert/strict');
const {transformSync} = require('esbuild');
const source = fs.readFileSync(process.argv[1],'utf8');
const start = source.indexOf('  const channel = event.channel ?? "";');
const end = source.indexOf('  const tuple: EventTuple = {',start);
assert.ok(start >= 0 && end > start);
const fragment = transformSync(source.slice(start,end)+'\nreturn true;',{loader:'ts'}).code;
const seen = new Set(); let admitted=0;
const originSource = fs.readFileSync(process.argv[2],'utf8');
const module = {exports:{}};
new Function('module','exports',transformSync(originSource,{loader:'ts',format:'cjs'}).code)(module,module.exports);
const receive = new Function('event','eventType','contextBotUserId','seen','appUserId','readHandledKeys','readQueuedKeys','remember','eventOriginIgnoreReason','log','requestAppServerDrain',fragment);
function invoke(event,kind) {
 const result = receive(event,kind,'synthetic-context-bot',seen,'U0BNFGX2UAX',()=>new Set(),()=>new Set(),k=>seen.add(k),module.exports.eventOriginIgnoreReason,()=>{},()=>{});
 if(result) admitted++;
}
const original={channel:'C0C7U35LBKR',ts:'2001.1',user:'UEQTTB97A',text:'ordinary unmentioned growth message'};
invoke(original,'message.group'); invoke(original,'app_mention'); assert.equal(admitted,1);
invoke({...original,ts:'2001.2',user:'U0BNFGX2UAX'},'message.group');
invoke({...original,ts:'2001.3',subtype:'message_changed'},'message.group');
invoke({...original,ts:'2001.4',user:'synthetic-context-bot'},'message.group');
assert.equal(admitted,1);
invoke({...original,ts:'2001.5',user:'U0ALEAYCAUT',bot_id:'synthetic-pm-bot',subtype:'bot_message'},'message.group');
assert.equal(admitted,2);
console.log('actual pre-await channel:ts/origin boundary PASS; transport mutations=0');
"""
    run = subprocess.run([shutil.which("node") or "node", "-e", code,
        str(ASSETS / "bridge/cto-bridge.ts"), str(ROOT / "test/fixtures/bridge/cto-event-policy.ts")], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize("expired_claim", [False, True])
def test_actual_fallback_unknown_is_not_reselected(tmp_path, monkeypatch, expired_claim):
    sop = configured_sop(tmp_path)
    source = event(sop)
    frozen = snapshot(tmp_path, sop, [source])[0]
    # The bridge's asRoutedEnvelope binds the helper invocation's claim owner.
    frozen["claim_owner"] = json.loads((tmp_path / "claims.json").read_text())[source["dedup_key"]]["owner"]
    envelope_file = tmp_path / "frozen.json"
    envelope_file.write_text(json.dumps(frozen))
    journal = tmp_path / "cto-ipc-monitor-trigger-receipts.jsonl"
    code = r"""
const fs = require('node:fs'), assert = require('node:assert/strict');
const {transformSync} = require('esbuild');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function appendReceipt(');
const end = source.indexOf('async function drainAppServer()', start);
assert.ok(start >= 0 && end > start);
const js = transformSync(source.slice(start, end), {loader:'ts'}).code;
const envelope = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const journal = process.argv[3], claims = process.argv[4];
let primary = 0, fallback = 0, release = 0;
const bindings = {
 appendFileSync: fs.appendFileSync, MONITOR_TRIGGER_RECEIPTS_FILE: journal,
 SLACK_MONITOR_THREAD_ID: '019fd9df-23ad-7500-8b3e-53ce9341a140',
 SLACK_MONITOR_SOP: 'synthetic-SOP', IPC_SENDER: 'fake-never-executed',
 ipcOwnerDiscoveryArgs: () => [],
 runPython: async (args) => {
  fallback++; assert.equal(args[args.indexOf('--success-receipt-file')+1], journal);
  return {code:3, stdout:JSON.stringify({deliveryState:'uncertain'}), stderr:'synthetic UNKNOWN'};
 },
 appServerClient: {deliver:async () => {primary++; return {status:'not_sent', detail:'synthetic pre-submit'};}},
 updateClaim: async (action,key,owner) => {
  assert.equal(action,'release'); assert.equal(key,envelope.dedup_key); assert.equal(owner,envelope.claim_owner);
  const state = JSON.parse(fs.readFileSync(claims,'utf8')); delete state[key];
  fs.writeFileSync(claims,JSON.stringify(state)); release++; return true;
 },
 setInterval: () => ({unref(){}}), clearInterval: () => {}, CLAIM_RENEW_INTERVAL_MS: 30000,
 log: () => {}, formatError: String, process: {stdout:{write(){}}},
};
const deliver = new Function(...Object.keys(bindings), js+'\nreturn deliverClaimedEnvelope;')(...Object.values(bindings));
(async () => {
 assert.equal(await deliver(envelope), 'pending');
 assert.deepEqual([primary,fallback,release],[1,1,1]);
 const receipt = JSON.parse(fs.readFileSync(journal,'utf8').trim());
 assert.equal(receipt.status,'uncertain'); assert.equal(receipt.receipt_key,envelope.dedup_key);
 console.log('actual writer: primary not_sent -> claim released -> fallback UNKNOWN journal');
})().catch(error => {console.error(error); process.exitCode=1;});
"""
    run = subprocess.run([
        shutil.which("node") or "node", "-e", code,
        str(ASSETS / "bridge/cto-bridge.ts"), str(envelope_file), str(journal),
        str(tmp_path / "claims.json"),
    ], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    receipt_bytes = journal.read_bytes()
    assert json.loads((tmp_path / "claims.json").read_text()) == {}
    if expired_claim:
        (tmp_path / "claims.json").write_text(json.dumps({source["dedup_key"]: {
            "owner": frozen["claim_owner"], "expires_at_epoch": 1,
        }}))
    # Remap the ACTUAL default journals to a fake filesystem, not a custom
    # receipt list that could conceal an omitted production journal.
    monkeypatch.setattr(MOD, "DEFAULT_DELIVERY_RECEIPTS_FILES", tuple(
        tmp_path / path.name for path in MOD.DEFAULT_DELIVERY_RECEIPTS_FILES
    ))
    args = [str(HELPER), "--route-sop", str(sop), "--limit", "1"]
    for option, filename in [
        ("--events-file", "events.json"), ("--ack-file", "ack.json"),
        ("--claims-file", "claims.json"), ("--output", "output.json"),
        ("--heartbeat-file", "heartbeat"), ("--lock-file", "lock"),
    ]:
        args += [option, str(tmp_path / filename)]
    monkeypatch.setattr(sys, "argv", args)
    assert MOD.main() == 0
    assert json.loads((tmp_path / "output.json").read_text()) == []
    assert journal.read_bytes() == receipt_bytes
    assert json.loads((tmp_path / "ack.json").read_text()) == {"handled": []}


@pytest.mark.parametrize("status", ["delivered", "queued"])
def test_successful_monitor_trigger_is_not_destination_acceptance(tmp_path, status):
    journal = tmp_path / "cto-ipc-monitor-trigger-receipts.jsonl"
    journal.write_text(json.dumps({"receipt_key": "synthetic-key", "status": status}) + "\n")
    assert MOD.receipt_handled_keys([journal]) == set()


DM = "01a0911a-a718-7743-b37e-e785f24f3708"


def availability_sop(tmp_path):
    sop = configured_sop(tmp_path)
    sop.write_text(sop.read_text().replace(
        '"admitted_after_epoch": 1000',
        '"admitted_after_epoch": 1000, "availability_gate": {"admitted_after_epoch": 1000}',
    ))
    return sop


def all_messages_sop(tmp_path, cutoff=2000):
    sop = availability_sop(tmp_path)
    text = sop.read_text()
    before, rest = text.split(MOD.ROUTE_TABLE_BEGIN, 1)
    data, after = rest.split(MOD.ROUTE_TABLE_END, 1)
    table = json.loads(data)
    table["exact_channel_routes"][GROWTH]["all_messages_gate"] = {
        "admitted_after_epoch": cutoff,
    }
    sop.write_text(before + MOD.ROUTE_TABLE_BEGIN + json.dumps(table) + MOD.ROUTE_TABLE_END + after)
    return sop


def new_growth_event(sop, text="ordinary growth message", kind="message.group"):
    return event(sop, ts="2001.000001", queued_at="1970-01-01T00:33:21Z", text=text, kind=kind)


@pytest.mark.parametrize("activity", ["busy", "idle", "queued", "notLoaded", "unknown", "unavailable"])
@pytest.mark.parametrize("text", ["ordinary growth message", "<@U0BNFGX2UAX> growth message"])
def test_historical_all_messages_routes_mentions_to_decisions_and_others_to_social_pool(tmp_path, monkeypatch, activity, text):
    # Rajiv 1793200758.332809 superseded the old no-probe/Social-only rule.
    sop = all_messages_sop(tmp_path)
    probes = []
    def observe(thread_id):
        probes.append(thread_id)
        return {"state": activity, "reason": "synthetic-" + activity}
    monkeypatch.setattr(MOD, "snapshot_social_availability", observe)
    source = new_growth_event(sop, text)
    source["exact_tuple"]["blocks"] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    first = snapshot(tmp_path, sop, [source], in_process=True)[0]
    if "<@U0BNFGX2UAX>" in text:
        assert first["relay_route"]["destination_thread_id"] == DECISIONS
        assert first["relay_route"]["match"] == "growth_cto_mention"
        assert "availability" not in first["relay_route"]
        assert probes == []
    else:
        assert first["relay_route"]["destination_thread_id"] == SOCIAL
        assert first["relay_route"]["match"] == "growth_all_messages"
        assert first["relay_route"]["availability"]["state"] == activity
        pool = MOD.extract_route_table(sop)["overflow_pool"]
        assert probes == ([SOCIAL] if activity == "idle" else
                          [SOCIAL] + [member["destination_thread_id"] for member in pool
                                      if member["destination_thread_id"] != SOCIAL])
    assert first["exact_tuple"] == source["exact_tuple"]
    assert first["attachments"] == source["attachments"]
    assert json.dumps(source, ensure_ascii=False) in first["routed_wake_text"]
    claims = json.loads((tmp_path / "claims.json").read_text())
    assert claims[source["dedup_key"]]["relay_route"] == first["relay_route"]


@pytest.mark.parametrize("user,kind", [("UEQTTB97A", "message.channel"), ("U0ALEAYCAUT", "message.group"), ("synthetic-bot", "message.group")])
def test_all_messages_human_pm_bot_and_duplicate_event(tmp_path, user, kind):
    sop = all_messages_sop(tmp_path)
    source = new_growth_event(sop, kind=kind)
    source["exact_tuple"]["user"] = user
    source["source_evidence"]["explicit_cto_mention"] = False
    assert snapshot(tmp_path, sop, [source])[0]["relay_route"]["destination_thread_id"] == SOCIAL
    # Slack's message/app_mention copies share channel:ts; ACK/receipt wins.
    duplicate = json.loads(json.dumps(source))
    duplicate["source_evidence"]["slack_event_type"] = "app_mention"
    assert snapshot(tmp_path, sop, [duplicate], handled=[source["dedup_key"]]) == []


@pytest.mark.parametrize("ts,queued", [("1999.9", "1970-01-01T00:33:21Z"), ("2001.0", "1970-01-01T00:33:19Z"), ("2001.0", None)])
def test_all_messages_does_not_drain_old_unmentioned_backlog(tmp_path, monkeypatch, ts, queued):
    sop = all_messages_sop(tmp_path)
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("unmentioned history"))
    source = event(sop, ts=ts, queued_at=queued, text="unmentioned old chatter", kind="message.group")
    assert snapshot(tmp_path, sop, [source], in_process=True) == []


def test_all_messages_frozen_dm_owner_and_unmentioned_social_survive_restart(tmp_path, monkeypatch):
    sop = all_messages_sop(tmp_path)
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: {"state": "unknown", "reason": "native-status-notLoaded"})
    old = event(sop)
    frozen = snapshot(tmp_path, sop, [old], in_process=True)[0]
    claims = json.loads((tmp_path / "claims.json").read_text())
    claims[old["dedup_key"]]["expires_at_epoch"] = 0
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("frozen owner"))
    assert snapshot(tmp_path, sop, [old], claims=claims)[0]["relay_route"] == frozen["relay_route"]
    assert frozen["relay_route"]["destination_thread_id"] == SOCIAL
    source = new_growth_event(sop)
    selected = snapshot(tmp_path, sop, [source])[0]
    claims = json.loads((tmp_path / "claims.json").read_text())
    claims[source["dedup_key"]]["expires_at_epoch"] = 0
    # Closing only the future interval must not revoke a frozen unmentioned route.
    sop.write_text(sop.read_text().replace('"admitted_after_epoch": 2000', '"admitted_after_epoch": 2000, "admitted_before_epoch": 2000.5'))
    assert snapshot(tmp_path, sop, [source], claims=claims)[0]["relay_route"] == selected["relay_route"]


@pytest.mark.parametrize("status", ["queued", "delivered", "uncertain"])
def test_all_messages_existing_transport_history_never_replayed(tmp_path, status):
    sop = all_messages_sop(tmp_path)
    source = new_growth_event(sop)
    receipt = {"receipt_key": source["dedup_key"], "status": status, "destination_thread_id": DM}
    assert snapshot(tmp_path, sop, [source], receipts=[receipt]) == []
    assert json.loads((tmp_path / "receipts.jsonl").read_text()) == receipt


def test_all_messages_inert_and_non_growth_routes_unchanged(tmp_path, monkeypatch):
    sop = all_messages_sop(tmp_path, None)
    probes = []
    # Inert growth and unmentioned messages must never probe. Direct messages now probe their own owner
    # for overflow (Rajiv C0C7U35LBKR/1792670433.869629, ts 1792689875.791679) and stay put when idle.
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda t: probes.append(t) or {"state": "idle", "reason": "synthetic"})
    assert snapshot(tmp_path, sop, [new_growth_event(sop)], in_process=True) == []
    assert probes == []
    for channel, kind, dest in [("C0ALZJHGE49", "app_mention", DECISIONS), ("C09TYQC1DEF", "app_mention", "01a02002-6986-7953-ab5a-cc8087476873"), ("Dsynthetic", "message.im", DM)]:
        source = event(sop, channel=channel, kind=kind)
        assert snapshot(tmp_path, sop, [source], in_process=True)[0]["relay_route"]["destination_thread_id"] == dest
    for channel in ["C0ALZJHGE49", "C09TYQC1DEF"]:
        assert snapshot(tmp_path, sop, [event(sop, channel=channel, text="unmentioned")], in_process=True) == []


@pytest.mark.parametrize("cutoff", [True, "2000", -1, float("nan"), float("inf")])
def test_all_messages_malformed_cutoff_refuses(tmp_path, cutoff):
    sop = all_messages_sop(tmp_path, cutoff)
    with pytest.raises(MOD.SnapshotError, match="all_messages_gate"):
        MOD.extract_route_table(sop)


def test_all_messages_release_renew_and_output_failure_preserve_choice(tmp_path, monkeypatch):
    sop = all_messages_sop(tmp_path)
    source = new_growth_event(sop)
    original_write = MOD.write_json_atomic
    def fail_output(path, value):
        if path.name == "output.json" and value:
            raise OSError("synthetic output failure after claim fsync")
        original_write(path, value)
    monkeypatch.setattr(MOD, "write_json_atomic", fail_output)
    with pytest.raises(AssertionError):
        snapshot(tmp_path, sop, [source], in_process=True)
    claims = json.loads((tmp_path / "claims.json").read_text())
    chosen = claims[source["dedup_key"]]
    assert chosen["relay_route"]["destination_thread_id"] == SOCIAL
    monkeypatch.setattr(MOD, "write_json_atomic", original_write)
    for action in ["renew", "release"]:
        args = [sys.executable, str(HELPER), "--route-sop", str(sop), "--" + action + "-key", source["dedup_key"], "--claim-owner", chosen["owner"]]
        for flag, name in [("--ack-file", "ack.json"), ("--claims-file", "claims.json"), ("--heartbeat-file", "heartbeat"), ("--lock-file", "lock")]:
            args.extend([flag, str(tmp_path / name)])
        result = subprocess.run(args, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        kept = json.loads((tmp_path / "claims.json").read_text())[source["dedup_key"]]
        assert kept["relay_route"] == chosen["relay_route"]
        assert kept["route_fingerprint"] == source["fingerprint"]


@pytest.mark.parametrize("text,admitted", [
    ("ordinary growth message", False),
    ("> <@U0BNFGX2UAX> quoted", False),
    ("`<@U0BNFGX2UAX>`", False),
    ("<@U0BNFGX2UAX> current mention", True),
])
def test_closed_all_message_window_admits_only_current_mentions(tmp_path, monkeypatch, text, admitted):
    # Exercise the managed table, not an indefinitely open synthetic cutover.
    probes = []
    def observe(thread_id):
        probes.append(thread_id)
        return {"state": "busy" if thread_id == SOCIAL else "idle", "reason": "synthetic"}
    monkeypatch.setattr(MOD, "snapshot_social_availability", observe)
    source = event(SOP, ts="1793205480.000001", queued_at="2026-10-28T16:38:00Z", text=text)
    routed = snapshot(tmp_path, SOP, [source], in_process=True)
    if admitted:
        assert routed[0]["relay_route"]["match"] == "growth_availability"
        assert routed[0]["relay_route"]["destination_thread_id"] == "01a18172-fae0-7ff0-8aad-2d41ef153dc6"
        assert probes == [SOCIAL, "01a18172-fae0-7ff0-8aad-2d41ef153dc6"]
    else:
        assert routed == []
        assert probes == []


def test_direct_messages_never_probe_or_overflow_to_social(tmp_path, monkeypatch):
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("DM overflow forbidden"))
    source = event(SOP, channel="Dsynthetic", kind="message.im")
    routed = snapshot(tmp_path, SOP, [source], in_process=True)[0]
    assert routed["relay_route"]["destination_thread_id"] == DM
    assert routed["relay_route"]["match"] == "direct_message"


def test_all_messages_managed_branch_and_cutover_contract():
    table = MOD.extract_route_table(SOP)
    growth = table["exact_channel_routes"][GROWTH]
    # Rajiv 1793205393.094809 closed the historical all-message interval.
    assert growth["all_messages_gate"] == {
        "admitted_after_epoch": 1793201061.427839,
        "admitted_before_epoch": 1793205479.293347,
    }
    assert [member["destination_thread_id"] for member in table["overflow_pool"]] == [
        SOCIAL, "01a18172-fae0-7ff0-8aad-2d41ef153dc6",
        "01a18174-ac86-7860-878d-a6f75c9a18c7",
    ]
    assert growth["admitted_after_epoch"] == 1791644828.031685
    assert growth["availability_gate"]["admitted_after_epoch"] == 1792508086.3273861
    instructions = SOP.read_text().split("### Historical availability cutover", 1)[0]
    for required in ["PROC_UID_ONLY=4", "second", "NOTE_EXIT", "INERT", "all_messages_gate.admitted_after_epoch", "all_messages_gate.admitted_before_epoch", "1792673034.611319", "1792675705.475119"]:
        assert required in instructions
    consumer = (ASSETS / "codex/monitors/heydonna-social-media/WAKE_SOP.md").read_text()
    assert "with or without a CTO mention" in consumer
    assert "No activity probe or CTO DM diversion" in consumer


@pytest.mark.parametrize("state,expected", [
    ("idle", SOCIAL), ("busy", SOCIAL), ("notLoaded", SOCIAL),
    ("unavailable", SOCIAL), ("unknown", SOCIAL),
])
def test_new_growth_selects_only_authoritative_idle(tmp_path, monkeypatch, state, expected):
    sop = availability_sop(tmp_path)
    calls = []
    def activity(thread_id):
        calls.append(thread_id)
        return {"state": state, "reason": "synthetic-" + state}
    monkeypatch.setattr(MOD, "snapshot_social_availability", activity, raising=False)
    source = event(sop)
    source["exact_tuple"]["blocks"] = [{"type": "section", "text": {"type": "mrkdwn", "text": "full block"}}]
    routed = snapshot(tmp_path, sop, [source], in_process=True)[0]
    assert routed["relay_route"]["destination_thread_id"] == expected
    assert routed["relay_route"]["availability"]["state"] == state
    assert calls[0] == SOCIAL and (calls == [SOCIAL] or state != "idle")  # non-idle also probes the overflow pool (Rajiv 1792689875.791679); stub marks them non-idle so Social keeps it
    assert routed["attachments"] == source["attachments"]
    assert routed["exact_tuple"] == source["exact_tuple"]
    assert json.dumps(source, ensure_ascii=False) in routed["routed_wake_text"]
    if expected == DM:
        assert routed["routed_wake_text"].startswith("SOP path: /Users/rajiv/.codex/monitors/cto-dms/WAKE_SOP.md\n")
    claim = json.loads((tmp_path / "claims.json").read_text())[source["dedup_key"]]
    assert claim["relay_route"] == routed["relay_route"]
    assert claim["route_fingerprint"] == source["fingerprint"]


@pytest.mark.parametrize("state,expected", [("idle", SOCIAL), ("busy", SOCIAL)])
def test_frozen_destination_survives_expiry_restart_and_activity_change(tmp_path, monkeypatch, state, expected):
    sop = availability_sop(tmp_path)
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: {"state": state, "reason": state}, raising=False)
    source = event(sop)
    first = snapshot(tmp_path, sop, [source], in_process=True)[0]
    claims = json.loads((tmp_path / "claims.json").read_text())
    claims[source["dedup_key"]]["expires_at_epoch"] = 1
    def forbidden(_):
        raise AssertionError("frozen destination must never resnapshot activity")
    monkeypatch.setattr(MOD, "snapshot_social_availability", forbidden, raising=False)
    second = snapshot(tmp_path, sop, [source], claims=claims, in_process=True)[0]
    assert second["relay_route"] == first["relay_route"]
    assert second["relay_route"]["destination_thread_id"] == expected


@pytest.mark.parametrize("status", ["queued", "delivered", "uncertain"])
def test_activity_never_consulted_for_existing_receipt(tmp_path, monkeypatch, status):
    sop = availability_sop(tmp_path)
    def forbidden(_):
        raise AssertionError("existing receipts cannot be reselected")
    monkeypatch.setattr(MOD, "snapshot_social_availability", forbidden, raising=False)
    source = event(sop)
    assert snapshot(tmp_path, sop, [source], receipts=[{
        "receipt_key": source["dedup_key"], "status": status,
        "destination_thread_id": SOCIAL,
    }], in_process=True) == []


def test_availability_cutoff_does_not_migrate_old_admissions(tmp_path, monkeypatch):
    sop = availability_sop(tmp_path)
    sop.write_text(sop.read_text().replace(
        '"availability_gate": {"admitted_after_epoch": 1000}',
        '"availability_gate": {"admitted_after_epoch": 1002}',
    ))
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("old admission"), raising=False)
    assert snapshot(tmp_path, sop, [event(sop)], in_process=True)[0]["relay_route"]["destination_thread_id"] == SOCIAL


@pytest.mark.parametrize("kind,queue,expected", [
    ("idle", {"data": [], "nextCursor": None}, "idle"),
    ("idle", {"data": [{"id": "accepted"}], "nextCursor": None}, "busy"),
    ("idle", {"data": [], "nextCursor": "next-page"}, "busy"),
    ("idle", {"data": []}, "unknown"),
    ("idle", {"data": None, "nextCursor": None}, "unknown"),
    ("active", None, "busy"), ("notLoaded", None, "unknown"),
    ("systemError", None, "unknown"), ("future-state", None, "unknown"),
])
def test_native_availability_requires_exact_idle_and_empty_queue(kind, queue, expected):
    assert MOD.availability_from_snapshot(SOCIAL, {"id": SOCIAL, "status": {"type": kind}}, queue)["state"] == expected
    assert MOD.availability_from_snapshot(SOCIAL, {"id": DM, "status": {"type": kind}}, queue)["state"] == "unknown"


@pytest.mark.parametrize("kind,expected", [("idle", "idle"), ("active", "busy"), ("notLoaded", "unknown"), ("rpc-error", "unknown")])
def test_actual_native_adapter_only_sends_bounded_read_methods(tmp_path, monkeypatch, kind, expected):
    # A fake app-server executable drives the actual RPC reader/adapter.
    # Never invoke Codex or a model in this test.
    audit = tmp_path / "methods.jsonl"
    fake = tmp_path / "fake-codex"
    fake.write_text("#!" + sys.executable + "\n" + f'''
import json,sys
assert sys.argv[1:] == ['app-server','--stdio']
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    with open({str(audit)!r}, 'a') as log:
        log.write(json.dumps(request) + '\\n')
    if method == 'initialized': continue
    if method == 'initialize': result = {{}}
    elif method == 'thread/read':
        assert request['params'] == {{'threadId': {SOCIAL!r}, 'includeTurns': False}}
        if {kind!r} == 'rpc-error':
            print(json.dumps({{'id':request['id'],'error':{{'code':-1,'message':'unavailable'}}}}),flush=True)
            continue
        result = {{'thread':{{'id':{SOCIAL!r},'status':{{'type':{kind!r}}}}}}}
    elif method == 'thread/queue/list':
        assert request['params'] == {{'threadId':{SOCIAL!r},'limit':1}}
        result = {{'data':[],'nextCursor':None}}
    else: raise AssertionError('MUTATION: ' + method)
    print(json.dumps({{'id':request['id'],'result':result}}),flush=True)
''')
    fake.chmod(0o755)
    monkeypatch.setattr(MOD, "CODEX_BIN", str(fake))
    # Hermetic: notLoaded falls back to the persisted rollout lifecycle, which must not read the host's real Codex state.
    monkeypatch.setattr(MOD, "persisted_activity", lambda _t: {"state": "unknown", "reason": "synthetic-no-persisted-state"})
    monkeypatch.setattr(MOD, "QUEUE_RPC_HELPER", ASSETS / "codex/skills/codex-stdio-send-message/scripts/queue_preview.py")
    actual = MOD.snapshot_social_availability(SOCIAL)
    assert actual["state"] == expected
    methods = [json.loads(line)["method"] for line in audit.read_text().splitlines()]
    assert methods == ["initialize", "initialized", "thread/read"] + (["thread/queue/list"] if kind == "idle" else [])
    assert actual["methods"] == methods


def test_claim_release_and_renew_retain_frozen_route(tmp_path, monkeypatch):
    sop = availability_sop(tmp_path)
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: {"state": "busy", "reason": "active"})
    source = event(sop)
    first = snapshot(tmp_path, sop, [source], in_process=True)[0]
    claim = json.loads((tmp_path / "claims.json").read_text())[source["dedup_key"]]
    for action in ("--renew-key", "--release-key"):
        monkeypatch.setattr(sys, "argv", [str(HELPER), "--route-sop", str(sop), action, source["dedup_key"], "--claim-owner", claim["owner"],
            "--ack-file", str(tmp_path / "ack.json"), "--claims-file", str(tmp_path / "claims.json"),
            "--lock-file", str(tmp_path / "lock"), "--heartbeat-file", str(tmp_path / "heartbeat")])
        assert MOD.main() == 0
        stored = json.loads((tmp_path / "claims.json").read_text())[source["dedup_key"]]
        assert stored["relay_route"] == first["relay_route"]
    assert stored["expires_at_epoch"] == 0
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("released route reselected"))
    assert snapshot(tmp_path, sop, [source], claims={source["dedup_key"]: stored}, in_process=True)[0]["relay_route"] == first["relay_route"]


def test_frozen_route_persisted_before_output_and_reused_after_failure(tmp_path, monkeypatch, capsys):
    sop = availability_sop(tmp_path)
    source = event(sop)
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: {"state": "busy", "reason": "active"})
    original = MOD.write_json_atomic
    def fail_snapshot(path, value):
        if path == tmp_path / "output.json" and value:
            stored = json.loads((tmp_path / "claims.json").read_text())[source["dedup_key"]]
            assert stored["relay_route"]["destination_thread_id"] == SOCIAL
            raise OSError("synthetic output failure after durable choice")
        original(path, value)
    monkeypatch.setattr(MOD, "write_json_atomic", fail_snapshot)
    with pytest.raises(AssertionError):
        snapshot(tmp_path, sop, [source], in_process=True)
    assert "synthetic output failure" in capsys.readouterr().err
    assert json.loads((tmp_path / "output.json").read_text()) == []
    claims = json.loads((tmp_path / "claims.json").read_text())
    claims[source["dedup_key"]]["expires_at_epoch"] = 1
    monkeypatch.setattr(MOD, "write_json_atomic", original)
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("choice must be reused"))
    assert snapshot(tmp_path, sop, [source], claims=claims, in_process=True)[0]["relay_route"]["destination_thread_id"] == SOCIAL


def test_frozen_fingerprint_drift_refuses_without_reselection(tmp_path, monkeypatch, capsys):
    sop = availability_sop(tmp_path)
    source = event(sop)
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: {"state": "idle", "reason": "idle"})
    snapshot(tmp_path, sop, [source], in_process=True)
    claims = json.loads((tmp_path / "claims.json").read_text())
    claims[source["dedup_key"]]["expires_at_epoch"] = 1
    source["fingerprint"] += "-different"
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("fingerprint drift"))
    with pytest.raises(AssertionError):
        snapshot(tmp_path, sop, [source], claims=claims, in_process=True)
    assert "frozen route fingerprint mismatch" in capsys.readouterr().err
    assert json.loads((tmp_path / "output.json").read_text()) == []
    assert json.loads((tmp_path / "claims.json").read_text()) == claims


def test_new_rule_does_not_consult_activity_for_ineligible_or_other_routes(tmp_path, monkeypatch):
    sop = availability_sop(tmp_path)
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("unrelated/mention eligibility"))
    assert snapshot(tmp_path, sop, [event(sop, text="> <@U0BNFGX2UAX> quote only")], in_process=True) == []
    assert snapshot(tmp_path, sop, [event(sop, channel="C09TYQC1DEF")], in_process=True)[0]["relay_route"]["destination_thread_id"] == "01a02002-6986-7953-ab5a-cc8087476873"


def test_gate_rollback_preserves_frozen_dms_owner(tmp_path, monkeypatch):
    sop = availability_sop(tmp_path)
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: {"state": "busy", "reason": "active"})
    source = event(sop)
    first = snapshot(tmp_path, sop, [source], in_process=True)[0]
    claims = json.loads((tmp_path / "claims.json").read_text())
    claims[source["dedup_key"]]["expires_at_epoch"] = 1
    sop.write_text(sop.read_text().replace('"availability_gate": {"admitted_after_epoch": 1000}',
        '"availability_gate": {"admitted_after_epoch": 1000, "admitted_before_epoch": 1002}'))
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("rollback must not resnapshot"))
    assert snapshot(tmp_path, sop, [source], claims=claims, in_process=True)[0]["relay_route"] == first["relay_route"]


@pytest.mark.parametrize("expires", [1, 9999999999])
def test_legacy_claim_owner_never_reselected(tmp_path, monkeypatch, expires, capsys):
    sop = availability_sop(tmp_path)
    source = event(sop)
    claims = {source["dedup_key"]: {"expires_at_epoch": expires, "owner": "original-owner"}}
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("existing owner"))
    if expires == 1:
        with pytest.raises(AssertionError):
            snapshot(tmp_path, sop, [source], claims=claims, in_process=True)
        assert "existing growth claim has no frozen destination" in capsys.readouterr().err
    else:
        assert snapshot(tmp_path, sop, [source], claims=claims, in_process=True) == []
    assert json.loads((tmp_path / "claims.json").read_text()) == claims


@pytest.mark.parametrize("state", ["queued", "uncertain"])
def test_receipt_suppresses_even_expired_frozen_choice(tmp_path, monkeypatch, state):
    sop = availability_sop(tmp_path)
    source = event(sop)
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: {"state": "idle", "reason": "idle"})
    snapshot(tmp_path, sop, [source], in_process=True)
    claims = json.loads((tmp_path / "claims.json").read_text())
    claims[source["dedup_key"]]["expires_at_epoch"] = 1
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("already accepted/uncertain"))
    assert snapshot(tmp_path, sop, [source], claims=claims, receipts=[{
        "receipt_key": source["dedup_key"], "status": state,
    }], in_process=True) == []
    assert json.loads((tmp_path / "claims.json").read_text()) == claims


def test_fresh_process_reuses_frozen_social_without_activity(tmp_path, monkeypatch):
    sop = availability_sop(tmp_path)
    source = event(sop)
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: {"state": "idle", "reason": "idle"})
    first = snapshot(tmp_path, sop, [source], in_process=True)[0]
    claims = json.loads((tmp_path / "claims.json").read_text())
    claims[source["dedup_key"]]["expires_at_epoch"] = 1
    (tmp_path / "claims.json").write_text(json.dumps(claims))
    code = '''
import importlib.util,sys
spec=importlib.util.spec_from_file_location('fresh_relay',sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.snapshot_social_availability=lambda _: sys.exit(99)
sys.argv=sys.argv[1:]
sys.exit(module.main())
'''
    args = [sys.executable, "-c", code, str(HELPER), "--route-sop", str(sop), "--limit", "1"]
    for option, filename in [("--events-file", "events.json"), ("--ack-file", "ack.json"),
        ("--claims-file", "claims.json"), ("--output", "output.json"),
        ("--heartbeat-file", "heartbeat"), ("--lock-file", "lock"), ("--delivery-receipts-file", "receipts.jsonl")]:
        args += [option, str(tmp_path / filename)]
    run = subprocess.run(args, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads((tmp_path / "output.json").read_text())[0]["relay_route"] == first["relay_route"]


@pytest.mark.parametrize("gate", [None, {"admitted_after_epoch": None}])
def test_unstamped_availability_gate_is_inert(tmp_path, monkeypatch, gate):
    sop = configured_sop(tmp_path)
    if gate:
        sop.write_text(sop.read_text().replace('"admitted_after_epoch": 1000',
            '"admitted_after_epoch": 1000, "availability_gate": {"admitted_after_epoch": null}'))
    monkeypatch.setattr(MOD, "snapshot_social_availability", lambda _: pytest.fail("unstamped policy"))
    assert snapshot(tmp_path, sop, [event(sop)], in_process=True)[0]["relay_route"]["destination_thread_id"] == SOCIAL


def test_cutover_contract_requires_inert_stage_and_parent_exit_barrier():
    instructions = " ".join(SOP.read_text().split())
    for required in (
        "Phase 1: inert helper installation", "Phase 2: parent-writer drain and activation",
        "source-load/exec race", "kernel birth identity", "NOTE_EXIT",
        "second complete census", "PARENT_WRITER_DRAIN_UNPROVEN",
        "Do not activate while holding the lock that a parent needs to finish",
    ):
        assert required in instructions


@pytest.mark.parametrize("operation", ["select", "renew", "release"])
@pytest.mark.parametrize("policy", ["availability", "all_messages"])
def test_two_phase_cutover_drains_preloaded_parent_before_activation(tmp_path, operation, policy):
    import fcntl
    import hashlib
    import os
    import select

    source_path = "scripts/pm/shared-assets/claude/scripts/cto-relay-snapshot.py"
    parent_commit = "320a7a7ca800160f43d94b2ba3393d3b62a4ee51" if policy == "all_messages" else "b7fe860a648d0ab05645424e0bb744c861c93e04"
    parent = subprocess.check_output(["git", "show", parent_commit + ":" + source_path], cwd=ROOT)
    staged_helper = tmp_path / "helper.py"
    staged_helper.write_bytes(parent)
    gate_name = policy + "_gate"
    cutoff = 2000 if policy == "all_messages" else 1000
    sop = all_messages_sop(tmp_path) if policy == "all_messages" else availability_sop(tmp_path)
    sop.write_text(sop.read_text().replace('"' + gate_name + '": {"admitted_after_epoch": ' + str(cutoff) + '}',
        '"' + gate_name + '": {"admitted_after_epoch": null}'))
    key = "synthetic-parent"
    initial_claim = {key: {"owner": "parent-owner", "expires_at_epoch": 9999999999}}
    for name, value in [("events", {"events": []}), ("ack", {"handled": []}), ("claims", initial_claim)]:
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    (tmp_path / "receipts.jsonl").write_text("")
    args = [str(staged_helper), "--route-sop", str(sop), "--limit", "1"]
    for option, filename in [("--events-file", "events.json"), ("--ack-file", "ack.json"),
        ("--claims-file", "claims.json"), ("--output", "output.json"),
        ("--heartbeat-file", "heartbeat"), ("--lock-file", "lock"), ("--delivery-receipts-file", "receipts.jsonl")]:
        args += [option, str(tmp_path / filename)]
    if operation != "select":
        args += ["--" + operation + "-key", key, "--claim-owner", "parent-owner"]

    # The CHILD loads actual parent source before it asks for flock. Its pipe
    # witness identifies those loaded bytes, not merely the current file path.
    preload = '''
import sys,os,pathlib,hashlib,fcntl,json
source=pathlib.Path(sys.argv[1]).read_bytes()
ns={'__name__':'preloaded_parent'}
exec(compile(source,sys.argv[1],'exec'),ns)
real_flock=fcntl.flock
def witness(fd,operation):
    os.write(int(os.environ['READY_FD']),json.dumps({'pid':os.getpid(),'loaded_sha':hashlib.sha256(source).hexdigest()}).encode()+b'\\n')
    return real_flock(fd,operation)
fcntl.flock=witness
sys.argv=sys.argv[1:]
sys.exit(ns['main']())
'''
    read_fd, write_fd = os.pipe()
    lock = (tmp_path / "lock").open("a+")
    process = None
    activated = False
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        process = subprocess.Popen([sys.executable, "-c", preload, *args],
            env={**os.environ, "READY_FD": str(write_fd)}, pass_fds=(write_fd,),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        os.close(write_fd)
        write_fd = None
        assert select.select([read_fd], [], [], 5)[0], "parent did not reach the temporary lock"
        loaded = json.loads(os.read(read_fd, 4096))
        assert loaded == {"pid": process.pid, "loaded_sha": hashlib.sha256(parent).hexdigest()}
        assert process.poll() is None

        replacement = tmp_path / "candidate.py"
        replacement.write_bytes(HELPER.read_bytes())
        os.replace(replacement, staged_helper)
        table = MOD.extract_route_table(sop)
        assert table["exact_channel_routes"][GROWTH][gate_name]["admitted_after_epoch"] is None
        assert process.poll() is None  # Installing new bytes did NOT drain old code.
        assert not activated
        fcntl.flock(lock, fcntl.LOCK_UN)
        out, err = process.communicate(timeout=5)  # Owned child's kernel exit/reap witness.
        assert process.returncode == 0, err
        parent_exit = (process.pid, process.returncode)

        fcntl.flock(lock, fcntl.LOCK_EX)
        assert parent_exit == (loaded["pid"], 0)
        assert process.poll() == 0
        assert staged_helper.read_bytes() == HELPER.read_bytes()
        assert MOD.extract_route_table(sop)["exact_channel_routes"][GROWTH][gate_name]["admitted_after_epoch"] is None
        active = sop.read_text().replace('"' + gate_name + '": {"admitted_after_epoch": null}',
            '"' + gate_name + '": {"admitted_after_epoch": ' + str(cutoff) + '}')
        replacement.write_text(active)
        os.replace(replacement, sop)
        activated = True
        fcntl.flock(lock, fcntl.LOCK_UN)

        # A new, separate interpreter loads only staged candidate code. No real
        # activity RPC, source event, queue, receipt or task is touched.
        source = new_growth_event(sop) if policy == "all_messages" else event(sop)
        (tmp_path / "events.json").write_text(json.dumps({"events": [source]}))
        candidate = '''
import pathlib,sys
ns={'__name__':'candidate'}
exec(compile(pathlib.Path(sys.argv[1]).read_bytes(),sys.argv[1],'exec'),ns)
ns['snapshot_social_availability']=lambda _: {'state':'busy','reason':'synthetic-active'}
sys.argv=sys.argv[1:]
sys.exit(ns['main']())
'''
        candidate_args = args if operation == "select" else args[:-4]
        run = subprocess.run([sys.executable, "-c", candidate, *candidate_args], capture_output=True, text=True, timeout=5)
        assert run.returncode == 0, run.stderr
        stored = json.loads((tmp_path / "claims.json").read_text())[source["dedup_key"]]
        assert stored["relay_route"]["destination_thread_id"] == SOCIAL
        assert stored["route_fingerprint"] == source["fingerprint"]
        assert process.poll() == 0  # Parent cannot write after activation.
        assert activated
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        if process is not None and process.poll() is None:
            process.kill()  # Only a failed fixture's owned synthetic child.
            process.communicate(timeout=5)
