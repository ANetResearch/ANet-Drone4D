"""可持续性评估（M20-FR-065、FR-066；M20 §6.10；AWR-04 §9.6；契约 `tasking/sustainability.schema.json`）。

```text
1 逐小时 h：照度档、MOR、风 → 可行窗口非空且站位视线可见（调用方按档预先求得 M19 feasibility_detail 与站位视线）
2 每个连续在岗位：按该档可用机型求 t_on = 0.8·t_endur(role) − 2·t_tr，N_air、通道、电池（M20 §6.3.4）
3 与可用资源比较：同类机（排除 crewed 机巢）、通道（cells ≤ cells_max）、电池（同节数）→ airframe | charger | battery
4 机巢与站位的链路距离、抗风 → link_range | wind
5 全部小时可行且资源满足 → verdict yes；否则 no 与瓶颈、不可行时段 [start, end, band, why]
```
M19 不可行原因到契约瓶颈码的映射：contrast → weather；los → los；其余（light、blur、pixels、no_sensor、narrow、keepout_gt_range）→
night_sensor（"所需等级的传感器能力不足"，detail.why 保留 M19 原因）。
"""

from __future__ import annotations

import math

import numpy as np

from .model import ModelInfo
from .nestmodel import charge_time_s, steady_state

__all__ = ["WHY_TO_CODE", "evaluate", "pack_of", "t_turn_s"]

WHY_TO_CODE = {"contrast": "weather", "los": "los", "light": "night_sensor", "blur": "night_sensor", "pixels": "night_sensor",
               "no_sensor": "night_sensor", "narrow": "night_sensor", "keepout_gt_range": "night_sensor", "wind": "wind",
               "link_range": "link_range"}


def pack_of(m: ModelInfo) -> dict:
    p = dict(m.pack or {})
    p.setdefault("capacity_ah", max(m.e_use_wh / 0.85 / 22.8, 1.0))
    p.setdefault("cells", 6)
    p.setdefault("c_max", 1.0)
    p.setdefault("packs_per_swap", 1)
    return p


def t_turn_s(nest: dict, t_charge: float) -> float:
    if str(nest.get("mode", "swap")) == "charge":
        return float(nest.get("turnaround_s", 60.0)) + t_charge
    return float(nest.get("turnaround_s", 60.0)) + float(nest.get("swap_s", 180.0)) + 2.0 * float(nest.get("launch_interval_s", 30.0))


def _charger_for(nest: dict, cells: int) -> tuple[int, float]:
    """该机巢对 cells 节电池的兼容通道数与最大电流。"""
    ch, i = 0, 0.0
    for c in nest.get("chargers") or []:
        if int(c.get("cells_min", 1)) <= cells <= int(c.get("cells_max", 0)):
            ch += int(c.get("count", 1)) * int(c.get("channels", 1))
            i = max(i, float(c.get("i_max_a", 10.0)))
    return ch, i


def evaluate(*, positions: list[dict], hours: list[dict], band_feasible: dict[str, dict], models: dict[str, ModelInfo],
             vehicles: list[dict], nests: dict[str, dict], wind_mps: float = 0.0, horizon_s: float | None = None,
             reserve: float = 0.2) -> dict:
    """positions：[{id, pos, role, models?}]（连续在岗位）；hours：[{t_from_unix_ns, t_to_unix_ns, band, wind_mps?}]；
    band_feasible：{band: {ok, why, d_lo_m, d_hi_m, models[], los}}；vehicles：[{id, model_id, nest_id}]；nests：机巢配置（含
    chargers、energy.packs 与 crewed）。返回契约 `awr.tasking.sustainability.v1` 形状加 `per_position`。"""
    bottlenecks: list[dict] = []
    windows: list[dict] = []
    seen: set[str] = set()

    def add(code: str, detail: dict | None = None) -> None:
        if code not in seen:
            seen.add(code)
            bottlenecks.append({"code": code, "detail": detail or {}})

    # 1 逐小时可行性
    for h in hours:
        bf = band_feasible.get(str(h["band"])) or {"ok": False, "why": "no_sensor"}
        w_lim = h.get("wind_mps")
        ok = bool(bf.get("ok")) and bool(bf.get("los", True))
        why = None if ok else (bf.get("why") or ("los" if not bf.get("los", True) else "no_sensor"))
        if ok and w_lim is not None and float(w_lim) > min((m.wind_max_mps for m in models.values()), default=12.0) + 1e-9:
            ok, why = False, "wind"
        if not ok:
            add(WHY_TO_CODE.get(str(why), "night_sensor"), {"band": h["band"], "why": why})
        if windows and windows[-1]["band"] == h["band"] and windows[-1]["feasible"] == ok and windows[-1]["why"] == why \
                and windows[-1]["t_to_unix_ns"] == str(h["t_from_unix_ns"]):
            windows[-1]["t_to_unix_ns"] = str(h["t_to_unix_ns"])
        else:
            windows.append({"t_from_unix_ns": str(h["t_from_unix_ns"]), "t_to_unix_ns": str(h["t_to_unix_ns"]), "band": str(h["band"]),
                            "feasible": ok, "why": why, "d_lo_m": bf.get("d_lo_m"), "d_hi_m": bf.get("d_hi_m")})
    # 2–4 资源
    uncrewed = {nid for nid, n in nests.items() if not n.get("crewed", n.get("kind") == "launcher")}
    pool: dict[str, list[dict]] = {}
    for v in vehicles:
        if v.get("nest_id") in uncrewed:
            pool.setdefault(str(v["model_id"]), []).append(v)
    need_air: dict[str, int] = {}
    need_ch: dict[str, int] = {}
    need_pk: dict[str, int] = {}
    per_pos: list[dict] = []
    n_air_tot = n_air_rec_tot = 0
    for pos in positions:
        P = np.asarray(pos["pos"], float)
        role = str(pos.get("role", "hover"))
        allowed = pos.get("models") or list(models)
        best: tuple | None = None
        for mid in sorted(allowed):
            m = models.get(mid)
            vs = pool.get(mid) or []
            if m is None or not vs:
                continue
            nid = str(vs[0].get("nest_id"))
            nest = nests.get(nid) or {}
            npos = np.asarray(nest.get("pos") or (0.0, 0.0, 0.0), float)
            dist = float(np.hypot(*(P[:2] - npos[:2])))
            rng = nest.get("link_range_m") or m.link_range_m
            if rng and dist > float(rng) + 1e-6:
                add("link_range", {"position": pos.get("id"), "dist_m": round(dist, 1), "range_m": rng})
                continue
            if wind_mps > min(m.wind_max_mps, float(nest.get("wind_max_mps", 12.0))) + 1e-9:
                add("wind", {"wind_mps": wind_mps, "model_id": mid})
                continue
            t_tr = dist / max(m.v_cruise_mps, 0.1)
            t_on = (1.0 - reserve) * m.endurance_s(role if m.hovers or role != "hover" else "loiter") - 2.0 * t_tr
            pk = pack_of(m)
            ch_have, i_max = _charger_for(nest, int(pk["cells"]))
            t_ch = charge_time_s(float(pk["capacity_ah"]), float(pk["c_max"]), i_max or 10.0, 0.0, 0.95) if ch_have else math.inf
            ss = steady_state(t_on, t_tr, t_turn_s(nest | {"mode": (nest.get("energy") or {}).get("mode", "swap"),
                                                         "swap_s": (nest.get("energy") or {}).get("swap_s", 180.0)}, t_ch),
                              float((nest.get("energy") or {}).get("swap_s", 180.0)), t_ch, packs_per_swap=int(pk["packs_per_swap"]))
            key = (ss["feasible"], t_on, mid)
            if best is None or key > best[0]:
                best = (key, mid, nid, ss, t_on, t_tr, t_ch)
        if best is None:
            add("airframe", {"position": pos.get("id"), "why": "no_model"})
            continue
        _k, mid, nid, ss, t_on, t_tr, t_ch = best
        if not ss["feasible"] or t_on <= 0:
            add("airframe", {"position": pos.get("id"), "model_id": mid, "t_on_s": round(t_on, 1)})
            continue
        need_air[mid] = need_air.get(mid, 0) + ss["n_air"]
        need_ch[nid] = need_ch.get(nid, 0) + ss["n_ch"]
        need_pk[nid] = need_pk.get(nid, 0) + ss["n_pack"]
        n_air_tot += ss["n_air"]
        n_air_rec_tot += ss["n_air_recommended"]
        per_pos.append({"id": pos.get("id"), "model_id": mid, "nest_id": nid, "t_on_s": round(t_on, 1), "t_tr_s": round(t_tr, 1),
                        "t_charge_s": None if not math.isfinite(t_ch) else round(t_ch, 1), **{k: ss[k] for k in ("n_air", "n_ch", "n_pack")}})
    for mid, need in sorted(need_air.items()):
        have = len(pool.get(mid) or [])
        if have < need:
            add("airframe", {"model_id": mid, "need": need, "have": have})
    ch_need = ch_have_t = pk_need = pk_have_t = 0
    for nid in sorted(set(need_ch) | set(need_pk)):
        nest = nests.get(nid) or {}
        cells = None
        mids = [p["model_id"] for p in per_pos if p["nest_id"] == nid]
        if mids:
            cells = int(pack_of(models[mids[0]])["cells"])
        have_ch = _charger_for(nest, cells or 6)[0]
        n_veh = sum(1 for v in vehicles if v.get("nest_id") == nid and (not mids or v["model_id"] in mids))
        spare = sum(int(p.get("count", 0)) for p in (nest.get("energy") or {}).get("packs") or [] if cells is None or int(p.get("cells", cells)) == cells)
        have_pk = n_veh + spare
        ch_need += need_ch.get(nid, 0)
        ch_have_t += have_ch
        pk_need += need_pk.get(nid, 0)
        pk_have_t += have_pk
        if have_ch < need_ch.get(nid, 0):
            add("charger", {"nest": nid, "need": need_ch[nid], "have": have_ch})
        if have_pk < need_pk.get(nid, 0):
            add("battery", {"nest": nid, "need": need_pk[nid], "have": have_pk})
    verdict = "yes" if not bottlenecks and positions else ("not_applicable" if not positions else "no")
    out = {"verdict": verdict, "bottlenecks": bottlenecks, "windows": windows, "n_air": int(n_air_tot), "n_air_recommended": int(n_air_rec_tot),
           "chargers": {"need": int(ch_need), "have": int(ch_have_t)}, "packs": {"need": int(pk_need), "have": int(pk_have_t)},
           "per_position": per_pos}
    if horizon_s is not None:
        out["horizon_s"] = float(horizon_s)
    return out
