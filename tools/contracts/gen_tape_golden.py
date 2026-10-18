"""Canonical Event Tape golden（M23 §6.7、§6.8、§6.17；前端磁带解码器 `apps/web/src/engine/actors/tape/` 对拍）。

写出 packages/contracts/golden/event/tape-small.{bin,json}：4 名演员（2 脚本、2 背景）、250 tick（3 个块，末块不满），覆盖
负坐标、跨 ±180° 的航向、触发 i16/i32 回绕的大差分、可见性与容器变化。json 含哈希、全部记录（按字段，tick 主序）与
`actor_states` 在若干时刻（tick 节点、tick 之间、状态阶跃、t < 0、末帧之后）的 Python 参考值。

用法：`.venv-d3/bin/python tools/contracts/gen_tape_golden.py`（需要 zstandard；重新生成后 tape-small.bin 的压缩字节可能随
zstandard 版本变化，内容哈希不变）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from awr.sim.events import tape as tp  # noqa: E402

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


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
