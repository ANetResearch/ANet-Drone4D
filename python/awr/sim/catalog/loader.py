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
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from ..fleet import profiles as PF
from .derive import num

__all__ = ["CATALOG_SCHEMA", "CODE_ENV_LIMIT", "CODE_NEST_INCOMPATIBLE", "CODE_PROFILE_INVALID", "DISCLAIMER_ZH",
           "FORBIDDEN_NAME_TOKENS", "Catalog", "CatalogError", "capabilities_of", "category_of", "env_temp_check", "nest_compatible",
           "problem"]

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

    @classmethod
    def load(cls, vdir: Path | str | None = None) -> Catalog:
        """读取 `catalog.json`（schema 失败抛 CatalogError）与目录列出的机型文档、型号文件；一致性问题见 `problems()`。"""
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
        return cls(d, cat, docs, sdocs)

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
