"""FAST-LIVO2 RPE100 矩阵与 HKisland03 回归（D3-AC-14、D3-AC-47；`tools/bench/d3/slam_matrix.py`）。

1. 矩阵规格：两组轨迹 × （主矩阵 clean 1 + realistic 5、8 m/s 单一位姿与 11 节点、native 节点、r1_mid 相位）共 24 个算例；
   算例名与 `slam_traj.case_name` 一致；已有算例按轨迹核对（PoseHistory sha256、节点、相位、航速），不一致记 stale 不复用。
2. 算例生成按（轨迹、节点、相位）分组调用 `build_cases`；运行：`slam_eval run` 命令行（`--cpus`、`--require-regress`、容器前缀）、
   已完成跳过、未完成旧输出改名保留。
3. 判定（ADR-215 冻结表）：主组 LIVO 的 clean / realistic 条款（区间 [0.07%, 0.18%]）、8 m/s ATE 比 ≥ 5（单一位姿发散计为满足）、
   HKisland03 ≤ 1.2 m（资源 ≥ 32 GB、8 核另报）、版本字段、运行有效性（处理帧比例）；任一条不通过或缺样本时总结论不通过或未定；
   native / r1_mid 对比；Markdown 表格。
4. 锚定点：首个估计带初始化姿态瞬态时，以事件零点重新锚定消除整体旋转；锚定坐标下的地图到网格距离随之回到真值；旧运行
   （首个估计锚定）的地图条款按 `reanchor.json` 的事件零点数值判定，r1 口径另报不计入。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools" / "bench" / "d3"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
_spec = importlib.util.spec_from_file_location("d3_slam_matrix", TOOLS / "slam_matrix.py")
sm = importlib.util.module_from_spec(_spec)
sys.modules["d3_slam_matrix"] = sm
_spec.loader.exec_module(sm)

from slam_eval.traj import R_to_quat, Tum, save_tum  # noqa: E402

T0 = 1_760_000_000.0
FRAMES = 1599


# ---------------------------------------------------------------------------------------------------------------- 夹具
def write_traj(root: Path, name: str, source: str, speed: float, sha: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "traj.json").write_text(json.dumps({"trajectory_source": source, "plan": {"speed_mps": speed}, "pose_history": {"sha256": sha}}))
    return d


@pytest.fixture
def trajs(tmp_path) -> Path:
    root = tmp_path / "traj"
    for g in ("gz_lockstep", "fast"):
        write_traj(root, g, g, 6.0, f"sha-{g}")
        write_traj(root, g + "_v8", g, 8.0, f"sha-{g}-v8")
    return root


def write_case(root: Path, name: str, *, source: str, noise: str, seed: int, pn: str = "11", phase: str = "tick_end", speed: float = 6.0,
               sha: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "sim.bag").write_bytes(b"bag")
    (d / "case.json").write_text(json.dumps({"name": name, "bag": "sim.bag", "noise_profile": noise, "seed": seed, "pose_nodes": pn,
                                             "slam_cam_phase": phase, "speed_mps": speed, "trajectory_source": source,
                                             "trajectory": {"pose_history_sha256": sha or f"sha-{source}"}}))
    return d


def run_rec(mode: str, k: int, rpe: float, ate: float, acc: float, processed: int = FRAMES, ok: bool = True,
            anchor: str | None = "event0") -> dict:
    """单次运行记录；`anchor = None` 模拟 ADR-215 之前以首个估计锚定评测的运行（`traj` 无 `anchor`）。"""
    return {"run": f"{mode}-r{k}", "mode": mode, "repeat": k, "ok": ok, "code": 0 if ok else 684, "why": None if ok else "oom_kill",
            "traj": {"rpe100_pct": rpe, "rpe100_median_pct": rpe * 0.7, "rpe100_max_pct": rpe * 2, "ate_aligned_rmse_m": ate,
                     "drift_max_m": 0.4, "drift_final_m": 0.1, **({"anchor": {"mode": anchor}} if anchor else {})},
            "map": {"acc_mean_m": acc, "fscore@0.2": 0.6}, "map_aligned": {"acc_mean_m": 0.03, "fscore@0.2": 0.9},
            "per_frame": {"lio": {"n": processed}}, "resources": {"rss_peak_gb": 2.8, "cpu_cores_equiv": 3.0},
            "r_map": {"r_map_slope": 26_000.0, "r_map_peak": 11_000.0}}


def mdoc(source: str, noise: str, seed: int, *, pn: str = "11", phase: str = "tick_end", speed: float = 6.0, livo=(), lio=(),
         image: str = "sha256:img", processed: int = FRAMES, anchor: str | None = "event0") -> dict:
    """一个算例的 metrics.json：livo / lio 为 [(rpe100_pct, ate_m, map_m)]（每项一次重复）。"""
    name = f"{source}-{noise}-s{seed}-{pn}-{phase}-v{speed:g}"
    runs = [run_rec(m, k, *v, processed=processed, anchor=anchor) for m, vals in (("livo", livo), ("lio", lio))
            for k, v in enumerate(vals, 1)]
    return {"case": {"name": name, "dir": f"/cases/{name}", "noise_profile": noise, "seed": seed, "trajectory_source": source,
                     "pose_nodes": pn, "slam_cam_phase": phase, "speed_mps": speed, "reflectance_model": "sem_table_v1",
                     "noise_table_version": "d3-noise-1", "world": "smalltown-01"},
            "image": {"tag": "d3-livo:dev", "id": image}, "contract": {"lidar_frames": FRAMES}, "status": "completed", "runs": runs}


def passing_docs(group: str = "gz_lockstep", *, clean_map: float = 0.10, processed: int = FRAMES, anchor: str | None = "event0") -> list[dict]:
    c3 = [(0.08, 0.10, clean_map)] * 3
    docs = [mdoc(group, "clean", 1, livo=c3, lio=c3, processed=processed, anchor=anchor)]
    for s in range(1, 6):
        r3 = [(0.09 + 0.01 * s, 0.3, 0.2)] * 3                             # 中位 0.12%，在 ADR-215 区间 [0.07%, 0.18%] 内
        docs.append(mdoc(group, "realistic", s, livo=r3, lio=r3, processed=processed))
    docs.append(mdoc(group, "clean", 1, pn="single", speed=8.0, livo=[(0.5, 0.60, 0.4)] * 3, lio=[(0.5, 0.40, 0.4)] * 3))
    docs.append(mdoc(group, "clean", 1, speed=8.0, livo=[(0.09, 0.10, 0.1)] * 3, lio=[(0.09, 0.10, 0.1)] * 3))
    for noise in ("clean", "realistic"):
        docs.append(mdoc(group, noise, 1, pn="native", livo=[(0.07, 0.09, 0.1)] * 3, lio=[(0.07, 0.09, 0.1)] * 3))
        docs.append(mdoc(group, noise, 1, phase="r1_mid", livo=[(0.12, 0.12, 0.1)] * 3, lio=[(0.08, 0.10, 0.1)] * 3))
    return docs


REGRESS_OK = {"ok": True, "ate": {"ate_rmse_m": 1.04}, "image": {"tag": "d3-livo:dev", "id": "sha256:img"}, "created_utc": "x"}


# ---------------------------------------------------------------------------------------------------------------- 规格
def test_matrix_specs_cover_ac14_and_ac47_blocks():
    specs = sm.matrix_specs()
    assert len(specs) == 24
    by = {b: [s for s in specs if s["block"] == b] for b in sm.BLOCKS}
    assert len(by["main"]) == 12 and len(by["v8"]) == 4 and len(by["nodes"]) == 4 and len(by["phase"]) == 4
    main_gz = [s for s in by["main"] if s["group"] == "gz_lockstep"]
    assert sorted((s["noise"], s["seed"]) for s in main_gz) == [("clean", 1)] + [("realistic", k) for k in range(1, 6)]
    assert all(s["pose_nodes"] == "11" and s["cam_phase"] == "tick_end" for s in main_gz)
    assert {(s["traj"], s["pose_nodes"]) for s in by["v8"]} == {(g + "_v8", pn) for g in ("gz_lockstep", "fast") for pn in ("single", "11")}
    assert {s["pose_nodes"] for s in by["nodes"]} == {"native"} and {s["cam_phase"] for s in by["phase"]} == {"r1_mid"}
    assert len(sm.matrix_specs(groups=("fast",), blocks=("v8",))) == 2


def test_resolve_names_existing_and_stale_cases(tmp_path, trajs):
    sl2 = tmp_path / "sl2cases"
    write_case(sl2, "gz_lockstep-clean-s1", source="gz_lockstep", noise="clean", seed=1)
    write_case(sl2, "fast-clean-s1", source="fast", noise="clean", seed=1, sha="sha-old")                 # 轨迹已重飞：不复用
    write_case(sl2, "gz_lockstep-realistic-s2", source="gz_lockstep", noise="realistic", seed=2, pn="native")   # 字段不符
    specs = sm.resolve(sm.matrix_specs(), trajs, [sl2, tmp_path / "out" / "cases"], tmp_path / "out" / "cases")
    by = {s["name"]: s for s in specs}
    assert len(by) == 24
    for n in ("gz_lockstep-clean-s1-pnsingle-v8", "gz_lockstep-clean-s1-v8", "fast-clean-s1-pnnative", "fast-realistic-s1-r1_mid"):
        assert n in by
    assert by["gz_lockstep-clean-s1"]["exists"] and by["gz_lockstep-clean-s1"]["dir"] == str(sl2 / "gz_lockstep-clean-s1")
    assert not by["fast-clean-s1"]["exists"] and by["fast-clean-s1"]["stale"]["fields"] == ["pose_history_sha256"]
    assert by["fast-clean-s1"]["dir"] == str(tmp_path / "out" / "cases" / "fast-clean-s1")
    assert by["gz_lockstep-realistic-s2"]["stale"]["fields"] == ["pose_nodes"]
    assert by["gz_lockstep-clean-s1-v8"]["speed_mps"] == 8.0 and by["gz_lockstep-clean-s1-v8"]["traj"] == "gz_lockstep_v8"
    doc = sm.plan_doc(specs, tmp_path / "out")
    assert doc["counts"] == {"cases": 24, "existing": 1, "stale": 2, "completed": 0}
    with pytest.raises(FileNotFoundError):
        sm.resolve(sm.matrix_specs(), tmp_path / "nowhere", [], tmp_path)


def test_cli_plan_filters_blocks_before_reading_trajectories(tmp_path):
    root = tmp_path / "traj"
    write_traj(root, "gz_lockstep_v8", "gz_lockstep", 8.0, "sha")                  # 只有 8 m/s 轨迹：v8 区块可单独解析
    out = tmp_path / "out"
    assert sm.main(["plan", "--traj-root", str(root), "--out", str(out), "--group", "gz_lockstep", "--block", "v8"]) == 0
    names = [c["name"] for c in json.loads((out / "plan.json").read_text())["cases"]]
    assert names == ["gz_lockstep-clean-s1-pnsingle-v8", "gz_lockstep-clean-s1-v8"]
    assert sm.main(["plan", "--traj-root", str(root), "--out", str(out)]) == 2                              # 缺 6 m/s 轨迹


def test_build_missing_groups_by_trajectory_nodes_and_phase(tmp_path, trajs):
    out = tmp_path / "cases"
    specs = sm.resolve(sm.matrix_specs(groups=("gz_lockstep",)), trajs, [], out)
    calls = []

    def builder(traj_dir, world, o, sets, *, pose_nodes, cam_phase, camera, max_frames, log):
        calls.append((Path(traj_dir).name, pose_nodes, cam_phase, list(sets)))
        return {"cases": {f"{Path(traj_dir).name}-{pose_nodes}-{cam_phase}-{n}{s}": {"frames": 3} for n, s in sets}}

    res = sm.build_missing(specs, tmp_path / "world", out, builder=builder, log=lambda m: None)
    assert sorted(calls) == sorted([
        ("gz_lockstep", "11", "tick_end", [("clean", 1)] + [("realistic", k) for k in range(1, 6)]),
        ("gz_lockstep_v8", "single", "tick_end", [("clean", 1)]), ("gz_lockstep_v8", "11", "tick_end", [("clean", 1)]),
        ("gz_lockstep", "native", "tick_end", [("clean", 1), ("realistic", 1)]),
        ("gz_lockstep", "11", "r1_mid", [("clean", 1), ("realistic", 1)])])
    assert len(res) == 12 and (out / "cases-gz_lockstep_v8-pnsingle-tick_end.json").is_file()


# ---------------------------------------------------------------------------------------------------------------- 运行
def test_slam_eval_argv():
    argv = sm.slam_eval_run_argv(Path("/c/x"), Path("/o/x"), cpus=4, require_regress="/o/hk/regress.json", parallel=6, python="python3")
    assert argv[:3] == ["python3", str(TOOLS / "slam_eval"), "run"]
    for k, v in (("--case", "/c/x"), ("--out", "/o/x"), ("--modes", "livo,lio"), ("--repeats", "3"), ("--rate", "4"), ("--parallel", "6"),
                 ("--name-prefix", "SL3"), ("--cpus", "4"), ("--require-regress", "/o/hk/regress.json"), ("--image", "d3-livo:dev")):
        assert argv[argv.index(k) + 1] == v
    assert "--cpus" not in sm.slam_eval_run_argv(Path("/c"), Path("/o"))
    rg = sm.slam_eval_regress_argv(Path("/b/HK.bag"), Path("/o/hk"), python="python3")
    assert rg[2] == "regress" and rg[rg.index("--bag") + 1] == "/b/HK.bag" and rg[rg.index("--name-prefix") + 1] == "SL3"


def test_run_matrix_skips_completed_and_keeps_partial_output(tmp_path, trajs):
    cases = tmp_path / "cases"
    for n, noise, seed in (("gz_lockstep-clean-s1", "clean", 1), ("gz_lockstep-realistic-s1", "realistic", 1)):
        write_case(cases, n, source="gz_lockstep", noise=noise, seed=seed)
    specs = sm.select(sm.resolve(sm.matrix_specs(groups=("gz_lockstep",), realistic_seeds=(1,), blocks=("main",)), trajs, [cases],
                                 cases))
    out = tmp_path / "out"
    done = out / "runs" / "gz_lockstep-clean-s1"
    done.mkdir(parents=True)
    (done / "metrics.json").write_text(json.dumps({"status": "completed"}))
    part = out / "runs" / "gz_lockstep-realistic-s1"
    part.mkdir(parents=True)
    (part / "metrics.json").write_text(json.dumps({"status": "partial"}))
    seen = []

    def runner(argv, logf):
        seen.append(argv)
        od = Path(argv[argv.index("--out") + 1])
        od.mkdir(parents=True)
        (od / "metrics.json").write_text(json.dumps({"status": "completed"}))
        return 0

    res = sm.run_matrix(specs, out, jobs=2, runner=runner, log=lambda m: None, cpus=4.0, require_regress="rg.json", python="python3")
    assert res["gz_lockstep-clean-s1"]["status"] == "skipped_completed"
    assert res["gz_lockstep-realistic-s1"]["status"] == "completed" and len(seen) == 1
    assert seen[0][seen[0].index("--cpus") + 1] == "4" and seen[0][seen[0].index("--case") + 1] == str(cases / "gz_lockstep-realistic-s1")
    prev = [p for p in (out / "runs").iterdir() if p.name.startswith("gz_lockstep-realistic-s1.prev-")]
    assert len(prev) == 1 and json.loads((prev[0] / "metrics.json").read_text())["status"] == "partial"


# ---------------------------------------------------------------------------------------------------------------- 判定
def test_classify_blocks():
    c = {"trajectory_source": "gz_lockstep", "pose_nodes": "11", "slam_cam_phase": "tick_end", "speed_mps": 6.0}
    assert sm.classify(c) == ("main", "gz_lockstep")
    assert sm.classify({**c, "speed_mps": 8.0, "pose_nodes": "single"}) == ("v8", "gz_lockstep")
    assert sm.classify({**c, "pose_nodes": "native"}) == ("nodes", "gz_lockstep")
    assert sm.classify({**c, "slam_cam_phase": "r1_mid", "trajectory_source": "fast"}) == ("phase", "fast")
    assert sm.classify({**c, "trajectory_source": "r1_spline"}) is None
    assert sm.classify({**c, "pose_nodes": "native", "slam_cam_phase": "r1_mid"}) is None


def test_report_passes_when_every_clause_holds():
    rep = sm.build_report(passing_docs() + passing_docs("fast"), REGRESS_OK)
    j = rep["groups"]["gz_lockstep"]["judge"]
    assert j["pass"] is True and j["modes"]["livo"]["role"] == "main" and j["modes"]["lio"]["role"] == "separate"
    r8 = rep["ratio8"]["gz_lockstep"]["livo"]
    assert r8["ratio"] == pytest.approx(6.0) and r8["pass"] is True and r8["single_diverged"] == 0 and r8["single_runs"] == 3
    assert rep["ratio8"]["gz_lockstep"]["lio"]["ratio"] == pytest.approx(4.0) and rep["ratio8"]["gz_lockstep"]["lio"]["pass"] is False
    va = rep["validity"]
    assert (va["runs"], va["failed"], va["incomplete"], va["pass"]) == (2 * 12 * 6, [], [], True)
    assert va["ac14_scope"]["runs"] == 8 * 6 and va["ac14_scope"]["pass"] is True
    assert rep["versions"]["reflectance_model"] == ["sem_table_v1"] and rep["versions"]["noise_table_version"] == ["d3-noise-1"]
    assert rep["regress"]["ok"] and rep["regress"]["same_image"] is True
    res = rep["resources"]["gz_lockstep"]["livo"]
    assert res["runs"] == 18 and res["r_map_slope"]["median"] == 26_000.0 and res["cores_equiv"]["median"] == 3.0
    assert rep["ac14"]["pass"] is True and rep["ac14"]["status"] == "frozen" and rep["ac14"]["adr"] == "ADR-215"
    assert r8["basis"] == "ratio" and rep["regress"]["resources_ok"] is None                # 回归记录无资源字段：不判定资源
    ph = rep["phase"]["gz_lockstep"]["clean-s1"]["livo"]["rpe100_pct"]
    assert ph["base"]["median"] == pytest.approx(0.08) and ph["variant"]["median"] == pytest.approx(0.12)
    assert ph["rel_change"] == pytest.approx(0.5)
    nd = rep["nodes"]["gz_lockstep"]["realistic-s1"]
    assert nd["base_cases"] == ["gz_lockstep-realistic-s1-11-tick_end-v6"]
    assert nd["livo"]["rpe100_pct"]["variant"]["median"] == pytest.approx(0.07)
    md = sm.render_md(rep)
    for s in ("主矩阵", "8 m/s", "`pose_nodes = native`", "r1_mid", "HKisland03", "总结论：通过", "sem_table_v1", "d3-noise-1", "6.00",
              "r_map 斜率口径"):
        assert s in md


def test_report_fails_or_is_undecided():
    # clean 地图到网格 0.32 m > 0.15 m：主组判定不通过
    rep = sm.build_report(passing_docs(clean_map=0.32), REGRESS_OK)
    chk = {c["clause"]: c for c in rep["groups"]["gz_lockstep"]["judge"]["modes"]["livo"]["checks"]}
    assert chk[sm.MAP_CLAUSE]["pass"] is False and rep["ac14"]["pass"] is False
    # r1 区间内（0.21%–0.25%）、ADR-215 冻结区间外：realistic 区间条款不通过
    docs = passing_docs()
    for d in docs:
        if d["case"]["noise_profile"] == "realistic" and d["case"]["pose_nodes"] == "11" and d["case"]["slam_cam_phase"] == "tick_end":
            for r in d["runs"]:
                r["traj"]["rpe100_pct"] = 0.20 + 0.01 * d["case"]["seed"]
    rep = sm.build_report(docs, REGRESS_OK)
    chk = {c["clause"]: c for c in rep["groups"]["gz_lockstep"]["judge"]["modes"]["livo"]["checks"]}
    assert chk["realistic RPE100 median in [0.07%, 0.18%]"]["pass"] is False and rep["ac14"]["pass"] is False
    # 处理帧不足（CPU 份额不足时 LIVO 丢帧）：运行有效性不通过
    rep = sm.build_report(passing_docs(processed=1200), REGRESS_OK)
    assert rep["validity"]["pass"] is False and len(rep["validity"]["incomplete"]) == 6 * 6 and rep["ac14"]["pass"] is False
    assert len(rep["validity"]["ac14_scope"]["bad"]) == 6 * 6
    assert rep["validity"]["incomplete"][0]["frac"] == pytest.approx(1200 / FRAMES)
    # 回归缺失：未定；回归镜像与矩阵镜像不同：不通过
    assert sm.build_report(passing_docs(), None)["ac14"]["pass"] is None
    rep = sm.build_report(passing_docs(), {**REGRESS_OK, "image": {"id": "sha256:other"}})
    assert rep["regress"]["same_image"] is False and rep["ac14"]["pass"] is False
    # realistic 只有 4 个种子：样本不全，判定不通过
    docs = [d for d in passing_docs() if not (d["case"]["noise_profile"] == "realistic" and d["case"]["seed"] == 5)]
    assert sm.build_report(docs, REGRESS_OK)["groups"]["gz_lockstep"]["judge"]["pass"] is False
    # 失败运行（684）计入有效性
    docs = passing_docs()
    docs[0]["runs"][0].update(ok=False, code=684)
    rep = sm.build_report(docs, REGRESS_OK)
    assert rep["validity"]["failed"][0]["code"] == 684 and rep["ac14"]["pass"] is False


def test_report_cli_writes_matrix_and_markdown(tmp_path):
    out = tmp_path / "out"
    for d in passing_docs():
        p = out / "runs" / d["case"]["name"] / "metrics.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(d))
    old = out / "runs" / "gz_lockstep-clean-s1.prev-x" / "metrics.json"                    # 改名保留的旧输出不计入
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps(mdoc("gz_lockstep", "clean", 1, livo=[(9.0, 9.0, 9.0)])))
    (out / "hk").mkdir()
    (out / "hk" / "regress.json").write_text(json.dumps(REGRESS_OK))
    assert sm.main(["report", "--out", str(out)]) == 0
    rep = json.loads((out / "matrix.json").read_text())
    assert rep["schema"] == sm.SCHEMA and rep["ac14"]["pass"] is True and len(rep["inputs"]["metrics"]) == 12
    assert "总结论：通过" in (out / "matrix.md").read_text()


# ---------------------------------------------------------------------------------------------------------------- 锚定点
def rotx(a: float) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.array([[1.0, 0, 0], [0, c, -s], [0, s, c]])


def rotz(a: float) -> np.ndarray:
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def loop(n_s: float = 120.0, hz: float = 10.0, v: float = 6.0, r: float = 120.0, hover_s: float = 3.0) -> Tum:
    """事件零点前悬停 `hover_s`，之后 6 m/s 圆形环线（30 m 高）。"""
    t = T0 + np.arange(0, n_s, 1 / hz)
    th = v * np.clip(t - T0 - hover_s, 0, None) / r
    p = np.stack([r * np.sin(th), r * (1 - np.cos(th)), np.full_like(th, 30.0)], 1)
    return Tum(t, p, R_to_quat(np.stack([rotz(a) for a in th])))


def in_frame(gt: Tum, T_F_W: np.ndarray, transient_rad: float, t_until: float) -> Tum:
    """估计 = 真值表达到 F 系；`t_until` 之前的样本带机体系 x 轴的姿态误差（FAST-LIVO2 初始化瞬态）。"""
    T = T_F_W @ gt.T()
    m = gt.t < t_until
    T[m, :3, :3] = T[m, :3, :3] @ rotx(transient_rad)
    return Tum(gt.t, T[:, :3, 3], R_to_quat(T[:, :3, :3]))


def test_anchor_sensitivity_separates_the_init_transient():
    gt = loop()
    T_F_W = np.eye(4)
    T_F_W[:3, :3], T_F_W[:3, 3] = rotz(0.4), [3.0, -2.0, 1.0]
    d = np.radians(0.2)
    est = in_frame(gt, T_F_W, d, T0 + 2.0)
    s = sm.anchor_sensitivity(est, gt, T0 + 2.0)
    assert s["t_first_s"] == pytest.approx(T0) and s["t_anchor_s"] == pytest.approx(T0 + 2.0)
    assert s["att_err_at_anchor_deg"] == pytest.approx(0.2, abs=1e-6)
    assert s["first"]["drift_max_m"] > 0.5                                   # 0.2° × 约 240 m 弦长
    assert s["event0"]["drift_max_m"] < 1e-6 and s["event0"]["ate_aligned_rmse_m"] < 1e-6
    assert s["first"]["ate_aligned_rmse_m"] < 0.05                            # 对齐 ATE 不受锚定点影响（只差首 2 s 的样本）
    clean = sm.anchor_sensitivity(in_frame(gt, T_F_W, 0.0, T0), gt, T0 + 2.0)
    assert clean["att_err_at_anchor_deg"] < 1e-6 and clean["first"]["drift_max_m"] < 1e-6


def test_reanchor_run_map_to_mesh(tmp_path):
    o3d = pytest.importorskip("open3d")
    case = tmp_path / "case"
    case.mkdir()
    half = 400.0
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(np.array([[-half, -half, 0], [half, -half, 0], [half, half, 0],
                                                                        [-half, half, 0.0]])),
                                  o3d.utility.Vector3iVector(np.array([[0, 1, 2], [0, 2, 3]])))
    o3d.io.write_triangle_mesh(str(case / "plane.ply"), m)
    gt = loop()
    save_tum(case / "gt_imu.tum", gt)
    (case / "case.json").write_text(json.dumps({"schema": "drone4d.bench.slam_case.v1", "name": "loop", "gt_imu": "gt_imu.tum",
                                                "mesh": "plane.ply", "t_event0_bag_ns": round((T0 + 2.0) * 1e9)}))
    T_F_W = np.eye(4)
    T_F_W[:3, :3], T_F_W[:3, 3] = rotz(-0.3), [1.0, 2.0, 0.5]
    rd = tmp_path / "runs" / "livo-r1"
    (rd / "Log" / "result").mkdir(parents=True)
    (rd / "Log" / "pcd").mkdir(parents=True)
    save_tum(rd / "Log" / "result" / "est.txt", in_frame(gt, T_F_W, np.radians(0.2), T0 + 2.0))
    rng = np.random.default_rng(3)
    Pw = np.stack([rng.uniform(-130, 130, 40_000), rng.uniform(-10, 250, 40_000), np.zeros(40_000)], 1)
    Pf = Pw @ T_F_W[:3, :3].T + T_F_W[:3, 3]
    o3d.io.write_point_cloud(str(rd / "Log" / "pcd" / "all_raw_points.pcd"), o3d.geometry.PointCloud(o3d.utility.Vector3dVector(Pf)))
    assert sm.event0_s(case) == pytest.approx(T0 + 2.0)
    doc = sm.reanchor_run(case, rd)
    assert doc["att_err_at_anchor_deg"] == pytest.approx(0.2, abs=1e-6) and doc["map"]["acc_mean_m"] < 1e-3
    on_disk = json.loads((rd / sm.REANCHOR).read_text())
    assert on_disk["anchor"] == "event0" and on_disk["event0"]["drift_max_m"] < 1e-4
    # metrics.json 汇总入口：reanchor_all 逐运行写出，load_docs 读回并进入算例摘要
    out = tmp_path
    mp = out / "runs" / "loop" / "metrics.json"
    doc_m = mdoc("gz_lockstep", "clean", 1, livo=[(0.08, 0.1, 0.3)], anchor=None)              # ADR-215 之前的运行（首个估计锚定）
    doc_m["case"]["dir"] = str(case)
    mp.parent.mkdir(parents=True, exist_ok=True)
    (mp.parent / "runs").mkdir(exist_ok=True)
    (rd.parent / "livo-r1").rename(mp.parent / "runs" / "livo-r1")
    mp.write_text(json.dumps(doc_m))
    res = sm.reanchor_all(out, log=lambda s: None, force=True)
    assert list(res) == ["loop/livo-r1"] and res["loop/livo-r1"] < 1e-3
    docs, _ = sm.load_docs(out)
    row = sm.case_row(docs[0])["modes"]["livo"]
    assert row["map_to_mesh_mean_event0_m"]["median"] < 1e-3 and row["att_err_at_event0_deg"]["median"] == pytest.approx(0.2, abs=1e-6)


def test_legacy_runs_are_rejudged_from_reanchor_without_rerun():
    """ADR-215 (6)：旧运行（首个估计锚定，clean 地图 0.32 m）以 `reanchor.json` 的事件零点数值（0.08 m）重判；r1 口径另报不计入。"""
    docs = passing_docs(clean_map=0.32, anchor=None)
    rep = sm.build_report(docs, REGRESS_OK)
    assert rep["ac14"]["pass"] is None                                         # 缺事件零点数据：地图条款未定，不回落到 r1 口径
    for d in docs:
        for r in d["runs"]:
            r["reanchor"] = {"att_err_at_anchor_deg": 0.2, "map": {"acc_mean_m": 0.08}, "event0": {"drift_max_m": 0.2}}
    rep = sm.build_report(docs, REGRESS_OK)
    chk = {c["clause"]: c for c in rep["groups"]["gz_lockstep"]["judge"]["modes"]["livo"]["checks"]}
    assert chk[sm.MAP_CLAUSE]["value"] == pytest.approx(0.08) and chk[sm.MAP_CLAUSE]["pass"] is True and rep["ac14"]["pass"] is True
    ref = rep["ac14_first_estimate_anchor"]
    assert ref["counted"] is False and ref["map_to_mesh_mean_first_m"] == pytest.approx(0.32) and ref["map_pass"] is False
    assert ref["pass"] is False
    row = rep["groups"]["gz_lockstep"]["cases"][0]["modes"]["livo"]
    assert row["anchors"] == ["first_estimate"] and row["drift_max_event0_m"]["median"] == pytest.approx(0.2)
    md = sm.render_md(rep)
    assert "参考口径（r1，首个估计锚定，不计入）" in md and "锚定点敏感性" in md and "ADR-215" in md
    # 新运行（以事件零点评测）：参考口径无数据
    assert sm.build_report(passing_docs(), REGRESS_OK)["ac14_first_estimate_anchor"]["pass"] is None


def test_single_pose_divergence_is_counted():
    docs = [d for d in passing_docs() if d["case"]["pose_nodes"] != "single"]
    docs.append(mdoc("gz_lockstep", "clean", 1, pn="single", speed=8.0, livo=[(300.0, 805.0, 50.0), (12.0, 65.0, 9.0), (0.5, 0.6, 0.4)]))
    r8 = sm.build_report(docs, REGRESS_OK)["ratio8"]["gz_lockstep"]["livo"]
    assert r8["single_diverged"] == 2 and r8["single_runs"] == 3 and r8["ratio"] == pytest.approx(65.0 / 0.10) and r8["pass"] is True
    assert r8["basis"] == "single_pose_diverged" and r8["reference_ratio"]["fast_tier"] == [51.0, 88.0]
    # ADR-215 (3)：单一位姿发散（中位 12 m > 10 m）时即使比值 < 5 也计为满足，并在报告中注明
    docs = [d for d in passing_docs() if d["case"]["speed_mps"] != 8.0]
    docs.append(mdoc("gz_lockstep", "clean", 1, pn="single", speed=8.0, livo=[(5.0, 12.0, 3.0)] * 3))
    docs.append(mdoc("gz_lockstep", "clean", 1, speed=8.0, livo=[(1.0, 4.0, 1.0)] * 3))
    rep = sm.build_report(docs, REGRESS_OK)
    r8 = rep["ratio8"]["gz_lockstep"]["livo"]
    assert r8["ratio"] == pytest.approx(3.0) and r8["pass"] is True and r8["basis"] == "single_pose_diverged"
    assert "单一位姿发散，计为满足" in sm.render_md(rep)


def test_regress_resources_are_reported_against_adr215():
    rg = {**REGRESS_OK, "resources": {"mem_gb_applied": 32.0, "cpus_applied": 8.0}}
    rep = sm.build_report(passing_docs(), rg)
    assert rep["regress"]["resources_ok"] is True and rep["regress"]["resources_required"] == {"mem_gb_min": 32.0, "cpus_min": 8.0}
    assert "ADR-215 要求 ≥ 32 GB、8 核：通过" in sm.render_md(rep)
    rep = sm.build_report(passing_docs(), {**REGRESS_OK, "resources": {"mem_gb_applied": 20.0, "cpus_applied": 4.0}})
    assert rep["regress"]["resources_ok"] is False and rep["ac14"]["pass"] is True              # 资源只报告，ATE 条款照常判定


def test_validity_scope_only_counts_judged_runs():
    docs = passing_docs() + passing_docs("fast")
    fast_phase = next(d for d in docs if d["case"]["trajectory_source"] == "fast" and d["case"]["slam_cam_phase"] == "r1_mid")
    fast_phase["runs"][1]["per_frame"]["lio"]["n"] = 1560                       # 附加组对比算例：只报告，不进入总判定
    rep = sm.build_report(docs, REGRESS_OK)
    assert rep["validity"]["pass"] is False and rep["validity"]["ac14_scope"]["pass"] is True and rep["ac14"]["pass"] is True
    assert "处理帧比例 0.976" in sm.render_md(rep)
    gz_main = next(d for d in docs if d["case"]["trajectory_source"] == "gz_lockstep" and d["case"]["noise_profile"] == "realistic")
    gz_main["runs"][0]["per_frame"]["lio"]["n"] = 1500
    rep = sm.build_report(docs, REGRESS_OK)
    assert rep["validity"]["ac14_scope"]["bad"][0]["case"] == gz_main["case"]["name"] and rep["ac14"]["pass"] is False
