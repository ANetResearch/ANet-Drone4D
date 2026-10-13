"""识别物配置模型：类别模板、合并、名称净化与逐字段校验（M18-FR-002、FR-003、FR-005 的参数部分、FR-008 的 541、FR-009 的 542、
FR-026；M18-M19 PRD §6.2.1、§6.3.1、§6.4.2、§6.5；契约 `target/{target,sensitivity,sound,reaction}.schema.json`、
`target/class_defaults.json`；17 §17.6.5）。

- 字段名按契约（17 为线上定义方，WP-01 裁决）：`class`、`label`、平铺的 `rcs_m2`、`rcs_fluct`、`rcs_band`；PRD 独有字段
  `alt_agl_m`、`hearing_th_dba`、`acoustic_adv_db`、`visual_theta0_arcmin`、`yaw_rad` 为可选字段。
- 合并规则：类别模板深拷贝后按请求覆盖；对象字段（`size_m`、`motion`、`thermal`、`sound`、`reaction` 的 onset）逐键合并，
  数组字段（`sensitivity`、`motion.path`、`motion.region`、`reaction.response`）整体替换（JSON Merge Patch 语义，17 §17.5 SB-43）。
- 校验失败抛 `TargetError(code, detail)`：`detail = {field, why, value?, limit?}`；码：541 声源、542 敏感体（含每识别物 > 4）、
  544 运动、540 放置（在 `placement.py`）、110 其余越界、类型错误与未知字段。取值范围以契约为准（WP-01 §4 第 7 条：声源
  `f0_hz` [20, 10000]、`period_s` [0.1, 3600]；敏感体半径 [1, 3000]）。
- 名称净化（AWR-03 §10.2）：去掉 emoji 与禁用字形区段（与 `rest/runs.py`、前端 `lib/sanitize.ts` 同一集合）及控制字符，
  合并空白，≤ 32 字符。

D2.1（M18-FR-028 至 FR-031，AWR-04 §15.10.1，ADR-124）：可选字段 `visual_acuity_arcmin`（角分辨率，0.3–20'，只用于等效像素与读数）、
`hearing_band_adj_db[24]`（类别频段修正，每带 −20 至 +20 dB）、`radar_rx{snr_ref_db, r_ref_m, snr_req_db, fov_az_deg, fov_el_deg}`（P1，
挂 radar 敏感体时生效）与 `rf_rx{sens_dbm, f_mhz, bw_mhz}`（P2，只入 schema）。类别缺省值在使用处解析（`visual_acuity_of`、
`band_adj_of`），不写入合并后的配置：契约 `class_defaults.json` 与 `target.schema.json` 合入这些字段（M00，契约 1.2.0）之前，导出的
配置与 D2 逐字段相同。

只依赖 numpy 与 `awr.contracts`；不读墙钟、不使用随机数。
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from awr.contracts._paths import contracts_root

__all__ = [
    "ACUITY_DEFAULT_ARCMIN", "CLASSES", "LABEL_MAX", "NULLABLE", "TargetConfig", "TargetError", "TargetState1", "TargetView", "band_adj_of",
    "class_defaults", "label_zh", "merge_patch", "merged_config", "radar_rx_of", "sanitize_label", "validate_config", "visual_acuity_of",
]

LABEL_MAX = 32
CLASSES = ("person", "uav", "machine", "dog", "vehicle", "object")
GROUND_CLASSES = frozenset(("person", "machine", "dog", "vehicle", "object"))
NULLABLE = frozenset(("sound", "rf", "critical_dim_m", "n_req_px", "hearing_band_adj_db", "radar_rx", "rf_rx"))
# 角分辨率的类别缺省（AWR-04 §15.10.1：人 Snellen 20/20 1'；狗约 20/75 取 4'；uav 1080P、80° 相机每像素 2.5'；车按驾驶员计 1'；
# machine 与 object 无视觉）
ACUITY_DEFAULT_ARCMIN = {"person": 1.0, "uav": 2.5, "dog": 4.0, "vehicle": 1.0}
RADAR_RX_FIELDS = frozenset(("snr_ref_db", "r_ref_m", "snr_req_db", "fov_az_deg", "fov_el_deg"))
RF_RX_FIELDS = frozenset(("sens_dbm", "f_mhz", "bw_mhz"))
N_HEARING_BANDS = 24
# 服务端净化：emoji 区段、U+25A0–25FF、U+2600–27BF、U+2194–21FF、U+2B00–2BFF、U+2300–23FF、变体选择符与连接符（同 rest/runs.py）
_STRIP_RANGES = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x25A0, 0x25FF), (0x2194, 0x21FF), (0x2B00, 0x2BFF), (0x2300, 0x23FF),
                 (0xFE0F, 0xFE0F), (0x200D, 0x200D), (0x20E3, 0x20E3), (0xE0020, 0xE007F), (0x0000, 0x001F), (0x007F, 0x009F))
_STRIP = re.compile("[" + "".join(re.escape(chr(a)) + ("-" + re.escape(chr(b)) if b != a else "") for a, b in _STRIP_RANGES) + "]")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

TOP_FIELDS = frozenset((
    "id", "label", "class", "enabled", "pos", "yaw_rad", "alt_agl_m", "size_m", "critical_dim_m", "motion", "color_srgb", "albedo_vis",
    "contrast0", "camouflage", "occlusion", "reflect_850", "reflect_905", "thermal", "rcs_m2", "rcs_fluct", "rcs_band", "sound", "rf",
    "criterion_mode", "alertness", "hearing_th_dba", "acoustic_adv_db", "visual_theta0_arcmin", "level_req", "n_req_px", "reaction",
    "calm_s", "alert_radius_m", "sensitivity", "cfg_rev",
    "visual_acuity_arcmin", "hearing_band_adj_db", "radar_rx", "rf_rx",   # D2.1
))
MOTION_FIELDS = frozenset(("mode", "speed_mps", "speed_max_mps", "accel_mps2", "turn_rate_max_rad_s", "path", "loop", "dwell_s", "region",
                           "speed_sigma_mps", "turn_tau_s", "pause_prob", "vz_mps"))
SOUND_FIELDS = frozenset(("waveform", "f0_hz", "harmonics_db", "lw_dba", "period_s", "duty", "phase_s", "bandwidth_hz"))
BODY_FIELDS = frozenset(("kind", "shape", "radius_max_m", "z_min_m", "z_max_m", "half_angle_deg", "azimuth_offset_deg", "gain_back_db", "enabled"))
THERMAL_FIELDS = frozenset(("mode", "delta_t_k", "t_surface_c", "emissivity", "hot_fraction"))
BODY_DEFAULTS = {"z_min_m": 0.0, "z_max_m": 300.0, "half_angle_deg": 60.0, "azimuth_offset_deg": 0.0, "gain_back_db": 0.0, "enabled": True}
HARMONICS_DEFAULT = (0.0, -6.0, -10.0, -14.0)
MAX_BODIES = 4
MAX_PATH = 64
MAX_REGION = 64


class TargetError(ValueError):
    """识别物命令或配置的拒绝（码与字段级 detail；sim-core 命令处理函数把它转为 `{status: rejected, code, detail}`）。"""

    def __init__(self, code: int, field_: str | None = None, why: str = "INVALID", **extra: Any) -> None:
        self.code = int(code)
        self.detail: dict[str, Any] = {"why": why}
        if field_ is not None:
            self.detail["field"] = field_
        for k, v in extra.items():
            if v is not None:
                self.detail[k] = v
        super().__init__(f"{code} {field_ or ''} {why}")


@cache
def class_defaults() -> dict[str, Any]:
    """`target/class_defaults.json`（模拟参考值，M18 所有）。"""
    return json.loads((contracts_root() / "target" / "class_defaults.json").read_text(encoding="utf-8"))


def label_zh(cls: str) -> str:
    return str(class_defaults().get("label_zh", {}).get(cls, cls))


def sanitize_label(s: Any, max_len: int = LABEL_MAX) -> str:
    out = " ".join(_STRIP.sub("", str(s or "")).split())
    return out[:max_len]


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def merge_patch(base: dict, patch: dict) -> dict:
    """JSON Merge Patch（RFC 7396）的识别物变体：对象逐键合并、数组整体替换；`null` 对可空字段（sound、rf、critical_dim_m、
    n_req_px）置空，对其余字段恢复模板值（由调用方在合并后补齐）。返回新字典。"""
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if v is None:
            if k in NULLABLE:
                out[k] = None
            else:
                out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_patch(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def merged_config(d: dict) -> dict:
    """类别模板 + 覆盖（缺省值补齐，含敏感体与声源的字段缺省）。不校验。"""
    cls = d.get("class")
    tmpl = class_defaults()["classes"].get(cls) if isinstance(cls, str) else None
    if tmpl is None:
        raise TargetError(110, "class", "UNKNOWN_CLASS", value=cls, allowed=list(class_defaults()["classes"]))
    out = merge_patch(tmpl, {k: v for k, v in d.items() if k != "class"})
    for k, v in tmpl.items():  # 被 null 删除的非可空字段恢复模板值
        if k not in out:
            out[k] = copy.deepcopy(v)
    out["class"] = cls
    out.setdefault("critical_dim_m", None)
    out.setdefault("n_req_px", None)
    out.setdefault("enabled", True)
    out.setdefault("yaw_rad", 0.0)
    if cls == "uav":
        out.setdefault("alt_agl_m", float(tmpl.get("alt_agl_m", 50.0)))
    snd = out.get("sound")
    if isinstance(snd, dict):
        snd.setdefault("phase_s", 0.0)
        if snd.get("waveform") in ("harmonic", "pulse_train"):
            snd.setdefault("harmonics_db", list(HARMONICS_DEFAULT))
    bodies = out.get("sensitivity")
    if isinstance(bodies, list):
        out["sensitivity"] = [({**b, **{k: v for k, v in BODY_DEFAULTS.items() if k not in b}} if isinstance(b, dict) else b) for b in bodies]
    return out


# ---------------------------------------------------------------- 校验
def _num(code: int, fld: str, v: Any, lo: float | None, hi: float | None, *, lo_open: bool = False, hi_open: bool = False) -> float:
    if not _is_num(v):
        raise TargetError(code, fld, "NOT_A_NUMBER", value=v if isinstance(v, (int, float, str, bool)) or v is None else str(v))
    x = float(v)
    if lo is not None and (x < lo or (lo_open and x <= lo)):
        raise TargetError(code, fld, "BELOW_MIN", value=x, limit=lo)
    if hi is not None and (x > hi or (hi_open and x >= hi)):
        raise TargetError(code, fld, "ABOVE_MAX", value=x, limit=hi)
    return x


def _enum(code: int, fld: str, v: Any, allowed: tuple[str, ...]) -> str:
    if v not in allowed:
        raise TargetError(code, fld, "NOT_ALLOWED", value=v if isinstance(v, (str, int, float)) else str(v), allowed=list(allowed))
    return str(v)


def _keys(code: int, prefix: str, d: Any, allowed: frozenset[str]) -> dict:
    if not isinstance(d, dict):
        raise TargetError(code, prefix, "NOT_AN_OBJECT")
    bad = sorted(set(d) - allowed)
    if bad:
        raise TargetError(code, f"{prefix}.{bad[0]}" if prefix else bad[0], "UNKNOWN_FIELD")
    return d


def _vec(code: int, fld: str, v: Any, n_min: int, n_max: int, allow_null_z: bool = True) -> list:
    if not isinstance(v, (list, tuple)) or not n_min <= len(v) <= n_max:
        raise TargetError(code, fld, "BAD_VECTOR", value=None if not isinstance(v, (list, tuple)) else len(v))
    for i, x in enumerate(v):
        if x is None and allow_null_z and i == 2:
            continue
        if not _is_num(x):
            raise TargetError(code, f"{fld}[{i}]", "NOT_A_NUMBER")
    return list(v)


def validate_sound(s: Any, fld: str = "sound") -> None:
    """§6.5.2 校验（541）。"""
    if s is None:
        return
    _keys(541, fld, s, SOUND_FIELDS)
    wf = _enum(541, f"{fld}.waveform", s.get("waveform"), ("tone", "harmonic", "pulse_train", "noise_band"))
    for k in ("f0_hz", "lw_dba", "period_s", "duty"):
        if k not in s:
            raise TargetError(541, f"{fld}.{k}", "REQUIRED")
    _num(541, f"{fld}.f0_hz", s["f0_hz"], 20.0, 10000.0)
    _num(541, f"{fld}.lw_dba", s["lw_dba"], 0.0, 140.0)
    per = _num(541, f"{fld}.period_s", s["period_s"], 0.1, 3600.0)
    _num(541, f"{fld}.duty", s["duty"], 0.0, 1.0)
    if s.get("phase_s") is not None:
        _num(541, f"{fld}.phase_s", s["phase_s"], 0.0, per, hi_open=True)
    h = s.get("harmonics_db")
    if h is not None:
        if not isinstance(h, list) or len(h) > 8:
            raise TargetError(541, f"{fld}.harmonics_db", "BAD_LIST", value=len(h) if isinstance(h, list) else None, limit=8)
        for i, x in enumerate(h):
            _num(541, f"{fld}.harmonics_db[{i}]", x, -60.0, 0.0)
    if s.get("bandwidth_hz") is not None:
        _num(541, f"{fld}.bandwidth_hz", s["bandwidth_hz"], 10.0, 5000.0)
    elif wf == "noise_band":
        raise TargetError(541, f"{fld}.bandwidth_hz", "REQUIRED")
    from .sound import band_spectrum  # 组合有效性（至少一个分量落在 50 Hz–10 kHz 的 24 带内）

    if not band_spectrum(s).any_in_band:
        raise TargetError(541, f"{fld}.f0_hz", "NO_BAND_IN_RANGE", value=float(s["f0_hz"]))


def validate_body(b: Any, i: int) -> None:
    """§6.3.1 校验（542）。"""
    fld = f"sensitivity[{i}]"
    _keys(542, fld, b, BODY_FIELDS)
    _enum(542, f"{fld}.kind", b.get("kind"), ("visual", "acoustic", "radar", "rf"))
    _enum(542, f"{fld}.shape", b.get("shape"), ("sphere", "hemisphere", "cylinder", "sector"))
    if "radius_max_m" not in b:
        raise TargetError(542, f"{fld}.radius_max_m", "REQUIRED")
    _num(542, f"{fld}.radius_max_m", b["radius_max_m"], 1.0, 3000.0)
    zmin = _num(542, f"{fld}.z_min_m", b.get("z_min_m", 0.0), -50.0, 1000.0)
    zmax = _num(542, f"{fld}.z_max_m", b.get("z_max_m", 300.0), -50.0, 1000.0)
    if zmin >= zmax:
        raise TargetError(542, f"{fld}.z_max_m", "Z_RANGE_EMPTY", value=zmax, limit=zmin)
    _num(542, f"{fld}.half_angle_deg", b.get("half_angle_deg", 60.0), 0.0, 180.0, lo_open=True)
    _num(542, f"{fld}.azimuth_offset_deg", b.get("azimuth_offset_deg", 0.0), -180.0, 180.0)
    _num(542, f"{fld}.gain_back_db", b.get("gain_back_db", 0.0), 0.0, 30.0)
    if not isinstance(b.get("enabled", True), bool):
        raise TargetError(542, f"{fld}.enabled", "NOT_A_BOOL")


def validate_motion(m: Any) -> None:
    """§6.5.1 的参数部分（544；区域面积与含起点在 placement.py 以 540 判定）。"""
    _keys(544, "motion", m, MOTION_FIELDS)
    mode = _enum(544, "motion.mode", m.get("mode"), ("static", "path", "random_walk"))
    for k, hi in (("speed_mps", 40.0), ("speed_max_mps", 40.0), ("speed_sigma_mps", 10.0)):
        if m.get(k) is not None:
            _num(544, f"motion.{k}", m[k], 0.0, hi)
    if m.get("accel_mps2") is not None:
        _num(544, "motion.accel_mps2", m["accel_mps2"], 0.0, 20.0)
    if m.get("turn_rate_max_rad_s") is not None:
        _num(544, "motion.turn_rate_max_rad_s", m["turn_rate_max_rad_s"], 0.0, 10.0)
    if m.get("turn_tau_s") is not None:
        _num(544, "motion.turn_tau_s", m["turn_tau_s"], 0.0, 600.0, lo_open=True)
    if m.get("pause_prob") is not None:
        _num(544, "motion.pause_prob", m["pause_prob"], 0.0, 1.0)
    if m.get("speed_mps") is not None and m.get("speed_max_mps") is not None and float(m["speed_mps"]) > float(m["speed_max_mps"]):
        raise TargetError(544, "motion.speed_mps", "ABOVE_SPEED_MAX", value=float(m["speed_mps"]), limit=float(m["speed_max_mps"]))
    if m.get("loop") is not None:
        _enum(544, "motion.loop", m["loop"], ("once", "pingpong", "loop"))
    path = m.get("path")
    if path is not None:
        if not isinstance(path, list) or len(path) > MAX_PATH:
            raise TargetError(544, "motion.path", "TOO_MANY_POINTS", value=len(path) if isinstance(path, list) else None, limit=MAX_PATH)
        for i, p in enumerate(path):
            _vec(544, f"motion.path[{i}]", p, 2, 3)
    if mode == "path" and (not isinstance(path, list) or len(path) < 2):
        raise TargetError(544, "motion.path", "PATH_TOO_SHORT", value=len(path) if isinstance(path, list) else 0, limit=2)
    dw = m.get("dwell_s")
    if dw is not None:
        if not isinstance(dw, list) or len(dw) > MAX_PATH:
            raise TargetError(544, "motion.dwell_s", "BAD_LIST")
        for i, x in enumerate(dw):
            _num(544, f"motion.dwell_s[{i}]", x, 0.0, 3600.0)
    reg = m.get("region")
    if reg is not None:
        if not isinstance(reg, list) or not 3 <= len(reg) <= MAX_REGION:
            raise TargetError(544, "motion.region", "BAD_POLYGON", value=len(reg) if isinstance(reg, list) else None, limit=MAX_REGION)
        for i, p in enumerate(reg):
            _vec(544, f"motion.region[{i}]", p, 2, 2, allow_null_z=False)


def validate_reaction(r: Any) -> None:
    _keys(110, "reaction", r, frozenset(("onset", "response", "alert_others")))
    on = r.get("onset")
    if on is not None:
        _keys(110, "reaction.onset", on, frozenset(("kind", "duration_s")))
        _enum(110, "reaction.onset.kind", on.get("kind"), ("none", "freeze", "look_at"))
        _num(110, "reaction.onset.duration_s", on.get("duration_s", 0.0), 0.0, 60.0)
    resp = r.get("response")
    if resp is not None:
        if not isinstance(resp, list) or len(resp) > 2:
            raise TargetError(110, "reaction.response", "BAD_LIST", limit=2)
        movers = 0
        for i, it in enumerate(resp):
            f = f"reaction.response[{i}]"
            _keys(110, f, it, frozenset(("kind", "speed_mps", "dist_m", "duration_s", "sound", "search_m")))
            k = _enum(110, f"{f}.kind", it.get("kind"), ("ignore", "flee", "approach", "return", "emit", "hide"))
            movers += k in ("flee", "approach", "return", "hide")
            for key, hi in (("speed_mps", 40.0), ("dist_m", 1000.0), ("duration_s", 3600.0), ("search_m", 500.0)):
                if it.get(key) is not None:
                    _num(110, f"{f}.{key}", it[key], 0.0, hi)
            snd = it.get("sound")
            if snd is not None:
                _keys(541, f"{f}.sound", snd, frozenset(("duty", "lw_dba", "period_s")))
                if snd.get("duty") is not None:
                    _num(541, f"{f}.sound.duty", snd["duty"], 0.0, 1.0)
                if snd.get("lw_dba") is not None:
                    _num(541, f"{f}.sound.lw_dba", snd["lw_dba"], 0.0, 140.0)
                if snd.get("period_s") is not None:
                    _num(541, f"{f}.sound.period_s", snd["period_s"], 0.1, 3600.0)
        if movers > 1:
            raise TargetError(110, "reaction.response", "TWO_MOTION_RESPONSES")
    if r.get("alert_others") is not None and not isinstance(r["alert_others"], bool):
        raise TargetError(110, "reaction.alert_others", "NOT_A_BOOL")


def validate_config(c: dict) -> list[str]:
    """完整配置（合并后）的逐字段校验；通过时返回警告列表（radar、rf 敏感体只入 schema）。放置相关（540）在 placement.py。"""
    _keys(110, "", c, TOP_FIELDS)
    _enum(110, "class", c.get("class"), tuple(class_defaults()["classes"]))
    if c.get("id") is not None and (not isinstance(c["id"], str) or not ID_RE.match(c["id"])):
        raise TargetError(110, "id", "BAD_ID", value=str(c["id"])[:40])
    if not isinstance(c.get("enabled", True), bool):
        raise TargetError(110, "enabled", "NOT_A_BOOL")
    if "pos" in c:
        _vec(540, "pos", c["pos"], 2, 3)
    _num(110, "yaw_rad", c.get("yaw_rad", 0.0), -math.pi, math.pi)
    if c.get("alt_agl_m") is not None:
        _num(540, "alt_agl_m", c["alt_agl_m"], 5.0, 300.0)
    sz = _keys(110, "size_m", c.get("size_m"), frozenset(("l", "w", "h")))
    for k in ("l", "w", "h"):
        _num(110, f"size_m.{k}", sz.get(k), 0.05, 30.0)
    if c.get("critical_dim_m") is not None:
        _num(110, "critical_dim_m", c["critical_dim_m"], 0.02, 30.0)
    validate_motion(c.get("motion"))
    for k, lo, hi in (("albedo_vis", 0.0, 1.0), ("contrast0", -1.0, 4.0), ("camouflage", 0.0, 1.0), ("occlusion", 0.0, 1.0),
                      ("reflect_850", 0.01, 1.0), ("reflect_905", 0.01, 1.0), ("rcs_m2", 1e-4, 1e3), ("alertness", 0.0, 1.0),
                      ("hearing_th_dba", 0.0, 60.0), ("acoustic_adv_db", 0.0, 20.0), ("visual_theta0_arcmin", 0.3, 10.0),
                      ("calm_s", 0.0, 3600.0), ("alert_radius_m", 0.0, 500.0)):
        if c.get(k) is not None:
            _num(110, k, c[k], lo, hi)
    th = _keys(110, "thermal", c.get("thermal"), THERMAL_FIELDS)
    _enum(110, "thermal.mode", th.get("mode"), ("relative", "absolute"))
    for k, lo, hi in (("delta_t_k", -30.0, 200.0), ("t_surface_c", -40.0, 600.0), ("emissivity", 0.1, 1.0), ("hot_fraction", 0.01, 1.0)):
        if th.get(k) is not None:
            _num(110, f"thermal.{k}", th[k], lo, hi)
    if c.get("rcs_fluct") is not None:
        _enum(110, "rcs_fluct", c["rcs_fluct"], ("swerling0", "swerling1"))
    if c.get("rcs_band") is not None and (not isinstance(c["rcs_band"], str) or len(c["rcs_band"]) > 16):
        raise TargetError(110, "rcs_band", "BAD_STRING")
    if c.get("color_srgb") is not None and (not isinstance(c["color_srgb"], str) or len(c["color_srgb"]) > 32):
        raise TargetError(110, "color_srgb", "BAD_STRING")
    validate_sound(c.get("sound"))
    rf = c.get("rf")
    if rf is not None:
        _keys(110, "rf", rf, frozenset(("freq_mhz", "eirp_dbm", "duty")))
        if rf.get("freq_mhz") is not None:
            _num(110, "rf.freq_mhz", rf["freq_mhz"], 0.0, 1e6, lo_open=True)
        if rf.get("eirp_dbm") is not None:
            _num(110, "rf.eirp_dbm", rf["eirp_dbm"], -200.0, 200.0)
        if rf.get("duty") is not None:
            _num(110, "rf.duty", rf["duty"], 0.0, 1.0)
    _enum(110, "criterion_mode", c.get("criterion_mode", "geometric"), ("geometric", "physical"))
    _enum(110, "level_req", c.get("level_req", "R"), ("D", "R", "I"))
    if c.get("n_req_px") is not None:
        _num(110, "n_req_px", c["n_req_px"], 1.0, 200.0)
    validate_reaction(c.get("reaction") or {})
    validate_d21(c)
    bodies = c.get("sensitivity")
    if not isinstance(bodies, list):
        raise TargetError(542, "sensitivity", "NOT_A_LIST")
    if len(bodies) > MAX_BODIES:
        raise TargetError(542, "sensitivity", "TOO_MANY_BODIES", value=len(bodies), limit=MAX_BODIES)
    warnings = []
    for i, b in enumerate(bodies):
        validate_body(b, i)
        if b["kind"] == "rf" or (b["kind"] == "radar" and not isinstance(c.get("radar_rx"), dict)):
            warnings.append("SENSITIVITY_KIND_STORED_ONLY")
    if c.get("label") is not None and not isinstance(c["label"], str):
        raise TargetError(110, "label", "BAD_STRING")
    return sorted(set(warnings))


def validate_d21(c: dict) -> None:
    """D2.1 字段（M18-FR-028 至 FR-031；110 字段级 detail）。"""
    if c.get("visual_acuity_arcmin") is not None:
        _num(110, "visual_acuity_arcmin", c["visual_acuity_arcmin"], 0.3, 20.0)
    adj = c.get("hearing_band_adj_db")
    if adj is not None:
        if not isinstance(adj, list) or len(adj) != N_HEARING_BANDS:
            raise TargetError(110, "hearing_band_adj_db", "BAD_LIST", value=len(adj) if isinstance(adj, list) else None, limit=N_HEARING_BANDS)
        for i, x in enumerate(adj):
            _num(110, f"hearing_band_adj_db[{i}]", x, -20.0, 20.0)
    rx = c.get("radar_rx")
    if rx is not None:
        _keys(110, "radar_rx", rx, RADAR_RX_FIELDS)
        for k, lo, hi, lo_open in (("snr_ref_db", -50.0, 100.0, False), ("r_ref_m", 1.0, 20000.0, False), ("snr_req_db", -20.0, 60.0, False),
                                   ("fov_az_deg", 0.0, 180.0, True), ("fov_el_deg", 0.0, 90.0, True)):
            if k not in rx:
                raise TargetError(110, f"radar_rx.{k}", "REQUIRED")
            _num(110, f"radar_rx.{k}", rx[k], lo, hi, lo_open=lo_open)
    rf = c.get("rf_rx")
    if rf is not None:
        _keys(110, "rf_rx", rf, RF_RX_FIELDS)
        if rf.get("sens_dbm") is not None:
            _num(110, "rf_rx.sens_dbm", rf["sens_dbm"], -200.0, 0.0)
        if rf.get("f_mhz") is not None:
            _num(110, "rf_rx.f_mhz", rf["f_mhz"], 0.0, 1e6, lo_open=True)
        if rf.get("bw_mhz") is not None:
            _num(110, "rf_rx.bw_mhz", rf["bw_mhz"], 0.0, 1e4, lo_open=True)


def visual_acuity_of(raw: dict) -> float | None:
    """角分辨率（arcmin）：配置值，否则类别缺省（machine、object 为 None）。"""
    v = raw.get("visual_acuity_arcmin")
    if _is_num(v):
        return float(v)
    return ACUITY_DEFAULT_ARCMIN.get(str(raw.get("class")))


def band_adj_of(raw: dict) -> Any:
    """类别频段修正 [24]（dB）；未配置时 None（D2 口径：不做加法，判据逐位不变）。"""
    adj = raw.get("hearing_band_adj_db")
    if isinstance(adj, list) and len(adj) == N_HEARING_BANDS:
        import numpy as np

        return np.asarray(adj, float)
    return None


def radar_rx_of(raw: dict) -> dict | None:
    rx = raw.get("radar_rx")
    return rx if isinstance(rx, dict) else None


# ---------------------------------------------------------------- 只读视图（与 tools/fake/targets_stub.py 冻结形状一致）
@dataclass(frozen=True)
class TargetConfig:
    """合并后的识别物配置（awr.target.v1 字段）；属性访问取 `raw` 中的字段（`cfg.sensitivity`、`cfg.motion` 等）。"""

    id: str
    cls: str
    raw: dict = field(repr=False, default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        raw = object.__getattribute__(self, "raw")
        if name in raw:
            return raw[name]
        raise AttributeError(name)

    @classmethod
    def from_dict(cls, d: dict, tid: str | None = None) -> TargetConfig:
        merged = merged_config(d)
        c = merged["class"]
        return cls(tid or d.get("id") or f"{c}-01", c, merged)

    def to_dict(self) -> dict:
        out = {"id": self.id, **copy.deepcopy(self.raw)}
        return out

    @property
    def size_lwh(self) -> tuple[float, float, float]:
        s = self.raw["size_m"]
        return float(s["l"]), float(s["w"]), float(s["h"])


@dataclass(frozen=True)
class TargetState1:
    """单个识别物的运行状态快照（M19、M20 只读）。"""

    id: str
    target_no: int
    pos: tuple[float, float, float]
    yaw_rad: float
    vel: tuple[float, float, float]
    speed_mps: float
    moving: bool
    awareness: str
    alertness: float


@dataclass(frozen=True)
class TargetView:
    """M19、M20 只读视图（字段与 `tools/fake/targets_stub.TargetView` 冻结形状一致；数组按 target_no 升序）。"""

    n: int
    target_no: Any
    cls: Any
    pos: Any
    yaw_rad: Any
    vel: Any
    size_m: Any
    occlusion: Any
    cfg: tuple[TargetConfig, ...]
    rev: int
