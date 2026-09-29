"""sim-core 运行配置（M08 §9.1 `runtime/config.py`；M08-FR-005；AWR-19 §6.3）。

来源优先级：显式参数 > `AWR_*` 环境变量（supervisor 注入）> 内置默认。FleetConfig 字段写入 `meta.json` 供重仿真比对（ext）。
walking skeleton 没有剧本加载器（M10）与剧本文件（M16）：启动时按 `AWR_SIM_N`（缺省 1）在出生点布设 p600_mid360，
出生点缺省取世界原点附近的平坦开阔格（DSM 与 DTM 高差 < 0.5 m），可用 `AWR_SIM_SPAWN="x,y"` 指定；时钟缺省自动播放
（AWR-03 §8.5"时钟自动开始播放"），`AWR_SIM_AUTOPLAY=0` 关闭。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..fleet.fleet import FleetConfig

__all__ = ["SimConfig"]

ROOT = Path(__file__).resolve().parents[4]


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class SimConfig:
    world_id: str = "shenzhen"
    worlds_dir: Path = ROOT / "worlds"
    run_id: str = "local"
    fleet: FleetConfig = field(default_factory=FleetConfig)
    n_vehicles: int = 1
    profile_id: str = "p600_mid360"
    limits_profile: str | None = None
    spawn_xy: tuple[float, float] | None = None
    spawn_spacing_m: float = 6.0
    autoplay: bool = True
    plugins: tuple[str, ...] = ()
    world_seed: int = 0
    load_world: bool = True
    scenario: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **over) -> SimConfig:
        e = os.environ if env is None else env
        spawn = None
        if e.get("AWR_SIM_SPAWN"):
            try:
                x, y = (float(v) for v in e["AWR_SIM_SPAWN"].split(",")[:2])
                spawn = (x, y)
            except ValueError:
                spawn = None
        cfg = cls(world_id=e.get("AWR_WORLD", "shenzhen"),
                  worlds_dir=Path(e.get("AWR_WORLDS_DIR", str(ROOT / "worlds"))),
                  run_id=e.get("AWR_RUN", "local"),
                  fleet=FleetConfig.from_env(e),  # AWR_KERNEL=numba|numpy 覆盖内核（M08-FR-040）
                  n_vehicles=max(0, int(e.get("AWR_SIM_N", "1") or 1)),
                  profile_id=e.get("AWR_SIM_PROFILE", "p600_mid360"),
                  spawn_xy=spawn,
                  autoplay=_bool(e.get("AWR_SIM_AUTOPLAY"), True),
                  plugins=tuple(p for p in (e.get("AWR_PLUGINS") or "").split(",") if p),
                  world_seed=int(e.get("AWR_WORLD_SEED", "0") or 0),
                  load_world=_bool(e.get("AWR_SIM_LOAD_WORLD"), True),
                  scenario=e.get("AWR_SCENARIO") or None)
        return replace(cfg, **over) if over else cfg
