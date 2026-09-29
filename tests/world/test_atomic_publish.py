"""M03-AC-020：原子发布与安全。任一阶段 kill -9 后 `worlds/<id>` 始终是完整旧包；残留 staging 下次构建清理；
同城并发构建后者退出码 3；`renameat2` 不可用时走两次 rename 并由启动恢复补齐；写路径拒绝 `..`。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from tinyworld_m03 import TinyAdapter

from awr.world.ingest.types import StageContext
from awr.world.package import publish as P
from awr.world.package.build import build_world
from awr.world.package.cli import main as worldpkg
from awr.world.package.params import BuildParams
from awr.world.package.validate import validate_world

HERE = Path(__file__).resolve().parent


@pytest.fixture
def worlds(tiny_built, tmp_path):
    w = tmp_path / "worlds"
    w.mkdir()
    shutil.copytree(tiny_built["dir"], w / "tiny")
    return w


def _cv(d: Path) -> str:
    return json.loads((d / "world.json").read_text())["contentVersion"]


def test_republish_exchanges_and_trashes(tiny_built, worlds, repo_env):
    old = _cv(worlds / "tiny")
    res = build_world(TinyAdapter(tiny_built["ply"], tiny_built["sha"]), worlds, params=BuildParams(seed=3), ctx=StageContext(quiet=True))
    assert res.exit_code == 0 and res.published and _cv(worlds / "tiny") == res.content_version != old
    trash = list((worlds / ".trash").glob("tiny-*"))
    assert len(trash) == 1 and _cv(trash[0]) == old
    st = json.loads((worlds / ".status" / "tiny.json").read_text())
    assert st["status"] == "ready" and st["content_version"] == res.content_version
    assert not list((worlds / ".staging").glob("tiny-*"))


def test_lock_busy_is_exit_3(tiny_built, worlds, repo_env):
    with P.Publisher(worlds, "tiny").lock():
        res = build_world(TinyAdapter(tiny_built["ply"], tiny_built["sha"]), worlds, ctx=StageContext(quiet=True))
    assert res.exit_code == 3 and res.error_code == "BUILD_IN_PROGRESS"
    assert validate_world(worlds / "tiny").ok


def test_rename_fallback_without_renameat2(tiny_built, worlds, repo_env, monkeypatch):
    monkeypatch.setattr(P, "renameat2_exchange", lambda a, b: False)
    res = build_world(TinyAdapter(tiny_built["ply"], tiny_built["sha"]), worlds, params=BuildParams(seed=4), ctx=StageContext(quiet=True))
    assert res.exit_code == 0 and _cv(worlds / "tiny") == res.content_version
    assert len(list((worlds / ".trash").glob("tiny-*"))) == 1


def test_recover_restores_from_trash(worlds):
    trash = worlds / ".trash"
    trash.mkdir(exist_ok=True)
    os.rename(worlds / "tiny", trash / "tiny-20260928T000000000000000")   # 两次 rename 之间崩溃的现场
    (worlds / ".staging").mkdir(exist_ok=True)
    (worlds / ".staging" / "tiny-dead").mkdir()
    pub = P.Publisher(worlds, "tiny")
    with pub.lock():
        acts = pub.recover(shallow_ok=lambda p: validate_world(p, staging=True).ok)
    assert (worlds / "tiny" / "world.json").exists() and not (worlds / ".staging" / "tiny-dead").exists()
    assert any(a.startswith("restored") for a in acts)


def test_renameat2_exchange_real(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x").write_text("a")
    (b / "x").write_text("b")
    if not P.renameat2_exchange(a, b):
        pytest.skip("renameat2 not supported on this filesystem")
    assert (a / "x").read_text() == "b" and (b / "x").read_text() == "a"


KILL_SCRIPT = r"""
import os, signal, sys
sys.path.insert(0, {tests!r})
from tinyworld_m03 import TinyAdapter
from awr.world.package import build as B
from awr.world.package import publish as P
stage = sys.argv[1]
def die(*a, **k):
    os.kill(os.getpid(), signal.SIGKILL)
if stage == "publish_mid":
    def half(self, stg):
        os.rename(self.worlds / self.wid, self.worlds / ".trash" / (self.wid + "-20260928T000000000000001"))
        die()
    P.Publisher.publish = half
else:
    setattr(B.BuildPipeline, stage, die)
B.build_world(TinyAdapter({ply!r}, {sha!r}), {worlds!r}, params=B.BuildParams(seed=5))
"""


@pytest.mark.parametrize("stage", ["run_ingest", "run_tile", "run_package", "publish_mid"])
def test_kill9_keeps_old_package(tiny_built, worlds, repo_env, stage):
    old = _cv(worlds / "tiny")
    code = KILL_SCRIPT.format(tests=str(HERE), ply=str(tiny_built["ply"]), sha=tiny_built["sha"], worlds=str(worlds))
    r = subprocess.run([sys.executable, "-c", code, stage], env=dict(os.environ), capture_output=True, text=True, timeout=300)
    assert r.returncode == -9, r.stderr[-2000:]
    if stage == "publish_mid":
        assert not (worlds / "tiny").exists()                      # 两次 rename 之间：由启动恢复补齐
    else:
        assert _cv(worlds / "tiny") == old and validate_world(worlds / "tiny").ok
    res = build_world(TinyAdapter(tiny_built["ply"], tiny_built["sha"]), worlds, ctx=StageContext(quiet=True))
    assert res.exit_code == 0 and validate_world(worlds / "tiny").ok
    assert not [p for p in (worlds / ".staging").iterdir() if p.name.startswith("tiny-")]


def test_cli_rejects_dotdot_paths(capsys):
    assert worldpkg(["build", "--worlds", "../x"]) == 3
    assert worldpkg(["validate", "no/such/dir"]) == 3
    capsys.readouterr()
