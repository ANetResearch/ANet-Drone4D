#!/usr/bin/env python3
"""Geometry 一致性：glb 包围盒对源 STL（换到 glTF 轴后）比较，误差 ≤ 2 cm（ADR-043 Geometry 行；M08 §6.7.2）。
用法：python tools/vehicles/check_geometry.py [--vehicle vehicles/p600] [--tol-m 0.02]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import GLTF_FROM_FLU, ROOT


def bbox_err(vdir: Path) -> dict:
    import trimesh

    model = yaml.safe_load((vdir / "model" / "model.yaml").read_text(encoding="utf-8"))
    hero = vdir / "model" / next(o for o in model["outputs"] if o["name"] == "hero")["path"]
    g = trimesh.load(hero, force="scene")
    gb = np.array(g.bounds)
    src = model.get("source")
    if not src or not (ROOT / src["path"]).exists():
        return {"status": "no_source", "glb_bounds": gb.round(4).tolist()}
    m = trimesh.load(ROOT / src["path"], force="mesh")
    v = np.asarray(m.vertices) @ GLTF_FROM_FLU.T
    sb = np.array([v.min(0), v.max(0)])
    err = float(np.abs(gb - sb).max())
    return {"status": "ok", "max_err_m": round(err, 4), "glb_bounds": gb.round(4).tolist(), "stl_bounds": sb.round(4).tolist()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vehicle", default=str(ROOT / "vehicles" / "p600"))
    ap.add_argument("--tol-m", type=float, default=0.02)
    a = ap.parse_args(argv)
    r = bbox_err(Path(a.vehicle))
    print(json.dumps(r))
    return 0 if r["status"] != "ok" or r["max_err_m"] <= a.tol_m else 1


if __name__ == "__main__":
    sys.exit(main())
