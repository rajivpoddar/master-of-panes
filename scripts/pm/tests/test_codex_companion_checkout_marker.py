from pathlib import Path


def test_companion_binds_marker_to_review_checkout():
    companion = Path(__file__).parents[1] / "shared-assets/claude/skills/codex-review-companion/codex-review-companion.mjs"
    source = companion.read_text()
    marker_writer = source.split("function writeMarker(", 1)[1].split("function ", 1)[0]
    assert "`CHECKOUT: ${fs.realpathSync(args.repoRoot)}`" in marker_writer
    assert "`HEAD_SHA: ${args._currentHead}`" in marker_writer
