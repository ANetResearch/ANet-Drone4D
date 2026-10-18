"""评测与分发两个 catalog、跨 Run 查询（M27-FR-079–085；AWR-05 §6.1.4–6.1.6；D3-AC-20 的 catalog 部分、D3-AC-16"分发 catalog 上
查询 `/truth/**` 返回 0 行"）。

- 层策略：评测 catalog 注册 obs.<uav>、agent.<uav>、truth、eval 全部层；分发 catalog 只注册 obs.* 与 agent.*，真值层与评测层在
  计划中排除、直接请求注册被拒绝；分发 catalog 拒绝含 `/truth/**` 实体或 `seed` 段属性的文件；dataset 名 `<world>.<event>`；
- 查询：分发 catalog 上 `/truth/**`、`/eval/**`、instance 图与真值位姿查询均为 0 行，segment 表无 `seed` 列；评测 catalog 有真值；
  跨 3 个 Run 的位姿查询 ≤ 1 s，`/fleet/<uav>` 逐点等于带噪导航解，latest-at 对齐；按属性筛选 Run；帧与可见度查询；
- 版本：segment 属性 `rerun_version` 不同 minor 时 `viewer_url` 返回 694 与迁移提示；文件头版本不同 minor 时拒绝注册（694）；
  Viewer 配置 `memory_limit` ≤ 1.5 GB；深链接经 rerun-proxy 地址改写且不含令牌；
- 持久化：`registry.json` 原子写入，服务重启自动重放（缺失文件跳过并列出），可由数据集目录重建；注册前 `rerun rrd optimize`
  不改变语义；保留期清理先在两个 catalog 注销再删除，注销失败时不删除；命令行。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rerun")
pytest.importorskip("datafusion")
pytest.importorskip("mcap_protobuf")
pytest.importorskip("foxglove_schemas_protobuf")
pytest.importorskip("zstandard")

from awr.bench import retention as ret
from awr.bench.catalog import __main__ as cli
from awr.bench.catalog import perf
from awr.bench.catalog.query import VIEWER_MEMORY_LIMIT, D3Catalog, parse_bytes, viewer_options
from awr.bench.catalog.registry import (
    CATALOG_LAYERS,
    REGISTRY_SCHEMA,
    CatalogError,
    CatalogVersionError,
    Entry,
    Registry,
    check_layer_file,
    dataset_name,
    deregister_callback,
    layer_allowed,
    plan_export,
    rebuild,
    register_asset,
    register_run,
)
from awr.bench.catalog.service import CatalogService, live_client
from awr.bench.recorder import rerun_export as rx

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tx = _load("d3_rerun_export_tests_for_catalog", ROOT / "tests" / "d3" / "rerun" / "test_export.py")     # 合成运行 make_run

EVENT_DIR = "handoff-01"
DS = "campus-v2.handoff-01"                       # make_run：world Campus.V2、event handoff.01
APP = "drone4d-d3-campus-v2-handoff-01"
RUNS = ("run-0001", "run-0002", "run-0003")       # run-0003 为排名运行（种子只在 truth / eval 层）
NAV_SCALE = {"run-0001": 1.0, "run-0002": 1.2, "run-0003": 0.8}
UAVS = ("uav-01", "uav-02")


def _agent_rrd(path: Path, run_id: str, uav: str, *, extra: str | None = None, props: dict | None = None) -> Path:
    """Agent 调试录制 agent.<uav>.rrd（只含 /agents/<uav>/**）；`extra` 写一个越界实体用于反例。"""
    import rerun as rr

    w = rx.LayerWriter(path, app_id=APP, recording_id=run_id, layer="agent", properties={"run_id": run_id, "layer": "agent", "uav": uav,
                       "rerun_version": rx.sdk_version(), **(props or {})}, timebase=rx.Timebase(250))
    w.static("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP)
    for k in range(10):
        t = k * 100_000_000
        w.log(f"/agents/{uav}/msg", t, rr.TextLog(f"step {k}"))
        w.log(f"/agents/{uav}/plan", t, rr.LineStrips3D([[[0, 0, 0], [k, 1, 2]]]))
        if extra:
            w.transform(extra, t, [1.0, 2.0, 3.0], [0, 0, 0, 1])
    w.close()
    return path


def _obs_rrd(path: Path, run_id: str, *, rerun_version: str | None = None, extra: str | None = None, seed: int | None = None) -> Path:
    """最小观测分片 obs.uav-01.rrd（/world + /fleet/uav-01 10 Hz）；用于版本、真值实体与种子反例。"""
    import rerun as rr

    props = {"run_id": run_id, "layer": "obs", "uav": "uav-01", "rerun_version": rerun_version or rx.sdk_version()}
    if seed is not None:
        props["seed"] = seed
    w = rx.LayerWriter(path, app_id=APP, recording_id=run_id, layer="obs", properties=props, timebase=rx.Timebase(250))
    w.static("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP)
    for k in range(20):
        w.transform("/fleet/uav-01", k * 100_000_000, [float(k), 0.0, 10.0], [0, 0, 0, 1])
        if extra:
            w.transform(extra, k * 100_000_000, [float(k), 0.0, 10.0], [0, 0, 0, 1])
    w.close()
    return path


def _export_json(d: Path, run_id: str, files: dict[str, Path]) -> Path:
    man = {"schema": rx.EXPORT_SCHEMA, "run_id": run_id, "recording_id": run_id, "application_id": APP, "world": "Campus.V2",
           "event": "handoff.01", "files": {k: {"path": str(p), "bytes": p.stat().st_size} for k, p in files.items()},
           "sensor_map": {"cam": {"eo": "cam0"}, "lidar": {"lidar0": "lidar0"}}}
    (d / "export.json").write_text(json.dumps(man), encoding="utf-8")
    return d / "export.json"


def _export(data_root: Path, run_id: str, *, ranked: bool = False, nav_scale: float = 1.0, layers=None) -> Path:
    """数据集布局：<data_root>/dataset/<event>/runs/<rid>/derived/rrd/{obs.<uav>.rrd, export.json}，
    <data_root>/evalstore/rrd/<event>/<rid>/{truth.rrd, eval.rrd}。"""
    runs = data_root / "dataset" / EVENT_DIR / "runs"
    d = tx.make_run(runs, run_id=run_id, nav_scale=nav_scale, ranked=ranked)
    out = d / "derived" / "rrd"
    rx.export_run(d, out, eval_out=data_root / "evalstore" / "rrd" / EVENT_DIR / run_id, profile="review", layers=layers)
    return out / "export.json"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """3 个 Run 导出并注册到评测与分发两个在线 catalog（run-0001 另带 agent.uav-01.rrd）。"""
    data_root = tmp_path_factory.mktemp("d3")
    exports = {r: _export(data_root, r, ranked=(r == "run-0003"), nav_scale=NAV_SCALE[r]) for r in RUNS}
    _agent_rrd(exports["run-0001"].parent / "agent.uav-01.rrd", "run-0001", "uav-01")
    orig = tmp_path_factory.mktemp("orig") / "obs.uav-01.rrd"
    shutil.copy2(exports["run-0001"].parent / "obs.uav-01.rrd", orig)
    root = data_root / "rerun"
    svc = {c: CatalogService(root, c).start() for c in ("eval", "dist")}
    reports = {c: [register_run(exports[r], catalog=c, root=root, client=svc[c].client()) for r in RUNS] for c in ("eval", "dist")}
    yield {"data_root": data_root, "root": root, "exports": exports, "svc": svc, "reports": reports, "orig_obs": orig,
           "eval": D3Catalog(client=svc["eval"].client(), target="eval"), "dist": D3Catalog(client=svc["dist"].client(), target="dist")}
    for s in svc.values():
        s.shutdown()


# ---------------------------------------------------------------------- 层策略与命名


def test_layer_policy_and_dataset_name(world) -> None:
    assert dataset_name("Campus.V2", "handoff.01") == DS                       # 点号只作层级分隔
    assert CATALOG_LAYERS["dist"] == ("obs", "agent")
    for layer, ev, di in (("obs.uav-01", True, True), ("agent.uav-02", True, True), ("truth", True, False), ("eval", True, False)):
        assert layer_allowed("eval", layer) is ev and layer_allowed("dist", layer) is di
    with pytest.raises(ValueError):
        layer_allowed("dist", "obs")
    plan, excluded = plan_export(world["exports"]["run-0001"], "dist")
    assert sorted(e.layer for e in plan) == ["agent.uav-01", "obs.uav-01", "obs.uav-02"] and sorted(excluded) == ["eval", "truth"]
    plan_e, excl_e = plan_export(world["exports"]["run-0001"], "eval")
    assert sorted(e.layer for e in plan_e) == ["agent.uav-01", "eval", "obs.uav-01", "obs.uav-02", "truth"] and excl_e == []
    r = world["reports"]["dist"][0]
    assert r["dataset"] == DS and r["segment"] == "run-0001" and sorted(r["excluded"]) == ["eval", "truth"]


def test_eval_catalog_registers_all_layers(world) -> None:
    t = world["eval"].runs(DS)
    assert t.column("segment").to_pylist() == list(RUNS)
    layers = dict(zip(t.column("segment").to_pylist(), t.column("layers").to_pylist(), strict=True))
    assert sorted(layers["run-0001"]) == ["agent.uav-01", "eval", "obs.uav-01", "obs.uav-02", "truth"]
    assert sorted(layers["run-0002"]) == ["eval", "obs.uav-01", "obs.uav-02", "truth"]
    seeds = dict(zip(t.column("segment").to_pylist(), t.column("seed").to_pylist(), strict=True))
    assert seeds["run-0003"] == 7                                               # 排名运行的种子只在评测 catalog
    assert set(t.column("rerun_version").to_pylist()) == {rx.RERUN_LOCKED}
    assert world["eval"].query(DS, "/truth/**").num_rows > 0
    ds = world["svc"]["eval"].client().get_dataset(DS)
    assert ds.default_blueprint() is not None and len(ds.blueprints()) == 1


def test_dist_catalog_has_no_truth(world) -> None:
    """D3-AC-16：分发 catalog 上查询 `/truth/**` 返回 0 行；D3-AC-20：分发 catalog 只含 obs.* 与 agent.*。"""
    dist = world["dist"]
    t = dist.runs(DS)
    for layers in t.column("layers").to_pylist():
        assert all(x.startswith(("obs.", "agent.")) for x in layers), layers
    assert sorted(t.column("layers").to_pylist()[0]) == ["agent.uav-01", "obs.uav-01", "obs.uav-02"]
    assert "seed" not in t.column_names                                         # 种子不进入分发 catalog
    for expr in ("/truth/**", "/eval/**", "/fleet/uav-01/cam0/image/instance", "/fleet/uav-01/cam0/image/semantic"):
        assert dist.query(DS, expr).num_rows == 0, expr
    assert dist.query(DS, "/truth/**", runs=list(RUNS)).num_rows == 0
    assert dist.poses(list(RUNS), 10.0, source="truth").num_rows == 0
    ents = {c.entity_path for c in world["svc"]["dist"].client().get_dataset(DS).schema().component_columns()}
    assert ents and not any(e.startswith(("/truth", "/eval")) for e in ents)
    assert dist.query(DS, "/agents/**").num_rows > 0                           # Agent 层在分发 catalog
    assert dist.query(DS, "/fleet/uav-01").num_rows > 0
    with pytest.raises(CatalogError) as ei:
        dist.visibility("person_a", (0.0, 3.0))
    assert ei.value.code == 115


def test_dist_refuses_truth_eval_and_seed(world, tmp_path: Path) -> None:
    ex = world["exports"]["run-0001"]
    truth = next(e for e in plan_export(ex, "eval")[0] if e.layer == "truth")
    with pytest.raises(CatalogError, match="may not be registered to the dist catalog"):
        check_layer_file(truth, "dist")
    bad = _obs_rrd(tmp_path / "obs.uav-01.rrd", "run-x", extra="/truth/fleet/uav-01")
    e = Entry(DS, "run-x", "obs.uav-01", str(bad), APP)
    with pytest.raises(CatalogError) as ei:
        check_layer_file(e, "dist")
    assert any("/truth" in p for p in ei.value.detail["problems"])
    with pytest.raises(CatalogError):
        check_layer_file(e, "eval")                                             # 观测层含真值实体在评测 catalog 也不合规
    seeded = _obs_rrd(tmp_path / "s" / "obs.uav-01.rrd", "run-s", seed=1234)
    es = Entry(DS, "run-s", "obs.uav-01", str(seeded), APP)
    with pytest.raises(CatalogError, match="seed"):
        check_layer_file(es, "dist")
    assert check_layer_file(es, "eval").bytes > 0
    agent_bad = _agent_rrd(tmp_path / "agent.uav-01.rrd", "run-a", "uav-01", extra="/truth/actors/person_a")
    with pytest.raises(CatalogError):
        check_layer_file(Entry(DS, "run-a", "agent.uav-01", str(agent_bad), APP), "dist")
    reg = Registry.load(world["root"], "dist")
    assert all(k[2].startswith(("obs.", "agent.")) for k in reg.entries)


# ---------------------------------------------------------------------- 查询


def test_cross_run_pose_query(world) -> None:
    """跨 3 个 Run 的位姿查询 ≤ 1 s；/fleet/<uav> 为带噪导航解（逐点等于 NavSolution），真值位姿只在评测 catalog 的 /truth/**。"""
    q = world["eval"]
    q.poses(list(RUNS), 10.0)
    t0 = time.perf_counter()
    tb = q.poses(list(RUNS), 10.0)
    dt = time.perf_counter() - t0
    assert dt <= 1.0, dt
    cold = D3Catalog(client=world["svc"]["eval"].client(), target="eval")
    t0 = time.perf_counter()
    cold.poses(list(RUNS), 10.0)
    assert time.perf_counter() - t0 <= 1.0
    assert tb.column_names == ["segment", "sim_time", "uav", "x", "y", "z", "qx", "qy", "qz", "qw"]
    n_t = int(tx.T_S * 10)
    assert tb.num_rows == len(RUNS) * len(UAVS) * n_t
    d = tb.to_pydict()
    for i in range(tb.num_rows):
        r, u, t = d["segment"][i], d["uav"][i], d["sim_time"][i]
        want = tx._truth_pos(u, t) + NAV_SCALE[r] * np.array(tx.NAV_OFF[u])
        assert np.allclose([d["x"][i], d["y"][i], d["z"][i]], want, atol=1e-4), (r, u, t)
    tt = q.poses(list(RUNS), 10.0, source="truth").to_pydict()
    assert len(tt["segment"]) == len(RUNS) * len(UAVS) * n_t
    for i in range(0, len(tt["segment"]), 7):
        assert np.allclose([tt["x"][i], tt["y"][i], tt["z"][i]], tx._truth_pos(tt["uav"][i], tt["sim_time"][i]), atol=1e-4)
    # latest-at：4 Hz 网格（0.25 s）取不晚于网格点的最后一个 10 Hz 样本；只取一机；接受 runs() 表
    sub = q.poses(q.runs(DS, ranked=False), 4.0, uavs=["uav-02"]).to_pydict()
    assert set(sub["segment"]) == {"run-0001", "run-0002"} and set(sub["uav"]) == {"uav-02"}
    for i, t in enumerate(sub["sim_time"]):
        assert t % 250_000_000 == 0
        t_src = (t // tx.PERIOD) * tx.PERIOD
        want = tx._truth_pos("uav-02", t_src) + NAV_SCALE[sub["segment"][i]] * np.array(tx.NAV_OFF["uav-02"])
        assert np.allclose([sub["x"][i], sub["y"][i], sub["z"][i]], want, atol=1e-4)
    assert world["dist"].poses(list(RUNS), 10.0).num_rows == tb.num_rows     # 分发 catalog 同样提供带噪导航解


def test_runs_filter_frames_and_visibility(world) -> None:
    q = world["eval"]
    assert q.runs(DS, policy="sd*").num_rows == 3 and q.runs(DS, policy="track*").num_rows == 0
    assert q.runs(DS, ranked=True).column("segment").to_pylist() == ["run-0003"]
    assert q.runs(DS, n_uav=2).num_rows == 3
    fr = list(q.frames("run-0001", "uav-01", "cam0", (0.5, 1.0)))
    assert [f.t_ns for f in fr] == [k * tx.PERIOD + 5_000_000 for k in range(5, 10)]
    for f in fr:                                                                # review 档 RGB 为存档 JPEG 原样
        k = (f.t_ns - 5_000_000) // tx.PERIOD
        assert f.kind == "encoded" and f.media_type == "image/jpeg" and f.data == tx._jpeg(k)
    assert list(world["dist"].frames("run-0002", "uav-02", "cam0", (0.0, 0.25)))
    vis = q.visibility("person_a", (0.0, 3.0)).to_pydict()
    assert sorted(zip(vis["segment"], vis["uav"], strict=True)) == sorted((r, u) for r in RUNS for u in UAVS)
    assert set(vis["n_samples"]) == {int(tx.T_S * 10)} and set(vis["n_visible"]) == {int(tx.T_S * 10)}
    win = q.visibility("person_a", (1.0, 1.95), runs=["run-0002"]).to_pydict()
    assert win["segment"] == ["run-0002", "run-0002"] and win["n_samples"] == [10, 10]
    assert q.visibility("person_a", (0.0, 3.0), min_value=1e9).to_pydict()["n_visible"] == [0] * 6
    assert q.visibility("person_b", (0.0, 3.0)).num_rows == 6                  # Env L2 可见度归约（h_unocc_px、vis）


def test_viewer_url_and_memory_limit(world) -> None:
    link = world["eval"].viewer_url("run-0001", (1.0, 2.0))
    assert link.dataset == DS and link.segment == "run-0001" and link.catalog == "eval" and link.migrate_hint is None
    assert "segment_id=run-0001" in link.url and "time_selection=sim_time@" in link.url and link.url.startswith("rerun+http://")
    assert link.viewer == {"memory_limit": VIEWER_MEMORY_LIMIT, "hide_welcome_screen": True, "check_for_updates_on_startup": False}
    assert parse_bytes(link.viewer["memory_limit"]) <= 1_500_000_000
    p = world["dist"].viewer_url("run-0002", (0.0, 1.0), proxy_base="https://lab.example.org/rerun/dist")
    assert p.url.startswith("https://lab.example.org/rerun/dist/dataset/") and "token" not in p.url.lower()
    for bad in ("2GB", "1.6GB", "1.5GiB"):
        with pytest.raises(ValueError):
            viewer_options(bad)
    assert viewer_options("1GiB")["memory_limit"] == "1GiB"
    with pytest.raises(CatalogError):
        world["eval"].viewer_url("run-9999")


def test_rerun_version_mismatch_migration_hint(world, tmp_path: Path) -> None:
    """segment 属性 rerun_version 不同 minor → viewer_url 694 + 迁移提示；文件头版本不同 minor → 拒绝注册（694）。"""
    d = tmp_path / "old"
    obs = _obs_rrd(d / "obs.uav-01.rrd", "run-old", rerun_version="0.37.2")
    ex = _export_json(d, "run-old", {"obs.uav-01": obs})
    root = world["root"]
    for c in ("eval", "dist"):
        register_run(ex, catalog=c, root=root, client=world["svc"][c].client())
    for c in ("eval", "dist"):
        with pytest.raises(CatalogVersionError) as ei:
            world[c].viewer_url("run-old", (0.0, 1.0))
        assert ei.value.code == 694 and "rerun rrd migrate" in ei.value.detail["hint"] and ei.value.detail["found"] == "0.37.2"
        assert "segment_id=run-old" in ei.value.detail["url"]
        soft = world[c].viewer_url("run-old", strict=False)
        assert soft.migrate_hint["code"] == 694 and soft.rerun_version == "0.37.2"
    # 文件头：RRF2 + major、minor、patch
    hdr = tmp_path / "hdr"
    f = _obs_rrd(hdr / "obs.uav-01.rrd", "run-hdr")
    b = bytearray(f.read_bytes())
    assert b[:4] == b"RRF2" and rx.rrd_version(f) == rx.RERUN_LOCKED
    b[5] = 37
    f.write_bytes(bytes(b))
    with pytest.raises(CatalogVersionError) as ei:
        register_run(_export_json(hdr, "run-hdr", {"obs.uav-01": f}), catalog="eval", root=tmp_path / "reg")
    assert ei.value.code == 694 and "migrate" in ei.value.detail["hint"]
    assert not (tmp_path / "reg" / "eval" / "registry.json").exists()


# ---------------------------------------------------------------------- 清单、重放、压实与保留期


def test_registry_persisted_replayed_and_rebuilt(world, tmp_path: Path) -> None:
    root = world["root"]
    doc = json.loads((root / "eval" / "registry.json").read_text(encoding="utf-8"))
    assert doc["schema"] == REGISTRY_SCHEMA and doc["catalog"] == "eval" and doc["layers"] == ["obs", "agent", "truth", "eval"]
    reg = Registry.load(root, "dist")
    keys = {(e.segment, e.layer) for e in reg.of_dataset(DS)}
    assert {("run-0001", "agent.uav-01"), ("run-0003", "obs.uav-02")} <= keys and not any(k[1] in ("truth", "eval") for k in keys)
    assert all(e.optimized and e.rrd_version == rx.RERUN_LOCKED for e in reg.entries.values())
    assert (root / "dist" / "blueprints" / f"{DS}.agent.rbl").is_file() and (root / "eval" / "blueprints" / f"{DS}.eval.rbl").is_file()
    # 服务重启：清单重放（缺失文件跳过并列出）
    r2 = tmp_path / "rerun"
    shutil.copytree(root / "dist", r2 / "dist", ignore=shutil.ignore_patterns("service.json", "*.lock"))
    reg2 = Registry(r2, "dist")
    with reg2.edit():
        reg2.entries[(DS, "run-gone", "obs.uav-01")] = Entry(DS, "run-gone", "obs.uav-01", str(tmp_path / "gone.rrd"), APP)
    t0 = time.perf_counter()
    with CatalogService(r2, "dist") as svc:
        assert time.perf_counter() - t0 < 10.0
        rep = svc.report
        assert rep["entries"] == len(reg.entries) and [s["why"] for s in rep["skipped"]] == ["missing"]
        q = D3Catalog(client=svc.client(), target="dist")
        assert q.runs(DS).column("segment").to_pylist() == ["run-0001", "run-0002", "run-0003", "run-old"]
        assert q.query(DS, "/truth/**").num_rows == 0
        assert q.poses(list(RUNS), 10.0).num_rows == len(RUNS) * len(UAVS) * int(tx.T_S * 10)
        sj = json.loads((r2 / "dist" / "service.json").read_text(encoding="utf-8"))
        assert sj["url"] == svc.url and live_client(r2, "dist") is not None
    assert live_client(tmp_path / "none", "dist") is None
    # 由数据集目录重建
    rb = rebuild(world["data_root"], catalog="dist", root=tmp_path / "rb")
    assert rb["runs"] == 3 and rb["errors"] == []
    assert {k[2] for k in Registry.load(tmp_path / "rb", "dist").entries} == {"obs.uav-01", "obs.uav-02", "agent.uav-01"}


def test_optimize_keeps_semantics(world) -> None:
    """注册前 `rerun rrd optimize` 只重排 Chunk：实体清单与逐实体行（时刻与分量值）不变，路径校验仍通过。"""
    after = world["exports"]["run-0001"].parent / "obs.uav-01.rrd"
    a, b = rx.scan_rrd(world["orig_obs"]), rx.scan_rrd(after)
    assert a.entities == b.entities and a.static == b.static and a.properties == b.properties and a.recording_id == b.recording_id
    for ent in sorted({*a.entities, *a.static}):
        ra = sorted(rx.read_entity(world["orig_obs"], ent), key=repr)
        rb = sorted(rx.read_entity(after, ent), key=repr)
        assert ra == rb, ent
    assert rx.lint_rrd(after, "obs", uav="uav-01", run_id="run-0001", app_id=APP) == []
    e = Registry.load(world["root"], "eval").entries[(DS, "run-0001", "obs.uav-01")]
    assert e.bytes == after.stat().st_size and e.mtime_ns == after.stat().st_mtime_ns


def test_asset_registration(world, tmp_path: Path) -> None:
    import rerun as rr

    p = tmp_path / "basemap.rrd"
    rec = rr.RecordingStream(APP, recording_id="basemap", send_properties=False)
    rec.save(str(p))
    rr.log("/world/basemap", rr.Points3D(np.random.default_rng(0).uniform(-50, 50, (2000, 3))), static=True, recording=rec)
    rec.flush()
    rec.disconnect()
    a = register_asset(p, dataset=DS, catalog="dist", root=tmp_path / "reg", client=world["svc"]["dist"].client())
    assert a["segment"] == "basemap" and "basemap" in world["svc"]["dist"].client().get_dataset(DS).assets()
    deregister_callback(tmp_path / "reg", {"dist": world["svc"]["dist"].client()}, catalogs=("dist",))(p)
    assert "basemap" not in world["svc"]["dist"].client().get_dataset(DS).assets()
    bad = tmp_path / "bad.rrd"
    _obs_rrd(bad, "run-b")
    with pytest.raises(CatalogError):
        register_asset(bad, dataset=DS, catalog="dist", root=tmp_path / "reg")


def test_retention_deregisters_before_delete(tmp_path: Path) -> None:
    """派生物按保留期清理（M27-FR-085）：.rrd 删除前先在评测与分发两个 catalog 注销；注销失败不删除。"""
    data_root = tmp_path / "d3"
    ex = _export(data_root, "run-r1")
    run_dir = ex.parent.parent.parent
    root = data_root / "rerun"
    with CatalogService(root, "eval") as se, CatalogService(root, "dist") as sd:
        clients = {"eval": se.client(), "dist": sd.client()}
        for c in ("eval", "dist"):
            register_run(ex, catalog=c, root=root, client=clients[c])
        t0 = time.time_ns()
        ret.register_derived(run_dir, "rrd", now=t0)
        policy = ret.RetentionPolicy(day_ns=1_000)
        later = t0 + 31 * 1_000

        def _fail(_p: Path) -> None:
            raise RuntimeError("catalog unreachable")

        rep = ret.retention_gc(data_root, policy=policy, now=later, deregister_rrd=_fail)
        assert rep["errors"] and (ex.parent / "obs.uav-01.rrd").is_file()
        assert D3Catalog(client=clients["eval"]).runs(DS).num_rows == 1
        rep = ret.retention_gc(data_root, policy=policy, now=later, deregister_rrd=deregister_callback(root, clients))
        assert rep["errors"] == [], rep["errors"]
        deleted = {Path(x["path"]).name for x in rep["deleted"]}
        assert {"rrd", "run-r1"} & deleted
        assert not (ex.parent / "obs.uav-01.rrd").exists()
        assert not (data_root / "evalstore" / "rrd" / EVENT_DIR / "run-r1" / "truth.rrd").exists()
        for c in ("eval", "dist"):
            assert clients[c].get_dataset(DS).segment_ids() == []
            assert Registry.load(root, c).entries == {}


def test_cli_register_list_and_deregister(world, tmp_path: Path, capsys) -> None:
    root = tmp_path / "reg"
    run_dir = world["exports"]["run-0002"].parent.parent.parent
    assert cli.main(["--root", str(root), "register", "--catalog", "dist", "--run", str(run_dir)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["live"] is None and sorted(out["registered"]) == ["obs.uav-01", "obs.uav-02"]
    assert cli.main(["--root", str(root), "list", "--catalog", "dist"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["datasets"][DS] == ["run-0002/obs.uav-01", "run-0002/obs.uav-02"]
    assert cli.main(["--root", str(root), "deregister", "--file", str(world["exports"]["run-0002"].parent / "obs.uav-01.rrd")]) == 0
    capsys.readouterr()
    assert [k[2] for k in Registry.load(root, "dist").entries] == ["obs.uav-02"]
    bad = tmp_path / "bad"
    f = _obs_rrd(bad / "obs.uav-01.rrd", "run-bad", extra="/truth/fleet/uav-01")
    _export_json(bad, "run-bad", {"obs.uav-01": f})
    assert cli.main(["--root", str(root), "register", "--catalog", "dist", "--run", str(bad)]) == 1
    assert "refused" in json.loads(capsys.readouterr().err)["error"]


def test_pose_query_scale_three_runs(tmp_path: Path) -> None:
    """规模测量（缩小版，正式数字在 dmax 以 3 Run × 3 机 × 360 s 采集）：跨 3 个 Run 位姿查询 ≤ 1 s；分发 catalog 真值 0 行。"""
    r = perf.measure(tmp_path, runs=3, n_uav=2, duration_s=30.0, cam_hz=5.0, lidar_points=1000, hz=(10.0, 50.0), reps=3)
    for name, q in r["queries"].items():
        assert q["warm"]["max_s"] <= 1.0 and q["cold"]["max_s"] <= 1.0, (name, q)
    assert r["queries"]["poses_10hz"]["rows"] == 3 * 2 * 300 and r["queries"]["poses_50hz"]["rows"] == 3 * 2 * 1500
    assert r["dist_truth_rows"] == 0 and r["dist_truth_poses_rows"] == 0
    assert r["replay_eval"]["entries"] == 3 * 4 and r["replay_dist"]["entries"] == 3 * 2
