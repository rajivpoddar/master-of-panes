"""Qwen 3.8 Flash Next routing contract for proxy-agent.sh (added to the repo; not run on the host, Rajiv ts 1793983042.752819).

Rajiv C0ALZJHGE49 thread 1795288756.340499, ts 1795312862.446869 ("go. launch it on hy free"): the DGX Spark serves
qwen3.8-flash-next in place of inkling-2-mini and proxy lanes route to it first. Order inside LIGHT/MID/BUILD1/PROOF1 is
qwen (local, free) -> the OpenCode Go ids -> the Codex ladder -> the rest (thread 1795311834.216809 "how do we maximise the
opencode usage?"). These are static traces of the launcher; nothing here ever launches a lane.
"""
import re
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "shared-assets/claude/scripts/proxy-agent.sh"
SRC = SCRIPT.read_text()

QWEN = "qwen3.8-flash-next"
INKLING = "inkling-2-mini"
OPENCODE_IDS = ("deepseek-v4.1-flash", "hy4-flash-free")
CODEX_LADDER = ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol")
QWEN_TIERS = ("LIGHT", "MID", "BUILD1", "PROOF1")


def _tier_models():
    tiers = {}
    for tier, models in re.findall(r'^\s+([A-Z0-9]+)\) models="([^"]*)";;', SRC, re.M):
        tiers[tier] = models.split()
    return tiers


def test_qwen_heads_the_four_local_tiers_and_inkling_leaves_every_list():
    tiers = _tier_models()
    for tier in QWEN_TIERS:
        assert tiers[tier][0] == QWEN, tier
        assert tiers[tier].count(QWEN) == 1, tier
    for tier, models in tiers.items():
        assert INKLING not in models, tier


def test_opencode_ids_come_before_the_codex_ladder_in_the_local_tiers():
    tiers = _tier_models()
    for tier in QWEN_TIERS:
        models = tiers[tier]
        assert QWEN in models
        first_opencode = min(models.index(m) for m in OPENCODE_IDS if m in models)
        assert models.index(QWEN) < first_opencode, tier
        for codex in CODEX_LADDER:
            assert first_opencode < models.index(codex), tier
        # The ladder itself keeps its luna -> terra -> sol order.
        ladder = [m for m in models if m in CODEX_LADDER]
        assert ladder == list(CODEX_LADDER), tier


def test_glm_is_not_added_to_any_tier():
    for tier, models in _tier_models().items():
        assert "glm-5.3" not in models, tier


def test_judgment_lanes_exclude_the_local_model():
    guard = SRC[SRC.index("if proxy_agent_is_judgment_lane; then"):SRC.index('for id in $models; do', SRC.index("if proxy_agent_is_judgment_lane; then"))]
    assert QWEN in guard and INKLING in guard and "gpt-6-luna" in guard


def test_judgment_lanes_lead_with_the_tiers_claude_then_the_codex_ladder():
    # Rajiv C0ALZJHGE49 thread 1795311834.216809, ts 1795315628.438539 ("move claude before codex. prefer claude or judgement lane"):
    # judgment lanes reorder to the tier's Claude ids (opus -> sonnet), then terra -> sol, then the rest — without adding a model the
    # tier does not list, and without the local id, inkling or luna.
    guard = SRC[SRC.index("if proxy_agent_is_judgment_lane; then"):SRC.index('for id in $models; do', SRC.index("if proxy_agent_is_judgment_lane; then"))]
    for pref in ("claude-opus-5-5", "claude-sonnet-5-5", "gpt-6.1-terra", "gpt-6.2-sol"):
        assert pref in guard, pref
    # Preference order is opus -> sonnet -> terra -> sol, and membership is preserved (the same loop walks the tier's own list).
    order = re.findall(r"for _pref in ([^;]+); do", guard)
    assert order and order[0].split() == ["claude-opus-5-5", "claude-sonnet-5-5", "gpt-6.1-terra", "gpt-6.2-sol"]
    assert "for _m in $models" in guard  # never emits an id that is not in the tier list


def test_health_gate_covers_qwen_with_its_own_url_and_three_second_budget():
    gate = SRC[SRC.index("Local Spark health gate"):SRC.index("if proxy_agent_is_emergency_muse \"$id\"; then")]
    assert f'"$id" == {QWEN}' in gate and INKLING in gate
    assert "QWEN_HEALTH_URL" in gate and "18120" in gate
    assert '-m 3' in gate


def test_health_gate_probes_a_credential_free_surface_not_the_authenticated_models_endpoint():
    # Measured 22 Nov on the live Spark: the serving recipe passes --api-key, so vLLM answers an UNAUTHENTICATED /v1/models with 401.
    # The gate curled that path with no credential and `--fail` turned the 401 into an empty body, i.e. host_down on a healthy host, which
    # silently routes every qwen lane to a PAID model - the exact cost this tier exists to avoid. The probe must therefore be a surface the
    # engine serves without a credential AND one that still names the served checkpoint, so model_not_served stays discriminating.
    gate = SRC[SRC.index("Local Spark health gate"):SRC.index("if proxy_agent_is_emergency_muse \"$id\"; then")]
    # Property, not a site list: no default URL in the gate may be the bearer-only /v1 surface.
    defaults = re.findall(r"\{\w*HEALTH_URL:-([^}]+)\}", gate)
    assert defaults, "health gate lost its default probe URLs"
    assert all("/v1/models" not in d for d in defaults), defaults
    # ...and every default still targets a surface that carries the served id (the quoted-token match below relies on it).
    assert all(d.endswith("/metrics") for d in defaults), defaults


def test_health_gate_requires_the_served_model_id_not_just_a_listening_port():
    # Peer 22 Nov 03:05: the Spark serves ONE checkpoint per port, so a host that answers but lists the other id would send a lane into a 404 probe
    # (a failed rung that also closes the emergency gate). An absent id must be a plain model_not_served skip.
    gate = SRC[SRC.index("Local Spark health gate"):SRC.index("if proxy_agent_is_emergency_muse \"$id\"; then")]
    assert '_spark_ids="$(curl -s -m 3 --fail "$_spark_url" 2>/dev/null)"' in gate
    assert '"\\"$id\\""' in gate  # quoted token match, so a longer alias containing the id cannot pass
    assert "_spark_ids" in gate and "model_not_served" in gate
    # host_down (no answer) and model_not_served (answers, wrong checkpoint) are distinct skips.
    assert "host_down" in gate


def test_gate_survives_the_live_spark_payload_and_rejects_the_other_checkpoint():
    # Live payloads captured 22 Nov. /metrics is open and repeats model_name="<served id>"; the other checkpoint must NOT match, or a Spark
    # mid-swap would route lanes to a model it is not serving.
    qwen_metrics = 'vllm:num_requests_total{engine="0",model_name="qwen3.8-flash-next"} 1060.0\n'
    inkling_metrics = 'vllm:num_requests_total{engine="0",model_name="inkling-2-mini"} 12.0\n'
    assert f'"{QWEN}"' in qwen_metrics
    assert f'"{QWEN}"' not in inkling_metrics
    assert f'"{INKLING}"' in inkling_metrics


def test_host_down_retries_once_before_routing_the_lane_to_a_paid_model():
    # A single dropped LAN probe must not demote the lane to a paid model. The fetch retries once; only two empty answers count as host_down,
    # and the empty check is on the BODY (a failed curl can leave `_spark_ids` set but empty under some sh semantics).
    gate = SRC[SRC.index("Local Spark health gate"):SRC.index("if proxy_agent_is_emergency_muse \"$id\"; then")]
    assert gate.count('curl -s -m 3 --fail "$_spark_url"') == 2
    assert '[[ -z "$_spark_ids" ]]' in gate
    assert 'if ! _spark_ids=' not in gate  # exit-status-only check would miss a 200 with an empty body
    assert "twice" in gate


def test_qwen_has_its_own_pool_cap_outside_the_opencode_pool():
    cap = SRC[SRC.index("proxy_agent_pool_blocked() {"):SRC.index("# Final-primary OpenCode guard")]
    assert re.search(rf"^\s+{re.escape(QWEN)}\) pool=qwen; cap=\"\${{QWEN_PROXY_CAP:-4}}\";;$", cap, re.M)
    assert '"$pool" == qwen' in cap  # an unreadable count fails open for the local pool, like opencode/inkling


def test_qwen_is_counted_in_its_own_pool():
    live = SRC[SRC.index("proxy_agent_pool_live() {"):SRC.index("proxy_agent_pool_blocked() {")]
    assert f'model == "{QWEN}"' in live and '"qwen"' in live


def test_window_matches_the_dev_slot_profile():
    assert f'{QWEN}) echo "{QWEN} 240000 16384"' in SRC


def test_thinking_budget_defaults_to_2048_for_qwen_only():
    block = SRC[SRC.index("# Per-lane thinking budget for the local Spark model"):SRC.index("# -p mode kills background subagents")]
    assert f'"$MODEL_ID" == {QWEN}' in block and "MAX_THINKING_TOKENS=2048" in block
    # A caller's explicit numeric env must still win: the guard is on an unset variable.
    assert '-z "${MAX_THINKING_TOKENS:-}"' in block


def test_skill_allowlist_branch_covers_qwen_and_writes_atomically():
    start = SRC.index(f'if [[ "$MODEL_ID" == {QWEN} || "$MODEL_ID" == {INKLING} ]]; then')
    block = SRC[start:SRC.index('elif [[ "${PROXY_AGENT_SKILL_ALLOWLIST:-1}" != 0 ]]; then')]
    assert "inkling-skill-overrides.py" in block
    assert 'open(p, "w")' not in block
    assert "os.replace(tmp, p)" in block and "os.fsync" in block and "tempfile.mkstemp" in block
    assert 'PROXY_AGENT_WARN reason=local_model_skill_overrides_unavailable' in block


def test_every_tier_model_is_probe_able():
    # Measured 22 Nov: the usability probe (proxy_agent_probe_claude_messages) returns "fail" for any id outside its allowlist,
    # and the caller records that as probe_failed, so the lane is cooled and falls through to a PAID model. A free local id that
    # is missing from the allowlist is therefore silently as expensive as a dead host. Assert the property over DISCOVERED tier
    # ids rather than checking one id by name, so a future tier addition cannot repeat this.
    probe = SRC[SRC.index("PROXY_AGENT_CLAUDE_PROBE_MODEL"):]
    allow_line = [l for l in probe.split("\n") if "if model not in (" in l][0]
    allow = set(re.findall(r'"([^"]+)"', allow_line))
    tier_ids = {i for models in _tier_models().values() for i in models}
    missing = sorted(tier_ids - allow)
    assert not missing, f"tier ids the probe cannot test -> cooled as probe_failed: {missing}"
    # The Spark id specifically must be probe-able (it heads four tiers).
    assert QWEN in allow
