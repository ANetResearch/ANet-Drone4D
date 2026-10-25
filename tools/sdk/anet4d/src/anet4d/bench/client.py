"""bench-api REST 薄封装（AWR-17 §18.4；BN-42、43、49 至 52）：只经 `Authorization: Bearer` 携带 bench token，不建立会话。

单帧与区间帧返回交付字节并逐帧校验 sha256（区间容器 `d3frames.v1` 另校验尾 sha256）；真值帧解析 `awr.sensor.camera_gt.v1`
容器（评测员）。图像与深度解码需要可选依赖 numpy、Pillow。
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = ["BenchClient", "BenchError", "Frame", "TruthFrame", "parse_d3frames", "parse_d3gt"]

PREFIX = "/api/bench/v1"


class BenchError(Exception):
    def __init__(self, status: int, body: dict | None) -> None:
        b = body or {}
        self.status = status
        self.code = b.get("code")
        self.name = b.get("name")
        self.detail = b.get("detail")
        super().__init__(f"{status} {self.code} {self.name}: {b.get('message', '')}")


@dataclass
class Frame:
    seq: int
    t_ns: int
    data: bytes
    content_type: str = "image/jpeg"
    size: tuple[int, int] | None = None              # (width, height)，深度帧

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def image(self):
        """JPEG → numpy (H, W, 3) uint8。"""
        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(self.data)) as im:
            return np.asarray(im.convert("RGB"))

    def depth(self):
        """`32FC1` 平面深度 → numpy (H, W) float32（米，0 为无几何）。"""
        import numpy as np

        if self.size is None:
            raise ValueError("not a depth frame")
        w, h = self.size
        return np.frombuffer(self.data, "<f4").reshape(h, w)


@dataclass
class TruthFrame:
    record: dict
    blocks: dict[str, bytes]

    def array(self, kind: str):
        """解压真值块：kind ∈ instance、instance_unocc、semantic、invalid_mask、gt_depth。"""
        import numpy as np

        gt = self.record["ground_truth"]
        av = self.record["agent_view"]
        h, w = av["height"], av["width"]
        ent = gt[kind]
        raw = self.blocks[ent["ref"]]
        if ent.get("codec") in ("zstd", "bshuf4-zstd"):
            import zstandard

            raw = zstandard.ZstdDecompressor().decompress(raw)
            if ent["codec"] == "bshuf4-zstd":                 # f32 的 4 个字节平面还原为逐元素小端序
                raw = np.ascontiguousarray(np.frombuffer(raw, np.uint8).reshape(4, -1).T).tobytes()
        dt = {"u16": "<u2", "u8": "u1", "f32": "<f4"}[ent["dtype"]]
        return np.frombuffer(raw, dt).reshape(h, w)


def parse_d3frames(b: bytes) -> tuple[dict, list[Frame]]:
    if b[:4] != b"D3FR":
        raise ValueError("not a d3frames.v1 container")
    if hashlib.sha256(b[:-32]).digest() != b[-32:]:
        raise ValueError("d3frames trailer sha256 mismatch")
    ver, _flags, hl = struct.unpack_from("<HHI", b, 4)
    if ver != 1:
        raise ValueError(f"d3frames version {ver}")
    head = json.loads(b[12:12 + hl])
    p, out = 12 + hl, []
    while p < len(b) - 32:
        seq, t, n = struct.unpack_from("<IqI", b, p)
        sha, data = b[p + 16:p + 48], b[p + 48:p + 48 + n]
        if hashlib.sha256(data).digest() != sha:
            raise ValueError(f"frame {seq}: sha256 mismatch")
        out.append(Frame(seq, t, data))
        p += 48 + n
    return head, out


def parse_d3gt(b: bytes) -> TruthFrame:
    if b[:4] != b"D3GT":
        raise ValueError("not an awr.sensor.camera_gt.v1 container")
    _ver, n, jl = struct.unpack_from("<HHI", b, 4)
    p = 12
    rec = json.loads(b[p:p + jl])
    p += jl
    blocks = {}
    for _ in range(n):
        (nl,) = struct.unpack_from("<H", b, p)
        name = b[p + 2:p + 2 + nl].decode()
        p += 2 + nl
        (ln,) = struct.unpack_from("<I", b, p)
        blocks[name] = b[p + 4:p + 4 + ln]
        p += 4 + ln
    return TruthFrame(rec, blocks)


class BenchClient:
    """`base_url` 为 bench 主机（不含 `/api/bench/v1`）；`client` 可注入现成的 httpx.Client（测试或自定义传输）。"""

    def __init__(self, base_url: str = "", token: str = "", *, client: httpx.Client | None = None, timeout_s: float = 30.0) -> None:
        self._own = client is None
        self.http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def close(self) -> None:
        if self._own:
            self.http.close()

    def __enter__(self) -> BenchClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, path: str, **params: Any) -> httpx.Response:
        r = self.http.get(PREFIX + path, params={k: v for k, v in params.items() if v is not None}, headers=self.headers)
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = None
            raise BenchError(r.status_code, body)
        return r

    def meta(self) -> dict:
        return self._get("/meta").json()

    def runs(self) -> list[dict]:
        return self._get("/runs").json()["items"]

    def run(self, run_id: str) -> dict:
        return self._get(f"/runs/{run_id}").json()

    def frame_index(self, run_id: str, uav: str, sensor: str, seq_from: int = 0, seq_to: int | None = None) -> Iterator[dict]:
        """逐页取 `bench/frame_index.v1` 的条目（自动跟随 next_seq）。"""
        nxt: int | None = seq_from
        while nxt is not None:
            page = self._get(f"/runs/{run_id}/frames/{uav}/{sensor}", seq_from=nxt, seq_to=seq_to).json()
            yield from page["items"]
            nxt = page["next_seq"]

    def frame(self, run_id: str, uav: str, sensor: str, seq: int) -> Frame:
        r = self._get(f"/runs/{run_id}/frames/{uav}/{sensor}/{seq}")
        etag = r.headers.get("etag", "").strip('"')
        f = Frame(seq, int(r.headers["x-frame-t-ns"]), r.content, r.headers.get("content-type", ""))
        if etag and etag != f.sha256:
            raise ValueError(f"frame {seq}: sha256 does not match ETag")
        if "x-frame-size" in r.headers:
            w, h = (int(v) for v in r.headers["x-frame-size"].split("x"))
            f.size = (w, h)
        return f

    def frames(self, run_id: str, uav: str, sensor: str, seq_from: int, seq_to: int, *, stride: int = 1) -> Iterator[Frame]:
        """区间帧（`d3frames.v1`），按 300 × stride 的上限自动分段请求。"""
        step = 300 * stride
        a = seq_from
        while a <= seq_to:
            b = min(a + step - 1, seq_to)
            r = self._get(f"/runs/{run_id}/frames/{uav}/{sensor}:range", seq_from=a, seq_to=b, stride=stride)
            _head, fs = parse_d3frames(r.content)
            yield from fs
            a = b + 1 + ((stride - (b + 1 - seq_from) % stride) % stride)

    def truth_frame(self, run_id: str, uav: str, sensor: str, seq: int) -> TruthFrame:
        return parse_d3gt(self._get(f"/runs/{run_id}/truth/frames/{uav}/{sensor}/{seq}").content)
