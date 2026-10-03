"""合成演示城市 synthcity 的演示事实（DEMO-W；ADR-077；M16 §6.2.5、§6.4.9）。

世界本身由 M03 `awr.world.ingest.synthetic` 按固定种子生成（生成帧即 World ENU，`T_world_source` 为单位阵），因此这里的坐标
可以直接写进剧本。与六城的 `CityFacts` 同构，供 `authoring` 生成 `s0-synthcity-showcase`、`free-synthcity`、curated zones 与
catalog 条目，并供 `tests/world/test_synthcity.py` 核对已构建的世界。

几何依据（`awr.world.ingest.synthetic.LANDMARKS`）：ANet Tower 圆柱塔中心 (62, 122)，底半径 25 m、300 m 处 19 m，塔冠 318 m、
桅杆 352 m；阶梯退台塔中心 (−62, −2)，裙楼 24 m、塔顶 286 m；河道中心线 y = −415 + 14·sin(2π(x + 600)/640)；中央公园
x ∈ [−594, −374]、y ∈ [194, 406]，池塘中心 (−530, 240)。
"""

from __future__ import annotations

import math

from .urbanscene3d.cities import CityFacts, ZoneDef

__all__ = ["DEMO_WORLD_IDS", "SHOWCASE", "SYNTHCITY", "SYNTH_ID", "river_yc"]

SYNTH_ID = "synthcity"
DEMO_WORLD_IDS: tuple[str, ...] = (SYNTH_ID,)


def river_yc(x: float) -> float:
    """河道中心线（与 M03 生成器同一公式，1200 m 城市）。"""
    return -415.0 + 14.0 * math.sin(2 * math.pi * (x + 600.0) / 640.0)


SYNTHCITY = CityFacts(
    SYNTH_ID, "合成演示城市", "", 1.0, "+z", 0.0, 0.0, "exact", (30.0, 120.0),
    (1200.0, 1200.0, 355.1), 352.0, 402.0, 0.382, "height", 1, 5, None, (0.0, 60.0), None,
    "s0-synthcity-showcase", ("s0-synthcity-showcase", "free-synthcity"),
    (ZoneDef("nofly-sc-steptower", "nofly", (-62.0, -2.0), 55.0, "No-fly: step tower", "禁飞区：阶梯塔"),
     ZoneDef("restricted-sc-pond", "restricted", (-530.0, 240.0), 60.0, "Restricted: park pond", "限制区：公园池塘")),
    ("示意坐标",))

# s0 剧本的定稿参数（M16 §6.4.9）：出生点都在路口内（开阔、无行道树，行道树离路口中心 ≥ 18 m）
SHOWCASE = {
    "tower_center": (62.0, 122.0),
    # 航迹半径 58 m：M10 立面覆盖的柱面代理半径 = radius_m − standoff_m = 28 m，须大于塔底半径 25 m 加 2 m DSM 格对角（代理格心
    # 外 0.5 m 的视线终点不能落进含塔体的 DSM 格，否则 M04 视线判为遮挡）；实际立面距 33.8–35.4 m（塔身 z 40–120 m 处半径
    # 24.2–22.6 m）
    "helix_radius_m": 58.0,
    "helix_bands": {"upper": (120.0, 80.0), "lower": (80.0, 40.0)},
    "helix_dz_m": 16.0,                     # 30 m 立面距、VFOV 42.1° 时垂直重叠约 30%
    "helix_speed_mps": 9.0,
    "helix_homes": {"p600-h1": (-8.0, 60.0), "p600-h2": (8.0, 60.0)},          # 路口 (0, 60)
    "blocks_polygon": ((260.0, -165.0), (460.0, -165.0), (460.0, -75.0), (260.0, -75.0)),   # 两个中层街区（≤ 42 m）
    "blocks_homes": {"p600-c1": (352.0, -60.0), "p600-c2": (368.0, -60.0)},    # 路口 (360, −60)
    "formation_homes": {"p600-f1": (-488.0, -300.0), "p600-f2": (-472.0, -300.0), "p600-f3": (-480.0, -314.0)},  # 路口 (−480, −300)
    "formation_z_m": 40.0,
    "formation_speed_mps": 10.0,
    "formation_east_x": 140.0,              # 河道段东端（x −430 → 140，再沿北岸 y = −335 返回）
    "weather": (("clear", 0.0), ("lightRain", 45.0), ("fog", 150.0)),           # 晴 → 小雨 → 雾（at_s）
}
