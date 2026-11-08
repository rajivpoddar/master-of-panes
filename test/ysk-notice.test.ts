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
  type YskPin,
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

const PIN: YskPin = { paneId: "%11", processId: "4242", sessionId: "sess-a" };

function harness(
  capture: (pin: YskPin) => Promise<string | null>,
  ready: YskDriveDeps["ready"] = async () => ({ ok: true, pin: PIN }),
) {
  const keys: string[] = [];
  let enters = 0;
  const rows = new Map<string, "started" | "ambiguous" | "accepted">();
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
    claimNotice: (hash) => {
      const row = rows.get(hash);
      if (row) return "consumed";
      rows.set(hash, "started");
      return "execute";
    },
    finishNotice: (hash, state) => {
      rows.set(hash, state);
    },
    ready,
    revalidate: async () => ({ ok: true }),
    log: (result) => logs.push(result),
    submenuTimeoutMs: 0,
    quoteTimeoutMs: 0,
    pollMs: 0,
    submitTiming: { dwellMs: 0, payloadStableMs: 0, payloadGraceMs: 0, clearGraceMs: 0, pollMs: 0 },
  };
  return {
    deps,
    keys,
    logs,
    rows,
    enters: () => enters,
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
  assert.equal(box.rows.size, 0);
});

test("no notice is a no-op", async () => {
  const box = harness(async () => pane(["Waiting for 1 background agent to finish"], ""));
  const result = await driveYskNotice(box.deps);
  assert.equal(result.reason, "no_notice");
  assert.deepEqual(box.keys, []);
  assert.equal(box.rows.size, 0);
});

test("submenu missing after 1 aborts and remembers the note", async () => {
  const box = harness(async () => top());
  const result = await driveYskNotice(box.deps);
  assert.equal(result.outcome, "abort");
  assert.equal(result.reason, "submenu_missing");
  assert.deepEqual(box.keys, ["1"]);
  assert.equal(box.enters(), 0);
  assert.equal([...box.rows.values()][0], "ambiguous");
});

test("quote missing after 2 aborts without the suffix", async () => {
  let phase: "top" | "sub" = "top";
  const box = harness(async () => (phase === "top" ? top() : submenu()));
  box.deps.sendLiteral = async (text) => {
    box.keys.push(text);
    if (text === "1") phase = "sub";
  };
  const result = await driveYskNotice(box.deps);
  assert.equal(result.reason, "quote_not_owned");
  assert.deepEqual(box.keys, ["1", "2"]);
  assert.equal(box.enters(), 0);
  assert.equal([...box.rows.values()][0], "ambiguous");
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
  assert.equal(busy.rows.size, 0);
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

  let spacedEnters = 0;
  const spaced = await submitWithComposerCheck(quote + YSK_NOTICE_PROCESS_SUFFIX, {
    capture: async () => pane([], quote.replace(" ", "  ") + YSK_NOTICE_PROCESS_SUFFIX),
    pressSubmit: async () => {
      spacedEnters += 1;
    },
    sleep: async () => {},
    prePasteComposer: quote,
    noticeAppend: { verifiedQuote: quote },
    dwellMs: 0,
    payloadGraceMs: 0,
    payloadStableMs: 0,
    pollMs: 0,
    maxEnterPresses: 1,
  });
  assert.equal(spaced.enterPresses, 0);
  assert.equal(spacedEnters, 0);
});

test("the paste path does not grow its own noticeAppend hole", () => {
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const notice = readFileSync(new URL("../src/yskNotice.ts", import.meta.url), "utf8");
  assert.match(server, /driveYskNotice/);
  assert.doesNotMatch(server, /noticeAppend\s*:/);
  assert.match(notice, /noticeAppend:\s*\{\s*verifiedQuote\s*\}/);
  assert.match(server, /yskNoticeAppendException/);
  assert.match(notice, /maxEnterPresses:\s*1/);
  assert.match(server, /beginSendCustody/);
  assert.doesNotMatch(server, /yskSeenMemory|slice\(-50\)/);
});

test("a replaced pane receives no further key", async () => {
  let phase: "top" | "sub" = "top";
  let checks = 0;
  const box = harness(async () => (phase === "top" ? top() : submenu()));
  box.deps.sendLiteral = async (text) => {
    box.keys.push(text);
    if (text === "1") phase = "sub";
  };
  box.deps.revalidate = async () => {
    checks += 1;
    return checks >= 3 ? { ok: false, reason: "pane_replaced" } : { ok: true };
  };
  const result = await driveYskNotice(box.deps);
  assert.equal(result.reason, "pane_replaced");
  assert.deepEqual(box.keys, ["1"]);
  assert.equal(box.enters(), 0);
  assert.equal([...box.rows.values()][0], "ambiguous");
});

test("a foreign draft that merely contains the notice is not submitted", async () => {
  let phase: "top" | "sub" | "foreign" = "top";
  const box = harness(async () => {
    if (phase === "top") return top();
    if (phase === "sub") return submenu();
    return pane(["quoted into the prompt"], `please ignore this ${NOTE}`);
  });
  box.deps.sendLiteral = async (text) => {
    box.keys.push(text);
    if (text === "1") phase = "sub";
    else if (text === "2") phase = "foreign";
  };
  const result = await driveYskNotice(box.deps);
  assert.equal(result.reason, "quote_not_owned");
  assert.deepEqual(box.keys, ["1", "2"]);
  assert.equal(box.enters(), 0);
  assert.equal([...box.rows.values()][0], "ambiguous");
});

test("a delayed first submit is not followed by a second Enter", async () => {
  let phase: "top" | "sub" | "quote" | "suffixed" = "top";
  const box = harness(async () => {
    if (phase === "top") return top();
    if (phase === "sub") return submenu();
    if (phase === "quote") return pane(["quoted into the prompt"], NOTE);
    return pane(["still visible"], NOTE + YSK_NOTICE_PROCESS_SUFFIX);
  });
  box.deps.sendLiteral = async (text) => {
    box.keys.push(text);
    if (text === "1") phase = "sub";
    else if (text === "2") phase = "quote";
    else if (text === YSK_NOTICE_PROCESS_SUFFIX) phase = "suffixed";
  };
  box.deps.pressEnter = async () => {
    /* the composer stays on the exact payload */
  };
  const result = await driveYskNotice(box.deps);
  assert.equal(result.reason, "submit_uncertain");
  assert.equal(result.enterPresses, 1);
  assert.equal(box.enters(), 1);
  assert.deepEqual(box.keys, ["1", "2", YSK_NOTICE_PROCESS_SUFFIX]);
  assert.equal([...box.rows.values()][0], "ambiguous");
  const again = await driveYskNotice(box.deps);
  assert.equal(again.reason, "deduped");
  assert.equal(box.enters(), 1);
});

test("a consumed custody row from before restart sends nothing", async () => {
  const box = harness(async () => top());
  box.deps.claimNotice = () => "consumed";
  const result = await driveYskNotice(box.deps);
  assert.equal(result.reason, "deduped");
  assert.deepEqual(box.keys, []);
  assert.equal(box.enters(), 0);
});
