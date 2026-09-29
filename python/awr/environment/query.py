"""查询接口的枚举、结果 SoA 与 `env/query` 请求处理（M07-FR-016、FR-024；M07 §6.2.7、§7.1；g06 §6.1；17 §4.3.8）。

Fields（位掩码）、Frame（gz TransformTypes 语义，只作用于风矢量）、EnvFlags 与 source_level 取自 `rt/enums.json`。
`env/query` 经 `ctl/sim-core/query` 在慢任务预算内执行：≤ 256 点，t_ns ∈ [当前关键帧锚点时刻, 当前 + 10 s]；回复为
msgpack 字典（SoA 各列为 float 列表），网关原样作为 call 结果的 `data`。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import Any

import numpy as np

__all__ = ["MAX_QUERY_POINTS", "QUERY_AHEAD_NS", "EnvFlags", "EnvSampleSoA", "Fields", "Frame", "parse_query", "soa_to_reply"]

MAX_QUERY_POINTS = 256
QUERY_AHEAD_NS = 10_000_000_000


class Fields(IntFlag):
    WIND = 1
    WIND_PARTS = 2
    TURB_SPEC = 4
    OPTICS = 8
    PRECIP = 16
    THERMO = 32
    DEFAULT = WIND | OPTICS | THERMO
    ALL = 63


class Frame(IntEnum):
    GLOBAL = 0
    LOCAL = 1
    ADD_VELOCITY_GLOBAL = 2
    ADD_VELOCITY_LOCAL = 3


class EnvFlags(IntFlag):
    VALID = 1
    IN_SOLID = 2
    OUTSIDE_GRID = 4
    ABOVE_GRID = 8
    IN_FOG_LAYER = 16
    BELOW_CLOUD_PRECIP = 32
    CALM = 64


@dataclass(slots=True)
class EnvSampleSoA:
    """预分配结果（M07 §6.2.7 单位后缀命名）；n 为有效行数，数组按容量分配、可复用（out=）。"""

    n: int
    wind_mps: np.ndarray
    wind_mean_mps: np.ndarray
    wind_gust_mps: np.ndarray
    wind_turb_mps: np.ndarray
    turb_sigma_mps: np.ndarray
    turb_l_m: np.ndarray
    gust_long_mps: np.ndarray
    sigma_ext_per_m: np.ndarray
    mor_m: np.ndarray
    sigma_precip_per_m: np.ndarray
    rain_eff_mmh: np.ndarray
    snow_eff_mmh: np.ndarray
    dust: np.ndarray
    temperature_c: np.ndarray
    pressure_pa: np.ndarray
    rho_kgm3: np.ndarray
    rh: np.ndarray
    flags: np.ndarray
    source_level: np.ndarray

    @classmethod
    def alloc(cls, cap: int) -> EnvSampleSoA:
        f3 = lambda: np.zeros((cap, 3))  # noqa: E731
        f1 = lambda: np.zeros(cap, np.float32)  # noqa: E731
        return cls(0, f3(), f3(), f3(), f3(), np.zeros((cap, 3), np.float32), np.zeros((cap, 3), np.float32), f1(), f1(), f1(), f1(),
                   f1(), f1(), f1(), f1(), f1(), f1(), f1(), np.zeros(cap, np.uint8), np.zeros(cap, np.uint8))

    @property
    def capacity(self) -> int:
        return int(self.wind_mps.shape[0])


_FIELD_NAMES = {"WIND": Fields.WIND, "WIND_PARTS": Fields.WIND_PARTS, "TURB_SPEC": Fields.TURB_SPEC, "OPTICS": Fields.OPTICS,
                "PRECIP": Fields.PRECIP, "THERMO": Fields.THERMO}
_FRAMES = {"global": Frame.GLOBAL, "local": Frame.LOCAL, "add_velocity_global": Frame.ADD_VELOCITY_GLOBAL,
           "add_velocity_local": Frame.ADD_VELOCITY_LOCAL}


class QueryError(ValueError):
    def __init__(self, code: int, detail: Any = None) -> None:
        super().__init__(f"env/query {code}: {detail}")
        self.code = int(code)
        self.detail = detail


def _arr(v: Any, n: int, width: int, name: str) -> np.ndarray:
    try:
        a = np.asarray(v, np.float64)
    except (TypeError, ValueError):
        raise QueryError(300, {"field": name}) from None
    if a.shape != (n, width):
        raise QueryError(300, {"field": name, "shape": list(a.shape)})
    if not np.all(np.isfinite(a)):
        raise QueryError(110, {"field": name, "why": "NOT_FINITE"})
    return a


def parse_query(args: dict[str, Any], t_now_ns: int, t_anchor_ns: int) -> dict[str, Any]:
    """校验 `env/query` 参数 -> {pos, t_ns, fields, frame, vel, quat}；失败抛 QueryError（300/110）。"""
    if not isinstance(args, dict):
        raise QueryError(300, {"field": "args"})
    pts = args.get("points")
    if not isinstance(pts, list) or not pts:
        raise QueryError(300, {"field": "points"})
    if len(pts) > MAX_QUERY_POINTS:
        raise QueryError(110, {"field": "points", "max": MAX_QUERY_POINTS})
    n = len(pts)
    pos = _arr(pts, n, 3, "points")
    t = args.get("t_ns", t_now_ns)
    if not isinstance(t, int) or isinstance(t, bool):
        raise QueryError(300, {"field": "t_ns"})
    if t < t_anchor_ns or t > t_now_ns + QUERY_AHEAD_NS:
        raise QueryError(110, {"field": "t_ns", "min": t_anchor_ns, "max": t_now_ns + QUERY_AHEAD_NS})
    fl = args.get("fields")
    if fl is None:
        fields = Fields.DEFAULT
    else:
        if not isinstance(fl, list) or any(f not in _FIELD_NAMES for f in fl):
            raise QueryError(300, {"field": "fields"})
        fields = Fields(0)
        for f in fl:
            fields |= _FIELD_NAMES[f]
    frame = _FRAMES.get(str(args.get("frame", "global")))
    if frame is None:
        raise QueryError(300, {"field": "frame"})
    vel = _arr(args["vel_mps"], n, 3, "vel_mps") if args.get("vel_mps") is not None else None
    quat = _arr(args["q_xyzw"], n, 4, "q_xyzw") if args.get("q_xyzw") is not None else None
    if frame in (Frame.ADD_VELOCITY_GLOBAL, Frame.ADD_VELOCITY_LOCAL) and vel is None:
        raise QueryError(300, {"field": "vel_mps", "why": "REQUIRED_BY_FRAME"})
    if frame in (Frame.LOCAL, Frame.ADD_VELOCITY_LOCAL) and quat is None:
        raise QueryError(300, {"field": "q_xyzw", "why": "REQUIRED_BY_FRAME"})
    return {"pos": pos, "t_ns": int(t), "fields": fields, "frame": frame, "vel": vel, "quat": quat}


def _col(a: np.ndarray, n: int) -> list:
    """C 级 tolist（值按构造有限；float32 列先转 float64，避免 7 位小数外的噪声位被 msgpack 编成 float64）。"""
    x = a[:n]
    return x.astype(np.float64).tolist() if x.dtype != np.float64 else x.tolist()


def soa_to_reply(s: EnvSampleSoA, fields: int, t_ns: int, version: int, frame: int) -> dict[str, Any]:
    n = s.n
    out: dict[str, Any] = {"v": 1, "code": 0, "n": n, "t_ns": int(t_ns), "version": int(version), "frame": int(frame),
                           "flags": s.flags[:n].tolist(), "source_level": s.source_level[:n].tolist()}
    if fields & Fields.WIND:
        out["wind_mps"] = _col(s.wind_mps, n)
    if fields & Fields.WIND_PARTS:
        out["wind_mean_mps"] = _col(s.wind_mean_mps, n)
        out["wind_gust_mps"] = _col(s.wind_gust_mps, n)
        out["wind_turb_mps"] = _col(s.wind_turb_mps, n)
        out["gust_long_mps"] = _col(s.gust_long_mps, n)
    if fields & Fields.TURB_SPEC:
        out["turb_sigma_mps"] = _col(s.turb_sigma_mps, n)
        out["turb_l_m"] = _col(s.turb_l_m, n)
    if fields & Fields.OPTICS:
        out["sigma_ext_per_m"] = _col(s.sigma_ext_per_m, n)
        out["mor_m"] = _col(s.mor_m, n)
        out["sigma_precip_per_m"] = _col(s.sigma_precip_per_m, n)
    if fields & Fields.PRECIP:
        out["rain_eff_mmh"] = _col(s.rain_eff_mmh, n)
        out["snow_eff_mmh"] = _col(s.snow_eff_mmh, n)
        out["dust"] = _col(s.dust, n)
    if fields & Fields.THERMO:
        out["temperature_c"] = _col(s.temperature_c, n)
        out["pressure_pa"] = _col(s.pressure_pa, n)
        out["rho_kgm3"] = _col(s.rho_kgm3, n)
        out["rh"] = _col(s.rh, n)
    return out
