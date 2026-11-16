"""Source-inspection guard: the hourly CTO review reminder anchors on the posted parent ts."""
from pathlib import Path

WRAPPER = (Path(__file__).resolve().parents[1] / "shared-assets" / "claude" / "scripts" / "pm-hourly-post.sh")


def test_cto_reminder_is_anchored_to_posted_parent_ts():
    text = WRAPPER.read_text()
    # post_at = ceil(posted ts) + 300 s, computed from the receipt-confirmed $TS.
    assert 'math.ceil(float(sys.argv[1]))+300' in text
    assert '"$TS" 2>/dev/null)' in text
    # The old wrapper-exit anchor must be gone.
    assert 'd.datetime.now(d.timezone.utc)+d.timedelta(minutes=5)' not in text
    # Scheduled only through the existing alerts scheduler, with --at derived from the anchor.
    assert '--at "$_AT"' in text
    assert text.count('alerts-schedule.py') == 1
