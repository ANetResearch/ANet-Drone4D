"""Env 运行写入器（M27 `EnvRecorderSink` 的 P0a 版；M27 §6.7.4、§7.5；D3-AC-19）：运行目录内的观测层（`raw.mcap`）、Agent 层与真值层。

    <run_dir>/
      raw.mcap                      观测与运行层的唯一权威存储（zstd chunk，见下）；帧数据只在此保存一份
      frames.idx                    raw.mcap 中相机与 LiDAR 帧的偏移索引（bench/frame_index.v1 `x-layout`；只含偏移不含帧数据）
      manifest.json                 运行清单（规格摘要、物理档与样本流摘要、统计、`recorder` 写入统计；不含 run_seed）
      task.json                     任务契约 bench/task.v1
      agents/<uav>/config.json      交付的 agent_config.v1
      agents/<uav>/acts.jsonl       Agent 原样动作（二进制载荷以 base64 记 {"$b64": ...}）与回执
      agents/<uav>/obs.sha256       每次交付观测的规范 JSON 摘要（k、sha256）
      reports.jsonl                 守卫通过的 report（uav、k_obs、apply_tick、t_apply_ns、report）
      net/records.jsonl             链路逐条记录
      truth/run_<run_id>.mcap       真值层 MCAP（`/uav/{id}/state`、`/eval/hidden/**`；不进入 raw.mcap）
      truth/fleet.npz               每 tick 末真值位姿与导航位置（t_ns、pos、vel、yaw、nav、status）
      truth/l1.jsonl                L1 检测的真值映射（uav、seq、t_ns、det_id、actor、h_px、h_unocc_px、vis、ident、delivered）
      truth/l2.jsonl                L2 相机帧的可见度归约（uav、sensor、seq、t_ns、actor、h_unocc_px、vis、bbox_xywh）
      truth/contacts.jsonl          接触事件
      truth/run_seed.json           运行种子（真值层；评测与审计用，不对 Agent 可见）

`raw.mcap`（M27 §7.5.1、§7.5.2）：写入发生在观测交付时（`on_obs`），存档的正是 Agent 收到的字节：

- `/uav/{id}/camera/{name}/image`（`foxglove.CompressedImage`，JPEG 原样）、`/depth`（L2-depth，`foxglove.RawImage` 32FC1，bshuf4-zstd
  无损）、`/uav/{id}/thermal/{name}`（`foxglove.RawImage` 16UC1，ThermalObs `temp_ck` 的 u16 + zstd 字节原样）、`/uav/{id}/lidar/{name}`（`awr.sensor.lidar_frame.v2`，由 LidarObs 的 xyz、offset_time、reflectivity、tag 无损重排）、
  `/uav/{id}/imu/{name}`、`/uav/{id}/gnss/{name}`（msgpack，交付对象原样）；
- `/uav/{id}/obs/nav`（msgpack）、`/uav/{id}/obs/detections`（L1，json）、`/uav/{id}/action`、`/uav/{id}/report`、`/net/records`（json）；
- Metadata `run`（run_id、config_sha256、spec_sha256、run_hash、排名运行的 seed_commit）、`run.wall`、`channels.units`；
- Attachment `tape.bin`、`tape.json`（M23 磁带原始字节）、`world_manifest.json`（public 形式）、`run_spec.json`（排名运行不含种子）。

背压（M27-FR-040；ADR-162）：两个 MCAP 均由 `RunWriter` 的写线程按入队顺序写出；队列满时 `on_obs` 等调用阻塞 Env 主循环（仿真时钟
随之停住），不丢弃、不降频，阻塞次数与时长计入 `manifest.json` 的 `recorder` 与 `writer_block_s`。运行结束时以 MCAP summary 统计对账
各通道消息数（`dropped` 须为 0，否则抛 `WriterError`，按平台失败处置），再由 `raw.mcap` 生成 `frames.idx`。

写入参数取自 `RecorderOptions`（缺省由环境变量 `AWR_REC_MCAP`、`AWR_REC_QUEUE_MSGS`、`AWR_REC_QUEUE_MB`、`AWR_REC_INJECT_DELAY_MS`、
`AWR_REC_RATE_MBPS` 给出；后两者为背压注入，只延长墙钟，注入值写入清单）。`recorder` 统计在写 `manifest.json` 时并入清单
（`write_json("manifest.json", ...)` 先完成 `finish()`）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

__all__ = ["RAW_TOPIC_PATTERNS", "TRUTH_TOPIC_PATTERNS", "EnvRecorder", "RecorderOptions", "canonical_json"]

TICK_NS = 100_000_000
FRAME_PERIOD_NS = 100_000_000                                 # L2 相机与 LiDAR 均为 10 Hz（env.l2）
# 通道白名单（M27-AC-008）：raw.mcap 只含观测与运行层，真值通道只在 truth/run_<id>.mcap
RAW_TOPIC_PATTERNS = (r"^/uav/[^/]+/camera/[A-Za-z0-9_]+/(image|depth)$", r"^/uav/[^/]+/thermal/[A-Za-z0-9_]+$", r"^/uav/[^/]+/lidar/[A-Za-z0-9_]+$",
                      r"^/uav/[^/]+/imu/[A-Za-z0-9_]+$",
                      r"^/uav/[^/]+/gnss/[A-Za-z0-9_]+$", r"^/uav/[^/]+/obs/[A-Za-z0-9_]+$", r"^/uav/[^/]+/(action|report)$", r"^/net/records$")
TRUTH_TOPIC_PATTERNS = (r"^/uav/[^/]+/state$", r"^/eval/hidden/.+$")
_SEED_KEYS = frozenset({"seed", "seeds", "seed_count"})


def _enc(x):
    if isinstance(x, bytes | bytearray):
        return {"$b64": base64.b64encode(bytes(x)).decode()}
    if isinstance(x, dict):
        return {k: _enc(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_enc(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    return x


def canonical_json(x) -> bytes:
    return json.dumps(_enc(x), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _plain(x):
    """msgpack 可编码的形式：numpy 标量与数组转 Python 值，bytes 原样。"""
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_plain(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, np.ndarray):
        return x.tolist()
    return x


@dataclass(frozen=True)
class RecorderOptions:
    mcap: bool = True                       # 写 raw.mcap 与 truth/run_<id>.mcap
    queue_msgs: int = 256                   # 写线程在途消息上限（含正在写的一条）
    queue_mb: float = 256.0                 # 在途字节上限（MiB）
    inject_delay_ms: float = 0.0            # 背压注入：每条消息额外等待
    rate_MBps: float | None = None          # 背压注入：写入速率上限（MB/s，10^6 B）

    @classmethod
    def from_env(cls, environ=None) -> RecorderOptions:
        e = os.environ if environ is None else environ
        rate = e.get("AWR_REC_RATE_MBPS")
        return cls(mcap=e.get("AWR_REC_MCAP", "1") not in ("0", "false", "no"), queue_msgs=int(e.get("AWR_REC_QUEUE_MSGS", 256)),
                   queue_mb=float(e.get("AWR_REC_QUEUE_MB", 256)), inject_delay_ms=float(e.get("AWR_REC_INJECT_DELAY_MS", 0)),
                   rate_MBps=float(rate) if rate else None)

    def writer_kw(self) -> dict:
        return {"queue_msgs": max(int(self.queue_msgs), 1), "queue_bytes": int(self.queue_mb * (1 << 20)),
                "inject_delay_s": float(self.inject_delay_ms) / 1000.0, "rate_Bps": float(self.rate_MBps) * 1e6 if self.rate_MBps else None}


class EnvRecorder:
    def __init__(self, run_dir: Path | None, *, spec=None, tape=None, options: RecorderOptions | None = None) -> None:
        """`spec`（EnvSpec）与 `tape`（TapeReader）用于 MCAP 元数据与附件；缺省时只写不依赖它们的通道。"""
        self.dir = Path(run_dir) if run_dir else None
        self.spec, self.tape = spec, tape
        self.opts = options or RecorderOptions.from_env()
        self.run_id = str(getattr(spec, "run_id", None) or (self.dir.name if self.dir else "run"))
        self._fh: dict[str, object] = {}
        self._raw = self._truth = None
        self._sent: dict[str, dict[str, int]] = {"raw": {}, "truth": {}}
        self.frames: dict[str, int] = {}
        self.summary: dict | None = None
        self._started_ns = time.time_ns()
        if self.dir:
            (self.dir / "truth").mkdir(parents=True, exist_ok=True)
            (self.dir / "net").mkdir(exist_ok=True)
            (self.dir / "agents").mkdir(exist_ok=True)
            if self.opts.mcap:
                self._open_mcap()

    # ------------------------------------------------------------------ MCAP
    def _open_mcap(self) -> None:
        from awr.bench.recorder.writer import RunWriter

        kw = self.opts.writer_kw()
        self._raw = RunWriter(self.dir / "raw.mcap", **kw)
        self._truth = RunWriter(self.dir / "truth" / f"run_{self.run_id}.mcap", **kw)
        self._raw.metadata("run", self._run_meta())
        self._raw.metadata("channels.units", {"imu.acc_mps2": "m/s^2", "imu.gyro_rps": "rad/s", "gnss.pos_enu_m": "m", "gnss.vel_enu_mps": "m/s",
                                              "nav.pos_enu_m": "m", "lidar.xyz_m": "m", "lidar.offset_time": "ns", "time": "ns"})
        self._truth.metadata("run", {"run_id": self.run_id, "layer": "truth"})
        for name, data, mt in self._attachments():
            self._raw.attachment(name, data, mt)

    def _run_meta(self) -> dict[str, str]:
        out = {"run_id": self.run_id, "attempt": "1"}
        sp = self.spec
        if sp is None:
            return out
        out.update(obs_level=str(sp.obs_level), ranked=str(bool(sp.ranked)).lower())
        try:
            ident = sp.identity()
        except ValueError:                                      # 排名运行缺 seed_commit：身份字段留空，不以种子代替
            ident = {}
        for k in ("config_sha256", "spec_sha256", "run_hash"):
            if ident.get(k):
                out[k] = ident[k]
        if sp.seed_commit:
            out["seed_commit"] = str(sp.seed_commit)
        return out

    def _attachments(self) -> list[tuple[str, bytes, str]]:
        out = []
        if self.tape is not None and getattr(self.tape, "path", None):
            tp = Path(self.tape.path)
            out.append(("tape.bin", tp.read_bytes(), "application/octet-stream"))
            if (tp.parent / "tape.json").is_file():
                out.append(("tape.json", (tp.parent / "tape.json").read_bytes(), "application/json"))
        sp = self.spec
        if sp is not None:
            out.append(("world_manifest.json", canonical_json(_world_manifest(Path(sp.world_dir))), "application/json"))
            rs = sp.resolved()
            if sp.ranked:
                rs = {k: v for k, v in rs.items() if k not in _SEED_KEYS}
            out.append(("run_spec.json", canonical_json(rs), "application/json"))
        return out

    def _emit(self, layer: str, topic: str, call, *args, **kw) -> None:
        w = self._raw if layer == "raw" else self._truth
        if w is None:
            return
        call(w, *args, **kw)
        c = self._sent[layer]
        c[topic] = c.get(topic, 0) + 1

    def _json(self, layer: str, topic: str, schema: str, seq: int, t_ns: int, obj) -> None:
        from awr.bench.recorder.writer import RunWriter

        self._emit(layer, topic, RunWriter.write_json, topic, schema, int(seq), int(t_ns), canonical_json(obj))

    def _frame(self, uav: str, sid: str, f: dict) -> None:
        from awr.bench.recorder.channels import camera_topic, imu_topic, lidar_topic
        from awr.bench.recorder.writer import RunWriter, gnss_topic

        seq = int(f["seq"])
        if "rgb" in f:
            depth = None
            if f.get("depth") is not None:
                depth = np.frombuffer(f["depth"]["data"], "<f4").reshape(int(f["height"]), int(f["width"]))
            w = self._raw
            w.write_camera(uav, sid, seq, int(f["t_mid_ns"]), f["rgb"]["data"], depth_m=depth, period_ns=FRAME_PERIOD_NS)
            c = self._sent["raw"]
            for kind in ("image",) + (("depth",) if depth is not None else ()):
                t = camera_topic(uav, sid, kind)
                c[t] = c.get(t, 0) + 1
        elif "temp_ck" in f:                                    # ThermalObs：temp_ck 交付字节原样存档（S17，D3-AC-61）
            from awr.bench.recorder.channels import thermal_topic

            self._emit("raw", thermal_topic(uav, sid), RunWriter.write_thermal, uav, sid, f, period_ns=FRAME_PERIOD_NS)
        elif "xyz_m" in f:
            from awr.bench.recorder.lidar import LIDAR_RECORD

            n = int(f["n"])
            pts = np.zeros(n, LIDAR_RECORD)
            xyz = np.frombuffer(f["xyz_m"], "<f4").reshape(n, 3)
            pts["x"], pts["y"], pts["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
            pts["offset_time"] = np.frombuffer(f["offset_time_ns"], "<u4")
            pts["reflectivity"] = np.frombuffer(f["reflectivity"], "u1")
            pts["tag"] = np.frombuffer(f["tag"], "u1")
            hdr = {"schema": "awr.sensor.lidar_frame.v2", "sensor_id": sid, "frame_id": f"{self.run_id}/{uav}/{sid}/{seq}", "frame_seq": seq,
                   "timebase_ns": int(f["t_start_ns"]), "point_count": n, "sync_type": "sim", "mount_nominal": _plain(f.get("mount_nominal"))}
            self._emit("raw", lidar_topic(uav, sid), RunWriter.write_lidar, uav, sid, seq, int(f["t_start_ns"]), hdr, pts,
                       period_ns=FRAME_PERIOD_NS)
        elif "acc_mps2" in f:
            self._emit("raw", imu_topic(uav, sid), RunWriter.write_imu, uav, sid, seq, int(f["t0_ns"]), _plain(f))
        elif "pos_enu_m" in f:
            self._emit("raw", gnss_topic(uav, sid), RunWriter.write_gnss, uav, sid, seq, int(f["t_ns"]), _plain(f))
        else:                                                   # 未知传感器类型：原样存档，不丢弃
            t = f"/uav/{uav}/obs/{sid}"
            self._emit("raw", t, RunWriter.write_msgpack, t, "awr.bench.SensorObs", seq, int(f.get("t_ns", 0)), _plain(f))
            return
        self.frames[f"{uav}/{sid}"] = self.frames.get(f"{uav}/{sid}", 0) + 1

    def _record_obs(self, uav: str, k: int, obs: dict) -> None:
        from awr.bench.recorder.writer import RunWriter

        t = int(obs.get("t_ns", k * TICK_NS))
        if obs.get("nav") is not None:
            topic = f"/uav/{uav}/obs/nav"
            self._emit("raw", topic, RunWriter.write_msgpack, topic, "awr.bench.NavSolution", int(k), t, _plain(obs["nav"]))
        sensors = obs.get("sensors") or {}
        if "detections" in sensors:
            self._json("raw", f"/uav/{uav}/obs/detections", "awr.bench.Detection[]", k, t, sensors["detections"])
            return
        for sid in sorted(sensors):
            for f in sensors[sid]:
                self._frame(uav, sid, f)

    def finish(self) -> dict | None:
        """关闭两个 MCAP（排空写队列）、对账各通道消息数、生成 `frames.idx`；→ 清单的 `recorder` 块（无 MCAP 时 None）。可重复调用。"""
        if self.summary is not None or self._raw is None:
            return self.summary
        from awr.bench.recorder.frames_idx import build_frames_idx
        from awr.bench.recorder.writer import WriterError, channel_counts

        self._raw.metadata("run.wall", {"started_unix_ns": self._started_ns, "ended_unix_ns": time.time_ns(), "host": platform.node()})
        files, dropped = {}, 0
        for layer, w in (("raw", self._raw), ("truth", self._truth)):
            w.close()
            got = channel_counts(w.path)
            sent = self._sent[layer]
            miss = {t: n - got.get(t, 0) for t, n in sent.items() if got.get(t, 0) != n}
            dropped += sum(max(v, 0) for v in miss.values())
            st = w.summary()
            files[layer] = {"path": str(w.path.relative_to(self.dir)), "bytes": w.path.stat().st_size, "messages": sum(got.values()),
                            "channels": len(got), "mismatch": miss, **{k: st[k] for k in (
                                "mode", "enqueued", "written", "dropped", "blocked", "block_s", "block_max_s", "block_frac", "max_depth",
                                "max_depth_bytes", "write_s", "wall_s")}, "blocks_head": st["blocks"][:20]}
            dropped += st["dropped"]
        head = build_frames_idx(self.dir / "raw.mcap", self.dir / "frames.idx")
        blk = files["raw"]["block_s"] + files["truth"]["block_s"]
        wall = max(files["raw"]["wall_s"], 1e-9)
        self.summary = {"schema": "drone4d.bench.recorder_stats.v1", "raw": files["raw"], "truth": files["truth"],
                        "frames": dict(sorted(self.frames.items())), "frames_idx": {"n_records": head["n_records"],
                                                                                     "bytes": (self.dir / "frames.idx").stat().st_size},
                        "options": asdict(self.opts), "writer_block_s": round(blk, 6), "writer_block_frac": round(blk / wall, 6),
                        "dropped": int(dropped)}
        if dropped:
            raise WriterError(f"recorder lost {dropped} messages: raw {files['raw']['mismatch']}, truth {files['truth']['mismatch']}")
        return self.summary

    # ------------------------------------------------------------------ 文件层
    def _line(self, rel: str, obj) -> None:
        if not self.dir:
            return
        f = self._fh.get(rel)
        if f is None:
            p = self.dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            f = self._fh[rel] = p.open("w", encoding="utf-8")
        f.write(canonical_json(obj).decode() + "\n")

    def write_json(self, rel: str, obj) -> None:
        if self.dir:
            if rel == "manifest.json" and isinstance(obj, dict):
                rs = self.finish()
                if rs is not None:                              # 原地并入，调用方返回的清单同样带写入统计
                    obj["recorder"] = rs
                    obj["writer_block_s"] = rs["writer_block_s"]
            p = self.dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(_enc(obj), ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------ Env 回调
    def on_obs(self, uav: str, k: int, msg: dict) -> str:
        h = hashlib.sha256(canonical_json(msg)).hexdigest()
        if self.dir:
            f = self._fh.get(f"agents/{uav}/obs.sha256")
            if f is None:
                p = self.dir / "agents" / uav / "obs.sha256"
                p.parent.mkdir(parents=True, exist_ok=True)
                f = self._fh[f"agents/{uav}/obs.sha256"] = p.open("w", encoding="utf-8")
            f.write(f"{k} {h}\n")
            if self._raw is not None:
                self._record_obs(uav, k, msg["obs"])
        return h

    def on_act(self, uav: str, k: int, act, acks: list[dict], apply_tick: int | None) -> None:
        row = {"k": k, "apply_tick": apply_tick, "act": act, "acks": acks}
        self._line(f"agents/{uav}/acts.jsonl", row)
        self._json("raw", f"/uav/{uav}/action", "drone4d.bench.act.v1", k, k * TICK_NS, row)

    def on_report(self, uav: str, k_obs: int, apply_tick: int, t_apply_ns: int, rep: dict) -> None:
        row = {"uav": uav, "k_obs": k_obs, "apply_tick": apply_tick, "t_apply_ns": t_apply_ns, "report": rep}
        self._line("reports.jsonl", row)
        self._json("raw", f"/uav/{uav}/report", "drone4d.bench.report.v1", k_obs, t_apply_ns, row)

    def on_l1(self, uav: str, seq: int, t_ns: int, det: dict, truth: dict) -> None:
        row = {"uav": uav, "seq": seq, "t_ns": t_ns, "det_id": det["det_id"], "class": det["class"], "h_px": det["h_px"],
               **{k: truth[k] for k in ("actor", "h_unocc_px", "vis", "ident")}}
        self._line("truth/l1.jsonl", row)
        self._json("truth", f"/eval/hidden/det_map/{uav}", "awr.bench.l1_truth.v1", seq, t_ns, row)

    def on_vis(self, row: dict) -> None:
        self._line("truth/l2.jsonl", row)
        self._json("truth", f"/eval/hidden/vis/{row['uav']}/{row['sensor']}", "awr.bench.l2_vis.v1", row["seq"], row["t_ns"], row)

    def on_contact(self, ev) -> None:
        row = {"uav": ev.uav, "t_ns": ev.t_ns, "kind": ev.kind, "detector": ev.detector, "pos_enu_m": list(ev.pos_enu_m), "speed_mps": ev.speed_mps}
        self._line("truth/contacts.jsonl", row)
        self._json("truth", f"/eval/hidden/contacts/{ev.uav}", "awr.bench.contact.v1", 0, ev.t_ns, row)

    def net(self, records: list[dict]) -> None:
        for i, r in enumerate(records):
            self._line("net/records.jsonl", r)
            self._json("raw", "/net/records", "awr.bench.net_record.v1", i, int(r.get("t_ns", 0)), r)

    def fleet(self, uavs: list[str], t_ns: list[int], pos, vel, yaw, nav, status) -> None:
        if not self.dir:
            return
        P, V, Y, N = (np.asarray(pos, np.float64), np.asarray(vel, np.float32), np.asarray(yaw, np.float32), np.asarray(nav, np.float64))
        S = np.asarray(status, "U10")
        np.savez_compressed(self.dir / "truth" / "fleet.npz", uavs=np.array(uavs), t_ns=np.asarray(t_ns, np.int64), pos=P, vel=V, yaw=Y, nav=N,
                            status=S)
        if self._truth is None:
            return
        from awr.bench.recorder.writer import RunWriter

        for i, t in enumerate(t_ns):                            # 真值位姿（每 tick 末）只进真值文件
            for j, u in enumerate(uavs):
                topic = f"/uav/{u}/state"
                self._emit("truth", topic, RunWriter.write_msgpack, topic, "awr.bench.TruthState.v1", i, int(t),
                           {"t_ns": int(t), "pos_enu_m": P[i, j].tolist(), "vel_enu_mps": V[i, j].tolist(), "yaw_rad": float(Y[i, j]),
                            "nav_pos_enu_m": N[i, j].tolist(), "status": str(S[i, j])})

    def close(self) -> None:
        try:
            self.finish()
        finally:
            for f in self._fh.values():
                f.close()
            self._fh.clear()


def _world_manifest(world_dir: Path) -> dict:
    """`world_manifest.json`（M27-FR-065，public 形式：不含内部路径与构建信息）；世界包不完整（如合成测试世界）时退回 world.json 的
    身份字段并标注 `partial`。"""
    try:
        from awr.world.glb.manifest import world_manifest

        return world_manifest(world_dir, public=True)
    except Exception as e:                                      # 附件不影响运行，缺项在清单中标注
        w = {}
        if (world_dir / "world.json").is_file():
            w = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
        return {"schema": "awr.world_manifest.v1", "world_id": w.get("id", world_dir.name), "contentVersion": w.get("contentVersion"),
                "public": True, "partial": True, "why": type(e).__name__}
