"""单个 Run 的评测（M27-FR-086）：装载运行目录与事件版本目录，计算 EC、EU、AO、CE 与安全，输出 `bench/evaluation.v1`；
另由 Env 规格、Env 运行清单与评测结果组成运行清单 `bench/run_manifest.v1`（M27 §7.3，`ranked_valid` 由评测回填）。

无效原因分两类：`actor_penetration`（穿越演员包围体）使本次结果作废，`ranked_valid = false` 且四项分量全部记 0
（M27-FR-161、ADR-191：计入样本、不剔除）；`physics_not_ranked`（运动学开发档）只表示该 Run 不进入正式榜，分量照常
输出供开发榜与诊断使用（正式榜由 `aggregate.ranked_row` 排除）。

只在可观测区计分（M27-FR-089、D3-AC-04）：发生在 quality 层不完整区域的注释（事件真值 `in_incomplete_region`）与窗口内参与者
全部在建筑内的注释由 `ec.exclusions` 剔除，不进入 EC、AO、CE 的分母，在 `excluded[]` 中单列；EU 的事件集不变。

AO oracle 的相机（§5.6.5"按本 Run 的标称相机"）：取运行清单的 `camera`（Env 相机覆盖时写入，ADR-217），没有时为标称相机
`d4d_eo1080@1`（HFOV 60°）；无覆盖的运行评测结果不变。

计分事件集与真值读取（评测器 0.3.0，K16；M27-FR-176、FR-114、FR-124）：计分事件为 plan.json 的注释加事件真值中属于计分类型的
类型化关键事件（目标搜索的 `target_window_k`），目标搜索的 EU 按 `report_map`（`found` → `target_present`，定位 ≤ 5 m）匹配；
事件真值与运行真值经 `truth.TruthAccess` 读取（test 事件经 HiddenStore 授权并审计），运行真值可在运行目录之外（`truth_dir`，
已移入隐藏区的 test 运行）。规则与 `inputs` 的摘要口径见 `awr.bench.eval.truth`。"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from awr.agentsdk.task import scored_events
from awr.bench.eval.ao import active_observation
from awr.bench.eval.ce import collaborative
from awr.bench.eval.ec import event_capture, exclusions, inside_masks
from awr.bench.eval.eu import event_understanding
from awr.bench.eval.ident import build_ident
from awr.bench.eval.run import load_run
from awr.bench.eval.safety import safety
from awr.bench.eval.thresholds import EVALUATOR_VERSION, ORACLE_VERSION, TH, THRESHOLDS_VERSION
from awr.bench.eval.truth import TruthAccess, eu_target_set, event_truth, event_truth_sha256, scored_event_set

__all__ = ["ZERO_REASONS", "check_run_manifest", "dev_seed_commit", "evaluate_run", "run_camera", "run_manifest", "write_run_manifest"]

ZERO_REASONS = ("actor_penetration",)          # 使结果作废、分量全部记 0 的无效原因（M27-FR-161）
TICK_S = 0.1
TICK_NS = 100_000_000


def _actor_stats(run, ident, present_all: dict[str, np.ndarray]) -> dict:
    out = {}
    for a in run.tape.actors_info(scripted_only=True):
        pres = np.nonzero(present_all[a["id"]])[0]
        seen = [k for k in pres if any(x[1] == a["id"] for x in ident.get(int(k), ()))]
        if len(pres):
            marks = [int(pres[0]) - 1, *seen, int(pres[-1]) + 1]
            gaps = np.diff(marks) / 10.0
        else:
            gaps = np.array([0.0])
        red = float(np.mean([sum(1 for x in ident[int(k)] if x[1] == a["id"]) for k in seen])) if seen else 0.0
        out[a["id"]] = {"observed_frac": len(seen) / max(len(pres), 1), "first_s": round(seen[0] / 10.0, 1) if seen else None,
                        "redundancy": red, "max_gap_s": float(np.max(gaps))}
    return out


def run_camera(env_manifest: dict) -> tuple[float, str]:
    """运行的 L1 相机 → (fx 像素, 型号)：运行清单 `camera`（相机覆盖）或标称相机。"""
    from awr.agentsdk.env.camera import CameraModel

    cam = env_manifest.get("camera")
    if cam:
        return float(cam["fx"]), str(cam["model_ref"])
    c = CameraModel()
    return c.fx, c.model_ref


def _found_locate(run, th=TH):
    """目标搜索 `found` 的定位核对：report `position_enu_m` 与目标在 report 窗口末时刻（截断到该真值事件区间）的磁带位置的水平距离
    ≤ `eu_found_m`；report 无位置时不成立。"""
    idx = {a["id"]: a["idx"] for a in run.tape.actors_info()}

    def ok(r: dict, e: dict) -> bool:
        pos = r.get("position_enu_m")
        if not pos or len(e["participants"]) != 1:
            return False
        t = min(max(r["window"]["t_end_ns"] / 1e9, e["a_s"]), e["b_s"])
        k = min(max(round(t * 10), 0), run.n_ticks)
        st = run.tape.actor_states(k * TICK_NS, [idx[e["participants"][0]]])
        p = st.pos_enu_m[0]
        return math.hypot(float(pos[0]) - float(p[0]), float(pos[1]) - float(p[1])) <= th.eu_found_m

    return ok


def evaluate_run(run_dir: Path, event_dir: Path, world, *, fx: float | None = None, diag: dict | None = None, truth_dir: Path | None = None,
                 access: TruthAccess | None = None) -> dict:
    """→ evaluation.v1；`diag` 若给出则填入诊断量（归一化中间量、阈值、计数），不进入评测文档。`fx` 给出时覆盖运行相机的焦距
    （诊断与敏感性分析用）。`truth_dir` 为运行真值目录（缺省 `<run_dir>/truth`）；`access` 为真值读取入口（缺省直接读文件，test 事件
    以 `TruthAccess.hidden` 经 HiddenStore）。"""
    access = access or TruthAccess()
    run = load_run(Path(run_dir), Path(event_dir), truth_dir=truth_dir, access=access)
    plan = json.loads((Path(event_dir) / "plan.json").read_text(encoding="utf-8"))
    ann = [e for e in scored_events(Path(event_dir)) if e["type"] in run.task["scored_event_types"]]
    truth = event_truth(Path(event_dir), access)
    E, key_rows = scored_event_set(ann, truth, run.task["scored_event_types"])
    U = run.uavs
    ident = build_ident(run)
    scripted = [a["id"] for a in run.tape.actors_info(scripted_only=True)]
    pres = run.present(scripted)
    present = {a: pres[:, i] for i, a in enumerate(scripted)}
    inside = inside_masks(run, scripted)
    truth_rows = None if truth is None else [*truth.get("events", []), *key_rows]
    E, excluded = exclusions(E, truth_rows, present, run.n_ticks, inside=inside)
    Es = [e for e in E if not e.get("excluded")]                  # 只在可观测区计分：EC、AO、CE 的分母（M27-FR-089）
    ec, cov, per_event = event_capture(Es, U, ident, present, run.n_ticks, inside=inside)
    tgt = eu_target_set(run.task, truth)
    if tgt is None:
        eu = event_understanding(run.reports, E, run)
    else:                                                         # 目标搜索：EU 真值为 target_present，found 须定位 ≤ 5 m
        eu = event_understanding(run.reports, tgt[0], run, report_map=tgt[1], locate=_found_locate(run))
    cam_fx, cam_ref = run_camera(run.manifest)
    ao = active_observation(Es, U, run, world, fx=fx or cam_fx, envelope_agl_m=tuple(run.task["altitude_envelope_agl_m"]),
                            truth_rows=truth_rows, present=present, camera_model=cam_ref)
    ce_raw, ce, ce_norm = collaborative(Es, U, ident, present, run.n_ticks, run)
    sf = safety(run, world)
    invalid = []
    if sf["penetrations"]:
        invalid.append("actor_penetration")
    if run.manifest.get("tier") == "kinematic":
        invalid.append("physics_not_ranked")
    ranked = bool(run.manifest.get("ranked"))
    comp = {"ec": ec, "eu": eu["f1"], "ao": ao["ao_real"], "ce": ce}
    if any(r in ZERO_REASONS for r in invalid):
        comp = {k: (None if v is None else 0.0) for k, v in comp.items()}     # CE 对单机无定义，仍为 null
    doc = {"schema": "drone4d.bench.evaluation.v1", "run_id": run.manifest["run_id"], "evaluator_version": EVALUATOR_VERSION,
           "thresholds_version": THRESHOLDS_VERSION, "oracle_version": ORACLE_VERSION,
           "inputs": run.hashes(event_truth_sha256=event_truth_sha256(plan.get("annotations", []), [e for e in E if e.get("key")])),
           "obs_level": run.manifest["obs_level"], "ranked": ranked, "ranked_valid": not invalid, "invalid_reasons": invalid,
           "ec": {"value": ec, "coverage": cov, "events": list(per_event.values())}, "excluded": excluded, "eu": eu, "ao": ao,
           "ce": ce_raw, "safety": sf, "actors": _actor_stats(run, ident, present),
           "components": comp}
    if diag is not None:
        diag.update(_clean({"ce": ce_norm, "thresholds": {"h_px": TH.h_px, "vis": TH.vis}, "n_reports": len(run.reports), "n_events": len(E),
                            "n_l1": len(run.l1), "n_excluded": len(excluded)}))
    return _clean(doc)


def dev_seed_commit(run_id: str, seed: int) -> str:
    """开发运行的种子承诺值 `sha256(run_id ‖ ":" ‖ seed)`（种子明文同在清单中，可直接核对）；排名运行用 M27 签发的
    `HMAC-SHA256(K_eval, run_id ‖ ":" ‖ seed)`（M27-FR-005），不经本函数。"""
    return hashlib.sha256(f"{run_id}:{int(seed)}".encode()).hexdigest()


def _run_spec_doc(spec, env_manifest: dict, experiment_id: str) -> dict:
    """运行清单的 `spec`（`bench/run_spec.v1`，单个 Run）：有规范规格时取之，否则由 Env 规格字段合成；展开前的
    `seeds`、`seed_count` 不属于单个 Run，物理档取实际生效值（运动学开发档记为 `fast`）。"""
    from awr.agentsdk.env.spec import resolve_clock_profile

    physics = {"kinematic": "fast"}.get(spec.physics, spec.physics)
    if spec.run_spec is not None:
        out = {k: v for k, v in spec.run_spec.items() if k not in ("seeds", "seed_count", "seed")}
        return {**out, "physics": physics}
    ev = env_manifest.get("event") or {}
    c = spec.comm
    out = {"schema": "drone4d.bench.run_spec.v1", "experiment": {"name": experiment_id}, "world": {"id": Path(spec.world_dir).name},
           "event": {"id": str(ev.get("id", Path(spec.event_dir).parent.name)), "version": int(ev.get("version", 1))},
           "fleet": [{"uav_id": f.uav, "vehicle_ref": f.vehicle_ref,
                      "start": {"kind": "pose", "pos_enu_m": [float(x) for x in f.pos_enu_m], "yaw_deg": math.degrees(f.yaw_rad)}}
                     for f in spec.fleet],
           "policy": {k: v for k, v in spec.policy.items() if k in ("kind", "name", "image_digest", "params")}, "physics": physics,
           "clock_profile": resolve_clock_profile(spec.clock_profile), "obs_level": spec.obs_level, "noise": spec.noise,
           "comm": {"enabled": c.enabled, "bucket_Bps": int(c.bucket_Bps), "burst_B": int(c.burst_B), "msg_max_B": int(c.msg_max_B),
                    "model": c.model},
           "ranked": bool(spec.ranked)}
    if spec.duration_s is not None:
        out["duration_s"] = float(spec.duration_s)
    return out


def run_manifest(spec, run_dir: Path, evaluation: dict | None = None, *, experiment_id: str, attempt: int = 1, restart_count: int = 0,
                 split: str = "dev", determinism_level: str | None = None, timing: dict | None = None) -> dict:
    """→ `bench/run_manifest.v1`（M27 §7.3），通过 schema 校验后返回；校验失败抛 ValueError。

    `spec` 为本次运行的 `EnvSpec`，`run_dir` 内须有 Env 运行清单 `manifest.json`；`evaluation` 给出时回填 `ranked_valid` 与
    `invalid_reasons`（否则 `ranked_valid = null`）。排名运行只写 `seed_commit`（取 `spec.seed_commit`，M27-FR-005），
    清单任何位置都不出现种子明文；开发运行同时写 `seed` 与 `dev_seed_commit`。`determinism_level` 缺省 L1 为 `1b`、
    其余观测档为 `2`。"""
    env = json.loads((Path(run_dir) / "manifest.json").read_text(encoding="utf-8"))
    ident = spec.identity()                                   # 排名运行缺 seed_commit 时抛 ValueError
    ranked = bool(spec.ranked)
    wall = float(env.get("wall_s") or 0.0)
    sim = round(int(env.get("n_ticks", 0)) * TICK_S, 6)
    tm = {"wall_s": wall, "sim_s": sim, **({"rtf": round(sim / wall, 3)} if wall > 0 else {}), **(timing or {})}
    doc = {"schema": "drone4d.bench.run_manifest.v1", "run_id": spec.run_id, "experiment_id": experiment_id, "attempt": int(attempt),
           "restart_count": int(restart_count), "spec": _run_spec_doc(spec, env, experiment_id),
           "seed_commit": spec.seed_commit if ranked else (spec.seed_commit or dev_seed_commit(spec.run_id, spec.seed)),
           "config_sha256": ident["config_sha256"], "spec_sha256": ident["spec_sha256"], "run_hash": ident["run_hash"],
           "versions": ident["versions"], "determinism_level": determinism_level or ("1b" if spec.obs_level == "L1" else "2"),
           "split": split, "ranked": ranked, "ranked_valid": None if evaluation is None else bool(evaluation["ranked_valid"]),
           "invalid_reasons": [] if evaluation is None else list(evaluation["invalid_reasons"]), "timing": tm,
           "agents": {u: {"agent_timeouts": int(a.get("timeouts", 0))} for u, a in sorted((env.get("agents") or {}).items())}}
    if not ranked:
        doc["seed"] = int(spec.seed)
    errs = check_run_manifest(doc)
    if errs:
        raise ValueError(f"run_manifest.v1 is invalid: {errs[:5]}")
    return doc


def check_run_manifest(doc: dict) -> list[str]:
    """运行清单校验：`bench/run_manifest.v1` schema，加排名运行的种子规则（M27-FR-005、FR-006：排名运行只含 `seed_commit`，
    清单与其中的 Run 规格不含 `seed`、`seeds`、`seed_count`）；→ 错误列表（空为通过）。"""
    from awr.agentsdk.schemas import bench_validator

    errs = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in bench_validator("run_manifest").iter_errors(doc)]
    if doc.get("ranked") is True:
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        errs += [f"{where}: ranked run manifest carries {k!r}" for where, d in (("<root>", doc), ("spec", spec))
                 for k in ("seed", "seeds", "seed_count") if k in d]
    return errs


def write_run_manifest(run_dir: Path, doc: dict) -> Path:
    """写 `<run_dir>/run_manifest.json`（键排序、UTF-8、末尾换行；同一输入逐字节一致）。"""
    p = Path(run_dir) / "run_manifest.json"
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return p


def _clean(x):
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    if isinstance(x, float):
        return round(x, 6) if math.isfinite(x) else None
    if isinstance(x, np.generic):
        return _clean(x.item())
    return x
