"""Recon IR semantic validator `validate_session` (M01 §6.3.4 rules V01-V18; M01-FR-010). D1-core.

Regular rules run on every call; V13, V14 and V18 only with `deep=True` (they re-derive the registered poses in float64
and hash every file). The job gate before TILING uses `deep=False` (M01-FR-029, 340 RECON_IR_INVALID); the offline CLI
is `python -m awr.reconstruction validate <session_dir> [--deep]`.
"""

from __future__ import annotations

import itertools
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from awr.world.georef.frames import quat_to_mat

from ..types import IR_VERSION, SCALE_STATUS_RECON, SCHEMA_NAMES
from .jsonio import file_sha256
from .reader import poses_from_rows
from .schema import DOC_SCHEMAS, schema_errors
from .trajectory_bin import AwtrError, read_trajectory_bin

__all__ = ["CAMERA_PARAM_COUNT", "FORBIDDEN_KEYS", "REQUIRED_FILES", "Report", "validate_session"]

REQUIRED_FILES = ("session.json", "engine.json", "rig.json", "cameras.json", "frames.jsonl", "alignment.json",
                  "trajectory.bin", "qa.json")
CAMERA_PARAM_COUNT = {"SIMPLE_PINHOLE": 3, "PINHOLE": 4, "SIMPLE_RADIAL": 4, "RADIAL": 5, "OPENCV": 8, "OPENCV_FISHEYE": 8,
                      "FULL_OPENCV": 12}
FORBIDDEN_KEYS = frozenset({"created_wall_ns", "created_at", "host", "duration_s", "job_id", "world_id"})
ENGINE_SESSION_ID = re.compile(r"^rs-[0-9a-f]{12}$")
Q_TOL = 1e-9
MAX_ERRORS = 200


@dataclass
class Report:
    session_dir: str
    deep: bool
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    rules_checked: int = 0
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors

    def rules(self) -> set[str]:
        return {e["rule"] for e in self.errors}

    def error(self, rule: str, message: str) -> None:
        if len(self.errors) < MAX_ERRORS:
            self.errors.append({"rule": rule, "message": message[:400]})

    def warn(self, rule: str, message: str) -> None:
        self.warnings.append({"rule": rule, "message": message[:400]})

    def to_json(self) -> dict:
        return {"ok": self.ok, "deep": self.deep, "rules_checked": self.rules_checked, "errors": self.errors,
                "warnings": self.warnings, "seconds": round(self.seconds, 3)}


def _load(d: Path, name: str, rep: Report) -> dict | None:
    try:
        return json.loads((d / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        rep.error("V01", f"missing {name}")
    except (OSError, ValueError) as e:
        rep.error("V01", f"{name}: unreadable JSON ({e})")
    return None


def _qnorm_ok(q) -> bool:
    return abs(math.sqrt(sum(float(v) * float(v) for v in q)) - 1.0) <= Q_TOL


def validate_session(session_dir: Path, *, deep: bool = False) -> Report:
    t0 = time.perf_counter()
    d = Path(session_dir)
    rep = Report(str(d), deep)
    rules = ["V01", "V02", "V03", "V04", "V05", "V06", "V07", "V08", "V09", "V10", "V11", "V12", "V15", "V16", "V17"]
    rep.rules_checked = len(rules) + (3 if deep else 0)
    if not d.is_dir():
        rep.error("V01", f"{d} is not a directory")
        rep.seconds = time.perf_counter() - t0
        return rep
    for name in REQUIRED_FILES:
        if not (d / name).exists():
            rep.error("V01", f"missing {name}")
    docs = {n: _load(d, n, rep) for n in ("session.json", "engine.json", "rig.json", "cameras.json", "alignment.json",
                                          "qa.json") if (d / n).exists()}
    rows: list[dict] = []
    if (d / "frames.jsonl").exists():
        with open(d / "frames.jsonl", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError as e:
                    rep.error("V01", f"frames.jsonl line {i + 1}: {e}")
    ses, eng, rig, cams, al, qa = (docs.get(n) for n in ("session.json", "engine.json", "rig.json", "cameras.json",
                                                          "alignment.json", "qa.json"))

    # ---- V01 schema, schema names, ir_version
    for name, doc in docs.items():
        if doc is None:
            continue
        key = name.removesuffix(".json")
        if doc.get("ir_version") != IR_VERSION:
            rep.error("V01", f"{name}: ir_version {doc.get('ir_version')!r} != {IR_VERSION}")
        if doc.get("schema") != SCHEMA_NAMES[key]:
            rep.error("V01", f"{name}: schema {doc.get('schema')!r} != {SCHEMA_NAMES[key]}")
        for e in schema_errors(doc, DOC_SCHEMAS[name], limit=10):
            rep.error("V01", f"{name}{e['path']}: {e['message']}")
    bad_rows = 0
    for r in rows:
        errs = schema_errors(r, DOC_SCHEMAS["frames.jsonl"], limit=3)
        if errs:
            bad_rows += 1
            if bad_rows <= 5:
                rep.error("V01", f"frames.jsonl frame {r.get('frame_id')}: {errs[0]['path']}: {errs[0]['message']}")
    if ses is not None and (ses.get("input") or {}).get("kind") in ("world_sample", "video", "images"):
        if not ENGINE_SESSION_ID.match(str(ses.get("session_id", ""))):
            rep.error("V01", f"session_id {ses.get('session_id')!r} does not match ^rs-[0-9a-f]{{12}}$")
        if ses.get("session_id") != d.name:
            rep.warn("V01", f"directory name {d.name!r} differs from session_id {ses.get('session_id')!r}")

    # ---- V02 frame ids and timestamps
    ids = [r.get("frame_id") for r in rows]
    if ids != list(range(len(rows))):
        rep.error("V02", "frame_id must run 0..N-1 without gaps or duplicates")
    ts = [r.get("t_ns") for r in rows]
    if any(not isinstance(t, int) for t in ts):
        rep.error("V02", "t_ns must be integers")
    elif any(b < a for a, b in itertools.pairwise(ts)):
        rep.error("V02", "t_ns must be non-decreasing")

    # ---- V03 cameras
    cam_by_id: dict[int, dict] = {}
    for c in (cams or {}).get("cameras", []) or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("camera_id")
        cam_by_id[cid] = c
        model = c.get("model")
        params = c.get("params") or []
        want = CAMERA_PARAM_COUNT.get(model)
        if want is None:
            rep.error("V03", f"camera {cid}: unknown model {model!r}")
            continue
        if len(params) != want:
            rep.error("V03", f"camera {cid}: {model} needs {want} params, got {len(params)}")
            continue
        W, H = c.get("width", 0), c.get("height", 0)
        cx, cy = (params[1], params[2]) if model in ("SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL") else (params[2], params[3])
        if not (0.0 <= cx <= W and 0.0 <= cy <= H):
            rep.error("V03", f"camera {cid}: principal point ({cx}, {cy}) outside {W}x{H}")
    missing_cam = sorted({r.get("camera_id") for r in rows} - set(cam_by_id))
    if missing_cam:
        rep.error("V03", f"frames reference unknown camera_id {missing_cam[:5]}")

    # ---- V04 / V05 quaternions and frame 0
    for r in rows:
        q = (r.get("T_engine_cam") or {}).get("q") or []
        if len(q) != 4 or not _qnorm_ok(q) or float(q[3]) < 0:
            rep.error("V04", f"frame {r.get('frame_id')}: quaternion {q} must be unit (1e-9) with w >= 0")
            if sum(e["rule"] == "V04" for e in rep.errors) >= 5:
                break
    if rows:
        T0 = rows[0].get("T_engine_cam") or {}
        q0, t0v = T0.get("q") or [], T0.get("t") or []
        if (len(q0) != 4 or len(t0v) != 3 or max(abs(float(a) - b) for a, b in zip(q0, (0, 0, 0, 1), strict=True)) > 1e-9
                or max(abs(float(v)) for v in t0v) > 1e-9):
            rep.error("V05", f"frame 0 T_engine_cam must be the identity, got q={q0} t={t0v}")

    # ---- V06 keyframes and frame types
    ftypes = [r.get("frame_type") for r in rows]
    if any(ft not in (0, 1, 2, 255) for ft in ftypes):
        rep.error("V06", "frame_type must be 0, 1, 2 or 255")
    if rows and not any(ft in (0, 1) for ft in ftypes):
        rep.error("V06", "at least one key (or scale) frame is required")

    # ---- V07 / V08 engine
    if eng is not None:
        sc = (eng.get("normalization") or {}).get("self_check") or {}
        if sc.get("pass") is not True:
            rep.error("V07", f"engine.json self_check did not pass (votes {sc.get('votes')})")
        w = eng.get("weights")
        if w is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", str((w or {}).get("sha256", ""))):
                rep.error("V08", "engine.json weights must carry a 64-hex sha256")
            if not (w or {}).get("license"):
                rep.error("V08", "engine.json weights must carry a license")

    # ---- V09 / V10 / V11 alignment and scale status
    if al is not None:
        S = al.get("T_world_engine") or {}
        s = S.get("s")
        if not (isinstance(s, (int, float)) and s > 0):
            rep.error("V09", f"T_world_engine.s must be > 0, got {s}")
        if not (len(S.get("q") or []) == 4 and _qnorm_ok(S["q"])):
            rep.error("V09", f"T_world_engine.q must be a unit quaternion, got {S.get('q')}")
        m_none = al.get("method") == "none"
        rel = al.get("scale_status") == "relative"
        if m_none != rel or (m_none and al.get("status") not in ("rejected", "skipped")):
            rep.error("V10", f"method {al.get('method')!r} inconsistent with scale_status {al.get('scale_status')!r} / "
                             f"status {al.get('status')!r} (method none <=> relative, and then rejected or skipped)")
        ss = al.get("scale_status")
        if ss not in SCALE_STATUS_RECON:
            rep.error("V11", f"alignment scale_status {ss!r} not in {SCALE_STATUS_RECON}")
        if ses is not None and ses.get("scale_status") != ss:
            rep.error("V11", f"session scale_status {ses.get('scale_status')!r} != alignment {ss!r}")
    if ses is not None and ses.get("scale_status") not in SCALE_STATUS_RECON:
        rep.error("V11", f"session scale_status {ses.get('scale_status')!r} not in {SCALE_STATUS_RECON}")

    # ---- V12 trajectory.bin structure
    traj = None
    if (d / "trajectory.bin").exists():
        try:
            traj = read_trajectory_bin(d / "trajectory.bin")
        except AwtrError as e:
            rep.error("V12", f"trajectory.bin: {e}")
        if traj is not None:
            if traj.n != len(rows):
                rep.error("V12", f"trajectory.bin N = {traj.n} != {len(rows)} frames")
            elif rows and not np.array_equal(traj.t_rel_ns, np.array(ts, dtype=np.int64)):
                rep.error("V12", "trajectory.bin t_rel_ns differs from frames.jsonl t_ns")
            if ses is not None and str(traj.t0_ns) != str((ses.get("time_base") or {}).get("t0_ns")):
                rep.error("V12", f"trajectory.bin t0_ns {traj.t0_ns} != session time_base.t0_ns")
            if traj.body:
                rep.error("V12", "trajectory.bin flags.bit0 must be 0 (T_world_cam) for recon sessions")

    # ---- V15 confidence histogram
    if qa is not None and ses is not None:
        hist = qa.get("conf_hist_u8") or []
        if sum(int(v) for v in hist) != int((ses.get("counts") or {}).get("points_fused", -1)):
            rep.error("V15", f"sum(conf_hist_u8) = {sum(int(v) for v in hist)} != counts.points_fused "
                             f"{(ses.get('counts') or {}).get('points_fused')}")

    # ---- V16 no context fields in published session files (top-level keys: `input` legitimately carries the source
    # world id, M01 §6.3.3; context fields such as the target world, job, host and wall clock never appear at the top)
    for name, doc in [*docs.items(), *((f"frames.jsonl#{r.get('frame_id')}", r) for r in rows)]:
        if isinstance(doc, dict):
            for k in doc:
                if k in FORBIDDEN_KEYS:
                    rep.error("V16", f"{name}: forbidden context key /{k}")

    # ---- V17 rig
    for sn in (rig or {}).get("sensors", []) or []:
        if not isinstance(sn, dict):
            continue
        T = sn.get("T_body_sensor")
        if not isinstance(T, dict) or len(T.get("q") or []) != 4 or not _qnorm_ok(T["q"]):
            rep.error("V17", f"sensor {sn.get('sensor_id')}: T_body_sensor must be rigid (unit quaternion, no scale)")
        off = sn.get("time_offset_ns")
        if not isinstance(off, int) or isinstance(off, bool):
            rep.error("V17", f"sensor {sn.get('sensor_id')}: time_offset_ns must be an integer")

    if deep:
        # ---- V13 registered poses (float64 reference)
        if traj is not None and al is not None and rows and traj.n == len(rows):
            try:
                S = al["T_world_engine"]
                s = float(S["s"])
                R = quat_to_mat(np.asarray(S["q"], dtype=np.float64))
                t = np.asarray(S["t"], dtype=np.float64)
                T, _ = poses_from_rows(rows)
                p_ref = s * T[:, :3, 3] @ R.T + t
                R_ref = R @ T[:, :3, :3]
                dp = np.linalg.norm(traj.pos.astype(np.float64) - p_ref, axis=1)
                lim = 1e-7 * np.maximum(1.0, np.linalg.norm(p_ref, axis=1))
                R_tr = quat_to_mat(traj.q_xyzw.astype(np.float64))
                Rrel = np.einsum("nji,njk->nik", R_ref, R_tr)
                vee = np.stack([Rrel[:, 2, 1] - Rrel[:, 1, 2], Rrel[:, 0, 2] - Rrel[:, 2, 0], Rrel[:, 1, 0] - Rrel[:, 0, 1]], 1)
                ang = np.arctan2(0.5 * np.linalg.norm(vee, axis=1), (np.trace(Rrel, axis1=1, axis2=2) - 1.0) / 2.0)
                if np.any(dp > lim):
                    i = int(np.argmax(dp - lim))
                    rep.error("V13", f"trajectory position of frame {i} differs from T_world_engine * T_engine_cam by {dp[i]:.3e} m")
                if np.any(ang > 1e-6 + 1e-7):
                    i = int(np.argmax(ang))
                    rep.error("V13", f"trajectory rotation of frame {i} differs by {ang[i]:.3e} rad")
                if np.any(traj.q_xyzw[:, 3] < 0):
                    rep.error("V13", "trajectory quaternions must have w >= 0")
            except (KeyError, TypeError, ValueError) as e:
                rep.error("V13", f"cannot recompute registered poses: {e}")
        # ---- V14 files[]
        if ses is not None:
            listed = set()
            for f in ses.get("files") or []:
                p = d / f.get("path", "")
                listed.add(f.get("path"))
                if not p.is_file():
                    rep.error("V14", f"files[]: {f.get('path')} missing")
                    continue
                if p.stat().st_size != f.get("bytes") or file_sha256(p) != f.get("sha256"):
                    rep.error("V14", f"files[]: {f.get('path')} bytes or sha256 differ")
            for p in sorted(d.rglob("*")):
                rel = p.relative_to(d).as_posix()
                if p.is_file() and rel != "session.json" and rel not in listed:
                    rep.warn("V14", f"{rel} is not listed in files[]")
        # ---- V18 retained points
        pdir = d / "points"
        if pdir.is_dir() and ses is not None:
            try:
                sc = json.loads((pdir / "source.json").read_text(encoding="utf-8"))
                n = int(sc.get("count", -1))
                if sc.get("frame") != "engine":
                    rep.error("V18", f"points/source.json frame {sc.get('frame')!r} != engine")
                if n != int((ses.get("counts") or {}).get("points_fused", -2)):
                    rep.error("V18", f"points count {n} != counts.points_fused")
                fs = pdir / "first_seen.u32"
                if fs.exists():
                    first = np.fromfile(fs, "<u4")
                    if len(first) != n or (len(first) and int(first.max()) > max(ids or [0])):
                        rep.error("V18", "points/first_seen.u32 length or range invalid")
            except (OSError, ValueError) as e:
                rep.error("V18", f"points/: {e}")
    rep.seconds = time.perf_counter() - t0
    return rep
