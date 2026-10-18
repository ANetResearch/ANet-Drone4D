"""越权与访问审计（D3-AC-51"越权返回 403 并审计"；AWR-17 §18.3.7；M27-FR-156、M27-AC-029；API-AC-075"审计中令牌与种子明文 0 次"）。

审计日志 `<data_root>/audit/access-<UTC 日期>.jsonl`：403 越权（115 角色与数据范围、303 Origin 或 CSRF）、写操作及结果码、
评测员及以上接口的读取、登录与登出、令牌签发与撤销；目录 0700、文件 0600；任何记录不含令牌、Cookie 值与种子明文。
"""

from __future__ import annotations

import json
import stat


def _last(lab, action: str) -> dict:
    return [a for a in lab.audit() if a["action"] == action][-1]


def test_role_denial_is_audited(lab) -> None:
    r = lab.client.get(f"{lab.P}/runs/{lab.RUN_D3}/truth/event/tape", headers=lab.bearer("researcher"))
    assert r.status_code == 403 and r.json()["code"] == 115
    a = _last(lab, "deny")
    assert a["principal"] == "alice" and a["role"] == "researcher" and a["required"] == "evaluator" and a["why"] == "role"
    assert a["route"] == f"{lab.P}/runs/{{rid}}/truth/event/tape" and a["path"] == f"{lab.P}/runs/{lab.RUN_D3}/truth/event/tape"
    assert a["ids"] == {"rid": lab.RUN_D3} and a["status"] == 403 and a["via"] == "token" and a["token_id"] == lab.token_ids["researcher"]
    assert a["request_id"] and int(a["t_unix_ns"]) > 0


def test_scope_denials_raised_by_handlers_are_audited(lab) -> None:
    lab.client.get(f"{lab.P}/runs/{lab.RUN_D3}", headers=lab.bearer("invited"))
    a = _last(lab, "deny")
    assert (a["code"], a["why"], a["principal"]) == (115, "invited_scope", "ivan")
    lab.client.delete(f"{lab.P}/runs/{lab.RUN_OTHER}", headers=lab.bearer("researcher"))
    a = _last(lab, "deny")
    assert (a["code"], a["why"], a["principal"], a["method"]) == (115, "project_scope", "alice", "DELETE")
    assert len([x for x in lab.audit() if x["action"] == "deny"]) == 2                        # 每次越权恰好一条
    assert not [x for x in lab.audit() if x["action"] == "write"]                             # 越权不按写操作重复记录


def test_csrf_rejection_is_audited(lab) -> None:
    c = lab.session_client("researcher")
    r = c.delete(f"{lab.P}/runs/{lab.RUN_D3}")
    assert r.status_code == 403 and r.json()["code"] == 303
    a = _last(lab, "deny")
    assert (a["code"], a["why"], a["via"], a["principal"]) == (303, "csrf", "session", "alice")


def test_writes_are_audited_with_result_code(lab) -> None:
    h = lab.bearer("researcher")
    assert lab.client.delete(f"{lab.P}/runs/{lab.RUN_D3}", headers=h).status_code == 204
    a = _last(lab, "write")
    assert (a["method"], a["status"], a["code"], a["ids"]) == ("DELETE", 204, None, {"rid": lab.RUN_D3})
    r = lab.client.post(f"{lab.P}/runs/{lab.RUN_TEAM}/retry", headers=h)                       # QUEUED 不可重试
    assert r.status_code in (403, 409)
    lab.client.post(f"{lab.P}/runs/{lab.RUN_D3}/retry", headers=h)                              # CANCELLED → 新 attempt
    a = _last(lab, "write")
    assert (a["method"], a["status"]) == ("POST", 202)
    lab.client.delete(f"{lab.P}/runs/run-nope000000", headers=lab.bearer("admin"))
    a = _last(lab, "write")
    assert (a["code"], a["status"], a["principal"]) == (305, 404, "root")
    assert not [x for x in lab.audit() if x["action"] == "write" and x["method"] == "GET"]    # 普通读取不进入审计


def test_privileged_reads_are_audited(lab) -> None:
    r = lab.client.get(f"{lab.P}/runs/{lab.RUN_D3}/truth/event/tape", headers=lab.bearer("evaluator"))
    assert r.status_code == 404
    a = _last(lab, "read_privileged")
    assert (a["principal"], a["code"], a["route"]) == ("eve", 305, f"{lab.P}/runs/{{rid}}/truth/event/tape")
    lab.client.get(f"{lab.P}/runs", headers=lab.bearer("evaluator"))
    assert [x["route"] for x in lab.audit() if x["action"] == "read_privileged"] == [f"{lab.P}/runs/{{rid}}/truth/event/tape"]


def test_session_and_token_lifecycle_is_audited(lab) -> None:
    acts = [(a["action"], a.get("principal")) for a in lab.audit()]
    assert ("token_issue", "alice") in acts and ("token_issue", "ivan") in acts
    c = lab.session_client("evaluator")
    a = _last(lab, "login")
    assert (a["principal"], a["role"], a["token_id"]) == ("eve", "evaluator", lab.token_ids["evaluator"])
    assert int(a["exp_unix"]) - int(a["t_unix_ns"]) // 10**9 in (43_200, 43_199)
    assert c.delete(f"{lab.P}/session", headers={"Origin": lab.ORIGIN, "X-D3-CSRF": "1"}).status_code == 204
    assert _last(lab, "logout")["principal"] == "eve"
    assert lab.app.state.auth.revoke_token(lab.token_ids["invited"]) is True
    a = _last(lab, "token_revoke")
    assert (a["token_id"], a["principal"], a["result"]) == (lab.token_ids["invited"], "ivan", "revoked")


def test_audit_never_contains_secrets(lab) -> None:
    clients = {role: lab.session_client(role) for role in lab.ROLES}
    cookies = [c.cookies.get("d3_session") for c in clients.values()]
    st = lab.client.post(f"{lab.P}/session/short-token", json={"aud": "frames"}, headers=lab.bearer("researcher")).json()["token"]
    for role, c in clients.items():                                                          # 越权、写操作与特权读取各若干
        c.get(f"{lab.P}/runs/{lab.RUN_D3}/truth/frames/uav-01/cam0/0")
        c.delete(f"{lab.P}/runs/{lab.RUN_OTHER}", headers={"Origin": lab.ORIGIN, "X-D3-CSRF": "1"})
        c.delete(f"{lab.P}/runs/{lab.RUN_OTHER}")
        lab.client.get(f"{lab.P}/runs/{lab.RUN_D3}/frames/uav-01/cam0", headers={"Authorization": f"Bearer {st}"})
        lab.client.get(f"{lab.P}/runs/{lab.RUN_D3}/evaluation?include=seed", headers=lab.bearer(role))
    lab.app.state.auth.audit.write("probe", token=lab.tokens["admin"], note=st, nested={"cookie": cookies[0], "x": [lab.tokens["invited"]]})
    files = sorted((lab.settings.data_root / "audit").glob("access-*.jsonl"))
    text = "".join(f.read_text(encoding="utf-8") for f in files)
    assert len(text.splitlines()) >= 20
    secrets_ = [*lab.tokens.values(), *cookies, st]
    assert [s for s in secrets_ if s in text] == []                                          # 令牌与 Cookie 明文 0 次
    assert "d3bt_" not in text and "d3st_v1." not in text and '"seed"' not in text
    probe = next(json.loads(x) for x in text.splitlines() if '"probe"' in x)
    assert probe["token"] == "[redacted]" and probe["note"] == "[redacted]" and probe["nested"] == {"cookie": "[redacted]", "x": ["[redacted]"]}
    d = lab.settings.data_root / "audit"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700 and all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)
