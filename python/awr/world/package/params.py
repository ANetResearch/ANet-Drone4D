"""构建参数（M03 §7.2 `BuildParams`；默认值同 §7.1，全部写入 `generator.params`）。"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from .derivers import worldpkg_config


@dataclass(frozen=True)
class BuildParams:
    G: int = 64
    leaf: int = 20000
    seed: int = 1
    compression: str = "none"
    forest: str = "auto"
    dtm_cell_m: float = 10.0
    dsm_cell_m: float = 2.0
    border_inset_m: float = 20.0
    border_headroom_m: float = 50.0
    dsm_occupancy: bool = True
    hag_grid: bool = False          # HAG 2 m 栅格（M03-FR-025，P2 / V0.2；`--hag` 时写出）
    semantic: str = "rules"
    keep_staging: bool = False
    keep_work: bool = False

    @classmethod
    def from_config(cls, **overrides) -> BuildParams:
        d = worldpkg_config()["defaults"]
        p = cls(dtm_cell_m=float(d.get("dtm_cell_m", 10)), dsm_cell_m=float(d.get("dsm_cell_m", 2)),
                border_inset_m=float(d.get("border_inset_m", 20)), border_headroom_m=float(d.get("border_headroom_m", 50)))
        return replace(p, **{k: v for k, v in overrides.items() if v is not None})

    def generator_params(self) -> dict:
        """16 §3.2 的 9 个键加 M03 的 `dsmOccupancy`、`numpy`（numpy 版本影响随机数流，R-1）。"""
        def num(v: float):
            return int(v) if float(v).is_integer() else float(v)

        return {"G": int(self.G), "leaf": int(self.leaf), "compression": self.compression, "seed": int(self.seed),
                "forest": self.forest, "dtmCellM": num(self.dtm_cell_m), "dsmCellM": num(self.dsm_cell_m),
                "borderInsetM": num(self.border_inset_m), "borderHeadroomM": num(self.border_headroom_m),
                "dsmOccupancy": bool(self.dsm_occupancy), "numpy": np.__version__,
                **({"hagGrid": True} if self.hag_grid else {})}
