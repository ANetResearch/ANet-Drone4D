"""M26 随机流（rt/rng_streams.json 扩展点 4）：流 20 链路、流 21 L1 检测、流 22 导航解（M25 GNSS 误差同源，P0a 由本模块实现）。

键沿用 `awr.sim.sensors.cbrng` 的五级派生 `(seed, stream, agent, tick, ch)`；M26 的六元键压入其中：
L1 `(seed, 21, uav_no·65536 + actor_no, seq, ch)`，链路 `(seed, 20, src_no·256 + dst_no, msg_seq, ch)`，
导航 `(seed, 22, uav_no, sample_index, ch)`。结果只取决于键，与调用次序无关。
"""

from __future__ import annotations

import numpy as np

from awr.sim.sensors import cbrng

__all__ = ["CH", "STREAM_L1", "STREAM_LINK", "STREAM_NAV", "hex64", "nrm", "u01"]

STREAM_LINK = 20
STREAM_L1 = 21
STREAM_NAV = 22

# 通道（每流内不重叠）
CH = {
    "l1_det": 0, "l1_gx": 1, "l1_gy": 2, "l1_range": 3, "l1_detid": 4, "l1_app": 8,          # l1_app 8..13：外观 6 个分量
    "link_loss": 0, "link_lat": 1,
    "nav_gm": 0, "nav_vel": 3,                                                               # nav_gm 0..2、nav_vel 3..5
}


def u01(seed: int, stream: int, agent: int, tick: int, ch: int) -> float:
    return float(cbrng.uniform_elem(seed, stream, agent, tick, ch))


def nrm(seed: int, stream: int, agent: int, tick: int, ch: int) -> float:
    return float(cbrng.normal_elem(seed, stream, agent, tick, ch))


def hex64(seed: int, stream: int, agent: int, tick: int, ch: int) -> str:
    return f"{int(cbrng.key(seed, stream, np.uint64(agent), np.uint64(tick), np.uint64(ch))):016x}"
