"""field 风模式（M24 §6.5.8、M24-FR-046；AWR-05 §5.3.1 第 3 条；D3-AC-35 档间对照运行）的宿主侧：M07 逐机采样与
`Drone4DWindDrag`（native/gz/plugins/wind）配置。

uniform 模式（缺省）由 `Drone4DClock` 把平均风写入 WindEffects 的风实体，属于全场均匀风加线性阻力，与 M07 冻结 von Kármán 场及
FleetSim `composite` 力律不同。field 模式不建风实体，改为：

- 采样（`FieldWind`）：每个 tick 以 tick 起点状态 (p_k, v_k)，在本 tick 内每个 20 ms 环境网格点 t_g（M07 env stage 的
  `every 5, phase 0`）预测机体位置 `p_k + v_k·(t_g − t_k)`，减去机体原点到 FleetSim 参考点的高度（`dz_m = −GEAR_Z_M`，Fast 档
  env stage 在参考点采样），按 env stage 的同一顺序调用 M07：`on_env_tick_all(t_g / step)` → `step_dryden(slots, p, v, 0.02)` →
  `query(p, t_g, fields = WIND_PARTS, vel, agent_idx)`。结果作为 `WindSp(uav_index, apply_step = (t_g − t_k)/step)` 下发，
  接收端零阶保持（与 Fast 档同为 50 Hz 零阶保持），与上次下发相同的风矢量省略。风输入函数与 Fast 档相同（同一 M07 服务、同一
  网格时刻、同一 slot 编号与 Dryden 推进顺序），差异只剩位置预测误差。
  M24 §6.5.8 原文写"子区间中点"：Fast 档在网格点（子区间起点）以当时位置采样，取起点才使两档的采样时刻一致，偏差记入文档增补。
- 力律（`drag_force` 为 numpy 对照，C++ 同式见 WindLaw.hh）：`linear` 为 `F = m·k·(w − v)`（k 单位 1/s，机型未标定 k 时 630）；
  `composite` 为 FleetSim `aero = composite` 同式 `F = −½ρ·CdA·‖v_r‖·v_r − ΣΩ·c_rd·(v_r − (v_r·b3)·b3)`，参数取 M21（`CdA`、
  `c_rd`），ΣΩ 取 gz 实际旋翼转速之和。gz MulticopterMotorModel 已按地速施加旋翼阻力 `−Σ|ω_i|·c_rd·v⊥`（rotorDragCoefficient
  与 M21 x500 `c_rd` 同值），插件只补风引起的增量 `+ΣΩ·c_rd·w⊥`（`motor_rotor_drag = True`，缺省），两者之和即作用于相对空速。
- 回放（M24-FR-078）：`for_tick` 按 tick 缓存下发内容。`SupervisedGzBackend` 从回合起点重放时同一 tick 直接返回缓存（M07 服务带
  状态，不能重算）；重放输入状态与缓存不一致时记 638 平台失败。
- 空气密度：插件取运行级常数 `rho`（`rho_at` 以 M07 ISA 在出生点求值），Fast 档按 10 Hz 全量求值逐机更新；低空范围内差异
  < 0.5%，计入档间差异。

组合：`field_plugin_xml` 生成插件段，`with_field_wind(run_sdf, xml)` 插入 `compose_run` 的输出（compose 不加风实体，即
`wind = False`）；`GzSimBackend(wind=FieldWind(...))` 每 tick 调用 `for_tick` 并把结果放入 `Control.wind`。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from awr.phys import PhysError
from awr.phys.gz import protocol as P
from awr.phys.gz.compose import ROTORS, STEP_NS, G
from awr.phys.kinematic import GEAR_Z_M

__all__ = ["DRAG_LAWS", "GRID_NS", "WIND_MODES", "DragSpec", "FieldWind", "drag_force", "field_plugin_xml", "load_drag_spec",
           "rho_at", "with_field_wind"]

WIND_MODES = ("uniform", "field")
DRAG_LAWS = ("linear", "composite")
GRID_NS = 20_000_000                     # M07 env 网格（env stage every 5、phase 0；d1_250 下 50 Hz）
DT_ENV_S = 0.02                          # env stage 的 Dryden 推进步长（awr.environment.stage.DT_ENV_S）
WIND_PARTS = 2                           # awr.environment.query.Fields.WIND_PARTS：只求风（均值、阵风、湍流）
THERMO = 32                              # awr.environment.query.Fields.THERMO：温度、气压、密度
RHO0 = 1.225
ROTOR_SLOWDOWN = 10.0                    # compose 中 MulticopterMotorModel 的 rotorVelocitySlowdownSim


# ---------------------------------------------------------------------------------------------------------------- 力律
@dataclass(frozen=True)
class DragSpec:
    """一架机的风阻参数（M21）。`k_per_s` 只用于 linear；`hover_sum_omega` 为旋翼关节速度尚不可用时的 ΣΩ。"""

    law: str = "composite"
    mass_kg: float = 0.0
    cda_m2: float = 0.0
    c_rd: float = 0.0
    k_per_s: float = 0.0
    n_rot: int = 4
    omega_max_rad_s: float = 1000.0
    t_max_n: float = 0.0
    profile: str = ""

    @property
    def hover_sum_omega(self) -> float:
        """ΣΩ = n·ω_max·√clip(T̂, 0, 1)，T̂ = m·g / T_max（FleetSim composite 的悬停值）。"""
        if self.t_max_n <= 0.0:
            return 0.0
        th = min(max(self.mass_kg * G / self.t_max_n, 0.0), 1.0)
        return self.n_rot * self.omega_max_rad_s * math.sqrt(th)

    def check(self) -> DragSpec:
        if self.law not in DRAG_LAWS:
            raise PhysError(630, f"drag law {self.law!r} is not one of {DRAG_LAWS}")
        if self.law == "linear" and not self.k_per_s > 0.0:
            raise PhysError(630, f"vehicle {self.profile or '?'} has no calibrated wind coefficient k for the linear drag law")
        if self.law == "linear" and not self.mass_kg > 0.0:
            raise PhysError(630, f"vehicle {self.profile or '?'} has no mass for the linear drag law")
        if self.law == "composite" and (self.cda_m2 < 0.0 or self.c_rd < 0.0):
            raise PhysError(630, f"vehicle {self.profile or '?'} has negative composite drag parameters")
        return self

    @classmethod
    def from_profile(cls, prof: Any, *, law: str | None = None, k_per_s: float | None = None) -> DragSpec:
        """M21 Profile（awr.sim.fleet.profiles.Profile）→ DragSpec。law 缺省取机型的 `aero`；linear 机型的 k 取 `k_dv / m`
        （FleetSim `F = −K_dv·v_r` 与 `F = m·k·(w − v)` 同式），可由 `k_per_s`（configs/phys/gz_wind_k.json 标定值）覆盖。"""
        m = float(prof.mass_kg)
        lw = law or ("linear" if str(prof.aero) == "linear" else "composite")
        k = float(k_per_s) if k_per_s is not None else (float(prof.k_dv) / m if str(prof.aero) == "linear" and m > 0 else 0.0)
        return cls(law=lw, mass_kg=m, cda_m2=float(prof.cda_m2), c_rd=float(prof.c_rd), k_per_s=k, n_rot=int(prof.n_rot),
                   omega_max_rad_s=float(prof.omega_max_rad_s), t_max_n=float(prof.t_max_n),
                   profile=str(getattr(prof, "profile_id", ""))).check()


def load_drag_spec(vehicle_ref: str = "x500", *, law: str | None = None, k_per_s: float | None = None, vehicles: Any = None) -> DragSpec:
    """按机型引用读取 M21 模板（D3 覆盖层变体取基机型，同 Fast 档 `resolve_profile`）。"""
    from awr.phys.fast import resolve_profile
    from awr.sim.fleet.profiles import ProfileTable

    T = ProfileTable(vehicles=vehicles)
    return DragSpec.from_profile(T.get(resolve_profile(vehicle_ref, T)), law=law, k_per_s=k_per_s)


def drag_force(spec: DragSpec, v: np.ndarray, w: np.ndarray, b3: np.ndarray, sum_omega: np.ndarray | float, *, rho: float = RHO0,
               motor_rotor_drag: bool = True) -> np.ndarray:
    """插件施加的风力（N，ENU；与 WindLaw.hh 逐式对应，运算顺序同 awr.sim.fleet.aero.aero_force）。v、w、b3 为 (N, 3)。"""
    v = np.asarray(v, np.float64).reshape(-1, 3)
    w = np.asarray(w, np.float64).reshape(-1, 3)
    b3 = np.asarray(b3, np.float64).reshape(-1, 3)
    so = np.broadcast_to(np.asarray(sum_omega, np.float64), (v.shape[0],))
    vr = v - w
    out = np.empty_like(vr)
    if spec.law == "linear":
        km = spec.mass_kg * spec.k_per_s
        for c in range(3):
            out[:, c] = -km * vr[:, c]
        return out
    spd = np.sqrt(vr[:, 0] * vr[:, 0] + vr[:, 1] * vr[:, 1] + vr[:, 2] * vr[:, 2])
    x = -w if motor_rotor_drag else vr
    dotb = x[:, 0] * b3[:, 0] + x[:, 1] * b3[:, 1] + x[:, 2] * b3[:, 2]
    kq = 0.5 * rho * spec.cda_m2
    kr = so * spec.c_rd
    for c in range(3):
        out[:, c] = -kq * spd * vr[:, c] - kr * (x[:, c] - dotb * b3[:, c])
    return out


# ---------------------------------------------------------------------------------------------------------------- SDF
def _g(x: float) -> str:
    return repr(float(x))


def field_plugin_xml(uavs: Sequence[str], specs: DragSpec | Mapping[str, DragSpec], *, rho: float = RHO0, motor_rotor_drag: bool = True,
                     rotors: Sequence[str] | None = None, rotor_slowdown: float = ROTOR_SLOWDOWN) -> str:
    """Drone4DWindDrag 插件段（world 级，`uav` 顺序同 compose 的机队顺序）。全部机体须同一力律。"""
    joints = [f"{r}_joint" for r, _d, _s in ROTORS] if rotors is None else list(rotors)
    per = {u: (specs if isinstance(specs, DragSpec) else specs[u]).check() for u in uavs}
    laws = {s.law for s in per.values()}
    if len(laws) != 1:
        raise PhysError(630, f"field wind mode needs one drag law for the whole fleet, got {sorted(laws)}")
    if not (rho > 0.0 and math.isfinite(rho)):
        raise PhysError(630, f"air density {rho!r} is not positive")
    rows = []
    for u in uavs:
        s = per[u]
        rot = "".join(f'<rotor joint="{j}"/>' for j in joints)
        rows.append(f'\n      <uav model="{u}" link="base_link" mass="{_g(s.mass_kg)}" cda="{_g(s.cda_m2)}" c_rd="{_g(s.c_rd)}" '
                    f'k="{_g(s.k_per_s)}" hover_sum_omega="{_g(s.hover_sum_omega)}">{rot}</uav>')
    return f"""
    <plugin filename="libDrone4DGz.so" name="drone4d::Drone4DWindDrag">
      <law>{laws.pop()}</law>
      <rho>{_g(rho)}</rho>
      <motor_rotor_drag>{str(bool(motor_rotor_drag)).lower()}</motor_rotor_drag>
      <rotor_slowdown>{_g(rotor_slowdown)}</rotor_slowdown>{"".join(rows)}
    </plugin>"""


def with_field_wind(run_sdf: str, plugin_xml: str) -> str:
    """把插件段插入 `compose_run(..., wind=False)` 的输出（world 末尾）。uniform 的风实体与 field 互斥（630）。"""
    if "gz-sim-wind-effects-system" in run_sdf:
        raise PhysError(630, "field wind mode cannot be combined with the uniform WindEffects system (compose with wind=False)")
    marker = "\n  </world>"
    if run_sdf.count(marker) != 1:
        raise ValueError("run.sdf does not have exactly one closing world element")
    return run_sdf.replace(marker, plugin_xml + marker)


def rho_at(env: Any, pos_enu: Sequence[float], t_ns: int = 0) -> float:
    """M07 ISA 空气密度（kg/m³），位置为 World ENU（插件的运行级常数，通常取首架机的出生点）。"""
    q = env.query(np.asarray(pos_enu, np.float64).reshape(1, 3), int(t_ns), fields=THERMO)
    return float(q.rho_kgm3[0])


# ---------------------------------------------------------------------------------------------------------------- 采样
@dataclass
class FieldWind:
    """field 模式宿主侧采样器（每个 Run 一个；`env` 为该 Run 独占的 M07 EnvironmentService，与 Fast 档同一世界、同一种子）。

    `for_tick(k, t_k_ns, n_steps, last)`：`last` 为带 `last_sample(uav)` 的 PoseHistoryBuffer（取 `pos_enu_m`、`vel_enu_mps`），
    或 uav → (pos, vel) 映射。返回本 tick 的 WindSp 列表（按 apply_step、uav_index 排序）。`record = True` 时逐网格点记录
    (k, apply_step, t_g, 采样位置, 风) 到 `log`（M24-AC-017 对拍）。
    """

    env: Any
    uavs: Sequence[str]
    step_ns: int = STEP_NS
    dz_m: float = -GEAR_Z_M
    agent_idx: Sequence[int] | None = None
    compress: bool = True
    record: bool = False
    log: list[tuple[int, int, int, np.ndarray, np.ndarray]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.uavs = tuple(self.uavs)
        if GRID_NS % self.step_ns:
            raise PhysError(637, f"physics step {self.step_ns} ns does not divide the 20 ms environment grid")
        self.slots = np.arange(len(self.uavs), dtype=np.int64) if self.agent_idx is None else np.asarray(self.agent_idx, np.int64)
        if self.slots.shape != (len(self.uavs),):
            raise ValueError("agent_idx must give one slot per vehicle")
        if hasattr(self.env, "tick_ns"):
            self.env.tick_ns = int(self.step_ns)             # env tick 序号 = t / 物理步长（Fast 档 d1_250 为 4 ms）
        self._sent = np.full((len(self.uavs), 3), np.nan)    # 接收端当前保持值（NaN：尚未下发）
        self._cache: dict[int, tuple[bytes, list[tuple[int, int, float, float, float]]]] = {}
        self._buf = None
        self.stats = {"ticks": 0, "grid_points": 0, "messages": 0, "replayed": 0, "keyframes": 0, "max_speed_mps": 0.0}

    def _state(self, last: Any) -> tuple[np.ndarray, np.ndarray]:
        pos = np.empty((len(self.uavs), 3))
        vel = np.empty((len(self.uavs), 3))
        for i, u in enumerate(self.uavs):
            if hasattr(last, "last_sample"):
                s = last.last_sample(u)
                pos[i], vel[i] = s.pos_enu_m, s.vel_enu_mps
            else:
                pos[i], vel[i] = last[u]
        return pos, vel

    def grid_steps(self, t_k_ns: int, n_steps: int) -> list[int]:
        """本 tick 内落在 20 ms 网格上的步号 j（t_g = t_k + j·step）。"""
        first = (-int(t_k_ns)) % GRID_NS // self.step_ns
        return list(range(first, int(n_steps), GRID_NS // self.step_ns))

    def sample(self, t_g_ns: int, pos: np.ndarray, vel: np.ndarray) -> np.ndarray:
        """在网格时刻 t_g 按 env stage 的顺序推进 M07 并求风（ENU 去向，(n, 3)）。"""
        env = self.env
        frames = env.on_env_tick_all(int(t_g_ns) // self.step_ns)
        self.stats["keyframes"] += len(frames or ())
        if hasattr(env, "outbox"):
            env.outbox.clear()                               # 关键帧与告警由 Fast 档或 M23 发出，这里不重复
        env.step_dryden(self.slots, pos, vel, DT_ENV_S)
        q = env.query(pos, int(t_g_ns), fields=WIND_PARTS, vel=vel, agent_idx=self.slots, out=self._buf)
        self._buf = q
        return np.array(q.wind_mps[: len(self.uavs)], np.float64)

    def for_tick(self, k: int, t_k_ns: int, n_steps: int, last: Any) -> list:
        pos0, vel0 = self._state(last)
        key = hashlib.sha256(np.int64(t_k_ns).tobytes() + pos0.tobytes() + vel0.tobytes()).digest()
        hit = self._cache.get(int(k))
        if hit is not None:
            if hit[0] != key:
                raise PhysError(638, f"field wind replay diverged at tick {k}: tick-start state differs from the original run",
                                detail={"platform_failure": True, "reason": "replay_diverged", "what": "field wind input"})
            self.stats["replayed"] += 1
            return [P.msg("WindSp", uav_index=i, apply_step=j, wx=x, wy=y, wz=z) for j, i, x, y, z in hit[1]]
        rows: list[tuple[int, int, float, float, float]] = []
        for j in self.grid_steps(t_k_ns, n_steps):
            dt = j * self.step_ns * 1e-9
            p = pos0 + vel0 * dt
            p[:, 2] += self.dz_m
            w = self.sample(int(t_k_ns) + j * self.step_ns, p, vel0)
            self.stats["grid_points"] += 1
            self.stats["max_speed_mps"] = max(self.stats["max_speed_mps"], float(np.max(np.linalg.norm(w, axis=1))) if len(w) else 0.0)
            if self.record:
                self.log.append((int(k), j, int(t_k_ns) + j * self.step_ns, p.copy(), w.copy()))
            for i in range(len(self.uavs)):
                if self.compress and np.array_equal(self._sent[i], w[i]):
                    continue
                self._sent[i] = w[i]
                rows.append((j, i, float(w[i, 0]), float(w[i, 1]), float(w[i, 2])))
        self._cache[int(k)] = (key, rows)
        self.stats["ticks"] += 1
        self.stats["messages"] += len(rows)
        return [P.msg("WindSp", uav_index=i, apply_step=j, wx=x, wy=y, wz=z) for j, i, x, y, z in rows]

    def describe(self) -> dict:
        """版本记录（M24-FR-067）与运行摘要。"""
        return {"wind_mode": "field", "grid_ms": GRID_NS // 1_000_000, "sample": "grid_start_predicted", "dz_m": self.dz_m,
                "slots": self.slots.tolist(), **self.stats}
