"""M22 GLB 读取与体检（FR-004、FR-005；M22-AC-001）：节点展平、限界检查、镜像拒绝、纹理登记与体检报告。"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from glbfix import png, write_glb

from awr.world.glb.inspect import inspect_glb
from awr.world.glb.reader import load_scene
from awr.world.ingest.types import ConfigError, GateFailed


def test_flatten_trs_and_matrix_in_json_child_order(tmp_path: Path) -> None:
    q = [0.0, math.sin(math.pi / 4), 0.0, math.cos(math.pi / 4)]          # 绕 Y 90°
    nodes = [{"children": [1, 2], "scale": [2, 2, 2]}, {"mesh": 0, "translation": [10, 0, 0], "rotation": q},
             {"mesh": 0, "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 5, 0, 1]}]
    sc = load_scene(write_glb(tmp_path / "a.glb", nodes=nodes, tex=png(8, 4)))
    assert [n for n, _ in sc.nodes] == [0, 1, 2] and [p.node for p in sc.prims] == [1, 2]
    p1, p2 = sc.prims
    # 节点 1：S(2)·(T(10,0,0)·R_y(90°))，顶点 (-1, 0, -1) → R_y: (-1, 0, 1) → +T → (9, 0, 1) → ×2
    np.testing.assert_allclose(p1.pos[0], [18, 0, 2], atol=1e-12)
    np.testing.assert_allclose(p2.pos[0], [-2, 10, -2], atol=1e-12)                # 列主序 matrix 的平移 (0, 5, 0) 再 ×2
    assert p1.texture == 0 and sc.textures[0].width == 8 and sc.textures[0].height == 4
    V, F, P = sc.merged()
    assert V.shape == (8, 3) and F.max() == 7 and P.tolist() == [0, 0, 1, 1]


def test_mirrored_node_is_rejected_but_inspectable(tmp_path: Path) -> None:
    p = write_glb(tmp_path / "m.glb", nodes=[{"mesh": 0, "scale": [-1, 1, 1]}])
    with pytest.raises(GateFailed) as e:
        load_scene(p)
    assert e.value.exit_code == 2
    rep = inspect_glb(p)
    assert rep["nodes"]["mirrored"] == [0] and "geometry" not in rep


@pytest.mark.parametrize("mutate", [
    lambda js: js["buffers"][0].update(uri="ext.bin"),
    lambda js: js["accessors"][0].update(count=10_000),
    lambda js: js["bufferViews"][0].update(byteOffset=1 << 30),
    lambda js: js.update(extensionsRequired=["KHR_draco_mesh_compression"]),
    lambda js: js["accessors"][0].update(componentType=5130),
])
def test_limits(tmp_path: Path, mutate) -> None:
    with pytest.raises(ConfigError):
        load_scene(write_glb(tmp_path / "x.glb", mutate=mutate))


def test_index_out_of_range_and_bad_container(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="index out of range"):
        load_scene(write_glb(tmp_path / "i.glb", F=np.array([[0, 1, 9]], np.uint32)))
    bad = tmp_path / "b.glb"
    bad.write_bytes(b"glTF" + b"\x01\0\0\0" + b"\0" * 12)
    with pytest.raises(ConfigError):
        load_scene(bad)
    p = write_glb(tmp_path / "t.glb")
    p.write_bytes(p.read_bytes() + b"\0\0\0\0")                                       # 头长度与文件不符
    with pytest.raises(ConfigError, match="header length"):
        load_scene(p)


def test_inspect_report(tmp_path: Path) -> None:
    p = write_glb(tmp_path / "r.glb", tex=png(16, 8), extras={"author": "Mitko", "license": "CC-BY-4.0", "title": "t", "other": 1})
    rep = inspect_glb(p)
    json.dumps(rep)
    assert rep["geometry"]["triangles"] == 2 and rep["geometry"]["unique_positions"] == 4
    assert rep["up_axis_vote"]["+y"] == 1.0                                          # 水平面法线 +Y（glTF 上轴）
    assert rep["textures"] == {"count": 1, "sizes": ["16x8"], "texels": 128, "bytes": rep["textures"]["bytes"], "mimes": ["image/png"]}
    assert rep["extras"] == {"author": "Mitko", "license": "CC-BY-4.0", "title": "t"}
    assert rep["extensions"]["materials"] == ["KHR_materials_unlit"] and rep["index_types"] == ["uint32"]
    fast = inspect_glb(p, geometry=False)
    assert "geometry" not in fast and fast["textures"]["count"] == 1
