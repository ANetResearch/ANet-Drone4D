"""预设加载、快照叠加、路由与补丁校验（M07-FR-002、FR-025；M07 §6.2.1、§6.3.2、§6.5、§9.2）。

唯一数据源是 `packages/contracts/env/presets.json`（经生成物 `awr.contracts.presets` 嵌入字节与 sha256）。EnvScalars 为
21 维 float64 位置向量，下标即 `fields[]` 顺序；4 个"用户轴"（风向、垂直风、ISA 偏差、湿度）不出现在预设快照中，
切预设时保持当前值。常数只从 presets.json 读取（M07 §6.4 "P" 列）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from awr.contracts import presets as _P

__all__ = [
    "BASE",
    "CLOUD2D",
    "COVER",
    "CTYPE",
    "DIR",
    "DUST",
    "FIELD_PATHS",
    "FOG_TOP",
    "GROUP",
    "GUST_AMP",
    "GUST_LEN",
    "GUST_RATE",
    "HORIZON",
    "ISA_DT",
    "LIGHTNING",
    "MOR_BG",
    "NF",
    "PRESETS",
    "PRESETS_SHA256",
    "PRESET_IDS",
    "RAIN",
    "RH",
    "SIGMA_REF",
    "SNOW",
    "SPACE",
    "SPEED_REF",
    "TOP",
    "USER_AXIS",
    "W_MEAN",
    "C",
    "EnvError",
    "Presets",
    "presets",
]

PRESETS: dict[str, Any] = _P.PRESETS
PRESETS_SHA256: str = _P.PRESETS_SHA256
FIELD_PATHS: tuple[str, ...] = _P.FIELD_PATHS
NF: int = _P.NF
PRESET_IDS: tuple[str, ...] = _P.PRESET_IDS
C: dict[str, Any] = PRESETS["constants"]

SPEED_REF = _P.F_WIND_SPEED_REF_MPS
DIR = _P.F_WIND_DIR_FROM_DEG
W_MEAN = _P.F_WIND_W_MEAN_MPS
SIGMA_REF = _P.F_WIND_TURB_SIGMA_U_REF_MPS
GUST_AMP = _P.F_WIND_GUST_AMP_MPS
GUST_RATE = _P.F_WIND_GUST_RATE_HZ
GUST_LEN = _P.F_WIND_GUST_LENGTH_M
COVER = _P.F_CLOUD_COVER
CTYPE = _P.F_CLOUD_TYPE
BASE = _P.F_CLOUD_BASE_M
TOP = _P.F_CLOUD_TOP_M
RAIN = _P.F_PRECIP_RAIN_MMH
SNOW = _P.F_PRECIP_SNOW_MMH
MOR_BG = _P.F_ATMOSPHERE_MOR_BG_M
FOG_TOP = _P.F_ATMOSPHERE_FOG_TOP_AGL_M
DUST = _P.F_ATMOSPHERE_DUST
ISA_DT = _P.F_ATMOSPHERE_ISA_DT_C
RH = _P.F_ATMOSPHERE_RH
LIGHTNING = _P.F_LIGHTNING_RATE_PER_MIN
HORIZON = _P.F_VISUAL_HORIZON_STEP
CLOUD2D = _P.F_VISUAL_CLOUD2D_ALPHA_MAX

FIELDS = PRESETS["fields"]
SPACE: tuple[str, ...] = tuple(f["space"] for f in FIELDS)
GROUP: tuple[str, ...] = tuple(f["group"] for f in FIELDS)
USER_AXIS: tuple[bool, ...] = tuple(bool(f.get("user_axis")) for f in FIELDS)
LO = np.array([float(f["min"]) for f in FIELDS])
HI = np.array([float(f["max"]) for f in FIELDS])
CLOUD_GAP_M = 100.0  # top >= base + 100（M07 §6.2.1）

assert len(FIELDS) == NF


class EnvError(Exception):
    """环境操作校验失败（M07 §7.5）：code 为 reasons.json 登记的数值码。"""

    def __init__(self, code: int, detail: Any = None) -> None:
        super().__init__(f"env error {code}: {detail}")
        self.code = int(code)
        self.detail = detail


# reasons.json（17 §8.4）
BAD_REQUEST = 300
PARAM_OUT_OF_RANGE = 110
ENV_PRESET_UNKNOWN = 440
ENV_FIELD_UNKNOWN = 441
ENV_LEVEL_UNAVAILABLE = 442


def _get_path(d: Mapping, path: str) -> Any:
    for k in path.split("."):
        if not isinstance(d, Mapping) or k not in d:
            return None
        d = d[k]
    return d


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    path: str
    unit: str
    group: str
    space: str
    lo: float
    hi: float
    user_axis: bool


class Presets:
    """presets.json 的只读视图（M07 §9.2）。"""

    def __init__(self, doc: Mapping[str, Any] | None = None, sha256: str | None = None) -> None:
        self.doc = PRESETS if doc is None else doc
        self.sha256 = PRESETS_SHA256 if sha256 is None else sha256
        self.fields = tuple(FieldSpec(f["path"], f["unit"], f["group"], f["space"], float(f["min"]), float(f["max"]),
                                      bool(f.get("user_axis"))) for f in self.doc["fields"])
        self.index = {f.path: i for i, f in enumerate(self.fields)}
        self.constants = self.doc["constants"]
        self.windows = self.doc["windows"]
        self.rates = self.doc["rates_per_s"]
        self.durations = self.doc["durations_s"]
        self.client = self.doc.get("client", {})
        self.ids = tuple(p["id"] for p in self.doc["presets"])
        self._defaults = np.array([float(_get_path(self.doc["defaults"]["scalars"], f.path)) for f in self.fields])
        self._snap: dict[str, np.ndarray] = {}
        for p in self.doc["presets"]:
            v = np.full(len(self.fields), np.nan)
            for i, f in enumerate(self.fields):
                x = _get_path(p["scalars"], f.path)
                if x is not None:
                    v[i] = float(x)
            self._snap[p["id"]] = v
        self._routes = {(r["from"], r["to"]): tuple(r["via"]) for r in self.doc["routes"]}
        self.meta = {p["id"]: {"name": p.get("name"), "name_zh": p.get("name_zh"), "icon": p.get("icon")} for p in self.doc["presets"]}

    # ------------------------------------------------------------ 取值
    def default_vector(self) -> np.ndarray:
        return self._defaults.copy()

    def snapshot(self, preset_id: str) -> np.ndarray:
        """17 个预设字段的快照，NaN 表示"不覆盖"（用户轴）。"""
        try:
            return self._snap[preset_id]
        except KeyError:
            raise EnvError(ENV_PRESET_UNKNOWN, {"name": preset_id}) from None

    def overlay(self, base: Sequence[float] | np.ndarray, preset_id: str, out: np.ndarray | None = None) -> np.ndarray:
        snap = self.snapshot(preset_id)
        out = np.array(base, np.float64) if out is None else out
        if out is not base:
            out[:] = base
        m = ~np.isnan(snap)
        out[m] = snap[m]
        return out

    def preset_vector(self, preset_id: str, base: Sequence[float] | None = None) -> np.ndarray:
        return self.overlay(self._defaults if base is None else base, preset_id)

    def route(self, from_id: str | None, to_id: str) -> list[str]:
        return list(self._routes.get((from_id, to_id), ())) if from_id is not None else []

    def matches(self, s: np.ndarray, preset_id: str, tol: float = 1e-9) -> bool:
        """s 的 17 个预设字段是否等于该预设（用于识别 `activePreset`）。"""
        snap = self._snap.get(preset_id)
        if snap is None:
            return False
        m = ~np.isnan(snap)
        return bool(np.all(np.abs(s[m] - snap[m]) <= tol * np.maximum(1.0, np.abs(snap[m]))))

    # ------------------------------------------------------------ 校验
    def validate_patch(self, patch: Mapping[str, Any]) -> tuple[list[tuple[int, float]], dict[str, Any] | None]:
        """嵌套 EnvScalars 部分快照 + 可选 `config` 补丁 -> ([(下标, 值)], config 补丁)；失败抛 EnvError。"""
        if not isinstance(patch, Mapping) or not patch:
            raise EnvError(BAD_REQUEST, {"field": "patch"})
        items: list[tuple[int, float]] = []
        cfg = None

        def walk(d: Mapping[str, Any], prefix: str) -> None:
            for k, v in d.items():
                if not isinstance(k, str):
                    raise EnvError(BAD_REQUEST, {"field": "patch"})
                path = f"{prefix}{k}"
                if isinstance(v, Mapping):
                    walk(v, path + ".")
                    continue
                i = self.index.get(path)
                if i is None:
                    raise EnvError(ENV_FIELD_UNKNOWN, {"field": path})
                if not _is_num(v):
                    raise EnvError(BAD_REQUEST, {"field": path})
                x = float(v)
                if not math.isfinite(x):
                    raise EnvError(PARAM_OUT_OF_RANGE, {"field": path, "why": "NOT_FINITE"})
                f = self.fields[i]
                if f.space == "arc" and x == f.hi:
                    x = 0.0
                if x < f.lo or x > f.hi or (f.space == "arc" and x >= f.hi):
                    raise EnvError(PARAM_OUT_OF_RANGE, {"field": path, "min": f.lo, "max": f.hi, "value": x})
                items.append((i, x))

        for k, v in patch.items():
            if k == "config":
                if not isinstance(v, Mapping):
                    raise EnvError(BAD_REQUEST, {"field": "patch.config"})
                cfg = validate_config_patch(v)
            elif isinstance(v, Mapping):
                walk(v, f"{k}.")
            else:
                raise EnvError(ENV_FIELD_UNKNOWN, {"field": str(k)})
        return items, cfg

    def check_state(self, s: np.ndarray) -> None:
        """范围与 `top ≥ base + 100` 约束（整条操作的最终 `to`）。"""
        if not np.all(np.isfinite(s)):
            raise EnvError(PARAM_OUT_OF_RANGE, {"why": "NOT_FINITE"})
        if s[TOP] < s[BASE] + CLOUD_GAP_M:
            raise EnvError(PARAM_OUT_OF_RANGE, {"field": "cloud.top_m", "why": "TOP_BELOW_BASE", "base_m": float(s[BASE]),
                                                "top_m": float(s[TOP])})

    def check_duration(self, duration_s: Any) -> float:
        if not _is_num(duration_s) or not math.isfinite(float(duration_s)):
            raise EnvError(PARAM_OUT_OF_RANGE, {"field": "duration_s"})
        d = float(duration_s)
        if d < float(self.durations["min"]) or d > float(self.durations["max"]):
            raise EnvError(PARAM_OUT_OF_RANGE, {"field": "duration_s", "min": self.durations["min"], "max": self.durations["max"]})
        return d


PROFILE_KINDS = ("log", "power", "uniform")
TURB_MODELS = ("box", "dryden", "off")


def validate_config_patch(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """`config` 补丁：wind.level ∈ {0, 1}（2、3 → 442）、wind.turbulence.model、wind.profile 数值；其余拒绝（441）。"""
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if k == "wind" and isinstance(v, Mapping):
            w: dict[str, Any] = {}
            for wk, wv in v.items():
                if wk == "level":
                    if not isinstance(wv, int) or isinstance(wv, bool) or not 0 <= wv <= 3:
                        raise EnvError(PARAM_OUT_OF_RANGE, {"field": "config.wind.level"})
                    if wv > 1:
                        raise EnvError(ENV_LEVEL_UNAVAILABLE, {"field": "config.wind.level", "value": wv})
                    w["level"] = int(wv)
                elif wk == "turbulence" and isinstance(wv, Mapping):
                    t = {}
                    for tk, tv in wv.items():
                        if tk != "model" or tv not in TURB_MODELS:
                            raise EnvError(ENV_FIELD_UNKNOWN if tk != "model" else PARAM_OUT_OF_RANGE,
                                           {"field": f"config.wind.turbulence.{tk}"})
                        t["model"] = tv
                    w["turbulence"] = t
                elif wk == "profile" and isinstance(wv, Mapping):
                    p: dict[str, Any] = {}
                    for pk, pv in wv.items():
                        if pk == "kind":
                            if pv not in PROFILE_KINDS:
                                raise EnvError(PARAM_OUT_OF_RANGE, {"field": "config.wind.profile.kind"})
                            p[pk] = pv
                        elif pk in ("z_ref_m", "z0_m", "d_m", "alpha", "adv_height_m"):
                            if not _is_num(pv) or not math.isfinite(float(pv)):
                                raise EnvError(PARAM_OUT_OF_RANGE, {"field": f"config.wind.profile.{pk}"})
                            x = float(pv)
                            lo, hi = {"z_ref_m": (1.0, 200.0), "z0_m": (1e-4, 5.0), "d_m": (0.0, 50.0), "alpha": (0.05, 0.6),
                                      "adv_height_m": (2.0, 300.0)}[pk]
                            if not lo <= x <= hi:
                                raise EnvError(PARAM_OUT_OF_RANGE, {"field": f"config.wind.profile.{pk}", "min": lo, "max": hi})
                            p[pk] = x
                        else:
                            raise EnvError(ENV_FIELD_UNKNOWN, {"field": f"config.wind.profile.{pk}"})
                    w["profile"] = p
                elif wk == "library" and wv is None:
                    w["library"] = None
                else:
                    raise EnvError(ENV_FIELD_UNKNOWN, {"field": f"config.wind.{wk}"})
            out["wind"] = w
        elif k == "sun" and isinstance(v, Mapping):
            s = {}
            for sk, sv in v.items():
                if sk not in ("azimuth_deg", "elevation_deg") or not _is_num(sv) or not math.isfinite(float(sv)):
                    raise EnvError(ENV_FIELD_UNKNOWN if sk not in ("azimuth_deg", "elevation_deg") else PARAM_OUT_OF_RANGE,
                                   {"field": f"config.sun.{sk}"})
                x = float(sv)
                if (sk == "azimuth_deg" and not 0.0 <= x < 360.0) or (sk == "elevation_deg" and not 0.0 <= x <= 90.0):
                    raise EnvError(PARAM_OUT_OF_RANGE, {"field": f"config.sun.{sk}"})
                s[sk] = x
            out["sun"] = s
        else:
            raise EnvError(ENV_FIELD_UNKNOWN, {"field": f"config.{k}"})
    return out


_DEFAULT: Presets | None = None


def presets() -> Presets:
    """进程内共享的 contract 预设视图。"""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Presets()
    return _DEFAULT
