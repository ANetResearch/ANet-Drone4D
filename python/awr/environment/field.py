"""EnvironmentService 实现（M07-FR-016–FR-026；M07 §6.1、§7.1；g06 §6.1；实现 awr.sim.backends.base.EnvironmentService）。

组件：KeyframeManager（关键帧与操作）、AnchorIntegrator（20 ms 网格锚点）、GustScheduler（RNG 流 6）、TurbBox 与
DrydenBank（RNG 流 1）、共享资产（湍流盒、天气图）。env stage 每 20 ms 调用 `on_env_tick` 与 `query`；慢任务发心跳与
EnvSample32；`handle_query` 服务 `env/query`。

`on_env_tick` 内的固定顺序（§6.3.10）：①用旧版本把锚点推进到本网格点；②应用本网格点到期的全部操作（按 t_apply、再按
到达序）与阵风调度，各生成一个新版本；③本 tick 的 query 用最终版本求值。

风合成（§6.3.5）：W = s*f(z_agl)*e(theta) + w_mean*z_hat + sum G_k*(e_k, 0) + g(z_agl)*T；物理侧 z_agl = z − dtm(x, y)（M04
`ground_dtm`，地形跟随）；光学层以 `coordinate.ground.zM` 为 AGL 基准。本模块不读墙钟（ADR-049）。

D2（WP-09；AWR-04 §6.2、§6.3、§6.5、§7.3；ADR-090、ADR-101、ADR-102）：
- 时钟档：tick 长度取 `self.tick_ns`（stage 按 `ctx.dt_tick` 设置，d1_250 为 4 ms、sandbox100 为 10 ms），不再写死；
- 日历、太阳、照度档、热背景与声学背景由 SkyModel 维护：`env/calendar` 与 `env/set{patch: {acoustic_bg, humidity_pct,
  surface: {class}}}` 在网格点生效并发新版本；1 Hz【仿真】复核，太阳方向变化 ≥ 0.5°、照度档变化等时发新版本（只改 config.sun 与
  D2 字段）；SkyModel 启用前关键帧逐字节同 D1；
- EnvPerceptionView（M18-M19 PRD §6.11）：`optical_depth(..., wavelength_nm)`（550、850、905 nm 与 LWIR）、`illuminance`、
  `thermal_bg`、`acoustic_bg_bands`、`rain_rate_mmph`，另有 `sky_sample`（M19 EnvSample 的超集）与 `forecast`（M20 昼夜预报）。
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import msgpack
import numpy as np

from . import kernels_rows as KR
from .anchors import H_NS, AnchorIntegrator, Anchors
from .atmosphere import kernels_od as _KOD
from .atmosphere.isa import isa_arr
from .atmosphere.optics import (
    FLAG_BELOW_CLOUD_PRECIP,
    FLAG_IN_FOG_LAYER,
    H_HAZE,
    LWIR_MIN_NM,
    kim_factor,
    optical_depth_arr,
    optical_depth_lambda_arr,
    sigma_at_arr,
    sigma_wv_per_m,
)
from .conventions import e
from .io.assets import ensure_turb_box, ensure_weather_map
from .keyframe import ApplyResult, EnvKeyframe, EnvOp, KeyframeManager
from .query import EnvFlags, EnvSampleSoA, Fields, Frame, QueryError, parse_query, soa_to_reply
from .sky import acoustic as AC
from .sky.calendar import Calendar, CalendarError
from .sky.illum import BAND_NAMES, illum_band_arr, illuminance_lx_arr
from .sky.solar import solar_position_arr
from .sky.state import SEC_NS, SkyInputs, SkyModel, SkySample
from .sky.thermal import SURFACE_CLASSES, ThermalBg
from .weather.derive import K_MOR, Derived, derive
from .weather.presets import (
    BASE,
    COVER,
    DIR,
    DUST,
    FOG_TOP,
    GUST_AMP,
    ISA_DT,
    MOR_BG,
    NF,
    PRESETS,
    PRESETS_SHA256,
    RAIN,
    RH,
    SIGMA_REF,
    SNOW,
    SPEED_REF,
    W_MEAN,
    EnvError,
    presets,
)
from .weather.transitions import eval_env
from .wind.gust import MAX_ACTIVE, GustEvent, GustScheduler, gust_arr, gust_create, gust_expired
from .wind.profile import f_adv, profile_arr
from .wind.turbulence import (
    FT,
    SIGMA_W_OVER_REF,
    TURB_PERIOD,
    DrydenBank,
    TurbBox,
    bilinear_cc,
    ground_fade_arr,
    mil_sigma_arr,
    wind_fused,
)

__all__ = ["EnvironmentServiceImpl", "env_seed_of", "sky_geo_of", "world_config"]

log = logging.getLogger("awr.environment")

TICK_NS = 4_000_000
CALM_MPS = 1e-6
_WIND, _TURB, _OPTICS, _PRECIP, _THERMO = int(Fields.WIND), int(Fields.TURB_SPEC), int(Fields.OPTICS), int(Fields.PRECIP), int(Fields.THERMO)
_F_LOCAL, _F_ADDV_G, _F_ADDV_L = int(Frame.LOCAL), int(Frame.ADD_VELOCITY_GLOBAL), int(Frame.ADD_VELOCITY_LOCAL)
_F_GLOBAL, _WIND_PARTS = int(Frame.GLOBAL), int(Fields.WIND_PARTS)
_VALID, _CALM = int(EnvFlags.VALID), int(EnvFlags.CALM)
GUST_AMP_RANGE = (0.0, 30.0)
GUST_LEN_RANGE = (10.0, 500.0)
WARN_MOR_M = 1000.0
WARN_WIND_MPS = 12.0
WARN_RAIN_MMH = 7.6


def env_seed_of(world_seed: int) -> int:
    """env_seed = hash32(world_seed, "env")：sha256("env|world_seed=<s>") 前 4 字节小端（本文设定，与 field_version 同式）。"""
    return int.from_bytes(hashlib.sha256(f"env|world_seed={int(world_seed)}".encode()).digest()[:4], "little")


def sky_geo_of(coordinate: Mapping[str, Any] | None) -> tuple[float, float, float]:
    """coordinate.json -> (lat_deg, lon_deg, yaw_offset_deg)；缺省为 synthcity 示意锚点 30°N、120°E（ADR-077）。"""
    from .sky.state import DEFAULT_LATLON

    c = coordinate or {}
    a = c.get("anchor") or {}
    lat, lon = a.get("latDeg"), a.get("lonDeg")
    ok = isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and math.isfinite(lat) and math.isfinite(lon)
    yaw = (c.get("trueNorth") or {}).get("yawOffsetDeg")
    yaw = float(yaw) if isinstance(yaw, (int, float)) and math.isfinite(yaw) else 0.0
    return (float(lat), float(lon), yaw) if ok else (DEFAULT_LATLON[0], DEFAULT_LATLON[1], yaw)


def world_config(env_world: Mapping[str, Any] | None) -> dict[str, Any]:
    """presets.defaults.config 叠加世界 env.json（廓线、粗糙度）与 presets_sha256。"""
    cfg = copy.deepcopy(PRESETS["defaults"]["config"])
    if env_world:
        prof = cfg["wind"]["profile"]
        for k in ("kind", "z_ref_m", "alpha", "adv_height_m"):
            if (env_world.get("profile") or {}).get(k) is not None:
                prof[k] = env_world["profile"][k]
        for k in ("z0_m", "d_m"):
            if (env_world.get("roughness") or {}).get(k) is not None:
                prof[k] = env_world["roughness"][k]
    cfg["presets_sha256"] = PRESETS_SHA256
    return cfg


class EnvironmentServiceImpl:
    """sim-core 进程内的环境服务（向量化，单线程，主循环调用）。"""

    def __init__(self, *, world_id: str = "", world_query: Any = None, env_world: Mapping[str, Any] | None = None,
                 coordinate: Mapping[str, Any] | None = None, world_seed: int = 0, epoch: int = 1, shared_dir: Path | None = None,
                 rng_gust: np.random.Generator | None = None, rng_dryden: np.random.Generator | None = None, capacity: int = 1024,
                 bounds: tuple[tuple[float, ...], tuple[float, ...]] | None = None, initial_preset: str | None = None,
                 initial_patch: Mapping[str, Any] | None = None, load_assets: bool = True) -> None:
        from awr.contracts.rng_streams import Stream, rng

        self.P = presets()
        self.world_id = world_id or getattr(world_query, "world_id", "") or ""
        self.wq = world_query
        self._dtm_nb: tuple | bool | None = None
        self.env_world = dict(env_world or {})
        self.coordinate = dict(coordinate or {})
        self.world_seed = int(world_seed)
        self.seed = env_seed_of(world_seed)
        self.capacity = int(capacity)
        self.ground_z = float((self.coordinate.get("ground") or {}).get("zM", 0.0) or 0.0)
        h_anchor = (self.coordinate.get("anchor") or {}).get("hMslM")
        if h_anchor is None:
            h_anchor = (self.env_world.get("ground") or {}).get("h_msl_m", 0.0)
        self.h_anchor = float(h_anchor or 0.0)
        if bounds is None and world_query is not None and getattr(world_query, "bounds_m", None) is not None:
            b = np.asarray(world_query.bounds_m, np.float64)
            bounds = (tuple(b[0]), tuple(b[1]))
        self.bounds = bounds or ((-1000.0, -1000.0, 0.0), (1000.0, 1000.0, 300.0))
        self.warnings: list[dict[str, Any]] = []
        self.outbox: list[tuple[str, dict[str, Any]]] = []  # (kind, data)：由 stage 以事件发出
        cfg = world_config(self.env_world)
        preset = initial_preset or self.env_world.get("default_preset") or "clear"
        s0 = self.P.preset_vector(preset)
        if initial_patch:
            items, cfg_patch = self.P.validate_patch(initial_patch)
            for i, x in items:
                s0[i] = x
            if cfg_patch:
                from .keyframe import _merge

                cfg = _merge(cfg, cfg_patch)
            self.P.check_state(s0)
            if items and not all(self.P.fields[i].user_axis for i, _ in items):
                preset = None
        # 共享资产（K01/K02）
        self.shared_dir = Path(shared_dir) if shared_dir is not None else None
        self.turb: TurbBox | None = None
        self.assets: dict[str, Any] = {}
        tcfg = cfg["wind"]["turbulence"]
        if load_assets and self.shared_dir is not None:
            r = ensure_turb_box(self.shared_dir, self.seed, int(tcfg["n"]), float(tcfg["dx_m"]), float(tcfg["l_m"]))
            self.assets["turb"] = {"path": str(r.path), "built": r.built, "error": r.error}
            if r.volume is not None:
                self.turb = TurbBox.from_volume(r.volume)
            w = ensure_weather_map(self.shared_dir, self.seed, int(cfg["weather_map"]["n"]))
            self.assets["weather"] = {"path": str(w.path), "built": w.built, "error": w.error}
            if w.error:
                self._warn("asset_unavailable", "enter", {"asset": "weather"})
        if self.turb is None and tcfg["model"] == "box":
            tcfg["model"] = "off"
            if load_assets and self.shared_dir is not None:
                self._warn("asset_unavailable", "enter", {"asset": "turb"})
        self.km = KeyframeManager(world_id=self.world_id, seed=self.seed, epoch=epoch, config=cfg, initial=s0, initial_preset=preset,
                                  bounds_min=self.bounds[0], bounds_max=self.bounds[1],
                                  vmax_mps=float(self.P.client.get("wind_ramp_vmax_mps", 20)))
        # analytic-field streamlines (D1-ext): URL prefix in every keyframe's vis.streamlines when env.json enables them
        from .wind.streamlines import StreamlineCache, analytic_field_id

        csha = str(getattr(world_query, "coordinate_sha256", "") or self.env_world.get("coordinate_hash", "")).removeprefix("sha256:")
        self.field_id = analytic_field_id(self.world_id, csha, cfg["wind"]["profile"])
        self._sl_cache = StreamlineCache()
        if (self.env_world.get("streamlines") or {}).get("analytic", True):
            self.km.streamlines = f"/api/env/streamlines/{self.field_id}"
            self.km.kf.vis["streamlines"] = self.km.streamlines
        self.anchors = AnchorIntegrator(self.km.kf.anchors.copy())
        self.gusts = GustScheduler(rng_gust if rng_gust is not None else rng(world_seed, int(Stream.GUST_SCHEDULE)))
        self.dryden = DrydenBank(self.capacity, rng_dryden if rng_dryden is not None else rng(world_seed, int(Stream.DRYDEN)))
        self.pending: list[tuple[int, int, EnvOp]] = []
        self._seq = 0
        self.k_done = 0
        self.last_tick = -1
        self._s = np.empty(NF)
        self._d = Derived()
        self._s_t = -1
        self._eval_key: tuple = (None, None)  # _eval_now 的稳态缓存键（TransitionKf、风剖面配置对象）
        self._kf_key = 0
        self.buf = EnvSampleSoA.alloc(self.capacity)
        self._qbuf: EnvSampleSoA | None = None
        # EnvSample32 行缓存（按 slot），由 stage 每 tick 更新
        from awr.contracts.layouts import ENV_SAMPLE32

        self.fuse_wind = True  # GLOBAL 帧的风走融合路径（False：全部走 numpy 路径，测试用作对拍基准）
        self.rows = np.zeros(self.capacity, ENV_SAMPLE32)
        self.row_valid = np.zeros(self.capacity, bool)
        self._warn_state = {"mor_low": False, "wind_high": False, "precip_heavy": False}
        self.stats = {"ticks": 0, "versions": 1, "queries": 0}
        self.tick_ns = TICK_NS  # 时钟档 tick 长度（stage 按 ctx 设置，ADR-090）
        # D2 SkyModel（WP-09）：世界锚点经纬度、真北偏差与地面海拔
        lat, lon, yaw = sky_geo_of(self.coordinate)
        self.sky = SkyModel(lat_deg=lat, lon_deg=lon, yaw_offset_deg=yaw, h_ground_msl_m=self.h_anchor + self.ground_z)
        surf = (self.env_world.get("surface") or {}).get("class") if isinstance(self.env_world.get("surface"), Mapping) else None
        if surf in SURFACE_CLASSES:
            self.sky.set_surface(str(surf))
        self._fixed_sun = (float(cfg["sun"]["azimuth_deg"]), float(cfg["sun"]["elevation_deg"]))
        self._sky_last_s = -1
        self._kf_out = False
        self._eval_now()

    # ------------------------------------------------------------ 状态访问
    @property
    def kf(self) -> EnvKeyframe:
        return self.km.kf

    @property
    def epoch(self) -> int:
        return self.km.epoch

    @property
    def t_grid_ns(self) -> int:
        return self.k_done * H_NS

    @property
    def f_adv(self) -> float:
        return f_adv(self.kf.config["wind"]["profile"])

    def _warn(self, code: str, state: str, value: Any) -> None:
        w = {"code": code, "state": state, "value": value}
        self.warnings.append(w)
        self.outbox.append(("env.warning", w))

    def scalars(self, t_sim_ns: int, out: np.ndarray | None = None) -> np.ndarray:
        return np.asarray(eval_env(self.kf.transition(), int(t_sim_ns), np.empty(NF) if out is None else out))

    def derived(self, t_sim_ns: int) -> Derived:
        return derive(self.scalars(t_sim_ns), self.kf.config["wind"]["profile"])

    def _sd_at(self, t_sim_ns: int) -> tuple[np.ndarray, Derived]:
        """t 时刻的 (标量, 派生量)。稳态关键帧（`step` 或已过 t1）下与时刻无关，按 (过渡, 剖面) 缓存（与 `_eval_now` 同一判据，
        结果逐位相同；D2-FX-R2-sim 第 2 次：`optical_depth` 每次调用重算约 50 µs）。"""
        tr = self.kf.transition()
        prof = self.kf.config["wind"]["profile"]
        t = int(t_sim_ns)
        steady = tr.mode == "step" or t >= tr.t1_ns
        c = self.__dict__.get("_sd_cache")
        if steady and c is not None and c[0] is tr and c[1] is prof:
            return c[2], c[3]
        s = self.scalars(t)
        d = derive(s, prof)
        if steady:
            self.__dict__["_sd_cache"] = (tr, prof, s, d)
        return s, d

    def _eval_now(self) -> None:
        t = self.t_grid_ns
        tr = self.kf.transition()
        prof = self.kf.config["wind"]["profile"]
        # 稳态关键帧（`step` 或已过 t1）下 eval_env 恒为 kf.to：同一关键帧与剖面时 _s、_d 已是该值，跳过重算（逐位相同；
        # D2-AC-08：每个 50 Hz env tick 约 40 µs）
        if (tr.mode == "step" or t >= tr.t1_ns) and self._eval_key[0] is tr and self._eval_key[1] is prof:
            self._s_t = t
            return
        eval_env(tr, t, self._s)
        derive(self._s, prof, self._d)
        self._s_t = t
        self._eval_key = (tr, prof)

    def keyframe(self) -> EnvKeyframe:
        """当前完整帧（心跳形态：t_ns 为最近已推进的网格时刻，anchors 为该时刻的积分值）。"""
        self._kf_out = True  # 已对外给出（会话配置此后改走新版本，见 configure_session）
        return self.kf.with_heartbeat(self.t_grid_ns, self.anchors.A)

    def heartbeat_bytes(self) -> bytes:
        return self.keyframe().encode()

    # ------------------------------------------------------------ 操作（CommandEngine 锁存后调用）
    def op_from_msg(self, op: str, args: Mapping[str, Any], by: str = "") -> EnvOp:
        """`env/set`、`env/preset`、`env/gust` 的参数 -> EnvOp（形状错误抛 EnvError 300）。"""
        if not isinstance(args, Mapping):
            raise EnvError(300, {"field": "args"})
        if op in ("env/set", "env.set"):
            mode = args.get("mode", "smooth")
            if mode not in ("smooth", "step", "exp"):
                raise EnvError(110, {"field": "mode"})
            patch, d2 = _split_d2_patch(args.get("patch"))
            if d2 and not patch:
                return EnvOp("d2", d2=d2, by=by)
            return EnvOp("set", patch=patch, duration_s=args.get("duration_s"), mode=mode, by=by, d2=d2 or None)
        if op in ("env/calendar", "env.calendar"):
            start, tz, jump = args.get("start_utc"), args.get("tz"), args.get("jump", True)
            if not isinstance(start, str):
                raise EnvError(300, {"field": "start_utc"})
            if not isinstance(tz, str):
                raise EnvError(300, {"field": "tz"})
            if not isinstance(jump, bool):
                raise EnvError(300, {"field": "jump"})
            try:
                Calendar.from_request(start, tz, jump=False)  # 形状、年份与时区校验（生效时刻在 apply 中确定）
            except CalendarError as ex:
                raise EnvError(110, ex.detail) from ex
            return EnvOp("calendar", d2={"start_utc": start, "tz": tz, "jump": jump}, by=by)
        if op in ("env/preset", "env.preset"):
            if not isinstance(args.get("name"), str):
                raise EnvError(300, {"field": "name"})
            return EnvOp("preset", name=args["name"], duration_s=args.get("duration_s"), by=by)
        if op in ("env/gust", "env.gust"):
            return EnvOp("gust", amp_mps=args.get("amp_mps"), length_m=args.get("length_m"), dir_from_deg=args.get("dir_from_deg"),
                         by=by)
        if op in ("env/reset", "env.reset"):
            return EnvOp("reset", by=by)
        raise EnvError(300, {"field": "op", "op": op})

    def _t_apply(self, apply_tick: int) -> int:
        t_cmd = int(apply_tick) * self.tick_ns
        t = -(-t_cmd // H_NS) * H_NS
        return max(t, self.t_grid_ns + H_NS)

    def _check_gust(self, op: EnvOp) -> tuple[float, float, float]:
        def num(x: Any, name: str, lo: float, hi: float, default: float) -> float:
            if x is None:
                return default
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
                raise EnvError(110, {"field": name})
            v = float(x)
            if not lo <= v <= hi:
                raise EnvError(110, {"field": name, "min": lo, "max": hi})
            return v

        s = self._s
        amp = num(op.amp_mps, "amp_mps", GUST_AMP_RANGE[0], GUST_AMP_RANGE[1], float(s[GUST_AMP]) or 5.0)
        # 剧本给出全波长 length_m（lambda），事件字段 lam_m = 2*d_m
        lam = num(op.length_m, "length_m", 2 * GUST_LEN_RANGE[0], 2 * GUST_LEN_RANGE[1], 2.0 * float(PRESETS["constants"]["gust_length_default_m"]))
        dirf = num(op.dir_from_deg, "dir_from_deg", 0.0, 360.0, float(s[DIR])) % 360.0
        return amp, lam, dirf

    def apply(self, op: EnvOp, apply_tick: int) -> ApplyResult:
        """校验并排队；在 t_apply = ceil(t_cmd / H)*H 的网格点生效。非法操作抛 EnvError（不改状态）。"""
        t_apply = self._t_apply(apply_tick)
        route: list[str] = []
        warns: list[str] = []
        if op.kind == "gust":
            self._check_gust(op)
            n_live = len([ev for ev in self.kf.events if not gust_expired(self.anchors.A.s_m, ev, self.f_adv)])
            if n_live >= MAX_ACTIVE:
                warns.append("GUST_DEFERRED")
        elif op.kind == "reset":
            pass
        elif op.kind == "calendar":
            a = op.d2 or {}
            try:
                cal = Calendar.from_request(str(a.get("start_utc")), str(a.get("tz")), jump=bool(a.get("jump", True)), t_apply_ns=t_apply)
            except CalendarError as ex:
                raise EnvError(110, ex.detail) from ex
            op = EnvOp("calendar", d2={"cal0_unix_ms": cal.cal0_unix_ms, "tz": cal.tz}, by=op.by)
        elif op.kind == "d2":
            pass
        else:
            s_to, route, *_ = self.km.prepare(op, t_apply)
            if s_to[SPEED_REF] > WARN_WIND_MPS:
                warns.append("ENV_LIMIT")
        self._seq += 1
        self.pending.append((t_apply, self._seq, op))
        self.pending.sort(key=lambda x: (x[0], x[1]))
        n_before = sum(1 for p in self.pending if (p[0], p[1]) <= (t_apply, self._seq))
        return ApplyResult(self.kf.version + n_before, t_apply, list(route), warns)

    def apply_now(self, op: EnvOp) -> ApplyResult:
        """测试与剧本初值：在下一个网格点生效（等价于 apply_tick = 当前网格 tick）。"""
        return self.apply(op, (self.t_grid_ns // self.tick_ns) + 1)

    # ------------------------------------------------------------ 网格推进
    def reset(self, t_ns: int = 0) -> EnvKeyframe:
        """剧本重置（K06）：时钟回到 t_ns，step 帧、锚点归零、事件与待处理操作清空，version + 1。"""
        self.pending.clear()
        self.k_done = int(t_ns) // H_NS
        kf = self.km.reset(self.k_done * H_NS)
        self.anchors.reset(kf.anchors.copy())
        self.gusts.next_t_ns = None
        self.dryden.live[:] = False
        self.row_valid[:] = False
        self._kf_key += 1
        self._eval_now()
        self._stamp(kf, self.k_done * H_NS)
        self.outbox.append(("env.changed", {"version": kf.version, "by": "scenario", "reason": "reset"}))
        return kf

    def on_env_tick(self, tick: int) -> EnvKeyframe | None:
        frames = self.on_env_tick_all(tick)
        return frames[-1] if frames else None

    def on_env_tick_all(self, tick: int) -> list[EnvKeyframe]:
        t = int(tick) * self.tick_ns
        k = t // H_NS
        out: list[EnvKeyframe] = []
        if self.last_tick >= 0 and tick < self.last_tick:
            out.append(self.reset(k * H_NS))  # 时钟回退：剧本重置
        self.last_tick = int(tick)
        if k < self.k_done:
            return out
        # ① 锚点推进（旧版本）
        if k > self.k_done:
            self.anchors.advance_to(self.kf.transition(), k, self._kf_key)
            self.k_done = k
        tg = k * H_NS
        A = self.anchors.A
        # ② 到期操作
        while self.pending and self.pending[0][0] <= tg:
            A = self.anchors.A
            _t, _seq, op = self.pending[0]
            if op.kind == "gust":
                fa = self.f_adv
                n_live = len([ev for ev in self.kf.events if not gust_expired(A.s_m, ev, fa)])
                if n_live >= MAX_ACTIVE:
                    break  # 顺延到最早一个过期之后的网格点
                self.pending.pop(0)
                amp, lam, dirf = self._check_gust(op)
                ev = gust_create(self.km.next_event_id, tg, amp, 0.5 * lam, dirf, A.s_m, float(self._s_at(tg)[SPEED_REF]),
                                 self.bounds[0], self.bounds[1], fa)
                self.km.next_event_id += 1
                kf = self.km.add_event(ev, tg, A)
                reason = "scenario" if op.by == "scenario" else "gust"
            elif op.kind == "reset":
                self.pending.pop(0)
                kf = self.reset(tg)
                out.append(kf)
                continue
            elif op.kind in ("calendar", "d2"):
                self.pending.pop(0)
                self._apply_d2(op)
                kf = self.km.touch(tg, A)
                reason = "calendar" if op.kind == "calendar" else "scenario" if op.by == "scenario" else "d2"
            else:
                self.pending.pop(0)
                try:
                    kf = self.km.apply(op, tg, A)
                except EnvError as ex:  # 排队期间状态变化使操作失效（例如云底约束）：丢弃并告警
                    log.warning("env op dropped at apply", extra={"kv": {"code": ex.code, "detail": ex.detail}})
                    continue
                if op.d2:
                    self._apply_d2(op)
                reason = "scenario" if op.by == "scenario" else op.kind
            self._kf_key += 1
            self._stamp(kf, tg)
            out.append(kf)
            self.outbox.append(("env.changed", {"version": kf.version, "by": op.by or "operator", "reason": reason}))
        # 阵风调度（RNG 流 6）
        A = self.anchors.A
        s_now = self._s_at(tg)
        n_live = len([ev for ev in self.kf.events if not gust_expired(A.s_m, ev, self.f_adv)])
        g = self.gusts.on_grid(tg, s_now, int(self.kf.config["wind"]["level"]), n_live, H_NS)
        if g is not None:
            amp, d_m, dirf = g
            ev = gust_create(self.km.next_event_id, tg, amp, d_m, dirf, A.s_m, float(s_now[SPEED_REF]), self.bounds[0], self.bounds[1],
                             self.f_adv)
            self.km.next_event_id += 1
            kf = self.km.add_event(ev, tg, A)
            self._kf_key += 1
            self._stamp(kf, tg)
            out.append(kf)
            self.outbox.append(("env.changed", {"version": kf.version, "by": "scheduler", "reason": "gust"}))
        self._eval_now()
        self._sky_tick(tg, out)
        self._check_warnings()
        self.stats["ticks"] += 1
        self.stats["versions"] = self.kf.version
        return out

    def _s_at(self, t_ns: int) -> np.ndarray:
        return np.asarray(eval_env(self.kf.transition(), t_ns, np.empty(NF)))

    # ------------------------------------------------------------ D2：日历、太阳、热与声学背景（WP-09）
    def _apply_d2(self, op: EnvOp) -> None:
        a = op.d2 or {}
        if op.kind == "calendar":
            self.sky.set_calendar(Calendar(int(a["cal0_unix_ms"]), str(a["tz"])))
        if a.get("acoustic_bg") is not None:
            self.sky.set_acoustic(str(a["acoustic_bg"]))
        if a.get("surface_class") is not None:
            self.sky.set_surface(str(a["surface_class"]))
            self.sky.enabled = True
        self._sky_last_s = -1

    def _sky_inputs(self, s: np.ndarray, d: Derived) -> SkyInputs:
        return SkyInputs(cover=float(s[COVER]), rain_eff_mmh=float(d.rain_eff_mmh), rh=float(s[RH]), isa_dt_c=float(s[ISA_DT]),
                         cloud_base_m=float(s[BASE]), mor_m=float(d.mor_m), wetness=float(self.anchors.A.wetness))

    def sky_sample(self, t_sim_ns: int) -> SkySample:
        """t 所在仿真秒的环境感知量（1 s 量化，与订阅无关；M19 EnvSample 的超集）。"""
        tq = (int(t_sim_ns) // SEC_NS) * SEC_NS
        hit = self.sky.peek(tq, self.kf.version)
        if hit is not None:
            return hit
        if tq == self._s_t:
            s, d = self._s, self._d
        else:
            s = self._s_at(tq)
            d = derive(s, self.kf.config["wind"]["profile"])
        return self.sky.sample(tq, self._sky_inputs(s, d), self._fixed_sun, key=self.kf.version)

    def _stamp(self, kf: EnvKeyframe, t_ns: int) -> None:
        """新版本（尚未发出）写入 t_ns 处的 config.sun 与 D2 字段；SkyModel 未启用时不动（D1 帧不变）。"""
        if not self.sky.enabled:
            return
        tq = (int(t_ns) // SEC_NS) * SEC_NS
        s = np.asarray(eval_env(kf.transition(), tq, np.empty(NF)))
        d = derive(s, kf.config["wind"]["profile"])
        smp = self.sky.sample(tq, self._sky_inputs(s, d), self._fixed_sun, key=("stamp", kf.version))
        sun, top = SkyModel.wire(smp)
        cfg = dict(kf.config)
        cfg["sun"] = sun
        kf.config = cfg
        kf.d2 = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in top.items()}
        kf.cal = self.sky.calendar
        self.km.d2 = kf.d2
        self.km.cal = kf.cal
        prev = self.sky.published
        self.sky.mark_published(sun, top)
        if prev is not None and prev["sun"]["band"] != sun["band"]:
            self.outbox.append(("env.changed", {"version": kf.version, "by": "calendar", "reason": "illum_band", "band": sun["band"]}))

    def _sky_tick(self, tg: int, out: list[EnvKeyframe]) -> None:
        """1 Hz【仿真】复核：太阳方向变化 ≥ 0.5°、照度档、热交叉等变化时发新版本（只改 config.sun 与 D2 字段）。"""
        if not self.sky.enabled:
            return
        sec = tg // SEC_NS
        if sec == self._sky_last_s:
            return
        self._sky_last_s = sec
        sun, top = SkyModel.wire(self.sky_sample(tg))
        if not self.sky.changed(sun, top):
            return
        kf = self.km.touch(tg, self.anchors.A)
        self._kf_key += 1
        self._stamp(kf, tg)
        out.append(kf)

    def configure_session(self, cfg: Mapping[str, Any] | None, t_ns: int | None = None) -> None:
        """会话配置（沙盒 `session.json`、`sim/reset{sandbox_config}`、配置快照恢复；AWR-17 §17.4.5）：
        `{calendar?: {start_utc, tz}, acoustic_bg?, surface_class?, env?: {preset?, overrides?}}`。日历按 `t_cal = start_utc + t_sim`
        解释（jump = false）。尚未推进任何网格点且初始帧尚未对外给出时直接改写初始版本（不另发版本），否则在下一个网格点生效。
        非法值抛 EnvError。"""
        if not cfg:
            return
        ops: list[EnvOp] = []
        c = cfg.get("calendar")
        if c:
            if not isinstance(c, Mapping):
                raise EnvError(300, {"field": "calendar"})
            ops.append(self.op_from_msg("env/calendar", {"start_utc": c.get("start_utc"), "tz": c.get("tz"), "jump": False}, by="scenario"))
        d2: dict[str, Any] = {}
        if cfg.get("acoustic_bg") is not None:
            _, d2 = _split_d2_patch({"acoustic_bg": cfg["acoustic_bg"]})
        if cfg.get("surface_class") is not None:
            _, sc = _split_d2_patch({"surface": {"class": cfg["surface_class"]}})
            d2.update(sc)
        if d2:
            ops.append(EnvOp("d2", d2=d2, by="scenario"))
        env = cfg.get("env") if isinstance(cfg.get("env"), Mapping) else {}
        if env.get("preset"):
            ops.append(EnvOp("preset", name=str(env["preset"]), duration_s=0.0, by="scenario"))
        if env.get("overrides"):
            ops.append(EnvOp("set", patch=dict(env["overrides"]), duration_s=0.0, mode="step", by="scenario"))
        if self.stats["ticks"] == 0 and self.k_done == 0 and t_ns in (None, 0) and not self._kf_out:
            for op in ops:
                if op.kind == "calendar":
                    a = op.d2 or {}
                    cal = Calendar.from_request(str(a["start_utc"]), str(a["tz"]), jump=False)
                    self._apply_d2(EnvOp("calendar", d2={"cal0_unix_ms": cal.cal0_unix_ms, "tz": cal.tz}))
                elif op.kind == "d2":
                    self._apply_d2(op)
                else:
                    self.km.kf = self.km.apply(op, 0, self.anchors.A)
                    self.km.version -= 1  # 初始帧仍为版本 1
                    self.km.kf.version = self.km.version
                    self.km.initial = self.km.kf.to.copy()  # 剧本重置回到会话配置的天气
                    self.km.initial_preset = self.km.kf.to_preset
                    self._kf_key += 1
            self._eval_now()
            self._stamp(self.km.kf, 0)
            return
        for op in ops:
            self.apply_now(op)

    def session_snapshot(self, t_ns: int | None = None) -> dict[str, Any]:
        """配置快照中 M07 负责的部分（`awr.sandbox.snapshot.v1` 的 calendar、acoustic_bg、env）：日历取 t 处的日历时刻。"""
        t = self.t_grid_ns if t_ns is None else int(t_ns)
        P = self.P
        to = self.kf.to
        preset = self.kf.to_preset
        base = P.preset_vector(preset) if preset in P.ids else None
        overrides: dict[str, Any] = {}
        for i, f in enumerate(P.fields):
            if base is None or float(to[i]) != float(base[i]) or f.user_axis:
                g, k = f.path.split(".", 1)
                overrides.setdefault(g, {})[k] = float(to[i])
        out: dict[str, Any] = {"acoustic_bg": self.sky.acoustic, "env": {"preset": preset, "overrides": overrides}}
        if self.sky.calendar is not None:
            out["calendar"] = self.sky.calendar.to_config(t)
        return out

    # ------------------------------------------------------------ EnvPerceptionView（M18-M19 PRD §6.11）
    def illuminance(self, pos: np.ndarray, t_sim_ns: int) -> tuple[np.ndarray, np.ndarray]:
        n = np.asarray(pos).reshape(-1, 3).shape[0] if np.asarray(pos).size else 0
        smp = self.sky_sample(t_sim_ns)
        return np.full(n, smp.illum_lx), np.full(n, smp.band_code, np.uint8)

    def thermal_bg(self, xy: np.ndarray, t_sim_ns: int) -> ThermalBg:
        n = np.asarray(xy).reshape(-1, 2).shape[0] if np.asarray(xy).size else 0
        smp = self.sky_sample(t_sim_ns)
        return ThermalBg(np.full(n, smp.t_bg_c), np.full(n, smp.eps_bg), np.full(n, smp.t_sky_c), np.full(n, smp.k_cross))

    def acoustic_bg_bands(self, xy: np.ndarray, t_sim_ns: int) -> np.ndarray:
        n = np.asarray(xy).reshape(-1, 2).shape[0] if np.asarray(xy).size else 0
        return np.broadcast_to(self.sky_sample(t_sim_ns).l_bg_bands_dba, (n, AC.N_BANDS)).copy()

    def rain_rate_mmph(self, t_sim_ns: int) -> float:
        return float(self.sky_sample(t_sim_ns).rain_mmph)

    def sigma_lambda_ground(self, t_sim_ns: int, wavelength_nm: float) -> float:
        """地面（z_agl = 0）按波长的点消光（1/m），含降水；LWIR 含水汽项。"""
        tq = (int(t_sim_ns) // SEC_NS) * SEC_NS
        s = self._s_at(tq)
        d = derive(s, self.kf.config["wind"]["profile"])
        k = 1.0 if wavelength_nm == 550.0 else kim_factor(d, wavelength_nm)
        sig = (d.sigma_haze0 + (d.sigma_fog if float(s[FOG_TOP]) > 0 else 0.0)) * k + (d.sigma_precip if float(s[BASE]) > 0 else 0.0)
        if wavelength_nm >= LWIR_MIN_NM:
            sig += sigma_wv_per_m(self.sky_sample(tq).rho_v_gm3)
        return float(sig)

    def forecast(self, t0_ns: int, horizon_s: float, step_s: float = 3600.0) -> dict[str, np.ndarray]:
        """昼夜预报（M20 §9.3、可持续性评估）：太阳为确定量，天气取当前状态（会话天气时间表由调用方叠加）。
        -> {t_ns, az_deg, el_deg, illum_lx, band, k_cross}；未设日历时太阳固定。"""
        n = int(max(0.0, horizon_s) // max(step_s, 1e-3)) + 1
        t = int(t0_ns) + (np.arange(n, dtype=np.int64) * round(step_s * 1e9))
        cal = self.sky.calendar
        cover = float(self._s_at(int(t0_ns))[COVER])
        if cal is None:
            az = np.full(n, self._fixed_sun[0])
            el = np.full(n, self._fixed_sun[1])
            kc = np.ones(n)
        else:
            ms = cal.cal0_unix_ms + t / 1e6
            az, el = solar_position_arr(ms, self.sky.lat, self.sky.lon)
            az = np.mod(az - self.sky.yaw_off, 360.0)
            _, eb = solar_position_arr(ms - 3_600_000.0, self.sky.lat, self.sky.lon)
            _, ea = solar_position_arr(ms + 3_600_000.0, self.sky.lat, self.sky.lon)
            h0 = -0.833
            kc = np.where((el >= h0) & ((eb < h0) | (ea < h0)), 0.5, 1.0)
        return {"t_ns": t, "az_deg": az, "el_deg": el, "illum_lx": illuminance_lx_arr(el, cover, self.sky.moon_lx),
                "band": illum_band_arr(el), "k_cross": kc}

    def next_band_change(self, t_ns: int, horizon_s: float = 86400.0) -> tuple[int, str, str] | None:
        """t 之后 horizon 内第一次照度档变化：-> (t_change_ns, band_from, band_to)，按 60 s 步长搜索再二分到 1 s。"""
        if self.sky.calendar is None:
            return None
        f = self.forecast(t_ns, horizon_s, 60.0)
        b = f["band"]
        idx = np.nonzero(b != b[0])[0]
        if idx.size == 0:
            return None
        j = int(idx[0])
        lo, hi = int(f["t_ns"][j - 1]), int(f["t_ns"][j])
        b0 = int(b[0])
        while hi - lo > SEC_NS:
            mid = (lo + hi) // 2
            if int(self.forecast(mid, 0.0)["band"][0]) == b0:
                lo = mid
            else:
                hi = mid
        return hi, BAND_NAMES[b0], BAND_NAMES[int(b[j])]

    def _check_warnings(self) -> None:
        d, s = self._d, self._s
        for code, on, val in (("mor_low", d.mor_m < WARN_MOR_M, d.mor_m), ("wind_high", s[SPEED_REF] > WARN_WIND_MPS, float(s[SPEED_REF])),
                              ("precip_heavy", d.rain_eff_mmh > WARN_RAIN_MMH, d.rain_eff_mmh)):
            if on != self._warn_state[code]:
                self._warn_state[code] = on
                self._warn(code, "enter" if on else "leave", round(float(val), 3))

    # ------------------------------------------------------------ 求值
    def _anchors_at(self, t_ns: int) -> Anchors:
        if t_ns == self.t_grid_ns:
            return self.anchors.A
        return self.anchors.at(self.kf.transition(), t_ns)

    def _wind_only_fused(self, pos: np.ndarray, s: np.ndarray, A: Any, cfg: Mapping[str, Any], level: int, fa: float,
                         agent_idx: np.ndarray | None, o: EnvSampleSoA) -> bool:
        """只求风（env stage 5 个 env tick 中的 4 个）的 numba 融合路径（`turbulence.wind_fused`）：无阵风事件、GLOBAL 帧、
        地面高可走 DTM 双线性或常数时使用，与下方 numpy 路径逐位相同；否则返回 False。N = 1000 时 env stage 约 1.3 ms →
        约 0.4 ms（FX2-R2，D1-AC-07 的偶数 tick + env 叠加）。"""
        if level >= 1 and self.kf.events:
            return False
        if self.wq is not None:
            g = self._dtm_nb
            if g is None:
                g = self._dtm_nb = self._dtm_args()
            if not g:
                return False
            dtm = g
        else:
            dtm = None
        model = cfg["turbulence"]["model"]
        mode = 0
        dq = dry = None
        if level >= 1 and model != "off":
            if model == "box" and self.turb is not None:
                mode = 1
                dq = np.mod(fa * np.asarray(A.d_enu_m, np.float64), TURB_PERIOD)
            elif model == "dryden" and agent_idx is not None:
                mode = 2
                dry = self.dryden.out
            else:
                return False
        ex, ey = e(float(s[DIR]))
        n = pos.shape[0]
        ok = wind_fused(pos, dtm, float(self.ground_z), cfg["profile"], float(s[SPEED_REF]), ex, ey, float(s[W_MEAN]), mode,
                        float(s[SIGMA_REF]), dq, self.turb if mode == 1 else None, dry, agent_idx, o, _VALID, _CALM, CALM_MPS)
        if not ok:
            return False
        o.gust_long_mps[:n] = 0.0
        o.source_level[:n] = min(level, 1)
        return True

    def _z_ground(self, xy: np.ndarray) -> np.ndarray:
        if self.wq is not None:
            g = self._dtm_nb
            if g is None:
                g = self._dtm_nb = self._dtm_args()
            if g:
                v = bilinear_cc(xy, *g)
                if v is not None:  # 与 M04 ground_dtm 相同：float32 结果再转 float64
                    return v.astype(np.float32).astype(np.float64)
            try:
                return np.asarray(self.wq.ground_dtm(xy), np.float64)
            except Exception:
                pass
        return np.full(xy.shape[0], self.ground_z)

    def _dtm_args(self) -> tuple | bool:
        """DTM 栅格（M04 `dtm_grid()` 只读视图，10 m）的 float64 一维副本与几何参数；取不到时为 False（走 M04 路径）。"""
        try:
            gv = self.wq.dtm_grid()
            a = np.asarray(gv.a)
            if a.ndim != 2 or a.size > (1 << 21):
                return False
            return (np.ascontiguousarray(a, np.float64).reshape(-1), a.shape[1], a.shape[0], float(gv.x0_m),
                    float(gv.y0_m), float(gv.cell_m))
        except Exception:
            return False

    def query(self, pos: np.ndarray, t_sim_ns: int, *, fields: int = Fields.DEFAULT, frame: Frame = Frame.GLOBAL,
              vel: np.ndarray | None = None, quat_xyzw: np.ndarray | None = None, agent_idx: np.ndarray | None = None,
              out: EnvSampleSoA | None = None) -> EnvSampleSoA:
        pos = np.asarray(pos, np.float64).reshape(-1, 3)
        n = pos.shape[0]
        fields = int(fields)
        frame = int(frame)
        o = out if (out is not None and out.capacity >= n) else EnvSampleSoA.alloc(max(n, 1))
        o.n = n
        t = int(t_sim_ns)
        if t == self._s_t:
            s, d = self._s, self._d
        else:
            s = self.scalars(t)
            d = derive(s, self.kf.config["wind"]["profile"])
        A = self._anchors_at(t)
        cfg = self.kf.config["wind"]
        level = int(cfg["level"])
        prof = cfg["profile"]
        fa = f_adv(prof)
        # GLOBAL 帧的风（均值、阵风、湍流、合成与 VALID/CALM 标志）先走融合路径（与下方 numpy 路径逐位相同）；只求风时
        # 直接返回，全量求值时其余字段照常计算（FX2-R3：env stage 的 10 Hz 全量 tick 此前整段走 numpy，N = 1000 约 0.83 ms）
        wind_ok = self.fuse_wind and frame == _F_GLOBAL and n and self._wind_only_fused(pos, s, A, cfg, level, fa, agent_idx, o)
        if wind_ok and fields == _WIND_PARTS:
            return o
        if wind_ok and KR.HAVE_NUMBA:
            # 风之外的字段同样一次遍历写完（env 全量 tick，ADR-073 第 3 条；与下方 numpy 路径逐字节相同）
            KR.full_rest(o, pos, self._z_ground(pos[:, :2]), n, self.ground_z, self.h_anchor,
                         SIGMA_W_OVER_REF * float(s[SIGMA_REF]), FT, d.sigma_haze0, H_HAZE, d.sigma_fog, d.sigma_precip,
                         float(s[FOG_TOP]), float(s[BASE]), K_MOR, FLAG_IN_FOG_LAYER, FLAG_BELOW_CLOUD_PRECIP,
                         fields & (_OPTICS | _WIND), fields & _TURB, fields & _PRECIP, fields & _THERMO, d.rain_eff_mmh,
                         d.snow_eff_mmh, float(s[DUST]), float(s[ISA_DT]), float(s[RH]), min(level, 1))
            return o
        z_agl = pos[:, 2] - self._z_ground(pos[:, :2])
        su, sw = mil_sigma_arr(z_agl, float(s[SIGMA_REF]))
        if wind_ok:
            flags = o.flags[:n].copy()
        else:
            ex, ey = e(float(s[DIR]))
            spd = float(s[SPEED_REF])
            wm = o.wind_mean_mps[:n]
            f = profile_arr(z_agl, prof)
            wm[:, 0] = spd * f * ex
            wm[:, 1] = spd * f * ey
            wm[:, 2] = float(s[W_MEAN])
            wg = o.wind_gust_mps[:n]
            glong = np.zeros(n)
            if level >= 1 and self.kf.events:
                gust_arr(pos, A.s_m, self.kf.events, fa, wg, (ex, ey), glong)
            else:
                wg[:] = 0.0
            o.gust_long_mps[:n] = glong
            wt = o.wind_turb_mps[:n]
            wt[:] = 0.0
            model = cfg["turbulence"]["model"]
            if level >= 1 and model != "off":
                g = ground_fade_arr(z_agl)
                if model == "box" and self.turb is not None:
                    Dq = np.mod(fa * np.asarray(A.d_enu_m, np.float64), TURB_PERIOD)
                    b = self.turb.sample(pos - Dq)
                    wt[:, 0] = su * b[:, 0] * g
                    wt[:, 1] = su * b[:, 1] * g
                    wt[:, 2] = sw * b[:, 2] * g
                elif model == "dryden" and agent_idx is not None:
                    wt[:] = self.dryden.out[np.asarray(agent_idx, np.int64)] * g[:, None]
            w = o.wind_mps[:n]
            np.add(wm, wg, out=w)
            w += wt
            # 帧
            if frame in (_F_ADDV_G, _F_ADDV_L) and vel is not None:
                w -= np.asarray(vel, np.float64).reshape(n, 3)
            if frame in (_F_LOCAL, _F_ADDV_L) and quat_xyzw is not None:
                w[:] = _rotate_inv(np.asarray(quat_xyzw, np.float64).reshape(n, 4), w)
            flags = np.full(n, _VALID, np.uint8)
            calm = np.hypot(wm[:, 0], wm[:, 1]) < CALM_MPS
            flags |= np.where(calm, _CALM, 0).astype(np.uint8)
        if fields & (_OPTICS | _WIND):
            z_opt = pos[:, 2] - self.ground_z
            sig, fl = sigma_at_arr(z_opt, d, float(s[FOG_TOP]), float(s[BASE]))
            flags |= fl
            o.sigma_ext_per_m[:n] = sig
            o.mor_m[:n] = K_MOR / np.maximum(sig, 1e-12)
            o.sigma_precip_per_m[:n] = np.where(z_opt < float(s[BASE]), d.sigma_precip, 0.0)
        o.flags[:n] = flags
        o.source_level[:n] = min(level, 1)
        if fields & _TURB:
            hft = np.maximum(z_agl / FT, 10.0)
            Lu = hft / (0.177 + 0.000823 * hft) ** 1.2 * FT
            o.turb_sigma_mps[:n, 0] = su
            o.turb_sigma_mps[:n, 1] = su
            o.turb_sigma_mps[:n, 2] = sw
            o.turb_l_m[:n, 0] = Lu
            o.turb_l_m[:n, 1] = Lu
            o.turb_l_m[:n, 2] = hft * FT
        if fields & _PRECIP:
            o.rain_eff_mmh[:n] = d.rain_eff_mmh
            o.snow_eff_mmh[:n] = d.snow_eff_mmh
            o.dust[:n] = float(s[DUST])
        if fields & _THERMO:
            tc, pp, rho = isa_arr(self.h_anchor + pos[:, 2], float(s[ISA_DT]))
            o.temperature_c[:n] = tc
            o.pressure_pa[:n] = pp
            o.rho_kgm3[:n] = rho
            o.rh[:n] = float(s[RH])
        return o

    def optical_depth(self, p0: np.ndarray, p1: np.ndarray, t_sim_ns: int, *, wavelength_nm: float = 550.0) -> np.ndarray:
        """p0 → p1 的光学厚度：550 nm 为 sigma = ln20/MOR（ADR-023，D1 逐位不变）；850、905 nm 气溶胶按 Kim 缩放；
        LWIR（≥ 8 µm，传 10000）另加水汽项（D2，AWR-04 §6.3 第 4 步）。透过率 T = exp(−od)。"""
        p0 = np.asarray(p0, np.float64).reshape(-1, 3)
        p1 = np.asarray(p1, np.float64).reshape(-1, 3)
        s, d = self._sd_at(t_sim_ns)
        lam = float(wavelength_nm)
        if _KOD.HAVE_NUMBA:
            # D2-FX-R2-sim 第 2 次：逐对核（与下面的 numpy 实现逐元素同式）
            if lam == 550.0:
                mode, kim, wv = 0, 1.0, 0.0
            else:
                lwir = lam >= LWIR_MIN_NM
                rho_v = self.sky_sample(t_sim_ns).rho_v_gm3 if lwir else 0.0
                mode, kim, wv = (3 if lwir else 1), kim_factor(d, lam), (sigma_wv_per_m(rho_v) if lwir else 0.0)
            out = np.empty(p1.shape[0])
            _KOD.od_pairs(np.ascontiguousarray(p0), np.ascontiguousarray(p1), float(self.ground_z), float(d.sigma_haze0), H_HAZE,
                          float(d.sigma_fog), float(s[FOG_TOP]), float(d.sigma_precip), float(s[BASE]), mode, float(kim), float(wv), out)
            return out
        dv = p1 - p0
        L = np.linalg.norm(dv, axis=1)
        rd_z = np.where(L > 0, dv[:, 2] / np.where(L > 0, L, 1.0), 0.0)
        if float(wavelength_nm) == 550.0:
            return optical_depth_arr(p0[:, 2] - self.ground_z, rd_z, L, d, float(s[FOG_TOP]), float(s[BASE]))
        rho_v = self.sky_sample(t_sim_ns).rho_v_gm3 if float(wavelength_nm) >= LWIR_MIN_NM else 0.0
        return optical_depth_lambda_arr(p0[:, 2] - self.ground_z, rd_z, L, d, float(s[FOG_TOP]), float(s[BASE]), float(wavelength_nm),
                                        rho_v)

    # ------------------------------------------------------------ env stage 辅助
    def step_dryden(self, slots: np.ndarray, pos: np.ndarray, vel: np.ndarray, dt: float) -> None:
        """Dryden 模式：按 slot 升序推进标准化状态（每个 env tick 一次，RNG 流 1）。"""
        cfg = self.kf.config["wind"]
        if cfg["turbulence"]["model"] != "dryden" or int(cfg["level"]) < 1:
            return
        order = np.argsort(slots, kind="stable")
        sl = np.asarray(slots, np.int64)[order]
        self.dryden.sync(sl)
        p = np.asarray(pos, np.float64)[order]
        v = np.asarray(vel, np.float64)[order]
        s = self._s
        z_agl = p[:, 2] - self._z_ground(p[:, :2])
        ex, ey = e(float(s[DIR]))
        f = profile_arr(z_agl, cfg["profile"])
        um = np.stack([float(s[SPEED_REF]) * f * ex, float(s[SPEED_REF]) * f * ey, np.full(len(sl), float(s[W_MEAN]))], axis=1)
        self.dryden.step(sl, z_agl, um - v, float(s[SIGMA_REF]), dt, (ex, ey))

    def store_rows(self, slots: np.ndarray, q: EnvSampleSoA) -> None:
        """把本 tick 的查询结果打包为 EnvSample32 行缓存（按 slot）。numba 可用时一次遍历写完（kernels_rows，与下面的 numpy
        实现逐字节相同；N = 1000 时约 0.3 ms → 数十 µs，FX2-R3）。"""
        n = q.n
        sl = np.asarray(slots, np.int64)[:n]
        r = self.rows
        if KR.HAVE_NUMBA and n:
            KR.store_rows_nb(sl, n, q.wind_mps, q.wind_mean_mps, q.turb_sigma_mps, q.sigma_ext_per_m, q.rain_eff_mmh,
                             q.rho_kgm3, q.flags, q.source_level, q.gust_long_mps, r["wind"], r["wind_mean"],
                             r["turb_sigma_uw"], r["sigma_ext"], r["rain_eff"], r["rho"], r["flags"], r["source_level"],
                             r["gust"], self.row_valid)
            return
        r["wind"][sl] = q.wind_mps[:n]
        r["wind_mean"][sl] = np.clip(np.round(q.wind_mean_mps[:n] / 0.01), -32768, 32767)
        tsig = np.stack([q.turb_sigma_mps[:n, 0], q.turb_sigma_mps[:n, 2]], axis=1)
        r["turb_sigma_uw"][sl] = np.clip(np.round(tsig / 0.05), 0, 255)
        r["sigma_ext"][sl] = q.sigma_ext_per_m[:n]
        r["rain_eff"][sl] = np.clip(np.round(q.rain_eff_mmh[:n] / 0.01), 0, 65535)
        r["rho"][sl] = np.clip(np.round(q.rho_kgm3[:n] / 3e-5), 0, 65535)
        r["flags"][sl] = q.flags[:n]
        r["source_level"][sl] = q.source_level[:n]
        r["gust"][sl] = np.clip(np.round(q.gust_long_mps[:n] / 0.01), -32768, 32767)
        self.row_valid[sl] = True

    def store_wind_rows(self, slots: np.ndarray, q: EnvSampleSoA) -> None:
        """只风的 env tick：行缓存中风与阵风两列随每个 env tick 更新（其余列保持最近一次全量求值，ADR-060）。"""
        n = q.n
        sl = np.asarray(slots, np.int64)[:n]
        r = self.rows
        if KR.HAVE_NUMBA and n:
            KR.store_wind_rows_nb(sl, n, q.wind_mps, q.gust_long_mps, r["wind"], r["gust"])
            return
        r["wind"][sl] = q.wind_mps[:n]
        r["gust"][sl] = np.clip(np.round(q.gust_long_mps[:n] / 0.01), -32768, 32767)

    def sample32(self, slots: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
        """兴趣集打包 awr.EnvSample32.v1（(n,) ENV_SAMPLE32）。"""
        sl = np.asarray(slots, np.int64)
        if out is None:
            return self.rows[sl].copy()
        out[: sl.size] = self.rows[sl]
        return out

    # ------------------------------------------------------------ env/query（ctl/sim-core/query）
    def handle_query(self, req: Mapping[str, Any]) -> dict[str, Any]:
        self.stats["queries"] += 1
        args = req.get("args") if isinstance(req, Mapping) else None
        try:
            q = parse_query(args or {}, self.t_grid_ns, self.anchors.A.t_ns)
        except QueryError as ex:
            return {"v": 1, "code": ex.code, "detail": ex.detail}
        if self._qbuf is None:
            self._qbuf = EnvSampleSoA.alloc(256)
        o = self.query(q["pos"], q["t_ns"], fields=int(q["fields"]), frame=q["frame"], vel=q["vel"], quat_xyzw=q["quat"],
                       out=self._qbuf)
        return soa_to_reply(o, int(q["fields"]), q["t_ns"], self.kf.version, int(q["frame"]))

    def streamlines(self, field_id: str, dir_deg: int, n_lines: int = 1000, k: int = 64) -> bytes:
        """AWSL of the analytic field (LRU 16 by (field_id, degree)); only the current field id is served."""
        from .wind.streamlines import generate

        if field_id != self.field_id:
            raise EnvError(404, {"field_id": field_id})
        prof = self.kf.config["wind"]["profile"]
        wq = self.wq

        def ground(xy: np.ndarray) -> np.ndarray:
            return self._z_ground(xy)

        solid = None
        if wq is not None and hasattr(wq, "height_dsm"):
            def solid(xy: np.ndarray) -> np.ndarray:
                return np.asarray(wq.height_dsm(xy), np.float64)

        return self._sl_cache.get(field_id, dir_deg, lambda: generate(float(dir_deg), prof, self.bounds, ground=ground, solid_top=solid,
                                                                      n_lines=n_lines, k=k, seed=self.seed), self.seed)

    def state_at(self, t_ns: int) -> dict[str, Any]:
        """REST `env/state?t_ns=`（实时部分）：当前版本在 t 的帧，锚点推进到 t（t ≥ 锚点时刻）。"""
        if t_ns < self.anchors.A.t_ns:
            raise EnvError(110, {"field": "t_ns", "min": self.anchors.A.t_ns})
        return self.kf.with_heartbeat(t_ns, self._anchors_at(t_ns)).to_wire()

    # ------------------------------------------------------------ checkpoint（ext）
    def checkpoint(self) -> bytes:
        st = {"v": 1, "kf": self.kf.to_wire(), "anchors": self.anchors.A.to_json(), "k_done": self.k_done, "last_tick": self.last_tick,
              "version": self.km.version, "next_event_id": self.km.next_event_id,
              "pending": [[t, q, {"kind": op.kind, "patch": op.patch, "name": op.name, "amp_mps": op.amp_mps, "length_m": op.length_m,
                                  "dir_from_deg": op.dir_from_deg, "duration_s": op.duration_s, "mode": op.mode, "by": op.by,
                                  "d2": op.d2}]
                          for t, q, op in self.pending],
              "seq": self._seq, "gusts": _pack_state(self.gusts.state()), "dryden": _pack_state(self.dryden.state()),
              "warn": dict(self._warn_state), "sky": self.sky.state(), "sky_last_s": self._sky_last_s}
        return msgpack.packb(st, use_bin_type=True, default=_np_default)

    def restore(self, blob: bytes) -> None:
        from .keyframe import from_wire

        st = msgpack.unpackb(blob, raw=False, strict_map_key=False)
        self.km.kf = from_wire(st["kf"])
        self.km.version = int(st["version"])
        self.km.next_event_id = int(st["next_event_id"])
        self.km.epoch += 1  # 生产者纪元 + 1（K08）
        self.km.kf.epoch = self.km.epoch
        self.anchors.reset(Anchors.from_json(st["anchors"]))
        self.k_done = int(st["k_done"])
        self.last_tick = int(st["last_tick"])
        self.pending = [(int(t), int(q), EnvOp(**o)) for t, q, o in st["pending"]]
        self._seq = int(st["seq"])
        self.gusts.restore(_unpack_state(st["gusts"]))
        self.dryden.restore(_unpack_state(st["dryden"]))
        self._warn_state.update(st.get("warn", {}))
        self.sky.restore(st.get("sky"))
        self._sky_last_s = int(st.get("sky_last_s", -1))
        self.km.d2 = self.km.kf.d2
        self.km.cal = self.km.kf.cal
        self._kf_key += 1
        self._eval_now()


def _split_d2_patch(patch: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """`env/set` 补丁中的 D2 键：`acoustic_bg`（剖面名或 `{profile}`）、`humidity_pct`（0–100，换算为 `atmosphere.rh`）、
    `surface: {class}`（地表类别）。-> (其余补丁或 None, D2 设置)。非法值抛 EnvError 110。"""
    if not isinstance(patch, Mapping):
        return patch, {}
    rest = dict(patch)
    d2: dict[str, Any] = {}
    if "acoustic_bg" in rest:
        v = rest.pop("acoustic_bg")
        if isinstance(v, Mapping):
            v = v.get("profile")
        if v not in AC.PROFILES:
            raise EnvError(110, {"field": "acoustic_bg", "allowed": list(AC.PROFILES)})
        d2["acoustic_bg"] = v
    if "humidity_pct" in rest:
        v = rest.pop("humidity_pct")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or not 0.0 <= float(v) <= 100.0:
            raise EnvError(110, {"field": "humidity_pct", "min": 0, "max": 100})
        a = rest.get("atmosphere")
        if a is not None and not isinstance(a, Mapping):
            raise EnvError(300, {"field": "patch.atmosphere"})
        atm = dict(a or {})
        atm["rh"] = float(v) / 100.0
        rest["atmosphere"] = atm
    if "surface" in rest:
        v = rest.pop("surface")
        cls = v.get("class") if isinstance(v, Mapping) else None
        if cls not in SURFACE_CLASSES:
            raise EnvError(110, {"field": "surface.class", "allowed": list(SURFACE_CLASSES)})
        d2["surface_class"] = cls
    return (rest or None), d2


def _rotate_inv(q_xyzw: np.ndarray, v: np.ndarray) -> np.ndarray:
    """R^T*v，R = WORLD←BODY（xyzw）。"""
    x, y, z, w = (q_xyzw[:, i] for i in range(4))
    # 共轭四元数旋转
    qx, qy, qz = -x, -y, -z
    vx, vy, vz = v[:, 0], v[:, 1], v[:, 2]
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return np.stack([vx + w * tx + (qy * tz - qz * ty), vy + w * ty + (qz * tx - qx * tz), vz + w * tz + (qx * ty - qy * tx)], axis=1)


def _np_default(o: Any) -> Any:
    if isinstance(o, np.ndarray):
        return {"__nd__": True, "dtype": str(o.dtype), "shape": list(o.shape), "data": o.tobytes()}
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(type(o))


def _pack_state(st: Any) -> Any:
    """RNG 状态含 128 位整数（PCG64 state/inc），msgpack 只支持 64 位：大整数转字符串。"""
    if isinstance(st, dict):
        return {k: _pack_state(v) for k, v in st.items()}
    if isinstance(st, (list, tuple)):
        return [_pack_state(v) for v in st]
    if isinstance(st, int) and not isinstance(st, bool) and not -(2**63) <= st < 2**64:
        return {"__bigint__": str(st)}
    return st


def _unpack_state(st: Any) -> Any:
    if isinstance(st, dict):
        if st.get("__nd__"):
            return np.frombuffer(st["data"], np.dtype(st["dtype"])).reshape(st["shape"]).copy()
        if "__bigint__" in st:
            return int(st["__bigint__"])
        return {k: _unpack_state(v) for k, v in st.items()}
    if isinstance(st, list):
        return [_unpack_state(v) for v in st]
    return st


_ = (COVER, MOR_BG, RAIN, SNOW, H_HAZE, GustEvent)
