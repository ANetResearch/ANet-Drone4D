"""Realistic 档机型映射（M24-FR-030、FR-083；AWR-20 §15.16.1）：在 `x500_d4d` 机体几何上替换质量、惯量与电机最大转速，得到
`d4d_quad` 模板参数与派生 gz 模型。

- `p600_bench`（排行榜标准轨机型，`vehicles/d3/variants/p600_bench.yaml`）：整机质量 3.935 kg（变体派生值，AWR-20 §15.16.1）；
  惯量按 p600 的同一缩放规则 J_x500 × (m / 2.064) × (0.300 / 0.246)²（`vehicles/p600/params.yaml` inertia_kgm2 的 src）；最大
  转速 1500 rad/s（`vehicles/p600/params.yaml` motor.omega_max_rad_s），k_f 不变（推重比 1.99，VH-1）。质量与最大转速与目录
  一致由 `tests/d3/phys/test_gz_vehicles.py` 核对（Fast 档按变体的基机型取 FleetSim 模板，两档参数同源）。
- 其余机型引用（含 `x500`）使用 gz 镜像内的 `x500_d4d` 原型，不改写 run.sdf（与映射接入前的 Realistic 档相同）。

派生模型写入 `<models_root>/<模型名>/`（model.sdf、model.config），由 gz 实例以 `/opt/d4d/models/<模型名>` 只读挂载（docker）或经
`GZ_SIM_RESOURCE_PATH` 解析（本机进程）；`apply_vehicle` 把 compose_run 的 `model://x500_d4d` 引用与 4 × 机队规模个电机的
`maxRotVelocity` 改为映射值，替换次数不符时报错（防止模板变化后静默失效）。原实现在 `tools/bench/d3/gz_lockstep.py`（F-09），
该脚本改为从本模块导入。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["P600_BENCH_MASS_KG", "P600_OMEGA_MAX", "P600_WHEELBASE_M", "VEHICLES", "X500_BASE", "X500_OMEGA_MAX", "X500_ROTOR_KG", "X500_SIH",
           "X500_WHEELBASE_M", "QuadParams",
           "apply_vehicle", "base_model_sdf", "derive_model_sdf", "model_config", "p600_bench_params", "quad_for", "quad_for_fleet",
           "write_model", "x500_params"]

G = 9.80665
REPO = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class QuadParams:
    """`d4d_quad` 模板参数（M24-FR-030、FR-083）：在 x500_d4d 机体几何上替换质量、惯量与电机最大转速。

    `mass_kg` 为整机质量（含 4 个桨叶连杆，每个 X500_ROTOR_KG）；惯量为 base_link 惯量（x500 的约定：base_link 惯量即 SIH 整机惯量）。"""

    name: str
    model: str                                   # gz 模型名（model://<model>）
    mass_kg: float
    inertia_kgm2: tuple[float, float, float]     # ixx, iyy, izz
    omega_max_rad_s: float
    k_f: float = 8.54858e-06
    src: str = ""

    @property
    def base_link_kg(self) -> float:
        return self.mass_kg - 4 * X500_ROTOR_KG

    @property
    def thrust_to_weight(self) -> float:
        return 4.0 * self.k_f * self.omega_max_rad_s ** 2 / (self.mass_kg * G)

    @property
    def mapped(self) -> bool:
        """是否需要派生模型与改写 run.sdf（x500 原型为 False）。"""
        return not (self.model == X500_BASE and self.omega_max_rad_s == X500_OMEGA_MAX)

    def as_dict(self) -> dict:
        return {"name": self.name, "model": self.model, "mass_kg": self.mass_kg, "base_link_kg": round(self.base_link_kg, 9),
                "inertia_kgm2": list(self.inertia_kgm2), "omega_max_rad_s": self.omega_max_rad_s, "k_f": self.k_f,
                "thrust_to_weight": round(self.thrust_to_weight, 4), "src": self.src}


X500_ROTOR_KG = 0.016076923076923075                    # native/gz/models/x500_d4d/model.sdf 的 rotor_i 连杆质量
X500_BASE = "x500_d4d"
X500_OMEGA_MAX = 1000.0                                 # compose_run 的 MulticopterMotorModel maxRotVelocity
X500_SIH = {"mass_kg": 2.064, "inertia_kgm2": (0.0217, 0.0217, 0.040)}   # vehicles/x500/params.yaml（SIH，置信度 A）
P600_WHEELBASE_M, X500_WHEELBASE_M = 0.300, 0.246       # 半轴距缩放（ADR-022；AWR-20 §3.3）
P600_BENCH_MASS_KG = 3.935                              # AWR-20 §15.16.1：p600_mid360 3.825 kg + lwir640 0.09 kg + 支架 0.02 kg
P600_OMEGA_MAX = 1500.0                                 # vehicles/p600/params.yaml motor.omega_max_rad_s（t_max 19.23 N）


def p600_bench_params() -> QuadParams:
    """`p600_bench` → d4d_quad：质量 3.935 kg；惯量按 p600 的同一缩放规则 J_x500 × (m / 2.064) × (0.300 / 0.246)²（vehicles/p600/params.yaml
    inertia_kgm2 的 src）；最大转速 1500 rad/s、k_f 不变（推重比 1.99，AWR-20 §15.16.1 VH-1）。"""
    s = (P600_BENCH_MASS_KG / X500_SIH["mass_kg"]) * (P600_WHEELBASE_M / X500_WHEELBASE_M) ** 2
    j = tuple(round(v * s, 6) for v in X500_SIH["inertia_kgm2"])
    return QuadParams("p600_bench", "x500_d4d_p600_bench", P600_BENCH_MASS_KG, j, P600_OMEGA_MAX,
                      src="AWR-20 §15.16.1 mass; inertia J_x500 x (m/2.064) x (0.300/0.246)^2; omega_max from vehicles/p600/params.yaml")


def x500_params() -> QuadParams:
    return QuadParams("x500", X500_BASE, 2.0 + 4 * X500_ROTOR_KG, (0.02166666666666667, 0.02166666666666667, 0.04000000000000001),
                      X500_OMEGA_MAX, src="native/gz/models/x500_d4d (unchanged)")


VEHICLES: dict[str, Callable[[], QuadParams]] = {"p600_bench": p600_bench_params, "x500": x500_params}


def quad_for(vehicle_ref: str) -> QuadParams:
    """机型引用（`<id>` 或 `<id>@<版本>`）→ 映射参数；未登记映射的机型使用 x500 原型。"""
    return VEHICLES.get(str(vehicle_ref).split("@", 1)[0], x500_params)()


def quad_for_fleet(refs: Iterable[str]) -> QuadParams:
    """机队的映射参数：run.sdf 的机队共用一个机体模型，机型引用映射到不同参数时报错。"""
    qs = {q.name: q for q in (quad_for(r) for r in refs)}
    if len(qs) != 1:
        raise ValueError(f"realistic tier needs one vehicle mapping per run, got {sorted(qs)}")
    return next(iter(qs.values()))


_INERTIAL = re.compile(r'(<link name="base_link">\s*<inertial>\s*<mass>)([^<]+)(</mass>\s*<inertia>)(.*?)(</inertia>)', re.S)


def derive_model_sdf(base_sdf: str, q: QuadParams) -> str:
    """x500_d4d/model.sdf → 派生模型：模型名改为 `q.model`，base_link 质量与对角惯量替换，其余（碰撞、桨叶、关节）不变。"""
    m = _INERTIAL.search(base_sdf)
    if m is None:
        raise ValueError("base_link inertial block not found in the x500_d4d model")
    ixx, iyy, izz = (float(v) for v in q.inertia_kgm2)
    inertia = (f"\n          <ixx>{ixx!r}</ixx>\n          <ixy>0</ixy>\n          <ixz>0</ixz>\n          <iyy>{iyy!r}</iyy>\n"
               f"          <iyz>0</iyz>\n          <izz>{izz!r}</izz>\n        ")
    out = base_sdf[:m.start()] + m.group(1) + repr(float(q.base_link_kg)) + m.group(3) + inertia + m.group(5) + base_sdf[m.end():]
    out, n = re.subn(r"<model name='x500_d4d'>", f"<model name='{q.model}'>", out, count=1)
    if n != 1:
        raise ValueError("model element not found in the x500_d4d model")
    return out


def model_config(q: QuadParams) -> str:
    return (f'<?xml version="1.0"?>\n<model>\n  <name>{q.model}</name>\n  <version>1.0</version>\n  <sdf version="1.9">model.sdf</sdf>\n'
            f"  <description>x500_d4d airframe with {q.name} mass, inertia and maximum rotor speed (awr.phys.gz.vehicles)."
            "</description>\n</model>\n")


def apply_vehicle(run_sdf: str, q: QuadParams, n_uav: int) -> str:
    """compose_run 的 run.sdf → 引用派生模型并替换电机最大转速；替换次数与机队规模不符时报错（防止模板变化后静默失效）。"""
    if not q.mapped:
        return run_sdf
    out, n = re.subn(rf"<uri>model://{X500_BASE}</uri>", f"<uri>model://{q.model}</uri>", run_sdf)
    if n != n_uav:
        raise ValueError(f"expected {n_uav} x500_d4d includes, found {n}")
    out, n = re.subn(r"<maxRotVelocity>[^<]+</maxRotVelocity>", f"<maxRotVelocity>{q.omega_max_rad_s!r}</maxRotVelocity>", out)
    if n != 4 * n_uav:
        raise ValueError(f"expected {4 * n_uav} motor models, found {n}")
    return out


def write_model(models_root: Path, q: QuadParams, base_sdf: str) -> Path | None:
    """派生模型写入 `<models_root>/<q.model>/`；x500 原型返回 None（用镜像内模型）。"""
    if q.model == X500_BASE:
        return None
    d = Path(models_root) / q.model
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.sdf").write_text(derive_model_sdf(base_sdf, q), encoding="utf-8")
    (d / "model.config").write_text(model_config(q), encoding="utf-8")
    return d


def base_model_sdf() -> str:
    """仓库 `native/gz/models/x500_d4d/model.sdf`；不在时按 `GZ_SIM_RESOURCE_PATH` 查找（gz 镜像内的模型目录）。"""
    p = REPO / "native" / "gz" / "models" / X500_BASE / "model.sdf"
    if not p.is_file():
        for r in os.environ.get("GZ_SIM_RESOURCE_PATH", "").split(":"):
            if r and (Path(r) / X500_BASE / "model.sdf").is_file():
                p = Path(r) / X500_BASE / "model.sdf"
                break
    return p.read_text(encoding="utf-8")
