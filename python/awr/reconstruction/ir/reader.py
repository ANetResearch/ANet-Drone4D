"""SessionReader: read-only access to a `reconstruction/<session_id>/` directory (M01 §9.1)."""

from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path

import numpy as np

from awr.world.georef.frames import quat_to_mat

from .trajectory_bin import Trajectory, read_trajectory_bin

__all__ = ["SessionReader", "poses_from_rows"]


def poses_from_rows(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """(T_engine_cam [N, 4, 4] float64, q_xyzw [N, 4]) from frames.jsonl rows."""
    q = np.array([r["T_engine_cam"]["q"] for r in rows], dtype=np.float64).reshape(-1, 4)
    t = np.array([r["T_engine_cam"]["t"] for r in rows], dtype=np.float64).reshape(-1, 3)
    T = np.tile(np.eye(4), (len(rows), 1, 1))
    if len(rows):
        T[:, :3, :3] = quat_to_mat(q)
        T[:, :3, 3] = t
    return T, q


class SessionReader:
    def __init__(self, session_dir: Path) -> None:
        self.dir = Path(session_dir)

    def doc(self, name: str) -> dict:
        return json.loads((self.dir / name).read_text(encoding="utf-8"))

    @cached_property
    def session(self) -> dict:
        return self.doc("session.json")

    @cached_property
    def engine(self) -> dict:
        return self.doc("engine.json")

    @cached_property
    def rig(self) -> dict:
        return self.doc("rig.json")

    @cached_property
    def cameras(self) -> dict:
        return self.doc("cameras.json")

    @cached_property
    def alignment(self) -> dict:
        return self.doc("alignment.json")

    @cached_property
    def qa(self) -> dict:
        return self.doc("qa.json")

    @cached_property
    def frames(self) -> list[dict]:
        with open(self.dir / "frames.jsonl", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def poses(self) -> tuple[np.ndarray, np.ndarray]:
        return poses_from_rows(self.frames)

    def trajectory(self) -> Trajectory:
        return read_trajectory_bin(self.dir / "trajectory.bin")
