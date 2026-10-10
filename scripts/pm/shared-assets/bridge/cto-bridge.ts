/**
 * Durable Slack Socket Mode ingress for the CTO Slack monitor task.
 *
 * This persistent LaunchAgent receives, durably queues, minimally verifies,
 * and queues the frozen routed wake to its existing destination task through
 * Codex's supported thread queue. The Slack-monitor task and its minute
 * heartbeat remain the definite-not-sent fallback.
 */
import { App } from "@slack/bolt";
import type { SocketModeReceiver } from "@slack/bolt";
import { appendFileSync, existsSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { createInterface } from "node:readline";
import { eventOriginIgnoreReason } from "./cto-event-policy.ts";
import {
  HEYDONNA_DEV_CHANNEL_ID,
  SUPERPROOFER_CHANNEL_ID,
  hasExplicitCtoMention,
  slackIngressEventType,
  type SlackIngressEventType,
} from "./cto-ingress-policy.ts";
import {
  findPreviousThreadMessage,
  type PreviousThreadMessage,
  type SlackThreadMessage,
} from "./cto-thread-context.ts";
import { ipcOwnerDiscoveryArgs } from "./cto-ipc-policy.ts";
import { materializeSlackImages, type MaterializedAttachment, type SlackFile } from "./cto-file-materialization.ts";
import { buildCtoDurableEnvelope, hasDurableEnvelopeKey } from "./cto-envelope-queue.ts";
import {
  CodexAppServerClient,
  type AppServerDeliveryResult,
} from "./codex-app-server-client.ts";

const ENV_FILE = "/Users/rajiv/Downloads/projects/heydonna-app/.env.local";
const QUEUE_FILE = "/tmp/cto-slack-queue.jsonl";
const EVENTS_FILE = "/tmp/cto-slack-events.json";
const ACK_FILE = "/tmp/cto-slack-ack.json";
const LOG_FILE = "/tmp/cto-bridge.log";
const DIRECT_DELIVERY_RECEIPTS_FILE = "/tmp/cto-ipc-delivery-receipts.jsonl";
const MONITOR_TRIGGER_RECEIPTS_FILE = "/tmp/cto-ipc-monitor-trigger-receipts.jsonl";
const APP_SERVER_DELIVERY_RECEIPTS_FILE = "/tmp/cto-app-server-delivery-receipts.jsonl";
const IPC_SENDER = "/Users/rajiv/.codex/skills/codex-ipc-send-message/scripts/send_message.py";
const RELAY_SNAPSHOT_HELPER = "/Users/rajiv/.claude/scripts/cto-relay-snapshot.py";
const RELAY_SNAPSHOT_FILE_PREFIX = "/tmp/cto-slack-relay-snapshot.bridge";
const RELAY_CLAIMS_FILE = "/tmp/cto-slack-relay-claims.json";
const RELAY_LOCK_FILE = "/tmp/cto-slack-relay.lock";
const RELAY_HEARTBEAT_FILE = "/tmp/cto-slack-relay-heartbeat";
const CLAIM_TTL_SECONDS = 120;
const CLAIM_RENEW_INTERVAL_MS = 30_000;
const SLACK_MONITOR_THREAD_ID = "019fd9df-23ad-7500-8b3e-53ce9341a140";
const SLACK_MONITOR_SOP = "/Users/rajiv/.codex/monitors/cto-slack-relay/WAKE_SOP.md";
const MAX_SEEN = 5_000;
const MAX_RECONNECT_DELAY_MS = 60_000;

type SlackEvent = {
  type?: string;
  channel?: string;
  channel_type?: string;
  ts?: string;
  thread_ts?: string;
  user?: string;
  text?: string;
  blocks?: unknown[];
  bot_id?: string;
  subtype?: string;
  files?: SlackFile[];
};

type SlackEventBody = {
  event_id?: string;
  event_time?: number;
  team_id?: string;
};

type EventTuple = {
  channel: string;
  ts: string;
  thread_ts: string | null;
  user: string | null;
  bot_id: string | null;
  subtype: string | null;
  text: string;
  blocks?: unknown[];
};

function readQueuedKeys(key: string): Set<string> {
  const keys = new Set<string>();
  for (const path of [QUEUE_FILE, EVENTS_FILE]) {
    if (hasDurableEnvelopeKey(path, key)) keys.add(key);
  }
  return keys;
}

function loadCtoEnvironment(): void {
  if (process.env.SLACK_CTO_BOT_TOKEN && process.env.SLACK_CTO_APP_TOKEN) return;
  if (!existsSync(ENV_FILE)) return;

  const contents = readFileSync(ENV_FILE, "utf8");
  for (const line of contents.split("\n")) {
    const match = line.match(/^\s*(SLACK_CTO_(?:BOT|APP)_TOKEN)\s*=\s*(.*?)\s*$/);
    if (!match || process.env[match[1]]) continue;
    let value = match[2];
    if (
      value.length >= 2 &&
      ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'")))
    ) {
      value = value.slice(1, -1);
    }
    process.env[match[1]] = value;
  }
}

function log(message: string): void {
  const line = `[${new Date().toISOString()}] ${message}`;
  try {
    appendFileSync(LOG_FILE, `${line}\n`, { encoding: "utf8", mode: 0o600 });
  } catch (error) {
    process.stderr.write(
      `[${new Date().toISOString()}] operational-log-write-failure ${formatError(error)}\n`,
    );
  }
}

function formatError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

loadCtoEnvironment();

const botToken = process.env.SLACK_CTO_BOT_TOKEN;
const appToken = process.env.SLACK_CTO_APP_TOKEN;
if (!botToken || !appToken) {
  log(`startup-failure missing SLACK_CTO_BOT_TOKEN or SLACK_CTO_APP_TOKEN in ${ENV_FILE}`);
  process.exit(1);
}

const app = new App({
  token: botToken,
  appToken,
  socketMode: true,
  ignoreSelf: false,
});

const receiver = (app as unknown as { receiver: SocketModeReceiver }).receiver;
const socketClient = receiver.client as unknown as {
  autoReconnectEnabled: boolean;
  on(event: string, listener: (...args: unknown[]) => void): void;
};
// Bolt reconnects automatically, but its built-in delay is linear. This task
// owns reconnect scheduling so the required exponential policy is explicit.
socketClient.autoReconnectEnabled = false;

const seen = new Set<string>();
let appUserId: string | null = null;
let connected = false;
let shuttingDown = false;
let reconnectAttempt = 0;
let reconnectTimer: NodeJS.Timeout | null = null;
let appServerDrain: Promise<void> | null = null;
let appServerDrainRequested = false;
const appServerClient = new CodexAppServerClient();

function remember(key: string): void {
  seen.add(key);
  if (seen.size <= MAX_SEEN) return;
  const oldest = seen.values().next().value as string | undefined;
  if (oldest) seen.delete(oldest);
}

function appendDurableEnvelope(envelope: Record<string, unknown>): boolean {
  try {
    appendFileSync(QUEUE_FILE, `${JSON.stringify(envelope)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    return true;
  } catch (error) {
    log(`queue-write-failure key=${String(envelope.dedup_key)} error=${formatError(error)}`);
    return false;
  }
}

function readJsonFile(path: string, fallback: unknown): unknown {
  try {
    if (!existsSync(path)) return fallback;
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    log(`json-read-failure path=${path} error=${formatError(error)}`);
    return fallback;
  }
}

function writeJsonFile(path: string, value: unknown): void {
  const temporary = `${path}.tmp.${process.pid}`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  renameSync(temporary, path);
}

function readHandledKeys(): Set<string> {
  const ack = readJsonFile(ACK_FILE, { handled: [] }) as { handled?: string[] };
  const handled = new Set(Array.isArray(ack.handled) ? ack.handled : []);
  for (const [path, uncertainIsHandled] of [
    [DIRECT_DELIVERY_RECEIPTS_FILE, true],
    [APP_SERVER_DELIVERY_RECEIPTS_FILE, false],
  ] as const) {
    if (!existsSync(path)) continue;
    try {
      const lines = readFileSync(path, "utf8").split("\n").filter(Boolean);
      for (const line of lines) {
        try {
          const receipt = JSON.parse(line) as { receipt_key?: unknown; status?: unknown };
          if (
            typeof receipt.receipt_key === "string" &&
            (receipt.status === "delivered" ||
              receipt.status === "queued" ||
              (uncertainIsHandled && receipt.status === "uncertain"))
          ) {
            handled.add(receipt.receipt_key);
          }
        } catch {
          // One malformed receipt must not hide later valid delivery receipts.
        }
      }
    } catch (error) {
      log(`delivery-receipt-read-failure path=${path} error=${formatError(error)}`);
    }
  }
  return handled;
}

type DirectDeliveryResult =
  | { status: "delivered"; receipt: Record<string, unknown> }
  | { status: "not_sent"; detail: string }
  | { status: "uncertain"; detail: string };

function runPython(
  args: string[],
  stdinText = "",
): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const child = spawn("python3", args, {
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout = `${stdout}${chunk}`.slice(-1_000_000);
    });
    child.stderr.on("data", (chunk: string) => {
      stderr = `${stderr}${chunk}`.slice(-1_000_000);
    });
    child.on("error", (error) => {
      resolve({ code: 2, stdout: "", stderr: formatError(error) });
    });
    child.on("close", (code) => {
      resolve({ code: code ?? 2, stdout, stderr });
    });
    child.stdin.end(stdinText);
  });
}

type RelayRoute = {
  project: "heydonna" | "superproofer";
  destination_thread_id: string;
  consumer_sop_path: string;
};

type RoutedEnvelope = Record<string, unknown> & {
  dedup_key: string;
  fingerprint: string;
  relay_route: RelayRoute;
  routed_wake_text: string;
  claim_owner: string;
};

function asRoutedEnvelope(value: unknown, claimOwner: string): RoutedEnvelope | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const envelope = value as Record<string, unknown>;
  const route = envelope.relay_route;
  if (!route || typeof route !== "object" || Array.isArray(route)) return null;
  const routeRecord = route as Record<string, unknown>;
  const project = routeRecord.project;
  const destination = routeRecord.destination_thread_id;
  const sop = routeRecord.consumer_sop_path;
  const key = envelope.dedup_key;
  const fingerprint = envelope.fingerprint;
  const wake = envelope.routed_wake_text;
  if (
    (project !== "heydonna" && project !== "superproofer") ||
    typeof destination !== "string" ||
    typeof sop !== "string" ||
    typeof key !== "string" ||
    typeof fingerprint !== "string" ||
    typeof wake !== "string"
  ) {
    return null;
  }
  return {
    ...envelope,
    dedup_key: key,
    fingerprint,
    relay_route: {
      project,
      destination_thread_id: destination,
      consumer_sop_path: sop,
    },
    routed_wake_text: wake,
    claim_owner: claimOwner,
  } as RoutedEnvelope;
}

async function runRelaySnapshot(): Promise<RoutedEnvelope | null> {
  const claimOwner = `bridge-${process.pid}-${randomUUID()}`;
  const snapshotFile = `${RELAY_SNAPSHOT_FILE_PREFIX}-${process.pid}-${randomUUID()}.json`;
  try {
    const result = await runPython([
      RELAY_SNAPSHOT_HELPER,
      "--limit",
      "1",
      "--route-sop",
      SLACK_MONITOR_SOP,
      "--output",
      snapshotFile,
      "--claim-ttl-seconds",
      String(CLAIM_TTL_SECONDS),
      "--claim-owner",
      claimOwner,
    ]);
    if (result.code !== 0) {
      log(`relay-snapshot-failure error=${result.stderr.trim() || `exit-${result.code}`}`);
      return null;
    }
    const snapshot = readJsonFile(snapshotFile, []) as unknown;
    if (!Array.isArray(snapshot) || snapshot.length === 0) return null;
    const envelope = asRoutedEnvelope(snapshot[0], claimOwner);
    if (!envelope) {
      log("relay-snapshot-invalid missing-exact-routed-fields");
      return null;
    }
    return envelope;
  } finally {
    try {
      unlinkSync(snapshotFile);
    } catch {
      // The helper may fail before creating its invocation-private output.
    }
  }
}

async function updateClaim(
  action: "renew" | "release",
  key: string,
  claimOwner: string,
): Promise<boolean> {
  const flag = action === "renew" ? "--renew-key" : "--release-key";
  const result = await runPython([
    RELAY_SNAPSHOT_HELPER,
    "--route-sop",
    SLACK_MONITOR_SOP,
    "--claims-file",
    RELAY_CLAIMS_FILE,
    "--lock-file",
    RELAY_LOCK_FILE,
    "--heartbeat-file",
    RELAY_HEARTBEAT_FILE,
    "--claim-owner",
    claimOwner,
    flag,
    key,
    ...(action === "renew" ? ["--claim-ttl-seconds", String(CLAIM_TTL_SECONDS)] : []),
  ]);
  if (result.code !== 0) {
    log(`relay-claim-${action}-failure key=${key} error=${result.stderr.trim() || `exit-${result.code}`}`);
    return false;
  }
  return true;
}

function appendReceipt(path: string, receipt: Record<string, unknown>): void {
  appendFileSync(path, `${JSON.stringify(receipt)}\n`, { encoding: "utf8", mode: 0o600 });
}

function acknowledgeEnvelope(envelope: RoutedEnvelope): void {
  const ack = readJsonFile(ACK_FILE, { handled: [] }) as { handled?: unknown };
  const handled = new Set(
    Array.isArray(ack.handled) ? ack.handled.filter((value): value is string => typeof value === "string") : [],
  );
  handled.add(envelope.dedup_key);
  handled.add(envelope.fingerprint);
  writeJsonFile(ACK_FILE, { handled: [...handled] });
}

function appendAppServerReceipt(
  envelope: RoutedEnvelope,
  result: AppServerDeliveryResult,
): void {
  appendReceipt(APP_SERVER_DELIVERY_RECEIPTS_FILE, {
    receipt_key: envelope.dedup_key,
    fingerprint: envelope.fingerprint,
    transport: "codex_thread_queue",
    project: envelope.relay_route.project,
    destination_thread_id: envelope.relay_route.destination_thread_id,
    ...result,
    status: result.status,
    recorded_at: new Date().toISOString(),
  });
}

function appendUncertainReceipt(
  key: string,
  detail: string,
): void {
  appendFileSync(
    MONITOR_TRIGGER_RECEIPTS_FILE,
    `${JSON.stringify({
      status: "uncertain",
      receipt_key: key,
      relay_thread_id: SLACK_MONITOR_THREAD_ID,
      detail,
      recorded_at: new Date().toISOString(),
    })}\n`,
    { encoding: "utf8", mode: 0o600 },
  );
}

async function triggerSlackMonitor(envelope: Record<string, unknown>): Promise<DirectDeliveryResult> {
  const key = String(envelope.dedup_key || "");
  if (!key) return { status: "not_sent", detail: "missing-dedup-key" };
  const wakeMessage = [
    `SOP path: ${SLACK_MONITOR_SOP}`,
    "",
    "CTO Slack relay trigger",
    `Dedup key: ${key}`,
    "Run one bounded relay tick for this exact queued key now:",
    `python3 /Users/rajiv/.claude/scripts/cto-relay-snapshot.py --key ${key} --limit 1 --route-sop ${SLACK_MONITOR_SOP}`,
    "Wait for that command to finish. If it exits nonzero, stop: do not read, deliver, or acknowledge any snapshot.",
    "Use only event.relay_route.destination_thread_id and event.routed_wake_text from the frozen snapshot.",
    "Claim and read the durable queue; do not route from this trigger text or Slack message text.",
  ].join("\n");
  const senderArgs = [
    IPC_SENDER,
    "--thread-id",
    SLACK_MONITOR_THREAD_ID,
    "--message-file",
    "-",
    "--receipt-key",
    key,
    "--success-receipt-file",
    MONITOR_TRIGGER_RECEIPTS_FILE,
    ...ipcOwnerDiscoveryArgs(),
  ];
  const result = await runPython(senderArgs, wakeMessage);
  let receipt: Record<string, unknown> = {};
  try {
    const parsed = JSON.parse(result.stdout.trim()) as unknown;
    if (parsed && typeof parsed === "object") receipt = parsed as Record<string, unknown>;
  } catch {
    // The process status below remains authoritative when stdout is malformed.
  }
  if (result.code === 0 && receipt.ok === true) {
    return { status: "delivered", receipt };
  }
  const detail = String(receipt.error || result.stderr.trim() || `sender-exit-${result.code}`);
  if (result.code === 3 || receipt.deliveryState === "uncertain") {
    try {
      appendUncertainReceipt(key, detail);
    } catch (error) {
      log(`uncertain-receipt-write-failure key=${key} error=${formatError(error)}`);
    }
    return { status: "uncertain", detail };
  }
  return { status: "not_sent", detail };
}

async function deliverClaimedEnvelope(envelope: RoutedEnvelope): Promise<"delivered" | "queued" | "pending" | "uncertain"> {
  const renewTimer = setInterval(() => {
    void updateClaim("renew", envelope.dedup_key, envelope.claim_owner);
  }, CLAIM_RENEW_INTERVAL_MS);
  renewTimer.unref();
  try {
    const appServerResult = await appServerClient.deliver({
      destinationThreadId: envelope.relay_route.destination_thread_id,
      routedWakeText: envelope.routed_wake_text,
      dedupKey: envelope.dedup_key,
    });
    if (appServerResult.status === "delivered") {
      appendAppServerReceipt(envelope, appServerResult);
      acknowledgeEnvelope(envelope);
      await updateClaim("release", envelope.dedup_key, envelope.claim_owner);
      log(
        `relay-delivered key=${envelope.dedup_key} transport=codex_thread_queue project=${envelope.relay_route.project} thread=${appServerResult.threadId} submission=${appServerResult.queuedSubmissionId} start=accepted`,
      );
      process.stdout.write(
        `RELAY_DELIVERED ${JSON.stringify({
          key: envelope.dedup_key,
          transport: "codex_thread_queue",
          thread_id: appServerResult.threadId,
          queued_submission_id: appServerResult.queuedSubmissionId,
          start_accepted: true,
        })}\n`,
      );
      return "delivered";
    }
    if (appServerResult.status === "queued") {
      appendAppServerReceipt(envelope, appServerResult);
      acknowledgeEnvelope(envelope);
      await updateClaim("release", envelope.dedup_key, envelope.claim_owner);
      log(
        `relay-queued-not-started key=${envelope.dedup_key} thread=${appServerResult.threadId} submission=${appServerResult.queuedSubmissionId} detail=${appServerResult.detail}`,
      );
      process.stdout.write(
        `RELAY_QUEUED ${JSON.stringify({
          key: envelope.dedup_key,
          thread_id: appServerResult.threadId,
          queued_submission_id: appServerResult.queuedSubmissionId,
          detail: appServerResult.detail,
        })}\n`,
      );
      return "queued";
    }
    if (appServerResult.status === "uncertain") {
      appendAppServerReceipt(envelope, appServerResult);
      log(`relay-app-server-uncertain key=${envelope.dedup_key} detail=${appServerResult.detail}`);
      process.stdout.write(
        `RELAY_UNCERTAIN ${JSON.stringify({ key: envelope.dedup_key, detail: appServerResult.detail })}\n`,
      );
      return "uncertain";
    }

    // Before a queue submission is accepted, return the lease and use the
    // existing Desktop trigger. Once queue acceptance is uncertain, keep the
    // lease to prevent a second relay from duplicating a possibly-running task.
    await updateClaim("release", envelope.dedup_key, envelope.claim_owner);
    const fallback = await triggerSlackMonitor(envelope);
    if (fallback.status === "delivered") {
      log(`relay-trigger-delivered key=${envelope.dedup_key} transport=codex_desktop_ipc_fallback`);
      process.stdout.write(
        `RELAY_TRIGGERED ${JSON.stringify({ key: envelope.dedup_key, transport: "codex_desktop_ipc_fallback", receipt: fallback.receipt })}\n`,
      );
    } else if (fallback.status === "uncertain") {
      log(`relay-trigger-uncertain key=${envelope.dedup_key} detail=${fallback.detail}`);
      process.stdout.write(
        `RELAY_TRIGGER_UNCERTAIN ${JSON.stringify({ key: envelope.dedup_key, detail: fallback.detail })}\n`,
      );
    } else {
      log(`relay-trigger-pending key=${envelope.dedup_key} detail=${fallback.detail}`);
      process.stdout.write(
        `WAKE_PENDING ${JSON.stringify({ key: envelope.dedup_key, transport: "relay_automation_fallback", detail: fallback.detail })}\n`,
      );
    }
    return "pending";
  } finally {
    clearInterval(renewTimer);
  }
}

async function drainAppServer(): Promise<void> {
  while (!shuttingDown) {
    const envelope = await runRelaySnapshot();
    if (!envelope) return;
    const status = await deliverClaimedEnvelope(envelope);
    // Queue acceptance is durable delivery even if this task is currently
    // busy. Do not strand later envelopes waiting for another Slack event.
    if (status !== "delivered" && status !== "queued") return;
  }
}

function requestAppServerDrain(): void {
  appServerDrainRequested = true;
  if (appServerDrain) return;
  appServerDrain = (async () => {
    while (appServerDrainRequested && !shuttingDown) {
      appServerDrainRequested = false;
      await drainAppServer();
    }
  })().catch((error) => {
    log(`relay-drain-failure error=${formatError(error)}`);
  }).finally(() => {
    appServerDrain = null;
    if (appServerDrainRequested && !shuttingDown) requestAppServerDrain();
  });
}

function envelopeIsHandled(
  envelope: Record<string, unknown>,
  handled: Set<string>,
): boolean {
  const dedupKey = String(envelope.dedup_key || "");
  const fingerprint = String(envelope.fingerprint || "");
  return (
    (dedupKey !== "" && handled.has(dedupKey)) ||
    (fingerprint !== "" && handled.has(fingerprint))
  );
}

function readPendingEnvelopes(): Array<Record<string, unknown>> {
  const events = readJsonFile(EVENTS_FILE, { events: [] }) as {
    events?: Array<Record<string, unknown>>;
  };
  const handled = readHandledKeys();
  return Array.isArray(events.events)
    ? events.events.filter((item) => !envelopeIsHandled(item, handled))
    : [];
}

function publishPendingEnvelope(envelope: Record<string, unknown>): void {
  const handled = readHandledKeys();
  const pending = readPendingEnvelopes().filter(
    (item) => !envelopeIsHandled(item, handled),
  );
  if (envelopeIsHandled(envelope, handled)) return;
  pending.push(envelope);
  pending.sort(compareSourceOrder);
  try {
    writeJsonFile(EVENTS_FILE, {
      version: 1,
      updated_at: new Date().toISOString(),
      events: pending,
    });
  } catch (error) {
    log(`events-write-failure error=${formatError(error)}`);
  }
}

function compareSourceOrder(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): number {
  const leftTuple = left.exact_tuple;
  const rightTuple = right.exact_tuple;
  const leftTs = leftTuple && typeof leftTuple === "object" && !Array.isArray(leftTuple)
    ? Number((leftTuple as Record<string, unknown>).ts)
    : Number.NaN;
  const rightTs = rightTuple && typeof rightTuple === "object" && !Array.isArray(rightTuple)
    ? Number((rightTuple as Record<string, unknown>).ts)
    : Number.NaN;
  if (Number.isFinite(leftTs) && Number.isFinite(rightTs) && leftTs !== rightTs) {
    return leftTs - rightTs;
  }
  return String(left.queued_at ?? "").localeCompare(String(right.queued_at ?? ""));
}

function rehydratePendingEvents(): void {
  const handled = readHandledKeys();
  const pending = readPendingEnvelopes();
  let changed = false;
  if (!existsSync(QUEUE_FILE)) return;
  try {
    const lines = readFileSync(QUEUE_FILE, "utf8").split("\n").filter(Boolean);
    for (const line of lines) {
      let envelope: Record<string, unknown>;
      try {
        envelope = JSON.parse(line) as Record<string, unknown>;
      } catch {
        continue;
      }
      if (envelopeIsHandled(envelope, handled)) continue;
      const key = String(envelope.dedup_key || envelope.fingerprint || "");
      if (pending.some((item) => String(item.dedup_key || item.fingerprint || "") === key)) continue;
      pending.push(envelope);
      changed = true;
    }
  } catch (error) {
    log(`queue-rehydrate-failure error=${formatError(error)}`);
    return;
  }
  if (!changed && pending.length === 0) return;
  pending.sort(compareSourceOrder);
  try {
    writeJsonFile(EVENTS_FILE, {
      version: 1,
      updated_at: new Date().toISOString(),
      events: pending,
    });
  } catch (error) {
    log(`events-rehydrate-write-failure error=${formatError(error)}`);
  }
}

type ThreadSnapshot = {
  verification: Record<string, unknown>;
  previousThreadMessage: PreviousThreadMessage | null;
};

async function fetchThreadSnapshot(tuple: EventTuple): Promise<ThreadSnapshot> {
  try {
    if (!tuple.thread_ts) {
      const result = await app.client.conversations.history({
        channel: tuple.channel,
        latest: tuple.ts,
        limit: 1,
        inclusive: true,
      });
      const messages = (result.messages ?? []) as SlackThreadMessage[];
      return {
        verification: {
          attempted: true,
          ok: result.ok === true,
          message_count: messages.length,
          parent_found: messages.some((message) => message.ts === tuple.ts),
          event_found: messages.some((message) => message.ts === tuple.ts),
          response_metadata_present: Boolean(result.response_metadata),
        },
        previousThreadMessage: null,
      };
    }

    const messages: SlackThreadMessage[] = [];
    const seenCursors = new Set<string>();
    let cursor: string | undefined;
    let responseMetadataPresent = false;
    let allPagesOk = true;
    do {
      const result = await app.client.conversations.replies({
        channel: tuple.channel,
        ts: tuple.thread_ts,
        latest: tuple.ts,
        inclusive: true,
        limit: 100,
        ...(cursor ? { cursor } : {}),
      });
      allPagesOk = allPagesOk && result.ok === true;
      responseMetadataPresent = responseMetadataPresent || Boolean(result.response_metadata);
      messages.push(...((result.messages ?? []) as SlackThreadMessage[]));

      const nextCursor = String(result.response_metadata?.next_cursor ?? "").trim();
      if (!nextCursor || seenCursors.has(nextCursor)) break;
      seenCursors.add(nextCursor);
      cursor = nextCursor;
    } while (true);

    return {
      verification: {
        attempted: true,
        ok: allPagesOk,
        message_count: messages.length,
        parent_found: messages.some((message) => message.ts === tuple.thread_ts),
        event_found: messages.some((message) => message.ts === tuple.ts),
        response_metadata_present: responseMetadataPresent,
      },
      previousThreadMessage: findPreviousThreadMessage(messages, tuple.ts),
    };
  } catch (error) {
    return {
      verification: {
        attempted: true,
        ok: false,
        error: formatError(error),
      },
      previousThreadMessage: null,
    };
  }
}

async function receiveEvent(
  event: SlackEvent,
  body: SlackEventBody,
  eventType: SlackIngressEventType,
  contextBotUserId?: string,
): Promise<void> {
  const channel = event.channel ?? "";
  const ts = event.ts ?? "";
  const user = event.user ?? "";
  const botId = event.bot_id ?? "";
  const text = event.text ?? "";
  const key = `${channel}:${ts}`;

  log(`event-receipt type=${eventType} subtype=${event.subtype || "none"} key=${key || "invalid"} user=${user || "missing"}`);

  if (!channel || !ts || (!user && !botId)) {
    log(`event-ignored reason=missing-exact-tuple-field key=${key || "invalid"}`);
    return;
  }
  if (seen.has(key)) {
    log(`deduplication key=${key}`);
    return;
  }
  if (readHandledKeys().has(key)) {
    remember(key);
    log(`event-ignored reason=durably-handled key=${key}`);
    return;
  }
  if (readQueuedKeys(key).has(key)) {
    remember(key);
    log(`event-ignored reason=durably-queued key=${key}`);
    requestAppServerDrain();
    return;
  }
  // Mark BEFORE any await: message.group + app_mention for the same message
  // arrive ~300ms apart and both pass the check while the first is still in
  // flight (verification + delivery take ~1s) — a check-then-await race that
  // double-delivers. Delivery failures still reach the durable queue, so an
  // early mark never loses an event.
  remember(key);
  const originIgnoreReason = eventOriginIgnoreReason(event, appUserId, contextBotUserId);
  if (originIgnoreReason) {
    log(`event-ignored reason=${originIgnoreReason} key=${key}`);
    return;
  }

  const tuple: EventTuple = {
    channel,
    ts,
    thread_ts: event.thread_ts ?? null,
    user: user || null,
    bot_id: botId || null,
    subtype: event.subtype ?? null,
    text,
    ...(channel === "C0C7U35LBKR" && Array.isArray(event.blocks) ? { blocks: event.blocks } : {}),
  };
  const attachments: MaterializedAttachment[] = event.files?.length
    ? await materializeSlackImages(event.files, key, {
        client: app.client,
        token: botToken,
      })
    : [];
  const threadSnapshot = await fetchThreadSnapshot(tuple);
  const sourceEvidence = {
    transport: "slack_bolt_socket_mode",
    slack_event_type: eventType,
    slack_event_id: body.event_id ?? null,
    slack_event_time: body.event_time ?? null,
    team_id: body.team_id ?? null,
    channel_type: event.channel_type ?? null,
    subtype: event.subtype ?? null,
    explicit_cto_mention:
      event.channel === HEYDONNA_DEV_CHANNEL_ID || event.channel === SUPERPROOFER_CHANNEL_ID
        ? hasExplicitCtoMention(event)
        : null,
  };
  let durableEnvelope: Record<string, unknown>;
  try {
    durableEnvelope = buildCtoDurableEnvelope({
      key,
      tuple,
      sourceEvidence,
      verification: threadSnapshot.verification,
      previousThreadMessage: threadSnapshot.previousThreadMessage,
      attachments,
      sopPath: SLACK_MONITOR_SOP,
    });
  } catch (error) {
    log(`wake-format-failure key=${key} error=${formatError(error)}`);
    return;
  }

  if (!appendDurableEnvelope(durableEnvelope)) return;
  // Publish before the bridge-owned app-server drain. The snapshot helper is
  // the sole route/claim authority for both this path and scheduled fallback.
  publishPendingEnvelope(durableEnvelope);
  requestAppServerDrain();
}

app.message(async ({ message, body, context }) => {
  const event = message as SlackEvent;
  const eventType = slackIngressEventType(event, "message");
  if (!eventType) return;
  await receiveEvent(
    event,
    body as SlackEventBody,
    eventType,
    context.botUserId,
  );
});

app.event("app_mention", async ({ event, body, context }) => {
  const eventType = slackIngressEventType(event as SlackEvent, "app_mention");
  if (!eventType) return;
  await receiveEvent(
    event as SlackEvent,
    body as SlackEventBody,
    eventType,
    context.botUserId,
  );
});

app.error(async (error) => {
  log(`bolt-error error=${formatError(error)}`);
});

function scheduleReconnect(reason: string): void {
  if (shuttingDown || reconnectTimer) return;
  connected = false;
  const delay = Math.min(1_000 * 2 ** reconnectAttempt, MAX_RECONNECT_DELAY_MS);
  reconnectAttempt += 1;
  log(`reconnect-scheduled attempt=${reconnectAttempt} delay_ms=${delay} reason=${reason}`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void startSocketMode("reconnect");
  }, delay);
}

async function startSocketMode(reason: "startup" | "reconnect"): Promise<void> {
  if (shuttingDown) return;
  try {
    if (reason === "reconnect") {
      log(`reconnect-attempt attempt=${reconnectAttempt}`);
    }
    await app.start();
  } catch (error) {
    log(`${reason}-failure error=${formatError(error)}`);
    scheduleReconnect(`${reason}-failure`);
  }
}

socketClient.on("connected", () => {
  const wasReconnect = reconnectAttempt > 0;
  connected = true;
  reconnectAttempt = 0;
  log(wasReconnect ? "Socket Mode reconnected" : "Socket Mode connected");
});

socketClient.on("disconnected", (error?: unknown) => {
  connected = false;
  log(`Socket Mode disconnected${error ? ` error=${formatError(error)}` : ""}`);
  scheduleReconnect("websocket-disconnected");
});

socketClient.on("error", (error: unknown) => {
  log(`websocket-error error=${formatError(error)}`);
});

async function explicitShutdown(source: string): Promise<void> {
  if (shuttingDown) return;
  shuttingDown = true;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  log(`explicit-shutdown source=${source}`);
  try {
    await app.stop();
  } catch (error) {
    log(`shutdown-error error=${formatError(error)}`);
  }
  appServerClient.stop();
  process.exit(0);
}

const stdin = createInterface({ input: process.stdin });
stdin.on("line", (line) => {
  if (line.trim() === "SHUTDOWN") {
    void explicitShutdown("stdin");
    return;
  }
  const match = line.match(/^(WAKE_DELIVERED|WAKE_DELIVERY_FAILED)\s+(.+)$/);
  if (!match) return;
  try {
    const receipt = JSON.parse(match[2]) as { key?: string; detail?: unknown };
    if (match[1] === "WAKE_DELIVERED") {
      log(`wake-delivered key=${receipt.key ?? "missing"}`);
    } else {
      log(
        `wake-delivery-failure key=${receipt.key ?? "missing"} detail=${JSON.stringify(receipt.detail ?? null)}`,
      );
    }
  } catch (error) {
    log(`wake-receipt-parse-failure error=${formatError(error)}`);
  }
});

process.on("SIGINT", () => void explicitShutdown("SIGINT"));
process.on("SIGTERM", () => void explicitShutdown("SIGTERM"));
process.on("uncaughtException", (error) => {
  log(`uncaught-exception error=${formatError(error)}`);
});
process.on("unhandledRejection", (error) => {
  log(`unhandled-rejection error=${formatError(error)}`);
});

setInterval(() => {
  log(`heartbeat connected=${connected} seen=${seen.size}`);
}, 60_000).unref();

log(
  `startup fallback_relay_thread=${SLACK_MONITOR_THREAD_ID} queue=${QUEUE_FILE} delivery=codex_thread_queue_then_codex_desktop_ipc_fallback`,
);
rehydratePendingEvents();
try {
  const auth = await app.client.auth.test();
  appUserId = auth.user_id ?? null;
  log(`app-identity-verified user_id=${appUserId ?? "unknown"}`);
} catch (error) {
  log(`app-identity-verification-failure error=${formatError(error)}`);
}
await startSocketMode("startup");
requestAppServerDrain();
