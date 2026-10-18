"""Env 测试夹具：合成世界（tests/d3/world/synthworld）上编译的合成交接事件（tests/d3/events/test_compile.EVENT），
补充计分标注与简报；物理取运动学开发档。

依赖跳过按夹具与模块落点（D3 封板 I01），不再整目录跳过：缺 open3d / zstandard 时仅跳过使用 `synth_event` 的用例；
`MODULE_DEPS` 登记不经夹具而直接用到 D3 依赖的模块或用例，缺失时只跳过匹配项。本目录与复用本夹具的 tests/d3/eval 其余用例
在 D2 环境（.venv，不含 D3 依赖）下照常运行。跳过原因沿用 `pytest.importorskip` 的措辞，统一入口按 dep 层计数。"""

from __future__ import annotations

import copy
import fnmatch
import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "world"))
sys.path.insert(0, str(HERE.parents[0] / "events"))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "sdk" / "drone4d" / "src"))

# 模块（或 `模块::用例` 通配）→ 直接用到的 D3 依赖（顶层模块名）
MODULE_DEPS: dict[str, tuple[str, ...]] = {
    "test_obs_audit.py": ("PIL",),                                           # JPEG 夹具帧（_jpeg）
    "test_tolerance.py::test_frame_dump_from_agent_observation": ("PIL",),
}


def _missing(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is None
    except (ImportError, ValueError):
        return True


def pytest_runtest_setup(item: pytest.Item) -> None:
    """按 `MODULE_DEPS` 跳过缺少直接依赖的模块或用例（只对本目录的用例生效：runtest 钩子按 conftest 目录调用）。"""
    path = Path(str(item.path))
    if path.parent != HERE:
        return
    key = f"{path.name}::{item.name}"
    for pat, deps in MODULE_DEPS.items():
        if pat == path.name or fnmatch.fnmatchcase(key, pat):
            for d in deps:
                if _missing(d):
                    pytest.skip(f"could not import '{d}': No module named '{d}'")


@pytest.fixture(scope="session")
def synth_event(tmp_path_factory):
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    from synthworld import build_synth_world
    from test_compile import EVENT, OVERLAY

    from awr.sim.events.compile import CompileOptions, compile_event

    tmp = tmp_path_factory.mktemp("env")
    mp = pytest.MonkeyPatch()
    w = build_synth_world(tmp, mp)
    ev = copy.deepcopy(EVENT)
    ev["annotations"] = [
        {"id": "meet_ab", "type": "proximity_encounter", "participants": ["person_a", "person_b"], "window": [5, 25]},
        {"id": "board_bc", "type": "board_vehicle", "participants": ["person_b", "vehicle_c"], "window": [70, 90]},
        {"id": "enter_c", "type": "enter_building", "participants": ["person_c"], "window": [35, 45], "params": {"anchor": "ev_door"}}]
    ev["brief"] = {"public_anchors": ["ev_plaza", "ev_door"],
                   "targets": [{"actor": "person_a", "description_en": "Blue top", "description_zh": "蓝色上衣"}]}
    d = tmp / "ev"
    d.mkdir()
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    (d / "event.json").write_text(json.dumps(ev))
    r = compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache"))
    assert r.status == "compiled", r.as_dict()
    yield w, d / "v1"
    mp.undo()
