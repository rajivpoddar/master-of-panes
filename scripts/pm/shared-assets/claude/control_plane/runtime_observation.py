"""Compatibility import for the installed PM Operator observation authority."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path("/Users/rajiv/.claude/pm-operator/current/lib")))
from pm_operator.control_plane.runtime_observation import *  # noqa: F401,F403,E402
