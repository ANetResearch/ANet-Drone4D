"""固定布局 PLY 读取（M03-FR-005；x01 §1.3、§2.4）：`binary_little_endian`，`x y z nx ny nz` float32，memmap。

校验 `header + N·24 == 文件大小`（不符 → RawDataError，退出码 4）与格式、属性白名单（不符 → ConfigError，退出码 3）；
sha256 以 4 MiB 分块流式计算。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .types import ConfigError, RawDataError

EXPECTED_PROPS = ("x", "y", "z", "nx", "ny", "nz")
_FLOAT_NAMES = {"float", "float32"}
MAX_HEADER = 1 << 16


@dataclass(slots=True)
class PlyInfo:
    header_bytes: int
    n: int
    props: tuple[str, ...]
    file_size: int
    expected: int


def read_header(path: Path) -> PlyInfo:
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(MAX_HEADER)
    end = head.find(b"end_header\n")
    if not head.startswith(b"ply\n") or end < 0:
        raise ConfigError(f"{path.name}: 不是 PLY 文件或 header 过长")
    off = end + len(b"end_header\n")
    lines = head[:off].decode("latin1").splitlines()
    fmt = None
    n = None
    props: list[tuple[str, str]] = []
    in_vertex = False
    for line in lines:
        t = line.split()
        if not t:
            continue
        if t[0] == "format":
            fmt = t[1:]
        elif t[0] == "element":
            in_vertex = t[1] == "vertex"
            if in_vertex:
                n = int(t[2])
            elif int(t[2]) != 0:
                raise ConfigError(f"{path.name}: 不支持的 element {t[1]}")
        elif t[0] == "property":
            if t[1] == "list":
                raise ConfigError(f"{path.name}: 不支持 list 属性")
            if in_vertex:
                props.append((t[2], t[1]))
    if fmt != ["binary_little_endian", "1.0"]:
        raise ConfigError(f"{path.name}: format {fmt} 不是 binary_little_endian 1.0")
    if n is None:
        raise ConfigError(f"{path.name}: 缺少 element vertex")
    names = tuple(p[0] for p in props)
    if names != EXPECTED_PROPS or any(p[1] not in _FLOAT_NAMES for p in props):
        raise ConfigError(f"{path.name}: 属性 {props} 不是 x y z nx ny nz（float32）")
    size = path.stat().st_size
    return PlyInfo(off, n, names, size, off + n * 24)


def read_ply(path: Path, expect_sha256: str | None = None, expect_bytes: int | None = None
             ) -> tuple[np.ndarray, np.ndarray, PlyInfo, str]:
    """返回 (xyz float32 memmap 视图 [N,3]，normal float32 [N,3]，header 信息，sha256)。"""
    path = Path(path)
    if not path.exists():
        raise RawDataError(f"原始数据缺失：{path}；修复：make fetch-data", missing=True)
    info = read_header(path)
    if info.file_size != info.expected:
        raise RawDataError(f"{path.name}: 文件 {info.file_size} B 与 header + N×24 = {info.expected} B 不符（截断或损坏）")
    if expect_bytes is not None and info.file_size != expect_bytes:
        raise RawDataError(f"{path.name}: 字节数 {info.file_size} 与清单 {expect_bytes} 不符")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 22), b""):
            h.update(blk)
    sha = h.hexdigest()
    if expect_sha256 is not None and sha != expect_sha256:
        raise RawDataError(f"{path.name}: sha256 {sha} 与 configs/data.yaml 的 {expect_sha256} 不符；修复：make fetch-data")
    arr = np.memmap(path, dtype="<f4", mode="r", offset=info.header_bytes, shape=(info.n, 6))
    return arr[:, :3], arr[:, 3:], info, sha
