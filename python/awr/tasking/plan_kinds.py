"""M20 规划作业的登记（M20-FR-036、FR-052、FR-065、FR-020；M20 §7.3；ADR-113；d2-WP-15-to-M20 第 1–5 条）。

| 类别 | 预算 | 优先级 | 输入（payload） | 输出（PlanResult.extra） |
|---|---|---|---|---|
| `tasking.scan` | 2000 ms | START | `{spec, snapshot, tid, now}`（任务分解，含 GSD 扫描） | `{ok, items[], geometry, preview, children?, relay?, warnings}` 或 `{ok: false, code, why, detail}` |
| `tasking.allocate` | 200 ms | INTERACTIVE | `{snapshot, allocator, budget_ms, external?}` | `{ok, plan}`（`Plan.to_payload()`）|
| `tasking.station` | 500 ms | INTERACTIVE | `{target_pos, windows, keepouts, nests, others, origin, v_mps, p_ratio, next_windows, moving, target_h_m, avoid?, nest_clear_m?, move_r_m?, border?, transit?}` | `{ok, station}` |
| `tasking.sustain` | 1000 ms | BACKGROUND | `{positions, hours, band_feasible, models, vehicles, nests, wind_mps, horizon_s}` | `{ok, sustainability}` |

导入本模块只登记（共享规划服务、每个 worker、sim-core 与共享展示的 plan-pool 都会导入），处理函数内部再延迟导入算法模块。
`run_kind(kind, payload, world)` 供 sim-core 在没有规划池时以同一函数同步执行（批处理 inline、单测）。输入配额 585 用
`jobs.quota_check`（面积、顶点、工作项、机体、站位候选、禁入体）。结果哈希 `result_digest((), None, extra)`，同一输入逐字节一致。
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

__all__ = ["BUDGETS", "KINDS", "measure", "run_kind", "validate"]

KINDS = ("tasking.scan", "tasking.allocate", "tasking.station", "tasking.sustain")
BUDGETS = {"tasking.scan": 2000, "tasking.allocate": 200, "tasking.station": 500, "tasking.sustain": 1000}


def measure(payload: dict) -> dict:
    """输入规模（AWR-04 §11.5；`jobs.PLAN_QUOTA` 的键）。"""
    m: dict[str, float] = {}
    spec = payload.get("spec") if isinstance(payload, dict) else None
    if isinstance(spec, dict):
        params = spec.get("params") or {}
        poly = params.get("polygon")
        if isinstance(poly, list) and len(poly) >= 3:
            m["vertices"] = len(poly)
            x = [float(p[0]) for p in poly]
            y = [float(p[1]) for p in poly]
            m["area_m2"] = 0.5 * abs(sum(x[i] * y[(i + 1) % len(x)] - x[(i + 1) % len(x)] * y[i] for i in range(len(x))))
        wps = params.get("waypoints")
        if isinstance(wps, list):
            m["vertices"] = max(m.get("vertices", 0), len(wps))
    snap = payload.get("snapshot") if isinstance(payload, dict) else None
    if isinstance(snap, dict):
        m["work_items"] = len(snap.get("items") or [])
        m["vehicles"] = len(snap.get("vehicles") or [])
        m["keepouts"] = len(snap.get("keepouts") or [])
    if isinstance(payload, dict) and isinstance(payload.get("keepouts"), (list, tuple)):
        m["keepouts"] = len(payload["keepouts"])
    return m


def validate(payload: dict) -> tuple[int, str, dict] | None:
    from awr.sim.planning.jobs import quota_check

    return quota_check(measure(payload))


class _HFun:
    """世界高度的注入（`DsmWorldQuery` 或测试替身）；没有世界时为平地 z = 0。"""

    def __init__(self, world: Any) -> None:
        self.w = world

    def dsm(self, xy):
        import numpy as np

        if self.w is None:
            return np.zeros(len(np.atleast_2d(xy)))
        return self.w.height_dsm(np.atleast_2d(np.asarray(xy, float))[:, :2])

    def dtm(self, xy):
        import numpy as np

        if self.w is None:
            return np.zeros(len(np.atleast_2d(xy)))
        return self.w.ground_dtm(np.atleast_2d(np.asarray(xy, float))[:, :2])

    def los(self, A, B):
        import numpy as np

        if self.w is None or not hasattr(self.w, "los_batch"):
            return np.ones(len(np.atleast_2d(A)), bool)
        A = np.atleast_2d(np.asarray(A, float))
        B = np.atleast_2d(np.asarray(B, float))
        # M04 `los_batch` 每次 ≤ MAX_LOS_BATCH（64）对（GeoError 110）：站位作业的候选 × 三射线常超过，分块查询（D2-INT：
        # relay_watch 因此一律 581 error，d2-WP-13-to-M20-M21 第 7 条）
        n = len(A)
        step = int(getattr(self.w, "max_los_batch", 0) or _max_los_batch())
        if n <= step:
            return self.w.los_batch(A, B)
        return np.concatenate([np.asarray(self.w.los_batch(A[i:i + step], B[i:i + step]), bool) for i in range(0, n, step)])


    def free_ok(self, xyz, inset_m: float = 30.0):
        """站位可用：世界边界内（离边界 ≥ inset_m）且不在禁飞区与限制区内（M04 zones；细校验 102 的同一口径）。"""
        import numpy as np

        P = np.atleast_2d(np.asarray(xyz, float))
        zones = getattr(self.w, "zones", None) if self.w is not None else None
        if zones is None:
            return np.ones(len(P), bool)
        ok = np.ones(len(P), bool)
        with contextlib.suppress(Exception):
            ok &= np.asarray(zones.border_signed_distance(P[:, :2]), float) >= float(inset_m)
        try:
            if P.shape[1] >= 3:
                ok &= ~np.asarray(zones.contains(P[:, :3], {"nofly", "restricted"}), bool)
        except Exception:
            pass
        return ok

    def transit_ok(self, origin, P, *, clear_m: float = 15.0, cap_agl_m: float = 160.0, step_m: float = 10.0):
        """起点到各候选的直线走廊可按 M20 执行器的转场高度飞越（走廊 DSM + clear_m ≤ 走廊最低 DTM + cap_agl_m，与
        `Executor.transit_z` 的上限同一口径）；没有世界时全部为真。"""
        import numpy as np

        P = np.atleast_2d(np.asarray(P, float))
        if self.w is None or origin is None or len(P) == 0:
            return np.ones(len(P), bool)
        o = np.asarray(origin, float).reshape(-1)[:2]
        out = np.ones(len(P), bool)
        for k in range(len(P)):
            L = float(np.hypot(*(P[k, :2] - o)))
            n = max(2, int(L / step_m) + 1)
            q = o[None, :] + (P[k, :2] - o)[None, :] * np.linspace(0.0, 1.0, n)[:, None]
            out[k] = float(np.max(self.dsm(q))) + clear_m <= float(np.min(self.dtm(q))) + cap_agl_m
        return out


def _max_los_batch() -> int:
    try:
        from awr.world.geometry.query import MAX_LOS_BATCH

        return int(MAX_LOS_BATCH)
    except Exception:
        return 64


def _err(e: Exception) -> dict:
    from .model import TaskingError

    if isinstance(e, TaskingError):
        return {"ok": False, "code": e.code, "why": e.why, "detail": e.detail}
    return {"ok": False, "code": 320, "why": "error", "detail": {"error": f"{type(e).__name__}: {e}"[:300]}}


def run_kind(kind: str, payload: dict, world: Any = None) -> dict:
    """执行一个 M20 作业，返回 extra 字典（不抛错：不可行与异常编码为 `{ok: false, code, why, detail}`）。"""
    from .model import Snapshot

    H = _HFun(world)
    try:
        if kind == "tasking.scan":
            from .decompose import DecompCtx, decompose

            snap = Snapshot.from_payload(payload["snapshot"])
            import numpy as np

            ctx = DecompCtx(snap, H.dsm, H.dtm, str(payload.get("tid") or "t"), float(payload.get("now", snap.t_s)),
                            los_fn=H.los if world is not None else None, free_fn=(lambda P: H.free_ok(P, 30.0)) if world is not None else None,
                            border_fn=(lambda xy, ins: H.free_ok(np.c_[np.asarray(xy, float)[:, :2], np.zeros(len(xy))], ins))
                            if world is not None else None, target_classes=dict(payload.get("target_classes") or {}),
                            avoid_pts=[(float(q[0]), float(q[1])) for q in payload.get("avoid_pts") or []],
                            avoid_layers=[tuple(float(x) for x in q) for q in payload.get("avoid_layers") or []])
            out = decompose(payload["spec"], ctx)
            return {"ok": True, "items": [w.to_payload() for w in out.get("items") or []], "geometry": out.get("geometry") or {},
                    "preview": out.get("preview") or {}, "children": out.get("children") or [], "relay": out.get("relay"),
                    "warnings": out.get("warnings") or [], "mode": out.get("mode"), "scan_detail": out.get("scan_detail")}
        if kind == "tasking.allocate":
            from .allocator import get_allocator

            snap = Snapshot.from_payload(payload["snapshot"])
            name = str(payload.get("allocator") or "greedy")
            if name == "external" and payload.get("external") is not None:
                from .allocator import ExternalAllocator

                verdicts, plan = ExternalAllocator().accept(snap, list(payload["external"]))
                return {"ok": True, "plan": plan.to_payload(), "verdicts": [v.to_payload() for v in verdicts]}
            plan = get_allocator(name, {"fallback": payload.get("fallback", "greedy")}).plan(snap, int(payload.get("budget_ms", 200)))
            return {"ok": True, "plan": plan.to_payload(), "digest": plan.digest()}
        if kind == "tasking.station":
            import numpy as np

            from .station import select_station

            ko = payload.get("keepouts")
            ko = np.zeros((0, 3)) if ko is None or len(ko) == 0 else np.asarray(ko, float).reshape(-1, 3)
            r = select_station(payload["target_pos"], [[tuple(w) for w in ws] for ws in payload.get("windows") or []],
                               keepouts=ko, height_dsm=H.dsm,
                               ground_dtm=H.dtm, los_fn=H.los if world is not None else None, target_h_m=float(payload.get("target_h_m", 1.75)),
                               nests=[(tuple(n[0]), n[1]) for n in payload.get("nests") or []], others=payload.get("others") or [],
                               origin=payload.get("origin"), v_mps=float(payload.get("v_mps", 8.0)), p_ratio=payload.get("p_ratio"),
                               next_windows=[[tuple(w) for w in ws] for ws in payload.get("next_windows") or []] or None,
                               moving=bool(payload.get("moving", False)), fw=payload.get("fw"), r_min=payload.get("r_min"),
                               avoid=[tuple(a) for a in payload.get("avoid") or []], nest_clear_m=float(payload.get("nest_clear_m", 0.0)),
                               move_r_m=float(payload.get("move_r_m", 0.0)), move_q=payload.get("move_q"), region_q=payload.get("region_q"),
                               see_pos=payload.get("see_pos"),
                               border_fn=(lambda xy: H.free_ok(np.c_[np.asarray(xy, float)[:, :2], np.zeros(len(xy))], 30.0))
                               if world is not None and payload.get("border", True) else None,
                               transit_fn=(lambda P: H.transit_ok(payload.get("origin"), P)) if world is not None and payload.get("transit", True)
                               else None)
            return {"ok": True, "station": r.to_payload()}
        if kind == "tasking.sustain":
            from .model import ModelInfo
            from .sustain import evaluate

            models = {k: ModelInfo.from_payload(v) for k, v in (payload.get("models") or {}).items()}
            s = evaluate(positions=payload.get("positions") or [], hours=payload.get("hours") or [],
                         band_feasible=payload.get("band_feasible") or {}, models=models, vehicles=payload.get("vehicles") or [],
                         nests=payload.get("nests") or {}, wind_mps=float(payload.get("wind_mps", 0.0)), horizon_s=payload.get("horizon_s"))
            return {"ok": True, "sustainability": s}
        return {"ok": False, "code": 300, "why": "unknown_kind", "detail": {"kind": kind}}
    except Exception as e:  # 作业内错误不抛出（worker 320 之外，给出可读原因）
        return _err(e)


def _handler(kind: str):
    def fn(req, world):  # PlanRequest, DsmWorldQuery
        from awr.sim.planning.jobs import PlanResult, result_digest

        t0 = time.perf_counter()
        extra = run_kind(kind, dict(req.payload), world)
        ms = (time.perf_counter() - t0) * 1000.0
        status = "ok" if extra.get("ok") else "infeasible"
        code = 0 if extra.get("ok") else int(extra.get("code") or 580)
        return PlanResult(req.job_id, status, (), None, {"plan_ms": round(ms, 3)}, None if extra.get("ok") else str(extra.get("why")), None,
                          result_digest((), None, extra), code, extra)

    fn.__name__ = "run_" + kind.replace(".", "_")
    fn.__module__ = __name__
    return fn


def _register() -> None:
    try:
        from awr.sim.planning.jobs import (
            PRIO_BACKGROUND,
            PRIO_INTERACTIVE,
            PRIO_START,
            PlanKindError,
            register_plan_kind,
        )
    except Exception:  # pragma: no cover - 纯算法环境（无 M10）只用 run_kind
        return
    prio = {"tasking.scan": PRIO_START, "tasking.allocate": PRIO_INTERACTIVE, "tasking.station": PRIO_INTERACTIVE,
            "tasking.sustain": PRIO_BACKGROUND}
    for k in KINDS:
        with contextlib.suppress(PlanKindError):
            register_plan_kind(k, _HANDLERS[k], owner="M20", budget_ms=BUDGETS[k], validate_input=validate, priority_default=prio[k])


_HANDLERS = {k: _handler(k) for k in KINDS}
_register()
