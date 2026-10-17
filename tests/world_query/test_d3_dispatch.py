"""D3 扩展点 7（AWR-05 §12.3，M22-FR-047）：`open_world_query` 只在 `AWR_D3_ENABLED = 1` 且有网格几何图层时以
`importlib` 分派到 `awr.world.mesh.query:open_mesh_world_query`；开关关闭或没有该图层时与 `open_dsm_world_query` 相同。"""

from __future__ import annotations

import json
import shutil
import sys
import types

import pytest

from awr.world.geometry import DsmWorldQuery, open_dsm_world_query, open_world_query
from awr.world.geometry.query import has_mesh_geometry

MESH = {"id": "geometry.mesh", "type": "mesh", "role": "geometry", "format": "awr-trimesh@1",
        "href": "geometry/mesh/mesh.trimesh", "status": "ready"}


@pytest.fixture
def mesh_world(fake_dir, tmp_path):
    """合成小世界的副本，world.json 追加网格几何图层（只用于判据；文件本身不需要存在）。"""
    wd = tmp_path / "tiny"
    shutil.copytree(fake_dir / "tiny", wd)
    w = json.loads((wd / "world.json").read_text(encoding="utf-8"))
    w["layers"].append(dict(MESH))
    (wd / "world.json").write_text(json.dumps(w), encoding="utf-8")
    return wd


@pytest.fixture
def factory(monkeypatch):
    calls: list[tuple] = []

    class _Mesh(DsmWorldQuery):
        pass

    def open_mesh_world_query(world_dir, cache_root=None, params=None, allow_derive=None):
        calls.append((world_dir, cache_root, params, allow_derive))
        return _Mesh.__new__(_Mesh)

    mod = types.ModuleType("awr.world.mesh.query")
    mod.open_mesh_world_query = open_mesh_world_query
    monkeypatch.setitem(sys.modules, "awr.world.mesh.query", mod)
    return calls, _Mesh


def test_flag_off_never_dispatches(mesh_world, fake_dir, factory, monkeypatch):
    monkeypatch.delenv("AWR_D3_ENABLED", raising=False)
    calls, _ = factory
    q = open_world_query(mesh_world, fake_dir / ".geo-cache")
    assert type(q) is DsmWorldQuery and calls == []


def test_flag_on_without_mesh_layer_is_dsm(fake_dir, factory, monkeypatch):
    monkeypatch.setenv("AWR_D3_ENABLED", "1")
    calls, _ = factory
    a = open_world_query(fake_dir / "tiny", fake_dir / ".geo-cache")
    b = open_dsm_world_query(fake_dir / "tiny", fake_dir / ".geo-cache")
    assert type(a) is DsmWorldQuery and calls == [] and a.derive_sha8 == b.derive_sha8


def test_flag_on_with_mesh_layer_loads_the_factory_by_name(mesh_world, fake_dir, factory, monkeypatch):
    monkeypatch.setenv("AWR_D3_ENABLED", "1")
    calls, Mesh = factory
    q = open_world_query(mesh_world, fake_dir / ".geo-cache", None, False)
    assert isinstance(q, Mesh) and isinstance(q, DsmWorldQuery)
    assert calls == [(mesh_world, fake_dir / ".geo-cache", None, False)]


def test_criterion_needs_role_type_and_format():
    assert has_mesh_geometry({"layers": [MESH]})
    for k, v in (("role", "visual"), ("type", "terrain"), ("format", "glb")):
        assert not has_mesh_geometry({"layers": [{**MESH, k: v}]})
    assert not has_mesh_geometry({})


def test_d2_packages_do_not_import_d3_packages():
    """import 方向：geometry 包只以字符串引用 D3 工厂。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "python/awr/world/geometry"
    for p in src.glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "import awr.world.mesh" not in text and "from awr.world.mesh" not in text, p
