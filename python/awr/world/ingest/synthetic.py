"""合成演示城市 synthcity（DEMO-W；ADR-077；M03-FR-066、FR-067）：确定性程序化生成约 1.2 km × 1.2 km 的城市点云。

用途：UrbanScene3D 禁止再分发，README 截图与"零下载试用"需要一座完全由本仓库程序生成的城市。本模块不读取任何外部数据，
全部点按固定种子生成，因此世界包与截图可自由再分发（仓库 LICENSE 覆盖）。`SyntheticAdapter` 把生成结果交给与六城相同的
ingest → grid → tile → derive → package → validate → publish 流水线（`awr.world.package.build.build_world`）。

城市内容：道路网格与路口（车道线、斑马线带来点密度纹理）、跨河桥梁；街区内的矩形、L 形、围合院落、板楼、塔楼、阶梯退台塔楼、
圆柱塔与坡屋顶低层；两座 300 m 级地标塔（圆柱 "ANet Tower" 塔冠 318 m、桅杆 352 m；阶梯退台塔 286 m）；立面窗格纹理
（窗框与窗间墙按全密度采样、玻璃按低接受率采样，点密度差即窗格纹理；玻璃不后退，避免 2 m 顶面格
边界落在玻璃与窗框之间时法线修正误翻立面，M03-FR-014）；屋顶设备（机组箱体、水箱）与女儿墙；行道树（球冠点簇与树干）、
中央公园（草坪、小丘、池塘、园路、树丛与灌木）、滨河绿带与河道。类别按 `anet-classes@1` 紧凑索引直接给出（地面 1、低矮植被 2、
中等植被 3、高植被 4、屋顶 5、立面 6、水面 8、路面 9、桥面 10），不经规则分类（`IngestConfig.semantic = provided`）。

生成帧即 World ENU（x 东、y 北、z 上，米）：城市范围 [−H, H]²（H = size_m / 2），四角各放一个地面点，包围盒中心与 DTM 中位数都
落在原点，规范化后的 world 坐标与生成坐标相同（`T_world_source` 为单位阵），剧本可直接使用本模块的坐标常量。

确定性：每个子系统使用独立的 `numpy.random.default_rng([seed, k, i])` 流，坐标舍入到 1 mm，法线 float32；同一平台、同一 numpy
版本下逐字节一致（`generator.params.numpy` 记录版本）。规范字节流（魔数 + xyz `<f8` + normal `<f4` + class `u1`）的 sha256
写入 `dataset.sourceFiles` 与 `coordinate.source.files`，作为"原始输入"的指纹。
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from .types import ConfigError, IngestConfig, RawCloud

__all__ = ["GENERATOR_VERSION", "LANDMARKS", "SynthCloud", "SynthSpec", "SyntheticAdapter", "generate", "synth_spec",
           "synth_specs"]

GENERATOR_VERSION = "1.0.0"            # 生成算法版本：改动几何或采样规则时递增（`--missing` 据此判定 raw_changed）
DEFAULT_ID = "synthcity"
MAGIC = b"AWRSYN1\n"

# 类别（anet-classes@1 紧凑索引，packages/contracts/classes/anet-classes-v1.json）
C_GROUND, C_LOWVEG, C_MEDVEG, C_HIGHVEG, C_ROOF, C_FACADE, C_WATER, C_ROAD, C_BRIDGE = 1, 2, 3, 4, 5, 6, 8, 9, 10

# 地标（world ENU，剧本与测试引用；ADR-077）
LANDMARKS: dict[str, dict[str, Any]] = {
    "anet-tower": {"center": (62.0, 122.0), "r_base": 25.0, "r_top": 19.0, "roof_z": 300.0, "crown_z": 318.0,
                   "mast_z": 352.0, "kind": "cylinder"},
    "step-tower": {"center": (-62.0, -2.0), "podium_z": 24.0, "tiers": ((44.0, 130.0), (36.0, 206.0), (28.0, 262.0),
                                                                          (18.0, 286.0)), "kind": "setback"},
}


# ================================================================ 规格（configs/worldpkg.yaml 的 synthetic 段）


@dataclass(frozen=True)
class SynthSpec:
    world_id: str = DEFAULT_ID
    seed: int = 20261003
    size_m: float = 1200.0
    target_points: int = 4_000_000
    name: str = "ANet Synthetic City"
    name_zh: str = "ANet Synthetic City"
    anchor: tuple[float, float, float] = (30.0, 120.0, 10.0)     # 示意锚点（lat, lon, hMsl）；不代表任何真实地点
    version: str = GENERATOR_VERSION

    def params(self) -> dict:
        """写入 `generator.params.synthetic`；与已发布包不同即判定输入变化（16 §3.5 条件 ⑤ 的合成世界口径）。

        `sourceSha256` 为本模块源文件 sha256 的前 16 位：改动生成算法而忘记递增 `GENERATOR_VERSION` 时，`--missing` 仍会重建。"""
        return {"generator": "awr.world.ingest.synthetic", "version": self.version, "seed": int(self.seed),
                "sizeM": _num(self.size_m), "targetPoints": int(self.target_points), "sourceSha256": _source_sha()}

    @property
    def source_name(self) -> str:
        return f"{self.world_id}-v{self.version}-seed{self.seed}.awrsyn"


def _num(v: float) -> float | int:
    return int(v) if float(v).is_integer() else float(v)


def _source_sha() -> str:
    from pathlib import Path

    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def synth_specs(config: dict | None = None) -> dict[str, SynthSpec]:
    """`configs/worldpkg.yaml` 的 `synthetic:` 段（缺省时只有内置 synthcity）。"""
    if config is None:
        from ..package.derivers import worldpkg_config

        try:
            config = worldpkg_config()
        except Exception:
            config = {}
    sec = (config or {}).get("synthetic") or {DEFAULT_ID: {}}
    if not isinstance(sec, dict):
        raise ConfigError("configs/worldpkg.yaml: synthetic 必须是映射 {world_id: {...}}")
    out: dict[str, SynthSpec] = {}
    for wid, d in sec.items():
        d = dict(d or {})
        unknown = set(d) - {"seed", "size_m", "target_points", "name", "name_zh", "anchor"}
        if unknown:
            raise ConfigError(f"configs/worldpkg.yaml: synthetic.{wid} 含未知键 {sorted(unknown)}")
        s = SynthSpec(world_id=str(wid))
        kw: dict[str, Any] = {}
        if "seed" in d:
            kw["seed"] = int(d["seed"])
        if "size_m" in d:
            kw["size_m"] = float(d["size_m"])
        if "target_points" in d:
            kw["target_points"] = int(d["target_points"])
        for k in ("name", "name_zh"):
            if k in d:
                kw[k] = str(d[k])
        if "anchor" in d:
            a = d["anchor"]
            kw["anchor"] = (float(a[0]), float(a[1]), float(a[2]))
        s = replace(s, **kw)
        if not 400.0 <= s.size_m <= 4000.0:
            raise ConfigError(f"synthetic.{wid}.size_m {s.size_m} 不在 [400, 4000]")
        if not 100_000 <= s.target_points <= 20_000_000:
            raise ConfigError(f"synthetic.{wid}.target_points {s.target_points} 不在 [1e5, 2e7]")
        out[str(wid)] = s
    return out


def synth_spec(world_id: str = DEFAULT_ID) -> SynthSpec:
    specs = synth_specs()
    if world_id not in specs:
        raise ConfigError(f"未知合成世界 {world_id!r}；configs/worldpkg.yaml 的 synthetic 段登记了 {sorted(specs)}")
    return specs[world_id]


# ================================================================ 城市规划（几何描述，不含点）

# 1200 m 城市的道路中心线（生成帧，m）；size_m 不同时按比例缩放位置，宽度不变
X_ROADS = (-480.0, -360.0, -240.0, -120.0, 0.0, 120.0, 240.0, 360.0, 480.0)
Y_ROADS = (-300.0, -180.0, -60.0, 60.0, 180.0, 300.0, 420.0, 540.0)
AVENUE_X = (-240.0, 0.0, 240.0)
AVENUE_Y = (60.0,)
SOUTH_ROAD_Y = -540.0
W_AVENUE, W_STREET, SIDEWALK = 26.0, 18.0, 5.0
RIVER_Y0, RIVER_AMP, RIVER_LAMBDA = -415.0, 14.0, 640.0
RIVER_HALF_WATER, RIVER_HALF_BANK = 22.0, 31.0
RIVER_Z, POND_Z, BRIDGE_Z = -3.0, -1.2, 0.6
PARK = (-600.0, 180.0, -360.0, 420.0)        # 中央公园（2 × 2 个街区，内部道路取消）
PARK_HILL = (-430.0, 340.0, 45.0, 10.0)        # 小丘：中心、标准差、高度
PARK_POND = (-530.0, 240.0, 45.0, 26.0)        # 池塘：中心、半轴
CBD_CENTER = (0.0, 60.0)
RES = 0.5                                      # 用地栅格分辨率（m）

# 用地码
U_PAVED, U_ROAD, U_MARK, U_LAWN, U_RIVER, U_POND, U_BANK, U_BRIDGE, U_BUILDING = range(9)
GROUND_RHO = np.array([0.9, 0.8, 1.7, 0.95, 0.5, 0.5, 0.9, 1.0, 0.0])     # 相对密度（点/m²，乘全局系数）
GROUND_CLS = np.array([C_GROUND, C_ROAD, C_ROAD, C_LOWVEG, C_WATER, C_WATER, C_GROUND, C_BRIDGE, 0], np.uint8)
RHO_FACADE, RHO_ROOF, RHO_EQUIP, RHO_CROWN, RHO_TRUNK = 1.2, 1.0, 1.4, 1.5, 1.2


@dataclass(frozen=True)
class Style:
    """立面窗格：横向窗距 pu、层高 fh、玻璃宽高占比 gw/gh、玻璃接受率 pg、底层商铺高度 shop_h。"""

    pu: float
    fh: float
    gw: float
    gh: float
    pg: float
    shop_h: float = 0.0

    def accept_mean(self) -> float:
        g = min(self.gw, 1.0) * min(self.gh, 1.0)
        return (1.0 - g) + g * self.pg


STYLES = {
    "curtain": Style(1.6, 4.0, 0.86, 0.82, 0.10, 6.0),
    "punched": Style(3.3, 3.5, 0.48, 0.52, 0.18, 4.5),
    "ribbon": Style(7.5, 3.8, 0.96, 0.46, 0.14, 4.5),
    "residential": Style(4.2, 3.0, 0.40, 0.50, 0.22, 0.0),
    "office": Style(2.4, 3.7, 0.66, 0.62, 0.14, 5.0),
}


@dataclass
class Part:
    """建筑的一个体块：轴对齐长方体（kind = box）或锥台圆柱（kind = cyl）。"""

    kind: str
    z0: float
    z1: float
    rect: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)   # box：x0, y0, x1, y1
    center: tuple[float, float] = (0.0, 0.0)                         # cyl
    r0: float = 0.0
    r1: float = 0.0
    roof: bool = True                  # 顶面是否采样（塔冠环只有立面）
    parapet: float = 0.0               # 女儿墙高（立面越过 z1 的部分）
    gable: str = ""                    # 坡屋顶屋脊方向 "x" | "y"（只用于单体块低层）
    equipment: bool = False            # 屋顶设备

    def inside(self, x: np.ndarray, y: np.ndarray, z: np.ndarray, eps: float = 0.0) -> np.ndarray:
        inz = (z >= self.z0 - 1e-6) & (z <= self.z1 + 1e-6)
        if self.kind == "box":
            x0, y0, x1, y1 = self.rect
            return inz & (x > x0 + eps) & (x < x1 - eps) & (y > y0 + eps) & (y < y1 - eps)
        t = np.clip((z - self.z0) / max(self.z1 - self.z0, 1e-9), 0.0, 1.0)
        r = self.r0 + (self.r1 - self.r0) * t
        return inz & ((x - self.center[0]) ** 2 + (y - self.center[1]) ** 2 < (r - eps) ** 2)

    def footprint_area(self) -> float:
        if self.kind == "box":
            x0, y0, x1, y1 = self.rect
            return (x1 - x0) * (y1 - y0)
        return math.pi * self.r1 ** 2

    def facade_area(self) -> float:
        h = self.z1 - self.z0 + self.parapet
        if self.kind == "box":
            x0, y0, x1, y1 = self.rect
            return 2.0 * ((x1 - x0) + (y1 - y0)) * h
        return math.pi * (self.r0 + self.r1) * h


@dataclass
class Building:
    parts: list[Part]
    style: Style
    equip: list[Part] = field(default_factory=list)


@dataclass
class Tree:
    x: float
    y: float
    z: float           # 树基地面高
    r: float           # 冠半径
    trunk: float       # 冠底离地高
    cls: int = C_HIGHVEG


@dataclass
class CityPlan:
    H: float
    buildings: list[Building]
    trees: list[Tree]
    landuse: np.ndarray            # int8 [ny, nx]，RES 网格，原点 (−H, −H)
    stats: dict


class _Planner:
    def __init__(self, spec: SynthSpec):
        self.spec = spec
        self.H = spec.size_m / 2.0
        self.s = spec.size_m / 1200.0
        self.rng = np.random.default_rng([spec.seed, 1])
        n = round(spec.size_m / RES)
        self.n = n
        self.lu = np.full((n, n), U_PAVED, np.int8)
        self.buildings: list[Building] = []
        self.trees: list[Tree] = []

    # ---- 栅格工具
    def _ix(self, v: float) -> int:
        return int(min(max(math.floor((v + self.H) / RES), 0), self.n))

    def fill_rect(self, x0: float, y0: float, x1: float, y1: float, code: int, where: np.ndarray | None = None) -> None:
        i0, i1, j0, j1 = self._ix(x0), self._ix(x1), self._ix(y0), self._ix(y1)
        if i1 <= i0 or j1 <= j0:
            return
        if where is None:
            self.lu[j0:j1, i0:i1] = code
        else:
            sub = self.lu[j0:j1, i0:i1]
            sub[np.isin(sub, where)] = code

    def cell_centres(self, i0: int, i1: int, j0: int, j1: int) -> tuple[np.ndarray, np.ndarray]:
        xs = -self.H + (np.arange(i0, i1) + 0.5) * RES
        ys = -self.H + (np.arange(j0, j1) + 0.5) * RES
        return np.meshgrid(xs, ys)

    # ---- 几何常量（按 size 缩放位置）
    def xr(self) -> list[float]:
        return [v * self.s for v in X_ROADS]

    def yr(self) -> list[float]:
        return [v * self.s for v in Y_ROADS]

    def road_w(self, v: float, axis: str) -> float:
        av = AVENUE_X if axis == "x" else AVENUE_Y
        return W_AVENUE if any(abs(v - a * self.s) < 1e-6 for a in av) else W_STREET

    def _wy(self, yc: float) -> float:
        return 16.0 if abs(yc - SOUTH_ROAD_Y * self.s) < 1e-6 else self.road_w(yc, "y")

    def river_yc(self, x: np.ndarray | float) -> np.ndarray | float:
        return RIVER_Y0 * self.s + RIVER_AMP * np.sin(2 * np.pi * (np.asarray(x) + self.H) / (RIVER_LAMBDA * self.s))

    def park(self) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = (v * self.s for v in PARK)
        return x0, y0, x1, y1

    # ---- 用地
    def plan_landuse(self) -> None:
        H = self.H
        # 道路（全长；公园内部的段随后被草坪覆盖）
        for xc in self.xr():
            w = self.road_w(xc, "x")
            self.fill_rect(xc - w / 2, -H, xc + w / 2, H, U_ROAD)
        for yc in [*self.yr(), SOUTH_ROAD_Y * self.s]:
            w = self._wy(yc)
            self.fill_rect(-H, yc - w / 2, H, yc + w / 2, U_ROAD)
        self._markings()
        # 中央公园：草坪、园路、池塘
        px0, py0, px1, py1 = self.park()
        wx = self.road_w(px1, "x") / 2 + SIDEWALK
        wy0 = self.road_w(py0, "y") / 2 + SIDEWALK
        wy1 = self.road_w(py1, "y") / 2 + SIDEWALK
        self.park_rect = (px0 + 6.0, py0 + wy0, px1 - wx, py1 - wy1)
        self.fill_rect(*self.park_rect, U_LAWN)
        self._park_paths()
        cx, cy, ax, ay = PARK_POND
        cx, cy = cx * self.s, cy * self.s
        i0, i1, j0, j1 = self._ix(cx - ax), self._ix(cx + ax), self._ix(cy - ay), self._ix(cy + ay)
        X, Y = self.cell_centres(i0, i1, j0, j1)
        sub = self.lu[j0:j1, i0:i1]
        sub[((X - cx) / ax) ** 2 + ((Y - cy) / ay) ** 2 < 1.0] = U_POND
        # 河道与桥（南北向道路跨河处为桥面）
        ys = -H + (np.arange(self.n) + 0.5) * RES
        xs = ys.copy()
        dy = ys[:, None] - self.river_yc(xs)[None, :]
        ad = np.abs(dy)
        bridge = np.zeros(self.n, bool)
        for xc in self.xr():
            w = self.road_w(xc, "x")
            bridge |= np.abs(xs - xc) < w / 2 + 1.0
        band = ad < RIVER_HALF_BANK
        self.lu[band & (ad < RIVER_HALF_WATER)] = U_RIVER
        self.lu[band & (ad >= RIVER_HALF_WATER)] = U_BANK
        self.lu[band & bridge[None, :]] = U_BRIDGE
        # 滨河绿带：河岸两侧到最近东西向道路之间（北岸到 y = −300 路的人行道，南岸到南路人行道）
        top = self.yr()[0] - self.road_w(self.yr()[0], "y") / 2 - SIDEWALK
        south = SOUTH_ROAD_Y * self.s + 8.0 + SIDEWALK
        strip = ((ys[:, None] < top) & (dy > RIVER_HALF_BANK) & (dy < RIVER_HALF_BANK + 200.0)) | \
                ((ys[:, None] > south) & (dy < -RIVER_HALF_BANK))
        strip &= np.isin(self.lu, (U_PAVED,))
        self.lu[strip] = U_LAWN
        # 滨河步道（北岸、南岸各一条 6 m 铺装带）
        for sgn in (1.0, -1.0):
            d0, d1 = RIVER_HALF_BANK + 3.0, RIVER_HALF_BANK + 9.0
            m = (sgn * dy > d0) & (sgn * dy < d1) & (self.lu == U_LAWN)
            self.lu[m] = U_PAVED

    def _markings(self) -> None:
        """车道线（虚线 6 m 实 6 m 空，路口内不画）与路口斑马线：只改用地码为 U_MARK（密度更高的路面点）。"""
        H = self.H
        c = -H + (np.arange(self.n) + 0.5) * RES
        dash = np.mod(c, 12.0) < 6.0
        xr, yr = self.xr(), [*self.yr(), SOUTH_ROAD_Y * self.s]
        in_x = np.zeros(self.n, bool)
        in_y = np.zeros(self.n, bool)
        for xc in xr:
            in_x |= np.abs(c - xc) < self.road_w(xc, "x") / 2
        for yc in yr:
            in_y |= np.abs(c - yc) < self._wy(yc) / 2
        for xc in xr:
            w = self.road_w(xc, "x")
            offs = (-0.45, 0.45, -w / 4, w / 4) if w > 20 else (0.0, -w / 4, w / 4)
            for k, o in enumerate(offs):
                i = self._ix(xc + o)
                col = self.lu[:, i]
                solid = w > 20 and k < 2
                col[(col == U_ROAD) & ~in_y & (True if solid else dash)] = U_MARK
        for yc in yr:
            w = self._wy(yc)
            offs = (-0.45, 0.45, -w / 4, w / 4) if w > 20 else (0.0, -w / 4, w / 4)
            for k, o in enumerate(offs):
                j = self._ix(yc + o)
                row = self.lu[j, :]
                solid = w > 20 and k < 2
                row[(row == U_ROAD) & ~in_x & (True if solid else dash)] = U_MARK
        # 斑马线：每个路口四个方向各一条 4 m 宽带，0.5 m 间隔
        for xc in xr:
            wx = self.road_w(xc, "x")
            for yc in yr:
                wy = self._wy(yc)
                for sgn in (-1.0, 1.0):
                    # 南北向道路上的横道（带沿 y，条纹沿 x 交替）
                    ya, yb = sorted((yc + sgn * (wy / 2 + 1.5), yc + sgn * (wy / 2 + 5.5)))
                    i0, i1, j0, j1 = self._ix(xc - wx / 2), self._ix(xc + wx / 2), self._ix(ya), self._ix(yb)
                    X, _ = self.cell_centres(i0, i1, j0, j1)
                    sub = self.lu[j0:j1, i0:i1]
                    sub[(np.floor((X - xc) / 0.5).astype(np.int64) % 2 == 0) & np.isin(sub, (U_ROAD, U_MARK))] = U_MARK
                    xa, xb = sorted((xc + sgn * (wx / 2 + 1.5), xc + sgn * (wx / 2 + 5.5)))
                    i0, i1, j0, j1 = self._ix(xa), self._ix(xb), self._ix(yc - wy / 2), self._ix(yc + wy / 2)
                    _, Y = self.cell_centres(i0, i1, j0, j1)
                    sub = self.lu[j0:j1, i0:i1]
                    sub[(np.floor((Y - yc) / 0.5).astype(np.int64) % 2 == 0) & np.isin(sub, (U_ROAD, U_MARK))] = U_MARK

    def _park_paths(self) -> None:
        x0, y0, x1, y1 = self.park_rect
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        i0, i1, j0, j1 = self._ix(x0), self._ix(x1), self._ix(y0), self._ix(y1)
        X, Y = self.cell_centres(i0, i1, j0, j1)
        sub = self.lu[j0:j1, i0:i1]
        r = np.hypot(X - cx, Y - cy)
        path = np.abs(r - 78.0) < 2.2                                       # 环形园路
        for (ax, ay), (bx, by) in (((x0, y0), (x1, y1)), ((x0, y1), (x1, y0))):   # 两条对角园路
            dx, dy = bx - ax, by - ay
            L = math.hypot(dx, dy)
            path |= np.abs((X - ax) * dy - (Y - ay) * dx) / L < 1.8
        sub[path & (sub == U_LAWN)] = U_PAVED

    # ---- 街区与建筑
    def plan_blocks(self) -> None:
        H, s = self.H, self.s
        xr, yr = self.xr(), self.yr()
        xb = [-H, *xr, H]
        yb = [*yr, H]
        b_index = 0
        for j in range(len(yb) - 1):
            for i in range(len(xb) - 1):
                cx0, cx1, cy0, cy1 = xb[i], xb[i + 1], yb[j], yb[j + 1]
                ix0 = cx0 + (self.road_w(cx0, "x") / 2 + SIDEWALK if i > 0 else 6.0)
                ix1 = cx1 - (self.road_w(cx1, "x") / 2 + SIDEWALK if i < len(xb) - 2 else 6.0)
                iy0 = cy0 + self.road_w(cy0, "y") / 2 + SIDEWALK
                iy1 = cy1 - (self.road_w(cy1, "y") / 2 + SIDEWALK if j < len(yb) - 2 else 6.0)
                ccx, ccy = (ix0 + ix1) / 2, (iy0 + iy1) / 2
                px0, py0, px1, py1 = self.park()
                if px0 - 1 <= ccx <= px1 + 1 and py0 - 1 <= ccy <= py1 + 1:
                    continue                                                 # 公园
                rng = np.random.default_rng([self.spec.seed, 1, 1000 + b_index])
                b_index += 1
                rect = (ix0, iy0, ix1, iy1)
                a = LANDMARKS["anet-tower"]["center"]
                b = LANDMARKS["step-tower"]["center"]
                if ix0 <= a[0] * s <= ix1 and iy0 <= a[1] * s <= iy1:
                    self._tower_a(rect)
                    continue
                if ix0 <= b[0] * s <= ix1 and iy0 <= b[1] * s <= iy1:
                    self._tower_b(rect)
                    continue
                d = math.hypot(ccx - CBD_CENTER[0] * s, ccy - CBD_CENTER[1] * s)
                if d < 330.0 * s:
                    self._cbd_block(rect, d, rng)
                elif d < 560.0 * s:
                    self._mid_block(rect, d, rng)
                else:
                    self._outer_block(rect, rng)
        # 南岸：南路以南一排低层
        y0 = -H + 6.0
        y1 = SOUTH_ROAD_Y * s - 8.0 - SIDEWALK
        x = -H + 8.0
        k = 0
        while x < H - 30.0:
            rng = np.random.default_rng([self.spec.seed, 1, 5000 + k])
            k += 1
            w = float(rng.uniform(18.0, 34.0))
            if x + w > H - 8.0:
                break
            if not any(abs(x + w / 2 - xc) < self.road_w(xc, "x") / 2 + SIDEWALK + w / 2 for xc in self.xr()):
                d = float(rng.uniform(16.0, max(17.0, y1 - y0 - 4.0)))
                self._lowrise((x, y1 - d, x + w, y1), rng)
            x += w + float(rng.uniform(6.0, 12.0))

    def _add(self, parts: list[Part], style: Style, rng: np.random.Generator) -> None:
        b = Building(parts, style)
        for p in parts:
            if p.equipment and p.kind == "box":
                for e in _equipment(p, rng):
                    x0, y0, x1, y1 = e.rect if e.kind == "box" else (e.center[0] - e.r0, e.center[1] - e.r0,
                                                                     e.center[0] + e.r0, e.center[1] + e.r0)
                    xs = np.array([x0, x1, x0, x1, (x0 + x1) / 2])
                    ys = np.array([y0, y0, y1, y1, (y0 + y1) / 2])
                    zs = np.full(5, e.z0 + 0.1)
                    if not any(q.inside(xs, ys, zs).any() for q in parts if q is not p and q.roof):
                        b.equip.append(e)
        self.buildings.append(b)
        for p in parts:
            if p.z0 <= 0.01:
                if p.kind == "box":
                    self.fill_rect(*p.rect, U_BUILDING)
                else:
                    cx, cy = p.center
                    i0, i1, j0, j1 = self._ix(cx - p.r0), self._ix(cx + p.r0), self._ix(cy - p.r0), self._ix(cy + p.r0)
                    X, Y = self.cell_centres(i0, i1, j0, j1)
                    sub = self.lu[j0:j1, i0:i1]
                    sub[(X - cx) ** 2 + (Y - cy) ** 2 < p.r0 ** 2] = U_BUILDING

    def _tower_a(self, rect: tuple[float, float, float, float]) -> None:
        """地标一：圆柱 ANet Tower（塔身 300 m、开口塔冠到 318 m、桅杆到 352 m），四周广场与树阵。"""
        L = LANDMARKS["anet-tower"]
        cx, cy = (v * self.s for v in L["center"])
        rng = np.random.default_rng([self.spec.seed, 1, 1])
        parts = [Part("cyl", 0.0, L["roof_z"], center=(cx, cy), r0=L["r_base"], r1=L["r_top"], equipment=False),
                 Part("cyl", L["roof_z"], L["crown_z"], center=(cx, cy), r0=L["r_top"], r1=L["r_top"], roof=False),
                 Part("cyl", L["roof_z"], L["mast_z"], center=(cx, cy), r0=1.6, r1=0.6)]
        self._add(parts, STYLES["curtain"], rng)
        b = self.buildings[-1]
        for k in range(4):                                                  # 塔顶设备（塔冠内）
            a = k * math.pi / 2 + math.pi / 4
            x, y = cx + 9.0 * math.cos(a), cy + 9.0 * math.sin(a)
            b.equip.append(Part("box", L["roof_z"], L["roof_z"] + 3.0, rect=(x - 2.5, y - 2.0, x + 2.5, y + 2.0)))
        for k in range(24):                                                 # 广场树阵
            a = 2 * math.pi * k / 24
            self.trees.append(Tree(cx + 38.0 * math.cos(a), cy + 38.0 * math.sin(a), 0.15, 2.8, 3.4))

    def _tower_b(self, rect: tuple[float, float, float, float]) -> None:
        """地标二：四级阶梯退台塔（裙楼 24 m，塔顶 286 m）。"""
        L = LANDMARKS["step-tower"]
        cx, cy = (v * self.s for v in L["center"])
        rng = np.random.default_rng([self.spec.seed, 1, 2])
        x0, y0, x1, y1 = rect
        parts = [Part("box", 0.0, L["podium_z"], rect=(x0 + 5.0, y0 + 5.0, x1 - 5.0, y1 - 5.0), parapet=1.1, equipment=True)]
        z = L["podium_z"]
        for side, ztop in L["tiers"]:
            h = side / 2
            parts.append(Part("box", z, ztop, rect=(cx - h, cy - h, cx + h, cy + h), parapet=1.2,
                              equipment=ztop == L["tiers"][-1][1]))
            z = ztop
        self._add(parts, STYLES["office"], rng)

    def _cbd_block(self, rect: tuple[float, float, float, float], d: float, rng: np.random.Generator) -> None:
        x0, y0, x1, y1 = rect
        W = x1 - x0
        hp = float(rng.uniform(12.0, 24.0))
        h_scale = 70.0 + 130.0 * math.exp(-d / (220.0 * self.s))
        two = W > 70 and rng.random() < 0.45
        podium = Part("box", 0.0, hp, rect=(x0 + 4.5, y0 + 4.5, x1 - 4.5, y1 - 4.5), parapet=1.0, equipment=True)
        towers: list[Part] = []
        style = STYLES[str(rng.choice(["curtain", "office", "ribbon"]))]
        centres = [((x0 + x1) / 2, (y0 + y1) / 2)] if not two else [(x0 + W * 0.28, (y0 + y1) / 2), (x0 + W * 0.72, (y0 + y1) / 2)]
        for k, (tx, ty) in enumerate(centres):
            h = float(min(210.0, h_scale * rng.uniform(0.65, 1.1))) * (0.8 if k else 1.0)
            # 双塔间距 ≥ 12 m（法线修正沿法线外取 2 m 再落 2 m 格，近邻高楼会让立面法线被误翻，M03-FR-014）
            side = float(rng.uniform(26.0, 40.0)) if not two else float(rng.uniform(22.0, min(28.0, W * 0.44 - 12.0)))
            kind = rng.choice(["rect", "rect", "rect", "setback", "setback", "cyl"])
            if kind == "cyl":
                r = side / 2
                towers.append(Part("cyl", hp, hp + h, center=(tx, ty), r0=r, r1=r * 0.9, equipment=False))
            elif kind == "setback":
                zz = hp
                for f, frac in ((1.0, 0.55), (0.78, 0.82), (0.56, 1.0)):
                    hh = side * f / 2
                    zt = hp + h * frac
                    towers.append(Part("box", zz, zt, rect=(tx - hh, ty - hh, tx + hh, ty + hh), parapet=1.2,
                                       equipment=frac == 1.0))
                    zz = zt
            else:
                hx = side / 2 * (float(rng.uniform(0.85, 1.15)) if not two else 1.0)
                hy = side / 2
                towers.append(Part("box", hp, hp + h, rect=(tx - hx, ty - hy, tx + hx, ty + hy), parapet=1.5, equipment=True))
        self._add([podium, *towers], style, rng)

    def _mid_block(self, rect: tuple[float, float, float, float], d: float, rng: np.random.Generator) -> None:
        x0, y0, x1, y1 = rect
        W, D = x1 - x0, y1 - y0
        hmax = 18.0 + 42.0 * math.exp(-(d - 330.0 * self.s) / (250.0 * self.s))
        dep = float(rng.uniform(14.0, 18.0))
        modes = ["lots", "lots"]                                              # 浅街区（北缘 40 m 一排）只用地块
        if min(W, D) >= 2 * dep + 34:
            modes.append("courtyard")
        if 2 * dep + 36 <= D:
            modes.append("slabs")
        mode = rng.choice(modes)
        style = STYLES[str(rng.choice(["punched", "office", "residential", "ribbon"]))]
        if mode == "courtyard":
            h = float(rng.uniform(16.0, max(17.0, min(hmax, 32.0))))
            a, b, c, e = x0 + 5, y0 + 5, x1 - 5, y1 - 5
            parts = [Part("box", 0.0, h, rect=(a, b, c, b + dep), parapet=1.0, equipment=True),
                     Part("box", 0.0, h, rect=(a, e - dep, c, e), parapet=1.0, equipment=True),
                     Part("box", 0.0, h, rect=(a, b + dep, a + dep, e - dep), parapet=1.0),
                     Part("box", 0.0, h, rect=(c - dep, b + dep, c, e - dep), parapet=1.0)]
            self._add(parts, style, rng)
            self.fill_rect(a + dep + 2, b + dep + 2, c - dep - 2, e - dep - 2, U_LAWN, where=(U_PAVED,))
            for _ in range(int(rng.integers(2, 5))):
                self.trees.append(Tree(float(rng.uniform(a + dep + 9, c - dep - 9)), float(rng.uniform(b + dep + 9, e - dep - 9)),
                                       0.05, float(rng.uniform(2.4, 3.4)), float(rng.uniform(2.6, 3.6))))
            return
        if mode == "slabs":
            for yy in (y0 + 6.0, y1 - 6.0 - dep):
                h = float(rng.uniform(20.0, max(21.0, hmax)))
                self._add([Part("box", 0.0, h, rect=(x0 + 6.0, yy, x1 - 6.0, yy + dep), parapet=1.0, equipment=True)], style, rng)
            self.fill_rect(x0 + 10, y0 + 12 + dep, x1 - 10, y1 - 12 - dep, U_LAWN, where=(U_PAVED,))
            return
        for (lx0, ly0, lx1, ly1) in _split(rect, 2 if W >= 50 else 1, 2 if D >= 50 else 1, 7.0):
            style = STYLES[str(rng.choice(["punched", "office", "residential", "ribbon"]))]
            h = float(rng.uniform(15.0, max(16.0, hmax)))
            m = 4.5
            if rng.random() < 0.35:                                           # L 形
                w1 = (lx1 - lx0) * float(rng.uniform(0.38, 0.5))
                d1 = (ly1 - ly0) * float(rng.uniform(0.38, 0.5))
                corner = int(rng.integers(0, 4))
                ax0, ay0, ax1, ay1 = lx0 + m, ly0 + m, lx1 - m, ly1 - m
                if corner in (0, 1):
                    bar = Part("box", 0.0, h, rect=(ax0, ay0, ax1, ay0 + d1), parapet=1.0, equipment=True)
                else:
                    bar = Part("box", 0.0, h, rect=(ax0, ay1 - d1, ax1, ay1), parapet=1.0, equipment=True)
                if corner in (0, 2):
                    leg = Part("box", 0.0, h, rect=(ax0, ay0, ax0 + w1, ay1), parapet=1.0)
                else:
                    leg = Part("box", 0.0, h, rect=(ax1 - w1, ay0, ax1, ay1), parapet=1.0)
                self._add([bar, leg], style, rng)
            else:
                self._add([Part("box", 0.0, h, rect=(lx0 + m, ly0 + m, lx1 - m, ly1 - m), parapet=1.0, equipment=True)],
                          style, rng)

    def _outer_block(self, rect: tuple[float, float, float, float], rng: np.random.Generator) -> None:
        nx = 3 if rect[2] - rect[0] > 60 else 2
        ny = 3 if rect[3] - rect[1] > 60 else 2
        for lot in _split(rect, nx, ny, 6.0):
            if rng.random() < 0.12:                                           # 空地：草坪与一棵树
                self.fill_rect(*lot, U_LAWN, where=(U_PAVED,))
                self.trees.append(Tree((lot[0] + lot[2]) / 2, (lot[1] + lot[3]) / 2, 0.05,
                                       float(rng.uniform(2.6, 3.8)), float(rng.uniform(2.6, 3.6))))
                continue
            self._lowrise((lot[0] + 4.0, lot[1] + 4.0, lot[2] - 4.0, lot[3] - 4.0), rng)

    def _lowrise(self, rect: tuple[float, float, float, float], rng: np.random.Generator) -> None:
        h = float(rng.uniform(7.0, 16.0))
        x0, y0, x1, y1 = rect
        if x1 - x0 < 6.0 or y1 - y0 < 6.0:
            return
        if rng.random() < 0.45:
            ridge = "x" if (x1 - x0) >= (y1 - y0) else "y"
            self._add([Part("box", 0.0, h, rect=rect, gable=ridge)], STYLES["residential"], rng)
        else:
            self._add([Part("box", 0.0, h, rect=rect, parapet=0.8, equipment=rng.random() < 0.5)],
                      STYLES[str(rng.choice(["residential", "punched"]))], rng)

    # ---- 树
    def plan_trees(self) -> None:
        H, s = self.H, self.s
        rng = np.random.default_rng([self.spec.seed, 1, 9000])
        xr, yr = self.xr(), [*self.yr(), SOUTH_ROAD_Y * s]

        def ok(x: float, y: float) -> bool:
            if not (-H + 3 <= x <= H - 3 and -H + 3 <= y <= H - 3):
                return False
            return int(self.lu[self._ix(y), self._ix(x)]) == U_PAVED

        step = 12.0
        for xc in xr:                                                         # 南北向道路两侧
            w = self.road_w(xc, "x")
            for sgn in (-1.0, 1.0):
                x = xc + sgn * (w / 2 + 1.8)
                for y in np.arange(-H + 6.0, H - 6.0, step):
                    if any(abs(y - yc) < self._wy(yc) / 2 + 9.0 for yc in yr):
                        continue
                    if ok(x, float(y)):
                        self.trees.append(Tree(x, float(y), 0.15, float(rng.uniform(2.0, 3.0)), float(rng.uniform(3.0, 4.2))))
        for yc in yr:                                                         # 东西向道路两侧
            w = self._wy(yc)
            for sgn in (-1.0, 1.0):
                y = yc + sgn * (w / 2 + 1.8)
                for x in np.arange(-H + 6.0 + step / 2, H - 6.0, step):
                    if any(abs(x - xc) < self.road_w(xc, "x") / 2 + 9.0 for xc in xr):
                        continue
                    if ok(float(x), y):
                        self.trees.append(Tree(float(x), y, 0.15, float(rng.uniform(2.0, 3.0)), float(rng.uniform(3.0, 4.2))))
        # 公园树丛：抖动网格 13 m，草坪上、离园路与池塘 ≥ 4 m
        x0, y0, x1, y1 = self.park_rect
        for gx in np.arange(x0 + 8.0, x1 - 6.0, 13.0):
            for gy in np.arange(y0 + 8.0, y1 - 6.0, 13.0):
                x, y = float(gx + rng.uniform(-4.5, 4.5)), float(gy + rng.uniform(-4.5, 4.5))
                if rng.random() > 0.62:
                    continue
                if not self._lawn_clear(x, y, 4.0):
                    continue
                self.trees.append(Tree(x, y, self.ground_z_scalar(x, y), float(rng.uniform(3.0, 5.5)), float(rng.uniform(2.6, 4.5))))
        for _ in range(170):                                                  # 灌木（中等植被）
            x, y = float(rng.uniform(x0 + 4, x1 - 4)), float(rng.uniform(y0 + 4, y1 - 4))
            if self._lawn_clear(x, y, 2.0):
                self.trees.append(Tree(x, y, self.ground_z_scalar(x, y), float(rng.uniform(0.8, 1.5)), 0.0, C_MEDVEG))
        # 滨河绿带：沿岸一排树，其余草坪上散植树丛与灌木
        for sgn in (1.0, -1.0):
            for x in np.arange(-H + 10.0, H - 10.0, 11.0):
                y = float(self.river_yc(float(x))) + sgn * (RIVER_HALF_BANK + 14.0 + rng.uniform(-2.0, 2.0))
                if self._lawn_clear(float(x), y, 3.0):
                    self.trees.append(Tree(float(x), y, 0.05, float(rng.uniform(2.6, 4.0)), float(rng.uniform(2.8, 4.0))))
        y_top = self.yr()[0]
        for gx in np.arange(-H + 12.0, H - 12.0, 17.0):
            for gy in np.arange(SOUTH_ROAD_Y * s, y_top, 17.0):
                x, y = float(gx + rng.uniform(-6.0, 6.0)), float(gy + rng.uniform(-6.0, 6.0))
                u = rng.random()
                if u < 0.42 and self._lawn_clear(x, y, 4.5):
                    self.trees.append(Tree(x, y, 0.05, float(rng.uniform(2.8, 4.6)), float(rng.uniform(2.6, 4.0))))
                elif u > 0.9 and self._lawn_clear(x, y, 2.0):
                    self.trees.append(Tree(x, y, 0.05, float(rng.uniform(0.8, 1.4)), 0.0, C_MEDVEG))

    def _lawn_clear(self, x: float, y: float, r: float) -> bool:
        if not (-self.H + r <= x <= self.H - r and -self.H + r <= y <= self.H - r):
            return False
        i0, i1, j0, j1 = self._ix(x - r), self._ix(x + r), self._ix(y - r), self._ix(y + r)
        return bool(np.all(self.lu[j0:j1, i0:i1] == U_LAWN))

    def ground_z_scalar(self, x: float, y: float) -> float:
        return float(_ground_z(np.array([x]), np.array([y]), np.array([int(self.lu[self._ix(y), self._ix(x)])]), self)[0][0])

    def run(self) -> CityPlan:
        self.plan_landuse()
        self.plan_blocks()
        self.plan_trees()
        stats = {"buildings": len(self.buildings), "parts": sum(len(b.parts) for b in self.buildings),
                 "trees": sum(1 for t in self.trees if t.cls == C_HIGHVEG),
                 "shrubs": sum(1 for t in self.trees if t.cls == C_MEDVEG)}
        return CityPlan(self.H, self.buildings, self.trees, self.lu, stats)


def _split(rect: tuple[float, float, float, float], nx: int, ny: int, gap: float) -> list[tuple[float, float, float, float]]:
    x0, y0, x1, y1 = rect
    wx = (x1 - x0 - gap * (nx - 1)) / nx
    wy = (y1 - y0 - gap * (ny - 1)) / ny
    return [(x0 + i * (wx + gap), y0 + j * (wy + gap), x0 + i * (wx + gap) + wx, y0 + j * (wy + gap) + wy)
            for j in range(ny) for i in range(nx)]


def _equipment(p: Part, rng: np.random.Generator) -> list[Part]:
    """屋顶设备：机组箱体与水箱（屋面内缩 3 m 内随机布置）。"""
    x0, y0, x1, y1 = p.rect
    area = (x1 - x0) * (y1 - y0)
    if area < 250.0 or x1 - x0 < 12 or y1 - y0 < 12:
        return []
    out = []
    n = int(np.clip(area / 380.0, 1, 7))
    for _ in range(n):
        w, d = float(rng.uniform(2.0, 6.0)), float(rng.uniform(2.0, 5.0))
        h = float(rng.uniform(1.2, 3.5))
        cx = float(rng.uniform(x0 + 3 + w / 2, x1 - 3 - w / 2))
        cy = float(rng.uniform(y0 + 3 + d / 2, y1 - 3 - d / 2))
        out.append(Part("box", p.z1, p.z1 + h, rect=(cx - w / 2, cy - d / 2, cx + w / 2, cy + d / 2)))
    if rng.random() < 0.35:
        r = float(rng.uniform(1.2, 2.0))
        cx = float(rng.uniform(x0 + 3 + r, x1 - 3 - r))
        cy = float(rng.uniform(y0 + 3 + r, y1 - 3 - r))
        out.append(Part("cyl", p.z1, p.z1 + float(rng.uniform(2.5, 4.0)), center=(cx, cy), r0=r, r1=r))
    return out


def _ground_z(x: np.ndarray, y: np.ndarray, code: np.ndarray, pl: _Planner) -> tuple[np.ndarray, np.ndarray]:
    """地面高程与法线（用地码决定）：人行道 +0.15、草坪 +0.05 与公园小丘、河水 −3、池塘 −1.2、河岸线性坡、桥面 +0.6。"""
    z = np.zeros(len(x))
    n = np.zeros((len(x), 3))
    n[:, 2] = 1.0
    z[code == U_PAVED] = 0.15
    z[code == U_MARK] = 0.01
    lawn = code == U_LAWN
    hx, hy, hs, hh = PARK_HILL
    hx, hy = hx * pl.s, hy * pl.s
    if lawn.any():
        dx, dy = x[lawn] - hx, y[lawn] - hy
        g = hh * np.exp(-(dx * dx + dy * dy) / (2 * hs * hs))
        z[lawn] = 0.05 + g
        gx, gy = -dx / (hs * hs) * g, -dy / (hs * hs) * g
        nn = np.c_[-gx, -gy, np.ones(len(gx))]
        n[lawn] = nn / np.linalg.norm(nn, axis=1, keepdims=True)
    z[code == U_RIVER] = RIVER_Z
    z[code == U_POND] = POND_Z
    z[code == U_BRIDGE] = BRIDGE_Z
    bank = code == U_BANK
    if bank.any():
        dy = y[bank] - pl.river_yc(x[bank])
        ad = np.abs(dy)
        slope = -RIVER_Z / (RIVER_HALF_BANK - RIVER_HALF_WATER)
        z[bank] = RIVER_Z + slope * (ad - RIVER_HALF_WATER)
        nn = np.c_[np.zeros(len(dy)), -np.sign(dy) * slope, np.ones(len(dy))]
        n[bank] = nn / np.linalg.norm(nn, axis=1, keepdims=True)
    return z, n


# ================================================================ 采样


class _Acc:
    def __init__(self) -> None:
        self.P: list[np.ndarray] = []
        self.N: list[np.ndarray] = []
        self.C: list[np.ndarray] = []

    def add(self, P: np.ndarray, N: np.ndarray, c: int | np.ndarray) -> None:
        if len(P) == 0:
            return
        self.P.append(np.asarray(P, np.float64))
        self.N.append(np.asarray(N, np.float64))
        self.C.append(np.full(len(P), c, np.uint8) if np.isscalar(c) else np.asarray(c, np.uint8))

    def count(self) -> int:
        return sum(len(p) for p in self.P)


def _window_keep(u: np.ndarray, v: np.ndarray, L: float, zb: float, st: Style, rng: np.random.Generator) -> np.ndarray:
    """窗格：返回保留掩码（窗框全保留，玻璃按接受率保留）。窗格在立面宽度上居中；立面两端 0.6 m 内总是窗框。"""
    off = (L - math.floor(L / st.pu) * st.pu) / 2 if st.pu < L else 0.0
    fu = np.mod(u - off, st.pu) / st.pu
    vv = v - zb
    shop = vv < st.shop_h
    fh = np.where(shop, max(st.shop_h, 1e-6), st.fh)
    fv = np.where(shop, vv / fh, np.mod(vv - st.shop_h, st.fh) / st.fh)
    gw = np.where(shop, 0.86, st.gw)
    gh = np.where(shop, 0.78, st.gh)
    glass = (np.abs(fu - 0.5) < gw / 2) & (np.abs(fv - 0.52) < gh / 2) & (u > 0.6) & (u < L - 0.6)
    return ~glass | (rng.random(len(u)) < np.where(shop, 0.3, st.pg))


def _facade_keep(p: Part, b: Building, x: np.ndarray, y: np.ndarray, z: np.ndarray, nx: np.ndarray | float,
                 ny: np.ndarray | float) -> np.ndarray:
    """外立面判定：外移 0.3 m 落入本建筑其他实心体块的点是内墙（剔除）；与更早体块共线的边界只保留一份（内移 0.3 m 落入更早的
    体块即剔除）。同高体块之间的女儿墙按体块顶高判定，使 L 形与院落的接缝处没有内部女儿墙。"""
    m = np.ones(len(x), bool)
    me = next(i for i, q in enumerate(b.parts) if q is p)
    for j, q in enumerate(b.parts):
        if q is p or not q.roof:
            continue
        zt = np.minimum(z, q.z1 - 0.01) if q.z1 >= p.z1 - 1e-6 else z
        m &= ~q.inside(x + nx * 0.3, y + ny * 0.3, zt)
        if j < me:
            m &= ~q.inside(x - nx * 0.3, y - ny * 0.3, zt)
    return m


def _roof_keep(p: Part, b: Building, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """被更高实心体块覆盖的屋面不采样；与更早的同高体块重叠的屋面只保留一份。"""
    m = np.ones(len(x), bool)
    me = next(i for i, q in enumerate(b.parts) if q is p)
    for j, q in enumerate(b.parts):
        if q is p or not q.roof:
            continue
        if q.z0 <= p.z1 + 1e-6 < q.z1:
            m &= ~q.inside(x, y, np.full(len(x), (q.z0 + q.z1) / 2))
        elif j < me and abs(q.z1 - p.z1) < 1e-6:
            m &= ~q.inside(x, y, np.full(len(x), q.z1 - 0.01))
    return m


def _sample_box_part(acc: _Acc, p: Part, b: Building, k: float, rng: np.random.Generator) -> None:
    x0, y0, x1, y1 = p.rect
    st = b.style
    ztop = p.z1 + p.parapet
    edges = (((x0, y0), (x1, y0), (0.0, -1.0)), ((x1, y0), (x1, y1), (1.0, 0.0)),
             ((x1, y1), (x0, y1), (0.0, 1.0)), ((x0, y1), (x0, y0), (-1.0, 0.0)))
    gable_h = 0.0
    if p.gable:
        span = (y1 - y0) if p.gable == "x" else (x1 - x0)
        gable_h = math.tan(math.radians(30.0)) * span / 2
    for (ax, ay), (bx, by), (nx, ny) in edges:
        L = math.hypot(bx - ax, by - ay)
        h = ztop - p.z0
        n = round(L * h * RHO_FACADE * k / st.accept_mean())
        if n <= 0:
            continue
        u = rng.random(n) * L
        v = p.z0 + rng.random(n) * h
        keep = _window_keep(u, v, L, p.z0, st, rng)
        u, v = u[keep], v[keep]
        t = u / L
        x = ax + (bx - ax) * t
        y = ay + (by - ay) * t
        m = _facade_keep(p, b, x, y, v, nx, ny)
        acc.add(np.c_[x[m], y[m], v[m]], np.tile([nx, ny, 0.0], (int(m.sum()), 1)), C_FACADE)
        # 坡屋顶的山墙三角（屋脊方向两端）
        if p.gable and ((p.gable == "x" and nx != 0.0) or (p.gable == "y" and ny != 0.0)):
            ng = round(L * gable_h / 2 * RHO_FACADE * k)
            if ng > 0:
                uu = rng.random(ng) * L
                vv = rng.random(ng) * gable_h
                half = L / 2
                ok = vv <= gable_h * (1 - np.abs(uu - half) / half)
                uu, vv = uu[ok], vv[ok]
                tt = uu / L
                acc.add(np.c_[ax + (bx - ax) * tt, ay + (by - ay) * tt, p.z1 + vv], np.tile([nx, ny, 0.0], (len(uu), 1)),
                        C_FACADE)
    if not p.roof:
        return
    area = (x1 - x0) * (y1 - y0)
    if p.gable:
        span = (y1 - y0) if p.gable == "x" else (x1 - x0)
        sn, cs = math.sin(math.radians(30.0)), math.cos(math.radians(30.0))
        n = round(area / cs * RHO_ROOF * k)
        x = x0 + rng.random(n) * (x1 - x0)
        y = y0 + rng.random(n) * (y1 - y0)
        d = (y - (y0 + y1) / 2) if p.gable == "x" else (x - (x0 + x1) / 2)
        z = p.z1 + gable_h * (1 - np.abs(d) / (span / 2))
        sg = np.where(d >= 0, 1.0, -1.0)
        zero = np.zeros(n)
        N = np.c_[zero, sg * sn, np.full(n, cs)] if p.gable == "x" else np.c_[sg * sn, zero, np.full(n, cs)]
        acc.add(np.c_[x, y, z], N, C_ROOF)
        return
    n = round(area * RHO_ROOF * k)
    x = x0 + rng.random(n) * (x1 - x0)
    y = y0 + rng.random(n) * (y1 - y0)
    m = _roof_keep(p, b, x, y)
    acc.add(np.c_[x[m], y[m], np.full(int(m.sum()), p.z1)], np.tile([0.0, 0.0, 1.0], (int(m.sum()), 1)), C_ROOF)


def _sample_cyl_part(acc: _Acc, p: Part, b: Building, k: float, rng: np.random.Generator, rho: float = RHO_FACADE,
                     windows: bool = True) -> None:
    cx, cy = p.center
    st = b.style
    h = p.z1 - p.z0
    rm = (p.r0 + p.r1) / 2
    L = 2 * math.pi * rm
    n = round(L * h * rho * k / (st.accept_mean() if windows else 1.0))
    if n > 0:
        u = rng.random(n) * L
        v = p.z0 + rng.random(n) * h
        if windows:
            keep = _window_keep(u, v, L, p.z0, st, rng)
            u, v = u[keep], v[keep]
        th = u / rm
        r = p.r0 + (p.r1 - p.r0) * (v - p.z0) / h
        c, s = np.cos(th), np.sin(th)
        N = np.c_[c, s, np.full(len(c), (p.r0 - p.r1) / h)]
        N /= np.linalg.norm(N, axis=1, keepdims=True)
        x, y = cx + r * c, cy + r * s
        m = _facade_keep(p, b, x, y, v, c, s)
        acc.add(np.c_[x[m], y[m], v[m]], N[m], C_FACADE)
    if p.roof:
        n = round(math.pi * p.r1 ** 2 * RHO_ROOF * k)
        rr = p.r1 * np.sqrt(rng.random(n))
        th = rng.random(n) * 2 * math.pi
        x, y = cx + rr * np.cos(th), cy + rr * np.sin(th)
        m = _roof_keep(p, b, x, y)
        acc.add(np.c_[x[m], y[m], np.full(int(m.sum()), p.z1)], np.tile([0.0, 0.0, 1.0], (int(m.sum()), 1)), C_ROOF)


def _sample_equipment(acc: _Acc, e: Part, k: float, rng: np.random.Generator) -> None:
    if e.kind == "cyl":
        tmp = Building([e], STYLES["punched"])
        _sample_cyl_part(acc, e, tmp, k, rng, rho=RHO_EQUIP, windows=False)
        return
    x0, y0, x1, y1 = e.rect
    h = e.z1 - e.z0
    for (ax, ay), (bx, by), (nx, ny) in (((x0, y0), (x1, y0), (0.0, -1.0)), ((x1, y0), (x1, y1), (1.0, 0.0)),
                                         ((x1, y1), (x0, y1), (0.0, 1.0)), ((x0, y1), (x0, y0), (-1.0, 0.0))):
        L = math.hypot(bx - ax, by - ay)
        n = round(L * h * RHO_EQUIP * k)
        t = rng.random(n)
        acc.add(np.c_[ax + (bx - ax) * t, ay + (by - ay) * t, e.z0 + rng.random(n) * h], np.tile([nx, ny, 0.0], (n, 1)),
                C_FACADE)
    n = round((x1 - x0) * (y1 - y0) * RHO_EQUIP * k)
    acc.add(np.c_[x0 + rng.random(n) * (x1 - x0), y0 + rng.random(n) * (y1 - y0), np.full(n, e.z1)],
            np.tile([0.0, 0.0, 1.0], (n, 1)), C_ROOF)


def _sample_trees(acc: _Acc, trees: list[Tree], k: float, rng: np.random.Generator) -> None:
    for t in trees:
        rv = t.r * (1.15 if t.cls == C_HIGHVEG else 0.8)                     # 冠体竖向半轴
        cz = t.z + t.trunk + rv
        n = round(4 * math.pi * t.r * t.r * RHO_CROWN * k)
        d = rng.normal(size=(n, 3))
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        rad = 0.72 + 0.28 * np.cbrt(rng.random(n))                            # 冠层厚度：外壳加内部稀疏点
        P = np.c_[t.x + d[:, 0] * t.r * rad, t.y + d[:, 1] * t.r * rad, cz + d[:, 2] * rv * rad]
        acc.add(P, d, t.cls)
        if t.trunk > 0.5:
            nt = round(2 * math.pi * 0.2 * (t.trunk + rv * 0.5) * RHO_TRUNK * k * 3)
            th = rng.random(nt) * 2 * math.pi
            zz = t.z + rng.random(nt) * (t.trunk + rv * 0.5)
            acc.add(np.c_[t.x + 0.2 * np.cos(th), t.y + 0.2 * np.sin(th), zz], np.c_[np.cos(th), np.sin(th), np.zeros(nt)],
                    C_HIGHVEG)


def _expected_points(plan: CityPlan, n_ground_cells: np.ndarray) -> float:
    """全局密度系数 k = 1 时的期望点数（窗格接受率已计入，k 由目标点数反推）。"""
    ground = float((n_ground_cells * GROUND_RHO).sum()) * RES * RES
    fac = roof = equip = 0.0
    for b in plan.buildings:
        for p in b.parts:
            fac += p.facade_area() * RHO_FACADE
            if p.roof:
                roof += p.footprint_area() * RHO_ROOF
        for e in b.equip:
            if e.kind == "box":
                x0, y0, x1, y1 = e.rect
                equip += (2 * ((x1 - x0) + (y1 - y0)) * (e.z1 - e.z0) + (x1 - x0) * (y1 - y0)) * RHO_EQUIP
            else:
                equip += (2 * math.pi * e.r0 * (e.z1 - e.z0) + math.pi * e.r0 ** 2) * RHO_EQUIP
    trees = sum(4 * math.pi * t.r * t.r * RHO_CROWN for t in plan.trees)
    return ground + fac + roof * 0.93 + equip + trees * 1.04


@dataclass
class SynthCloud:
    xyz: np.ndarray            # float64 (N, 3)，mm 舍入
    normal: np.ndarray         # float32 (N, 3)，单位向量
    cls: np.ndarray            # uint8 (N,)
    sha256: str
    nbytes: int
    stats: dict


def generate(spec: SynthSpec | None = None) -> SynthCloud:
    """按规格生成点云（确定性）。返回坐标、法线、类别与规范字节流的 sha256。"""
    spec = spec or SynthSpec()
    t0 = time.perf_counter()
    plan = _Planner(spec).run()
    t_plan = time.perf_counter() - t0
    H = plan.H
    lu = plan.landuse
    counts = np.bincount(lu.ravel().astype(np.int64), minlength=9)[:9]
    k = spec.target_points / max(_expected_points(plan, counts), 1.0)
    acc = _Acc()
    # ---- 地面：均匀候选点按用地码接受
    rng = np.random.default_rng([spec.seed, 2])
    rho_max = float(GROUND_RHO.max())
    n_cand = round((2 * H) ** 2 * rho_max * k)
    x = (rng.random(n_cand) * 2 - 1) * H
    y = (rng.random(n_cand) * 2 - 1) * H
    ix = np.minimum(((x + H) / RES).astype(np.int64), lu.shape[1] - 1)
    iy = np.minimum(((y + H) / RES).astype(np.int64), lu.shape[0] - 1)
    code = lu[iy, ix].astype(np.int64)
    keep = rng.random(n_cand) < GROUND_RHO[code] / rho_max
    x, y, code = x[keep], y[keep], code[keep]
    z, N = _ground_z(x, y, code, _PlanCtx(spec, H))
    acc.add(np.c_[x, y, z], N, GROUND_CLS[code])
    corners = np.array([[-H, -H, 0.15], [H, -H, 0.15], [H, H, 0.15], [-H, H, 0.15]])
    acc.add(corners, np.tile([0.0, 0.0, 1.0], (4, 1)), C_GROUND)
    n_ground = acc.count()
    # ---- 建筑
    for bi, b in enumerate(plan.buildings):
        brng = np.random.default_rng([spec.seed, 3, bi])
        for p in b.parts:
            if p.kind == "box":
                _sample_box_part(acc, p, b, k, brng)
            else:
                _sample_cyl_part(acc, p, b, k, brng, windows=p.r1 > 3.0)
        for e in b.equip:
            _sample_equipment(acc, e, k, brng)
    # 桅杆顶点：与四角地面点一样显式放置，使最高点（G-01 峰值、border 上限）与点数无关
    L = LANDMARKS["anet-tower"]
    cx, cy = (v * (spec.size_m / 1200.0) for v in L["center"])
    acc.add(np.array([[cx, cy, L["mast_z"]]]), np.array([[0.0, 0.0, 1.0]]), C_ROOF)
    n_build = acc.count() - n_ground
    # ---- 树与灌木
    _sample_trees(acc, plan.trees, k, np.random.default_rng([spec.seed, 4]))
    P = np.concatenate(acc.P)
    Nn = np.concatenate(acc.N)
    C = np.concatenate(acc.C)
    P[:, 0] = np.clip(P[:, 0], -H, H)
    P[:, 1] = np.clip(P[:, 1], -H, H)
    P = np.round(P, 3) + 0.0
    Nn = (Nn / np.linalg.norm(Nn, axis=1, keepdims=True)).astype(np.float32)
    h = hashlib.sha256(MAGIC)
    for blk in (np.ascontiguousarray(P, "<f8"), np.ascontiguousarray(Nn, "<f4"), np.ascontiguousarray(C, "u1")):
        h.update(memoryview(blk).cast("B"))
    nbytes = len(MAGIC) + P.size * 8 + Nn.size * 4 + C.size
    hist = {str(i): int(v) for i, v in enumerate(np.bincount(C, minlength=16)) if v}
    stats = {**plan.stats, "points": len(P), "ground_points": n_ground, "building_points": n_build,
             "vegetation_points": len(P) - n_ground - n_build, "density_scale": round(k, 6),
             "class_histogram": hist, "max_z_m": round(float(P[:, 2].max()), 3),
             "plan_s": round(t_plan, 3), "seconds": round(time.perf_counter() - t0, 3)}
    return SynthCloud(P, Nn, C, h.hexdigest(), nbytes, stats)


class _PlanCtx:
    """`_ground_z` 只需要缩放系数、半宽与河道中心线。"""

    def __init__(self, spec: SynthSpec, H: float):
        self.s = spec.size_m / 1200.0
        self.H = H

    def river_yc(self, x: np.ndarray | float) -> np.ndarray | float:
        return RIVER_Y0 * self.s + RIVER_AMP * np.sin(2 * np.pi * (np.asarray(x) + self.H) / (RIVER_LAMBDA * self.s))


# ================================================================ IngestAdapter


DATASET_URL = "https://github.com/ANetResearch/ANet-Drone4D"
LICENSE_TEXT = ("ANet Open Source License (modified Apache License 2.0), same as the ANet-Drone4D repository; generated "
                "content without third-party data, free to redistribute and to show in screenshots")


class SyntheticAdapter:
    """`IngestAdapter`：生成合成城市，走与六城相同的十步 ingest（类别由生成器给出，`semantic = provided`）。"""

    kind = "synthetic"

    def __init__(self, spec: SynthSpec | str = DEFAULT_ID):
        self.spec = synth_spec(spec) if isinstance(spec, str) else spec
        self.cloud: SynthCloud | None = None

    def config(self) -> IngestConfig:
        s = self.spec
        return IngestConfig(
            s.world_id, "simulation", 1.0, "+z", False, 0.0, "exact", "assumed", None, s.anchor,
            evidence=(f"procedurally generated by awr.world.ingest.synthetic v{s.version}, seed {s.seed}",
                      "metric by construction: 1 generator unit = 1 m"),
            north_evidence=("synthetic world: +Y is north by construction",),
            semantic="provided",
            anchor_label=f"illustrative: {s.name}, procedurally generated; no real location")

    def load(self) -> RawCloud:
        self.cloud = c = generate(self.spec)
        files = [{"name": self.spec.source_name, "bytes": int(c.nbytes), "points": len(c.xyz), "sha256": c.sha256,
                  "header_bytes": len(MAGIC)}]
        return RawCloud(xyz=c.xyz, normal=c.normal, class_index=c.cls, files=files)

    def provenance(self) -> dict:
        s = self.spec
        d = {"name": s.name, "version": f"synthcity generator v{s.version} (seed {s.seed})", "url": DATASET_URL,
             "citation": "ANet Drone4D synthetic demo city, procedurally generated by python/awr/world/ingest/synthetic.py",
             "license": LICENSE_TEXT, "redistribution": True,
             "notice": "No third-party data: every point is generated from the seed (ADR-077)."}
        if self.cloud is not None:
            d["sourceFiles"] = [{"name": s.source_name, "bytes": int(self.cloud.nbytes), "sha256": self.cloud.sha256}]
        return d

    def manifest_extras(self) -> dict:
        s = self.spec
        return {"name": s.name, "nameZh": s.name_zh,
                "description": "Procedurally generated demo city (about 1.2 km x 1.2 km): road grid, river, park and two "
                               "300 m class landmark towers; free to redistribute.",
                "tags": ["synthetic", "builtin", "generated", "redistributable"], "source_dataset": None,
                "default_color_mode": "height",
                "camera_home": {"position": [-560.0, -720.0, 430.0], "target": [10.0, 70.0, 110.0], "fovDeg": 50}}

    def build_params(self, base: Any) -> Any:
        from awr.world.package.params import with_synthetic

        return with_synthetic(base, self.spec.params())
