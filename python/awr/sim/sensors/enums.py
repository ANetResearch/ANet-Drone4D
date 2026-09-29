"""M13 枚举（M13-FR-002、§6.5.2、§6.7.2）。

`SensorKind` 前 4 个值与 `rt/enums.json` 的 SensorKind（SensorPose48 `kind`）一致；gnss、imu、baro 三个值与 `GnssFix`、
`GimbalMode` 已提请 M00 登记到 `rt/enums.json`（`.cache/impl/requests/M13-to-M00.md`），登记前以本文件为准。
"""

from __future__ import annotations

from enum import IntEnum

__all__ = ["FIX_NAMES", "GIMBAL_MODE_NAMES", "KIND_BIT", "KIND_NAMES", "GimbalMode", "GnssFix", "SensorKind", "SensorState"]


class SensorKind(IntEnum):
    CAMERA = 0
    LIDAR = 1
    THERMAL = 2
    RADAR = 3
    GNSS = 4
    IMU = 5
    BARO = 6


KIND_NAMES = {k: k.name.lower() for k in SensorKind}

# `has`、`act` 位序（M13 §6.4.2）：bit0 CAM、bit1 THERMAL、bit2 GNSS、bit3 IMU、bit4 LIDAR、bit5 BARO
KIND_BIT = {SensorKind.CAMERA: 1, SensorKind.THERMAL: 2, SensorKind.GNSS: 4, SensorKind.IMU: 8, SensorKind.LIDAR: 16,
            SensorKind.BARO: 32, SensorKind.RADAR: 0}


class GnssFix(IntEnum):
    NO_FIX = 0
    SINGLE = 1
    DGPS = 2
    RTK_FLOAT = 3
    RTK_FIXED = 4


FIX_NAMES = {f: f.name for f in GnssFix}


class GimbalMode(IntEnum):
    FIXED = 0
    LOOK_AT = 1
    LOOK_AT_AXIS = 2
    NADIR = 3
    FORWARD = 4


GIMBAL_MODE_NAMES = {m: m.name.lower() for m in GimbalMode}


class SensorState(IntEnum):
    """传感器实例生命周期（M13 §6.7.1）；INIT 到 ACTIVE、STANDBY 切换不发事件。"""

    OFF = 0
    INIT = 1
    ACTIVE = 2
    DEGRADED = 3
    STANDBY = 4
    FAULT = 5
