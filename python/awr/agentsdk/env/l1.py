"""L1 检测级观测模型（M26 §6.6.3；r6 `Env._sense` 的产品化，P0a）。

每帧（相机 10 Hz，`t_mid` 取控制步时刻）对磁带中可见演员（moving、waiting）做视锥剔除（边距 20 px、深度 ≥ 1 m、斜距 ≤ 400 m），
对候选体取 5 个身体采样点（人：0.30H、0.55H、0.90H 与沿航向 ±0.35L 的 0.5H；车同式）做世界网格视线与车辆盒遮挡，
可见点比例 fv < 0.2 不出检测。像素高度 `h = fx · (H cos el + max(L, W) sin el) / R · fv`，大气透过率
`τ = exp(−3.912 R / V) · (1 − 0.02 r)`，检测概率 `logistic((h − 8) / 2) · clip((τ − 0.2) / 0.3, 0, 1)`；h ≥ 16 px 且 fv ≥ 0.5
时可识别并给出带噪外观。地面位置以本机导航误差为参照（真值 + e_xy + sigma(R) 噪声），`det_id` 每帧匿名（键含帧序号），
检测列表按 `det_id` 排序，不携带演员序号。随机数键 `(seed, 21, uav_no·65536 + actor_no, seq, ch)`。

真值侧记录（只写入运行真值层，不进入观测）：`det_id → actor`、无遮挡像素高度、fv、是否可识别。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from awr.agentsdk.env.camera import CameraModel, project
from awr.agentsdk.env.geom import los
from awr.agentsdk.rng import CH, STREAM_L1, hex64, nrm, u01

__all__ = ["L1Model", "L1Params"]

_VIS_STATES = (1, 2)


@dataclass(frozen=True)
class L1Params:
    margin_px: float = 20.0
    z_min_m: float = 1.0
    r_max_m: float = 400.0
    fv_min: float = 0.2
    px_mid: float = 8.0
    px_scale: float = 2.0
    ident_px: float = 16.0
    ident_vis: float = 0.5
    sig0_m: float = 0.4
    sig_per_m: float = 0.008
    range_rel: float = 0.02
    rgb_sigma: float = 12.0


def _ray_obb_block(org: np.ndarray, tgt: np.ndarray, c: np.ndarray, yaw: float, half: np.ndarray) -> np.ndarray:
    cy, sy = math.cos(yaw), math.sin(yaw)
    R = np.array([[cy, sy, 0.0], [-sy, cy, 0.0], [0.0, 0.0, 1.0]])
    o = (org - c) @ R.T
    d = (tgt - org) @ R.T
    with np.errstate(divide="ignore", invalid="ignore"):
        t1 = (-half - o) / d
        t2 = (half - o) / d
    tmin = np.nanmax(np.minimum(t1, t2), axis=1)
    tmax = np.nanmin(np.maximum(t1, t2), axis=1)
    return (tmax >= np.maximum(tmin, 0)) & (tmin <= 0.97)


def _appearance(info: dict) -> tuple[np.ndarray, np.ndarray | None]:
    a = info.get("appearance") or {}
    top = a.get("top_rgb") or a.get("body_rgb") or a.get("tint_rgb") or [128, 128, 128]
    bot = a.get("bottom_rgb")
    return np.asarray(top, np.float64), (np.asarray(bot, np.float64) if bot is not None else None)


class L1Model:
    def __init__(self, seed: int, world, tape, cam: CameraModel, params: L1Params | None = None) -> None:
        self.seed, self.world, self.tape, self.cam = int(seed), world, tape, cam
        self.p = params or L1Params()
        self.info = tape.actors_info()
        self.ids = [a["id"] for a in self.info]
        self.cls = np.array(["vehicle" if a["cls"] == "vehicle" else "person" for a in self.info])
        self.size = np.array([a["size_m"] for a in self.info], np.float64)          # (n, 3) L, W, H
        self.app = [_appearance(a) for a in self.info]
        self.n_rays = 0

    def frame(self, uav_no: int, seq: int, t_ns: int, o: np.ndarray, axes, e_nav: np.ndarray, env) -> tuple[list[dict], list[dict]]:
        """o 为相机光心（真值，ENU）；axes = (forward, right, down)；e_nav 为本机导航误差。→ (检测列表, 真值记录)。"""
        P = self.p
        st = self.tape.actor_states(t_ns)
        vis = np.isin(st.state, _VIS_STATES)
        idx = np.nonzero(vis)[0]
        if not len(idx):
            return [], []
        c = st.pos_enu_m[idx]
        H = self.size[idx, 2]
        u, v, z = project(self.cam, o, axes, c + np.stack([np.zeros_like(H), np.zeros_like(H), 0.5 * H], 1))
        R = np.linalg.norm(c - o, axis=1)
        m = P.margin_px
        cand = (z > P.z_min_m) & (u > -m) & (u < self.cam.width + m) & (v > -m) & (v < self.cam.height + m) & (P.r_max_m > R)
        ci = idx[cand]
        if not len(ci):
            return [], []
        yaw = st.yaw_rad[ci]
        L, W, Hh = self.size[ci, 0], self.size[ci, 1], self.size[ci, 2]
        cy, sy = np.cos(yaw), np.sin(yaw)
        base = st.pos_enu_m[ci]
        off = np.stack([
            np.stack([np.zeros_like(L), np.zeros_like(L), 0.30 * Hh], 1),
            np.stack([np.zeros_like(L), np.zeros_like(L), 0.55 * Hh], 1),
            np.stack([np.zeros_like(L), np.zeros_like(L), 0.90 * Hh], 1),
            np.stack([0.35 * L * cy, 0.35 * L * sy, 0.5 * Hh], 1),
            np.stack([-0.35 * L * cy, -0.35 * L * sy, 0.5 * Hh], 1)], 1)             # (n, 5, 3)
        pts = (base[:, None, :] + off).reshape(-1, 3)
        org = np.repeat(o[None], len(pts), 0)
        ok = los(self.world, org, pts)
        self.n_rays += len(pts)
        for j in np.nonzero(self.cls[idx] == "vehicle")[0]:                           # 车辆盒遮挡（目标为车辆本身的点除外）
            a = idx[j]
            cc = st.pos_enu_m[a] + np.array([0.0, 0.0, self.size[a, 2] / 2])
            blocked = _ray_obb_block(org, pts, cc, float(st.yaw_rad[a]), self.size[a] / 2)
            own = np.repeat(ci == a, 5)
            ok &= ~blocked | own | (np.linalg.norm(pts - cc, axis=1) < 3.0)
        fv_all = ok.reshape(-1, 5).mean(axis=1)
        dets, truth = [], []
        tau = math.exp(-3.912 * 1.0 / max(env.visibility_m, 1.0))                    # 每米透过率底数，按斜距取幂
        for n, a in enumerate(ci):
            fv = float(fv_all[n])
            if fv < P.fv_min:
                continue
            Rn = float(np.linalg.norm(base[n] - o))
            el = math.asin(min(max((o[2] - pts[n * 5, 2]) / Rn, -1.0), 1.0))
            size = Hh[n] * math.cos(el) + max(L[n], W[n]) * math.sin(el)
            h_unocc = self.cam.fx * size / Rn
            h_vis = h_unocc * fv
            t_atm = tau ** Rn * (1.0 - 0.02 * env.rain_mm_h)
            p_det = 1.0 / (1.0 + math.exp(-(h_vis - P.px_mid) / P.px_scale)) * min(max((t_atm - 0.2) / 0.3, 0.0), 1.0)
            ag = uav_no * 65536 + int(a)
            if u01(self.seed, STREAM_L1, ag, seq, CH["l1_det"]) >= p_det:
                continue
            ident = h_unocc >= P.ident_px and fv >= P.ident_vis
            sig = P.sig0_m + P.sig_per_m * Rn
            g = base[n, :2] + e_nav[:2] + sig * np.array([nrm(self.seed, STREAM_L1, ag, seq, CH["l1_gx"]),
                                                        nrm(self.seed, STREAM_L1, ag, seq, CH["l1_gy"])])
            det_id = hex64(self.seed, STREAM_L1, ag, seq, CH["l1_detid"])
            corners = self._corners(base[n], float(yaw[n]), L[n], W[n], Hh[n])
            cu, cv, _ = project(self.cam, o, axes, corners)
            x0, x1 = float(np.clip(cu.min(), 0, self.cam.width)), float(np.clip(cu.max(), 0, self.cam.width))
            y0, y1 = float(np.clip(cv.min(), 0, self.cam.height)), float(np.clip(cv.max(), 0, self.cam.height))
            d = {"det_id": det_id, "sensor_id": self.cam.sensor_id, "seq": int(seq), "t_mid_ns": int(t_ns), "class": str(self.cls[a]),
                 "bbox_xywh": [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)], "h_px": round(h_vis, 1),
                 "slant_range_m": round(Rn * (1.0 + P.range_rel * nrm(self.seed, STREAM_L1, ag, seq, CH["l1_range"])), 1),
                 "ground_xy_noisy": [round(float(g[0]), 2), round(float(g[1]), 2)]}
            if ident:
                top, bot = self.app[a]
                ap = {"top_rgb": self._rgb(top, ag, seq, 0)}
                if bot is not None:
                    ap["bottom_rgb"] = self._rgb(bot, ag, seq, 3)
                d["appearance"] = ap
            dets.append(d)
            truth.append({"det_id": det_id, "actor": self.ids[a], "h_unocc_px": round(h_unocc, 2), "vis": round(fv, 2), "ident": bool(ident)})
        order = np.argsort([d["det_id"] for d in dets], kind="stable")
        return [dets[i] for i in order], [truth[i] for i in order]

    def _rgb(self, base: np.ndarray, ag: int, seq: int, c0: int) -> list[int]:
        n = [nrm(self.seed, STREAM_L1, ag, seq, CH["l1_app"] + c0 + k) for k in range(3)]
        return [int(x) for x in np.clip(np.round(base + self.p.rgb_sigma * np.asarray(n)), 0, 255)]

    @staticmethod
    def _corners(c: np.ndarray, yaw: float, L: float, W: float, H: float) -> np.ndarray:
        cy, sy = math.cos(yaw), math.sin(yaw)
        return np.asarray([[c[0] + sl * L * cy - sw * W * sy, c[1] + sl * L * sy + sw * W * cy, c[2] + h]
                           for sl in (-0.5, 0.5) for sw in (-0.5, 0.5) for h in (0.0, H)])
