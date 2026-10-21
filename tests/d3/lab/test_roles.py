"""四类角色 × 全部已交付 bench 接口的权限矩阵（D3-AC-51；AWR-05 §7.11；AWR-17 §18.3.5、§18.23.3；API-AC-075；M27-AC-029）。

逐项用例：每个操作 × 每类角色，分别经 bench token（`Authorization: Bearer`）与会话 Cookie（写操作带 Origin 与 `X-D3-CSRF`）两条通道：

- 角色低于最低角色：403 `115`，`detail.required` 为最低角色，且审计日志新增一条 `deny` 记录（principal、路由模板、结果码）；
- 角色不低于最低角色：不返回 401 或 403（具体结果码取决于对象是否存在，如 305、680、682）。

另有数据范围用例：受邀成员只见本团队 dev 运行与 dev 事件；研究者的取消与重试限本人及所属项目；`actions` 按请求者收窄。
"""

from __future__ import annotations

import re

import pytest

PARAMS = {"rid": "run-a000000001", "wid": "oryahovo-crop1", "eid": "abc-crossing", "sid": "sub-aaaaaaaaaa", "track": "std3-l1",
          "entry_id": "ent-aaaaaaaaaa", "uav": "uav-01", "sensor": "cam0", "seq": "0",
          "path": "video/uav-01_cam0.mp4"}
BODIES = {("POST", "/session/short-token"): {"aud": "frames"}, ("POST", "/submissions"): {},
          ("POST", "/submissions/{sid}/visibility"): {"visibility": "board"}}
RANK = {"none": -1, "invited": 0, "researcher": 1, "evaluator": 2, "admin": 3}


def _url(lab, role: str, path: str) -> str:
    params = dict(PARAMS, rid=lab.RUN_TEAM if role == "invited" else lab.RUN_D3)        # 受邀成员取本团队运行，其余取项目 d3 的运行
    return lab.P + re.sub(r"\{([a-z_]+)\}", lambda m: params[m.group(1)], path)


def _call(lab, client, op: tuple[str, str], role: str, headers: dict):
    m, path = op
    body = {"token": lab.tokens[role]} if op == ("POST", "/session") else BODIES.get(op)
    kw = {"headers": headers}
    if m in ("POST", "PUT") or body is not None:
        kw["json"] = body if body is not None else {}
    return client.request(m, _url(lab, role, path), **kw)


def _check(lab, op, role, r, before: int) -> None:
    need = lab.EXPECTED[op]
    name = lab.PRINCIPALS[role][0]
    if RANK[role] < RANK[need]:
        assert r.status_code == 403, (op, role, r.status_code, r.text)
        body = r.json()
        assert body["code"] == 115 and body["detail"] == {"required": need, "role": role}
        new = lab.audit()[before:]
        assert [(a["action"], a["code"], a["principal"], a["role"], a["route"], a["method"]) for a in new if a["action"] == "deny"] == \
            [("deny", 115, name, role, lab.P + op[1], op[0])], new
    else:
        assert r.status_code not in (401, 403), (op, role, r.status_code, r.text)


def test_matrix_bearer(lab, op, role) -> None:
    """bench token 通道（SDK 与脚本，§18.3.1 第 4 条）。"""
    before = len(lab.audit())
    _check(lab, op, role, _call(lab, lab.client, op, role, lab.bearer(role)), before)


def test_matrix_session_cookie(lab, op, role) -> None:
    """会话 Cookie 通道（工作区网页，§18.3.1 第 2、3 条）。"""
    c = lab.session_client(role)
    h = {} if op[0] == "GET" else {"Origin": lab.ORIGIN, "X-D3-CSRF": "1"}
    before = len(lab.audit())
    _check(lab, op, role, _call(lab, c, op, role, h), before)


# 排行榜接口（BN-83、84、97）对匿名访问返回 404 `305`，不暴露接口存在（D3-AC-43；tests/d3/board/test_visibility.py）
BOARD_READS = {("GET", "/board"), ("GET", "/leaderboard"), ("GET", "/board/{track}/entries/{entry_id}")}


def test_unauthenticated_is_401_on_every_member_route(lab) -> None:
    for (m, path), need in lab.EXPECTED.items():
        if need == "none":
            continue
        r = lab.client.request(m, _url(lab, "admin", path), json={} if m == "POST" else None)
        want = (404, 305) if (m, path) in BOARD_READS else (401, 301)
        assert (r.status_code, r.json()["code"]) == want, (m, path)


# ── 数据范围 ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_invited_sees_only_team_dev_runs(lab) -> None:
    h = lab.bearer("invited")
    r = lab.client.get(f"{lab.P}/runs", headers=h).json()
    assert [i["run_id"] for i in r["items"]] == [lab.RUN_TEAM] and r["total"] == 1
    assert r["items"][0]["actions"] == {"cancel": False, "retry": False}                          # 受邀成员无写操作
    assert lab.client.get(f"{lab.P}/runs/{lab.RUN_TEAM}", headers=h).status_code == 200
    for rid in (lab.RUN_D3, lab.RUN_OTHER, lab.RUN_TEST):
        r = lab.client.get(f"{lab.P}/runs/{rid}", headers=h)
        assert r.status_code == 403 and r.json()["code"] == 115 and r.json()["detail"]["why"] == "invited_scope", rid
    ids = {i["run_id"] for i in lab.client.get(f"{lab.P}/runs", headers=lab.bearer("researcher")).json()["items"]}
    assert ids == {lab.RUN_D3, lab.RUN_TEAM, lab.RUN_OTHER, lab.RUN_TEST}                         # 研究者及以上全部 dev 与 test 运行可见


def test_invited_sees_only_dev_events(lab) -> None:
    h = lab.bearer("invited")
    assert [e["event_id"] for e in lab.client.get(f"{lab.P}/events", headers=h).json()["items"]] == ["abc-crossing"]
    assert [e["event_id"] for e in lab.client.get(f"{lab.P}/worlds/oryahovo-crop1", headers=h).json()["events"]] == ["abc-crossing"]
    r = lab.client.get(f"{lab.P}/events/tst-hidden", headers=h)
    assert r.status_code == 403 and r.json()["detail"] == {"why": "invited_scope", "event_id": "tst-hidden", "split": "test"}
    items = {e["event_id"]: e for e in lab.client.get(f"{lab.P}/events", headers=lab.bearer("researcher")).json()["items"]}
    assert set(items) == {"abc-crossing", "tst-hidden"} and items["tst-hidden"]["split"] == "test" and items["abc-crossing"]["split"] == "dev"


@pytest.mark.parametrize("op", ["cancel", "retry"])
def test_researcher_writes_limited_to_own_projects(lab, op) -> None:
    st = lab.runs()
    st.transition(lab.RUN_OTHER, "CANCELLED")
    st.transition(lab.RUN_D3, "CANCELLED")
    call = (lambda rid, h: lab.client.delete(f"{lab.P}/runs/{rid}", headers=h)) if op == "cancel" else \
        (lambda rid, h: lab.client.post(f"{lab.P}/runs/{rid}/retry", headers=h))
    if op == "cancel":                                                                              # 取消需非终态
        st.retry(lab.RUN_OTHER, actor="t")
        st.retry(lab.RUN_D3, actor="t")
    r = call(lab.RUN_OTHER, lab.bearer("researcher"))
    assert r.status_code == 403 and r.json()["detail"] == {"why": "project_scope", "run_id": lab.RUN_OTHER, "project": "other"}
    assert call(lab.RUN_D3, lab.bearer("researcher")).status_code in (202, 204)                   # 本项目
    assert call(lab.RUN_OTHER, {"Authorization": f"Bearer {lab.tokens['other']}"}).status_code in (202, 204)   # 所属项目的研究者
    deny = [a for a in lab.audit() if a["action"] == "deny"]
    assert deny[-1]["why"] == "project_scope" and deny[-1]["ids"] == {"rid": lab.RUN_OTHER} and deny[-1]["principal"] == "alice"


def test_evaluator_and_admin_write_any_project(lab) -> None:
    assert lab.client.delete(f"{lab.P}/runs/{lab.RUN_OTHER}", headers=lab.bearer("evaluator")).status_code == 204
    assert lab.client.post(f"{lab.P}/runs/{lab.RUN_OTHER}/retry", headers=lab.bearer("admin")).status_code == 202


def test_actions_follow_requester_scope(lab) -> None:
    by = {i["run_id"]: i for i in lab.client.get(f"{lab.P}/runs", headers=lab.bearer("researcher")).json()["items"]}
    assert by[lab.RUN_D3]["actions"]["cancel"] is True and by[lab.RUN_OTHER]["actions"] == {"cancel": False, "retry": False}
    assert lab.client.get(f"{lab.P}/runs/{lab.RUN_OTHER}", headers=lab.bearer("researcher")).json()["actions"]["cancel"] is False
    assert lab.client.get(f"{lab.P}/runs/{lab.RUN_OTHER}", headers=lab.bearer("evaluator")).json()["actions"]["cancel"] is True
