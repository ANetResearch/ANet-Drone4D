"""D3-AC-18 r6 EC 对照（A10；`configs/bench.d3/ref6-l1.yaml`，`tools/bench/d3/r6_ec.py`）。

- 对照口径：6 个参考行与登记表一致、12 个种子、判据逐条对照 §11 原文（覆盖扫描 AO_real v0 ≤ 0.05 且 AO_pos ≥ 0.85）；r6 对照条款按
  ADR-218（用户 2026-10-22 批准的放宽）为"同量级、排序一致"：覆盖扫描行 EC 均值与 AO_real 在 6 行中最低、track5_share EC 最高（均可并列），
  原文"EC 均值差 ≤ 0.1"保留为参考列与 `--rule original` 回退；
  `crop-handoff-001` 含 r6 的 4 类关键事件（`zone_pass` 即"车辆经过"），总体 EC 可直接对照（§24.5 F-02）；
- 6 行 run_spec 派生：契约有效、机数与链路与登记一致、track5_share 采用配置的起飞点；
- 观测设置（ADR-217 决策 (1)）：配置与 `REFERENCE6` 一致、派生规格带相机覆盖与 `policy.params.baseline`，运行清单与评测的相机逐运行核对；
- 汇总与判定：构造评测文件覆盖通过、EC 超差（ADR-218 下只计参考）、AO 超限、缺运行、非 realistic 档、磁带不符、观测设置不符各分支；
  排序判据（`rank_check`）的通过、并列与三个子条件各自不成立的分支；K10 实测数值（ADR-218 通过、原文 3/6）；命令行 `--rule`；
- 单个运行（`_job`，合成世界、运动学开发档）：运行清单记录相机覆盖与策略参数，评测 oracle 按覆盖相机计算；
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
sys.path.insert(0, str(ROOT / "tools" / "sdk" / "anet4d" / "src"))
_spec = importlib.util.spec_from_file_location("r6_ec", ROOT / "tools" / "bench" / "d3" / "r6_ec.py")
r6 = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("r6_ec", r6)
_spec.loader.exec_module(r6)

from anet4d.baselines import REFERENCE6, check_reference_spec, reference_run_spec  # noqa: E402

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
    obs = cfg["observation"]                                                   # ADR-217 决策 (1)：r6 §12.3 观测设置
    assert obs["camera_hfov_deg"] == 40.0 and r6.observation_problems(cfg) == []
    assert obs["params"]["lawn3_cover"] == {"alt_agl_m": 70.0}
    assert all(obs["params"][r] == {"alt_agl_m": 60.0, "gaze": "fixed"} for r in ("hover1_plaza", "hover3_anchors"))
    assert all(obs["params"][r] == {"alt_agl_m": 60.0, "track_alt_agl_m": 45.0} for r in ("track3_nocomm", "track3_share", "track5_share"))


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
        assert rs["policy"] == {"kind": "reference", "name": name, "params": {"baseline": cfg["observation"]["params"][name]}}
        assert len(rs["fleet"]) == ref.n_uav and rs["comm"]["enabled"] is ref.comm
        assert all(f["sensors"] == [{"sensor_id": "eo", "overrides": {"hfov_deg": 40.0}}] for f in rs["fleet"])
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
    p.write_text(txt.replace("camera_hfov_deg: 40.0", "camera_hfov_deg: 60.0"), encoding="utf-8")
    with pytest.raises(ValueError, match="observation"):
        r6.load_config(p)
    p.write_text(txt.replace("lawn3_cover: {alt_agl_m: 70.0}", "lawn3_cover: {alt_agl_m: 75.0}"), encoding="utf-8")
    with pytest.raises(ValueError, match="observation"):
        r6.load_config(p)


# ------------------------------------------------------------------ 汇总与判定（构造评测文件）
def _ev(ec_events: dict[str, bool], *, ao_real: float, ao_pos: float, f1: float = 0.5) -> dict:
    evs = [{"id": f"e_{t}", "type": t, "captured": c, "coverage": 0.5 if c else 0.0, "by": ["uav-01"] if c else []} for t, c in ec_events.items()]
    ec = sum(ec_events.values()) / len(ec_events)
    return {"ec": {"value": ec, "coverage": ec / 2, "events": evs}, "excluded": [], "ranked_valid": True,
            "ao": {"ao_real": ao_real, "ao_pos": ao_pos, "ao_pos_los": ao_pos,
                   "oracle": {"version": "ao-oracle-v0-grid@1", "camera_models": ["d4d_eo1080@1+hfov40"]}},
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
            man = {"tier": tier, "physics": tier, "camera": {"model_ref": "d4d_eo1080@1+hfov40", "hfov_deg": 40.0},
                   "policy": {"kind": "reference", "name": name, "params": {"baseline": dict(ref.params)}}}
            if tweak and f"{name}:manifest" in tweak:
                man = tweak[f"{name}:manifest"](man, i)
            (d / "manifest.json").write_text(json.dumps(man))


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
        assert r["n_runs"] == 12 and r["ec_diff_ok"] and abs(r["d_ec"]) <= 0.05, name   # 12 种子下逐事件取整后的残差
        assert r["observation_ok"] and r["observation_problems_n"] == 0, name
        assert set(r["per_event"]) == set(r6.R6_TYPES)
    assert by["track3_nocomm"]["per_event"]["board_vehicle"] == round(7 / 12, 4)
    assert doc["coverage"] == {"row": "lawn3_cover", "ao_real": 0.0, "ao_pos": 0.92, "ao_pos_los": 0.92, "pass": True}
    assert doc["inputs"] == {"tape_content_sha256": TAPE_SHA, "world_content_version": "7d8c8b895e55"} and doc["inputs_ok"]
    assert doc["config"]["sha256"] == r6.config_sha256(CONFIG)
    v = doc["verdict"]
    assert doc["rule"] == v["rule"] == "adr218" and v["ec_pass"] and v["ec_rank_pass"] and v["ec_diff_pass"]
    assert (v["ec_diff_ok_rows"], v["n_rows"]) == (6, 6)
    rk = doc["ranking"]                         # r6 表本身满足排序：lawn3 0.25 最低，track5 1.00 最高
    assert rk["pass"] and rk["lowest_row"] == "lawn3_cover" and rk["highest_row"] == "track5_share" and rk["problems"] == []
    assert by["lawn3_cover"]["ec_rank"] == max(r["ec_rank"] for r in by.values()) and by["track5_share"]["ec_rank"] == 1
    assert by["lawn3_cover"]["ao_real_rank"] == 1
    t = r6.format_table(doc)
    assert "PASS" in t and "ranking (ADR-218)" in t and "in 6/6 rows" in t and "verdict [adr218]" in t


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
    assert not by["hover3_anchors"]["ec_diff_ok"] and by["hover3_anchors"]["d_ec"] == -0.25
    assert all(r["ec_diff_ok"] for n, r in by.items() if n != "hover3_anchors")
    v = doc["verdict"]                          # ADR-218：超差只计参考，排序仍成立（hover3 0.50 > lawn3 0.25）；AO 超限 → 不通过
    assert v["ec_pass"] and v["ec_rank_pass"] and not v["ec_diff_pass"] and v["ec_diff_ok_rows"] == 5
    assert not v["ao_pass"] and not v["pass"]
    assert "!" in r6.format_table(doc) and "FAIL" in r6.format_table(doc)
    orig = r6.report(cfg, out, events_root=er, worlds_root=wr, rule="original")   # 回退口径：逐行差值判定
    assert orig["verdict"]["rule"] == "original" and not orig["verdict"]["ec_pass"] and orig["verdict"]["ec_rank_pass"]
    with pytest.raises(ValueError, match="rule"):
        r6.report(cfg, out, events_root=er, worlds_root=wr, rule="r6")

    out2 = tmp_path / "out2"                    # 缺运行、运动学开发档、磁带不符
    _fake_runs(out2, cfg, tier="kinematic")
    (out2 / "runs" / "track5_share-s12" / "evaluation.json").unlink()
    er2, wr2 = _roots(tmp_path / "b", cfg, sha="0" * 64)
    doc2 = r6.report(cfg, out2, events_root=er2, worlds_root=wr2)
    v = doc2["verdict"]
    assert doc2["missing_runs"] == ["track5_share-s12"] and not v["complete"]
    assert doc2["dev"] and not v["formal"] and doc2["physics_tiers"] == ["kinematic"]
    assert v["inputs_ok"] is False and not v["pass"]


def test_report_observation_mismatch(tmp_path: Path, cfg) -> None:
    """观测设置不符（标称相机运行、旧 A10 的无参数运行、评测 oracle 相机不符）→ observation_ok = False，判不通过。"""
    def nominal(man, i):                        # hover1_plaza 种子 1：标称相机（无 camera）
        if i == 0:
            man.pop("camera")
        return man

    def no_params(man, i):                      # lawn3_cover：旧 A10 运行（策略只有名称）
        man["policy"] = {"kind": "reference", "name": "lawn3_cover"}
        return man

    def oracle60(doc, i):
        if i == 1:
            doc["ao"]["oracle"]["camera_models"] = ["d4d_eo1080@1"]
        return doc

    out = tmp_path / "out"
    _fake_runs(out, cfg, tweak={"hover1_plaza:manifest": nominal, "lawn3_cover:manifest": no_params, "track5_share": oracle60})
    er, wr = _roots(tmp_path, cfg)
    doc = r6.report(cfg, out, events_root=er, worlds_root=wr)
    probs = doc["observation_problems"]
    assert any(p.startswith("hover1_plaza-s1: camera None") for p in probs)
    assert sum(p.startswith("lawn3_cover-s") and "policy.params.baseline None" in p for p in probs) == 12
    assert any(p.startswith("track5_share-s2: AO oracle camera ['d4d_eo1080@1']") for p in probs)
    v = doc["verdict"]
    assert v["ec_pass"] and v["ao_pass"] and v["observation_ok"] is False and not v["pass"]
    assert "observation False" in r6.format_table(doc)
    by = {r["name"]: r for r in doc["rows"]}    # 逐行 observation_ok
    assert [n for n, r in by.items() if not r["observation_ok"]] == ["hover1_plaza", "lawn3_cover", "track5_share"]
    assert (by["hover1_plaza"]["observation_problems_n"], by["lawn3_cover"]["observation_problems_n"]) == (1, 12)


# ------------------------------------------------------------------ ADR-218 排序判据
# K10 实测（dmax，2026-10-21 19:49–20:32 UTC，72 个运行）：各行 EC 均值与覆盖扫描行 AO_real（docs/05 ADR-218 实测段）
K10_EC = {"hover1_plaza": 0.75, "hover3_anchors": 1.0, "lawn3_cover": 0.25, "track3_nocomm": 0.979, "track3_share": 1.0, "track5_share": 1.0}
K10_AO = {"hover1_plaza": 0.8, "hover3_anchors": 1.0, "lawn3_cover": 0.0, "track3_nocomm": 0.933, "track3_share": 0.983, "track5_share": 1.0}


def _rows(ec: dict, ao: dict) -> list[dict]:
    return [{"name": n, "ec": {"mean": ec[n]}, "ao_real": {"mean": ao.get(n)}} for n in ec]


def test_rank_check_k10_numbers() -> None:
    """K10 数值：lawn3_cover 的 EC（0.25）与 AO_real（0.000）最低，track5_share 的 EC（1.000）最高并与两行并列 → 通过。"""
    rk = r6.rank_check(_rows(K10_EC, K10_AO), lowest_row="lawn3_cover")
    assert rk["pass"] and rk["problems"] == [] and rk["highest_row"] == r6.RANK_HIGHEST_ROW == "track5_share"
    assert rk["ec_lowest"] == {"row": "lawn3_cover", "value": 0.25, "others_bound": 0.75, "tied_with": [], "ok": True}
    assert rk["ao_real_lowest"]["ok"] and rk["ao_real_lowest"]["others_bound"] == 0.8
    assert rk["ec_highest"]["tied_with"] == ["hover3_anchors", "track3_share"] and rk["ec_highest"]["ok"]
    assert rk["ec_rank"] == {"hover1_plaza": 3, "hover3_anchors": 1, "lawn3_cover": 4, "track3_nocomm": 2, "track3_share": 1, "track5_share": 1}
    assert rk["ao_real_rank"]["lawn3_cover"] == 1 and rk["ao_real_rank"]["track5_share"] == rk["ao_real_rank"]["hover3_anchors"]


def test_rank_check_fail_and_tie_branches() -> None:
    lo = "lawn3_cover"
    ec_tie = {**K10_EC, "hover1_plaza": 0.25}                                 # 最低并列：通过
    assert r6.rank_check(_rows(ec_tie, K10_AO), lowest_row=lo)["ec_lowest"]["tied_with"] == ["hover1_plaza"]
    assert r6.rank_check(_rows(ec_tie, K10_AO), lowest_row=lo)["pass"]
    bad = r6.rank_check(_rows({**K10_EC, "hover1_plaza": 0.2}, K10_AO), lowest_row=lo)          # 覆盖扫描行 EC 不是最低
    assert not bad["pass"] and not bad["ec_lowest"]["ok"] and bad["ao_real_lowest"]["ok"] and bad["ec_highest"]["ok"]
    assert bad["problems"] == ["lawn3_cover: EC lowest fails (0.25 vs 0.2)"]
    bad = r6.rank_check(_rows(K10_EC, {**K10_AO, "lawn3_cover": 0.9}), lowest_row=lo)           # AO_real 不是最低
    assert not bad["pass"] and not bad["ao_real_lowest"]["ok"] and bad["ec_lowest"]["ok"]
    bad = r6.rank_check(_rows({**K10_EC, "track5_share": 0.98}, K10_AO), lowest_row=lo)         # track5_share 不是最高
    assert not bad["pass"] and not bad["ec_highest"]["ok"] and bad["ec_highest"]["others_bound"] == 1.0
    bad = r6.rank_check(_rows({**K10_EC, "track3_share": None}, K10_AO), lowest_row=lo)         # 缺运行的行：不通过
    assert not bad["pass"] and bad["problems"] == ["track3_share: no runs"] and bad["ec_rank"]["track3_share"] is None
    bad = r6.rank_check(_rows({k: v for k, v in K10_EC.items() if k != "track5_share"}, K10_AO), lowest_row=lo)
    assert not bad["pass"] and "track5_share: not a reported row" in bad["problems"]
    custom = r6.rank_check(_rows(K10_EC, K10_AO), lowest_row=lo, highest_row="hover1_plaza")   # 配置可改最高行
    assert not custom["pass"] and not custom["ec_highest"]["ok"]


def _k10_tweak(name: str):
    """把 r6 样式的构造评测改成 K10 的各行 EC：前 round(EC × 12 × 4) 个事件捕获（track3_nocomm 11 个种子全捕获、1 个种子 3/4）。"""
    def f(doc, i):
        target = 12 * 4 * K10_EC[name]
        k = round(target)
        n_cap = min(4, max(0, k - 4 * i))
        for j, e in enumerate(doc["ec"]["events"]):
            e["captured"] = j < n_cap
        doc["ec"]["value"] = n_cap / 4
        doc["ao"]["ao_real"] = K10_AO[name]
        if name == "lawn3_cover":
            doc["ao"]["ao_pos"] = 0.976
        return doc
    return f


def test_report_k10_like_runs_adr218_pass_original_fail(tmp_path: Path, cfg) -> None:
    """K10 实测各行 EC：ADR-218 排序判据通过；原文逐行差 ≤ 0.1 为 3/6（参考），`--rule original` 判不通过；表中逐行给出
    EC、r6、差值、名次与观测设置核对。"""
    out = tmp_path / "out"
    _fake_runs(out, cfg, tweak={n: _k10_tweak(n) for n in cfg["rows"]})
    er, wr = _roots(tmp_path, cfg)
    doc = r6.report(cfg, out, events_root=er, worlds_root=wr)
    by = {r["name"]: r for r in doc["rows"]}
    assert {n: r["ec"]["mean"] for n, r in by.items()} == {**K10_EC, "track3_nocomm": round(47 / 48, 4)}
    assert [n for n, r in by.items() if r["ec_diff_ok"]] == ["lawn3_cover", "track3_nocomm", "track5_share"]
    assert {n: r["d_ec"] for n, r in by.items()} == {"hover1_plaza": 0.25, "hover3_anchors": 0.25, "lawn3_cover": 0.0,
                                                     "track3_nocomm": 0.0792, "track3_share": 0.12, "track5_share": 0.0}
    v = doc["verdict"]
    assert v["pass"] and v["ec_pass"] and v["ec_rank_pass"] and not v["ec_diff_pass"] and (v["ec_diff_ok_rows"], v["n_rows"]) == (3, 6)
    assert doc["coverage"]["ao_real"] == 0.0 and doc["coverage"]["ao_pos"] == 0.976 and v["ao_pass"]
    t = r6.format_table(doc)
    assert "in 3/6 rows" in t and "tied with hover3_anchors, track3_share" in t and t.rstrip().endswith("PASS")
    assert t.count("+0.250!") == 2 and "+0.120!" in t
    orig = r6.report(cfg, out, events_root=er, worlds_root=wr, rule="original")
    assert not orig["verdict"]["pass"] and not orig["verdict"]["ec_pass"] and orig["verdict"]["ao_pass"]


def test_cli_report_rule(tmp_path: Path, cfg, capsys) -> None:
    """命令行 `report --no-pads [--rule original]`：写出 r6_ec.json、打印对照表，退出码随口径。"""
    out = tmp_path / "out"
    _fake_runs(out, cfg, tweak={n: _k10_tweak(n) for n in cfg["rows"]})
    er, wr = _roots(tmp_path, cfg)
    base = ["report", "--config", str(CONFIG), "--out", str(out), "--events-root", str(er), "--worlds-root", str(wr), "--no-pads"]
    assert r6.main(base) == 0
    doc = json.loads((out / "r6_ec.json").read_text(encoding="utf-8"))
    assert doc["rule"] == "adr218" and doc["verdict"]["pass"] and doc["pads"] is None
    assert "verdict [adr218]" in capsys.readouterr().out
    assert r6.main([*base, "--rule", "original"]) == 1
    assert json.loads((out / "r6_ec.json").read_text(encoding="utf-8"))["verdict"]["rule"] == "original"


# ------------------------------------------------------------------ 单个运行（合成世界、运动学开发档）
@pytest.fixture(scope="module")
def synth_event(tmp_path_factory):
    import importlib.util as ilu

    sp = ilu.spec_from_file_location("_env_conftest_r6", ROOT / "tests/d3/env/conftest.py")
    mod = ilu.module_from_spec(sp)
    sp.loader.exec_module(mod)
    yield from mod.synth_event_at(tmp_path_factory)


def test_job_records_observation_setting(synth_event, tmp_path: Path, cfg) -> None:
    """`_job` 以参考行规格运行：运行清单记录 HFOV 40° 相机与 `policy.params.baseline`，agent 配置的相机内参为覆盖值，
    评测 oracle 按覆盖相机；报告的逐运行核对通过，hover 行固定注视（云台目标只有一个）。"""
    w, ev = synth_event
    base = json.loads((ROOT / cfg["base_run_spec"]).read_text(encoding="utf-8"))
    base = {**base, "world": {"id": w.name}, "event": {"id": ev.parent.name, "version": 1},
            "fleet": [{**base["fleet"][0], "start": {"kind": "pose", "pos_enu_m": [-10.0, 50.0, 0.0], "yaw_deg": 90.0}}]}
    rs = reference_run_spec(base, "hover1_plaza")
    job = {"spec": rs, "policy": "hover1_plaza", "seed": 3, "out": str(tmp_path), "tag": "t", "physics": "kinematic", "gz": {},
           "worlds_root": str(w.parent), "events_root": str(ev.parent.parent), "agents": "inproc", "duration_s": 60.0}
    r = r6._job(job)
    rd = tmp_path / "runs" / "hover1_plaza-s3"
    assert not r["skipped"] and r["run_dir"] == str(rd)
    man = json.loads((rd / "manifest.json").read_text())
    assert man["camera"]["model_ref"] == "d4d_eo1080@1+hfov40" and man["camera"]["hfov_deg"] == 40.0
    assert man["policy"] == {"kind": "reference", "name": "hover1_plaza", "params": {"baseline": {"alt_agl_m": 60.0, "gaze": "fixed"}}}
    acfg = json.loads((rd / "agents/uav-01/config.json").read_text())
    assert next(s for s in acfg["sensors"] if s["kind"] == "camera")["K"][0] == pytest.approx(2637.6, abs=0.1)
    ev_doc = json.loads((rd / "evaluation.json").read_text())
    assert r6.observation_check("hover1_plaza", rd.name, r6._run_inputs(rd), ev_doc, cfg) == []
    looks = {tuple(json.loads(x)["act"]["gimbal"]["target_enu_m"][:2]) for x in (rd / "agents/uav-01/acts.jsonl").read_text().splitlines()
             if json.loads(x)["act"].get("gimbal")}
    assert len(looks) == 1                                                       # 固定注视
    assert r6._job(job)["skipped"]                                               # 已有 evaluation.json：断点续跑
    with pytest.raises(ValueError, match="is not row"):
        r6._job({**job, "policy": "hover3_anchors"})


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
    assert doc["verdict"]["inputs_ok"] is not False and doc["verdict"]["observation_ok"], doc["observation_problems"]
    assert doc["verdict"]["ec_pass"], doc["ranking"]["problems"]                # ADR-218 排序判据
    assert doc["verdict"]["ao_pass"], doc["coverage"]
