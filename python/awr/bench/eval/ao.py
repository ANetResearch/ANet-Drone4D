"""主动观测效率 AO（M27-FR-090；P0a 为 v0）。

关键时刻（r6 口径，`key_moments`；K07 修正 A10 遗留 F-77）：每个含人员参与者的计分事件取事件区间的开始、中点与结束 3 个时刻；
区间优先取事件真值（`truth/r<k>/event_truth.json` 中该注释的首个命中 `interval_ms`，即 r6 的"已验证区间"），无真值时取声明窗口。
参与者须在场：某时刻有参与者已不在场（上车、进入建筑等瞬时事件在区间中点或结束时参与者已消失）时，改取该时刻之前、不早于窗口起点
前 `pad_s`（2 s，与 EC 窗口同口径）、全部人员参与者都在场的最近 tick（"事件前时刻"）；窗口内找不到全员在场的 tick 时取该时刻在场的参与者子集，无人在场则跳过。
同一事件换算后重合的时刻只计一次。AO 为全部（事件、关键时刻）比值的平均。
证据质量 `q = min(1, h / 30 px) · vis`（与识别同口径）：
- `AO_real`：τ ± 1 s 内同一架机对全部参与者实际获得的证据质量（取各参与者最大值后取最小，再对机取最大）÷ oracle 最优质量；
- `AO_pos_los`：τ 时各机所在位置（不看相机指向）的视点质量 ÷ oracle 最优质量（含静态网格遮挡）；
- `AO_pos`：同上但不计遮挡（只看距离），÷ 无遮挡 oracle 最优质量。
oracle：以参与者质心为中心、半径 `fx · 1.6 / 16`、10 m 网格与任务高度包络（10 m 一档）的候选视点上取最大质量；视点可见性以
身体 5 点视线计。P0a 的 oracle 按本 Run 的标称相机计算（版本 `ao-oracle-v0-grid@1`；相机覆盖的运行按覆盖后的相机，ADR-217，
`evaluator.run_camera`），P1 的 v1 按机队相机与云台范围计算。

实际质量的检索按列数组进行（D3-AC-18 评测耗时）：识别真值行一次性转为按 `seq` 排序的（机、演员、质量）列，每个关键时刻以二分
定位 τ ± 1 s 的行区间，再按（机、演员）分组取最大值；与逐行扫描的定义逐值一致。
"""

from __future__ import annotations

import math

import numpy as np

from awr.agentsdk.env.geom import los
from awr.bench.eval.thresholds import ORACLE_VERSION, TH

__all__ = ["active_observation", "key_moments", "truth_intervals"]

TICK_NS = 100_000_000


def _body_points(c: np.ndarray, H: float, L: float, yaw: float) -> np.ndarray:
    cy, sy = math.cos(yaw), math.sin(yaw)
    return c + np.array([[0, 0, 0.30 * H], [0, 0, 0.55 * H], [0, 0, 0.90 * H], [0.35 * L * cy, 0.35 * L * sy, 0.5 * H],
                         [-0.35 * L * cy, -0.35 * L * sy, 0.5 * H]])


def _quality(V: np.ndarray, persons: list[tuple[np.ndarray, float, np.ndarray]], fx: float, world, with_los: bool, th) -> np.ndarray:
    """V (m, 3) 视点；persons [(中心, 身高, 5 点)] → (m,) 质量（对人员取最小）。"""
    q = np.full(len(V), np.inf)
    for c, H, pts in persons:
        d = np.linalg.norm(V - (c + np.array([0, 0, H / 2])), axis=1)
        qp = np.minimum(1.0, fx * H / np.maximum(d, 1e-3) / th.ao_q_px)
        if with_los:
            A = np.repeat(V, 5, axis=0)
            B = np.tile(pts, (len(V), 1))
            vis = los(world, A, B).reshape(len(V), 5).mean(axis=1)
            qp = qp * vis
        q = np.minimum(q, qp)
    return q


class _RealIndex:
    """识别真值行的列索引：按 `seq` 稳定排序的机序号、演员序号与证据质量 `min(1, h_unocc / 30 px) · vis`（只收 U 内的机）。"""

    def __init__(self, rows: list[dict], U: list[str], th) -> None:
        uix = {u: i for i, u in enumerate(U)}
        self.n_u = len(U)
        self.aix: dict[str, int] = {}
        sel = [r for r in rows if r["uav"] in uix]
        seq = np.fromiter((int(r["seq"]) for r in sel), np.int64, len(sel))
        u = np.fromiter((uix[r["uav"]] for r in sel), np.int64, len(sel))
        a = np.fromiter((self.aix.setdefault(r["actor"], len(self.aix)) for r in sel), np.int64, len(sel))
        h = np.fromiter((r["h_unocc_px"] for r in sel), np.float64, len(sel))
        v = np.fromiter((r["vis"] for r in sel), np.float64, len(sel))
        o = np.argsort(seq, kind="stable")
        self.seq, self.u, self.a = seq[o], u[o], a[o]
        self.q = (np.minimum(1.0, h / th.ao_q_px) * v)[o]

    def best(self, k0: int, ids: list[str], win: float) -> float:
        """τ = k0 ± win（tick）内，同一架机对 `ids` 全部参与者都有证据时取各参与者最大质量的最小值，再对机取最大；无则 0。"""
        codes = [self.aix.get(p, -1) for p in ids]
        if not ids or min(codes) < 0 or not self.n_u:
            return 0.0
        i0 = int(np.searchsorted(self.seq, math.ceil(k0 - win), "left"))
        i1 = int(np.searchsorted(self.seq, math.floor(k0 + win), "right"))
        a = self.a[i0:i1]
        col = np.full(len(self.aix), -1, np.int64)
        col[codes] = np.arange(len(codes))
        j = col[a]
        m = j >= 0
        per = np.zeros((self.n_u, len(codes)))
        seen = np.zeros((self.n_u, len(codes)), bool)
        np.maximum.at(per, (self.u[i0:i1][m], j[m]), self.q[i0:i1][m])
        seen[self.u[i0:i1][m], j[m]] = True
        ok = seen.all(axis=1)
        return float(per[ok].min(axis=1).max()) if ok.any() else 0.0


def truth_intervals(truth_rows: list[dict] | None) -> dict[str, tuple[float, float]]:
    """{注释 id: 首个真值命中的区间（秒）}：`event_truth.json` 中带 `annotation` 的事件行按 `interval_ms` 起点取最早一处。"""
    out: dict[str, tuple[float, float]] = {}
    for r in truth_rows or ():
        a, iv = r.get("annotation"), r.get("interval_ms")
        if a is None or not iv or len(iv) != 2:
            continue
        t = (float(iv[0]) / 1000.0, float(iv[1]) / 1000.0)
        if str(a) not in out or t < out[str(a)]:
            out[str(a)] = t
    return out


def key_moments(e: dict, present: np.ndarray, n_ticks: int, interval_s: tuple[float, float] | None = None, *,
                pad_s: float = TH.pad_s) -> list[tuple[int, np.ndarray]]:
    """计分事件 `e` 的关键时刻（r6 口径，规则见模块说明）→ [(tick, 在场掩码)]，按 tick 升序、去重。

    `present`：(n_ticks + 1, n) bool，列为 `e` 的人员参与者（tick 末时刻在场，`RunData.present` 同口径）；`interval_s` 为真值区间，
    缺省取声明窗口 `[a_s, b_s]`。掩码给出该时刻计入的参与者（全员在场时全为 True）。"""
    a, b = interval_s if interval_s is not None else (float(e["a_s"]), float(e["b_s"]))

    def tick(t: float) -> int:
        return min(max(round(t * 10), 0), n_ticks)

    lo = tick(min(float(e["a_s"]), a) - pad_s)
    allp = present.all(axis=1)
    out: list[tuple[int, np.ndarray]] = []
    seen: set[tuple[int, bytes]] = set()
    for k in sorted({tick(a), tick((a + b) / 2), tick(b)}):
        nz = np.flatnonzero(allp[lo:k + 1]) if k >= lo else np.empty(0, np.int64)
        if nz.size:
            kk, m = lo + int(nz[-1]), np.ones(present.shape[1], bool)
        else:
            kk, m = k, present[k].copy()
            if not m.any():
                continue
        key = (kk, m.tobytes())
        if key not in seen:
            seen.add(key)
            out.append((kk, m))
    return sorted(out, key=lambda x: x[0])


def active_observation(E: list[dict], U: list[str], run, world, *, fx: float, envelope_agl_m: tuple[float, float], th=TH,
                       truth_rows: list[dict] | None = None, present: dict[str, np.ndarray] | None = None,
                       camera_model: str = "d4d_eo1080@1") -> dict:
    """→ `evaluation.json` 的 `ao`。`camera_model` 为 oracle 相机的型号（与 `fx` 对应，记入 `oracle.camera_models`）。`truth_rows` 为事件真值行（缺省按声明窗口取关键时刻）；`present` 为 {演员: (n_ticks + 1,) 在场}
    （评测器已算出时传入，缺省按磁带逐 tick 计算）。"""
    info = {a["id"]: a for a in run.tape.actors_info()}
    pos = np.asarray(run.fleet["pos"], np.float64)
    idx = _RealIndex(run.l1, U, th)
    ivs = truth_intervals(truth_rows)
    present = dict(present or {})
    real, pos_los, pos_only = [], [], []
    for e in E:
        ids = [p for p in e["participants"] if info[p]["cls"] == "person"]
        if not ids:
            continue
        miss = [p for p in ids if p not in present]
        if miss:
            pm = run.present(miss)
            present.update({p: pm[:, i] for i, p in enumerate(miss)})
        P = np.column_stack([np.asarray(present[p][:run.n_ticks + 1], bool) for p in ids])
        for k0, m in key_moments(e, P, run.n_ticks, ivs.get(str(e["id"])), pad_s=th.pad_s):
            sel = [p for p, ok in zip(ids, m, strict=True) if ok]
            st = run.tape.actor_states(k0 * TICK_NS, [info[p]["idx"] for p in sel])
            here = [(p, st.pos_enu_m[i], info[p]["size_m"][2], float(st.yaw_rad[i])) for i, p in enumerate(sel)]
            persons = [(c, H, _body_points(c, H, info[p]["size_m"][0], yaw)) for p, c, H, yaw in here]
            cen = np.mean([c for _p, c, _H, _y in here], axis=0)
            r_max = fx * 1.6 / th.h_px
            g = np.arange(-r_max, r_max + 1e-9, th.oracle_grid_m)
            gx, gy = np.meshgrid(g, g)
            mm = gx ** 2 + gy ** 2 <= r_max ** 2
            xy = np.stack([cen[0] + gx[mm], cen[1] + gy[mm]], 1)
            zg = np.asarray(world.ground_dtm(xy), np.float64)
            lo, hi = envelope_agl_m
            V = np.concatenate([np.column_stack([xy, zg + a]) for a in np.arange(lo, hi + 1e-9, th.oracle_alt_step_m)])
            q_star = float(_quality(V, persons, fx, world, True, th).max())
            q_star_nl = float(_quality(V, persons, fx, world, False, th).max())
            best = idx.best(k0, sorted(sel), th.ao_window_s * 10)
            Vu = pos[k0]
            real.append(min(1.0, best / max(q_star, 1e-9)))
            pos_los.append(min(1.0, float(_quality(Vu, persons, fx, world, True, th).max()) / max(q_star, 1e-9)))
            pos_only.append(min(1.0, float(_quality(Vu, persons, fx, world, False, th).max()) / max(q_star_nl, 1e-9)))
    return {"ao_real": float(np.mean(real)) if real else 0.0, "ao_pos_los": float(np.mean(pos_los)) if pos_los else 0.0,
            "ao_pos": float(np.mean(pos_only)) if pos_only else 0.0,
            "oracle": {"version": ORACLE_VERSION, "camera_models": [camera_model], "grid_m": th.oracle_grid_m, "alt_step_m": th.oracle_alt_step_m,
                       "envelope_agl_m": [float(envelope_agl_m[0]), float(envelope_agl_m[1])]}}
