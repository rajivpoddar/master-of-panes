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
    trap = source.index('trap _release_unsent_claim EXIT')
    compose = source.index('sakshi-heartbeat.sh compose --mode 1h --sample-usage-claim "$CLAIM"')
    send_started = source.index('SEND_STARTED=1')
    send = source.index('slack-send.sh -c C0ALZJHGE49 -f')
    assert claim < already < uncertain < trap < compose < send_started < send
    assert source.count('sakshi-heartbeat.sh compose') == 1
    assert 'rmdir "$CLAIM"' in source and 'if [ "$SEND_STARTED" = 0 ]' in source
    # Posted/uncertain buckets exit before compose can run.
    assert source[already:compose].count('exit 0') >= 1 and source[uncertain:compose].count('exit 1') >= 1


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
