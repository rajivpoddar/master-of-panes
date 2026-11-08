import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  YSK_NOTICE_PROCESS_SUFFIX,
  submitWithComposerCheck,
  yskNoticeAppendException,
} from "../src/composer.js";
import {
  driveYskNotice,
  normalizeYskNote,
  parseYskCapture,
  yskNoteHash,
  type YskDriveDeps,
  type YskDriveResult,
} from "../src/yskNotice.js";

/** Live PM capture, Rajiv DM D0AMF0XE6TS thread 1794143890.463219 (image ts 1794144956). */
const NOTE = "Lockout fixes are still set up to merge without waiting for CI, and they no longer get a full test run beforehand either.";
const CHOICES = "1: Learn more   2: Knew this already   0: Dismiss";
const SUBMENU = "1: Understood   2: Chat in main session   0: Dismiss";
const RULE = "─".repeat(40);

function pane(above: string[], composer: string): string {
  return [...above, RULE, `❯ ${composer}`, RULE, "   Dhruva PM  Opus 5.5 · heydonna-app · main", ""].join("\n");
}

const top = (heads = `Heads up · ${NOTE}`) => pane([heads, CHOICES], "");
const submenu = () => pane([
  `Heads up · ${NOTE}`,
  "CI is not required before these merges.",
  SUBMENU,
], "");

function harness(capture: () => Promise<string | null>, ready: YskDriveDeps["ready"] = async () => ({ ok: true })) {
  const keys: string[] = [];
  let enters = 0;
  let stored: string[] | null = [];
  const saves: string[][] = [];
  const logs: YskDriveResult[] = [];
  const deps: YskDriveDeps = {
    capture,
    sendLiteral: async (text) => {
      keys.push(text);
    },
    pressEnter: async () => {
      enters += 1;
    },
    sleep: async () => {},
    loadSeen: () => (stored ? [...stored] : null),
    saveSeen: (hashes) => {
      stored = [...hashes];
      saves.push([...hashes]);
    },
    ready,
    log: (result) => logs.push(result),
    submenuTimeoutMs: 0,
    quoteTimeoutMs: 0,
    pollMs: 0,
    submitTiming: { dwellMs: 0, payloadStableMs: 0, payloadGraceMs: 0, clearGraceMs: 0, pollMs: 0 },
  };
  return {
    deps,
    keys,
    saves,
    logs,
    enters: () => enters,
    stored: () => stored,
  };
}

test("notice detected from the live Heads-up fixture, with or without the sparkle", () => {
  const plain = parseYskCapture(top());
  const sparkle = parseYskCapture(top(`✦ Heads up · ${NOTE}`));
  assert.equal(plain.kind, "top");
  assert.equal(sparkle.kind, "top");
  if (plain.kind !== "top" || sparkle.kind !== "top") return;
  assert.equal(plain.note, NOTE);
  assert.equal(sparkle.note, NOTE);
  assert.equal(yskNoteHash(plain.note), yskNoteHash(sparkle.note));
});

test("a wrapped Heads-up line is the same note", () => {
  const wrapped = pane([
    "Heads up · Lockout fixes are still set up to merge",
    "without waiting for CI, and they no longer get a full test run beforehand either.",
    CHOICES,
  ], "");
  const parsed = parseYskCapture(wrapped);
  assert.equal(parsed.kind, "top");
  if (parsed.kind !== "top") return;
  assert.equal(parsed.note, NOTE);
  assert.equal(normalizeYskNote(parsed.note), NOTE);
});

test("draft present is a no-op and is not remembered", async () => {
  const box = harness(async () => pane([`Heads up · ${NOTE}`, CHOICES], "keep this draft"));
  const result = await driveYskNotice(box.deps);
  assert.equal(result.outcome, "noop");
  assert.equal(result.reason, "draft_present");
  assert.deepEqual(box.keys, []);
  assert.equal(box.enters(), 0);
  assert.deepEqual(box.saves, []);
});

test("no notice is a no-op", async () => {
  const box = harness(async () => pane(["Waiting for 1 background agent to finish"], ""));
  const result = await driveYskNotice(box.deps);
  assert.equal(result.reason, "no_notice");
  assert.deepEqual(box.keys, []);
  assert.deepEqual(box.saves, []);
});

test("submenu missing after 1 aborts and remembers the note", async () => {
  const box = harness(async () => top());
  const result = await driveYskNotice(box.deps);
  assert.equal(result.outcome, "abort");
  assert.equal(result.reason, "submenu_missing");
  assert.deepEqual(box.keys, ["1"]);
  assert.equal(box.enters(), 0);
  assert.equal(box.stored()?.length, 1);
});

test("quote missing after 2 aborts without the suffix", async () => {
  let step = 0;
  const box = harness(async () => {
    step += 1;
    if (step === 1) return top();
    return submenu();
  });
  const result = await driveYskNotice(box.deps);
  assert.equal(result.reason, "quote_missing");
  assert.deepEqual(box.keys, ["1", "2"]);
  assert.equal(box.enters(), 0);
  assert.equal(box.stored()?.length, 1);
});

test("learn more then chat quotes the note, then the exact suffix is submitted once", async () => {
  let phase: "top" | "sub" | "quote" | "suffixed" | "done" = "top";
  const box = harness(async () => {
    if (phase === "top") return top();
    if (phase === "sub") return submenu();
    if (phase === "quote") return pane(["quoted into the prompt"], NOTE);
    if (phase === "suffixed") return pane(["quoted into the prompt"], NOTE + YSK_NOTICE_PROCESS_SUFFIX);
    return pane(["sent"], "");
  });
  box.deps.sendLiteral = async (text) => {
    box.keys.push(text);
    if (text === "1") phase = "sub";
    else if (text === "2") phase = "quote";
    else if (text === YSK_NOTICE_PROCESS_SUFFIX) phase = "suffixed";
  };
  box.deps.pressEnter = async () => {
    phase = "done";
  };
  const result = await driveYskNotice(box.deps);
  assert.equal(result.outcome, "submitted");
  assert.deepEqual(box.keys, ["1", "2", YSK_NOTICE_PROCESS_SUFFIX]);
  assert.equal(result.enterPresses, 1);
  const again = await driveYskNotice(box.deps);
  assert.equal(again.reason, "deduped");
  assert.deepEqual(box.keys, ["1", "2", YSK_NOTICE_PROCESS_SUFFIX]);
});

test("an unreadable composer or a busy pane sends nothing", async () => {
  const unreadable = harness(async () => "plain shell $ ");
  assert.equal((await driveYskNotice(unreadable.deps)).reason, "composer_unreadable");
  assert.deepEqual(unreadable.keys, []);
  const busy = harness(async () => top(), async () => ({ ok: false, reason: "pm_busy" }));
  assert.equal((await driveYskNotice(busy.deps)).reason, "pm_busy");
  assert.deepEqual(busy.keys, []);
  assert.deepEqual(busy.saves, []);
});

test("the non-empty composer guard allows only the verified quote plus the exact suffix", async () => {
  const quote = NOTE;
  assert.equal(yskNoticeAppendException(quote + YSK_NOTICE_PROCESS_SUFFIX, quote, { verifiedQuote: quote }), true);
  assert.equal(yskNoticeAppendException(quote + " process something else", quote, { verifiedQuote: quote }), false);
  assert.equal(yskNoticeAppendException(quote + YSK_NOTICE_PROCESS_SUFFIX, quote + " extra", { verifiedQuote: quote }), false);
  assert.equal(yskNoticeAppendException(quote + YSK_NOTICE_PROCESS_SUFFIX, quote, undefined), false);
  assert.equal(yskNoticeAppendException(YSK_NOTICE_PROCESS_SUFFIX, "", { verifiedQuote: "" }), false);

  const blocked = await submitWithComposerCheck(quote, {
    capture: async () => pane([], quote),
    pressSubmit: async () => {},
    sleep: async () => {},
    prePasteComposer: quote,
    dwellMs: 0,
    payloadGraceMs: 0,
    payloadStableMs: 0,
    pollMs: 0,
  });
  assert.equal(blocked.enterPresses, 0);

  let enters = 0;
  const allowed = await submitWithComposerCheck(quote + YSK_NOTICE_PROCESS_SUFFIX, {
    capture: async () => pane([], quote + YSK_NOTICE_PROCESS_SUFFIX),
    pressSubmit: async () => {
      enters += 1;
    },
    sleep: async () => {},
    prePasteComposer: quote,
    noticeAppend: { verifiedQuote: quote },
    dwellMs: 0,
    payloadGraceMs: 0,
    payloadStableMs: 0,
    clearGraceMs: 0,
    pollMs: 0,
    maxEnterPresses: 1,
  });
  assert.equal(allowed.enterPresses, 1);
  assert.equal(enters, 1);

  const extra = await submitWithComposerCheck(quote + YSK_NOTICE_PROCESS_SUFFIX, {
    capture: async () => pane([], `${quote}${YSK_NOTICE_PROCESS_SUFFIX} and more`),
    pressSubmit: async () => {},
    sleep: async () => {},
    prePasteComposer: quote,
    noticeAppend: { verifiedQuote: quote },
    dwellMs: 0,
    payloadGraceMs: 0,
    payloadStableMs: 0,
    pollMs: 0,
  });
  assert.equal(extra.enterPresses, 0);
});

test("the paste path does not grow its own noticeAppend hole", () => {
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const notice = readFileSync(new URL("../src/yskNotice.ts", import.meta.url), "utf8");
  assert.match(server, /driveYskNotice/);
  assert.doesNotMatch(server, /noticeAppend\s*:/);
  assert.match(notice, /noticeAppend:\s*\{\s*verifiedQuote\s*\}/);
  assert.match(server, /yskNoticeAppendException/);
});
