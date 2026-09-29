"""切片器公共入口 `tile()`（AWR-16 §4.12 冻结签名；AWR-03 §8.7 落点）。编排森林切分、建树、编码与写出。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np

from .octree import forest_split
from .writer import RootEntry, write_container

VISUAL_HREF = "visual/pointcloud/"


def tile(points_world: np.ndarray, normals: np.ndarray | None, cls_index: np.ndarray, out_dir: Path, *,
         G: int = 64, leaf: int = 20000, compression: Literal["none", "gzip"] = "none",
         forest: Literal["auto", "off"] = "auto", seed: int = 1, z_range: tuple[float, float],
         hag_range: tuple[float, float] | None, nn_median_m: float, twin_default: bool = False,
         name: str = "world", normals_flipped_frac: float | None = None,
         presorted: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
         href_prefix: str = VISUAL_HREF, normals_oct16: np.ndarray | None = None) -> list[RootEntry]:
    """切片 float64 world 点（m）；`out_dir` 为 `visual/pointcloud/` 目录。

    `presorted` 是世界立方体上的 Morton 排序结果（源点云写出时得到），只在单根世界复用（立方体相同）。
    """
    if twin_default:
        raise NotImplementedError("--twin-default 为 P2（16 §4.13），D1 不提供")
    if G not in (16, 32, 64, 128, 256):
        raise ValueError(f"G must be one of 16, 32, 64, 128, 256 (got {G})")
    if leaf < 100:
        raise ValueError(f"leaf must be >= 100 (got {leaf})")
    P = np.asarray(points_world, np.float64)
    out_dir = Path(out_dir)
    roots = forest_split(P, forest)
    k = len(roots)
    entries: list[RootEntry] = []
    for ri, (cm, size, mask) in enumerate(roots):
        rname = "r" if k == 1 else f"r-{ri}"
        sub = out_dir if k == 1 else out_dir / rname
        href = href_prefix if k == 1 else f"{href_prefix}{rname}/"
        if mask is None:
            Pm, Nm, Cm, pre, Wm = P, normals, cls_index, presorted, normals_oct16
        else:
            Pm = P[mask]
            Nm = None if normals is None else np.asarray(normals)[mask]
            Cm = np.asarray(cls_index)[mask]
            Wm = None if normals_oct16 is None else np.asarray(normals_oct16)[mask]
            pre = None
        md = write_container(sub, name if k == 1 else f"{name}-{ri}", Pm, Nm, Cm, z_range, cm, size, G=G, leaf=leaf,
                             seed=seed, compression=compression, forest=(ri, k), hag_range=hag_range,
                             nn_median_m=nn_median_m, normals_flipped_frac=normals_flipped_frac, presorted=pre,
                             normals_oct16=Wm)
        a = md["anet"]
        entries.append(RootEntry(name=rname, href=href, cube_min=np.asarray(cm, np.float64), cube_size=float(size),
                                 points=int(md["points"]), depth=int(md["hierarchy"]["depth"]),
                                 first_screen_bytes=int(a["levelsByteEnd"][a["firstScreenLevel"]]), metadata=md))
    return entries
