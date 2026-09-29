"""六城事实常量（M16 §6.2.2–§6.2.4、§6.4.8；定义方为 AWR-16 §7、§10.2、§10.3，数值冲突时以 16 为准）。

这些常量是"演示数据正确"的预言值：`tests/e2e/test_builtin_worlds.py` 用它们核对已构建的 World Package，
`awr.datasets.scenarios.authoring` 用出生地块与 curated 区域生成剧本与 zones 文件。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["CITIES", "CITY_IDS", "CityFacts", "ZoneDef", "city"]


@dataclass(frozen=True)
class ZoneDef:
    """curated 区域：32 边形近似圆（M16 §6.2.4）。"""

    zone_id: str
    kind: str                      # nofly | restricted
    center: tuple[float, float]    # world ENU，m
    radius_m: float
    label: str
    label_zh: str


@dataclass(frozen=True)
class CityFacts:
    world_id: str
    name_zh: str
    ply_name: str
    units_to_m: float
    up_axis: str
    leveled_deg: float
    yaw_deg: float
    true_north: str                            # assumed | verified | unknown
    anchor_lat_lon: tuple[float, float]
    extent_enu_m: tuple[float, float, float]   # E × N × U（16 §10.3）
    max_world_z_m: float                       # coordinate.extent.max[2]
    border_max_z_m: float                      # 16 §7：round(max(dsm) + 50, 2)
    nn_median_m: float
    default_color: str                         # height | hag
    roots: int
    depth: int
    synthetic_ground_z: float | None
    free_pad: tuple[float, float]              # free 剧本出生地块中心（M16 §6.4.8 表）
    free_pad_z: float | None                   # 苏州无点区显式 z = 0
    default_scenario: str
    demo_scenarios: tuple[str, ...]
    zones: tuple[ZoneDef, ...] = field(default_factory=tuple)
    ui_labels: tuple[str, ...] = ("示意坐标",)  # UI 与报告必须出现的诚实标识（M16 §6.2.2）


CITIES: dict[str, CityFacts] = {
    "shenzhen": CityFacts(
        "shenzhen", "深圳", "Shenzhen_sampled_5m.ply", 1.0, "+z", 0.0, 0.0, "assumed", (22.5160584, 113.9432472),
        (1848.0, 1999.0, 391.0), 374.05, 424.05, 0.526, "height", 1, 5, None, (-35.0, -94.0), None,
        "s1-shenzhen-facade", ("s1-shenzhen-facade", "ladder-shenzhen", "free-shenzhen"),
        (ZoneDef("nofly-sz-t2", "nofly", (-98.0, 346.5), 60.0, "No-fly: tower T2", "禁飞区：T2 塔"),
         ZoneDef("restricted-sz-t3", "restricted", (110.0, 162.5), 50.0, "Restricted: tower T3", "限制区：T3 塔")),
        ("示意坐标", "北向未验证")),
    "shanghai": CityFacts(
        "shanghai", "上海", "shanghai_sampled_5m.ply", 1.0, "+z", 0.0, 0.0, "verified", (31.2281892, 121.5316942),
        (7736.0, 6211.0, 645.0), 636.73, 686.73, 1.845, "height", 1, 5, None, (-781.0, -830.0), None,
        "free-shanghai", ("s2-shanghai-formation",),
        (ZoneDef("nofly-sh-pearl", "nofly", (-3427.2, 1547.6), 100.0, "No-fly: Oriental Pearl", "禁飞区：东方明珠"),)),
    "newyork": CityFacts(
        "newyork", "纽约", "New York_sampled_5m.ply", 1.0, "+z", 0.0, 0.0, "verified", (40.7130611, -74.0023445),
        (2928.0, 3167.0, 292.0), 287.00, 337.00, 1.016, "height", 1, 5, None, (160.0, 0.0), None,
        "free-newyork", ("s3-newyork-sar",),
        (ZoneDef("nofly-ny-70pine", "nofly", (-434.1, -741.3), 60.0, "No-fly: 70 Pine Street", "禁飞区：70 Pine"),)),
    "sanfrancisco": CityFacts(
        "sanfrancisco", "旧金山", "San Francisco_sampled_5m.ply", 10.15, "+z", 0.0, 90.0, "verified",
        (37.7791216, -122.4212116), (7278.0, 7512.0, 558.0), 443.20, 493.20, 1.887, "hag", 1, 5, None,
        (239.0, 20.0), None, "free-sanfrancisco", ("s5-sanfrancisco-terrain",),
        (ZoneDef("nofly-sf-sutro", "nofly", (-2795.9, -2612.5), 120.0, "No-fly: Sutro Tower", "禁飞区：Sutro 塔"),)),
    "suzhou": CityFacts(
        "suzhou", "苏州", "Suzhou_sampled_5m.ply", 1.0, "+y", 0.0, 0.0, "unknown", (31.30, 120.62),
        (4407.0, 686.0, 155.0), 152.37, 202.37, 0.348, "height", 6, 4, 0.0, (0.0, 300.0), 0.0,
        "free-suzhou", ("s6-suzhou-corridor",), (), ("示意坐标", "北向未知", "合成地面")),
    "chicago": CityFacts(
        "chicago", "芝加哥", "Chicago_sampled_5m.ply", 1000.0, "+z", 2.025, 0.0, "verified", (41.8841302, -87.6225307),
        (4176.0, 8038.0, 451.0), 445.24, 495.24, 1.587, "height", 1, 6, None, (0.0, 0.0), None,
        "free-chicago", ("s4-chicago-lakeshore",),
        (ZoneDef("nofly-chi-hancock", "nofly", (-15.0, 1636.0), 80.0, "No-fly: John Hancock Center", "禁飞区：汉考克中心"),)),
}

CITY_IDS: tuple[str, ...] = ("shenzhen", "shanghai", "newyork", "sanfrancisco", "suzhou", "chicago")
GATE_CITY = "shenzhen"


def city(world_id: str) -> CityFacts:
    try:
        return CITIES[world_id]
    except KeyError:
        raise KeyError(f"unknown built-in world {world_id!r}; built-in: {', '.join(CITY_IDS)}") from None
