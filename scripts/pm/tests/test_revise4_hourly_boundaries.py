"""CTO REVISE 1794583885.992979: named 3h overflow and real Asks header."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).parents[1] / 'shared-assets/claude/scripts'

def test_real_asks_header_is_accepted():
    source = (ROOT / 'pm-hourly-post.sh').read_text()
    assert "grep -Eq '^(<@[A-Z0-9]+> *)?\\*Hourly'" in source
    assert re.match(r'^(<@[A-Z0-9]+> *)?\*Hourly', '<@U0BNFGX2UAX> *Hourly 1h*')

def test_usage_sampler_only_after_bucket_claim():
    """CTO REVISE 1794756599.261169: the claim precedes compose; compose gets the claim path; the claim is released only unsent."""
    source = (ROOT / 'pm-hourly-post.sh').read_text()
    claim = source.index('mkdir "$CLAIM"')
    already = source.index('HOURLY_ALREADY_POSTED')
    uncertain = source.index('HOURLY_POST_UNCERTAIN bucket=$BUCKET existing claim')
    trap = source.index('trap _settle_unsent_claim EXIT')
    compose = source.index('sakshi-heartbeat.sh compose --mode 1h --sample-usage-claim "$CLAIM"')
    send_started = source.index('SEND_STARTED=1')
    send = source.index('slack-send.sh -c C0ALZJHGE49 -f')
    assert claim < already < uncertain < trap < compose < send_started < send
    assert source.count('sakshi-heartbeat.sh compose') == 1
    assert 'rmdir "$CLAIM"' in source and 'if [ "$SEND_STARTED" = 0 ]' in source
    assert '_release_unsent_claim' not in source
    # Posted/uncertain buckets exit before compose can run.
    assert source[already:compose].count('exit 0') >= 1 and source[uncertain:compose].count('exit 1') >= 1


def test_sampling_custody_marker_retains_claim_once_quota_io_may_have_begun():
    """CTO REVISE 1794758174.846249 (thread 1794753780.694859): the sampler writes nothing to the claim, so the wrapper
    writes sampling_started BEFORE compose and the EXIT cleanup removes only a claim that provably never reached sampling.
    Static path trace (no run): effect-then-compose-failure, signal mid-sample, normal success."""
    source = (ROOT / 'pm-hourly-post.sh').read_text()
    trap = source.index('trap _settle_unsent_claim EXIT')
    mark_flag = source.index('\nSAMPLING_MARKED=1\n')
    marker_write = source.index("printf 'ts=%s pid=%s bucket=%s\\n'")
    compose = source.index('sakshi-heartbeat.sh compose --mode 1h --sample-usage-claim "$CLAIM"')
    send_started = source.index('SEND_STARTED=1')
    # The marker (and the flag the trap reads) are in place before compose, which is the only route to the sampler.
    assert trap < mark_flag < marker_write < compose < send_started
    assert 'sampling_started' in source[marker_write:marker_write + 200]
    # A failed marker write rolls the flag back and never launches compose.
    write_fail = source[marker_write:compose]
    assert 'SAMPLING_MARKED=0' in write_fail and 'exit 1' in write_fail and 'sakshi-heartbeat.sh' not in write_fail

    # EXIT cleanup: marked -> retain + record why; unmarked -> rmdir (which only succeeds on an EMPTY claim).
    start = source.index('_settle_unsent_claim() {')
    body = source[start:source.index('_claim_note()')]
    assert body.index('if [ "$SEND_STARTED" = 0 ]') < body.index('if [ "$SAMPLING_MARKED" = 1 ]') \
        < body.index('sampling_retained') < body.index('else') < body.index('rmdir "$CLAIM"')
    assert 'local rc=$?' in body and 'exit "$rc"' in body  # exit code preserved
    assert source.count('rmdir "$CLAIM"') == 1  # the only claim removal is inside the settle function

    # Signals mid-sample: recorded, and routed through the same EXIT cleanup (which retains once marked).
    for sig in ('INT', 'TERM', 'HUP'):
        assert f"trap 'EXIT_REASON=signal_{sig}; exit 130' {sig}" in source
    assert source.index("trap 'EXIT_REASON=signal_INT; exit 130' INT") < marker_write

    # The marker is only rolled back on the two provable no-sample paths: failed marker write, collectors-absent shim exit.
    assert source.count('rm -f -- "$CLAIM/sampling_started"') == 2
    sentinel = source.index("grep -q '^HEARTBEAT_COLLECTORS_ABSENT: '")
    assert compose < sentinel < send_started
    assert "grep -c . /tmp/heartbeat-1h-compose.err" in source[sentinel - 200:sentinel]

    # Normal success: SEND_STARTED=1 precedes the send, so the cleanup never touches the claim afterwards.
    assert source.index('SEND_STARTED=1') < source.index('slack-send.sh -c C0ALZJHGE49 -f')
    assert 'rmdir' not in source[send_started:]

    # Next-invocation advice: a marked claim is reconciliation, never deletion; the bare-claim advice stays for unmarked ones.
    assert 'RECONCILIATION, not deletion' in source
    uncertain_marked = source.index('[ -e "$CLAIM/sampling_started" ] || [ -e "$CLAIM/sampling_retained" ]')
    uncertain_bare = source.index('a claim with NO sampling_started marker')
    assert source.index('HOURLY_ALREADY_POSTED') < uncertain_marked < uncertain_bare < trap


def test_usage_sampler_flag_is_the_only_way_to_enable_it():
    source = (ROOT / 'pm/heartbeat/heartbeat-compose.py').read_text()
    assert '--sample-usage-claim' in source
    assert source.count('hourly_usage_job(') == 2
    assert 'if usage_sample_claim_ok(getattr(args, "sample_usage_claim", None)):' in source
    assert '"usage": hourly_usage_job' not in source


def test_long_3h_fold_names_every_route():
    spec = importlib.util.spec_from_file_location('revise4_composer', ROOT / 'pm/heartbeat/heartbeat-compose.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = [f'route_{i:03d}_' + 'x' * 90 for i in range(80)]
    result = module.compact_report(['*Routes:*'] + ['• ' + n + ' — 1 ev; owner PM' for n in names], 3, cadence='3h')
    result = module.cap_lines(result, module.MAX_LINE_CHARS - module.LINE_NOTE_RESERVE)
    assert all(n in result for n in names)
    assert all(len(line) <= module.MAX_LINE_CHARS - module.LINE_NOTE_RESERVE for line in result.splitlines())

def test_overlap_has_forced_kill_backstop():
    source = (ROOT / 'ci/pm-admit-ci.sh').read_text()
    assert 'timeout --kill-after=5 "$OVERLAP_TIMEOUT" python3 -I' in source
