"""榜单测试的评测行构造（D3-AC-57）。"""

from __future__ import annotations


def row(eid: str, seed: int, ec: float, eu: float, ao: float, ce: float, *, event: str = "ev1", track: str = "std3-l1",
        lane: str = "rendezvous_handoff", baseline: bool = False, valid: bool = True, ranked: bool = True, reasons: list[str] | None = None,
        **extra) -> dict:
    return {"entry_id": eid, "team": extra.pop("team", eid.split("/")[0]), "display_name": eid, "baseline": baseline, "track": track, "lane": lane,
            "event_type": "rendezvous_handoff", "event": event, "seed": seed,
            "evaluation": {"components": {"ec": ec, "eu": eu, "ao": ao, "ce": ce}, "ranked": ranked, "ranked_valid": valid,
                           "invalid_reasons": reasons or ([] if valid else ["actor_penetration"])}, **extra}


def entries(board: dict) -> dict[str, dict]:
    return {e["entry_id"]: e for e in board["entries"]}
