"""目录图层 sidecar 的逐元素数据流（M22 §7.3.1 `streams[]`、§7.3.7）：`awr-trimesh@1` 的 `cls`、`q` 与外观目录的 `tri.cls`、`tri.q`
由后续阶段写入同目录，并以 `{name, href, dtype, components}` 登记到 `index.json` 的 `streams`。

写入为小端原始数组；同名数据流替换原条目（位置不变），新数据流追加在末尾，`index.json` 的其余字段与键序不变，结果确定。
本模块只依赖 numpy 与标准库（D2 包，供 D3 派生阶段与读取方共用；AWR-05 §4.8）。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

__all__ = ["DTYPES", "add_stream", "read_stream", "stream_entry"]

DTYPES = {"uint8": "<u1", "uint16": "<u2", "uint32": "<u4", "int32": "<i4", "float16": "<f2", "float32": "<f4", "float64": "<f8"}


def _load(index_json: Path) -> dict:
    return json.loads(Path(index_json).read_text(encoding="utf-8"))


def stream_entry(index_json: Path, name: str) -> dict | None:
    """`index.json` 中名为 `name` 的数据流条目；不存在为 None。"""
    for s in _load(index_json).get("streams", []):
        if s.get("name") == name:
            return s
    return None


def add_stream(index_json: Path, name: str, href: str, data: np.ndarray, *, dtype: str, components: int = 1,
               count: int | None = None) -> dict:
    """写出 `<index.json 所在目录>/<href>` 并在 `streams` 中登记（同名替换）。`count` 给出时校验元素数（按 `components` 计）。

    返回登记的条目。`dtype` 取 `DTYPES` 的键；数组按小端写出，不做取值检查（质量位、类别码由调用方保证范围）。
    """
    if dtype not in DTYPES:
        raise ValueError(f"stream dtype {dtype!r} not in {sorted(DTYPES)}")
    index_json = Path(index_json)
    a = np.ascontiguousarray(np.asarray(data).astype(DTYPES[dtype], copy=False))
    if count is not None and a.size != int(count) * int(components):
        raise ValueError(f"stream {name}: {a.size} values != {count} x {components}")
    (index_json.parent / href).write_bytes(a.tobytes())
    doc = _load(index_json)
    ent = {"name": name, "href": href, "dtype": dtype, "components": int(components)}
    streams = list(doc.get("streams", []))
    for i, s in enumerate(streams):
        if s.get("name") == name:
            streams[i] = ent
            break
    else:
        streams.append(ent)
    doc["streams"] = streams
    index_json.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    return ent


def read_stream(index_json: Path, name: str) -> np.ndarray | None:
    """读取数据流为 numpy 数组（`components > 1` 时形状为 (n, components)）；未登记为 None。"""
    index_json = Path(index_json)
    s = stream_entry(index_json, name)
    if s is None:
        return None
    a = np.fromfile(index_json.parent / s["href"], DTYPES[s["dtype"]])
    k = int(s.get("components", 1))
    return a.reshape(-1, k) if k > 1 else a
