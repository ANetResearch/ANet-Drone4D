"""样张渲染（M25 §6.4；P0a-1 自检与公开站预渲染样例的原始素材）：对世界包按预设或给定视点渲染 RGB（JPEG）、深度（16 位 PNG，厘米）
与计时摘要。用法：`python -m awr.sensorsvc.camera.snap <world_dir> --out DIR [--size 1280x720] [--hfov 84] [--view x,y,z,yaw,pitch ...]`。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

from awr.sensorsvc.camera.model import CameraModel
from awr.sensorsvc.camera.proj import gimbal_R, look_at_R
from awr.sensorsvc.camera.scene import load_appearance


def ground_z(world_dir: Path):
    """DSM 2 m 栅格最近格查询（无栅格或越界时为 0）：视点高度按离地高度给出。"""
    g = world_dir / "geometry/terrain/dsm_2m.json"
    if not g.exists():
        return lambda x, y: 0.0
    m = json.loads(g.read_text(encoding="utf-8"))
    a = np.fromfile(g.parent / m["href"], "<f4").reshape(m["height"], m["width"])        # 行序 south-to-north
    ox, oy, c = m["originXY"][0], m["originXY"][1], m["cellM"]

    def z(x: float, y: float) -> float:
        i, j = int((y - oy) // c), int((x - ox) // c)
        if 0 <= i < a.shape[0] and 0 <= j < a.shape[1]:
            win = a[max(i - 2, 0):i + 3, max(j - 2, 0):j + 3]
            return float(np.nanmax(win))
        return 0.0
    return z


def preset_views(world_dir: Path) -> list[dict]:
    """正下视离地 250 m、四个方向离地 150 m 斜视（看向原点地面）、一个离地 40 m 低空前视。"""
    c = json.loads((world_dir / "coordinate.json").read_text(encoding="utf-8"))
    lo, hi = np.asarray(c["extent"]["min"]), np.asarray(c["extent"]["max"])
    r = 0.3 * float(min(hi[0] - lo[0], hi[1] - lo[1]))
    gz = ground_z(world_dir)
    tgt = [0.0, 0.0, gz(0.0, 0.0)]
    views = [{"name": "nadir-250m", "p": [0.0, 0.0, gz(0.0, 0.0) + 250.0], "target": [0.0, 0.0, gz(0.0, 0.0)]}]
    for k, name in enumerate(("east", "north", "west", "south")):
        a = k * math.pi / 2
        x, y = r * math.cos(a), r * math.sin(a)
        views.append({"name": f"oblique-{name}-150m", "p": [x, y, gz(x, y) + 150.0], "target": tgt})
    x, y = -0.5 * r, -0.2 * r
    views.append({"name": "low-40m", "p": [x, y, gz(x, y) + 40.0], "yaw": 20.0, "pitch": -12.0})
    return views


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.sensorsvc.camera.snap")
    ap.add_argument("world_dir")
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--hfov", type=float, default=84.0)
    ap.add_argument("--view", action="append", default=[], help="x,y,z,yaw_deg,pitch_deg (ENU m, deg)")
    ap.add_argument("--repeat", type=int, default=5, help="timed renders per view after the first")
    a = ap.parse_args(argv)
    from PIL import Image

    from awr.sensorsvc.camera.raster import RENDERER_VERSION, RasterCamera

    wd, out = Path(a.world_dir), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    w, h = (int(v) for v in a.size.split("x"))
    cm = CameraModel.from_hfov(w, h, a.hfov)
    t0 = time.perf_counter()
    scene = load_appearance(wd)
    t_load = time.perf_counter() - t0
    cam = RasterCamera(scene)
    views = [{"name": f"view{i}", "p": v[:3], "yaw": v[3], "pitch": v[4]}
             for i, v in enumerate([[float(x) for x in s.split(",")] for s in a.view])] or preset_views(wd)
    rows = []
    for v in views:
        R = look_at_R(np.asarray(v["p"]), np.asarray(v["target"])) if "target" in v else gimbal_R(v["yaw"], v["pitch"])
        r = cam.render(cm, R, np.asarray(v["p"]))
        ms = []
        q = r
        for _ in range(a.repeat):
            q = cam.render(cm, R, np.asarray(v["p"]))
            ms.append(q.draw_ms + q.readback_ms)
        Image.fromarray(r.rgb8).save(out / f"{v['name']}.jpg", quality=90)
        Image.fromarray(np.minimum(r.depth_m * 100.0, 65535).astype(np.uint16)).save(out / f"{v['name']}_depth_cm.png")
        valid = r.depth_m > 0
        rows.append({"name": v["name"], "p": [float(x) for x in v["p"]], "batches": r.batches_drawn, "ms_p50": float(np.median(ms)) if ms else None,
                     "first_ms": r.draw_ms + r.readback_ms, "draw_ms": q.draw_ms, "readback_ms": q.readback_ms, "geometry_frac": float(valid.mean()),
                     "depth_p50_m": float(np.median(r.depth_m[valid])) if valid.any() else None})
    summary = {"renderer": RENDERER_VERSION, "gl": cam.info, "world": scene.world_id, "size": [w, h], "hfov_deg": a.hfov,
               "triangles": scene.triangles, "batches": len(scene.batches), "load_s": round(t_load, 2), "texture_s": round(cam.texture_s, 2),
               "upload_s": round(cam.upload_s, 2), "views": rows}
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=1))
    cam.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
