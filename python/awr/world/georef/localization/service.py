"""`LocalizationService` and `LioBackend` protocols with their value types (M02 §6.3.5; M02-FR-015 to FR-017, P2).

Signatures are frozen in D1 (no implementation): Truth / Mock (V0.2) / ServerRelocalizer / OnboardBridge (V0.5b) plug in
behind the same protocol (P-07). LocStatus and PoseSrc come from `rt/enums.json` (generated `awr.contracts.enums`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np

__all__ = ["SIGMA_DEFAULTS", "InitialGuess", "LioBackend", "LioResult", "LocState", "LocalizationService", "ReleaseRef"]

# initial-guess sigmas per source (M02 §6.3.5 table): (sigma_pos_m, sigma_yaw_rad)
SIGMA_DEFAULTS: dict[str, tuple[float, float]] = {"pad": (0.1, 0.035), "rtk": (0.05, 0.175), "manual": (2.0, 0.087)}
MAX_INITIAL_POS_SIGMA_M = 2.0


@dataclass(frozen=True)
class LioResult:
    odom_tum: Path
    odom_cov: Path
    frames_qa: Path
    gravity_aligned: bool                       # mandatory (r05 §4.3)
    engine: Mapping[str, str]                   # name, version, commit, config_sha256


@dataclass(frozen=True)
class ReleaseRef:
    world_id: str
    map_id: str
    version: str
    manifest_sha256: str


@dataclass(frozen=True)
class InitialGuess:
    source: Literal["pad", "rtk", "manual", "last_good"]
    T_world_body: np.ndarray                    # 4x4
    sigma_pos_m: float
    sigma_yaw_rad: float

    def __post_init__(self) -> None:
        if not self.sigma_pos_m <= MAX_INITIAL_POS_SIGMA_M:
            raise ValueError(f"initial position sigma {self.sigma_pos_m} m > {MAX_INITIAL_POS_SIGMA_M} m: provide a better guess")

    @classmethod
    def with_defaults(cls, source: str, T_world_body: np.ndarray) -> InitialGuess:
        sp, sy = SIGMA_DEFAULTS[source]
        return cls(source, T_world_body, sp, sy)  # type: ignore[arg-type]


@dataclass
class LocState:
    status: str = "NO_MAP"                      # LocStatus (seven states)
    pose_src: str = "FUSED"                     # PoseSrc
    map_id: str | None = None
    map_version: str | None = None
    fitness_m2: float = math.nan
    fit_ratio: float = math.nan
    inlier: float = math.nan
    degeneracy: float = math.nan                # dimensionless (not an angle)
    corr: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)   # dx_m, dy_m, dz_m, dyaw_rad
    reset_counter: int = 0
    last_ok_ns: int = -1
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        nan = lambda v: None if isinstance(v, float) and math.isnan(v) else v  # noqa: E731
        return {"status": self.status, "pose_src": self.pose_src, "map_id": self.map_id, "map_version": self.map_version,
                "fitness_m2": nan(self.fitness_m2), "fit_ratio": nan(self.fit_ratio), "inlier": nan(self.inlier),
                "degeneracy": nan(self.degeneracy),
                "corr": {"dx_m": self.corr[0], "dy_m": self.corr[1], "dz_m": self.corr[2], "dyaw_rad": self.corr[3]},
                "reset_counter": self.reset_counter & 0xFF, "last_ok_ns": self.last_ok_ns}


@runtime_checkable
class LocalizationService(Protocol):
    kind: Literal["uav"]

    def load_release(self, ref: ReleaseRef) -> None: ...

    def set_initial_guess(self, guess: InitialGuess) -> None: ...

    def on_odom(self, t_sim_ns: int, T_odom_body: np.ndarray) -> None: ...

    def on_scan(self, frame: Any, T_odom_body: np.ndarray) -> None: ...

    def tick(self, t_sim_ns: int) -> LocState: ...

    @property
    def T_world_odom(self) -> np.ndarray: ...


@runtime_checkable
class LioBackend(Protocol):
    name: str
    version: str

    def run(self, session: Path, cfg: Mapping[str, Any]) -> LioResult: ...
