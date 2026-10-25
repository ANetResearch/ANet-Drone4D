"""排行榜 v1 测试夹具：以 `configs/bench.d3/board-v1/board.json` 封板（8 个 test 事件，K = 5，R = 2），写服务端配置与 EvalStore。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from awr.bench.eval.aggregate import board_config, load_board_template, seal_board

TEST_EVENTS = {"reid-t01": "reid_occlusion", "reid-t02": "reid_occlusion", "handoff-t01": "rendezvous_handoff", "handoff-t02": "rendezvous_handoff",
               "vehicle-t01": "vehicle_track", "vehicle-t02": "vehicle_track", "search-t01": "target_search", "search-t02": "target_search"}
LANE_EVENTS = {"reid": ["reid-t01", "reid-t02"], "handoff": ["handoff-t01", "handoff-t02"], "vehicle": ["vehicle-t01", "vehicle-t02"],
               "search": ["search-t01", "search-t02"]}
PLATFORM = {k: "sha256:" + c * 64 for k, c in (("gz", "1"), ("m25_worker", "2"), ("env", "3"), ("baseline", "4"))}
SEAL = {"test_split_version": "splits-sha256:" + "a" * 16, "platform": PLATFORM, "gpu_model": "NVIDIA A40", "cpu_isa_level": "x86-64-v3",
        "sealed_utc": "2026-10-19T00:00:00Z"}


def seal(root: Path, *, template: dict | None = None, store: dict | None = None, **over) -> tuple[Path, dict, dict]:
    """→ (榜单版本目录 `<root>/boards/v1`，服务端配置，EvalStore 文档)。"""
    cfg = template or load_board_template()
    lock, store = seal_board(cfg, TEST_EVENTS, store=store, **{**SEAL, **over})
    es = root / "evalstore" / f"{cfg['board_version']}.json"
    es.parent.mkdir(parents=True, exist_ok=True)
    es.write_text(json.dumps(store), encoding="utf-8")
    os.chmod(es, 0o600)
    d = root / "boards" / cfg["board_version"]
    d.mkdir(parents=True, exist_ok=True)
    full = board_config(cfg, lock, es)
    (d / "config.json").write_text(json.dumps(full, ensure_ascii=False, indent=1), encoding="utf-8")
    return d, full, store


def v1_rows(eid: str, mu: tuple[float, float, float, float], *, track: str = "std3-l2", lanes: tuple[str, ...] = ("handoff",), k: int = 5, r: int = 2,
            noise: float = 0.0, invalid: int = 0, rng_seed: int = 0, baseline: bool = False, **extra) -> list[dict]:
    """一个条目在若干赛道上的全部 K × R 个评测行（`seed` 为种子序号）；前 `invalid` 个 Run 标为 ranked_valid = false（分量记 0）。"""
    g = np.random.default_rng(rng_seed)
    out = []
    for lane in lanes:
        for ev in LANE_EVENTS[lane]:
            for s in range(k):
                for rep in range(r):
                    c = [float(np.clip(m + (g.normal(0, noise) if noise else 0.0), 0, 1)) for m in mu]
                    bad = len([x for x in out if x["lane"] == lane]) < invalid
                    comp = dict(zip(("ec", "eu", "ao", "ce"), [0.0] * 4 if bad else c, strict=True))
                    out.append({"entry_id": eid, "team": extra.get("team", eid.split("/")[0]), "display_name": eid, "baseline": baseline, "track": track,
                                "lane": lane, "event_type": TEST_EVENTS[ev], "event": ev, "seed": s, "rep": rep,
                                "first_listed_unix_ns": extra.get("first_listed_unix_ns", 0),
                                "evaluation": {"components": comp, "ranked": True, "ranked_valid": not bad,
                                               "invalid_reasons": ["actor_penetration"] if bad else [], "physics": "realistic"}})
    return out
