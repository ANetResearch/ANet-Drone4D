"""BN-85、BN-95、BN-96、BN-98、BN-99 内部提交接口（AWR-17 §18.23；API-AC-096 的本机部分）：校验、配额、团队范围、撤回与可见性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from awr.api.bench_main import create_app
from awr.bench.boardstore import append_rows, publish
from awr.bench.settings import BenchSettings
from awr.bench.submissions import ACTIVE_PER_TEAM, SubmissionStore

P = "/api/bench/v1"
REQ = {"track": "std3-l1", "image_digest": "sha256:" + "b" * 64, "entry": {"cmd": ["python", "-m", "my_agent"]}, "notes": "first try"}


@pytest.fixture()
def env(tmp_path: Path):
    s = BenchSettings(data_root=tmp_path / "d3", allowed_origins=(), secure_cookie=False)
    b = s.data_root / "boards" / "proto-0"
    b.mkdir(parents=True)
    (b / "config.json").write_text(json.dumps({"track": {"fleet_ref": "3x-x500"}, "seeds": [1]}))
    ev = {"components": {"ec": 0.5, "eu": 0.1, "ao": 0.4, "ce": 0.2}, "ranked_valid": True, "invalid_reasons": []}
    append_rows(b, [{"entry_id": "baseline/x", "team": "D3 reference", "display_name": "X", "baseline": True, "track": "std3-l1",
                     "lane": "rendezvous_handoff", "event": "e1", "seed": 0, "evaluation": ev}])
    publish(b, n_boot=100)
    app = create_app(s)
    c = TestClient(app)
    tok = {n: app.state.auth.issue_token(n, role, proj)[1] for n, role, proj in
           (("ann", "invited", ("team-a",)), ("amy", "invited", ("team-a",)), ("bob", "invited", ("team-b",)), ("rae", "researcher", ("d3",)))}
    return c, {n: {"Authorization": f"Bearer {t}"} for n, t in tok.items()}, s


def test_create_validate_and_list(env) -> None:
    c, h, _s = env
    r = c.post(f"{P}/submissions", json=REQ, headers=h["ann"])
    assert r.status_code == 202 and r.json()["position"] == 1
    sid = r.json()["submission_id"]
    assert c.post(f"{P}/submissions", json={**REQ, "image_digest": "latest"}, headers=h["ann"]).json()["detail"]["why"] == "schema"
    assert c.post(f"{P}/submissions", json={**REQ, "state": "DONE"}, headers=h["ann"]).json()["detail"]["why"] == "fields"
    assert c.post(f"{P}/submissions", json={**REQ, "track": "std9"}, headers=h["ann"]).json()["detail"]["why"] == "track_unknown"
    assert c.post(f"{P}/submissions", json={**REQ, "lanes": ["nope"]}, headers=h["ann"]).json()["detail"]["why"] == "lane_unknown"
    d = c.get(f"{P}/submissions/{sid}", headers=h["amy"]).json()                          # 同团队可见
    assert d["state"] == "RECEIVED" and d["visibility"] == "private" and "_meta" not in d
    assert c.get(f"{P}/submissions/{sid}", headers=h["bob"]).json()["detail"]["why"] == "invited_scope"
    assert c.get(f"{P}/submissions/{sid}", headers=h["rae"]).status_code == 200
    assert [x["submission_id"] for x in c.get(f"{P}/submissions", headers=h["ann"]).json()["items"]] == [sid]
    assert c.get(f"{P}/submissions", headers=h["bob"]).json()["items"] == []
    assert len(c.get(f"{P}/submissions", headers=h["rae"]).json()["items"]) == 1


def test_quota_withdraw_and_visibility(env) -> None:
    c, h, s = env
    sids = [c.post(f"{P}/submissions", json=REQ, headers=h["ann"]).json()["submission_id"] for _ in range(ACTIVE_PER_TEAM)]
    r = c.post(f"{P}/submissions", json=REQ, headers=h["amy"])
    assert r.status_code == 429 and r.json()["detail"]["why"] == "submit_quota"
    assert c.post(f"{P}/submissions", json=REQ, headers=h["bob"]).status_code == 202          # 配额按团队
    assert c.delete(f"{P}/submissions/{sids[0]}", headers=h["bob"]).json()["detail"]["why"] == "invited_scope"
    assert c.delete(f"{P}/submissions/{sids[0]}", headers=h["ann"]).status_code == 204
    assert c.get(f"{P}/submissions/{sids[0]}", headers=h["ann"]).json()["state"] == "WITHDRAWN"
    assert c.delete(f"{P}/submissions/{sids[0]}", headers=h["ann"]).json()["code"] == 680
    assert c.post(f"{P}/submissions/{sids[0]}/visibility", json={"visibility": "board"}, headers=h["ann"]).json()["code"] == 680
    st = SubmissionStore(s.data_root / "submissions")
    for state in ("SCANNING", "VALIDATING", "EVALUATING"):
        st.transition(sids[1], state)
    assert c.delete(f"{P}/submissions/{sids[1]}", headers=h["ann"]).json()["code"] == 680      # EVALUATING 起不可撤回
    ev = {"components": {"ec": 0.9, "eu": 0.3, "ao": 0.6, "ce": 0.4}, "ranked_valid": True, "invalid_reasons": []}
    append_rows(s.data_root / "boards/proto-0", [{"entry_id": f"team-a/{sids[1]}", "team": "team-a", "display_name": "team-a", "baseline": False,
                                                 "track": "std3-l1", "lane": "rendezvous_handoff", "event": "e1", "seed": 0, "submission_id": sids[1],
                                                 "evaluation": ev}])
    st.transition(sids[1], "SCORING")
    st.transition(sids[1], "DONE", result={"lanes": []})
    lb = {e["entry_id"] for e in c.get(f"{P}/leaderboard", headers=h["bob"]).json()["entries"]}
    assert lb == {"baseline/x"}                                                                # private：不上榜
    r = c.post(f"{P}/submissions/{sids[1]}/visibility", json={"visibility": "board"}, headers=h["ann"])
    assert r.status_code == 200 and r.json()["visibility"] == "board"
    lb = {e["entry_id"] for e in c.get(f"{P}/leaderboard", headers=h["bob"]).json()["entries"]}
    assert lb == {"baseline/x", f"team-a/{sids[1]}"}
    c.post(f"{P}/submissions/{sids[1]}/visibility", json={"visibility": "private"}, headers=h["ann"])
    assert {e["entry_id"] for e in c.get(f"{P}/leaderboard", headers=h["bob"]).json()["entries"]} == {"baseline/x"}


def test_ctl_board_publish_and_submission_create(tmp_path: Path, monkeypatch, capsys) -> None:
    from awr.bench import ctl

    src = tmp_path / "round"
    src.mkdir()
    (src / "config.json").write_text(json.dumps({"track": {"fleet_ref": "3x-x500"}}))
    ev = {"components": {"ec": 0.5, "eu": 0.1, "ao": 0.4, "ce": 0.2}, "ranked_valid": True, "invalid_reasons": []}
    append_rows(src, [{"entry_id": "baseline/x", "team": "D3 reference", "display_name": "X", "baseline": True, "track": "std3-l1",
                       "lane": "rendezvous_handoff", "event": "e1", "seed": 0, "evaluation": ev}])
    monkeypatch.setenv("AWR_BENCH_DATA_ROOT", str(tmp_path / "d3"))
    assert ctl.main(["board", "publish", str(src), "--version", "round-1", "--current", "--n-boot", "100"]) == 0
    assert (tmp_path / "d3/boards/current").read_text().strip() == "round-1"
    assert (tmp_path / "d3/boards/round-1/board.json").exists()
    assert ctl.main(["board", "publish", str(tmp_path / "d3/boards/round-1"), "--n-boot", "100"]) == 0           # 原地重算
    capsys.readouterr()
    assert ctl.main(["submission", "create", "--team", "t", "--owner", "o", "--track", "std3-l1", "--digest", "sha256:" + "c" * 64,
                     "--cmd", "python -m drone4d.serve pkg:Agent", "--env", "MODE=eval", "--visibility", "board"]) == 0
    sid = capsys.readouterr().out.strip()
    d = SubmissionStore(tmp_path / "d3/submissions").get(sid)
    assert d["entry"] == {"cmd": ["python", "-m", "drone4d.serve", "pkg:Agent"], "env": {"MODE": "eval"}} and d["visibility"] == "board"
    assert ctl.main(["submission", "create", "--team", "t", "--owner", "o", "--track", "nope", "--digest", "sha256:" + "c" * 64, "--cmd", "x"]) == 1
