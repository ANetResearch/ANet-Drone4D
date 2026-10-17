"""Run 写入器（M27 §6.7.4、§7.5）：每个 Run 一个无损 `raw.mcap`，zstd chunk；相机帧写入 `/uav/{id}/camera/{name}/{image,depth}`，
`log_time = publish_time = t_ns`（帧时间戳：中间行曝光中点），MCAP `sequence` 为帧序号 seq。

本版为同步写入（相机帧由 sensorsvc 会话逐帧交付）。真值文件 `truth/run_<id>.mcap` 用同一类写入 `/eval/hidden/camera/{uav}/{name}`
（`awr.sensor.camera_gt.v1`，无 schema 的二进制消息，见 `truth.py`）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from mcap.writer import CompressionType, IndexType, Writer

from awr.bench.recorder.channels import camera_topic, truth_camera_topic
from awr.bench.recorder.foxglove import compressed_image, raw_image_f32, schema_data
from awr.bench.recorder.truth import pack_camera_gt

__all__ = ["RunWriter"]

CHUNK_BYTES = 4 << 20


class RunWriter:
    def __init__(self, path: Path, *, profile: str = "drone4d-d3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("wb")
        self._w = Writer(self._f, chunk_size=CHUNK_BYTES, compression=CompressionType.ZSTD, index_types=IndexType.ALL)
        self._w.start(profile=profile, library="awr.bench.recorder")
        self._schemas: dict[str, int] = {}
        self._channels: dict[str, int] = {}
        self.n_messages = 0
        self.closed = False

    def _channel(self, topic: str, schema: str, meta: dict[str, str], *, encoding: str = "protobuf") -> int:
        cid = self._channels.get(topic)
        if cid is None:
            if encoding == "protobuf":
                sid = self._schemas.get(schema)
                if sid is None:
                    sid = self._schemas[schema] = self._w.register_schema(name=schema, encoding="protobuf", data=schema_data(schema))
            else:
                sid = 0                                        # 无 schema 的二进制消息；schema 名写入通道元数据
                meta = {**meta, "schema": schema}
            cid = self._channels[topic] = self._w.register_channel(topic=topic, message_encoding=encoding, schema_id=sid, metadata=meta)
        return cid

    def write_camera_truth(self, uav: str, cam: str, seq: int, t_ns: int, rec: dict, blocks: dict[str, bytes]) -> None:
        cid = self._channel(truth_camera_topic(uav, cam), "awr.sensor.camera_gt.v1", {"codec": "d3gt"}, encoding="binary")
        self._w.add_message(channel_id=cid, log_time=int(t_ns), publish_time=int(t_ns), sequence=int(seq), data=pack_camera_gt(rec, blocks))
        self.n_messages += 1

    def write_camera(self, uav: str, cam: str, seq: int, t_ns: int, jpeg: bytes, *, depth_m: np.ndarray | None = None,
                     period_ns: int | None = None, agent_delivered: bool = True) -> None:
        meta = {"agent_delivered": "true" if agent_delivered else "false"}
        if period_ns:
            meta["period_ns"] = str(int(period_ns))
        fid = f"{uav}/{cam}"
        cid = self._channel(camera_topic(uav, cam, "image"), "foxglove.CompressedImage", {**meta, "codec": "jpeg"})
        self._w.add_message(channel_id=cid, log_time=int(t_ns), publish_time=int(t_ns), sequence=int(seq),
                            data=compressed_image(t_ns, fid, jpeg))
        self.n_messages += 1
        if depth_m is not None:
            cid = self._channel(camera_topic(uav, cam, "depth"), "foxglove.RawImage", {**meta, "codec": "32FC1"})
            self._w.add_message(channel_id=cid, log_time=int(t_ns), publish_time=int(t_ns), sequence=int(seq),
                                data=raw_image_f32(t_ns, fid, depth_m))
            self.n_messages += 1

    def metadata(self, name: str, data: dict[str, str]) -> None:
        self._w.add_metadata(name=name, data={k: str(v) for k, v in data.items()})

    def attachment(self, name: str, data: bytes, media_type: str = "application/json", t_ns: int = 0) -> None:
        self._w.add_attachment(create_time=int(t_ns), log_time=int(t_ns), name=name, media_type=media_type, data=data)

    def close(self) -> None:
        if not self.closed:
            self._w.finish()
            self._f.close()
            self.closed = True

    def __enter__(self) -> RunWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
