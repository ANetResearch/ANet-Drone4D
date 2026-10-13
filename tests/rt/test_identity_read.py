"""D2.1 身份令牌的只读提升（ADR-123；AWR-04 §15.9.3；d2-WP-13b-to-M11 第 7 条，D2-INT）：带有效身份令牌的 GET、HEAD 在 admin 依赖
（`/api/sys/audit`）与 `/api/rt/inspect` 的角色检查中按 admin 处理；写入与无身份令牌时仍按 token 角色（403 `115`）。
公开站真实栈的同一判据见 `tests/e2e/test_identity_public_e2e.py::test_hidden_rest_family_reads_with_the_identity`。"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from awr.api.deps import identity_read, require_role
from awr.api.problem import ApiProblem
from awr.api.security import ApiPrincipal, principal_id_from_hint


def _req(method: str, *, identity: object | None, role: str = "viewer") -> Request:
    p = ApiPrincipal(id="p-x", role=role, jti="j", exp=0, mode="public")
    scope = {"type": "http", "method": method, "path": "/api/sys/audit", "headers": [], "query_string": b"",
             "state": {"principal": p, "auth_error": None, "identity": identity}}
    return Request(scope)


def test_identity_get_reads_as_admin() -> None:
    r = _req("GET", identity={"sub": "id-1", "tier": "internal"})
    assert identity_read(r)
    p = require_role("admin")(r)
    assert p.role == "admin" and p.id == "p-x"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_identity_writes_keep_the_token_role(method: str) -> None:
    r = _req(method, identity={"sub": "id-1"})
    assert not identity_read(r)
    with pytest.raises(ApiProblem) as e:
        require_role("admin")(r)
    assert e.value.code == 115


def test_without_identity_viewer_is_refused() -> None:
    with pytest.raises(ApiProblem) as e:
        require_role("admin")(_req("GET", identity=None))
    assert e.value.code == 115
    assert require_role("viewer")(_req("GET", identity=None)).role == "viewer"


def test_hint_forms_share_one_principal() -> None:
    """浏览器的沙盒、观看排队与页面 token 共用一个提示（d2-WP-13b-to-M15 第 10 条）：base32 与 32 位十六进制都原样映射。"""
    assert principal_id_from_hint("QCRYQQRE5XB2LIGAB2WGRC7QCQ") == ("p-qcryqqre5xb2ligab2wgrc7qcq", "QCRYQQRE5XB2LIGAB2WGRC7QCQ")
    hexh = "0123456789abcdef0123456789abcdef"
    assert principal_id_from_hint(hexh) == ("p-" + hexh, hexh)
    pid, hint = principal_id_from_hint("bad hint!")
    assert pid == "p-" + hint.lower() and hint != "bad hint!"
