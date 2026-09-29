"""能力 HTTP 端点（V1.0；M14 §6.15）：daemon 的 `service` 模块经 `POST /anet/cap/<vehicle>/<capability>` 调用 agent-runtime
的能力处理器（Starlette，只监听 127.0.0.1:8790）；api 不承载（`awr.api` 禁止 import `awr.agent`）。D1 只保留路由约定。"""

from __future__ import annotations

__all__ = ["BIND", "ROUTE"]

BIND = ("127.0.0.1", 8790)
ROUTE = "/anet/cap/{vehicle_id}/{capability}"


def build_app(*_a: object, **_k: object) -> object:
    raise NotImplementedError("能力 HTTP 端点在 V1.0 实现（M14-FR-069）")
