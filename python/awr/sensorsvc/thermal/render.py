"""热成像渲染（`therm512`；M25 §15.6 管线第 1–6 步、M25-FR-185 至 190；AWR-05 §5.4.10；ADR-180）。

- 参数表 `thermal_table_v1`（`configs/thermal_table_v1.yaml`）：静态类别的表面温度增量与发射率、动态实体（人员、犬类、车辆发动机与
  排气）、天空有效温度、大气消光与降雨湿润。版本号与 sha256（解析后规范 JSON 的摘要）写入 FrameMeta 与运行清单。
- `thermal_bake`：对静态外观网格逐三角形质心向事件起点太阳方向求交（Warp `mesh_query_ray`；无 Warp 时为 numpy 参照），输出
  `thermal_static.npz`（u8 日照可见度 0/255），文件字节确定（zip 成员时间固定），sha256 进入运行清单。
- `ThermalCamera`：与可见光共用 EGL 上下文（调用方给出的 `RasterCamera`，或自建只含上下文者），静态几何按瓦片合并上传，单独一遍 640 × 512
  MRT：表面温度、发射率与法向竖直分量（RGBA32F）、平面深度（R32F）、实例（R16UI）、语义（R8UI）。静态三角形的温度增量与发射率
  以 SSBO 按 `gl_PrimitiveID` 查表（批次内三角形序 → 外观三角形号 `tri_ids` → 逐三角形值），语义取世界包的 `tri.cls`（无时为 0）；
  演员为代理网格，分区（着装、皮肤、车身盒体）为逐顶点属性，发动机区与排气区在片元着色器按局部坐标划分。每个子帧一遍几何与一遍
  辐射换算（`L_sensor`，与 `radiometry.sensor_radiance` 同式），随后子帧加权、可分离高斯光学模糊、`B⁻¹`（波段 Planck 表的折线精确
  反函数）、固定图案噪声与 NETD 时间噪声（与 `noise.apply_noise` 同键同式，GLSL SplitMix64），量化为 u16 厘开尔文。演员与可见光
  相同以合批绘制（`camera.actorbuf`，每个子帧一次绘制，分区温度与发射率取自 SSBO）。`submit` 绘制并把 `temp_ck`、无噪表观温度
  （真值 `dT_app_k` 用）、实例、语义与只渲染演员的实例写入持久映射的读回槽位（`camera.readback`）后立即返回，`result()` 只等栅栏，
  数组为槽位上的只读视图；`render` 为同步包装（返回只读副本）。
- 交付与存档：`temp_ck` 为 u16 小端 + zstd 级别 1（交付字节即存档字节；D3-AC-11 吞吐，S20、F-54：级别 3 单帧 5.6–7.8 ms，级别 1
  约 3.7 ms，体积增加约 9%）；`thermal_frame` 组装帧记录（agent_view、ground_truth 含 `objects[].dT_app_k`、internal 含参数表与
  Planck 表版本）。

本实现设定（M25 未规定处，并入文档见 S13 实现记录）：
1. 表面温度与发射率附件取 RGBA32F（第三通道为法向竖直分量），不取 RG16F：f16 在 300 K 处的分辨率为 0.25 K，超出动态实体 0.2 K 的
   误差预算；静态法向由片元导数求平面法向并朝向相机，演员法向取代理网格顶点法向并朝向相机。
2. 子帧时刻：帧窗口为 [t_end − (S − 1)·τ_b, t_end]，S 个子帧等间隔（S = 4、τ_b = 10 ms 时窗口 30 ms，权重约 0.03、0.09、0.24、0.64）；
   读出为全局读出。真值层（实例、语义、无遮挡实例、平面深度）取最后一个子帧（t_end，权重最大）。全部子帧的机体位姿与演员状态
   相同时只绘制一次（权重之和为 1），与逐子帧绘制在数学上相同。
3. 车辆：车身盒体上局部 x ≥ 0.2·l 为发动机区，x ≤ −0.45·l 且 z ≤ 0.35·h 为排气区；熄火后两区以 τ = 600 s 指数回落到车身温度；
   `engine_on` 为行驶中或停车后 120 s 内，熄火时长 = 停车时长 − 120 s；车身日照项按无遮挡计。人员代理的颈与头为皮肤分区（33 °C）。
4. `dT_app_k`：实体可见像素无噪表观温度中位数减去其 3 px 环带（切比雪夫距离 ≤ 3，不含任何演员像素）的中位数；环带为空时不给出，
   并加标记 `no_ring`。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import threading
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from awr.sensorsvc.camera.actors import proxy_mesh, sun_dir, visibility_objects
from awr.sensorsvc.camera.model import CameraModel
from awr.sensorsvc.thermal import noise as tn
from awr.sensorsvc.thermal.radiometry import (
    BAND_UM,
    N_TABLE,
    T_MAX_K,
    T_MIN_K,
    T_STEP_K,
    PlanckTable,
    ThermalEnv,
    planck_table,
)

__all__ = ["MAX_SUBFRAMES", "TEMP_CK_ENCODER", "THERM512", "THERMAL_RENDERER_VERSION", "ActorThermal", "NoiseKeys", "StaticRow", "ThermalCamera",
           "ThermalJob", "ThermalModel", "ThermalOut", "ThermalStatic", "ThermalTable", "actor_thermal", "actor_zones", "decode_temp_ck",
           "encode_temp_ck", "engine_off_s", "last_moving_time", "load_thermal_table", "scene_triangles", "static_classes",
           "static_surface", "subframe_times_s", "thermal_bake", "thermal_env", "thermal_frame", "thermal_manifest",
           "thermal_objects", "thermal_table_from_doc", "triangle_sun_cos"]

THERMAL_RENDERER_VERSION = "thermal-gl@2"
THERMAL_TABLE_SCHEMA = "awr.thermal_table.v1"
DEFAULT_TABLE = Path(__file__).resolve().parents[4] / "configs" / "thermal_table_v1.yaml"
MAX_SUBFRAMES = 4                              # 着色器的子帧纹理数（M25 §15.6 S = 4）
MAX_BLUR_R = 7                                 # 模糊核半径上限（sigma ≤ 2.33 px）
ZSTD_LEVEL = 3                                 # 真值层（实例、语义），与 `sensorsvc.frame` 相同
TEMP_CK_ZSTD_LEVEL = 1                         # 交付的 `temp_ck`（D3-AC-11 吞吐；单线程、字节确定）
TEMP_CK_ENCODER = f"zstd-{TEMP_CK_ZSTD_LEVEL}/u16le"
SEM_NONE = 255
ZONE_BASE, ZONE_SKIN, ZONE_BODY = 0, 1, 2      # 顶点分区；片元着色器把车身盒体再分为 0 车身、2 发动机、3 排气
KELVIN = 273.15
_FAR = 1.0e30
_NPZ_DATE = (1980, 1, 1, 0, 0, 0)


# ---------------------------------------------------------------- 相机模型

@dataclass(frozen=True)
class ThermalModel:
    """热成像相机模型（M25 §15.6 数据模型；`therm0` 取机型 `lwir` 槽位，标准轨 `lwir640` 13 mm F1.0，12 µm 像元）。

    噪声参数缺省取 `noise` 档（`thermal.noise.PROFILES`，数值以 AWR-20 §15.6.5 为单一真源）；显式给出的字段覆盖档位值。"""

    width: int = 640
    height: int = 512
    fx: float = 13.0 / 0.012
    fy: float = 13.0 / 0.012
    cx: float = 320.0
    cy: float = 256.0
    band_um: tuple[float, float] = BAND_UM
    fps: str = "10"
    noise: str = "realistic"
    tau_bolometer_s: float | None = None        # 微测辐射热计时间常数
    lag_subframes: int = 4                      # 帧窗口内加权子帧数
    optics_sigma_px: float | None = None
    netd_mk_300k: float | None = None           # 标称 NETD（300 K，F1.0）
    fpn_offset_k: float | None = None
    fpn_gain_frac: float | None = None
    quant_k: float = 0.01                       # u16 输出量化步长（0.01 K/LSB）
    codec: str = "radiometric_u16"
    near_m: float = 0.3
    far_m: float = 3000.0

    def __post_init__(self) -> None:
        if not 1 <= self.lag_subframes <= MAX_SUBFRAMES:
            raise ValueError(f"lag_subframes must be 1..{MAX_SUBFRAMES}")
        if self.quant_k != 0.01:
            raise ValueError("only quant_k = 0.01 K (u16 centikelvin) is defined")
        if self.codec not in ("radiometric_u16", "agc_jpeg"):
            raise ValueError(f"unknown thermal codec {self.codec!r}")
        tn.profile(self.noise)
        if math.ceil(3.0 * self.noise_profile().optics_sigma_px) > MAX_BLUR_R:
            raise ValueError("optics_sigma_px too large for the blur pass")

    def noise_profile(self) -> tn.ThermalNoiseProfile:
        p = tn.profile(self.noise)

        def pick(v, d):
            return float(d if v is None else v)
        return tn.ThermalNoiseProfile(p.name, pick(self.netd_mk_300k, p.netd_mk_300k), pick(self.fpn_offset_k, p.fpn_offset_k),
                                      pick(self.fpn_gain_frac, p.fpn_gain_frac), pick(self.optics_sigma_px, p.optics_sigma_px),
                                      pick(self.tau_bolometer_s, p.tau_bolometer_s))

    def camera(self) -> CameraModel:
        """几何用相机模型（全局读出、无畸变）：投影、视锥剔除与斜距换算。"""
        return CameraModel(self.width, self.height, self.fx, self.fy, self.cx, self.cy, near_m=self.near_m, far_m=self.far_m, fps=self.fps)

    def K(self) -> list[float]:
        return [self.fx, 0.0, self.cx, 0.0, self.fy, self.cy, 0.0, 0.0, 1.0]


THERM512 = ThermalModel()                       # `therm512` 档：640 × 512，HFOV 32.9°、VFOV 26.6°


def subframe_times_s(t_end_s: float, model: ThermalModel) -> np.ndarray:
    """子帧时刻（s，float64，升序，末项 = t_end）：t_j = t_end − (S − 1 − j)·τ_b。"""
    s = model.lag_subframes
    tau = model.noise_profile().tau_bolometer_s
    return float(t_end_s) - (s - 1 - np.arange(s, dtype=np.float64)) * tau


# ---------------------------------------------------------------- 参数表 thermal_table_v1

@dataclass(frozen=True)
class StaticRow:
    name: str
    codes: tuple[int, ...]
    dT_base: float
    dT_sun: float
    dT_night: float
    dT_wet: float
    eps: float
    sun_incidence: bool = False


_STATIC_KEYS = {"codes", "dT_base", "dT_sun", "dT_night", "dT_wet", "eps", "sun_incidence"}
_ACTOR_KEYS = {"blend": {"model", "t_ref_c", "k", "eps", "skin_c", "skin_eps"},
               "body": {"model", "dT_body", "dT_body_sun", "eps", "dT_engine", "dT_exhaust", "engine_on_after_stop_s", "cool_tau_s",
                        "engine_x_frac", "exhaust_x_frac", "exhaust_z_frac"}}
_ENGINE_KEYS = ("dT_engine", "dT_exhaust", "engine_on_after_stop_s", "cool_tau_s", "engine_x_frac", "exhaust_x_frac", "exhaust_z_frac")
_TOP_KEYS = {"schema", "version", "confidence", "static", "actors", "sky", "extinction_per_km", "wet"}


@dataclass(frozen=True, eq=False)
class ThermalTable:
    """解析后的参数表；`sha256` 为规范 JSON（键排序、紧凑分隔、UTF-8）的摘要，与 YAML 注释与排版无关。"""

    version: str
    sha256: str
    doc: dict
    rows: tuple[StaticRow, ...]
    code_row: np.ndarray                       # (256,) int16，d3-semantic@1 码 → rows 下标
    actors: dict
    sky: dict
    ext: dict
    wet: dict

    def row_of(self, code: int) -> StaticRow:
        return self.rows[int(self.code_row[int(code)])]

    def sky_k(self, t_air_k: float, cloud: float) -> float:
        c = min(1.0, max(0.0, float(cloud)))
        return float(t_air_k) + self.sky["dT_clear"] * (1.0 - self.sky["cloud_factor"] * c)

    def beta_per_km(self, rh: float, fog_visibility_km: float | None = None, rain_mmh: float = 0.0) -> float:
        e = self.ext
        b = e["clear_a"] + e["clear_rh"] * min(1.0, max(0.0, float(rh)))
        if fog_visibility_km is not None:
            if fog_visibility_km <= 0:
                raise ValueError(f"fog visibility must be positive, got {fog_visibility_km!r}")
            b += e["fog_k"] * e["fog_c"] / float(fog_visibility_km)
        if rain_mmh > 0:
            b += e["rain_a"] * float(rain_mmh) ** e["rain_exp"]
        return b

    def is_wet(self, rain_mmh: float) -> bool:
        return float(rain_mmh) >= self.wet["rain_mmh"]

    def wet_eps(self, eps, rain_mmh: float) -> np.ndarray:
        e = np.asarray(eps, np.float64)
        return np.minimum(e + self.wet["eps_add"], self.wet["eps_max"]) if self.is_wet(rain_mmh) else e


def _num(d: dict, k: str, where: str) -> float:
    v = d.get(k)
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise ValueError(f"{where}.{k}: expected a finite number, got {v!r}")
    return float(v)


def thermal_table_from_doc(doc: dict) -> ThermalTable:
    """校验并构建参数表（未知键、缺键、类别码重复或越界一律 ValueError）。"""
    if not isinstance(doc, dict) or set(doc) - _TOP_KEYS or _TOP_KEYS - {"confidence"} - set(doc):
        raise ValueError(f"thermal table keys must be {sorted(_TOP_KEYS)}")
    if doc["schema"] != THERMAL_TABLE_SCHEMA:
        raise ValueError(f"schema must be {THERMAL_TABLE_SCHEMA}")
    if not isinstance(doc["version"], str) or not doc["version"]:
        raise ValueError("version must be a non-empty string")
    rows: list[StaticRow] = []
    code_row = np.full(256, -1, np.int16)
    if "default" not in doc["static"]:
        raise ValueError("static.default row is required")
    for name, r in doc["static"].items():
        where = f"static.{name}"
        if set(r) - _STATIC_KEYS or {"codes", "dT_base", "dT_sun", "dT_night", "dT_wet", "eps"} - set(r):
            raise ValueError(f"{where}: keys must be {sorted(_STATIC_KEYS)}")
        eps = _num(r, "eps", where)
        if not 0.0 < eps <= 1.0:
            raise ValueError(f"{where}.eps out of (0, 1]")
        codes = tuple(int(c) for c in r["codes"])
        for c in codes:
            if not 0 <= c < 255 or code_row[c] >= 0:
                raise ValueError(f"{where}: code {c} out of range or listed twice")
            code_row[c] = len(rows)
        if name == "default" and codes:
            raise ValueError("static.default must not list codes")
        rows.append(StaticRow(name, codes, _num(r, "dT_base", where), _num(r, "dT_sun", where), _num(r, "dT_night", where),
                              _num(r, "dT_wet", where), eps, bool(r.get("sun_incidence", False))))
    d_idx = next(i for i, r in enumerate(rows) if r.name == "default")
    code_row[code_row < 0] = d_idx
    actors = {}
    for name, a in doc["actors"].items():
        m = a.get("model")
        if m not in _ACTOR_KEYS or set(a) - _ACTOR_KEYS[m]:
            raise ValueError(f"actors.{name}: model must be blend or body with keys {sorted(_ACTOR_KEYS.get(m, set()))}")
        out = {"model": m}
        for k in _ACTOR_KEYS[m] - {"model"}:
            if k in a and a[k] is not None:
                out[k] = _num(a, k, f"actors.{name}")
        need = {"t_ref_c", "k", "eps"} if m == "blend" else {"dT_body", "dT_body_sun", "eps"}
        if need - set(out):
            raise ValueError(f"actors.{name}: missing {sorted(need - set(out))}")
        if m == "body" and any(k in out for k in _ENGINE_KEYS) and not all(k in out for k in _ENGINE_KEYS):
            raise ValueError(f"actors.{name}: engine parameters must be given together: {_ENGINE_KEYS}")
        actors[name] = out
    sky = {k: _num(doc["sky"], k, "sky") for k in ("dT_clear", "cloud_factor")}
    ext = {k: _num(doc["extinction_per_km"], k, "extinction_per_km") for k in ("clear_a", "clear_rh", "fog_k", "fog_c", "rain_a", "rain_exp")}
    wet = {k: _num(doc["wet"], k, "wet") for k in ("rain_mmh", "eps_add", "eps_max")}
    if set(doc["sky"]) - set(sky) or set(doc["extinction_per_km"]) - set(ext) or set(doc["wet"]) - set(wet):
        raise ValueError("unknown keys in sky / extinction_per_km / wet")
    canon = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    code_row.setflags(write=False)
    return ThermalTable(doc["version"], hashlib.sha256(canon).hexdigest(), doc, tuple(rows), code_row, actors, sky, ext, wet)


@lru_cache(maxsize=4)
def _load_table(path: str) -> ThermalTable:
    import yaml

    return thermal_table_from_doc(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def load_thermal_table(path: Path | str | None = None) -> ThermalTable:
    """读取参数表（缺省 `configs/thermal_table_v1.yaml`，环境变量 `AWR_THERMAL_TABLE` 可改路径）。"""
    p = path or os.environ.get("AWR_THERMAL_TABLE") or DEFAULT_TABLE
    return _load_table(str(Path(p).resolve()))


def thermal_env(table: ThermalTable, t_air_k: float, *, cloud: float = 0.0, rh: float = 0.5, fog_visibility_km: float | None = None,
                rain_mmh: float = 0.0) -> ThermalEnv:
    """由参数表构建一帧的环境量：天空有效温度按表计算并显式写入（`ThermalEnv.t_sky_k`），消光系数由表给出（`table.beta_per_km`，
    与 `radiometry.extinction_beta_per_km` 同式同数，测试交叉校验）。"""
    return ThermalEnv(float(t_air_k), float(cloud), float(rh), fog_visibility_km, float(rain_mmh), table.sky_k(t_air_k, cloud))


def _sin_el(sun_az_el_deg: tuple[float, float] | None) -> float:
    return float(sun_dir(sun_az_el_deg)[2])


# ---------------------------------------------------------------- 静态表面温度

def static_surface(table: ThermalTable, tri_cls: np.ndarray, sun_vis_u8: np.ndarray | None, cos_inc: np.ndarray | None, env: ThermalEnv,
                   sun_az_el_deg: tuple[float, float] | None) -> tuple[np.ndarray, np.ndarray]:
    """逐三角形表面温度增量（相对 T_air，K）与发射率（均 float32）：
    `dT = dT_base + s·dT_sun·max(0, sin h)·[cos_i] + (1 − cloud)·dT_night·[h < 0] − wet·dT_wet`，发射率降雨时按表加湿润增量。"""
    cls = np.asarray(tri_cls, np.uint8)
    n = cls.size
    ridx = table.code_row[cls]
    col = {k: np.array([getattr(r, k) for r in table.rows], np.float64)[ridx] for k in ("dT_base", "dT_sun", "dT_night", "dT_wet", "eps")}
    inc = np.array([r.sun_incidence for r in table.rows], bool)[ridx]
    s = np.zeros(n) if sun_vis_u8 is None else np.asarray(sun_vis_u8, np.float64) / 255.0
    sin_h = _sin_el(sun_az_el_deg)
    c = np.ones(n) if cos_inc is None else np.asarray(cos_inc, np.float64)
    sun = s * col["dT_sun"] * max(0.0, sin_h) * np.where(inc, c, 1.0)
    night = (1.0 - min(1.0, max(0.0, env.cloud))) * col["dT_night"] * (1.0 if sin_h < 0 else 0.0)
    wet = 1.0 if table.is_wet(env.rain_mmh) else 0.0
    dT = col["dT_base"] + sun + night - wet * col["dT_wet"]
    return dT.astype(np.float32), table.wet_eps(col["eps"], env.rain_mmh).astype(np.float32)


def scene_triangles(scene) -> np.ndarray:
    """外观场景 → 世界 ENU 三角形 (n, 3, 3) float64，按外观三角形号排列（批次的 `tri_ids`）。"""
    T = np.zeros((scene.triangles, 3, 3), np.float64)
    for b in scene.batches:
        T[b.tri_ids.astype(np.int64)] = b.pos.astype(np.float64)[b.idx.astype(np.int64)] + b.origin
    return T


def triangle_sun_cos(tris: np.ndarray, sun_az_el_deg: tuple[float, float] | None) -> np.ndarray:
    """入射余弦 |n·sun|（双面网格，float64；退化三角形为 0）。"""
    T = np.asarray(tris, np.float64)
    n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    ln = np.linalg.norm(n, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        c = np.abs(n @ sun_dir(sun_az_el_deg)) / ln
    return np.where(ln > 0, c, 0.0)


def _stream_of(index_json: Path, name: str) -> np.ndarray | None:
    if not index_json.exists():
        return None
    doc = json.loads(index_json.read_text(encoding="utf-8"))
    for s in doc.get("streams", []):
        if s.get("name") == name:
            dt = {"uint8": "<u1", "uint16": "<u2", "uint32": "<u4"}[s["dtype"]]
            return np.fromfile(index_json.parent / s["href"], dt)
    return None


def static_classes(world_dir: Path | str, n_triangles: int) -> np.ndarray:
    """逐外观三角形 d3-semantic@1 静态类码（u8）：外观目录的 `tri.cls` 流；没有时由求交网格的 `cls` 与 `src`（外观三角形号）回填；
    都没有时为 0（unclassified，参数表 default 行）。"""
    wd = Path(world_dir)
    a = _stream_of(wd / "visual/mesh/appearance/index.json", "tri.cls")
    if a is not None:
        if a.size != n_triangles:
            raise ValueError(f"tri.cls has {a.size} values for {n_triangles} appearance triangles")
        return a.astype(np.uint8)
    out = np.zeros(n_triangles, np.uint8)
    rc = wd / "geometry/mesh/raycast/index.json"
    cls, src = _stream_of(rc, "cls"), _stream_of(rc, "src")
    if cls is not None and src is not None and cls.size == src.size:
        ok = src < n_triangles
        out[src[ok].astype(np.int64)] = cls[ok]
    return out


# ---------------------------------------------------------------- 动态实体

@dataclass(frozen=True)
class ActorThermal:
    """一名演员一帧的分区温度（K）与发射率：分区 0 着装/车身/皮毛、1 皮肤、2 发动机、3 排气；分区几何为局部帧（m）。"""

    zone_t_k: tuple[float, float, float, float]
    zone_eps: tuple[float, float, float, float]
    engine_x_m: float = _FAR
    exhaust_x_m: float = -_FAR
    exhaust_z_m: float = -_FAR


def engine_off_s(t_s: float, t_last_moving_s: float | None, table: ThermalTable, row: str = "vehicle") -> float | None:
    """熄火时长（s）：行驶中或停车后 `engine_on_after_stop_s` 内为 None（engine_on）；从未行驶为 +inf（冷车）。"""
    if t_last_moving_s is None:
        return math.inf
    dt = float(t_s) - float(t_last_moving_s)
    on = table.actors[row].get("engine_on_after_stop_s", 0.0)
    return None if dt <= on else dt - on


def last_moving_time(ts: Sequence[float], speeds: Sequence[float], t_s: float, *, v_eps_mps: float = 0.2) -> float | None:
    """磁带采样（时刻升序、速度 m/s）中 t_s 之前最后一次速度 > v_eps 的时刻；若 t_s 时仍在行驶返回 t_s；从未行驶返回 None。"""
    t = np.asarray(ts, np.float64)
    v = np.asarray(speeds, np.float64)
    k = int(np.searchsorted(t, float(t_s), side="right"))
    mv = np.flatnonzero(v[:k] > v_eps_mps)
    if mv.size == 0:
        return None
    return float(t_s) if mv[-1] == k - 1 else float(t[mv[-1]])


def actor_thermal(actor: dict, table: ThermalTable, env: ThermalEnv, sun_az_el_deg: tuple[float, float] | None, *,
                  engine_off: float | None = math.inf) -> ActorThermal:
    """磁带演员表一行 → 分区温度与发射率（`thermal_table_v1` 与 DSL `actors[].thermal`）。`engine_off` 为熄火时长（s，
    `engine_off_s`）：None 为 engine_on，+inf 为冷车；只对带发动机参数的类别生效。"""
    th = actor.get("thermal") or {}
    if set(th) - {"surface_temp_c", "emissivity", "profile"}:
        raise ValueError(f"{actor.get('id')}: unknown thermal keys {sorted(set(th) - {'surface_temp_c', 'emissivity', 'profile'})}")
    key = th.get("profile") or actor["class"]
    if key not in table.actors:
        raise ValueError(f"{actor.get('id')}: no thermal_table row {key!r}")
    r = table.actors[key]
    t_air = float(env.t_air_k)
    ln, _w, h = (s / 1000.0 for s in actor["size_mm"])
    geo = (_FAR, -_FAR, -_FAR)
    if r["model"] == "blend":
        base = t_air + r["k"] * (r["t_ref_c"] + KELVIN - t_air)
        skin = r["skin_c"] + KELVIN if "skin_c" in r else base
        eps = (r["eps"], r.get("skin_eps", r["eps"]))
        eng = exh = base
    else:
        base = t_air + r["dT_body"] + r["dT_body_sun"] * max(0.0, _sin_el(sun_az_el_deg))
        skin = base
        eps = (r["eps"], r["eps"])
        eng = exh = None
        if "dT_engine" in r:
            geo = (r["engine_x_frac"] * ln, r["exhaust_x_frac"] * ln, r["exhaust_z_frac"] * h)
    if th.get("surface_temp_c") is not None:
        base = float(th["surface_temp_c"]) + KELVIN
        if r["model"] == "blend" and "skin_c" not in r:
            skin = base
    if r["model"] == "body":
        if "dT_engine" in r:
            on_e, on_x = t_air + r["dT_engine"], t_air + r["dT_exhaust"]
            if engine_off is None:
                eng, exh = on_e, on_x
            else:
                f = math.exp(-float(engine_off) / r["cool_tau_s"]) if math.isfinite(engine_off) else 0.0
                eng, exh = base + (on_e - base) * f, base + (on_x - base) * f
        else:
            eng = exh = base
    if th.get("emissivity") is not None:
        e = float(th["emissivity"])
        if not 0.0 < e <= 1.0:
            raise ValueError(f"{actor.get('id')}: emissivity out of (0, 1]")
        eps = (e, e)
    e0, e1 = (float(v) for v in table.wet_eps(np.array(eps), env.rain_mmh))
    return ActorThermal((base, skin, eng, exh), (e0, e1, e0, e0), *geo)


# 代理网格部件的顶点数（`camera.actors.proxy_mesh`：n 棱柱 12n 个顶点，盒体 n = 4）与分区
_PART_LAYOUT = {
    "person": ((96, ZONE_BASE), (96, ZONE_BASE), (96, ZONE_BASE), (72, ZONE_SKIN), (96, ZONE_SKIN)),   # 两腿、躯干、颈、头
    "vehicle": ((48, ZONE_BODY), (48, ZONE_BASE), (48, ZONE_BASE), (192, ZONE_BASE)),                # 车身盒体、座舱、车顶、四轮
    "dog": ((288, ZONE_BASE),),
}


def actor_zones(actor: dict) -> np.ndarray:
    """代理网格逐顶点分区（float32：0 着装/车身、1 皮肤、2 车身盒体，片元着色器再分出发动机与排气）。机器与物体整个盒体为 2；
    部件布局与 `proxy_mesh` 不符时（代理几何改版）全部为 0。"""
    n = len(proxy_mesh(actor)[0])
    lay = _PART_LAYOUT.get(actor["class"], ((48, ZONE_BODY),))
    if sum(k for k, _ in lay) != n:
        return np.zeros(n, np.float32)
    return np.concatenate([np.full(k, z, np.float32) for k, z in lay])


# ---------------------------------------------------------------- thermal_bake

@dataclass(frozen=True, eq=False)
class ThermalStatic:
    """`thermal_static.npz`：逐外观三角形日照可见度（u8，0 遮挡、255 可见）与太阳方向；`sha256` 为文件字节摘要。"""

    sun_vis: np.ndarray
    sun_az_el_deg: tuple[float, float]
    backend: str
    npz: bytes
    sha256: str

    def write(self, path: Path | str) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.npz)
        return p

    @staticmethod
    def read(path: Path | str) -> ThermalStatic:
        b = Path(path).read_bytes()
        with np.load(io.BytesIO(b)) as z:
            vis = z["sun_vis"].astype(np.uint8)
            az, el = (float(v) for v in z["sun_az_el_deg"])
            backend = bytes(z["backend"]).decode()
        return ThermalStatic(vis, (az, el), backend, b, hashlib.sha256(b).hexdigest())


def _npz_bytes(arrays: dict[str, np.ndarray]) -> bytes:
    """确定性 npz：成员按给定顺序、zip 时间固定 1980-01-01、不压缩（numpy.savez 以当前时间写成员头，字节不确定）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for k, a in arrays.items():
            ab = io.BytesIO()
            np.lib.format.write_array(ab, np.ascontiguousarray(a), allow_pickle=False)
            zi = zipfile.ZipInfo(f"{k}.npy", date_time=_NPZ_DATE)
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, ab.getvalue())
    return buf.getvalue()


def _hits_numpy(T: np.ndarray, org: np.ndarray, d: np.ndarray, max_t: float) -> np.ndarray:
    """Möller–Trumbore 逐射线 × 全部三角形（float64，分块；只用于小网格与测试）。"""
    v0, e1, e2 = T[:, 0], T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]
    p = np.cross(d, e2)
    det = np.einsum("nk,nk->n", e1, p)
    ok = np.abs(det) > 1e-12
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    out = np.zeros(len(org), bool)
    step = max(1, 1_000_000 // max(len(T), 1))
    for s in range(0, len(org), step):
        o = org[s:s + step]
        tv = o[:, None, :] - v0[None]
        u = np.einsum("rnk,nk->rn", tv, p) * inv
        q = np.cross(tv, e1[None])
        v = (q @ d) * inv
        t = np.einsum("rnk,nk->rn", q, e2) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0) & (t <= max_t)
        out[s:s + step] = hit.any(1)
    return out


@lru_cache(maxsize=1)
def _sunvis_kernel():
    import warp as wp

    @wp.kernel
    def sunvis(mesh: wp.uint64, org: wp.array(dtype=wp.vec3), d: wp.vec3, max_t: float, out: wp.array(dtype=wp.uint8)):
        tid = wp.tid()
        q = wp.mesh_query_ray(mesh, org[tid], d, max_t)
        if q.result:
            out[tid] = wp.uint8(0)
        else:
            out[tid] = wp.uint8(255)

    return sunvis


def _hits_warp(T: np.ndarray, org: np.ndarray, d: np.ndarray, max_t: float, device: str | None) -> tuple[np.ndarray, str]:
    import warp as wp

    wp.config.quiet = True
    wp.init()
    dev = device or ("cuda:0" if wp.get_cuda_device_count() > 0 else "cpu")
    c = T.reshape(-1, 3).mean(0)                                         # 局部原点：三角形顶点均值（float64 减后转 float32）
    pts = wp.array((T.reshape(-1, 3) - c).astype(np.float32), dtype=wp.vec3, device=dev)
    idx = wp.array(np.arange(len(T) * 3, dtype=np.int32), dtype=int, device=dev)
    mesh = wp.Mesh(points=pts, indices=idx)
    out = wp.zeros(len(org), dtype=wp.uint8, device=dev)
    wp.launch(_sunvis_kernel(), dim=len(org), device=dev,
              inputs=[mesh.id, wp.array((org - c).astype(np.float32), dtype=wp.vec3, device=dev), wp.vec3(*[float(v) for v in d]),
                      float(max_t), out])
    wp.synchronize_device(dev)
    return out.numpy() == 0, f"warp-{wp.config.version}/{'cuda' if dev.startswith('cuda') else 'cpu'}"


def thermal_bake(scene_or_tris, sun_az_el_deg: tuple[float, float], *, backend: str = "auto", device: str | None = None,
                 offset_m: float = 0.02, max_t_m: float = 5000.0) -> ThermalStatic:
    """逐三角形质心向太阳方向求交（射线起点沿太阳方向外移 `offset_m` 以避开自身）：未命中为 255，命中为 0；太阳在地平线下时全部为 0。

    `scene_or_tris` 为 `AppearanceScene` 或世界帧三角形 (n, 3, 3)。`backend`：warp（GPU，无 CUDA 时为 Warp CPU 设备）、numpy
    （小网格参照）或 auto（warp 可导入即用）。同镜像同型号 GPU 两次运行逐字节一致（命中判定为布尔，无浮点归约）。"""
    T = np.asarray(scene_or_tris if isinstance(scene_or_tris, np.ndarray) else scene_triangles(scene_or_tris), np.float64)
    az, el = (float(v) for v in sun_az_el_deg)
    d = sun_dir((az, el))
    org = T.mean(1) + d * float(offset_m)
    if backend == "auto":
        try:
            import warp  # noqa: F401
            backend = "warp"
        except ImportError:
            backend = "numpy"
    if el <= 0.0 or len(T) == 0:
        hit, tag = np.ones(len(T), bool), "none"
    elif backend == "warp":
        hit, tag = _hits_warp(T, org, d, max_t_m, device)
    elif backend == "numpy":
        hit, tag = _hits_numpy(T, org, d, max_t_m), "numpy"
    else:
        raise ValueError(f"unknown thermal_bake backend {backend!r}")
    vis = np.where(hit, 0, 255).astype(np.uint8)
    b = _npz_bytes({"sun_vis": vis, "sun_az_el_deg": np.array([az, el], "<f8"), "backend": np.frombuffer(tag.encode(), np.uint8)})
    vis.setflags(write=False)
    return ThermalStatic(vis, (az, el), tag, b, hashlib.sha256(b).hexdigest())


# ---------------------------------------------------------------- 编码、真值与帧记录

_tls = threading.local()


def _compressor(level: int):
    d = getattr(_tls, "zc", None)
    if d is None:
        d = _tls.zc = {}
    c = d.get(level)
    if c is None:
        import zstandard

        c = d[level] = zstandard.ZstdCompressor(level=level)
    return c


def encode_temp_ck(temp_ck: np.ndarray) -> bytes:
    """u16 厘开尔文 → 小端字节 + zstd（单线程、级别 1，字节确定；压缩器按线程复用）；交付字节即存档字节。"""
    a = np.ascontiguousarray(np.asarray(temp_ck, np.uint16).astype("<u2", copy=False))
    return _compressor(TEMP_CK_ZSTD_LEVEL).compress(memoryview(a).cast("B"))


def decode_temp_ck(data: bytes, width: int, height: int) -> np.ndarray:
    import zstandard

    raw = zstandard.ZstdDecompressor().decompress(data, max_output_size=width * height * 2)
    if len(raw) != width * height * 2:
        raise ValueError(f"temp_ck payload has {len(raw)} bytes, expected {width * height * 2}")
    return np.frombuffer(raw, "<u2").reshape(height, width).astype(np.uint16)


def _ring(mask: np.ndarray, r: int) -> np.ndarray:
    """切比雪夫距离 ≤ r 的膨胀（布尔，可分离最大值滤波）。"""
    h, w = mask.shape
    p = np.pad(mask, r)
    a = np.zeros((h + 2 * r, w), bool)
    for k in range(2 * r + 1):
        a |= p[:, k:k + w]
    out = np.zeros((h, w), bool)
    for k in range(2 * r + 1):
        out |= a[k:k + h]
    return out


def thermal_objects(t_app_k: np.ndarray, instance: np.ndarray, instance_unocc: np.ndarray, semantic: np.ndarray, actors: list[dict],
                    invalid: np.ndarray | None = None, *, ring_px: int = 3, stats: np.ndarray | None = None) -> list[dict]:
    """可见度归约（`camera.actors.visibility_objects`）加 `dT_app_k`：可见像素无噪表观温度中位数减 `ring_px` 环带中位数（K，6 位小数）。
    `stats` 为 GPU 可见度统计（`ThermalOut.vis`，无 `invalid` 时使用）。"""
    objs = visibility_objects(instance, instance_unocc, semantic, invalid, actors, stats=stats if invalid is None else None)
    H, W = instance.shape
    t = np.asarray(t_app_k, np.float64)
    bg = instance == 0 if invalid is None else (instance == 0) & (invalid == 0)
    for o in objs:
        x, y, w, h = o["bbox_xywh"]
        if w == 0 or o["px_visible"] == 0:
            o["flags"] = [*o["flags"], "no_ring"]
            continue
        x0, y0, x1, y1 = max(x - ring_px, 0), max(y - ring_px, 0), min(x + w + ring_px, W), min(y + h + ring_px, H)
        m = instance[y0:y1, x0:x1] == o["inst"]
        ring = _ring(m, ring_px) & bg[y0:y1, x0:x1]
        if not ring.any():
            o["flags"] = [*o["flags"], "no_ring"]
            continue
        sub = t[y0:y1, x0:x1]
        o["dT_app_k"] = round(float(np.median(sub[m]) - np.median(sub[ring])), 6)
    return objs


@dataclass(frozen=True)
class NoiseKeys:
    """探测器噪声键（流 22）：时间噪声 (run_seed, 22, uav_no, frame_index, therm_temporal + sensor_no)，
    固定图案噪声 (run_seed, 22, uav_no, 0, therm_fpn + sensor_no)。"""

    seed: int
    uav_no: int
    sensor_no: int
    frame_index: int


def thermal_manifest(table: ThermalTable, planck: PlanckTable, model: ThermalModel, static: ThermalStatic | None = None) -> dict:
    """参数表、Planck 表、噪声档与 `thermal_static.npz` 的版本与摘要（FrameMeta `internal.thermal` 与运行清单，M25-FR-186、187）。"""
    p = model.noise_profile()
    d = {"renderer": THERMAL_RENDERER_VERSION, "thermal_table": {"version": table.version, "sha256": table.sha256},
         "planck_table": {"version": planck.version, "sha256": planck.sha256, "band_um": list(planck.band_um)},
         "noise": {"version": tn.THERMAL_NOISE_VERSION, "profile": p.name, "netd_mk_300k": p.netd_mk_300k, "fpn_offset_k": p.fpn_offset_k,
                   "fpn_gain_frac": p.fpn_gain_frac, "optics_sigma_px": p.optics_sigma_px, "tau_bolometer_s": p.tau_bolometer_s,
                   "lag_subframes": model.lag_subframes}}
    if static is not None:
        d["thermal_static"] = {"sha256": static.sha256, "sun_az_el_deg": list(static.sun_az_el_deg), "backend": static.backend}
    return d


def _gimbal(g: tuple[float, float, float]) -> dict:
    d = {"az_deg": round(math.degrees(g[0]), 6), "el_deg": round(math.degrees(g[1]), 6)}
    if g[2] > 0:
        d["zoom_f_mm"] = g[2]
    return d


class _Now:
    """已完成的结果（`thermal_frame` 无辅助池时与 Future 同一接口）。"""

    __slots__ = ("v",)

    def __init__(self, v) -> None:
        self.v = v

    def result(self):
        return self.v


def thermal_frame(ctx, *, uav: str, sensor_id: str, seq: int, t_mid_ns: int, model: ThermalModel, mount, gimbal_reading, gimbal_true,
                  pos_enu_m, R_ws, out: ThermalOut, manifest: dict, actors: list[dict] | None = None,
                  ref_prefix: str = "", aux=None) -> tuple[dict, dict[str, bytes]]:
    """组装一帧热成像记录（`drone4d.frame.v1` 的热成像分区，`kind = thermal`）与载荷块 {ref: 字节}。

    `ctx` 为 `sensorsvc.frame.FrameContext`；`manifest` 为 `thermal_manifest(...)`。agent_view 的 `temp_ck` 为 u16 + zstd，sha256 取
    交付字节，载荷块存同一 `bytes` 对象（交付字节即存档字节）；ground_truth 为实例、无遮挡实例、语义（zstd）与 `objects[]`。
    `aux` 给出时（辅助线程池，`concurrent.futures.Executor`），`temp_ck` 编码与 sha256、真值分区的 zstd 在该池中与可见度归约并行，
    返回前全部回填（字节与同步组装逐字节相同）。"""
    from awr.sensorsvc.poses import q_world_opt

    if model.codec != "radiometric_u16":
        raise ValueError("thermal_frame only assembles radiometric_u16 frames (agc_jpeg is a preview codec)")
    blocks: dict[str, bytes] = {}

    def put(name: str, b: bytes) -> str:
        ref = f"{ref_prefix}{name}"
        blocks[ref] = b
        return ref

    def z(a: np.ndarray) -> bytes:
        return _compressor(ZSTD_LEVEL).compress(memoryview(np.ascontiguousarray(a)).cast("B"))

    def ck_sha(a: np.ndarray) -> tuple[bytes, str]:
        b = encode_temp_ck(a)
        return b, hashlib.sha256(b).hexdigest()

    def run(fn, *args):
        return aux.submit(fn, *args) if aux is not None else _Now(fn(*args))

    unocc = out.instance_unocc if out.instance_unocc is not None else np.zeros_like(out.instance)
    f_ck = run(ck_sha, out.temp_ck)
    f_gt = [run(z, np.asarray(out.instance, "<u2")), run(z, np.asarray(unocc, "<u2")), run(z, out.semantic)]
    objects = (thermal_objects(out.t_app_k, out.instance, unocc, out.semantic, actors, stats=out.vis)
               if actors and out.instance_unocc is not None else [])
    tb, tsha = f_ck.result()
    zi, zu, zs = (f.result() for f in f_gt)
    av = {"sensor_id": sensor_id, "seq": int(seq), "t_mid_ns": int(t_mid_ns), "exposure_ns": 0, "readout_ns": 0,
          "width": model.width, "height": model.height, "K": model.K(), "dist": [0.0] * 5, "pixel_convention": "colmap_center_0.5",
          "mount_nominal": {"xyz_m": list(mount.xyz_m), "rpy_deg": list(mount.rpy_deg)}, "gimbal_reading": _gimbal(gimbal_reading),
          "temp_ck": {"dtype": "u16", "unit": "0.01 K", "codec": "radiometric_u16", "compression": "zstd", "ref": put("temp_ck.u16.zst", tb),
                      "sha256": tsha}}
    gt = {"T_world_sensor": {"pos_enu_m": [float(v) for v in pos_enu_m], "q_ws_xyzw": [float(v) for v in q_world_opt(R_ws)], "frame": "ENU<-RDF"},
          "gimbal_true": _gimbal(gimbal_true),
          "instance": {"dtype": "u16", "codec": "zstd", "ref": put("instance.u16.zst", zi)},
          "instance_unocc": {"dtype": "u16", "codec": "zstd", "ref": put("instance_unocc.u16.zst", zu)},
          "semantic": {"dtype": "u8", "codec": "zstd", "code_table": "d3-semantic@1", "ref": put("semantic.u8.zst", zs)},
          "objects": objects,
          "timing": {"t_truth_ns": [round(out.t_sub_s[-1] * 1e9)] if out.t_sub_s is not None else [int(t_mid_ns)],
                     "lag_subframes": len(out.weights), "subframe_weights": [round(float(w), 9) for w in out.weights]}}
    rec = {"schema": "drone4d.frame.v1", "frame_id": f"{ctx.run_id}/{uav}/{sensor_id}/{int(seq)}", "kind": "thermal", "agent_view": av,
           "ground_truth": gt, "internal": {**ctx.internal(), "encoder": TEMP_CK_ENCODER, "renderer_version": THERMAL_RENDERER_VERSION,
                                             "thermal": manifest}}
    return rec, blocks


# ---------------------------------------------------------------- GPU 渲染

_GLSL_PLANCK = """
const int NLUT = @N@;
const float T_MIN = @TMIN@;
const float T_STEP = @TSTEP@;
float lutv(sampler2D t, int i) { return texelFetch(t, ivec2(i, 0), 0).r; }
float lut_interp(sampler2D t, float T) {
    float u = (clamp(T, T_MIN, @TMAX@) - T_MIN) / T_STEP;
    int i = clamp(int(floor(u)), 0, NLUT - 2);
    float f = u - float(i);
    float a = lutv(t, i);
    return a + f * (lutv(t, i + 1) - a);
}
float planck_inv(sampler2D t, float L) {
    int lo = 0;
    int hi = NLUT - 1;
    if (L >= lutv(t, NLUT - 1)) lo = NLUT - 2;
    else {
        while (hi - lo > 1) { int m = (lo + hi) >> 1; if (lutv(t, m) <= L) lo = m; else hi = m; }
    }
    float a = lutv(t, lo);
    float f = clamp((L - a) / (lutv(t, lo + 1) - a), 0.0, 1.0);
    return T_MIN + (float(lo) + f) * T_STEP;
}
""".replace("@N@", str(N_TABLE)).replace("@TMIN@", f"{T_MIN_K:.1f}").replace("@TSTEP@", f"{T_STEP_K:.2f}").replace("@TMAX@", f"{T_MAX_K:.1f}")

TH_VS = """
#version 450
uniform mat4 u_mvp;
uniform mat4 u_mv;
in vec3 in_pos;
out vec3 v_q;
void main() {
    vec4 p = vec4(in_pos, 1.0);
    v_q = (u_mv * p).xyz;
    gl_Position = u_mvp * p;
}
"""
TH_FS = """
#version 450
layout(std430, binding = 0) readonly buffer TriTE { vec2 tri_te[]; };
layout(std430, binding = 1) readonly buffer TriSem { uint tri_sem[]; };
uniform uint u_tri0;
uniform float u_tair;
uniform mat3 u_Rwo;
in vec3 v_q;
layout(location = 0) out vec4 o_te;
layout(location = 1) out float o_z;
layout(location = 2) out uint o_inst;
layout(location = 3) out uint o_sem;
void main() {
    uint t = u_tri0 + uint(gl_PrimitiveID);
    vec2 te = tri_te[t];
    vec3 n = cross(dFdx(v_q), dFdy(v_q));
    float ln = length(n);
    n = ln > 0.0 ? n / ln : vec3(0.0, 0.0, -1.0);
    if (dot(n, v_q) > 0.0) n = -n;                       // 朝向相机的一面
    o_te = vec4(u_tair + te.x, te.y, (u_Rwo * n).z, 1.0);
    o_z = v_q.z;
    o_inst = 0u;
    o_sem = (tri_sem[t >> 2] >> ((t & 3u) * 8u)) & 0xFFu;
}
"""
def _th_actor_vs() -> str:
    from awr.sensorsvc.camera.actorbuf import GLSL_ACTOR_SSBO

    return """
#version 450
""" + GLSL_ACTOR_SSBO + """
uniform mat4 u_P;
in vec3 in_pos;
in vec3 in_nrm;
in float in_zone;
in float in_aid;
out vec3 v_q;
out vec3 v_nw;
out vec3 v_loc;
flat out int v_zone;
flat out int v_a;
void main() {
    int a = int(in_aid + 0.5);
    v_a = a;
    v_nw = mat3(act_rw[u_base + a]) * in_nrm;
    v_loc = in_pos;
    v_zone = int(in_zone + 0.5);
    vec4 q = act_mv[u_mvbase + a] * vec4(in_pos, 1.0);
    v_q = q.xyz;
    gl_Position = act_info[u_base + a].z == 0u ? vec4(2.0, 2.0, 2.0, 1.0)   // 本帧未出现：退化到裁剪体外
        : u_P * vec4(q.z > 0.02 ? q.xyz * ((q.z - 0.01) / q.z) : q.xyz, 1.0);   // 与可见光演员相同的 1 cm 深度偏置
}
"""


TH_ACTOR_FS = """
#version 450
layout(std430, binding = 4) readonly buffer ActInfo { uvec4 act_info[]; };
layout(std430, binding = 5) readonly buffer ActTh { vec4 act_th[]; };   // 每演员 3 项：分区温度、分区发射率、(发动机 x, 排气 x, 排气 z)
uniform int u_base;
uniform mat3 u_Rwo;
in vec3 v_q;
in vec3 v_nw;
in vec3 v_loc;
flat in int v_zone;
flat in int v_a;
layout(location = 0) out vec4 o_te;
layout(location = 1) out float o_z;
layout(location = 2) out uint o_inst;
layout(location = 3) out uint o_sem;
void main() {
    vec4 zT = act_th[3 * v_a];
    vec4 zE = act_th[3 * v_a + 1];
    vec3 geo = act_th[3 * v_a + 2].xyz;
    int z = v_zone;
    if (z == 2) z = v_loc.x >= geo.x ? 2 : ((v_loc.x <= geo.y && v_loc.z <= geo.z) ? 3 : 0);
    vec3 n = normalize(v_nw);
    if (dot(n, u_Rwo * (-v_q)) < 0.0) n = -n;
    uvec4 inf = act_info[u_base + v_a];
    o_te = vec4(zT[z], zE[z], n.z, 1.0);
    o_z = v_q.z;
    o_inst = inf.x;
    o_sem = inf.y;
}
"""
FULLSCREEN_VS = """
#version 450
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2) * 2.0 - 1.0;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""
RAD_FS = """
#version 450
""" + _GLSL_PLANCK + """
uniform sampler2D u_te;
uniform sampler2D u_z;
uniform sampler2D u_lut;
uniform vec4 u_K;
uniform vec3 u_env;                                      // B(T_air), B(T_sky), β（m⁻¹）
layout(location = 0) out float o_L;
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    float z = texelFetch(u_z, p, 0).r;
    if (z > 0.0) {
        vec4 te = texelFetch(u_te, p, 0);
        float x = (gl_FragCoord.x - u_K.z) / u_K.x;
        float y = (gl_FragCoord.y - u_K.w) / u_K.y;
        float tau = exp(-u_env.z * (z * sqrt(1.0 + x * x + y * y)));
        float w = clamp(te.z, 0.0, 1.0);
        float e = clamp(te.y, 0.0, 1.0);
        float lobj = e * lut_interp(u_lut, te.x) + (1.0 - e) * (w * u_env.y + (1.0 - w) * u_env.x);
        o_L = tau * lobj + (1.0 - tau) * u_env.x;
    } else {
        o_L = u_env.y;                                   // 无几何：天空辐射，不另乘透过
    }
}
"""
BLEND_FS = """
#version 450
uniform sampler2D u_L0;
uniform sampler2D u_L1;
uniform sampler2D u_L2;
uniform sampler2D u_L3;
uniform vec4 u_w;
uniform int u_S;
layout(location = 0) out float o_L;
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    float s = u_w.x * texelFetch(u_L0, p, 0).r;
    if (u_S > 1) s += u_w.y * texelFetch(u_L1, p, 0).r;
    if (u_S > 2) s += u_w.z * texelFetch(u_L2, p, 0).r;
    if (u_S > 3) s += u_w.w * texelFetch(u_L3, p, 0).r;
    o_L = s;
}
"""
HBLUR_FS = """
#version 450
uniform sampler2D u_src;
uniform float u_k[@NK@];
uniform int u_r;
layout(location = 0) out float o_L;
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    int w = textureSize(u_src, 0).x;
    float s = 0.0;
    for (int j = 0; j <= 2 * u_r; ++j) s += u_k[j] * texelFetch(u_src, ivec2(clamp(p.x + j - u_r, 0, w - 1), p.y), 0).r;
    o_L = s;
}
""".replace("@NK@", str(2 * MAX_BLUR_R + 1))


def _det_fs() -> str:
    from awr.sensorsvc.camera.raster import _GLSL_H64

    return """
#version 450
""" + _GLSL_H64 + _GLSL_PLANCK + """
uniform sampler2D u_src;
uniform sampler2D u_lut;
uniform sampler2D u_dlut;
uniform float u_k[@NK@];
uniform int u_r;
uniform vec4 u_noise;                                    // NETD_300·dB/dT(300 K)、FPN 偏置标准差、增益标准差、T_NUC
uniform uvec2 u_kt;
uniform uvec2 u_kf;
uniform uint u_W;
layout(location = 0) out uint o_ck;
layout(location = 1) out float o_t;
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    int h = textureSize(u_src, 0).y;
    float L = 0.0;
    for (int j = 0; j <= 2 * u_r; ++j) L += u_k[j] * texelFetch(u_src, ivec2(p.x, clamp(p.y + j - u_r, 0, h - 1)), 0).r;
    float t = planck_inv(u_lut, L);
    o_t = t;
    float o = t;
    uint64_t pix = uint64_t(p.y) * uint64_t(u_W) + uint64_t(p.x);
    if (u_noise.y > 0.0 || u_noise.z > 0.0) {
        uint64_t K = packUint2x32(u_kf);
        float off = u_noise.y * gauss(K, pix * 2UL);
        float gain = u_noise.z * gauss(K, pix * 2UL + 1UL);
        o = o + gain * (t - u_noise.w) + off;
    }
    if (u_noise.x > 0.0) o = o + (u_noise.x / lut_interp(u_dlut, t)) * gauss(packUint2x32(u_kt), pix);
    o_ck = uint(clamp(floor(o * 100.0 + 0.5), 0.0, 65535.0));
}
""".replace("@NK@", str(2 * MAX_BLUR_R + 1))


@dataclass
class ThermalOut:
    """一帧热成像输出；GPU 读回的数组为只读视图（PBO 映射字节），需要就地修改时先 `copy()`。"""

    temp_ck: np.ndarray                     # (H, W) uint16，0.01 K/LSB（含噪声档）
    t_app_k: np.ndarray                     # (H, W) float32 无噪表观温度（子帧加权、模糊与 B⁻¹ 之后，噪声之前）
    depth_m: np.ndarray | None              # (H, W) float32 平面深度（末子帧，`depth = True` 时），几何外 0
    instance: np.ndarray                    # (H, W) uint16（末子帧）
    semantic: np.ndarray                    # (H, W) uint8（末子帧，几何外 255）
    instance_unocc: np.ndarray | None       # (H, W) uint16 只渲染演员（演员给出时）
    weights: np.ndarray                     # 实际使用的子帧权重
    t_sub_s: np.ndarray | None              # 实际绘制的子帧时刻
    draw_ms: float
    readback_ms: float
    batches_drawn: int
    actors_drawn: int
    debug: dict | None = None               # `debug=True`：te (S, H, W, 4) f32、z (S, H, W)、L (S, H, W) 子帧入瞳辐亮度
    gpu_ms: float | None = None             # GL 计时查询的 GPU 时间（`submit(..., timer=True)`）
    vis: np.ndarray | None = None           # (K, VIS_STRIDE) uint32 GPU 可见度统计（`camera.actors.visibility_stats` 同一布局；有演员时）


class _ThTarget:
    def __init__(self, ctx, gl, w: int, h: int) -> None:
        from awr.sensorsvc.camera.raster import _Mrt

        self.w, self.h = w, h
        self.mrt = _Mrt(ctx, gl, w, h, col_dtype="f4")
        self.unocc = None

        def r32() -> tuple:
            t = ctx.texture((w, h), 1, dtype="f4")
            t.filter = (0x2600, 0x2600)
            return t, ctx.framebuffer(color_attachments=[t])
        self.L = [r32() for _ in range(MAX_SUBFRAMES)]
        self.blend = r32()
        self.hblur = r32()
        self.ck = ctx.texture((w, h), 1, dtype="u2")
        self.ck.filter = (0x2600, 0x2600)
        self.tc = ctx.texture((w, h), 1, dtype="f4")
        self.tc.filter = (0x2600, 0x2600)
        self.out = ctx.framebuffer(color_attachments=[self.ck, self.tc])

    def release(self, gl) -> None:
        self.mrt.release(gl)
        if self.unocc is not None:
            self.unocc.release(gl)


@dataclass
class ThermalJob:
    """已提交的一帧热成像（`ThermalCamera.submit`）：`result()` 在渲染线程等待读回并返回 `ThermalOut`（数组为读回槽位上的只读
    视图），`release()` 归还槽位（任一线程，幂等；归还后视图失效）。"""

    read: object
    meta: dict
    query: object | None = None
    _out: ThermalOut | None = None

    def result(self) -> ThermalOut:
        if self._out is None:
            t0 = time.perf_counter()
            v = self.read.wait()
            m = self.meta
            o = ThermalOut(v["ck"], v["tc"], v.get("z"), v["inst"], v["sem"], v.get("unocc"), m["weights"], m["t_sub"], m["draw_ms"],
                           (time.perf_counter() - t0) * 1e3, m["batches"], m["actors"], m["debug"])
            o.gpu_ms = self.query.elapsed / 1e6 if self.query is not None else None
            if v.get("vis") is not None:
                from awr.sensorsvc.camera.actors import VIS_STRIDE

                o.vis = v["vis"].reshape(-1, VIS_STRIDE)
            self._out = o
        return self._out

    def release(self) -> None:
        self.read.release()
        self._out = None


def _same_poses(poses) -> bool:
    R0, p0 = (np.asarray(v, np.float64) for v in poses[0])
    return all(np.array_equal(np.asarray(R, np.float64), R0) and np.array_equal(np.asarray(p, np.float64), p0) for R, p in poses[1:])


def _same_actor_frames(frames) -> bool:
    if frames is None:
        return True
    f0 = frames[0]
    return all(np.array_equal(f.idx, f0.idx) and np.array_equal(f.pos, f0.pos) and np.array_equal(f.yaw, f0.yaw) for f in frames[1:])


def _frustum_visible(corners: np.ndarray, cm: CameraModel, M: np.ndarray) -> np.ndarray:
    """视锥剔除（保守，与 `RasterCamera._visible` 同式）：八个角点同在某一裁剪面外侧时剔除。"""
    Xo = corners @ M[:3, :3].T + M[:3, 3]
    x, y, z = Xo[..., 0], Xo[..., 1], Xo[..., 2]
    out = ((z < cm.near_m).all(1) | (z > cm.far_m).all(1) | (x * cm.fx < -cm.cx * z).all(1)
           | (x * cm.fx > (cm.width - cm.cx) * z).all(1) | (y * cm.fy < -cm.cy * z).all(1) | (y * cm.fy > (cm.height - cm.cy) * z).all(1))
    return ~out


def _blur_kernel(sigma_px: float) -> tuple[np.ndarray, int]:
    k = np.zeros(2 * MAX_BLUR_R + 1, np.float32)
    if sigma_px <= 0:
        k[0] = 1.0
        return k, 0
    kk = tn._gauss_kernel(sigma_px)
    r = (kk.size - 1) // 2
    k[:kk.size] = kk
    return k, r


class ThermalCamera:
    """热成像 GPU 渲染器：持有静态逐三角形热参数（SSBO）、Planck 表纹理与演员代理网格；`render` 线程不安全（同 `RasterCamera`）。

    `raster` 给出时共用其 EGL 上下文（同一 worker 的可见光与热成像共用上下文与 clip control）；否则自建只含上下文的 `RasterCamera`。
    静态几何按 512 m 瓦片合并为每瓦片一次绘制（只含位置 `3f` 与索引，热成像不需要纹理分批）：crop 世界 64 个外观批次合为约 20 次
    绘制，CPU 提交时间约降为 1/4；显存为每顶点 12 B、每三角形 12 B（crop 世界 2.88 M 三角形约 90 MB）。
    `tri_cls` 为逐外观三角形静态类码（`static_classes`），`static` 为 `thermal_bake` 产物（无时日照可见度按 0 计），`sun_az_el_deg`
    为事件起点太阳位置（与 bake 相同）。"""

    def __init__(self, scene, *, table: ThermalTable | None = None, tri_cls: np.ndarray | None = None, static: ThermalStatic | None = None,
                 sun_az_el_deg: tuple[float, float] | None = None, raster=None, ctx=None, band_um: tuple[float, float] = BAND_UM,
                 readback_slots: int = 6) -> None:
        from awr.sensorsvc.camera.raster import RasterCamera

        self._own = raster is None
        if raster is None:
            from awr.sensorsvc.camera.scene import AppearanceScene

            shell = AppearanceScene(scene.world_id, [], [], 0, scene.bounds_min, scene.bounds_max)
            raster = RasterCamera(shell, ctx=ctx, textures=False)
        self.raster = raster
        rc = self.raster
        self.ctx, self.gl, self._mgl = rc.ctx, rc.gl, rc._mgl
        self.table = table or load_thermal_table()
        self.planck = planck_table(band_um)
        self.scene = scene
        n = scene.triangles
        self.tri_cls = np.zeros(n, np.uint8) if tri_cls is None else np.asarray(tri_cls, np.uint8)
        if self.tri_cls.size != n:
            raise ValueError(f"tri_cls has {self.tri_cls.size} values for {n} triangles")
        if static is not None and static.sun_vis.size != n:
            raise ValueError("thermal_static does not match the scene")
        self.static = static
        self.sun_az_el = tuple(static.sun_az_el_deg) if static is not None and sun_az_el_deg is None else sun_az_el_deg
        self.sun_cos = triangle_sun_cos(scene_triangles(scene), self.sun_az_el) if any(r.sun_incidence for r in self.table.rows) else None
        ctx_ = self.ctx
        self.prog = ctx_.program(vertex_shader=TH_VS, fragment_shader=TH_FS)
        self.actp = ctx_.program(vertex_shader=_th_actor_vs(), fragment_shader=TH_ACTOR_FS)
        self.radp = ctx_.program(vertex_shader=FULLSCREEN_VS, fragment_shader=RAD_FS)
        self.blendp = ctx_.program(vertex_shader=FULLSCREEN_VS, fragment_shader=BLEND_FS)
        self.hblurp = ctx_.program(vertex_shader=FULLSCREEN_VS, fragment_shader=HBLUR_FS)
        self.detp = ctx_.program(vertex_shader=FULLSCREEN_VS, fragment_shader=_det_fs())
        self._vaos = {k: ctx_.vertex_array(p, []) for k, p in (("rad", self.radp), ("blend", self.blendp), ("h", self.hblurp), ("det", self.detp))}
        self.draws: list[tuple] = []                                      # (瓦片, 原点, vao, 三角形偏移, 缓冲)
        groups: dict[tuple[int, int], list] = {}
        for b in sorted(scene.batches, key=lambda b: (b.tile, -1 if b.texture is None else b.texture)):
            groups.setdefault(b.tile, []).append(b)
        order, corners = [], []
        off = 0
        self.gpu_bytes = 0
        for tile, bs in groups.items():
            voff = np.cumsum([0] + [len(b.pos) for b in bs[:-1]])
            pos = np.concatenate([b.pos for b in bs]).astype("<f4")
            idx = np.concatenate([b.idx.astype(np.int64) + o for b, o in zip(bs, voff, strict=True)]).astype("<u4")
            vbo, ibo = ctx_.buffer(pos.tobytes()), ctx_.buffer(idx.tobytes())
            self.gpu_bytes += pos.nbytes + idx.nbytes
            self.draws.append((tile, bs[0].origin, ctx_.vertex_array(self.prog, [(vbo, "3f", "in_pos")], ibo), off, (vbo, ibo)))
            order.append(np.concatenate([b.tri_ids for b in bs]).astype(np.int64))
            off += idx.reshape(-1, 3).shape[0]
            lo, hi = bs[0].origin + pos.min(0), bs[0].origin + pos.max(0)
            corners.append([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        self.corners = np.asarray(corners, np.float64).reshape(-1, 8, 3)
        self.tile_org = np.asarray([d[1] for d in self.draws], np.float64).reshape(-1, 3)
        self._order = np.concatenate(order) if order else np.zeros(0, np.int64)
        sem = np.zeros(-(-max(off, 1) // 4) * 4, np.uint8)
        sem[:off] = self.tri_cls[self._order]
        self.sem_buf = ctx_.buffer(sem.tobytes())
        self.te_buf = ctx_.buffer(reserve=max(off, 1) * 8)
        self._static_key = None
        self.lut = ctx_.texture((N_TABLE, 1), 1, self.planck.radiance_lut.astype("<f4").tobytes(), dtype="f4")
        self.dlut = ctx_.texture((N_TABLE, 1), 1, self.planck.dbdt_lut.astype("<f4").tobytes(), dtype="f4")
        for t in (self.lut, self.dlut):
            t.filter = (0x2600, 0x2600)
        self.actors = None                                                # camera.actorbuf.ActorBuffers（set_actors）
        self.vis_buf = None                                               # GPU 可见度统计（set_actors 时创建）
        self.vis_K = 1
        self._vis0 = b""
        self.actor_sem: list[int] = []
        self.th_buf = ctx_.buffer(reserve=48)
        self._env_key: tuple | None = None
        self._env_rad = (0.0, 0.0, 0.0)
        self._netd_key: tuple | None = None
        self._netd = 0.0
        self._targets: dict[tuple[int, int], _ThTarget] = {}
        self._th_key: object = None
        self.static_s = 0.0
        from awr.sensorsvc.camera.readback import ReadbackRing

        self.ring = ReadbackRing(slots=readback_slots)
        self._queries: list = []

    # ------------------------------------------------------------ 静态与演员
    def _ensure_static(self, env: ThermalEnv) -> None:
        key = (round(float(env.cloud), 9), self.table.is_wet(env.rain_mmh))
        if key == self._static_key:
            return
        t0 = time.perf_counter()
        vis = None if self.static is None else self.static.sun_vis
        dT, eps = static_surface(self.table, self.tri_cls, vis, self.sun_cos, env, self.sun_az_el)
        te = np.stack([dT[self._order], eps[self._order]], 1).astype("<f4") if len(self._order) else np.zeros((1, 2), "<f4")
        self.te_buf.write(te.tobytes())
        self._static_key = key
        self.static_s = time.perf_counter() - t0

    def set_actors(self, actors: list[dict]) -> None:
        """磁带演员表（按 idx）→ 合批的代理网格（顶点、法向、分区），与可见光代理几何相同。"""
        from awr.sensorsvc.camera.actorbuf import ActorBuffers
        from awr.sensorsvc.camera.actors import SEM_CODE

        if self.actors is not None:
            self.actors.release()
        from awr.sensorsvc.camera.actors import vis_init

        self.vis_K = len(actors) + 1
        self._vis0 = vis_init(self.vis_K).tobytes()
        if self.vis_buf is None:
            self.vis_buf = self.ctx.buffer(reserve=len(self._vis0))
        meshes, info = [], []
        for k, a in enumerate(actors):
            P, N, _C = proxy_mesh(a)
            meshes.append(np.hstack([P, N, actor_zones(a)[:, None]]))
            info.append((k + 1, SEM_CODE.get(a["class"], 13)))
        self.actor_sem = [s_ for _i, s_ in info]
        self.actors = ActorBuffers(self.ctx, meshes, "3f 3f 1f", ("in_pos", "in_nrm", "in_zone"), info)
        self._th_key = None

    def _target(self, w: int, h: int) -> _ThTarget:
        t = self._targets.get((w, h))
        if t is None:
            t = self._targets[(w, h)] = _ThTarget(self.ctx, self.gl, w, h)
        return t

    @staticmethod
    def _put(prog, k: str, v) -> None:
        with contextlib.suppress(KeyError):                               # 驱动裁掉的未使用 uniform
            prog[k].value = v

    def _upload_th(self, actor_th: Sequence[ActorThermal]) -> None:
        """逐演员分区温度、发射率与车辆分区几何写入 SSBO（binding 5）；同一 `actor_th` 对象只写一次。"""
        if self._th_key is actor_th:
            return
        n = max(len(actor_th), 1)
        a = np.zeros((n, 3, 4), np.float32)
        for k, t in enumerate(actor_th):
            a[k, 0, :len(t.zone_t_k)] = t.zone_t_k
            a[k, 1, :len(t.zone_eps)] = t.zone_eps
            a[k, 2, :3] = (t.engine_x_m, t.exhaust_x_m, t.exhaust_z_m)
        b = a.astype("<f4").tobytes()
        self.th_buf.orphan(max(len(b), self.th_buf.size))               # 换新存储：不与仍在执行的上一帧绘制同步
        self.th_buf.write(b)
        self._th_key = actor_th

    def _draw_actors(self, P: np.ndarray, R_ws, p_w, base: int, mvbase: int) -> None:
        from awr.sensorsvc.camera.proj import world_to_opt

        prog = self.actp
        prog["u_P"].write(P.T.astype("<f4").tobytes())
        prog["u_Rwo"].write(world_to_opt(R_ws, p_w)[:3, :3].astype("<f4").tobytes())
        self.th_buf.bind_to_storage_buffer(5)
        self.actors.draw(prog, base, mvbase)

    def _geometry(self, tg: _ThTarget, cm: CameraModel, R_ws, p_w, env: ThermalEnv, aset) -> tuple[int, int]:
        from awr.sensorsvc.camera.proj import gl_mat4, mv_tiles, proj_reversed_z, world_to_opt

        mgl = self._mgl
        tg.mrt.fbo.use()
        self.ctx.viewport = (0, 0, tg.w, tg.h)
        tg.mrt.clear(self.gl)
        self.ctx.enable(mgl.DEPTH_TEST)
        self.ctx.disable(mgl.CULL_FACE)
        self.ctx.depth_func = ">"
        P = proj_reversed_z(cm)
        Mw = world_to_opt(R_ws, p_w)
        prog = self.prog
        prog["u_tair"].value = float(env.t_air_k)
        prog["u_Rwo"].write(Mw[:3, :3].astype("<f4").tobytes())         # 列主序写入 R_ow 的字节，着色器得 R_wo（光学 → 世界）
        self.te_buf.bind_to_storage_buffer(0)
        self.sem_buf.bind_to_storage_buffer(1)
        n = 0
        idx = np.flatnonzero(_frustum_visible(self.corners, cm, Mw))
        if len(idx):                                                     # 可见瓦片的模型视图一次计算（float64），转列主序 float32
            MV = mv_tiles(R_ws, p_w, self.tile_org[idx])
            mvb, mvpb = gl_mat4(MV), gl_mat4(P @ MV)
        u_mv, u_mvp, u_tri0 = prog["u_mv"], prog["u_mvp"], prog["u_tri0"]
        for k, i in enumerate(idx):
            _tile, _origin, vao, off, _bufs = self.draws[i]
            u_mv.write(mvb[k].tobytes())
            u_mvp.write(mvpb[k].tobytes())
            u_tri0.value = int(off)
            vao.render()
            n += 1
        na = 0
        if aset is not None and aset[2]:
            self._draw_actors(P, R_ws, p_w, aset[0], aset[1])
            na = aset[2]
        self.ctx.disable(mgl.DEPTH_TEST)
        return n, na

    def _unocc(self, tg: _ThTarget, cm: CameraModel, R_ws, p_w, aset) -> None:
        from awr.sensorsvc.camera.proj import proj_reversed_z
        from awr.sensorsvc.camera.raster import _Mrt

        mgl = self._mgl
        if tg.unocc is None:
            tg.unocc = _Mrt(self.ctx, self.gl, tg.w, tg.h, col_dtype="f4")
        tg.unocc.fbo.use()
        self.ctx.viewport = (0, 0, tg.w, tg.h)
        tg.unocc.clear(self.gl)
        if aset is not None and aset[2]:
            self.ctx.enable(mgl.DEPTH_TEST)
            self.ctx.depth_func = ">"
            self._draw_actors(proj_reversed_z(cm), R_ws, p_w, aset[0], aset[1])
            self.ctx.disable(mgl.DEPTH_TEST)

    def render(self, model: ThermalModel, poses: Sequence[tuple[np.ndarray, np.ndarray]], t_sub_s, t_end_s: float, env: ThermalEnv, *,
               actor_frames: Sequence | None = None, actor_th: Sequence[ActorThermal] | None = None, noise: NoiseKeys | None = None,
               depth: bool = False, debug: bool = False) -> ThermalOut:
        """同步接口：`submit` 后立即等待读回，返回只读副本。参数同 `submit`。"""
        job = self.submit(model, poses, t_sub_s, t_end_s, env, actor_frames=actor_frames, actor_th=actor_th, noise=noise, depth=depth,
                          debug=debug)
        try:
            o = job.result()

            def cp(a):
                if a is None:
                    return None
                c = np.array(a)
                c.setflags(write=False)
                return c
            out = ThermalOut(cp(o.temp_ck), cp(o.t_app_k), cp(o.depth_m), cp(o.instance), cp(o.semantic), cp(o.instance_unocc), o.weights,
                             o.t_sub_s, o.draw_ms, o.readback_ms, o.batches_drawn, o.actors_drawn, o.debug, o.gpu_ms, cp(o.vis))
        finally:
            job.release()
        return out

    def _query(self, k: int):
        while len(self._queries) <= k:
            self._queries.append(self.ctx.query(time=True))
        return self._queries[k]

    def submit(self, model: ThermalModel, poses: Sequence[tuple[np.ndarray, np.ndarray]], t_sub_s, t_end_s: float, env: ThermalEnv, *,
               actor_frames: Sequence | None = None, actor_th: Sequence[ActorThermal] | None = None, noise: NoiseKeys | None = None,
               depth: bool = False, debug: bool = False, timer: bool = False) -> ThermalJob:
        """绘制一帧热成像并提交读回，立即返回 `ThermalJob`。`poses[j]` 为子帧 j 的 (R_ws, p_w)（与 `t_sub_s` 对应，升序，末项 ≤ t_end），
        `actor_frames[j]` 为该子帧的 `raster.ActorFrame`，`actor_th[idx]` 为演员 idx 的分区温度（`actor_thermal`）。`env` 为本帧环境
        （`thermal_env`）。`depth = True` 时另读回末子帧平面深度（帧记录不含深度，在线路径不读）；`timer = True` 以 GL 计时查询记录
        GPU 时间。"""
        from awr.sensorsvc.camera.proj import world_to_opt
        from awr.sensorsvc.camera.readback import ReadJob, plane_layout

        mgl = self._mgl
        S = len(poses)
        t_sub = np.asarray(t_sub_s, np.float64)
        if not 1 <= S <= MAX_SUBFRAMES or t_sub.shape != (S,):
            raise ValueError(f"need 1..{MAX_SUBFRAMES} sub-frame poses matching t_sub_s")
        if actor_frames is not None and (len(actor_frames) != S or actor_th is None):
            raise ValueError("actor_frames must give one frame per sub-frame, with actor_th")
        prof = model.noise_profile()
        if (prof.netd_mk_300k > 0 or prof.fpn_offset_k > 0 or prof.fpn_gain_frac > 0) and noise is None:
            raise ValueError(f"noise profile {prof.name!r} needs NoiseKeys")
        weights = tn.subframe_weights(t_sub, t_end_s, prof.tau_bolometer_s)
        sub = list(range(S))
        if S > 1 and _same_poses(poses) and _same_actor_frames(actor_frames):   # 静止：一遍即可（Σw = 1）
            sub, weights = [S - 1], np.ones(1)
        self._ensure_static(env)
        cm = model.camera()
        W, H = cm.width, cm.height
        tg = self._target(W, H)
        has_unocc = actor_frames is not None
        items = [("ck", W, H, "u16"), ("tc", W, H, "f32"), ("inst", W, H, "u16"), ("sem", W, H, "u8")]
        if has_unocc:
            items.append(("unocc", W, H, "u16"))
        want_vis = has_unocc and self.actors is not None
        if want_vis:
            from awr.sensorsvc.camera.actors import VIS_STRIDE

            items.append(("vis", VIS_STRIDE * self.vis_K, 1, "u32"))
        if depth:
            items.append(("z", W, H, "f32"))
        layout, nbytes = plane_layout(items)
        t0 = time.perf_counter()
        slot = self.ring.acquire(nbytes)
        q = None
        if timer:
            q = self._query(slot)
            q.mglo.begin()
        tbl = self.planck
        ek = (env.t_air_k, env.sky_k, env.rh, env.fog_visibility_km, env.rain_mmh)
        if self._env_key != ek:                                          # 同一环境逐帧相同：辐亮度与消光只算一次
            self._env_rad = (float(tbl.radiance(env.t_air_k)), float(tbl.radiance(env.sky_k)),
                             self.table.beta_per_km(env.rh, env.fog_visibility_km, env.rain_mmh) * 1e-3)
            self._env_key = ek
        b_air, b_sky, beta_m = self._env_rad
        asets = [None] * len(sub)
        if actor_frames is not None and self.actors is not None:
            self._upload_th(actor_th)
            asets = self.actors.upload([(actor_frames[j], [world_to_opt(*poses[j])]) for j in sub])
        dbg = {"te": [], "z": [], "L": []} if debug else None
        n = na = 0
        self.lut.use(3)
        self.dlut.use(4)
        for slot_j, j in enumerate(sub):
            R, p = poses[j]
            n, na = self._geometry(tg, cm, R, p, env, asets[slot_j])
            tg.mrt.col.use(5)
            tg.mrt.z.use(6)
            tg.L[slot_j][1].use()
            self.ctx.viewport = (0, 0, W, H)
            for k, v in (("u_te", 5), ("u_z", 6), ("u_lut", 3), ("u_K", (cm.fx, cm.fy, cm.cx, cm.cy)), ("u_env", (b_air, b_sky, beta_m))):
                self._put(self.radp, k, v)
            self._vaos["rad"].render(mgl.TRIANGLES, vertices=3)
            if dbg is not None:
                dbg["te"].append(np.frombuffer(tg.mrt.col.read(), np.float32).reshape(H, W, 4).copy())
                dbg["z"].append(np.frombuffer(tg.mrt.z.read(), np.float32).reshape(H, W).copy())
                dbg["L"].append(np.frombuffer(tg.L[slot_j][0].read(), np.float32).reshape(H, W).copy())
        for k in range(len(sub)):
            tg.L[k][0].use(7 + k)
        wv = np.zeros(4, np.float32)
        wv[:len(sub)] = weights.astype(np.float32)
        tg.blend[1].use()
        for k, v in (("u_L0", 7), ("u_L1", 8), ("u_L2", 9), ("u_L3", 10), ("u_w", tuple(float(x) for x in wv)), ("u_S", len(sub))):
            self._put(self.blendp, k, v)
        self._vaos["blend"].render(mgl.TRIANGLES, vertices=3)
        kern, r = _blur_kernel(prof.optics_sigma_px)
        tg.blend[0].use(11)
        tg.hblur[1].use()
        self._put(self.hblurp, "u_src", 11)
        self.hblurp["u_k"].write(kern.astype("<f4").tobytes())
        self._put(self.hblurp, "u_r", r)
        self._vaos["h"].render(mgl.TRIANGLES, vertices=3)
        last = sub[-1]
        if has_unocc:
            self._unocc(tg, cm, *poses[last], asets[-1])
        tg.hblur[0].use(12)
        tg.out.use()

        def key2(k: int) -> tuple[int, int]:
            return (k & 0xFFFFFFFF, (k >> 32) & 0xFFFFFFFF)

        from awr.sim.sensors.cbrng import key

        kt = kf = 0
        if noise is not None:
            tn._check_ids(noise.uav_no, noise.sensor_no)
            kt = int(key(noise.seed, tn.STREAM_M25, noise.uav_no, noise.frame_index, tn.THERM_CHANNELS["therm_temporal"][0] + noise.sensor_no))
            kf = int(key(noise.seed, tn.STREAM_M25, noise.uav_no, 0, tn.THERM_CHANNELS["therm_fpn"][0] + noise.sensor_no))
        nk = ("netd", prof.netd_mk_300k)
        if self._netd_key != nk:
            self._netd = float(np.float32(prof.netd_mk_300k * 1e-3 * float(tbl.dbdt(tn.T_REF_NETD_K))))
            self._netd_key = nk
        netd_num = self._netd
        self.detp["u_k"].write(kern.astype("<f4").tobytes())
        for k, v in (("u_src", 12), ("u_lut", 3), ("u_dlut", 4), ("u_r", r), ("u_noise", (netd_num, prof.fpn_offset_k, prof.fpn_gain_frac, tn.T_NUC_K)),
                     ("u_kt", key2(kt)), ("u_kf", key2(kf)), ("u_W", W)):
            self._put(self.detp, k, v)
        self._vaos["det"].render(mgl.TRIANGLES, vertices=3)
        if want_vis:                                                     # 末子帧实例、无遮挡实例与语义 → 逐实例统计（与光栅相机同一归约）
            visp = self.raster.visp                                      # 与光栅相机共用归约程序（同一上下文），统计缓冲各自持有
            self.vis_buf.orphan(len(self._vis0))
            self.vis_buf.write(self._vis0)
            self.vis_buf.bind_to_storage_buffer(7)
            tg.mrt.inst.use(8)
            tg.unocc.inst.use(9)
            tg.mrt.sem.use(10)
            visp["u_size"].value = (W, H)
            visp["u_K"].value = self.vis_K
            visp.run(-(-W // 16), -(-H // 16), 1)
            self.ctx.memory_barrier()
        if q is not None:
            q.mglo.end()
        tex = {"ck": tg.ck.glo, "tc": tg.tc.glo, "inst": tg.mrt.inst.glo, "sem": tg.mrt.sem.glo, "z": tg.mrt.z.glo}
        if has_unocc:
            tex["unocc"] = tg.unocc.inst.glo
        self.ring.issue(slot, layout, {name: tex[name] for name, *_ in items if name != "vis"},
                        {"vis": self.vis_buf.glo} if want_vis else None)
        t1 = time.perf_counter()
        if dbg is not None:
            dbg = {k: np.stack(v) for k, v in dbg.items()}
        meta = {"weights": weights, "t_sub": t_sub[sub], "draw_ms": (t1 - t0) * 1e3, "batches": n, "actors": na, "debug": dbg}
        return ThermalJob(ReadJob(self.ring, slot, layout, tuple(name for name, *_ in items)), meta, q)

    def release(self) -> None:
        for t in self._targets.values():
            t.release(self.gl)
        self._targets.clear()
        self.ring.close()
        if self.actors is not None:
            self.actors.release()
            self.actors = None
        self.th_buf.release()
        if self.vis_buf is not None:
            self.vis_buf.release()
            self.vis_buf = None
        for _tile, _o, vao, _off, bufs in self.draws:
            vao.release()
            for b in bufs:
                b.release()
        self.draws = []
        for b in (self.sem_buf, self.te_buf, self.lut, self.dlut):
            b.release()
        if self._own:
            self.raster.release()

