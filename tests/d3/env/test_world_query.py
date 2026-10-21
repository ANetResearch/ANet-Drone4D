"""Env 世界查询的类型记录与正式运行的退回拒绝（K07；A10 遗留 F-78 (c)；M26 §6.6.1；ADR-196 决策 (5) 的"正式运行"）。

- 网格查询可用时为 `MeshWorldQuery`，运行清单 `world_query.kind = mesh`；
- 网格查询打不开时：非正式运行记录 warning、退回 DSM，清单写 `kind = dsm` 与 `fallback{reason, error}`；正式运行（排名且 realistic）
  抛 `WorldQueryFallbackRefused`，不退回；注入 DSM 查询的正式运行同样拒绝；
- 退回原因区分 `no_mesh_layer`（世界包没有 `mesh.raycast`，DSM 夹具）与 `mesh_open_failed`（图层存在但打不开，如派生缓存不可写）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from awr.agentsdk.env import core
from awr.agentsdk.env.core import BenchEnv, WorldQueryFallbackRefused, open_world_query, world_query_info
from awr.agentsdk.env.spec import EnvSpec, FleetEntry

FLEET = (FleetEntry("uav-01", (-10.0, 50.0, 0.0)),)


def _broken_mesh(monkeypatch) -> None:
    import awr.world.mesh.query as mq

    def boom(*_a, **_k):
        raise PermissionError("[Errno 13] Permission denied: '.geo-cache/synth-glb'")

    monkeypatch.setattr(mq, "open_mesh_world_query", boom)


def test_mesh_query_recorded(synth_event) -> None:
    w, _ev = synth_event
    q, info = open_world_query(w, formal=True)
    assert info == {"kind": "mesh", "class": "MeshWorldQuery"} and world_query_info(q) == info


def test_fallback_warns_and_records_for_non_formal(synth_event, monkeypatch, caplog) -> None:
    w, _ev = synth_event
    _broken_mesh(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="awr.agentsdk.env.core"):
        q, info = open_world_query(w)
    assert info["kind"] == "dsm" and info["class"] == "DsmWorldQuery" and world_query_info(q)["kind"] == "dsm"
    assert info["fallback"]["reason"] == "mesh_open_failed" and info["fallback"]["error"].startswith("PermissionError")
    assert any("falling back to the DSM query" in r.getMessage() for r in caplog.records)
    assert core._open_world(w).__class__.__name__ == "DsmWorldQuery"


def test_formal_refuses_fallback(synth_event, monkeypatch) -> None:
    w, _ev = synth_event
    _broken_mesh(monkeypatch)
    with pytest.raises(WorldQueryFallbackRefused) as ei:
        open_world_query(w, formal=True)
    assert ei.value.detail["why"] == "mesh_query_unavailable" and ei.value.detail["reason"] == "mesh_open_failed"
    with pytest.raises(WorldQueryFallbackRefused):
        core._open_world(w, formal=True)


def test_no_mesh_layer_reason(tmp_path: Path) -> None:
    (tmp_path / "world.json").write_text(json.dumps({"layers": [{"id": "terrain.dsm"}, {"id": "terrain.dtm"}]}))
    assert not core._has_mesh_layer(tmp_path)
    (tmp_path / "world.json").write_text(json.dumps({"layers": [{"id": "terrain.dsm"}, {"id": "mesh.raycast"}]}))
    assert core._has_mesh_layer(tmp_path) and not core._has_mesh_layer(tmp_path / "missing")


def test_env_manifest_records_world_query(synth_event, tmp_path: Path) -> None:
    w, ev = synth_event
    env = BenchEnv(EnvSpec("wq-1", 1, w, ev, FLEET, physics="kinematic", duration_s=1.0, out_dir=tmp_path / "r"))
    obs, _infos = env.reset()
    while env.agents:
        obs, *_ = env.step({u: {"kind": "hover"} for u in obs})
    m = env.close()
    assert m["world_query"] == {"kind": "mesh", "class": "MeshWorldQuery"}
    assert json.loads((tmp_path / "r" / "manifest.json").read_text())["world_query"]["kind"] == "mesh"


def test_env_formal_rejects_injected_dsm(synth_event, monkeypatch) -> None:
    from awr.world.geometry.query import open_dsm_world_query

    w, ev = synth_event
    monkeypatch.setattr(core, "check_tape_world", lambda *_a, **_k: {"formal": True})   # 只核对世界查询这一级
    spec = EnvSpec("wq-2", 1, w, ev, FLEET, physics="realistic", ranked=True, seed_commit="c" * 64)
    with pytest.raises(WorldQueryFallbackRefused):
        BenchEnv(spec, world=open_dsm_world_query(w))
    dev = EnvSpec("wq-3", 1, w, ev, FLEET, physics="kinematic")
    assert BenchEnv(dev, world=open_dsm_world_query(w)).world_query == {"kind": "dsm", "class": "DsmWorldQuery"}
