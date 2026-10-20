"""BenchEnv 打开磁带时的世界版本与 NavMesh 校验（M26 §6.6.1；ADR-196 决策 (5)；原因码 615；S21、W12 遗留）。

- 所有运行：磁带头 `world.content_version` 与所加载世界包 `contentVersion` 不符即 615，构造期抛出、不创建运行目录、不启动物理档；
- 正式运行（排名 + `realistic`）：拒绝栅格后端编译的磁带（`generator_version` 以 `-grid` 结尾）与 NavMesh 哈希和世界图层不符的
  磁带；出处或图层缺失同为 615；开发档（kinematic）与 Fast 档的排名运行不受第 2 级约束；
- 合成世界（tests/d3/world/synthworld）含 NavMesh 图层，有 Node 时其事件以 Detour 编译、无 Node 时退回栅格：第 2 级的各分支以
  复制的事件目录（只改 `tape.json` 的出处，不改磁带字节）与只含清单的世界目录构造，不依赖本机是否有 Node。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from awr.agentsdk.env.core import BenchEnv
from awr.agentsdk.env.spec import EnvSpec, FleetEntry
from awr.agentsdk.env.tapecheck import check_tape_world, is_formal, world_navmesh_sha256

FLEET = (FleetEntry("uav-01", (-10.0, 50.0, 0.0)), FleetEntry("uav-02", (10.0, 50.0, 0.0)))


def _spec(w: Path, ev: Path, out: Path | None, **kw) -> EnvSpec:
    return EnvSpec("t-tc", 5, w, ev, FLEET, duration_s=2.0, out_dir=out, **{"physics": "kinematic", **kw})


def _event_copy(ev: Path, dst: Path, generator_version: str | None) -> Path:
    """复制事件版本目录，改写 `tape.json` 的 `provenance.generator_version`（None 时删除出处）；磁带字节不变。"""
    d = dst / ev.parent.name / ev.name
    shutil.copytree(ev, d)
    m = json.loads((d / "tape.json").read_text())
    if generator_version is None:
        m.pop("provenance", None)
    else:
        m["provenance"]["generator_version"] = generator_version
    (d / "tape.json").write_text(json.dumps(m))
    return d


def _world_stub(dst: Path, content_version: str, nav: dict[str, str] | None) -> Path:
    """只含 `world.json`（与可选的 `detour-navmesh@1` 图层清单）的世界目录：校验发生在打开几何之前。"""
    dst.mkdir(parents=True)
    layers = []
    for prof, sha in (nav or {}).items():
        (dst / "geometry" / "nav").mkdir(parents=True, exist_ok=True)
        (dst / "geometry" / "nav" / f"{prof}.json").write_text(json.dumps({"profile": prof, "bin": {"href": f"{prof}.bin", "sha256": sha}}))
        layers.append({"id": f"nav.{prof}", "type": "navmesh", "format": "detour-navmesh@1", "href": f"geometry/nav/{prof}.json"})
    (dst / "world.json").write_text(json.dumps({"id": dst.name, "contentVersion": content_version, "layers": layers}))
    return dst


def _tape_world(ev: Path) -> dict:
    return json.loads((ev / "tape.json").read_text())["header"]["world"]


def test_is_formal_only_ranked_realistic(synth_event) -> None:
    w, ev = synth_event
    assert is_formal(_spec(w, ev, None, ranked=True, physics="realistic", seed_commit="hmac:00"))
    assert not is_formal(_spec(w, ev, None, ranked=True, seed_commit="hmac:00"))                # 开发档（kinematic）
    assert not is_formal(_spec(w, ev, None, ranked=True, physics="fast", seed_commit="hmac:00"))
    assert not is_formal(_spec(w, ev, None, physics="realistic"))                                 # 不排名


def test_matching_world_passes_and_is_recorded(synth_event, tmp_path: Path) -> None:
    w, ev = synth_event
    cv = json.loads((Path(w) / "world.json").read_text())["contentVersion"]
    assert _tape_world(ev)["content_version"] == cv
    env = BenchEnv(_spec(w, ev, tmp_path / "run"))
    assert env.tape_check == {"content_version": cv, "formal": False}
    env.reset()
    while env.agents:
        env.step({})
    assert env.close()["tape_check"] == {"content_version": cv, "formal": False}
    assert json.loads((tmp_path / "run" / "manifest.json").read_text())["tape_check"]["content_version"] == cv


@pytest.mark.parametrize("formal", [False, True])
def test_world_version_mismatch_raises_615_before_run_dir(synth_event, tmp_path: Path, formal: bool) -> None:
    """S21 发现：crop-handoff-001/v1（为 efad268468de 编译）在 7d3429a04895 上运行未报错；现构造期即 615。"""
    from awr.sim.events.errors import EventError

    _w, ev = synth_event
    other = _world_stub(tmp_path / "w-other", "0123456789ab", None)
    kw = {"ranked": True, "physics": "realistic", "seed_commit": "hmac:00"} if formal else {}
    with pytest.raises(EventError) as ei:
        BenchEnv(_spec(other, ev, tmp_path / "run", **kw))
    assert ei.value.code == 615 and ei.value.reason == "TAPE_WORLD_MISMATCH"
    assert ei.value.detail["loaded"]["content_version"] == "0123456789ab"
    assert ei.value.detail["tape"]["content_version"] == _tape_world(ev)["content_version"]
    assert not (tmp_path / "run").exists()


def test_formal_run_rejects_grid_tape(synth_event, tmp_path: Path) -> None:
    """栅格后端编译的磁带（`-grid`，无 Node 时合成事件即如此）：开发档排名运行照常构造，正式运行 615 `grid_generator`，
    不创建运行目录、不启动物理档。合成事件本身（本机有 Node 时为 Detour 编译）在合成世界上通过正式运行的两级校验。"""
    from awr.sim.events.errors import EventError

    w, ev = synth_event
    grid = _event_copy(ev, tmp_path / "ev-grid", "0.2.0-grid")
    BenchEnv(_spec(w, grid, None, ranked=True, seed_commit="hmac:00"))
    with pytest.raises(EventError) as ei:
        BenchEnv(_spec(w, grid, tmp_path / "run", ranked=True, physics="realistic", seed_commit="hmac:00"))
    assert ei.value.code == 615 and ei.value.detail == {"why": "grid_generator", "generator_version": "0.2.0-grid"}
    assert not (tmp_path / "run").exists()
    gv = json.loads((ev / "tape.json").read_text())["provenance"]["generator_version"]
    if gv.endswith("-detour"):
        env = BenchEnv(_spec(w, ev, None, ranked=True, physics="realistic", seed_commit="hmac:00"))
        assert env.tape_check["formal"] and env.tape_check["generator_version"] == gv
        assert env.tape_check["navmesh"] == world_navmesh_sha256(w) == _tape_world(ev)["navmesh_sha256"]


def test_formal_navmesh_checks(synth_event, tmp_path: Path) -> None:
    """正式运行的 NavMesh 判据：图层哈希相同通过；不同、缺图层、缺出处与世界清单缺失均为 615（各自的 `why`）。"""
    from awr.sim.events.errors import EventError
    from awr.sim.events.tape import open_tape

    _w, ev = synth_event
    tw = _tape_world(ev)
    cv, nav = tw["content_version"], tw["navmesh_sha256"]
    assert set(nav) == {"person", "vehicle"}
    detour = open_tape(_event_copy(ev, tmp_path / "ev-detour", "0.2.0-detour"))

    ok = _world_stub(tmp_path / "w-ok", cv, nav)
    assert world_navmesh_sha256(ok) == nav
    got = check_tape_world(detour, ok, formal=True)
    assert got == {"content_version": cv, "formal": True, "generator_version": "0.2.0-detour", "navmesh": nav}

    def why(world: Path, tape=detour) -> dict:
        with pytest.raises(EventError) as ei:
            check_tape_world(tape, world, formal=True)
        assert ei.value.code == 615
        return ei.value.detail

    bad = _world_stub(tmp_path / "w-bad", cv, {**nav, "vehicle": "f" * 64})
    assert why(bad) == {"why": "navmesh_mismatch", "profile": "vehicle", "tape": nav["vehicle"], "loaded": "f" * 64}
    assert why(_world_stub(tmp_path / "w-person", cv, {"person": nav["person"]})) == {
        "why": "navmesh_layer_missing", "profile": "vehicle", "loaded": ["person"]}
    no_prov = open_tape(_event_copy(ev, tmp_path / "ev-noprov", None))
    assert why(ok, no_prov) == {"why": "provenance_missing"}
    empty = tmp_path / "w-empty"
    empty.mkdir()
    assert why(empty)["why"] == "world_manifest_missing"
    assert check_tape_world(detour, empty, formal=False) == {"content_version": None, "formal": False}   # 开发夹具（无 world.json）
    assert check_tape_world(detour, bad, formal=False) == {"content_version": cv, "formal": False}       # 第 2 级只约束正式运行
