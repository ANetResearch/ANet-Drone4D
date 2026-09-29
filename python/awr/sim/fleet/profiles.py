"""机型 profile 加载、自洽检查与 ProfileTable（M08-FR-042 至 FR-047；M08 §6.3.2、§6.7；AWR-16 §11；ADR-022、ADR-043）。

加载流程（M08 §6.7.1）：解析 `vehicles/*/params.yaml`（`yaml.safe_load`）→ schema 校验（`vehicle/vehicle_profile.schema.json`，
jsonschema 2020-12，`$ref` 经 contracts 全部 schema 的 referencing 注册表解析）→ 深合并 `variants` → 计算 `derived` 并与文件值比对
（相对差 ≤ 1%）→ VH-1 至 VH-7 → E 级值（`rejected[]` 中的值）不得出现在参数位 → 写入 ProfileTable。任一不通过抛
`ProfileError`（`353 VEHICLE_PROFILE_INVALID`），sim-core 以此拒绝启动。按 `id` 与 `variants` 建全局索引，id 重复即拒绝。

ProfileTable 按 `profile_id` 把机型参数 gather 成矩阵 `PT`（f64[P, 10]，列见 `kernels_l1.P_*`），按限速配置名 gather `LT`
（f64[L, 4]：`MPC_XY_VEL_MAX`、`MPC_XY_CRUISE`、`MPC_ACC_HOR`、`MC_YAWRATE_MAX`（rad/s））；核内按下标取值，不逐 slot 复制。
`describe(profile_id)` 给出 `GET /api/fleet/profiles/{id}` 的内容：规范化参数、derived、7 条检查、11 行孪生表（M08 7 行、
M09 Battery 1 行、M13 Camera/LiDAR/RTK 3 行）、`sensors{}`（M13 `describe()` 登记后填入）。
"""

from __future__ import annotations

import copy
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

from . import kernels_l1 as K
from . import params_px4 as P

__all__ = ["AERO_COMPOSITE", "AERO_LINEAR", "CHECKS", "LimitsProfile", "Profile", "ProfileError", "ProfileTable",
           "load_vehicle_docs", "register_sensor_describer", "vehicles_dir"]

AERO_LINEAR, AERO_COMPOSITE = 0, 1
CODE_PROFILE_INVALID = 353
ROOT = Path(__file__).resolve().parents[4]
V_TEST = 8.0  # VH-3 风速
CHECKS = ("VH-1", "VH-2", "VH-3", "VH-4", "VH-5", "VH-6", "VH-7")
CHECK_RANGE: dict[str, list[float] | None] = {"VH-1": [0.30, 0.65], "VH-2": [1.6, 3.0], "VH-3": [5.0, 15.0], "VH-4": None,
                                              "VH-5": [0.005, 0.02], "VH-6": None, "VH-7": None}
_SENSOR_DESCRIBER: list[Callable[[str, dict], dict]] = []


class ProfileError(ValueError):
    """机型文件 schema 或 VH-1 至 VH-7 失败（353 VEHICLE_PROFILE_INVALID，AWR-17 §8.4）。"""

    code = CODE_PROFILE_INVALID

    def __init__(self, profile_id: str, problems: list[str]) -> None:
        self.profile_id = profile_id
        self.problems = list(problems)
        super().__init__(f"{profile_id}: " + "; ".join(problems))


def vehicles_dir() -> Path:
    return Path(os.environ.get("AWR_VEHICLES_DIR") or (ROOT / "vehicles"))


def register_sensor_describer(fn: Callable[[str, dict], dict]) -> None:
    """M13 登记 `describe(profile_id, profile_doc) -> sensors{}`（GET /api/fleet/profiles/{id} 的 `sensors{}`）。"""
    _SENSOR_DESCRIBER.append(fn)


@dataclass(frozen=True)
class LimitsProfile:
    name: str
    vxy_max_mps: float
    cruise_mps: float
    acc_hor_mps2: float
    yawrate_max_deg_s: float
    src: str = ""


@dataclass(frozen=True)
class Profile:
    profile_id: str
    model: str
    mass_kg: float
    n_rot: int
    t_max_rotor_n: float
    omega_max_rad_s: float
    tau_motor_s: float
    aero: str  # linear | composite
    k_dv: float
    cda_m2: float
    c_rd: float
    arm_xy_m: float
    inertia_kgm2: tuple[float, float, float]
    collision_radius_m: float
    default_limits: str
    status: str = "placeholder"
    version: str = "1.0.0"
    display_name: str = ""
    limits: tuple[str, ...] = ()
    battery: dict | None = None
    doc: dict = field(default_factory=dict)
    source: str = ""

    @property
    def t_max_n(self) -> float:
        return self.n_rot * self.t_max_rotor_n

    @property
    def hover(self) -> float:
        return self.mass_kg * P.G / self.t_max_n

    @property
    def twr(self) -> float:
        return 1.0 / self.hover


# ---------------------------------------------------------------- 读取与校验
def _val(d: Any) -> Any:
    return d.get("value") if isinstance(d, dict) and "value" in d else d


def _get(doc: dict, path: str) -> Any:
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and "value" not in v:
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@cache
def _validator():
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    from awr.contracts._paths import contracts_root

    reg = Registry()
    root = contracts_root()
    for p in sorted(root.rglob("*.schema.json")):
        if "gen" in p.relative_to(root).parts or "node_modules" in p.parts:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict) and "$id" in d:
            reg = reg.with_resource(d["$id"], Resource.from_contents(d))
    schema = json.loads((root / "vehicle" / "vehicle_profile.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=reg)


def schema_errors(doc: dict) -> list[str]:
    return [f"schema {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:160]}"
            for e in _validator().iter_errors(doc)]


def derived_values(doc: dict) -> dict[str, float | None]:
    """AWR-16 §11.3 的派生量：悬停推力比、TWR、8 m/s 风悬停倾角、CdA/m、续航一致性。"""
    m = float(_val(doc["mass_kg"]))
    mot = doc["motor"]
    n = int(mot["n"])
    tmax = float(_val(mot["t_max_n"]))
    wmax = float(_val(mot["omega_max_rad_s"]))
    hover = m * P.G / (n * tmax)
    aero = doc.get("aero") or {}
    cda = _val(aero.get("cda_m2"))
    c_rd = _val((doc.get("prop") or {}).get("c_rd")) or 0.0
    if aero.get("model") == "linear":
        F = float(_val(aero["k_dv_n_per_mps"])) * V_TEST
    else:
        so = n * wmax * math.sqrt(hover)
        F = 0.5 * P.RHO0 * float(cda or 0.0) * V_TEST * V_TEST + so * float(c_rd) * V_TEST
    tilt = math.degrees(math.atan(F / (m * P.G)))
    bat = doc.get("battery")
    endur = None
    if isinstance(bat, dict):
        endur = float(_val(bat["usable_frac"])) * float(_val(bat["capacity_wh"])) * 3600.0 / float(_val(bat["p_hover_w"]))
    return {"hover_thrust": hover, "twr": 1.0 / hover, "tilt_at_8mps_deg": tilt,
            "cda_per_mass_m2kg": (float(cda) / m) if cda is not None else None, "endurance_check_s": endur}


def run_checks(doc: dict) -> list[dict[str, Any]]:
    """VH-1 至 VH-7（AWR-16 §11.3）：返回 `[{id, value, range, pass}]`。"""
    d = derived_values(doc)
    m = float(_val(doc["mass_kg"]))
    J = _val(doc["inertia_kgm2"])
    out: list[dict[str, Any]] = []

    def rng(cid: str, v: float | None) -> None:
        lo, hi = CHECK_RANGE[cid]  # type: ignore[misc]
        out.append({"id": cid, "value": None if v is None else round(v, 6), "range": [lo, hi],
                    "pass": v is None or (lo <= v <= hi)})

    rng("VH-1", d["hover_thrust"])
    rng("VH-2", d["twr"])
    rng("VH-3", d["tilt_at_8mps_deg"])
    jx, jy, jz = float(J["xx"]), float(J["yy"]), float(J["zz"])
    out.append({"id": "VH-4", "value": {"xx": jx, "yy": jy, "zz": jz}, "range": None,
                "pass": abs(jx - jy) <= 1e-6 * jx and jz > jx})
    rng("VH-5", d["cda_per_mass_m2kg"])
    bat = doc.get("battery")
    if isinstance(bat, dict) and bat.get("hover_endurance_s") is not None:
        e_file = float(_val(bat["hover_endurance_s"]))
        e = d["endurance_check_s"]
        out.append({"id": "VH-6", "value": round(float(e), 1), "range": [e_file * 0.9, e_file * 1.1],
                    "pass": abs(float(e) - e_file) <= 0.1 * e_file})
    else:
        out.append({"id": "VH-6", "value": None, "range": None, "pass": True})
    mtow = _val(doc.get("mtow_kg"))
    out.append({"id": "VH-7", "value": m, "range": None if mtow is None else [0.0, float(mtow)],
                "pass": mtow is None or m <= float(mtow)})
    return out


def _e_level_problems(doc: dict) -> list[str]:
    """E 级值（rejected[]）不得出现在参数位（AWR-16 §11.3 规则）。"""
    probs = []
    for r in doc.get("rejected") or []:
        cur = _get(doc, str(r.get("param", "")))
        val = _val(cur)
        rv = r.get("value")
        if val is None:
            continue
        same = (isinstance(val, (int, float)) and isinstance(rv, (int, float)) and math.isclose(val, rv, rel_tol=1e-9)) \
            or val == rv
        if same:
            probs.append(f"E_VALUE_IN_PARAM {r.get('param')}={rv}（{r.get('why')}）")
    return probs


def validate_doc(doc: dict, *, pid: str | None = None) -> tuple[list[dict], dict, list[str]]:
    """schema、derived 比对（≤ 1%）、VH-1..VH-7、E 级值；返回 (checks, derived, problems)。"""
    pid = pid or str(doc.get("id"))
    problems = schema_errors(doc)
    if problems:
        return [], {}, problems
    d = derived_values(doc)
    checks = run_checks(doc)
    problems += [f"{c['id']} value={c['value']} range={c['range']}" for c in checks if not c["pass"]]
    for k, v in (doc.get("derived") or {}).items():
        calc = d.get(k)
        if calc is None or v is None:
            continue
        if abs(float(v) - calc) > 0.01 * max(abs(calc), 1e-12):
            problems.append(f"DERIVED_MISMATCH {k}: file {v} vs computed {calc:.4g}")
    problems += _e_level_problems(doc)
    return checks, d, problems


def load_vehicle_docs(vdir: Path | None = None) -> dict[str, tuple[dict, Path]]:
    """读取 `vehicles/*/params.yaml` 并展开 variants；返回 {profile_id: (规范化文档, 文件路径)}；id 重复即拒绝。"""
    import yaml

    vdir = vdir or vehicles_dir()
    out: dict[str, tuple[dict, Path]] = {}
    for f in sorted(vdir.glob("*/params.yaml")):
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise ProfileError(f.parent.name, [f"{f}: {e}"]) from e
        if not isinstance(doc, dict):
            raise ProfileError(f.parent.name, [f"{f}: not a mapping"])
        base = {k: v for k, v in doc.items() if k != "variants"}
        items = [(str(doc.get("id")), base)]
        for vid, over in (doc.get("variants") or {}).items():
            merged = _deep_merge(base, over)
            merged["id"] = vid
            items.append((vid, merged))
        for pid, d in items:
            if pid in out:
                raise ProfileError(pid, [f"V-VH-01 id 重复：{f} 与 {out[pid][1]}"])
            out[pid] = (d, f)
    return out


def _limits_from_doc(doc: dict) -> dict[str, LimitsProfile]:
    out = {}
    for name, v in (doc.get("limits_profiles") or {}).items():
        if name == "default" or not isinstance(v, dict):
            continue
        pp = v.get("px4_params") or {}
        out[name] = LimitsProfile(name, float(pp.get("MPC_XY_VEL_MAX", P.MPC_XY_VEL_MAX)),
                                  float(pp.get("MPC_XY_CRUISE", P.MPC_XY_CRUISE)),
                                  float(pp.get("MPC_ACC_HOR", P.MPC_ACC_HOR)),
                                  float(pp.get("MC_YAWRATE_MAX", P.MC_YAWRATE_MAX)), str(v.get("src", "")))
    return out


def profile_from_doc(doc: dict, source: str = "") -> Profile:
    mot = doc["motor"]
    aero = doc.get("aero") or {}
    J = _val(doc["inertia_kgm2"])
    geo = doc["geometry"]
    lp = doc.get("limits_profiles") or {}
    return Profile(
        profile_id=str(doc["id"]), model=str(doc["id"]).split("_")[0], mass_kg=float(_val(doc["mass_kg"])),
        n_rot=int(mot["n"]), t_max_rotor_n=float(_val(mot["t_max_n"])), omega_max_rad_s=float(_val(mot["omega_max_rad_s"])),
        tau_motor_s=float(_val(mot["tau_s"])), aero=str(aero.get("model", "composite")),
        k_dv=float(_val(aero.get("k_dv_n_per_mps")) or 0.0) if aero.get("model") == "linear" else 0.0,
        cda_m2=float(_val(aero.get("cda_m2")) or 0.0) if aero.get("model") != "linear" else 0.0,
        c_rd=float(_val((doc.get("prop") or {}).get("c_rd")) or 0.0) if aero.get("model") != "linear" else 0.0,
        arm_xy_m=float(_val(geo["arm_xy_m"])), inertia_kgm2=(float(J["xx"]), float(J["yy"]), float(J["zz"])),
        collision_radius_m=float(_val(geo["collision_radius_m"])), default_limits=str(lp.get("default", "px4_default")),
        status=str(doc.get("status", "placeholder")), version=str(doc.get("profile_version", "1.0.0")),
        display_name=str(doc.get("display_name", doc["id"])),
        limits=tuple(k for k in lp if k != "default"), battery=doc.get("battery"), doc=doc, source=source)


@cache
def _load_cached(vdir: str, stamp: tuple) -> tuple[dict[str, Profile], dict[str, LimitsProfile], dict[str, dict]]:
    docs = load_vehicle_docs(Path(vdir))
    profiles: dict[str, Profile] = {}
    limits: dict[str, LimitsProfile] = {}
    meta: dict[str, dict] = {}
    for pid, (doc, f) in docs.items():
        checks, derived, probs = validate_doc(doc, pid=pid)
        if probs:
            raise ProfileError(pid, probs)
        profiles[pid] = profile_from_doc(doc, str(f))
        meta[pid] = {"checks": checks, "derived": derived}
        for name, lp in _limits_from_doc(doc).items():
            prev = limits.get(name)
            if prev is not None and (prev.vxy_max_mps, prev.cruise_mps, prev.acc_hor_mps2, prev.yawrate_max_deg_s) != \
                    (lp.vxy_max_mps, lp.cruise_mps, lp.acc_hor_mps2, lp.yawrate_max_deg_s):
                raise ProfileError(pid, [f"限速配置 {name} 与其他机型的同名配置取值不同"])
            limits.setdefault(name, lp)
    return profiles, limits, meta


def _stamp(vdir: Path) -> tuple:
    return tuple((str(p), p.stat().st_mtime_ns) for p in sorted(vdir.glob("*/params.yaml")))


class ProfileTable:
    """按 profile 下标与限速配置下标 gather 的参数矩阵（M08 §6.3.2）。"""

    def __init__(self, profiles: dict[str, Profile] | None = None, limits: dict[str, LimitsProfile] | None = None, *,
                 vehicles: Path | None = None) -> None:
        meta: dict[str, dict] = {}
        if profiles is None:
            vdir = Path(vehicles) if vehicles is not None else vehicles_dir()
            profiles, lims, meta = _load_cached(str(vdir), _stamp(vdir))
            limits = {**lims, **(limits or {})}
        self.profiles = dict(profiles)
        self.limits = dict(limits or {})
        self.meta = meta
        for p in self.profiles.values():
            if p.default_limits not in self.limits:
                raise ProfileError(p.profile_id, [f"默认限速配置 {p.default_limits} 未定义"])
        self.ids = list(self.profiles)
        self.limit_ids = list(self.limits)
        if len(self.ids) > 65535 or len(self.limit_ids) > 255:
            raise ProfileError("*", ["profile 或限速配置过多"])
        pr = [self.profiles[k] for k in self.ids]
        self.PT = np.zeros((len(pr), 10))
        for r, p in enumerate(pr):
            self.PT[r] = (p.mass_kg, p.t_max_n, p.hover, p.tau_motor_s, float(p.n_rot), p.omega_max_rad_s,
                          float(AERO_COMPOSITE if p.aero == "composite" else AERO_LINEAR), p.k_dv, p.cda_m2, p.c_rd)
        li = [self.limits[k] for k in self.limit_ids]
        self.LT = np.zeros((len(li), 4))
        for r, x in enumerate(li):
            self.LT[r] = (x.vxy_max_mps, x.cruise_mps, x.acc_hor_mps2, math.radians(x.yawrate_max_deg_s))
        # 兼容字段（按列的视图）
        self.mass = self.PT[:, K.P_MASS]
        self.t_max = self.PT[:, K.P_TMAX]
        self.hover = self.PT[:, K.P_HOVER]
        self.tau = self.PT[:, K.P_TAU]
        self.n_rot = self.PT[:, K.P_NROT]
        self.omega_max = self.PT[:, K.P_WMAX]
        self.aero_model = self.PT[:, K.P_AERO].astype(np.uint8)
        self.k_dv = self.PT[:, K.P_KDV]
        self.cda = self.PT[:, K.P_CDA]
        self.c_rd = self.PT[:, K.P_CRD]
        self.collision_r = np.array([p.collision_radius_m for p in pr])
        self.lim_vxy_max = self.LT[:, K.L_VXY]
        self.lim_cruise = self.LT[:, K.L_CRUISE]
        self.lim_acc_hor = self.LT[:, K.L_ACC]
        self.lim_yawrate = self.LT[:, K.L_YAWRATE]

    def index(self, profile_id: str) -> int:
        try:
            return self.ids.index(profile_id)
        except ValueError:
            raise KeyError(profile_id) from None

    def limits_index(self, name: str | None, profile_id: str) -> int:
        n = name or self.profiles[profile_id].default_limits
        try:
            return self.limit_ids.index(n)
        except ValueError:
            raise KeyError(n) from None

    def get(self, profile_id: str) -> Profile:
        return self.profiles[profile_id]

    def with_aero(self, profile_id: str, model: str) -> ProfileTable:
        """回归用：返回把某 profile 的气动模型覆盖为 linear/composite 的新表（FleetConfig.aero_override）。"""
        p = self.profiles[profile_id]
        if p.aero == model:
            return self
        doc = dict(p.doc)
        new = Profile(**{**p.__dict__, "aero": model})
        if model == "linear":
            kdv = _val((doc.get("aero") or {}).get("sih_kdv_equiv")) or 0.35
            new = Profile(**{**new.__dict__, "k_dv": float(kdv), "cda_m2": 0.0, "c_rd": 0.0})
        return ProfileTable({**self.profiles, profile_id: new}, self.limits)

    # ------------------------------------------------------------ REST 数据（GET /api/fleet/profiles[/{id}]）
    def summary(self) -> list[dict[str, Any]]:
        out = []
        for pid in self.ids:
            p = self.profiles[pid]
            out.append({"profile_id": pid, "model": p.model, "display_name": p.display_name, "mass_kg": p.mass_kg,
                        "twr": round(p.twr, 3), "cda_m2": p.cda_m2 or None, "vmax_mps": self.limits[p.default_limits].vxy_max_mps,
                        "status": p.status, "speed_profiles": list(p.limits), "default_speed_profile": p.default_limits})
        return out

    def describe(self, profile_id: str) -> dict[str, Any]:
        p = self.profiles[profile_id]
        doc = p.doc
        m = self.meta.get(profile_id) or {}
        checks = m.get("checks") or run_checks(doc)
        derived = m.get("derived") or derived_values(doc)
        sensors: dict[str, Any] = {}
        for fn in _SENSOR_DESCRIBER:
            try:
                sensors.update(fn(profile_id, doc) or {})
            except Exception:  # 传感器描述失败不影响机型参数
                continue
        return {"profile": json.loads(json.dumps(doc, default=str)), "derived": {k: v for k, v in derived.items()},
                "checks": checks, "twin": twin_table(doc, Path(p.source).parent if p.source else None), "sensors": sensors}


_CONF_ORDER = "ABCDE"


def _worst(*confs: str | None) -> str | None:
    cs = [c for c in confs if c]
    return max(cs, key=_CONF_ORDER.index) if cs else None


def _conf(doc: dict, *paths: str) -> str | None:
    return _worst(*((_get(doc, p) or {}).get("conf") if isinstance(_get(doc, p), dict) else None for p in paths))


def _sensor_conf(vdir: Path | None, rel: str | None) -> tuple[str | None, str]:
    if not rel or vdir is None:
        return None, "missing"
    f = vdir / rel
    if not f.exists():
        return None, "missing"
    try:
        import yaml

        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        return (d.get("conf") if isinstance(d, dict) else None), "present"
    except Exception:
        return None, "invalid"


def twin_table(doc: dict, vdir: Path | None) -> list[dict[str, Any]]:
    """ADR-043 的 11 个组成部分（M08 §6.7.2；UI 单机详情"孪生"标签）。"""
    placeholder = doc.get("status") == "placeholder"
    st = "placeholder" if placeholder else ("regression" if doc.get("status") == "regression" else "identified")
    sensors = doc.get("sensors") or {}
    cam_c, cam_s = _sensor_conf(vdir, sensors.get("camera"))
    lid_c, lid_s = _sensor_conf(vdir, sensors.get("mid360"))
    rtk_c, rtk_s = _sensor_conf(vdir, sensors.get("gnss"))
    bat = doc.get("battery")
    rows = [
        ("Geometry", "geometry.*、model/model.yaml", _conf(doc, "geometry.arm_xy_m", "geometry.collision_radius_m",
                                                           "geometry.wheelbase_m"), "displayed", "外形尺寸误差 ≤ 2 cm（对照 STL）",
         "V0.1", "M08"),
        ("Mass", "mass_kg", _conf(doc, "mass_kg"), st, "称重误差 ≤ 1%", "V0.4", "M08"),
        ("Inertia", "inertia_kgm2", _conf(doc, "inertia_kgm2"), st, "姿态阶跃响应 RMSE ≤ 2°（ULog 对照）", "V0.4", "M08"),
        ("Motor", "motor.{tau_s, k_f, omega_max_rad_s}", _conf(doc, "motor.tau_s", "motor.k_f", "motor.omega_max_rad_s"), st,
         "推力台测曲线误差 ≤ 5%", "V0.4", "M08"),
        ("Propeller", "prop.{d_m, c_rd}", _conf(doc, "prop.d_m", "prop.c_rd"), st, "同上；风天悬停倾角拟合 CdA、c_rd", "V0.4", "M08"),
        ("Battery", "battery{}（字段 M08，模型 M09）",
         _conf(doc, "battery.capacity_wh", "battery.usable_frac", "battery.p_hover_w") if isinstance(bat, dict) else None,
         st if isinstance(bat, dict) else "none", "悬停功率误差 ≤ 10%，续航误差 ≤ 10%", "V0.4", "M09"),
        ("Flight Controller", "fidelity、limits_profiles", "A" if doc.get("status") == "regression" else "C", "L1",
         "SIH 或 ULog 对照 17 项容差（g08 §9.3）", "V0.2", "M08"),
        ("Camera", sensors.get("camera") or "sensors/camera.yaml", cam_c, cam_s, "重投影误差 ≤ 1 px（标定板）", "V0.4", "M13"),
        ("LiDAR", sensors.get("mid360") or "sensors/mid360.yaml", lid_c, lid_s, "点密度分布 KL 散度 ≤ 0.1", "V0.2", "M13"),
        ("RTK", sensors.get("gnss") or "sensors/gnss.yaml", rtk_c, rtk_s, "静态定位 95% 分位误差差值 ≤ 1 cm", "V0.5", "M13"),
        ("Payload", "payload[]", None, "none" if not doc.get("payload") else st, "质量与挂点误差 ≤ 1%", "V0.6", "M08"),
    ]
    return [{"component": c, "carrier": car, "conf": conf, "status": s, "metric": met, "target_version": tv, "owner": own}
            for c, car, conf, s, met, tv, own in rows]
