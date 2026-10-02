"""覆盖度量（M10-FR-052、FR-067、FR-068；M10 §6.3.5、§6.5.12、§6.5.16）：stage `coverage`（order 155、every 50、phase 13，5 Hz）。

- FacadeGrid（`facade_coverage`，core）：塔体外立面的柱面代理，半径 `r_fp = radius_m − standoff_m`，角向步长 `2 m/r_fp`，
  竖向 2 m；z 区间取 `facade_z_range_m`，缺省为任务 z 区间向下扩 5 m ∩ [dtm, 柱面内 DSM 最大高度]（S1 为 [45, 374.1]）。
  对执行 helix_scan 的机体，格心 x 满足以下四条即置 seen：在相机视锥内（相机看向中轴，HFOV/VFOV 取 M13 相机模型，
  缺省 60°/42.1°）；距离 ≤ 2·standoff；入射角 `acos(n·(cam − x)/‖·‖) ≤ 60°`；M04 视线 `segment_los(cam, x + 0.5·n)` 为真。
  只检查当前方位 ±60° 且高度在 ±VFOV 足迹内的未见格（每机每次 < 1000 格）；
- CoverageGrid（`area_coverage`，ext）：`res = max(2, sqrt(AOI 面积/65536))`、`owner u8`、`count u8`，按相机下视矩形
  `W × L`（随航向旋转）戳记执行覆盖类作业项的机体；`area_coverage` 为 AOI 内已扫描格的比例；
- `agl_min_m`、`agl_rms_err_m`（地形跟随段，10 Hz 由 5 Hz 采样近似）与 `formation_err_rms_m`（编队 CRUISE 段参考与实际位置之差）。
单次调用 > 1 ms 时自动降到 2 Hz（NFR-003；本模块只读 SimClock 仿真时刻，耗时统计由 pipeline 计时器给出）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .runtime import M10Runtime

__all__ = ["CoverageGrid", "CoverageTracker", "FacadeGrid", "blob_grid_u8"]

INCIDENCE_MAX = math.radians(60.0)


@dataclass
class FacadeGrid:
    center: np.ndarray            # (2,)
    r_fp: float
    z0: float
    z1: float
    standoff: float
    hfov: float = math.radians(60.0)
    vfov: float = math.radians(42.1)
    seen: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.uint8))
    d_ang: float = 0.0
    n_ang: int = 0
    n_z: int = 0

    def __post_init__(self) -> None:
        self.n_ang = max(8, math.ceil(2 * math.pi * self.r_fp / 2.0))
        self.d_ang = 2 * math.pi / self.n_ang
        self.n_z = max(1, math.ceil((self.z1 - self.z0) / 2.0))
        self.seen = np.zeros((self.n_z, self.n_ang), np.uint8)

    @property
    def ratio(self) -> float:
        return float(self.seen.mean()) if self.seen.size else 0.0

    def cells_near(self, cam: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        c = self.center
        az = math.atan2(cam[1] - c[1], cam[0] - c[0])
        k0 = round(az / self.d_ang)
        span = math.ceil(math.radians(60.0) / self.d_ang)
        ka = (np.arange(k0 - span, k0 + span + 1) % self.n_ang)
        dist = max(math.hypot(cam[0] - c[0], cam[1] - c[1]) - self.r_fp, 1.0)
        half = dist * math.tan(self.vfov / 2.0) + 2.0
        j0 = math.floor((cam[2] - half - self.z0) / 2.0)
        j1 = math.ceil((cam[2] + half - self.z0) / 2.0)
        kz = np.arange(max(0, j0), min(self.n_z, j1 + 1))
        return ka, kz

    def stamp(self, cam: np.ndarray, world: Any) -> int:
        """相机看向中轴（look_at_axis，俯仰 0）；返回新置位的格数。"""
        ka, kz = self.cells_near(cam)
        if ka.size == 0 or kz.size == 0:
            return 0
        # 与 np.meshgrid(ka, kz) 的行优先展开同序（广播视图代替 meshgrid 与 np.c_，逐位相同，5 Hz 的固定开销，ADR-060）
        shape = (kz.size, ka.size)
        new = self.seen[np.ix_(kz, ka)] == 0
        if not new.any():
            return 0
        ka_n = np.broadcast_to(ka, shape)[new]
        kz_n = np.broadcast_to(kz[:, None], shape)[new]
        ang = (ka_n + 0.5) * self.d_ang
        nrm = np.stack([np.cos(ang), np.sin(ang), np.zeros(ang.size)], axis=1)
        X = np.stack([self.center[0] + self.r_fp * nrm[:, 0], self.center[1] + self.r_fp * nrm[:, 1],
                      self.z0 + (kz_n + 0.5) * 2.0], axis=1)
        d = cam[None, :] - X
        dist = np.linalg.norm(d, axis=1)
        ok = dist <= 2.0 * self.standoff
        cosinc = (nrm * d).sum(1) / np.maximum(dist, 1e-9)
        ok &= cosinc >= math.cos(INCIDENCE_MAX)
        fwd = np.array([self.center[0] - cam[0], self.center[1] - cam[1], 0.0])
        fn = float(np.linalg.norm(fwd))
        if fn < 1e-6:
            return 0
        fwd /= fn
        left = np.array([-fwd[1], fwd[0], 0.0])
        v = X - cam[None, :]
        xf = v @ fwd
        ok &= xf > 0
        ok &= np.abs(np.arctan2(v @ left, np.maximum(xf, 1e-9))) <= self.hfov / 2.0
        ok &= np.abs(np.arctan2(v[:, 2], np.maximum(xf, 1e-9))) <= self.vfov / 2.0
        idx = np.flatnonzero(ok)
        if idx.size and world is not None:
            B = X[idx] + 0.5 * nrm[idx]
            los = np.ones(idx.size, bool)
            for i0 in range(0, idx.size, 64):
                A = np.repeat(cam[None, :], min(64, idx.size - i0), axis=0)
                try:
                    los[i0:i0 + 64] = np.asarray(world.los_batch(A, B[i0:i0 + 64]), bool)
                except Exception:
                    los[i0:i0 + 64] = True
            idx = idx[los]
        self.seen[kz_n[idx], ka_n[idx]] = 1
        return int(idx.size)


@dataclass
class CoverageGrid:
    x0: float
    y0: float
    res: float
    w: int
    h: int
    inside: np.ndarray            # bool[h, w]：AOI 内的格
    owner: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.uint8))
    count: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.uint8))
    dirty: bool = False
    seq: int = 0

    def __post_init__(self) -> None:
        self.owner = np.zeros((self.h, self.w), np.uint8)
        self.count = np.zeros((self.h, self.w), np.uint8)

    @classmethod
    def for_polygon(cls, poly: np.ndarray, max_cells: int = 65536) -> CoverageGrid:
        from awr.swarm.coverage import point_in_polygon, polygon_area

        P = np.asarray(poly, np.float64)[:, :2]
        res = max(2.0, math.sqrt(max(polygon_area(P), 1.0) / max_cells))
        while True:                                  # 含 1 格外边距后总格数仍 ≤ max_cells
            x0, y0 = (P.min(0) - res).tolist()
            x1, y1 = (P.max(0) + res).tolist()
            w = max(1, math.ceil((x1 - x0) / res))
            h = max(1, math.ceil((y1 - y0) / res))
            if w * h <= max_cells:
                break
            res *= math.sqrt(w * h / max_cells) * 1.001
        X, Y = np.meshgrid(x0 + (np.arange(w) + 0.5) * res, y0 + (np.arange(h) + 0.5) * res)
        inside = point_in_polygon(np.c_[X.ravel(), Y.ravel()], P).reshape(h, w)
        return cls(x0, y0, res, w, h, inside)

    @property
    def ratio(self) -> float:
        n = int(self.inside.sum())
        return float(((self.owner != 0) & self.inside).sum()) / n if n else 0.0

    def stamp(self, p: np.ndarray, psi: float, W: float, L: float, who: int) -> int:
        c, s = math.cos(psi), math.sin(psi)
        r = 0.5 * math.hypot(W, L)
        i0 = max(0, int((p[0] - r - self.x0) / self.res))
        i1 = min(self.w, int((p[0] + r - self.x0) / self.res) + 1)
        j0 = max(0, int((p[1] - r - self.y0) / self.res))
        j1 = min(self.h, int((p[1] + r - self.y0) / self.res) + 1)
        if i0 >= i1 or j0 >= j1:
            return 0
        X, Y = np.meshgrid(self.x0 + (np.arange(i0, i1) + 0.5) * self.res, self.y0 + (np.arange(j0, j1) + 0.5) * self.res)
        dx, dy = X - p[0], Y - p[1]
        along = c * dx + s * dy
        across = -s * dx + c * dy
        m = (np.abs(along) <= L / 2.0) & (np.abs(across) <= W / 2.0)
        sub_o = self.owner[j0:j1, i0:i1]
        new = m & (sub_o == 0)
        sub_o[new] = (who % 254) + 1
        sub_c = self.count[j0:j1, i0:i1]
        sub_c[m] = np.minimum(sub_c[m].astype(np.int32) + 1, 255).astype(np.uint8)
        n_new = int(new.sum())
        if n_new:
            self.dirty = True
        return n_new

    def snapshot_k(self, max_cells: int = 16384) -> int:
        k = 1
        while (math.ceil(self.h / k) * math.ceil(self.w / k)) > max_cells:
            k += 1
        return k

    def snapshot_geom(self, max_cells: int = 16384) -> dict:
        """推送快照的几何（R24 `coverage_grid`）：原点、格边长 res·k、抽取后的 w × h（行 0 在南）。"""
        k = self.snapshot_k(max_cells)
        return {"x0_m": float(self.x0), "y0_m": float(self.y0), "res_m": float(self.res * k),
                "w": math.ceil(self.w / k), "h": math.ceil(self.h / k), "k": int(k)}

    def snapshot(self, max_cells: int = 16384) -> tuple[np.ndarray, int]:
        """owner 按 k × k 步长抽取（每块取首个非零值），k 取使格数 ≤ max_cells 的最小整数。"""
        k = self.snapshot_k(max_cells)
        if k == 1:
            return self.owner.copy(), 1
        hh, ww = math.ceil(self.h / k), math.ceil(self.w / k)
        pad = np.zeros((hh * k, ww * k), np.uint8)
        pad[:self.h, :self.w] = self.owner
        blk = pad.reshape(hh, k, ww, k).transpose(0, 2, 1, 3).reshape(hh, ww, k * k)
        nz = blk != 0
        first = np.where(nz.any(-1), blk[np.arange(hh)[:, None], np.arange(ww)[None, :], nz.argmax(-1)], 0)
        return first.astype(np.uint8), k


BLOB_MAGIC = b"AWRB"
BLOB_U8 = 3


def blob_grid_u8(a: np.ndarray) -> bytes:
    """`awr.blob.grid_u8.v1`（17 §6.5）：16 B 头 `AWRB | u16 version=1 | u16 dtype=3 (u8) | u32 count | u32 comp=1`，
    其后为 count 个 u8（行优先，行 0 在南），不压缩。"""
    import struct

    b = np.ascontiguousarray(np.asarray(a, np.uint8)).ravel()
    return BLOB_MAGIC + struct.pack("<HHII", 1, BLOB_U8, int(b.size), 1) + b.tobytes()


LAG_ERR_M = 2.0
LAG_HOLD_NS = 5_000_000_000


class CoverageTracker:
    def __init__(self, rt: M10Runtime) -> None:
        self.rt = rt
        self.facade: dict[tuple, FacadeGrid] = {}
        self.facade_of: dict[str, tuple] = {}          # mid → facade key
        self.area: dict[str, CoverageGrid] = {}
        self.agl: dict[str, list[float]] = {}           # mid → [min, sum_sq, n]
        self.form: dict[str, list[float]] = {}          # mid → [sum_sq, n]
        self.lag: dict[tuple[str, int], int] = {}       # (mid, slot) → 偏差超限起始 t_ns（-1 为已告警）
        self.cov_emit: dict[str, tuple[int, float]] = {}   # mid → (上次 coverage.progress 的 t_ns, ratio)
        self.slow = False
        self.calls = 0

    def reset(self) -> None:
        self.facade.clear()
        self.facade_of.clear()
        self.area.clear()
        self.agl.clear()
        self.form.clear()
        self.lag.clear()
        self.cov_emit.clear()

    def _facade_for(self, m: Any) -> FacadeGrid | None:
        if m.mid in self.facade_of:
            return self.facade.get(self.facade_of[m.mid])
        p = m.spec.get("params") or {}
        try:
            c = np.asarray(p["center_enu_m"], np.float64)[:2]
            R = float(p["radius_m"])
            so = float(p.get("standoff_m", 30.0))
            z0, z1 = sorted(float(v) for v in p["z_range_m"])
        except (KeyError, TypeError, ValueError):
            return None
        r_fp = max(R - so, 1.0)
        key = (round(float(c[0]), 2), round(float(c[1]), 2), round(r_fp, 2))
        fz = p.get("facade_z_range_m")
        g = self.facade.get(key)
        w = self.rt.world
        if fz:
            lo, hi = sorted(float(v) for v in fz)
        else:
            lo, hi = z0 - 5.0, z1
            if w is not None:
                th = np.linspace(0, 2 * math.pi, 64, endpoint=False)
                ring = np.c_[c[0] + 0.7 * r_fp * np.cos(th), c[1] + 0.7 * r_fp * np.sin(th)]
                try:
                    top = float(np.max(w.height_dsm(np.vstack([ring, c[None]]))))
                    gnd = float(np.min(w.ground_dtm(np.vstack([ring, c[None]]))))
                    lo, hi = max(lo, gnd), min(hi, top)
                except Exception:
                    pass
        if g is None:
            hf = math.radians(float(self.rt.camera.get("hfov_deg", 60.0)))
            vf = math.radians(float(self.rt.camera.get("vfov_deg", 42.1)))
            g = FacadeGrid(c, r_fp, lo, max(hi, lo + 2.0), so, hf, vf)
            self.facade[key] = g
        else:
            # 多个任务共用一个立面：z 区间取并
            nlo, nhi = min(g.z0, lo), max(g.z1, hi)
            if nlo < g.z0 - 1e-6 or nhi > g.z1 + 1e-6:
                ng = FacadeGrid(g.center, g.r_fp, nlo, nhi, g.standoff, g.hfov, g.vfov)
                j = round((g.z0 - nlo) / 2.0)
                n = max(0, min(g.n_z, ng.n_z - j))
                ng.seen[j:j + n, :] = g.seen[:n, :]
                self.facade[key] = ng
                g = ng
        self.facade_of[m.mid] = key
        return g

    def stage(self, S: Any, ctx: Any) -> None:
        self.calls += 1
        if self.slow and self.calls % 2 == 1:     # 降到 2.5 Hz
            return
        rt = self.rt
        pos = S.enu.pos
        for mid in rt.missions.order:
            m = rt.missions.missions.get(mid)
            if m is None or m.state != "RUNNING":
                continue
            gen = m.spec.get("generator")
            working = [t for t in m.tracks.values() if t.state == "WORKING" and t.slot >= 0]
            if not working:
                continue
            if gen == "helix_scan":
                g = self._facade_for(m)
                if g is None:
                    continue
                for t in working:
                    g.stamp(np.asarray(pos[t.slot], np.float64), rt.world)
            elif gen in ("lawnmower", "corridor", "terrain_follow"):
                grid = self.area.get(mid)
                if grid is None:
                    reg = m.extra.get("region") if m.extra else None
                    if not reg:
                        continue
                    grid = self.area[mid] = CoverageGrid.for_polygon(np.asarray(reg, np.float64))
                cov = (m.extra.get("stats") or {}).get("coverage") or {}
                h = float(cov.get("h_eff_m", 60.0))
                W = 2.0 * h * math.tan(math.radians(float(rt.camera.get("hfov_deg", 60.0))) / 2.0)
                L = 2.0 * h * math.tan(math.radians(float(rt.camera.get("vfov_deg", 42.1))) / 2.0)
                for k, t in enumerate(working):
                    psi = rt.psi_enu(S, t.slot)
                    grid.stamp(np.asarray(pos[t.slot], np.float64), psi, W, L, k)
                if gen == "terrain_follow" and rt.world is not None:
                    self._agl(mid, m, working, pos)
            if gen == "formation":
                self._formation(mid, working, S)
        self._progress_events(S)

    def _agl(self, mid: str, m: Any, working: list, pos: np.ndarray) -> None:
        agl_t = float((m.spec.get("params") or {}).get("agl_m", 0.0))
        P = np.asarray([pos[t.slot] for t in working], np.float64)
        g = np.asarray(self.rt.world.ground_dtm(P[:, :2]), np.float64)
        agl = P[:, 2] - g
        st = self.agl.setdefault(mid, [math.inf, 0.0, 0.0])
        st[0] = min(st[0], float(agl.min()))
        st[1] += float(((agl - agl_t) ** 2).sum())
        st[2] += len(agl)

    def _formation(self, mid: str, working: list, S: Any) -> None:
        B = self.rt.tracker.B
        if B is None:
            return
        sl = [t.slot for t in working if int(B["kind"][t.slot]) == 3]
        if not sl:
            return
        e = np.linalg.norm(B["out_p"][sl] - S.enu.pos[sl], axis=1)
        st = self.form.setdefault(mid, [0.0, 0.0])
        st[0] += float((e ** 2).sum())
        st[1] += len(sl)
        # formation.member_lagging：pos_err > 2 m 持续 5 s【仿真】，每次超限只告警一次
        now = int(S.t_ns)
        for s, err in zip(sl, e.tolist(), strict=True):
            k = (mid, int(s))
            if err > LAG_ERR_M:
                t0 = self.lag.get(k)
                if t0 is None:
                    self.lag[k] = now
                elif t0 >= 0 and now - t0 >= LAG_HOLD_NS:
                    self.lag[k] = -1
                    vid = next((t.vehicle_id for t in working if t.slot == s), None)
                    self.rt.emit("formation.member_lagging", mid=mid, vehicle_id=vid, pos_err_m=round(err, 2), level=2)
            else:
                self.lag.pop(k, None)

    def _progress_events(self, S: Any) -> None:
        """coverage.progress：每个运行中的覆盖类任务 ≤ 1 Hz【仿真】，比例变化 ≥ 0.001 时发出。"""
        now = int(S.t_ns)
        for mid in sorted(self.facade_of.keys() | self.area.keys()):
            last = self.cov_emit.get(mid, (-(10 ** 18), -1.0))
            if now - last[0] < 1_000_000_000:
                continue
            r = self.facade_ratio([mid]) if mid in self.facade_of else self.area_ratio([mid])
            if r is None or abs(r - last[1]) < 1e-3:
                continue
            self.cov_emit[mid] = (now, r)
            self.rt.emit("coverage.progress", mid=mid, ratio=round(float(r), 4), level=0)
            g = self.area.get(mid)
            if g is not None and g.dirty:
                g.dirty = False
                g.seq += 1
                snap, _ = g.snapshot()
                self.rt.publish_coverage(mid, g.seq, blob_grid_u8(snap), g.snapshot_geom())

    # ------------------------------------------------------------ 度量
    def facade_ratio(self, mids: list[str]) -> float | None:
        keys = {self.facade_of[m] for m in mids if m in self.facade_of}
        if not keys:
            return None
        seen = sum(int(self.facade[k].seen.sum()) for k in keys)
        tot = sum(int(self.facade[k].seen.size) for k in keys)
        return seen / tot if tot else None

    def area_ratio(self, mids: list[str]) -> float | None:
        gs = [self.area[m] for m in mids if m in self.area]
        if not gs:
            return None
        n = sum(int(g.inside.sum()) for g in gs)
        s = sum(int(((g.owner != 0) & g.inside).sum()) for g in gs)
        return s / n if n else None

    def metrics_for(self, mid: str) -> dict:
        out = {}
        f = self.facade_ratio([mid])
        if f is not None:
            out["facade_coverage"] = round(f, 4)
        a = self.area_ratio([mid])
        if a is not None:
            out["area_coverage"] = round(a, 4)
        fr = self.form.get(mid)
        if fr and fr[1] > 0:
            out["formation_err_rms_m"] = round(math.sqrt(fr[0] / fr[1]), 3)
        return out
