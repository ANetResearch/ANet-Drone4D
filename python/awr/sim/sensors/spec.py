"""SensorSpec 装配（M13-FR-001、FR-002；M13 §6.4.1；AWR-16 §11.6；ADR-043）。

`vehicles/<model>/sensors/*.yaml` 经 contracts 的 schema（`vehicle/{camera,thermal,gnss,imu,lidar_livox}.schema.json`，
jsonschema 2020-12）校验后装成冻结的 `SensorSpec`；另做 schema 表达不了的语义检查（云台限位倒置、主点越界、RTK 固定解
sigma 一致）。任一失败抛 `SensorSpecError`（`110 PARAM_OUT_OF_RANGE`，detail = SENSOR_SPEC_INVALID），机型被拒绝。

传感器组（rig）：机型 `params.yaml` 的 `sensors:` 映射给出名称与文件；`sensor_no` 为规范序号——有视场的传感器
（camera、thermal、lidar）在前、按映射顺序，其余（gnss、imu）在后——对同一机型恒定，因此在机体生命周期内稳定
（剧本 `vehicles[].sensors` 只挑选子集，不重新编号）。roster `sensors[]` 只列有视场的传感器（fleet_roster schema 的 kind
枚举为 camera、lidar、thermal、radar）。

只依赖 numpy、yaml、jsonschema；无副作用（缓存按文件路径与修改时间）。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .enums import KIND_BIT, SensorKind
from .frames import mount_R

__all__ = [
    "CODE_SPEC_INVALID",
    "DetectorSpec",
    "GimbalSpec",
    "Intrinsics",
    "Rig",
    "SensorSpec",
    "SensorSpecError",
    "load_rig",
    "load_sensor_file",
    "rig_entries",
    "rig_for_model",
    "vehicles_dir",
]

CODE_SPEC_INVALID = 110
ROOT = Path(__file__).resolve().parents[4]

SCHEMA_KIND = {
    "awr.sensor.camera.v1": (SensorKind.CAMERA, "vehicle/camera.schema.json"),
    "awr.sensor.thermal.v1": (SensorKind.THERMAL, "vehicle/thermal.schema.json"),
    "awr.sensor.gnss.v1": (SensorKind.GNSS, "vehicle/gnss.schema.json"),
    "awr.sensor.imu.v1": (SensorKind.IMU, "vehicle/imu.schema.json"),
    "awr.sensor.lidar_livox.v1": (SensorKind.LIDAR, "vehicle/lidar_livox.schema.json"),
}
FOV_KINDS = (SensorKind.CAMERA, SensorKind.THERMAL, SensorKind.LIDAR, SensorKind.RADAR)
DEFAULT_RANGE_M = {SensorKind.CAMERA: 300.0, SensorKind.THERMAL: 400.0, SensorKind.LIDAR: 70.0}
# 缺省云台（M13 §6.5.2：俯仰 [−90°, +30°]、方位 [−150°, +150°]、90°/s，conf D）
GIMBAL_DEFAULTS = {"rate_max_deg_s": 90.0, "default_mode": "fixed", "default_yaw_deg": 0.0, "default_pitch_deg": -15.0}


class SensorSpecError(ValueError):
    """传感器文件无效（110 PARAM_OUT_OF_RANGE，detail = SENSOR_SPEC_INVALID；M13 §7.5）。"""

    code = CODE_SPEC_INVALID
    detail = "SENSOR_SPEC_INVALID"

    def __init__(self, where: str, problems: list[str]) -> None:
        self.where = where
        self.problems = list(problems)
        super().__init__(f"{where}: " + "; ".join(problems))

    def to_json(self) -> dict:
        return {"code": self.code, "detail": self.detail, "file": self.where, "problems": self.problems}


@dataclass(frozen=True)
class Intrinsics:
    w: int
    h: int
    fx: float
    fy: float
    cx: float
    cy: float
    dist: tuple[float, ...] = ()


@dataclass(frozen=True)
class GimbalSpec:
    az_min_rad: float
    az_max_rad: float
    el_min_rad: float
    el_max_rad: float
    rate_max_rad_s: float
    default_mode: str
    default_az_rad: float
    default_el_rad: float


@dataclass(frozen=True)
class DetectorSpec:
    capability: str
    p0: float
    r_fp_m: float
    t_look_s: float = 1.0
    pos_sigma_m: float = 0.0
    conf_model: str = "scenario_or_default"


@dataclass(frozen=True)
class SensorSpec:
    name: str
    kind: SensorKind
    sensor_no: int
    mount_R: np.ndarray
    mount_t: np.ndarray
    intr: Intrinsics | None
    hfov_rad: float
    vfov_rad: float
    range_m: float
    gimbal: GimbalSpec | None
    detector: DetectorSpec | None
    noise: dict = field(default_factory=dict)
    rate_hz: float = float("nan")
    conf: str = "D"
    src: str = ""
    file: str = ""
    doc: dict = field(default_factory=dict)

    @property
    def bit(self) -> int:
        return KIND_BIT[self.kind]

    @property
    def has_fov(self) -> bool:
        return self.kind in FOV_KINDS


@dataclass(frozen=True)
class Rig:
    """一个机型的全部传感器（按 sensor_no 排序）。"""

    model: str
    specs: tuple[SensorSpec, ...]

    def by_name(self, name: str) -> SensorSpec | None:
        for s in self.specs:
            if s.name == name:
                return s
        return None

    def by_kind(self, kind: SensorKind) -> SensorSpec | None:
        for s in self.specs:
            if s.kind == kind:
                return s
        return None

    @property
    def has_bits(self) -> int:
        b = 0
        for s in self.specs:
            b |= s.bit
        if (imu := self.by_kind(SensorKind.IMU)) is not None and isinstance(imu.doc.get("baro"), dict):
            b |= KIND_BIT[SensorKind.BARO]
        return b

    def fov_specs(self) -> tuple[SensorSpec, ...]:
        return tuple(s for s in self.specs if s.has_fov)


def vehicles_dir() -> Path:
    return Path(os.environ.get("AWR_VEHICLES_DIR") or (ROOT / "vehicles"))


# ---------------------------------------------------------------- schema 校验
def _registry():
    from awr.sim.schemas import contracts_registry  # 与机型、剧本校验共用一次扫描（ADR-061）

    return contracts_registry()


@cache
def _validator(rel: str):
    from jsonschema import Draft202012Validator

    from awr.contracts._paths import contracts_root

    schema = json.loads((contracts_root() / rel).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=_registry())


def schema_problems(doc: dict) -> list[str]:
    sid = doc.get("schema") if isinstance(doc, dict) else None
    if sid not in SCHEMA_KIND:
        return [f"schema: unknown sensor schema {sid!r}"]
    v = _validator(SCHEMA_KIND[sid][1])
    return [f"schema {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:160]}" for e in v.iter_errors(doc)]


def semantic_problems(doc: dict) -> list[str]:
    """schema 之外的检查（M13-AC-001）：云台限位倒置、主点越界、速率非正、RTK 固定解 sigma 一致。"""
    out: list[str] = []
    g = doc.get("gimbal")
    if isinstance(g, dict):
        if not float(g["pitch_min_deg"]) < float(g["pitch_max_deg"]):
            out.append("gimbal: pitch_min_deg must be < pitch_max_deg (GIMBAL_LIMITS_INVERTED)")
        if not float(g["yaw_min_deg"]) < float(g["yaw_max_deg"]):
            out.append("gimbal: yaw_min_deg must be < yaw_max_deg (GIMBAL_LIMITS_INVERTED)")
        for k in ("default_pitch_deg", "default_yaw_deg"):
            lo, hi = ("pitch_min_deg", "pitch_max_deg") if "pitch" in k else ("yaw_min_deg", "yaw_max_deg")
            if k in g and not float(g[lo]) <= float(g[k]) <= float(g[hi]):
                out.append(f"gimbal: {k} outside [{lo}, {hi}]")
    if "cx" in doc and "width" in doc and not 0.0 <= float(doc["cx"]) <= float(doc["width"]):
        out.append("intrinsics: cx outside [0, width]")
    if "cy" in doc and "height" in doc and not 0.0 <= float(doc["cy"]) <= float(doc["height"]):
        out.append("intrinsics: cy outside [0, height]")
    if doc.get("schema") == "awr.sensor.gnss.v1":
        rtk = doc.get("rtk") or {}
        fx = rtk.get("fixed") or {}
        out.extend(f"rtk.fixed.{k} must equal rtk.{k}" for k in ("sigma_h_m", "sigma_v_m")
                   if k in fx and k in rtk and float(fx[k]) != float(rtk[k]))
        if rtk.get("enabled") and ("sigma_h_m" not in rtk or "sigma_v_m" not in rtk):
            out.append("rtk: sigma_h_m and sigma_v_m are required when enabled")
    return out


# ---------------------------------------------------------------- 装配
def _fov(w: float, h: float, fx: float, fy: float, cx: float, cy: float) -> tuple[float, float]:
    """非对称主点时取两侧之和（M13 §6.4.1）。"""
    return (math.atan(cx / fx) + math.atan((w - cx) / fx), math.atan(cy / fy) + math.atan((h - cy) / fy))


def _mount(doc: dict) -> tuple[np.ndarray, np.ndarray]:
    m = doc.get("mount") or {}
    t = np.asarray(m.get("xyz_m") or m.get("t") or [0.0, 0.0, 0.0], np.float64)
    if "q_xyzw" in m:
        from .frames import quat_to_R

        q = np.asarray(m["q_xyzw"], np.float64)
        R = quat_to_R((q / np.linalg.norm(q))[None])[0]
    else:
        R = mount_R(m.get("rpy_deg") or [0.0, 0.0, 0.0])
    R.flags.writeable = False
    t.flags.writeable = False
    return R, t


def _gimbal(doc: dict) -> GimbalSpec | None:
    g = doc.get("gimbal")
    if not isinstance(g, dict):
        return None
    d = {**GIMBAL_DEFAULTS, **g}
    r = math.radians
    return GimbalSpec(r(float(d["yaw_min_deg"])), r(float(d["yaw_max_deg"])), r(float(d["pitch_min_deg"])),
                      r(float(d["pitch_max_deg"])), r(float(d["rate_max_deg_s"])), str(d["default_mode"]),
                      r(float(d["default_yaw_deg"])), r(float(d["default_pitch_deg"])))


def _detector(doc: dict) -> DetectorSpec | None:
    d = doc.get("detector")
    if not isinstance(d, dict):
        return None
    return DetectorSpec(str(d["capability"]), float(d["p0"]), float(d["r_fp_m"]), float(d.get("t_look_s", 1.0)),
                        float(d.get("pos_sigma_m", 0.0)))


def build_spec(name: str, doc: dict, sensor_no: int, file: str = "") -> SensorSpec:
    problems = schema_problems(doc)
    if not problems:
        problems = semantic_problems(doc)
    if problems:
        raise SensorSpecError(file or name, problems)
    kind = SCHEMA_KIND[doc["schema"]][0]
    R, t = _mount(doc)
    intr = None
    hf = vf = float("nan")
    if kind in (SensorKind.CAMERA, SensorKind.THERMAL):
        k = doc.get("distortion", {}).get("k") or []
        intr = Intrinsics(int(doc["width"]), int(doc["height"]), float(doc["fx"]), float(doc["fy"]), float(doc["cx"]),
                          float(doc["cy"]), tuple(float(x) for x in k))
        hf, vf = _fov(intr.w, intr.h, intr.fx, intr.fy, intr.cx, intr.cy)
    elif kind == SensorKind.LIDAR:
        fov = doc["fov"]
        hf = math.radians(float(fov["h_deg"]))
        vf = math.radians(float(fov["v_max_deg"]) - float(fov["v_min_deg"]))
    rng = doc.get("range_m")
    if kind == SensorKind.LIDAR:
        rng = (doc.get("range") or {}).get("hard_max_m", rng)
    range_m = float(rng) if rng is not None else DEFAULT_RANGE_M.get(kind, float("nan"))
    rate = doc.get("rate_hz", doc.get("frame_rate_hz"))
    noise: dict[str, Any] = {}
    if kind == SensorKind.GNSS:
        noise = {k: doc.get(k) for k in ("gauss_markov", "dgps", "rtk", "timing", "sats", "hdop", "fix_type", "warm_start")}
        R = np.eye(3)
        R.flags.writeable = False
        t = np.asarray((doc.get("antenna") or {}).get("xyz_m") or [0.0, 0.0, 0.0], np.float64)
        t.flags.writeable = False
    elif kind == SensorKind.IMU:
        noise = {k: doc.get(k) for k in ("gyro", "accel", "baro")}
    elif kind == SensorKind.LIDAR:
        noise = dict(doc.get("noise") or {})
    elif kind == SensorKind.THERMAL:
        noise = {"netd_k": float(doc["netd_k"])}
    return SensorSpec(name=name, kind=kind, sensor_no=int(sensor_no), mount_R=R, mount_t=t, intr=intr, hfov_rad=hf,
                      vfov_rad=vf, range_m=range_m, gimbal=_gimbal(doc), detector=_detector(doc), noise=noise,
                      rate_hz=float(rate) if rate is not None else float("nan"), conf=str(doc.get("conf", "D")),
                      src=str(doc.get("src", "")), file=file, doc=doc)


def _yaml_load(text: str):
    """`yaml.safe_load` 的等价物（有 libyaml 时用 CSafeLoader；sim-core 启动路径，FX-SIM1）。"""
    return yaml.load(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))


def load_sensor_file(path: Path | str, name: str | None = None, sensor_no: int = 0) -> SensorSpec:
    p = Path(path)
    try:
        doc = _yaml_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise SensorSpecError(str(p), [f"read: {e}"]) from e
    if not isinstance(doc, dict):
        raise SensorSpecError(str(p), ["document is not a mapping"])
    return build_spec(name or p.stem, doc, sensor_no, str(p))


def _order(entries: list[tuple[str, dict, str]]) -> list[tuple[str, dict, str]]:
    fov = [e for e in entries if SCHEMA_KIND.get(e[1].get("schema"), (None,))[0] in FOV_KINDS]
    rest = [e for e in entries if e not in fov]
    return fov + rest


def load_rig(model_dir: Path | str, sensors_map: dict[str, str] | None = None) -> Rig:
    """读取一个机型目录下的传感器组；`sensors_map` 缺省取 `params.yaml` 的 `sensors:`（无该键时为空组）。"""
    d = Path(model_dir)
    if sensors_map is None:
        try:
            params = _yaml_load((d / "params.yaml").read_text(encoding="utf-8")) or {}
        except OSError:
            params = {}
        sensors_map = dict(params.get("sensors") or {})
    raw: list[tuple[str, dict, str]] = []
    for name, rel in sensors_map.items():
        p = d / str(rel)
        try:
            doc = _yaml_load(p.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            raise SensorSpecError(str(p), [f"read: {e}"]) from e
        if not isinstance(doc, dict):
            raise SensorSpecError(str(p), ["document is not a mapping"])
        raw.append((str(name), doc, str(p)))
    specs = tuple(build_spec(n, doc, i, f) for i, (n, doc, f) in enumerate(_order(raw)))
    return Rig(d.name, specs)


_RIGS: dict[tuple[str, float], Rig] = {}


def rig_for_model(model: str, base: Path | None = None) -> Rig:
    """按机型目录名（roster `model`，例如 p600、x500）取传感器组；按 params.yaml 修改时间缓存。"""
    d = (base or vehicles_dir()) / model
    try:
        mt = (d / "params.yaml").stat().st_mtime
    except OSError:
        return Rig(model, ())
    key = (str(d), mt)
    r = _RIGS.get(key)
    if r is None:
        r = _RIGS[key] = load_rig(d)
    return r


def rig_entries(rig: Rig, names: list[str] | None = None) -> list[dict]:
    """roster `sensors[]`（M13-FR-002）：只列有视场的传感器；`names` 为剧本 `vehicles[].sensors` 子集（缺省全部）。"""
    kind_name = {SensorKind.CAMERA: "camera", SensorKind.LIDAR: "lidar", SensorKind.THERMAL: "thermal",
                 SensorKind.RADAR: "radar"}
    out = []
    for s in rig.specs:
        if not s.has_fov or (names is not None and s.name not in names):
            continue
        out.append({"sensor_no": s.sensor_no, "name": s.name, "kind": kind_name[s.kind]})
    return out
