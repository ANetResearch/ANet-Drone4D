"""机型目录索引的加载与校验（CAT-FR-001、FR-016、FR-017；AWR-20 §4.5、§4.6、§7.1、§7.2、§8.2 CI-1 至 CI-4、MT-8）。

`vehicles/catalog.json`（`awr.vehicle_catalog.v1`）列出可见机型（`visible`、`order`、类别、机巢类型）与隐藏的回归机体、7 个
传感器型号、电池包与充电器。机型参数本身仍由 M08 的加载器读取（`profiles.load_vehicle_docs`：目录存在时只装载目录列出的包，
展开 variants）；本模块只做目录层面的一致性检查与查询：

- CI-1：目录通过 schema；id 唯一；`dir/params.yaml` 存在且 `id`、`profile_version`、`display_name`、`reference` 与目录一致；
  类别与 `frame.type` 一致；
- CI-2：可见机型中多旋翼 ≥ 2、固定翼类（fixed_wing 与 vtol）≥ 2；
- CI-3：`reference = true` 的 id 以 `awr_` 开头，显示名不含真实产品型号（自动检查的禁用词表，人工评审清单另行执行）；
- CI-4：`sensor_models[]` 的文件存在且 id、版本、类型一致；机型的 `battery.pack_id` 在 `packs[]` 中且节数、化学体系与容量一致；
  挂载文件的 `spec_ref` 指向目录列出的型号与版本；
- 能力合成（§4.6）：实例有效能力 = 机型 `capabilities[]` 与 已挂载槽位合成的能力；
- 机巢兼容（MT-8）：`box` 只收多旋翼、`launcher` 只收固定翼、`vtol_pad` 收 VTOL 与多旋翼；电池节数须在机巢某个充电器的
  `[cells_min, cells_max]` 内，否则 `525 NEST_INCOMPATIBLE`（`detail.why ∈ {kind, cells}`）。

文件级失败以 `353 VEHICLE_PROFILE_INVALID` 报告（问题逐项给出规则、路径、值与说明）。

D3 目录覆盖层（AWR-20 §15.7、§15.8；ADR-157；CAT-FR-023、FR-031、FR-033、FR-034、FR-037；D3-AC-13、D3-AC-32）：
`vehicles/d3/catalog.d3.json`（`awr.vehicle_catalog_overlay.v1`）登记 D3 新增的传感器型号、D2 型号的 D3 侧表与机型变体
（`awr.vehicle_variant.v1`）。覆盖层只在 D3 进程中读取：`Catalog.load(d3=True)`，或缺省 `d3=None` 且 `AWR_D3_ENABLED = 1`；
否则 `Catalog.overlay` 为 None，D2 的目录、机型文档、检查与 REST 视图与不读覆盖层时逐字节一致（CAT-NFR-008、D3-AC-01）。
变体有效文档 = `apply_mounts(deep_merge(基机型, set), mounts, remove)`，按 §5.5 重算质量、悬停功率、续航与派生值后重跑 D2 的
VH、MT、SN 检查（OV-3）；D3 新增槽位（如 `slam_cam`）只进 D3 rig，不写入 D2 `sensors` 映射（D2 装配器不认识 D3 型号类型）。
覆盖层检查 OV-1 至 OV-3、SD-1（侧表与 D2 型号一致）、LR-1、LR-2、LR-4、CM-1、RG-1、RG-2 见 `Overlay.problems()`；D3 加载器
发现任何问题即以 353 拒绝（`Overlay.require_ok`，D3 进程拒绝启动）。
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from ..fleet import profiles as PF
from . import derive as D
from .derive import num

__all__ = ["CATALOG_SCHEMA", "CODE_ENV_LIMIT", "CODE_NEST_INCOMPATIBLE", "CODE_PROFILE_INVALID", "D3_ENV", "DISCLAIMER_ZH",
           "FORBIDDEN_NAME_TOKENS", "OVERLAY_SCHEMA", "VARIANT_SCHEMA", "Catalog", "CatalogError", "Overlay", "RigSensor", "Variant",
           "capabilities_of", "category_of", "d3_enabled", "env_temp_check", "imu_noise_tier", "nest_compatible", "problem", "rpy_matrix"]

CATALOG_SCHEMA = "awr.vehicle_catalog.v1"
DISCLAIMER_ZH = "模拟参考值，非任何真实产品"
CODE_PROFILE_INVALID = 353
CODE_NEST_INCOMPATIBLE = 525
CODE_ENV_LIMIT = 521  # VEHICLE_ENV_LIMIT：只告警（AWR-20 EN-1）
CATEGORY_OF_FRAME = {"quad_x": "multirotor", "hex_x": "multirotor", "fixed_wing": "fixed_wing", "vtol_quadplane": "vtol"}
TYPE_OF_SCHEMA = {"awr.sensor.eo_zoom.v1": "eo_zoom", "awr.sensor.nir_illuminator.v1": "nir_illuminator",
                  "awr.sensor.lwir.v1": "lwir", "awr.sensor.radar_mmw.v1": "radar_mmw",
                  "awr.sensor.acoustic_array.v1": "acoustic_array", "awr.sensor.lidar_livox.v1": "lidar_livox",
                  "awr.sensor.gnss.v1": "gnss", "awr.sensor.imu.v1": "imu"}
# CI-3 的自动部分：常见真实无人机品牌与型号词（不区分大小写，按词边界）；人工评审清单另行执行（AWR-20 §8.2）
FORBIDDEN_NAME_TOKENS = ("dji", "matrice", "mavic", "phantom", "inspire", "agras", "autel", "parrot", "anafi", "skydio",
                         "wingtra", "quantum", "trinity", "jouav", "ebee", "sensefly", "yuneec", "freefly", "teal", "flir")
NEST_ACCEPTS = {"box": frozenset({"multirotor"}), "launcher": frozenset({"fixed_wing"}),
                "vtol_pad": frozenset({"vtol", "multirotor"})}


class CatalogError(ValueError):
    """目录文件级失败（353 VEHICLE_PROFILE_INVALID）：`problems` 每项 `{rule, path, value, message}`。"""

    code = CODE_PROFILE_INVALID

    def __init__(self, problems: list[dict]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(f"{p['rule']} {p.get('path', '')}: {p.get('message', '')}" for p in self.problems))


def problem(rule: str, path: str, value: Any, message: str) -> dict:
    return {"rule": rule, "path": path, "value": value, "message": message}


@cache
def _catalog_validator():
    from jsonschema import Draft202012Validator

    from awr.contracts._paths import contracts_root
    from awr.sim.schemas import contracts_registry

    schema = json.loads((contracts_root() / "vehicle" / "catalog.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=contracts_registry())


def category_of(doc: dict) -> str:
    return CATEGORY_OF_FRAME.get(PF.frame_type_of(doc), "multirotor")


@dataclass
class Catalog:
    """已加载的目录索引与机型文档（不含 M08 的检查；检查见 `checks.py` 与 `check.py`）。"""

    vdir: Path
    doc: dict
    docs: dict[str, tuple[dict, Path]] = field(default_factory=dict)  # profile_id -> (规范化文档, params.yaml)
    sensor_docs: dict[str, tuple[dict, Path]] = field(default_factory=dict)  # model_id -> (型号文档, 路径)
    overlay: Overlay | None = None  # D3 目录覆盖层：只在 D3 进程中加载（模块说明）

    @classmethod
    def load(cls, vdir: Path | str | None = None, *, d3: bool | None = None) -> Catalog:
        """读取 `catalog.json`（schema 失败抛 CatalogError）与目录列出的机型文档、型号文件；一致性问题见 `problems()`。
        `d3`（缺省按 `AWR_D3_ENABLED`）为真时另读 D3 覆盖层并严格校验（任何 OV、SD、LR、CM、RG 问题抛 CatalogError）。"""
        d = Path(vdir) if vdir is not None else PF.vehicles_dir()
        f = d / "catalog.json"
        try:
            cat = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise CatalogError([problem("CI-1", str(f), None, f"目录不可读：{e}")]) from e
        errs = [problem("CI-1", "/".join(map(str, e.absolute_path)) or "<root>", None, e.message[:200])
                for e in _catalog_validator().iter_errors(cat)]
        if errs:
            raise CatalogError(errs)
        try:
            docs = PF.load_vehicle_docs(d)
        except PF.ProfileError as e:
            raise CatalogError([problem("CI-1", e.profile_id, None, p) for p in e.problems]) from e
        sdocs: dict[str, tuple[dict, Path]] = {}
        for sm in cat.get("sensor_models") or []:
            p = d / str(sm["file"])
            try:
                sd = PF.yaml_load(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(sd, dict):
                sdocs[str(sm["id"])] = (sd, p)
        out = cls(d, cat, docs, sdocs)
        if d3_enabled() if d3 is None else d3:
            out.overlay = Overlay.load(out).require_ok()
        return out

    # ------------------------------------------------------------ 查询
    @property
    def version(self) -> str:
        return str(self.doc.get("catalog_version"))

    @property
    def disclaimer_zh(self) -> str:
        return str(self.doc.get("disclaimer_zh") or DISCLAIMER_ZH)

    def entries(self) -> list[dict]:
        return [m for m in self.doc.get("models") or [] if isinstance(m, dict)]

    def entry(self, model_id: str) -> dict | None:
        return next((m for m in self.entries() if m.get("id") == model_id), None)

    def visible_ids(self) -> list[str]:
        """可见机型按 `order` 排序。"""
        vis = [m for m in self.entries() if m.get("visible")]
        return [str(m["id"]) for m in sorted(vis, key=lambda m: (int(m.get("order", 0)), str(m["id"])))]

    def model_dir(self, model_id: str) -> Path | None:
        e = self.entry(model_id)
        if e is not None:
            return self.vdir / str(e["dir"])
        hit = self.docs.get(model_id)
        return hit[1].parent if hit else None

    def pack(self, pack_id: str | None) -> dict | None:
        return next((p for p in self.doc.get("packs") or [] if p.get("pack_id") == pack_id), None)

    def charger(self, model: str | None) -> dict | None:
        return next((c for c in self.doc.get("chargers") or [] if c.get("model") == model), None)

    def sensor_entry(self, model_id: str) -> dict | None:
        return next((s for s in self.doc.get("sensor_models") or [] if s.get("id") == model_id), None)

    # ------------------------------------------------------------ CI-1 至 CI-4
    def problems(self) -> list[dict]:
        out: list[dict] = []
        ids = [str(m.get("id")) for m in self.entries()]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            out.append(problem("CI-1", "models[].id", dup, "机型 id 重复"))
        for m in self.entries():
            mid = str(m["id"])
            f = self.vdir / str(m["dir"]) / "params.yaml"
            if not f.exists():
                out.append(problem("CI-1", f"models[{mid}].dir", m["dir"], f"缺少 {f}"))
                continue
            hit = self.docs.get(mid)
            if hit is None:
                out.append(problem("CI-1", f"models[{mid}].id", mid, f"{f} 中没有 id（或变体）{mid}"))
                continue
            doc = hit[0]
            ref = bool((doc.get("catalog") or {}).get("reference", False))
            for key, cv, pv in (("profile_version", m.get("profile_version"), doc.get("profile_version")),
                                ("display_name", m.get("display_name"), doc.get("display_name")),
                                ("reference", bool(m.get("reference")), ref)):
                if str(cv) != str(pv):
                    out.append(problem("CI-1", f"models[{mid}].{key}", cv, f"与 params.yaml 的 {pv!r} 不一致"))
            cat = category_of(doc)
            if m.get("category") != cat:
                out.append(problem("CI-1", f"models[{mid}].category", m.get("category"), f"frame.type 对应类别为 {cat}"))
            pc = (doc.get("catalog") or {})
            if pc and (pc.get("category") != m.get("category") or
                       (pc.get("size_class") and m.get("size_class") and pc.get("size_class") != m.get("size_class"))):
                out.append(problem("CI-1", f"models[{mid}].category", m.get("category"), "与 params.yaml 的 catalog 字段不一致"))
            out += [problem("CI-1", f"models[{mid}].versions", v, "存档版本文件不存在") for v in m.get("versions") or []
                    if not (self.vdir / str(m["dir"]) / "history" / f"params-{v}.yaml").exists()]
        vis = [m for m in self.entries() if m.get("visible")]
        n_mr = sum(1 for m in vis if m.get("category") == "multirotor")
        n_fw = sum(1 for m in vis if m.get("category") in ("fixed_wing", "vtol"))
        if n_mr < 2 or n_fw < 2:
            out.append(problem("CI-2", "models[]", {"multirotor": n_mr, "fixed_wing_class": n_fw},
                               "可见机型须至少 2 个四旋翼与 2 个固定翼类（R-D2-15）"))
        tok = re.compile(r"\b(" + "|".join(FORBIDDEN_NAME_TOKENS) + r")\b", re.IGNORECASE)
        for m in self.entries():
            if not m.get("reference"):
                continue
            mid = str(m["id"])
            if not mid.startswith("awr_"):
                out.append(problem("CI-3", f"models[{mid}].id", mid, "参考机型 id 须以 awr_ 开头"))
            for key in ("display_name", "display_name_zh"):
                hit = tok.search(str(m.get(key) or ""))
                if hit:
                    out.append(problem("CI-3", f"models[{mid}].{key}", m.get(key), f"显示名含真实产品词 {hit.group(1)!r}"))
        out += self._ci4()
        return out

    def _ci4(self) -> list[dict]:
        out: list[dict] = []
        listed: dict[str, str] = {}
        for sm in self.doc.get("sensor_models") or []:
            sid = str(sm["id"])
            hit = self.sensor_docs.get(sid)
            if hit is None:
                out.append(problem("CI-4", f"sensor_models[{sid}].file", sm.get("file"), "型号文件不存在或不可读"))
                continue
            sd = hit[0]
            if str(sd.get("model_id")) != sid or str(sd.get("model_version")) != str(sm.get("model_version")):
                out.append(problem("CI-4", f"sensor_models[{sid}]", f"{sd.get('model_id')}@{sd.get('model_version')}",
                                   "型号文件的 model_id 或 model_version 与目录不一致"))
            if TYPE_OF_SCHEMA.get(str(sd.get("schema"))) != sm.get("type"):
                out.append(problem("CI-4", f"sensor_models[{sid}].type", sm.get("type"), f"型号文件 schema 为 {sd.get('schema')}"))
            if bool(sd.get("reference", False)) != bool(sm.get("reference")):
                out.append(problem("CI-4", f"sensor_models[{sid}].reference", sm.get("reference"), "与型号文件不一致"))
            listed[sid] = str(sm.get("model_version"))
        packs = {str(p["pack_id"]): p for p in self.doc.get("packs") or []}
        for m in self.entries():
            mid = str(m["id"])
            hit = self.docs.get(mid)
            if hit is None:
                continue
            doc, f = hit
            bat = doc.get("battery")
            if isinstance(bat, dict) and bat.get("pack_id") is not None:
                p = packs.get(str(bat["pack_id"]))
                if p is None:
                    out.append(problem("CI-4", f"{mid}.battery.pack_id", bat["pack_id"], "不在目录 packs[] 中"))
                else:
                    k = int(bat.get("packs_per_swap") or p.get("packs_per_swap") or 1)
                    cap = num(bat.get("capacity_wh"))
                    if int(bat.get("cells", 0)) != int(p["cells"]) or str(bat.get("chemistry")) != str(p["chemistry"]):
                        out.append(problem("CI-4", f"{mid}.battery", bat.get("cells"), f"节数或化学体系与电池包 {p['pack_id']} 不一致"))
                    if cap is not None and p.get("capacity_wh") and abs(cap - k * float(p["capacity_wh"])) > 0.01 * cap:
                        out.append(problem("CI-4", f"{mid}.battery.capacity_wh", cap,
                                           f"与 {k} × {p['capacity_wh']} Wh（{p['pack_id']}）不一致"))
            for slot, v in (doc.get("sensors") or {}).items():
                rel = v.get("file") if isinstance(v, dict) else v
                try:
                    md = PF.yaml_load((f.parent / str(rel)).read_text(encoding="utf-8")) or {}
                except (OSError, ValueError):
                    out.append(problem("CI-4", f"{mid}.sensors.{slot}", rel, "挂载文件不存在或不可读"))
                    continue
                ref = md.get("spec_ref") if isinstance(md, dict) else None
                if ref:
                    sid, _, ver = str(ref).partition("@")
                    if listed.get(sid) != ver:
                        out.append(problem("CI-4", f"{mid}.sensors.{slot}.spec_ref", ref, "引用的型号或版本不在目录 sensor_models[] 中"))
        if not self.doc.get("chargers"):
            out.append(problem("CI-4", "chargers[]", None, "目录没有充电器"))
        return out


# ---------------------------------------------------------------- 能力合成（AWR-20 §4.6）
def capabilities_of(doc: dict, mounted: list[Any] | tuple[Any, ...] = ()) -> list[str]:
    """机型 `capabilities[]` 与 `payload[].capability_id` 与 由已挂载传感器（M13 `SensorSpec`，或带 `schema_id`、`doc` 的对象）
    合成的能力，排序去重。"""
    caps = {str(c) for c in (doc.get("capabilities") or [])}
    caps |= {str(p["capability_id"]) for p in doc.get("payload") or [] if isinstance(p, dict) and p.get("capability_id")}
    cat = category_of(doc)
    if cat in ("multirotor", "vtol"):
        caps.add("flight.hover")
    if cat in ("fixed_wing", "vtol"):
        caps.add("flight.loiter")
    for s in mounted:
        sid = str(getattr(s, "schema_id", "") or (getattr(s, "doc", {}) or {}).get("schema") or "")
        sd = getattr(s, "doc", {}) or {}
        if sid == "awr.sensor.eo_zoom.v1":
            caps.add("rgb.zoom")
            night = sd.get("night") or {}
            if night:
                caps.add("nir.lowlight")
            if isinstance(night, dict) and night.get("illuminator"):
                caps.add("nir.active")
        elif sid == "awr.sensor.nir_illuminator.v1":
            caps.add("nir.active")
        elif sid in ("awr.sensor.lwir.v1", "awr.sensor.thermal.v1"):
            caps.add("thermal.imaging")
        elif sid == "awr.sensor.radar_mmw.v1":
            caps.add("radar.mmw")
        elif sid == "awr.sensor.acoustic_array.v1":
            caps.add("acoustic.array")
        elif sid == "awr.sensor.lidar_livox.v1":
            caps.add("lidar.mapping")
        elif sid == "awr.sensor.camera.v1":
            caps.add("rgb.zoom")
    if "lidar.mapping" in caps and "compute.orin_nx" in caps:
        caps.add("nav.slam3d")
    return sorted(caps)


# ---------------------------------------------------------------- 机巢兼容（MT-8）
def nest_compatible(doc: dict, nest: dict) -> dict[str, Any]:
    """MT-8：`{ok, code, why?, detail?}`；`nest = {kind, chargers[{model?, cells_min?, cells_max}]}`（AWR-04 §8.2）。"""
    cat = category_of(doc)
    kind = str(nest.get("kind") or "box")
    if cat not in NEST_ACCEPTS.get(kind, frozenset()):
        return {"ok": False, "code": CODE_NEST_INCOMPATIBLE, "why": "kind", "detail": {"nest_kind": kind, "category": cat}}
    bat = doc.get("battery")
    cells = int(bat.get("cells", 0)) if isinstance(bat, dict) else 0
    chargers = [c for c in nest.get("chargers") or [] if isinstance(c, dict)]
    if cells and chargers and not any(int(c.get("cells_min", 1)) <= cells <= int(c.get("cells_max", 0)) for c in chargers):
        return {"ok": False, "code": CODE_NEST_INCOMPATIBLE, "why": "cells",
                "detail": {"cells": cells, "cells_max": max(int(c.get("cells_max", 0)) for c in chargers)}}
    return {"ok": True, "code": 0}


# ---------------------------------------------------------------- 工作温度（SN-9、EN-1）
def env_temp_check(doc: dict, mounted: list[Any] | tuple[Any, ...] = (), temp_c: float | None = None) -> dict[str, Any]:
    """机体与已挂载传感器工作温度的交集；`temp_c` 越界时只告警（`521 VEHICLE_ENV_LIMIT`，不拒绝任何命令，AWR-20 EN-1）。
    返回 `{range: [lo, hi] | None, ok, warning?: {code, temp_c, range}}`。"""
    lims: list[tuple[float, float]] = []
    tc = (doc.get("env_limits") or {}).get("temp_c")
    if isinstance(tc, list) and len(tc) == 2:
        lims.append((float(tc[0]), float(tc[1])))
    for s in mounted:
        el = getattr(s, "env_limits", None)
        if el is not None:
            lims.append((float(el[0]), float(el[1])))
    if not lims:
        return {"range": None, "ok": True}
    lo, hi = max(x[0] for x in lims), min(x[1] for x in lims)
    out: dict[str, Any] = {"range": [lo, hi], "ok": lo <= hi}
    if temp_c is not None and not lo <= float(temp_c) <= hi:
        out["warning"] = {"code": CODE_ENV_LIMIT, "temp_c": float(temp_c), "range": [lo, hi]}
    return out


# ================================================================ D3 目录覆盖层（AWR-20 §15.7、§15.8；ADR-157）
D3_ENV = "AWR_D3_ENABLED"
OVERLAY_SCHEMA = "awr.vehicle_catalog_overlay.v1"
VARIANT_SCHEMA = "awr.vehicle_variant.v1"
OVERLAY_DIR = "d3"
OVERLAY_FILE = "catalog.d3.json"
OFFSET_TIME_RULE = "floor(i*1e8/N)"  # 唯一允许的槽位时刻规则（v1；LR-1）
NAV_RATE_HZ = 10.0  # 导航解 10 Hz（M25-FR-094）
EO_D3_RATE_HZ = 10.0  # cam0 缺省帧率 "10"（AWR-20 §15.6.2）
D3_TYPE_OF_SCHEMA = {**TYPE_OF_SCHEMA, "awr.sensor.camera_fixed.v1": "camera_fixed", "awr.sensor.imu_mems.v1": "imu_mems"}
SIDE_TYPE_OF_SCHEMA = {"awr.sensor.lidar_d3.v1": "lidar_d3", "awr.sensor.eo_zoom_render.v1": "eo_zoom_render",
                       "awr.sensor.lwir_render.v1": "lwir_render"}
D3_KIND_OF_SCHEMA = {"awr.sensor.eo_zoom.v1": "camera", "awr.sensor.camera.v1": "camera", "awr.sensor.camera_fixed.v1": "camera",
                     "awr.sensor.lidar_livox.v1": "lidar", "awr.sensor.imu.v1": "imu", "awr.sensor.imu_mems.v1": "imu",
                     "awr.sensor.gnss.v1": "gnss", "awr.sensor.lwir.v1": "thermal"}
SENSOR_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
VREF_RE = re.compile(r"^(?P<id>[a-z0-9_]{1,40})@(?P<ver>[0-9]+\.[0-9]+\.[0-9]+)$")
# 变体 `set` 白名单：AWR-20 §7.6 克隆路径（不含限速档与电池容量，v1 由克隆接口承担）加 capabilities、labels（§15.7.2）
VARIANT_SET_PATHS = ("mass_kg", "acoustic.lwa_hover_dba", "acoustic.lwa_cruise_dba", "acoustic.tonal_db", "visual.contrast_sky",
                     "link.range_m", "capabilities")
VARIANT_SET_PREFIXES = ("labels.",)
VARIANT_LOCKED = ("frame", "geometry", "motor", "prop", "aero", "battery.cells", "battery.chemistry")
IMU_TIERS = ("clean", "realistic", "harsh")
_OVERLAY_SENSOR_KEYS = ("id", "file", "type", "model_version", "reference", "visible")
_OVERLAY_VARIANT_KEYS = ("id", "file", "base", "profile_version", "display_name", "display_name_zh", "category", "reference",
                         "visible", "order", "d3_tags", "summary_zh")
_VARIANT_REQUIRED = ("schema", "id", "base", "profile_version", "display_name", "display_name_zh", "reference", "d3", "conf", "src")
_DERIVED_KEYS = ("mass_kg", "p_hover_w", "hover_endurance_s", "hover_thrust", "twr", "mtow_margin_kg")


def d3_enabled() -> bool:
    """D3 进程标志：`AWR_D3_ENABLED = 1`（runtime `d3` profile 设置，AWR-05 §12.3 扩展点 12）。"""
    return os.environ.get(D3_ENV, "") == "1"


# ---------------------------------------------------------------- 刚体变换（父帧 base_link FLU；rpy 按 ZYX，同 M13 mount_R）
Mat3 = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
Vec3 = tuple[float, float, float]


def rpy_matrix(rpy_deg: Any) -> Mat3:
    """`Rz(yaw)·Ry(pitch)·Rx(roll)`（度），与 `awr.sim.sensors.frames.mount_R` 同一约定；纯 Python，结果确定。"""
    r, p, y = (math.radians(float(v)) for v in rpy_deg)
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return ((cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
            (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
            (-sp, cp * sr, cp * cr))


def _mat(rows: Any) -> Mat3:
    """3×3（嵌套）或按行展开的 9 元列表 -> Mat3。"""
    v = [float(x) for row in rows for x in (row if isinstance(row, (list, tuple)) else [row])]
    if len(v) != 9:
        raise ValueError("旋转矩阵须为 3×3 或 9 元")
    return ((v[0], v[1], v[2]), (v[3], v[4], v[5]), (v[6], v[7], v[8]))


def _mm(a: Mat3, b: Mat3) -> Mat3:
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))  # type: ignore[return-value]


def _mt(a: Mat3) -> Mat3:
    return tuple(tuple(a[j][i] for j in range(3)) for i in range(3))  # type: ignore[return-value]


def _mv(a: Mat3, v: Any) -> Vec3:
    return tuple(sum(a[i][k] * float(v[k]) for k in range(3)) for i in range(3))  # type: ignore[return-value]


def _rpy_of(R: Mat3) -> Vec3:
    """Mat3 -> ZYX rpy（度）；万向锁时 roll 记 0。"""
    sp = max(-1.0, min(1.0, -R[2][0]))
    p = math.asin(sp)
    if abs(sp) > 1.0 - 1e-12:
        return (0.0, math.degrees(p), math.degrees(math.atan2(-R[0][1], R[1][1])))
    return (math.degrees(math.atan2(R[2][1], R[2][2])), math.degrees(p), math.degrees(math.atan2(R[1][0], R[0][0])))


def _clean_num(x: float, nd: int = 9) -> float:
    v = round(float(x), nd)
    return 0.0 if v == 0 else v


@dataclass(frozen=True)
class RigSensor:
    """D3 rig 的一个传感器（AWR-20 §15.3；17 §18.5.2 `d3_rig[]`）。挂载为标称值（`mount_nominal`），父帧 base_link（FLU）。"""

    sensor_id: str
    sensor_no: int
    kind: str  # camera | lidar | imu | gnss | nav | thermal
    slot: str | None  # 目录槽位；内置 IMU 与导航解为 None
    model_ref: str | None  # `<model_id>@<version>`；D1 文件（imu.yaml、gnss.yaml）为 None
    role: str  # agent | slam（17 §18.5.2 的单值口径）
    roles: tuple[str, ...]
    default: bool
    removable: bool
    rate_hz: float | None
    controllable: bool
    agent_delivered: bool
    xyz_m: Vec3 = (0.0, 0.0, 0.0)
    rpy_deg: Vec3 = (0.0, 0.0, 0.0)
    R: Mat3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))  # 机体 <- 传感器
    mount_preset: str | None = None
    file: str | None = None
    parent: str | None = None  # 内置 IMU 所属的 LiDAR sensor_id
    gimbal: bool = False

    def row(self) -> dict[str, Any]:
        """17 §18.5.2 的 `d3_rig[]` 行。"""
        return {"sensor_id": self.sensor_id, "slot": self.slot, "kind": self.kind, "model_ref": self.model_ref, "role": self.role,
                "default": self.default, "rate_hz": self.rate_hz, "controllable": self.controllable}

    def mount(self) -> dict[str, Any]:
        out: dict[str, Any] = {"xyz_m": list(self.xyz_m), "rpy_deg": list(self.rpy_deg)}
        if self.mount_preset:
            out["preset"] = self.mount_preset
        return out


@dataclass
class Variant:
    """已解析的机型变体：有效机型文档（`awr.vehicle.v1`，通过 D2 schema 与检查）、D3 rig 与重算的派生值。"""

    id: str
    entry: dict  # catalog.d3.json 的 variants[] 条目
    spec: dict  # 变体文件（awr.vehicle_variant.v1）
    path: Path
    base_ref: str
    base_dir: Path  # 基机型包目录（有效文档中相对路径的基准）
    doc: dict
    rig: tuple[RigSensor, ...]
    derived: dict[str, float | None]
    delta: dict[str, float]  # {dm_kg, dp_w}：挂载变化相对基机型缺省挂载
    lidar_d3: dict[str, dict] = field(default_factory=dict)  # lidar sensor_id -> 合并后的 D3 LiDAR 参数（D2 型号 + 侧表）
    problems: list[dict] = field(default_factory=list)

    @property
    def tags(self) -> list[str]:
        return [str(t) for t in ((self.spec.get("d3") or {}).get("tags") or [])]

    @property
    def rig_order(self) -> list[str]:
        return [str(t) for t in ((self.spec.get("d3") or {}).get("rig_order") or [])]

    def sensor(self, sensor_id: str) -> RigSensor:
        hit = next((s for s in self.rig if s.sensor_id == sensor_id), None)
        if hit is None:
            raise KeyError(sensor_id)
        return hit

    def rig_rows(self) -> list[dict[str, Any]]:
        return [s.row() for s in self.rig]

    def lidar_imu_extrinsics(self, lidar_id: str = "lidar0") -> dict[str, Any]:
        """LiDAR 与其内置 IMU 的外参（FAST-LIO / FAST-LIVO2 语义）：

        - `extrinsic_T`、`extrinsic_R`（按行展开）：LiDAR 在内置 IMU 系中的位置与姿态，= 侧表 `lidar_in_imu`（与机体挂载无关，
          倒装时随 LiDAR 一同倒装，因此不变）；
        - `T_B_L`：LiDAR 在机体系中的标称挂载；`T_B_I = T_B_L · T_I_L⁻¹`：内置 IMU 在机体系中的位姿。"""
        L = self.sensor(lidar_id)
        d = self.lidar_d3.get(lidar_id) or {}
        R_IL, t_IL = _lidar_in_imu(d)
        R_BI = _mm(L.R, _mt(R_IL))
        t_BI = tuple(L.xyz_m[i] - _mv(R_BI, t_IL)[i] for i in range(3))
        return {"extrinsic_T": [_clean_num(x) for x in t_IL], "extrinsic_R": [_clean_num(x) for r in R_IL for x in r],
                "T_B_L": {"R": [[_clean_num(x) for x in r] for r in L.R], "t": [_clean_num(x) for x in L.xyz_m]},
                "T_B_I": {"R": [[_clean_num(x) for x in r] for r in R_BI], "t": [_clean_num(x) for x in t_BI]}}


def _lidar_in_imu(d: dict) -> tuple[Mat3, Vec3]:
    lii = d.get("lidar_in_imu") or (d.get("imu") or {}).get("lidar_in_imu") or {}
    t = tuple(float(x) for x in (lii.get("t") or (0.0, 0.0, 0.0)))
    R = _mat(lii["R"]) if lii.get("R") is not None else rpy_matrix(lii.get("rpy_deg") or (0.0, 0.0, 0.0))
    return R, t  # type: ignore[return-value]


def _get_path(d: Any, path: str) -> Any:
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    """`set` 的嵌套对象或点路径 -> {点路径: 值}；值对象 `{value, ...}` 与列表作为整体。"""
    out: dict[str, Any] = {}
    for k, v in (d or {}).items():
        p = f"{prefix}{k}"
        if isinstance(v, dict) and "value" not in v and v:
            out.update(_flatten(v, p + "."))
        else:
            out[p] = v
    return out


def _assign(doc: dict, path: str, v: Any) -> None:
    parts = path.split(".")
    cur = doc
    for k in parts[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    old = cur.get(parts[-1])
    if isinstance(old, dict) and "value" in old and not isinstance(v, dict):
        cur[parts[-1]] = {**old, "value": v, "conf": "D", "src": "D3 机型变体（set）"}
    else:
        cur[parts[-1]] = copy.deepcopy(v)


def _set_derived(d: dict, key: str, v: float, src: str) -> None:
    cur = d.get(key)
    d[key] = {**cur, "value": v, "conf": "D", "src": src} if isinstance(cur, dict) and "value" in cur else v


def _slot_default(v: Any) -> bool:
    return True if not isinstance(v, dict) else bool(v.get("default", False))


def _slot_removable(v: Any) -> bool:
    return True if not isinstance(v, dict) else bool(v.get("removable", True))


def _slot_file(v: Any) -> str | None:
    rel = v.get("file") if isinstance(v, dict) else v
    return str(rel) if rel else None


def imu_noise_tier(imu_doc: dict, tier: str, *, bpf_hz: float | None = None) -> dict[str, Any]:
    """内置 IMU 型号（`awr.sensor.imu_mems.v1`）在噪声档 `tier` 下的参数（AWR-20 §15.6.5 单一真源）：clean 全部为 0；realistic
    取型号文件；harsh 为 realistic 加振动（频率 `bpf_hz` 取机型 `acoustic.bpf_hz`，幅值取 `vibration_harsh.amp_mps2`）。
    键：`acc_sigma_sample_g`、`gyro_sigma_sample_rps`、`acc_bias_rw_g_rts`、`gyro_bias_rw_rps_rts`、`turn_on_acc_mps2`、
    `turn_on_gyro_dps`、`vib_hz`、`vib_amp_mps2`（无振动为 None 与 0）、`rate_hz`、`acc_unit_export`、`aa_cutoff_hz`。"""
    if tier not in IMU_TIERS:
        raise ValueError(f"噪声档 {tier!r} 不在 {IMU_TIERS} 中")
    n = imu_doc.get("noise") or {}
    rw = n.get("bias_rw") or {}
    on = n.get("turn_on_sigma") or {}
    real = {"acc_sigma_sample_g": float(n.get("acc_sigma_sample_g", 0.0)), "gyro_sigma_sample_rps": float(n.get("gyro_sigma_sample_rps", 0.0)),
            "acc_bias_rw_g_rts": float(rw.get("acc_g_rts", 0.0)), "gyro_bias_rw_rps_rts": float(rw.get("gyro_rps_rts", 0.0)),
            "turn_on_acc_mps2": float(on.get("acc_mps2", 0.0)), "turn_on_gyro_dps": float(on.get("gyro_dps", 0.0))}
    out: dict[str, Any] = {k: (0.0 if tier == "clean" else v) for k, v in real.items()}
    vib = imu_doc.get("vibration_harsh") or {}
    if tier == "harsh" and vib:
        if bpf_hz is None:
            raise ValueError("harsh 档振动需要机型 acoustic.bpf_hz")
        out["vib_hz"], out["vib_amp_mps2"] = float(bpf_hz), float(vib.get("amp_mps2", 0.0))
    else:
        out["vib_hz"], out["vib_amp_mps2"] = None, 0.0
    out.update({"rate_hz": int(imu_doc.get("rate_hz", 200)), "acc_unit_export": str(imu_doc.get("acc_unit_export", "g")),
                "aa_cutoff_hz": float(imu_doc.get("aa_cutoff_hz", 80.0)), "tier": tier})
    return out


def _overlay_shape(doc: Any, f: Path) -> list[dict]:
    """`awr.vehicle_catalog_overlay.v1` 的结构（OV-1；契约 schema 待登记，见 AWR-20 §15.7.1 字段表）。"""
    if not isinstance(doc, dict):
        return [problem("OV-1", str(f), None, "覆盖层不是 JSON 对象")]
    out: list[dict] = []
    if doc.get("schema") != OVERLAY_SCHEMA:
        out.append(problem("OV-1", "schema", doc.get("schema"), f"须为 {OVERLAY_SCHEMA}"))
    out += [problem("OV-1", k, doc.get(k), "须为 semver") for k in ("overlay_version", "base_catalog_version")
            if not SEMVER_RE.match(str(doc.get(k, "")))]
    for key, req in (("sensor_models", _OVERLAY_SENSOR_KEYS), ("side_tables", ("for", "file", "type")),
                     ("variants", _OVERLAY_VARIANT_KEYS)):
        items = doc.get(key)
        if not isinstance(items, list):
            out.append(problem("OV-1", f"{key}[]", None, "缺少数组"))
            continue
        for i, it in enumerate(items):
            miss = [k for k in req if not isinstance(it, dict) or k not in it]
            if miss:
                out.append(problem("OV-1", f"{key}[{i}]", miss, "缺少必填字段"))
    return out


@dataclass
class Overlay:
    """D3 目录覆盖层（`vehicles/d3/`）：型号、侧表与机型变体；只由 D3 加载器读取（模块说明）。"""

    odir: Path
    doc: dict
    catalog: Catalog
    sensor_docs: dict[str, tuple[dict, Path]] = field(default_factory=dict)  # model_id -> (型号文档, 路径)
    side_tables: dict[str, tuple[dict, Path]] = field(default_factory=dict)  # "<model_id>@<version>" -> (侧表, 路径)
    variants: dict[str, Variant] = field(default_factory=dict)
    load_problems: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, catalog: Catalog, odir: Path | str | None = None) -> Overlay:
        """读取覆盖层与其登记的文件；不可读或结构不符抛 CatalogError（OV-1）；其余问题见 `problems()`。"""
        d = Path(odir) if odir is not None else catalog.vdir / OVERLAY_DIR
        f = d / OVERLAY_FILE
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise CatalogError([problem("OV-1", str(f), None, f"覆盖层不可读：{e}")]) from e
        errs = _overlay_shape(doc, f)
        if errs:
            raise CatalogError(errs)
        ov = cls(d, doc, catalog)
        for sm in doc["sensor_models"]:
            p = d / str(sm["file"])
            try:
                sd = PF.yaml_load(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                ov.load_problems.append(problem("OV-1", f"sensor_models[{sm['id']}].file", sm["file"], f"型号文件不可读：{e}"))
                continue
            if isinstance(sd, dict):
                ov.sensor_docs[str(sm["id"])] = (sd, p)
        for st in doc["side_tables"]:
            p = d / str(st["file"])
            try:
                sd = PF.yaml_load(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                ov.load_problems.append(problem("OV-1", f"side_tables[{st['for']}].file", st["file"], f"侧表不可读：{e}"))
                continue
            if isinstance(sd, dict):
                ov.side_tables[str(st["for"])] = (sd, p)
        for e in doc["variants"]:
            v = ov._resolve_variant(e)
            if v is not None:
                ov.variants[v.id] = v
        return ov

    # ------------------------------------------------------------ 查询
    @property
    def version(self) -> str:
        return str(self.doc.get("overlay_version"))

    def entries(self) -> list[dict]:
        return [v for v in self.doc.get("variants") or [] if isinstance(v, dict)]

    def visible_ids(self) -> list[str]:
        vis = [v for v in self.entries() if v.get("visible")]
        return [str(v["id"]) for v in sorted(vis, key=lambda v: (int(v.get("order", 0)), str(v["id"])))]

    def variant(self, vid: str) -> Variant:
        return self.variants[vid]

    def sensor_model(self, ref: str) -> tuple[dict, Path] | None:
        """`<model_id>@<version>` -> (型号文档, 路径)：先查覆盖层，再查 D2 目录型号库；版本须一致。"""
        m = VREF_RE.match(str(ref))
        if m is None:
            return None
        for table in (self.sensor_docs, self.catalog.sensor_docs):
            hit = table.get(m.group("id"))
            if hit is not None and str(hit[0].get("model_version")) == m.group("ver"):
                return hit
        return None

    def lidar_d3(self, model_ref: str) -> dict | None:
        """LiDAR 的 D3 参数：D2 型号 + 侧表（侧表字段覆盖，`for` 精确匹配），或覆盖层自带 D3 字段的型号（如 Avia，§15.7.3）。"""
        st = self.side_tables.get(model_ref)
        hit = self.sensor_model(model_ref)
        if st is not None:
            base = copy.deepcopy(hit[0]) if hit is not None else {}
            for k, v in st[0].items():
                if k not in ("schema", "for", "conf", "src"):
                    base[k] = copy.deepcopy(v)
            base["side_table"] = {"schema": st[0].get("schema"), "file": str(st[1]), "conf": st[0].get("conf")}
            return base
        if hit is not None and isinstance((hit[0].get("pattern") or {}).get("slots_per_frame"), int):
            return copy.deepcopy(hit[0])
        return None

    # ------------------------------------------------------------ 变体解析（§15.7.2）
    def _mount_doc(self, path: Path) -> tuple[dict | None, dict | None]:
        """挂载文件 -> (挂载文档, 引用的型号文档 | None)。"""
        try:
            md = PF.yaml_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None, None
        if not isinstance(md, dict):
            return None, None
        ref = md.get("spec_ref")
        hit = self.sensor_model(str(ref)) if ref else None
        return md, (hit[0] if hit else None)

    def _resolve_variant(self, e: dict) -> Variant | None:
        vid = str(e["id"])
        f = self.odir / str(e["file"])
        probs: list[dict] = []
        try:
            spec = PF.yaml_load(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as ex:
            self.load_problems.append(problem("OV-2", f"variants[{vid}].file", e["file"], f"变体文件不可读：{ex}"))
            return None
        if not isinstance(spec, dict):
            self.load_problems.append(problem("OV-2", f"variants[{vid}].file", e["file"], "变体文件不是映射"))
            return None
        miss = [k for k in _VARIANT_REQUIRED if k not in spec]
        if miss or spec.get("schema") != VARIANT_SCHEMA:
            self.load_problems.append(problem("OV-2", f"{vid}", miss or spec.get("schema"), f"变体文件须为 {VARIANT_SCHEMA} 且含必填字段"))
            return None
        base_ref = str(spec["base"])
        m = VREF_RE.match(base_ref)
        hit = self.catalog.docs.get(m.group("id")) if m else None
        if m is None or hit is None or str(hit[0].get("profile_version")) != m.group("ver"):
            self.load_problems.append(problem("OV-2", f"{vid}.base", base_ref, "基机型须精确解析到 D2 目录中的机型与当前版本"))
            return None
        base_doc, base_f = hit
        base_dir = base_f.parent
        if category_of(base_doc) != "multirotor":
            self.load_problems.append(problem("OV-2", f"{vid}.base", base_ref, "v1 变体只支持多旋翼基机型（§15.5）"))
            return None
        doc = copy.deepcopy(base_doc)
        # ---- set（白名单）
        for path, val in sorted(_flatten(spec.get("set") or {}).items()):
            if any(path == k or path.startswith(k + ".") for k in VARIANT_LOCKED):
                probs.append(problem("OV-2", f"{vid}.set.{path}", val, "不可改变构型、几何、动力、气动、电池节数与化学体系"))
            elif path in VARIANT_SET_PATHS or path.startswith(VARIANT_SET_PREFIXES):
                _assign(doc, path, val)
            else:
                probs.append(problem("OV-2", f"{vid}.set.{path}", val, "不在变体 set 白名单内（AWR-20 §7.6、§15.7.2）"))
        # ---- 挂载与移除
        smap: dict[str, Any] = dict(doc.get("sensors") or {})
        mounts: dict[str, dict] = {str(k): dict(v or {}) for k, v in (spec.get("mounts") or {}).items()}
        remove = [str(x) for x in spec.get("remove") or []]
        dm = dp = 0.0
        slots: dict[str, dict[str, Any]] = {}  # 槽位 -> {file, doc, model, default, removable, base_default, changed, ov}
        for slot, v in smap.items():
            rel = _slot_file(v)
            p = base_dir / rel if rel else None
            md, model = self._mount_doc(p) if p is not None else (None, None)
            slots[slot] = {"file": p, "doc": md, "model": model, "default": _slot_default(v), "removable": _slot_removable(v),
                           "base_default": _slot_default(v), "changed": False, "ov": {}, "new": False}
        for slot in remove:
            s = slots.get(slot)
            if s is None:
                probs.append(problem("OV-2", f"{vid}.remove", slot, "基机型没有该槽位"))
            elif not s["removable"]:
                probs.append(problem("OV-2", f"{vid}.remove", slot, "不可拆槽位不能移除"))
            elif slot in mounts:
                probs.append(problem("OV-2", f"{vid}.remove", slot, "同一槽位不能同时移除与改挂"))
        for slot, o in mounts.items():
            s = slots.get(slot)
            if s is None:
                if not o.get("file"):
                    probs.append(problem("OV-2", f"{vid}.mounts.{slot}", None, "新增槽位须给出挂载文件"))
                    continue
                s = {"file": None, "doc": None, "model": None, "default": False, "removable": True, "base_default": False,
                     "changed": True, "ov": {}, "new": True}
                slots[slot] = s
            if o.get("file"):
                p = (f.parent / str(o["file"])).resolve()
                md, model = self._mount_doc(p)
                if md is None:
                    probs.append(problem("OV-2", f"{vid}.mounts.{slot}.file", o["file"], "挂载文件不存在或不可读"))
                    continue
                if md.get("spec_ref") and model is None:
                    probs.append(problem("OV-2", f"{vid}.mounts.{slot}.spec_ref", md.get("spec_ref"), "引用的型号不在覆盖层或 D2 型号库中"))
                s.update({"file": p, "doc": md, "model": model, "changed": True})
            if o.get("spec_ref"):
                hm = self.sensor_model(str(o["spec_ref"]))
                if hm is None:
                    probs.append(problem("OV-2", f"{vid}.mounts.{slot}.spec_ref", o["spec_ref"], "引用的型号不在覆盖层或 D2 型号库中"))
                elif s["model"] is None:
                    s["model"] = hm[0]
            if "default" in o:
                s["default"] = bool(o["default"])
            if "removable" in o:
                s["removable"] = bool(o["removable"])
            s["ov"] = o
        for slot in remove:
            if slot in slots and slots[slot]["removable"] and slot not in mounts:
                slots[slot]["removed"] = True
        for slot, s in slots.items():  # 缺省挂载状态变化的槽位计入 Δm、ΔP_e（新增槽位的 base_default 为 False）
            on_new = False if s.get("removed") else bool(s["default"])
            if on_new == bool(s["base_default"]):
                continue
            mass = num((s["doc"] or {}).get("mass_kg"), num((s["model"] or {}).get("mass_kg")))
            power = num((s["doc"] or {}).get("power_w"), num((s["model"] or {}).get("power_w"), 0.0))
            if mass is None:
                probs.append(problem("OV-3", f"{vid}.mounts.{slot}", None, "缺省挂载变化的槽位缺少质量（挂载文件或型号）"))
                continue
            sign = 1.0 if on_new else -1.0
            dm += sign * float(mass)
            dp += sign * float(power or 0.0)
        new_smap: dict[str, Any] = {}
        for slot, v in smap.items():
            s = slots[slot]
            if s.get("removed"):
                continue
            if isinstance(v, dict) and bool(s["default"]) != bool(s["base_default"]):
                v = {**v, "default": bool(s["default"])}
            new_smap[slot] = v
        if smap:
            doc["sensors"] = new_smap
        # ---- 质量与能量（§5.5）、派生值与标识
        corr = D.mass_energy_correction(doc, dm, dp)
        m1 = float(corr["mass_kg"])
        src = f"派生：{base_ref} 加挂载变化 Δm {dm:+.4g} kg、ΔP_e {dp:+.4g} W（AWR-20 §5.5、§15.5）"
        if abs(dm) > 1e-12:
            _set_derived(doc, "mass_kg", round(m1, 6), src)
        bat = doc.get("battery") if isinstance(doc.get("battery"), dict) else None
        if bat is not None and corr.get("p_hover_w") is not None and (abs(dm) > 1e-12 or dp):
            _set_derived(bat, "p_hover_w", round(float(corr["p_hover_w"]), 4), src)
            if bat.get("hover_endurance_s") is not None and corr.get("hover_s") is not None:
                _set_derived(bat, "hover_endurance_s", round(float(corr["hover_s"]), 3), src)
        dv = PF.derived_values(doc)
        der = dict(doc.get("derived") or {})
        for k in ("hover_thrust", "twr", "tilt_at_8mps_deg", "cda_per_mass_m2kg", "endurance_check_s"):
            if dv.get(k) is not None:
                der[k] = round(float(dv[k]), 6)
        doc["derived"] = der
        doc["id"] = vid
        doc["profile_version"] = str(spec["profile_version"])
        doc["display_name"] = str(spec["display_name"])
        cat = dict(doc.get("catalog") or {})
        cat["reference"] = bool(spec.get("reference", False))
        cat["category"] = category_of(doc)
        doc["catalog"] = cat
        mtow = num(doc.get("mtow_kg"))
        derived = {"mass_kg": round(m1, 6), "p_hover_w": num((doc.get("battery") or {}).get("p_hover_w")),
                   "hover_endurance_s": num((doc.get("battery") or {}).get("hover_endurance_s")),
                   "hover_thrust": dv.get("hover_thrust"), "twr": dv.get("twr"),
                   "mtow_margin_kg": None if mtow is None else round(mtow - m1, 6)}
        v = Variant(vid, e, spec, f, base_ref, base_dir, doc, (), derived, {"dm_kg": dm, "dp_w": dp}, problems=probs)
        v.rig, rig_probs = self._build_rig(v, slots)
        v.problems += rig_probs
        return v

    def _build_rig(self, v: Variant, slots: dict[str, dict[str, Any]]) -> tuple[tuple[RigSensor, ...], list[dict]]:
        """D3 rig（§15.3）：挂载里带 `d3_sensor_id` 的槽位、带 `imu_ref` 的 LiDAR 的内置 IMU（`<lidar>_imu`）与导航解 `nav`；
        `sensor_no` 为 `d3.rig_order` 中的位置（RG-2）。"""
        probs: list[dict] = []
        found: list[dict[str, Any]] = []
        for slot, s in slots.items():
            sid = (s.get("ov") or {}).get("d3_sensor_id")
            if not sid or s.get("removed"):
                continue
            md, model, o = s["doc"] or {}, s["model"] or {}, s["ov"]
            schema = str(md.get("schema") or model.get("schema") or "")
            kind = D3_KIND_OF_SCHEMA.get(schema)
            if kind is None:
                probs.append(problem("RG-2", f"{v.id}.mounts.{slot}", schema, "D3 rig 不支持该传感器类型"))
                continue
            model_ref = o.get("spec_ref") or md.get("spec_ref")
            if model_ref is None and model.get("model_id"):
                model_ref = f"{model['model_id']}@{model['model_version']}"
            preset = o.get("mount_preset")
            if preset:
                presets = md.get("mount_presets") or model.get("mount_presets") or {}
                mt = presets.get(preset)
                if not isinstance(mt, dict):
                    probs.append(problem("OV-2", f"{v.id}.mounts.{slot}.mount_preset", preset, "挂载文件没有登记该预设"))
                    mt = {}
            else:
                mt = o.get("mount") or md.get("mount") or model.get("mount") or {}
            xyz = tuple(float(x) for x in (mt.get("xyz_m") or (0.0, 0.0, 0.0)))
            rpy = tuple(float(x) for x in (mt.get("rpy_deg") or (0.0, 0.0, 0.0)))
            roles: tuple[str, ...]
            if o.get("role"):
                roles = (str(o["role"]),)
            elif schema == "awr.sensor.camera_fixed.v1":
                roles = tuple(str(r) for r in (model.get("role_allowed") or md.get("role_allowed") or ["slam"]))
            else:
                roles = ("agent",)
            if kind == "lidar":
                ld = self.lidar_d3(str(model_ref)) if model_ref else None
                rate = num((ld or {}).get("frame_rate_hz"), num(((ld or {}).get("pattern") or {}).get("frame_rate_hz"),
                                                              num(md.get("frame_rate_hz"))))
            elif schema == "awr.sensor.camera_fixed.v1":
                rate = num(md.get("fps"), num(model.get("fps")))
            elif schema == "awr.sensor.eo_zoom.v1":
                rate = EO_D3_RATE_HZ
            else:
                rate = num(md.get("rate_hz"), num(model.get("rate_hz")))
            gimbal = bool(md.get("gimbal") or model.get("gimbal"))
            row = {"sensor_id": str(sid), "kind": kind, "slot": slot, "model_ref": model_ref, "roles": roles,
                   "default": bool(s["default"]), "removable": bool(s["removable"]), "rate_hz": rate,
                   "controllable": schema == "awr.sensor.eo_zoom.v1" and gimbal, "agent_delivered": "agent" in roles,
                   "xyz_m": xyz, "rpy_deg": rpy, "R": rpy_matrix(rpy), "mount_preset": preset or None,
                   "file": str(s["file"]) if s["file"] else None, "parent": None, "gimbal": gimbal}
            found.append(row)
            if kind == "lidar" and model_ref:
                ld = self.lidar_d3(str(model_ref))
                if ld is None:
                    continue
                v.lidar_d3[str(sid)] = ld
                imu_ref = ld.get("imu_ref")
                imu = self.sensor_model(str(imu_ref)) if imu_ref else None
                if imu_ref is None:
                    continue
                R_IL, t_IL = _lidar_in_imu(ld)
                R_BI = _mm(row["R"], _mt(R_IL))
                t_BI = tuple(xyz[i] - _mv(R_BI, t_IL)[i] for i in range(3))
                found.append({"sensor_id": f"{sid}_imu", "kind": "imu", "slot": None, "model_ref": str(imu_ref),
                              "roles": ("agent", "slam"), "default": row["default"], "removable": row["removable"],
                              "rate_hz": num((imu or ({}, None))[0].get("rate_hz")), "controllable": False,
                              "agent_delivered": True, "xyz_m": tuple(_clean_num(x) for x in t_BI),
                              "rpy_deg": tuple(_clean_num(x) for x in _rpy_of(R_BI)), "R": R_BI, "mount_preset": None,
                              "file": None, "parent": str(sid), "gimbal": False})
        found.append({"sensor_id": "nav", "kind": "nav", "slot": None, "model_ref": None, "roles": ("agent",), "default": True,
                      "removable": False, "rate_hz": NAV_RATE_HZ, "controllable": False, "agent_delivered": True,
                      "xyz_m": (0.0, 0.0, 0.0), "rpy_deg": (0.0, 0.0, 0.0), "R": rpy_matrix((0, 0, 0)), "mount_preset": None,
                      "file": None, "parent": None, "gimbal": False})
        ids = [r["sensor_id"] for r in found]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            probs.append(problem("RG-2", f"{v.id}.rig", dup, "D3 sensor_id 在机型内重复"))
        bad = [i for i in ids if not SENSOR_ID_RE.match(i)]
        if bad:
            probs.append(problem("RG-2", f"{v.id}.rig", bad, "D3 sensor_id 须符合 ^[a-z][a-z0-9_]{0,23}$"))
        order = v.rig_order
        if sorted(order) != sorted(set(ids)) or len(order) != len(set(order)):
            probs.append(problem("RG-2", f"{v.id}.d3.rig_order", order, f"rig_order 须恰好覆盖全部 D3 传感器 {sorted(set(ids))}"))
        pos = {sid: i for i, sid in enumerate(order)}
        found.sort(key=lambda r: (pos.get(r["sensor_id"], len(order)), r["sensor_id"]))
        rig = tuple(RigSensor(sensor_id=r["sensor_id"], sensor_no=i, kind=r["kind"], slot=r["slot"], model_ref=r["model_ref"],
                              role=r["roles"][0], roles=tuple(r["roles"]), default=r["default"], removable=r["removable"],
                              rate_hz=r["rate_hz"], controllable=r["controllable"], agent_delivered=r["agent_delivered"],
                              xyz_m=tuple(_clean_num(x) for x in r["xyz_m"]), rpy_deg=tuple(_clean_num(x) for x in r["rpy_deg"]),
                              R=r["R"], mount_preset=r["mount_preset"], file=r["file"], parent=r["parent"], gimbal=r["gimbal"])
                    for i, r in enumerate(found))
        return rig, probs

    # ------------------------------------------------------------ OV、SD、LR、CM、RG（§15.8）
    def problems(self) -> list[dict]:
        out: list[dict] = list(self.load_problems)
        cat = self.catalog
        if str(self.doc.get("base_catalog_version")) != cat.version:
            out.append(problem("OV-1", "base_catalog_version", self.doc.get("base_catalog_version"),
                               f"须等于 D2 目录版本 {cat.version}"))
        d2_ids = {str(m.get("id")) for m in cat.entries()} | set(cat.docs)
        d2_sensors = {str(s.get("id")) for s in cat.doc.get("sensor_models") or []}
        vids = [str(v["id"]) for v in self.entries()]
        sids = [str(s["id"]) for s in self.doc.get("sensor_models") or []]
        for kind, ids, taken in (("variants", vids, d2_ids), ("sensor_models", sids, d2_sensors)):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            if dup:
                out.append(problem("OV-1", f"{kind}[].id", dup, "覆盖层 id 重复"))
            clash = sorted(set(ids) & taken)
            if clash:
                out.append(problem("OV-1", f"{kind}[].id", clash, "覆盖层 id 与 D2 目录重复"))
        tok = re.compile(r"\b(" + "|".join(FORBIDDEN_NAME_TOKENS) + r")\b", re.IGNORECASE)
        for sm in self.doc.get("sensor_models") or []:
            sid = str(sm["id"])
            hit = self.sensor_docs.get(sid)
            if hit is None:
                continue
            sd = hit[0]
            if str(sd.get("model_id")) != sid or str(sd.get("model_version")) != str(sm.get("model_version")):
                out.append(problem("OV-1", f"sensor_models[{sid}]", f"{sd.get('model_id')}@{sd.get('model_version')}",
                                   "型号文件的 model_id 或 model_version 与覆盖层不一致"))
            if D3_TYPE_OF_SCHEMA.get(str(sd.get("schema"))) != sm.get("type"):
                out.append(problem("OV-1", f"sensor_models[{sid}].type", sm.get("type"), f"型号文件 schema 为 {sd.get('schema')}"))
            if bool(sd.get("reference", False)) != bool(sm.get("reference")):
                out.append(problem("OV-1", f"sensor_models[{sid}].reference", sm.get("reference"), "与型号文件不一致"))
            if sd.get("reference"):
                for key in ("display_name", "display_name_zh"):
                    h = tok.search(str(sd.get(key) or ""))
                    if h:
                        out.append(problem("CI-3", f"sensor_models[{sid}].{key}", sd.get(key), f"显示名含真实产品词 {h.group(1)!r}"))
            if sd.get("schema") == "awr.sensor.camera_fixed.v1":
                out += _cm1(sid, sd)
            if sd.get("schema") == "awr.sensor.lidar_livox.v1" and (sd.get("pattern") or {}).get("slots_per_frame") is not None:
                out += self._lidar_rules(f"sensor_models[{sid}]", sd)
        for st in self.doc.get("side_tables") or []:
            ref = str(st["for"])
            hit = self.side_tables.get(ref)
            if hit is None:
                continue
            sd = hit[0]
            if SIDE_TYPE_OF_SCHEMA.get(str(sd.get("schema"))) != st.get("type"):
                out.append(problem("OV-1", f"side_tables[{ref}].type", st.get("type"), f"侧表 schema 为 {sd.get('schema')}"))
            if str(sd.get("for")) != ref:
                out.append(problem("OV-1", f"side_tables[{ref}].for", sd.get("for"), "侧表的 for 与覆盖层不一致"))
            base = self.sensor_model(ref)
            if base is None:
                out.append(problem("OV-1", f"side_tables[{ref}].for", ref, "侧表引用的型号或版本不在 D2 目录或覆盖层中"))
                continue
            if sd.get("schema") == "awr.sensor.lidar_d3.v1":
                out += _sd1(ref, sd, base[0])
                merged = self.lidar_d3(ref)
                if merged is not None:
                    out += self._lidar_rules(f"side_tables[{ref}]", merged)
        for e in self.entries():
            vid = str(e["id"])
            v = self.variants.get(vid)
            if v is None:
                continue
            out += v.problems
            spec = v.spec
            for key in ("id", "base", "profile_version", "display_name", "display_name_zh"):
                if str(spec.get(key)) != str(e.get(key)):
                    out.append(problem("OV-1", f"variants[{vid}].{key}", e.get(key), f"与变体文件的 {spec.get(key)!r} 不一致"))
            if bool(spec.get("reference", False)) != bool(e.get("reference")):
                out.append(problem("OV-1", f"variants[{vid}].reference", e.get("reference"), "与变体文件不一致"))
            if sorted(str(t) for t in e.get("d3_tags") or []) != sorted(v.tags):
                out.append(problem("OV-1", f"variants[{vid}].d3_tags", e.get("d3_tags"), f"与变体文件 d3.tags {v.tags} 不一致"))
            if e.get("category") != category_of(v.doc):
                out.append(problem("OV-1", f"variants[{vid}].category", e.get("category"), f"frame.type 对应类别为 {category_of(v.doc)}"))
            if e.get("reference"):
                if not vid.startswith("awr_"):
                    out.append(problem("CI-3", f"variants[{vid}].id", vid, "参考机型 id 须以 awr_ 开头"))
                for key in ("display_name", "display_name_zh"):
                    h = tok.search(str(e.get(key) or ""))
                    if h:
                        out.append(problem("CI-3", f"variants[{vid}].{key}", e.get(key), f"显示名含真实产品词 {h.group(1)!r}"))
            out += self._ov3(v)
            out += self._rg1(v)
        return out

    def require_ok(self) -> Overlay:
        """D3 加载器的严格模式：有任何问题即抛 CatalogError（353，D3 进程拒绝启动；OV-1 至 OV-3）。"""
        probs = self.problems()
        if probs:
            raise CatalogError(probs)
        return self

    def _ov3(self, v: Variant) -> list[dict]:
        """OV-3：合并后的有效文档重跑 D2 的 schema、VH、MT、SN 检查（M08 `validate_doc`），派生值与变体文件 `derived` ≤ 1%。"""
        out: list[dict] = []
        _checks, _d, probs = PF.validate_doc(v.doc, pid=v.id, vdir=v.base_dir)
        out += [problem("OV-3", v.id, None, p) for p in probs]
        for k, want in (v.spec.get("derived") or {}).items():
            if k not in _DERIVED_KEYS:
                out.append(problem("OV-3", f"{v.id}.derived.{k}", want, f"未知派生量（{', '.join(_DERIVED_KEYS)}）"))
                continue
            calc = v.derived.get(k)
            if calc is None or not isinstance(want, (int, float)) or abs(float(want) - calc) > 0.01 * max(abs(calc), 1e-12):
                out.append(problem("OV-3", f"{v.id}.derived.{k}", want, f"与重算值 {calc!r} 相差超过 1%"))
        return out

    def _rg1(self, v: Variant) -> list[dict]:
        """RG-1 与 LR-4（SLAM 用途）：`slam_rig` 机型须有带 `imu_ref` 的 LiDAR、内置 IMU 导出单位为 g，以及 `role = slam` 的
        全局快门固连相机（无云台）。"""
        if "slam_rig" not in v.tags:
            return []
        out: list[dict] = []
        lidars = [s for s in v.rig if s.kind == "lidar"]
        imus = {s.parent: s for s in v.rig if s.kind == "imu" and s.parent}
        ok_lidar = [s for s in lidars if s.sensor_id in imus]
        if not ok_lidar:
            out.append(problem("RG-1", f"{v.id}.rig", [s.sensor_id for s in lidars], "slam_rig 机型须有带 imu_ref 的 LiDAR"))
        for s in ok_lidar:
            imu = self.sensor_model(str(imus[s.sensor_id].model_ref))
            if imu is None or str(imu[0].get("acc_unit_export")) != "g":
                out.append(problem("LR-4", f"{v.id}.{s.sensor_id}_imu", imus[s.sensor_id].model_ref,
                                   "SLAM 用途的 LiDAR 内置 IMU 须可解析且 acc_unit_export = g"))
        cams = [s for s in v.rig if s.kind == "camera" and "slam" in s.roles]
        good = []
        for s in cams:
            hit = self.sensor_model(str(s.model_ref)) if s.model_ref else None
            shutter = ((hit[0] if hit else {}).get("sensor") or {}).get("shutter")
            if shutter == "global" and not s.gimbal:
                good.append(s)
        if not good:
            out.append(problem("RG-1", f"{v.id}.rig", [s.sensor_id for s in cams], "slam_rig 机型须有 role = slam 的全局快门固连相机（无云台）"))
        return out

    def _lidar_rules(self, where: str, d: dict) -> list[dict]:
        """LR-1（槽位数与时刻规则）、LR-2（量程规格点 ≤ 2%）、LR-4（内置 IMU 引用与外参存在）。"""
        out: list[dict] = []
        pat = d.get("pattern") or {}
        rate = pat.get("point_rate_hz")
        fr = pat.get("frame_rate_hz", d.get("frame_rate_hz"))
        n = pat.get("slots_per_frame")
        if not (isinstance(rate, int) and isinstance(fr, int) and fr > 0 and rate % fr == 0 and n == rate // fr):
            out.append(problem("LR-1", f"{where}.pattern.slots_per_frame", n, f"须等于整数 point_rate_hz / frame_rate_hz（{rate}/{fr}）"))
        elif (n - 1) * 10**8 // n >= 10**8:
            out.append(problem("LR-1", f"{where}.pattern", n, "offset_time(N − 1) 须 < 10^8 ns"))
        if pat.get("offset_time_rule") != OFFSET_TIME_RULE:
            out.append(problem("LR-1", f"{where}.pattern.offset_time_rule", pat.get("offset_time_rule"), f"唯一允许值为 {OFFSET_TIME_RULE}"))
        if not isinstance(pat.get("beams"), int) or pat["beams"] < 1:
            out.append(problem("LR-1", f"{where}.pattern.beams", pat.get("beams"), "须为正整数"))
        rg = d.get("range") or {}
        r10, ex, hard = num(rg.get("r_at_rho10_m")), num(rg.get("exponent")), num(rg.get("hard_max_m"))
        for key, rho in (("r_at_rho10_m", 0.1), ("r_at_rho20_m", 0.2), ("r_at_rho80_m", 0.8)):
            spec = num(rg.get(key))
            if spec is None or r10 is None or ex is None:
                continue
            model = r10 * (rho / 0.1) ** ex
            if hard is not None:
                model = min(model, hard)
            if abs(model - spec) > 0.02 * spec:
                out.append(problem("LR-2", f"{where}.range.{key}", spec, f"量程模型 {model:.2f} m 与规格点相差超过 2%"))
        imu_ref = d.get("imu_ref")
        hit = self.sensor_model(str(imu_ref)) if imu_ref else None
        if hit is None or hit[0].get("schema") != "awr.sensor.imu_mems.v1":
            out.append(problem("LR-4", f"{where}.imu_ref", imu_ref, "须解析到 awr.sensor.imu_mems.v1 型号"))
        lii = d.get("lidar_in_imu")
        try:
            if not isinstance(lii, dict) or len(lii.get("t") or ()) != 3:
                raise ValueError("缺少 t")
            _lidar_in_imu(d)
        except (ValueError, TypeError, KeyError) as e:
            out.append(problem("LR-4", f"{where}.lidar_in_imu", lii, f"须给出 t[3] 与 R（3×3 或 9 元）或 rpy_deg：{e}"))
        return out


def _cm1(sid: str, sd: dict) -> list[dict]:
    """CM-1：给出的内参与光学推导值相差 ≤ 1%；`role_allowed` 含 slam 时须为全局快门。"""
    out: list[dict] = []
    sen = sd.get("sensor") or {}
    f_mm = num((sd.get("optics") or {}).get("f_mm"))
    pix = num(sen.get("pixel_um"))
    intr = sd.get("intrinsics") or {}
    for o in sd.get("outputs") or []:
        name = str(o.get("name"))
        w, h, b = num(o.get("width_px")), num(o.get("height_px")), num(o.get("binning"), 1.0)
        if None in (f_mm, pix, w, h):
            out.append(problem("CM-1", f"sensor_models[{sid}].outputs[{name}]", None, "缺少 f_mm、pixel_um 或输出尺寸"))
            continue
        fx = float(f_mm) / (float(pix) * float(b) * 1e-3)  # type: ignore[arg-type]
        want = {"fx": fx, "fy": fx, "cx": float(w) / 2.0, "cy": float(h) / 2.0}  # type: ignore[arg-type]
        got = intr.get(name) or {}
        for k, dv in want.items():
            gv = num(got.get(k))
            if gv is not None and abs(gv - dv) > 0.01 * dv:
                out.append(problem("CM-1", f"sensor_models[{sid}].intrinsics.{name}.{k}", gv, f"与光学推导值 {dv:.3f} 相差超过 1%"))
    if "slam" in (sd.get("role_allowed") or []) and sen.get("shutter") != "global":
        out.append(problem("CM-1", f"sensor_models[{sid}].sensor.shutter", sen.get("shutter"), "SLAM 用途的相机须为全局快门"))
    return out


def _sd1(ref: str, st: dict, model: dict) -> list[dict]:
    """SD-1：LiDAR 侧表的 fov、range、noise、点频、帧率与 lidar_in_imu.t 与 D2 型号逐项一致（D2 型号文件不变，侧表不得改写它）。"""
    out = [problem("SD-1", f"side_tables[{ref}].{key}", st[key], f"与 D2 型号的 {model.get(key)!r} 不一致")
           for key in ("fov", "range", "noise") if key in st and st[key] != model.get(key)]
    pat, mpat = st.get("pattern") or {}, model.get("pattern") or {}
    if pat.get("point_rate_hz") != mpat.get("point_rate_hz") or pat.get("beams") != mpat.get("beams"):
        out.append(problem("SD-1", f"side_tables[{ref}].pattern", pat.get("point_rate_hz"), "点频或线数与 D2 型号不一致"))
    fr = pat.get("frame_rate_hz", st.get("frame_rate_hz"))
    if fr is not None and fr != model.get("frame_rate_hz"):
        out.append(problem("SD-1", f"side_tables[{ref}].frame_rate_hz", fr, "帧率与 D2 型号不一致"))
    mt = ((model.get("imu") or {}).get("lidar_in_imu") or {}).get("t")
    st_t = (st.get("lidar_in_imu") or {}).get("t")
    if mt is not None and st_t is not None and [float(x) for x in mt] != [float(x) for x in st_t]:
        out.append(problem("SD-1", f"side_tables[{ref}].lidar_in_imu.t", st_t, f"与 D2 型号 imu.lidar_in_imu.t {mt} 不一致"))
    return out
