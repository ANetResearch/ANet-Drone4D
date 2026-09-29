"""UrbanScene3D 航线文件解析（D1 桩，P2 功能在 V0.2；M16 §2.2 "D1 桩"，M02 UC-06、M12-FR-053）。

D1 只冻结接口：`parse_path_file(path)` 读取 `data/raw/urbanscene3d/paths/` 下的航线文本（每行
`x y z [yaw_deg]`，`#` 起始为注释，空行忽略），返回 `CameraPath`。V0.2 由 M12 的导入器把 43,654 个视点转成回放剧本。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = ["CameraPath", "parse_path_file", "parse_path_text"]


@dataclass(frozen=True)
class CameraPath:
    name: str
    points: np.ndarray        # (n, 3) float64，文件单位（未规范化到 world ENU）
    yaw_deg: np.ndarray | None

    def __len__(self) -> int:
        return int(self.points.shape[0])


def parse_path_text(text: str, name: str = "<text>") -> CameraPath:
    rows: list[list[float]] = []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip().replace(",", " ")
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            raise ValueError(f"{name}:{i}: expected at least 3 numbers, got {len(parts)}")
        try:
            rows.append([float(v) for v in parts[:4]])
        except ValueError as e:
            raise ValueError(f"{name}:{i}: {e}") from None
    if not rows:
        return CameraPath(name, np.zeros((0, 3)), None)
    width = min(len(r) for r in rows)
    a = np.asarray([r[:width] for r in rows], np.float64)
    yaw = a[:, 3].copy() if width >= 4 else None
    return CameraPath(name, a[:, :3].copy(), yaw)


def parse_path_file(path: Path | str) -> CameraPath:
    p = Path(path)
    return parse_path_text(p.read_text(encoding="utf-8", errors="replace"), p.name)
