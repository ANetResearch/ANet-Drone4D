#!/usr/bin/env python3
"""程序化低模：按 `frame.type`、`motor.n` 与 `geometry` 生成十字臂 + 机身盒 + 电机 + 桨盘（M08-FR-048；AWR-16 §11.5；M08 §14 F-09）。

两档写入同一 glb 的两个 mesh：`lowpoly`（≤ 300 三角形）与 `lowpoly_s`（≤ 150 三角形，Tier S）。尺寸取自 params.yaml：
`geometry.arm_xy_m`（桨轴 x/y 偏移）、`geometry.rotor_z_m`（桨盘高度，缺省 0.12 m）、`prop.d_m`（桨径）、`geometry.wheelbase_m`；
PX4 quad_x 桨位（FLU）：(+a, −a)、(−a, +a)、(+a, +a)、(−a, −a)；hex_x 按 60° 均布。glTF (x, y, z) = (y_flu, z_flu, x_flu)。
用法：python tools/vehicles/lowpoly.py [--vehicle vehicles/p600] [--out <dir>] [--as-hero] [--copy-web]
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import GLTF_FROM_FLU, ROOT, glb_triangles, load_params, val

WEB_MODELS = ROOT / "apps" / "web" / "public" / "models"
MAX_LOW, MAX_LOW_S = 300, 150


def _box(c, half) -> tuple[np.ndarray, np.ndarray]:
    cx, cy, cz = c
    hx, hy, hz = half
    v = np.array([[cx + sx * hx, cy + sy * hy, cz + sz * hz] for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
    f = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
                  [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]])
    return v, f


def _arm(p0, p1, w, h) -> tuple[np.ndarray, np.ndarray]:
    """两点间的长方体臂（沿 xy 方向，截面 w × h）。"""
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    d = p1 - p0
    L = float(np.linalg.norm(d))
    u = d / L
    n = np.array([-u[1], u[0], 0.0])
    z = np.array([0.0, 0.0, 1.0])
    corners = [p0 + d * t + n * (a * w / 2) + z * (b * h / 2) for t in (0.0, 1.0) for a in (-1, 1) for b in (-1, 1)]
    v = np.array(corners)
    f = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
                  [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]])
    return v, f


def _prism(c, r, h, k) -> tuple[np.ndarray, np.ndarray]:
    """k 边棱柱（电机）：侧面 2k + 顶底各 k−2 三角形。"""
    ang = np.linspace(0, 2 * math.pi, k, endpoint=False)
    ring = np.stack([np.cos(ang) * r, np.sin(ang) * r], 1)
    bot = np.c_[ring + c[:2], np.full(k, c[2] - h / 2)]
    top = np.c_[ring + c[:2], np.full(k, c[2] + h / 2)]
    v = np.vstack([bot, top])
    f = []
    for i in range(k):
        j = (i + 1) % k
        f += [[i, j, k + j], [i, k + j, k + i]]
    for i in range(1, k - 1):
        f += [[0, i + 1, i], [k, k + i, k + i + 1]]
    return v, np.array(f)


def _disc(c, r, k, double: bool) -> tuple[np.ndarray, np.ndarray]:
    ang = np.linspace(0, 2 * math.pi, k, endpoint=False)
    v = np.c_[np.cos(ang) * r + c[0], np.sin(ang) * r + c[1], np.full(k, c[2])]
    f = [[0, i, i + 1] for i in range(1, k - 1)]
    if double:
        f += [[0, i + 1, i] for i in range(1, k - 1)]
    return v, np.array(f)


def rotor_positions(frame: str, n: int, a: float) -> list[tuple[float, float]]:
    if frame == "quad_x" and n == 4:
        return [(a, -a), (-a, a), (a, a), (-a, -a)]
    r = a * math.sqrt(2.0)
    return [(r * math.cos(math.radians(30 + 360 * i / n)), r * math.sin(math.radians(30 + 360 * i / n))) for i in range(n)]


def build(params: dict, *, small: bool) -> tuple[np.ndarray, np.ndarray]:
    geo = params["geometry"]
    a = float(val(geo["arm_xy_m"]))
    rz = float(val(geo.get("rotor_z_m", {"value": 0.12})))
    d = float(val(params["prop"]["d_m"]))
    n = int(params["motor"]["n"])
    frame = params["frame"]["type"]
    body_half = (a * 0.45, a * 0.35, 0.05)
    parts = [_box((0.0, 0.0, 0.08), body_half)]
    for x, y in rotor_positions(frame, n, a):
        parts.append(_arm((0.0, 0.0, 0.1), (x, y, rz - 0.03), 0.03, 0.025))
        if not small:
            parts.append(_prism((x, y, rz - 0.02), 0.03, 0.05, 6))
        parts.append(_disc((x, y, rz + 0.01), d / 2, 8 if small else 16, double=not small))
    vs, fs, off = [], [], 0
    for v, f in parts:
        vs.append(v)
        fs.append(f + off)
        off += len(v)
    return np.vstack(vs), np.vstack(fs)


def write_glb(params: dict, out: Path, *, as_hero: bool = False) -> dict[str, int]:
    import trimesh

    meshes = {}
    for name, small in (("lowpoly", False), ("lowpoly_s", True)):
        v, f = build(params, small=small)
        m = trimesh.Trimesh(vertices=v @ GLTF_FROM_FLU.T, faces=f, process=False)
        rgba = np.array([0.38, 0.40, 0.44, 1.0]) * 255
        m.visual = trimesh.visual.ColorVisuals(m, face_colors=np.tile(rgba, (len(f), 1)))
        meshes["hero" if as_hero and not small else name] = m
    out.parent.mkdir(parents=True, exist_ok=True)
    trimesh.Scene(meshes).export(out)
    tris = glb_triangles(out)
    lim = {"lowpoly": MAX_LOW, "lowpoly_s": MAX_LOW_S, "hero": MAX_LOW}
    for k, t in tris.items():
        if t > lim.get(k, MAX_LOW):
            raise SystemExit(f"{k}: {t} 三角形超过上限 {lim.get(k)}")
    return tris


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vehicle", default=str(ROOT / "vehicles" / "p600"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--name", default=None, help="输出文件名（缺省取 model.yaml 的 lowpoly 路径或 <model>_lowpoly.glb）")
    ap.add_argument("--as-hero", action="store_true", help="源 STL 缺失时：同时写 hero 档（fallback lowpoly_only）")
    ap.add_argument("--copy-web", action="store_true")
    a = ap.parse_args(argv)
    vdir = Path(a.vehicle)
    params = load_params(vdir)
    out_dir = Path(a.out) if a.out else vdir / "model"
    name = a.name
    my = vdir / "model" / "model.yaml"
    if name is None and my.exists():
        model = yaml.safe_load(my.read_text(encoding="utf-8"))
        name = Path(next(o for o in model["outputs"] if o["name"] == "lowpoly")["path"]).name
    name = name or f"{params['id'].split('_')[0]}_lowpoly.glb"
    out = out_dir / name
    tris = write_glb(params, out)
    print(f"lowpoly {out} triangles={tris}")
    outs = [out]
    if a.as_hero and my.exists():
        model = yaml.safe_load(my.read_text(encoding="utf-8"))
        hero = out_dir / Path(next(o for o in model["outputs"] if o["name"] == "hero")["path"]).name
        write_glb(params, hero, as_hero=True)
        outs.append(hero)
        print(f"hero (fallback lowpoly_only) {hero}")
    if a.copy_web:
        WEB_MODELS.mkdir(parents=True, exist_ok=True)
        for o in outs:
            shutil.copyfile(o, WEB_MODELS / o.name)
            print(f"copied {WEB_MODELS / o.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
