"""无人机的声学与可见性签名（只读消费 M21 与机型包；ADR-097；AWR-20 §4.4；M18-M19 PRD §2.2 M21 行）。

M21（WP-08）的签名接口交付前，本模块直接由机型包文档（`Profile.doc` 的 `acoustic`、`visual` 段，D2 P600 2.0.0 已含）构造
`VehicleSignature`，缺省谱形按 AWR-20 §4.4 的 `default_band_shape` 生成（文件给出 `band_shape_db` 或 `derived.band_shape_db` 时
直接采用）；M21 交付后若 `awr.sim.catalog` 提供 `signature(model_id)`，优先使用（见 `signature_of`）。

声功率随推力：`L_WA(T) = L_WA,ref + 10·k·lg(T/T_ref)`（k = `thrust_exp`）；本包以悬停（多旋翼）或巡航（固定翼）推力为参考，
推力比缺省 1（悬停、巡航）。纯音带掩蔽余量 `M_b`：BPF 及其前 4 个谐波所在带 −4 dB，其余 0 dB（AWR-04 §7.3）。

D2.1 特征量（AWR-04 §15.10.2，ADR-124；WP-19b）：`rcs_m2`（机型文件 `rcs_m2.value`）、热斑温升 `ir_delta_t_k`（`ir.motor_delta_t_k`）、
热斑面积占比 `ir_hot_fraction`（`ir.hot_fraction`；M21 目录增加该字段之前按 §15.10.2 表的机型缺省值）、发射率 `ir_emissivity`
（`ir.emissivity`，缺省 0.9）与派生量：1 m 处声源级 `SL_A = L_WA − 11`（ISO 9613-2 的 `20·lg r + 11` 在 r = 1 m 处）、三向可见尺寸
`D_vis = √A` 与侧向热斑尺寸 `d_hot = √(A_side·hot_fraction)`。
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .sound import A_W, EXACT_HZ, N_BANDS, band_of

__all__ = ["HOT_FRACTION_DEFAULT", "P600", "VehicleSignature", "default_band_shape", "signature_from_doc", "signature_of"]

# 热斑面积占比的机型缺省（AWR-04 §15.10.2 表；M21 目录写入 `ir.hot_fraction` 后以文件为准）
HOT_FRACTION_DEFAULT = {"p600_mid360": 0.15, "awr_q1": 0.15, "awr_q9": 0.15, "awr_f1": 0.10, "awr_v1": 0.12}
HOT_FRACTION_FALLBACK = 0.15


def default_band_shape(bpf_hz: float, tonal_db: float, f_pk_hz: float, *, k_bb: float = 1.0, n_h: int = 5, dec: float = 3.0) -> np.ndarray:
    """AWR-20 §4.4 缺省 1/3 倍频程谱形（A 计权能量和为 0 dB）。"""
    B = -k_bb * np.log2(EXACT_HZ / float(f_pk_hz)) ** 2
    lin = 10.0 ** (B / 10.0)
    for h in range(1, n_h + 1):
        b = band_of(h * float(bpf_hz))
        if b >= 0:
            lin[b] += 10.0 ** ((B[b] + float(tonal_db) - dec * (h - 1)) / 10.0)
    S = 10.0 * np.log10(lin)
    c = -10.0 * math.log10(float(np.sum(10.0 ** ((S + A_W) / 10.0))))
    return S + c


def _tonal_mask(bpf_hz: float | None, n_h: int = 5) -> tuple[bool, ...]:
    m = [False] * N_BANDS
    if bpf_hz:
        for h in range(1, n_h + 1):
            b = band_of(h * float(bpf_hz))
            if b >= 0:
                m[b] = True
    return tuple(m)


@dataclass(frozen=True)
class VehicleSignature:
    """机型签名（前 7 个字段与 `tools/fake/targets_stub.VehicleSignature` 冻结形状一致，其后为追加的带缺省值字段）。"""

    model_id: str
    hold_sigma_h_m: float = 0.02     # RTK 档
    lwa_hover_dba: float | None = 88.0
    lwa_cruise_dba: float | None = None
    area_m2: tuple[float, float, float] = (0.36, 0.12, 0.12)   # top, side, front
    contrast_sky: float = -0.8
    lights_nav: bool = False
    contrast_ground: float = 0.3
    lights_strobe: bool = False
    night_visible_m: float = 30.0
    thrust_exp: float = 1.5
    bpf_hz: float | None = 150.0
    band_shape_db: tuple[float, ...] = field(default_factory=lambda: tuple(default_band_shape(150.0, 12.0, 1414.0)))
    tonal_mask: tuple[bool, ...] = field(default_factory=lambda: _tonal_mask(150.0))
    # ---- 追加（D2.1，AWR-04 §15.10.2）
    rcs_m2: float = 0.05
    ir_delta_t_k: float = 20.0
    ir_hot_fraction: float = HOT_FRACTION_FALLBACK
    ir_emissivity: float = 0.9

    @property
    def lwa_ref_dba(self) -> float:
        v = self.lwa_hover_dba if self.lwa_hover_dba is not None else self.lwa_cruise_dba
        return float(v if v is not None else 85.0)

    def lw_bands(self, thrust_ratio: float = 1.0) -> np.ndarray:
        """未计权带声功率级 L_W,b（dB）。"""
        lwa = self.lwa_ref_dba + 10.0 * self.thrust_exp * math.log10(max(float(thrust_ratio), 1e-3))
        return lwa + np.asarray(self.band_shape_db, float)

    def masking_db(self) -> np.ndarray:
        return np.where(np.asarray(self.tonal_mask, bool), -4.0, 0.0)

    def lwa_dba(self, thrust_ratio: float = 1.0) -> float:
        """A 计权声功率级 L_WA(T)（dB(A)）。"""
        return self.lwa_ref_dba + 10.0 * self.thrust_exp * math.log10(max(float(thrust_ratio), 1e-3))

    def sl_a_1m_dba(self, thrust_ratio: float = 1.0) -> float:
        """1 m 处声源级 `SL_A = L_WA − 11`（自由场，AWR-04 §15.10.2）。"""
        return self.lwa_dba(thrust_ratio) - 11.0

    def d_vis_faces(self) -> tuple[float, float, float]:
        """三向可见尺寸 `D_vis = √A`（顶、侧、前，m）。"""
        top, side, front = self.area_m2
        return math.sqrt(top), math.sqrt(side), math.sqrt(front)

    @property
    def d_hot_m(self) -> float:
        """侧向热斑尺寸 `d_hot = √(A_side·hot_fraction)`（m）。"""
        return math.sqrt(max(self.area_m2[1], 0.0) * max(self.ir_hot_fraction, 0.0))

    @property
    def lights_on(self) -> bool:
        return bool(self.lights_nav or self.lights_strobe)

    def d_vis(self, elev_rad: float) -> float:
        """视觉等效直径 √A：自识别物看无人机的仰角 ≥ 45° 取顶面截面，否则侧面（AWR-04 §7.3）。"""
        top, side, _front = self.area_m2
        return math.sqrt(top if elev_rad >= math.radians(45.0) else side)

    def with_lights(self, nav: bool, strobe: bool) -> VehicleSignature:
        from dataclasses import replace

        return replace(self, lights_nav=bool(nav), lights_strobe=bool(strobe))


def _hold_sigma(doc: dict) -> float:
    nav = doc.get("nav") or {}
    v = nav.get("hold_sigma_m")
    if isinstance(v, dict):
        v = v.get("h") or v.get("rtk") or next((x for x in v.values() if isinstance(x, (int, float))), None)
    return float(v) if isinstance(v, (int, float)) else 0.02


def signature_from_doc(model_id: str, doc: dict) -> VehicleSignature:
    """机型包文档（params.yaml）的 `acoustic`、`visual` 段 → 签名；缺段时取 P600 参考值。"""
    ac = doc.get("acoustic") or {}
    vi = doc.get("visual") or {}
    bpf = ac.get("bpf_hz", ac.get("bpf_cruise_hz"))
    bpf = float(bpf) if isinstance(bpf, (int, float)) else 150.0
    shape = ac.get("band_shape_db") or (doc.get("derived") or {}).get("band_shape_db")
    if not (isinstance(shape, list) and len(shape) == N_BANDS):
        shape = default_band_shape(bpf, float(ac.get("tonal_db", 12.0)), float(ac.get("f_pk_hz", 1414.0)))
    area = vi.get("area_m2") or {}
    top = float(area.get("top", 0.36)) if isinstance(area, dict) else 0.36
    side = float(area.get("side", 0.12)) if isinstance(area, dict) else 0.12
    front = float(area.get("front", side)) if isinstance(area, dict) else side
    lw_h = ac.get("lwa_hover_dba")
    lw_c = ac.get("lwa_cruise_dba")
    ir = doc.get("ir") or {}
    rcs = doc.get("rcs_m2")
    rcs = rcs.get("value") if isinstance(rcs, dict) else rcs
    hot = ir.get("hot_fraction")
    dtk = ir.get("motor_delta_t_k")
    emis = ir.get("emissivity")
    return VehicleSignature(
        model_id=str(model_id), hold_sigma_h_m=_hold_sigma(doc),
        lwa_hover_dba=float(lw_h) if isinstance(lw_h, (int, float)) else None,
        lwa_cruise_dba=float(lw_c) if isinstance(lw_c, (int, float)) else None,
        area_m2=(top, side, front), contrast_sky=float(vi.get("contrast_sky", -0.8)), lights_nav=False,
        contrast_ground=float(vi.get("contrast_ground", 0.3)), lights_strobe=False,
        night_visible_m=float(vi.get("night_visible_m", 30.0)), thrust_exp=float(ac.get("thrust_exp", 1.5)), bpf_hz=bpf,
        band_shape_db=tuple(float(x) for x in shape), tonal_mask=_tonal_mask(bpf),
        rcs_m2=float(rcs) if _pos_num(rcs) else 0.05,
        ir_delta_t_k=float(dtk) if isinstance(dtk, (int, float)) and not isinstance(dtk, bool) else 20.0,
        ir_hot_fraction=float(hot) if _pos_num(hot) else HOT_FRACTION_DEFAULT.get(str(model_id), HOT_FRACTION_FALLBACK),
        ir_emissivity=float(emis) if _pos_num(emis) else 0.9)


def _pos_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) and float(v) > 0.0


P600 = signature_from_doc("p600_mid360", {
    "acoustic": {"lwa_hover_dba": 88.0, "thrust_exp": 1.5, "bpf_hz": 150.0, "blades": 2, "tonal_db": 12.0, "f_pk_hz": 1414.0},
    "visual": {"area_m2": {"top": 0.22, "side": 0.19, "front": 0.19}, "contrast_sky": -0.8, "contrast_ground": 0.3, "night_visible_m": 30},
    "nav": {"hold_sigma_m": 0.02},
    "rcs_m2": {"value": 0.05}, "ir": {"motor_delta_t_k": 20.0},
})

_CACHE: dict[tuple[str, int], VehicleSignature] = {}


def signature_of(model_id: str, profiles: Any = None) -> VehicleSignature:
    """会话机型的签名：M21 `awr.sim.catalog.signature(model_id)`（已装配时）> ProfileTable 文档 > P600 参考值。按 (id, 表) 缓存。"""
    key = (str(model_id), id(profiles))
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    sig = None
    cat = sys.modules.get("awr.sim.catalog")
    fn = getattr(cat, "signature", None)
    if callable(fn):
        try:
            got = fn(model_id)
            if isinstance(got, VehicleSignature):
                sig = got
        except Exception:
            sig = None
    if sig is None and profiles is not None:
        try:
            p = profiles.get(model_id)
            sig = signature_from_doc(model_id, dict(getattr(p, "doc", {}) or {}))
        except Exception:
            sig = None
    if sig is None:
        sig = P600 if model_id == P600.model_id else VehicleSignature(str(model_id))
    if len(_CACHE) > 64:
        _CACHE.clear()
    _CACHE[key] = sig
    return sig
