"""Actual snapshot helper, synthetic state and fake transport; no live effects."""
import importlib.util
import json
import subprocess
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


def snapshot(tmp_path, sop, events, handled=(), claims=None, receipts=()):
    for name, obj in [("events", {"events": events}), ("ack", {"handled": list(handled)}), ("claims", claims or {})]:
        (tmp_path / f"{name}.json").write_text(json.dumps(obj))
    receipt_file = tmp_path / "receipts.jsonl"
    receipt_file.write_text("".join(json.dumps(r) + "\n" for r in receipts))
    args = [sys.executable, str(HELPER), "--route-sop", str(sop), "--limit", "1"]
    for option, filename in [("--events-file", "events.json"), ("--ack-file", "ack.json"), ("--claims-file", "claims.json"), ("--output", "output.json"), ("--heartbeat-file", "heartbeat"), ("--lock-file", "lock"), ("--delivery-receipts-file", "receipts.jsonl")]:
        args += [option, str(tmp_path / filename)]
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
    # An unstamped source candidate cannot accidentally cut over runtime.
    assert route["admitted_after_epoch"] is None


def test_actual_envelope_writer_retains_current_growth_blocks(tmp_path):
    # Execute only the actual tuple-construction statements, never bridge startup.
    bridge = ASSETS / "bridge/cto-bridge.ts"
    code = r"""
const fs = require('node:fs');
const {transformSync} = require('/Users/rajiv/Downloads/projects/tmux-slack-bridge/node_modules/esbuild/lib/main.js');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('  const tuple: EventTuple = {');
const end = source.indexOf('  const attachments:', start);
if (start < 0 || end < 0) throw new Error('actual tuple boundary missing');
const js = transformSync(source.slice(start, end) + '\nreturn tuple;', {loader:'ts'}).code;
const construct = new Function('event','channel','ts','user','botId','text',js);
const blocks = [{type:'rich_text',elements:[{type:'rich_text_section',elements:[{type:'user',user_id:'U0BNFGX2UAX'}]}]}];
console.log(JSON.stringify(construct({blocks,thread_ts:'1000.1'}, 'C0C7U35LBKR','1001.000001','UEQTTB97A',null,'')));
"""
    result = subprocess.run(["/opt/homebrew/bin/node", "-e", code, str(bridge)], capture_output=True, text=True)
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


def test_actual_ingress_origin_and_loop_exclusions():
    code = r"""
const fs = require('node:fs'), assert = require('node:assert/strict');
const {transformSync} = require('/Users/rajiv/Downloads/projects/tmux-slack-bridge/node_modules/esbuild/lib/main.js');
function load(name) {
 const source = fs.readFileSync('/Users/rajiv/Downloads/projects/tmux-slack-bridge/'+name,'utf8');
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
    run = subprocess.run(["/opt/homebrew/bin/node", "-e", code], capture_output=True, text=True)
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
const {transformSync} = require('/Users/rajiv/Downloads/projects/tmux-slack-bridge/node_modules/esbuild/lib/main.js');
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
        "/opt/homebrew/bin/node", "-e", code,
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
