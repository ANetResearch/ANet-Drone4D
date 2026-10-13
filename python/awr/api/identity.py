"""内部身份在 api 进程中的入口（M11-FR-226、FR-227、FR-228；AWR-04 §15.9；17 §17.20.5；ADR-123）。

比对、限流、三级锁定、固定时延、令牌签发与撤销表全部是 M17 的身份库 `awr.sandbox.identity.IdentityService`（纯库，
`tools/lint/py-imports.toml` 白名单）；本模块只把它接到 show-api：

- 密钥只来自 `AWR_ADMIN_SECRET_FILE`（任何访问模式；未配置时 R79 为 403 `115`，与池的验签口径一致）；
- `verify(token)`：REST 头 `X-AWR-Identity` 与 WS 子协议 `ident.<令牌>` 的验签与撤销检查（撤销表按修改时间 ≤ 1 Hz 重载）；
- 审计写共享展示 run 的 `audit.jsonl`（只有 jti、principal 与前缀散列，从不记录输入、口令或令牌）。

口令、令牌与请求体不进入任何日志：路由只把口令交给 `acheck()`，随即丢弃引用；中间件不记录请求体与这两个头。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from awr.sandbox.identity import IdentityInvalid, IdentityService

if TYPE_CHECKING:
    from .audit import AuditWriter

__all__ = ["ApiIdentity", "IdentityInvalid", "subprotocol_token"]

log = logging.getLogger("awr.api.identity")

IDENT_PREFIX = "ident."


def subprotocol_token(protos: list[str]) -> str | None:
    """WS 子协议 `ident.<身份令牌>`（与 `bearer.<token>` 并列；17 §17.20.5）。"""
    return next((p[len(IDENT_PREFIX):] for p in protos if p.startswith(IDENT_PREFIX)), None)


class ApiIdentity:
    def __init__(self, svc: IdentityService) -> None:
        self.svc = svc

    @classmethod
    def from_env(cls, env: dict[str, str] | None, audit: AuditWriter) -> ApiIdentity:
        def write(kind: str, detail: dict) -> None:
            pid = detail.get("principal") if isinstance(detail.get("principal"), str) else None
            audit.write(kind, principal_id=pid, role="internal" if kind in ("auth.identity_unlock", "auth.identity_logout") else None,
                        detail=detail)

        return cls(IdentityService.from_env(env, audit=write))

    @property
    def enabled(self) -> bool:
        return self.svc.enabled

    def verify(self, token: str | None) -> dict | None:
        """有效身份令牌的载荷；无效、过期、撤销或未配置时 None。"""
        if not token:
            return None
        try:
            return self.svc.verify_token(token)
        except IdentityInvalid:
            return None

    def why(self, token: str | None) -> str:
        try:
            self.svc.verify_token(token)
        except IdentityInvalid as e:
            return e.why
        return "ok"

    async def unlock(self, secret: str, addr: str | None, principal_id: str) -> Any:
        return await self.svc.acheck(secret, addr, principal_id=principal_id)

    async def check_admin_secret(self, secret: str, addr: str | None) -> Any:
        """R01 的 `admin_secret` 路径（公开模式）：同一比对、限流与锁定，成功不签身份令牌。"""
        return await self.svc.acheck(secret, addr, issue=False)

    def revoke(self, token: str) -> dict | None:
        return self.svc.revoke(token)

    def flush(self) -> None:
        try:
            self.svc.flush(force=True)
        except Exception:
            log.exception("identity state flush failed")
