"""`recon-job-params`: defaults, validation, session id and target id (M01 §6.9, §6.3.2; M01-FR-014, FR-044). D1-core.

`resolve_params()` validates the request against `recon-job-params.schema.json` (unknown fields -> 330) and fills every
default of M01 §6.9, so the job and the IR always see the complete parameter set; `session_id_of()` hashes the
canonical {engine, source, params} with the seed, the source `contentVersion` and the engine id and version
(`rs-<sha256[:12]>`, target world id excluded, M01-NFR-008).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable

from ..types import ParamsInvalid

__all__ = ["BUILTIN_WORLDS", "DEFAULTS", "TARGET_RE", "canonical", "default_target_id", "params_hash", "resolve_params",
           "session_id_of"]

TARGET_RE = re.compile(r"^[a-z0-9-]{1,63}$")
BUILTIN_WORLDS = frozenset({"shenzhen", "shanghai", "newyork", "sanfrancisco", "suzhou", "chicago"})

DEFAULTS: dict = {
    "engine": "mock",
    "source": {"kind": "world_sample", "path": "helix", "frames": 600},
    "seed": 1,
    "params": {
        "camera": {"fps": 10, "hfov_deg": 60.0, "depth_res": [128, 72], "pitch_deg": -45.0},
        "path": {"alt_agl_m": 150.0, "helix_radius_m": None, "helix_turns": 2, "helix_climb_m": 20.0,
                 "lawnmower_spacing_m": 160.0, "lawnmower_legs": 5},
        "mock": {"source_keep": 0.25, "depth_noise_rel": 0.005, "raw_pose_dir": "c2w", "keyframe_interval": 1, "drift": None,
                 "gravity_noise_deg": 0.2,
                 "gnss": {"sigma_h_m": 2.0, "sigma_v_m": 3.0, "outlier_frac": 0.01, "dropout_frac": 0.02, "time_offset_s": 0.0}},
        "fuse": {"conf_min_u8": 85, "conf_pct": 0.0, "depth_pct": 98.0, "stride": 1, "jitter": False, "voxel_k": 0.5,
                 "keyframes_only": True, "min_points": 10_000},
        "georef": {"mode": "gnss", "gravity": "auto", "gravity_weight": 1.0, "time_offset": "off", "on_reject": "publish_relative"},
        "retain": {"raw_points": False, "depth": "none"},
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def resolve_params(req: dict, *, max_bytes: int = 64 * 1024) -> dict:
    """Validate a request body and return the complete parameter set (raises ParamsInvalid, code 330)."""
    from ..ir.schema import schema_errors

    if not isinstance(req, dict):
        raise ParamsInvalid("parameters must be a JSON object", detail=[{"path": "/", "message": "not an object"}])
    if len(json.dumps(req, ensure_ascii=False)) > max_bytes:
        raise ParamsInvalid("parameter body exceeds 64 KB", detail=[{"path": "/", "message": "too large"}])
    errs = schema_errors(req, "recon-job-params.schema.json")
    if errs:
        raise ParamsInvalid("recon-job-params schema violation", detail=errs)
    p = _merge(DEFAULTS, req)
    src = p["source"]
    if src["kind"] == "world_sample" and not src.get("world_id"):
        raise ParamsInvalid("source.world_id is required for world_sample", detail=[{"path": "/source/world_id",
                                                                                       "message": "required"}])
    if src["kind"] != "world_sample" and p["engine"] == "mock":
        raise ParamsInvalid("the mock engine only samples an existing world", detail=[{"path": "/source/kind",
                                                                                         "message": "mock needs world_sample"}])
    if p["engine"] == "mock" and p["params"]["georef"]["mode"] not in ("gnss", "none"):
        raise ParamsInvalid("Mock georef.mode is gnss or none (synthetic anchors forbid rtk, V-C-11)",
                            detail=[{"path": "/params/georef/mode", "message": "gnss or none"}])
    tid = p.get("target_world_id")
    if tid is not None and not TARGET_RE.match(tid):
        raise ParamsInvalid("target_world_id must match ^[a-z0-9-]{1,63}$", detail=[{"path": "/target_world_id",
                                                                                      "message": "pattern"}])
    return p


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def session_id_of(params: dict, seed: int, source_content_version: str | None, engine: str, engine_version: str) -> str:
    """`rs-` + sha256(canonical {engine, source, params} || seed || source contentVersion || engine@version)[:12]."""
    body = canonical({"engine": params["engine"], "source": params["source"], "params": params["params"]})
    h = hashlib.sha256()
    for part in (body, b"\x1f", str(int(seed)).encode(), b"\x1f", (source_content_version or "").encode(), b"\x1f",
                 f"{engine}@{engine_version}".encode()):
        h.update(part)
    return "rs-" + h.hexdigest()[:12]


def params_hash(params: dict) -> str:
    """Hash for stage completion markers (the target world id is included: TILING and PACKAGING depend on it)."""
    return hashlib.sha256(canonical(params)).hexdigest()


def default_target_id(source_id: str, existing: Iterable[str]) -> str:
    taken = set(existing)
    for nn in range(1, 100):
        cand = f"{source_id}-recon-{nn:02d}"
        if cand not in taken and cand not in BUILTIN_WORLDS:
            return cand
    raise ParamsInvalid("no free <source>-recon-<nn> id", detail=[{"path": "/target_world_id", "message": "exhausted"}])
