"""REST 路由共用依赖：角色检查与应用上下文访问（AWR-17 §3.5、§4.1；M11-FR-080）。

`rest/*.py` 通过 `Depends(require_role("viewer"))` 要求 token；缺失 token 为 401 `301`，无效为 401 `302`，角色不足 403 `115`。
`app_ctx(request)` 返回 `main.create_app` 放在 `app.state.awr` 上的 `ApiContext`（设置、token 服务、总线、Gateway、世界目录）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, Request

from .problem import ApiProblem
from .security import ApiPrincipal

if TYPE_CHECKING:
    from .main import ApiContext

__all__ = ["Admin", "Operator", "Viewer", "app_ctx", "require_role"]


def require_role(role: str) -> Callable[[Request], Any]:
    def dep(request: Request) -> ApiPrincipal:
        p = getattr(request.state, "principal", None)
        if p is None:
            raise ApiProblem(302 if getattr(request.state, "auth_error", None) else 301)
        if not p.at_least(role):
            raise ApiProblem(115, status=403)
        return p

    return dep


def app_ctx(request: Request) -> ApiContext:
    return request.app.state.awr


# 路由参数写法：`p: Viewer`（等价于 `Depends(require_role("viewer"))`，避免在默认值里调用函数，ruff B008）
Viewer = Annotated[ApiPrincipal, Depends(require_role("viewer"))]
Operator = Annotated[ApiPrincipal, Depends(require_role("operator"))]
Admin = Annotated[ApiPrincipal, Depends(require_role("admin"))]
