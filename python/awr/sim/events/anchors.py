"""锚点解析（M23 §6.3）：世界层 `awr-anchors@1`（M22，可缺省）与事件覆盖层（`extends = world`）合并，按 id 排序后求 JCS 哈希。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from awr.sim.events.jcs import jcs

__all__ = ["DEFAULT_RADIUS_M", "anchor_xy", "anchors_sha256", "load_overlay", "point_in_polygon", "resolve_anchors", "world_anchors"]

DEFAULT_RADIUS_M = {"point": 3.0, "door": 1.5, "curb": 4.0, "zone": 0.0}


def world_anchors(world_dir: Path) -> list[dict]:
    """世界包锚点图层（world.json 中 role = semantic、format = awr-anchors@1；缺省 semantic/anchors.json）；不存在返回空表。"""
    w = Path(world_dir)
    href = "semantic/anchors.json"
    try:
        doc = json.loads((w / "world.json").read_text(encoding="utf-8"))
        lay = next((L for L in doc.get("layers", []) if L.get("format") == "awr-anchors@1"), None)
        if lay:
            href = lay["href"]
    except (OSError, ValueError):
        pass
    p = w / href
    if not p.exists():
        return []
    return list(json.loads(p.read_text(encoding="utf-8")).get("anchors", []))


def load_overlay(src: Path | dict | None) -> dict | None:
    if src is None or isinstance(src, dict):
        return src
    return json.loads(Path(src).read_text(encoding="utf-8"))


def overlay_issues(ov: dict) -> list[str]:
    import jsonschema

    from awr.contracts._paths import schema_path

    sch = json.loads(schema_path("event/anchors.schema.json").read_text(encoding="utf-8"))
    return [f"/{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in jsonschema.Draft202012Validator(sch).iter_errors(ov)]


def resolve_anchors(world: list[dict], overlay: dict | None) -> dict[str, dict]:
    """同 id 覆盖、`remove` 删除、新增；返回 id → 锚点（按 id 排序的 dict）。"""
    out = {a["id"]: dict(a) for a in world}
    if overlay:
        for rid in overlay.get("remove", []):
            out.pop(rid, None)
        for a in overlay.get("anchors", []):
            out[a["id"]] = dict(a)
    return {k: out[k] for k in sorted(out)}


def anchors_sha256(anchors: dict[str, dict]) -> str:
    return hashlib.sha256(jcs([anchors[k] for k in sorted(anchors)])).hexdigest()


def anchor_xy(a: dict) -> tuple[float, float]:
    """点类锚点取 xy；zone 取多边形面积质心。"""
    if "xy" in a:
        return float(a["xy"][0]), float(a["xy"][1])
    P = np.asarray(a["polygon"], np.float64)
    x, y = P[:, 0], P[:, 1]
    xs, ys = np.roll(x, -1), np.roll(y, -1)
    cr = x * ys - xs * y
    A = cr.sum() / 2.0
    if abs(A) < 1e-12:
        return float(x.mean()), float(y.mean())
    return float(((x + xs) * cr).sum() / (6 * A)), float(((y + ys) * cr).sum() / (6 * A))


def point_in_polygon(P: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """偶奇规则（向量化）；P (K, 2)。"""
    P = np.asarray(P, np.float64)
    inside = np.zeros(np.shape(x), bool)
    for (x1, y1), (x2, y2) in zip(P, np.roll(P, -1, 0), strict=True):
        cond = (y1 > y) != (y2 > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            xc = (x2 - x1) * (y - y1) / (y2 - y1) + x1
        inside ^= cond & (x < xc)
    return inside
