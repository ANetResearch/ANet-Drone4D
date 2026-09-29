"""MockEngine (M01 §6.4.1; M01-FR-016 to FR-023). D1-ext.

Produces output shaped like a feed-forward engine on a CPU: along a synthetic helix or lawnmower path it z-buffers the
subsampled source geometry into 128x72 (or 256x144) depth maps, transfers the winning point's normal, adds 0.5 %
relative depth noise, derives a synthetic confidence from range and incidence, and expresses every pose and depth in a
hidden random Sim3 gauge (scale log-uniform in [1/100, 1/20], random rotation, N(0, 1) translation), i.e. without metric
scale or gravity alignment. The truth (hidden Sim3, true centres) goes to `mock_truth.json` in the job directory only
(M01-FR-023), never into the World Package.

Random streams (M01 §6.4.1; PCG64(SeedSequence([seed, stream, k]))): 12 hidden gauge (and drift windows), 13 depth
noise, 15 gravity noise; per-frame children keep results independent of evaluation order (P-M01-7).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import ClassVar

import numpy as np

from awr.world.georef.frames import quat_to_mat

from ..conventions import CONVENTIONS, preproc_identity
from ..ir.jsonio import write_json
from ..pipeline.source import SourceCloud
from ..types import (
    FRAME_KEY,
    FRAME_NONKEY,
    FRAME_SCALE,
    STREAM_RECON_MOCK_DEPTH_NOISE,
    STREAM_RECON_MOCK_GAUGE,
    STREAM_RECON_MOCK_GRAVITY,
    EngineCaps,
    EngineContext,
    FrameInput,
    RawFrame,
    SessionSpec,
    rng,
)
from .base import AdapterBase, register_engine
from .mock_paths import FlightPath

__all__ = ["H_ORIG", "W_ORIG", "HiddenGauge", "MockEngine", "MockInputs", "hidden_gauge"]

W_ORIG, H_ORIG = 1920, 1080
N_SCALE_FRAMES = 8
FAR_M, NEAR_M = 900.0, 0.5


@dataclass(frozen=True)
class HiddenGauge:
    """x_engine = s R x_world + t."""

    s: float
    R: np.ndarray
    t: np.ndarray

    def to_json(self) -> dict:
        from ..conventions import mat_to_quat_xyzw

        return {"s": self.s, "q": [float(v) for v in mat_to_quat_xyzw(self.R)], "t": [float(v) for v in self.t]}


def hidden_gauge(seed: int, window: int = 0) -> HiddenGauge:
    r = rng(seed, STREAM_RECON_MOCK_GAUGE, window)
    s = float(np.exp(r.uniform(np.log(1.0 / 100.0), np.log(1.0 / 20.0))))
    q = r.normal(size=4)
    q /= np.linalg.norm(q)
    return HiddenGauge(s, quat_to_mat(q), r.normal(size=3))


def _rot_z(deg: float) -> np.ndarray:
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


@dataclass
class MockInputs:
    source: SourceCloud
    path: FlightPath
    params: dict                     # resolved recon-job-params


@register_engine("mock")
class MockEngine(AdapterBase):
    version = "0.1.0"
    variant = "zbuffer"
    target_version = "V0.1"
    license: ClassVar[dict] = {"code": "project", "weights": "n/a"}

    def __init__(self, **kw) -> None:
        raw_dir = kw.pop("raw_pose_dir", "c2w")
        conv = CONVENTIONS["mock"]
        if raw_dir == "w2c":
            from dataclasses import replace

            conv = replace(conv, pose_dir="w2c")
        super().__init__(convention=conv, **kw)
        self.inputs: MockInputs | None = None
        self.gauge: HiddenGauge | None = None

    def capabilities(self) -> EngineCaps:
        return EngineCaps(True, None, frozenset({"batch", "depth"}), "cpu", 3000, None, 1.1)

    def prepare(self, session: SessionSpec, ctx: EngineContext) -> None:
        super().prepare(session, ctx)
        if not isinstance(session.source, MockInputs):
            raise TypeError("MockEngine needs MockInputs as session.source")
        self.inputs = session.source
        self.variant = f"zbuffer-{self.inputs.path.kind}"
        self.gauge = hidden_gauge(session.seed)

    # ---- helpers
    def _window_gauge(self, k: int) -> HiddenGauge:
        """Hidden gauge of frame k, with the optional per-window drift (M01-FR-021, P2)."""
        G = self.gauge
        drift = self.inputs.params["params"]["mock"].get("drift")
        if not drift:
            return G
        w = k // int(drift.get("window_frames", 120))
        if w == 0:
            return G
        r = rng(self.session.seed, STREAM_RECON_MOCK_GAUGE, 1000 + w)
        ds = 1.0 + r.uniform(-1.0, 1.0) * float(drift.get("scale_jitter", 0.03))
        yaw = r.uniform(-1.0, 1.0) * float(drift.get("yaw_jitter_deg", 0.5))
        return HiddenGauge(G.s * ds, G.R @ _rot_z(yaw), G.t)

    def frame_truth(self, k: int) -> dict:
        p = self.inputs.path
        return {"C_world": p.C[k].tolist(), "target_world": p.target[k].tolist()}

    def _run_raw(self, frames: Iterable[FrameInput]) -> Iterator[RawFrame]:
        inp = self.inputs
        prm = inp.params["params"]
        cam, mk = prm["camera"], prm["mock"]
        Wd, Hd = (int(v) for v in cam["depth_res"])
        fx = (W_ORIG / 2.0) / math.tan(math.radians(float(cam["hfov_deg"])) / 2.0)
        sx = Wd / W_ORIG
        fxd = fx * sx
        cxd, cyd = W_ORIG / 2.0 * sx, H_ORIG / 2.0 * (Hd / H_ORIG)
        Kd = np.array([[fxd, 0.0, cxd], [0.0, fxd * (Hd / H_ORIG) / sx, cyd], [0.0, 0.0, 1.0]])
        pre = preproc_identity(W_ORIG, H_ORIG, Wd, Hd)
        # unit viewing rays per pixel (half-pixel convention: pixel u covers [u, u + 1))
        uu, vv = np.meshgrid(np.arange(Wd) + 0.5, np.arange(Hd) + 0.5)
        rays = np.stack([(uu - Kd[0, 2]) / Kd[0, 0], (vv - Kd[1, 2]) / Kd[1, 1], np.ones_like(uu)], -1)
        rays /= np.linalg.norm(rays, axis=-1, keepdims=True)
        noise_rel = float(mk["depth_noise_rel"])
        g_noise = math.radians(float(mk.get("gravity_noise_deg", 0.2)))
        kf_int = int(mk.get("keyframe_interval", 1))
        src = inp.source
        xyz32 = src.xyz.astype(np.float32)
        down = np.array([0.0, 0.0, -1.0])
        for fi in frames:
            k = fi.idx
            C = inp.path.C[k]
            R_wc = inp.path.R_wc[k]
            idx = src.grid.cull(C, R_wc[:, 2], far_m=FAR_M)
            # float32 projection of the sampled source (depth error < 1e-4 m at 1 km; the pose stays float64)
            X = (xyz32[idx] - C.astype(np.float32)) @ R_wc.astype(np.float32)
            m = X[:, 2] > NEAR_M
            X, idx = X[m], idx[m]
            inv_z = 1.0 / X[:, 2]
            u = Kd[0, 0] * X[:, 0] * inv_z + Kd[0, 2]
            v = Kd[1, 1] * X[:, 1] * inv_z + Kd[1, 2]
            iu, iv = np.floor(u).astype(np.int64), np.floor(v).astype(np.int64)
            ok = (iu >= 0) & (iu < Wd) & (iv >= 0) & (iv < Hd)
            pix = iv[ok] * Wd + iu[ok]
            z = X[ok, 2].astype(np.float64)
            idx = idx[ok]
            # one packed key (pixel, depth in 10 um steps): a single stable sort puts the nearest point first per pixel
            key = (pix << 32) | np.minimum(np.rint(z * 1e5), 2**32 - 1).astype(np.int64)
            o = np.argsort(key, kind="stable")
            ps = pix[o]
            first = o[np.r_[True, ps[1:] != ps[:-1]]] if len(o) else o
            D = np.zeros(Wd * Hd)
            win = np.full(Wd * Hd, -1, dtype=np.int64)
            D[pix[first]] = z[first]
            win[pix[first]] = idx[first]
            D = D.reshape(Hd, Wd)
            win = win.reshape(Hd, Wd)
            valid = D > 0
            if noise_rel > 0:
                D = D * (1.0 + rng(self.session.seed, STREAM_RECON_MOCK_DEPTH_NOISE, k).normal(0.0, noise_rel, D.shape) * valid)
            n_w = np.zeros((Hd, Wd, 3), dtype=np.float64)
            n_w[valid] = src.normals[win[valid]]
            n_cam = (n_w @ R_wc).astype(np.float32)                # world -> camera (R_wc^T n)
            cosi = np.abs(np.einsum("hwc,hwc->hw", n_cam.astype(np.float64), -rays))
            conf01 = np.clip(1.0 - 0.5 * D / FAR_M - 0.3 * (1.0 - cosi), 0.05, 1.0) * valid
            G = self._window_gauge(k)
            R_ec = G.R @ R_wc
            c_ec = G.s * (G.R @ C) + G.t
            T = np.eye(4)
            T[:3, :3] = R_ec
            T[:3, 3] = c_ec
            pose = T if self.convention.pose_dir == "c2w" else np.linalg.inv(T)
            # gravity in the camera frame with 0.2 deg noise (IMU attitude error)
            g_cam = R_wc.T @ down
            if g_noise > 0:
                r = rng(self.session.seed, STREAM_RECON_MOCK_GRAVITY, k)
                axis = r.normal(size=3)
                axis /= np.linalg.norm(axis)
                ang = r.normal(0.0, g_noise)
                Kx = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
                Rn = np.eye(3) + math.sin(ang) * Kx + (1 - math.cos(ang)) * Kx @ Kx
                g_cam = Rn @ g_cam
            ft = FRAME_SCALE if k < N_SCALE_FRAMES else (FRAME_KEY if k % kf_int == 0 else FRAME_NONKEY)
            yield RawFrame(k, int(inp.path.t_ns[k]), pose[:3, :4].copy(), Kd, pre, (D * G.s).astype(np.float32), conf01,
                           normals=n_cam, normals_frame="cam", frame_type=ft,
                           extras={"gravity_cam": g_cam, "valid_ratio": float(valid.mean())})

    def write_truth(self, path, T_frame0_raw: np.ndarray) -> dict:
        """mock_truth.json (job directory only): hidden gauge, frame-0 raw pose and the implied T_world_engine."""
        from awr.world.georef.frames import Sim3

        G = self.gauge
        S_eng_world = Sim3.from_matrix(np.block([[G.s * G.R, G.t[:, None]], [np.zeros((1, 3)), np.ones((1, 1))]]))
        T0 = np.asarray(T_frame0_raw, dtype=np.float64)
        S_raw_norm = Sim3.from_matrix(T0)                         # normalised engine -> raw engine (rigid)
        S_world_norm = S_eng_world.inverse().compose(S_raw_norm)  # x_world = G^-1(T0 x_norm)
        doc = {"schema": "awr.recon.mock_truth.v1", "hidden_sim3": G.to_json(), "T_frame0_raw": T0.tolist(),
               "T_world_engine": S_world_norm.to_json(), "path": self.inputs.path.kind,
               "C_world": self.inputs.path.C.tolist(), "target_world": self.inputs.path.target.tolist(),
               "drift": self.inputs.params["params"]["mock"].get("drift")}
        write_json(path, doc)
        return doc
