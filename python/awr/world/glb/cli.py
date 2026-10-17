"""M22 CLI（§7.7）：`inspect`、`register`；`build`、`validate`、`status`、`anchors`、`summary` 等子命令随各阶段加入。
退出码沿用 M03（`WorldpkgError.exit_code`：1 校验、2 门禁、3 配置与 IO、4 原始数据）。"""

from __future__ import annotations

import argparse
import json
import sys

from awr.world.ingest.types import WorldpkgError


def _inspect(a: argparse.Namespace) -> int:
    from awr.world.glb.inspect import inspect_glb

    rep = inspect_glb(a.file, geometry=not a.fast)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0
    g = rep.get("geometry", {})
    print(f"{rep['file']['name']}: {rep['file']['bytes']} B, nodes {rep['nodes']['flattened']} (mesh {rep['nodes']['mesh_nodes']}, "
          f"mirrored {len(rep['nodes']['mirrored'])})")
    if g:
        print(f"  triangles {g['triangles']}, vertices {g['vertices']}, primitives {g['primitives']}")
        print(f"  up-axis vote {rep['up_axis_vote']}")
    t = rep["textures"]
    print(f"  textures {t['count']} {t['sizes']}, texels {t['texels']}, extensions {rep['extensions']['used']}")
    return 0


def _register(a: argparse.Namespace) -> int:
    from awr.world.glb import register as R
    from awr.world.glb.recipe import load_recipe

    rec = load_recipe(a.world_id)
    ev = list(a.evidence or [])
    if a.frame_of:
        doc = R.register_frame_of(rec, a.frame_of, h_msl_m=a.h_msl, evidence=ev)
    elif a.method == "manual":
        need = {"--scale": a.scale, "--yaw": a.yaw, "--t-osm": a.t_osm, "--osm-ref": a.osm_ref}
        missing = [k for k, v in need.items() if v is None]
        if missing:
            print(f"error: --method manual needs {', '.join(missing)}", file=sys.stderr)
            return 3
        params = {"s": a.scale, "yaw_deg": a.yaw, "g": R.parse_pair(a.g or "0,0"), "t_osm_m": R.parse_pair(a.t_osm),
                  "osm_ref": R.parse_pair(a.osm_ref)}
        doc = R.register_manual(rec, params, h_msl_m=a.h_msl, evidence=ev)
    else:
        print(f"error: method {a.method} is not implemented yet (use --method manual or --frame-of)", file=sys.stderr)
        return 3
    p = R.write_registration(rec, doc, accept_by=a.accept)
    r = doc["result"]
    print(f"{p}: {doc['method']} {doc['status']}; s {r['m_per_unit']:.6f} m/u, yaw {r['yaw_deg']:.4f} deg, "
          f"origin {r['anchor']['lat_deg']:.7f}, {r['anchor']['lon_deg']:.7f}; bounds {r['bounds_m']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.world.glb", description="GLB world import (M22)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inspect", help="GLB report (FR-004)")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fast", action="store_true", help="container, nodes and textures only")
    p.set_defaults(fn=_inspect)
    p = sub.add_parser("register", help="write configs/worlds.d3/<id>/registration.json (§7.6.2)")
    p.add_argument("world_id")
    p.add_argument("--method", default="manual", choices=["manual", "roads", "footprints"])
    p.add_argument("--frame-of", help="inherit the accepted frame of another world (inherit-frame)")
    p.add_argument("--scale", type=float, help="metres per glTF unit")
    p.add_argument("--yaw", type=float, help="yaw from the glTF frame to true north, degrees")
    p.add_argument("--g", help="levelling gradient gE,gN")
    p.add_argument("--t-osm", help="translation tE,tN into the OSM local ENU, metres")
    p.add_argument("--osm-ref", help="OSM local ENU origin lat,lon")
    p.add_argument("--h-msl", type=float, help="ground height above MSL at the world origin, metres (DEM)")
    p.add_argument("--evidence", action="append", help="evidence summary line (up to 8)")
    p.add_argument("--accept", metavar="BY", help="accept the record (status accepted, accepted_by BY)")
    p.set_defaults(fn=_register)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except WorldpkgError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
