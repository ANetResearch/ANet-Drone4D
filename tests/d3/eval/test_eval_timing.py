"""D3-AC-18 评测耗时：30 机 L1 360 s 评测 ≤ 30 s，3 机 L2 360 s 评测 ≤ 120 s（环境：dmax CPU 池，`--cpus 16` 的 CPU 容器）。

评测输入由真实 Env 运行目录构造（不改变单机的行密度与证据分布）：
- `merge_runs`：机队并联，把同一事件的若干 Run 按机拼接为一个大机队 Run，机号按来源顺序重排为 `uav-01` … `uav-NN`，真值行、
  report、链路记录、接触与 Agent 产物中的机号一并改写（30 机 L1 = 10 个 3 机 L1 Run 并联）；
- `tile_run`：时间平铺，把短 Run 的机队轨迹、真值行、report、链路与接触记录按 tick 平移复制到目标时长（L2 30 s → 360 s）；
- `fill_l2_masks`：为未记录掩膜的旧 L2 真值行补出掩膜（编码同 Env）；
- `add_l2_reports`：为没有 report 的 L2 Run 按固定间隔补出含框证据与点证据（掩膜关联）的 report，使 EU 的 L2 关联路径计入耗时。

正式计时（`test_eval_timing_formal`）读取环境变量，未设置时跳过：
`AWR_D3_EVAL_TIMING_L1_RUNS`（以 `os.pathsep` 分隔的 3 机 L1 Run 目录）、`AWR_D3_EVAL_TIMING_L2_RUN`（L2 Run 目录）、
`AWR_D3_EVAL_TIMING_EVENT`（事件版本目录）、`AWR_D3_EVAL_TIMING_WORLD`（世界包目录）、`AWR_D3_EVAL_TIMING_WORK`（构造输入的工作
目录）、可选 `AWR_D3_EVAL_TIMING_OUT`（计时记录 JSON）与 `AWR_D3_EVAL_TIMING_REPEATS`（缺省 3 次取中位）。

常规用例在合成世界上核对：安全与 AO 的向量化实现与逐 tick、逐行扫描的参考实现逐值一致（含穿越人员、车辆包围体的构造轨迹），
构造器产出的 Run 可被评测且 `evaluation.v1` 通过 schema 校验、同一输入逐字节一致。"""

from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from awr.agentsdk.schemas import bench_validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "env"))

TICK_NS = 100_000_000
LIMIT_S = {"l1_30uav_360s": 30.0, "l2_3uav_360s": 120.0}           # D3-AC-18 原文阈值


# ------------------------------------------------------------------------------------------------------------ 输入构造


def _rows(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_rows(p: Path, rows) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _rename(x, m: dict[str, str]):
    """递归改写等于旧机号的字符串值（键不改）。"""
    if isinstance(x, dict):
        return {k: _rename(v, m) for k, v in x.items()}
    if isinstance(x, list):
        return [_rename(v, m) for v in x]
    if isinstance(x, str):
        return m.get(x, x)
    return x


def _fleet(d: Path) -> dict[str, np.ndarray]:
    z = np.load(d / "truth" / "fleet.npz")
    return {k: z[k] for k in z.files}


def _save_fleet(d: Path, f: dict[str, np.ndarray]) -> None:
    (d / "truth").mkdir(parents=True, exist_ok=True)
    np.savez_compressed(d / "truth" / "fleet.npz", **f)


def merge_runs(srcs: list[Path], out: Path) -> Path:
    """机队并联：同一事件、同一时长的 Run 按机拼接；机号按来源顺序重排为 uav-01 … uav-NN；`n_ticks` 取最短者。"""
    srcs = [Path(s) for s in srcs]
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "truth").mkdir(parents=True)
    mans = [json.loads((s / "manifest.json").read_text(encoding="utf-8")) for s in srcs]
    n = min(int(m["n_ticks"]) for m in mans)
    maps, j = [], 0
    for s in srcs:
        m = {}
        for u in _fleet(s)["uavs"]:
            j += 1
            m[str(u)] = f"uav-{j:02d}"
        maps.append(m)
    fl = [_fleet(s) for s in srcs]
    L = min(len(f["t_ns"]) for f in fl)
    F = {"uavs": np.array([maps[i][str(u)] for i, f in enumerate(fl) for u in f["uavs"]]), "t_ns": fl[0]["t_ns"][:L]}
    for k in ("pos", "vel", "yaw", "nav", "status"):
        F[k] = np.concatenate([f[k][:L] for f in fl], axis=1)
    _save_fleet(out, F)
    for rel in ("truth/l1.jsonl", "truth/l2.jsonl", "truth/contacts.jsonl", "reports.jsonl", "net/records.jsonl"):
        rows = [_rename(r, maps[i]) for i, s in enumerate(srcs) for r in _rows(s / rel)
                if int(r.get("seq", 0)) <= n and int(r.get("t_ns", 0)) <= n * TICK_NS]
        if rows or any((s / rel).exists() for s in srcs):
            _write_rows(out / rel, rows)
    for i, s in enumerate(srcs):
        for u_old, u_new in maps[i].items():
            if (s / "agents" / u_old).is_dir():
                shutil.copytree(s / "agents" / u_old, out / "agents" / u_new)
    if (srcs[0] / "truth" / "run_seed.json").exists():
        shutil.copy2(srcs[0] / "truth" / "run_seed.json", out / "truth" / "run_seed.json")
    man = dict(mans[0])
    man.update(run_id=out.name, n_ticks=n, duration_s=round(n * 0.1, 6),
               agents={maps[i][u]: a for i, m in enumerate(mans) for u, a in sorted((m.get("agents") or {}).items()) if u in maps[i]},
               fleet=[_rename(f, maps[i]) for i, m in enumerate(mans) for f in m.get("fleet", [])])
    (out / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    shutil.copy2(srcs[0] / "task.json", out / "task.json")
    return out


def _shift_report(r: dict, dk: int) -> dict:
    r = json.loads(json.dumps(r))
    for k in ("apply_tick", "k_obs"):
        if k in r:
            r[k] = int(r[k]) + dk
    w = r["report"]["window"]
    w["t_start_ns"] = int(w["t_start_ns"]) + dk * TICK_NS
    w["t_end_ns"] = int(w["t_end_ns"]) + dk * TICK_NS
    for p in r["report"]["participants"]:
        for ev in p["evidence"]:
            ev["frame_seq"] = int(ev["frame_seq"]) + dk
    return r


def tile_run(src: Path, out: Path, duration_s: float) -> Path:
    """时间平铺：把 `src` 的 [0, n_src) tick 依次平移 i · n_src 复制，直到 `duration_s`；末段补到 n_tgt（含末时刻）。"""
    src, out = Path(src), Path(out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out, ignore=shutil.ignore_patterns("evaluation*.json", "run_manifest.json", "*.sock", "agent_sock", "phys"))
    man = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    n_src, n_tgt = int(man["n_ticks"]), round(duration_s * 10)
    reps = math.ceil(n_tgt / n_src)
    f = _fleet(src)
    L = len(f["t_ns"])
    src_k = np.arange(n_tgt + (L - n_src)) % n_src
    tile = np.arange(len(src_k)) // n_src
    F = {k: (v[src_k] if k not in ("uavs",) else v) for k, v in f.items()}
    F["t_ns"] = f["t_ns"][src_k] + tile * n_src * TICK_NS
    _save_fleet(out, F)

    def keep(s: int, i: int) -> bool:
        return (s < n_src or i == reps - 1) and s + i * n_src <= n_tgt

    for rel in ("truth/l1.jsonl", "truth/l2.jsonl"):
        rows = _rows(src / rel)
        if rows:
            _write_rows(out / rel, ({**r, "seq": int(r["seq"]) + i * n_src, "t_ns": int(r["t_ns"]) + i * n_src * TICK_NS}
                                    for i in range(reps) for r in rows if keep(int(r["seq"]), i)))
    for rel in ("truth/contacts.jsonl", "net/records.jsonl"):
        rows = _rows(src / rel)
        if (src / rel).exists():
            _write_rows(out / rel, ({**r, "t_ns": int(r["t_ns"]) + i * n_src * TICK_NS} for i in range(reps) for r in rows
                                    if keep(int(r["t_ns"]) // TICK_NS, i)))
    reps_rows = _rows(src / "reports.jsonl")
    if (src / "reports.jsonl").exists():
        _write_rows(out / "reports.jsonl", (_shift_report(r, i * n_src) for i in range(reps) for r in reps_rows
                                            if keep(int(r["report"]["window"]["t_end_ns"]) // TICK_NS, i)))
    man.update(run_id=out.name, n_ticks=n_tgt, duration_s=float(duration_s))
    (out / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return out


def fill_l2_masks(run_dir: Path) -> int:
    """为未记录掩膜的旧 L2 真值行（`vis > 0` 且框非空）补出包围盒内切椭圆的可见掩膜，编码同 Env（`awr.agentsdk.env.l2.mask_code`），
    使输入与当前 L2 真值格式一致。→ 补出的行数。"""
    from awr.agentsdk.env.l2 import mask_code

    p = Path(run_dir) / "truth" / "l2.jsonl"
    rows, n = _rows(p), 0
    for r in rows:
        x, y, w, h = (int(v) for v in r["bbox_xywh"])
        if "mask" not in r and r["vis"] > 0 and w > 0 and h > 0:
            yy, xx = np.mgrid[0:h, 0:w]
            inst = np.zeros((y + h, x + w), np.int32)
            inst[y:, x:] = (((xx + 0.5 - w / 2) / (w / 2)) ** 2 + ((yy + 0.5 - h / 2) / (h / 2)) ** 2 <= 1.0).astype(np.int32)
            r["mask"] = mask_code(inst, 1, (x, y, w, h))
            n += 1
    _write_rows(p, rows)
    return n


def add_l2_reports(run_dir: Path, *, every_s: float = 10.0, n_evidence: int = 6) -> int:
    """为 L2 Run 补出 report：每机每 `every_s` 取此前 1 s 内可见（有掩膜）的两名演员，各给 `n_evidence` 条证据，框证据与点证据
    （框中心像素）交替；类型轮流取任务的 report 类型。→ 补出的 report 数。"""
    d = Path(run_dir)
    rows = [r for r in _rows(d / "truth" / "l2.jsonl") if r.get("mask") and r["bbox_xywh"][2] > 0 and r["bbox_xywh"][3] > 0]
    task = json.loads((d / "task.json").read_text(encoding="utf-8"))
    types = sorted(task.get("report_types") or ["meet"])
    by: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        by.setdefault((r["uav"], int(r["seq"]) // round(every_s * 10)), []).append(r)
    out = _rows(d / "reports.jsonl")
    for n, ((u, b), rs) in enumerate(sorted(by.items())):
        acts = sorted({r["actor"] for r in rs})[:2]
        parts = []
        for j, a in enumerate(acts):
            ev = []
            for i, r in enumerate([r for r in rs if r["actor"] == a][-n_evidence:]):
                x, y, w, h = r["bbox_xywh"]
                ev.append({"uav": u, "sensor": r.get("sensor", "eo"), "frame_seq": int(r["seq"]),
                           **({"bbox_xywh": [x, y, w, h]} if i % 2 == 0 else {"point_xy": [x + w / 2, y + h / 2]})})
            parts.append({"track_id": f"{u}-t{b}-{j}", "evidence": ev})
        k1 = max(int(r["seq"]) for r in rs)
        out.append({"apply_tick": k1 + 1, "k_obs": k1, "uav": u,
                    "report": {"type": types[n % len(types)], "confidence": 0.5, "participants": parts,
                               "window": {"t_start_ns": (k1 - 10) * TICK_NS, "t_end_ns": k1 * TICK_NS}}})
    _write_rows(d / "reports.jsonl", out)
    return len(out)


def time_eval(run_dir: Path, event_dir: Path, world, *, repeats: int = 3) -> dict:
    """`evaluate_run` 的墙钟耗时（同一进程内重复 `repeats` 次取中位；世界查询对象由调用方先行打开，批量评测时共用）。"""
    from awr.bench.eval.evaluator import evaluate_run

    walls, docs = [], []
    for _ in range(repeats):
        t0 = time.perf_counter()
        docs.append(evaluate_run(run_dir, event_dir, world))
        walls.append(time.perf_counter() - t0)
    same = len({json.dumps(d, sort_keys=True) for d in docs}) == 1
    return {"wall_s": [round(w, 3) for w in walls], "median_s": round(statistics.median(walls), 3), "identical": same, "doc": docs[0]}


# ------------------------------------------------------------------------------------------------------------ 参考实现


def _safety_reference(run, world, th) -> dict:
    """逐 tick、逐机、逐车辆的原始定义（向量化之前的实现），只返回需要逐值比对的量。"""
    from awr.bench.eval.safety import _segments

    info = run.tape.actors_info()
    pos = np.asarray(run.fleet["pos"], np.float64)
    status = np.asarray(run.fleet["status"])
    n, nu = pos.shape[0], pos.shape[1]
    air = np.isin(status, ("AIRBORNE", "LANDING"))
    sep = run.task["safety"]
    persons = np.array([a["cls"] == "person" for a in info])
    sizes = np.array([a["size_m"] for a in info], np.float64)
    dp = np.full((n, nu), np.inf)
    dv = np.full((n, nu), np.inf)
    for k in range(n):
        st = run.tape.actor_states(k * TICK_NS)
        vis = np.isin(st.state, (1, 2))
        for u in range(nu):
            if not air[k, u]:
                continue
            p = pos[k, u]
            m = vis & persons
            if m.any():
                c = st.pos_enu_m[m]
                dh = np.hypot(p[0] - c[:, 0], p[1] - c[:, 1])
                zr = p[2] - c[:, 2]
                dz = zr - np.clip(zr, 0.0, th.person_h_m)
                dp[k, u] = float(np.min(np.sqrt(np.maximum(dh - th.person_r_m, 0.0) ** 2 + dz ** 2) - th.uav_r_m))
            m = vis & ~persons
            if m.any():
                best = np.inf
                for c, yaw, s in zip(st.pos_enu_m[m], st.yaw_rad[m], sizes[m], strict=True):
                    cy, sy = np.cos(yaw), np.sin(yaw)
                    rel = p - (c + np.array([0, 0, s[2] / 2]))
                    local = np.array([cy * rel[0] + sy * rel[1], -sy * rel[0] + cy * rel[1], rel[2]])
                    q = np.abs(local) - s / 2
                    best = min(best, float(np.linalg.norm(np.maximum(q, 0.0)) + min(q.max(), 0.0)) - th.uav_r_m)
                dv[k, u] = best
    return {"dp": dp, "dv": dv,
            "violations": {"person": sum(_segments(dp[:, u] < sep["min_sep_person_m"]) for u in range(nu)),
                           "vehicle": sum(_segments(dv[:, u] < sep["min_sep_vehicle_m"]) for u in range(nu))},
            "penetrations": sum(_segments(dp[:, u] <= 0) + _segments(dv[:, u] <= 0) for u in range(nu))}


def _best_reference(rows, U, k0: int, here_ids: set[str], th) -> float:
    best = 0.0
    for u in U:
        per: dict[str, float] = {}
        for r in rows:
            if r["uav"] == u and r["actor"] in here_ids and abs(int(r["seq"]) - k0) <= th.ao_window_s * 10:
                per[r["actor"]] = max(per.get(r["actor"], 0.0), min(1.0, r["h_unocc_px"] / th.ao_q_px) * r["vis"])
        if len(per) == len(here_ids):
            best = max(best, min(per.values()))
    return best


def _ao_real_reference(E, U, run, th) -> list[float]:
    """逐行扫描的实际质量（各事件 τ ± 1 s 内对机取最大、对参与者取最小前的最大质量；向量化之前的实现）。"""
    info = {a["id"]: a for a in run.tape.actors_info()}
    out = []
    for e in E:
        k0 = min(round((e["a_s"] + e["b_s"]) / 2 * 10), run.n_ticks)
        ids = [p for p in e["participants"] if info[p]["cls"] == "person"]
        if not ids:
            continue
        st = run.tape.actor_states(k0 * TICK_NS, [info[p]["idx"] for p in ids])
        here_ids = {p for i, p in enumerate(ids) if st.state[i] in (1, 2)}
        if not here_ids:
            continue
        best = _best_reference(run.l1, U, k0, here_ids, th)
        out.append(best)
    return out


# ------------------------------------------------------------------------------------------------------------ 合成世界用例


@pytest.fixture(scope="module")
def synth_run(synth_event, tmp_path_factory):
    """合成事件上的 2 机 track_share 运行（120 s，运动学开发档）。"""
    from anet4d.baselines import BASELINES
    from test_env import _run, _spec

    d = tmp_path_factory.mktemp("timing") / "r"
    _run(_spec(synth_event, d, dur=120.0), lambda u: BASELINES["track_share"]())
    return d


def _close_pass(run_dir: Path, ev: Path, out: Path) -> Path:
    """构造贴近演员的轨迹：uav-01 在 20–40 s 贴着 person_a 头顶（含穿越圆柱）、uav-02 在 60–110 s 穿过并掠过 vehicle_c 包围体，
    两机全程 AIRBORNE。"""
    from awr.sim.events.tape import open_tape

    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(run_dir, out)
    f = _fleet(out)
    tape = open_tape(ev)
    idx = {a["id"]: a["idx"] for a in tape.actors_info()}
    f["status"] = np.full(f["status"].shape, "AIRBORNE", f["status"].dtype)
    for k in range(200, 401):
        c = tape.actor_states(k * TICK_NS, [idx["person_a"]]).pos_enu_m[0]
        f["pos"][k, 0] = c + np.array([0.2 + 0.02 * (k - 200), 0.0, 1.0 + 0.01 * (k - 200)])
    for k in range(600, 1101):
        st = tape.actor_states(k * TICK_NS, [idx["vehicle_c"]])
        c, yaw = st.pos_enu_m[0], float(st.yaw_rad[0])
        off = -6.0 + 0.024 * (k - 600)                                  # 沿车身纵轴由后向前穿过
        f["pos"][k, 1] = c + np.array([off * math.cos(yaw), off * math.sin(yaw), 0.8 + 0.004 * (k - 600)])
    _save_fleet(out, f)
    return out


def test_safety_vectorised_matches_reference(synth_event, synth_run, tmp_path: Path) -> None:
    from awr.agentsdk.env.core import _open_world
    from awr.bench.eval.run import load_run
    from awr.bench.eval.safety import safety
    from awr.bench.eval.thresholds import TH

    w, ev = synth_event
    world = _open_world(w)
    for d in (synth_run, _close_pass(synth_run, ev, tmp_path / "close")):
        run = load_run(d, ev)
        got, ref = safety(run, world), _safety_reference(run, world, TH)
        assert got["violations"] == ref["violations"] and got["penetrations"] == ref["penetrations"]
        for k, a in (("person", ref["dp"]), ("vehicle", ref["dv"])):
            exp = float(np.min(a)) if np.isfinite(a).any() else None
            assert (got["min_sep_m"][k] is None) == (exp is None)
            if exp is not None:
                assert got["min_sep_m"][k] == pytest.approx(exp, abs=1e-9)
    assert ref["penetrations"] >= 2 and ref["violations"]["person"] >= 1 and ref["violations"]["vehicle"] >= 1   # 构造轨迹确有穿越


def test_ao_vectorised_matches_reference(synth_event, synth_run, tmp_path: Path) -> None:
    from awr.agentsdk.task import scored_events
    from awr.bench.eval.ao import _RealIndex
    from awr.bench.eval.run import load_run
    from awr.bench.eval.thresholds import TH

    _w, ev = synth_event
    big = merge_runs([synth_run, synth_run, synth_run], tmp_path / "m6")
    for d in (synth_run, big):
        run = load_run(d, ev)
        E = [e for e in scored_events(ev) if e["type"] in run.task["scored_event_types"]]
        idx = _RealIndex(run.l1, run.uavs, TH)
        ref = _ao_real_reference(E, run.uavs, run, TH)
        info = {a["id"]: a for a in run.tape.actors_info()}
        got = []
        for e in E:
            k0 = min(round((e["a_s"] + e["b_s"]) / 2 * 10), run.n_ticks)
            ids = [p for p in e["participants"] if info[p]["cls"] == "person"]
            if not ids:
                continue
            st = run.tape.actor_states(k0 * TICK_NS, [info[p]["idx"] for p in ids])
            here = sorted(p for i, p in enumerate(ids) if st.state[i] in (1, 2))
            if here:
                got.append(idx.best(k0, here, TH.ao_window_s * 10))
        assert got == ref                                                 # 计分事件的关键时刻：逐值一致（同一浮点运算）
        # 任意关键时刻与参与者组合（含窗口边界 ±10 / ±11 tick）：与逐行扫描逐值一致，且覆盖到有证据（> 0）的组合
        rng = np.random.default_rng(7)
        acts = sorted({r["actor"] for r in run.l1})
        seqs = sorted({int(r["seq"]) for r in run.l1 if r["vis"] > 0})
        pos = 0
        for i in range(60):
            k0 = int(rng.choice(seqs)) + (10, -10, 11, -11, 0, 3)[i % 6]
            ids = sorted(set(rng.choice(acts, size=1 + i % 2).tolist()))
            q = idx.best(k0, ids, TH.ao_window_s * 10)
            assert q == _best_reference(run.l1, run.uavs, k0, set(ids), TH), (k0, ids)
            pos += q > 0
        assert pos >= 10
    assert idx.best(0, ["no_such_actor"], 10.0) == 0.0 and idx.best(0, [], 10.0) == 0.0


def test_constructed_inputs_evaluate(synth_event, synth_run, tmp_path: Path) -> None:
    """构造器：2 机 × 3 并联 → 6 机，再平铺到 300 s；评测通过 schema 校验、同一输入逐字节一致，机号、tick 与行数按构造规则扩展。"""
    from awr.agentsdk.env.core import _open_world

    w, ev = synth_event
    m = merge_runs([synth_run] * 3, tmp_path / "m")
    t = tile_run(m, tmp_path / "t", 300.0)
    man = json.loads((t / "manifest.json").read_text())
    f = _fleet(t)
    assert man["n_ticks"] == 3000 and f["pos"].shape[:2] == (len(f["t_ns"]), 6) and list(f["uavs"]) == [f"uav-{i:02d}" for i in range(1, 7)]
    assert np.all(np.diff(f["t_ns"]) == TICK_NS)
    n1 = len(_rows(synth_run / "truth" / "l1.jsonl"))
    n6 = len(_rows(t / "truth" / "l1.jsonl"))
    assert 3 * 2 * n1 <= n6 <= 3 * 3 * n1                                      # 120 s → 300 s 为 2.5 倍
    assert sorted(set(json.loads((t / "manifest.json").read_text())["agents"])) == [f"uav-{i:02d}" for i in range(1, 7)]
    r = time_eval(t, ev, _open_world(w), repeats=2)
    doc = r["doc"]
    assert r["identical"] and not [e.message for e in bench_validator("evaluation").iter_errors(doc)]
    assert {u for e in doc["ec"]["events"] for u in e["by"]} <= {f"uav-{i:02d}" for i in range(1, 7)}


def test_l2_constructors_feed_eu(synth_event, synth_run, tmp_path: Path) -> None:
    """L2 构造：由 L1 真值行派生无掩膜的 L2 行（旧格式）→ 补掩膜 → 补 report；评测走 L2 路径，框证据与点证据都能关联到演员。"""
    from awr.agentsdk.env.core import _open_world
    from awr.bench.eval.eu import decode_mask
    from awr.bench.eval.run import load_run

    w, ev = synth_event
    d = tile_run(synth_run, tmp_path / "l2", 120.0)
    l2 = []
    for i, r in enumerate(_rows(d / "truth" / "l1.jsonl")):
        h = max(round(r["h_unocc_px"]), 1)
        l2.append({"actor": r["actor"], "uav": r["uav"], "seq": r["seq"], "t_ns": r["t_ns"], "sensor": "eo", "h_unocc_px": r["h_unocc_px"],
                   "vis": r["vis"], "bbox_xywh": [40 * (i % 20), 30 * (i % 13), max(h // 3, 1), h] if r["vis"] > 0 else [0, 0, 0, 0]})
    _write_rows(d / "truth" / "l2.jsonl", l2)
    n_mask = fill_l2_masks(d)
    rows = _rows(d / "truth" / "l2.jsonl")
    assert n_mask == sum(1 for r in l2 if r["vis"] > 0) and n_mask > 0
    r0 = next(r for r in rows if "mask" in r and r["bbox_xywh"][2] >= 3)
    m = decode_mask(r0["mask"], r0["bbox_xywh"][2], r0["bbox_xywh"][3])
    assert m.shape == (r0["bbox_xywh"][3], r0["bbox_xywh"][2]) and m[m.shape[0] // 2, m.shape[1] // 2] and not m[0, 0]
    n_rep = add_l2_reports(d)
    reps = _rows(d / "reports.jsonl")
    ev_kinds = {k for x in reps for p in x["report"]["participants"] for e in p["evidence"] for k in ("bbox_xywh", "point_xy") if k in e}
    assert n_rep == len(reps) > 0 and ev_kinds == {"bbox_xywh", "point_xy"}
    assert load_run(d, ev).l1 is not None and len(load_run(d, ev).vis_boxes) > 0
    doc = time_eval(d, ev, _open_world(w), repeats=1)["doc"]
    assert not [e.message for e in bench_validator("evaluation").iter_errors(doc)]
    assert doc["eu"]["participants"]["resolved"] > 0


# ------------------------------------------------------------------------------------------------------------ 正式计时


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None


@pytest.mark.skipif(not _env("AWR_D3_EVAL_TIMING_L1_RUNS"), reason="formal timing runs in the dmax CPU pool: set AWR_D3_EVAL_TIMING_L1_RUNS= and AWR_D3_EVAL_TIMING_* to run")
def test_eval_timing_formal() -> None:
    """30 机 L1 360 s（10 个 3 机 L1 Run 并联）≤ 30 s；3 机 L2 360 s（L2 Run 平铺并补 report）≤ 120 s；各取 3 次中位。"""
    from awr.agentsdk.env.core import _open_world

    l1 = [Path(p) for p in os.environ["AWR_D3_EVAL_TIMING_L1_RUNS"].split(os.pathsep) if p]
    ev, wd = Path(os.environ["AWR_D3_EVAL_TIMING_EVENT"]), Path(os.environ["AWR_D3_EVAL_TIMING_WORLD"])
    work = Path(os.environ["AWR_D3_EVAL_TIMING_WORK"])
    reps = int(os.environ.get("AWR_D3_EVAL_TIMING_REPEATS", "3"))
    t0 = time.perf_counter()
    world = _open_world(wd)
    rec: dict = {"world_open_s": round(time.perf_counter() - t0, 3), "cpu_count": os.cpu_count(), "repeats": reps, "cases": {}}
    cases = {}
    t0 = time.perf_counter()
    big = merge_runs(l1, work / "l1-30uav-360s")
    assert len(_fleet(big)["uavs"]) == 30 and json.loads((big / "manifest.json").read_text())["n_ticks"] == 3600
    cases["l1_30uav_360s"] = (big, time.perf_counter() - t0)
    l2 = _env("AWR_D3_EVAL_TIMING_L2_RUN")
    if l2:
        t0 = time.perf_counter()
        t = tile_run(Path(l2), work / "l2-3uav-360s", 360.0)
        rec["l2_masks_filled"] = fill_l2_masks(t)
        n_rep = add_l2_reports(t)
        assert len(_fleet(t)["uavs"]) == 3 and n_rep > 0
        cases["l2_3uav_360s"] = (t, time.perf_counter() - t0)
    for name, (d, build_s) in cases.items():
        r = time_eval(d, ev, world, repeats=reps)
        doc = r.pop("doc")
        assert r["identical"] and not [e.message for e in bench_validator("evaluation").iter_errors(doc)]
        lines = {p.name: sum(1 for _ in p.open(encoding="utf-8")) for p in (d / "truth").glob("l?.jsonl")}
        rec["cases"][name] = {**r, "limit_s": LIMIT_S[name], "pass": r["median_s"] <= LIMIT_S[name], "build_s": round(build_s, 3),
                              "n_uav": len(_fleet(d)["uavs"]), "n_ticks": json.loads((d / "manifest.json").read_text())["n_ticks"],
                              "truth_rows": lines, "reports": sum(1 for _ in (d / "reports.jsonl").open(encoding="utf-8")),
                              "components": doc["components"], "ranked_valid": doc["ranked_valid"]}
    out = _env("AWR_D3_EVAL_TIMING_OUT")
    if out:
        Path(out).write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for name, c in rec["cases"].items():
        assert c["pass"], (name, c["median_s"], c["limit_s"])
