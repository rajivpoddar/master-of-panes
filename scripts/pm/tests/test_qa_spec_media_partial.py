"""Media completeness is independent of the retained Playwright verdict."""
import importlib.util
import subprocess
import pytest
from pathlib import Path

ROOT = Path(__file__).parents[1] / 'shared-assets/claude/scripts'
spec = importlib.util.spec_from_file_location('qa_spec_fetch', ROOT / 'remote-qa-spec-fetch.py')
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


def test_missing_media_returns_partial_and_preserves_small_evidence(tmp_path):
    source, snapshot, target = (tmp_path / p for p in ('source', 'snapshot', 'target'))
    source.mkdir()
    target.mkdir()
    (target / 'assertions.txt').write_text('Playwright passed\n')
    fetch.prepare(source, snapshot, 'media')
    assert fetch.publish_media(snapshot, target) == 3
    assert (target / 'assertions.txt').read_text() == 'Playwright passed\n'
    result = subprocess.run(['python3', '-I', str(ROOT / 'remote-qa-spec-fetch.py'),
                             'publish', str(snapshot), str(target)], capture_output=True, text=True)
    assert result.returncode == 3
    assert 'QA_FETCH_MEDIA=PARTIAL' in result.stdout


@pytest.mark.parametrize('failure', ['snapshot', 'transfer', 'publication', 'none'])
def test_controller_fetch_propagates_media_status(tmp_path, failure):
    text = (ROOT / 'remote-qa-spec.sh').read_text()
    function = text.split('qa_spec_fetch() {', 1)[1].split('# QA_SPEC_FETCH_END', 1)[0]
    script = '''
set -eu
run_id=qa-spec-20261113T000000Z-123
pool_lane=1; box=stub; fetch_helper=stub
out="$1"; failure="$2"; media_status=UNKNOWN
mkdir -p "$out/out"
printf 'passed\\n' >"$out/out/assertions.txt"
timeout() {
  case "$*" in
    *push-file*) return 0;;
    *fetch*) if [ "$failure" = transfer ] && [[ "$*" == *'/media/out'* ]]; then return 124; fi;;
  esac
  return 0
}
qa_spec_fetch_remote() {
  if [ "$failure" = snapshot ] && [[ "$*" == *' media' ]]; then return 124; fi
  return 0
}
python3() { [ "$failure" != publication ]; }
qa_spec_fetch() {''' + function + '''
rc=0
qa_spec_fetch || rc=$?
printf 'RESULT rc=%s media_status=%s\\n' "$rc" "$media_status"
'''
    result = subprocess.run(['bash', '-c', script, 'stub', str(tmp_path), failure],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    expected = 'rc=0 media_status=COMPLETE' if failure == 'none' else 'rc=3 media_status=PARTIAL'
    assert expected in result.stdout
    assert (tmp_path / 'out/assertions.txt').read_text() == 'passed\n'


def test_complete_media_publishes_and_preserves_small_evidence(tmp_path):
    source, snapshot, target = (tmp_path / p for p in ('source', 'snapshot', 'target'))
    source.mkdir()
    target.mkdir()
    (source / 'trace.zip').write_bytes(b'trace')
    (source / 'video.webm').write_bytes(b'video')
    (target / 'legs.tsv').write_text('GREEN\t0\n')
    fetch.prepare(source, snapshot, 'media')
    assert fetch.publish_media(snapshot, target) == 0
    assert (target / 'video.webm').read_bytes() == b'video'
    assert (target / 'legs.tsv').read_text() == 'GREEN\t0\n'
