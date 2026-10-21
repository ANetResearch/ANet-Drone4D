#!/usr/bin/env python3
"""tools/bench/d3/r6_ec.py：6 种参考策略 × 12 种子（L1）与 r6 §12.3 的 EC 对照（D3-AC-18；A10；M26-FR-081；docs/05 §24.5 F-02）。

口径（`configs/bench.d3/ref6-l1.yaml`）：
- 参考行由 `drone4d.baselines.REFERENCE6` 登记（r6 行名 → 策略类、机数、链路开关、r6 实测值）；每行的 run_spec 由基础规格经
  `reference_run_spec` 派生，配置可逐行指定起飞点（`pads`）；新增起飞点须机体投影 ±0.7 m 内网格高差 ≤ `pad_check.level_m`、离
  360 s 磁带中全部演员（含背景人群）的轨迹 ≥ `pad_check.clear_m`；
- 每行 × 每个种子一个运行（`proto_board._one`：Env 与评测同一路径，产出 `<out>/runs/<行>-s<种子>/evaluation.json`）；
- 判定：每行 EC 均值与 r6 EC 之差 ≤ `criteria.ec_mean_abs_diff_max`；覆盖扫描行（`criteria.coverage_row`）AO_real 均值 ≤
  `ao_real_max` 且 AO_pos 均值 ≥ `ao_pos_min`。`crop-handoff-001` 含 r6 的 4 类关键事件（`zone_pass` 即 r6 的"车辆经过"，
  `event_type_map`），总体 EC 直接对照；逐事件捕获率按类型列出供诊断，不参与判定；
- 报告核对运行的磁带 `content_sha256` 与世界 `contentVersion`（`expect`），物理档非 realistic 时标注 `dev`（不作正式结论）。

子命令：
  specs   写出 6 行 run_spec（`<out>/specs/<行>.run_spec.json`）并核对与登记一致
  pads    核对新增起飞点（需世界包与已编译事件）→ `<out>/pads.json`
  run     运行全部（或 `--rows`、`--seeds` 所选）行 × 种子；已有 evaluation.json 的跳过（断点续跑），失败写 `<out>/failures.json`
  report  汇总 `<out>/runs/` → `<out>/r6_ec.json`（`drone4d.bench.r6_ec.v1`）并打印对照表；退出码 0 通过、1 不通过或缺运行
  reeval  以当前评测器重评 `--from` 的完整运行目录（不重跑仿真），新的 evaluation.json 与运行清单写入 `<out>/runs/`，原目录不动；
          `<out>/reeval.json` 逐运行列出新旧评测器版本与 EC、AO 分量（K07：AO 关键时刻修正后重评 A10 的 ref6）

用法（dmax，L1 在 d3-gz 容器内运行，gz 与宿主同容器）：
  python3 tools/bench/d3/r6_ec.py run --config configs/bench.d3/ref6-l1.yaml --events-root d3data/events --worlds-root worlds \\
      --out out/a10 [--jobs 6] [--physics kinematic] [--rows lawn3_cover] [--seeds 1,2]
  python3 tools/bench/d3/r6_ec.py report --config configs/bench.d3/ref6-l1.yaml --out out/a10 --events-root d3data/events --worlds-root worlds
  python3 tools/bench/d3/r6_ec.py reeval --from a10/out/ref6 --out out/ref6-k07 --events-root d3data/events --worlds-root worlds [--jobs 8]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT / "python", ROOT / "tools/sdk/drone4d/src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from drone4d.baselines import REFERENCE6, check_reference_spec, reference_run_spec  # noqa: E402

SCHEMA = "drone4d.bench.r6_ec.v1"
CONFIG_SCHEMA = "drone4d.bench.r6_ec_config.v1"
R6_TYPES = ("proximity_encounter", "board_vehicle", "enter_building", "vehicle_pass")

__all__ = ["CONFIG_SCHEMA", "SCHEMA", "check_pads", "load_config", "main", "mean_sd_ci", "reeval", "report", "row_specs", "summarize_row"]


# ------------------------------------------------------------------ 配置与规格
def load_config(path: Path) -> dict:
    import yaml

    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if cfg.get("schema") != CONFIG_SCHEMA:
        raise ValueError(f"{path}: schema must be {CONFIG_SCHEMA}")
    unknown = [r for r in cfg["rows"] if r not in REFERENCE6]
    if unknown:
        raise ValueError(f"{path}: rows not in REFERENCE6: {unknown}")
    for r, pads in (cfg.get("pads") or {}).items():
        if r not in cfg["rows"]:
            raise ValueError(f"{path}: pads given for unknown row {r}")
        if len(pads) != REFERENCE6[r].n_uav:
            raise ValueError(f"{path}: {r} needs {REFERENCE6[r].n_uav} pads, got {len(pads)}")
    return cfg


def config_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def row_specs(cfg: dict, root: Path = ROOT) -> dict[str, dict]:
    """→ {r6 行名: run_spec}：基础规格派生，观测档与物理档取配置值，种子集取配置的种子。"""
    base = json.loads((root / cfg["base_run_spec"]).read_text(encoding="utf-8"))
    base = {**base, "obs_level": cfg.get("obs_level", base["obs_level"]), "physics": cfg.get("physics", base.get("physics", "realistic")),
            "seeds": list(cfg["seeds"])}
    if base["event"]["id"] != cfg["event"]["id"] or int(base["event"].get("version", 1)) != int(cfg["event"].get("version", 1)):
        raise ValueError("base_run_spec event differs from config event")
    out = {}
    for name in cfg["rows"]:
        pads = (cfg.get("pads") or {}).get(name)
        rs = reference_run_spec(base, name, pads=pads)
        probs = check_reference_spec(rs)
        if probs:
            raise ValueError(f"{name}: {probs}")
        out[name] = rs
    return out


def added_pads(cfg: dict, specs: dict[str, dict], root: Path = ROOT) -> list[dict]:
    """相对基础机队新增（或改动）的起飞点：[{row, uav_id, pos}]。"""
    base = json.loads((root / cfg["base_run_spec"]).read_text(encoding="utf-8"))
    known = {tuple(float(x) for x in f["start"]["pos_enu_m"][:2]) for f in base["fleet"]}
    out, seen = [], set()
    for name, rs in specs.items():
        for f in rs["fleet"]:
            xy = tuple(float(x) for x in f["start"]["pos_enu_m"][:2])
            if xy not in known and xy not in seen:
                seen.add(xy)
                out.append({"row": name, "uav_id": f["uav_id"], "pos": list(xy)})
    return out


def check_pads(pads: list[dict], world, tape, *, level_m: float, clear_m: float, step_ticks: int = 5) -> list[dict]:
    """逐个起飞点：机体投影（Env 起飞检查的 5×5 足印，±0.7 m）内网格表面高差、离演员轨迹（全部演员、每 `step_ticks` 取样）的
    水平最小距离与判定。"""
    import numpy as np

    from awr.agentsdk.env.core import SPAWN_FOOT
    from awr.agentsdk.env.geom import surface_z

    idx = list(range(tape.n_actors))
    tracks = []
    for k in range(0, tape.n_ticks, step_ticks):
        st = tape.actor_states(k * 100_000_000, idx)
        m = np.isin(st.state, (1, 2))
        tracks.append(np.asarray(st.pos_enu_m, np.float64)[m][:, :2])
    P = np.concatenate(tracks) if tracks else np.zeros((0, 2))
    out = []
    for p in pads:
        xy = np.asarray(p["pos"][:2], np.float64)
        zs = np.asarray(surface_z(world, xy + SPAWN_FOOT), np.float64)
        spread = float(np.ptp(zs))
        clear = float(np.sqrt(((P - xy) ** 2).sum(1)).min()) if len(P) else math.inf
        out.append({**p, "level_spread_m": round(spread, 4), "surface_z_m": round(float(zs.max()), 3), "actor_clear_m": round(clear, 2),
                    "ok": spread <= level_m and clear >= clear_m})
    return out


# ------------------------------------------------------------------ 运行
def _proto_board():
    spec = importlib.util.spec_from_file_location("_d3_proto_board", Path(__file__).with_name("proto_board.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _job(job: dict) -> dict:
    return _proto_board()._one(job)


def run_all(cfg: dict, out: Path, *, events_root: Path, worlds_root: Path, rows: list[str] | None = None, seeds: list[int] | None = None,
            jobs: int | None = None, physics: str | None = None, gz_launcher: str | None = None, gz_image: str | None = None,
            duration_s: float | None = None) -> int:
    specs = row_specs(cfg)
    rc = cfg.get("run") or {}
    physics = physics or cfg.get("physics", "realistic")
    launcher = gz_launcher or rc.get("gz_launcher")
    gz = {"launcher": launcher, "image": gz_image, "export_root": None} if physics == "realistic" else {}
    out.mkdir(parents=True, exist_ok=True)
    (out / "specs").mkdir(exist_ok=True)
    for name, rs in specs.items():
        (out / "specs" / f"{name}.run_spec.json").write_text(json.dumps(rs, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    todo = [{"spec": specs[r], "policy": r, "seed": int(s), "out": str(out), "tag": f"a10-{cfg['event']['id']}", "physics": physics, "gz": gz,
             "worlds_root": str(worlds_root), "events_root": str(events_root), "agents": rc.get("agents", "inproc"),
             "agent_image": rc.get("agent_image", "d3-agent-py:dev"), "duration_s": duration_s}
            for r in (rows or cfg["rows"]) for s in (seeds or cfg["seeds"])]
    failures = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=max(1, jobs or int(rc.get("jobs", 4)))) as ex:
        futs = {ex.submit(_job, j): j for j in todo}
        for f in as_completed(futs):
            j = futs[f]
            try:
                r = f.result()
            except Exception as e:                               # 单个运行失败不中断其余运行
                failures.append({"row": j["policy"], "seed": j["seed"], "error": repr(e), "trace": traceback.format_exc()[-4000:]})
                print(f"FAIL {j['policy']} s{j['seed']}: {e!r}", flush=True)
                continue
            c = r.get("components")
            print(f"{r['policy']} s{r['seed']}: " + ("skipped (evaluation.json exists)" if r["skipped"] else
                  f"EC {c['ec']:.3f} AO {c['ao']:.3f}  run {r['run_wall_s']} s  eval {r['eval_wall_s']} s  [{time.time() - t0:.0f} s]"), flush=True)
    fp = out / "failures.json"
    if failures:
        fp.write_text(json.dumps(failures, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    else:
        fp.unlink(missing_ok=True)
    return 1 if failures else 0


# ------------------------------------------------------------------ 重评（不重跑仿真）
_WORLDS: dict[tuple[str, bool], object] = {}


def _reeval_one(job: dict) -> dict:
    """单个运行目录的重评：世界查询按运行清单判定正式运行（排名且 realistic 时不退回 DSM），每个进程按（世界、正式）缓存一份。"""
    for p in (ROOT / "python", ROOT / "tools/sdk/drone4d/src"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from awr.agentsdk.env.core import _open_world
    from awr.bench.eval.evaluator import evaluate_run

    src, dst = Path(job["src"]), Path(job["dst"])
    man = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    formal = bool(man.get("ranked")) and man.get("physics") == "realistic"
    key = (job["world_dir"], formal)
    if key not in _WORLDS:
        _WORLDS[key] = _open_world(Path(job["world_dir"]), formal=formal)
    t0 = time.time()
    doc = evaluate_run(src, Path(job["event_dir"]), _WORLDS[key])
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "evaluation.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (dst / "manifest.json").write_text((src / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
    old = json.loads((src / "evaluation.json").read_text(encoding="utf-8")) if (src / "evaluation.json").exists() else {}
    pick = ("ec", "ao_real", "ao_pos", "ao_pos_los")

    def comps(d: dict) -> dict:
        if not d:
            return {}
        return {"ec": d["ec"]["value"], **{k: d["ao"][k] for k in pick[1:]}}

    return {"run": src.name, "formal": formal, "eval_wall_s": round(time.time() - t0, 2),
            "old": {"evaluator_version": old.get("evaluator_version"), **comps(old)},
            "new": {"evaluator_version": doc["evaluator_version"], **comps(doc)}}


def reeval(cfg: dict, src_out: Path, out: Path, *, events_root: Path, worlds_root: Path, jobs: int = 4) -> dict:
    """以当前评测器重评 `src_out/runs/<行>-s<种子>/`（完整运行目录）→ `out/runs/` 与 `out/reeval.json`；运行目录不修改。"""
    base = json.loads((ROOT / cfg["base_run_spec"]).read_text(encoding="utf-8"))
    ev_dir = Path(events_root) / cfg["event"]["id"] / f"v{cfg['event'].get('version', 1)}"
    world_dir = Path(worlds_root) / base["world"]["id"]
    todo = [{"src": str(Path(src_out) / "runs" / f"{r}-s{s}"), "dst": str(Path(out) / "runs" / f"{r}-s{s}"), "event_dir": str(ev_dir),
             "world_dir": str(world_dir)} for r in cfg["rows"] for s in cfg["seeds"]]
    todo = [j for j in todo if (Path(j["src"]) / "manifest.json").exists()]
    rows, failures = [], []
    with ProcessPoolExecutor(max_workers=max(1, jobs)) as ex:
        futs = {ex.submit(_reeval_one, j): j for j in todo}
        for f in as_completed(futs):
            try:
                r = f.result()
            except Exception as e:
                failures.append({"run": Path(futs[f]["src"]).name, "error": repr(e), "trace": traceback.format_exc()[-4000:]})
                print(f"FAIL {Path(futs[f]['src']).name}: {e!r}", flush=True)
                continue
            rows.append(r)
            print(f"{r['run']}: AO_real {r['old'].get('ao_real')} -> {r['new']['ao_real']:.4f}  EC {r['new']['ec']:.4f}  "
                  f"({r['eval_wall_s']} s)", flush=True)
    doc = {"schema": "drone4d.bench.r6_ec_reeval.v1", "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "from": str(src_out),
           "event_dir": str(ev_dir), "world_dir": str(world_dir), "runs": sorted(rows, key=lambda r: r["run"]), "failures": failures}
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / "reeval.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return doc


# ------------------------------------------------------------------ 汇总与判定
def mean_sd_ci(xs: list[float]) -> dict:
    """均值、样本标准差与 95% 区间（t 分布近似，n < 2 时区间为点）。"""
    n = len(xs)
    if not n:
        return {"n": 0, "mean": None, "sd": None, "ci95": None}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    t = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262, 11: 2.228, 12: 2.201}.get(n, 1.96)
    h = t * sd / math.sqrt(n) if n > 1 else 0.0
    return {"n": n, "mean": round(m, 4), "sd": round(sd, 4), "ci95": [round(m - h, 4), round(m + h, 4)]}


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(float(x), nd)


def summarize_row(name: str, evals: list[dict], type_map: dict[str, str]) -> dict:
    """一行（同一参考行的各种子 evaluation.json）→ 均值统计、逐事件类型捕获率与 r6 对照值。"""
    ref = REFERENCE6[name]
    per: dict[str, list[float]] = {}
    for ev in evals:
        for e in ev["ec"]["events"]:
            per.setdefault(type_map.get(e["type"], e["type"]), []).append(1.0 if e["captured"] else 0.0)
    ec = mean_sd_ci([float(ev["ec"]["value"]) for ev in evals])
    row = {"name": name, "policy": ref.policy, "n_uav": ref.n_uav, "comm": ref.comm, "n_runs": len(evals),
           "ec": ec, "ec_coverage": mean_sd_ci([float(ev["ec"]["coverage"]) for ev in evals]),
           "per_event": {t: _r(sum(v) / len(v)) for t, v in sorted(per.items())}, "per_event_n": {t: len(v) for t, v in sorted(per.items())},
           "ao_real": mean_sd_ci([float(ev["ao"]["ao_real"]) for ev in evals]),
           "ao_pos": mean_sd_ci([float(ev["ao"]["ao_pos"]) for ev in evals]),
           "ao_pos_los": mean_sd_ci([float(ev["ao"]["ao_pos_los"]) for ev in evals]),
           "eu_f1": mean_sd_ci([float(ev["eu"]["f1"]) for ev in evals]),
           "ce_gain": mean_sd_ci([float(ev["ce"]["gain"]) for ev in evals]),
           "redundancy": mean_sd_ci([float(ev["ce"]["redundancy"]) for ev in evals]),
           "flight_km": mean_sd_ci([float(ev["ce"]["flight_km"]) for ev in evals]),
           "comm_kb": mean_sd_ci([float(ev["ce"]["comm_kb"]) for ev in evals]),
           "excluded_events": sorted({x["event_id"] for ev in evals for x in ev.get("excluded", [])}),
           "ranked_valid_frac": _r(sum(1 for ev in evals if ev.get("ranked_valid")) / max(len(evals), 1)),
           "r6": ref.r6}
    row["d_ec"] = None if ec["mean"] is None else round(ec["mean"] - ref.r6["ec"], 4)
    row["d_per_event"] = {t: _r(row["per_event"][t] - ref.r6["per_event"][t]) for t in R6_TYPES if t in row["per_event"]}
    return row


def _run_inputs(run_dir: Path) -> dict:
    m = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8")) if (run_dir / "manifest.json").exists() else {}
    return {"tier": m.get("tier"), "physics": m.get("physics") or (m.get("spec") or {}).get("physics")}


def report(cfg: dict, out: Path, *, config_path: Path | None = None, events_root: Path | None = None, worlds_root: Path | None = None,
           pads: list[dict] | None = None) -> dict:
    """汇总 `<out>/runs/` 并判定（不运行仿真）。"""
    crit = cfg["criteria"]
    type_map = dict(cfg.get("event_type_map") or {})
    rows, missing, tiers, oracle = [], [], set(), set()
    for name in cfg["rows"]:
        evals = []
        for s in cfg["seeds"]:
            rd = out / "runs" / f"{name}-s{s}"
            p = rd / "evaluation.json"
            if not p.exists():
                missing.append(f"{name}-s{s}")
                continue
            ev = json.loads(p.read_text(encoding="utf-8"))
            evals.append(ev)
            oracle.add(json.dumps(ev["ao"].get("oracle"), sort_keys=True))
            tiers.add(_run_inputs(rd)["tier"] or "")
        rows.append(summarize_row(name, evals, type_map))
    for r in rows:
        r["ec_pass"] = r["ec"]["mean"] is not None and abs(r["d_ec"]) <= crit["ec_mean_abs_diff_max"] + 1e-9
    cov = next((r for r in rows if r["name"] == crit["coverage_row"]), None)
    ao_ok = bool(cov and cov["ao_real"]["mean"] is not None and cov["ao_real"]["mean"] <= crit["ao_real_max"] + 1e-9
                 and cov["ao_pos"]["mean"] >= crit["ao_pos_min"] - 1e-9)
    inputs = {}
    if events_root is not None:
        tj = Path(events_root) / cfg["event"]["id"] / f"v{cfg['event'].get('version', 1)}" / "tape.json"
        if tj.exists():
            inputs["tape_content_sha256"] = json.loads(tj.read_text(encoding="utf-8"))["content_sha256"]
    if worlds_root is not None:
        base = json.loads((ROOT / cfg["base_run_spec"]).read_text(encoding="utf-8"))
        wj = Path(worlds_root) / base["world"]["id"] / "world.json"
        if wj.exists():
            inputs["world_content_version"] = json.loads(wj.read_text(encoding="utf-8")).get("contentVersion")
    exp = cfg.get("expect") or {}
    if any(k in inputs and inputs[k] != v for k, v in exp.items()):
        inputs_ok = False                                       # 实际磁带或世界与对照口径不符
    else:
        inputs_ok = True if exp and all(k in inputs for k in exp) else None   # 缺输入时为未核对
    physics = sorted(t for t in tiers if t)
    dev = physics != ["realistic"]
    pads_ok = None if pads is None else all(p["ok"] for p in pads)
    ec_ok = all(r["ec_pass"] for r in rows)
    doc = {"schema": SCHEMA, "ac": cfg.get("ac", "D3-AC-18"), "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "config": {"path": str(config_path) if config_path else None, "sha256": config_sha256(config_path) if config_path else None},
           "event": cfg["event"], "obs_level": cfg.get("obs_level"), "seeds": list(cfg["seeds"]), "physics_tiers": physics, "dev": dev,
           "inputs": inputs, "expect": exp, "inputs_ok": inputs_ok, "oracle": [json.loads(o) for o in sorted(oracle)],
           "event_type_map": type_map, "criteria": crit, "pads": pads, "rows": rows, "missing_runs": missing,
           "coverage": {"row": crit["coverage_row"], "ao_real": cov["ao_real"]["mean"] if cov else None,
                        "ao_pos": cov["ao_pos"]["mean"] if cov else None, "ao_pos_los": cov["ao_pos_los"]["mean"] if cov else None,
                        "pass": ao_ok},
           "verdict": {"ec_pass": ec_ok, "ao_pass": ao_ok, "complete": not missing, "inputs_ok": inputs_ok, "pads_ok": pads_ok, "formal": not dev,
                       "pass": ec_ok and ao_ok and not missing and inputs_ok is not False and pads_ok is not False}}
    return doc


def format_table(doc: dict) -> str:
    lines = ["row              n  EC (mean ± sd)   r6    ΔEC     cov    AO_real AO_pos  EU-F1  gain   | " + " ".join(f"{t[:6]:>6}" for t in R6_TYPES)]
    for r in doc["rows"]:
        if r["ec"]["mean"] is None:
            lines.append(f"{r['name']:<16} 0  (no runs)")
            continue
        pe = " ".join(f"{(r['per_event'].get(t) if r['per_event'].get(t) is not None else float('nan')):6.2f}" for t in R6_TYPES)
        lines.append(f"{r['name']:<16} {r['n_runs']:<2} {r['ec']['mean']:.3f} ± {r['ec']['sd']:.3f}  {r['r6']['ec']:.2f}  {r['d_ec']:+.3f}"
                     f"{'' if r['ec_pass'] else '!'}  {r['ec_coverage']['mean']:.3f}  {r['ao_real']['mean']:.3f}   {r['ao_pos']['mean']:.3f}"
                     f"   {r['eu_f1']['mean']:.3f}  {r['ce_gain']['mean']:+.3f} | {pe}")
    v = doc["verdict"]
    lines.append(f"verdict: EC {v['ec_pass']}  AO {v['ao_pass']}  complete {v['complete']}  inputs {v['inputs_ok']}  pads {v['pads_ok']}  "
                 f"formal {v['formal']}  -> {'PASS' if v['pass'] else 'FAIL'}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 命令行
def _open(events_root: Path, worlds_root: Path, cfg: dict):
    from awr.agentsdk.env.core import _open_world
    from awr.sim.events.tape import TapeReader

    base = json.loads((ROOT / cfg["base_run_spec"]).read_text(encoding="utf-8"))
    world = _open_world(Path(worlds_root) / base["world"]["id"])
    tape = TapeReader(Path(events_root) / cfg["event"]["id"] / f"v{cfg['event'].get('version', 1)}" / "tape.bin")
    return world, tape


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="r6_ec.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("specs", "pads", "run", "report", "reeval"):
        p = sub.add_parser(c)
        p.add_argument("--config", type=Path, default=ROOT / "configs/bench.d3/ref6-l1.yaml")
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--events-root", type=Path, default=ROOT / "d3data/events")
        p.add_argument("--worlds-root", type=Path, default=ROOT / "worlds")
        if c == "run":
            p.add_argument("--rows", help="comma-separated r6 rows (default: config rows)")
            p.add_argument("--seeds", help="comma-separated (default: config seeds)")
            p.add_argument("--jobs", type=int)
            p.add_argument("--physics", choices=("realistic", "kinematic"))
            p.add_argument("--gz-launcher", choices=("docker", "local"))
            p.add_argument("--gz-image")
            p.add_argument("--duration-s", type=float, help="override run duration (smoke only; not a formal comparison)")
        if c in ("report", "reeval"):
            p.add_argument("--no-pads", action="store_true", help="skip the pad check (no world or tape at hand)")
        if c == "reeval":
            p.add_argument("--from", dest="src", type=Path, required=True, help="output directory holding the full run directories")
            p.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    a.out.mkdir(parents=True, exist_ok=True)
    if a.cmd == "specs":
        specs = row_specs(cfg)
        (a.out / "specs").mkdir(exist_ok=True)
        for name, rs in specs.items():
            (a.out / "specs" / f"{name}.run_spec.json").write_text(json.dumps(rs, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            print(f"{name}: {len(rs['fleet'])} UAV, comm {rs['comm']['enabled']}, pads {[f['start']['pos_enu_m'][:2] for f in rs['fleet']]}")
        return 0
    if a.cmd == "reeval":
        doc = reeval(cfg, a.src, a.out, events_root=a.events_root, worlds_root=a.worlds_root, jobs=a.jobs)
        if doc["failures"]:
            return 1
        a.cmd = "report"                                        # 重评后直接汇总判定（与 report 子命令相同）
    if a.cmd in ("pads", "report") and not getattr(a, "no_pads", False):
        world, tape = _open(a.events_root, a.worlds_root, cfg)
        pc = cfg.get("pad_check") or {}
        pads = check_pads(added_pads(cfg, row_specs(cfg)), world, tape, level_m=float(pc.get("level_m", 0.03)), clear_m=float(pc.get("clear_m", 15.0)))
        (a.out / "pads.json").write_text(json.dumps(pads, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        if a.cmd == "pads":
            for p in pads:
                print(json.dumps(p, ensure_ascii=False))
            return 0 if all(p["ok"] for p in pads) else 1
    else:
        pads = None
    if a.cmd == "run":
        return run_all(cfg, a.out, events_root=a.events_root, worlds_root=a.worlds_root,
                       rows=a.rows.split(",") if a.rows else None, seeds=[int(s) for s in a.seeds.split(",")] if a.seeds else None,
                       jobs=a.jobs, physics=a.physics, gz_launcher=a.gz_launcher, gz_image=a.gz_image, duration_s=a.duration_s)
    doc = report(cfg, a.out, config_path=a.config, events_root=a.events_root, worlds_root=a.worlds_root, pads=pads)
    (a.out / "r6_ec.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(format_table(doc))
    return 0 if doc["verdict"]["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
