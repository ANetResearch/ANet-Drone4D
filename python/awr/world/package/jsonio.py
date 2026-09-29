"""确定性 JSON 序列化（M03-FR-041）：固定键序（按构造顺序）、`indent=1`、`ensure_ascii=False`、末尾换行。

参与哈希的文件不含时间戳与主机信息；浮点数按 Python `repr`（最短往返表示）写出，跨机器一致。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def _check_finite(obj: Any, path: str = "$") -> None:
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"non-finite float at {path}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _check_finite(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check_finite(v, f"{path}[{i}]")


def dumps(obj: Any) -> str:
    _check_finite(obj)
    return json.dumps(obj, indent=1, ensure_ascii=False, allow_nan=False) + "\n"


def write_json(path: Path, obj: Any) -> bytes:
    """写出并返回文件字节（调用方可直接求 sha256）。"""
    data = dumps(obj).encode("utf-8")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_file(path: Path, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
