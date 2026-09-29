"""Mock 目标表与检测器（M13-FR-040 至 FR-043、FR-045；M13 §6.5.9、§6.7.3；D1-ext）。

`P_d = P0·exp(−(r/R_fp)²)·LOS·vis·FOV` 为 1 s 凝视的检出概率，每个抽样 tick 换算为 `1 − (1 − P_d)^{Δt/T_look}`，因此检测
频率不改变结果。LOS 用 M04 `los_batch`（≤ 16 对，异常时视为 0 并计数 `los_skip`），vis 为 M07 550 nm 透过率
`exp(−optical_depth)`，FOV 用 `CameraGeom.in_fov`。抽样为流 4 的计数器 RNG：`uniform(seed, 4, agent_no, tick, 256 + 2·idx + d)`
（d = 0 相机、1 热成像）；首检 conf、位置误差、热成像帧种子取通道 `1024 + idx·8 + j`。

观察者：空中、已装配、对应检测器位 `det_en`（按机体有效能力集计算，M13-FR-041）与 `act` 位均为 1。候选：相机只对 UNSEEN，
热成像对 UNSEEN、SUSPECT，以及距上次发布 ≥ 2 s 的 CONFIRMED（复检）；水平距离 ≤ 3·R_fp。按 (agent_no, d, idx) 升序，
超过 16 对时按 tick 确定性轮转。同一 tick 同一目标多次命中时取 (agent_no, d) 最小者。

`expected_pd`、`bayes_miss`、`pd_1s` 与目标表只依赖 numpy（agent-runtime 与 M08 estimate 可直接 import）；`Detector`
的运行期依赖（相机几何、runtime）在方法内延迟导入。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .runtime import SensorRuntime

__all__ = ["DT_DETECT_S", "KIND_CODES", "MAX_PAIRS", "MAX_TARGETS", "REPEAT_NS", "TARGET_FIELDS", "TargetState", "TargetTable",
           "bayes_miss", "expected_pd", "pd_1s", "pd_tick"]

MAX_TARGETS = 64
MAX_PAIRS = 16
DT_DETECT_S = 0.2
REPEAT_NS = 2_000_000_000
KIND_CODES = {"person": 0, "vessel": 1, "vehicle": 2, "generic": 3}
KIND_NAMES = {v: k for k, v in KIND_CODES.items()}
CONF_FIRST_RANGE = (0.35, 0.6)
CONF_CONFIRM_DEFAULT = 0.9
ERR_CODE = 110


class TargetState:
    UNSEEN = 0
    SUSPECT = 1
    CONFIRMED = 2
    NAMES = ("unseen", "suspect", "confirmed")


TARGET_DTYPE = np.dtype([("used", "u1"), ("idx", "<u2"), ("target_id", "S16"), ("kind", "u1"), ("pos", "<f8", (3,)),
                         ("state", "u1"), ("conf", "<f4"), ("conf_first", "<f4"), ("conf_confirm", "<f4"),
                         ("t_first_ns", "<i8"), ("t_confirm_ns", "<i8"), ("t_last_emit_ns", "<i8"), ("apply_tick", "<i8")])
# 目标表作为 M08 分配的状态块 `sensor_targets` 登记（参与 checkpoint；容量行中只用前 64 行）
TARGET_FIELDS: dict[str, tuple[Any, tuple[int, ...]]] = {
    ("tg_" + name): (TARGET_DTYPE.fields[name][0].base, TARGET_DTYPE.fields[name][0].shape) for name in TARGET_DTYPE.names}


# ---------------------------------------------------------------- 纯函数（只依赖 numpy）
def pd_1s(p0: float | np.ndarray, r_fp_m: float | np.ndarray, r_m: np.ndarray, los: np.ndarray | float = 1.0,
          vis: np.ndarray | float = 1.0, fov: np.ndarray | float = 1.0) -> np.ndarray:
    """1 s 凝视检出概率 `P0·exp(−(r/R_fp)²)·LOS·vis·FOV`。"""
    r = np.asarray(r_m, np.float64)
    return np.asarray(p0, np.float64) * np.exp(-(r / np.asarray(r_fp_m, np.float64)) ** 2) * np.asarray(los, np.float64) \
        * np.asarray(vis, np.float64) * np.asarray(fov, np.float64)


def pd_tick(pd: np.ndarray, dt_s: float = DT_DETECT_S, t_look_s: float | np.ndarray = 1.0) -> np.ndarray:
    """每个抽样 tick 的检出概率 `1 − (1 − P_d)^{Δt/T_look}`。"""
    return 1.0 - (1.0 - np.asarray(pd, np.float64)) ** (float(dt_s) / np.asarray(t_look_s, np.float64))


def _vis(env: Any, p0: np.ndarray, p1: np.ndarray, t_sim_ns: int) -> np.ndarray:
    if env is None or not callable(getattr(env, "optical_depth", None)):
        return np.ones(p0.shape[0])
    try:
        tau = np.asarray(env.optical_depth(p0, p1, int(t_sim_ns), wavelength_nm=550.0), np.float64).reshape(-1)
        return np.exp(-np.maximum(tau, 0.0))
    except Exception:
        return np.ones(p0.shape[0])


def expected_pd(spec: Any, p_uav: np.ndarray, p_tgt: np.ndarray, env: Any = None, t_sim_ns: int = 0, *,
                los: np.ndarray | float = 1.0) -> np.ndarray:
    """报价用的期望检出概率（与检测器同一公式，FOV 视为 1；M13-FR-045）。spec 为 DetectorSpec（p0、r_fp_m）。"""
    A = np.asarray(p_uav, np.float64).reshape(-1, 3)
    B = np.asarray(p_tgt, np.float64).reshape(-1, 3)
    A, B = np.broadcast_arrays(A, B)
    r = np.linalg.norm(B - A, axis=1)
    return pd_1s(float(spec.p0), float(spec.r_fp_m), r, los, _vis(env, A, B, t_sim_ns), 1.0)


def bayes_miss(p_c: np.ndarray, pd: np.ndarray) -> np.ndarray:
    """未检出时的贝叶斯更新 `p_c·(1 − pd)/(1 − p_c·pd)`（x01 §3.11；M14 概率栅格）。"""
    pc = np.asarray(p_c, np.float64)
    p = np.asarray(pd, np.float64)
    return pc * (1.0 - p) / (1.0 - pc * p)


# ---------------------------------------------------------------- 目标表
class TargetError(ValueError):
    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(f"{ERR_CODE} {detail}")
        self.code = ERR_CODE
        self.detail = detail
        self.extra = extra


class RowRef:
    """目标表第 i 行的字段访问（SoA 上的 `row[name]` 读写）。"""

    __slots__ = ("i", "rows")

    def __init__(self, rows: dict[str, np.ndarray], i: int) -> None:
        self.rows = rows
        self.i = i

    def __getitem__(self, name: str) -> Any:
        return self.rows[name][self.i]

    def __setitem__(self, name: str, v: Any) -> None:
        self.rows[name][self.i] = v


class TargetTable:
    """≤ 64 行的 Mock 目标表（剧本 `target.spawn` 写入）。字段为 SoA：独立使用时自带数组；sim-core 中 `bind()` 到 M08 分配的
    状态块 `sensor_targets`（字段 `tg_<name>`），随 checkpoint 保存与恢复。`rows[name]` 为该字段前 64 行的视图。"""

    def __init__(self) -> None:
        self.rows: dict[str, np.ndarray] = {}
        self._own = np.zeros(MAX_TARGETS, TARGET_DTYPE)
        self.rows = {name: self._own[name] for name in TARGET_DTYPE.names}

    def bind(self, block: dict[str, np.ndarray]) -> None:
        """改用状态块数组；本地已有的行（装配前的 spawn）拷入块中（块为空时）。"""
        rows = {name: block["tg_" + name][:MAX_TARGETS] for name in TARGET_DTYPE.names}
        if self.rows["used"] is not rows["used"] and not rows["used"].any() and self.rows["used"].any():
            for name in TARGET_DTYPE.names:
                rows[name][...] = self.rows[name]
        self.rows = rows

    @property
    def n(self) -> int:
        return int(np.count_nonzero(self.rows["used"]))

    def clear(self) -> None:
        for a in self.rows.values():
            a[...] = 0

    def find(self, target_id: str) -> int:
        tid = target_id.encode("utf-8")
        for i in range(self.n):
            if self.rows["target_id"][i] == tid:
                return i
        return -1

    def spawn(self, target_id: str, pos_enu_m: Any, kind: str = "generic", *, conf_first: float | None = None,
              conf_confirm: float | None = None, apply_tick: int = 0) -> int:
        tid = str(target_id)
        raw = tid.encode("utf-8")
        if not tid or len(raw) > 16:
            raise TargetError("TARGET_ID_INVALID", target_id=tid)
        if self.find(tid) >= 0:
            raise TargetError("TARGET_ID_DUP", target_id=tid)
        n = self.n
        if n >= MAX_TARGETS:
            raise TargetError("TARGET_TABLE_FULL", target_id=tid)
        if kind not in KIND_CODES:
            raise TargetError("TARGET_KIND_UNKNOWN", kind=kind)
        p = [float(v) for v in pos_enu_m]
        if len(p) != 3 or not all(math.isfinite(v) for v in p):
            raise TargetError("TARGET_POS_INVALID", target_id=tid)
        i, r = n, self.rows
        r["idx"][i] = i
        r["target_id"][i] = raw
        r["kind"][i] = KIND_CODES[kind]
        r["pos"][i] = p
        r["state"][i] = TargetState.UNSEEN
        r["conf"][i] = 0.0
        r["conf_first"][i] = np.nan if conf_first is None else float(conf_first)
        r["conf_confirm"][i] = np.nan if conf_confirm is None else float(conf_confirm)
        r["t_first_ns"][i] = -1
        r["t_confirm_ns"][i] = -1
        r["t_last_emit_ns"][i] = -1
        r["apply_tick"][i] = int(apply_tick)
        r["used"][i] = 1
        return i

    def checkpoint(self) -> bytes:
        return b"".join(np.ascontiguousarray(self.rows[name]).tobytes() for name in TARGET_DTYPE.names)

    def restore(self, b: bytes) -> None:
        off = 0
        for name in TARGET_DTYPE.names:
            a = self.rows[name]
            size = a.nbytes
            a[...] = np.frombuffer(b[off:off + size], a.dtype).reshape(a.shape)
            off += size

    def items(self) -> list[dict]:
        r = self.rows
        return [{"idx": int(r["idx"][i]), "target_id": r["target_id"][i].decode("utf-8"), "kind": KIND_NAMES[int(r["kind"][i])],
                 "pos_enu_m": [float(v) for v in r["pos"][i]], "state": TargetState.NAMES[int(r["state"][i])],
                 "conf": round(float(r["conf"][i]), 4)} for i in range(self.n)]


# ---------------------------------------------------------------- 检测器（运行期）
class Detector:
    def __init__(self, rt: SensorRuntime) -> None:
        self.rt = rt
        self.targets = TargetTable()
        self.pending: list[tuple[int, dict]] = []
        self.stats = {"ticks": 0, "pairs": 0, "rotated": 0, "los_skip": 0, "events": 0, "hits": 0}
        self.dt_s = DT_DETECT_S  # 测试开关可改为 0.1（M13-AC-021 频率无关）
        self.last_events: list[dict] = []

    def enabled(self) -> bool:
        return self.targets.n > 0

    def spawn(self, target_id: str, pos_enu_m: Any, kind: str = "generic", *, conf_first: float | None = None,
              conf_confirm: float | None = None, apply_tick: int | None = None) -> int:
        """TargetApi.spawn（M10 剧本导演在 sim-core 内调用；返回 idx，满或重复抛 TargetError(110, ...)）。"""
        return self.targets.spawn(target_id, pos_enu_m, kind, conf_first=conf_first, conf_confirm=conf_confirm,
                                  apply_tick=int(self.rt.tick if apply_tick is None else apply_tick))

    # ------------------------------------------------------------ tick
    def tick(self, S: Any, ctx: Any) -> int:
        from . import cbrng
        from .camera import CameraGeom, world_pose
        from .enums import SensorKind, SensorState

        rt = self.rt
        T = self.targets
        self.last_events = []
        if T.n == 0:
            return 0
        self.stats["ticks"] += 1
        b = rt.blk
        tick = int(ctx.tick)
        t_ns = int(ctx.t_ns)
        act = S.active_idx()
        if act.size == 0:
            return 0
        ok = (b["init"][act] == S.agent_no[act] + 1) & S.in_air[act] & (b["det_en"][act] != 0)
        obs = act[ok]
        if obs.size == 0:
            return 0
        n_t = T.n
        rows = {k: v[:n_t] for k, v in T.rows.items()}
        st = rows["state"]
        tpos = rows["pos"]
        # 候选对 (agent_no, d, idx)
        cand: list[tuple[int, int, int, int]] = []  # (agent_no, d, idx, slot)
        kinds = (SensorKind.CAMERA, SensorKind.THERMAL)
        P_obs = S.enu.pos[obs]
        for j, s in enumerate(obs):
            s = int(s)
            rig = rt.rig_of(s)
            if rig is None:
                continue
            for d, kind in enumerate(kinds):
                spec = rig.by_kind(kind)
                if spec is None or spec.detector is None or not (b["det_en"][s] >> d) & 1 or not (b["act"][s] & spec.bit):
                    continue
                if b["state"][s, kind] == SensorState.STANDBY:
                    continue
                if d == 0:
                    want = st == TargetState.UNSEEN
                else:
                    want = (st == TargetState.UNSEEN) | (st == TargetState.SUSPECT) | (
                        (st == TargetState.CONFIRMED) & (t_ns - rows["t_last_emit_ns"] >= REPEAT_NS))
                rh = np.hypot(tpos[:, 0] - P_obs[j, 0], tpos[:, 1] - P_obs[j, 1])
                a_no = int(S.agent_no[s])
                cand.extend((a_no, d, int(i), s) for i in np.flatnonzero(want & (rh <= 3.0 * spec.detector.r_fp_m)))
        if not cand:
            return 0
        cand.sort()
        n = len(cand)
        if n > MAX_PAIRS:
            start = ((tick // 50) * MAX_PAIRS) % n
            cand = sorted(cand[(start + k) % n] for k in range(MAX_PAIRS))
            self.stats["rotated"] += 1
        m = len(cand)
        self.stats["pairs"] += m
        agent = np.array([c[0] for c in cand], np.int64)
        dd = np.array([c[1] for c in cand], np.int64)
        ii = np.array([c[2] for c in cand], np.int64)
        ss = np.array([c[3] for c in cand], np.int64)
        tp = tpos[ii]
        ps = np.empty((m, 3))
        Rs = np.empty((m, 3, 3))
        fov = np.zeros(m, bool)
        specs = []
        PQ = S.enu.pose_enu_flu(ss)
        for k in range(m):
            spec = rt.rig_of(int(ss[k])).by_kind(kinds[int(dd[k])])
            specs.append(spec)
            p, R = world_pose(PQ[k:k + 1], spec, b["g_az"][ss[k:k + 1], dd[k]], b["g_el"][ss[k:k + 1], dd[k]])
            ps[k] = p[0]
            Rs[k] = R[0]
            fov[k] = bool(CameraGeom.in_fov(tp[k:k + 1], ps[k], Rs[k], spec, 1.0, spec.range_m)[0])
        los = np.zeros(m)
        if fov.any():
            w = getattr(ctx, "world", None)
            if w is None or not callable(getattr(w, "los_batch", None)):
                los[fov] = 1.0
            else:
                try:
                    los[fov] = np.asarray(w.los_batch(ps[fov], tp[fov]), np.float64)
                except Exception:
                    self.stats["los_skip"] += 1
        r = np.linalg.norm(tp - ps, axis=1)
        env = getattr(ctx, "env", None)
        vis = np.ones(m)
        if fov.any():
            vis[fov] = _vis(env, ps[fov], tp[fov], t_ns)
        p0 = np.array([sp.detector.p0 for sp in specs])
        rfp = np.array([sp.detector.r_fp_m for sp in specs])
        tl = np.array([sp.detector.t_look_s for sp in specs])
        Pd = pd_1s(p0, rfp, r, los, vis, fov.astype(np.float64))
        p = pd_tick(Pd, self.dt_s, tl)
        u = cbrng.uniform_elem(rt.seed, 4, agent, tick, 256 + 2 * ii + dd)
        hit = u < p
        done: set[int] = set()
        n_ev = 0
        for k in np.flatnonzero(hit):
            i = int(ii[k])
            if i in done:
                continue
            done.add(i)
            self.stats["hits"] += 1
            if self._on_detect(S, ctx, int(ss[k]), int(agent[k]), int(dd[k]), i, specs[k], ps[k], Rs[k], float(r[k]),
                               float(Pd[k]), float(vis[k]), tick, t_ns):
                n_ev += 1
        return n_ev

    def _on_detect(self, S: Any, ctx: Any, slot: int, agent_no: int, d: int, i: int, spec: Any, ps: np.ndarray,
                   R: np.ndarray, r: float, pd: float, vis: float, tick: int, t_ns: int) -> bool:
        from . import cbrng

        rt = self.rt
        row = RowRef(self.targets.rows, i)
        state = int(row["state"])
        base = 1024 + 8 * i
        repeat = False
        artifact = None
        if d == 0:
            if state != TargetState.UNSEEN:
                return False
            cf = float(row["conf_first"])
            if not math.isfinite(cf):
                u = float(cbrng.uniform_elem(rt.seed, 4, agent_no, tick, base + 0))
                cf = CONF_FIRST_RANGE[0] + (CONF_FIRST_RANGE[1] - CONF_FIRST_RANGE[0]) * u
            row["conf"] = cf
            row["state"] = TargetState.SUSPECT
            row["t_first_ns"] = t_ns
            row["t_last_emit_ns"] = t_ns
            new_state = TargetState.SUSPECT
        else:
            if state == TargetState.CONFIRMED:
                if t_ns - int(row["t_last_emit_ns"]) < REPEAT_NS:
                    return False
                repeat = True
            else:
                cc = float(row["conf_confirm"])
                row["conf"] = CONF_CONFIRM_DEFAULT if not math.isfinite(cc) else cc
                row["state"] = TargetState.CONFIRMED
                row["t_confirm_ns"] = t_ns
            row["t_last_emit_ns"] = t_ns
            new_state = TargetState.CONFIRMED
            artifact = self._artifact(ctx, spec, row, ps, R, r, vis, agent_no, tick, base)
        sig = float(spec.detector.pos_sigma_m)
        e = cbrng.normal_elem(rt.seed, 4, agent_no, tick, base + np.arange(1, 4)) * sig
        pos = [round(float(row["pos"][k] + e[k]), 3) for k in range(3)]
        uav = rt.uav_id(S, slot)
        data = {"uav": uav, "sensor": spec.name, "capability": spec.detector.capability,
                "target_id": row["target_id"].decode("utf-8"), "target_kind": KIND_NAMES[int(row["kind"])],
                "pos_enu_m": pos, "range_m": round(r, 3), "pd": round(pd, 6), "conf": round(float(row["conf"]), 4),
                "state": TargetState.NAMES[new_state], "repeat": repeat, "artifact": artifact}
        self.last_events.append(data)
        self.stats["events"] += 1
        ev = getattr(ctx, "events", None)
        if ev is not None:
            ev.emit("sensor.detect", t_sim_ns=t_ns, severity=1, uav=uav, fields=data)
        return True

    def _artifact(self, ctx: Any, spec: Any, row: Any, ps: np.ndarray, R: np.ndarray, r: float, vis: float, agent_no: int,
                  tick: int, base: int) -> dict:
        from . import cbrng
        from .camera import CameraGeom
        from .thermal_mock import FRAME_H, FRAME_W, background_temps

        it = spec.intr
        px = CameraGeom.pixel_of(np.asarray(row["pos"], np.float64)[None], ps, R, spec)[0]
        sx, sy = FRAME_W / it.w, FRAME_H / it.h
        u = float(px[0]) * sx if math.isfinite(px[0]) else FRAME_W / 2.0
        v = float(px[1]) * sy if math.isfinite(px[1]) else FRAME_H / 2.0
        size_px = max(1.0, it.fx * sx * 0.6 / max(r, 1e-3))
        t_env = _t_env(getattr(ctx, "env", None), row["pos"], int(ctx.t_ns))
        t_bg, t_tgt = background_temps(KIND_NAMES[int(row["kind"])], t_env)
        seed = int(cbrng.key(self.rt.seed, 4, agent_no, tick, base + 4))
        return {"kind": "thermal_frame", "params": {"w": FRAME_W, "h": FRAME_H, "u": round(u, 3), "v": round(v, 3),
                                                     "size_px": round(size_px, 4), "t_bg_c": round(t_bg, 3),
                                                     "t_tgt_c": round(t_tgt, 3), "t_env_c": round(t_env, 3),
                                                     "tau": round(float(vis), 6),
                                                     "netd_k": float(spec.noise.get("netd_k", 0.05)), "seed": str(seed)}}


def _t_env(env: Any, pos: Any, t_ns: int) -> float:
    if env is None or not callable(getattr(env, "query", None)):
        return 20.0
    try:
        q = env.query(np.asarray(pos, np.float64).reshape(1, 3), int(t_ns), fields=32)
        return float(q.temperature_c[0])
    except Exception:
        return 20.0
