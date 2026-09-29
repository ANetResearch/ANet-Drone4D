"""SessionSpec 草案（`packages/contracts/sim/session_spec.schema.json`，V0.2 生效；M08 §6.12；M08-FR-074）。

一个会话 = 一个 World + N 个外部飞控机体。D1 冻结信封（schema、world_id、backends[]），其余字段按 M08 §6.12 的草案给出缺省：
`image`（`px4io/px4-sitl:v1.18.0-rc1`）、`resources{cpu_per_vehicle, rtf_min, max_parallel_spawn}`、`policies{restart, teardown}`。
生命周期枚举复用 `rt/enums.json` 的 Lifecycle；Mock 生命周期即 MockDriver 语义（M08-FR-067）。容量准入：`空闲核 × 0.8 ≥ N × 0.25`。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["SessionSpec", "capacity_ok", "default_spec"]

IMAGE = "px4io/px4-sitl:v1.18.0-rc1"


@dataclass
class SessionSpec:
    world_id: str
    run_id: str | None = None
    backends: list[dict[str, Any]] = field(default_factory=list)
    image: str = IMAGE
    resources: dict[str, Any] = field(default_factory=lambda: {"cpu_per_vehicle": 0.3, "rtf_min": 0.9,
                                                               "max_parallel_spawn": 4})
    policies: dict[str, Any] = field(default_factory=lambda: {
        "restart": {"max": 3, "window_s": 60, "backoff_s": [1, 2, 4, 8]},
        "teardown": {"land_first": True, "land_timeout_s": 30}})

    def to_json(self) -> dict[str, Any]:
        d = {"schema": "awr.sim.session_spec.v1", **asdict(self)}
        if d["run_id"] is None:
            d.pop("run_id")
        return d


def default_spec(world_id: str, n_sih: int = 0) -> SessionSpec:
    b = [{"backend": "mock", "vehicles": []}]
    if n_sih:
        b.append({"backend": "px4_sih", "vehicles": [f"x500-{k + 1:02d}" for k in range(n_sih)]})
    return SessionSpec(world_id, backends=b)


def capacity_ok(free_cores: float, n: int, *, per_vehicle: float = 0.25, headroom: float = 0.8) -> bool:
    """容量准入（r20 §3.9；r22 §4.2）：`空闲核 × 0.8 ≥ N × 0.25`；本机上限 8 架。"""
    return n <= 8 and free_cores * headroom >= n * per_vehicle
