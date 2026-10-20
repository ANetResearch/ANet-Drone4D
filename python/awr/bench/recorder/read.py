"""帧读取（M27-FR-052；AWR-17 §18.8：BN-49 帧索引、BN-50 单帧、BN-51 区间帧 `d3frames.v1`）与内部回放磁带演员源（M27-FR-053）。

按 `frames.idx` 定位 `raw.mcap` 中的消息，块缓存复用 D2 `ChunkCache`；返回交付字节（`foxglove.CompressedImage.data` 即 Agent 收到
的 JPEG；深度为 f32 行主序，存档的字节重排 + zstd 在此解开），索引与容器中的 sha256 取交付字节。交付字节的长度与 sha256 按帧缓存，
索引分页不重复解码。

`TapeActorSource`（M12 扩展点 11 的演员源；M23-FR-049 `tape_target_rows`；D3-AC-19、M27-AC-009、M23-AC-026）：内部回放不在 MCAP
中保存演员状态，而是读取 `raw.mcap` 的 `tape.bin` / `tape.json` 附件（Env 录制时写入的磁带原始字节，按 `content_sha256` 校验，
不一致 614），在回放播放头 `t_sim_ns` 处以 M23 `TapeReader.actor_states`（§6.8 插值，与运行期 M18 运动源、M25 曝光时刻同一函数）
补出脚本演员，并打包为 `awr.TargetLite32.v1` 行（与运行期 `state/sim-core/targets` 同一布局，前端识别物图层零改动）：

- 行序与 `agent_no`：脚本演员 idx 升序，`agent_no = idx`；识别物 id 与运行期 `awr.sim.events.runtime.target_id` 相同；
- `class`：演员类别名 → TargetClass（未知 → UNKNOWN）；`state`：bit7 ENABLED = 磁带状态 1 移动或 2 等待（与 `TapeMotionSource`
  一致），bit4 MOVING 恒为 1（timeline 识别物，M18 `is_moving`）；awareness、best、flags（HIDDEN 恒为 0）、警觉度与发现次数在
  回放中不可得，记 0；
- `pos` 为 ENU f32（磁带毫米定点经 Hermite 插值后取 f32，|坐标| < 131 km 时量化误差 ≤ 3.9 mm，远小于 5 cm 阈值）；`heading_rad`
  取磁带航向；`speed_cms` 为水平速度（cm/s，饱和 ±32767）。

时间：Env 运行的仿真时刻即事件时刻（`offset_ns = 0`）；会话以 `event.offset_s` 装配时传入 `offset_ns`，与 `TapeMotionSource` 同义。
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from awr.bench.recorder.channels import camera_topic, parse_obs_topic
from awr.bench.recorder.foxglove import decode_image
from awr.bench.recorder.frames_idx import FramesIdx
from awr.bench.recorder.lidar import LIDAR_CODEC
from awr.recorder.chunk_cache import ChunkCache
from awr.recorder.mcap_index import MsgRef, SegmentIndex

__all__ = ["CONTENT_TYPES", "MAX_FRAMES_PER_STRIDE", "MAX_RANGE_BYTES", "REPLAY_TOL_M", "Frame", "FrameLimitError", "FrameStore", "TapeActorSource",
           "TruthStore", "run_attachments", "tape_target_rows"]

MAX_FRAMES_PER_STRIDE = 300
MAX_RANGE_BYTES = 64 << 20
MAX_INDEX_ITEMS = 1000
CONTENT_TYPES = {"jpeg": "image/jpeg", "32FC1": "application/octet-stream", "16UC1": "application/zstd"}
# 16UC1：热成像通道，载荷为 ThermalObs.temp_ck 交付字节（u16 + zstd 原样，存档压缩即交付形式，不解开；channels.thermal_message）
_SCHEMA_OF_CODEC = {"jpeg": "foxglove.CompressedImage", "32FC1": "foxglove.RawImage", "16UC1": "foxglove.RawImage",
                    LIDAR_CODEC: "awr.sensor.lidar_frame.v2"}
# lidar-v2：LiDAR 通道（`/uav/{id}/lidar/{name}`，`awr.sensor.lidar_frame.v2`），消息字节即交付字节，不经图像解封装；
# 帧索引契约（`bench/frame_index.v1`）的 codec 枚举写作 `lidar_v2`，通道元数据沿用写入器的 `lidar-v2`，在接口输出处换名
_CONTRACT_CODEC = {LIDAR_CODEC: "lidar_v2"}


def _codec_fields(info: dict) -> dict:
    """通道元数据 → 帧索引与区间容器头的 `encoding`、`codec`（契约名）；未登记的编解码抛 ValueError（指明通道，不以 KeyError 中断）。"""
    codec = info.get("codec", "jpeg")
    if codec not in _SCHEMA_OF_CODEC:
        raise ValueError(f"channel {info.get('channel')!r}: frame codec {codec!r} is not supported by the reader")
    return {"encoding": _SCHEMA_OF_CODEC[codec], "codec": _CONTRACT_CODEC.get(codec, codec)}


class FrameLimitError(ValueError):
    code = 698

    def __init__(self, detail: dict) -> None:
        super().__init__(f"698 FRAME_REQUEST_LIMIT {detail}")
        self.detail = detail


@dataclass
class Frame:
    seq: int
    t_ns: int
    codec: str
    payload: bytes
    width: int | None = None
    height: int | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    @property
    def content_type(self) -> str:
        return CONTENT_TYPES.get(self.codec, "application/octet-stream")


class FrameStore:
    """一个 Run 目录（`raw.mcap`、`frames.idx`）的只读视图；线程安全由 ChunkCache 保证。"""

    def __init__(self, run_dir: Path, run_id: str | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id or self.run_dir.name
        self.idx = FramesIdx.open(self.run_dir / "frames.idx")
        self.seg = SegmentIndex.build(self.run_dir / "raw.mcap", allow_open=False)
        self.cache = ChunkCache(self.seg)
        self.chunk_no = {c.start: i for i, c in enumerate(self.seg.chunks)}
        self.meta: dict[tuple[str, str], dict] = {}
        self._digest: dict[tuple[str, str, int], tuple[int, str]] = {}
        for ch in self.seg.channels.values():
            us = parse_obs_topic(ch.topic)
            if us is not None:
                self.meta[us] = {"channel": ch.topic, **ch.metadata}

    def close(self) -> None:
        self.seg.close()

    def channel_info(self, uav: str, sensor: str) -> dict | None:
        return self.meta.get((uav, sensor))

    def _frame(self, uav: str, sensor: str, r) -> Frame:
        info = self.meta[(uav, sensor)]
        codec = info.get("codec", "jpeg")
        m = self.cache.message(MsgRef(int(r["t_ns"]), self.chunk_no[int(r["chunk_offset"])], int(r["msg_offset"])))
        if codec == LIDAR_CODEC:                                     # awr.sensor.lidar_frame.v2：消息即载荷
            f = Frame(int(r["seq"]), int(r["t_ns"]), codec, bytes(m.data), None, None)
            self._digest[(uav, sensor, f.seq)] = (len(f.payload), f.sha256)
            return f
        d = decode_image(_SCHEMA_OF_CODEC[codec], m.data, info.get("compression"))
        f = Frame(int(r["seq"]), int(r["t_ns"]), d["codec"], d["payload"], d.get("width"), d.get("height"))
        self._digest[(uav, sensor, f.seq)] = (len(f.payload), f.sha256)
        return f

    def _digest_of(self, uav: str, sensor: str, r) -> tuple[int, str]:
        k = (uav, sensor, int(r["seq"]))
        if k not in self._digest:
            self._frame(uav, sensor, r)
        return self._digest[k]

    def frame(self, uav: str, sensor: str, seq: int) -> Frame | None:
        r = self.idx.find(uav, sensor, seq)
        return None if r is None else self._frame(uav, sensor, r)

    def latest_seq(self, uav: str, sensor: str) -> int | None:
        s = self.idx.span(uav, sensor, 0, 2 ** 32 - 1)
        return int(s["seq"][-1]) if len(s) else None

    def index_page(self, uav: str, sensor: str, seq_from: int = 0, seq_to: int | None = None) -> dict | None:
        """BN-49：`bench/frame_index.v1`，每页 ≤ 1000 条，`next_seq` 为下一页起点。"""
        info = self.channel_info(uav, sensor)
        if info is None:
            return None
        rs = self.idx.span(uav, sensor, seq_from, 2 ** 32 - 1 if seq_to is None else seq_to)
        page, more = rs[:MAX_INDEX_ITEMS], rs[MAX_INDEX_ITEMS:MAX_INDEX_ITEMS + 1]
        items = [{"seq": int(r["seq"]), "t_ns": int(r["t_ns"]), "bytes": n, "sha256": h}
                 for r in page for n, h in [self._digest_of(uav, sensor, r)]]
        return {"schema": "drone4d.bench.frame_index.v1", "run_id": self.run_id, "uav_id": uav, "sensor_id": sensor, "channel": info["channel"],
                **_codec_fields(info), "agent_delivered": info.get("agent_delivered", "true") == "true",
                "period_ns": int(info.get("period_ns", 100_000_000)), "items": items,
                "next_seq": int(more["seq"][0]) if len(more) else None}

    def range_container(self, uav: str, sensor: str, seq_from: int, seq_to: int, stride: int = 1) -> bytes | None:
        """BN-51：`d3frames.v1` 容器；缺失序号不占记录；超出 300 × stride 帧或 64 MiB 时 698。"""
        info = self.channel_info(uav, sensor)
        if info is None:
            return None
        stride = max(int(stride), 1)
        if seq_to < seq_from or seq_to - seq_from + 1 > MAX_FRAMES_PER_STRIDE * stride:
            raise FrameLimitError({"max_frames": MAX_FRAMES_PER_STRIDE, "max_bytes": MAX_RANGE_BYTES})
        rs = [r for r in self.idx.span(uav, sensor, seq_from, seq_to) if (int(r["seq"]) - seq_from) % stride == 0]
        head = {"run_id": self.run_id, "uav_id": uav, "sensor_id": sensor, **_codec_fields(info), "n": len(rs),
                "seq_from": int(seq_from), "seq_to": int(seq_to), "stride": stride}
        hj = json.dumps(head, separators=(",", ":")).encode()
        parts = [b"D3FR", struct.pack("<HHI", 1, 0, len(hj)), hj]
        total = sum(len(p) for p in parts) + 32
        for r in rs:
            f = self._frame(uav, sensor, r)
            total += 48 + len(f.payload)
            if total > MAX_RANGE_BYTES:
                raise FrameLimitError({"max_frames": MAX_FRAMES_PER_STRIDE, "max_bytes": MAX_RANGE_BYTES})
            parts += [struct.pack("<IqI", f.seq, f.t_ns, len(f.payload)), hashlib.sha256(f.payload).digest(), f.payload]
        body = b"".join(parts)
        return body + hashlib.sha256(body).digest()


class TruthStore:
    """真值文件 `<event>/truth/run_<id>.mcap` 与其索引 `run_<id>.frames.idx` 的只读视图（BN-52）：按 (uav, cam, seq) 返回
    `camera_gt.v1` 消息字节。"""

    def __init__(self, mcap_path: Path, idx_path: Path) -> None:
        self.idx = FramesIdx.open(idx_path)
        self.seg = SegmentIndex.build(mcap_path, allow_open=False)
        self.cache = ChunkCache(self.seg)
        self.chunk_no = {c.start: i for i, c in enumerate(self.seg.chunks)}

    def close(self) -> None:
        self.seg.close()

    def get(self, uav: str, cam: str, seq: int) -> tuple[int, bytes] | None:
        r = self.idx.find(uav, cam, seq)
        if r is None:
            return None
        m = self.cache.message(MsgRef(int(r["t_ns"]), self.chunk_no[int(r["chunk_offset"])], int(r["msg_offset"])))
        return int(r["t_ns"]), bytes(m.data)


def topic_for(uav: str, sensor: str) -> str:
    cam, _, kind = sensor.partition(".")
    return camera_topic(uav, cam, "depth" if kind == "depth" else "image")


# ---------------------------------------------------------------------- 内部回放磁带演员源（M27-FR-053）
REPLAY_TOL_M = 0.05                                  # D3-AC-19：内部回放演员与磁带、前端解码器与内部回放的位置差上限
_ST_MOVING, _ST_ENABLED = 1 << 4, 1 << 7
_TAPE_VISIBLE = (1, 2)


def run_attachments(mcap_path: Path, names: tuple[str, ...] | None = None) -> dict[str, bytes]:
    """`raw.mcap` 的附件 {name: data}（经 summary 的附件索引读取；`names` 给出时只取所列名称）。"""
    from mcap.reader import make_reader

    with Path(mcap_path).open("rb") as f:
        return {a.name: bytes(a.data) for a in make_reader(f).iter_attachments() if names is None or a.name in names}


def _reader_from_bytes(data: bytes):
    """磁带字节 → `TapeReader`（读取器构造时整文件读入内存，临时文件随即删除）。"""
    import tempfile

    from awr.sim.events.tape import TapeReader

    with tempfile.TemporaryDirectory(prefix="awr-tape-") as d:
        p = Path(d) / "tape.bin"
        p.write_bytes(data)
        return TapeReader(p)


def _class_code(cls: str) -> int:
    from awr.contracts.enums import TargetClass

    try:
        return int(TargetClass[str(cls).upper()])
    except KeyError:
        return int(TargetClass.UNKNOWN)


def tape_target_rows(reader, t_ns: int, *, ids=None, offset_ns: int = 0) -> np.ndarray:
    """M23-FR-049：磁带在事件时刻 `offset_ns + t_ns` 的演员 → `awr.TargetLite32.v1` 行（缺省为全部脚本演员，idx 升序）。"""
    from awr.contracts.layouts import TARGET_LITE32

    sel = np.arange(reader.n_scripted, dtype=np.int64) if ids is None else np.asarray(ids, np.int64)
    st = reader.actor_states(int(offset_ns) + int(t_ns), ids=sel)
    rows = np.zeros(len(sel), TARGET_LITE32)
    rows["agent_no"] = sel.astype(np.uint16)
    rows["class"] = [_class_code(reader.actors[int(i)]["class"]) for i in sel]
    rows["state"] = np.where(np.isin(st.state, _TAPE_VISIBLE), _ST_ENABLED, 0) | _ST_MOVING
    rows["pos"] = st.pos_enu_m.astype(np.float32)
    rows["heading_rad"] = st.yaw_rad.astype(np.float32)
    sp = np.hypot(st.vel_enu_mps[:, 0], st.vel_enu_mps[:, 1])
    rows["speed_cms"] = np.clip(np.round(sp * 100.0), -32767, 32767).astype(np.int16)
    return rows


class TapeActorSource:
    """内部回放的磁带演员源：包装 M23 `TapeReader`，按回放播放头 `t_sim_ns` 产出脚本演员的 TargetLite32 行（M12 扩展点 11）。

    `from_run(run_dir)` 从 `raw.mcap` 附件取磁带并按 `tape.json` 的 `content_sha256` 校验（缺附件 620，哈希不符 614）；
    `expect_content_sha256` 另给出时（如运行清单中的磁带哈希）一并核对。"""

    def __init__(self, reader, *, offset_ns: int = 0, run_id: str | None = None) -> None:
        from awr.sim.events.runtime import target_id

        self.reader = reader
        self.offset_ns = int(offset_ns)
        self.run_id = run_id
        self.ids = np.arange(reader.n_scripted, dtype=np.int64)
        taken: set[str] = set()
        self.tids = [target_id(a["id"], taken) for a in reader.actors[:reader.n_scripted]]
        self.content_sha256: str | None = None

    @classmethod
    def from_run(cls, run_dir: Path, *, offset_ns: int = 0, verify: bool = True, expect_content_sha256: str | None = None) -> TapeActorSource:
        from awr.sim.events.errors import EventError

        run_dir = Path(run_dir)
        att = run_attachments(run_dir / "raw.mcap", ("tape.bin", "tape.json"))
        if "tape.bin" not in att:
            raise EventError(620, "raw.mcap carries no tape.bin attachment", {"run": run_dir.name})
        r = _reader_from_bytes(att["tape.bin"])
        src = cls(r, offset_ns=offset_ns, run_id=run_dir.name)
        if verify:
            man = json.loads(att["tape.json"]) if "tape.json" in att else {}
            want = [h for h in (man.get("content_sha256"), expect_content_sha256) if h]
            h = r.verify(want[0] if want else None)
            if any(w != h["content_sha256"] for w in want):
                raise EventError(614, "content_sha256 mismatch", {"expected": want, "actual": h["content_sha256"]})
            src.content_sha256 = h["content_sha256"]
        return src

    @property
    def duration_ns(self) -> int:
        return int(self.reader.n_ticks) * 100_000_000

    def roster(self) -> list[dict]:
        """回放 roster 的识别物条目（`kind = target`）：{agent_no, id, label, class, class_code, size_m}。"""
        out = []
        for i, a in enumerate(self.reader.actors[:self.reader.n_scripted]):
            out.append({"agent_no": i, "id": self.tids[i], "label": a["id"], "kind": "target", "class": a["class"],
                        "class_code": _class_code(a["class"]), "size_m": [s / 1000.0 for s in a["size_mm"]]})
        return out

    def rows(self, t_sim_ns: int) -> np.ndarray:
        return tape_target_rows(self.reader, int(t_sim_ns), ids=self.ids, offset_ns=self.offset_ns)

    def payload(self, t_sim_ns: int) -> dict:
        """与运行期 `state/sim-core/targets` 同形的载荷 `{v: 1, t_sim_ns, rows: bin(n·32)}`。"""
        return {"v": 1, "t_sim_ns": int(t_sim_ns), "rows": self.rows(t_sim_ns).tobytes()}

    def positions(self, t_sim_ns: int) -> np.ndarray:
        """回放行中的位置（n, 3）float64（即前端识别物图层显示的位置）。"""
        return self.rows(t_sim_ns)["pos"].astype(np.float64)
