"""tests/tasking 公共工具（M20；模块名 m20lib 以免与其他目录的 tlib 冲突）：平地 + 长方体建筑的假世界、P600 等机型摘要与快照构造。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from awr.tasking.model import Camera, Keepout, ModelInfo, NestView, Snapshot, VehicleView, WorkItem

GX40 = Camera("eo_gx40", "eo", 1920, 1080, 2.9, 4.8, 48.0, (), 0.001, 2.0)
LWIR = Camera("lwir640", "lwir", 640, 512, 12.0, 100.0, 100.0, (100.0,), 0.01, 2.0)

P600 = ModelInfo("p600_mid360", "multirotor", 8.0, 12.0, 3.0, 3.825, 401.0, 420.0, 401.0, 193.8, 3500.0, 12.0, 0.0,
                 ("rgb.zoom", "lidar.mapping"), (GX40,), ("eo_gx40",), {"pack_id": "lpb610hv", "cells": 6, "capacity_ah": 10.0, "c_max": 1.0})
Q1 = ModelInfo("awr_q1", "multirotor", 10.0, 15.0, 3.0, 0.95, 120.0, 130.0, 120.0, 70.0, 3000.0, 10.0, 0.0, ("rgb.zoom",), (GX40,),
               ("eo",), {"cells": 4, "capacity_ah": 5.0, "c_max": 1.0})
V1 = ModelInfo("awr_v1", "vtol", 22.0, 30.0, 3.0, 9.5, 900.0, 520.0, 480.0, 807.8, 15000.0, 12.0, 132.5, ("rgb.zoom", "thermal.imaging", "nir.active"),
               (GX40, LWIR), ("eo", "lwir"), {"cells": 12, "capacity_ah": 22.0, "c_max": 0.5})
F1 = ModelInfo("awr_f1", "fixed_wing", 22.0, 30.0, 3.0, 6.0, 300.0, 300.0, 250.0, 600.0, 20000.0, 12.0, 96.5, ("rgb.zoom", "thermal.imaging"),
               (GX40,), ("eo",), {"cells": 12, "capacity_ah": 22.0, "c_max": 0.5})
MODELS = {m.id: m for m in (P600, Q1, V1, F1)}


@dataclass
class FakeWorld:
    """平地（z = ground）加长方体建筑 `(xmin, xmax, ymin, ymax, height)`；DSM 为建筑顶面，DTM 为地面。"""

    ground: float = 0.0
    boxes: list[tuple[float, float, float, float, float]] = field(default_factory=list)
    world_id: str = "fake"
    content_version: str = "1"
    coordinate_sha256: str = "0" * 64

    def height_dsm(self, xy: Any, **_: Any) -> np.ndarray:
        p = np.atleast_2d(np.asarray(xy, float))[:, :2]
        z = np.full(len(p), self.ground)
        for x0, x1, y0, y1, h in self.boxes:
            m = (p[:, 0] >= x0) & (p[:, 0] <= x1) & (p[:, 1] >= y0) & (p[:, 1] <= y1)
            z[m] = np.maximum(z[m], self.ground + h)
        return z

    def ground_dtm(self, xy: Any, **_: Any) -> np.ndarray:
        return np.full(len(np.atleast_2d(np.asarray(xy, float))), self.ground)

    def los_batch(self, A: Any, B: Any, step_m: float = 1.0, eps_m: float = 0.5) -> np.ndarray:
        A = np.asarray(A, float).reshape(-1, 3)
        B = np.asarray(B, float).reshape(-1, 3)
        out = np.ones(len(A), bool)
        for i in range(len(A)):
            L = float(np.linalg.norm(B[i] - A[i]))
            n = max(2, math.ceil(max(L - 2 * eps_m, 0) / step_m) + 1)
            t = np.linspace(eps_m / max(L, 1e-9), 1 - eps_m / max(L, 1e-9), n) if 2 * eps_m < L else np.array([0.5])
            P = A[i][None, :] + (B[i] - A[i])[None, :] * t[:, None]
            out[i] = bool(np.all(P[:, 2] >= self.height_dsm(P[:, :2]) - 1e-9))
        return out


def nest(nid: str, pos=(0.0, 0.0, 0.0), kind: str = "box", **kw: Any) -> NestView:
    return NestView(nid, tuple(float(x) for x in pos), kind, kind == "launcher", **kw)  # type: ignore[arg-type]


def veh(vid: str, model: ModelInfo, nest_v: NestView | None, *, soc: float = 0.95, pos=None, state: str = "ready", t_ready: float = 0.0,
        airborne: bool = False) -> VehicleView:
    p = tuple(float(x) for x in (pos if pos is not None else (nest_v.pos if nest_v is not None else (0.0, 0.0, 0.0))))
    return VehicleView(vid, model.id, None if nest_v is None else nest_v.id, p, soc, soc * model.e_use_wh, t_ready, state, airborne)  # type: ignore[arg-type]


def snap(vehicles: list[VehicleView], nests: list[NestView], items: list[WorkItem], keepouts: list[Keepout] | None = None, **kw: Any) -> Snapshot:
    return Snapshot(1, 0.0, vehicles, dict(MODELS), {n.id: n for n in nests}, items, keepouts or [], **kw)


def station(wid: str, pos, *, tid: str = "t1", persistent: bool = False, workload: float = 300.0, **kw: Any) -> WorkItem:
    return WorkItem(wid, tid, "station", persistent=persistent, workload_s=workload, pos=tuple(float(x) for x in pos),  # type: ignore[arg-type]
                    needs={"role": "hover", "fw_ok": True}, **kw)


def path_item(wid: str, P, *, tid: str = "t1", group: str | None = None, **kw: Any) -> WorkItem:
    P = np.asarray(P, float)
    L = float(np.linalg.norm(np.diff(P[:, :2], axis=0), axis=1).sum())
    return WorkItem(wid, tid, "segment", workload_s=L / 8.0, path=P, group=group, needs={"role": "cruise"}, **kw)
