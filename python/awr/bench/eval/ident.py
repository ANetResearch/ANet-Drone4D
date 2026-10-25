"""识别表 I[k] = {(uav, actor)}（M27-FR-087、FR-176；AWR-05 §5.6.5、§5.4.10）。

识别真值行（`RunData.l1`）三种来源，键相同（uav、seq、actor、h_unocc_px、vis）：

- L1：已交付检测的真值映射（truth/l1.jsonl）。检测高度 `h_px` 已乘可见比例，识别谓词与 L2 的 `h_px_unocc` 同口径，取真值侧车记录的
  无遮挡投影高度与身体 5 点可见比例；
- L2 可见光：相机帧可见度归约（truth/l2.jsonl，M25 §6.6.1），`vis` 为可见像素比例；
- 热成像：热成像帧 `ground_truth.objects[]`（`kind = thermal`，由 `thermal_rows` 转为识别行），另带 `dT_app_k`。

识别判据：`h_unocc_px ≥ 16` 且 `vis ≥ 0.5`；热成像行另要求 `|dT_app_k| ≥ 1.0 K`（`eval_thresholds.v1`，D3-AC-61）。热成像行缺 `dT_app_k`
（环带为空，帧记录标 `no_ring`）时表观温差无法确定，不计识别。同一机同一帧的多个传感器取并集：任一传感器满足各自判据即识别。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from awr.bench.eval.thresholds import TH, Thresholds

__all__ = ["THERMAL_KIND", "build_ident", "identified", "is_thermal", "thermal_rows"]

THERMAL_KIND = "thermal"


def is_thermal(r: dict) -> bool:
    """识别行是否来自热成像帧：`kind = thermal`，或带 `dT_app_k` / `no_ring` 标记（只有热成像真值产出这两者）。"""
    return r.get("kind") == THERMAL_KIND or "dT_app_k" in r or "no_ring" in (r.get("flags") or ())


def identified(r: dict, th: Thresholds = TH) -> bool:
    """单行识别谓词：几何（无遮挡投影高度、可见比例）与热成像表观温差。阈值取闭区间（等于阈值即识别）。"""
    if not (float(r["h_unocc_px"]) >= th.h_px and float(r["vis"]) >= th.vis):
        return False
    if is_thermal(r):
        dt = r.get("dT_app_k")
        return dt is not None and abs(float(dt)) >= th.dT_app_k
    return True


def thermal_rows(objects: Iterable[dict], *, uav: str, sensor: str, seq: int, t_ns: int) -> list[dict]:
    """热成像帧 `ground_truth.objects[]`（`render.thermal_objects`）→ 识别行（只写入运行真值层）。

    `h_unocc_px` 取 `h_px_unocc`，`vis` 取 `visible_ratio`（可见像素比例，与 L2 可见光同口径）；`dT_app_k` 缺失时不写该键并保留 `no_ring`。"""
    rows = []
    for o in objects:
        r = {"uav": uav, "sensor": sensor, "kind": THERMAL_KIND, "seq": int(seq), "t_ns": int(t_ns), "actor": o["actor"],
             "h_unocc_px": int(o["h_px_unocc"]), "vis": float(o["visible_ratio"]), "bbox_xywh": list(o["bbox_xywh"])}
        if "dT_app_k" in o:
            r["dT_app_k"] = float(o["dT_app_k"])
        if o.get("flags"):
            r["flags"] = list(o["flags"])
        rows.append(r)
    return rows


def build_ident(run, th: Thresholds = TH) -> dict[int, set[tuple[str, str]]]:
    ident: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for r in run.l1:
        if identified(r, th):
            ident[int(r["seq"])].add((r["uav"], r["actor"]))
    return ident
