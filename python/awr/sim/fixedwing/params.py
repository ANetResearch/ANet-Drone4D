"""固定翼与 VTOL 的运行参数（AWR-20 §5.1、§5.4；ADR-096）：由 ProfileTable 的机型文档按表键取得，按 `id(T.PT)` 失效（ProfileTable
增删存档版本或会话克隆时写时复制重建矩阵，M08-FR-208）。

`FwParams` 汇总 `catalog.derive.FwAero`（气动、速度、滚转、爬升、盘旋）、VTOL 模式参数与能量口径（可用能量、会话克隆的附加电功率
`extra_power_w`，AWR-20 §5.5）。纯数据，不读状态。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from awr.sim.catalog import derive as D

__all__ = ["FwParams", "ParamsCache", "params_from_doc"]


@dataclass(frozen=True)
class FwParams:
    ref: str
    frame_kind: int  # 1 固定翼、2 VTOL
    aero: D.FwAero
    e_use_wh: float | None
    extra_power_w: float = 0.0
    # 起降（AWR-20 §5.1）
    launch_type: str = "catapult"
    launch_t_s: float = 1.0
    launch_climb_mps: float = 4.0
    launch_alt_agl_m: float = 60.0
    recovery_type: str = "parachute"
    chute_descent_mps: float = 5.0
    # VTOL（AWR-20 §5.4）
    transition_fw_s: float = 15.0
    transition_mc_s: float = 10.0
    v_transition_mps: float = 0.0
    p_hover_w: float = 0.0
    hover_max_s: float = math.inf
    d_fw_min_m: float = 300.0
    agl_transition_min_m: float = 30.0
    mc_cruise_mps: float = 3.0
    mc_climb_mps: float = 2.5
    mc_sink_mps: float = 1.5
    acoustic: dict = field(default_factory=dict)

    @property
    def vtol(self) -> bool:
        return self.frame_kind == 2


def params_from_doc(doc: dict, ref: str, frame_kind: int, *, extra_power_w: float = 0.0) -> FwParams | None:
    fw = D.fw_aero(doc)
    if fw is None:
        return None
    af = doc.get("airframe") or {}
    la = af.get("launch") or {}
    rc = af.get("recovery") or {}
    vt = doc.get("vtol") or {}
    lp = doc.get("limits_profiles") or {}
    name = (doc.get("catalog") or {}).get("limits_default") or lp.get("default")
    px = ((lp.get(str(name)) or {}).get("px4_params") or {}) if name else {}
    kw: dict[str, Any] = {"ref": ref, "frame_kind": int(frame_kind), "aero": fw, "e_use_wh": D.e_use_wh(doc),
                          "extra_power_w": float(extra_power_w), "acoustic": dict(doc.get("acoustic") or {})}
    if la:
        kw.update({"launch_type": str(la.get("type", "catapult")), "launch_t_s": float(la.get("t_s", 1.0)),
                   "launch_climb_mps": float(la.get("climb_mps", fw.climb_max_mps)),
                   "launch_alt_agl_m": float(la.get("alt_agl_m", 60.0))})
    if rc:
        kw.update({"recovery_type": str(rc.get("type", "parachute")), "chute_descent_mps": float(rc.get("descent_mps", 5.0))})
    if vt:
        kw.update({"transition_fw_s": float(D.num(vt.get("transition_fw_s"), 15.0)),
                   "transition_mc_s": float(D.num(vt.get("transition_mc_s"), 10.0)),
                   "v_transition_mps": float(D.num(vt.get("v_transition_mps"), fw.v_min_mps)),
                   "p_hover_w": float(D.num(vt.get("p_hover_w"), 0.0)), "hover_max_s": float(D.num(vt.get("hover_max_s"), math.inf)),
                   "d_fw_min_m": float(D.num(vt.get("d_fw_min_m"), 300.0)),
                   "agl_transition_min_m": float(D.num(vt.get("agl_transition_min_m"), 30.0))})
    if px:
        kw.update({"mc_cruise_mps": float(px.get("MPC_XY_CRUISE", 3.0)), "mc_climb_mps": float(px.get("MPC_Z_VEL_MAX_UP", 2.5)),
                   "mc_sink_mps": float(px.get("MPC_Z_VEL_MAX_DN", 1.5))})
    return FwParams(**kw)


class ParamsCache:
    """按 ProfileTable 表键缓存 FwParams；`T.PT` 换新（克隆增删、存档加载）时清空。"""

    def __init__(self) -> None:
        self._key: int | None = None
        self._by_ref: dict[str, FwParams | None] = {}

    def get(self, T: Any, pidx: int) -> FwParams | None:
        if self._key != id(T.PT):
            self._key = id(T.PT)
            self._by_ref = {}
        ref = T.ids[int(pidx)]
        if ref not in self._by_ref:
            p = T.profiles[ref]
            sess = (getattr(T, "sessions", {}) or {}).get(ref) or {}
            self._by_ref[ref] = params_from_doc(p.doc, ref, int(p.frame_kind), extra_power_w=float(sess.get("extra_power_w") or 0.0))
        return self._by_ref[ref]

    def by_profile(self, profile: Any) -> FwParams | None:
        doc = getattr(profile, "doc", None) or {}
        return params_from_doc(doc, str(getattr(profile, "ref", doc.get("id"))), int(getattr(profile, "frame_kind", 1)))
