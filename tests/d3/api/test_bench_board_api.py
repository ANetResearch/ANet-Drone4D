"""BN-42（Env 运行列表项）、BN-47 评测结果、BN-83、BN-84、BN-97 原型榜接口（AWR-17 §18.4；M27 §6.7.9）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from awr.agentsdk.schemas import bench_validator
from awr.api.bench_main import create_app
from awr.bench.eval.aggregate import build_board, entry_details
from awr.bench.settings import BenchSettings

P = "/api/bench/v1"


def _ev(ec: float, valid: bool = True) -> dict:
    return {"components": {"ec": ec, "eu": 0.1, "ao": 0.5, "ce": 0.2}, "ranked_valid": valid, "invalid_reasons": [] if valid else ["actor_penetration"]}


@pytest.fixture()
def env(tmp_path: Path):
    s = BenchSettings(data_root=tmp_path / "d3", allowed_origins=("https://bench.example",), secure_cookie=False)
    run = s.dataset_dir / "crop-handoff-001" / "runs" / "crop-handoff-001-track_share-s1"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({"schema": "drone4d.bench.env_run.v1", "run_id": run.name, "world": "oryahovo-crop1",
                                                   "policy": {"kind": "reference", "name": "track_share"}, "tier": "gz-sim", "obs_level": "L1",
                                                   "ranked": True, "fleet": [{"uav": "uav-01"}, {"uav": "uav-02"}], "duration_s": 360.0}))
    (run / "evaluation.json").write_text(json.dumps({"schema": "drone4d.bench.evaluation.v1", **_ev(0.9)}))
    rows = []
    for sd in range(3):
        for eid, ec, base in (("team-a/v1", 0.9, False), ("team-b/v2", 0.3, False), ("baseline/track_share", 0.6, True)):
            rows.append({"entry_id": eid, "team": eid.split("/")[0], "display_name": eid, "baseline": base, "track": "std3-l1",
                         "lane": "rendezvous_handoff", "event_type": "rendezvous_handoff", "event": "crop-handoff-001", "seed": sd,
                         "evaluation": _ev(ec + 0.01 * sd, valid=not (eid == "team-b/v2" and sd == 0))})
    for v in ("proto-0", "proto-1"):
        d = s.data_root / "boards" / v
        d.mkdir(parents=True)
        (d / "board.json").write_text(json.dumps(build_board(rows, board_version=v, n_boot=300)))
        (d / "details.json").write_text(json.dumps(entry_details(rows)))
    (s.data_root / "boards" / "current").write_text("proto-0\n")
    app = create_app(s)
    c = TestClient(app)
    tok = app.state.auth.issue_token("alice", "researcher", ("d3",))[1]
    tok_inv = app.state.auth.issue_token("bob", "invited")[1]
    return c, {"Authorization": f"Bearer {tok}"}, {"Authorization": f"Bearer {tok_inv}"}


def test_env_run_listing_and_evaluation(env) -> None:
    c, h, h_inv = env
    items = c.get(f"{P}/runs", headers=h).json()["items"]
    it = next(i for i in items if i["run_id"] == "crop-handoff-001-track_share-s1")
    assert it["kind"] == "bench" and it["state"] == "EVALUATED" and it["policy"] == "track_share" and it["n_uav"] == 2
    assert it["evaluation"]["components"]["ec"] == 0.9 and it["evaluation"]["ranked_valid"] is True
    assert c.get(f"{P}/runs/{it['run_id']}", headers=h).json()["state"] == "EVALUATED"
    assert c.get(f"{P}/runs/{it['run_id']}/evaluation", headers=h).json()["components"]["ec"] == 0.9
    assert c.get(f"{P}/runs/nope/evaluation", headers=h).json()["code"] == 305
    assert c.get(f"{P}/runs/{it['run_id']}/evaluation", headers=h_inv).json()["code"] == 115      # 评测结果只对研究员及以上


def test_board_versions_and_leaderboard(env) -> None:
    c, _h, h_inv = env
    b = c.get(f"{P}/board", headers=h_inv).json()
    assert b["current"] == "proto-0" and [v["board_version"] for v in b["versions"]] == ["proto-0", "proto-1"]
    assert b["versions"][0]["weights"] is None and b["versions"][0]["n_entries"] == 3
    lb = c.get(f"{P}/leaderboard", headers=h_inv).json()
    assert lb["board_version"] == "proto-0" and not list(bench_validator("board").iter_errors(lb))
    e = {x["entry_id"]: x for x in lb["entries"]}
    assert e["team-a/v1"]["metrics"]["ec"]["rank"] == 1 and "rank" not in e["baseline/track_share"]["metrics"]["ec"]
    assert c.get(f"{P}/leaderboard?board_version=proto-1", headers=h_inv).json()["board_version"] == "proto-1"
    assert c.get(f"{P}/leaderboard?track=std9-l2", headers=h_inv).json()["detail"]["why"] == "track_unknown"
    assert c.get(f"{P}/leaderboard?lane=nope", headers=h_inv).json()["detail"]["why"] == "lane_unknown"
    assert c.get(f"{P}/leaderboard?board_version=zzz", headers=h_inv).json()["code"] == 305
    assert c.get(f"{P}/leaderboard").json()["code"] == 301


def test_board_entry_detail(env) -> None:
    c, _h, h_inv = env
    d = c.get(f"{P}/board/std3-l1/entries/team-b/v2", headers=h_inv).json()
    ev = d["lanes"][0]["events"][0]
    assert d["entry_id"] == "team-b/v2" and ev["event"] == "crop-handoff-001" and ev["n_runs"] == 3 and ev["invalid_runs"] == 1
    assert ev["invalid_reasons"] == ["actor_penetration"] and ev["metrics"]["ec"] == pytest.approx(0.315) and ev["repeat_delta"] is None
    assert c.get(f"{P}/board/std3-l1/entries/nobody", headers=h_inv).json()["code"] == 305


def test_board_closed_without_versions(tmp_path: Path) -> None:
    s = BenchSettings(data_root=tmp_path / "d3", allowed_origins=(), secure_cookie=False)
    app = create_app(s)
    tok = app.state.auth.issue_token("bob", "invited")[1]
    c = TestClient(app)
    h = {"Authorization": f"Bearer {tok}"}
    assert c.get(f"{P}/board", headers=h).json() == {"current": None, "versions": []}
    r = c.get(f"{P}/leaderboard", headers=h)
    assert r.status_code == 422 and r.json()["detail"]["why"] == "board_closed"
