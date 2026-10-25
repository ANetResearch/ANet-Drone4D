"""分发 catalog 真值 0 行（D3-AC-16"分发 catalog 上查询 `/truth/**` 返回 0 行"；AWR-05 §6.1.4–6.1.6；M27-FR-079–085；
M26 §8.2 L-18）。

R03 已在合成运行上验证 catalog 的层策略；本文件从 Env 端到端复核：在排名配置（`ranked = True`、63 位种子、M27 形式的运行
标识与种子承诺值）下真实运行 L1 与 L2-depth 回合（运动学开发档，CPU 替身渲染），经 EnvRecorder 写 MCAP → R01 离线导出
（`review` 档）→ `register_run` 注册到评测与分发两个在线 catalog（rerun-sdk 0.38.1 OSS catalog 服务），再以 URL 连接的
`CatalogClient`（与部署中经内网访问同一路径）在分发 catalog 上穷举查询面：

- 每个 dataset 的 schema 不含 `/truth/**`、`/eval/**` 实体；`/truth/**`、`/truth`、`/eval/**`、各机 `/truth/fleet/<uav>`、
  `/truth/actors/**` 按实体过滤均为 0 行（全部 segment 与逐 segment 两种口径）；真值位姿查询 0 行；
- 全量读取（`/**`）有数据但无真值或评测列；segment 表无种子列，段属性取值中不出现种子的十进制与十六进制文本；
- 分发 catalog 的默认版面（blueprint dataset）不引用 `/truth`、`/eval`；
- 内容核对：分发 catalog 的 `/fleet/<uav>` 位置为带噪导航解，与评测 catalog 的 `/truth/fleet/<uav>` 在 1 mm 容差内成对吻合
  的样本为 0（真值没有经其他实体路径进入分发 catalog）；
- 服务重启重放注册清单、由数据集目录重建清单之后，分发 catalog 仍为 0 行。

正向对照：评测 catalog 上同一组查询返回真值行，版面引用 `/truth/**`，segment 表带排名运行种子；两个 catalog 的导航解逐点相同。

已知缺陷（与 tests/d3/leak_redteam/test_search.py 的 KNOWN_FINDINGS 同源，严格 xfail 跟踪）：导航解姿态取真值
（`env.nav.NavModel`，M26-FR-040 姿态误差未落地），分发 catalog `/fleet/<uav>` 的旋转与 `/truth/fleet/<uav>` 逐点相同。
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rerun")
pytest.importorskip("datafusion")
pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")
pytest.importorskip("zstandard")

from awr.bench.catalog.query import D3Catalog
from awr.bench.catalog.registry import Registry, rebuild, register_run
from awr.bench.catalog.service import CatalogService
from awr.bench.recorder import rerun_export as rx

ROOT = Path(__file__).resolve().parents[3]
UAVS = ("uav-01", "uav-02")
TRUTH_PREFIXES = ("/truth", "/eval")
SEED_KEYS = ("seed", "run_seed", "seed_commit", "agent_seed")
TOL_M = 1e-3                                         # 成对吻合容差（与 A06 R1 位置判据相同）

# 已知缺陷：判据名 → 原因（修复后对应的严格 xfail 用例转为 XPASS 而失败，届时从此处移除）
KNOWN_FINDINGS = {
    "dist.fleet_rotation_equals_truth": "NavSolution.q_wb_xyzw 取真值姿态（M26-FR-040 姿态误差未落地；A06 r1/r2.own_attitude 同源）",
}


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _open3d_error() -> str | None:
    """Env 合成世界需要 open3d；未安装或无法载入（如镜像缺 libX11）时只跳过本文件。"""
    try:
        import open3d  # noqa: F401
    except (ImportError, OSError) as e:
        return f"{type(e).__name__}: {e}"
    return None


_O3D = _open3d_error()
if _O3D is None:
    synth_event = _load("d3_env_conftest_for_catalog_truth", ROOT / "tests" / "d3" / "env" / "conftest.py").synth_event
else:
    @pytest.fixture(scope="module")
    def synth_event():
        pytest.skip(f"[dep] open3d unavailable (Env synthetic world): {_O3D}")


def _ranked_ids(case: str) -> tuple[int, str, str]:
    """排名配置：63 位高熵种子与 M27 形式的提交标识（`<sid>-r<k>`）与种子承诺值；按用例名由带密钥 BLAKE2b 派生以便复现。"""
    d = hashlib.blake2b(case.encode(), key=b"d3-redteam-a08", digest_size=32).digest()
    seed = int.from_bytes(d[:8], "little") >> 1
    sid = f"sub-{d[8:14].hex()}"
    commit = "hmac-sha256:" + hashlib.sha256(d[14:] + seed.to_bytes(8, "little")).hexdigest()
    return seed, sid, commit


def _episode(synth_event, run_dir: Path, run_id: str, seed: int, commit: str, obs_level: str) -> None:
    """排名配置下的一个 Env 回合（2 机，运动学开发档；L2 类以 CPU 替身渲染），EnvRecorder 写 MCAP 与真值文件。"""
    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.spec import EnvSpec

    t_l2 = _load("d3_env_test_l2_for_catalog_truth", ROOT / "tests" / "d3" / "env" / "test_l2.py")
    w, ev = synth_event
    spec = EnvSpec(run_id, seed, w, ev, t_l2.FLEET, obs_level=obs_level, physics="kinematic", duration_s=2.0, out_dir=run_dir,
                   ranked=True, seed_commit=commit)
    env = BenchEnv(spec, l2=(lambda e: t_l2._l2(e, False)) if obs_level != "L1" else None)
    obs, _ = env.reset()
    vel = {"uav-01": [3.0, 1.5, 0.0], "uav-02": [-2.0, 2.5, 0.0]}                  # 两机速度与偏航角速度不同，姿态非平凡
    while env.agents:
        obs, *_ = env.step({u: {"schema": "drone4d.bench.act.v1", "k": o["obs"]["k"],
                                "nav": {"mode": "velocity", "vel_mps": vel[u], "yaw_rate_rps": 0.4, "frame": "world"}}
                            for u, o in obs.items()})
    env.close()


@pytest.fixture(scope="module")
def catalogs(synth_event, tmp_path_factory):
    """两个排名运行（L1、L2-depth）导出并注册到评测与分发两个在线 catalog；查询客户端按服务 URL 连接。"""
    import rerun as rr

    data_root = tmp_path_factory.mktemp("d3")
    seed, sid, commit = _ranked_ids("catalog-truth")
    runs = {f"{sid}-r0": ("L1", seed), f"{sid}-r1": ("L2-depth", seed ^ 0x5DEECE66D)}
    exports = {}
    for rid, (level, s) in runs.items():
        run_dir = data_root / "dataset" / "synth-handoff" / "runs" / rid                 # M27 数据集布局（subworker 同）
        _episode(synth_event, run_dir, rid, s, commit, level)
        man = rx.export_run(run_dir, run_dir / "derived" / "rrd", eval_out=data_root / "evalstore" / "rrd" / "synth-handoff" / rid,
                            profile="review")
        assert {"truth", "eval"} <= set(man["files"]), sorted(man["files"])
        exports[rid] = run_dir / "derived" / "rrd" / "export.json"
    root = data_root / "rerun"
    svc = {c: CatalogService(root, c).start() for c in ("eval", "dist")}
    reports = {c: {rid: register_run(ex, catalog=c, root=root, client=svc[c].client()) for rid, ex in exports.items()} for c in svc}
    clients = {c: rr.catalog.CatalogClient(s.url) for c, s in svc.items()}
    yield {"data_root": data_root, "root": root, "runs": runs, "exports": exports, "svc": svc, "reports": reports, "clients": clients,
           "q": {c: D3Catalog(client=cl, target=c) for c, cl in clients.items()}}
    for s in svc.values():
        s.shutdown()


def _entities(client, ds: str) -> set[str]:
    return {c.entity_path for c in client.get_dataset(ds).schema().component_columns()}


def _blueprint_text(client, ds: str) -> str:
    """dataset 默认版面（blueprint dataset）全部取值的文本形式（视图查询表达式、原点等）。"""
    bp = client.get_dataset(ds).blueprint_dataset()
    if bp is None:
        return ""
    idx = [c.name for c in bp.schema().index_columns()]
    return str(bp.reader(index=idx[0]).to_arrow_table().to_pylist()) if idx else ""


def _full(client, ds: str):
    """dataset 全量读取（不按实体过滤，`sim_time` 索引）→ pyarrow.Table。"""
    return client.get_dataset(ds).reader(index="sim_time").to_arrow_table()


def _truth_exprs(uavs) -> list[str]:
    return ["/truth/**", "/truth", "/eval/**", "/eval", "/truth/actors/**", *[f"/truth/fleet/{u}" for u in uavs]]


# ---------------------------------------------------------------------- 查询 0 行


def test_dist_catalog_truth_queries_return_zero_rows(catalogs) -> None:
    """D3-AC-16：分发 catalog 上 `/truth/**` 等真值与评测实体的全部查询口径返回 0 行；评测 catalog 为正向对照。"""
    dist, ev = catalogs["q"]["dist"], catalogs["q"]["eval"]
    segs = sorted(catalogs["runs"])
    assert dist.datasets() == ev.datasets() and dist.datasets()
    for ds in dist.datasets():
        ents = _entities(catalogs["clients"]["dist"], ds)
        assert ents and not [e for e in ents if e.startswith(TRUTH_PREFIXES)], sorted(ents)
        for expr in _truth_exprs(UAVS):
            assert dist.query(ds, expr).num_rows == 0, expr
            assert dist.query(ds, expr, runs=segs).num_rows == 0, expr
            for s in segs:
                assert dist.query(ds, expr, runs=[s]).num_rows == 0, (expr, s)
            assert dist.query(ds, expr, index=None).num_rows == 0, expr                 # 静态口径
        assert dist.poses(segs, 10.0, event=ds, source="truth").num_rows == 0
        full = _full(catalogs["clients"]["dist"], ds)
        assert full.num_rows > 0
        assert not [c for c in full.column_names if c.startswith(TRUTH_PREFIXES)], full.column_names
        # 正向对照：评测 catalog 同一查询有真值行，且逐 segment 都有
        assert ev.query(ds, "/truth/**").num_rows > 0
        for s in segs:
            assert ev.query(ds, "/truth/**", runs=[s]).num_rows > 0, s
        assert ev.poses(segs, 10.0, event=ds, source="truth").num_rows > 0
        assert {e for e in _entities(catalogs["clients"]["eval"], ds) if e.startswith("/truth/fleet/")} == {f"/truth/fleet/{u}" for u in UAVS}
    # 注册报告：分发 catalog 排除 truth 与 eval 层，只注册 obs.*（本用例无 Agent 调试层）
    for rep in catalogs["reports"]["dist"].values():
        assert sorted(rep["excluded"]) == ["eval", "truth"] and all(x.startswith("obs.") for x in rep["registered"]), rep
    for rep in catalogs["reports"]["eval"].values():
        assert {"truth", "eval"} <= set(rep["registered"]), rep


def test_dist_catalog_segment_table_and_blueprint_carry_no_truth(catalogs) -> None:
    """segment 表无种子列且段属性中不出现种子文本；默认版面不引用 `/truth`、`/eval`；catalog 条目只有 dataset 本身。"""
    dist, ev = catalogs["q"]["dist"], catalogs["q"]["eval"]
    seeds = {s for _lvl, s in catalogs["runs"].values()}
    needles = {str(s) for s in seeds} | {f"{s:x}" for s in seeds} | {f"{s:016x}" for s in seeds}
    for ds in dist.datasets():
        t = dist.runs(ds)
        assert sorted(t.column("segment").to_pylist()) == sorted(catalogs["runs"])
        assert not [k for k in t.column_names if k in SEED_KEYS], t.column_names
        cells = " ".join(str(v) for col in t.columns for v in col.to_pylist()).lower()
        assert not [n for n in needles if n in cells]
        for layers in t.column("layers").to_pylist():
            assert all(x.startswith(("obs.", "agent.")) for x in layers), layers
        te = ev.runs(ds)                                                               # 正向对照：排名运行种子只在评测 catalog
        assert dict(zip(te.column("segment").to_pylist(), te.column("seed").to_pylist(), strict=True)) == {
            rid: s for rid, (_lvl, s) in catalogs["runs"].items()}
        bd, be = _blueprint_text(catalogs["clients"]["dist"], ds), _blueprint_text(catalogs["clients"]["eval"], ds)
        assert bd and "/fleet" in bd and "/truth" not in bd and "/eval" not in bd
        assert "/truth/**" in be
    names = set(catalogs["clients"]["dist"].entry_names())
    assert names == set(dist.datasets()), names
    assert catalogs["clients"]["dist"].table_names() == []


# ---------------------------------------------------------------------- 内容核对


def _joined(catalogs, ds: str) -> tuple[dict, dict, dict]:
    """→ (分发导航解, 评测导航解, 评测真值) 三张位姿表，各为 {(segment, sim_time, uav): (x, y, z, qx, qy, qz, qw)}。"""
    segs = sorted(catalogs["runs"])

    def rows(q, source):
        d = q.poses(segs, 10.0, event=ds, source=source).to_pydict()
        return {(d["segment"][i], d["sim_time"][i], d["uav"][i]): tuple(d[k][i] for k in ("x", "y", "z", "qx", "qy", "qz", "qw"))
                for i in range(len(d["segment"]))}

    return rows(catalogs["q"]["dist"], "nav"), rows(catalogs["q"]["eval"], "nav"), rows(catalogs["q"]["eval"], "truth")


def test_dist_fleet_positions_are_noisy_nav_not_truth(catalogs) -> None:
    """真值没有经其他实体进入分发 catalog：`/fleet/<uav>` 位置与 `/truth/fleet/<uav>` 在 1 mm 内成对吻合的样本为 0；
    分发与评测两个 catalog 的导航解逐点相同（同一观测分片）。"""
    for ds in catalogs["q"]["dist"].datasets():
        nav_d, nav_e, truth = _joined(catalogs, ds)
        assert nav_d and nav_d.keys() == nav_e.keys()
        assert all(np.allclose(nav_d[k], nav_e[k], atol=0, rtol=0) for k in nav_d)
        common = sorted(nav_d.keys() & truth.keys())
        assert len(common) >= 0.9 * len(nav_d), (len(common), len(nav_d))
        pos = np.array([nav_d[k][:3] for k in common]) - np.array([truth[k][:3] for k in common])
        pair = np.sum(np.abs(pos) <= TOL_M, axis=1) >= 2                                 # 同一样本两个分量同时吻合
        assert int(pair.sum()) == 0, int(pair.sum())
        assert float(np.median(np.linalg.norm(pos, axis=1))) > 0.05                       # 导航误差量级（GM 标准差约 1 m）


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=KNOWN_FINDINGS["dist.fleet_rotation_equals_truth"])
def test_dist_fleet_rotation_is_not_truth(catalogs) -> None:
    """严格 xfail：导航解姿态误差落地后，分发 catalog `/fleet/<uav>` 的旋转不再逐点等于真值（四元数取绝对值比较，覆盖 ±q）。"""
    for ds in catalogs["q"]["dist"].datasets():
        nav_d, _nav_e, truth = _joined(catalogs, ds)
        common = sorted(nav_d.keys() & truth.keys())
        q_n = np.abs(np.array([nav_d[k][3:] for k in common]))
        q_t = np.abs(np.array([truth[k][3:] for k in common]))
        nontrivial = np.abs(q_t - np.array([0.0, 0.0, 0.0, 1.0])).max(axis=1) > 1e-3
        assert nontrivial.sum() > 0
        same = np.all(np.abs(q_n - q_t) <= 1e-6, axis=1) & nontrivial
        assert int(same.sum()) == 0, f"{int(same.sum())} of {int(nontrivial.sum())} non-trivial rotations equal truth"


# ---------------------------------------------------------------------- 重放与重建


def test_dist_truth_zero_after_replay_and_rebuild(catalogs, tmp_path: Path) -> None:
    """服务重启（注册清单重放）与由数据集目录重建清单之后，分发 catalog 仍无真值；评测 catalog 重建后有真值。"""
    root = catalogs["root"]
    r2 = tmp_path / "rerun"
    shutil.copytree(root / "dist", r2 / "dist", ignore=shutil.ignore_patterns("service.json", "*.lock"))
    with CatalogService(r2, "dist") as svc:
        assert svc.report["skipped"] == []
        q = D3Catalog(svc.url, target="dist")
        for ds in q.datasets():
            assert sorted(q.runs(ds).column("segment").to_pylist()) == sorted(catalogs["runs"])
            for expr in _truth_exprs(UAVS):
                assert q.query(ds, expr).num_rows == 0, expr
            assert _full(q.client, ds).num_rows > 0
    for c in ("dist", "eval"):
        rb = rebuild(catalogs["data_root"], catalog=c, root=tmp_path / "rb")
        assert rb["runs"] == len(catalogs["runs"]) and rb["errors"] == [], rb
    assert {k[2] for k in Registry.load(tmp_path / "rb", "dist").entries} == {f"obs.{u}" for u in UAVS}
    assert {"truth", "eval"} <= {k[2] for k in Registry.load(tmp_path / "rb", "eval").entries}
    with CatalogService(tmp_path / "rb", "dist") as sd, CatalogService(tmp_path / "rb", "eval") as se:
        qd, qe = D3Catalog(sd.url, target="dist"), D3Catalog(se.url, target="eval")
        for ds in qd.datasets():
            assert qd.query(ds, "/truth/**").num_rows == 0 and qd.query(ds, "/eval/**").num_rows == 0
            assert qe.query(ds, "/truth/**").num_rows > 0
