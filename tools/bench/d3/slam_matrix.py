#!/usr/bin/env python3
"""tools/bench/d3/slam_matrix.py：FAST-LIVO2 RPE100 矩阵与 HKisland03 回归（D3-AC-14；D3-AC-47 的 SLAM 实测项；SL3）。

在 SL2 的 town 883 m 环线轨迹（`slam_traj.py fly`）与算例（`slam_traj.py cases`）上，用 SL1 的 `slam_eval` 作业跑完整矩阵并判定：

- 矩阵（`matrix_specs`，每个轨迹组一份，`gz_lockstep` 为主组，`fast` 为附加组"另报一组"）：
  - `main`：6 m/s，clean 种子 1 与 realistic 种子 1–5，`pose_nodes = 11`、`slam_cam_phase = tick_end`（D3-AC-14 主矩阵）；
  - `v8`：8 m/s 轨迹（`<组>_v8`），clean 种子 1，`pose_nodes = single` 与 `11` 各一个算例（逐点位姿相对单一位姿的 ATE 比）；
  - `nodes`：6 m/s，clean 与 realistic 种子 1，`pose_nodes = native`（与 `main` 同种子的 `11` 对比，D3-AC-47）；
  - `phase`：6 m/s，clean 与 realistic 种子 1，`slam_cam_phase = r1_mid`（与 `tick_end` 对比，D3-AC-47）。
  每个算例跑 LIVO（主口径）与 LIO（单列）各 `--repeats` 次（缺省 3，4 倍速）。
- 子命令：
  - `plan`：解析算例名（`slam_traj.case_name`）并在算例根目录中查找；核对已有算例与轨迹一致（噪声档、种子、`pose_nodes`、相位、
    航速与 PoseHistory sha256），写 `<out>/plan.json`；
  - `cases`：生成缺失的算例（进程内调用 `slam_traj.build_cases`，须在 `d3-sensorsvc` 容器内运行，与 SL2 相同）；
  - `run`：在宿主上对每个算例调用 `slam_eval run`（子进程；FAST-LIVO2 与评测容器由 `slam_eval` 启动），`--jobs` 个算例并行，
    已完成（`metrics.json` 的 status = completed）的算例跳过；未完成的旧输出移到 `<算例>.prev-<UTC>` 后重跑；
  - `regress`：HKisland03 真机回归（调用 `slam_eval regress`），结果供 `run --require-regress` 作镜像闸门（M27-FR-110）；
  - `reanchor`：锚定点敏感性诊断（d3-sensorsvc 容器内）：以事件零点（预滚窗口结束）处的估计重新锚定，重算锚定漂移与地图到网格
    距离，写各运行目录的 `reanchor.json`；只作诊断，不进入判定；
  - `report`：汇总 `<out>/runs/*/metrics.json` 与回归记录，写 `<out>/matrix.json`（`drone4d.bench.slam_matrix.v1`）与
    `<out>/matrix.md`（实测记录用的表格）。
- 判定（`build_report`）：主组按 `slam_eval.report.judge_ac14`（clean 三项、realistic 四项与样本完整性，LIVO 为主判定，LIO 单列）；
  8 m/s 逐点（`11`）相对单一位姿（`single`）的对齐 ATE 比（跨重复中位数之比）≥ 5；HKisland03 ATE ≤ 1.2 m；报告注明反射率模型与
  噪声档参数表版本。另做运行有效性核对：FAST-LIVO2 实际处理的 LiDAR 帧数（逐帧耗时记录数）不少于 bag 帧数的 `MIN_FRAME_FRAC`，
  否则该运行记为"处理不完整"（CPU 份额不足时 LIVO 模式的图像经 republish 丢帧、LiDAR 帧被合并处理，结果不代表算法本身），
  D3-AC-14 的总结论要求全部运行完整。阈值为 §11 暂定值（`AC14["status"] = provisional`），由 D01 以 ADR 冻结，本工具不放宽。

用法（dmax，工作目录 `/data/anet-drone4d-d3/d3dev/sl3`，`src` 为仓库同步副本）：
    # 算例（d3-sensorsvc 容器内，CPU 池，挂载 src、sl2/out、out 与世界包）
    python src/tools/bench/d3/slam_matrix.py cases --traj-root ../sl2/out/traj --cases-root ../sl2/out/cases --out out \\
        --world <世界包 smalltown-01> [--block v8] [--group gz_lockstep]
    # 宿主（python3 + numpy）
    python3 src/tools/bench/d3/slam_matrix.py regress --bag <HKisland_GNSS03.bag> --out out
    python3 src/tools/bench/d3/slam_matrix.py run --traj-root ../sl2/out/traj --cases-root ../sl2/out/cases --out out \\
        --jobs 3 --parallel 6 --cpus 4 --require-regress out/hk/regress.json
    python3 src/tools/bench/d3/slam_matrix.py report --out out
退出码：0 成功（`report`：D3-AC-14 判定通过）；1 有算例或运行失败（`report`：判定不通过或未定）；2 参数或前置条件错误。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for _p in (ROOT / "python", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from slam_eval import EVAL_VERSION  # noqa: E402
from slam_eval.report import AC14, distribution, judge_ac14, run_scalars  # noqa: E402
from slam_eval.traj import TrajInterp, Tum, load_tum, traj_metrics  # noqa: E402

SCHEMA_PLAN = "drone4d.bench.slam_matrix_plan.v1"
SCHEMA = "drone4d.bench.slam_matrix.v1"
EXIT_OK, EXIT_FAIL, EXIT_ARGS = 0, 1, 2

GROUPS = {"gz_lockstep": "main", "fast": "additional"}          # 轨迹组 → 角色（D3-AC-14：gz 锁步主组，Fast 档另报一组）
MAIN_GROUP = "gz_lockstep"
V8_SUFFIX = "_v8"                                               # 8 m/s 轨迹目录 = <组><V8_SUFFIX>（SL2 `fly --speed 8`）
BLOCKS = ("main", "v8", "nodes", "phase")
REALISTIC_SEEDS = (1, 2, 3, 4, 5)                               # D3-AC-14：realistic 5 种子 × 3 重复
BASE_NODES, BASE_PHASE = "11", "tick_end"                      # 缺省位姿节点与 slam_cam 相位（§5.4.6、§4.3 规则 4）
BASE_SPEED_MPS, V8_SPEED_MPS = 6.0, 8.0
DIVERGED_M = 10.0                                               # 对齐 ATE 超过该值记为发散（只用于报告计数，不改变比值判定）
MIN_FRAME_FRAC = 0.98                                           # 运行有效性：处理帧数 / bag 帧数（FAST-LIVO2 初始化消耗前几帧）
MODES = ("livo", "lio")
REANCHOR = "reanchor.json"                                      # 锚定点敏感性诊断（每次运行一份，`reanchor` 子命令写出）


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return _jsonable(o.tolist())
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, float) and not np.isfinite(o):
        return None
    return o


def write_json(path: Path, doc: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(doc), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _slam_traj():
    """`slam_traj.py`（脚本，非包）：算例命名与算例生成；按路径加载一次。"""
    mod = sys.modules.get("slam_traj")
    if mod is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("slam_traj", HERE / "slam_traj.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["slam_traj"] = mod
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------------------------------------------- 矩阵
def _spec(block: str, group: str, traj: str, noise: str, seed: int, pose_nodes: str, cam_phase: str) -> dict:
    return {"block": block, "group": group, "traj": traj, "noise": noise, "seed": int(seed), "pose_nodes": pose_nodes,
            "cam_phase": cam_phase}


def matrix_specs(groups: Sequence[str] = tuple(GROUPS), realistic_seeds: Sequence[int] = REALISTIC_SEEDS,
                 blocks: Sequence[str] = BLOCKS) -> list[dict]:
    """矩阵算例规格（模块说明"矩阵"）；`traj` 为轨迹目录名（相对 `--traj-root`）。"""
    out: list[dict] = []
    for g in groups:
        if "main" in blocks:
            out += [_spec("main", g, g, "clean", 1, BASE_NODES, BASE_PHASE)]
            out += [_spec("main", g, g, "realistic", s, BASE_NODES, BASE_PHASE) for s in realistic_seeds]
        if "v8" in blocks:
            out += [_spec("v8", g, g + V8_SUFFIX, "clean", 1, pn, BASE_PHASE) for pn in ("single", BASE_NODES)]
        for noise, seed in (("clean", 1), ("realistic", 1)):
            if "nodes" in blocks:
                out.append(_spec("nodes", g, g, noise, seed, "native", BASE_PHASE))
            if "phase" in blocks:
                out.append(_spec("phase", g, g, noise, seed, BASE_NODES, "r1_mid"))
    return out


def resolve(specs: Sequence[Mapping], traj_root: Path, case_roots: Sequence[Path], new_cases: Path) -> list[dict]:
    """补全算例名与目录：名称按 `slam_traj.case_name`（轨迹的 trajectory_source 与航速取自 `traj.json`）；在 `case_roots` 中按序
    查找已有算例并核对（噪声档、种子、`pose_nodes`、相位、航速、PoseHistory sha256 与轨迹一致），不一致记 `stale`（不复用、不覆盖）；
    找不到时目录取 `new_cases/<名称>`、`exists = False`。"""
    st = _slam_traj()
    trajs: dict[str, dict] = {}
    out = []
    for s in specs:
        tdir = Path(traj_root) / s["traj"]
        if s["traj"] not in trajs:
            p = tdir / "traj.json"
            if not p.is_file():
                raise FileNotFoundError(f"{p}: trajectory not flown (slam_traj.py fly)")
            trajs[s["traj"]] = read_json(p)
        t = trajs[s["traj"]]
        speed = float(t["plan"]["speed_mps"])
        name = st.case_name(t["trajectory_source"], s["noise"], s["seed"], s["pose_nodes"], s["cam_phase"], speed)
        r = {**s, "name": name, "traj_dir": str(tdir), "speed_mps": speed, "trajectory_source": t["trajectory_source"],
             "pose_history_sha256": t["pose_history"]["sha256"], "dir": str(Path(new_cases) / name), "exists": False, "stale": None}
        for root in case_roots:
            cj = Path(root) / name / "case.json"
            if not cj.is_file():
                continue
            c = read_json(cj)
            bad = [k for k, want in (("noise_profile", s["noise"]), ("seed", s["seed"]), ("pose_nodes", s["pose_nodes"]),
                                     ("slam_cam_phase", s["cam_phase"]), ("speed_mps", speed),
                                     ("trajectory_source", t["trajectory_source"]))
                   if c.get(k) != want]
            if (c.get("trajectory") or {}).get("pose_history_sha256") != r["pose_history_sha256"]:
                bad.append("pose_history_sha256")
            if not (Path(root) / name / (c.get("bag") or "sim.bag")).is_file():
                bad.append("bag")
            if bad:
                r["stale"] = {"dir": str(cj.parent), "fields": bad}
                continue
            r.update(dir=str(cj.parent), exists=True, stale=None)
            break
        out.append(r)
    names = [r["name"] for r in out]
    if len(set(names)) != len(names):
        raise ValueError("duplicate case names in the matrix")
    return out


def select(specs: Iterable[Mapping], *, blocks: Sequence[str] | None = None, groups: Sequence[str] | None = None,
           names: Sequence[str] | None = None) -> list[dict]:
    return [dict(s) for s in specs if (not blocks or s["block"] in blocks) and (not groups or s["group"] in groups)
            and (not names or s["name"] in names)]


def metrics_status(out: Path, name: str) -> str | None:
    p = Path(out) / "runs" / name / "metrics.json"
    if not p.is_file():
        return None
    try:
        return read_json(p).get("status")
    except (OSError, ValueError):
        return "unreadable"


def plan_doc(specs: Sequence[Mapping], out: Path) -> dict:
    rows = [{**s, "metrics_status": metrics_status(out, s["name"])} for s in specs]
    return {"schema": SCHEMA_PLAN, "created_utc": _utc(), "cases": rows,
            "counts": {"cases": len(rows), "existing": sum(r["exists"] for r in rows), "stale": sum(bool(r["stale"]) for r in rows),
                       "completed": sum(r["metrics_status"] == "completed" for r in rows)}}


# ---------------------------------------------------------------------------------------------------------------- 算例
def build_missing(specs: Sequence[Mapping], world: Path, new_cases: Path, *, camera: bool = True, max_frames: int | None = None,
                  builder: Callable[..., dict] | None = None, log=print) -> dict:
    """生成缺失算例：按（轨迹、`pose_nodes`、相位）分组调用 `slam_traj.build_cases`（算例组共用网格裁剪文件）。"""
    builder = builder or _slam_traj().build_cases
    todo: dict[tuple, list] = {}
    for s in specs:
        if s["exists"]:
            continue
        if Path(s["dir"]).parent.resolve() != Path(new_cases).resolve():
            raise ValueError(f"{s['name']}: new case dir {s['dir']} is outside {new_cases}")
        todo.setdefault((s["traj_dir"], s["pose_nodes"], s["cam_phase"]), []).append((s["noise"], s["seed"]))
    res = {}
    for (traj_dir, pn, ph), sets in todo.items():
        log(f"[cases] {Path(traj_dir).name} pose_nodes={pn} cam_phase={ph}: {sets}")
        r = builder(Path(traj_dir), Path(world), Path(new_cases), sets, pose_nodes=pn, cam_phase=ph, camera=camera, max_frames=max_frames,
                    log=log)
        write_json(Path(new_cases) / f"cases-{Path(traj_dir).name}-pn{pn}-{ph}.json", r)
        res.update(r.get("cases", {}))
    return res


# ---------------------------------------------------------------------------------------------------------------- 运行
def slam_eval_run_argv(case_dir: Path, out_dir: Path, *, modes: str = "livo,lio", repeats: int = 3, rate: float = 4.0, parallel: int = 6,
                       name_prefix: str = "SL3", image: str = "d3-livo:dev", cpus: float | None = None,
                       require_regress: str | None = None, extra: Sequence[str] = (), python: str | None = None) -> list[str]:
    """一个算例的 `slam_eval run` 命令行（宿主子进程；容器由 slam_eval 启动，名称 `d3-livo-<前缀>-<算例>-<模式>-r<k>`）。"""
    argv = [python or sys.executable, str(HERE / "slam_eval"), "run", "--case", str(case_dir), "--out", str(out_dir), "--modes", modes,
            "--repeats", str(int(repeats)), "--rate", f"{float(rate):g}", "--parallel", str(int(parallel)), "--name-prefix", name_prefix,
            "--image", image]
    if cpus:
        argv += ["--cpus", f"{float(cpus):g}"]
    if require_regress:
        argv += ["--require-regress", str(require_regress)]
    return [*argv, *extra]


def run_matrix(specs: Sequence[Mapping], out: Path, *, jobs: int = 3, force: bool = False, runner: Callable[[list[str], Path], int] | None = None,
               log=print, **kw) -> dict:
    """逐算例执行 `slam_eval run`（`jobs` 个并行）；已完成的跳过，未完成的旧输出改名保留后重跑。返回 {算例: 状态}。"""
    runs = Path(out) / "runs"
    runs.mkdir(parents=True, exist_ok=True)

    def _sub(argv: list[str], logf: Path) -> int:
        with open(logf, "w") as fh:
            return subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT, check=False).returncode

    runner = runner or _sub
    todo, res = [], {}
    for s in specs:
        if not s["exists"]:
            res[s["name"]] = {"status": "missing_case"}
            continue
        st = metrics_status(out, s["name"])
        if st == "completed" and not force:
            res[s["name"]] = {"status": "skipped_completed"}
            continue
        od = runs / s["name"]
        if od.exists():
            od.rename(od.with_name(f"{od.name}.prev-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"))
        todo.append((s, od))

    def _one(item) -> tuple[str, dict]:
        s, od = item
        argv = slam_eval_run_argv(Path(s["dir"]), od, **kw)
        t0 = time.time()
        log(f"[run] {s['name']} start")
        rc = runner(argv, runs / f"{s['name']}.log")
        st = metrics_status(out, s["name"])
        log(f"[run] {s['name']} rc={rc} status={st} {time.time() - t0:.0f} s")
        return s["name"], {"status": st, "rc": rc, "wall_s": round(time.time() - t0, 1), "argv": argv}

    with cf.ThreadPoolExecutor(max_workers=max(1, int(jobs))) as ex:
        res.update(dict(ex.map(_one, todo)))
    return res


def slam_eval_regress_argv(bag: Path, out_dir: Path, *, name_prefix: str = "SL3", image: str = "d3-livo:dev",
                           extra: Sequence[str] = (), python: str | None = None) -> list[str]:
    return [python or sys.executable, str(HERE / "slam_eval"), "regress", "--bag", str(bag), "--out", str(out_dir), "--name-prefix", name_prefix,
            "--image", image, *extra]


# ---------------------------------------------------------------------------------------------------------------- 锚定点敏感性
def event0_s(case_dir: Path) -> float:
    """算例的事件零点（bag 时刻，s）：case.json `t_event0_bag_ns`（SL2 算例 = 时基偏移 1.76e9 s，预滚窗口在其前 2 s）。"""
    c = read_json(Path(case_dir) / "case.json")
    return float(c.get("t_event0_bag_ns") or c.get("t0_bag_ns")) / 1e9


def trim(est: Tum, t_from_s: float) -> Tum:
    m = est.t >= t_from_s
    return Tum(est.t[m], est.p[m], est.q[m])


def anchor_sensitivity(est: Tum, gt: Tum | TrajInterp, t_anchor_s: float) -> dict:
    """锚定点敏感性：`traj_metrics` 以首个估计锚定（r1 口径；首个估计位于 FAST-LIVO2 初始化阶段），这里另以首个不早于 `t_anchor_s`
    （事件零点，预滚窗口结束）的估计锚定，给出两种锚定下的漂移、对齐 ATE，以及首个估计锚定时事件零点处的姿态误差（初始化瞬态）。
    对齐 ATE 与 RPE100 不依赖锚定点；锚定漂移与锚定坐标下的地图到网格距离随锚定点变化。"""
    g = gt if isinstance(gt, TrajInterp) else TrajInterp(gt)
    first = traj_metrics(est, g)
    late_est = trim(est, t_anchor_s)
    late = traj_metrics(late_est, g)
    T = first["_T_W_F"] @ late_est.T()[:1]
    Rg, pg = g.at(late_est.t[:1])
    dR = Rg[0].T @ T[0, :3, :3]
    ang = float(np.degrees(np.arccos(np.clip((np.trace(dR) - 1.0) / 2.0, -1.0, 1.0))))
    keys = ("drift_max_m", "drift_final_m", "ate_aligned_rmse_m", "ate_unaligned_rmse_m", "rpe100_pct", "n")
    return {"t_first_s": float(est.t[0]), "t_anchor_s": float(late_est.t[0]), "att_err_at_anchor_deg": ang,
            "pos_err_at_anchor_m": float(np.linalg.norm(T[0, :3, 3] - pg[0])),
            "first": {k: first[k] for k in keys}, "event0": {k: late[k] for k in keys}, "_T_W_F_event0": late["_T_W_F"]}


def reanchor_run(case_dir: Path, run_dir: Path, *, ref: Path | None = None, mesh=None, t_anchor_s: float | None = None) -> dict:
    """一次运行的锚定点敏感性（含事件零点锚定下的地图到网格距离；地图评测需要 open3d，在 d3-sensorsvc 容器内执行）。"""
    from slam_eval.case import load_case
    from slam_eval.cli import _trajectory_file
    from slam_eval.mapeval import map_metrics, open_mesh, read_cloud, transform_points, voxel_down

    case = load_case(case_dir)
    est = load_tum(_trajectory_file(run_dir))
    t_a = event0_s(case_dir) if t_anchor_s is None else float(t_anchor_s)
    sens = anchor_sensitivity(est, load_tum(case.gt_imu), t_a)
    T = sens.pop("_T_W_F_event0")
    doc = {"schema": SCHEMA + ".reanchor", "run": str(run_dir), "anchor": "event0", **sens, "map": None}
    pcd = Path(run_dir) / "Log" / "pcd" / "all_raw_points.pcd"
    if pcd.is_file() and case.mesh:
        mesh = mesh or open_mesh(case.mesh, None if case.mesh_transform is None else np.asarray(case.mesh_transform, np.float64))
        R = read_cloud(ref)[0] if ref is not None and Path(ref).is_file() else None
        Pd, _ = voxel_down(transform_points(read_cloud(pcd)[0], T), None, 0.1)
        doc["map"] = map_metrics(Pd, mesh, R)
    write_json(Path(run_dir) / REANCHOR, doc)
    return doc


def reanchor_all(out: Path, *, names: Sequence[str] | None = None, force: bool = False, log=print) -> dict:
    """`<out>/runs/<算例>/metrics.json` 中全部成功运行的锚定点敏感性（已有 `reanchor.json` 的跳过）。"""
    from slam_eval.case import load_case
    from slam_eval.mapeval import open_mesh

    res = {}
    for mp in sorted((Path(out) / "runs").glob("*/metrics.json")):
        if ".prev-" in mp.parent.name or (names and mp.parent.name not in names):
            continue
        d = read_json(mp)
        cdir = Path((d.get("case") or {}).get("dir") or "")
        if not (cdir / "case.json").is_file():
            res[mp.parent.name] = "case dir not found"
            continue
        mesh = None
        for r in d.get("runs", []):
            rd = mp.parent / "runs" / str(r.get("run"))
            if not r.get("ok") or ((rd / REANCHOR).is_file() and not force):
                continue
            if mesh is None:
                c = load_case(cdir)
                mesh = open_mesh(c.mesh, None if c.mesh_transform is None else np.asarray(c.mesh_transform, np.float64))
            doc = reanchor_run(cdir, rd, ref=mp.parent / "gt_lidar_ref.ply", mesh=mesh)
            key = f"{mp.parent.name}/{r['run']}"
            res[key] = (doc.get("map") or {}).get("acc_mean_m")
            log(f"[reanchor] {key}: att_err {doc['att_err_at_anchor_deg']:.3f} deg, map-to-mesh (event0 anchor) {res[key]}")
    return res


# ---------------------------------------------------------------------------------------------------------------- 汇总与判定
def classify(case: Mapping) -> tuple[str, str] | None:
    """metrics.json 的 `case` → (区块, 轨迹组)；不属于矩阵的算例返回 None。"""
    g = case.get("trajectory_source")
    if g not in GROUPS:
        return None
    pn, ph, v = str(case.get("pose_nodes")), case.get("slam_cam_phase"), float(case.get("speed_mps") or 0.0)
    if abs(v - V8_SPEED_MPS) < 1e-6 and ph == BASE_PHASE and pn in ("single", BASE_NODES):
        return "v8", g
    if abs(v - BASE_SPEED_MPS) > 1e-6:
        return None
    if pn == BASE_NODES and ph == BASE_PHASE:
        return "main", g
    if pn == "native" and ph == BASE_PHASE:
        return "nodes", g
    if pn == BASE_NODES and ph == "r1_mid":
        return "phase", g
    return None


def run_frames(doc: Mapping, run: Mapping) -> dict:
    """运行有效性：FAST-LIVO2 处理的 LiDAR 帧数（逐帧耗时记录）相对 bag 帧数。"""
    bag_frames = (doc.get("contract") or {}).get("lidar_frames")
    n = ((run.get("per_frame") or {}).get("lio") or {}).get("n")
    if not bag_frames or n is None:
        return {"bag_frames": bag_frames, "processed": n, "frac": None, "complete": None}
    frac = float(n) / float(bag_frames)
    return {"bag_frames": int(bag_frames), "processed": int(n), "frac": frac, "complete": bool(frac >= MIN_FRAME_FRAC)}


def _ok_runs(docs: Sequence[Mapping], mode: str) -> list[Mapping]:
    return [r for d in docs for r in d.get("runs", []) if r.get("mode") == mode and r.get("ok")]


def _dist(docs: Sequence[Mapping], mode: str, key: str) -> dict:
    return distribution(run_scalars(r)[key] for r in _ok_runs(docs, mode))


def case_row(doc: Mapping) -> dict:
    """单个算例的逐模式摘要（分布与有效性）。"""
    c = doc.get("case") or {}
    row: dict = {"case": c.get("name"), "noise": c.get("noise_profile"), "seed": c.get("seed"), "pose_nodes": c.get("pose_nodes"),
                 "slam_cam_phase": c.get("slam_cam_phase"), "speed_mps": c.get("speed_mps"), "status": doc.get("status"), "modes": {}}
    for mode in MODES:
        rs = [r for r in doc.get("runs", []) if r.get("mode") == mode]
        if not rs:
            continue
        fr = [run_frames(doc, r) for r in rs]
        row["modes"][mode] = {
            "runs": len(rs), "ok": sum(bool(r.get("ok")) for r in rs),
            **{k: _dist([doc], mode, k) for k in ("rpe100_pct", "rpe100_median_pct", "ate_aligned_rmse_m", "drift_max_m",
                                                  "map_to_mesh_mean_m", "fscore@0.2", "rss_peak_gb")},
            "map_to_mesh_mean_aligned_m": distribution(((r.get("map_aligned") or {}).get("acc_mean_m")) for r in rs if r.get("ok")),
            "map_to_mesh_mean_event0_m": distribution((((r.get("reanchor") or {}).get("map") or {}).get("acc_mean_m"))
                                                      for r in rs if r.get("ok")),
            "drift_max_event0_m": distribution((((r.get("reanchor") or {}).get("event0") or {}).get("drift_max_m"))
                                               for r in rs if r.get("ok")),
            "att_err_at_event0_deg": distribution(((r.get("reanchor") or {}).get("att_err_at_anchor_deg")) for r in rs if r.get("ok")),
            "rpe100_runs": [run_scalars(r)["rpe100_pct"] for r in rs if r.get("ok")],
            "frames": {"min_frac": min((f["frac"] for f in fr if f["frac"] is not None), default=None),
                       "complete": None if any(f["complete"] is None for f in fr) else all(f["complete"] for f in fr)},
            "cores_equiv": distribution(((r.get("resources") or {}).get("cpu_cores_equiv")) for r in rs),
            "r_map_slope": distribution(((r.get("r_map") or {}).get("r_map_slope")) for r in rs),
            "r_map_peak": distribution(((r.get("r_map") or {}).get("r_map_peak")) for r in rs),
        }
    return row


def ate_ratio(single: Sequence[Mapping], per_point: Sequence[Mapping], mode: str) -> dict:
    """8 m/s：单一位姿与逐点位姿的对齐 ATE（各自跨重复取中位数）之比（D3-AC-14"逐点位姿相对单一位姿的 ATE 改善 ≥ 5 倍"）。"""
    a, b = _dist(single, mode, "ate_aligned_rmse_m"), _dist(per_point, mode, "ate_aligned_rmse_m")
    ratio = None
    if a.get("n") and b.get("n") and b["median"] > 0:
        ratio = a["median"] / b["median"]
    lim = AC14["per_point_vs_single_pose_ate_ratio_min"]
    div = [run_scalars(r)["ate_aligned_rmse_m"] for r in _ok_runs(single, mode)]
    return {"single": a, "per_point": b, "ratio": ratio, "limit": lim, "pass": None if ratio is None else bool(ratio >= lim),
            "single_diverged": sum(1 for x in div if x is not None and x > DIVERGED_M), "single_runs": len(div),
            "rpe100_single": _dist(single, mode, "rpe100_pct"), "rpe100_per_point": _dist(per_point, mode, "rpe100_pct")}


def compare(base: Sequence[Mapping], variant: Sequence[Mapping], mode: str) -> dict:
    """同一（组、噪声档、种子）上基线与变体的 RPE100、ATE 与地图到网格对比（中位数与相对差）。"""
    out = {}
    for k in ("rpe100_pct", "ate_aligned_rmse_m", "map_to_mesh_mean_m"):
        a, b = _dist(base, mode, k), _dist(variant, mode, k)
        rel = None
        if a.get("n") and b.get("n") and a["median"]:
            rel = (b["median"] - a["median"]) / a["median"]
        out[k] = {"base": a, "variant": b, "rel_change": rel}
    return out


def build_report(docs: Sequence[Mapping], regress: Mapping | None = None) -> dict:
    """矩阵汇总与 D3-AC-14 判定（模块说明"判定"）。`docs` 为各算例的 metrics.json。"""
    by: dict[tuple[str, str], list[Mapping]] = {}
    skipped = []
    for d in docs:
        k = classify(d.get("case") or {})
        if k is None:
            skipped.append((d.get("case") or {}).get("name"))
            continue
        by.setdefault(k, []).append(d)

    def pick(block: str, group: str, **flt) -> list[Mapping]:
        return [d for d in by.get((block, group), []) if all((d.get("case") or {}).get(k) == v for k, v in flt.items())]

    rep: dict = {"schema": SCHEMA, "created_utc": _utc(), "thresholds": AC14, "min_frame_frac": MIN_FRAME_FRAC, "skipped_cases": skipped,
                 "groups": {}, "ratio8": {}, "nodes": {}, "phase": {}}
    images, versions = set(), {"reflectance_model": set(), "noise_table_version": set(), "world": set()}
    for d in docs:
        images.add((d.get("image") or {}).get("id"))
        c = d.get("case") or {}
        for k in versions:
            versions[k].add(c.get(k))
    rep["image_ids"] = sorted(str(i) for i in images)
    rep["versions"] = {**{k: sorted(str(x) for x in v if x is not None) for k, v in versions.items()}, "eval_version": EVAL_VERSION}
    for g, role in GROUPS.items():
        main = by.get(("main", g), [])
        clean = [d for d in main if (d.get("case") or {}).get("noise_profile") == "clean"]
        real = [d for d in main if (d.get("case") or {}).get("noise_profile") == "realistic"]
        rows = [case_row(d) for d in sorted(main, key=lambda d: ((d["case"].get("noise_profile") or ""), d["case"].get("seed") or 0))]
        rep["groups"][g] = {"role": role, "cases": rows, "judge": judge_ac14(clean, real) if main else None,
                            "lio_map_aligned": _dist(clean, "lio", "map_to_mesh_mean_m")}
        rep["ratio8"][g] = {m: ate_ratio(pick("v8", g, pose_nodes="single"), pick("v8", g, pose_nodes=BASE_NODES), m) for m in MODES}
        rep["ratio8"][g]["cases"] = [case_row(d) for d in by.get(("v8", g), [])]
        for blk, var in (("nodes", "native"), ("phase", "r1_mid")):
            rep[blk][g] = {}
            for d in by.get((blk, g), []):
                c = d["case"]
                base = pick("main", g, noise_profile=c.get("noise_profile"), seed=c.get("seed"))
                rep[blk][g][f"{c.get('noise_profile')}-s{c.get('seed')}"] = {
                    "variant": var, "case": c.get("name"), "base_cases": [b["case"].get("name") for b in base],
                    **{m: compare(base, [d], m) for m in MODES}}
    # 资源与 r_map（主矩阵，逐组逐模式；共享 CPU 池上的开发参考，r_map 供 D01 冻结 slam_eval 内存模型）
    rep["resources"] = {}
    for g in GROUPS:
        rs_g = {}
        for m in MODES:
            rs = [r for d in by.get(("main", g), []) for r in d.get("runs", []) if r.get("mode") == m and r.get("ok")]
            res_of = [(r.get("resources") or {}) for r in rs]
            rs_g[m] = {"runs": len(rs), "rss_peak_gb": distribution(x.get("rss_peak_gb") for x in res_of),
                       "cores_equiv": distribution(x.get("cpu_cores_equiv") for x in res_of),
                       "wall_s": distribution(sum(v for v in (x.get("timing") or {}).values() if isinstance(v, int | float)) or None
                                              for x in res_of),
                       "r_map_slope": distribution((r.get("r_map") or {}).get("r_map_slope") for r in rs),
                       "r_map_peak": distribution((r.get("r_map") or {}).get("r_map_peak") for r in rs),
                       "cpus": sorted({r.get("cpus") for r in rs if r.get("cpus") is not None}),
                       "mem_gb": sorted({r.get("mem_gb") for r in rs if r.get("mem_gb") is not None})}
        rep["resources"][g] = rs_g
    # 运行有效性（全部算例、全部运行）
    fr = [(d["case"].get("name"), r.get("run"), run_frames(d, r)) for d in docs if classify(d.get("case") or {}) for r in d.get("runs", [])]
    incomplete = [{"case": c, "run": r, **f} for c, r, f in fr if f["complete"] is False]
    unknown = sum(1 for _, _, f in fr if f["complete"] is None)
    failed = [{"case": d["case"].get("name"), "run": r.get("run"), "code": r.get("code"), "why": r.get("why")}
              for d in docs if classify(d.get("case") or {}) for r in d.get("runs", []) if not r.get("ok")]
    # 判定范围：进入 D3-AC-14 总判定的运行（主组的主矩阵与 8 m/s 区块）；其余运行（Fast 附加组、AC-47 对比）只报告
    scope = {d["case"].get("name") for d in docs if classify(d.get("case") or {}) in (("main", MAIN_GROUP), ("v8", MAIN_GROUP))}
    fr_s = [x for x in fr if x[0] in scope]
    bad_s = [x for x in incomplete + failed if x["case"] in scope]
    rep["validity"] = {"runs": len(fr), "failed": failed, "incomplete": incomplete, "unknown": unknown,
                       "pass": None if not fr or unknown else bool(not incomplete and not failed),
                       "ac14_scope": {"cases": sorted(scope), "runs": len(fr_s), "bad": bad_s,
                                      "pass": None if not fr_s or any(f["complete"] is None for _, _, f in fr_s) else not bad_s}}
    # HKisland03 回归
    if regress:
        ate = (regress.get("ate") or {}).get("ate_rmse_m")
        rep["regress"] = {"ate_rmse_m": ate, "limit_m": AC14["hkisland03_ate_m_max"], "ok": regress.get("ok"),
                          "image": regress.get("image"), "created_utc": regress.get("created_utc"),
                          "resources": regress.get("resources"), "ate": regress.get("ate"),
                          "same_image": (regress.get("image") or {}).get("id") in images if images else None}
    else:
        rep["regress"] = None
    # D3-AC-14 总判定（主组 LIVO；Fast 档与 LIO 另报）
    j = (rep["groups"].get(MAIN_GROUP) or {}).get("judge") or {}
    r8 = rep["ratio8"].get(MAIN_GROUP, {}).get(AC14["main_mode"], {})
    rg = rep["regress"] or {}
    ver_ok = bool(rep["versions"]["reflectance_model"]) and bool(rep["versions"]["noise_table_version"])
    clauses = [
        {"clause": "main group (gz lockstep) LIVO: clean and realistic RPE100/ATE/map clauses", "pass": j.get("pass")},
        {"clause": "8 m/s per-point vs single-pose aligned ATE ratio >= 5 (main group, LIVO)", "value": r8.get("ratio"),
         "pass": r8.get("pass")},
        {"clause": "HKisland03 ATE <= 1.2 m", "value": rg.get("ate_rmse_m"),
         "pass": None if not rg else bool(rg.get("ok")) and rg.get("same_image") is not False},
        {"clause": "report states reflectance model and noise table version", "value": {k: rep["versions"][k] for k in
                                                                                       ("reflectance_model", "noise_table_version")},
         "pass": ver_ok if docs else None},
        {"clause": f"every judged run (main group: main matrix and 8 m/s) processed >= {MIN_FRAME_FRAC:.0%} of the bag LiDAR frames "
                   "and none failed", "value": {"runs": rep["validity"]["ac14_scope"]["runs"], "bad": len(rep["validity"]["ac14_scope"]["bad"])},
         "pass": rep["validity"]["ac14_scope"]["pass"]},
    ]
    v = [c["pass"] for c in clauses]
    rep["ac14"] = {"clauses": clauses, "pass": None if any(x is None for x in v) else all(v), "status": AC14["status"],
                   "note": "thresholds are the provisional r1-spline values; D01 freezes them by ADR from this matrix"}
    rep["ac14_event0_anchor"] = event0_variant(rep, clauses)
    return rep


MAP_CLAUSE = "clean map-to-mesh mean (median over runs) <= 0.15 m"


def event0_variant(rep: Mapping, clauses: Sequence[Mapping]) -> dict:
    """拟议口径（不计入判定，供 D01 以 ADR 决定）：clean 地图到网格距离改以事件零点锚定（`reanchor`），其余条款不变。
    缺少锚定点诊断数据时结论为 None。"""
    j = (rep["groups"].get(MAIN_GROUP) or {}).get("judge") or {}
    jm = (j.get("modes") or {}).get(AC14["main_mode"]) or {}
    clean = [r for r in (rep["groups"].get(MAIN_GROUP) or {}).get("cases", []) if r["noise"] == "clean"]
    d = clean[0]["modes"].get(AC14["main_mode"], {}).get("map_to_mesh_mean_event0_m", {}) if clean else {}
    lim = AC14["clean"]["map_to_mesh_mean_m_max"]
    map_ok = None if not d.get("n") else bool(d["median"] <= lim)
    others = [c["pass"] for c in jm.get("checks", []) if c["clause"] != MAP_CLAUSE]
    main_ok = None if map_ok is None or any(x is None for x in others) else (map_ok and all(others))
    v = [main_ok] + [c["pass"] for c in clauses[1:]]
    return {"map_to_mesh_mean_event0_m": d.get("median"), "limit_m": lim, "map_pass": map_ok, "main_group_pass": main_ok,
            "pass": None if any(x is None for x in v) else all(v), "counted": False,
            "note": "proposed anchoring at the event zero (end of the pre-roll window); not counted until D01 decides by ADR"}


def load_docs(out: Path) -> tuple[list[dict], list[str]]:
    paths = sorted((Path(out) / "runs").glob("*/metrics.json"))
    paths = [p for p in paths if ".prev-" not in p.parent.name]
    docs = []
    for p in paths:
        d = read_json(p)
        if not d.get("runs"):
            continue
        for r in d["runs"]:                                    # 锚定点敏感性诊断（若已计算）
            ra = p.parent / "runs" / str(r.get("run")) / REANCHOR
            if ra.is_file():
                r["reanchor"] = read_json(ra)
        docs.append(d)
    return docs, [str(p) for p in paths]


# ---------------------------------------------------------------------------------------------------------------- Markdown
def _f(x, nd: int = 3, pct: bool = False) -> str:
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "通过" if x else "不通过"
    return f"{x:.{nd}f}%" if pct else f"{x:.{nd}f}"


def _verdict(p) -> str:
    return "未定" if p is None else ("通过" if p else "不通过")


def render_md(rep: Mapping) -> str:
    """实测记录用的表格（中文；数值保留 3 位小数，RPE100 以 % 表示）。"""
    L: list[str] = []
    L.append("### 主矩阵（6 m/s，`pose_nodes = 11`，`slam_cam` 相位 `tick_end`）\n")
    L.append("| 组 | 算例 | 模式 | 成功 / 运行 | RPE100 段均值（中位，各次） | 对齐 ATE 中位（m） | 锚定最大漂移中位（m） | 地图到网格均值中位（锚定 / 对齐，m） "
             "| 处理帧比例最小值 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for g, gd in rep["groups"].items():
        for row in gd["cases"]:
            for m, md in row["modes"].items():
                runs = " / ".join(_f(x, 3) for x in md["rpe100_runs"])
                L.append(f"| {g} | {row['case']} | {m.upper()} | {md['ok']} / {md['runs']} | {_f(md['rpe100_pct'].get('median'), 3, True)}"
                         f"（{runs}） | {_f(md['ate_aligned_rmse_m'].get('median'))} | {_f(md['drift_max_m'].get('median'))} | "
                         f"{_f(md['map_to_mesh_mean_m'].get('median'))} / {_f(md['map_to_mesh_mean_aligned_m'].get('median'))} | "
                         f"{_f(md['frames']['min_frac'], 3)} |")
    L.append("\n### D3-AC-14 判定（各组；LIVO 为主口径，LIO 单列）\n")
    L.append("| 组 | 模式 | 条款 | 实测 | 阈值 | 结论 |")
    L.append("|---|---|---|---|---|---|")
    for g, gd in rep["groups"].items():
        j = gd.get("judge") or {}
        for m, jm in (j.get("modes") or {}).items():
            for c in jm["checks"]:
                val = c["value"]
                if isinstance(val, list):
                    val = " / ".join(_f(x, 4) for x in val)
                elif isinstance(val, dict):
                    val = ", ".join(f"{k} {v}" for k, v in val.items())
                else:
                    val = _f(val, 4)
                L.append(f"| {g} | {m.upper()} | {c['clause']} | {val} | {c['limit']} | {_verdict(c['pass'])} |")
    L.append("\n### 8 m/s 逐点位姿与单一位姿（clean 种子 1，对齐 ATE 跨重复中位数）\n")
    L.append(f"| 组 | 模式 | 单一位姿 ATE（m） | 单一位姿发散次数（ATE > {DIVERGED_M:g} m） | 逐点（11 节点）ATE（m） | 比值 "
             "| 单一位姿 / 逐点 RPE100 | 结论（≥ 5） |")
    L.append("|---|---|---|---|---|---|---|---|")
    for g, gd in rep["ratio8"].items():
        for m in MODES:
            r = gd.get(m) or {}
            L.append(f"| {g} | {m.upper()} | {_f((r.get('single') or {}).get('median'))} | {r.get('single_diverged', 0)} / "
                     f"{r.get('single_runs', 0)} | {_f((r.get('per_point') or {}).get('median'))} | "
                     f"{_f(r.get('ratio'), 2)} | {_f((r.get('rpe100_single') or {}).get('median'), 3, True)} / "
                     f"{_f((r.get('rpe100_per_point') or {}).get('median'), 3, True)} | {_verdict(r.get('pass'))} |")
    for blk, title in (("nodes", "`pose_nodes = native` 与 `11`"), ("phase", "`slam_cam` 相位 `r1_mid` 与 `tick_end`")):
        L.append(f"\n### {title}（同组、同噪声档、同种子；中位数，相对差 = 变体 / 基线 − 1）\n")
        L.append("| 组 | 档与种子 | 模式 | RPE100 基线 → 变体 | 对齐 ATE（m） | 地图到网格均值（m） |")
        L.append("|---|---|---|---|---|---|")
        for g, gd in rep[blk].items():
            for key, cd in gd.items():
                for m in MODES:
                    c = cd.get(m) or {}

                    def cell(k: str, pct: bool = False, c=c) -> str:
                        x = c.get(k) or {}
                        rel = x.get("rel_change")
                        return (f"{_f((x.get('base') or {}).get('median'), 3, pct)} → {_f((x.get('variant') or {}).get('median'), 3, pct)}"
                                f"（{'—' if rel is None else f'{rel * 100:+.0f}%'}）")
                    L.append(f"| {g} | {key} | {m.upper()} | {cell('rpe100_pct', True)} | {cell('ate_aligned_rmse_m')} | "
                             f"{cell('map_to_mesh_mean_m')} |")
    if any(md["map_to_mesh_mean_event0_m"].get("n") for gd in rep["groups"].values() for row in gd["cases"]
           for md in row["modes"].values()):
        L.append("\n### 锚定点敏感性（诊断，不进入判定；中位数）\n")
        L.append("| 组 | 算例 | 模式 | 首个估计锚定时事件零点处姿态误差（°） | 锚定最大漂移：首个估计 → 事件零点（m） "
                 "| 地图到网格均值：首个估计 → 事件零点 → 对齐（m） |")
        L.append("|---|---|---|---|---|---|")
        for g, gd in rep["groups"].items():
            for row in gd["cases"]:
                for m, md in row["modes"].items():
                    if not md["map_to_mesh_mean_event0_m"].get("n"):
                        continue
                    L.append(f"| {g} | {row['case']} | {m.upper()} | {_f(md['att_err_at_event0_deg'].get('median'))} | "
                             f"{_f(md['drift_max_m'].get('median'))} → {_f(md['drift_max_event0_m'].get('median'))} | "
                             f"{_f(md['map_to_mesh_mean_m'].get('median'))} → {_f(md['map_to_mesh_mean_event0_m'].get('median'))} → "
                             f"{_f(md['map_to_mesh_mean_aligned_m'].get('median'))} |")
    if rep.get("resources"):
        L.append("\n### 资源与 r_map（主矩阵；共享 CPU 池，开发参考；中位数（最小–最大））\n")
        L.append("| 组 | 模式 | 运行数 | --cpus / --memory（GB） | 核当量 | RSS 峰值（GB） | 单次墙钟（回放 + 排空 + 存图，s） "
                 "| r_map 斜率口径（点/s） | r_map 峰值口径（点/s） |")
        L.append("|---|---|---|---|---|---|---|---|---|")

        def mm(d: Mapping, nd: int = 2) -> str:
            return "—" if not d.get("n") else f"{d['median']:.{nd}f}（{d['min']:.{nd}f}–{d['max']:.{nd}f}）"
        for g, gr in rep["resources"].items():
            for m, x in gr.items():
                if not x["runs"]:
                    continue
                L.append(f"| {g} | {m.upper()} | {x['runs']} | {'/'.join(f'{c:g}' for c in x['cpus'])} / "
                         f"{'/'.join(f'{c:g}' for c in x['mem_gb'])} | {mm(x['cores_equiv'])} | {mm(x['rss_peak_gb'])} | "
                         f"{mm(x['wall_s'], 0)} | {mm(x['r_map_slope'], 0)} | {mm(x['r_map_peak'], 0)} |")
    rg = rep.get("regress")
    if rg:
        L.append("\n### HKisland03 真机回归\n")
        L.append(f"ATE RMSE {_f(rg['ate_rmse_m'])} m（阈值 ≤ {rg['limit_m']} m），结论 {_verdict(rg['ok'])}；镜像 "
                 f"`{(rg.get('image') or {}).get('id', '')[:19]}`，与矩阵运行镜像一致：{_verdict(rg.get('same_image'))}。")
    L.append("\n### D3-AC-14 总判定（主组 gz 锁步，LIVO）\n")
    L.append("| 条款 | 实测 | 结论 |")
    L.append("|---|---|---|")
    for c in rep["ac14"]["clauses"]:
        v = c.get("value")
        v = _f(v, 3) if isinstance(v, float) else ("—" if v is None else str(v))
        L.append(f"| {c['clause']} | {v} | {_verdict(c['pass'])} |")
    va = rep["validity"]
    if va["incomplete"] or va["failed"]:
        L.append("\n运行有效性（全部运行）：" + "；".join(
            [f"{x['case']}/{x['run']} 处理帧比例 {x['frac']:.3f}" for x in va["incomplete"]]
            + [f"{x['case']}/{x['run']} 失败（{x['code']}）" for x in va["failed"]]) + "。")
    L.append(f"\n总结论：{_verdict(rep['ac14']['pass'])}（阈值状态 {rep['ac14']['status']}）。版本：反射率模型 "
             f"{', '.join(rep['versions']['reflectance_model']) or '—'}，噪声档参数表 {', '.join(rep['versions']['noise_table_version']) or '—'}。")
    ev = rep.get("ac14_event0_anchor") or {}
    L.append(f"\n拟议口径（不计入）：clean 地图到网格均值以事件零点锚定为 {_f(ev.get('map_to_mesh_mean_event0_m'))} m"
             f"（阈值 ≤ {ev.get('limit_m')} m，{_verdict(ev.get('map_pass'))}），按此口径主组 {_verdict(ev.get('main_group_pass'))}，"
             f"总结论 {_verdict(ev.get('pass'))}。")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="slam_matrix.py", description="FAST-LIVO2 RPE100 matrix and HKisland03 regression (D3-AC-14, 47)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def matrix_args(p):
        p.add_argument("--traj-root", required=True, help="slam_traj.py fly 的轨迹根目录（含 gz_lockstep、fast 与 *_v8）")
        p.add_argument("--cases-root", action="append", default=[], help="已有算例根目录（可重复，按序查找）")
        p.add_argument("--out", required=True)
        p.add_argument("--block", action="append", choices=BLOCKS)
        p.add_argument("--group", action="append", choices=tuple(GROUPS))
        p.add_argument("--name", action="append")
        p.add_argument("--seeds", default="1-5", help="realistic 种子（主矩阵）")

    p = sub.add_parser("plan")
    matrix_args(p)
    p = sub.add_parser("cases")
    matrix_args(p)
    p.add_argument("--world", required=True)
    p.add_argument("--no-camera", action="store_true")
    p.add_argument("--max-frames", type=int)
    p = sub.add_parser("run")
    matrix_args(p)
    p.add_argument("--jobs", type=int, default=3, help="并行算例数")
    p.add_argument("--parallel", type=int, default=6, help="每个算例内并行的 FAST-LIVO2 容器数（slam_eval --parallel）")
    p.add_argument("--modes", default="livo,lio")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--rate", type=float, default=4.0)
    p.add_argument("--cpus", type=float, help="每个 FAST-LIVO2 容器的 --cpus（缺省取 slam_eval 内存模型的型号核数）")
    p.add_argument("--image", default="d3-livo:dev")
    p.add_argument("--name-prefix", default="SL3")
    p.add_argument("--require-regress")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("regress")
    p.add_argument("--bag", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--image", default="d3-livo:dev")
    p.add_argument("--name-prefix", default="SL3")
    p = sub.add_parser("reanchor")
    p.add_argument("--out", required=True)
    p.add_argument("--name", action="append")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("report")
    p.add_argument("--out", required=True)
    p.add_argument("--regress", help="regress.json（缺省 <out>/hk/regress.json）")
    return ap


def _parse_seeds(s: str) -> list[int]:
    out = []
    for part in s.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def _specs_from_args(a) -> list[dict]:
    out = Path(a.out)
    roots = [Path(r) for r in a.cases_root] + [out / "cases"]
    specs = matrix_specs(groups=a.group or tuple(GROUPS), realistic_seeds=_parse_seeds(a.seeds), blocks=a.block or BLOCKS)
    return select(resolve(specs, Path(a.traj_root), roots, out / "cases"), names=a.name)


def main(argv: Sequence[str] | None = None) -> int:
    a = build_parser().parse_args(argv)

    def log(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    if a.cmd == "regress":
        out = Path(a.out) / "hk"
        out.mkdir(parents=True, exist_ok=True)
        with open(Path(a.out) / "hk.log", "w") as fh:
            rc = subprocess.run(slam_eval_regress_argv(Path(a.bag), out, name_prefix=a.name_prefix, image=a.image), stdout=fh,
                                stderr=subprocess.STDOUT, check=False).returncode
        print(json.dumps({"rc": rc, "regress": str(out / "regress.json")}))
        return EXIT_OK if rc == 0 else EXIT_FAIL
    if a.cmd == "reanchor":
        res = reanchor_all(Path(a.out), names=a.name, force=a.force, log=log)
        print(json.dumps({"runs": len(res)}))
        return EXIT_OK
    if a.cmd == "report":
        out = Path(a.out)
        docs, paths = load_docs(out)
        rp = Path(a.regress) if a.regress else out / "hk" / "regress.json"
        rep = build_report(docs, read_json(rp) if rp.is_file() else None)
        rep["inputs"] = {"metrics": paths, "regress": str(rp) if rp.is_file() else None}
        write_json(out / "matrix.json", rep)
        (out / "matrix.md").write_text(render_md(rep), encoding="utf-8")
        print(json.dumps({"ac14": rep["ac14"]["pass"], "cases": len(docs), "validity": rep["validity"]["pass"]}))
        return EXIT_OK if rep["ac14"]["pass"] else EXIT_FAIL
    try:
        specs = _specs_from_args(a)
    except (FileNotFoundError, ValueError) as e:
        log(f"error: {e}")
        return EXIT_ARGS
    out = Path(a.out)
    if a.cmd == "plan":
        doc = plan_doc(specs, out)
        write_json(out / "plan.json", doc)
        print(json.dumps(doc["counts"]))
        return EXIT_OK
    stale = [s["name"] for s in specs if s["stale"]]
    if stale:
        log(f"warning: stale cases ignored (rebuilt under {out / 'cases'}): {stale}")
    if a.cmd == "cases":
        res = build_missing(specs, Path(a.world), out / "cases", camera=not a.no_camera, max_frames=a.max_frames, log=log)
        print(json.dumps({k: v.get("frames") for k, v in res.items()}))
        return EXIT_OK
    missing = [s["name"] for s in specs if not s["exists"]]
    if missing:
        log(f"warning: cases not built yet (slam_matrix.py cases): {missing}")
    res = run_matrix(specs, out, jobs=a.jobs, force=a.force, log=log, modes=a.modes, repeats=a.repeats, rate=a.rate, parallel=a.parallel,
                     name_prefix=a.name_prefix, image=a.image, cpus=a.cpus, require_regress=a.require_regress)
    write_json(out / f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json", res)
    bad = {k: v for k, v in res.items() if v["status"] not in ("completed", "skipped_completed")}
    print(json.dumps({"cases": len(res), "not_completed": bad and {k: v["status"] for k, v in bad.items()}}))
    return EXIT_OK if not bad else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
