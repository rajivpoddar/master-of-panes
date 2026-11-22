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
