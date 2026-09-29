"""SensorModel 接口与 D1 实现（M13-FR-004；M13 §7.1；ADR-048；P-07 保真度阶梯）。

`SensorModel`（Protocol）在 V0.1 冻结：`kind`、`caps`、`describe()`、`on_spawn(slot, spec, tick)`、`on_remove(slot)`、
`step(ctx)`、`checkpoint()`、`restore(b)`。D1 交付 Camera、Thermal、Gnss、Imu 的实现与 Lidar 桩：它们是 SensorRuntime 各 Bank
的薄门面（状态在 M08 分配的 sensors 块里，checkpoint 随块走，这里的 checkpoint 只含模型私有状态）。Gazebo（V0.6）、
Isaac（V0.8）经 `backends.base.SensorBackend` 接入，UI 与协议不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from .enums import SensorKind
from .spec import SensorSpec

if TYPE_CHECKING:
    from .runtime import SensorRuntime

__all__ = ["CameraModel", "GnssModel", "ImuModel", "LidarStub", "SensorCaps", "SensorModel", "ThermalModel", "models_for"]

Raycast = Literal["none", "dsm", "geo", "gz", "isaac"]


@dataclass(frozen=True)
class SensorCaps:
    pose: bool = True
    noise: bool = False
    detect: bool = False
    raycast: Raycast = "none"


@runtime_checkable
class SensorModel(Protocol):
    kind: SensorKind
    caps: SensorCaps

    def describe(self) -> dict: ...
    def on_spawn(self, slot: int, spec: SensorSpec, tick: int) -> None: ...
    def on_remove(self, slot: int) -> None: ...
    def step(self, ctx: Any) -> None: ...
    def checkpoint(self) -> bytes: ...
    def restore(self, b: bytes) -> None: ...


class _Base:
    kind: SensorKind = SensorKind.CAMERA
    caps = SensorCaps()

    def __init__(self, rt: SensorRuntime, spec: SensorSpec) -> None:
        self.rt = rt
        self.spec = spec

    def describe(self) -> dict:
        from .describe import spec_json

        d = spec_json(self.spec)
        d["caps"] = {"pose": self.caps.pose, "noise": self.caps.noise, "detect": self.caps.detect, "raycast": self.caps.raycast}
        return d

    def on_spawn(self, slot: int, spec: SensorSpec, tick: int) -> None:  # 装配由 SensorRuntime.sync 统一完成
        return None

    def on_remove(self, slot: int) -> None:
        return None

    def step(self, ctx: Any) -> None:
        return None

    def checkpoint(self) -> bytes:
        return b""

    def restore(self, b: bytes) -> None:
        return None


class CameraModel(_Base):
    kind = SensorKind.CAMERA
    caps = SensorCaps(pose=True, detect=True)


class ThermalModel(_Base):
    kind = SensorKind.THERMAL
    caps = SensorCaps(pose=True, detect=True)

    def checkpoint(self) -> bytes:
        return self.rt.detector.targets.checkpoint()

    def restore(self, b: bytes) -> None:
        if b:
            self.rt.detector.targets.restore(b)


class GnssModel(_Base):
    kind = SensorKind.GNSS
    caps = SensorCaps(pose=False, noise=True)


class ImuModel(_Base):
    kind = SensorKind.IMU
    caps = SensorCaps(pose=False, noise=True)


class LidarStub(_Base):
    """V0.2 起由 geo-worker `svc/geo/lidar` 求交；D1 只有花样纯函数与离线夹具。"""

    kind = SensorKind.LIDAR
    caps = SensorCaps(pose=True, raycast="none")


_IMPL = {SensorKind.CAMERA: CameraModel, SensorKind.THERMAL: ThermalModel, SensorKind.GNSS: GnssModel,
         SensorKind.IMU: ImuModel, SensorKind.LIDAR: LidarStub}


def models_for(rt: SensorRuntime, specs: tuple[SensorSpec, ...]) -> list[SensorModel]:
    return [_IMPL[s.kind](rt, s) for s in specs if s.kind in _IMPL]
