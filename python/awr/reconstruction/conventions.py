"""Engine conventions table and normalisation pure functions (M01 §6.2.2, §6.2.3; M01-FR-002 to FR-006). D1-core.

Every engine output passes `normalize()` before it reaches the IR (P-M01-1): W2C poses are inverted in closed form,
the gauge is re-anchored on frame 0 (`T <- T0^-1 T`, recorded as `T_frame0_raw`), rotations are SVD-orthonormalised,
quaternions are xyzw with w >= 0, intrinsics are mapped back to original-image pixels in the COLMAP pixel-centre
convention and confidences to `conf_u8`. Quaternion and matrix maths come from M02 `frames.py` (AWR-03 §5.1 rule 8).
"""

from __future__ import annotations

import numpy as np

from awr.world.georef.frames import mats_to_quats, quat_to_mat

from .types import ConventionSpec, EngineFrame, NormState, PoseConventionError, Preproc, RawFrame

__all__ = [
    "CONVENTIONS",
    "conf_to_u8",
    "inv_se3",
    "k_model_to_orig",
    "k_orig_to_model",
    "k_scale",
    "mat_to_quat_xyzw",
    "normalize",
    "normals_to_engine",
    "orthonormalize",
    "pose_to_4x4",
    "preproc_identity",
    "preproc_resize",
    "vggt_preproc",
]

# M01 §6.2.2 (frozen at MS1). The Mock default is C2W; `params.mock.raw_pose_dir = w2c` swaps in a W2C spec at runtime.
CONVENTIONS: dict[str, ConventionSpec] = {
    "mock": ConventionSpec("c2w", "matrix", "opencv_rdf", "hidden_sim3", "half", "synthetic", "relative"),
    "lingbot_map": ConventionSpec("c2w", "xyzw", "opencv_rdf", "scale_frames", "integer", "expp1", "relative"),
    "vggt": ConventionSpec("w2c", "xyzw", "opencv_rdf", "frame0", "integer", "expp1", "relative"),
    "da3": ConventionSpec("w2c", "matrix", "opencv_rdf", "ref_view", "integer", "expp1", "relative"),
    "mapanything": ConventionSpec("c2w", "xyzw", "opencv_rdf", "view0", "integer", "expp1", "metric_predicted"),
    "colmap": ConventionSpec("w2c", "wxyz", "opencv_rdf", "world_prior", "half", "reproj_err", "metric_conditioned",
                             revises_poses=True),
}


# ---------------------------------------------------------------- rigid helpers
def pose_to_4x4(pose: np.ndarray, quat_order: str) -> np.ndarray:
    """(4, 4) / (3, 4) matrix, or a 7-vector [tx, ty, tz, q0, q1, q2, q3] with q in `quat_order` -> float64 4x4."""
    p = np.asarray(pose, dtype=np.float64)
    T = np.eye(4)
    if p.shape in ((4, 4), (3, 4)):
        T[:3, :] = p[:3, :]
        return T
    if p.shape != (7,) or quat_order == "matrix":
        raise PoseConventionError(f"pose of shape {p.shape} does not match quat_order {quat_order!r}")
    q = p[3:] if quat_order == "xyzw" else p[[4, 5, 6, 3]]
    n = np.linalg.norm(q)
    if not n > 0:
        raise PoseConventionError("zero quaternion in raw pose")
    T[:3, :3] = quat_to_mat(q / n)
    T[:3, 3] = p[:3]
    return T


def inv_se3(T: np.ndarray) -> np.ndarray:
    """Closed-form rigid inverse [R^T, -R^T t] (the rotation block must be orthonormal)."""
    T = np.asarray(T, dtype=np.float64)
    out = np.eye(4)
    R = T[:3, :3]
    out[:3, :3] = R.T
    out[:3, 3] = -R.T @ T[:3, 3]
    return out


def orthonormalize(M: np.ndarray) -> np.ndarray:
    """Closest rotation by SVD; det must be +1 (a reflection is a convention error, 336)."""
    U, _, Vt = np.linalg.svd(np.asarray(M, dtype=np.float64))
    R = U @ Vt
    if np.linalg.det(R) < 0:
        raise PoseConventionError("rotation has det = -1 (reflected camera axes)")
    return R


def mat_to_quat_xyzw(R: np.ndarray) -> np.ndarray:
    """(..., 3, 3) -> (..., 4) xyzw with w >= 0 (M02 frames.mats_to_quats)."""
    return mats_to_quats(R)


# ---------------------------------------------------------------- intrinsics
def preproc_identity(W_orig: int, H_orig: int, W_model: int | None = None, H_model: int | None = None) -> Preproc:
    """Pure resize (no crop) from the original image to the model / depth resolution."""
    Wm = W_orig if W_model is None else W_model
    Hm = H_orig if H_model is None else H_model
    return Preproc(Wm / W_orig, Hm / H_orig, 0.0, 0.0, W_orig, H_orig, Wm, Hm)


def preproc_resize(W_orig: int, H_orig: int, W_resized: int, H_resized: int, crop_x: float = 0.0, crop_y: float = 0.0,
                   W_model: int | None = None, H_model: int | None = None) -> Preproc:
    """Resize to (W_resized, H_resized) then crop `crop_x`, `crop_y` pixels from the top-left."""
    return Preproc(W_resized / W_orig, H_resized / H_orig, float(crop_x), float(crop_y), W_orig, H_orig,
                   W_resized if W_model is None else W_model, H_resized if H_model is None else H_model)


def vggt_preproc(W_orig: int, H_orig: int, target: int = 518, patch: int = 14) -> Preproc:
    """VGGT `load_and_preprocess_images(mode="crop")`: width -> 518, height rounded to a multiple of 14, centre crop
    when taller than 518 (r02 §2.3; 1920x1080 -> 518x294, sx = 0.26979, sy = 0.27222)."""
    W = target
    H = round(H_orig * (W / W_orig) / patch) * patch
    crop_y = 0.0
    Hm = H
    if target < H:
        crop_y = (H - target) // 2
        Hm = target
    return preproc_resize(W_orig, H_orig, W, H, 0.0, crop_y, W, Hm)


def k_model_to_orig(K: np.ndarray, p: Preproc, pixel_center: str) -> np.ndarray:
    """Model-resolution K -> original-image K in COLMAP continuous coordinates (per-axis scale, crop undone; r02 §3.3)."""
    K = np.asarray(K, dtype=np.float64)
    off = 0.5 if pixel_center == "integer" else 0.0          # integer index = centre  ->  continuous coordinate + 0.5
    fx, fy = K[0, 0] / p.sx, K[1, 1] / p.sy
    cx = (K[0, 2] + off + p.crop_x) / p.sx
    cy = (K[1, 2] + off + p.crop_y) / p.sy
    return np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]])


def k_orig_to_model(K_orig: np.ndarray, p: Preproc, pixel_center: str) -> np.ndarray:
    """Inverse of k_model_to_orig (used by the round-trip test and by engines that need model-space K)."""
    K = np.asarray(K_orig, dtype=np.float64)
    off = 0.5 if pixel_center == "integer" else 0.0
    return np.array([[K[0, 0] * p.sx, 0.0, K[0, 2] * p.sx - p.crop_x - off],
                     [0.0, K[1, 1] * p.sy, K[1, 2] * p.sy - p.crop_y - off], [0.0, 0.0, 1.0]])


def k_scale(K_orig: np.ndarray, orig_wh: tuple[int, int], new_wh: tuple[int, int]) -> np.ndarray:
    """Rescale a COLMAP-convention K to another resolution (continuous coordinates scale linearly)."""
    sx = new_wh[0] / orig_wh[0]
    sy = new_wh[1] / orig_wh[1]
    K = np.asarray(K_orig, dtype=np.float64)
    return np.array([[K[0, 0] * sx, 0.0, K[0, 2] * sx], [0.0, K[1, 1] * sy, K[1, 2] * sy], [0.0, 0.0, 1.0]])


# ---------------------------------------------------------------- confidence
def conf_to_u8(c: np.ndarray | None, kind: str) -> np.ndarray | None:
    """conf -> u8 (M01-FR-005): expp1 `1 - 1/conf`, sigmoid for logits, reprojection error (0 px -> 255, >= 4 px -> 0),
    synthetic already in [0, 1]; `none` (or missing data) returns None and the writer uses 255 / conf_source = none."""
    if c is None or kind == "none":
        return None
    c = np.asarray(c, dtype=np.float64)
    if kind == "expp1":
        c01 = 1.0 - 1.0 / np.maximum(c, 1.0)
    elif kind == "sigmoid_logit":
        c01 = 1.0 / (1.0 + np.exp(-c))
    elif kind == "reproj_err":
        c01 = np.clip(1.0 - c / 4.0, 0.0, 1.0)
    elif kind == "synthetic":
        c01 = np.clip(c, 0.0, 1.0)
    else:
        raise ValueError(f"unknown conf_kind {kind!r}")
    return np.rint(255.0 * np.nan_to_num(c01, nan=0.0)).astype(np.uint8)


# ---------------------------------------------------------------- normalisation (M01 §6.2.3)
def normals_to_engine(raw: RawFrame, R_engine_cam: np.ndarray, st: NormState) -> np.ndarray | None:
    """Normals are rotated only: camera-frame normals by R_engine_cam, raw-gauge normals by the frame-0 rotation."""
    if raw.normals is None:
        return None
    Rn = R_engine_cam if raw.normals_frame == "cam" else st.T0_inv[:3, :3]
    return (np.asarray(raw.normals, dtype=np.float64) @ Rn.T).astype(np.float32)


def normalize(raw: RawFrame, conv: ConventionSpec, st: NormState) -> EngineFrame:
    T = pose_to_4x4(raw.pose, conv.quat_order)
    T[:3, :3] = orthonormalize(T[:3, :3])
    if conv.pose_dir == "w2c":
        T = inv_se3(T)
        st.inverted = True
    if st.T0_inv is None:                                   # frame 0 fixes the gauge change
        st.T_frame0_raw = T.copy()
        st.T0_inv = inv_se3(T)
    T = st.T0_inv @ T                                       # rigid: depth and scale unchanged
    R = orthonormalize(T[:3, :3])
    T[:3, :3] = R
    q = mat_to_quat_xyzw(R)
    K_orig = k_model_to_orig(raw.K_model, raw.preproc, conv.pixel_center)
    K_depth = None
    depth = None
    if raw.depth is not None:
        depth = np.asarray(raw.depth, dtype=np.float32)
        K_depth = k_scale(K_orig, (raw.preproc.W_orig, raw.preproc.H_orig), (depth.shape[1], depth.shape[0]))
    conf_u8 = conf_to_u8(raw.conf_raw, conv.conf_kind)
    st.frames += 1
    return EngineFrame(raw.idx, int(raw.t_ns), T, q, K_orig, K_depth, depth, conf_u8, normals_to_engine(raw, R, st),
                       int(raw.frame_type), None, dict(raw.extras))
