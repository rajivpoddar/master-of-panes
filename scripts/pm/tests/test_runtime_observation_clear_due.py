from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from control_plane.runtime_observation import RuntimeObservationAdapter


class ClearDuePredicateTests(unittest.TestCase):
    def test_only_aged_free_idle_inactive_non_dnd_numbered_rows_are_due(self) -> None:
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        start = now - timedelta(hours=7)
        base = {
            "effective_start": start,
            "occupied": False,
            "idle": True,
            "active": False,
            "dnd": False,
            "is_pm": False,
            "now": now,
        }
        self.assertTrue(RuntimeObservationAdapter.clear_due_for(**base))
        for field, value in (("occupied", True), ("idle", False), ("active", True), ("dnd", True)):
            candidate = {**base, field: value}
            self.assertFalse(RuntimeObservationAdapter.clear_due_for(**candidate), field)
        self.assertFalse(RuntimeObservationAdapter.clear_due_for(**{**base, "is_pm": True}))
        self.assertFalse(RuntimeObservationAdapter.clear_due_for(**{**base, "dnd": None}))


if __name__ == "__main__":
    unittest.main()
