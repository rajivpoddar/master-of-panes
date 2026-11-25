import json
import os
from pathlib import Path
import subprocess
import zipfile


LAUNCHER = (Path(__file__).resolve().parents[1] / 'shared-assets/claude/scripts/remote-qa-spec.sh')


def test_sample_check_uses_the_pinned_zip_and_keeps_inputs_private(tmp_path):
    app = tmp_path / 'app checkout'
    fixtures = app / 'tests/e2e/fixtures'
    fixtures.mkdir(parents=True)
    sample, stale = 'a' * 32, 'b' * 32

    def git(*args):
        return subprocess.check_output(['git', '-C', str(app), *args], text=True).strip()

    def seed(project_id):
        with zipfile.ZipFile(fixtures / 'preview-seed.zip', 'w') as z:
            z.writestr('projects/documents.jsonl', json.dumps({'_id': project_id}) + '\n')
            z.writestr('transcripts/documents.jsonl', json.dumps({
                'projectId': project_id, 'status': 'ready', 'contentR2Key': 'synthetic/fixture',
            }) + '\n')
        git('add', '.')
        git('-c', 'user.name=QA', '-c', 'user.email=qa@example.test', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'seed')
        return git('rev-parse', 'HEAD')

    git('init', '-q')
    (fixtures / 'preview-seed-projects.sh').write_text(f'SEED_PROJECT_MEDIUM={sample}\n')
    pinned = seed(sample)
    missing = seed(stale)  # manifest unchanged but project absent from this commit's ZIP
    home = tmp_path / 'home'
    scripts = home / '.claude/scripts'
    scripts.mkdir(parents=True)
    (scripts / 'remote-qa-ownership.sh').touch()
    env = tmp_path / 'base env'
    env.write_text(f'NEXT_PUBLIC_SAMPLE_PROJECT_ID={stale}\nexport NEXT_PUBLIC_SAMPLE_PROJECT_ID="{stale}"')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'SAMPLE_PROJECT_ID': stale}))
    before = (env.read_bytes(), config.read_bytes())
    out = tmp_path / 'no-run'

    tour = 'tests/e2e/specs/qa-tests/14667-sample-editor-tour.spec.ts'
    other = 'tests/e2e/specs/qa-tests/unrelated.spec.ts'
    disposable = ['--disposable-convex']
    opt_in = ['--pair-sample-id']
    for head, spec, flags, expected, rc in [
        (pinned, tour, disposable + opt_in, 'match=y', 0),
        (missing, tour, disposable + opt_in, 'match=n', 2),
        (pinned, other, disposable + opt_in, 'pairing=skipped', 0),
        (pinned, tour, disposable, 'pairing=skipped', 0),
        (pinned, tour, opt_in, 'pairing=skipped', 0),
    ]:
        run = subprocess.run([
            'bash', str(LAUNCHER), '--slot', '5', '--branch', 'test', '--head', head,
            '--spec', spec, '--out', str(out), '--allow-seed-replace', *flags,
            '--env-file', str(env), '--config', str(config), '--dry-run', '--check-sample-ids', str(app),
        ], env=dict(os.environ, HOME=str(home), CI='1'), capture_output=True, text=True)
        assert run.returncode == rc
        assert [line for line in run.stdout.splitlines() if line.startswith('sample_ids ')] == [f'sample_ids {expected}']
        assert sample not in run.stdout + run.stderr and stale not in run.stdout + run.stderr
        assert (env.read_bytes(), config.read_bytes()) == before
        assert not out.exists()
