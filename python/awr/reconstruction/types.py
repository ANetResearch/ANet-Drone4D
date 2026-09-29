"""Engine Adapter v2 data types, enums and errors (M01 §6.2.1; ADR-035). D1-core, frozen at MS1.

Everything is float64 or an explicit dtype; on-disk and wire fields are snake_case (AWR-03 §5.6 rule 2).
Pose naming (M01 §6.2.1): the raw layer stores `T_engine_cam` (C2W, OpenCV RDF camera, xyzw quaternions with w >= 0,
normalised so that frame 0 is the identity); the registered layer is `T_world_cam = T_world_engine * T_engine_cam`
(Sim3 acting on positions, rotation left-multiplied by R).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:                     # numpy is imported lazily: `import awr.reconstruction` stays light (M01-NFR-011)
    import numpy as np

# ---------------------------------------------------------------- enums (proposed for rt/enums.json, M01 §14 item 7)
EngineName = Literal["mock", "da3", "lingbot_map", "vggt", "mapanything", "colmap"]
RECON_ENGINES: tuple[str, ...] = ("mock", "da3", "lingbot_map", "vggt", "mapanything", "colmap")   # ReconEngine
RECON_STAGES: tuple[str, ...] = ("PREPARING", "SEGMENTING", "INFERRING", "FUSING", "GEOREFERENCING", "TILING", "PACKAGING")
JOB_STATES: tuple[str, ...] = ("QUEUED", *RECON_STAGES, "SUCCEEDED", "FAILED", "CANCELLED")          # JobState.recon
TERMINAL_STATES: tuple[str, ...] = ("SUCCEEDED", "FAILED", "CANCELLED")
SCALE_STATUS_RECON: tuple[str, ...] = ("relative", "gnss", "rtk", "lidar")    # subset of ScaleStatus (M01-FR-007)
SCALE_STATUS_ORDER: dict[str, int] = {"relative": 0, "gnss": 1, "lidar": 2, "rtk": 3}   # common.schema order
ENGINE_SCALES: tuple[str, ...] = ("relative", "metric_predicted", "metric_conditioned")
CAPS_REASONS: tuple[str, ...] = ("GPU_REQUIRED", "VENV_MISSING", "WEIGHTS_MISSING", "NOT_IMPLEMENTED", "WORKER_UNAVAILABLE")

FRAME_SCALE, FRAME_KEY, FRAME_NONKEY, FRAME_SIM = 0, 1, 2, 255
IR_VERSION = "1.0.0"
SCHEMA_NAMES = {"session": "awr.recon.session.v1", "engine": "awr.recon.engine.v1", "rig": "awr.recon.rig.v1",
                "cameras": "awr.recon.cameras.v1", "alignment": "awr.recon.alignment.v1", "qa": "awr.recon.qa.v1"}

# RNG streams requested by M01 (AWR-17 §10.8; M01 §6.4.1, §14 item 18) until registered in rt/rng_streams.json.
# Mock GNSS uses M02 stream 9 (georef_mock_gnss) and the LO-RANSAC M02 stream 8 (georef_ransac).
STREAM_RECON_MOCK_SUBSAMPLE = 11
STREAM_RECON_MOCK_GAUGE = 12
STREAM_RECON_MOCK_DEPTH_NOISE = 13
STREAM_RECON_MOCK_GNSS = 14          # reserved; superseded by M02 stream 9
STREAM_RECON_MOCK_GRAVITY = 15
STREAM_RECON_SELFCHECK = 16


def rng(seed: int, stream: int, *keys: int) -> np.random.Generator:
    """`PCG64(SeedSequence([seed, stream, *keys]))` (ADR-049)."""
    import numpy as np

    return np.random.Generator(np.random.PCG64(np.random.SeedSequence([int(seed), int(stream), *map(int, keys)])))


# ---------------------------------------------------------------- errors (reason codes 330-348, AWR-17 §8.4)
RESUMABLE_CODES = frozenset({335, 344, 346})


class ReconError(Exception):
    """A recon failure with its registered reason code; `resumable` follows M01 §6.7.1 R08/R09."""

    code = 342

    def __init__(self, message: str = "", *, code: int | None = None, detail: Any = None, resumable: bool | None = None,
                 stage: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = int(code)
        self.detail = detail
        self.resumable = (self.code in RESUMABLE_CODES) if resumable is None else bool(resumable)
        self.stage = stage

    def to_json(self) -> dict:
        from awr.contracts.reasons import info

        return {"code": self.code, "name": info(self.code).name, "stage": self.stage, "resumable": self.resumable,
                "detail": self.detail if self.detail is not None else (str(self) or None)}


class ParamsInvalid(ReconError):
    code = 330


class EngineUnavailable(ReconError):
    code = 331


class InputInvalid(ReconError):
    code = 334


class PoseConventionError(ReconError):
    code = 336


class NoGnss(ReconError):
    code = 338


class GeorefRejected(ReconError):
    code = 339


class IrInvalid(ReconError):
    code = 340


class ReconEmpty(ReconError):
    code = 341


class TilingFailed(ReconError):
    code = 342


class PackageInvalid(ReconError):
    code = 343


class JobCancelled(Exception):
    """Raised at a cancellation checkpoint (M01-FR-041)."""


# ---------------------------------------------------------------- Engine Adapter v2 value types (M01 §6.2.1)
@dataclass(frozen=True)
class ConventionSpec:
    pose_dir: Literal["c2w", "w2c"]
    quat_order: Literal["xyzw", "wxyz", "matrix"]
    camera_axes: Literal["opencv_rdf"]
    gauge: Literal["frame0", "scale_frames", "ref_view", "view0", "world_prior", "hidden_sim3"]
    pixel_center: Literal["integer", "half"]
    conf_kind: Literal["expp1", "sigmoid_logit", "reproj_err", "synthetic", "none"]
    engine_scale: Literal["relative", "metric_predicted", "metric_conditioned"]
    revises_poses: bool = False

    def to_json(self) -> dict:
        return {"pose_dir": self.pose_dir, "quat_order": self.quat_order, "camera_axes": self.camera_axes, "gauge": self.gauge,
                "pixel_center": self.pixel_center, "conf_kind": self.conf_kind}


@dataclass(frozen=True)
class EngineCaps:
    available: bool
    reason: str | None
    features: frozenset[str]
    device: Literal["cpu", "cuda"]
    max_frames: int
    est_vram_gb: float | None
    est_rss_gb: float


@dataclass(frozen=True)
class Preproc:
    """Model-input preprocessing: model pixel = orig pixel * s - crop (continuous COLMAP coordinates)."""

    sx: float
    sy: float
    crop_x: float
    crop_y: float
    W_orig: int
    H_orig: int
    W_model: int
    H_model: int


@dataclass(frozen=True)
class FrameInput:
    idx: int
    t_ns: int
    image_uri: str | None = None
    prior_T_world_body: np.ndarray | None = None
    sparse_depth: np.ndarray | None = None


@dataclass
class RawFrame:
    """Engine-private output; the pose is a (3|4, 4) matrix, or a 7-vector [tx, ty, tz, q...] with q in quat_order."""

    idx: int
    t_ns: int
    pose: np.ndarray
    K_model: np.ndarray
    preproc: Preproc
    depth: np.ndarray | None
    conf_raw: np.ndarray | None
    normals: np.ndarray | None = None
    normals_frame: Literal["cam", "engine_raw"] = "cam"
    frame_type: int = FRAME_KEY
    extras: dict = field(default_factory=dict)


@dataclass
class EngineFrame:
    idx: int
    t_ns: int
    T_engine_cam: np.ndarray          # 4x4 C2W, OpenCV RDF, frame 0 = identity
    q_xyzw: np.ndarray                # w >= 0
    K_orig: np.ndarray                # original-image pixels, COLMAP pixel centres
    K_depth: np.ndarray | None        # depth-map resolution, same convention
    depth: np.ndarray | None          # float32, engine units
    conf_u8: np.ndarray | None        # uint8
    normals_engine: np.ndarray | None  # float32, engine gauge
    frame_type: int
    self_check: dict | None = None
    extras: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SelfCheck:
    method: str
    votes: tuple[str, ...]
    margins: tuple[float, ...]
    passed: bool
    declared: str = ""

    def to_json(self) -> dict:
        return {"method": self.method, "votes": list(self.votes), "margins": [round(float(m), 4) for m in self.margins],
                "pass": bool(self.passed)}


@dataclass
class NormState:
    """Per-session normalisation state: the frame-0 gauge change is fixed by the first frame."""

    T_frame0_raw: np.ndarray | None = None
    T0_inv: np.ndarray | None = None
    inverted: bool = False
    frames: int = 0


@dataclass(frozen=True)
class SessionSpec:
    """What an adapter needs to prepare a session (produced by PREPARING)."""

    session_id: str
    n_frames: int
    fps: float
    width: int
    height: int
    seed: int
    params: dict = field(default_factory=dict)
    source: Any = None                # engine-specific input (Mock: the prepared source cloud and path)


@dataclass
class EngineContext:
    workdir: Path
    check_cancel: Callable[[], None] = lambda: None
    log: Callable[..., None] = lambda *a, **k: None


@dataclass
class EngineSessionExtras:
    chunks: list[dict] = field(default_factory=list)
    revised_poses: dict[int, np.ndarray] | None = None
    metrics: dict = field(default_factory=dict)
