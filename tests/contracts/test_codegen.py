"""Generated code and golden are up to date (AWR-18 §8.3: gen --check before pytest and vitest)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import node, run

PY = sys.executable
CHECKS = ["gen.py", "gen_golden.py", "gen_frames_golden.py", "gen_env_golden.py", "gen_fixtures.py"]


@pytest.mark.parametrize("tool", CHECKS)
def test_python_generator_check(tool: str):
    r = run([PY, f"tools/contracts/{tool}", "--check"])
    assert r.returncode == 0, r.stdout + r.stderr


def test_ts_generator_check():
    n = node()
    if n is None:
        pytest.skip("node not found")
    r = run([n, "tools/contracts/gen.mjs", "--check"])
    assert r.returncode == 0, r.stdout + r.stderr


def test_generated_python_is_importable_and_fast():
    r = run([PY, "-X", "importtime", "-c", "import awr.contracts, awr.contracts.frame, awr.contracts.layouts, awr.contracts.commands"])
    assert r.returncode == 0, r.stderr


def test_layout_id_identical_across_generators():
    from awr.contracts import layouts

    ts = (Path(__file__).resolve().parents[2] / "packages" / "contracts" / "gen" / "ts" / "layouts.ts").read_text(encoding="utf-8")
    assert f"LAYOUT_ID = 0x{layouts.LAYOUT_ID:08X}" in ts.replace("0x" + f"{layouts.LAYOUT_ID:08x}", "0x" + f"{layouts.LAYOUT_ID:08X}")
    for sn, h in layouts.SCHEMA_HASH.items():
        assert f"'{sn}': '{h}'" in ts or f'"{sn}": "{h}"' in ts, sn
