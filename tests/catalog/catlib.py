"""tests/catalog 共用工具：M21 插件装配、带对象形式 P600 映射的临时机型目录、单机固定翼运行夹具。"""

from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
VEH = ROOT / "vehicles"
P600_V2_SENSORS = {"camera": "sensors/camera.yaml", "thermal": "sensors/thermal.yaml",
                   "mid360": {"file": "sensors/mid360.yaml", "default": True, "removable": True},
                   "gnss": {"file": "sensors/gnss.yaml", "default": True, "removable": False},
                   "imu": {"file": "sensors/imu.yaml", "default": True, "removable": False},
                   "eo": {"file": "sensors/eo_gx40.yaml", "default": True, "removable": False},
                   "nir_ill": {"file": "sensors/nir_ill_l.yaml", "default": False}}


def install() -> tuple[Any, Any]:
    """在当前登记表中装配 M21 的两个插件（目录、固定翼）；返回 (CatalogRuntime, FwRuntime)。"""
    from awr.sim.catalog import plugin as CP
    from awr.sim.fixedwing import plugin as FP

    return CP.install(), FP.install()


def vehicles_copy(tmp: Path, *, p600_object_mapping: bool = False) -> Path:
    """复制 vehicles/（不含 glb）；`p600_object_mapping` 时把 P600 的 `sensors:` 改为 AWR-20 §7.4 的对象形式（M08 待切换的写法）。"""
    dst = tmp / "vehicles"
    shutil.copytree(VEH, dst, ignore=shutil.ignore_patterns("*.glb"))
    if p600_object_mapping:
        f = dst / "p600" / "params.yaml"
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        doc["sensors"] = dict(P600_V2_SENSORS)
        doc["payload"] = [x for x in doc.get("payload") or [] if x["name"] == "orin_nx"]
        f.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return dst


def rel(a: float, b: float) -> float:
    return abs(a - b) / max(abs(b), 1e-12)


def wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi
