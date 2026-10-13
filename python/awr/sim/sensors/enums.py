"""M13 枚举（M13-FR-002、§6.5.2、§6.7.2；D2：M13 §15.1、FR-205、FR-206）。

`SensorKind` 与 `rt/enums.json` 的 SensorKind 一致（D2 只追加 `illuminator = 7`、`acoustic = 8`，AWR-04 附录 B 第 82 行；二者不进入
SensorPose48）。D1 状态块 `sensors.state` 只为前 7 类（`D1_KINDS`）保留生命周期列，D2 两类的启停记在 `payload` 块。
`PointSrc`、`ImagingMode`、`ImagingCmd`、`IllumCfg` 为 `payload` 块的取值（M13 §15.3.2；线上名称见各 `*_NAMES`）。
"""

from __future__ import annotations

from enum import IntEnum

__all__ = ["D1_KINDS", "FIX_NAMES", "GIMBAL_MODE_NAMES", "ILLUM_CFG_NAMES", "IMAGING_CMD_NAMES", "IMAGING_MODE_NAMES", "KIND_BIT",
           "KIND_NAMES", "LIGHT_BAND_NAMES", "POINT_SRC_NAMES", "GimbalMode", "GnssFix", "IllumCfg", "ImagingCmd", "ImagingMode",
           "LightBand", "PointSrc", "SensorKind", "SensorState"]


class SensorKind(IntEnum):
    CAMERA = 0
    LIDAR = 1
    THERMAL = 2
    RADAR = 3
    GNSS = 4
    IMU = 5
    BARO = 6
    ILLUMINATOR = 7
    ACOUSTIC = 8


KIND_NAMES = {k: k.name.lower() for k in SensorKind}
D1_KINDS = tuple(k for k in SensorKind if k < SensorKind.ILLUMINATOR)

# `has`、`act` 位序（M13 §6.4.2）：bit0 CAM、bit1 THERMAL、bit2 GNSS、bit3 IMU、bit4 LIDAR、bit5 BARO；D2 的雷达、照明器与
# 声阵列不占位（挂载与启停由 payload 块的按 sensor_no 位掩码 `mnt`、`off` 给出）
KIND_BIT = {SensorKind.CAMERA: 1, SensorKind.THERMAL: 2, SensorKind.GNSS: 4, SensorKind.IMU: 8, SensorKind.LIDAR: 16,
            SensorKind.BARO: 32, SensorKind.RADAR: 0, SensorKind.ILLUMINATOR: 0, SensorKind.ACOUSTIC: 0}


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


class PointSrc(IntEnum):
    """云台指向源（M13-FR-206），数值越大优先级越高。"""

    DEFAULT = 0
    CUE = 1
    TASKING = 2
    OPERATOR = 3


POINT_SRC_NAMES = {s: s.name.lower() for s in PointSrc}


class ImagingMode(IntEnum):
    """成像传感器的当前工作模式（M13-FR-205；AWR-04 §6.1 夜视）。"""

    DAY = 0
    LOWLIGHT = 1
    NIR = 2


IMAGING_MODE_NAMES = {m: m.name.lower() for m in ImagingMode}


class ImagingCmd(IntEnum):
    """`sensor/mode` 的 `mode`：AUTO 随照度档切换，其余锁定。"""

    AUTO = 0
    DAY = 1
    LOWLIGHT = 2
    NIR = 3


IMAGING_CMD_NAMES = {m: m.name.lower() for m in ImagingCmd}


class IllumCfg(IntEnum):
    """`sensor/mode` 的 `illum.builtin`、`illum.ext`。"""

    AUTO = 0
    ON = 1
    OFF = 2


ILLUM_CFG_NAMES = {m: m.name.lower() for m in IllumCfg}


class LightBand(IntEnum):
    """照度档（AWR-04 §6.5；`rt/enums.json` LightBand：day、low、night）。"""

    DAY = 0
    LOW = 1
    NIGHT = 2


LIGHT_BAND_NAMES = {b: ("day", "low", "night")[int(b)] for b in LightBand}
