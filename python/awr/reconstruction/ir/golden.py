"""Small golden Recon IR session (24 frames) and the 12 mutation samples of M01 §6.3.4 (M01-AC-005).

`write_golden_session(dir)` writes a deterministic, valid session (no randomness, no clock); `MUTATIONS` maps each
mutation to the rules that must reject it. `python -m awr.reconstruction golden <out_dir>` writes the golden session
plus one directory per mutation (source for `packages/contracts/fixtures/recon/golden-session/`, request M01-to-M00).
"""

from __future__ import annotations

import json
import math
import shutil
from collections.abc import Callable
from pathlib import Path

import numpy as np

from awr.world.georef.frames import quat_to_mat

from ..conventions import mat_to_quat_xyzw
from .jsonio import write_json
from .trajectory_bin import write_trajectory_bin
from .writer import SessionWriter, frame_row

__all__ = ["GOLDEN_SESSION_ID", "MUTATIONS", "write_golden_session", "write_mutation"]

GOLDEN_SESSION_ID = "rs-0123456789ab"
N_GOLDEN = 24
_S = {"s": 57.3, "q": [0.1043, -0.312, 0.2215, 0.918], "t": [12.31, -40.08, 150.22]}


def _look_at(C: np.ndarray, target: np.ndarray) -> np.ndarray:
    z = target - C
    z /= np.linalg.norm(z)
    x = np.cross(z, [0.0, 0.0, 1.0])
    x /= np.linalg.norm(x)
    return np.stack([x, np.cross(z, x), z], 1)


def _sim3() -> tuple[float, np.ndarray, np.ndarray, list[float]]:
    q = np.asarray(_S["q"], dtype=np.float64)
    q = q / np.linalg.norm(q)
    return float(_S["s"]), quat_to_mat(q), np.asarray(_S["t"]), [float(v) for v in q]


def write_golden_session(root: Path) -> Path:
    """Write the golden session to `root/<GOLDEN_SESSION_ID>/` and return that directory."""
    d = Path(root) / GOLDEN_SESSION_ID
    if d.exists():
        shutil.rmtree(d)
    w = SessionWriter(d)
    s, R, t, q = _sim3()
    # engine-gauge poses: a small arc, frame 0 normalised to the identity
    th = np.linspace(0.0, math.pi / 2, N_GOLDEN)
    C = np.c_[3.0 * np.cos(th), 3.0 * np.sin(th), 1.5 + 0.02 * np.arange(N_GOLDEN)]
    Tw = []
    for k in range(N_GOLDEN):
        T = np.eye(4)
        T[:3, :3] = _look_at(C[k], np.array([0.5, 0.5, 0.0]))
        T[:3, 3] = C[k]
        Tw.append(T)
    T0i = np.linalg.inv(Tw[0])
    Te = [T0i @ T for T in Tw]
    fx = 1662.768775266122
    w.write_doc("engine", {
        "engine": "mock", "variant": "zbuffer-helix", "version": "0.1.0", "commit": None, "weights": None,
        "license": {"code": "project", "weights": "n/a", "notice": "golden fixture; synthetic, not a real capture"},
        "convention_raw": {"pose_dir": "c2w", "quat_order": "matrix", "camera_axes": "opencv_rdf", "gauge": "hidden_sim3",
                           "pixel_center": "half", "conf_kind": "synthetic"},
        "normalization": {"inverted": False, "renormalized_to_frame0": True, "T_frame0_raw": Tw[0].tolist(),
                          "self_check": {"method": "chamfer_pair_vote", "votes": ["c2w", "c2w", "c2w", "c2w"],
                                         "margins": [6.1, 5.4, 7.9, 4.8], "pass": True}},
        "engine_scale": "relative", "conditioning": {"intrinsics": True, "poses": "none", "depth": "none"},
        "params": {"path": "helix", "frames": N_GOLDEN, "fps": 10, "depth_res": [128, 72], "hfov_deg": 60.0, "seed": 1},
        "runtime": {"device": "cpu", "threads": 1, "dtype": "float64"}})
    w.write_doc("rig", {"rig_id": "mock-rig", "ref_sensor": "cam0", "sensors": [
        {"sensor_id": "cam0", "type": "CAMERA", "camera_id": 1, "T_body_sensor": {"q": [0.0, 0.0, 0.0, 1.0], "t": [0.0, 0.0, 0.0]},
         "time_offset_ns": 0},
        {"sensor_id": "gnss0", "type": "GNSS", "T_body_sensor": {"q": [0.0, 0.0, 0.0, 1.0], "t": [0.0, 0.0, 0.0]}, "time_offset_ns": 0}]})
    w.write_doc("cameras", {"cameras": [{"camera_id": 1, "model": "PINHOLE", "width": 1920, "height": 1080,
                                         "params": [fx, fx, 960.0, 540.0], "source": "synthetic", "pixel_convention": "colmap"}]})
    w.open_frames()
    t_rel = []
    for k, T in enumerate(Te):
        qk = mat_to_quat_xyzw(T[:3, :3])
        if k == 0:
            T = np.eye(4)
            qk = np.array([0.0, 0.0, 0.0, 1.0])
        tk = k * 100_000_000
        t_rel.append(tk)
        w.append_frame(frame_row(k, tk, T, qk, frame_type=0 if k < 8 else 1, pose_source="mock", conf_mean_u8=180 + k % 7,
                                 valid_ratio=0.9, gnss={"lat_deg": 22.5160584 + 1e-6 * k, "lon_deg": 113.9432472, "h_ellipsoid_m": 160.0,
                                                        "sigma_h_m": 2.0, "sigma_v_m": 3.0, "fix": "gnss"},
                                 gravity_cam=(T[:3, :3].T @ np.array([0.0, 0.0, -1.0]))))
        Te[k] = T
    w.close_frames()
    w.write_doc("alignment", {
        "T_world_engine": {"s": s, "q": q, "t": [float(v) for v in t]}, "method": "gnss-sim3", "scale_status": "gnss",
        "status": "accepted", "needs_review": False,
        "reference": {"kind": "gnss", "origin": "mock", "sigma_h_m": 2.0, "sigma_v_m": 3.0, "rtk_fixed_frac": 0.0},
        "n_sync": N_GOLDEN, "inliers": N_GOLDEN, "inlier_ratio": 1.0, "rmse_m": 3.71, "thr_m": 6.65, "rot_err_deg": None,
        "gravity": {"available": True, "used": False, "sv_ratio": 0.4, "virtual_len_m": None, "weight": 1.0},
        "time_offset_ns": 0, "time_offset_searched": False, "lever_arm_m": [0.0, 0.0, 0.0], "chunks": [],
        "gates": [{"name": "n_sync", "value": N_GOLDEN, "reject_below": 10, "result": "pass"}],
        "library": {"module": "awr.world.georef.sim3", "version": "0.1.0"}})
    Tarr = np.stack(Te)
    pos = s * Tarr[:, :3, 3] @ R.T + t
    qw = mat_to_quat_xyzw(R @ Tarr[:, :3, :3])
    write_trajectory_bin(d / "trajectory.bin", 0, t_rel, pos, qw, [0 if k < 8 else 1 for k in range(N_GOLDEN)],
                         [180 + k % 7 for k in range(N_GOLDEN)], [1] * N_GOLDEN)
    hist = [0] * 256
    hist[85], hist[200], hist[255] = 10, 20, 70
    w.write_doc("qa", {"status": "pass", "conf_hist_u8": hist, "dedupe_ratio": 0.5, "vox_engine": 0.01,
                       "pose_check": {"method": "chamfer_pair_vote", "votes": ["c2w", "c2w", "c2w", "c2w"],
                                      "margins": [6.1, 5.4, 7.9, 4.8], "pass": True},
                       "georef": {"method": "gnss-sim3", "status": "accepted", "inlier_ratio": 1.0, "rmse_m": 3.71,
                                  "sv_ratio": 0.4, "gravity_used": False},
                       "c2c_to_source_m": {"p50": 1.0, "p95": 2.5}, "gates": []})
    w.finalize({"session_id": GOLDEN_SESSION_ID,
                "source_world": {"id": "shenzhen", "content_version": "cf5fcd7b3791", "coordinate_sha256": "0" * 64},
                "engine_ref": "engine.json", "gauge": "engine", "scale_status": "gnss",
                "time_base": {"kind": "session", "t0_ns": "0", "sync": "none"},
                "input": {"kind": "world_sample", "world_id": "shenzhen", "path": "helix", "frames": N_GOLDEN},
                "counts": {"frames": N_GOLDEN, "keyframes": N_GOLDEN, "points_raw": 200, "points_fused": 100},
                "conf_source": "synthetic", "origin_engine": [0.0, 0.0, 0.0]})
    return d


# ---------------------------------------------------------------- mutations (M01 §6.3.4)
def _edit_json(d: Path, name: str, fn: Callable[[dict], None]) -> None:
    doc = json.loads((d / name).read_text(encoding="utf-8"))
    fn(doc)
    write_json(d / name, doc)


def _edit_frames(d: Path, fn: Callable[[list[dict]], None]) -> None:
    rows = [json.loads(x) for x in (d / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    fn(rows)
    (d / "frames.jsonl").write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows),
                                    encoding="utf-8")


def _m_w2c_as_c2w(d: Path) -> None:
    def inv(rows):
        for r in rows[1:]:
            q = np.asarray(r["T_engine_cam"]["q"])
            Rm = quat_to_mat(q)
            r["T_engine_cam"]["t"] = (-Rm.T @ np.asarray(r["T_engine_cam"]["t"])).tolist()
            r["T_engine_cam"]["q"] = [-q[0], -q[1], -q[2], q[3]]
    _edit_frames(d, inv)
    _edit_json(d, "engine.json", lambda e: e["normalization"]["self_check"].update(votes=["w2c", "w2c", "w2c"], **{"pass": False}))


def _m_wxyz(d: Path) -> None:
    def f(rows):
        for r in rows:
            x, y, z, w = r["T_engine_cam"]["q"]
            r["T_engine_cam"]["q"] = [w, x, y, z]
    _edit_frames(d, f)


def _m_frame0(d: Path) -> None:
    _edit_frames(d, lambda rows: rows[0]["T_engine_cam"].update(t=[0.3, -0.2, 0.1]))


def _m_det_neg(d: Path) -> None:
    def f(rows):
        for r in rows[1:]:
            Rm = quat_to_mat(np.asarray(r["T_engine_cam"]["q"])) @ np.diag([1.0, 1.0, -1.0])   # reflected camera
            tr = Rm[0, 0] + Rm[1, 1] + Rm[2, 2]
            w = math.sqrt(max(0.0, 1.0 + tr)) / 2.0                                          # naive formula on a reflection
            r["T_engine_cam"]["q"] = [(Rm[2, 1] - Rm[1, 2]) / 4.0, (Rm[0, 2] - Rm[2, 0]) / 4.0, (Rm[1, 0] - Rm[0, 1]) / 4.0, w]
    _edit_frames(d, f)


def _m_metric(d: Path) -> None:
    _edit_json(d, "session.json", lambda s: s.update(scale_status="metric"))
    _edit_json(d, "alignment.json", lambda a: a.update(scale_status="metric"))


def _m_method_relative(d: Path) -> None:
    _edit_json(d, "alignment.json", lambda a: a.update(scale_status="relative"))
    _edit_json(d, "session.json", lambda s: s.update(scale_status="relative"))


def _m_truncated(d: Path) -> None:
    b = (d / "trajectory.bin").read_bytes()
    (d / "trajectory.bin").write_bytes(b[:-10])


def _m_t_reversed(d: Path) -> None:
    def f(rows):
        rows[5]["t_ns"], rows[6]["t_ns"] = rows[6]["t_ns"], rows[5]["t_ns"]
    _edit_frames(d, f)


def _m_cam_params(d: Path) -> None:
    _edit_json(d, "cameras.json", lambda c: c["cameras"][0].update(params=c["cameras"][0]["params"][:3]))


def _m_weights_sha(d: Path) -> None:
    _edit_json(d, "engine.json", lambda e: e.update(weights={"repo": "robbyant/lingbot-map", "file": "lingbot-map.pt",
                                                             "license": "Apache-2.0"}))


def _m_wall_clock(d: Path) -> None:
    _edit_json(d, "session.json", lambda s: s.update(created_wall_ns="1790000000000000000"))


def _m_lever_scale(d: Path) -> None:
    _edit_json(d, "rig.json", lambda r: r["sensors"][1]["T_body_sensor"].update(q=[0.0, 0.0, 0.0, 2.0], t=[0.1, 0.0, 0.3]))


# name -> (mutator, rules any of which must fire)
MUTATIONS: dict[str, tuple[Callable[[Path], None], frozenset[str]]] = {
    "01-w2c-as-c2w": (_m_w2c_as_c2w, frozenset({"V07"})),
    "02-quat-wxyz": (_m_wxyz, frozenset({"V04", "V13"})),
    "03-frame0-not-identity": (_m_frame0, frozenset({"V05"})),
    "04-det-negative": (_m_det_neg, frozenset({"V04", "V13"})),
    "05-scale-status-metric": (_m_metric, frozenset({"V01", "V11"})),
    "06-gnss-sim3-but-relative": (_m_method_relative, frozenset({"V10"})),
    "07-trajectory-truncated": (_m_truncated, frozenset({"V12"})),
    "08-t-ns-reversed": (_m_t_reversed, frozenset({"V02"})),
    "09-camera-param-count": (_m_cam_params, frozenset({"V03"})),
    "10-weights-without-sha256": (_m_weights_sha, frozenset({"V08"})),
    "11-created-wall-ns": (_m_wall_clock, frozenset({"V16"})),
    "12-lever-arm-with-scale": (_m_lever_scale, frozenset({"V17"})),
}


def write_mutation(golden_dir: Path, name: str, out_root: Path) -> Path:
    d = Path(out_root) / name / GOLDEN_SESSION_ID
    if d.parent.exists():
        shutil.rmtree(d.parent)
    shutil.copytree(golden_dir, d)
    MUTATIONS[name][0](d)
    return d
