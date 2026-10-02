"""Unit tests for the 3h/1h heartbeat redesign scripts (pure functions only)."""

import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "shared-assets/claude/scripts/pm/heartbeat"


def _load(name: str, base: Path = HERE):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), base / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


axiom = _load("heartbeat-axiom")
errmap = _load("heartbeat-error-map")
support = _load("support-inbox-snapshot")
prsnap = _load("pr-open-snapshot")
compose = _load("heartbeat-compose")

NOW = datetime(2026, 9, 29, 5, 30, tzinfo=timezone.utc)  # 11:00 IST


# --- axiom ---------------------------------------------------------------


def test_build_error_code_report_top_rest_and_deltas():
    codes = [{"code": f"c{i}", "events": i, "users": 1} for i in range(1, 8)]
    r = axiom.build_error_code_report(codes, {"events": 28, "users": 4}, {"events": 20}, [{"events": n} for n in (5, 30, 10, 12, 8, 40, 11)])
    assert [c["code"] for c in r["top"]] == ["c7", "c6", "c5", "c4", "c3"]
    assert r["other_codes"] == 2 and r["other_events"] == 3
    assert r["delta_vs_prior"] == 8
    assert r["same_slot_median_events"] == 11
    assert r["delta_vs_median"] == 17


def test_compare_windows_prior_and_same_slot():
    start = datetime(2026, 9, 29, 2, 30, tzinfo=timezone.utc)
    w = axiom.compare_windows(start, NOW)
    assert w["prior"] == (datetime(2026, 9, 28, 23, 30, tzinfo=timezone.utc), start)
    assert len(w["same_slot"]) == 7
    assert w["same_slot"][0][1] == datetime(2026, 9, 28, 5, 30, tzinfo=timezone.utc)


def test_median_even_and_empty():
    assert axiom.median([1, 3, 2, 4]) == 2.5
    assert axiom.median([]) is None


# --- error map -----------------------------------------------------------


def test_classify_mapping_new_when_nothing_matches():
    assert errmap.classify_mapping("x_failed", [], [], [])["status"] == "NEW"


def test_classify_mapping_orders_refs():
    m = errmap.classify_mapping(
        "x", [{"number": 8401, "is_pr": False}], [{"id": 7, "pr": 8402, "issue": None}], ["/a/docs/investigations/r.md"]
    )
    assert m["status"] == "mapped"
    assert m["refs"] == ["#8401 (issue)", "obl 7 PR#8402", "r.md"]


def test_search_pm_ops_only_open_rows(tmp_path):
    db = tmp_path / "pm-ops.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE obligations (id INTEGER PRIMARY KEY, status TEXT, target_id TEXT, pr INTEGER, issue INTEGER,"
        " title TEXT, required_action TEXT, blocker TEXT, evidence_json TEXT)"
    )
    conn.execute("INSERT INTO obligations VALUES (1,'open','','',NULL,'sync_save_failed spike','','','{}')")
    conn.execute("INSERT INTO obligations VALUES (2,'resolved','','',NULL,'sync_save_failed old','','','{}')")
    conn.commit()
    conn.close()
    assert [r["id"] for r in errmap.search_pm_ops("sync_save_failed", db)] == [1]


# --- support -------------------------------------------------------------

CUST = "UCUSTOMER1"


def _m(user, ts, text="hello", bot=None):
    d = {"user": user, "ts": f"{ts}.000000", "text": text}
    if bot:
        d["bot_id"] = bot
    return d


def test_customer_last_is_needs_reply():
    t = support.classify_thread([_m(CUST, 1000, "audio won't load"), _m("U0ALEAYCAUT", 1100, bot="B1"), _m(CUST, 1200, "still broken")], 1200 + 95 * 60)
    assert t["state"] == "needs_reply" and t["age_min"] == 95


def test_team_last_is_answered_and_pm_bot_counts_as_team():
    t = support.classify_thread([_m(CUST, 1000), _m("U0ALEAYCAUT", 1100, "Fixed in #8405", bot="B1")], 2000)
    assert t["state"] == "answered" and t["answered_by"] == "PM" and t["links"] == [8405]


def test_customer_thanks_does_not_reopen():
    t = support.classify_thread([_m(CUST, 1000), _m("UEQTTB97A", 1100, "done"), _m(CUST, 1200, "It worked!")], 2000)
    assert t["state"] == "answered"


def test_bot_and_team_roots_are_excluded_but_abi_root_is_support():
    assert support.classify_thread([_m("UALERT", 1000, bot="B9")], 2000) is None
    assert support.classify_thread([_m("U0BNFGX2UAX", 1000)], 2000) is None
    assert support.classify_thread([_m("U09L1CTGP9T", 1000, "roster q")], 2000)["state"] == "needs_reply"


def test_threshold_business_hours():
    assert support.needs_reply_threshold_minutes(NOW) == 60  # Tue 11:00 IST
    assert support.needs_reply_threshold_minutes(datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc)) == 180


def test_select_items_keeps_open_old_threads():
    rows = [{"state": "answered", "last_activity": 10}, {"state": "needs_reply", "last_activity": 10}, {"state": "answered", "last_activity": 500}]
    assert len(support.select_items(rows, 100)) == 2


def test_old_open_thread_outside_lookback_is_carried_forward_until_answered(tmp_path):
    # Root older than the 24h lookback: not in this run's history, but open last run.
    old = support.classify_thread([_m(CUST, 1000, "export broken")], 1000 + 30 * 3600)
    fetched = []

    def fetch(ts):
        fetched.append(ts)
        return old

    threads = support.carry_forward([], ["1000.000000"], fetch)
    assert fetched == ["1000.000000"] and threads[0]["state"] == "needs_reply"
    assert support.select_items(threads, 1000 + 27 * 3600) == threads
    path = tmp_path / "open.json"
    support.save_open_state({"C1": support.open_roots(threads)}, path)
    assert support.load_open_state(path) == {"C1": {"1000.000000": 0.0}}  # legacy list format still loads
    # Once answered it drops out of the persisted open set.
    answered = support.classify_thread([_m(CUST, 1000), _m("UEQTTB97A", 1100, "done")], 2000)
    assert support.open_roots(support.carry_forward([], ["1000.000000"], lambda ts: answered)) == []
    # Already-fetched roots are not refetched.
    fetched.clear()
    support.carry_forward([old], ["1000.000000"], fetch)
    assert fetched == []


def test_feedback_rows_window_and_link():
    rows = [
        {"_id": "a", "createdAt": 5000, "type": "bug", "title": "blank page, see #8405", "status": "new"},
        {"_id": "b", "createdAt": 10, "type": "bug", "title": "old", "status": "new"},
        {"_id": "c", "createdAt": 6000, "type": "feature", "title": "x", "status": "new"},
    ]
    out = support.classify_feedback_rows(rows, 1000)
    assert [(r["id"], r["state"]) for r in out] == [("a", "linked"), ("c", "needs_triage")]


# --- PR snapshot ---------------------------------------------------------

HEAD = "a" * 40


def _pr(checks, labels=(), draft=False, number=8400):
    return {"number": number, "headRefOid": HEAD, "isDraft": draft, "title": "t",
            "labels": [{"name": x} for x in labels], "statusCheckRollup": checks}


def _c(name, conclusion, status="COMPLETED", workflow="CI"):
    return {"name": name, "conclusion": conclusion, "status": status, "workflowName": workflow}


def test_duplicate_skipped_placeholder_does_not_hide_success():
    s = prsnap.summarize_checks([_c("e2e", "SKIPPED"), _c("e2e", "SUCCESS"), _c("test", "SUCCESS")])
    assert s["green"] == ["e2e", "test"] and not s["red"]


def test_green_gates_on_admitted_unslotted_head_is_merge_ask():
    r = prsnap.classify_pr(_pr([_c("test", "SUCCESS"), _c("e2e", "SUCCESS")], [f"ci-head:{HEAD}"]))
    assert r["state"] == "CI+E2E green" and r["next"] == "CTO merge ask" and r["owner"] == "PM"


def test_no_merge_ask_for_open_slot_lane_or_unadmitted_head():
    r = prsnap.classify_pr(_pr([_c("test", "SUCCESS"), _c("e2e", "SUCCESS")], ["slot:4"]))
    assert r["next"] != "CTO merge ask" and r["owner"] == "S4"
    r = prsnap.classify_pr(_pr([_c("test", "SUCCESS"), _c("e2e", "SUCCESS")]))
    assert r["next"] != "CTO merge ask"


def test_exemption_placeholder_plus_skipped_real_ci_is_not_green():
    ex = "CI Exemption (control-plane-only stable check)"
    checks = [_c("test", "SUCCESS", workflow=ex), _c("test", "SKIPPED"),
              _c("e2e", "SUCCESS", workflow=ex), _c("e2e", "SKIPPED", workflow="E2E Smoke Tests")]
    r = prsnap.classify_pr(_pr(checks, [f"ci-head:{HEAD}"]))
    assert r["state"] != "CI+E2E green" and r["next"] != "CTO merge ask"


def test_admitted_head_with_stale_slot_label_is_pm_owned():
    r = prsnap.classify_pr(_pr([_c("e2e", None, "IN_PROGRESS")], ["slot:1", f"ci-head:{HEAD}"]))
    assert r["owner"] == "PM" and r["state"] == "CI running"


def test_red_unowned_routes_to_repair():
    r = prsnap.classify_pr(_pr([_c("test", "FAILURE"), _c("e2e", "SKIPPED")]))
    assert r["state"] == "red: test, unowned" and r["next"] == "ci-repair-agent"
    r = prsnap.classify_pr(_pr([_c("e2e", "FAILURE")], ["slot:2"]))
    assert r["next"] == "e2e-failure-investigator"


def test_pm_obligation_claim_counts_as_owned():
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    iso = lambda d: d.isoformat().replace("+00:00", "Z")
    live = prsnap.active_claim({"owner": "pm-capture-8629", "suppress_until": iso(now + timedelta(minutes=30))}, now)
    assert live and live["owner"] == "pm-capture-8629"
    assert prsnap.active_claim({"owner": "pm-x", "suppress_until": iso(now - timedelta(minutes=1))}, now) is None
    assert prsnap.active_claim({"owner": "", "suppress_until": iso(now + timedelta(minutes=30))}, now) is None
    assert prsnap.active_claim({"owner": "pm-x", "suppress_until": None}, now) is None
    pr = _pr([_c("test", "FAILURE")], number=8629)
    r = prsnap.classify_pr(pr, claims={8629: live})
    assert r["state"] == "red: test"
    assert r["owner"].startswith("owner: pm-capture-8629 (until ")
    assert "unowned" not in prsnap.format_line(r)
    assert "unowned" in prsnap.classify_pr(pr, claims={1: live})["state"]
    r = prsnap.classify_pr(_pr([_c("test", "SUCCESS")], number=8629), claims={8629: live})
    assert r["owner"].startswith("owner: pm-capture-8629")


def test_running_on_admitted_head():
    r = prsnap.classify_pr(_pr([_c("typescript", None, "IN_PROGRESS")], [f"ci-head:{HEAD}"]))
    assert r["state"] == "CI running" and r["admitted"]


def test_stale_admission_label_is_not_admitted():
    r = prsnap.classify_pr(_pr([_c("test", "SKIPPED")], [f"ci-head:{'b' * 40}"]))
    assert r["state"] == "stale admission" and not r["admitted"]


def test_advisory_pm_state_label_is_ignored():
    r = prsnap.classify_pr(_pr([_c("test", "SUCCESS"), _c("e2e", "SUCCESS")], ["pm-state:qa-passed-awaiting-ci"]))
    assert r["state"] == "CI+E2E green"


def test_pm_blocked_green_admitted_head_never_gets_merge_ask():
    r = prsnap.classify_pr(_pr([_c("test", "SUCCESS"), _c("e2e", "SUCCESS")],
                               [f"ci-head:{HEAD}", "pm-blocked:product"]))
    assert r["next"] != "CTO merge ask" and r["state"].startswith("held (product)")


def _thread(author="chatgpt-codex-connector", body="![P1 Badge] bug", resolved=False, outdated=False):
    return {"author": author, "body": body, "isResolved": resolved, "isOutdated": outdated}


def test_stale_codex_label_with_zero_live_threads_is_not_a_wait_and_emits_action():
    pr = _pr([_c("test", "SUCCESS"), _c("e2e", "SUCCESS")], [f"ci-head:{HEAD}", "pm-blocked:codex"])
    pr["reviewThreads"] = [_thread(resolved=True), _thread(author="rajiv", body="P1 nit")]
    r = prsnap.classify_pr(pr)
    assert "codex" not in r["state"] and "codex" not in r["next"]
    assert r["next"] == "CTO merge ask"
    assert r["actions"] == [f"stale pm-blocked:codex label PR#8400@{HEAD} (0 live Codex P0/P1 threads) -> remove label"]


def test_unresolved_codex_p1_thread_is_a_wait_even_without_label():
    pr = _pr([_c("test", "SUCCESS"), _c("e2e", "SUCCESS")], [f"ci-head:{HEAD}"])
    pr["reviewThreads"] = [_thread()]
    r = prsnap.classify_pr(pr)
    assert r["state"].startswith("held (codex P1 x1)") and r["next"] != "CTO merge ask"
    assert r["actions"] == []


def test_outdated_codex_p1_is_owner_confirm_not_wait_and_p2_ignored():
    pr = _pr([_c("test", "SUCCESS"), _c("e2e", "SUCCESS")], [f"ci-head:{HEAD}"])
    pr["reviewThreads"] = [_thread(outdated=True), _thread(body="![P2 Badge] style")]
    r = prsnap.classify_pr(pr)
    assert "codex P1 outdated x1 — owner confirm" in r["state"] and r["next"] == "CTO merge ask"


def test_thread_fetch_failure_falls_back_to_label_hold():
    r = prsnap.classify_pr(_pr([_c("test", "SUCCESS")], ["pm-blocked:codex"]))
    assert r["state"].startswith("held (codex)") and r["actions"] == []


def test_advisory_check_red_does_not_block_merge_ask():
    checks = [_c("test", "SUCCESS"), _c("e2e", "SUCCESS"),
              _c("core-large-file-correctness", "FAILURE", workflow="E2E Large File Correctness")]
    r = prsnap.classify_pr(_pr(checks, [f"ci-head:{HEAD}"]))
    assert r["next"] == "CTO merge ask"
    assert r["state"].startswith("CI+E2E green") and "advisory red: core-large-file-correctness" in r["state"]
    pend = [_c("test", "SUCCESS"), _c("e2e", "SUCCESS"), _c("core-large-file-correctness", None, "IN_PROGRESS")]
    assert prsnap.classify_pr(_pr(pend, [f"ci-head:{HEAD}"]))["next"] == "CTO merge ask"


# --- composer ------------------------------------------------------------

AXIOM = {"events": 14, "users": 6, "prior_events": 9, "same_slot_median_events": 11,
         "top": [{"code": "sync_save_failed", "events": 6, "users": 3}, {"code": "export_docx_error", "events": 4, "users": 1}],
         "other_codes": 3, "other_events": 4}
MAP = {"sync_save_failed": {"status": "mapped", "refs": ["#8401 (issue)"]}, "export_docx_error": {"status": "NEW", "refs": []}}
PRS = [
    {"number": 8425, "head": "4c15294", "state": "CI+E2E green", "owner": "S2", "next": "CTO merge ask"},
    {"number": 8430, "head": "9ab12cd", "state": "red: test, unowned", "owner": "PM", "next": "ci-repair-agent"},
]
SUPPORT = {"threshold_min": 60, "in_app": [],
           "slack": [{"channel": "#heydonna-feedback", "state": "needs_reply", "age_min": 95, "summary": "audio won't load", "links": []}]}


def test_3h_always_mentions_cto_first_even_without_asks():
    out = compose.compose("3h", NOW, {"events": 0, "users": 0, "top": []}, {}, [], support={"slack": [], "in_app": []})
    first = out.splitlines()[0]
    assert first.startswith("<@U0BNFGX2UAX> *Heartbeat 11:00 IST*")
    assert "quiet" in out and "Asks" not in out


def test_3h_full_post_shape():
    out = compose.compose("3h", NOW, AXIOM, MAP, PRS, support=SUPPORT)
    lines = out.splitlines()
    assert len(lines) <= 20
    assert "|---" not in out  # no tables
    assert "NEW → launch customer-artifact-investigator" in out
    assert "NEEDS REPLY (95m) ⚠" in out
    assert lines[-1] == "*Asks:* merge ask: #8425@4c15294"
    assert out.index("#8430") < out.index("#8425")  # red first


def test_1h_mentions_cto_only_with_asks():
    no_ask = compose.compose("1h", NOW, AXIOM, MAP, [PRS[1]])
    assert no_ask.startswith("*Hourly — 11:00 IST*") and "<@U0BNFGX2UAX>" not in no_ask
    assert "Support" not in no_ask
    ask = compose.compose("1h", NOW, AXIOM, MAP, PRS)
    assert ask.startswith("<@U0BNFGX2UAX> *Hourly — 11:00 IST*")
    esc = compose.compose("1h", NOW, AXIOM, MAP, [], escalations=["ladder row 12: product call on #8401"])
    assert esc.startswith("<@U0BNFGX2UAX>") and "escalation: ladder row 12" in esc


def test_ops_footer_only_on_failure():
    assert "_Ops:_" not in compose.compose("1h", NOW, AXIOM, MAP, PRS)
    out = compose.compose("1h", NOW, None, {}, PRS, failures=["axiom: rc=1"])
    assert out.splitlines()[-1].startswith("_Ops:_ 1 failed") and "unavailable" in out


@pytest.mark.parametrize("n", [3, 20])
def test_pr_list_is_capped(n):
    prs = [{"number": 8000 + i, "head": "abc1234", "state": "not admitted", "owner": "PM", "next": "x"} for i in range(n)]
    out = compose.compose("3h", NOW, AXIOM, MAP, prs, support=SUPPORT)
    assert len(out.splitlines()) <= 22


# --- active users (Rajiv 2026-09-29 13:17 IST) ---------------------------


def test_active_users_report_groups_by_userid_and_splits_internal():
    rows = [
        # same person, two loggers with different masks -> ONE user
        {"userId": "user_A", "emails": ["b**in**i@gmail.com", "unknown", "userId-only"], "actions": 50, "files": 2},
        {"userId": "user_A", "emails": ["b***@gmail.com"], "actions": 5, "files": 1},
        {"userId": "user_B", "emails": ["bob@law.com"], "actions": 9, "files": 4},
        {"userId": "user_C", "emails": ["a**la**ha@scribie.com"], "actions": 30, "files": 2},
        {"userId": "user_D", "emails": ["e**-r**ne*@gmail.com"], "actions": 3, "files": 1},
        {"userId": "pseudonymous", "emails": ["not_collected"], "actions": 900, "files": 0},
        {"userId": "unknown", "emails": ["cron"], "actions": 9, "files": 0},
    ]
    prior = [{"userId": "user_A", "emails": ["b***@gmail.com"], "actions": 1, "files": 1}]
    r = axiom.build_active_users_report(rows, prior)
    assert r["count"] == 2 and r["internal"] == 2 and r["prior_count"] == 1
    assert [u["user"] for u in r["users"]] == ["bo***@law.com", "b**in**i@gmail.com"]


def test_internal_matching_survives_masking():
    assert axiom.is_internal_identity("user_x", "e**-r**ne*@gmail.com")
    assert axiom.is_internal_identity("user_x", "q*-b**@gmail.com")
    assert axiom.is_internal_identity("user_x", "a**@scribie.com")
    assert not axiom.is_internal_identity("user_x", "e***@gmail.com")
    assert not axiom.is_internal_identity("user_x", "b**in**i@gmail.com")
    # 12b must-fix: masked customer names must not hit separator-terminated prefixes
    assert not axiom.is_internal_identity("u", "qa*im@gmail.com")   # e.g. Qasim
    assert not axiom.is_internal_identity("u", "t**t*r*@x.com")     # e.g. Trotter
    assert axiom.is_internal_identity("u", "p*a*w*i*h*@gmail.com")  # playwright keeps 2-char min


def test_active_users_query_excludes_none_fileid_and_groups_by_userid(monkeypatch):
    seen = {}
    monkeypatch.setattr(axiom, "run_apl_query", lambda t, apl, s, e: seen.setdefault("apl", apl) and {})
    monkeypatch.setattr(axiom, "extract_aggregations", lambda raw: [])
    axiom.query_active_user_files("t", "s", "e")
    assert "['fileId'] != 'none'" in seen["apl"] and "by ['userId']" in seen["apl"]


def test_compose_1h_includes_active_users_block_under_line_budget():
    active = {"count": 3, "prior_count": 5, "internal": 1,
              "users": [{"user": f"u{i}***@x.com", "files": i, "actions": 1} for i in range(3)]}
    prs = [{"number": str(8000 + i), "head": "abc1234", "state": "open", "owner": "s1", "next": "wait"} for i in range(8)]
    msg = compose.compose("1h", NOW, {"events": 0, "users": 0}, {}, prs, active=active, include_active=True)
    lines = msg.splitlines()
    assert "*Active users (1h):* 3 (prior 5) +1 internal" in lines
    assert "• u0***@x.com (0f), u1***@x.com (1f), u2***@x.com (2f)" in lines
    assert "3 active users" in lines[0]
    assert len(lines) <= 22


def test_compose_without_flag_omits_active_users():
    msg = compose.compose("1h", NOW, {"events": 0, "users": 0}, {}, [])
    assert "Active users" not in msg


# --- slots (Rajiv 2026-09-29 13:23 IST) -----------------------------------

SLOTS_FIXTURE = {"slots": [
    {"slot": 1, "name": "Rohini", "occupied": True, "issue": 8153, "pr": 8473, "idle": False,
     "active_turn_state": "active", "last_meaningful_work_at": "2026-09-29T05:29:00Z",
     "last_activity": "2026-09-29T05:29:30.000"},
    {"slot": 2, "name": "Hasta", "occupied": True, "issue": 8478, "pr": None, "idle": True,
     "active_turn_state": "idle", "last_meaningful_work_at": "2026-09-29T05:05:00Z",
     "last_activity": "2026-09-29T05:06:00.000"},
    {"slot": 3, "name": "Ashwini", "occupied": False, "issue": None, "pr": None, "idle": True,
     "active_turn_state": None, "last_meaningful_work_at": None, "last_activity": "2026-09-29T04:50:00.000"},
    {"slot": 4, "name": "Chitra", "occupied": False, "issue": None, "pr": None, "idle": True,
     "active_turn_state": None, "last_meaningful_work_at": None, "last_activity": "2026-09-29T05:20:00.000"},
]}


def test_slot_block_flags_idle_occupied_and_free_over_20m():
    lines, tldr, actions = compose.slot_block(SLOTS_FIXTURE, NOW)
    text = "\n".join(lines)
    assert "S1 Rohini: #8153/PR#8473 working" in text
    assert "S2 Hasta: #8478 idle 25m ⚠" in text
    assert "S3 Ashwini: free 40m ⚠" in text
    assert "S4 Chitra: free 10m" in text and "S4 Chitra: free 10m ⚠" not in text
    assert actions == ["idle-slot S2 #8478 idle 25m", "idle-slot S3 free idle 40m"]
    assert tldr.startswith("2 idle slot(s)")


def test_slot_block_mop_unreachable_does_not_fail():
    lines, tldr, actions = compose.slot_block(None, NOW)
    assert lines == ["*Slots:* MoP unreachable"] and actions == []


def test_compose_with_slots_and_active_users_stays_under_24_lines():
    active = {"count": 3, "prior_count": 5, "internal": 1,
              "users": [{"user": f"u{i}***@x.com", "files": i, "actions": 1} for i in range(5)]}
    prs = [{"number": str(8000 + i), "head": "abc1234", "state": "open", "owner": "s1", "next": "wait"} for i in range(12)]
    six = {"slots": SLOTS_FIXTURE["slots"] + [dict(SLOTS_FIXTURE["slots"][0], slot=5), dict(SLOTS_FIXTURE["slots"][0], slot=6)]}
    msg = compose.compose("1h", NOW, {"events": 3, "users": 1, "top": [{"code": "x", "events": 3, "users": 1}]},
                          {}, prs, active=active, include_active=True, slots=six, include_slots=True)
    lines = msg.splitlines()
    assert "*ACTIONS (PM):* idle-slot S2 #8478 idle 25m; idle-slot S3 free idle 40m" in lines
    assert len(lines) <= 24


def test_known_codes_map_marks_code_mapped_not_new(tmp_path):
    kc = tmp_path / "known.json"
    kc.write_text('{"contact_config_error": "missing TURNSTILE_SECRET_KEY (thread 1790662166.619739)"}')
    known = errmap.load_known_codes(kc)
    r = errmap.classify_mapping("contact_config_error", [], [], [], known)
    assert r["status"] == "mapped" and r["refs"] == ["missing TURNSTILE_SECRET_KEY (thread 1790662166.619739)"]
    assert errmap.classify_mapping("other", [], [], [], known)["status"] == "NEW"
    assert errmap.load_known_codes(tmp_path / "absent.json") == {}


def test_shipped_known_codes_seed():
    assert "TURNSTILE_SECRET_KEY" in errmap.load_known_codes()["contact_config_error"]


# --- Codex round 2 on MoP #7 ------------------------------------------------


def test_answered_thread_is_tracked_and_reopens_on_new_customer_reply(tmp_path):
    answered = support.classify_thread([_m(CUST, 1000, "export broken"), _m("UEQTTB97A", 1100, "fixed now")], 2000)
    assert answered["state"] == "answered"
    tracked = support.tracked_roots([answered], 2000)
    assert tracked == {"1000.000000": 1100.0}
    path = tmp_path / "open.json"
    support.save_open_state({"C1": tracked}, path)
    prev = support.load_open_state(path)["C1"]
    # Next run: root is outside the lookback, customer wrote again after PM's reply.
    reopened = support.classify_thread(
        [_m(CUST, 1000, "export broken"), _m("UEQTTB97A", 1100, "fixed now"), _m(CUST, 5000, "still failing on page 3")],
        6000)
    fetched = []
    threads = support.carry_forward([], prev, lambda ts: fetched.append(ts) or reopened)
    assert fetched == [answered["ts"]]
    assert threads[0]["state"] == "needs_reply"
    assert support.select_items(threads, 5900) == threads


def test_tracked_roots_retains_stale_answered_and_open():
    now = 100 * 86400
    old_answered = {"ts": "1", "state": "answered", "last_activity": 1.0}
    old_open = {"ts": "2", "state": "needs_reply", "last_activity": 2.0}
    # Codex #7 P1: answered roots are retained indefinitely so a later customer reply is rediscovered.
    assert list(support.tracked_roots([old_answered, old_open], now)) == ["1", "2"]


def test_free_slot_idle_measured_from_release_not_stale_work():
    now = NOW
    # Released 5 min ago; last_meaningful_work_at is 3h old (pre-release) and must be ignored.
    doc = {"slots": [{"slot": 3, "name": "Ashwini", "occupied": False, "assignment_epoch": 801,
                      "last_meaningful_work_at": "2026-09-29T02:30:00Z", "last_activity": None}]}
    prev = {"3": {"sig": [800, True], "since": "2026-09-29T01:00:00+00:00"}}
    changed, state = compose.slot_transitions(doc, now - __import__("datetime").timedelta(minutes=5), prev)
    lines, _, actions = compose.slot_block(doc, now, changed)
    assert "S3 Ashwini: free 5m" in "\n".join(lines) and actions == []
    # Unchanged signature keeps the original release time -> flagged once >= 20m.
    changed2, _ = compose.slot_transitions(doc, now + __import__("datetime").timedelta(minutes=30), state)
    _, _, actions2 = compose.slot_block(doc, now + __import__("datetime").timedelta(minutes=30), changed2)
    assert actions2 == ["idle-slot S3 free idle 35m"]


def test_error_users_count_by_userid_not_sentinel_email():
    rows = [("user_A", "not_collected"), ("user_B", "not_collected"), ("user_C", "none"),
            ("unknown", "b***@gmail.com"), ("pseudonymous", "not_collected")]
    who = {axiom.error_identity(u, e) for u, e in rows} - {""}
    assert who == {"user_A", "user_B", "user_C", "b***@gmail.com"}
    ext = axiom.ERROR_CODE_EXTEND
    assert "case(isnotempty(uid)" in ext and "'not_collected'" in ext and "'none'" in ext
    assert ext.index("uid, isnotempty(em)") > 0  # userId branch precedes email fallback


# --- PM-bot replies + closing thanks (false "needs_reply" on answered threads, 2026-09-30) ---------
# Sanitized shapes of the two real threads. The PM bot posts as user U0ALEAYCAUT *and* carries
# bot_id/app_id/bot_profile; requester closings are synthetic paraphrases (no customer text).

PM_ID = "U0ALEAYCAUT"
ABI = "U09L1CTGP9T"


def _pm_bot(ts, text, requester, **extra):
    d = {
        "type": "message", "user": PM_ID, "bot_id": "B0ALH9R1LRK", "app_id": "A0ALQA1BVLL",
        "bot_profile": {"id": "B0ALH9R1LRK", "name": "Dhurva PM", "app_id": "A0ALQA1BVLL", "deleted": False},
        "parent_user_id": requester, "ts": f"{ts}.000000", "text": text,
    }
    d.update(extra)
    return d


def _req(user, ts, text):
    return {"type": "message", "user": user, "client_msg_id": f"m{ts}", "ts": f"{ts}.000000", "text": text}


def test_feedback_thread_pm_bot_reply_then_closing_thanks_is_answered():
    # #heydonna-feedback shape: root, PM bot reply, requester closes with "... it worked. thank you!"
    msgs = [
        _req(CUST, 1000, "Is there an autocorrect feature?"),
        _pm_bot(1100, "Yes, under Settings > Text replacement.", CUST),
        _req(CUST, 1900, "I turned it on and it worked. thank you!"),
    ]
    t = support.classify_thread(msgs, 1900 + 79 * 60)
    assert t["state"] == "answered" and t["answered_by"] == "PM"


def test_pm_channel_thread_abi_root_pm_bot_replies_then_closing_confirmation_is_answered():
    # #heydonna-pm shape: Abi (in TEAM) is the requester; mention-prefixed closings after PM bot replies.
    msgs = [
        _req(ABI, 1000, "Got an error creating a project"),
        _pm_bot(1010, "Got it, looking.", ABI),
        _req(ABI, 1400, "Same error on the second file"),
        _pm_bot(2000, "Fixed, please retry.", ABI),
        _req(ABI, 3000, f"<@{PM_ID}> thanks for this, I re-ran it and it went through"),
        _pm_bot(3010, "Thanks Abi, glad it went through. Did the second file also create fine?", ABI),
        _req(ABI, 3050, f"<@{PM_ID}> Was able to create both projects."),
    ]
    t = support.classify_thread(msgs, 3050 + 183 * 60)
    assert t["state"] == "answered" and t["answered_by"] == "PM"
    assert support.select_items([t], 3050 + 183 * 60 + 1) == []  # answered + stale -> not reported


def test_bot_id_only_pm_reply_counts_as_answer():
    # Defensive: a bot_message-shaped post from the PM bot carries bot_id but no user.
    reply = {"type": "message", "subtype": "bot_message", "bot_id": "B0ALH9R1LRK", "username": "Dhurva PM",
             "ts": "1100.000000", "text": "Fixed."}
    t = support.classify_thread([_req(CUST, 1000, "export blank"), reply], 2000)
    assert t["state"] == "answered" and t["answered_by"] == "PM"
    # ...and a bot_id-only PM post is never a support root.
    assert support.classify_thread([reply], 2000) is None


@pytest.mark.parametrize("text", [
    "It worked!",
    "thanks",
    "Thank you!",
    "I tried that and it worked. thank you!",
    f"<@{PM_ID}> thanks for this, I re-ran it and it went through",
    f"<@{PM_ID}> Was able to create both projects.",
    "Perfect, thanks a lot for the quick fix",
    "all set, appreciate the help",
])
def test_closing_ack_recognised(text):
    assert support.is_closing_ack(text)


@pytest.mark.parametrize("text", [
    "Thanks, but it still does not work",                                   # Codex #7 P1
    "It worked for the first file but not the second",
    "thanks! can you also check the second file?",
    "that worked, however the export is blank",
    "thanks. the second file failed with an error",
    "Yes, it is about 40 pages",                                            # bare 'yes' + new info
    "still broken",
    "audio won't load",
    "Thanks. Also please process the attached file and send me the transcript when it is ready tonight.",
    f"<@{PM_ID}> can you look at this again",
    "",
])
def test_non_closing_messages_still_reopen(text):
    assert not support.is_closing_ack(text)


def test_thanks_with_new_problem_after_pm_reply_stays_needs_reply():
    msgs = [_req(CUST, 1000, "export blank"), _pm_bot(1100, "Fixed.", CUST),
            _req(CUST, 1200, "Thanks, but it still does not work")]
    assert support.classify_thread(msgs, 2000)["state"] == "needs_reply"


def test_closing_thanks_without_any_team_reply_does_not_answer():
    # A lone customer "thanks" must not be swallowed when nobody on the team ever replied.
    msgs = [_req(CUST, 1000, "export blank"), _req(CUST, 1100, "thanks")]
    assert support.classify_thread(msgs, 2000)["state"] == "needs_reply"


def test_answered_pm_threads_compose_without_needs_reply_or_overdue():
    feedback = support.classify_thread(
        [_req(CUST, 1000, "autocorrect?"), _pm_bot(1100, "Yes.", CUST), _req(CUST, 1900, "it worked. thank you!")], 1900 + 79 * 60)
    pm = support.classify_thread(
        [_req(ABI, 1000, "error"), _pm_bot(1100, "Fixed?", ABI), _req(ABI, 1200, f"<@{PM_ID}> Was able to create both projects.")], 1200 + 183 * 60)
    for t, ch in ((feedback, "#heydonna-feedback"), (pm, "#heydonna-pm")):
        t["channel"] = ch
    lines, tldr = compose.support_block({"threshold_min": 60, "slack": [feedback, pm], "in_app": []}, "3h")
    assert tldr == "support answered"
    assert "NEEDS REPLY" not in "\n".join(lines) and "overdue" not in tldr
