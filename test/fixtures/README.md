# Remote test fixtures

`python3 scripts/test-remote-fixtures.py --root /tmp/mop-fixtures -- npm run test:all`
materializes the versioned shared-asset manifest in an empty disposable Linux
container, then runs the command with a synthetic child HOME. It refuses any existing legacy
HOME. Never run it on a workstation or copy installed state into the sandbox.
Required tools: Node (20 or 22), npm, Python 3.12+, pytest, pytest-subtests, Git
with the candidate's history, sqlite3, jq and tmux. Install package dependencies
with `npm install --no-package-lock --no-audit --no-fund` (this repo has bun.lock,
not package-lock.json). No production database, credentials or config are inputs.

The synthetic HOME contains an empty SQLite obligations schema, source-managed
scripts, eight empty checkout directories and a fake token used only by sender
tests with a fake curl. The executable Codex fallback refuses execution; resolver
tests check its path, while invocation tests supply their own fake app-server.
No fixture claims that a real account is logged in or that a live service ran.

* `foreground-agent-guard.py`: snapshot of the existing external dev-slot guard
  on 2026-10-20, used only as test input for the settings writer and guard tests.
  It is not a managed install or a new live writer.
* `explore-issue-latch-guard.sh`: snapshot of the existing external audit-hook
  dependency. Tests inject this path and fake curl; no live Slack reads occur.
* `bridge/cto-*-policy.ts`: snapshots of the external bridge's pure origin and
  ingress policies on 2026-10-20. The growth tests execute these snapshots and
  the actual managed writer separately; no bridge module startup occurs.
* `legacy-four-slot-mcp.js`: synthetic negative schema witness for replacement;
  it is not represented as the bytes of an old live installation.
* `home-hook-registration.json`: synthetic sibling wrapper registration shape.
* `classifier-7589.json`: frozen expected app-classifier interface for the
  existing exact-head gate case. MoP tests the gate's consumer contract; the
  app-owned classifier is not executed or read from a workstation checkout.

Keep parent Git objects: existing causal tests deliberately execute older source,
including off-main `c33c295c46c471de233e3420523bfc2e96c9e760` (a main-only
bundle omits it). Include that exact historical object's closure in the bundle.
Do not archive only the current tree and silently skip these tests.
