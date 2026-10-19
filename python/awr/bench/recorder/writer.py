"""Run 写入器（M27 §6.7.4、§7.5）：每个 Run 一个无损 `raw.mcap`，zstd chunk；相机帧写入 `/uav/{id}/camera/{name}/{image,depth}`，
`log_time = publish_time = t_ns`（帧时间戳：中间行曝光中点），MCAP `sequence` 为帧序号 seq。真值文件 `truth/run_<id>.mcap` 用同一类
写入 `/eval/hidden/**`（相机真值为 `awr.sensor.camera_gt.v1`，无 schema 的二进制消息，见 `truth.py`）。

两种模式：

- 同步（缺省，`queue_msgs = 0`）：调用方线程直接写文件（离线采集 `sensorsvc.capture`）；
- 异步（`queue_msgs > 0`，Env 运行，M27-FR-040、D3-AC-19）：调用方只把消息放入有界队列，独立写线程按入队顺序编码并写入。队列满
  （在途消息数达到 `queue_msgs` 或在途字节数将超过 `queue_bytes`）时**阻塞生产者**直至写线程腾出空间，不丢弃、不合并、不降频；
  每次阻塞计入 `stats`（`blocked`、`block_s`、`block_max_s`，前 `BLOCK_LOG_MAX` 次阻塞的 `[t_ns, wait_ms]`）。在途包括正在写的那条，
  单条超过 `queue_bytes` 的消息在队列为空时放行。

背压注入（测试与 dmax 复测用）：`inject_delay_s` 为写线程每条消息额外等待，`rate_Bps` 为写入速率上限（按消息字节累计限速，
模拟磁盘 20 MB/s 等慢下游，M27-AC-006）。两者只延长墙钟，消息集合与字节不变。

写线程异常保存后在下一次写入或 `close()` 时于调用方线程抛出；`close()` 先排空队列再写 MCAP summary。`channel_counts(path)` 从已
关闭文件的 summary 统计读回各通道消息数，用于与写入计数对账（丢帧数）。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import numpy as np
from mcap.writer import CompressionType, IndexType, Writer

from awr.bench.recorder.channels import camera_topic, imu_topic, lidar_topic, truth_camera_topic, truth_lidar_topic
from awr.bench.recorder.depthcodec import DEPTH_COMPRESSION
from awr.bench.recorder.truth import pack_camera_gt

__all__ = ["BLOCK_LOG_MAX", "CHUNK_BYTES", "RunWriter", "WriterError", "channel_counts", "gnss_topic"]

CHUNK_BYTES = 4 << 20
BLOCK_LOG_MAX = 1000


class WriterError(RuntimeError):
    """写线程失败（磁盘满、编码错误等）；原异常为 `__cause__`。"""


def gnss_topic(uav: str, name: str) -> str:
    return f"/uav/{uav}/gnss/{name}"


def _frozen(a: np.ndarray, dtype=None) -> np.ndarray:
    """异步写入时保存数组快照：只读视图（如 `np.frombuffer(bytes)`）原样使用，可写数组复制一份，调用方随后修改不影响存档。"""
    a = np.asarray(a, dtype)
    return a.copy() if a.flags.writeable else a


class RunWriter:
    def __init__(self, path: Path, *, profile: str = "drone4d-d3", depth_compression: str | None = DEPTH_COMPRESSION,
                 queue_msgs: int = 0, queue_bytes: int = 256 << 20, inject_delay_s: float = 0.0, rate_Bps: float | None = None) -> None:
        self.path = Path(path)
        self.depth_compression = depth_compression           # None 只用于兼容测试（D3 首版写出的未压缩深度）
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("wb")
        self._w = Writer(self._f, chunk_size=CHUNK_BYTES, compression=CompressionType.ZSTD, index_types=IndexType.ALL)
        self._w.start(profile=profile, library="awr.bench.recorder")
        self._schemas: dict[str, int] = {}
        self._channels: dict[str, int] = {}
        self.n_messages = 0
        self.topic_counts: dict[str, int] = {}
        self.closed = False
        self.queue_msgs, self.queue_bytes = int(queue_msgs), int(queue_bytes)
        self.inject_delay_s, self.rate_Bps = float(inject_delay_s), (float(rate_Bps) if rate_Bps else None)
        self.stats = {"mode": "async" if self.queue_msgs > 0 else "sync", "queue_msgs": self.queue_msgs, "queue_bytes": self.queue_bytes,
                      "inject_delay_ms": round(self.inject_delay_s * 1000, 3), "rate_MBps": round(self.rate_Bps / 1e6, 3) if self.rate_Bps else None,
                      "enqueued": 0, "written": 0, "bytes": 0, "blocked": 0, "block_s": 0.0, "block_max_s": 0.0, "max_depth": 0,
                      "max_depth_bytes": 0, "write_s": 0.0, "blocks": []}
        self._t0 = time.perf_counter()
        self._t_rate: float | None = None
        self._err: BaseException | None = None
        self._q: deque = deque()
        self._n_pending = 0
        self._b_pending = 0
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        if self.queue_msgs > 0:
            self._thread = threading.Thread(target=self._loop, name=f"runwriter:{self.path.name}", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ 队列与写线程
    def _full(self, n: int) -> bool:
        return self._n_pending > 0 and (self._n_pending >= self.queue_msgs or self._b_pending + n > self.queue_bytes)

    def _submit(self, job: Callable[[], int], nbytes: int, t_ns: int = 0) -> None:
        if self.closed:
            raise ValueError(f"{self.path}: writer is closed")
        self._raise()
        if self._thread is None:
            self.stats["enqueued"] += 1
            self._exec(job)
            return
        st = self.stats
        with self._cv:
            if self._full(nbytes):
                t_wait = time.perf_counter()
                while self._full(nbytes) and self._err is None:
                    self._cv.wait()
                dt = time.perf_counter() - t_wait
                st["blocked"] += 1
                st["block_s"] += dt
                st["block_max_s"] = max(st["block_max_s"], dt)
                if len(st["blocks"]) < BLOCK_LOG_MAX:
                    st["blocks"].append([int(t_ns), round(dt * 1000, 3)])
            if self._err is not None:
                self._raise()
            self._q.append((job, nbytes))
            self._n_pending += 1
            self._b_pending += nbytes
            st["enqueued"] += 1
            st["max_depth"] = max(st["max_depth"], self._n_pending)
            st["max_depth_bytes"] = max(st["max_depth_bytes"], self._b_pending)
            self._cv.notify_all()

    def _exec(self, job: Callable[[], int]) -> None:
        t = time.perf_counter()
        n = job()
        st = self.stats
        st["written"] += 1
        st["bytes"] += n
        if self.inject_delay_s > 0:
            time.sleep(self.inject_delay_s)
        if self.rate_Bps:
            if self._t_rate is None:
                self._t_rate = t
            lag = self._t_rate + st["bytes"] / self.rate_Bps - time.perf_counter()
            if lag > 0:
                time.sleep(lag)
        st["write_s"] += time.perf_counter() - t

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._q:
                    self._cv.wait()
                job, n = self._q.popleft()
            if job is None:
                return
            try:
                self._exec(job)
            except BaseException as e:                       # 保存后由调用方线程抛出；队列中其余消息不再写入
                with self._cv:
                    self._err = e
                    self._q.clear()
                    self._n_pending = self._b_pending = 0
                    self._cv.notify_all()
                return
            with self._cv:
                self._n_pending -= 1
                self._b_pending -= n
                self._cv.notify_all()

    def _raise(self) -> None:
        if self._err is not None:
            raise WriterError(f"{self.path}: writer thread failed: {self._err!r}") from self._err

    def _add(self, cid: int, topic: str, t_ns: int, seq: int, data: bytes) -> int:
        self._w.add_message(channel_id=cid, log_time=int(t_ns), publish_time=int(t_ns), sequence=int(seq), data=data)
        self.n_messages += 1
        self.topic_counts[topic] = self.topic_counts.get(topic, 0) + 1
        return len(data)

    # ------------------------------------------------------------------ 通道
    def _channel(self, topic: str, schema: str, meta: dict[str, str], *, encoding: str = "protobuf") -> int:
        cid = self._channels.get(topic)
        if cid is None:
            if encoding == "protobuf":
                sid = self._schemas.get(schema)
                if sid is None:
                    from awr.bench.recorder.foxglove import schema_data

                    sid = self._schemas[schema] = self._w.register_schema(name=schema, encoding="protobuf", data=schema_data(schema))
            else:
                sid = 0                                        # 无 schema 的二进制消息；schema 名写入通道元数据
                meta = {**meta, "schema": schema}
            cid = self._channels[topic] = self._w.register_channel(topic=topic, message_encoding=encoding, schema_id=sid, metadata=meta)
        return cid

    # ------------------------------------------------------------------ 消息
    def write_camera_truth(self, uav: str, cam: str, seq: int, t_ns: int, rec: dict, blocks: dict[str, bytes]) -> None:
        data = pack_camera_gt(rec, blocks)
        topic = truth_camera_topic(uav, cam)

        def job() -> int:
            cid = self._channel(topic, "awr.sensor.camera_gt.v1", {"codec": "d3gt"}, encoding="binary")
            return self._add(cid, topic, t_ns, seq, data)

        self._submit(job, len(data), t_ns)

    def write_camera(self, uav: str, cam: str, seq: int, t_ns: int, jpeg: bytes, *, depth_m: np.ndarray | None = None,
                     period_ns: int | None = None, agent_delivered: bool = True) -> None:
        """`jpeg` 为 Agent 收到的字节原样（`foxglove.CompressedImage.data`）；`depth_m` 为 f32 观测深度，存档为 bshuf4-zstd 无损形式。"""
        meta = {"agent_delivered": "true" if agent_delivered else "false"}
        if period_ns:
            meta["period_ns"] = str(int(period_ns))
        fid = f"{uav}/{cam}"
        jpeg = bytes(jpeg)
        it, dt = camera_topic(uav, cam, "image"), camera_topic(uav, cam, "depth")

        def job_image() -> int:
            from awr.bench.recorder.foxglove import compressed_image

            cid = self._channel(it, "foxglove.CompressedImage", {**meta, "codec": "jpeg"})
            return self._add(cid, it, t_ns, seq, compressed_image(t_ns, fid, jpeg))

        self._submit(job_image, len(jpeg), t_ns)
        if depth_m is not None:
            depth = _frozen(depth_m, "<f4") if self._thread is not None else depth_m
            dm = {**meta, "codec": "32FC1"} | ({"compression": self.depth_compression} if self.depth_compression else {})

            def job_depth() -> int:
                from awr.bench.recorder.foxglove import raw_image_f32

                cid = self._channel(dt, "foxglove.RawImage", dm)
                return self._add(cid, dt, t_ns, seq, raw_image_f32(t_ns, fid, depth, compression=self.depth_compression))

            self._submit(job_depth, int(np.asarray(depth).nbytes), t_ns)

    def write_thermal(self, uav: str, name: str, obs: dict, *, period_ns: int | None = None, agent_delivered: bool = True) -> None:
        """ThermalObs → `/uav/{id}/thermal/{name}`（`foxglove.RawImage` 16UC1，data 为 `temp_ck` 交付字节原样；见 `channels`），
        log_time = `t_mid_ns`，sequence = seq。调用方线程打包：校验载荷长度并取得字节快照。"""
        from awr.bench.recorder.channels import thermal_message, thermal_topic

        schema, meta, data = thermal_message(uav, obs)
        meta = {"agent_delivered": "true" if agent_delivered else "false", **meta}
        if period_ns:
            meta["period_ns"] = str(int(period_ns))
        topic, seq, t_ns = thermal_topic(uav, name), int(obs["seq"]), int(obs["t_mid_ns"])

        def job() -> int:
            cid = self._channel(topic, schema, meta)
            return self._add(cid, topic, t_ns, seq, data)

        self._submit(job, len(data), t_ns)

    def write_lidar(self, uav: str, name: str, seq: int, t_ns: int, header: dict, points: np.ndarray, *, period_ns: int | None = None,
                    agent_delivered: bool = True) -> None:
        """`awr.sensor.lidar_frame.v2`（log_time = 帧首 timebase_ns）。"""
        from awr.bench.recorder.lidar import LIDAR_CODEC, pack_lidar_frame

        meta = {"agent_delivered": "true" if agent_delivered else "false", "codec": LIDAR_CODEC}
        if period_ns:
            meta["period_ns"] = str(int(period_ns))
        data = pack_lidar_frame(header, points)                # 调用方线程打包：校验点数并取得字节快照
        topic = lidar_topic(uav, name)

        def job() -> int:
            cid = self._channel(topic, "awr.sensor.lidar_frame.v2", meta, encoding="binary")
            return self._add(cid, topic, t_ns, seq, data)

        self._submit(job, len(data), t_ns)

    def write_lidar_truth(self, uav: str, name: str, seq: int, t_ns: int, header: dict, inst: np.ndarray, flags: np.ndarray) -> None:
        from awr.bench.recorder.lidar import pack_lidar_gt

        data = pack_lidar_gt(header, inst, flags)
        topic = truth_lidar_topic(uav, name)

        def job() -> int:
            cid = self._channel(topic, "awr.sensor.lidar_gt.v1", {"codec": "lidar-gt"}, encoding="binary")
            return self._add(cid, topic, t_ns, seq, data)

        self._submit(job, len(data), t_ns)

    def write_msgpack(self, topic: str, schema: str, seq: int, t_ns: int, obj, *, meta: dict[str, str] | None = None) -> None:
        """msgpack 消息（bin 字段原样）；`schema` 写入通道元数据。"""
        import msgpack

        data = msgpack.packb(obj, use_bin_type=True)
        m = {"codec": "msgpack", **(meta or {})}

        def job() -> int:
            cid = self._channel(topic, schema, m, encoding="msgpack")
            return self._add(cid, topic, t_ns, seq, data)

        self._submit(job, len(data), t_ns)

    def write_json(self, topic: str, schema: str, seq: int, t_ns: int, data: bytes, *, meta: dict[str, str] | None = None) -> None:
        """已序列化的 JSON 字节（调用方负责规范化）。"""
        data = bytes(data)
        m = {"codec": "json", **(meta or {})}

        def job() -> int:
            cid = self._channel(topic, schema, m, encoding="json")
            return self._add(cid, topic, t_ns, seq, data)

        self._submit(job, len(data), t_ns)

    def write_imu(self, uav: str, name: str, seq: int, t_ns: int, obs: dict) -> None:
        """ImuObs 块（bench/obs.v1，msgpack；`acc_mps2`、`gyro_rps` 为 f32le bin），log_time = 块首样本时刻。"""
        self.write_msgpack(imu_topic(uav, name), "awr.bench.ImuObs", seq, t_ns, obs)

    def write_gnss(self, uav: str, name: str, seq: int, t_ns: int, obs: dict) -> None:
        """GnssObs（bench/obs.v1，msgpack），log_time = 观测时刻。"""
        self.write_msgpack(gnss_topic(uav, name), "awr.bench.GnssObs", seq, t_ns, obs)

    def metadata(self, name: str, data: dict[str, str]) -> None:
        d = {k: str(v) for k, v in data.items()}

        def job() -> int:
            self._w.add_metadata(name=name, data=d)
            return 0

        self._submit(job, 0)

    def attachment(self, name: str, data: bytes, media_type: str = "application/json", t_ns: int = 0) -> None:
        data = bytes(data)

        def job() -> int:
            self._w.add_attachment(create_time=int(t_ns), log_time=int(t_ns), name=name, media_type=media_type, data=data)
            return len(data)

        self._submit(job, len(data), t_ns)

    # ------------------------------------------------------------------ 关闭
    def summary(self) -> dict:
        """写入统计（JSON 可序列化）：入队与写出条数、阻塞次数与时长、队列峰值、`dropped = enqueued − written`（设计上恒为 0）。"""
        st = dict(self.stats)
        wall = time.perf_counter() - self._t0
        st.update(block_s=round(st["block_s"], 6), block_max_s=round(st["block_max_s"], 6), write_s=round(st["write_s"], 6),
                  wall_s=round(wall, 6), block_frac=round(st["block_s"] / wall, 6) if wall > 0 else 0.0,
                  dropped=st["enqueued"] - st["written"], messages=self.n_messages, blocks=list(st["blocks"]))
        return st

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self._thread is not None:
                with self._cv:
                    self._q.append((None, 0))
                    self._cv.notify_all()
                self._thread.join()
                self._raise()
            self._w.finish()
        finally:
            self._f.close()

    def __enter__(self) -> RunWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def channel_counts(path: Path) -> dict[str, int]:
    """已关闭 MCAP 的 summary 统计 → {topic: 消息数}（与写入计数对账）。"""
    from mcap.reader import make_reader

    with Path(path).open("rb") as f:
        s = make_reader(f).get_summary()
    if s is None or s.statistics is None:
        raise ValueError(f"{path}: MCAP has no summary statistics")
    out: dict[str, int] = {}
    for cid, n in s.statistics.channel_message_counts.items():
        topic = s.channels[cid].topic
        out[topic] = out.get(topic, 0) + int(n)
    return out
