"""`qa/report.json`（AWR-16 §9；`qa-report.schema.json`，snake_case）。非内容文件：在哈希与校验之后写出。"""

from __future__ import annotations

from ..package.jsonio import write_json

STAGE_NAMES = ("read", "ingest", "dtm", "normals", "classify", "tile", "dsm", "zones", "hash", "validate")


def first_screen_summary(roots_md: list[dict]) -> dict:
    """16 §4.11 两条规则：规则 G（生成器参考）与规则 R（Tier S 上限 1e5、Tier B/A 上限 4.5e5，各根取同一层）。"""
    k = len(roots_md)
    g_pts = g_bytes = 0
    for md in roots_md:
        a = md["anet"]
        L = a["firstScreenLevel"]
        g_pts += a["levelsPoints"][L]
        g_bytes += a["levelsByteEnd"][L]
    depth = max(md["hierarchy"]["depth"] for md in roots_md)

    def summed(L: int) -> tuple[int, int]:
        p = b = 0
        for md in roots_md:
            a = md["anet"]
            li = min(L, md["hierarchy"]["depth"])
            p += a["levelsPoints"][li]
            b += a["levelsByteEnd"][li]
        return p, b

    def rule_r(cap: float) -> dict:
        best = 0
        for L in range(depth + 1):
            if summed(L)[0] <= cap:
                best = L
        p, b = summed(best)
        return {"level": best, "points": p, "bytes": b}

    return {"rule_g": {"points": g_pts, "bytes": g_bytes, "requests": k}, "tier_s": rule_r(1e5), "tier_ba": rule_r(4.5e5)}


def build_report(*, world_id: str, content_version: str, status: str, generator: dict, inputs: list[dict],
                 stages: list[dict], ingest: dict, gates: list[dict], validation: dict, first_screen: dict,
                 sizes: dict) -> dict:
    return {"schema": "awr.world.qa.v1", "schema_version": "1.0.0", "world_id": world_id,
            "content_version": content_version, "status": status, "generator": generator, "inputs": inputs,
            "stages": stages, "ingest": ingest, "gates": gates, "validation": validation,
            "first_screen": first_screen, "sizes": sizes}


def write_report(path, report: dict) -> None:
    write_json(path, report)
