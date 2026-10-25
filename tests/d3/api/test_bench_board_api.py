"""BN-42（Env 运行列表项）、BN-47 评测结果、BN-83、BN-84、BN-97 原型榜接口（AWR-17 §18.4；M27 §6.7.9）；D3-AC-57 的接口侧条款：
`weights = null` 时任何响应不出现合成主得分（反向断言），旧榜单版本只读。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# D2 环境（.venv）无 D3 依赖：bench API 与回放读取经 awr.bench.recorder.foxglove 导入 foxglove_schemas_protobuf
pytest.importorskip("foxglove_schemas_protobuf")

from awr.agentsdk.schemas import bench_validator
from awr.api.bench_main import create_app
from awr.bench.boardstore import append_rows
from awr.bench.eval.aggregate import build_board, entry_details
from awr.bench.settings import BenchSettings
from awr.bench.submissions import SubmissionStore

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


def test_env_run_with_run_manifest_falls_back_to_env_item(env, tmp_path: Path) -> None:
    """正式 Env 运行同时写 run_manifest.v1（不含 state）与 manifest.json：列表与 BN-43 回退到 env_run_item（K12，B20 后续事项 3）。"""
    c, h, _ = env
    base = tmp_path / "d3" / "dataset" / "crop-handoff-001" / "runs"
    for rid, evaluated in (("crop-handoff-001-track_share-s2", True), ("crop-handoff-001-track_share-s3", False)):
        run = base / rid
        run.mkdir(parents=True)
        (run / "run_manifest.json").write_text(json.dumps({"schema": "drone4d.bench.run_manifest.v1", "run_id": rid, "attempt": 1,
                                                           "split": "dev", "ranked": True}))
        (run / "manifest.json").write_text(json.dumps({"schema": "drone4d.bench.env_run.v1", "run_id": rid, "world": "oryahovo-crop1",
                                                       "policy": {"kind": "reference", "name": "track_share"}, "tier": "gz-sim",
                                                       "obs_level": "L1", "ranked": True, "fleet": [{"uav": "uav-01"}], "duration_s": 360.0}))
        if evaluated:
            (run / "evaluation.json").write_text(json.dumps({"schema": "drone4d.bench.evaluation.v1", **_ev(0.7)}))
    items = {i["run_id"]: i for i in c.get(f"{P}/runs", headers=h).json()["items"]}
    s2, s3 = items["crop-handoff-001-track_share-s2"], items["crop-handoff-001-track_share-s3"]
    assert s2["state"] == "EVALUATED" and s2["kind"] == "bench" and s2["world_id"] == "oryahovo-crop1" and s2["evaluation"]["components"]["ec"] == 0.7
    assert s3["state"] == "SUCCEEDED" and s3["evaluation"] is None
    assert c.get(f"{P}/runs/crop-handoff-001-track_share-s2", headers=h).json()["state"] == "EVALUATED"
    assert c.get(f"{P}/runs/crop-handoff-001-track_share-s3", headers=h).json()["state"] == "SUCCEEDED"
    assert "UNKNOWN" not in {i["state"] for i in items.values()}
    assert [i["run_id"] for i in c.get(f"{P}/runs?state=EVALUATED", headers=h).json()["items"]] == [
        "crop-handoff-001-track_share-s1", "crop-handoff-001-track_share-s2"]


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
    assert c.get(f"{P}/leaderboard").json()["code"] == 305                                  # 匿名访问 404（D3-AC-43，tests/d3/board/test_visibility.py）


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


# ---- D3-AC-57：weights = null 时任何接口响应中不出现合成主得分（反向断言） -------------------------------------------------------

_COMPOSITE_KEYS = {"composite", "composite_ci95", "s", "score", "main_score", "weighted"}


def _composite_free(x, path: str = "$") -> list[str]:
    """→ 违例路径：`composite` 只允许为 null（契约 score.v1 的 weights = null 分支），其余合成字段不得出现，`weights` 只允许为 null。"""
    bad = []
    if isinstance(x, dict):
        for k, v in x.items():
            p = f"{path}.{k}"
            if (k == "composite" and v is not None) or k in _COMPOSITE_KEYS - {"composite"} or (k == "weights" and v is not None):
                bad.append(p)
            bad += _composite_free(v, p)
    elif isinstance(x, list):
        for i, v in enumerate(x):
            bad += _composite_free(v, f"{path}[{i}]")
    return bad


def test_no_composite_score_in_any_board_response(env) -> None:
    c, h, h_inv = env
    root = c.app.state.settings.data_root
    st = SubmissionStore(root / "submissions")
    sid = st.create({"track": "std3-l1", "image_digest": "sha256:" + "a" * 64, "entry": {"cmd": ["python", "-m", "agent"]}}, owner="alice",
                    team="d3", board_version="proto-0", tracks={"std3-l1"}, lanes={"rendezvous_handoff"})["submission_id"]
    for state in ("SCANNING", "VALIDATING", "EVALUATING", "SCORING"):
        st.transition(sid, state)
    lane = {"lane": "rendezvous_handoff", "metrics": {}, "runs": {"total": 3, "valid": 3, "invalid": 0, "platform_rerun": 0}, "status": "valid",
            "composite": None}                                                              # subworker 写出的形态
    st.transition(sid, "DONE", result={"lanes": [lane]})
    urls = ["/board", "/leaderboard", "/leaderboard?board_version=proto-1", "/leaderboard?track=std3-l1&lane=rendezvous_handoff",
            "/board/std3-l1/entries/team-a/v1", "/board/std3-l1/entries/baseline/track_share?board_version=proto-1", "/submissions",
            f"/submissions/{sid}", "/runs", "/runs/crop-handoff-001-track_share-s1/evaluation"]
    for u in urls:
        r = c.get(P + u, headers=h)
        assert r.status_code == 200, u
        assert _composite_free(r.json()) == [], u
    b = c.get(f"{P}/board", headers=h_inv).json()
    assert all(v["weights"] is None for v in b["versions"])
    assert not [r.path for r in c.app.routes if "compose" in getattr(r, "path", "")]          # 合成接口只在 ctl（不经 HTTP）
    assert c.get(f"{P}/board/compose", headers=h).status_code in (404, 405)
    assert _composite_free({"lanes": [{"composite": 0.5}], "s": 80}) == ["$.lanes[0].composite", "$.s"]   # 断言本身能发现合成字段


# ---- D3-AC-57：评分版本或赛道集合变更产生新榜单版本，旧版本只读 -------------------------------------------------------------

def test_old_board_version_is_read_only_through_the_api(env) -> None:
    c, h, h_inv = env
    boards = c.app.state.settings.data_root / "boards"
    (boards / "current").write_text("proto-1\n")                                              # 新版本发布为当前版本：proto-0 冻结
    old = boards / "proto-0"
    (old / "config.json").write_text("{}")
    st = SubmissionStore(c.app.state.settings.data_root / "submissions")
    sid = st.create({"track": "std3-l1", "image_digest": "sha256:" + "a" * 64, "entry": {"cmd": ["python", "-m", "agent"]}}, owner="alice",
                    team="d3", board_version="proto-0", tracks={"std3-l1"}, lanes={"rendezvous_handoff"})["submission_id"]
    for state in ("SCANNING", "VALIDATING", "EVALUATING", "SCORING"):
        st.transition(sid, state)
    st.transition(sid, "DONE", result={"lanes": []})
    append_rows(old, [{"entry_id": f"d3/{sid}", "team": "d3", "display_name": sid, "baseline": False, "track": "std3-l1", "lane": "rendezvous_handoff",
                       "event": "crop-handoff-001", "seed": 0, "submission_id": sid, "evaluation": _ev(0.99)}])
    before = {f: (old / f).read_bytes() for f in ("board.json", "details.json")}
    idx = c.get(f"{P}/board", headers=h_inv).json()
    assert idx["current"] == "proto-1" and [v["current"] for v in idx["versions"]] == [False, True]
    lb = c.get(f"{P}/leaderboard?board_version=proto-0", headers=h_inv).json()
    assert lb == json.loads(before["board.json"])                                             # 旧版本按原样可读
    for u in ("/board", "/leaderboard", "/leaderboard?board_version=proto-0", "/board/std3-l1/entries/team-a/v1?board_version=proto-0"):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert c.request(method, P + u, headers=h, json={}).status_code == 405, (method, u)
    r = c.post(f"{P}/submissions/{sid}/visibility", json={"visibility": "board"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == 680 and r.json()["detail"] == {"why": "board_frozen", "board_version": "proto-0"}
    assert st.get(sid)["visibility"] == "private"
    assert {f: (old / f).read_bytes() for f in before} == before                              # 旧版本文件不被重算
    assert c.get(f"{P}/leaderboard", headers=h_inv).json()["board_version"] == "proto-1"
