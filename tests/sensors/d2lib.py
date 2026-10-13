"""tests/sensors 的 D2 夹具（M13 WP-10）：在临时目录里搭一个 schema 1.1 的机型库。

- `p600`：仓库 `vehicles/p600` 的 params.yaml（数值不变，P600 2.0.0 由 M08 交付）把 `sensors:` 换成 AWR-20 §7.4 的 v2 映射
  （camera、thermal 为 D1 槽位；mid360、gnss、imu、eo 为对象形式；nir_ill 可选），并加能力 `compute.orin_nx`；
  传感器文件取仓库 `vehicles/p600/sensors/` 原样（含本工作包的 `eo_gx40.yaml`、`nir_ill_l.yaml`、`mid360.yaml` v2）。
- `tq9`：以 x500 机体参数为底的测试机型，挂 GX40、LWIR 100 mm（独立云台）、毫米波雷达、声阵列与可选 `nir_ill_l`（Q9 的挂载组合）。
- `tq1`：x500 机体加 EO-Z8（无照明器）与跟随 EO 云台的 LWIR 13 mm（Q1 的挂载组合）。
- `sensor_models/`：`tests/sensors/fixtures/sensor_models/`（AWR-20 §6、§7.8 的型号；正式型号库归 M21）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
VEH = ROOT / "vehicles"
LIB = Path(__file__).resolve().parent / "fixtures" / "sensor_models"

P600_V2_SENSORS = {
    "camera": "sensors/camera.yaml",
    "thermal": "sensors/thermal.yaml",
    "mid360": {"file": "sensors/mid360.yaml", "default": True, "removable": True},
    "gnss": {"file": "sensors/gnss.yaml", "default": True, "removable": False},
    "imu": {"file": "sensors/imu.yaml", "default": True, "removable": False},
    "eo": {"file": "sensors/eo_gx40.yaml", "default": True, "removable": False},
    "nir_ill": {"file": "sensors/nir_ill_l.yaml", "default": False},
}

_GNSS_IMU = ("gnss", "imu")

TQ9_MOUNTS = {
    "eo": {"schema": "awr.sensor.eo_zoom.v1", "sensor_id": "eo", "spec_ref": "eo_gx40@1.0.0",
           "mount": {"xyz_m": [0.15, 0.0, -0.10], "rpy_deg": [0, 0, 0]}, "conf": "D", "src": "测试机型 tq9"},
    "lwir": {"schema": "awr.sensor.lwir.v1", "sensor_id": "lwir", "spec_ref": "lwir640@1.0.0", "lens_mm": 100,
             "mount": {"xyz_m": [0.15, 0.08, -0.10], "rpy_deg": [0, 0, 0]}, "conf": "D", "src": "测试机型 tq9"},
    "radar": {"schema": "awr.sensor.radar_mmw.v1", "sensor_id": "radar", "spec_ref": "mmw77@1.0.0",
              "mount": {"xyz_m": [0.2, 0.0, -0.02], "rpy_deg": [0, 10, 0]}, "conf": "D", "src": "测试机型 tq9"},
    "mic": {"schema": "awr.sensor.acoustic_array.v1", "sensor_id": "mic", "spec_ref": "mic8@1.0.0",
            "mount": {"xyz_m": [0.0, 0.0, 0.12], "rpy_deg": [0, 0, 0]}, "conf": "D", "src": "测试机型 tq9"},
    "nir_ill": {"schema": "awr.sensor.nir_illuminator.v1", "sensor_id": "nir_ill", "spec_ref": "nir_ill_l@1.0.0",
                "mount": {"xyz_m": [0.15, 0.0, -0.12], "rpy_deg": [0, 0, 0]}, "on_gimbal_of": "eo", "conf": "D", "src": "测试机型 tq9"},
}
TQ9_MAP = {"eo": {"file": "sensors/eo.yaml", "default": True, "removable": False},
           "lwir": {"file": "sensors/lwir.yaml", "default": True},
           "radar": {"file": "sensors/radar.yaml", "default": True},
           "mic": {"file": "sensors/mic.yaml", "default": True},
           "nir_ill": {"file": "sensors/nir_ill.yaml", "default": False},
           "gnss": {"file": "sensors/gnss.yaml", "default": True, "removable": False},
           "imu": {"file": "sensors/imu.yaml", "default": True, "removable": False}}
TQ1_MOUNTS = {
    "eo": {"schema": "awr.sensor.eo_zoom.v1", "sensor_id": "eo", "spec_ref": "eo_z8@1.0.0",
           "mount": {"xyz_m": [0.1, 0.0, -0.05], "rpy_deg": [0, 0, 0]}, "conf": "D", "src": "测试机型 tq1"},
    "lwir": {"schema": "awr.sensor.lwir.v1", "sensor_id": "lwir", "spec_ref": "lwir640@1.0.0", "lens_mm": 13,
             "mount": {"xyz_m": [0.1, 0.03, -0.05], "rpy_deg": [0, 0, 0]}, "on_gimbal_of": "eo", "conf": "D", "src": "测试机型 tq1"},
}
TQ1_MAP = {"eo": {"file": "sensors/eo.yaml", "default": True, "removable": False},
           "lwir": {"file": "sensors/lwir.yaml", "default": False},
           "gnss": {"file": "sensors/gnss.yaml", "default": True, "removable": False},
           "imu": {"file": "sensors/imu.yaml", "default": True, "removable": False}}


def _dump(p: Path, doc: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _x500_based(root: Path, vid: str, pid: str, mounts: dict, smap: dict) -> None:
    doc = yaml.safe_load((VEH / "x500" / "params.yaml").read_text(encoding="utf-8"))
    doc.pop("variants", None)
    doc["id"] = pid
    doc["display_name"] = f"{vid} 测试机型"
    doc["sensors"] = smap
    d = root / vid
    _dump(d / "params.yaml", doc)
    (d / "sensors").mkdir(parents=True, exist_ok=True)
    for name in _GNSS_IMU:
        shutil.copy(VEH / "p600" / "sensors" / f"{name}.yaml", d / "sensors" / f"{name}.yaml")
    for name, m in mounts.items():
        _dump(d / "sensors" / f"{name}.yaml", m)


def make_d2_vehicles(root: Path, *, extra: bool = True) -> Path:
    """临时机型库（见模块说明）；返回目录（含 p600、tq9、tq1 与 sensor_models/）。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(LIB, root / "sensor_models", dirs_exist_ok=True)
    p = root / "p600"
    shutil.copytree(VEH / "p600" / "sensors", p / "sensors", dirs_exist_ok=True)
    shutil.copytree(VEH / "p600" / "model", p / "model", dirs_exist_ok=True)
    doc = yaml.safe_load((VEH / "p600" / "params.yaml").read_text(encoding="utf-8"))
    doc["sensors"] = P600_V2_SENSORS
    doc["capabilities"] = ["compute.orin_nx"]
    _dump(p / "params.yaml", doc)
    if extra:
        _x500_based(root, "tq9", "tq9_test", TQ9_MOUNTS, TQ9_MAP)
        _x500_based(root, "tq1", "tq1_test", TQ1_MOUNTS, TQ1_MAP)
    return root
