#!/usr/bin/env python3
"""M13 golden（Python 与 TS 对拍的共同基准；M13-AC-003、AC-008、AC-010、AC-024）。

写出 `tests/sensors/golden/{frames,intrinsics,mid360}.json`：帧常量与云台旋转、相机投影/视锥角点/T_base_cam/画幅框/像素、
MID-360 参考公式采样点。数值全部由 `awr.sim.sensors`（frames、camera、lidar.pattern）计算；TS 用例
（apps/web/tests/sensors）读取同一文件。`--check` 只比较不写入。
用法：.venv/bin/python tests/sensors/gen_golden.py [--check]
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from awr.sim.sensors.camera import CameraGeom, project  # noqa: E402
from awr.sim.sensors.frames import R_FLU_CAM, R_FLU_OPT, gimbal_R, mount_R  # noqa: E402
from awr.sim.sensors.lidar.pattern import mid360_reference  # noqa: E402
from awr.sim.sensors.spec import Intrinsics, rig_for_model  # noqa: E402

GOLD = Path(__file__).resolve().parent / "golden"
GIMBALS_DEG = [(0.0, -15.0), (30.0, -60.0), (-120.0, 10.0), (0.0, -90.0), (150.0, 30.0)]
ASPECTS = [16 / 9, 4 / 3, 1.5, 1.0, 2.4]


def lst(a) -> list:
    return [float(x) for x in np.asarray(a, np.float64).ravel()]


def cam_cases() -> list[dict]:
    cam = rig_for_model("p600").by_name("camera")
    assert cam is not None and cam.intr is not None
    it = cam.intr
    base = {"w": it.w, "h": it.h, "fx": it.fx, "fy": it.fy, "cx": it.cx, "cy": it.cy, "t": lst(cam.mount_t), "R": lst(cam.mount_R)}
    off = dict(base, cx=it.cx + 37.5, cy=it.cy - 21.25, t=[0.1, 0.02, -0.05], R=lst(mount_R([0.0, 20.0, 5.0])))
    th = {"w": 640, "h": 512, "fx": 686.3, "fy": 686.3, "cx": 320.0, "cy": 256.0, "t": lst(cam.mount_t), "R": lst(cam.mount_R)}
    return [dict(base, id="p600_camera"), dict(off, id="offset_mount"), dict(th, id="thermal")]


def spec_like(c: dict):
    class S:
        name = c["id"]
        intr = Intrinsics(int(c["w"]), int(c["h"]), float(c["fx"]), float(c["fy"]), float(c["cx"]), float(c["cy"]))
        mount_R = np.asarray(c["R"], np.float64).reshape(3, 3)
        mount_t = np.asarray(c["t"], np.float64)
        range_m = 300.0
    return S


def projection(c: dict, aspect: float, near: float, far: float) -> list[float]:
    w, h, fx, fy, cx, cy = (float(c[k]) for k in ("w", "h", "fx", "fy", "cx", "cy"))
    a_s = w / h
    sx, sy, ox, oy = 2 * fx / w, 2 * fy / h, 1 - 2 * cx / w, 2 * cy / h - 1
    if aspect > a_s:
        k = a_s / aspect
        sx, ox = k * sx, k * ox
    elif aspect < a_s:
        k = aspect / a_s
        sy, oy = k * sy, k * oy
    A, B = -(far + near) / (far - near), -2 * far * near / (far - near)
    P = np.array([[sx, 0, ox, 0], [0, sy, oy, 0], [0, 0, A, B], [0, 0, -1, 0]])
    return lst(P.T)  # column-major


def frame_rect(c: dict, aspect: float) -> list[float]:
    a_s = float(c["w"]) / float(c["h"])
    kx = a_s / aspect if aspect > a_s else 1.0
    ky = aspect / a_s if aspect < a_s else 1.0
    return [-kx, -ky, kx, ky]


def intrinsics_golden() -> dict:
    rng = np.random.default_rng(13)
    out = []
    for c in cam_cases():
        S = spec_like(c)
        Rm = S.mount_R
        g_rows = []
        for az_d, el_d in GIMBALS_DEG:
            Rg = gimbal_R(math.radians(az_d), math.radians(el_d))
            Rbs = Rm @ Rg
            T = np.eye(4)
            T[:3, :3] = Rbs @ R_FLU_CAM
            T[:3, 3] = S.mount_t
            corners = {}
            for L in (60.0, 17.5):
                pts = [S.mount_t] + [S.mount_t + Rbs @ (d * L) for d in CameraGeom.corner_dirs(S)]
                corners[str(L)] = lst(np.stack(pts))
            g_rows.append({"az_deg": az_d, "el_deg": el_d, "T_base_cam": lst(T.T), "corners": corners})
        pts = np.stack([rng.uniform(1, 400, 64), rng.uniform(-200, 200, 64), rng.uniform(-150, 150, 64)], axis=1)
        pos = np.zeros(3)
        u, v, _z = project(pts, pos, np.eye(3), S)
        out.append({"id": c["id"], "sensor": {k: c[k] for k in ("w", "h", "fx", "fy", "cx", "cy")},
                    "mount": lst(np.block([[Rm, S.mount_t[:, None]], [np.zeros((1, 3)), np.ones((1, 1))]])),
                    "gimbals": g_rows,
                    "projection": [{"aspect": a, "near": 0.2, "far": 5000.0, "P": projection(c, a, 0.2, 5000.0),
                                    "frameRect": frame_rect(c, a)} for a in ASPECTS],
                    "pixels": {"pts_flu": lst(pts), "uv": lst(np.stack([u, v], axis=1))}})
    return {"doc": "M13 intrinsics golden (gen_golden.py): column-major 4x4, corners origin + 4 far corners in the body frame",
            "cases": out}


def frames_golden() -> dict:
    return {"doc": "M13 frame constants (row-major 3x3) and Rz(az)·Ry(-el)", "R_FLU_OPT": lst(R_FLU_OPT), "R_FLU_CAM": lst(R_FLU_CAM),
            "gimbal": [{"az_deg": a, "el_deg": e, "R": lst(gimbal_R(math.radians(a), math.radians(e)))} for a, e in GIMBALS_DEG],
            "mount": [{"rpy_deg": r, "R": lst(mount_R(r))} for r in ([0, 0, 0], [0, 20, 0], [180, 0, 0], [5, -10, 30])]}


def mid360_golden() -> dict:
    frames = []
    idx = np.arange(0, 20000, 97)
    for f in (0, 1, 987654):
        az, el = mid360_reference(f)
        frames.append({"frame": f, "idx": [int(i) for i in idx], "az_deg": lst(az[idx]), "el_deg": lst(el[idx])})
    return {"doc": "MID-360 r04 reference formula samples (every 97th point)", "frames": frames}


def main() -> int:
    GOLD.mkdir(parents=True, exist_ok=True)
    files = {"frames.json": frames_golden(), "intrinsics.json": intrinsics_golden(), "mid360.json": mid360_golden()}
    bad = 0
    for name, doc in files.items():
        text = json.dumps(doc, indent=None, separators=(",", ":")) + "\n"
        p = GOLD / name
        if "--check" in sys.argv:
            if not p.exists() or p.read_text() != text:
                print(f"{p.relative_to(ROOT)} out of date", file=sys.stderr)
                bad += 1
        else:
            p.write_text(text)
            print(f"wrote {p.relative_to(ROOT)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
