"""SessionWriter: writes a `reconstruction/<session_id>/` directory (M01 §6.3.2, M01-FR-011).

JSON documents are canonical and atomic; `frames.jsonl` is appended to `frames.jsonl.partial` during INFERRING and
renamed once when the stage ends (raw layer written once, P-M01-2); `finalize()` hashes every file except `session.json`
into `files[]` (sorted by path) and writes `session.json` last, so a complete `session.json` implies a complete session.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from ..types import IR_VERSION, SCHEMA_NAMES
from .jsonio import dumps_line, file_sha256, write_json

__all__ = ["SessionWriter", "file_list", "frame_row", "rigid_json"]

FRAMES = "frames.jsonl"
FRAMES_PARTIAL = "frames.jsonl.partial"


def rigid_json(T: np.ndarray, q_xyzw: np.ndarray | None = None) -> dict:
    """{q: [x, y, z, w], t: [x, y, z]} from a 4x4 rigid transform (q supplied or derived by M02 frames)."""
    if q_xyzw is None:
        from ..conventions import mat_to_quat_xyzw

        q_xyzw = mat_to_quat_xyzw(np.asarray(T)[:3, :3])
    return {"q": [float(v) for v in q_xyzw], "t": [float(v) for v in np.asarray(T)[:3, 3]]}


def frame_row(frame_id: int, t_ns: int, T_engine_cam: np.ndarray, q_xyzw: np.ndarray, *, camera_id: int = 1,
              frame_type: int = 1, pose_source: str = "engine", conf_mean_u8: int | None = None,
              valid_ratio: float | None = None, chunk: int | None = None, image: dict | None = None, gnss: dict | None = None,
              gravity_cam: np.ndarray | None = None, depth: dict | None = None) -> dict:
    row: dict = {"frame_id": int(frame_id), "t_ns": int(t_ns), "camera_id": int(camera_id), "frame_type": int(frame_type),
                 "T_engine_cam": rigid_json(T_engine_cam, q_xyzw), "pose_source": pose_source, "image": image}
    if conf_mean_u8 is not None:
        row["conf_mean_u8"] = int(conf_mean_u8)
    if valid_ratio is not None:
        row["valid_ratio"] = round(float(valid_ratio), 6)
    if chunk is not None:
        row["chunk"] = int(chunk)
    if gnss is not None:
        row["gnss"] = gnss
    if gravity_cam is not None:
        row["gravity_cam"] = [float(v) for v in gravity_cam]
    if depth is not None:
        row["depth"] = depth
    return row


def file_list(session_dir: Path, *, exclude: tuple[str, ...] = ("session.json",)) -> list[dict]:
    d = Path(session_dir)
    out = []
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(d).as_posix()
        if rel in exclude or rel.endswith(".partial") or ".tmp." in p.name:
            continue
        out.append({"path": rel, "bytes": p.stat().st_size, "sha256": file_sha256(p)})
    return sorted(out, key=lambda f: f["path"])


class SessionWriter:
    def __init__(self, session_dir: Path) -> None:
        self.dir = Path(session_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._frames = None
        self.n_frames = 0

    def write_doc(self, name: str, obj: dict) -> Path:
        """Write one of session/engine/rig/cameras/alignment/qa .json (schema and ir_version added when missing)."""
        key = name.removesuffix(".json")
        doc = {"schema": SCHEMA_NAMES[key], "ir_version": IR_VERSION, **obj}
        p = self.dir / f"{key}.json"
        write_json(p, doc)
        return p

    # -- raw layer
    def open_frames(self) -> None:
        if (self.dir / FRAMES).exists():
            raise FileExistsError(f"{self.dir / FRAMES} already written (raw layer is written once)")
        self._frames = open(self.dir / FRAMES_PARTIAL, "wb")  # noqa: SIM115 - kept open across the INFERRING loop
        self.n_frames = 0

    def append_frame(self, row: dict) -> None:
        if self._frames is None:
            raise RuntimeError("open_frames() first")
        if row["frame_id"] != self.n_frames:
            raise ValueError(f"frame_id {row['frame_id']} != {self.n_frames} (ids are contiguous from 0)")
        self._frames.write(dumps_line(row))
        self.n_frames += 1

    def close_frames(self) -> Path:
        if self._frames is None:
            raise RuntimeError("open_frames() first")
        self._frames.flush()
        os.fsync(self._frames.fileno())
        self._frames.close()
        self._frames = None
        dst = self.dir / FRAMES
        os.replace(self.dir / FRAMES_PARTIAL, dst)
        return dst

    def abort_frames(self) -> None:
        if self._frames is not None:
            self._frames.close()
            self._frames = None
        (self.dir / FRAMES_PARTIAL).unlink(missing_ok=True)

    def finalize(self, session: dict) -> dict:
        """Hash the other files into `files[]` and write session.json; returns the written document."""
        doc = {"schema": SCHEMA_NAMES["session"], "ir_version": IR_VERSION, **session, "files": file_list(self.dir)}
        write_json(self.dir / "session.json", doc)
        return doc
