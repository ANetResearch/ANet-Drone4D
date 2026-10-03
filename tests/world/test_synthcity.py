"""合成演示城市 synthcity（DEMO-W；ADR-077；M03-FR-066、FR-067；M03-AC-033 至 AC-036）。

- 生成器确定性：同一规格两次生成的坐标、法线、类别与规范字节流 sha256 逐字节一致；种子不同则不同；
- 内容：anet-classes@1 的九类都出现、地标塔与桅杆高度、范围与四角、法线为单位向量、点数接近目标；
- 构建：缩小点数的同一城市走 build_world 全流程（ingest → grid → tile → derive → package → validate --deep → publish），
  校验 0 错误 0 警告，坐标帧为单位阵、锚点与清单字段符合 ADR-077；两次独立构建的 contentVersion 与全部内容文件逐字节一致；
- `--missing`：生成器参数变化判定 raw_changed；缺原始数据且深圳未发布时 build --missing 自动生成 synthcity；
- 默认世界回退：M03 `resolve_default_world` 与 runtime `apply_world_fallback` 在同一组状态下给出相同结论；
- 生成耗时：完整规格（约 400 万点）生成 ≤ 15 s（G1 宽松上限，实测约 5 s）；完整构建 ≤ 60 s 为 perf 用例（make perf-world）；
- 已发布的 worlds/synthcity（make demo-world）与 `awr.datasets.synthcity.SYNTHCITY` 的事实一致。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from m03_common import REPO, WORLDS

from awr.world.ingest.synthetic import GENERATOR_VERSION, LANDMARKS, SyntheticAdapter, SynthSpec, generate, synth_specs
from awr.world.ingest.types import ConfigError, StageContext

SMALL = SynthSpec(target_points=300_000)
_skip_no_synth = pytest.mark.skipif(not (WORLDS / "synthcity" / "world.json").exists(),
                                    reason="worlds/synthcity 未生成（make demo-world）")


def needs_synth(fn):
    """需要已构建的 worlds/synthcity：needs_data 标记，缺失时 skip（AWR-18 §8.2，SHOW-CI）。"""
    return pytest.mark.needs_data(_skip_no_synth(fn))


@pytest.fixture(scope="module")
def small_cloud():
    return generate(SMALL)


# ---------------------------------------------------------------- 生成器


def test_generator_deterministic(small_cloud):
    b = generate(SMALL)
    assert b.sha256 == small_cloud.sha256 and b.nbytes == small_cloud.nbytes
    assert np.array_equal(b.xyz, small_cloud.xyz) and np.array_equal(b.normal, small_cloud.normal)
    assert np.array_equal(b.cls, small_cloud.cls)
    c = generate(replace(SMALL, seed=SMALL.seed + 1))
    assert c.sha256 != small_cloud.sha256


def test_generator_content(small_cloud):
    c = small_cloud
    P, N, C = c.xyz, c.normal, c.cls
    assert P.dtype == np.float64 and N.dtype == np.float32 and C.dtype == np.uint8
    assert abs(len(P) - SMALL.target_points) / SMALL.target_points < 0.08
    assert set(np.unique(C).tolist()) == {1, 2, 3, 4, 5, 6, 8, 9, 10}           # anet-classes@1 紧凑索引
    assert np.allclose(np.linalg.norm(N, axis=1), 1.0, atol=1e-5)
    assert np.array_equal(np.round(P, 3), P)                                     # mm 舍入
    H = SMALL.size_m / 2
    assert P[:, 0].min() == -H and P[:, 0].max() == H and P[:, 1].min() == -H and P[:, 1].max() == H
    a = LANDMARKS["anet-tower"]
    assert P[:, 2].max() == pytest.approx(a["mast_z"], abs=0.01)                 # 桅杆顶即最高点
    r = np.hypot(P[:, 0] - a["center"][0], P[:, 1] - a["center"][1])
    tower = (r < a["r_base"] + 0.5) & (C == 6)
    assert P[tower, 2].max() >= a["crown_z"] - 1.0                               # 圆柱塔冠 318 m
    b = LANDMARKS["step-tower"]
    near_b = (np.abs(P[:, 0] - b["center"][0]) < 10) & (np.abs(P[:, 1] - b["center"][1]) < 10) & (C == 5)
    assert np.isclose(P[near_b, 2], b["tiers"][-1][1], atol=0.01).sum() > 20      # 阶梯塔塔顶 286 m 屋面（另有屋顶设备）
    assert (C == 8).any() and P[C == 8, 2].max() < 0                             # 水面低于地面
    # 立面窗格纹理：立面点在窗格方向上的密度不均匀（窗框密、玻璃疏），按 0.5 m 条带计数的变异系数明显大于均匀采样
    fac = P[(C == 6) & (np.abs(P[:, 2] - 60) < 20)]
    hist = np.bincount(np.floor((fac[:, 2] - 40) / 0.5).astype(int))
    assert hist.std() / hist.mean() > 0.15


def test_synth_specs_config():
    specs = synth_specs()
    assert set(specs) == {"synthcity"}
    s = specs["synthcity"]
    assert s.name == "ANet Synthetic City" and s.size_m == 1200 and s.version == GENERATOR_VERSION
    p = s.params()
    assert {k: v for k, v in p.items() if k != "sourceSha256"} == {"generator": "awr.world.ingest.synthetic",
                                                                   "version": GENERATOR_VERSION, "seed": s.seed,
                                                                   "sizeM": 1200, "targetPoints": s.target_points}
    assert len(p["sourceSha256"]) == 16
    with pytest.raises(ConfigError):
        synth_specs({"synthetic": {"x": {"colour": 1}}})
    with pytest.raises(ConfigError):
        synth_specs({"synthetic": {"x": {"target_points": 10}}})


def test_full_generation_time():
    """生成耗时（G1 宽松上限 15 s；验收实测记录在 DEMO-W 报告）。"""
    t = time.perf_counter()
    c = generate(SynthSpec())
    dt = time.perf_counter() - t
    assert 3_000_000 <= len(c.xyz) <= 5_000_000
    assert dt <= 15.0, dt


# ---------------------------------------------------------------- 构建与校验


def _build(worlds: Path, spec: SynthSpec):
    from awr.world.package.build import build_world

    return build_world(SyntheticAdapter(spec), worlds, ctx=StageContext(quiet=True))


@pytest.fixture(scope="module")
def small_world(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthroot")
    (root / "scenarios" / "zones").mkdir(parents=True)
    old = {k: os.environ.get(k) for k in ("AWR_REPO_ROOT", "AWR_DATA_YAML", "AWR_WORLDPKG_YAML")}
    os.environ.update(AWR_REPO_ROOT=str(root), AWR_DATA_YAML=str(REPO / "configs" / "data.yaml"),
                      AWR_WORLDPKG_YAML=str(REPO / "configs" / "worldpkg.yaml"))
    try:
        w1, w2 = tmp_path_factory.mktemp("w1"), tmp_path_factory.mktemp("w2")
        r1 = _build(w1, SMALL)
        r2 = _build(w2, SMALL)
        return {"w1": w1 / "synthcity", "w2": w2 / "synthcity", "r1": r1, "r2": r2}
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_build_validate_deep(small_world):
    from awr.world.package.validate import validate_world

    assert small_world["r1"].exit_code == 0, small_world["r1"].message
    rep = validate_world(small_world["w1"], deep=True)
    assert rep.ok and not rep.errors and not rep.warnings, (rep.errors, rep.warnings)


def test_build_byte_identical(small_world):
    a, b = small_world["w1"], small_world["w2"]
    assert small_world["r1"].content_version == small_world["r2"].content_version
    wa, wb = json.loads((a / "world.json").read_text()), json.loads((b / "world.json").read_text())
    wa.pop("createdAt")
    wb.pop("createdAt")
    assert wa == wb
    for f in wa["files"]:
        assert (a / f["path"]).read_bytes() == (b / f["path"]).read_bytes(), f["path"]


def test_manifest_fields(small_world):
    d = small_world["w1"]
    w = json.loads((d / "world.json").read_text())
    c = json.loads((d / "coordinate.json").read_text())
    assert w["id"] == "synthcity" and w["name"] == "ANet Synthetic City"
    assert {"synthetic", "builtin", "redistributable"} <= set(w["tags"])
    ds = w["dataset"]
    assert ds["redistribution"] is True and ds["name"] == "ANet Synthetic City" and ds["sourceFiles"][0]["sha256"]
    assert w["generator"]["params"]["synthetic"] == SMALL.params()
    assert c["anchor"]["kind"] == "synthetic" and c["anchor"]["georeferenced"] is False
    assert c["anchor"]["label"].startswith("illustrative:") and "no real location" in c["anchor"]["label"]
    assert c["trueNorth"]["confidence"] == "exact" and c["scaleStatus"] == "assumed"
    assert c["source"]["kind"] == "simulation"
    # 生成帧即 world：包围盒中心在原点，原点 z 取 DTM 中位数（完整点数时恰为 0，缩小点数时在 mm 级）
    assert np.allclose(c["source"]["T_world_source"], np.eye(4), atol=0.01)
    assert c["extent"]["min"][:2] == [-600.0, -600.0] and c["extent"]["max"][:2] == [600.0, 600.0]
    # 类别由生成器给出（semantic = provided）：源点云类别流含植被、水面、路面与桥面
    rep = json.loads((d / "qa" / "report.json").read_text())
    assert rep["status"] == "pass"
    hist = json.loads((d / "geometry" / "pointcloud" / "source" / "source.json").read_text())["stats"]["class_histogram"]
    assert {"2", "3", "4", "8", "9", "10"} <= set(hist)


def test_missing_reason_synthetic(small_world, tmp_path):
    import shutil

    from awr.world.ingest.manifest import load_data_config
    from awr.world.package.build import missing_reason

    w = tmp_path / "worlds"
    shutil.copytree(small_world["w1"].parent, w, ignore=shutil.ignore_patterns(".locks", ".trash", ".staging"))
    data = load_data_config(REPO / "configs" / "data.yaml")
    zones = Path(os.environ.get("AWR_REPO_ROOT", REPO)) / "scenarios" / "zones" / "synthcity.zones.geojson"
    reason, info = missing_reason(w, "synthcity", raw_dir=tmp_path, data=data, synthetic=replace(SMALL, seed=1).params())
    assert reason == "raw_changed" and info["synthetic"]["want"]["seed"] == 1
    reason, _ = missing_reason(w, "synthcity", raw_dir=tmp_path, data=data, synthetic=SMALL.params())
    assert reason in (None, "zones_changed") and (reason is None) == (not zones.exists())


def test_build_missing_generates_fallback(tmp_path, monkeypatch):
    """缺原始数据、深圳未发布：build --missing 生成 synthcity（缩小点数的配置），六城报 RAW_MISSING。"""
    from awr.world.package.build import build_missing

    cfg = tmp_path / "worldpkg.yaml"
    base = (REPO / "configs" / "worldpkg.yaml").read_text(encoding="utf-8")
    cfg.write_text(base.replace("target_points: 4000000", "target_points: 300000"), encoding="utf-8")
    root = tmp_path / "root"
    (root / "scenarios" / "zones").mkdir(parents=True)
    (root / "configs").mkdir()
    (root / "configs" / "runtime.yaml").write_text((REPO / "configs" / "runtime.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("AWR_REPO_ROOT", str(root))
    monkeypatch.setenv("AWR_DATA_YAML", str(REPO / "configs" / "data.yaml"))
    monkeypatch.setenv("AWR_WORLDPKG_YAML", str(cfg))
    monkeypatch.delenv("AWR_WORLD", raising=False)
    worlds, raw = tmp_path / "worlds", tmp_path / "raw"
    raw.mkdir()
    res = build_missing(worlds, raw, jobs=1, ctx=StageContext(quiet=True))
    assert res["synthcity"].exit_code == 0 and res["synthcity"].published
    assert all(res[c].error_code == "RAW_MISSING" for c in ("shenzhen", "shanghai", "chicago"))
    assert (worlds / "synthcity" / "world.json").exists()
    # 第二次：synthcity 已是最新，不再生成
    res2 = build_missing(worlds, raw, jobs=1, ctx=StageContext(quiet=True))
    assert res2["synthcity"].exit_code == 0 and not res2["synthcity"].published
    # 有深圳世界时不自动生成（新目录，只放一个深圳 world.json 占位）
    w3 = tmp_path / "w3"
    (w3 / "shenzhen").mkdir(parents=True)
    (w3 / "shenzhen" / "world.json").write_text("{}", encoding="utf-8")
    from awr.world.ingest.manifest import load_data_config
    from awr.world.package.defaults import fallback_needed

    assert not fallback_needed(w3, raw, load_data_config())
    assert fallback_needed(tmp_path / "empty", raw, load_data_config())


# ---------------------------------------------------------------- 默认世界回退规则（M03 与 runtime 对拍）


@pytest.mark.parametrize("have,env,want", [
    ((), {}, ("shenzhen", "s1-shenzhen-facade")),
    (("synthcity",), {}, ("synthcity", "s0-synthcity-showcase")),
    (("shenzhen", "synthcity"), {}, ("shenzhen", "s1-shenzhen-facade")),
    (("synthcity",), {"AWR_WORLD": "shenzhen"}, ("shenzhen", "s1-shenzhen-facade")),
    (("synthcity", "chicago"), {"AWR_WORLD": "chicago", "AWR_SCENARIO": "free-chicago"}, ("chicago", "free-chicago")),
    (("synthcity",), {"AWR_SCENARIO": "free-synthcity"}, ("synthcity", "free-synthcity")),
])
def test_default_world_rule_matches_runtime(tmp_path, have, env, want):
    from awr.runtime.config import load_runtime_config
    from awr.world.package.defaults import resolve_default_world

    for wid in have:
        (tmp_path / wid).mkdir()
        (tmp_path / wid / "world.json").write_text("{}", encoding="utf-8")
    e = {"AWR_WORLDS_DIR": str(tmp_path), "AWR_SCENARIOS_DIR": str(REPO / "scenarios"), **env}
    dw = resolve_default_world(tmp_path, env=e, runtime_yaml=REPO / "configs" / "runtime.yaml")
    cfg = load_runtime_config(profile="demo", env=e, world_fallback=True)
    assert (dw.world, dw.scenario) == want
    assert (cfg.run.world, cfg.run.scenario) == want
    assert dw.fallback == bool(cfg.world_fallback_note)


def test_runtime_fallback_off_without_flag(tmp_path):
    from awr.runtime.config import load_runtime_config

    (tmp_path / "synthcity").mkdir()
    (tmp_path / "synthcity" / "world.json").write_text("{}", encoding="utf-8")
    cfg = load_runtime_config(profile="demo", env={"AWR_WORLDS_DIR": str(tmp_path)})
    assert cfg.run.world == "shenzhen" and cfg.world_fallback_note is None
    cfg = load_runtime_config(profile="demo", env={"AWR_WORLDS_DIR": str(tmp_path)}, argv=["run.world=shenzhen"], world_fallback=True)
    assert cfg.run.world == "shenzhen"
    cfg = load_runtime_config(profile="demo", env={"AWR_WORLDS_DIR": str(tmp_path)}, argv=["run.fallback_world=null"], world_fallback=True)
    assert cfg.run.world == "shenzhen"


# ---------------------------------------------------------------- 已发布的 worlds/synthcity


@needs_synth
def test_published_synthcity_facts():
    from awr.datasets.synthcity import SYNTHCITY
    from awr.world.package.validate import validate_world

    d = WORLDS / "synthcity"
    w = json.loads((d / "world.json").read_text())
    c = json.loads((d / "coordinate.json").read_text())
    assert c["extent"]["max"][2] == pytest.approx(SYNTHCITY.max_world_z_m, abs=0.5)
    assert w["render"]["nnMedianM"] == pytest.approx(SYNTHCITY.nn_median_m, rel=0.02)
    assert w["render"]["defaultColorMode"] == SYNTHCITY.default_color
    pc = next(L for L in w["layers"] if L["id"] == "pointcloud.visual")
    assert len(pc["roots"]) == SYNTHCITY.roots and max(r["depth"] for r in pc["roots"]) == SYNTHCITY.depth
    zones = json.loads((d / "semantic" / "zones.geojson").read_text())
    border = next(f for f in zones["features"] if f["properties"]["kind"] == "border")
    assert border["properties"]["max_z_m"] == pytest.approx(SYNTHCITY.border_max_z_m, abs=0.05)
    assert {f["id"] for f in zones["features"]} >= {z.zone_id for z in SYNTHCITY.zones}
    spec = synth_specs()["synthcity"]
    assert w["generator"]["params"]["synthetic"] == spec.params()
    assert validate_world(d, deep=True).ok


@pytest.mark.perf
def test_full_build_budget(tmp_path):
    """完整 synthcity 构建 ≤ 60 s（与六城单城门禁相同，ADR-033 性能运行协议：make perf-world）。"""
    t = time.perf_counter()
    res = _build(tmp_path, synth_specs()["synthcity"])
    assert res.exit_code == 0 and time.perf_counter() - t <= 60.0
