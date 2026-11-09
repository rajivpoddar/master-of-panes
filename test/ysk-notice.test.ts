import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";

import {
  YSK_NOTICE_PROCESS_SUFFIX,
  composerFrame,
  composerMatchesLogical,
  submitWithComposerCheck,
  yskNoticeAppendException,
} from "../src/composer.js";
import { MoPDatabase } from "../src/db.js";
import { DEFAULT_CONFIG } from "../src/types.js";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  driveYskNotice,
  normalizeYskNote,
  parseYskCapture,
  submenuBodyLines,
  yskAbortScreenHash,
  yskActivityRefusal,
  yskNoteHash,
  yskSideAgentQuote,
  type YskDriveDeps,
  type YskDriveResult,
  type YskNoticeCustody,
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

const PIN: YskPin = { paneId: "%11", processId: "4242", sessionId: "sess-a", activity: "idle" };
const QUOTE = yskSideAgentQuote(submenuBodyLines(submenu()) ?? []) ?? "";

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
    authority: () => null,
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
  assert.equal([...box.rows.values()][0], "retryable");
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
    if (phase === "quote") return pane(["quoted into the prompt"], QUOTE);
    if (phase === "suffixed") return pane(["quoted into the prompt"], QUOTE + YSK_NOTICE_PROCESS_SUFFIX);
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

test("an unreadable composer or unknown pane activity sends nothing", async () => {
  const unreadable = harness(async () => "plain shell $ ");
  assert.equal((await driveYskNotice(unreadable.deps)).reason, "composer_unreadable");
  assert.deepEqual(unreadable.keys, []);
  assert.equal(yskActivityRefusal("unknown"), "pm_activity_unknown");
  assert.equal(yskActivityRefusal("idle"), null);
  assert.equal(yskActivityRefusal("active"), null);
  const unknown = harness(async () => top(), async () => ({ ok: false, reason: "pm_activity_unknown" }));
  assert.equal((await driveYskNotice(unknown.deps)).reason, "pm_activity_unknown");
  assert.deepEqual(unknown.keys, []);
  assert.equal(unknown.rows.size, 0);
});

test("a running turn with an empty composer still submits the notice once", async () => {
  assert.equal(yskActivityRefusal("active"), null);
  let phase: "top" | "sub" | "quote" | "suffixed" | "done" = "top";
  const box = harness(async () => {
    if (phase === "top") return top();
    if (phase === "sub") return submenu();
    if (phase === "quote") return pane(["quoted into the prompt"], QUOTE);
    if (phase === "suffixed") return pane(["quoted into the prompt"], QUOTE + YSK_NOTICE_PROCESS_SUFFIX);
    return pane(["sent"], "");
  }, async () => {
    const refusal = yskActivityRefusal("active");
    return refusal ? { ok: false, reason: refusal } : { ok: true, pin: PIN };
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
  assert.equal(result.reason, "process_this_notice");
  assert.deepEqual(box.keys, ["1", "2", YSK_NOTICE_PROCESS_SUFFIX]);
  assert.equal(result.enterPresses, 1);
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  assert.match(server, /yskActivityRefusal\(activity\)/);
  assert.doesNotMatch(server, /activity === "active" \? "pm_busy"/);
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

test("a SessionStart during the awaited capture after revalidate sends no key or Enter", async () => {
  let session: string = PIN.sessionId;
  let captures = 0;
  const box = harness(async () => {
    captures += 1;
    // Capture 1 classifies the notice. Capture 2 is blocked()'s await, after
    // revalidate has already returned ok. The screen is still the old notice.
    if (captures >= 2) session = "sess-successor";
    return top();
  });
  box.deps.revalidate = async () => ({ ok: true });
  box.deps.authority = () => (session === PIN.sessionId ? null : "session_replaced");
  const result = await driveYskNotice(box.deps);
  assert.equal(result.reason, "session_replaced");
  assert.equal(result.outcome, "noop");
  assert.deepEqual(box.keys, []);
  assert.equal(box.enters(), 0);
  assert.equal(box.rows.size, 0);
  assert.ok(captures >= 2);
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

/** Composer box whose continuation lines are indented past ❯, as Ink paints them. */
function indentedQuote(logical: string): string {
  const visual = logical.split("\n").flatMap((line) => {
    if (line.startsWith("> Heads up") && line.length > 24) return [line.slice(0, 24), line.slice(24)];
    return [line];
  });
  const body = visual
    .map((line, index) => (index === 0 ? `❯ ${line}` : line === "" ? "" : `  ${line}`))
    .join("\n");
  return ["quoted into the prompt", RULE, body, RULE, "   Dhruva PM", ""].join("\n");
}

test("a prompt-indented quote stuck in the composer is submitted once, including mid-turn", async () => {
  assert.equal(composerMatchesLogical(composerFrame(indentedQuote(QUOTE))?.composer, QUOTE), true);
  assert.equal(composerMatchesLogical(composerFrame(indentedQuote(`${QUOTE} please ignore`))?.composer, QUOTE), false);
  let phase: "top" | "sub" | "quote" | "suffixed" | "done" = "top";
  const box = harness(async () => {
    if (phase === "top") return top();
    if (phase === "sub") return submenu();
    if (phase === "quote") return indentedQuote(QUOTE);
    if (phase === "suffixed") return indentedQuote(QUOTE + YSK_NOTICE_PROCESS_SUFFIX);
    return pane(["sent"], "");
  }, async () => ({ ok: true, pin: { ...PIN, activity: "active" } }));
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
  assert.equal(result.reason, "process_this_notice");
  assert.equal(result.enterPresses, 1);
  assert.deepEqual(box.keys, ["1", "2", YSK_NOTICE_PROCESS_SUFFIX]);
  assert.equal([...box.rows.values()][0], "accepted");
  phase = "top";
  const again = await driveYskNotice(box.deps);
  assert.equal(again.reason, "deduped");
  assert.equal(box.enters(), 1);
  assert.deepEqual(box.keys, ["1", "2", YSK_NOTICE_PROCESS_SUFFIX]);
});

test("exact side-agent quote submits process this once; foreign text does not", async () => {
  assert.ok(QUOTE.startsWith("Here is a note offered by a side agent:\n\n> Heads up · "));
  assert.equal(QUOTE + YSK_NOTICE_PROCESS_SUFFIX, QUOTE + "\n\nprocess this");
  let phase: "top" | "sub" | "quote" | "suffixed" | "done" = "top";
  const box = harness(async () => {
    if (phase === "top") return top();
    if (phase === "sub") return submenu();
    if (phase === "quote") return pane(["quoted"], QUOTE);
    if (phase === "suffixed") return pane(["quoted"], QUOTE + YSK_NOTICE_PROCESS_SUFFIX);
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
  assert.equal(result.enterPresses, 1);
  assert.equal([...box.rows.values()][0], "accepted");

  let foreignEnters = 0;
  const foreign = harness(async () => top());
  foreign.deps.sendLiteral = async (text) => {
    foreign.keys.push(text);
  };
  // top checks, then the submenu, then a composer that is not the exact quote.
  let step = 0;
  foreign.deps.capture = async () => {
    step += 1;
    if (step <= 3) return top();
    if (step <= 5) return submenu();
    return pane(["quoted"], `${QUOTE} please ignore`);
  };
  foreign.deps.pressEnter = async () => {
    foreignEnters += 1;
  };
  const refused = await driveYskNotice(foreign.deps);
  assert.equal(refused.reason, "quote_not_owned");
  assert.equal(refused.enterPresses, 0);
  assert.equal(foreignEnters, 0);
  assert.equal(yskNoticeAppendException(`${QUOTE} please ignore${YSK_NOTICE_PROCESS_SUFFIX}`, QUOTE, { verifiedQuote: QUOTE }), false);
});

test("submenu_missing retries only while idle, three times, then one failed log", async () => {
  type Row = { state: "started" | "ambiguous" | "accepted"; reason: string | null; attempts: number; detail: string | null };
  const row: { current: Row | null } = { current: null };
  const logs: YskDriveResult[] = [];
  const keys: string[] = [];
  const custody = (): YskNoticeCustody | null => {
    if (!row.current) return null;
    return {
      retryable: row.current.state === "started" && row.current.reason === "submenu_missing" && row.current.attempts < 3,
      terminal: row.current.state === "ambiguous" && row.current.reason === "failed",
      attempts: row.current.attempts,
    };
  };
  const deps = (activity: YskPin["activity"]): YskDriveDeps => ({
    capture: async () => top(),
    sendLiteral: async (text) => {
      keys.push(text);
    },
    pressEnter: async () => {
      throw new Error("enter");
    },
    sleep: async () => {},
    noticeCustody: () => custody(),
    claimNotice: () => {
      if (!row.current) {
        row.current = { state: "started", reason: null, attempts: 1, detail: null };
        return "execute";
      }
      if (row.current.state === "ambiguous" && row.current.reason === "failed") return "terminal";
      if (row.current.state === "started" && row.current.reason === "submenu_missing" && row.current.attempts < 3) {
        row.current = { ...row.current, attempts: row.current.attempts + 1, reason: null };
        return "execute";
      }
      return "consumed";
    },
    finishNotice: (_hash, state, reason, extra) => {
      if (!row.current) return;
      if (state === "retryable") {
        row.current.detail = extra?.screenHash ?? null;
        if (row.current.attempts >= 3) {
          row.current.state = "ambiguous";
          row.current.reason = "failed";
          return "terminal";
        }
        row.current.reason = "submenu_missing";
        return "retryable";
      }
      row.current.state = state === "accepted" ? "accepted" : "ambiguous";
      row.current.reason = reason;
      return state === "accepted" ? "accepted" : "ambiguous";
    },
    ready: async () => ({ ok: true, pin: { ...PIN, activity } }),
    revalidate: async () => ({ ok: true }),
    authority: () => null,
    log: (result) => logs.push(result),
    submenuTimeoutMs: 0,
    quoteTimeoutMs: 0,
    pollMs: 0,
  });

  const first = await driveYskNotice(deps("active"));
  assert.equal(first.reason, "submenu_missing");
  assert.equal(first.enterPresses, 0);
  assert.equal(row.current?.reason, "submenu_missing");
  assert.match(first.screenHash ?? "", /^[a-f0-9]{64}$/);
  assert.equal(first.screenHash, yskAbortScreenHash(top()));

  const busy = await driveYskNotice(deps("active"));
  assert.equal(busy.reason, "retry_waits_for_idle");
  assert.deepEqual(keys, ["1"]);

  const second = await driveYskNotice(deps("idle"));
  assert.equal(second.reason, "submenu_missing");
  const third = await driveYskNotice(deps("idle"));
  assert.equal(third.reason, "failed");
  assert.deepEqual(keys, ["1", "1", "1"]);
  assert.equal(logs.filter((line) => line.reason === "failed").length, 1);

  const again = await driveYskNotice(deps("idle"));
  assert.equal(again.reason, "deduped");
  assert.equal(again.pmLog, false);
  assert.deepEqual(keys, ["1", "1", "1"]);
  assert.equal(logs.filter((line) => line.reason === "failed").length, 1);
  assert.equal(row.current?.detail, third.screenHash);
});

test("custody retries submenu_missing at most three times and stores only safe abort metadata", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-ysk-retry-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  try {
    const scope = "ysk-notice:abc";
    const request = {
      scope,
      slot: 0,
      key: "abc",
      digest: "abc",
      commandDigest: "abc",
      destination: "%1|1|sess",
    };
    const tail = Array.from({ length: 50 }, (_, i) => `line ${i}`).join("\n");
    const stored = yskAbortScreenHash(tail);
    assert.match(stored, /^[a-f0-9]{64}$/);
    for (let attempt = 1; attempt <= 3; attempt++) {
      assert.equal(db.claimYskNotice(request, attempt), "execute");
      const marked = db.markYskSubmenuMissing(scope, stored, attempt);
      assert.equal(marked.attempts, attempt);
      assert.equal(marked.terminal, attempt === 3);
    }
    const failed = db.getSendCustody(scope);
    assert.equal(failed?.state, "ambiguous");
    assert.equal(failed?.reason, "failed");
    assert.equal(failed?.detail, stored);
    assert.equal(failed?.attempts, 3);
    assert.equal(db.claimYskNotice(request, 4), "terminal");
    assert.equal(db.claimYskNotice(request, 5), "terminal");

    const other = { ...request, scope: "ysk-notice:other", key: "other", digest: "other", commandDigest: "other" };
    assert.equal(db.claimYskNotice(other, 6), "execute");
    db.finishSendCustody(other.scope, "ambiguous", "quote_not_owned", 7, stored);
    assert.equal(db.claimYskNotice(other, 8), "consumed");
    assert.equal(db.getSendCustody(other.scope)?.detail, stored);

    db.finishSendCustody(scope, "accepted", "process_this_notice", 9, "enter=1");
    assert.equal(db.getSendCustody(scope)?.detail, "enter=1");
  } finally {
    db.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test("the notice server path claims through custody and does not gate PM input", () => {
  const server = readFileSync(new URL("../src/server.ts", import.meta.url), "utf8");
  const notice = readFileSync(new URL("../src/yskNotice.ts", import.meta.url), "utf8");
  assert.match(server, /claimYskNotice/);
  assert.match(server, /markYskSubmenuMissing/);
  assert.match(server, /joinWrapped \? "-J -p"/);
  assert.match(notice, /prior\?\.retryable && pin\.activity !== "idle"/);
  assert.match(notice, /YSK_NOTICE_PROCESS_SUFFIX/);
  assert.match(notice, /composerMatchesLogical\(composerOf\(snapshot\), ownedQuote\)/);
  assert.doesNotMatch(server, /UserPromptSubmit/);
});

test("retry cannot rebind original session, process, pane or unreadable destination", () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-ysk-pin-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  try {
    const request = { scope: "ysk-notice:pin", slot: 0, key: "pin", digest: "pin", commandDigest: "pin", destination: "%11|4242|sess-a" };
    assert.equal(db.claimYskNotice(request, 1), "execute");
    db.markYskSubmenuMissing(request.scope, null, 2);
    const original = db.getSendCustody(request.scope);
    for (const destination of ["%11|4242|sess-b", "%11|9999|sess-a", "%12|4242|sess-a", ""]) {
      assert.equal(db.claimYskNotice({ ...request, destination }, 3), "consumed");
      assert.deepEqual(db.getSendCustody(request.scope), original);
    }
  } finally { db.close(); rmSync(directory, { recursive: true, force: true }); }
});

test("foreign sensitive abort capture never reaches custody or events", async () => {
  const directory = mkdtempSync(join(tmpdir(), "mop-ysk-privacy-"));
  const db = new MoPDatabase({ ...DEFAULT_CONFIG, dbPath: join(directory, "mop.db") });
  const sentinel = "SENSITIVE_FOREIGN_CREDENTIAL_SENTINEL";
  const scope = "ysk-notice:" + yskNoteHash(NOTE);
  let reads = 0;
  const box = harness(async () => ++reads <= 3 ? top() : pane([sentinel], sentinel));
  box.deps.submenuTimeoutMs = 0;
  box.deps.claimNotice = (hash, destination) => db.claimYskNotice({ scope, slot: 0, key: hash, digest: hash, commandDigest: hash, destination }, 1);
  box.deps.finishNotice = (_hash, _state, reason, extra) => {
    db.markYskSubmenuMissing(scope, JSON.stringify({ ...extra, reason }), 2);
    return "retryable";
  };
  box.deps.log = result => { db.logEvent(0, "ysk_notice", null, null, { ...result }); };
  try {
    const result = await driveYskNotice(box.deps);
    assert.equal(result.outcome, "abort");
    assert.match(result.screenHash ?? "", /^[a-f0-9]{64}$/);
    assert.equal(result.screenHash, yskAbortScreenHash(pane([sentinel], sentinel)));
    assert.ok(!JSON.stringify(db.getSendCustody(scope)).includes(sentinel));
    assert.ok(!JSON.stringify(db.getEvents(0)).includes(sentinel));
    assert.ok(!JSON.stringify(result).includes(sentinel));
  } finally { db.close(); rmSync(directory, { recursive: true, force: true }); }
});
