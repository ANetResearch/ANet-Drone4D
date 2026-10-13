"""感知用传感器参数（M19-FR-001；M18-M19 PRD §6.6；AWR-04 §6.1；AWR-20 §6、§7.5）。

冻结类型（WP-01，`tools/fake/perception_stub.py`；只允许追加带缺省值的尾部字段）：`Illuminator`、`SensorSpecP`、`SensorState`、
`ViewGeom`、`EnvSample`。非成像传感器的参数另有 `RadarSpec`、`AcousticSpec`、`LidarSpec`（本模块新增）。

来源：
- 型号库 `vehicles/sensor_models/<model_id>.yaml`（M21 交付；`load_model_doc`）→ `*_from_doc`；
- M13 运行时的成像状态 `ImagingState`（挂载、变焦、模式、照明器，`imaging_state_spec`）；
- 参考传感器（GX40、`nir_ill_l`、LWIR 640、mmw77、mic8、MID-360）的工厂函数，取值与型号库一致（单测比对）。

校验失败抛 `PerceptionError`：码 560（SENSOR_UNSUPPORTED，机型无该传感器或模式）、561（SENSOR_PARAM_INVALID，焦距、镜头、
分辨率越界或 4K 缺少算力）、562（LEVEL_UNREACHABLE）、563（ILLUMINATOR_UNAVAILABLE）。只依赖 numpy、yaml 与 `awr.contracts`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Any, Literal

from .params import LEVELS

__all__ = [
    "AcousticSpec", "EnvSample", "Illuminator", "LidarSpec", "PerceptionError", "RadarSpec", "SensorSpecP", "SensorState", "ViewGeom",
    "acoustic_from_doc", "eo_from_doc", "gx40", "gx40_nir", "illuminator_from_doc", "imaging_state_spec", "lidar_from_doc", "load_model_doc",
    "lwir640", "lwir_from_doc", "mic8", "mid360", "mmw77", "radar_from_doc", "sensor_for", "with_resolution",
]

Light = Literal["day", "low", "night"]
_BL = LEVELS["blur"]
_LI = LEVELS["light"]


class PerceptionError(ValueError):
    """感知参数或查询的拒绝：码 560–563 与字段级 detail（`{field, why, value?, limit?}`）。"""

    def __init__(self, code: int, field_: str | None = None, why: str = "INVALID", **extra: Any) -> None:
        self.code = int(code)
        self.detail: dict[str, Any] = {"why": why}
        if field_ is not None:
            self.detail["field"] = field_
        for k, v in extra.items():
            if v is not None:
                self.detail[k] = v
        super().__init__(f"{code} {field_ or ''} {why}")


# ---------------------------------------------------------------- 冻结类型（与桩同名同序，尾部可追加）
@dataclass(frozen=True)
class Illuminator:
    kind: str                              # builtin | nir_ill_l（型号名）
    p_w: float
    divergence_deg: float | None           # None = 随当前 HFOV（GX40 机内补光）
    r_ref_m: float
    p_ref_w: float
    theta_ref_deg: float
    rho_ref: float = 0.3
    r_spec_max_m: float | None = None
    wavelength_nm: float = 850.0

    def r0_m(self, theta_rad: float, rho_850: float) -> float:
        """无消光的有效距离 `R_ref·(θ_ref/θ)·√(P/P_ref)·√(rho/rho_ref)`（AWR-04 §6.1）。"""
        th_deg = math.degrees(theta_rad) if self.divergence_deg is None else float(self.divergence_deg)
        if not th_deg > 0.0:
            return 0.0
        return (self.r_ref_m * (self.theta_ref_deg / th_deg) * math.sqrt(self.p_w / self.p_ref_w)
                * math.sqrt(max(float(rho_850), 0.0) / self.rho_ref))

    def theta_rad(self, hfov_rad: float) -> float:
        return float(hfov_rad) if self.divergence_deg is None else math.radians(float(self.divergence_deg))


@dataclass(frozen=True)
class SensorSpecP:
    """感知用的有效传感器参数（型号文件与挂载文件合并，AWR-20 §7.5）。"""

    name: str
    kind: Literal["eo", "nir", "lwir"]
    pixel_um: float
    w_native_px: int
    h_native_px: int
    f_min_mm: float
    f_max_mm: float
    f_number_min: float                    # f_min 处
    f_number_max: float                    # f_max 处
    outputs: dict = field(default_factory=dict)   # 输出名 -> (w_out_px, crop_w_px)
    analysis: str = "native"
    f_law: Literal["linear_f", "linear_ln_f"] = "linear_f"
    netd_k: float = 0.05
    sigma_clutter_k: tuple[float, float] = (2.0, 1.0)   # 日、夜
    e_min_color_lx: float = 0.5
    e_min_mono_lx: float = 0.05
    illuminators: tuple[Illuminator, ...] = ()
    pitch_min_deg: float = -90.0
    pitch_max_deg: float = 30.0
    jitter_short_urad: float = 10.0
    jitter_long_urad: float = 30.0
    # ---- 追加（M19）
    model_id: str = ""
    t_exp_day_s: float = float(_BL["t_exp_day_s"])
    t_exp_low_s: float = float(_BL["t_exp_low_s"])
    t_int_s: float = float(_BL["t_exp_lwir_s"])         # LWIR 积分时间
    switch_lx: float = float(_BL["switch_lx"])
    jitter_fast_max_s: float = float(_BL["t_short_max_s"])
    k4_requires: str | None = None
    crop_h_px: dict = field(default_factory=dict)       # 输出名 -> 裁切高（VFOV；缺省原生高）
    lens_mm: tuple[float, ...] = ()                     # LWIR 可选镜头
    digital_zoom_max: float = 1.0                       # LWIR 数字变焦（只影响显示）
    virtual: bool = False                               # D1 相机与热像：按内参换算的虚拟像元（p = 1 µm，f = fx·1e-3 mm）


@dataclass(frozen=True)
class SensorState:
    spec: SensorSpecP
    f_mm: float
    mode: Literal["auto", "day", "lowlight", "nir"] = "auto"
    resolution: str | None = None          # 输出名；None = spec.analysis
    illum_on: tuple[bool, ...] = ()        # 按 spec.illuminators；空 = nir 模式全部开启


@dataclass(frozen=True)
class ViewGeom:
    p_sensor: tuple[float, float, float]   # World ENU m
    k_los: float = 1.0                     # 0、1/3、2/3、1（管线填入）
    tracking: bool = True                  # 云台跟踪该识别物
    v_rel_perp_mps: float = 0.0            # 平台与识别物在视线垂直方向的相对速度
    in_fov: bool = True
    in_illum_cone: bool = True
    # ---- 追加（M19）
    worst_aspect: bool | None = None       # None：识别物运动时取 β 最不利（规划口径）；管线传 False（按实际方位）
    tau_path: float | None = None          # 传感器波长的路径透过率（M07 optical_depth）；None 按 env.mor_m 均匀消光
    tau_850: float | None = None           # 850 nm 路径透过率（NIR 照明）


@dataclass(frozen=True)
class EnvSample:
    illum_lx: float = 1.0e5
    band: Light = "day"
    mor_m: float = 20000.0
    rho_v_gm3: float = 7.5                 # LWIR 水汽项
    k_cross: float = 1.0
    t_bg_c: float = 20.0
    eps_bg: float = 0.95
    t_sky_c: float = -20.0
    rain_mmph: float = 0.0
    # ---- 追加（M19）
    fallback: bool = False                 # 未装配 M07（PRD §6.11 末段）


# ---------------------------------------------------------------- 非成像传感器
@dataclass(frozen=True)
class RadarSpec:
    name: str = "radar"
    model_id: str = "mmw77"
    fov_az_deg: float = 60.0               # ±
    fov_el_deg: float = 15.0               # ±
    r_ref_m: float = 180.0
    sigma_ref_m2: float = 1.0
    snr_d_db: float = 13.2
    r_max_m: float = 400.0
    range_res_m: float = 0.3
    az_res_deg: float = 4.0
    vel_res_mps: float = 0.1
    sigma_range_m: float = 0.15
    sigma_az_deg: float = 1.0
    sigma_el_deg: float = 2.0
    sigma_vr_mps: float = 0.05
    rain_k: float = 1.0                    # gamma_rain = k·RR^alpha（dB/km；AWR-20 §6.5）
    rain_alpha: float = 0.7
    snr_r_db: float = 20.0                 # P1
    dwell_r_s: float = 2.0                 # P1
    sigma0_db: float = -20.0               # P1 地杂波
    clutter_k: float = 10.0                # P1：sigma ≥ k·sigma0·A_cell
    v_clutter_mps: float = 0.3             # P1


@dataclass(frozen=True)
class AcousticSpec:
    name: str = "mic"
    model_id: str = "mic8"
    band_hz: tuple[float, float] = (100.0, 8000.0)
    array_gain_db: float = 9.0
    nr_db: dict = field(default_factory=lambda: {"multirotor": 25.0, "fixed_wing": 35.0})
    r_ego_m: dict = field(default_factory=lambda: {"multirotor": 0.5, "fixed_wing": 1.0})
    bearing_sigma_deg: float = 5.0
    snr_d_db: float = 6.0
    snr_r_db: float = 10.0                 # P1
    n_harm: int = 3                        # P1


@dataclass(frozen=True)
class LidarSpec:
    name: str = "mid360"
    model_id: str = "mid360"
    pts_per_sr: float = float(LEVELS["lidar"]["pts_per_sr"])
    frame_hz: float = float(LEVELS["lidar"]["frame_hz"])
    r_ref_m: float = float(LEVELS["lidar"]["r_ref_m"])
    rho_ref: float = float(LEVELS["lidar"]["rho_ref"])
    rho_exp: float = float(LEVELS["lidar"]["rho_exp"])
    hard_max_m: float = float(LEVELS["lidar"]["hard_max_m"])
    blind_m: float = 0.1
    fov_v_deg: tuple[float, float] = (-7.0, 52.0)
    n_d: float = float(LEVELS["lidar"]["n_d"])
    n_r: float = float(LEVELS["lidar"]["n_r"])


# ---------------------------------------------------------------- 型号库
def _vehicles_dir() -> Path:
    from awr.sim.sensors.spec import vehicles_dir

    return vehicles_dir()


@cache
def _model_doc_cached(model_id: str, root: str) -> dict:
    import yaml

    p = Path(root) / "sensor_models" / f"{model_id}.yaml"
    if not p.is_file():
        raise PerceptionError(560, "sensor", "SENSOR_UNSUPPORTED", value=model_id)
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def load_model_doc(model_id: str) -> dict:
    """型号库文档（`vehicles/sensor_models/<model_id>.yaml`）；不存在 560。"""
    return _model_doc_cached(str(model_id), str(_vehicles_dir()))


def _num(d: Any, default: float) -> float:
    if isinstance(d, dict):
        d = d.get("value", default)
    try:
        v = float(d)
    except (TypeError, ValueError):
        return float(default)
    return v if math.isfinite(v) else float(default)


def illuminator_from_doc(doc: dict, *, kind: str | None = None, builtin: bool = False) -> Illuminator:
    """`awr.sensor.nir_illuminator.v1` 型号或 eo_zoom 的 `night.illuminator`（机内）→ Illuminator。"""
    if builtin:
        il = (doc.get("night") or {}).get("illuminator") or {}
        return Illuminator("builtin", float(il.get("p_w", 0.8)), None if il.get("divergence", "follow_hfov") == "follow_hfov"
                           else float(il.get("divergence_deg", 6.6)), float(il.get("r_ref_m", 200.0)), float(il.get("p_ref_w", 0.8)),
                           float(il.get("theta_ref_deg", 6.6)), float(il.get("rho_ref", 0.3)),
                           float(il["r_spec_max_m"]) if il.get("r_spec_max_m") is not None else None, float(il.get("wavelength_nm", 850.0)))
    ref = doc.get("ref") or {}
    return Illuminator(kind or str(doc.get("model_id", "nir_ill_l")), float(doc.get("p_w", 3.0)), float(doc.get("divergence_deg", 5.5)),
                       float(ref.get("r_m", 200.0)), float(ref.get("p_w", 0.8)), float(ref.get("theta_deg", 6.6)), float(ref.get("rho", 0.3)),
                       None, float(doc.get("wavelength_nm", 850.0)))


def eo_from_doc(doc: dict, *, name: str | None = None, analysis: str | None = None, ext: tuple[Illuminator, ...] = (),
                kind: Literal["eo", "nir"] = "eo") -> SensorSpecP:
    """`awr.sensor.eo_zoom.v1` 型号文档 → SensorSpecP（机内照明器在前，外挂在后）。"""
    sn = doc.get("sensor") or {}
    op = doc.get("optics") or {}
    fn = op.get("f_number") or {}
    outs = {str(o["name"]): (int(o["width_px"]), int(o.get("crop_w_px", sn.get("width_px")))) for o in doc.get("outputs") or []}
    crop_h = {str(o["name"]): int(o.get("crop_h_px", sn.get("height_px"))) for o in doc.get("outputs") or []}
    night = doc.get("night") or {}
    ill: tuple[Illuminator, ...] = ()
    if night.get("illuminator"):
        ill = (illuminator_from_doc(doc, builtin=True),)
    g = doc.get("gimbal") or {}
    ex = doc.get("exposure") or {}
    jit = doc.get("jitter_urad") or {}
    an = doc.get("analysis") or {}
    return SensorSpecP(
        name or str(doc.get("model_id", "eo")), kind, float(sn.get("pixel_um", 1.45)), int(sn.get("width_px", 3840)),
        int(sn.get("height_px", 2160)), float(op.get("f_min_mm", 4.8)), float(op.get("f_max_mm", 48.0)), float(fn.get("at_min", 1.7)),
        float(fn.get("at_max", 3.2)), outs, analysis or str(an.get("default", "1080p")), str(fn.get("law", "linear_f")),  # type: ignore[arg-type]
        e_min_color_lx=float(night.get("e_min_color_lx", _LI["e_min_color_lx"])), e_min_mono_lx=float(night.get("e_min_mono_lx", _LI["e_min_mono_lx"])),
        illuminators=ill + tuple(ext), pitch_min_deg=float(g.get("pitch_min_deg", -90.0)), pitch_max_deg=float(g.get("pitch_max_deg", 30.0)),
        jitter_short_urad=float(jit.get("fast", _BL["jitter_short_urad"])), jitter_long_urad=float(jit.get("slow", _BL["jitter_long_urad"])),
        model_id=str(doc.get("model_id", "")), t_exp_day_s=float(ex.get("day_s", _BL["t_exp_day_s"])),
        t_exp_low_s=float(ex.get("lowlight_s", _BL["t_exp_low_s"])), jitter_fast_max_s=float(jit.get("fast_max_exposure_s", _BL["t_short_max_s"])),
        k4_requires=an.get("k4_requires"), crop_h_px=crop_h)


def lwir_from_doc(doc: dict, lens_mm: float | None = None, *, name: str | None = None) -> SensorSpecP:
    """`awr.sensor.lwir.v1` 型号文档与镜头 → SensorSpecP（定焦：f_min = f_max；镜头不在型号表中 561）。"""
    det = doc.get("detector") or {}
    lenses = doc.get("lenses") or []
    fs = tuple(float(x["f_mm"]) for x in lenses)
    f = float(lens_mm) if lens_mm is not None else (fs[-1] if fs else 25.0)
    lens = next((x for x in lenses if abs(float(x["f_mm"]) - f) < 1e-9), None)
    if lenses and lens is None:
        raise PerceptionError(561, "lens_mm", "LENS_UNKNOWN", value=f, allowed=list(fs))
    fnum = float(lens.get("f_number", 1.0)) if lens else float(det.get("netd_f_number", 1.0))
    g = doc.get("gimbal") or {}
    jit = float(doc.get("jitter_urad", _BL["jitter_lwir_urad"]))
    clutter = LEVELS["lwir"]["sigma_clutter_k"]
    return SensorSpecP(
        name or str(doc.get("model_id", "lwir")), "lwir", float(det.get("pixel_um", 12.0)), int(det.get("width_px", 640)),
        int(det.get("height_px", 512)), f, f, fnum, fnum, {}, "native", "linear_f", netd_k=float(det.get("netd_mk", 50.0)) / 1000.0,
        sigma_clutter_k=(float(clutter["day"]), float(clutter["night"])), pitch_min_deg=float(g.get("pitch_min_deg", -90.0)),
        pitch_max_deg=float(g.get("pitch_max_deg", 30.0)), jitter_short_urad=jit, jitter_long_urad=jit, model_id=str(doc.get("model_id", "")),
        t_int_s=float(det.get("integration_s", _BL["t_exp_lwir_s"])), lens_mm=fs, digital_zoom_max=float(doc.get("digital_zoom_max", 1.0)))


def radar_from_doc(doc: dict, *, name: str | None = None) -> RadarSpec:
    fov = doc.get("fov") or {}
    rain = doc.get("rain") or {}
    cl = doc.get("clutter") or {}
    cls = doc.get("classify") or {}
    return RadarSpec(name or "radar", str(doc.get("model_id", "mmw77")), float(fov.get("az_deg", 60.0)), float(fov.get("el_deg", 15.0)),
                     float(doc.get("r_ref_m", 180.0)), float(doc.get("sigma_ref_m2", 1.0)), float(doc.get("snr_req_db", 13.2)),
                     float(doc.get("r_max_m", 400.0)), float(doc.get("range_res_m", 0.3)), float(doc.get("az_res_deg", 4.0)),
                     float(doc.get("vel_res_mps", 0.1)), 0.15, float(doc.get("az_acc_deg", 1.0)), 2.0, 0.05, float(rain.get("k", 1.0)),
                     float(rain.get("alpha", 0.7)), float(cls.get("snr_db", 20.0)), float(cls.get("dwell_s", 2.0)), float(cl.get("sigma0_db", -20.0)),
                     float(cl.get("k", 10.0)))


def acoustic_from_doc(doc: dict, *, name: str | None = None) -> AcousticSpec:
    cls = doc.get("classify") or {}
    band = doc.get("band_hz") or [100, 8000]
    return AcousticSpec(name or "mic", str(doc.get("model_id", "mic8")), (float(band[0]), float(band[1])), float(doc.get("array_gain_db", 9.0)),
                        {k: float(v) for k, v in (doc.get("nr_db") or {"multirotor": 25, "fixed_wing": 35}).items()},
                        {k: float(v) for k, v in (doc.get("r_ego_m") or {"multirotor": 0.5, "fixed_wing": 1.0}).items()},
                        float(doc.get("az_acc_deg", 5.0)), float(doc.get("detect_snr_db", 6.0)), float(cls.get("snr_db", 10.0)),
                        int(cls.get("bands", 3)))


def lidar_from_doc(doc: dict, *, name: str | None = None) -> LidarSpec:
    rg = doc.get("range") or {}
    fov = doc.get("fov") or {}
    return LidarSpec(name or "mid360", str(doc.get("model_id", "mid360")), float(LEVELS["lidar"]["pts_per_sr"]),
                     float(doc.get("frame_rate_hz", 10.0)), float(rg.get("r_at_rho10_m", 40.0)), 0.1, float(rg.get("exponent", 0.269)),
                     float(rg.get("hard_max_m", 100.0)), float(rg.get("blind_m", 0.1)),
                     (float(fov.get("v_min_deg", -7.0)), float(fov.get("v_max_deg", 52.0))))


# ---------------------------------------------------------------- 参考传感器（AWR-04 §6.1；AWR-20 §6）
def gx40(analysis: str = "1080p") -> SensorSpecP:
    try:
        return eo_from_doc(load_model_doc("eo_gx40"), name="eo_gx40", analysis=analysis)
    except Exception:  # 型号库不可用（单测替身目录）：规格常量
        builtin = Illuminator("builtin", 0.8, None, 200.0, 0.8, 6.6, 0.3, 200.0)
        return SensorSpecP("eo_gx40", "eo", 1.45, 3840, 2160, 4.8, 48.0, 1.7, 3.2,
                           {"4k": (3840, 3840), "1080p": (1920, 3840), "720p": (1280, 3840), "sxga": (1280, 2700), "1p3m": (1280, 2880)},
                           analysis, illuminators=(builtin,), model_id="eo_gx40", k4_requires="compute.orin_nx")


def gx40_nir(with_ext: bool = False, analysis: str = "1080p") -> SensorSpecP:
    s = gx40(analysis)
    ext: tuple[Illuminator, ...] = ()
    if with_ext:
        try:
            ext = (illuminator_from_doc(load_model_doc("nir_ill_l"), kind="nir_ill_l"),)
        except Exception:
            ext = (Illuminator("nir_ill_l", 3.0, 5.5, 200.0, 0.8, 6.6, 0.3, None),)
    return replace(s, kind="nir", illuminators=s.illuminators + ext)


def lwir640(lens_mm: float = 100.0) -> SensorSpecP:
    try:
        return lwir_from_doc(load_model_doc("lwir640"), lens_mm, name="lwir640")
    except PerceptionError:
        raise
    except Exception:
        return SensorSpecP("lwir640", "lwir", 12.0, 640, 512, lens_mm, lens_mm, 1.0, 1.0, {}, "native", netd_k=0.05, sigma_clutter_k=(2.0, 1.0),
                           jitter_short_urad=20.0, jitter_long_urad=20.0, model_id="lwir640", lens_mm=(13.0, 25.0, 75.0, 100.0))


def mmw77() -> RadarSpec:
    try:
        return radar_from_doc(load_model_doc("mmw77"))
    except Exception:
        return RadarSpec()


def mic8() -> AcousticSpec:
    try:
        return acoustic_from_doc(load_model_doc("mic8"))
    except Exception:
        return AcousticSpec()


def mid360() -> LidarSpec:
    try:
        return lidar_from_doc(load_model_doc("mid360"))
    except Exception:
        return LidarSpec()


def with_resolution(sn: SensorSpecP, resolution: str, *, capabilities: tuple[str, ...] | list[str] | None = None) -> SensorSpecP:
    """分析分辨率（M19-FR-002）：输出名须在型号中（561 `RESOLUTION_UNKNOWN`）；4K 需要 `compute.orin_nx`（561 `K4_REQUIRES`）。"""
    if sn.kind == "lwir" or sn.virtual:
        raise PerceptionError(561, "resolution", "RESOLUTION_FIXED", value=resolution)
    if resolution not in sn.outputs:
        raise PerceptionError(561, "resolution", "RESOLUTION_UNKNOWN", value=resolution, allowed=sorted(sn.outputs))
    if resolution == "4k" and sn.k4_requires and capabilities is not None and sn.k4_requires not in capabilities:
        raise PerceptionError(561, "resolution", "K4_REQUIRES", value=resolution, limit=sn.k4_requires)
    return replace(sn, analysis=resolution)


def sensor_for(name: str, *, lens_mm: float | None = None, resolution: str | None = None, ext: bool = False,
               light: Light = "day") -> SensorSpecP:
    """可行窗口与计算器的传感器选择：`eo_gx40`、`eo_z8`（EO；夜间按 NIR，带机内照明器与可选外挂）、`lwir640`（镜头）、
    `nir_ill_l`（等同 GX40 加外挂照明器的 NIR）。型号不存在 560。"""
    if name == "nir_ill_l":
        return gx40_nir(True, resolution or "1080p")
    doc = load_model_doc(name)
    schema = str(doc.get("schema", ""))
    if schema == "awr.sensor.eo_zoom.v1":
        ext_il: tuple[Illuminator, ...] = ()
        if ext:
            ext_il = (illuminator_from_doc(load_model_doc("nir_ill_l"), kind="nir_ill_l"),)
        s = eo_from_doc(doc, name=name, ext=ext_il)
        if resolution:
            s = with_resolution(s, resolution)
        return replace(s, kind="nir") if light == "night" and s.illuminators else s
    if schema == "awr.sensor.lwir.v1":
        return lwir_from_doc(doc, lens_mm, name=name)
    raise PerceptionError(560, "sensor", "NOT_IMAGING", value=name)


# ---------------------------------------------------------------- M13 成像状态 → 感知参数
_CACHE: dict[tuple, SensorSpecP] = {}
_FAST: dict[tuple, tuple] = {}


def _ill_from_m13(d: dict) -> Illuminator:
    sp = d.get("spec")
    kind = "builtin" if d.get("kind") == "builtin" else str(d.get("kind") or "nir_ill_l")
    if sp is None:
        return Illuminator(kind, float(d.get("p_w", 0.8)), math.degrees(float(d.get("theta_rad", 0.1))), float(d.get("range_m", 200.0)),
                           float(d.get("p_w", 0.8)), math.degrees(float(d.get("theta_rad", 0.1))))
    return Illuminator(kind, float(sp.p_w), None if sp.follow_hfov else math.degrees(sp.divergence_rad), float(sp.r_ref_m), float(sp.p_ref_w),
                       math.degrees(sp.theta_ref_rad), float(sp.rho_ref),
                       float(sp.r_spec_max_m) if math.isfinite(float(sp.r_spec_max_m)) else None, float(sp.wavelength_nm))


def imaging_state_spec(st: Any) -> tuple[SensorSpecP, SensorState]:
    """M13 `ImagingState`（`awr.sim.sensors.payload`）→ (SensorSpecP, SensorState)。

    eo_zoom：型号参数加当前分析分辨率、焦距、成像模式（nir 时 kind = nir）与照明器开关；LWIR：原生像元与镜头；D1 相机与热像
    （无变焦参数）：按内参的虚拟像元（p = 1 µm、f = fx·1e-3 mm，N 与 GSD 与 `d·fx/R` 一致，无衍射项）。"""
    sp = st.spec
    mode_eff = str(getattr(st, "mode", "day"))
    kind: str = "lwir" if st.kind == "lwir" else ("nir" if mode_eff == "nir" else "eo")
    st_ill = getattr(st, "illuminators", ()) or ()
    # D2-FX-R2-sim 第 2 次：M13 在载荷状态不变时复用同一照明器元组（payload `_state` 缓存），按其身份与其余键字段记忆结果
    # （照明器字段、规格与 SensorState 取值与逐次计算相同；只在 M19 每次评估一个平台时省去照明器展开与键构造）
    fk = (id(sp), str(st.analysis), kind, st.kind, int(getattr(st, "width_px", 0)), int(getattr(st, "height_px", 0)), id(st_ill),
          _nk(st.f_mm), str(getattr(st, "mode_cmd", "auto")), _nk(st.p_eff_um), _nk(st.f_number), _nk(st.jitter_urad),
          _nk(st.t_exp_s or 0.0), _nk(st.hfov_rad))
    hit = _FAST.get(fk)
    if hit is not None and hit[0] is st_ill and hit[1] is sp:
        return hit[2], hit[3]
    res = _imaging_state_spec(st, sp, kind, st_ill)
    if len(_FAST) > 512:
        _FAST.clear()
    _FAST[fk] = (st_ill, sp, res[0], res[1])
    return res


def _nk(x: Any) -> float | None:
    """记忆键的浮点分量（NaN 记为 None，使键可以相等）。"""
    v = float(x)
    return v if v == v else None


def _imaging_state_spec(st: Any, sp: Any, kind: str, st_ill: Any) -> tuple[SensorSpecP, SensorState]:
    ills = tuple(_ill_from_m13(d) for d in st_ill)
    key = (id(sp), str(st.analysis), kind, tuple((i.kind, i.p_w, i.divergence_deg) for i in ills), st.kind,
           float(st.f_mm) if st.kind == "lwir" else 0.0, int(getattr(st, "width_px", 0)))
    out = _CACHE.get(key)
    if out is None:
        z = getattr(sp, "zoom", None)
        doc = getattr(sp, "doc", {}) or {}
        if z is not None:
            night = doc.get("night") or {}
            g = getattr(sp, "gimbal", None)
            out = SensorSpecP(
                str(st.sensor), kind, float(z.pixel_um), int(z.width_px), int(z.height_px), float(z.f_min_mm), float(z.f_max_mm),
                float(z.fnum_min), float(z.fnum_max), {o.name: (int(o.w), int(o.crop_w)) for o in z.outputs}, str(st.analysis), str(z.law),  # type: ignore[arg-type]
                e_min_color_lx=float(night.get("e_min_color_lx", _LI["e_min_color_lx"])),
                e_min_mono_lx=float(night.get("e_min_mono_lx", _LI["e_min_mono_lx"])), illuminators=ills,
                pitch_min_deg=math.degrees(g.el_min_rad) if g is not None else -90.0, pitch_max_deg=math.degrees(g.el_max_rad) if g is not None else 30.0,
                jitter_short_urad=float(z.jitter_urad.get("fast", 10.0)), jitter_long_urad=float(z.jitter_urad.get("slow", 30.0)),
                model_id=str(getattr(sp, "model_id", "")), t_exp_day_s=float(z.exposure.get("day_s", 0.001)),
                t_exp_low_s=float(z.exposure.get("lowlight_s", 1.0 / 30.0)), jitter_fast_max_s=float(z.jitter_urad.get("fast_max_exposure_s", 0.004)),
                k4_requires=z.k4_requires, crop_h_px={o.name: int(o.crop_h) for o in z.outputs})
        elif st.kind == "lwir" and math.isfinite(float(st.p_eff_um)) and math.isfinite(float(st.f_mm)):
            det = doc.get("detector") or {}
            fnum = float(st.f_number) if math.isfinite(float(st.f_number)) else 1.0
            jit = float(st.jitter_urad) if math.isfinite(float(st.jitter_urad)) else 20.0
            out = SensorSpecP(str(st.sensor), "lwir", float(st.p_eff_um), int(det.get("width_px", st.width_px or 640)),
                              int(det.get("height_px", st.height_px or 512)), float(st.f_mm), float(st.f_mm), fnum, fnum, {}, "native",
                              netd_k=float(det.get("netd_mk", 50.0)) / 1000.0, jitter_short_urad=jit, jitter_long_urad=jit,
                              model_id=str(getattr(sp, "model_id", "")), t_int_s=float(st.t_exp_s) if st.t_exp_s else 0.01)
        else:  # D1 相机与热像：内参换算的虚拟像元
            w = int(getattr(st, "width_px", 0) or 640)
            h = int(getattr(st, "height_px", 0) or 480)
            hf = float(st.hfov_rad) if math.isfinite(float(st.hfov_rad)) and st.hfov_rad > 0 else math.radians(60.0)
            fx = 0.5 * w / math.tan(0.5 * hf)
            f = fx * 1e-3
            out = SensorSpecP(str(st.sensor), "lwir" if st.kind == "lwir" else "eo", 1.0, w, h, f, f, 0.0, 0.0, {}, "native",
                              jitter_short_urad=20.0 if st.kind == "lwir" else 10.0, jitter_long_urad=20.0 if st.kind == "lwir" else 30.0,
                              model_id=str(getattr(sp, "model_id", "")), virtual=True)
        if len(_CACHE) > 512:
            _CACHE.clear()
        _CACHE[key] = out
    illum_on = tuple(bool(d.get("on")) for d in (getattr(st, "illuminators", ()) or ()))
    f_now = out.f_min_mm if out.virtual or out.kind == "lwir" else float(st.f_mm)
    return out, SensorState(out, f_now, str(getattr(st, "mode_cmd", "auto")), None, illum_on)  # type: ignore[arg-type]
