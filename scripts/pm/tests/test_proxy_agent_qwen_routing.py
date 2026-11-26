"""Spark proxy routing: Ornith heads the local tiers, with Qwen retained as a compatible alias."""
import re
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "shared-assets/claude/scripts/proxy-agent.sh"
SRC = SCRIPT.read_text()

ORNITH = "ornith-1.5-35b-a3b"
QWEN = "qwen3.8-flash-next"
INKLING = "inkling-2-mini"
OPENCODE_IDS = ("deepseek-v4.1-flash", "hy4-flash-free")
CODEX_LADDER = ("gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol")
SPARK_TIERS = ("LIGHT", "MID", "BUILD1", "PROOF1")


def _tier_models():
    tiers = {}
    for tier, models in re.findall(r'^\s+([A-Z0-9]+)\) models="([^"]*)";;', SRC, re.M):
        tiers[tier] = models.split()
    return tiers


def test_ornith_heads_local_tiers_and_legacy_ids_stay_out_of_the_lists():
    tiers = _tier_models()
    for tier in SPARK_TIERS:
        assert tiers[tier][0] == ORNITH, tier
        assert tiers[tier].count(ORNITH) == 1, tier
    for tier, models in tiers.items():
        assert all(legacy not in models for legacy in (QWEN, INKLING)), tier


def test_opencode_ids_come_before_the_codex_ladder_in_the_local_tiers():
    tiers = _tier_models()
    for tier in SPARK_TIERS:
        models = tiers[tier]
        assert ORNITH in models
        first_opencode = min(models.index(m) for m in OPENCODE_IDS if m in models)
        assert models.index(ORNITH) < first_opencode, tier
        for codex in CODEX_LADDER:
            assert first_opencode < models.index(codex), tier
        # The ladder itself keeps its luna -> terra -> sol order.
        ladder = [m for m in models if m in CODEX_LADDER]
        assert ladder == list(CODEX_LADDER), tier


def test_glm_keeps_the_livesynced_tier_positions():
    for tier, models in _tier_models().items():
        if tier in {"MID", "BUILD1", "PROOF1"}:
            assert models.index("deepseek-v4.1-flash") < models.index("glm-5.3") < models.index("hy4-flash-free")
        else:
            assert "glm-5.3" not in models, tier


def test_judgment_lanes_exclude_the_local_model():
    guard = SRC[SRC.index("if proxy_agent_is_judgment_lane; then"):SRC.index('for id in $models; do', SRC.index("if proxy_agent_is_judgment_lane; then"))]
    assert all(model in guard for model in (ORNITH, QWEN, INKLING, "gpt-6-luna"))


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


def test_health_gate_covers_both_spark_aliases_with_three_second_budget():
    gate = SRC[SRC.index("Local Spark health gate"):SRC.index("if proxy_agent_is_emergency_muse \"$id\"; then")]
    assert all(f'"$id" == {model}' in gate for model in (ORNITH, QWEN, INKLING))
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


def test_host_down_retries_once_before_routing_the_lane_to_a_paid_model():
    # A single dropped LAN probe must not demote the lane to a paid model. The fetch retries once; only two empty answers count as host_down,
    # and the empty check is on the BODY (a failed curl can leave `_spark_ids` set but empty under some sh semantics).
    gate = SRC[SRC.index("Local Spark health gate"):SRC.index("if proxy_agent_is_emergency_muse \"$id\"; then")]
    assert gate.count('curl -s -m 3 --fail "$_spark_url"') == 2
    assert '[[ -z "$_spark_ids" ]]' in gate
    assert 'if ! _spark_ids=' not in gate  # exit-status-only check would miss a 200 with an empty body
    assert "twice" in gate


def test_spark_aliases_share_a_cap_outside_the_opencode_pool():
    cap = SRC[SRC.index("proxy_agent_pool_blocked() {"):SRC.index("# Final-primary OpenCode guard")]
    assert f'{ORNITH}|{QWEN}) pool=spark; cap="${{SPARK_PROXY_CAP:-${{QWEN_PROXY_CAP:-6}}}}";;' in cap
    assert '"$pool" == spark' in cap  # still skip when the process count is unavailable
    assert f'{INKLING}) pool=inkling;' in cap
    live = SRC[SRC.index("proxy_agent_pool_live() {"):SRC.index("proxy_agent_pool_blocked() {")]
    assert f'"spark" if model in ("{ORNITH}", "{QWEN}")' in live


def test_both_aliases_keep_the_local_window():
    assert f'{ORNITH}|{QWEN}) echo "$1 240000 16384"' in SRC


def test_thinking_budget_defaults_to_2048_for_both_aliases():
    block = SRC[SRC.index("# Per-lane thinking budget for the local Spark model"):SRC.index("# -p mode kills background subagents")]
    assert all(f'"$MODEL_ID" == {model}' in block for model in (ORNITH, QWEN))
    assert "MAX_THINKING_TOKENS=2048" in block
    assert '-z "${MAX_THINKING_TOKENS:-}"' in block


def test_spark_has_no_separate_skill_override_generator():
    assert not re.search(r'^\s*if .*python3.*inkling-skill-overrides\.py', SRC, re.M)


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
    assert {ORNITH, QWEN} <= allow
