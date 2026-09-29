"""Engine Adapter v2: protocol, base class, registry and the pose-direction self-check (M01 §6.2, §9.2; M01-FR-001,
FR-004). D1-core, frozen at MS1.

A new engine is one adapter file: subclass `AdapterBase`, declare `name`, `version`, `variant`, `convention` and
implement `_run_raw()` (engine-private `RawFrame`s). `AdapterBase.infer()` normalises every frame and runs the
self-check once (first 32 frames, or all frames of a shorter session) before the frames reach the IR.
Engine modules are imported lazily through `ENGINE_MODULES` so `import awr.reconstruction` stays light (M01-NFR-011).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Iterator
from typing import Any, ClassVar, Protocol, runtime_checkable

import numpy as np

from ..conventions import CONVENTIONS, inv_se3, pose_to_4x4
from ..types import (
    STREAM_RECON_SELFCHECK,
    ConventionSpec,
    EngineCaps,
    EngineContext,
    EngineFrame,
    EngineName,
    EngineSessionExtras,
    EngineUnavailable,
    FrameInput,
    NormState,
    PoseConventionError,
    RawFrame,
    SelfCheck,
    SessionSpec,
    rng,
)

__all__ = [
    "ENGINE_MODULES",
    "AdapterBase",
    "EngineAdapter",
    "engine_catalog",
    "get_engine",
    "pose_direction_selfcheck",
    "register_engine",
]

SELFCHECK_WINDOW = 32

# engine id -> module (lazy import; M01 §9.1)
ENGINE_MODULES: dict[str, str] = {
    "mock": "awr.reconstruction.engines.mock",
    "da3": "awr.reconstruction.engines.da3",
    "lingbot_map": "awr.reconstruction.engines.lingbot",
    "vggt": "awr.reconstruction.engines.vggt",
    "mapanything": "awr.reconstruction.engines.mapanything",
    "colmap": "awr.reconstruction.engines.colmap",
}
_REGISTRY: dict[str, type] = {}


@runtime_checkable
class EngineAdapter(Protocol):
    name: EngineName
    version: str
    convention: ConventionSpec

    def capabilities(self) -> EngineCaps: ...

    def prepare(self, session: SessionSpec, ctx: EngineContext) -> None: ...

    def infer(self, frames: Iterable[FrameInput]) -> Iterator[EngineFrame]: ...

    def finalize(self) -> EngineSessionExtras: ...

    def close(self) -> None: ...


def register_engine(name: str) -> Callable[[type], type]:
    def deco(cls: type) -> type:
        prev = _REGISTRY.get(name)
        if prev is not None and prev is not cls and prev.__qualname__ != cls.__qualname__:
            raise ValueError(f"engine {name!r} already registered by {prev.__module__}.{prev.__qualname__}")
        _REGISTRY[name] = cls
        cls.name = name
        return cls

    return deco


def _engine_class(name: str) -> type:
    if name not in _REGISTRY:
        mod = ENGINE_MODULES.get(name)
        if mod is None:
            raise EngineUnavailable(f"unknown engine {name!r}", detail={"engine": name, "reason": "NOT_IMPLEMENTED"})
        importlib.import_module(mod)
    return _REGISTRY[name]


def get_engine(name: str, **kw: Any) -> AdapterBase:
    """Instantiate a registered engine (imports its module on first use)."""
    return _engine_class(name)(**kw)


def engine_catalog() -> list[dict]:
    """R63 items (M01 §7.1): capability probe of every engine (job-worker side; the api never imports engines)."""
    out = []
    for name in ENGINE_MODULES:
        eng = get_engine(name)
        try:
            caps = eng.capabilities()
            out.append({"engine": name, "variant": eng.variant, "available": caps.available, "reason": caps.reason,
                        "features": sorted(caps.features), "engine_scale": eng.convention.engine_scale, "device": caps.device,
                        "license": dict(eng.license), "target_version": eng.target_version})
        finally:
            eng.close()
    return out


# ---------------------------------------------------------------- self-check (M01 §6.2.4)
def _unproject(raw: RawFrame, T_c2w: np.ndarray, pixel_center: str, n_pts: int, r: np.random.Generator) -> np.ndarray:
    D = np.asarray(raw.depth, dtype=np.float64)
    K = np.asarray(raw.K_model, dtype=np.float64)
    m = np.isfinite(D) & (D > 0)
    if raw.conf_raw is not None:
        c = np.asarray(raw.conf_raw, dtype=np.float64)
        if m.any():
            thr = np.percentile(c[m], 30)
            m &= c >= thr
    v, u = np.nonzero(m)
    if len(u) == 0:
        return np.zeros((0, 3))
    if len(u) > n_pts:
        sel = np.sort(r.choice(len(u), n_pts, replace=False))
        v, u = v[sel], u[sel]
    off = 0.0 if pixel_center == "integer" else 0.5
    # the model K is in the engine's own pixel convention: "half" engines index pixel centres at i + 0.5
    z = D[v, u]
    X = np.c_[(u + off - K[0, 2]) * z / K[0, 0], (v + off - K[1, 2]) * z / K[1, 1], z]
    return X @ T_c2w[:3, :3].T + T_c2w[:3, 3]


def _median_nn(A: np.ndarray, B: np.ndarray) -> float:
    from scipy.spatial import cKDTree

    d, _ = cKDTree(B).query(A, k=1)
    return float(np.median(d))


def _pairs(n: int, pairs: int, gap: int, r: np.random.Generator) -> list[int]:
    cand = np.arange(0, max(0, n - gap))
    if len(cand) == 0:
        return []
    k = min(pairs, len(cand))
    return sorted(int(i) for i in r.choice(cand, k, replace=False))


def pose_direction_selfcheck(buf: list[RawFrame], conv: ConventionSpec, r: np.random.Generator, *, pairs: int = 4,
                             gap: int | None = None, n_pts: int = 2000) -> SelfCheck:
    """Vote C2W vs W2C by the symmetric Chamfer median of back-projected frame pairs (r02 §3.3 `detect_pose_convention`).

    A pair abstains when the two hypotheses differ by less than 1.5x (too little overlap). The check passes when at
    least 2 pairs vote and all votes equal the declared `conv.pose_dir`.
    """
    n = len(buf)
    g = gap if gap is not None else (5 if n >= SELFCHECK_WINDOW else max(1, n // 4))
    votes: list[str] = []
    margins: list[float] = []
    for i in _pairs(n, pairs, g, r):
        a, b = buf[i], buf[i + g]
        if a.depth is None or b.depth is None:
            continue
        Ta = pose_to_4x4(a.pose, conv.quat_order)
        Tb = pose_to_4x4(b.pose, conv.quat_order)
        err = {}
        for hyp in ("c2w", "w2c"):
            Ta_h = Ta if hyp == "c2w" else inv_se3(Ta)
            Tb_h = Tb if hyp == "c2w" else inv_se3(Tb)
            Pa = _unproject(a, Ta_h, conv.pixel_center, n_pts, r)
            Pb = _unproject(b, Tb_h, conv.pixel_center, n_pts, r)
            if len(Pa) < 10 or len(Pb) < 10:
                err = {}
                break
            err[hyp] = _median_nn(Pa, Pb) + _median_nn(Pb, Pa)
        if not err:
            continue
        best, worst = sorted(err, key=err.get)
        margin = err[worst] / max(err[best], 1e-12)
        if margin < 1.5:
            continue
        votes.append(best)
        margins.append(margin)
    ok = len(votes) >= 2 and all(v == conv.pose_dir for v in votes)
    return SelfCheck("chamfer_pair_vote", tuple(votes), tuple(margins), ok, conv.pose_dir)


# ---------------------------------------------------------------- base class
class AdapterBase:
    """Shared normalisation and self-check; concrete engines implement `_run_raw()` only."""

    name: str = ""
    version: str = "0.0.0"
    variant: str = ""
    commit: str | None = None
    target_version: str = "V0.1"
    license: ClassVar[dict] = {"code": "project", "weights": "n/a"}
    weights: dict | None = None
    convention: ConventionSpec

    def __init__(self, **kw: Any) -> None:
        self.convention = kw.pop("convention", None) or CONVENTIONS[self.name]
        self.options = kw
        self._norm = NormState()
        self._checked = False
        self._n_expected = 0
        self._seed = 0
        self.self_check: SelfCheck | None = None
        self.session: SessionSpec | None = None
        self.ctx: EngineContext | None = None

    # -- protocol
    def capabilities(self) -> EngineCaps:  # pragma: no cover - every engine overrides
        raise NotImplementedError

    def prepare(self, session: SessionSpec, ctx: EngineContext) -> None:
        caps = self.capabilities()
        if not caps.available:
            raise EngineUnavailable(f"engine {self.name} unavailable: {caps.reason}", detail={"engine": self.name,
                                                                                          "reason": caps.reason})
        self.session = session
        self.ctx = ctx
        self._n_expected = int(session.n_frames)
        self._seed = int(session.seed)
        self._norm = NormState()
        self._checked = False
        self.self_check = None

    def infer(self, frames: Iterable[FrameInput]) -> Iterator[EngineFrame]:
        buf: list[RawFrame] = []
        window = min(SELFCHECK_WINDOW, self._n_expected) if self._n_expected else SELFCHECK_WINDOW
        for raw in self._run_raw(frames):
            if not self._checked and len(buf) < window:
                buf.append(raw)
            ef = self._normalize(raw)
            if not self._checked and len(buf) >= window:
                self._run_selfcheck(buf)
                buf.clear()
                ef.self_check = self.self_check.to_json()
            yield ef
        if not self._checked and buf:            # sessions shorter than expected: check what we have
            self._run_selfcheck(buf)

    def finalize(self) -> EngineSessionExtras:
        return EngineSessionExtras()

    def close(self) -> None:
        return None

    # -- helpers
    def _normalize(self, raw: RawFrame) -> EngineFrame:
        from ..conventions import normalize

        return normalize(raw, self.convention, self._norm)

    def _run_selfcheck(self, buf: list[RawFrame]) -> None:
        sc = pose_direction_selfcheck(buf, self.convention, rng(self._seed, STREAM_RECON_SELFCHECK))
        self.self_check = sc
        self._checked = True
        if not sc.passed:
            raise PoseConventionError(f"pose direction self-check failed: declared {self.convention.pose_dir}, votes "
                                      f"{list(sc.votes)}", detail=sc.to_json())

    def _run_raw(self, frames: Iterable[FrameInput]) -> Iterator[RawFrame]:  # pragma: no cover - engines override
        raise NotImplementedError
        yield

    def normalization_record(self) -> dict:
        st = self._norm
        return {"inverted": bool(self.convention.pose_dir == "w2c"), "renormalized_to_frame0": True,
                "T_frame0_raw": None if st.T_frame0_raw is None else st.T_frame0_raw.tolist(),
                "self_check": None if self.self_check is None else self.self_check.to_json()}


class StubAdapter(AdapterBase):
    """D1 stub (M01-FR-015): full convention declaration, unavailable with a reason, `infer()` raises 331."""

    reason = "NOT_IMPLEMENTED"
    features: frozenset[str] = frozenset()
    device = "cuda"
    max_frames = 3000
    est_vram_gb: float | None = None
    est_rss_gb = 2.0

    def capabilities(self) -> EngineCaps:
        return EngineCaps(False, self.reason, self.features, self.device, self.max_frames, self.est_vram_gb, self.est_rss_gb)

    def _run_raw(self, frames: Iterable[FrameInput]) -> Iterator[RawFrame]:
        raise EngineUnavailable(f"engine {self.name} is a D1 stub ({self.reason}, {self.target_version})",
                                detail={"engine": self.name, "reason": self.reason})
        yield
