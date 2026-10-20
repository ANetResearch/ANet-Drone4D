"""D3-AC-18 r6 EC 对照（A10；`configs/bench.d3/ref6-l1.yaml`，`tools/bench/d3/r6_ec.py`）。

- 对照口径：6 个参考行与登记表一致、12 个种子、判据逐条对照 §11 原文（EC 均值差 ≤ 0.1；覆盖扫描 AO_real v0 ≤ 0.05 且 AO_pos ≥ 0.85）；
  `crop-handoff-001` 含 r6 的 4 类关键事件（`zone_pass` 即"车辆经过"），总体 EC 可直接对照（§24.5 F-02）；
- 6 行 run_spec 派生：契约有效、机数与链路与登记一致、track5_share 采用配置的起飞点；
- 汇总与判定：构造评测文件覆盖通过、EC 超差、AO 超限、缺运行、非 realistic 档、磁带不符各分支；
- 起飞点核对（世界包与已编译事件，`AWR_EVENTS_ROOT`）；
- 正式结果（`AWR_R6_EC_DIR=<r6_ec.py run 的输出目录>`，dmax 实测）：报告判定通过。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "sdk" / "drone4d" / "src"))
_spec = importlib.util.spec_from_file_location("r6_ec", ROOT / "tools" / "bench" / "d3" / "r6_ec.py")
r6 = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("r6_ec", r6)
_spec.loader.exec_module(r6)

from drone4d.baselines import REFERENCE6, check_reference_spec, reference_run_spec  # noqa: E402

CONFIG = ROOT / "configs/bench.d3/ref6-l1.yaml"
TAPE_SHA = "b1285649605e08efe8445da1e73f23a1a22f8eafaac310210e220706f6a08e54"


@pytest.fixture(scope="module")
def cfg() -> dict:
    pytest.importorskip("yaml")
    return r6.load_config(CONFIG)


# ------------------------------------------------------------------ 口径
def test_config_matches_ac18_text(cfg) -> None:
    assert cfg["ac"] == "D3-AC-18" and cfg["event"] == {"id": "crop-handoff-001", "version": 1}
    assert cfg["rows"] == list(REFERENCE6)                                     # 6 种参考策略，r6 表的行序
    assert len(cfg["seeds"]) == 12 and len(set(cfg["seeds"])) == 12          # × 12 种子
    assert cfg["obs_level"] == "L1" and cfg["physics"] == "realistic"
    c = cfg["criteria"]
    assert c == {"ec_mean_abs_diff_max": 0.1, "coverage_row": "lawn3_cover", "ao_real_max": 0.05, "ao_pos_min": 0.85}
    assert REFERENCE6[c["coverage_row"]].policy == "coverage_scan"
    assert cfg["expect"]["tape_content_sha256"] == TAPE_SHA


def test_event_has_all_r6_event_types(cfg) -> None:
    """crop-handoff-001 的 4 条计分声明映射后恰为 r6 的 4 类关键事件：r6 EC（4 事件均值）可与总体 EC 直接对照。"""
    yaml = pytest.importorskip("yaml")
    ev = yaml.safe_load((ROOT / "configs/events.d3/crop-handoff-001/event.yaml").read_text(encoding="utf-8"))
    types = [cfg["event_type_map"].get(a["type"], a["type"]) for a in ev["annotations"]]
    assert sorted(types) == sorted(r6.R6_TYPES)
    for r in REFERENCE6.values():
        assert set(r.r6["per_event"]) == set(r6.R6_TYPES)


def test_row_specs_derivation_and_contract(cfg) -> None:
    pytest.importorskip("jsonschema")
    from awr.agentsdk.schemas import bench_validator

    base = json.loads((ROOT / cfg["base_run_spec"]).read_text(encoding="utf-8"))
    specs = r6.row_specs(cfg)
    assert list(specs) == cfg["rows"]
    v = bench_validator("run_spec")
    for name, rs in specs.items():
        ref = REFERENCE6[name]
        assert not [e.message for e in v.iter_errors(rs)], name
        assert check_reference_spec(rs) == []
        assert rs["policy"] == {"kind": "reference", "name": name} and len(rs["fleet"]) == ref.n_uav and rs["comm"]["enabled"] is ref.comm
        assert rs["seeds"] == cfg["seeds"] and rs["obs_level"] == "L1" and rs["physics"] == "realistic"
        if name not in cfg["pads"]:
            assert rs == {**reference_run_spec(base, name), "seeds": cfg["seeds"]}
    five = [f["start"]["pos_enu_m"] for f in specs["track5_share"]["fleet"]]
    assert five == cfg["pads"]["track5_share"] and five[:3] == [f["start"]["pos_enu_m"] for f in base["fleet"]]
    added = r6.added_pads(cfg, specs)
    assert [p["pos"] for p in added] == [[80.0, -92.0], [80.0, -96.0]]


def test_config_rejects_bad_rows(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    txt = CONFIG.read_text(encoding="utf-8")
    p = tmp_path / "bad.yaml"
    p.write_text(txt.replace("rows: [hover1_plaza,", "rows: [hover9_x,"), encoding="utf-8")
    with pytest.raises(ValueError, match="REFERENCE6"):
        r6.load_config(p)
    p.write_text(txt.replace(", [80.0, -96.0, 0.0]]", "]"), encoding="utf-8")
    with pytest.raises(ValueError, match="pads"):
        r6.load_config(p)


# ------------------------------------------------------------------ 汇总与判定（构造评测文件）
def _ev(ec_events: dict[str, bool], *, ao_real: float, ao_pos: float, f1: float = 0.5) -> dict:
    evs = [{"id": f"e_{t}", "type": t, "captured": c, "coverage": 0.5 if c else 0.0, "by": ["uav-01"] if c else []} for t, c in ec_events.items()]
    ec = sum(ec_events.values()) / len(ec_events)
    return {"ec": {"value": ec, "coverage": ec / 2, "events": evs}, "excluded": [], "ranked_valid": True,
            "ao": {"ao_real": ao_real, "ao_pos": ao_pos, "ao_pos_los": ao_pos, "oracle": {"version": "ao-oracle-v0-grid@1"}},
            "eu": {"f1": f1}, "ce": {"gain": 0.0, "redundancy": 1.2, "flight_km": 1.0, "comm_kb": 0.0}}


def _fake_runs(out: Path, cfg: dict, *, tier: str = "realistic", tweak: dict | None = None) -> None:
    """按 r6 逐事件捕获率构造各行 12 个种子的评测：前 round(rate × 12) 个种子捕获该事件。"""
    inv = {v: k for k, v in cfg["event_type_map"].items()}
    for name in cfg["rows"]:
        ref = REFERENCE6[name]
        n = len(cfg["seeds"])
        for i, s in enumerate(cfg["seeds"]):
            cap = {inv.get(t, t): i < round(rate * n) for t, rate in ref.r6["per_event"].items()}
            ao = (0.0, 0.92) if name == "lawn3_cover" else (0.8, 1.0)
            doc = _ev(cap, ao_real=ao[0], ao_pos=ao[1])
            if tweak and name in tweak:
                doc = tweak[name](doc, i)
            d = out / "runs" / f"{name}-s{s}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "evaluation.json").write_text(json.dumps(doc))
            (d / "manifest.json").write_text(json.dumps({"tier": tier, "physics": tier}))


def _roots(tmp: Path, cfg: dict, sha: str = TAPE_SHA) -> tuple[Path, Path]:
    ev = tmp / "events" / cfg["event"]["id"] / "v1"
    ev.mkdir(parents=True)
    (ev / "tape.json").write_text(json.dumps({"content_sha256": sha}))
    w = tmp / "worlds" / "oryahovo-crop1"
    w.mkdir(parents=True)
    (w / "world.json").write_text(json.dumps({"contentVersion": cfg["expect"]["world_content_version"]}))
    return tmp / "events", tmp / "worlds"


def test_report_pass_on_r6_like_runs(tmp_path: Path, cfg) -> None:
    _fake_runs(tmp_path / "out", cfg)
    er, wr = _roots(tmp_path, cfg)
    doc = r6.report(cfg, tmp_path / "out", config_path=CONFIG, events_root=er, worlds_root=wr)
    assert doc["schema"] == r6.SCHEMA and doc["verdict"]["pass"] and doc["verdict"]["formal"]
    by = {r["name"]: r for r in doc["rows"]}
    for name, r in by.items():
        assert r["n_runs"] == 12 and r["ec_pass"] and abs(r["d_ec"]) <= 0.05, name   # 12 种子下逐事件取整后的残差
        assert set(r["per_event"]) == set(r6.R6_TYPES)
    assert by["track3_nocomm"]["per_event"]["board_vehicle"] == round(7 / 12, 4)
    assert doc["coverage"] == {"row": "lawn3_cover", "ao_real": 0.0, "ao_pos": 0.92, "ao_pos_los": 0.92, "pass": True}
    assert doc["inputs"] == {"tape_content_sha256": TAPE_SHA, "world_content_version": "7d8c8b895e55"} and doc["inputs_ok"]
    assert doc["config"]["sha256"] == r6.config_sha256(CONFIG)
    assert "PASS" in r6.format_table(doc)


def test_report_fail_branches(tmp_path: Path, cfg) -> None:
    def miss_all(doc, i):                       # hover3_anchors：各种子 EC 降 0.25 → 均值差 0.25 > 0.1
        e = next(x for x in doc["ec"]["events"] if x["captured"])
        e["captured"] = False
        doc["ec"]["value"] -= 0.25
        return doc

    def ao_hi(doc, i):                          # lawn3_cover：AO_real 0.06 > 0.05、AO_pos 0.84 < 0.85
        doc["ao"].update(ao_real=0.06, ao_pos=0.84)
        return doc

    out = tmp_path / "out"
    _fake_runs(out, cfg, tweak={"hover3_anchors": miss_all, "lawn3_cover": ao_hi})
    er, wr = _roots(tmp_path, cfg)
    doc = r6.report(cfg, out, events_root=er, worlds_root=wr)
    by = {r["name"]: r for r in doc["rows"]}
    assert not by["hover3_anchors"]["ec_pass"] and by["hover3_anchors"]["d_ec"] == -0.25
    assert all(r["ec_pass"] for n, r in by.items() if n != "hover3_anchors")
    assert not doc["verdict"]["ec_pass"] and not doc["verdict"]["ao_pass"] and not doc["verdict"]["pass"]
    assert "!" in r6.format_table(doc) and "FAIL" in r6.format_table(doc)

    out2 = tmp_path / "out2"                    # 缺运行、运动学开发档、磁带不符
    _fake_runs(out2, cfg, tier="kinematic")
    (out2 / "runs" / "track5_share-s12" / "evaluation.json").unlink()
    er2, wr2 = _roots(tmp_path / "b", cfg, sha="0" * 64)
    doc2 = r6.report(cfg, out2, events_root=er2, worlds_root=wr2)
    v = doc2["verdict"]
    assert doc2["missing_runs"] == ["track5_share-s12"] and not v["complete"]
    assert doc2["dev"] and not v["formal"] and doc2["physics_tiers"] == ["kinematic"]
    assert v["inputs_ok"] is False and not v["pass"]


def test_mean_sd_ci() -> None:
    assert r6.mean_sd_ci([]) == {"n": 0, "mean": None, "sd": None, "ci95": None}
    assert r6.mean_sd_ci([0.5]) == {"n": 1, "mean": 0.5, "sd": 0.0, "ci95": [0.5, 0.5]}
    m = r6.mean_sd_ci([0.0, 1.0] * 6)
    assert m["mean"] == 0.5 and m["sd"] == pytest.approx(0.5222, abs=1e-4)
    assert m["ci95"] == [pytest.approx(0.5 - 2.201 * 0.52223 / 12 ** 0.5, abs=1e-3), pytest.approx(0.5 + 2.201 * 0.52223 / 12 ** 0.5, abs=1e-3)]


# ------------------------------------------------------------------ 起飞点（世界包 + 已编译事件）
def test_added_pads_level_and_clear(cfg) -> None:
    world_dir = ROOT / "worlds" / "oryahovo-crop1"
    er = os.environ.get("AWR_EVENTS_ROOT")
    if not (world_dir / "world.json").exists():
        pytest.skip("[data] needs worlds/oryahovo-crop1")
    if not er or not (Path(er) / cfg["event"]["id"] / "v1" / "tape.bin").exists():
        pytest.skip("[optin] set AWR_EVENTS_ROOT=<dir holding crop-handoff-001/v1 compiled for the current world>")
    pytest.importorskip("open3d")
    world, tape = r6._open(Path(er), ROOT / "worlds", cfg)
    specs = r6.row_specs(cfg)
    pc = cfg["pad_check"]
    got = r6.check_pads(r6.added_pads(cfg, specs), world, tape, level_m=pc["level_m"], clear_m=pc["clear_m"])
    assert len(got) == 2 and all(p["ok"] for p in got), got
    # 沿基础机队间隔延长的缺省起飞点 (76,-90)、(76,-94) 跨在平台边缘：不满足判据（配置改点的依据）
    dflt = [{"row": "track5_share", "uav_id": f"uav-0{i + 4}", "pos": p} for i, p in enumerate(([76.0, -90.0], [76.0, -94.0]))]
    assert not any(p["ok"] for p in r6.check_pads(dflt, world, tape, level_m=pc["level_m"], clear_m=pc["clear_m"]))


# ------------------------------------------------------------------ 正式结果（dmax 实测目录）
def test_formal_r6_ec_report(cfg) -> None:
    d = os.environ.get("AWR_R6_EC_DIR")
    if not d:
        pytest.skip("[optin] set AWR_R6_EC_DIR=<r6_ec.py run output dir> to judge the formal runs")
    out = Path(d)
    er = Path(os.environ.get("AWR_EVENTS_ROOT", ROOT / "d3data/events"))
    doc = r6.report(cfg, out, config_path=CONFIG, events_root=er, worlds_root=ROOT / "worlds")
    print(r6.format_table(doc))
    assert doc["verdict"]["complete"] and doc["verdict"]["formal"], doc["missing_runs"]
    assert doc["verdict"]["inputs_ok"] is not False
    assert doc["verdict"]["ec_pass"], [(r["name"], r["d_ec"]) for r in doc["rows"]]
    assert doc["verdict"]["ao_pass"], doc["coverage"]
