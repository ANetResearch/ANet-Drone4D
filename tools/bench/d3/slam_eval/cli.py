"""`slam_eval` 命令行（M27 §6.6.2 时序；子命令见包说明）。

    PYTHONPATH=python python tools/bench/d3/slam_eval run --case <算例目录> --out <输出目录> [--mesh <世界包或网格>]
        [--modes livo,lio] [--repeats 3] [--rate 4] [--image d3-livo:dev] [--parallel 4] [--name-prefix d3-slam]
        [--mem-gb G | --mem-scale S] [--cpus C] [--eval-runner local|docker] [--eval-image d3-sensorsvc:dev] [--repo <仓库根>]
        [--no-pcd] [--l0] [--require-regress <regress.json>] [--watchdog-s 600]
    ... eval --case <算例> --run <运行目录> --out <eval.json> [--mesh ...] [--ref <参考点 ply>] [--l0-out <l0 文件>]
    ... ref --case <算例> --out <参考点 ply>
    ... regress --bag <HKisland_GNSS03.bag> --out <目录> [--rtk <tum>] [--start 72] [--rate 1] [--image d3-livo:dev]
    ... judge --clean <metrics.json>... --realistic <metrics.json>... --out <judge.json>
    ... memmodel --model mid360 --flight-s 360 | --calibrate <运行目录>
    ... rebag <源 bag> <目标 bag>
    ... fleet --l0 <l0 文件>... [--mesh ...] --out <fleet.json>

退出码：0 完成且无失败运行（`judge` / `regress` 为判定通过）；1 有运行失败或判定不通过；2 前置条件不满足（镜像回归未通过等）；
3 bag 合同违反（642）。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

from . import EVAL_SCHEMA, EVAL_VERSION, JUDGE_SCHEMA, REGRESS_SCHEMA, SCHEMA
from .case import Case, load_case, rebag
from .livo import (
    MODES,
    LivoRun,
    image_id,
    parse_log,
    patch_yaml,
    read_resources,
    read_source,
    run_parallel,
    run_with_retry,
)
from .memmodel import MEM, calibrate_r_map, job_resources
from .report import AC14, judge_ac14, summarize_runs
from .traj import R_to_quat, TrajInterp, Tum, load_tum, positions_ate, save_tum, traj_metrics

HK_CFG = "image:/ws/src/FAST-LIVO2/config/MARS_LVIG.yaml"
HK_CAM = "image:/ws/src/FAST-LIVO2/config/camera_MARS_LVIG.yaml"
REPO = Path(__file__).resolve().parents[4]


def _utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017 — dmax 宿主为 Python 3.10


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, Path):
        return str(o)
    return o


def _write(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_jsonable(doc), indent=1, ensure_ascii=False))
    return path


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", s)


def _trajectory_file(run_dir: Path) -> Path:
    res = Path(run_dir) / "Log" / "result"
    fs = sorted(res.glob("*.txt")) if res.is_dir() else []
    if not fs:
        raise FileNotFoundError(f"{res}: no FAST-LIVO2 trajectory")
    return fs[0]


# ---------------------------------------------------------------------------------------------------------------- eval
def evaluate_run(case: Case, run_dir: Path, *, mesh_spec: str | None = None, ref_path: Path | None = None,
                 l0_out: Path | None = None) -> dict:
    """一次运行的 `eval_sim`：轨迹指标、锚定与对齐坐标下的地图指标、可选 L0 地图；写 `est_world.tum` 与 `traj_err.txt`。"""
    import time

    t0 = time.perf_counter()
    run_dir = Path(run_dir)
    est = load_tum(_trajectory_file(run_dir))
    if case.gt_imu is None:
        raise FileNotFoundError(f"{case.dir}: gt_imu.tum missing")
    gt = TrajInterp(load_tum(case.gt_imu))
    tm = traj_metrics(est, gt)
    T_W_F, (R_al, t_al) = tm["_T_W_F"], tm["_align"]
    Tw = T_W_F @ est.T()
    save_tum(run_dir / "est_world.tum", Tum(est.t, Tw[:, :3, 3], R_to_quat(Tw[:, :3, :3])))
    _, pg = gt.at(est.t)
    pw = Tw[:, :3, 3]
    np.savetxt(run_dir / "traj_err.txt", np.stack([est.t - est.t[0], np.linalg.norm(pw - pg, axis=1),
                                                    np.linalg.norm(pw @ R_al.T + t_al - pg, axis=1)], 1),
               fmt="%.4f", header="t_s drift_anchored_m err_aligned_m")
    out: dict = {"schema": EVAL_SCHEMA, "eval_version": EVAL_VERSION, "run": str(run_dir), "traj": tm,
                 "T_W_F": T_W_F.tolist(), "align": {"R": R_al.tolist(), "t": t_al.tolist()}}
    mesh_spec = mesh_spec or case.mesh
    pcd = run_dir / "Log" / "pcd" / "all_raw_points.pcd"
    if mesh_spec and pcd.exists():
        from .mapeval import map_metrics, open_mesh, read_cloud, transform_points, voxel_down

        mesh = open_mesh(mesh_spec, None if case.mesh_transform is None else np.asarray(case.mesh_transform, np.float64))
        out["mesh"] = mesh.info()
        ref, ref_src = None, None
        if ref_path is not None and Path(ref_path).exists():
            ref, ref_src = read_cloud(ref_path)[0], str(ref_path)
        elif case.gt_lidar_ref is not None:
            ref, ref_src = read_cloud(case.gt_lidar_ref)[0], str(case.gt_lidar_ref)
        P, _ = read_cloud(pcd)
        out["map_raw_points"] = len(P)
        Pd, _ = voxel_down(transform_points(P, T_W_F), None, 0.1)
        out["map"] = map_metrics(Pd, mesh, ref)
        out["map_aligned"] = map_metrics(Pd @ R_al.T + t_al, mesh, ref)
        out["ref"] = {"source": ref_src, "points": None if ref is None else len(ref)}
    else:
        out["map"] = None
        out["map_skipped"] = "no mesh" if not mesh_spec else "no map pcd (pcd_save_en false or save failed)"
    if l0_out is not None:
        from awr.bench.eval.lidar_map import distance_stats, mesh_distance

        from .mapeval import l0_from_bag, open_mesh

        m = l0_from_bag(case.bag, gt, case.T_I_L, uav=str(case.meta.get("uav") or case.name))
        m.save(Path(l0_out))
        st = m.stats()
        if mesh_spec:
            st["mesh_distance"] = distance_stats(mesh_distance(m.occupied_centers(), open_mesh(mesh_spec), 5.0), m.params.voxel_m)
        out["l0"] = {"path": str(l0_out), **st}
    out["eval_s"] = time.perf_counter() - t0
    return out


def _eval_docker_argv(args, case: Case, run_dir: Path, out_json: Path, ref: Path | None, l0: Path | None, mesh: str | None) -> list[str]:
    repo = Path(args.repo).resolve()
    mounts = {str(repo), str(case.dir), str(Path(run_dir).resolve()), str(Path(case.bag).resolve().parent)}
    if mesh:
        mounts.add(str(Path(mesh).resolve() if Path(mesh).is_dir() else Path(mesh).resolve().parent))
    if ref is not None:
        mounts.add(str(Path(ref).resolve().parent))
    argv = ["docker", "run", "--rm", "--name", _safe(f"{args.name_prefix}-eval-{case.name}-{Path(run_dir).name}"), "--network", "none",
            "--memory", f"{args.eval_mem_gb}g", "--cpus", str(args.eval_cpus), "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
            "-e", f"PYTHONPATH={repo}/python:{repo}/tools/bench/d3",
            "-e", f"AWR_CONTRACTS_DIR={repo}/packages/contracts", "-w", str(repo)]
    for m in sorted(mounts):
        argv += ["-v", f"{m}:{m}"]
    cmd = ["python", "-m", "slam_eval", "eval", "--case", str(case.dir), "--run", str(Path(run_dir).resolve()), "--out", str(out_json)]
    if mesh:
        cmd += ["--mesh", str(Path(mesh).resolve())]
    if ref is not None:
        cmd += ["--ref", str(Path(ref).resolve())]
    if l0 is not None:
        cmd += ["--l0-out", str(l0)]
    return [*argv, args.eval_image, *cmd]


def _run_eval(args, case: Case, run_dir: Path, ref: Path | None, l0: Path | None) -> dict:
    out_json = Path(run_dir) / "eval.json"
    if args.eval_runner == "local":
        doc = evaluate_run(case, run_dir, mesh_spec=args.mesh, ref_path=ref, l0_out=l0)
        _write(out_json, doc)
        return json.loads(out_json.read_text())
    r = subprocess.run(_eval_docker_argv(args, case, run_dir, out_json, ref, l0, args.mesh or case.mesh), capture_output=True, text=True)
    (Path(run_dir) / "eval.log").write_text(r.stdout + r.stderr)
    if r.returncode != 0 or not out_json.exists():
        raise RuntimeError(f"eval failed ({r.returncode}): {r.stderr.strip()[-400:]}")
    return json.loads(out_json.read_text())


def cmd_eval(a) -> int:
    case = load_case(a.case, mesh=a.mesh)
    doc = evaluate_run(case, Path(a.run), mesh_spec=a.mesh, ref_path=Path(a.ref) if a.ref else None,
                       l0_out=Path(a.l0_out) if a.l0_out else None)
    _write(Path(a.out), doc)
    t = doc["traj"]
    print(json.dumps({"rpe100_pct": t["rpe100_pct"], "ate_aligned_rmse_m": t["ate_aligned_rmse_m"],
                      "map_to_mesh_mean_m": (doc.get("map") or {}).get("acc_mean_m")}))
    return 0


def cmd_ref(a) -> int:
    from .livo import extrinsic_T_I_L
    from .mapeval import gt_lidar_reference

    case = load_case(a.case)
    import open3d as o3d

    P, n_raw = gt_lidar_reference(case.bag, TrajInterp(load_tum(case.gt_imu)), extrinsic_T_I_L(case.livo_yaml.read_text()))
    o3d.io.write_point_cloud(str(a.out), o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P)))
    print(json.dumps({"ref": str(a.out), "points": len(P), "raw_points": n_raw}))
    return 0


# ---------------------------------------------------------------------------------------------------------------- run
def _prepare(case: Case, out: Path, mode: str, k: int, a, res: dict, mem_gb: float, cpus: float) -> LivoRun:
    rd = out / "runs" / f"{mode}-r{k}"
    (rd / "Log").mkdir(parents=True, exist_ok=True)
    kv = {**MODES[mode], "evo/seq_name": "est", "evo/pose_output_en": True, "pcd_save/pcd_save_en": not a.no_pcd}
    (rd / "livo.yaml").write_text(patch_yaml(case.livo_yaml.read_text(), kv))
    (rd / "camera.yaml").write_text(case.camera_yaml.read_text())
    return LivoRun(name=_safe(f"{a.name_prefix}-{case.name}-{mode}-r{k}"), mode=mode, repeat=k, run_dir=rd, bag=case.bag, image=a.image,
                   mem_gb=mem_gb, cpus=cpus, rate=a.rate, quiet_s=a.quiet_s)


def _run_record(lr: LivoRun, res: dict, flight_s: float) -> dict:
    rs = read_resources(lr.run_dir)
    rec = {"run": f"{lr.mode}-r{lr.repeat}", "mode": lr.mode, "repeat": lr.repeat, "ok": res["ok"], "code": res["code"], "why": res["why"],
           "container": lr.name, "attempts": res.get("attempts", []), "mem_gb": lr.mem_gb, "cpus": lr.cpus, "rate": lr.rate,
           "resources": {k: v for k, v in rs.items() if not k.startswith("_")}, "per_frame": parse_log(Path(lr.run_dir) / "livo.log")}
    st, a = rs.get("_status") or {}, rs.get("_res")
    if a is not None and len(a) >= 3 and st.get("play_t0") and st.get("play_t1"):
        with contextlib.suppress(ValueError):
            rec["r_map"] = calibrate_r_map(a[:, 0], a[:, 1] * 1024.0, rate=lr.rate, play_t0_s=st["play_t0"], play_t1_s=st["play_t1"],
                                           flight_s=flight_s)
    return rec


def cmd_run(a) -> int:
    from awr.sensorsvc.errors import SensorSvcError
    from awr.sensorsvc.slam.bag import verify_slam_bag

    case = load_case(a.case, mesh=a.mesh)
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    if any(m not in MODES for m in modes):
        raise SystemExit(f"modes must be a subset of {sorted(MODES)}")
    doc: dict = {"schema": SCHEMA, "created_utc": _utc(), "case": {**case.describe(), "name": case.name},
                 "job": {"modes": modes, "main_mode": AC14["main_mode"], "repeats": a.repeats, "rate": a.rate, "parallel": a.parallel,
                         "pcd_save": not a.no_pcd, "l0": a.l0, "watchdog_s": a.watchdog_s},
                 "image": {"tag": a.image, "id": image_id(a.image)}, "thresholds": AC14, "mem_model": MEM.as_dict()}
    mp = out / "metrics.json"
    if a.require_regress:
        rg = json.loads(Path(a.require_regress).read_text())
        if not rg.get("ok") or rg.get("image", {}).get("id") != doc["image"]["id"]:
            doc.update({"status": "rejected", "why": "image has no passing slam_regress (M27-FR-110)", "regress": rg.get("ate")})
            _write(mp, doc)
            print(f"rejected: image {a.image} has no passing regression", file=sys.stderr)
            return 2
        doc["regress"] = {"path": a.require_regress, "ate_rmse_m": rg["ate"]["ate_rmse_m"], "ok": True}
    try:
        rep = verify_slam_bag(case.bag, imu_period_ns=None if a.no_imu_period_check else 5_000_000)
        doc["contract"] = {"ok": True, **rep.as_dict()}
    except SensorSvcError as e:
        doc.update({"status": "failed", "code": e.code, "why": str(e), "contract": {"ok": False, "code": e.code, "detail": e.detail}})
        _write(mp, doc)
        print(str(e), file=sys.stderr)
        return 3
    flight_s = float(case.flight_s or (rep.lidar_t1_ns - rep.lidar_t0_ns) / 1e9 + 0.1)
    res = job_resources(case.lidar_model, flight_s, min_gb=a.min_mem_gb)
    mem_gb = float(a.mem_gb) if a.mem_gb else res["mem_model_gb"] * a.mem_scale if a.mem_scale != 1.0 else float(res["mem_gb"])
    cpus = float(a.cpus) if a.cpus else res["cpus"]
    doc["resources"] = {**res, "mem_gb_applied": mem_gb, "cpus_applied": cpus, "mem_scale": a.mem_scale}
    runs = [_prepare(case, out, m, k, a, res, mem_gb, cpus) for m in modes for k in range(1, a.repeats + 1)]
    _write(out / "plan.json", {"runs": [r.as_dict() for r in runs]})
    results = run_parallel(runs, a.parallel, retries=a.retries, watchdog_s=a.watchdog_s)
    ref = None
    if (a.mesh or case.mesh) and case.gt_lidar_ref is None and not a.no_pcd:
        ref = out / "gt_lidar_ref.ply"
        if not ref.exists():
            if a.eval_runner == "local":
                cmd_ref(argparse.Namespace(case=str(case.dir), out=str(ref)))
            else:
                argv = _eval_docker_argv(a, case, out, out / "unused.json", None, None, None)
                i = argv.index("eval")
                argv = [*argv[:i], "ref", "--case", str(case.dir), "--out", str(ref)]
                r = subprocess.run(argv, capture_output=True, text=True)
                (out / "ref.log").write_text(r.stdout + r.stderr)
                if r.returncode != 0:
                    ref = None
    records = []
    for lr, rr in zip(runs, results, strict=True):
        rec = _run_record(lr, rr, flight_s)
        if rr["ok"]:
            try:
                ev = _run_eval(a, case, lr.run_dir, ref, (Path(lr.run_dir) / "l0.npz") if a.l0 and lr.repeat == 1 and lr.mode == modes[0]
                               else None)
                rec.update({k: ev.get(k) for k in ("traj", "map", "map_aligned", "ref", "mesh", "l0", "map_raw_points", "eval_s")})
            except Exception as e:
                rec.update({"ok": False, "code": 684, "why": f"eval_failed: {e}"})
        records.append(rec)
    doc["runs"] = records
    doc["summary"] = summarize_runs(records)
    nf = sum(1 for r in records if not r["ok"])
    doc["status"] = "completed" if nf == 0 else ("partial" if nf < len(records) else "failed")
    doc["finished_utc"] = _utc()
    _write(mp, doc)
    print(json.dumps({"metrics": str(mp), "status": doc["status"],
                      "summary": {m: {k: s[k].get("median") for k in ("rpe100_pct", "ate_aligned_rmse_m", "map_to_mesh_mean_m")}
                                  for m, s in doc["summary"].items()}}))
    return 0 if nf == 0 else 1


# ---------------------------------------------------------------------------------------------------------------- regress
def cmd_regress(a) -> int:
    out = Path(a.out).resolve()
    (out / "run" / "Log").mkdir(parents=True, exist_ok=True)
    bag = Path(a.bag).resolve()
    doc: dict = {"schema": REGRESS_SCHEMA, "created_utc": _utc(), "dataset": {"name": a.dataset, "bag": str(bag)},
                 "image": {"tag": a.image, "id": image_id(a.image)}, "threshold": {"ate_rmse_m_max": AC14["hkisland03_ate_m_max"]}}
    rtk = Path(a.rtk) if a.rtk else out / "rtk_enu.tum"
    if not rtk.exists():
        r = subprocess.run(["docker", "run", "--rm", "--name", _safe(f"{a.name_prefix}-rtk"), "--network", "none", "--memory", "4g",
                            "--cpus", "1", "-v", f"{bag.parent}:/bag:ro", "-v", f"{out}:/out", "--entrypoint", "bash", a.image, "-c",
                            f"source /opt/ros/noetic/setup.bash && python3 /opt/d3-livo/extract_rtk.py /bag/{bag.name} /out/{rtk.name} "
                            f"{a.rtk_topic}"], capture_output=True, text=True)
        (out / "rtk.log").write_text(r.stdout + r.stderr)
        if r.returncode != 0:
            doc.update({"ok": False, "why": "rtk extraction failed"})
            _write(out / "regress.json", doc)
            return 1
    rd = out / "run"
    cfg = patch_yaml(read_source(a.cfg, a.image), {"evo/pose_output_en": True, "evo/seq_name": "est", "pcd_save/pcd_save_en": not a.no_pcd})
    (rd / "livo.yaml").write_text(cfg)
    (rd / "camera.yaml").write_text(read_source(a.cam, a.image))
    res = job_resources(a.lidar_model, a.flight_s, min_gb=a.min_mem_gb, min_cpus=a.min_cpus)
    lr = LivoRun(name=_safe(f"{a.name_prefix}-regress"), mode="livo", repeat=1, run_dir=rd, bag=bag, image=a.image,
                 mem_gb=float(a.mem_gb or res["mem_gb"]), cpus=float(a.cpus or res["cpus"]), rate=a.rate, start_s=a.start,
                 quiet_s=a.quiet_s)
    doc["resources"] = {**res, "mem_gb_applied": lr.mem_gb, "cpus_applied": lr.cpus, "rate": lr.rate, "start_s": lr.start_s}
    rr = run_with_retry(lr, retries=a.retries, watchdog_s=a.watchdog_s)
    rec = _run_record(lr, rr, a.flight_s)
    doc["run"] = rec
    if not rr["ok"]:
        doc.update({"ok": False, "code": rr["code"], "why": rr["why"]})
        _write(out / "regress.json", doc)
        return 1
    ref = load_tum(rtk)
    est = load_tum(_trajectory_file(rd))
    doc["ate"] = positions_ate(est, ref.t, ref.p)
    doc["ok"] = bool(doc["ate"]["ate_rmse_m"] <= AC14["hkisland03_ate_m_max"])
    doc["note"] = "RTK antenna lever arm not compensated (not provided by MARS-LVIG); error includes ~0.1-0.3 m lever arm and RTK noise"
    _write(out / "regress.json", doc)
    print(json.dumps({"ok": doc["ok"], "ate_rmse_m": doc["ate"]["ate_rmse_m"], "image": doc["image"]}))
    return 0 if doc["ok"] else 1


# ---------------------------------------------------------------------------------------------------------------- 其他
def cmd_judge(a) -> int:
    clean = [json.loads(Path(p).read_text()) for p in a.clean]
    real = [json.loads(Path(p).read_text()) for p in a.realistic]
    j = judge_ac14(clean, real)
    doc = {"schema": JUDGE_SCHEMA, "created_utc": _utc(), "inputs": {"clean": a.clean, "realistic": a.realistic}, **j}
    _write(Path(a.out), doc)
    print(json.dumps({"pass": j["pass"], "modes": {m: v["pass"] for m, v in j["modes"].items()}}))
    return 0 if j["pass"] else 1


def cmd_memmodel(a) -> int:
    if a.calibrate:
        rs = read_resources(a.calibrate)
        st, arr = rs["_status"], rs["_res"]
        cal = calibrate_r_map(arr[:, 0], arr[:, 1] * 1024.0, rate=float(st.get("rate") or a.rate), play_t0_s=st["play_t0"],
                              play_t1_s=st["play_t1"], flight_s=a.flight_s)
        print(json.dumps(cal, indent=1))
        return 0
    print(json.dumps(job_resources(a.model, a.flight_s, r_map=a.r_map), indent=1))
    return 0


def cmd_rebag(a) -> int:
    print(json.dumps(rebag(a.src, a.dst, imu_period_ns=None if a.no_imu_period_check else 5_000_000), indent=1))
    return 0


def cmd_fleet(a) -> int:
    from awr.bench.eval.lidar_map import L0Map, fleet_report

    from .mapeval import open_mesh

    maps = {}
    for p in a.l0:
        m = L0Map.load(Path(p))
        maps[m.uav or Path(p).stem] = m
    rep = fleet_report(maps, q=open_mesh(a.mesh) if a.mesh else None)
    _write(Path(a.out), rep)
    print(json.dumps({"jaccard_ok": rep.get("jaccard_ok"), "merged_coverage": rep.get("merged_coverage")}))
    return 0 if rep.get("jaccard_ok") else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="slam_eval", description="slam_eval 作业（M27 §6.6.2；D3-AC-14、15、47）")
    sp = ap.add_subparsers(dest="cmd", required=True)

    def common_docker(p):
        p.add_argument("--image", default="d3-livo:dev")
        p.add_argument("--name-prefix", default="d3-slam")
        p.add_argument("--watchdog-s", type=float, default=600.0)
        p.add_argument("--retries", type=int, default=1)
        p.add_argument("--quiet-s", type=int, default=10)
        p.add_argument("--mem-gb", type=float)
        p.add_argument("--cpus", type=float)
        p.add_argument("--no-pcd", action="store_true")

    p = sp.add_parser("run")
    p.add_argument("--case", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--mesh")
    p.add_argument("--modes", default="livo,lio")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--rate", type=float, default=4.0)
    p.add_argument("--parallel", type=int, default=4)
    p.add_argument("--mem-scale", type=float, default=1.0)
    p.add_argument("--min-mem-gb", type=float, default=0.0)
    p.add_argument("--eval-runner", choices=("local", "docker"), default="docker")
    p.add_argument("--eval-image", default="d3-sensorsvc:dev")
    p.add_argument("--eval-mem-gb", type=int, default=16)
    p.add_argument("--eval-cpus", type=float, default=4)
    p.add_argument("--repo", default=str(REPO))
    p.add_argument("--l0", action="store_true")
    p.add_argument("--require-regress")
    p.add_argument("--no-imu-period-check", action="store_true")
    common_docker(p)
    p.set_defaults(fn=cmd_run)

    p = sp.add_parser("eval")
    p.add_argument("--case", required=True)
    p.add_argument("--run", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--mesh")
    p.add_argument("--ref")
    p.add_argument("--l0-out")
    p.set_defaults(fn=cmd_eval)

    p = sp.add_parser("ref")
    p.add_argument("--case", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_ref)

    p = sp.add_parser("regress")
    p.add_argument("--bag", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--dataset", default="MARS-LVIG HKisland03")
    p.add_argument("--rtk")
    p.add_argument("--rtk-topic", default="/dji_osdk_ros/rtk_position")
    p.add_argument("--cfg", default=HK_CFG)
    p.add_argument("--cam", default=HK_CAM)
    p.add_argument("--start", type=float, default=72.0)
    p.add_argument("--rate", type=float, default=1.0)
    p.add_argument("--flight-s", type=float, default=318.0)
    p.add_argument("--lidar-model", default="avia")
    # HKisland03 1× 回放：4 核配额下 republish 解码 2448×2048 JPEG 与节点争用，处理滞后、消息积压使 RSS 在 240 s 内超过 20 GB
    # 被 OOM 结束（SL1 实测）；缺省取 r1 的 8 核与 32 GB（D3-AC-14 写"≥ 20 GB、4 核"）
    p.add_argument("--min-cpus", type=float, default=8.0)
    p.add_argument("--min-mem-gb", type=float, default=32.0)
    common_docker(p)
    p.set_defaults(fn=cmd_regress)

    p = sp.add_parser("judge")
    p.add_argument("--clean", nargs="+", default=[])
    p.add_argument("--realistic", nargs="*", default=[])
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_judge)

    p = sp.add_parser("memmodel")
    p.add_argument("--model", default="mid360")
    p.add_argument("--flight-s", type=float)
    p.add_argument("--r-map", type=float)
    p.add_argument("--rate", type=float, default=4.0)
    p.add_argument("--calibrate")
    p.set_defaults(fn=cmd_memmodel)

    p = sp.add_parser("rebag")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--no-imu-period-check", action="store_true")
    p.set_defaults(fn=cmd_rebag)

    p = sp.add_parser("fleet")
    p.add_argument("--l0", nargs="+", required=True)
    p.add_argument("--mesh")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_fleet)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return int(a.fn(a))
