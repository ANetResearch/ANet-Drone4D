"""D3-AC-43 榜单可见性、撤回下线与 gz 档限定（AWR-17 §18.4、§18.23、API-AC-097；M27 §6.7.9"结果可见性"）。

- 最低角色：BN-83、BN-84、BN-97 实际执行的最低角色为 `invited`（OpenAPI `x-d3-role` 与角色对照），受邀成员与研究员可读；
- 匿名与公开站：无凭据、凭据无效或已撤销的请求，以及经公开站主机名到达的请求（即使带有效令牌）一律 404 `305`，与未注册路由同形，
  并写 `deny` 审计；公开站 nginx 不转发 bench-api；
- 撤回下线：提交者把可见性改回 `private` 后条目立即不再返回（接口读取侧），`delist_withdrawn` 在 24 h 时限内重算当前版本，旧版本
  只读、只由读取侧隐藏；评测行与评分记录保留；
- gz 档限定：正式榜只收 gz 锁步档（`realistic`）的正式运行：非 gz 档的评测行拒绝写入、不计入，非 gz 档的轮次配置拒绝发布。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("foxglove_schemas_protobuf")          # bench-api 依赖 D3 环境（.venv-d3）；D2 环境下整模块跳过

from fastapi.testclient import TestClient

from awr.api.bench_main import create_app
from awr.bench.api.deps import role_matrix
from awr.bench.boardstore import (
    LISTING,
    WITHDRAW_SLA_S,
    BoardPhysicsError,
    append_rows,
    board_physics,
    compose_board,
    delist_withdrawn,
    gz_row,
    load_rows,
    publish,
    slim_evaluation,
)
from awr.bench.settings import BenchSettings
from awr.bench.submissions import SubmissionStore

sys.path.insert(0, str(Path(__file__).parent))
from _rows import entries, row

P = "/api/bench/v1"
REPO = Path(__file__).resolve().parents[3]
BOARD_OPS = (("GET", "/board"), ("GET", "/leaderboard"), ("GET", "/board/{track}/entries/{entry_id}"))
CFG = {"physics": "realistic", "track": {"fleet_ref": "3x-x500"}, "seeds": [1, 2, 3]}
H = 3_600 * 10**9


def _sub_rows(team: str, sid: str, ec: float, n: int = 3, **extra) -> list[dict]:
    return [row(f"{team}/{sid}", s, ec + 0.01 * s, 0.4, 0.5, 0.3, team=team, submission_id=sid, **extra) for s in range(n)]


def _board(root: Path, v: str, cfg: dict | None = None) -> Path:
    d = root / "boards" / v
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(cfg or CFG))
    append_rows(d, [row("baseline/x", s, 0.5, 0.2, 0.3, 0.2, baseline=True) for s in range(3)])
    return d


def _done(st: SubmissionStore, team: str, v: str, visibility: str = "board") -> str:
    """登记到榜单版本 v 并走完流水线的提交（DONE，按需进入内部榜）。"""
    doc = st.create({"track": "std3-l1", "image_digest": "sha256:" + "c" * 64, "entry": {"cmd": ["python", "-m", "agent"]}}, owner=team,
                    team=team, board_version=v, tracks={"std3-l1"}, lanes={"rendezvous_handoff"})
    sid = doc["submission_id"]
    for s, e in (("SCANNING", "RECEIVED"), ("VALIDATING", "SCANNING"), ("EVALUATING", "VALIDATING"), ("SCORING", "EVALUATING"), ("DONE", "SCORING")):
        st.transition(sid, s, expect=e, result={"lanes": []} if s == "DONE" else None)
    st.set_visibility(sid, visibility)
    return sid


@pytest.fixture()
def env(tmp_path: Path):
    """当前版本 v1（两个团队的提交已上榜）与旧版本 v0（team-a 的早期提交已上榜）。"""
    s = BenchSettings(data_root=tmp_path / "d3", allowed_origins=(), secure_cookie=False)
    st = SubmissionStore(s.data_root / "submissions")
    v0 = _board(s.data_root, "v0")
    old = _done(st, "team-a", "v0")
    append_rows(v0, _sub_rows("team-a", old, 0.7))
    publish(v0, st.board_view(), n_boot=200)
    v1 = _board(s.data_root, "v1")
    sa, sb = _done(st, "team-a", "v1"), _done(st, "team-b", "v1")
    append_rows(v1, _sub_rows("team-a", sa, 0.8) + _sub_rows("team-b", sb, 0.6))
    publish(v1, st.board_view(), n_boot=200)
    (s.data_root / "boards" / "current").write_text("v1\n")
    app = create_app(s)
    app.state.submissions = st
    tok = {n: app.state.auth.issue_token(n, role, proj) for n, role, proj in
           (("ann", "invited", ("team-a",)), ("bob", "invited", ("team-b",)), ("rae", "researcher", ("d3",)))}
    hdr = {n: {"Authorization": f"Bearer {t[1]}"} for n, t in tok.items()}
    return {"c": TestClient(app), "app": app, "s": s, "st": st, "h": hdr, "tid": {n: t[0] for n, t in tok.items()}, "ids": (old, sa, sb)}


def _lb(c: TestClient, h: dict, **q) -> set[str]:
    r = c.get(f"{P}/leaderboard", headers=h, params=q)
    assert r.status_code == 200, r.json()
    return {e["entry_id"] for e in r.json()["entries"]}


# ---- 最低角色 invited ----------------------------------------------------------------------------------------------------------

def test_board_endpoints_minimum_role_is_invited(env) -> None:
    c, h = env["c"], env["h"]
    rows = {(r["method"], r["path"]): r for r in role_matrix(env["app"])}
    for op in BOARD_OPS:
        assert rows[op]["role"] == "invited" == rows[op]["declared"], rows[op]
    spec = c.get(f"{P}/openapi.json").json()["paths"]
    for m, path in BOARD_OPS:
        assert spec[P + path][m.lower()]["x-d3-role"] == "invited"
    _old, sa, _sb = env["ids"]
    for who in ("ann", "bob", "rae"):
        assert c.get(f"{P}/board", headers=h[who]).status_code == 200
        assert f"team-a/{sa}" in _lb(c, h[who])
        assert c.get(f"{P}/board/std3-l1/entries/team-a/{sa}", headers=h[who]).status_code == 200


# ---- 匿名与公开站 404 ----------------------------------------------------------------------------------------------------------

def _shape(r) -> dict:
    """problem+json 去掉逐请求的 request_id。"""
    return {k: v for k, v in r.json().items() if k != "request_id"}


def _paths(sa: str) -> list[str]:
    return [f"{P}/board", f"{P}/leaderboard", f"{P}/leaderboard?board_version=v0", f"{P}/board/std3-l1/entries/team-a/{sa}"]


def test_anonymous_access_is_404_like_an_unregistered_route(env) -> None:
    c, app = env["c"], env["app"]
    _old, sa, _sb = env["ids"]
    ghost = c.get(f"{P}/no-such-route")
    assert ghost.status_code == 404 and ghost.json()["code"] == 305
    revoked = app.state.auth.issue_token("eve", "invited", ("team-e",))
    assert app.state.auth.revoke_token(revoked[0])
    cases = [{}, {"Authorization": "Bearer " + "d3b" + "t_not-a-real-token"}, {"Authorization": f"Bearer {revoked[1]}"}, {"Cookie": "d3_session=forged"}]
    for hdr in cases:
        for path in _paths(sa):
            r = c.get(path, headers=hdr)
            assert r.status_code == 404 and r.json()["code"] == 305, (path, hdr.keys(), r.json())
            assert _shape(r) == _shape(ghost)                                                 # 与未注册路由同形，不暴露接口存在
            assert r.headers["content-type"] == ghost.headers["content-type"]
    deny = [x for x in app.state.auth.audit.read() if x["action"] == "deny" and x.get("why") == "anonymous"]
    assert len(deny) == len(cases) * len(_paths(sa)) and all(x["principal"] is None and x["code"] == 305 for x in deny)
    assert not any("d3b" + "t_" in json.dumps(x) for x in deny)                                    # 审计不含令牌


def test_public_site_access_is_404_even_with_a_valid_token(env) -> None:
    c, h, app = env["c"], env["h"], env["app"]
    _old, sa, _sb = env["ids"]
    ghost = _shape(c.get(f"{P}/no-such-route"))
    for hdr in ({"Host": "anet4d.agentnetwork.org.cn"}, {"Host": "ANET4D.agentnetwork.org.cn:443"},
                {"X-Forwarded-Host": "anet4d.agentnetwork.org.cn, lab.internal"},
                {"Host": "drone4d.agentnetwork.org.cn"}, {"X-Forwarded-Host": "drone4d.agentnetwork.org.cn"}):   # 旧域名（迁移期 301）
        for path in _paths(sa):
            for who in ("ann", "rae"):
                r = c.get(path, headers={**h[who], **hdr})
                assert r.status_code == 404 and _shape(r) == ghost, (path, hdr)
    assert any(x.get("why") == "public_site" for x in app.state.auth.audit.read())
    assert c.get(f"{P}/board", headers={**h["ann"], "Host": "lab.internal"}).status_code == 200       # 内部工作区入口照常
    app.state.settings.extra["public_hosts"] = ("demo.example",)                               # 部署可覆盖公开站主机名
    assert c.get(f"{P}/board", headers={**h["ann"], "Host": "demo.example"}).status_code == 404
    assert c.get(f"{P}/board", headers={**h["ann"], "Host": "anet4d.agentnetwork.org.cn"}).status_code == 200


def test_public_site_nginx_never_forwards_to_bench_api() -> None:
    """公开站（emax）站点文件不含 bench-api 上游（端口 18650）；`/api/bench/*` 落到 show-api，由其按未注册路由返回 404。
    现用域名 anet4d 的站点文件与旧域名 drone4d 的跳转站点文件（迁移期只做 301）都检查。"""
    emax = REPO / "tools" / "deploy" / "emax"
    confs = [p for p in (emax / "anet4d.agentnetwork.org.cn.conf", emax / "drone4d.agentnetwork.org.cn.conf") if p.exists()]
    assert confs and confs[0].name.startswith("anet4d."), confs
    for p in confs:
        code = "\n".join(ln.split("#", 1)[0] for ln in p.read_text(encoding="utf-8").splitlines())
        assert "18650" not in code and "bench" not in code.lower().replace("/api/bench/", ""), p.name
        assert "dmax" not in code.lower(), p.name


# ---- 撤回下线（24 h） ----------------------------------------------------------------------------------------------------------

def test_withdrawal_hides_the_entry_at_once_and_delists_within_24h(env) -> None:
    c, h, s, st = env["c"], env["h"], env["s"], env["st"]
    _old, sa, sb = env["ids"]
    v1 = s.data_root / "boards" / "v1"
    assert json.loads((v1 / LISTING).read_text())["entries"] == {f"team-a/{sa}": sa, f"team-b/{sb}": sb}
    n_rows = len(load_rows(v1))
    assert {f"team-a/{sa}", f"team-b/{sb}", "baseline/x"} == _lb(c, h["bob"])
    st.set_visibility(sa, "private")                                    # 撤回（未经接口、榜单尚未重算）
    t_withdraw = st.get(sa)["_meta"]["updated_unix_ns"]
    assert f"team-a/{sa}" in entries(json.loads((v1 / "board.json").read_text()))
    assert _lb(c, h["bob"]) == {f"team-b/{sb}", "baseline/x"}           # 读取侧立即下线
    assert c.get(f"{P}/board/std3-l1/entries/team-a/{sa}", headers=h["rae"]).json()["code"] == 305
    cur = next(v for v in c.get(f"{P}/board", headers=h["bob"]).json()["versions"] if v["board_version"] == "v1")
    assert cur["n_entries"] == 2
    rep = delist_withdrawn(s.data_root / "boards", st.board_view(), n_boot=200, now_ns=t_withdraw + 23 * H,
                           updated={sa: t_withdraw})
    v1_rep = next(x for x in rep if x["board_version"] == "v1")
    assert v1_rep["action"] == "republished" and v1_rep["entries"] == [{"entry_id": f"team-a/{sa}", "submission_id": sa, "lag_s": 23 * 3600.0,
                                                                         "within_sla": True}]
    b = json.loads((v1 / "board.json").read_text())
    assert set(entries(b)) == {f"team-b/{sb}", "baseline/x"} and entries(b)[f"team-b/{sb}"]["metrics"]["ec"]["rank"] == 1
    assert json.loads((v1 / LISTING).read_text())["entries"] == {f"team-b/{sb}": sb}
    assert len(load_rows(v1)) == n_rows and "result" in st.get(sa)            # 评测行与评分记录保留
    assert not [x for x in delist_withdrawn(s.data_root / "boards", st.board_view(), n_boot=200) if x["board_version"] == "v1"]
    late = delist_withdrawn(s.data_root / "boards", st.board_view(), n_boot=200, now_ns=t_withdraw + WITHDRAW_SLA_S * 10**9 + 1,
                            updated={_old: t_withdraw})
    assert late == []                                                   # 旧版本 team-a 的早期提交仍上榜：无须下线
    st.set_visibility(sa, "board")                                      # 重新进入内部榜：读取侧恢复，重算后进入 board.json
    publish(v1, st.board_view(), n_boot=200)
    assert f"team-a/{sa}" in _lb(c, h["bob"])


def test_withdrawal_via_visibility_endpoint_republishes_the_current_version(env) -> None:
    c, h, s = env["c"], env["h"], env["s"]
    _old, sa, sb = env["ids"]
    r = c.post(f"{P}/submissions/{sa}/visibility", json={"visibility": "private"}, headers=h["ann"])
    assert r.status_code == 200 and r.json()["visibility"] == "private"
    assert set(entries(json.loads((s.data_root / "boards" / "v1" / "board.json").read_text()))) == {f"team-b/{sb}", "baseline/x"}
    assert _lb(c, h["rae"]) == {f"team-b/{sb}", "baseline/x"}
    assert c.post(f"{P}/submissions/{sb}/visibility", json={"visibility": "private"}, headers=h["ann"]).json()["detail"]["why"] == "invited_scope"


def test_old_board_versions_stay_read_only_and_hide_withdrawn_entries(env) -> None:
    c, h, s, st = env["c"], env["h"], env["s"], env["st"]
    old, _sa, _sb = env["ids"]
    v0 = s.data_root / "boards" / "v0"
    digest = {f: hashlib.sha256((v0 / f).read_bytes()).hexdigest() for f in ("board.json", "details.json", LISTING)}
    assert f"team-a/{old}" in _lb(c, h["ann"], board_version="v0")
    st.set_visibility(old, "private")
    t = st.get(old)["_meta"]["updated_unix_ns"]
    assert _lb(c, h["ann"], board_version="v0") == {"baseline/x"}
    assert c.get(f"{P}/board/std3-l1/entries/team-a/{old}", headers=h["ann"], params={"board_version": "v0"}).json()["code"] == 305
    rep = delist_withdrawn(s.data_root / "boards", st.board_view(), n_boot=200, now_ns=t + H, updated={old: t})
    assert rep == [{"board_version": "v0", "action": "hidden", "entries": [{"entry_id": f"team-a/{old}", "submission_id": old, "lag_s": 3600.0,
                                                                            "within_sla": True}]}]
    assert digest == {f: hashlib.sha256((v0 / f).read_bytes()).hexdigest() for f in digest}          # 旧版本制品不变
    v0_listed = next(v for v in c.get(f"{P}/board", headers=h["ann"]).json()["versions"] if v["board_version"] == "v0")
    assert v0_listed["n_entries"] == 1


def test_withdrawal_on_an_old_board_version_is_allowed_via_the_endpoint(env) -> None:
    """旧版本只读：再次进入榜单 680 `board_frozen`；撤回（private）仍允许，旧版本制品不重算，读取侧立即隐藏。"""
    c, h, s, st = env["c"], env["h"], env["s"], env["st"]
    old, _sa, _sb = env["ids"]
    v0 = s.data_root / "boards" / "v0"
    before = {f: (v0 / f).read_bytes() for f in ("board.json", "details.json", LISTING)}
    r = c.post(f"{P}/submissions/{old}/visibility", json={"visibility": "private"}, headers=h["ann"])
    assert r.status_code == 200 and r.json()["visibility"] == "private"
    assert _lb(c, h["bob"], board_version="v0") == {"baseline/x"}
    assert {f: (v0 / f).read_bytes() for f in before} == before
    r = c.post(f"{P}/submissions/{old}/visibility", json={"visibility": "board"}, headers=h["ann"])
    assert r.status_code == 409 and r.json()["detail"] == {"why": "board_frozen", "board_version": "v0"}
    assert st.get(old)["visibility"] == "private"


def test_missing_submission_records_and_legacy_boards_fail_closed(env) -> None:
    c, h, s, st = env["c"], env["h"], env["s"], env["st"]
    _old, sa, sb = env["ids"]
    v1 = s.data_root / "boards" / "v1"
    (v1 / LISTING).unlink()                                             # 旧制品（无 listing.json）：按 <team>/<submission_id> 识别提交条目
    assert _lb(c, h["bob"]) == {f"team-a/{sa}", f"team-b/{sb}", "baseline/x"}
    (st.root / f"{sb}.json").unlink()                                   # 提交记录缺失：视为不上榜
    assert _lb(c, h["bob"]) == {f"team-a/{sa}", "baseline/x"}
    rep = delist_withdrawn(s.data_root / "boards", st.board_view(), n_boot=200)
    assert [x["entries"] for x in rep if x["board_version"] == "v1"] == [[{"entry_id": f"team-b/{sb}", "submission_id": sb}]]
    assert set(entries(json.loads((v1 / "board.json").read_text()))) == {f"team-a/{sa}", "baseline/x"}


# ---- 只收 gz 锁步档 --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [{"physics": "fast"}, {"physics": "kinematic"}, {"physics": "sitl"}, {"lockstep": False},
                                 {"evaluation_physics": "fast"}])
def test_ranked_board_refuses_rows_that_are_not_gz_lockstep(tmp_path: Path, bad: dict) -> None:
    d = _board(tmp_path, "v1")
    before = (d / "rows.jsonl").read_bytes()
    rows = _sub_rows("ta", "sub-a1", 0.6)
    if "evaluation_physics" in bad:
        rows[1]["evaluation"]["physics"] = bad["evaluation_physics"]
    else:
        rows[1].update(bad)
    assert not gz_row(rows[1]) and gz_row(rows[0])
    with pytest.raises(BoardPhysicsError, match="gz lockstep"):
        append_rows(d, rows)
    assert (d / "rows.jsonl").read_bytes() == before                     # 整批不写
    with (d / "rows.jsonl").open("a") as f:                              # 绕过写入检查的行：发布时同样不计入
        for r in rows:
            f.write(json.dumps(r) + "\n")
    b = publish(d, {"sub-a1": {"state": "DONE", "visibility": "board", "team": "ta", "done_unix_ns": 1}}, n_boot=200)
    det = json.loads((d / "details.json").read_text())
    assert entries(b)["ta/sub-a1"] and det["ta/sub-a1"]["tracks"]["std3-l1"]["rendezvous_handoff"][0]["n_runs"] == 2
    assert all(t["physics"] == "realistic" for t in b["tracks"])


def test_ranked_board_config_must_be_gz_lockstep(tmp_path: Path) -> None:
    for i, cfg in enumerate(({"physics": "fast"}, {"physics": "kinematic"}, {"run_spec": {"physics": "fast"}})):
        d = _board(tmp_path / str(i), "v1", cfg)
        assert board_physics(cfg) != "realistic"
        with pytest.raises(BoardPhysicsError, match="gz lockstep"):
            publish(d, n_boot=100)
        with pytest.raises(BoardPhysicsError):
            compose_board(d, {"ec": 1, "eu": 1, "ao": 1, "ce": 1}, n_boot=100)
        assert not (d / "board.json").exists()
    assert board_physics({}) == board_physics({"run_spec": {}}) == "realistic"                  # 缺省物理档为 gz 锁步
    dev = _board(tmp_path / "dev", "dev-0", {"physics": "kinematic", "track": {"dev": True}})   # 开发榜（运动学自检）不受限
    append_rows(dev, _sub_rows("ta", "sub-a1", 0.6, physics="kinematic"))
    assert "ta/sub-a1" in entries(publish(dev, {"sub-a1": {"state": "DONE", "visibility": "board", "team": "ta", "done_unix_ns": 1}}, n_boot=100))


def test_slim_evaluation_carries_the_declared_physics_only_when_given() -> None:
    doc = {"components": {"ec": 0.1, "eu": 0.2, "ao": 0.3, "ce": None}, "ranked": True, "ranked_valid": True, "invalid_reasons": []}
    assert "physics" not in slim_evaluation(doc)                        # 既有评测行的字节不变
    assert slim_evaluation(doc, physics="realistic")["physics"] == "realistic"
    assert not gz_row({"evaluation": slim_evaluation(doc, physics="fast")})
