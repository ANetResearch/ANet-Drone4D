"""Synthetic raw engine outputs for the six conventions of M01 §6.2.2 (test fixture; M01-AC-001, AC-002).

A small scene (ground plane plus boxes) is rendered by z-buffer from an orbiting camera; each engine's raw output is then
produced in its own convention: gauge (frame 0, first scale frames, saddle reference view, chunk-centred view 0, world
prior, hidden Sim3), pose direction, rotation representation, pixel-centre convention and model preprocessing, and
confidence activation. `truth` keeps the world C2W poses and the engine-gauge scale so the normalised output can be
compared with `T0^-1 Ti` exactly.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from awr.reconstruction.conventions import CONVENTIONS, k_orig_to_model, preproc_identity, vggt_preproc
from awr.reconstruction.types import FRAME_KEY, FRAME_SCALE, ConventionSpec, RawFrame
from awr.world.georef.frames import mat_to_quat, quat_to_mat

W_ORIG, H_ORIG = 640, 360


def scene(rng: np.random.Generator, n: int = 40_000) -> np.ndarray:
    g = np.c_[rng.uniform(-60, 60, (n // 2, 2)), np.zeros(n // 2)]
    boxes = []
    for cx, cy, sx, sy, h in ((-20, 10, 12, 8, 18), (15, -15, 10, 14, 25), (25, 25, 8, 8, 12), (-25, -25, 10, 10, 30)):
        m = n // 8
        f = rng.integers(0, 5, m)
        u, v = rng.random(m), rng.random(m)
        x = np.where(f == 0, cx - sx / 2, np.where(f == 1, cx + sx / 2, cx - sx / 2 + u * sx))
        y = np.where(f == 2, cy - sy / 2, np.where(f == 3, cy + sy / 2, cy - sy / 2 + (u if f[0] > 1 else v) * sy))
        y = np.where(f <= 1, cy - sy / 2 + u * sy, y)
        z = np.where(f == 4, h, v * h)
        x = np.where(f == 4, cx - sx / 2 + u * sx, x)
        y = np.where(f == 4, cy - sy / 2 + v * sy, y)
        boxes.append(np.c_[x, y, z])
    return np.vstack([g, *boxes])


def look_at(C: np.ndarray, target: np.ndarray) -> np.ndarray:
    z = target - C
    z /= np.linalg.norm(z)
    x = np.cross(z, [0.0, 0.0, 1.0])
    x /= np.linalg.norm(x)
    return np.stack([x, np.cross(z, x), z], 1)


def orbit(n: int, radius: float = 50.0, alt: float = 30.0, turns: float = 1.0) -> list[np.ndarray]:
    out = []
    for k in range(n):
        th = 2 * np.pi * turns * k / n
        C = np.array([radius * np.cos(th), radius * np.sin(th), alt + 3 * np.sin(3 * th)])
        T = np.eye(4)
        T[:3, :3] = look_at(C, np.array([0.3 * C[0], 0.3 * C[1], 0.0]))
        T[:3, 3] = C
        out.append(T)
    return out


_CACHE: dict = {}


def render(P: np.ndarray, T_wc: np.ndarray, K: np.ndarray, W: int, H: int, pixel_center: str) -> np.ndarray:
    key = (P.ctypes.data, P.shape, T_wc.tobytes(), K.tobytes(), W, H, pixel_center)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    if len(_CACHE) > 4096:
        _CACHE.clear()
    X = (P - T_wc[:3, 3]) @ T_wc[:3, :3]
    m = X[:, 2] > 0.5
    X = X[m]
    u = K[0, 0] * X[:, 0] / X[:, 2] + K[0, 2]
    v = K[1, 1] * X[:, 1] / X[:, 2] + K[1, 2]
    if pixel_center == "integer":
        iu, iv = np.rint(u).astype(np.int64), np.rint(v).astype(np.int64)
    else:
        iu, iv = np.floor(u).astype(np.int64), np.floor(v).astype(np.int64)
    ok = (iu >= 0) & (iu < W) & (iv >= 0) & (iv < H)
    pix = iv[ok] * W + iu[ok]
    z = X[ok, 2]
    o = np.argsort(-z)
    D = np.zeros(W * H)
    D[pix[o]] = z[o]
    D = D.reshape(H, W)
    D.setflags(write=False)
    _CACHE[key] = D
    return D


def rand_rot(rng: np.random.Generator) -> np.ndarray:
    q = rng.normal(size=4)
    return quat_to_mat(q / np.linalg.norm(q))


@dataclass
class Truth:
    T_world: list[np.ndarray]     # world C2W
    s_gauge: float                # engine units per metre
    K_orig: np.ndarray            # COLMAP-convention K of the original image


def _gauge(engine: str, conv: ConventionSpec, Tw: list[np.ndarray], rng: np.random.Generator):
    """(s, M) with engine pose = M @ (world pose with translation scaled by s); M is rigid."""
    M = np.eye(4)
    s = 1.0
    if conv.gauge == "frame0":
        M = np.linalg.inv(Tw[0])
    elif conv.gauge == "scale_frames":                     # near the first 8 frames: mean of their centres, random yaw
        M[:3, :3] = rand_rot(rng)
        M[:3, 3] = -M[:3, :3] @ np.mean([T[:3, 3] for T in Tw[:8]], axis=0)
        s = float(np.exp(rng.uniform(np.log(0.02), np.log(0.2))))
    elif conv.gauge == "ref_view":                         # saddle reference view (not frame 0)
        M = np.linalg.inv(Tw[len(Tw) // 3])
        s = float(np.exp(rng.uniform(np.log(0.02), np.log(0.2))))
    elif conv.gauge == "view0":                            # input poses minus the chunk centre
        M[:3, 3] = -np.mean([T[:3, 3] for T in Tw], axis=0)
    elif conv.gauge == "world_prior":                      # COLMAP pose prior: the world itself
        pass
    elif conv.gauge == "hidden_sim3":
        M[:3, :3] = rand_rot(rng)
        M[:3, 3] = rng.normal(size=3)
        s = float(np.exp(rng.uniform(np.log(1 / 100), np.log(1 / 20))))
    if engine == "frame0_unscaled":
        s = 1.0
    return s, M


def raw_frames(engine: str, n: int = 60, seed: int = 0, *, conv: ConventionSpec | None = None, points: np.ndarray | None = None,
               poses: list[np.ndarray] | None = None) -> tuple[list[RawFrame], Truth]:
    conv = conv or CONVENTIONS[engine]
    rng = np.random.default_rng(seed)
    P = scene(rng) if points is None else points
    Tw = orbit(n) if poses is None else poses
    fx = (W_ORIG / 2) / np.tan(np.radians(60.0) / 2)
    K_orig = np.array([[fx, 0, W_ORIG / 2], [0, fx, H_ORIG / 2], [0, 0, 1.0]])
    pre = vggt_preproc(W_ORIG, H_ORIG, target=126) if conv.pixel_center == "integer" else preproc_identity(W_ORIG, H_ORIG, 128, 72)
    K_model = k_orig_to_model(K_orig, pre, conv.pixel_center)
    s, M = _gauge(engine, conv, Tw, rng)
    frames = []
    for k, T in enumerate(Tw):
        D = render(P, T, K_model, pre.W_model, pre.H_model, conv.pixel_center)
        Ts = T.copy()
        Ts[:3, 3] *= s
        Ms = M.copy()
        Ms[:3, 3] *= s
        Te = Ms @ Ts                                        # engine C2W, engine units
        pose_m = Te if conv.pose_dir == "c2w" else np.linalg.inv(Te)
        if conv.quat_order == "matrix":
            pose = pose_m[:3, :4].copy()
        else:
            x, y, z, w = mat_to_quat(pose_m[:3, :3])
            if rng.random() < 0.5:                          # engines may emit q or -q
                x, y, z, w = -x, -y, -z, -w
            q = [x, y, z, w] if conv.quat_order == "xyzw" else [w, x, y, z]
            pose = np.r_[pose_m[:3, 3], q]
        valid = D > 0
        if conv.conf_kind == "expp1":
            conf = 1.0 + np.exp(rng.normal(1.0, 1.0, D.shape))
        elif conv.conf_kind == "sigmoid_logit":
            conf = rng.normal(0.0, 2.0, D.shape)
        elif conv.conf_kind == "reproj_err":
            conf = rng.uniform(0.0, 3.0, D.shape)
        elif conv.conf_kind == "synthetic":
            conf = rng.uniform(0.3, 1.0, D.shape) * valid
        else:
            conf = None
        frames.append(RawFrame(k, k * 100_000_000, pose, K_model, pre, (D * s).astype(np.float32), conf,
                               frame_type=FRAME_SCALE if k < 8 else FRAME_KEY))
    return frames, Truth(Tw, s, K_orig)
