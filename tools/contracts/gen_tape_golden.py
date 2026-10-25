"""Canonical Event Tape golden（M23 §6.7、§6.8、§6.17；前端磁带解码器 `apps/web/src/engine/actors/tape/` 对拍）。

写出 packages/contracts/golden/event/tape-small.{bin,json}：4 名演员（2 脚本、2 背景）、250 tick（3 个块，末块不满），覆盖
负坐标、跨 ±180° 的航向、触发 i16/i32 回绕的大差分、可见性与容器变化。json 含哈希、全部记录（按字段，tick 主序）与
`actor_states` 在若干时刻（tick 节点、tick 之间、状态阶跃、t < 0、末帧之后）的 Python 参考值。

同时写出 `walkable_height` golden（D3-AC-07；M22-FR-046、M22-AC-015；前端 `walkable.ts` 对拍）walkable-small.{bin,json}：bin 为
`awr-trimesh@1` 的 `xyz.f32` 与 `tri.u32` 两条流首尾相接（16 m 瓦片，由 M22 `write_raycast` 分瓦片），json 含瓦片表与查询的
Python 参考值（z 为 float64、z32 为 float32 后的值、prim 为全局三角形号；"NaN"、"Infinity" 以字符串书写）。网格覆盖倾斜地面、
桥面与坡道（两层表面）、带竖直墙与夹层的建筑、重复三角形（同高决胜）、细长三角形与远离原点的大坐标片；查询覆盖顶点、
共边、瓦片与格边界、起点恰好等于表面高度、无导航面与界外。

用法：`.venv-d3/bin/python tools/contracts/gen_tape_golden.py [--only tape|walkable]`（需要 zstandard；重新生成后 tape-small.bin
的压缩字节可能随 zstandard 版本变化，内容哈希不变）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from awr.sim.events import tape as tp  # noqa: E402
from awr.world.glb.derive.raycast import write_raycast  # noqa: E402
from awr.world.mesh.query import WALK_ABOVE_M, WALK_CELL_M, WalkIndex  # noqa: E402

OUT = ROOT / "packages" / "contracts" / "golden" / "event"
N_T, N_S = 250, 2
H64 = "0" * 64


def frames() -> np.ndarray:
    f = np.zeros((N_T, 4), tp.RECORD)
    k = np.arange(N_T)
    f["container"] = tp.NO_CONTAINER
    # 0 person：t 0.5 s 出现，以 1.2 m/s 西南行；航向越过 ±180°；t 15 s 上车（容器 = 2），20 s 起离场
    f["state"][5:150, 0], f["state"][150:200, 0], f["state"][200:, 0] = 1, 3, 5
    f["x_mm"][:, 0] = -12000 - np.clip(k - 5, 0, 145) * 85
    f["y_mm"][:, 0] = 3000 - np.clip(k - 5, 0, 145) * 85
    f["z_mm"][:, 0] = 1250
    f["yaw_cdeg"][:, 0] = tp.fold_yaw(17500 + k * 40)
    f["vx_cmps"][5:150, 0], f["vy_cmps"][5:150, 0] = -85, -85
    f["container"][150:200, 0] = 2
    # 1 vehicle：大坐标（差分接近 i32 回绕）、等待与移动交替
    f["x_mm"][:, 1] = 2_000_000_000 - k * 1100
    f["y_mm"][:, 1] = -2_100_000_000 + k * 700
    f["state"][:, 1] = np.where((k // 40) % 2 == 0, 2, 1)
    f["vx_cmps"][:, 1] = np.where(f["state"][:, 1] == 1, -1100, 0)
    f["vy_cmps"][:, 1] = np.where(f["state"][:, 1] == 1, 700, 0)
    f["vz_cmps"][:, 1] = np.where(k % 7 == 0, -3, 2)
    f["yaw_cdeg"][:, 1] = tp.fold_yaw(-17950 - k * 13)
    # 2、3 background：速度跨 i16 两端（差分回绕），进出建筑
    f["state"][:, 2] = np.where(k < 100, 1, np.where(k < 180, 4, 1))
    f["x_mm"][:, 2] = (k * 37) % 5000 - 2500
    f["vx_cmps"][:, 2] = np.where(k % 2 == 0, 32000, -32000)
    f["state"][:, 3] = 0
    f["state"][60:, 3] = 2
    f["x_mm"][:, 3], f["y_mm"][:, 3], f["z_mm"][:, 3] = 15, -15, -7
    return f


def header(f: np.ndarray) -> dict:
    actors = [{"idx": 0, "id": "person_A", "class": "person", "scripted": True, "profile": "person", "size_mm": [500, 300, 1760],
               "appearance": {"top_rgb": [42, 93, 176]}},
              {"idx": 1, "id": "vehicle_C", "class": "vehicle", "scripted": True, "profile": "vehicle", "size_mm": [4500, 1800, 1500],
               "appearance": {}},
              {"idx": 2, "id": "bg0_0", "class": "person", "scripted": False, "profile": "person", "size_mm": [500, 300, 1700],
               "appearance": {}},
              {"idx": 3, "id": "bg0_1", "class": "person", "scripted": False, "profile": "person", "size_mm": [500, 300, 1650],
               "appearance": {}}]
    return {"schema": "drone4d.tape.v1", "event_id": "golden-small", "event_version": 1, "event_sha256": H64, "mode": "exogenous", "seed": 3,
            "tick_hz": 10, "n_ticks": N_T, "duration_ms": N_T * 100, "record": tp.record_spec(),
            "codec": {"name": "col-delta-zstd@1", "block_ticks": 100}, "actors": actors,
            "world": {"id": "synth", "content_version": "golden0", "raycast_sha256": H64, "navmesh_sha256": {"person": H64},
                      "anchors_sha256": H64},
            "environment": {}, "env_track": [], "comms": {}, "anim": None, "realized": None}


def write_tape_golden() -> None:
    f = frames()
    h = tp.write_tape(OUT / "tape-small.bin", header(f), f, n_scripted=N_S)
    r = tp.TapeReader(OUT / "tape-small.bin")
    F = np.concatenate([r.block_frames(i) for i in range(len(r.blocks))])
    samples = []
    for t in (-5, 0, 50_000_000, 512_345_678, 1_000_000_000, 14_950_000_000, 15_000_000_001, 3_999_999_999, 4_037_500_000,
              19_999_000_000, 24_900_000_000, 30_000_000_000):
        for ids in (None, [0, 1]):
            st = r.actor_states(t, ids)
            samples.append({"t_ns": t, "ids": ids, "pos_enu_m": st.pos_enu_m.tolist(), "yaw_rad": st.yaw_rad.tolist(),
                            "vel_enu_mps": st.vel_enu_mps.tolist(), "state": st.state.tolist(), "container": st.container.tolist()})
    doc = {"schema": "awr.golden.tape.v1", "file": "tape-small.bin", "n_ticks": N_T, "n_actors": 4, "n_scripted": N_S,
           "hashes": {k: h[k] for k in ("content_sha256", "body_sha256", "header_sha256", "file_sha256")},
           "records": {name: F[name].T.tolist() for name, _ in tp.FIELDS}, "actor_states": samples}
    (OUT / "tape-small.json").write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"tape-small.bin {h['bytes']} B, content {h['content_sha256'][:12]}")


# ---------------------------------------------------------------------- walkable_height golden
WALK_TILE_M = 16.0
SLAB_Z, ROOF_Z, FLOOR_Z = 3.2, 6.25, 3.0
FAR = (1_234_567.25, -765_432.5, 812.375)


def _quad(a, b, c, d) -> tuple[list, list]:
    return [a, b, c, d], [[0, 1, 2], [0, 2, 3]]


def walk_mesh() -> tuple[np.ndarray, np.ndarray]:
    """世界帧 V (n, 3) float64 与 F (m, 3)。"""
    parts: list[tuple[list, list]] = []
    gx, gy = np.arange(-20.0, 28.0 + 1e-9, 4.0), np.arange(-20.0, 20.0 + 1e-9, 4.0)

    def gz(x: float, y: float) -> float:
        return 0.031 * x - 0.017 * y + 0.4 * np.sin(0.7 * x) * np.cos(0.5 * y)

    for j in range(len(gy) - 1):                                   # 倾斜起伏地面，对角线棋盘交替
        for i in range(len(gx) - 1):
            p = [[gx[i], gy[j]], [gx[i + 1], gy[j]], [gx[i + 1], gy[j + 1]], [gx[i], gy[j + 1]]]
            p = [[x, y, gz(x, y)] for x, y in p]
            parts.append((p, [[0, 1, 2], [0, 2, 3]] if (i + j) % 2 == 0 else [[0, 1, 3], [1, 2, 3]]))
    parts.append(_quad([-6, -3, SLAB_Z], [6.5, -3, SLAB_Z], [6.5, 3, SLAB_Z], [-6, 3, SLAB_Z]))                    # 桥面
    parts.append(_quad([6.5, -3, SLAB_Z], [14, -3, 0.9], [14, 3, 0.9], [6.5, 3, SLAB_Z]))                          # 坡道
    x0, x1, y0, y1 = 16.0, 22.0, 8.0, 14.0                                                                       # 建筑
    parts.append(_quad([x0, y0, ROOF_Z], [x1, y0, ROOF_Z], [x1, y1, ROOF_Z], [x0, y1, ROOF_Z]))
    parts.append(_quad([x0, y0, FLOOR_Z], [x1, y0, FLOOR_Z], [x1, y1, FLOOR_Z], [x0, y1, FLOOR_Z]))
    for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):        # 竖直墙
        parts.append(_quad([a[0], a[1], 0.0], [b[0], b[1], 0.0], [b[0], b[1], ROOF_Z], [a[0], a[1], ROOF_Z]))
    g0 = parts[0][0]
    parts.append(([g0[0], g0[1], g0[2]], [[0, 1, 2]]))                                                           # 重复三角形
    parts.append(([[-18.0, 10.0, 1.5], [-2.0, 10.0000001, 1.7], [-10.0, 10.00000015, 2.1]], [[0, 1, 2]]))         # 细长三角形
    parts.append(([[-18.0, 12.0, 1.0], [-2.0, 12.0, 1.0], [-10.0, 12.0 + 3e-12, 1.0]], [[0, 1, 2]]))              # 投影退化
    fx, fy, fz = FAR
    parts.append(_quad([fx, fy, fz], [fx + 9.5, fy, fz + 0.3], [fx + 9.5, fy + 7.25, fz + 0.55], [fx, fy + 7.25, fz + 0.1]))
    V, F, off = [], [], 0
    for p, f in parts:
        V.extend(p)
        F.extend([[off + i for i in t] for t in f])
        off += len(p)
    return np.asarray(V, np.float64), np.asarray(F, np.int64)


def walk_queries(rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    pts, nav = [], []

    def add(x: float, y: float, z: float) -> None:
        pts.append([x, y])
        nav.append(z)

    for x in np.arange(-20.0, 28.0 + 1e-9, 4.0):                   # 地面顶点（瓦片与格边界）与共边中点
        for y in np.arange(-20.0, 20.0 + 1e-9, 4.0):
            add(x, y, 0.0)
            add(x + 2.0, y, 0.0)
            add(x, y + 2.0, -1.5)
    for x, y in ((0.0, 0.0), (-6.0, 0.0), (6.5, 3.0), (-6.0, -3.0), (2.25, 1.75), (10.0, 0.0), (13.999, -2.5)):
        add(x, y, 0.0)                                             # 桥下：起点 2 m 低于桥面，取地面
        add(x, y, SLAB_Z)                                          # 桥上
    s32 = float(np.float32(SLAB_Z))
    add(1.0, 1.0, s32 - WALK_ABOVE_M)                              # 起点等于桥面的 float32 高度（插值高度可能高出数 ulp）
    for x, y in ((19.0, 11.0), (16.0, 8.0), (22.0, 11.0), (17.5, 14.0)):
        add(x, y, 0.0)                                             # 室内地面
        add(x, y, FLOOR_Z)                                         # 夹层
        add(x, y, ROOF_Z)                                          # 屋顶
        add(x, y, float("inf"))
    for x, y in ((-10.0, 10.00000005), (-17.9, 10.0), (-10.0, 12.0), (-20.0, -20.0), (28.0, 20.0)):
        add(x, y, 1.0)
    fx, fy, fz = FAR
    for x, y in ((fx, fy), (fx + 4.75, fy + 3.625), (fx + 9.5, fy + 7.25), (fx + 2.0, fy + 7.0)):
        add(x, y, fz)
    add(0.0, 0.0, float("nan"))                                    # 无导航面
    add(100.0, 0.0, 0.0)                                           # 界外
    add(-20.000001, 0.0, 0.0)
    xy = np.c_[rng.uniform(-22.0, 30.0, 600), rng.uniform(-22.0, 22.0, 600)]
    for (x, y), z in zip(xy, rng.uniform(-2.0, 8.0, 600), strict=True):
        add(float(x), float(y), float(z))
    for x, y in np.c_[rng.uniform(-21.0, 29.0, 150), rng.uniform(-21.0, 21.0, 150)]:
        add(float(x), float(y), float("inf"))                      # 无导航面函数的过渡口径：最高表面
    for x, y in np.c_[rng.uniform(FAR[0], FAR[0] + 9.5, 50), rng.uniform(FAR[1], FAR[1] + 7.25, 50)]:
        add(float(x), float(y), FAR[2] + float(rng.uniform(-3.0, 1.0)))
    return np.asarray(pts, np.float64), np.asarray(nav, np.float64)


def _raw(v: float) -> float | str:
    return "NaN" if np.isnan(v) else ("Infinity" if v == np.inf else float(v))


def write_walkable_golden() -> None:
    V, F = walk_mesh()
    with tempfile.TemporaryDirectory() as td:
        idx = write_raycast(Path(td), "golden-walkable", "0" * 64, V, F, tile_m=WALK_TILE_M)
        d = Path(td) / "geometry" / "mesh" / "raycast"
        xyz, tri = (d / "xyz.f32").read_bytes(), (d / "tri.u32").read_bytes()
    (OUT / "walkable-small.bin").write_bytes(xyz + tri)
    tiles = [{k: t[k] for k in ("id", "origin_m", "v0", "nv", "t0", "nt")} for t in idx["tiles"]]
    w = WalkIndex.build(np.frombuffer(xyz, "<f4"), np.frombuffer(tri, "<u4"), tiles)
    xy, nav = walk_queries(np.random.default_rng(20261018))
    zs = float(w.surface(np.array([[1.0, 1.0]]), np.array([np.inf]))[0][0])
    xy = np.r_[xy, [[1.0, 1.0], [1.0, 1.0]]]                       # 起点恰好等于桥面插值高度（≤ 取等）与低一个 ulp
    nav = np.r_[nav, zs - WALK_ABOVE_M, np.nextafter(zs - WALK_ABOVE_M, -np.inf)]
    z, prim = w.surface(xy, nav + WALK_ABOVE_M)
    doc = {"schema": "awr.golden.walkable.v1", "file": "walkable-small.bin", "above_m": WALK_ABOVE_M, "cell_m": WALK_CELL_M,
           "counts": idx["counts"], "xyz_bytes": len(xyz), "tri_bytes": len(tri), "tiles": tiles,
           "queries": {"x": xy[:, 0].tolist(), "y": xy[:, 1].tolist(), "nav_z": [_raw(v) for v in nav],
                       "z": [_raw(v) for v in z], "z32": [_raw(v) for v in z.astype(np.float32).astype(np.float64)],
                       "prim": prim.tolist()}}
    (OUT / "walkable-small.json").write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
    hit = int(np.isfinite(z).sum())
    print(f"walkable-small.bin {len(xyz) + len(tri)} B, {idx['counts']['triangles']} triangles / {len(tiles)} tiles, "
          f"{len(z)} queries ({hit} hits)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=("tape", "walkable"))
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.only in (None, "tape"):
        write_tape_golden()
    if a.only in (None, "walkable"):
        write_walkable_golden()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
