"""tests/georef (M02): perf-marked cases run only when selected with `-m perf` (ADR-033 performance protocol), so
`make test-contracts` (which runs this directory without a marker filter) never executes them."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    if "perf" in (config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="performance case: run with -m perf under the performance protocol (ADR-033)")
    for it in items:
        if "perf" in it.keywords and "tests/georef/" in str(it.fspath).replace("\\", "/"):
            it.add_marker(skip)
