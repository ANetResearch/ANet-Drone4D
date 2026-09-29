"""Reconstruction engine (M01): Engine Adapter v2, Recon IR `recon-ir@1`, ReconstructionJob and the Mock chain.

Owner: M01 (AWR-03 §4.3). This package facade only re-exports types and constants; engines, the pipeline and the IR
tools are imported lazily by their users so `import awr.reconstruction` stays below 200 ms and never pulls torch or
engine venvs (M01-NFR-011). The api process must not import any `awr.reconstruction` submodule (AWR-03 §4.2 rule 1).
"""

from .types import (
    ENGINE_SCALES,
    IR_VERSION,
    JOB_STATES,
    RECON_ENGINES,
    RECON_STAGES,
    SCALE_STATUS_RECON,
    TERMINAL_STATES,
    ConventionSpec,
    EngineCaps,
    EngineFrame,
    FrameInput,
    RawFrame,
    ReconError,
)

__version__ = "0.1.0"

__all__ = [
    "ENGINE_SCALES",
    "IR_VERSION",
    "JOB_STATES",
    "RECON_ENGINES",
    "RECON_STAGES",
    "SCALE_STATUS_RECON",
    "TERMINAL_STATES",
    "ConventionSpec",
    "EngineCaps",
    "EngineFrame",
    "FrameInput",
    "RawFrame",
    "ReconError",
    "__version__",
]
