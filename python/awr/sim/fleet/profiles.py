"""机型 profile 加载、自洽检查与 ProfileTable（M08-FR-042 至 FR-047；D2：M08-FR-205 至 FR-209；M08 §6.3.2、§6.7、§15.3.3；
AWR-16 §11；AWR-20 §7、§8；ADR-022、ADR-043、ADR-095、ADR-115）。

加载流程（M08 §6.7.1；AWR-20 §8.1）：`vehicles/catalog.json`（M21 维护，`awr.vehicle_catalog.v1`）存在时只装载目录列出的机型包，
否则扫描 `vehicles/*/params.yaml`（D1 行为）→ schema 校验（`vehicle/vehicle_profile.schema.json` 1.1，jsonschema 2020-12，`$ref`
经 contracts 全部 schema 的 referencing 注册表解析）→ 深合并 `variants` → 按 `frame.type` 分支检查（ADR-115 ①）：多旋翼 VH-1 至
VH-7；VTOL 升力系统 VH-1 至 VH-3（VH-4 由 M21 登记的 VT-3 代替）；固定翼与 VTOL 的固定翼部分执行 M21 经
`register_profile_check` 登记的 FW、VT 检查；全部构型另执行挂载质量检查 MNT-1 → 计算 `derived` 并与文件值比对（相对差 ≤ 1%）
→ E 级值（`rejected[]` 中的值）不得出现在参数位 → 写入 ProfileTable。任一不通过抛 `ProfileError`（`353 VEHICLE_PROFILE_INVALID`），
sim-core 以此拒绝启动。按 `id` 与 `variants` 建全局索引，id 重复即拒绝。

版本（M08-FR-206，ADR-115 ③）：机型引用写作 `<id>`（当前版本）或 `<id>@<semver>`；旧版本存档在 `vehicles/<dir>/history/
params-<version>.yaml`，只在显式引用时按需加载（不进入目录），表键为 `<id>@<version>`。会话克隆（M08-FR-208）的 id 为
`<源 id>_c<n>`，只允许覆盖白名单字段（AWR-20 §7.6），在步边界由调用方执行；表在增删条目时写时复制重建 `PT`、`LT`（O(P)，
不在 stage 中途替换数组的内容）。

ProfileTable 按表键把机型参数 gather 成矩阵 `PT`（f64[P, 10]，列见 `kernels_l1.P_*`），按限速配置名 gather `LT`（f64[L, 6]：
`MPC_XY_VEL_MAX`、`MPC_XY_CRUISE`、`MPC_ACC_HOR`、`MC_YAWRATE_MAX`（rad/s）、`MPC_Z_VEL_MAX_UP`、`MPC_Z_VEL_MAX_DN`；后两列 D2
起按机型生效，缺省 3.0、1.5 m/s 与 D1 的全局常量相同）；核内按下标取值，不逐 slot 复制。固定翼没有 L1 参数，其 PT 行只为下标
连续而填中性值（L1 不处理带 FW 位的 slot）。`describe(profile_id)` 给出 `GET /api/fleet/profiles/{id}` 的内容：规范化参数、
derived、检查（VH 与 MNT-1，另含登记的构型检查）、11 行孪生表、`sensors{}`、`frame_kind`、`catalog`。
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

from . import kernels_l1 as K
from . import params_px4 as P

__all__ = ["AERO_COMPOSITE", "AERO_LINEAR", "CHECKS", "CLONE_MAX", "CLONE_SESSION_QUOTA", "CloneError", "LimitsProfile", "Profile",
           "ProfileError", "ProfileTable", "all_checks", "frame_kind_of", "frame_type_of", "load_vehicle_docs",
           "register_sensor_describer", "vehicles_dir"]

AERO_LINEAR, AERO_COMPOSITE = 0, 1
CODE_PROFILE_INVALID = 353
CODE_MODEL_UNKNOWN = 520
CODE_CLONE_INVALID = 522
CODE_ENTITY_LIMIT = 505
ROOT = Path(__file__).resolve().parents[4]
V_TEST = 8.0  # VH-3 风速
CHECKS = ("VH-1", "VH-2", "VH-3", "VH-4", "VH-5", "VH-6", "VH-7")
CHECK_RANGE: dict[str, list[float] | None] = {"VH-1": [0.30, 0.65], "VH-2": [1.6, 3.0], "VH-3": [5.0, 15.0], "VH-4": None,
                                              "VH-5": [0.005, 0.02], "VH-6": None, "VH-7": None}
MULTIROTOR_FRAMES = frozenset({"quad_x", "hex_x"})
FRAME_KIND = {"quad_x": 0, "hex_x": 0, "fixed_wing": 1, "vtol_quadplane": 2}
CLONE_MAX = 16  # 表内会话克隆的容量（M08 §15.4.4）
CLONE_SESSION_QUOTA = 8  # 每会话克隆配额（AWR-04 §4.4.3；超出 505）
REF_RE = re.compile(r"^(?P<id>[a-z0-9_]{1,40})(?:@(?P<ver>[0-9]+\.[0-9]+\.[0-9]+))?$")
_SENSOR_DESCRIBER: list[Callable[[str, dict], dict]] = []


class ProfileError(ValueError):
    """机型文件 schema 或检查失败（353 VEHICLE_PROFILE_INVALID，AWR-17 §8.4）。"""

    code = CODE_PROFILE_INVALID

    def __init__(self, profile_id: str, problems: list[str]) -> None:
        self.profile_id = profile_id
        self.problems = list(problems)
        super().__init__(f"{profile_id}: " + "; ".join(problems))


class CloneError(ValueError):
    """会话克隆失败：522 VEHICLE_CLONE_INVALID（逐项原因）、505 SANDBOX_ENTITY_LIMIT（配额）、520 MODEL_UNKNOWN、105（删除被引用的
    克隆，detail MODEL_IN_USE）。`problems` 每项 `{field, rule, value, limit, message_zh}`（契约 clone.schema.json violation；
    AWR-20 §8.2 的 path、bound 即 field、limit）。"""

    def __init__(self, code: int, problems: list[dict] | None = None, detail: str | None = None) -> None:
        self.code = int(code)
        self.problems = list(problems or [])
        self.detail = detail
        super().__init__(f"{code} {detail or ''} {self.problems}")


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
    z_up_mps: float = P.MPC_Z_VEL_MAX_UP  # D2：MPC_Z_VEL_MAX_UP（LT 第 5 列）
    z_dn_mps: float = P.MPC_Z_VEL_MAX_DN  # D2：MPC_Z_VEL_MAX_DN（LT 第 6 列）

    def key(self) -> tuple:
        return (self.vxy_max_mps, self.cruise_mps, self.acc_hor_mps2, self.yawrate_max_deg_s, self.z_up_mps, self.z_dn_mps)


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
    # D2（M08-FR-205 至 FR-210）
    frame_type: str = "quad_x"
    frame_kind: int = 0
    mtow_kg: float | None = None
    catalog: dict | None = None
    ref: str = ""  # 表键：`<id>`（当前版本）、`<id>@<version>`（存档）或会话克隆 id
    base_ref: str | None = None  # 会话克隆的源

    @property
    def t_max_n(self) -> float:
        return self.n_rot * self.t_max_rotor_n

    @property
    def hover(self) -> float:
        return self.mass_kg * P.G / self.t_max_n

    @property
    def twr(self) -> float:
        return 1.0 / self.hover

    @property
    def model_dir(self) -> str:
        """机型目录（传感器挂载文件等相对路径的基准）：存档版本 `history/params-<v>.yaml` 的上一级，其余为 source 所在目录。"""
        if not self.source:
            return ""
        f = Path(self.source)
        return str(f.parent.parent if f.parent.name == "history" else f.parent)

    @property
    def sensors_map(self) -> dict:
        """本版本文档的 `sensors:` 映射（字符串或对象形式；存档版本与会话克隆各取自己的文档）。"""
        return dict(self.doc.get("sensors") or {}) if isinstance(self.doc, dict) else {}

    @property
    def sandbox_limits(self) -> str:
        """沙盒实例的缺省限速档（`catalog.limits_default`，AWR-20 §4.3；没有目录字段时取 D1 的 `limits_profiles.default`）。"""
        c = self.catalog or {}
        return str(c.get("limits_default") or self.default_limits)


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


def frame_type_of(doc: dict) -> str:
    return str((doc.get("frame") or {}).get("type") or "quad_x")


def frame_kind_of(doc: dict) -> int:
    """0 多旋翼、1 固定翼、2 VTOL（M08-FR-210）。"""
    return FRAME_KIND.get(frame_type_of(doc), 0)


@cache
def _validator():
    from jsonschema import Draft202012Validator

    from awr.contracts._paths import contracts_root
    from awr.sim.schemas import contracts_registry  # 与传感器、剧本校验共用一次扫描（ADR-061）

    schema = json.loads((contracts_root() / "vehicle" / "vehicle_profile.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=contracts_registry())


def schema_errors(doc: dict) -> list[str]:
    return [f"schema {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:160]}"
            for e in _validator().iter_errors(doc)]


def derived_values(doc: dict) -> dict[str, float | None]:
    """AWR-16 §11.3 的派生量（多旋翼与 VTOL 升力系统）：悬停推力比、TWR、8 m/s 风悬停倾角、CdA/m、续航一致性。"""
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
    if isinstance(bat, dict) and bat.get("p_hover_w") is not None:
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
    if isinstance(bat, dict) and bat.get("hover_endurance_s") is not None and d["endurance_check_s"] is not None:
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


def _mount_mass(vdir: Path | None, rel: str) -> float | None:
    """挂载文件的质量（kg）：文件内 `mass_kg`，否则按 `spec_ref` 取型号库 `vehicles/sensor_models/<id>.yaml` 的 `mass_kg`。"""
    if vdir is None:
        return None
    f = vdir / rel
    try:
        d = yaml_load(f.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    m = _val(d.get("mass_kg")) if isinstance(d, dict) else None
    if m is None and isinstance(d, dict) and isinstance(d.get("spec_ref"), str):
        mid = d["spec_ref"].split("@", 1)[0]
        lib = vdir.parent / "sensor_models" / f"{mid}.yaml"
        try:
            md = yaml_load(lib.read_text(encoding="utf-8")) or {}
            m = _val(md.get("mass_kg"))
        except Exception:
            m = None
    return float(m) if isinstance(m, (int, float)) else None


def mount_check(doc: dict, vdir: Path | None = None) -> dict[str, Any]:
    """MNT-1（M08-FR-207）：`mass_kg + Σ 可选挂载质量 ≤ mtow_kg`；可选挂载为 `sensors` 映射中对象形式且 `default: false` 的
    槽位（质量取挂载文件或型号库）。没有 `mtow_kg` 时通过。"""
    m = float(_val(doc["mass_kg"]))
    opt = 0.0
    unknown: list[str] = []
    for slot, v in (doc.get("sensors") or {}).items():
        if isinstance(v, dict) and not v.get("default", False):
            mm = _mount_mass(vdir, str(v.get("file", "")))
            if mm is None:
                unknown.append(slot)
            else:
                opt += mm
    mtow = _val(doc.get("mtow_kg"))
    total = m + opt
    row = {"id": "MNT-1", "value": round(total, 4), "range": None if mtow is None else [0.0, float(mtow)],
           "pass": mtow is None or total <= float(mtow) + 1e-9}
    if unknown:
        row["unknown_mounts"] = unknown
    return row


def registered_checks(doc: dict) -> list[dict[str, Any]]:
    """M21 等经 `register_profile_check` 登记、适用于本构型的检查（M08-FR-207）；检查函数异常记为不通过。"""
    from .stages.registry import registry

    ft = frame_type_of(doc)
    out: list[dict[str, Any]] = []
    for spec in registry().profile_checks:
        if ft not in spec.frame_types:
            continue
        try:
            r = spec.fn(doc)
        except Exception as e:  # 检查本身出错按不通过处理（不让坏检查放行坏参数）
            r = {"pass": False, "msg": f"{type(e).__name__}: {e}"}
        rows = r if isinstance(r, list) else [r]
        for x in rows:
            if isinstance(x, bool):
                x = {"pass": x}
            if not isinstance(x, dict):
                continue
            y = {"id": x.get("id") or x.get("check") or spec.check_id, "value": x.get("value"), "range": x.get("range"),
                 "pass": bool(x.get("pass", True))}
            if x.get("msg"):
                y["msg"] = str(x["msg"])
            out.append(y)
    return out


def all_checks(doc: dict, vdir: Path | None = None) -> list[dict[str, Any]]:
    """按构型分支的全部检查行（ADR-115 ①；AWR-20 §8.1）。"""
    ft = frame_type_of(doc)
    out: list[dict[str, Any]] = []
    if ft in MULTIROTOR_FRAMES:
        out += run_checks(doc)
    elif ft == "vtol_quadplane":
        out += [c for c in run_checks(doc) if c["id"] in ("VH-1", "VH-2", "VH-3")]
    out += registered_checks(doc)
    out.append(mount_check(doc, vdir))
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


def validate_doc(doc: dict, *, pid: str | None = None, vdir: Path | None = None) -> tuple[list[dict], dict, list[str]]:
    """schema、按构型的检查、derived 比对（≤ 1%）、E 级值；返回 (checks, derived, problems)。"""
    pid = pid or str(doc.get("id"))
    problems = schema_errors(doc)
    if problems:
        return [], {}, problems
    ft = frame_type_of(doc)
    d: dict[str, Any] = derived_values(doc) if ft in MULTIROTOR_FRAMES or ft == "vtol_quadplane" else {}
    checks = all_checks(doc, vdir)
    problems += [f"{c['id']} value={c['value']} range={c['range']}" + (f" {c['msg']}" if c.get("msg") else "")
                 for c in checks if not c["pass"]]
    for k, v in (doc.get("derived") or {}).items():
        calc = d.get(k)
        if calc is None or v is None or not isinstance(v, (int, float)):
            continue
        if abs(float(v) - calc) > 0.01 * max(abs(calc), 1e-12):
            problems.append(f"DERIVED_MISMATCH {k}: file {v} vs computed {calc:.4g}")
    problems += _e_level_problems(doc)
    return checks, d, problems


def yaml_load(text: str):
    """`yaml.safe_load` 的等价物：有 libyaml 时用 CSafeLoader（同一 SafeConstructor，结果相同；约快 10 倍，sim-core 启动与
    重启时解析机型与传感器文件，D1-AC-11a，FX-SIM1）。"""
    import yaml

    return yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))


def read_catalog(vdir: Path) -> dict | None:
    """`vehicles/catalog.json`（M21，`awr.vehicle_catalog.v1`）；不存在时 None（D1 行为：扫描全部机型包）。"""
    f = vdir / "catalog.json"
    if not f.exists():
        return None
    try:
        cat = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ProfileError("catalog", [f"{f}: {e}"]) from e
    if not isinstance(cat, dict) or not isinstance(cat.get("models"), list):
        raise ProfileError("catalog", [f"{f}: 缺少 models[]（awr.vehicle_catalog.v1）"])
    return cat


def _expand(doc: dict) -> list[tuple[str, dict]]:
    base = {k: v for k, v in doc.items() if k != "variants"}
    items = [(str(doc.get("id")), base)]
    for vid, over in (doc.get("variants") or {}).items():
        merged = _deep_merge(base, over)
        merged["id"] = vid
        items.append((vid, merged))
    return items


def load_vehicle_docs(vdir: Path | None = None) -> dict[str, tuple[dict, Path]]:
    """读取机型包并展开 variants；返回 {profile_id: (规范化文档, 文件路径)}；id 重复即拒绝。目录存在时只读目录列出的包
    （`models[].dir`，M08-FR-205），否则读 `vehicles/*/params.yaml`（`history/` 与 `sensor_models/` 不会被读到）。"""
    import yaml

    vdir = vdir or vehicles_dir()
    cat = read_catalog(vdir)
    if cat is not None:
        dirs = sorted({str(m.get("dir") or m.get("package") or m.get("id")) for m in cat["models"] if isinstance(m, dict)})
        files = [vdir / d / "params.yaml" for d in dirs]
        for f in files:
            if not f.exists():
                raise ProfileError(f.parent.name, [f"CI-1 目录列出的机型包缺少 {f}"])
    else:
        files = sorted(vdir.glob("*/params.yaml"))
    out: dict[str, tuple[dict, Path]] = {}
    for f in files:
        try:
            doc = yaml_load(f.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise ProfileError(f.parent.name, [f"{f}: {e}"]) from e
        if not isinstance(doc, dict):
            raise ProfileError(f.parent.name, [f"{f}: not a mapping"])
        for pid, d in _expand(doc):
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
                                  float(pp.get("MC_YAWRATE_MAX", P.MC_YAWRATE_MAX)), str(v.get("src", "")),
                                  float(pp.get("MPC_Z_VEL_MAX_UP", P.MPC_Z_VEL_MAX_UP)),
                                  float(pp.get("MPC_Z_VEL_MAX_DN", P.MPC_Z_VEL_MAX_DN)))
    return out


def profile_from_doc(doc: dict, source: str = "", *, ref: str | None = None) -> Profile:
    """文档 → Profile。固定翼没有 L1 字段（schema 1.1），其 L1 参数取中性占位值（推重比 2，L1 不处理带 FW 位的 slot）。"""
    ft = frame_type_of(doc)
    fk = FRAME_KIND.get(ft, 0)
    aero = doc.get("aero") or {}
    geo = doc.get("geometry") or {}
    lp = doc.get("limits_profiles") or {}
    m = float(_val(doc["mass_kg"]))
    mtow = _val(doc.get("mtow_kg"))
    pid = str(doc["id"])
    model = "-".join(pid.split("_")[:2]) if pid.startswith("awr_") else pid.split("_")[0]  # awr_v1 -> awr-v1（17 §17.1 机体 id）
    common = {"profile_id": pid, "model": model, "mass_kg": m,
              "collision_radius_m": float(_val(geo.get("collision_radius_m")) or 0.5),
              "default_limits": str(lp.get("default", "px4_default")), "status": str(doc.get("status", "placeholder")),
              "version": str(doc.get("profile_version", "1.0.0")), "display_name": str(doc.get("display_name", pid)),
              "limits": tuple(k for k in lp if k != "default"), "battery": doc.get("battery"), "doc": doc, "source": source,
              "frame_type": ft, "frame_kind": fk, "mtow_kg": None if mtow is None else float(mtow),
              "catalog": doc.get("catalog") if isinstance(doc.get("catalog"), dict) else None, "ref": ref or pid}
    if ft == "fixed_wing":
        size = _val(geo.get("size_m")) or {}
        span = max([float(size.get(k, 0.0)) for k in ("l", "w")] + [0.5])
        return Profile(n_rot=4, t_max_rotor_n=2.0 * m * P.G / 4.0, omega_max_rad_s=1000.0, tau_motor_s=0.05,
                       aero="composite", k_dv=0.0, cda_m2=0.0, c_rd=0.0, arm_xy_m=span / 2.0, inertia_kgm2=(1.0, 1.0, 2.0),
                       **common)
    mot = doc["motor"]
    J = _val(doc["inertia_kgm2"])
    return Profile(
        n_rot=int(mot["n"]), t_max_rotor_n=float(_val(mot["t_max_n"])), omega_max_rad_s=float(_val(mot["omega_max_rad_s"])),
        tau_motor_s=float(_val(mot["tau_s"])), aero=str(aero.get("model", "composite")),
        k_dv=float(_val(aero.get("k_dv_n_per_mps")) or 0.0) if aero.get("model") == "linear" else 0.0,
        cda_m2=float(_val(aero.get("cda_m2")) or 0.0) if aero.get("model") != "linear" else 0.0,
        c_rd=float(_val((doc.get("prop") or {}).get("c_rd")) or 0.0) if aero.get("model") != "linear" else 0.0,
        arm_xy_m=float(_val(geo["arm_xy_m"])), inertia_kgm2=(float(J["xx"]), float(J["yy"]), float(J["zz"])), **common)


def _merge_limits(limits: dict[str, LimitsProfile], new: dict[str, LimitsProfile], pid: str) -> None:
    for name, lp in new.items():
        prev = limits.get(name)
        if prev is not None and prev.key() != lp.key():
            raise ProfileError(pid, [f"限速配置 {name} 与其他机型的同名配置取值不同"])
        limits.setdefault(name, lp)


def _check_key() -> tuple:
    from .stages.registry import registry

    return tuple((c.check_id, tuple(sorted(c.frame_types))) for c in registry().profile_checks)


@cache
def _load_cached(vdir: str, stamp: tuple, checks: tuple = ()) -> tuple[dict[str, Profile], dict[str, LimitsProfile],
                                                                          dict[str, dict]]:
    docs = load_vehicle_docs(Path(vdir))
    profiles: dict[str, Profile] = {}
    limits: dict[str, LimitsProfile] = {}
    meta: dict[str, dict] = {}
    for pid, (doc, f) in docs.items():
        checks_, derived, probs = validate_doc(doc, pid=pid, vdir=f.parent)
        if probs:
            raise ProfileError(pid, probs)
        profiles[pid] = profile_from_doc(doc, str(f))
        meta[pid] = {"checks": checks_, "derived": derived}
        _merge_limits(limits, _limits_from_doc(doc), pid)
    return profiles, limits, meta


def _stamp(vdir: Path) -> tuple:
    files = sorted(vdir.glob("*/params.yaml"))
    cat = vdir / "catalog.json"
    if cat.exists():
        files.append(cat)
    return tuple((str(p), p.stat().st_mtime_ns) for p in files)


# ---------------------------------------------------------------- 会话克隆的白名单与边界（AWR-20 §7.6，CL-1 至 CL-4）
_MR = frozenset({"multirotor", "vtol"})  # 多旋翼与 VTOL 的 MC 模式
_FW = frozenset({"fixed_wing", "vtol"})
_ALL = frozenset({"multirotor", "fixed_wing", "vtol"})
_CATEGORY = {0: "multirotor", 1: "fixed_wing", 2: "vtol"}


def _prob(rule: str, path: str, value: Any, bound: Any, msg: str) -> dict:
    """违规项（契约 `vehicle/clone.schema.json#/$defs/violation`：field、rule、value、limit、message_zh）。"""
    return {"field": path, "rule": rule, "value": value, "limit": bound, "message_zh": msg}


class ProfileTable:
    """按表键与限速配置下标 gather 的参数矩阵（M08 §6.3.2）。"""

    def __init__(self, profiles: dict[str, Profile] | None = None, limits: dict[str, LimitsProfile] | None = None, *,
                 vehicles: Path | None = None) -> None:
        meta: dict[str, dict] = {}
        self.vdir = Path(vehicles) if vehicles is not None else vehicles_dir()
        if profiles is None:
            profiles, lims, meta = _load_cached(str(self.vdir), _stamp(self.vdir), _check_key())
            limits = {**lims, **(limits or {})}
        self.profiles = dict(profiles)
        self.limits = dict(limits or {})
        self.meta = dict(meta)
        self.catalog = self._catalog_safe()
        self.sessions: dict[str, dict] = {}  # 会话克隆：clone_id -> {base, set, display_name, extra_mass_kg, extra_power_w}
        self._clone_seq = 0
        for p in self.profiles.values():
            if p.default_limits not in self.limits:
                raise ProfileError(p.profile_id, [f"默认限速配置 {p.default_limits} 未定义"])
        self._rebuild()

    def _catalog_safe(self) -> dict | None:
        try:
            return read_catalog(self.vdir)
        except ProfileError:
            return None

    def _rebuild(self) -> None:
        """由 `profiles`、`limits` 重建下标与矩阵（写时复制：新数组整体替换属性，不改动旧数组的内容）。"""
        ids = list(self.profiles)
        limit_ids = list(self.limits)
        if len(ids) > 65535 or len(limit_ids) > 255:
            raise ProfileError("*", ["profile 或限速配置过多"])
        pr = [self.profiles[k] for k in ids]
        PT = np.zeros((len(pr), 10))
        for r, p in enumerate(pr):
            PT[r] = (p.mass_kg, p.t_max_n, p.hover, p.tau_motor_s, float(p.n_rot), p.omega_max_rad_s,
                     float(AERO_COMPOSITE if p.aero == "composite" else AERO_LINEAR), p.k_dv, p.cda_m2, p.c_rd)
        li = [self.limits[k] for k in limit_ids]
        LT = np.zeros((len(li), 6))
        for r, x in enumerate(li):
            LT[r] = (x.vxy_max_mps, x.cruise_mps, x.acc_hor_mps2, math.radians(x.yawrate_max_deg_s), x.z_up_mps, x.z_dn_mps)
        self.ids, self.limit_ids, self.PT, self.LT = ids, limit_ids, PT, LT
        self._index = {k: i for i, k in enumerate(ids)}
        self._lindex = {k: i for i, k in enumerate(limit_ids)}
        self.frame_kind = np.array([p.frame_kind for p in pr], np.uint8)
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

    # ------------------------------------------------------------ 引用解析与版本（M08-FR-206）
    def resolve(self, ref: str) -> Profile:
        """`<id>`、`<id>@<semver>` 或会话克隆 id → Profile；存档版本按需从 `history/` 加载并加入表（写时复制）。
        未知抛 KeyError（调用方映射为 520 MODEL_UNKNOWN 或 D1 的 110 UNKNOWN_PROFILE）。"""
        p = self.profiles.get(ref)
        if p is not None:
            return p
        m = REF_RE.match(str(ref))
        if m is None or m.group("ver") is None:
            raise KeyError(ref)
        pid, ver = m.group("id"), m.group("ver")
        cur = self.profiles.get(pid)
        if cur is None:
            raise KeyError(ref)
        if cur.version == ver:
            return cur
        f = Path(cur.source).parent / "history" / f"params-{ver}.yaml" if cur.source else None
        if f is None or not f.exists():
            raise KeyError(ref)
        doc = yaml_load(f.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ProfileError(ref, [f"{f}: not a mapping"])
        found = None
        for vid, d in _expand(doc):
            if vid == pid:
                found = d
        if found is None or str(found.get("profile_version")) != ver:
            raise ProfileError(ref, [f"{f}: id 或 profile_version 与引用 {ref} 不符"])
        checks_, derived, probs = validate_doc(found, pid=ref, vdir=f.parent.parent)
        if probs:
            raise ProfileError(ref, probs)
        new_limits = dict(self.limits)
        _merge_limits(new_limits, _limits_from_doc(found), ref)
        prof = profile_from_doc(found, str(f), ref=ref)
        self.profiles[ref] = prof
        self.limits = new_limits
        self.meta[ref] = {"checks": checks_, "derived": derived}
        self._rebuild()
        return prof

    def has(self, ref: str) -> bool:
        try:
            self.resolve(ref)
        except (KeyError, ProfileError):
            return False
        return True

    def key_of(self, ref: str) -> str:
        """引用的表键（当前版本的 `<id>@<当前版本>` 归一为 `<id>`）。"""
        return self.resolve(ref).ref

    def index(self, profile_id: str) -> int:
        try:
            key = self.key_of(profile_id)  # 先解析（可能按需加载存档并重建下标表），再取下标表
            return self._index[key]
        except (KeyError, ProfileError):
            raise KeyError(profile_id) from None

    def limits_index(self, name: str | None, profile_id: str) -> int:
        n = name or self.resolve(profile_id).default_limits
        try:
            return self._lindex[n]
        except KeyError:
            raise KeyError(n) from None

    def get(self, profile_id: str) -> Profile:
        try:
            return self.resolve(profile_id)
        except ProfileError:
            raise KeyError(profile_id) from None

    def frame_kind_of(self, profile_id: str) -> int:
        return int(self.resolve(profile_id).frame_kind)

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
        T = ProfileTable({**self.profiles, profile_id: new}, self.limits)
        T.meta = dict(self.meta)
        T.vdir = self.vdir
        T.catalog = self.catalog
        return T

    # ------------------------------------------------------------ 会话克隆（M08-FR-208；AWR-20 §7.6）
    def add_session(self, base_ref: str, overrides: dict | None = None, *, display_name: str | None = None,
                    extra_mass_kg: float = 0.0, extra_power_w: float = 0.0, quota: int = CLONE_SESSION_QUOTA) -> str:
        """新建会话克隆，返回克隆 id `<base_id>_c<n>`。`overrides` 为点路径 → 值（白名单与边界见 AWR-20 §7.6）；
        `extra_mass_kg`、`extra_power_w` 为挂载变化的质量与电功率（由 M21 按挂载合成，§5.5）。多旋翼按 `P ∝ m^1.5` 重算悬停功率
        与续航，电池容量变化按比能量不变同比改变电池质量与机体质量；限速覆盖生成限速档 `<clone_id>_std`。打补丁后重跑该构型
        的全部检查（CL-3）。失败抛 CloneError（522 逐项原因、505 配额、520 源机型未知）。调用方须在步边界执行。"""
        if len(self.sessions) >= min(int(quota), CLONE_MAX):
            raise CloneError(CODE_ENTITY_LIMIT, detail="CLONE_QUOTA")
        try:
            base = self.resolve(base_ref)
        except (KeyError, ProfileError):
            raise CloneError(CODE_MODEL_UNKNOWN, detail=str(base_ref)) from None
        if base.base_ref is not None:
            raise CloneError(CODE_CLONE_INVALID, [_prob("CL-1", "base", base_ref, None, "不能以会话克隆为源")])
        cid = f"{base.profile_id}_c{self._clone_seq + 1}"  # 序号只在成功时推进（会话内单调、不复用）
        doc, new_limits, probs = self._clone_doc(base, cid, dict(overrides or {}), display_name, float(extra_mass_kg),
                                                 float(extra_power_w))
        if not probs:
            checks_ = all_checks(doc, Path(base.source).parent if base.source else None)
            probs += [_prob("CL-3", c["id"], c["value"], c["range"], f"{c['id']} 检查不通过" + (f"：{c['msg']}" if c.get("msg")
                                                                                         else ""))
                      for c in checks_ if not c["pass"]]
        if probs:
            raise CloneError(CODE_CLONE_INVALID, probs)
        prof = profile_from_doc(doc, base.source, ref=cid)
        prof = replace(prof, base_ref=base.ref, model=base.model)
        lims = dict(self.limits)
        lims.update(new_limits)
        self.profiles[cid] = prof
        self.limits = lims
        ft = frame_type_of(doc)
        self.meta[cid] = {"checks": all_checks(doc, Path(base.source).parent if base.source else None),
                          "derived": derived_values(doc) if ft in MULTIROTOR_FRAMES or ft == "vtol_quadplane" else {}}
        self.sessions[cid] = {"base": f"{base.profile_id}@{base.version}", "set": dict(overrides or {}), "display_name": display_name,
                              "extra_mass_kg": float(extra_mass_kg), "extra_power_w": float(extra_power_w)}
        self._clone_seq += 1
        self._rebuild()
        return cid

    def remove_session(self, ref: str, *, in_use: Callable[[str], bool] | None = None) -> None:
        """删除会话克隆；仍被机体引用时抛 CloneError(105, detail MODEL_IN_USE)；不是克隆时 CloneError(520)。"""
        if ref not in self.sessions:
            raise CloneError(CODE_MODEL_UNKNOWN, detail=str(ref))
        if in_use is not None and in_use(ref):
            raise CloneError(105, detail="MODEL_IN_USE")
        self.sessions.pop(ref)
        self.profiles.pop(ref)
        self.meta.pop(ref, None)
        self.limits.pop(f"{ref}_std", None)  # 克隆生成的限速档只属于该克隆
        self._rebuild()

    def session_specs(self) -> list[dict]:
        """会话克隆的重建参数（checkpoint 与会话配置快照，按创建顺序）。"""
        return [{"id": cid, **copy.deepcopy(v)} for cid, v in self.sessions.items()]

    def clone_docs(self) -> list[dict]:
        """会话克隆的导出形状（`awr.vehicle_clone.v1`；会话配置快照的 `models[]`）。"""
        out = []
        for cid, v in self.sessions.items():
            d = {"schema": "awr.vehicle_clone.v1", "base": v["base"], "id": cid, "set": dict(v.get("set") or {})}
            if v.get("display_name"):
                d["display_name"] = v["display_name"]
            out.append(d)
        return out

    def restore_sessions(self, specs: list[dict]) -> list[str]:
        """按 `session_specs()` 重建克隆（id 不变）；返回未能重建的 id。"""
        bad = []
        for s in specs:
            cid = str(s.get("id"))
            if cid in self.sessions:
                continue
            m = re.match(r"^(.*)_c([0-9]+)$", cid)
            seq0 = self._clone_seq
            if m:
                self._clone_seq = int(m.group(2)) - 1
            try:
                got = self.add_session(s["base"], s.get("set") or {}, display_name=s.get("display_name"),
                                       extra_mass_kg=float(s.get("extra_mass_kg") or 0.0),
                                       extra_power_w=float(s.get("extra_power_w") or 0.0), quota=CLONE_MAX)
                if got != cid:
                    self.remove_session(got)
                    bad.append(cid)
            except CloneError:
                bad.append(cid)
            self._clone_seq = max(seq0, self._clone_seq)
        return bad

    def _clone_doc(self, base: Profile, cid: str, ov: dict, display_name: str | None, extra_m: float,
                   extra_p: float) -> tuple[dict, dict[str, LimitsProfile], list[dict]]:
        doc = copy.deepcopy(base.doc)
        cat = _CATEGORY.get(base.frame_kind, "multirotor")
        probs: list[dict] = []
        doc["id"] = cid
        if display_name is not None:
            dn = str(display_name)
            if not 1 <= len(dn) <= 40:
                probs.append(_prob("CL-2", "display_name", dn, [1, 40], "显示名长度须为 1–40 字符"))
            doc["display_name"] = dn
        m0 = float(_val(doc["mass_kg"]))
        mtow = _val(doc.get("mtow_kg"))
        bat = doc.get("battery") if isinstance(doc.get("battery"), dict) else None
        lim_over: dict[str, float] = {}
        new_mass: float | None = None

        def num(path: str, v: Any) -> float | None:
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
                probs.append(_prob("CL-2", path, v, None, "须为有限数值"))
                return None
            return float(v)

        for path, v in ov.items():
            if path == "mass_kg":
                x = num(path, v)
                if x is not None:
                    hi = float(mtow) if mtow is not None else 1.5 * m0
                    if not 0.7 * m0 <= x <= hi:
                        probs.append(_prob("CL-2", path, x, [round(0.7 * m0, 4), hi], "质量须在 [0.7·m_base, mtow_kg] 内"))
                    new_mass = x
            elif path == "battery.capacity_ah":
                x = num(path, v)
                if x is not None and bat is not None:
                    c0 = float(_val(bat["capacity_ah"]))
                    if not 0.5 * c0 <= x <= 1.5 * c0:
                        probs.append(_prob("CL-2", path, x, [0.5 * c0, 1.5 * c0], "电池容量须在 [0.5, 1.5]·base 内"))
                    else:
                        r = x / c0
                        _set_val(bat, "capacity_ah", x)
                        _set_val(bat, "capacity_wh", round(float(_val(bat["capacity_wh"])) * r, 4))
                        if bat.get("mass_kg") is not None:
                            bm0 = float(_val(bat["mass_kg"]))
                            _set_val(bat, "mass_kg", round(bm0 * r, 4))
                            extra_m += bm0 * (r - 1.0)
            elif path in ("limits.vxy_max_mps", "limits.cruise_mps", "limits.z_up_mps", "limits.z_dn_mps"):
                if cat not in _MR:
                    probs.append(_prob("CL-2", path, v, None, "该构型不可编辑多旋翼限速"))
                    continue
                x = num(path, v)
                if x is not None:
                    lim_over[path.split(".", 1)[1]] = x
            elif path in ("acoustic.lwa_hover_dba", "acoustic.lwa_cruise_dba"):
                x = num(path, v)
                ac = doc.get("acoustic") or {}
                b0 = ac.get(path.split(".", 1)[1])
                if x is not None:
                    if b0 is None:
                        probs.append(_prob("CL-2", path, x, None, "该构型没有此声学字段"))
                    elif abs(x - float(b0)) > 10.0 + 1e-9:
                        probs.append(_prob("CL-2", path, x, [float(b0) - 10, float(b0) + 10], "声功率须在 base ± 10 dB 内"))
                    else:
                        ac[path.split(".", 1)[1]] = x
                        doc["acoustic"] = ac
            elif path == "acoustic.tonal_db":
                x = num(path, v)
                if x is not None:
                    if not 0.0 <= x <= 20.0:
                        probs.append(_prob("CL-2", path, x, [0, 20], "tonal_db 须在 [0, 20] 内"))
                    else:
                        ac = doc.get("acoustic") or {}
                        ac["tonal_db"] = x
                        ac.pop("band_shape_db", None)
                        doc["acoustic"] = ac
            elif path == "visual.contrast_sky":
                x = num(path, v)
                if x is not None:
                    if not -1.0 <= x <= 1.0:
                        probs.append(_prob("CL-2", path, x, [-1, 1], "contrast_sky 须在 [-1, 1] 内"))
                    else:
                        doc.setdefault("visual", {})["contrast_sky"] = x
            elif path == "link.range_m":
                x = num(path, v)
                lk = doc.get("link") or {}
                if x is not None:
                    r0 = float(lk.get("range_m") or 0.0)
                    if r0 <= 0 or not 0.5 * r0 <= x <= 2.0 * r0:
                        probs.append(_prob("CL-2", path, x, [0.5 * r0, 2.0 * r0], "链路距离须在 [0.5, 2]·base 内"))
                    else:
                        lk["range_m"] = x
                        doc["link"] = lk
            elif path in ("airframe.v_cruise_mps", "airframe.v_loiter_mps", "airframe.phi_max_deg"):
                if cat not in _FW:
                    probs.append(_prob("CL-2", path, v, None, "该构型没有 airframe"))
                    continue
                x = num(path, v)
                af = doc.get("airframe") or {}
                if x is not None:
                    key = path.split(".", 1)[1]
                    if key == "phi_max_deg" and not 15.0 <= x <= 45.0:
                        probs.append(_prob("CL-2", path, x, [15, 45], "最大滚转须在 [15°, 45°] 内"))
                        continue
                    if key != "phi_max_deg":
                        lo, hi = float(_val(af.get("v_min_mps")) or 0.0), float(_val(af.get("v_max_mps")) or 1e9)
                        if not lo <= x <= hi:
                            probs.append(_prob("CL-2", path, x, [lo, hi], "空速须在 [v_min, v_max] 内"))
                            continue
                    af[key] = x
                    doc["airframe"] = af
            else:
                probs.append(_prob("CL-2", path, v, None, "不在克隆白名单内（AWR-20 §7.6）"))
        if probs:
            return doc, {}, probs
        # 质量与能量联动（§5.5）：多旋翼与 VTOL 的悬停功率 P = P0·(m'/m0)^1.5 + P_extra，续航随之重算
        m1 = (new_mass if new_mass is not None else m0) + extra_m
        if abs(m1 - m0) > 1e-12 or extra_p:
            _set_val(doc, "mass_kg", round(m1, 6))
        if bat is not None and bat.get("p_hover_w") is not None and cat in _MR:
            p0 = float(_val(bat["p_hover_w"]))
            p1 = p0 * (m1 / m0) ** 1.5 + float(extra_p)
            _set_val(bat, "p_hover_w", round(p1, 4))
            if bat.get("hover_endurance_s") is not None:
                e = float(_val(bat["usable_frac"])) * float(_val(bat["capacity_wh"])) * 3600.0 / p1
                _set_val(bat, "hover_endurance_s", round(e, 3))
        ft = frame_type_of(doc)
        if ft in MULTIROTOR_FRAMES or ft == "vtol_quadplane":
            dv = derived_values(doc)
            der = dict(doc.get("derived") or {})
            for k in ("hover_thrust", "twr", "tilt_at_8mps_deg", "cda_per_mass_m2kg", "endurance_check_s"):
                if dv.get(k) is not None:
                    der[k] = round(float(dv[k]), 6)
            doc["derived"] = der
        new_limits: dict[str, LimitsProfile] = {}
        if lim_over:
            src_name = base.sandbox_limits
            b = self.limits.get(src_name)
            if b is None:
                return doc, {}, [_prob("CL-1", "limits", src_name, None, "源机型的缺省限速档未定义")]
            vmax = lim_over.get("vxy_max_mps", b.vxy_max_mps)
            cr = lim_over.get("cruise_mps", min(b.cruise_mps, vmax))
            zu = lim_over.get("z_up_mps", b.z_up_mps)
            zd = lim_over.get("z_dn_mps", b.z_dn_mps)
            if not cr <= vmax <= min(25.0, 1.25 * b.vxy_max_mps):
                probs.append(_prob("CL-2", "limits.vxy_max_mps", vmax, [cr, min(25.0, 1.25 * b.vxy_max_mps)],
                                   "最大水平速度须在 [cruise, min(25, 1.25·base)] 内"))
            if not 1.0 <= cr <= vmax:
                probs.append(_prob("CL-2", "limits.cruise_mps", cr, [1.0, vmax], "巡航速度须在 [1, vxy_max] 内"))
            if not 0.5 <= zu <= b.z_up_mps:
                probs.append(_prob("CL-2", "limits.z_up_mps", zu, [0.5, b.z_up_mps], "上升限速须在 [0.5, base] 内"))
            if not 0.5 <= zd <= b.z_dn_mps:
                probs.append(_prob("CL-2", "limits.z_dn_mps", zd, [0.5, b.z_dn_mps], "下降限速须在 [0.5, base] 内"))
            name = f"{cid}_std"
            new_limits[name] = replace(b, name=name, vxy_max_mps=vmax, cruise_mps=cr, z_up_mps=zu, z_dn_mps=zd,
                                       src=f"会话克隆 {cid}（源 {src_name}）")
            pp = {"MPC_XY_VEL_MAX": vmax, "MPC_XY_CRUISE": cr, "MPC_ACC_HOR": b.acc_hor_mps2,
                  "MC_YAWRATE_MAX": b.yawrate_max_deg_s, "MPC_Z_VEL_MAX_UP": zu, "MPC_Z_VEL_MAX_DN": zd}
            doc.setdefault("limits_profiles", {})[name] = {"px4_params": pp, "src": new_limits[name].src}
            c = dict(doc.get("catalog") or {"reference": False, "category": cat, "limits_default": name})
            c["limits_default"] = name
            doc["catalog"] = c
        return doc, new_limits, probs

    # ------------------------------------------------------------ REST 数据（GET /api/fleet/profiles[/{id}]）
    def summary(self) -> list[dict[str, Any]]:
        out = []
        hidden = self.hidden_ids()
        for pid in self.ids:
            p = self.profiles[pid]
            if p.base_ref is not None or "@" in pid:
                continue  # 会话克隆与存档版本不出现在机型列表
            row = {"profile_id": pid, "model": p.model, "display_name": p.display_name, "mass_kg": p.mass_kg,
                   "twr": round(p.twr, 3), "cda_m2": p.cda_m2 or None, "vmax_mps": self.limits[p.default_limits].vxy_max_mps,
                   "status": p.status, "speed_profiles": list(p.limits), "default_speed_profile": p.default_limits}
            if p.frame_kind or p.catalog or pid in hidden:
                row.update({"frame_kind": p.frame_kind, "version": p.version, "hidden": pid in hidden,
                            "reference": bool((p.catalog or {}).get("reference", False))})
            out.append(row)
        return out

    def hidden_ids(self) -> set[str]:
        """目录中 `visible = false` 或 `hidden = true` 的机型（x500、x500_sih）：可按 id 加载，不出现在目录接口。"""
        cat = self.catalog or {}
        out = set()
        for m in cat.get("models") or []:
            if isinstance(m, dict) and (m.get("visible") is False or m.get("hidden") is True):
                out.add(str(m.get("id")))
        return out

    def describe(self, profile_id: str) -> dict[str, Any]:
        p = self.resolve(profile_id)
        doc = p.doc
        m = self.meta.get(p.ref) or {}
        checks = m.get("checks") or all_checks(doc, Path(p.source).parent if p.source else None)
        derived = m.get("derived")
        if derived is None:
            derived = derived_values(doc) if p.frame_type in MULTIROTOR_FRAMES or p.frame_type == "vtol_quadplane" else {}
        sensors: dict[str, Any] = {}
        for fn in _SENSOR_DESCRIBER:
            try:
                sensors.update(fn(p.base_ref or p.profile_id, doc) or {})
            except Exception:  # 传感器描述失败不影响机型参数
                continue
        out = {"profile": json.loads(json.dumps(doc, default=str)), "derived": dict(derived), "checks": checks,
               "twin": twin_table(doc, Path(p.source).parent if p.source else None), "sensors": sensors}
        if p.frame_kind or p.catalog or p.base_ref is not None or "@" in p.ref:
            out.update({"ref": p.ref, "version": p.version, "frame_type": p.frame_type, "frame_kind": p.frame_kind,
                        "catalog": p.catalog, "base": p.base_ref, "sandbox_limits": p.sandbox_limits})
        return out


def _set_val(d: dict, key: str, v: Any) -> None:
    """值对象 `{value, conf, src}` 的就地改值（标为 D 级、来源改为会话克隆）；纯数值直接替换。"""
    cur = d.get(key)
    if isinstance(cur, dict) and "value" in cur:
        d[key] = {**cur, "value": v, "conf": "D", "src": "会话克隆（派生）"}
    else:
        d[key] = v


_CONF_ORDER = "ABCDE"


def _worst(*confs: str | None) -> str | None:
    cs = [c for c in confs if c]
    return max(cs, key=_CONF_ORDER.index) if cs else None


def _conf(doc: dict, *paths: str) -> str | None:
    return _worst(*((_get(doc, p) or {}).get("conf") if isinstance(_get(doc, p), dict) else None for p in paths))


def _sensor_conf(vdir: Path | None, rel: str | dict | None) -> tuple[str | None, str]:
    rel = rel.get("file") if isinstance(rel, dict) else rel  # D2：sensors 映射的对象形式（AWR-20 §7.4）
    if not rel or vdir is None:
        return None, "missing"
    f = vdir / rel
    if not f.exists():
        return None, "missing"
    try:
        d = yaml_load(f.read_text(encoding="utf-8")) or {}
        return (d.get("conf") if isinstance(d, dict) else None), "present"
    except Exception:
        return None, "invalid"


def twin_table(doc: dict, vdir: Path | None) -> list[dict[str, Any]]:
    """ADR-043 的 11 个组成部分（M08 §6.7.2；UI 单机详情"孪生"标签）。"""
    placeholder = doc.get("status") == "placeholder"
    st = "placeholder" if placeholder else ("regression" if doc.get("status") == "regression" else "identified")
    sensors = {k: (v.get("file") if isinstance(v, dict) else v) for k, v in (doc.get("sensors") or {}).items()}
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
