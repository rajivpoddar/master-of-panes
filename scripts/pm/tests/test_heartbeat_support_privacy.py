"""3h heartbeat support block: no customer text, no full email, bounded post length.

27 Nov: the 3h post quoted customer message excerpts and a full email (a 50-char cut broke the
address, so the post-level masker missed it) and ran to 30 lines, so the posting lane refused it.
"""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


support = _load("support-inbox-snapshot")
compose = _load("heartbeat-compose")

NOW = datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc)
EMAIL = "joshuachad03@gmail.com"
BODY = ("is running into a locked editor again after the save spinner, her deposition for "
        "Henderson County is due tomorrow morning")
ROOT_TEXT = f"<@U0ALEAYCAUT> <mailto:{EMAIL}|{EMAIL}> {BODY}"


def _thread():
    root = {"ts": "1795684309.275549", "user": "U09L1CTGP9T", "text": ROOT_TEXT}
    reply = {"ts": "1795684400.000000", "user": "U0ALEAYCAUT", "text": "On it, see #14634"}
    item = support.classify_thread([root, reply], 1795684309.275549 + 300 * 60)
    return support.stamp_channel(item, "C0AGWPQFKHA")


def _assert_no_customer_text(text: str):
    assert EMAIL not in text and "joshuachad" not in text and "mailto" not in text
    for word in ("spinner", "deposition", "Henderson", "tomorrow", "running into"):
        assert word not in text


def test_mask_matches_app_3212_examples():
    # lib/logger.ts maskEmail: "abilaasha" -> "a**la**ha", "kaiju03" -> "k**ju**".
    assert support.mask_email("abilaasha@x.com") == "a**la**ha@x.com"
    assert support.mask_email("kaiju03@x.com") == "k**ju**@x.com"
    assert compose._support_mask_email("abilaasha@x.com") == "a**la**ha@x.com"


def test_slack_thread_renders_masked_sender_topic_and_link_without_excerpt():
    item = _thread()
    assert "summary" not in item
    masked = support.mask_email(EMAIL)
    assert item["sender_masked"] == masked and item["topic"] == "editor locked / read-only"
    lines, _ = compose.support_block({"slack": [item], "in_app": []}, "3h")
    out = "\n".join(lines)
    _assert_no_customer_text(out)
    assert masked in out and "editor locked / read-only" in out
    assert "<https://scribiers.slack.com/archives/C0AGWPQFKHA/p1795684309275549|thread>" in out
    assert "answered by PM" in out and "linked #14634" in out


def test_needs_reply_line_keeps_age_flag_and_no_excerpt():
    root = {"ts": "1795684309.275549", "user": "U09L1CTGP9T", "text": ROOT_TEXT}
    item = support.stamp_channel(support.classify_thread([root], 1795684309.275549 + 95 * 60), "C0AGWPQFKHA")
    lines, tldr = compose.support_block({"slack": [item], "in_app": [], "threshold_min": 60}, "3h")
    out = "\n".join(lines)
    _assert_no_customer_text(out)
    assert "NEEDS REPLY (95m) ⚠" in out and "need a reply" in tldr


def test_in_app_row_renders_masked_submitter_topic_and_ticket_link():
    rows = [{"_id": "k17abc", "createdAt": 5000, "type": "bug", "status": "new", "userEmail": EMAIL,
             "title": "Export to docx drops my line numbers", "description": BODY}]
    item = support.classify_feedback_rows(rows, 1000)[0]
    assert "title" not in item and item["topic"] == "export / download issue"
    lines, _ = compose.support_block({"slack": [], "in_app": [item]}, "3h")
    out = "\n".join(lines)
    _assert_no_customer_text(out)
    assert "line numbers" not in out
    assert support.mask_email(EMAIL) in out
    assert "<https://app.heydonna.law/admin/feedback/k17abc|ticket>" in out


def test_composer_never_renders_free_text_or_raw_email_fields():
    # An older or tampered input: raw email, free-text topic, legacy summary/title.
    item = {"state": "needs_reply", "age_min": 5, "links": [], "sender_masked": EMAIL,
            "topic": BODY, "summary": ROOT_TEXT, "permalink": "javascript:alert(1)"}
    in_app = {"type": "bug", "state": "needs_triage", "title": BODY, "sender_masked": None}
    lines, _ = compose.support_block({"slack": [item], "in_app": [in_app]}, "3h")
    out = "\n".join(lines)
    _assert_no_customer_text(out)
    assert support.mask_email(EMAIL) in out and "general question" in out and "(no email)" in out
    assert "javascript" not in out


def test_snapshot_topics_are_inside_the_composer_closed_set():
    labels = {label for _, label in support.TOPIC_RULES} | {support.DEFAULT_TOPIC}
    assert labels <= compose.SUPPORT_TOPICS
    assert all(len(label.split()) <= 6 for label in labels)


def test_3h_post_is_capped_with_more_marker():
    slack = [dict(_thread(), ts=f"17956843{i:02d}.000000") for i in range(8)]
    for i, t in enumerate(slack):
        t.update(state="needs_reply", age_min=100 + i)
    decisions = {"items": [{"id": i, "issue": 9000 + i, "title": f"Decision {i}",
                            "created_at": "2026-11-27T00:00:00+00:00"} for i in range(8)]}
    in_app = support.classify_feedback_rows(
        [{"_id": f"k{i}", "createdAt": 5000, "type": "bug", "status": "new", "userEmail": EMAIL,
          "title": BODY} for i in range(5)], 1000)
    out = compose.compose("3h", NOW, None, {}, [], support={"slack": slack, "in_app": in_app},
                          decisions=decisions, email_doc={"status": "ok", "items": []},
                          failures=[f"collector {i}: rc=1" for i in range(3)])
    rows = out.split("\n")
    assert len(rows) <= compose.HEARTBEAT_3H_MAX_LINES
    assert rows[0].startswith("*Heartbeat") and rows[-1].startswith("_MoP composer release:")
    # The uncapped post is ~30 lines; the largest sections (Support, Decisions) were trimmed.
    support_rows = rows[rows.index(next(r for r in rows if r.startswith("*Support (3h):*"))):]
    support_rows = support_rows[:support_rows.index("")]
    assert any(compose._MORE_ROW.match(r) for r in support_rows)
    assert "*Support (3h):*" in out and "*Decisions pending on Rajiv:*" in out
    _assert_no_customer_text(out)


def test_cap_increments_existing_more_row_and_keeps_headers():
    report = "\n".join(["*Heartbeat x* — TL;DR: t.", "", "*A:* h", *[f"• a{i}" for i in range(6)], "• +2 more",
                        "", "*B:* h", "• b0", "_Ops:_ 1 failed — x"])
    out = compose.cap_3h_physical_lines(report, 9)
    rows = out.split("\n")
    assert len(rows) <= 9
    assert rows[0].startswith("*Heartbeat") and "*A:* h" in rows and "*B:* h" in rows
    assert "_Ops:_ 1 failed — x" in rows
    more = [r for r in rows if compose._MORE_ROW.match(r)]
    assert len(more) == 1 and int(compose._MORE_ROW.match(more[0])[1]) > 2


# CTO REVISE of 6c135e49 (C0ALZJHGE49 thread 1795719214.341069): raw Convex stderr reached the snapshot's
# failures field (printed in Slack), and a one-character mailbox stayed unmasked in the snapshot.
FAKE_STDERR = (f"Error fetching feedback row for {EMAIL}: description='{BODY[:60]}' "
               "(clipped joshuachad03@gmai")


def test_feedback_fetch_failure_is_fixed_text_and_raw_stderr_goes_to_0600_diagnostics(monkeypatch, tmp_path):
    diag = tmp_path / "diag.log"
    monkeypatch.setattr(support, "DIAG_LOG", diag)

    class Proc:
        returncode, stdout, stderr = 1, "", FAKE_STDERR

    monkeypatch.setattr(support.subprocess, "run", lambda *a, **k: Proc())
    monkeypatch.setattr(support, "DEADLINE", None)
    try:
        support.fetch_in_app(0)
        raise AssertionError("expected FetchFailed")
    except support.FetchFailed as exc:
        failure = support.record_failure("in-app feedback", exc)
    assert failure == "in-app feedback: fetch failed (exit 1)"
    snapshot = {"failures": [failure], "slack": [], "in_app": []}
    _assert_no_customer_text(support.json.dumps(snapshot))
    assert "@" not in support.json.dumps(snapshot)
    lines, status = compose.support_block(snapshot, "3h")
    assert lines == ["*Support (3h):* UNAVAILABLE (in-app feedback: fetch failed (exit 1))"]
    assert status == "support unavailable"
    assert FAKE_STDERR in support.json.loads(diag.read_text().splitlines()[-1])["detail"]
    assert (diag.stat().st_mode & 0o777) == 0o600


def test_composer_withholds_any_non_fixed_support_failure_and_collector_stderr(monkeypatch, tmp_path):
    diag = tmp_path / "diag.log"
    monkeypatch.setattr(compose, "SUPPORT_DIAG_LOG", diag)
    # An older collector's raw-text failure never reaches Slack.
    legacy = {"failures": [f"in-app feedback: convex data feedback failed: {FAKE_STDERR}"]}
    assert compose.support_failures(legacy) == [compose.SUPPORT_FAILURE_WITHHELD]
    lines, _ = compose.support_block(dict(legacy, slack=[], in_app=[]), "3h")
    _assert_no_customer_text("\n".join(lines))
    # The collector process exiting non-zero: fixed text, raw stderr in the 0600 file only.

    class Proc:
        returncode, stdout, stderr = 2, "", FAKE_STDERR

    monkeypatch.setattr(compose.subprocess, "run", lambda *a, **k: Proc())
    failures: list[str] = []
    assert compose._run_json(["support"], failures, "support", redact_stderr=True) is None
    assert failures == ["support: fetch failed (exit 2)"]
    assert FAKE_STDERR in diag.read_text() and (diag.stat().st_mode & 0o777) == 0o600


def test_slack_api_and_carry_failures_carry_only_status(monkeypatch, tmp_path):
    monkeypatch.setattr(support, "DIAG_LOG", tmp_path / "diag.log")

    def fetch(ts):
        raise RuntimeError(FAKE_STDERR)

    stats: dict = {}
    support.carry_forward([], {"1795684309.275549": 0.0}, fetch, stats=stats)
    problems = support.carry_problems(stats)
    text = support.failure_text("#heydonna-pm", "partial: " + "; ".join(problems))
    assert text == "#heydonna-pm: fetch failed (partial: 1 of 1 tracked threads failed RuntimeError)"
    assert compose.support_failures({"failures": [text]}) == [text]
    assert support.record_failure("#heydonna-pm", RuntimeError(FAKE_STDERR)) == "#heydonna-pm: fetch failed (RuntimeError)"


def test_one_character_and_short_mailboxes_are_masked_at_collection():
    for addr in ("a@x.com", "ab@x.com"):
        assert support.mask_email(addr) == "***@x.com"
        assert compose._support_mask_email(addr) == "***@x.com"
        root = {"ts": "1795684309.275549", "user": "U09L1CTGP9T", "text": f"<mailto:{addr}|{addr}> locked editor"}
        item = support.stamp_channel(support.classify_thread([root], 1795684309.275549 + 60), "C0AGWPQFKHA")
        assert item["sender_masked"] == "***@x.com"
        row = support.classify_feedback_rows([{"_id": "k1", "createdAt": 5000, "userEmail": addr}], 1000)[0]
        assert row["sender_masked"] == "***@x.com"
        assert addr not in support.json.dumps([item, row]).replace("***@x.com", "")
    assert support.mask_email("abc@x.com") == "a**@x.com"
