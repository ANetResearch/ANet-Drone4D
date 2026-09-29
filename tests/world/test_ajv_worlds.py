"""M03-AC-002、D1-AC-01（Ajv 部分）：World Package 全部 JSON 在 Ajv 8 strict 下 0 个无效文档。

校验脚本已由 M00 采纳为 `tools/contracts/check-world.mjs`（请求 M03-to-M00 第 1 条），`make validate` 同样调用它。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from m03_common import CITIES, REPO, WORLDS, needs_worlds

NODE = shutil.which("node") or str(Path.home() / ".local" / "node" / "bin" / "node")
SCRIPT = REPO / "tools" / "contracts" / "check-world.mjs"
need_node = pytest.mark.skipif(not (Path(NODE).exists() and (REPO / "node_modules" / "ajv").exists()), reason="node 或 ajv 不可用")


def _run(dirs):
    return subprocess.run([NODE, str(SCRIPT), *map(str, dirs)], capture_output=True, text=True, cwd=REPO,
                          env=dict(os.environ), timeout=120)


@need_node
def test_tiny_world_ajv(tiny_built):
    r = _run([tiny_built["dir"]])
    assert r.returncode == 0, r.stdout + r.stderr
    assert " 0 invalid" in r.stdout


@pytest.mark.needs_data
@needs_worlds
@need_node
def test_six_worlds_ajv():
    r = _run([WORLDS / c for c in CITIES])
    assert r.returncode == 0, r.stdout[-3000:]
    assert r.stdout.strip().endswith("0 invalid")
