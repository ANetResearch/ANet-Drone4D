"""会话内克隆：REST 补丁的规范化、挂载变化的质量与功率、克隆的创建、修改与删除（CAT-FR-014、FR-015；AWR-20 §5.5、§7.6、§8.2
CL-1 至 CL-4、MT-1、MT-3 至 MT-6；17 §17.6.2 SB-24 至 SB-27；契约 `vehicle/clone.schema.json`）。

克隆表与白名单边界由 M08 的 ProfileTable 维护（`add_session`、`remove_session`、`restore_sessions`，M08-FR-208）；本模块在其前后：

1. 规范化请求：`{from_model_id, model_id?, label?, patch}`（17 §17.6.2）或 `awr.vehicle_clone.v1` 的 `{base, set, default_mounts,
   mounts, display_name}`；`patch` 可为嵌套对象或点路径，17 的写法按机型构型映射到 AWR-20 §7.6 的路径（`speed.cruise_mps` 对
   多旋翼为 `limits.cruise_mps`、对固定翼与 VTOL 为 `airframe.v_cruise_mps`，`airframe.bank_max_deg` 为 `airframe.phi_max_deg` 等），
   `sensors[{name, enabled}]` 为缺省挂载的增减；改构型、电池节数或化学体系为 CL-4（522）；
2. 挂载变化（MT-1、MT-6、§5.5）：按 M13 `resolve_mounts`、`mount_summary` 求新的缺省挂载相对源机型的 Δm 与 ΔP_e（不存在或
   D1 槽位 560、拆不可拆槽位 522、镜头不在表内 561），交 M08 `add_session(extra_mass_kg, extra_power_w)` 重算质量、多旋翼悬停
   功率与续航并重跑该构型的全部检查（CL-3，含 M21 登记的 FW、VT、MT 检查）；
3. 克隆之后：把缺省挂载写入克隆文档的 `sensors` 映射，固定翼与 VTOL 的派生值按新质量与附加功率重算（§5.5），附加功率、缺省挂载
   与镜头覆盖记入克隆记录（`T.sessions[cid]`）。

失败抛 `CloneFailure(code, detail)`：522 带 `violations[{field, rule, value, limit, message_zh}]`，505 配额，520 源机型未知，560、561
挂载，105 删除或修改被引用的克隆（detail MODEL_IN_USE）。
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..fleet import profiles as PF
from . import derive as D
from .checks import is_clone_id, model_dir_for

__all__ = ["CloneFailure", "CloneRequest", "create_clone", "delete_clone", "flatten_patch", "mount_delta", "normalize_request",
           "update_clone"]

CODE_CLONE_INVALID = 522
CODE_MODEL_UNKNOWN = 520
CODE_IN_USE = 105
LOCKED_PATHS = ("frame", "battery.cells", "battery.chemistry")  # CL-4
_MR_ALIAS = {"speed.cruise_mps": "limits.cruise_mps", "speed.max_mps": "limits.vxy_max_mps", "speed.climb_mps": "limits.z_up_mps",
             "speed.sink_mps": "limits.z_dn_mps"}
_FW_ALIAS = {"speed.cruise_mps": "airframe.v_cruise_mps", "speed.loiter_mps": "airframe.v_loiter_mps",
             "speed.max_mps": "airframe.v_max_mps", "speed.min_mps": "airframe.v_min_mps", "speed.climb_mps": "airframe.climb_max_mps",
             "speed.sink_mps": "airframe.sink_max_mps"}
_ALIAS = {"airframe.bank_max_deg": "airframe.phi_max_deg", "airframe.c_d0": "airframe.cd0", "airframe.c_lmax": "airframe.cl_max",
          "label": "display_name"}


class CloneFailure(ValueError):
    def __init__(self, code: int, detail: Any = None) -> None:
        self.code = int(code)
        self.detail = detail
        super().__init__(f"{code} {detail}")


def _v(field_: str, rule: str, value: Any, limit: Any, msg: str) -> dict:
    return {"field": field_, "rule": rule, "value": value, "limit": limit, "message_zh": msg}


def flatten_patch(patch: Any, prefix: str = "") -> dict[str, Any]:
    """嵌套对象 -> 点路径（数组与标量为叶子）；已是点路径的键原样保留。"""
    out: dict[str, Any] = {}
    if not isinstance(patch, dict):
        return out
    for k, v in patch.items():
        p = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and p not in ("mounts", "default_mounts") and not p.startswith("mounts."):
            out.update(flatten_patch(v, p))
        else:
            out[p] = v
    return out


@dataclass
class CloneRequest:
    base: str
    set: dict[str, Any] = field(default_factory=dict)
    default_mounts: dict[str, bool] = field(default_factory=dict)
    mounts: dict[str, dict] = field(default_factory=dict)
    display_name: str | None = None
    violations: list[dict] = field(default_factory=list)


def normalize_request(body: dict, base_doc: dict | None = None) -> CloneRequest:
    """REST 请求体（17 §17.6.2）或克隆补丁（awr.vehicle_clone.v1）-> CloneRequest；源机型文档用于按构型映射 17 的路径别名。"""
    base = str(body.get("from_model_id") or body.get("base") or "")
    fk = PF.frame_kind_of(base_doc) if isinstance(base_doc, dict) else 0
    req = CloneRequest(base=base)
    dn = body.get("display_name", body.get("label"))
    if dn is not None:
        req.display_name = str(dn)
    req.default_mounts = {str(k): bool(v) for k, v in (body.get("default_mounts") or {}).items()}
    req.mounts = {str(k): dict(v or {}) for k, v in (body.get("mounts") or {}).items()}
    flat = dict(body.get("set") or {})
    flat.update(flatten_patch(body.get("patch") or {}))
    for path, val in flat.items():
        if path == "sensors":
            for it in val if isinstance(val, list) else []:
                if isinstance(it, dict) and it.get("name"):
                    req.default_mounts[str(it["name"])] = bool(it.get("enabled", it.get("mounted", True)))
                    if it.get("lens_mm") is not None:
                        req.mounts.setdefault(str(it["name"]), {})["lens_mm"] = it["lens_mm"]
            continue
        if path == "default_mounts" and isinstance(val, dict):
            req.default_mounts.update({str(k): bool(v) for k, v in val.items()})
            continue
        if path == "mounts" and isinstance(val, dict):
            req.mounts.update({str(k): dict(v or {}) for k, v in val.items()})
            continue
        if path.startswith("mounts.") and path.endswith(".lens_mm"):
            req.mounts.setdefault(path.split(".")[1], {})["lens_mm"] = val
            continue
        p = _ALIAS.get(path, path)
        p = (_MR_ALIAS if fk == 0 else _FW_ALIAS).get(p, p)
        if p == "display_name":
            req.display_name = str(val)
            continue
        if any(p == lp or p.startswith(lp + ".") for lp in LOCKED_PATHS):
            req.violations.append(_v(path, "CL-4", val, None, "不可改变构型、电池节数与化学体系"))
            continue
        req.set[p] = val
    return req


# ---------------------------------------------------------------- 挂载变化（MT-1、MT-6、§5.5）
def mount_delta(mdir: Path, sensors_map: dict, default_mounts: dict[str, bool], mounts: dict[str, dict],
                capabilities: list[str] | None = None) -> dict[str, Any]:
    """新的缺省挂载相对源机型缺省挂载的 `{dm_kg, dp_w, mask, summary}`；非法时抛 CloneFailure（560、561、522）。"""
    from awr.sim.sensors.gimbal import SensorError
    from awr.sim.sensors.mounts import mount_summary, resolve_mounts
    from awr.sim.sensors.spec import SensorSpecError, load_rig

    try:
        rig = load_rig(mdir, dict(sensors_map))
    except SensorSpecError as e:
        raise CloneFailure(CODE_CLONE_INVALID, {"violations": [_v("sensors", "MT-1", None, None, "; ".join(e.problems)[:300])]}) from e
    if not rig.d2:
        if default_mounts or mounts:
            raise CloneFailure(560, {"why": "LEGACY_MAPPING", "detail": "该机型的传感器映射为 D1 写法，不能在克隆中改挂"})
        return {"dm_kg": 0.0, "dp_w": 0.0, "mask": 0, "summary": None}
    cfg: dict[str, dict] = {}
    for slot, on in default_mounts.items():
        cfg.setdefault(slot, {})["enabled"] = bool(on)
    for slot, o in mounts.items():
        if o.get("lens_mm") is not None:
            cfg.setdefault(slot, {})["lens_mm"] = float(o["lens_mm"])
    try:
        r = resolve_mounts(rig, cfg, capabilities)
    except SensorError as e:
        raise CloneFailure(int(e.code), {"why": e.detail, **dict(e.extra)}) from e
    base_mask = rig.default_mask(sandbox=True)
    s0 = mount_summary(rig, base_mask)
    lens = tuple(x for no, f in sorted(r.lens.items()) for x in (float(no), float(f)))
    s1 = mount_summary(rig.subset(r.mask, lens), r.mask)
    return {"dm_kg": float(s1["mass_kg"]) - float(s0["mass_kg"]), "dp_w": float(s1["power_w"]) - float(s0["power_w"]),
            "mask": int(r.mask), "summary": s1, "lens": dict(r.lens)}


# ---------------------------------------------------------------- 创建、修改、删除
@contextlib.contextmanager
def _base_mapping(base: Any, default_mounts: dict[str, bool]) -> Iterator[None]:
    """在 M08 `add_session` 期间把源机型文档的 `sensors` 缺省挂载标记换成克隆的写法（M08 深拷贝源文档后执行 MNT-1 等检查，
    已计入 Δm 的槽位不能再按可选挂载计一次）；退出时原样恢复。只在步边界的单线程调用中使用。"""
    smap = base.doc.get("sensors") if isinstance(base.doc, dict) else None
    if not default_mounts or not isinstance(smap, dict):
        yield
        return
    saved = smap
    new = dict(smap)
    for slot, on in default_mounts.items():
        v = new.get(slot)
        if isinstance(v, dict):
            new[slot] = {**v, "default": bool(on)}
    base.doc["sensors"] = new
    try:
        yield
    finally:
        base.doc["sensors"] = saved


def _post_process(T: Any, cid: str, req: CloneRequest, delta: dict) -> None:
    """克隆文档的缺省挂载与派生值（§5.5）；附加功率等记入克隆记录。"""
    p = T.profiles[cid]
    doc = p.doc
    smap = doc.get("sensors") or {}
    for slot, on in req.default_mounts.items():
        v = smap.get(slot)
        if isinstance(v, dict):
            smap[slot] = {**v, "default": bool(on)}
    ft = PF.frame_type_of(doc)
    if ft in ("fixed_wing", "vtol_quadplane"):
        der = dict(doc.get("derived") or {})
        for k, v in D.fw_derived(doc, extra_w=float(delta.get("dp_w", 0.0))).items():
            der[k] = round(float(v), 6)
        doc["derived"] = der
    rec = T.sessions.get(cid)
    if isinstance(rec, dict):
        rec["default_mounts"] = dict(req.default_mounts)
        rec["mounts"] = copy.deepcopy(req.mounts)


def _base_of(T: Any, base_ref: str) -> Any:
    try:
        b = T.resolve(base_ref)
    except (KeyError, PF.ProfileError):
        raise CloneFailure(CODE_MODEL_UNKNOWN, {"model_id": base_ref}) from None
    if b.base_ref is not None or is_clone_id(b.ref):
        raise CloneFailure(CODE_CLONE_INVALID, {"violations": [_v("from_model_id", "CL-1", base_ref, None, "不能以会话克隆为源")]})
    hidden = getattr(T, "hidden_ids", lambda: set())()
    if b.profile_id in hidden or "@" in b.ref:
        raise CloneFailure(CODE_CLONE_INVALID, {"violations": [_v("from_model_id", "CL-1", base_ref, None,
                                                                  "源机型须为目录中的可见机型")]})
    return b


def create_clone(T: Any, body: dict, *, quota: int | None = None) -> str:
    """新建会话克隆，返回 `<源 id>_c<n>`；失败抛 CloneFailure。须在步边界调用（M08 ProfileTable 写时复制重建矩阵）。"""
    base_ref = str(body.get("from_model_id") or body.get("base") or "")
    b = _base_of(T, base_ref)
    req = normalize_request(body, b.doc)
    if req.violations:
        raise CloneFailure(CODE_CLONE_INVALID, {"violations": req.violations})
    mdir = Path(b.model_dir) if b.model_dir else model_dir_for(b.doc)
    delta = {"dm_kg": 0.0, "dp_w": 0.0}
    if (req.default_mounts or req.mounts) and mdir is not None:
        caps = [str(c) for c in (b.doc.get("capabilities") or [])]
        delta = mount_delta(mdir, b.doc.get("sensors") or {}, req.default_mounts, req.mounts, caps)
    kw: dict[str, Any] = {"display_name": req.display_name, "extra_mass_kg": float(delta["dm_kg"]),
                          "extra_power_w": float(delta["dp_w"])}
    if quota is not None:
        kw["quota"] = int(quota)
    try:
        with _base_mapping(b, req.default_mounts):
            cid = T.add_session(b.ref, dict(req.set), **kw)
    except PF.CloneError as e:
        raise CloneFailure(e.code, {"violations": e.problems} if e.problems else {"why": e.detail}) from e
    _post_process(T, cid, req, delta)
    return str(cid)


def _in_use_fn(roster: Any) -> Any:
    def in_use(ref: str) -> bool:
        if roster is None:
            return False
        return any(str(getattr(e, "profile_id", "")) == ref for e in getattr(roster, "by_slot", {}).values())
    return in_use


def delete_clone(T: Any, cid: str, roster: Any = None) -> None:
    if cid not in getattr(T, "sessions", {}):
        raise CloneFailure(CODE_MODEL_UNKNOWN, {"model_id": cid})
    try:
        T.remove_session(cid, in_use=_in_use_fn(roster))
    except PF.CloneError as e:
        raise CloneFailure(e.code, {"why": e.detail, "model_id": cid}) from e


def update_clone(T: Any, cid: str, body: dict, roster: Any = None) -> str:
    """修改克隆（SB-26）：只改显示名时原地修改；其余字段在未被实例引用时按同一 id 重建，被引用时 105（17 §17.5：已有实例使用时
    只允许改 label）。新补丁先以临时克隆校验（取得逐项原因），通过后删除原克隆并以原 id 重建。"""
    rec = getattr(T, "sessions", {}).get(cid)
    if rec is None:
        raise CloneFailure(CODE_MODEL_UNKNOWN, {"model_id": cid})
    base = T.resolve(str(rec["base"]))
    req = normalize_request({**body, "from_model_id": base.profile_id}, base.doc)
    if req.violations:
        raise CloneFailure(CODE_CLONE_INVALID, {"violations": req.violations})
    if not req.set and not req.default_mounts and not req.mounts:
        if req.display_name is not None:
            dn = str(req.display_name)
            if not 1 <= len(dn) <= 40:
                raise CloneFailure(CODE_CLONE_INVALID, {"violations": [_v("display_name", "CL-2", dn, [1, 40], "显示名长度须为 1–40 字符")]})
            p = T.profiles[cid]
            p.doc["display_name"] = dn
            T.profiles[cid] = dataclasses.replace(p, display_name=dn)
            rec["display_name"] = dn
        return cid
    if _in_use_fn(roster)(cid):
        raise CloneFailure(CODE_IN_USE, {"why": "MODEL_IN_USE", "model_id": cid})
    merged_set = {**dict(rec.get("set") or {}), **req.set}
    merged_dm = {**dict(rec.get("default_mounts") or {}), **req.default_mounts}
    merged_mo = {**copy.deepcopy(rec.get("mounts") or {}), **req.mounts}
    dn = req.display_name if req.display_name is not None else rec.get("display_name")
    mdir = Path(base.model_dir) if base.model_dir else model_dir_for(base.doc)
    delta: dict[str, Any] = {"dm_kg": 0.0, "dp_w": 0.0}
    if (merged_dm or merged_mo) and mdir is not None:
        delta = mount_delta(mdir, base.doc.get("sensors") or {}, merged_dm, merged_mo,
                            [str(c) for c in (base.doc.get("capabilities") or [])])
    try:
        with _base_mapping(base, merged_dm):
            tmp = T.add_session(base.ref, merged_set, display_name=dn, extra_mass_kg=float(delta["dm_kg"]),
                                extra_power_w=float(delta["dp_w"]), quota=PF.CLONE_MAX)
    except PF.CloneError as e:
        raise CloneFailure(e.code, {"violations": e.problems} if e.problems else {"why": e.detail}) from e
    T.remove_session(tmp)
    old = {"id": cid, **copy.deepcopy(rec)}
    old_req = CloneRequest(base=base.ref, set=dict(rec.get("set") or {}), default_mounts=dict(rec.get("default_mounts") or {}),
                           mounts=copy.deepcopy(rec.get("mounts") or {}), display_name=rec.get("display_name"))
    T.remove_session(cid)
    spec = {"id": cid, "base": base.ref, "set": merged_set, "display_name": dn, "extra_mass_kg": float(delta["dm_kg"]),
            "extra_power_w": float(delta["dp_w"])}
    with _base_mapping(base, merged_dm):
        bad = T.restore_sessions([spec])
    if bad:
        with _base_mapping(base, dict(old.get("default_mounts") or {})):
            T.restore_sessions([old])
        if cid in T.sessions:
            _post_process(T, cid, old_req, {"dp_w": float(old.get("extra_power_w") or 0.0)})
        raise CloneFailure(CODE_CLONE_INVALID, {"why": "REBUILD_FAILED", "model_id": cid})
    _post_process(T, cid, CloneRequest(base=base.ref, set=merged_set, default_mounts=merged_dm, mounts=merged_mo, display_name=dn),
                  delta)
    return cid
