"""真 ANet 编排（V1.0；M14 §6.15）：每机一个 daemon 与自建 ANetHub 的启停、`runs/.anet/<world>/<vehicle>/`（0700）身份目录、
退出时清理 `/tmp/anet-<uid>`。D1 不启动真 daemon（§2.1 不在 D1），本模块只保留接口。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["AnetOrchestrator"]


class AnetOrchestrator:
    def __init__(self, configs: Mapping[str, Mapping[str, Any]]) -> None:
        self.configs = dict(configs)

    async def start(self) -> None:
        raise NotImplementedError("真 ANet 编排在 V1.0 实现（M14-FR-069）")

    async def stop(self) -> None:
        return None
