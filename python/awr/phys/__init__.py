"""物理后端档位（M24；AWR-05 §5.3）：档位适配层、PoseHistory 真值通道与 gz-sim 锁步（Realistic 档）。

- `awr.phys.pose_history`：PoseSample、PoseHistoryBuffer（每机环形 SoA，单写者）与 `drone4d.pose_history.v1` 消息编码。
- `awr.phys.adapter`：TierCommand、ContactEvent、TickResult、VehicleStatus 统一派生、时钟档校验与 PhysicsTier 协议。
- `awr.phys.gz`：Realistic 档宿主侧（控制协议 `drone4d.gz.v1`、实例管理、GzSimBackend、路径跟踪器）。

D2 代码不得导入本包（AWR-05 §4.8）；原因码 630–639。
"""

from __future__ import annotations

from typing import Any

__all__ = ["PhysError"]


class PhysError(Exception):
    """M24 错误：code ∈ 630–639（`awr.contracts.reasons`）。"""

    def __init__(self, code: int, message: str = "", detail: Any = None) -> None:
        super().__init__(f"{code}: {message}" if message else str(code))
        self.code = int(code)
        self.detail = detail
