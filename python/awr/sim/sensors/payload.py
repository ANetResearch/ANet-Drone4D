"""载荷状态块 `payload` 与变焦、成像模式、照明器推进；`uav/{id}/payload` 打包（M13-FR-203–205、FR-209、FR-217；M13 §15.3.2、§15.5）。

`payload` 块（M08 `register_state_block`，M13 唯一写者，参与 checkpoint），每 slot：

- 挂载：`mnt` 为按 sensor_no 的挂载位掩码（0 = D1 机型的完整传感器组，D1 行为不变）；`off` 为经 `sensor/mode{active: false}`
  关闭的传感器；`lens` 为 LWIR 镜头覆盖（每云台列，NaN 为文件值）。
- 云台指向源（每列，M13-FR-206，仲裁在 `gimbal.py`）：`src`；operator 层 `op_on`、`t_op`、`op_w`（angles、look_at、track、
  stow）、`op_a`（az、el）、`op_t`、`op_trk`；tasking 层 `tk_on`、`tk_t`；cue 层 `cu_on`、`t_cue`、`cu_t`；缺省层（D1 的
  任务项与生成器缺省）`df_mode`、`df_a`、`df_t`。
- 成像（每列所有者为 eo_zoom 时）：当前与指令焦距 `f`、`f_cmd`（mm；无光学变焦为 NaN），`im_cmd`（auto、day、lowlight、nir），
  `im_mode`（day、lowlight、nir），照明器 `ill_cfg`、`ill_on`（[机内, 外挂]），分析分辨率 `res`（0 缺省、1 = 4K），
  调色板 `pal`（LWIR 显示约定），`cue`（0 未设、1 on、2 off）。
- `band`：该机最近一次应用的照度档（自动模式只在档位变化时切换，确定性，无随机）；`scan`：MID-360 扫描状态（off、active、paused）。

变焦（50 Hz，`dt = 0.02 s`）：`f ← f·exp(clamp(ln(f_cmd/f), −s, s))`，`s = ln(f_max/f_min)·dt/t_full`（GX40 全程 4 s）；视场
`2·atan(W_crop/2f)`；F 数按型号的 `f_number.law`；机内照明器发散角取当前 HFOV。只依赖 numpy 与 math。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from awr.sim.fleet import kernels_sel as KS

from .block import N_GIMBAL
from .enums import (
    ILLUM_CFG_NAMES,
    IMAGING_MODE_NAMES,
    POINT_SRC_NAMES,
    IllumCfg,
    ImagingCmd,
    ImagingMode,
    LightBand,
    PointSrc,
    SensorKind,
)

if TYPE_CHECKING:
    from .runtime import SensorRuntime
    from .spec import Rig, SensorSpec

__all__ = [
    "ILL_BUILTIN",
    "ILL_EXT",
    "PAYLOAD_BLOCK",
    "PAYLOAD_FIELDS",
    "WIRE_MODES",
    "ImagingState",
    "PayloadBank",
    "PayloadView",
    "light_band_of",
]

PAYLOAD_BLOCK = "payload"
ILL_BUILTIN, ILL_EXT = 0, 1
WIRE_MODES = ("angles", "look_at", "track", "stow")  # operator 层 `op_w`（`uav/{id}/cmd/gimbal` 的 mode）
SCAN_NAMES = ("off", "active", "paused")
BAND_UNSET = 255

_U8, _I32, _I64, _U32, _F32, _F64 = np.uint8, np.int32, np.int64, np.uint32, np.float32, np.float64
NG = N_GIMBAL

PAYLOAD_FIELDS: dict[str, tuple[type, tuple[int, ...]]] = {
    "mnt": (_U32, ()),
    "off": (_U32, ()),
    "lens": (_F32, (NG,)),
    "src": (_U8, (NG,)),
    "op_on": (_U8, (NG,)),
    "t_op": (_I64, (NG,)),
    "op_w": (_U8, (NG,)),
    "op_a": (_F64, (NG, 2)),
    "op_t": (_F64, (NG, 3)),
    "op_trk": (_I32, (NG,)),
    "tk_on": (_U8, (NG,)),
    "tk_t": (_F64, (NG, 3)),
    "cu_on": (_U8, (NG,)),
    "t_cue": (_I64, (NG,)),
    "cu_t": (_F64, (NG, 3)),
    "df_mode": (_U8, (NG,)),
    "df_a": (_F64, (NG, 2)),
    "df_t": (_F64, (NG, 3)),
    "f": (_F64, (NG,)),
    "f_cmd": (_F64, (NG,)),
    "im_cmd": (_U8, (NG,)),
    "im_mode": (_U8, (NG,)),
    "ill_cfg": (_U8, (NG, 2)),
    "ill_on": (_U8, (NG, 2)),
    "res": (_U8, (NG,)),
    "pal": (_U8, (NG,)),
    "cue": (_U8, (NG,)),
    "band": (_U8, ()),
    "scan": (_U8, ()),
}

_BAND_BY_NAME = {"day": LightBand.DAY, "low": LightBand.LOW, "lowlight": LightBand.LOW, "night": LightBand.NIGHT}


def light_band_of(v: Any) -> LightBand | None:
    """照度档的各种写法（`rt/enums.json` LightBand 名称、整数、M07 关键帧 `sun.band`）-> LightBand。"""
    if v is None:
        return None
    if isinstance(v, (int, np.integer)) and 0 <= int(v) <= 2:
        return LightBand(int(v))
    b = _BAND_BY_NAME.get(str(getattr(v, "name", v)).lower())
    return b


@dataclass(frozen=True)
class ImagingState:
    """M19 只读的成像状态（`payload_view`、`imaging_specs`；M13 §15.3.4）。"""

    sensor: str
    sensor_no: int
    kind: str                  # eo、lwir、eo（D1 camera）、lwir（D1 thermal）
    col: int
    f_mm: float
    f_cmd_mm: float
    f_number: float
    hfov_rad: float
    vfov_rad: float
    p_eff_um: float
    analysis: str
    width_px: int
    height_px: int
    mode: str                  # day、lowlight、nir（无夜视的传感器为 day）
    mode_cmd: str              # auto、day、lowlight、nir
    active: bool
    t_exp_s: float
    jitter_urad: float
    illuminators: tuple[dict, ...]   # {sensor, kind, on, theta_rad, p_w, range_m, wavelength_nm}
    az_rad: float
    el_rad: float
    src: str
    spec: Any = None


@dataclass(frozen=True)
class PayloadView:
    uav: str
    slot: int
    agent_no: int
    t_sim_ns: int
    imaging: tuple[ImagingState, ...]
    mounted: tuple[str, ...]
    off: tuple[str, ...]
    band: str | None


class PayloadBank:
    def __init__(self, rt: SensorRuntime) -> None:
        self.rt = rt
        self.band_override: LightBand | None = None
        self.stats = {"zoom_steps": 0, "mode_switches": 0}
        self._stc: dict[tuple[int, int], tuple] = {}   # (slot, 列) -> (rig, spec, 键, 除云台角外的字段)；`_state` 的缓存
        self._cols: dict[int, tuple] = {}              # id(rig) -> (rig, 成像列, 传感器名, {关闭位: 关闭的传感器名})

    # ------------------------------------------------------------ 块与参数
    @property
    def blk(self) -> dict[str, np.ndarray] | None:
        S = self.rt.S
        return None if S is None else S.blocks.get(PAYLOAD_BLOCK)

    def light_band(self, ctx: Any) -> LightBand:
        """当前照度档：测试覆盖值 > M07 `env.light_band(t_ns)` > `env.sky_sample(t_ns).band_code`（WP-09 的 SkySample，1 s 量化
        缓存）> `env.sun_band` 属性 > 日间（无 M07 D2 部分时）。"""
        if self.band_override is not None:
            return self.band_override
        env = getattr(ctx, "env", None)
        if env is None:
            return LightBand.DAY
        t = int(getattr(ctx, "t_ns", 0))
        v: Any = None
        try:
            fn = getattr(env, "light_band", None)
            if callable(fn):
                v = fn(t)
            else:
                ss = getattr(env, "sky_sample", None)
                if callable(ss):
                    smp = ss(t)
                    v = getattr(smp, "band_code", None)
                    if v is None:
                        v = getattr(smp, "band", None)
                else:
                    v = getattr(env, "sun_band", None)
        except Exception:
            v = None
        b = light_band_of(v)
        return LightBand.DAY if b is None else b

    # ------------------------------------------------------------ 装配
    def on_spawn(self, s: int, rig: Rig, ctx: Any) -> None:
        """新机体的载荷缺省：云台 FIXED(缺省角) 写入缺省层；变焦取广角端；成像 auto 并按当前照度档定初始模式（不发事件）。"""
        P = self.blk
        if P is None:
            return
        P["t_op"][s] = 0
        P["t_cue"][s] = 0
        P["op_trk"][s] = -1
        P["lens"][s] = np.nan
        P["f"][s] = np.nan
        P["f_cmd"][s] = np.nan
        P["op_t"][s] = np.nan
        P["tk_t"][s] = np.nan
        P["cu_t"][s] = np.nan
        P["df_t"][s] = np.nan
        P["band"][s] = BAND_UNSET
        band: LightBand | None = None
        for k in range(NG):
            sp = rig.col_specs[k]
            if sp is not None and sp.gimbal is not None:
                g = sp.gimbal
                P["df_mode"][s, k] = 0  # GimbalMode.FIXED
                P["df_a"][s, k] = (g.default_az_rad, g.default_el_rad)
            if sp is not None and sp.zoom is not None:
                if band is None:
                    band = self.light_band(ctx)
                P["f"][s, k] = sp.zoom.f_min_mm
                P["f_cmd"][s, k] = sp.zoom.f_min_mm
                P["band"][s] = int(band)
                m = self._auto_mode(rig, k, band)
                P["im_mode"][s, k] = int(m)
                self._apply_illum(s, rig, k)

    # ------------------------------------------------------------ 成像模式（M13-FR-205，§15.6.1）
    @staticmethod
    def has_illuminator(rig: Rig, k: int) -> bool:
        sp = rig.col_specs[k]
        if sp is None:
            return False
        return sp.illum is not None or any(f.illum is not None for f in rig.followers(k))

    def _auto_mode(self, rig: Rig, k: int, band: LightBand) -> ImagingMode:
        if band == LightBand.DAY:
            return ImagingMode.DAY
        if band == LightBand.NIGHT and self.has_illuminator(rig, k):
            return ImagingMode.NIR
        return ImagingMode.LOWLIGHT

    def _apply_illum(self, s: int, rig: Rig, k: int) -> None:
        """照明器开关：AUTO 时随模式（NIR 开、其余关），ON、OFF 锁定；外挂照明器只在挂载且未关闭时可开。"""
        P = self.blk
        nir = int(P["im_mode"][s, k]) == ImagingMode.NIR
        sp = rig.col_specs[k]
        cfg_b, cfg_x = int(P["ill_cfg"][s, k, 0]), int(P["ill_cfg"][s, k, 1])
        has_b = sp is not None and sp.illum is not None
        ext = [f for f in rig.followers(k) if f.illum is not None]
        has_x = bool(ext) and not all((int(P["off"][s]) >> f.sensor_no) & 1 for f in ext)
        P["ill_on"][s, k, 0] = int(has_b and (cfg_b == IllumCfg.ON or (cfg_b == IllumCfg.AUTO and nir)))
        P["ill_on"][s, k, 1] = int(has_x and (cfg_x == IllumCfg.ON or (cfg_x == IllumCfg.AUTO and nir)))

    def set_mode(self, s: int, k: int, cmd: ImagingCmd, ctx: Any, *, reason: str = "command") -> None:
        P = self.blk
        rig = self.rt.rig_of(s)
        if P is None or rig is None:
            return
        P["im_cmd"][s, k] = int(cmd)
        if cmd == ImagingCmd.AUTO:
            band = self.light_band(ctx)
            P["band"][s] = int(band)
            new = self._auto_mode(rig, k, band)
        else:
            new = {ImagingCmd.DAY: ImagingMode.DAY, ImagingCmd.LOWLIGHT: ImagingMode.LOWLIGHT, ImagingCmd.NIR: ImagingMode.NIR}[cmd]
        self._switch(s, k, rig, new, ctx, reason)

    def _switch(self, s: int, k: int, rig: Rig, new: ImagingMode, ctx: Any, reason: str) -> None:
        P = self.blk
        old = ImagingMode(int(P["im_mode"][s, k]))
        P["im_mode"][s, k] = int(new)
        self._apply_illum(s, rig, k)
        if old != new:
            self.stats["mode_switches"] += 1
            ev = getattr(ctx, "events", None)
            if ev is not None:
                sp = rig.col_specs[k]
                uav = self.rt.uav_id(self.rt.S, s)
                ev.emit("sensor.mode", t_sim_ns=int(getattr(ctx, "t_ns", 0)), severity=1, uav=uav,
                        fields={"uav": uav, "sensor": sp.name if sp is not None else None, "from": IMAGING_MODE_NAMES[old],
                                "to": IMAGING_MODE_NAMES[new], "reason": reason})

    def step_modes(self, ctx: Any) -> int:
        """照度档变化时切换 auto 模式（同一 tick，按 slot 升序；M13-AC-203）。"""
        P = self.blk
        if P is None:
            return 0
        band = int(self.light_band(ctx))
        S = self.rt.S
        if KS.HAVE_NUMBA and P["band"].dtype == np.uint8:
            # 一次融合遍历（与下式逐位相同；D2-AC-08：50 Hz 调用，三步整容量筛选在主循环中约 27 µs）
            buf = np.empty(P["band"].shape[0], np.int64)
            cand = buf[:KS.sel_ne2_active(P["band"], BAND_UNSET, band, S.active, buf)]
        else:
            cand = np.flatnonzero((P["band"] != BAND_UNSET) & (P["band"] != band) & S.active)
        n = 0
        for s in cand.tolist():
            rig = self.rt.rig_of(s)
            P["band"][s] = band
            if rig is None:
                continue
            for k in range(NG):
                sp = rig.col_specs[k]
                if sp is None or sp.zoom is None or int(P["im_cmd"][s, k]) != ImagingCmd.AUTO:
                    continue
                self._switch(s, k, rig, self._auto_mode(rig, k, LightBand(band)), ctx, "light_band")
                n += 1
        return n

    # ------------------------------------------------------------ 变焦（M13-FR-204，§15.5 第 1 步）
    def step_zoom(self, dt_s: float) -> int:
        P = self.blk
        if P is None:
            return 0
        f, fc = P["f"], P["f_cmd"]
        if KS.HAVE_NUMBA and f.ndim == 2:
            # 一次融合遍历（行主序，与下式逐位相同；D2-AC-08：50 Hz 调用，整容量的三步掩码与 nonzero 约 36 µs）
            bi = np.empty(f.size, np.int64)
            bk = np.empty(f.size, np.int64)
            m = KS.unequal_finite2(f, fc, bi, bk)
            ii, kk = bi[:m], bk[:m]
        else:
            mv = np.isfinite(f) & np.isfinite(fc) & (f != fc)
            ii, kk = np.nonzero(mv)
        for s, k in zip(ii.tolist(), kk.tolist(), strict=True):
            rig = self.rt.rig_of(s)
            sp = None if rig is None else rig.col_specs[k]
            if sp is None or sp.zoom is None:
                f[s, k] = fc[s, k] = np.nan
                continue
            step = sp.zoom.log_rate * dt_s
            lam = math.log(float(fc[s, k]) / float(f[s, k]))
            if abs(lam) <= step * (1.0 + 1e-9):
                f[s, k] = fc[s, k]
            else:
                f[s, k] = float(f[s, k]) * math.exp(math.copysign(step, lam))
        self.stats["zoom_steps"] += int(ii.size)
        return int(ii.size)

    def zoom_to(self, s: int, k: int, f_mm: float) -> None:
        self.blk["f_cmd"][s, k] = float(f_mm)

    # ------------------------------------------------------------ 读取
    def imaging_states(self, s: int) -> list[ImagingState]:
        rt = self.rt
        b = rt.blk
        rig = rt.rig_of(s)
        if rig is None or b is None:
            return []
        out: list[ImagingState] = []
        cols = self._img_cols(rig)
        if cols:
            has = int(b["has"][s])
            for k, sp in cols:
                if has & sp.bit:
                    out.append(self._state(s, k, rig, sp))
        return out

    def _img_cols(self, rig: Rig) -> tuple:
        """rig 的成像列 ((列, spec), …)（相机与热像；按 rig 对象缓存，rig 在挂载变化时整体替换）。"""
        c = self._cols.get(id(rig))
        if c is None or c[0] is not rig:
            cols = tuple((k, sp) for k in range(NG) if (sp := rig.col_specs[k]) is not None
                         and sp.kind in (SensorKind.CAMERA, SensorKind.THERMAL))
            if len(self._cols) > 256:
                self._cols.clear()
            c = self._cols[id(rig)] = (rig, cols, tuple(sp.name for sp in rig.specs), {})
        return c[1]

    def _names(self, rig: Rig, off: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(全部传感器名, 关闭的传感器名)，按 (rig, 关闭位) 缓存。"""
        self._img_cols(rig)
        c = self._cols[id(rig)]
        offn = c[3].get(off)
        if offn is None:
            offn = c[3][off] = tuple(sp.name for sp in rig.specs if (off >> sp.sensor_no) & 1)
        return c[2], offn

    def _state(self, s: int, k: int, rig: Rig, sp: SensorSpec) -> ImagingState:
        """一列成像传感器的状态。D2-FX-R2-sim 第 2 次：除云台角外的字段只取决于 rig、spec 与载荷块的焦距、指令焦距、分辨率、模式、
        模式指令、照明器开关、指向源、关闭位与激活位，按 (slot, 列) 以这些值为键缓存（M19 每次评估一个平台读取一次，此前每次
        重建约 50–100 µs）；键相同则只换云台角构造（字段值相同）。"""
        rt = self.rt
        P, b = self.blk, rt.blk
        if P is not None:
            key: tuple = (float(P["f"][s, k]), float(P["f_cmd"][s, k]), int(P["res"][s, k]), int(P["im_mode"][s, k]),
                          int(P["im_cmd"][s, k]), bool(P["ill_on"][s, k, 0]), bool(P["ill_on"][s, k, 1]), int(P["src"][s, k]),
                          int(P["off"][s]), int(b["act"][s]))
        else:
            key = (int(b["act"][s]),)
        g_az, g_el = float(b["g_az"][s, k]), float(b["g_el"][s, k])
        hit = self._stc.get((s, k))
        if hit is not None and hit[0] is rig and hit[1] is sp and hit[2] == key:
            a = hit[3]
            return ImagingState(*a[:19], g_az, g_el, *a[19:])
        st = self._state_build(s, k, rig, sp)
        if len(self._stc) > 4096:
            self._stc.clear()
        self._stc[(s, k)] = (rig, sp, key, (st.sensor, st.sensor_no, st.kind, st.col, st.f_mm, st.f_cmd_mm, st.f_number, st.hfov_rad,
                                            st.vfov_rad, st.p_eff_um, st.analysis, st.width_px, st.height_px, st.mode, st.mode_cmd,
                                            st.active, st.t_exp_s, st.jitter_urad, st.illuminators, st.src, st.spec))
        return st

    def _state_build(self, s: int, k: int, rig: Rig, sp: SensorSpec) -> ImagingState:
        rt = self.rt
        P, b = self.blk, rt.blk
        z = sp.zoom
        if P is not None and z is not None:
            f = float(P["f"][s, k])
            fc = float(P["f_cmd"][s, k])
            res = "4k" if int(P["res"][s, k]) == 1 else z.analysis_default
            o = z.output(res)
            mode = ImagingMode(int(P["im_mode"][s, k]))
            cmd = ImagingCmd(int(P["im_cmd"][s, k]))
            fn, hf, vf, pe = z.fnum(f), z.hfov(f, res), z.vfov(f, res), z.p_eff_um(res)
            w, h = o.w, o.h
            exp_ = z.exposure.get("day_s", 0.001) if mode == ImagingMode.DAY else z.exposure.get("lowlight_s", 1.0 / 30.0)
            jit = z.jitter_urad
            jv = float(jit.get("fast", 10.0) if exp_ <= float(jit.get("fast_max_exposure_s", 0.004)) else jit.get("slow", 30.0))
        else:
            f = fc = float(sp.lens_mm) if math.isfinite(sp.lens_mm) else float("nan")
            it = sp.intr
            w, h = (it.w, it.h) if it is not None else (0, 0)
            hf, vf = sp.hfov_rad, sp.vfov_rad
            pe = float((sp.doc.get("detector") or {}).get("pixel_um", float("nan"))) if sp.schema_id == "awr.sensor.lwir.v1" \
                else float("nan")
            fn = next((x.f_number for x in sp.lenses if abs(x.f_mm - f) < 1e-9), float("nan")) if sp.lenses else float("nan")
            mode, cmd = ImagingMode.DAY, ImagingCmd.AUTO
            res = "native"
            exp_ = float((sp.doc.get("detector") or {}).get("integration_s", 0.01)) if sp.kind == SensorKind.THERMAL else 0.001
            jv = float(sp.doc.get("jitter_urad", 20.0)) if isinstance(sp.doc.get("jitter_urad"), (int, float)) else 20.0
        ill: list[dict] = []
        if P is not None:
            if sp.illum is not None:
                th = sp.illum.theta(hf)
                ill.append({"sensor": sp.name, "kind": "builtin", "on": bool(P["ill_on"][s, k, 0]), "theta_rad": th,
                            "p_w": sp.illum.p_w, "range_m": sp.illum.range_m(th), "wavelength_nm": sp.illum.wavelength_nm,
                            "spec": sp.illum})
            for fsp in rig.followers(k):
                if fsp.illum is None:
                    continue
                th = fsp.illum.theta(hf)
                ill.append({"sensor": fsp.name, "kind": fsp.model_id or "nir_ill_l", "on": bool(P["ill_on"][s, k, 1]),
                            "theta_rad": th, "p_w": fsp.illum.p_w, "range_m": fsp.illum.range_m(th),
                            "wavelength_nm": fsp.illum.wavelength_nm, "spec": fsp.illum})
        src = PointSrc(int(P["src"][s, k])) if P is not None else PointSrc.DEFAULT
        off = P is not None and (int(P["off"][s]) >> sp.sensor_no) & 1
        act = bool(rt.blk["act"][s] & sp.bit) and not off
        return ImagingState(sp.name, sp.sensor_no, sp.wire_kind, k, f, fc, fn, hf, vf, pe, res, w, h, IMAGING_MODE_NAMES[mode],
                            ("auto", "day", "lowlight", "nir")[int(cmd)], act, float(exp_), jv, tuple(ill),
                            float(b["g_az"][s, k]), float(b["g_el"][s, k]), POINT_SRC_NAMES[src], sp)

    def view_key(self, s: int) -> tuple | None:
        """`view(s)` 中除云台角之外全部字段的取值键（rig 身份、has/act/off/band 位、agent_no 与各成像列的焦距、指令焦距、分辨率、
        模式、模式指令、照明器开关、指向源）：键相同则 `view(s)` 的这些字段相同（D2-FX-R2-sim 第 2 次：M19 按键复用成像状态，
        每次评估只重算位姿）。slot 未就绪时为 None（与 `view` 返回 None 的条件相同）。"""
        rt = self.rt
        S = rt.S
        rig = rt.rig_of(s)
        if S is None or rig is None or not rt._ready(s):
            return None
        b = rt.blk
        P = self.blk
        has = int(b["has"][s])
        if P is None:
            return (id(rig), has, int(b["act"][s]), 0, BAND_UNSET, int(S.agent_no[s]), ())
        parts = tuple((k, float(P["f"][s, k]), float(P["f_cmd"][s, k]), int(P["res"][s, k]), int(P["im_mode"][s, k]),
                       int(P["im_cmd"][s, k]), bool(P["ill_on"][s, k, 0]), bool(P["ill_on"][s, k, 1]), int(P["src"][s, k]))
                      for k, sp in self._img_cols(rig) if has & sp.bit)
        return (id(rig), has, int(b["act"][s]), int(P["off"][s]), int(P["band"][s]), int(S.agent_no[s]), parts)

    def view(self, s: int) -> PayloadView | None:
        rt = self.rt
        S = rt.S
        rig = rt.rig_of(s)
        if S is None or rig is None or not rt._ready(s):
            return None
        P = self.blk
        off = int(P["off"][s]) if P is not None else 0
        band = int(P["band"][s]) if P is not None else BAND_UNSET
        names, offn = self._names(rig, off)
        return PayloadView(rt.uav_id(S, s), int(s), int(S.agent_no[s]), int(rt.tick_ns()), tuple(self.imaging_states(s)), names, offn,
                           None if band == BAND_UNSET else ("day", "low", "night")[band])

    # ------------------------------------------------------------ `uav/{id}/payload`（awr.uav.payload.v1）
    def message(self, s: int, t_sim_ns: int) -> dict | None:
        """一架机的 `awr.uav.payload.v1`（角度为弧度；云台取主云台列，无云台时为机体固连 0、0）。"""
        rt = self.rt
        S, b, P = rt.S, rt.blk, self.blk
        rig = rt.rig_of(s)
        if S is None or rig is None or b is None or not rt._ready(s):
            return None
        out: dict[str, Any] = {"uav": rt.uav_id(S, s), "t_sim_ns": int(t_sim_ns), "gimbal": self.gimbal_msg(s, rig), "sensors": []}
        off = int(P["off"][s]) if P is not None else 0
        imaging = {st.sensor: st for st in self.imaging_states(s)}
        for sp in rig.specs:
            if sp.kind in (SensorKind.GNSS, SensorKind.IMU, SensorKind.BARO):
                continue
            if P is not None and int(P["mnt"][s]) == 0 and not (b["has"][s] & sp.bit) and sp.bit:
                continue  # D1 剧本子集外的传感器
            on = not ((off >> sp.sensor_no) & 1)
            e: dict[str, Any] = {"sensor": sp.name, "kind": sp.wire_kind, "enabled": True, "active": on, "f_mm": None,
                                 "hfov_rad": None, "vfov_rad": None, "mode": None, "resolution": None, "palette": None,
                                 "cue": None, "illuminator": None}
            st = imaging.get(sp.name)
            if st is not None:
                e["active"] = st.active
                e["f_mm"] = _r(st.f_mm, 4)
                e["hfov_rad"] = _r(st.hfov_rad, 6)
                e["vfov_rad"] = _r(st.vfov_rad, 6)
                if sp.zoom is not None:
                    e["mode"] = st.mode
                    e["resolution"] = "4k" if st.analysis == "4k" else "1080p"
                    b_ill = next((x for x in st.illuminators if x["kind"] == "builtin"), None)
                    if b_ill is not None:
                        e["illuminator"] = {"on": b_ill["on"], "kind": "builtin", "power_w": b_ill["p_w"],
                                            "divergence_rad": _r(b_ill["theta_rad"], 6), "range_m": _r(b_ill["range_m"], 2)}
                if sp.schema_id == "awr.sensor.lwir.v1" and P is not None:
                    e["palette"] = ("white_hot", "black_hot")[int(P["pal"][s, st.col]) & 1]
                if P is not None:
                    e["cue"] = (None, "on", "off")[int(P["cue"][s, st.col]) % 3]
            elif sp.kind == SensorKind.ILLUMINATOR and sp.illum is not None:
                k = rig.col_of(sp.name)
                hf = float("nan")
                if k is not None:
                    st2 = imaging.get(rig.col_specs[k].name)
                    hf = st2.hfov_rad if st2 is not None else float("nan")
                th = sp.illum.theta(hf)
                ison = bool(P["ill_on"][s, k, 1]) if (P is not None and k is not None) else False
                e["illuminator"] = {"on": ison and on, "kind": "nir_ill_l" if not sp.illum.builtin else "builtin",
                                    "power_w": sp.illum.p_w, "divergence_rad": _r(th, 6), "range_m": _r(sp.illum.range_m(th), 2)}
            elif sp.kind == SensorKind.LIDAR:
                e["hfov_rad"] = _r(sp.hfov_rad, 6)
                e["vfov_rad"] = _r(sp.vfov_rad, 6)
                e["active"] = on and bool(b["act"][s] & sp.bit)
                e["mode"] = SCAN_NAMES[int(P["scan"][s]) % 3] if P is not None else "off"
            elif sp.kind == SensorKind.RADAR:
                e["hfov_rad"] = _r(sp.hfov_rad, 6)
                e["vfov_rad"] = _r(sp.vfov_rad, 6)
            out["sensors"].append(e)
        return out

    def gimbal_msg(self, s: int, rig: Rig) -> dict:
        rt = self.rt
        b, P = rt.blk, self.blk
        for k in range(NG):
            sp = rig.col_specs[k]
            if sp is None or sp.gimbal is None or not (b["has"][s] & sp.bit):
                continue
            src = PointSrc(int(P["src"][s, k])) if P is not None else PointSrc.DEFAULT
            trk = None
            if src == PointSrc.OPERATOR:
                w = int(P["op_w"][s, k])
                mode = WIRE_MODES[w] if w < len(WIRE_MODES) else "angles"
                if mode == "track":
                    trk = rt.target_id_of(int(P["op_trk"][s, k]))
            elif src == PointSrc.TASKING:
                mode = "look_at"
            elif src == PointSrc.CUE:
                mode = "cue"
            else:
                gm = int(b["g_mode"][s, k])
                mode = "look_at" if gm in (1, 2) else "fixed"
            return {"pitch_rad": _r(float(b["g_el"][s, k]), 6), "yaw_rad": _r(float(b["g_az"][s, k]), 6), "mode": mode,
                    "track_target": trk, "src": POINT_SRC_NAMES[src]}
        return {"pitch_rad": 0.0, "yaw_rad": 0.0, "mode": "fixed", "track_target": None, "src": "default"}

    def rest_sensors(self, s: int, t_sim_ns: int) -> dict | None:
        """`GET .../vehicles/{vid}/sensors`（17 §17.6.3）：`{payload, sensors[{name, type, kind, spec_ref, mount, state, mode, enabled}]}`。"""
        from .enums import SensorState
        from .frames import R_to_quat_xyzw

        rt = self.rt
        rig = rt.rig_of(s)
        msg = self.message(s, t_sim_ns)
        if rig is None or msg is None:
            return None
        P, b = self.blk, rt.blk
        off = int(P["off"][s]) if P is not None else 0
        modes = {e["sensor"]: e.get("mode") for e in msg["sensors"]}
        items = []
        for sp in rig.specs:
            st = "ACTIVE"
            if (off >> sp.sensor_no) & 1:
                st = "STANDBY"
            elif sp.kind in (SensorKind.CAMERA, SensorKind.LIDAR, SensorKind.THERMAL, SensorKind.RADAR, SensorKind.GNSS,
                             SensorKind.IMU):
                v = int(b["state"][s, int(sp.kind)])
                st = {SensorState.STANDBY: "STANDBY", SensorState.DEGRADED: "DEGRADED", SensorState.FAULT: "FAULT"}.get(v, "ACTIVE")
            q = R_to_quat_xyzw(np.asarray(sp.mount_R)[None])[0]
            items.append({"name": sp.name, "type": sp.schema_id.split(".")[2] if sp.schema_id else "", "kind": sp.wire_kind
                          if sp.kind not in (SensorKind.GNSS, SensorKind.IMU) else sp.schema_id.split(".")[2],
                          "spec_ref": sp.spec_ref or None,
                          "mount": {"pos_m": [_r(float(x), 4) for x in sp.mount_t], "q_xyzw": [_r(float(x), 6) for x in q]},
                          "state": st, "mode": modes.get(sp.name), "enabled": True})
        return {"payload": msg, "sensors": items}

    @staticmethod
    def illum_cfg_name(v: int) -> str:
        return ILLUM_CFG_NAMES[IllumCfg(int(v))]


def _r(x: float, nd: int) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(v) else round(v, nd)
