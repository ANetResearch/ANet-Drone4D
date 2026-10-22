"""Shared fixtures under pytest importlib mode, matching the targets test suite."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
