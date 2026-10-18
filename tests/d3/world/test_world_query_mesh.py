"""D3-AC-05：D2 `tests/world_query` 的 105 个用例在 DSM 柱体网格夹具下对 MeshWorldQuery 重跑，另做逐射线统计。

重跑方式：按文件导入 D2 的 14 个测试模块（`importlib` 以独立模块名装载，D2 文件不改），把其中的测试函数与模块级夹具
原名登记到本模块，由本模块的 `fake_dir`、`wq`、`city_wq` 替换 `tests/world_query/conftest.py` 的同名夹具：合成小世界
追加由有效 DSM 生成的 `awr-trimesh@1` 柱体网格图层（`fixtures_mesh.py`），本模块全部用例在 `AWR_D3_ENABLED = 1` 下运行
（含子进程），`open_world_query` 经扩展点 7 分派到 MeshWorldQuery。2.5D 方法沿用 2 m 栅格（`contact` 半径约束
`min(2.0, cell)` 不变），3D 方法为网格精确求交。六城（needs_data）与性能（perf）用例保留 D2 标记，城市世界没有网格图层，
分派后仍为 DsmWorldQuery（验证 D2 七个世界走原路径）。

逐射线统计：`ray_hit`、`free_distance`、`los_batch` 与整幅网格单一 Open3D `RaycastingScene`（世界帧 float32，不分瓦片）
参照比较，距离差 p99 ≤ 1 mm、最大 ≤ 1 cm，视线判定逐对一致；另与 D2 的 2 m 栅格精确遍历比较（同一柱体几何）。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import math
import sys
import types
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("open3d")

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
D2_DIR = REPO / "tests" / "world_query"
sys.path.insert(0, str(D2_DIR))                     # D2 模块的 `from geo_m04_common import ...`
sys.path.insert(0, str(HERE))

from fixtures_mesh import Open3DReference, mesh_tiny_world  # noqa: E402

from awr.world.geometry import DsmWorldQuery, open_world_query  # noqa: E402

D2_MODULES = ("test_bench_geo", "test_bus_contract", "test_derive", "test_grid25", "test_grids", "test_los_kernel", "test_path",
              "test_perf", "test_probe_server", "test_queries", "test_rest", "test_status", "test_traverse_oracle", "test_zones")
D2_CASES = 105                                       # AWR-05 §12.3 第 7 处、D3-AC-05：D2 tests/world_query 的测试函数数
P99_MAX_M, MAX_MAX_M = 1e-3, 1e-2


# ---------------------------------------------------------------------------------------------------- D2 用例导入
def _load_d2(name: str):
    spec = importlib.util.spec_from_file_location(f"d3mesh_{name}", D2_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _is_fixture(obj) -> bool:
    return hasattr(obj, "_fixture_function_marker") or hasattr(obj, "_pytestfixturefunction") or type(obj).__name__ == "FixtureFunctionDefinition"


def _with_module_marks(fn, marks):
    """测试函数的副本，追加 D2 模块级 `pytestmark`（只导入函数时模块级标记不随之而来；不改 D2 模块中的原函数）。"""
    if not marks:
        return fn
    g = types.FunctionType(fn.__code__, fn.__globals__, fn.__name__, fn.__defaults__, fn.__closure__)
    g.__dict__.update({k: (list(v) if k == "pytestmark" else v) for k, v in fn.__dict__.items()})
    g.__kwdefaults__, g.__qualname__, g.__module__, g.__doc__ = fn.__kwdefaults__, fn.__qualname__, fn.__module__, fn.__doc__
    for mk in marks:
        g = mk(g)
    return g


_IMPORTED: dict[str, str] = {}
for _m in D2_MODULES:
    _mod = _load_d2(_m)
    _marks = getattr(_mod, "pytestmark", [])
    _marks = _marks if isinstance(_marks, list) else [_marks]
    for _name, _obj in vars(_mod).items():
        _test = _name.startswith("test_") and inspect.isfunction(_obj)
        if not (_test or (_is_fixture(_obj) and _name not in ("fake_dir", "wq", "city_wq"))):
            continue
        assert _name not in globals(), f"D2 用例名冲突：{_m}.{_name}"
        globals()[_name] = _with_module_marks(_obj, _marks) if _test else _obj
        if _test:
            _IMPORTED[_name] = _m
del _m, _mod, _marks, _name, _obj, _test


# ---------------------------------------------------------------------------------------------------- 夹具
@pytest.fixture(scope="module", autouse=True)
def _d3_enabled():
    """本模块全部用例（含子进程，环境变量继承）打开 D3 开关。"""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AWR_D3_ENABLED", "1")
        yield


@pytest.fixture(scope="module")
def mesh_fixture(tmp_path_factory, _d3_enabled):
    root = tmp_path_factory.mktemp("geo-mesh")
    wd, V, F = mesh_tiny_world(root)
    return root, wd, V, F


@pytest.fixture(scope="module")
def fake_dir(mesh_fixture) -> Path:
    """替换 D2 conftest 的 `fake_dir`：合成小世界 + 柱体网格图层。"""
    return mesh_fixture[0]


@pytest.fixture(scope="module")
def wq(fake_dir):
    """替换 D2 conftest 的 `wq`：分派到 MeshWorldQuery。"""
    from awr.world.mesh.query import MeshWorldQuery

    q = open_world_query(fake_dir / "tiny", fake_dir / ".geo-cache")
    assert type(q) is MeshWorldQuery
    return q


@pytest.fixture(scope="module")
def city_wq():
    from geo_m04_common import WORLDS

    cache = {}

    def get(city: str):
        if city not in cache:
            cache[city] = open_world_query(WORLDS / city, WORLDS / ".geo-cache")
            assert type(cache[city]) is DsmWorldQuery                     # 城市世界无网格图层：不分派
        return cache[city]
    return get


@pytest.fixture(scope="module")
def ref(mesh_fixture) -> Open3DReference:
    return Open3DReference(mesh_fixture[2], mesh_fixture[3])


@pytest.fixture(scope="module")
def dsm_wq(fake_dir):
    """同一世界不分派的 DsmWorldQuery（D2 栅格精确遍历，比较用）。"""
    from awr.world.geometry import open_dsm_world_query

    return open_dsm_world_query(fake_dir / "tiny", fake_dir / ".geo-cache")


# ---------------------------------------------------------------------------------------------------- 导入完整性
def test_mesh_rerun_imports_all_d2_cases():
    """导入的测试函数恰为 D2 tests/world_query（不含 D3 新增的 test_d3_dispatch.py）的 105 个。"""
    names = {}
    for m in D2_MODULES:
        for line in (D2_DIR / f"{m}.py").read_text(encoding="utf-8").splitlines():
            if line.startswith("def test_"):
                names[line[4:line.index("(")]] = m
    assert len(_IMPORTED) == D2_CASES == len(names) and names == _IMPORTED
    extra = sorted(p.stem for p in D2_DIR.glob("test_*.py") if p.stem not in D2_MODULES)
    assert extra == ["test_d3_dispatch"]


def test_mesh_fixture_matches_effective_dsm(mesh_fixture, wq, dsm_wq):
    """网格图层登记正确；网格派生的 2.5D 数组与 D2 相同（2 m 栅格、`contact` 半径上限不变）。"""
    _root, wd, _V, F = mesh_fixture
    doc = json.loads((wd / "world.json").read_text(encoding="utf-8"))
    assert any(L["format"] == "awr-trimesh@1" and L["role"] == "geometry" for L in doc["layers"])
    info = wq.mesh_info()
    assert info["triangles"] == len(F) and info["tiles"] == 16                      # 128 m 瓦片：x、y 各 4 列
    assert wq.eff.cell == 2.0 and np.array_equal(np.asarray(wq.eff.a), np.asarray(dsm_wq.eff.a))
    assert wq.derive_sha8 == dsm_wq.derive_sha8
    p = np.array([[-40.0, -20.0, 99.0], [-150.0, 50.0, 30.0], [-51.0, -20.0, 50.0]])
    assert wq.contact_mask(p, 2.0).tolist() == dsm_wq.contact_mask(p, 2.0).tolist() == [True, False, True]
    from awr.world.geometry import GeoError

    with pytest.raises(GeoError):
        wq.contact_mask(p, 2.5)                                                  # min(2.0, cell) 约束不变


# ---------------------------------------------------------------------------------------------------- 逐射线统计
def _dirs(rng, n, el_lo=-80.0, el_hi=10.0) -> np.ndarray:
    az = rng.uniform(0, 2 * math.pi, n)
    el = np.radians(rng.uniform(el_lo, el_hi, n))
    return np.c_[np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)]


def _origins(rng, q, n, zr=(5.0, 160.0), margin=10.0) -> np.ndarray:
    o = np.c_[rng.uniform(-200 - margin, 200 + margin, n), rng.uniform(-150 - margin, 150 + margin, n), rng.uniform(*zr, n)]
    above = o[:, 2] > np.asarray(q.height_dsm(o[:, :2]), np.float64)
    return o[above]


def _stats(a: np.ndarray, b: np.ndarray) -> dict:
    fa, fb = np.isfinite(a), np.isfinite(b)
    assert np.array_equal(fa, fb), f"命中集合不一致：{int(np.sum(fa != fb))} 条"
    e = np.abs(a[fa] - b[fb])
    return {"n": len(a), "hits": int(fa.sum()), "p99_m": float(np.percentile(e, 99)) if e.size else 0.0,
            "max_m": float(e.max()) if e.size else 0.0}


def _check(st: dict, min_hits: int) -> None:
    assert st["hits"] >= min_hits, st
    assert st["p99_m"] <= P99_MAX_M and st["max_m"] <= MAX_MAX_M, st


def test_ray_hit_per_ray_vs_open3d(wq, ref, dsm_wq, record_property):
    rng = np.random.default_rng(20261018)
    Org = _origins(rng, wq, 6000)
    D = _dirs(rng, len(Org))
    L = 600.0
    got = np.array([(h.dist_m if h.hit else np.inf) for h in (wq.ray_hit(o, d, L) for o, d in zip(Org, D, strict=True))])
    st = _stats(got, ref.first_hit(Org, D, np.full(len(Org), L)))
    _check(st, 3000)
    dsm = np.array([(h.dist_m if h.hit else np.inf) for h in (dsm_wq.ray_hit(o, d, L) for o, d in zip(Org, D, strict=True))])
    st_dsm = _stats(got, dsm)
    assert st_dsm["max_m"] <= MAX_MAX_M, st_dsm                           # 同一柱体几何：与 D2 栅格精确遍历一致
    record_property("ray_hit", {"open3d": st, "dsm_traversal": st_dsm})


def test_free_distance_per_ray_vs_open3d(wq, ref, dsm_wq, record_property):
    rng = np.random.default_rng(20261019)
    Org = _origins(rng, wq, 40000, zr=(2.0, 120.0))
    D = _dirs(rng, len(Org), -60.0, 30.0)
    max_m = 150.0
    got = np.concatenate([wq.free_distance(Org[i:i + 16], D[i:i + 16], max_m) for i in range(0, len(Org), 16)])    # D2 上限 16 条
    want = ref.first_hit(Org, D, np.full(len(Org), max_m))
    st = _stats(np.where(got < max_m, got, np.inf), want)
    _check(st, 10000)
    dsm = np.concatenate([dsm_wq.free_distance(Org[i:i + 16], D[i:i + 16], max_m) for i in range(0, 3200, 16)])
    st_dsm = _stats(np.where(got[:3200] < max_m, got[:3200], np.inf), np.where(dsm < max_m, dsm, np.inf))
    assert st_dsm["max_m"] <= MAX_MAX_M, st_dsm
    record_property("free_distance", {"open3d": st, "dsm_traversal": st_dsm})


def test_los_batch_per_ray_vs_open3d(wq, ref, record_property):
    """视线：逐对判定与参照一致；遮挡对的首个遮挡点距离（两端各裁 eps）差 p99 ≤ 1 mm、最大 ≤ 1 cm。"""
    rng = np.random.default_rng(20261020)
    n, eps = 64 * 400, 0.5
    A = np.c_[rng.uniform(-200, 200, n), rng.uniform(-150, 150, n), rng.uniform(1, 140, n)]
    B = np.c_[rng.uniform(-200, 200, n), rng.uniform(-150, 150, n), rng.uniform(1, 140, n)]
    A[:, 2] = np.maximum(A[:, 2], wq.height_dsm(A[:, :2]) + 0.6)
    B[:, 2] = np.where(np.arange(n) % 3 == 0, wq.height_dsm(B[:, :2]) + rng.uniform(0.6, 1.0, n), np.maximum(B[:, 2], wq.height_dsm(B[:, :2]) + 0.6))
    vis = np.concatenate([wq.los_batch(A[i:i + 64], B[i:i + 64], 1.0, eps) for i in range(0, n, 64)])
    Lab = np.linalg.norm(B - A, axis=1)
    d = (B - A) / Lab[:, None]
    t_ref = ref.first_hit(A, d, Lab - eps, eps)
    assert np.array_equal(vis, ~np.isfinite(t_ref)), int(np.sum(vis != ~np.isfinite(t_ref)))
    t_mesh, _, _ = wq._cast(A + d * eps, d, Lab - 2 * eps)                  # 判定所依据的首个遮挡距离（与 los_batch 同一路径）
    st = _stats(t_mesh + eps, t_ref)
    assert (~vis).sum() >= 0.1 * n and vis.sum() >= 0.5 * n, (int(vis.sum()), n)
    _check(st, int(0.1 * n))
    record_property("los_batch", {"open3d": st, "visible": int(vis.sum())})
