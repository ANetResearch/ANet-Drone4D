"""运行级 SDF 组合（M24 §6.5.1、§6.12）：world_static（世界碰撞，`include`）+ 机队（x500_d4d、MulticopterMotorModel × 4、
Drone4DVelocityControl）+ Physics（dartsim + bullet，4 ms）+ UserCommands + Drone4DClock（锁步，`uav_index → 模型名` 表）。

`physics` 导出档不加载渲染与传感器系统。机队按 `uavs` 顺序编号 uav_index，模型名为 `uav-01` 之类的 uav id。速度控制增益沿用 r9
原型（velocityGain 2.7、attitudeGain (2, 3, 0.15)、angularRateGain (0.4, 0.52, 0.18)、最大加速度 3 m/s²）；限幅取
`VelocityLimits`（缺省 12 m/s 水平、5 m/s 竖直、2 rad/s 偏航），对应 M21 限幅档。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

__all__ = ["STEP_NS", "G", "UavSpawn", "VelocityLimits", "compose_run"]

G = 9.80665
STEP_NS = 4_000_000
ROTORS = (("rotor_0", "ccw", 1), ("rotor_1", "ccw", 1), ("rotor_2", "cw", -1), ("rotor_3", "cw", -1))
X500_Z0 = 0.24                                   # x500 模型原点到 base_link 的竖直偏移（model.sdf 的 pose）


@dataclass(frozen=True)
class VelocityLimits:
    v_xy_mps: float = 12.0
    v_z_mps: float = 5.0
    yaw_rate_rps: float = 2.0
    acc_mps2: float = 3.0


@dataclass(frozen=True)
class UavSpawn:
    uav: str
    pos_enu_m: tuple[float, float, float]
    yaw_rad: float = 0.0
    limits: VelocityLimits = field(default_factory=VelocityLimits)


def _f(v) -> str:
    return " ".join(f"{float(x):.9g}" for x in v)


def _vehicle(u: UavSpawn) -> str:
    lim = u.limits
    motors = "".join(f"""
      <plugin filename="gz-sim-multicopter-motor-model-system" name="gz::sim::systems::MulticopterMotorModel">
        <robotNamespace>{u.uav}</robotNamespace><jointName>{r}_joint</jointName><linkName>{r}</linkName>
        <turningDirection>{d}</turningDirection><timeConstantUp>0.0125</timeConstantUp><timeConstantDown>0.025</timeConstantDown>
        <maxRotVelocity>1000.0</maxRotVelocity><motorConstant>8.54858e-06</motorConstant><momentConstant>0.016</momentConstant>
        <commandSubTopic>command/motor_speed</commandSubTopic><motorNumber>{i}</motorNumber>
        <rotorDragCoefficient>8.06428e-05</rotorDragCoefficient><rollingMomentCoefficient>1e-06</rollingMomentCoefficient>
        <rotorVelocitySlowdownSim>10</rotorVelocitySlowdownSim><motorType>velocity</motorType>
      </plugin>""" for i, (r, d, _s) in enumerate(ROTORS))
    rotors = "".join(f"""
          <rotor><jointName>{r}_joint</jointName><forceConstant>8.54858e-06</forceConstant><momentConstant>0.016</momentConstant><direction>{s}</direction></rotor>"""
                     for r, _d, s in ROTORS)
    x, y, z = u.pos_enu_m
    return f"""
    <model name="{u.uav}">
      <pose>{_f((x, y, z - X500_Z0, 0.0, 0.0, u.yaw_rad))}</pose>
      <include merge="true"><uri>model://x500_d4d</uri></include>{motors}
      <plugin filename="libDrone4DGz.so" name="drone4d::Drone4DVelocityControl">
        <robotNamespace>{u.uav}</robotNamespace>
        <commandFrame>world</commandFrame>
        <init_hover>true</init_hover>
        <comLinkName>base_link</comLinkName>
        <velocityGain>2.7 2.7 2.7</velocityGain>
        <attitudeGain>2 3 0.15</attitudeGain>
        <angularRateGain>0.4 0.52 0.18</angularRateGain>
        <maximumLinearAcceleration>{_f((lim.acc_mps2,) * 3)}</maximumLinearAcceleration>
        <maximumLinearVelocity>{_f((lim.v_xy_mps, lim.v_xy_mps, lim.v_z_mps))}</maximumLinearVelocity>
        <maximumAngularVelocity>{_f((3.0, 3.0, lim.yaw_rate_rps))}</maximumAngularVelocity>
        <rotorConfiguration>{rotors}
        </rotorConfiguration>
      </plugin>
    </model>"""


def compose_run(uavs: list[UavSpawn], *, world_static_uri: str | None = None, ground_plane: bool = False,
                endpoint: str = "ipc:///run/d4d/ctl.sock", step_ns: int = STEP_NS, lockstep: bool = True, wind: bool = False,
                world_name: str = "d4d", spherical: dict | None = None) -> str:
    """run.sdf 文本。`world_static_uri` 为世界碰撞模型（`model://world_static`，见 awr.world.export.gz）；`ground_plane` 只用于测试
    （z = 0 平面）；`spherical` 为导出清单的 `spherical_coordinates`（world 帧 ENU 原点）。"""
    if step_ns % 1000:
        raise ValueError("step_ns must be a whole number of microseconds")
    static = f"""
    <include><uri>{world_static_uri}</uri><name>world_static</name></include>""" if world_static_uri else ""
    plane = """
    <model name="ground"><static>true</static>
      <link name="l"><collision name="c"><geometry><plane><normal>0 0 1</normal><size>4000 4000</size></plane></geometry></collision></link>
    </model>""" if ground_plane else ""
    sph = f"""
    <spherical_coordinates><surface_model>EARTH_WGS84</surface_model><world_frame_orientation>ENU</world_frame_orientation>
      <latitude_deg>{float(spherical["latitude_deg"])!r}</latitude_deg><longitude_deg>{float(spherical["longitude_deg"])!r}</longitude_deg>
      <elevation>{float(spherical.get("elevation_m") or 0.0)!r}</elevation><heading_deg>0</heading_deg>
    </spherical_coordinates>""" if spherical and spherical.get("latitude_deg") is not None else ""
    table = "".join(f'\n      <uav model="{u.uav}" link="base_link"/>' for u in uavs)
    wind_xml = """
    <wind><linear_velocity>0 0 0</linear_velocity></wind>
    <plugin filename="gz-sim-wind-effects-system" name="gz::sim::systems::WindEffects">
      <force_approximation_scaling_factor>0.3</force_approximation_scaling_factor>
    </plugin>""" if wind else ""
    return f"""<?xml version="1.0" ?>
<sdf version="1.9" xmlns:gz="http://gazebosim.org/schema">
  <world name="{world_name}">
    <physics name="p" type="ignored">
      <max_step_size>{step_ns / 1e9:.6f}</max_step_size>
      <real_time_factor>0</real_time_factor>
      <dart><collision_detector>bullet</collision_detector></dart>
    </physics>
    <gravity>0 0 {-G}</gravity>{sph}
    <plugin filename="libDrone4DGz.so" name="drone4d::Drone4DClock">
      <gz:system_priority>-200</gz:system_priority>
      <endpoint>{endpoint}</endpoint>
      <step_ns>{step_ns}</step_ns>
      <lockstep>{str(lockstep).lower()}</lockstep>{table}
    </plugin>
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"><engine><filename>gz-physics-dartsim-plugin</filename></engine></plugin>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>{wind_xml}{static}{plane}{"".join(_vehicle(u) for u in uavs)}
  </world>
</sdf>
"""


def hover_speed_rad_s(mass_kg: float = 2.0644615384615385, k_f: float = 8.54858e-06) -> float:
    """四旋翼悬停转速（测试与日志用）：4·k_f·ω² = m·g。"""
    return math.sqrt(mass_kg * G / (4.0 * k_f))
