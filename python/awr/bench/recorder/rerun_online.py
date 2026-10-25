"""M27 `.rrd` 在线写入（AWR-05 §6.1.2；M27-FR-075；D3-AC-20、M27-AC-021"离线导出与在线写入逐实体一致"）：运行期间从两个 MCAP
写入器的消息流同步写出与离线导出（`rerun_export --from-mcap`）同名、同层、同实体的 `.rrd`。

    <out>/obs.<uav>.rrd     观测层，每机一个分片（`mode="process"` 时每个分片一个独立进程）
    <eval_out>/truth.rrd    真值层（真值位姿、实例与语义、LiDAR 真值世界系点）
    <eval_out>/eval.rrd     评测层（可见度、链路时延与投递结果）
    <out>/export.json       与离线导出同一清单 drone4d.bench.rrd_export.v1，`writer = "online"`，`online` 块记录模式、队列、追赶

拓扑（§6.1.2"② RRD 分片写入器……从 MCAP 或总线读取，可离线追赶；不在仿真主循环内"）：

- `McapTee` 包装 `RunWriter` 内部的 mcap `Writer`，在写线程上把通道注册、消息、附件与关闭事件转发给 `OnlineRrd`（路由器），MCAP
  的写入内容与顺序不变；转发异常不影响 MCAP，只使在线结果改由离线追赶；
- 路由器按通道把消息分发到分片：本机观测通道 → `obs.<uav>`；真值位姿、相机真值、LiDAR 真值与观测 LiDAR 帧（去畸变用）→ `truth`；
  相机真值的 `objects`、可见度归约与链路记录 → `eval`；观测分片只收到相机真值帧中的 `agent_view` 分区（交付给 Agent 的内参与云台
  读数，与离线导出的唯一例外相同），不接触真值载荷；
- 分片逻辑逐条对照离线导出的 `export_obs`、`export_truth`、`export_eval`（同一 `LayerWriter`、`ColumnBuffer`、`Timebase`、
  `_CameraSink`、`sensor_map` 与抽帧规则）；
- `mode="process"`（缺省）：每个分片一个 spawn 进程与有界队列（`queue_msgs`）；队列满时 `on_full="catchup"`（缺省）不阻塞写线程，
  该分片停止在线写入，在 `finalize` 中由 MCAP 离线追赶重新派生；`on_full="block"` 阻塞写线程（经 RunWriter 队列反压到仿真，墙钟变慢、
  仿真不变）；`mode="inline"` 在写线程上直接处理（测试与小规模运行）。

只能在运行结束时确定的项在 `finalize(run_dir)` 中按与离线相同的规则补写（静态数据不带时间，写入先后不影响内容）：

- 段属性（policy、physics、noise、git_sha 等来自运行结束时写出的 `manifest.json`）；
- 相机 `Pinhole` 与 `/fleet/<uav>/cam<k>` 位姿：离线按全部内参与云台读数判定静态或逐时刻（变焦、云台随帧变化），在线缓存内参与
  RGB 帧时刻，结束时判定并写出；
- 评测层只含小体量标量，在线缓存后按 `(log_time, 到达序)` 排序写出，与离线按 log_time 读取的顺序一致。

一致性保护：结束时以 `RunSource` 复核在线运行所用的身份、`tick` 频率与传感器映射；任一不一致、通道 log_time 逆序、LiDAR 真值帧
配对缓存溢出、分片异常或队列满时，该分片（或全部分片）由 MCAP 离线追赶，原因写入清单 `online.catchup`。追赶后的文件即离线导出，
逐实体一致仍成立；测试与 dmax 复测以 `catchup == {}` 判定在线路径本身一致。

结束时按 §6.1.2 执行 `rerun rrd optimize`（chunk 压实，缺省档）。`rerun rrd compare`（含 `--unordered`）按 chunk 比较，而 Rerun SDK
的批处理按墙钟与体量切分 chunk，在线与离线的 chunk 边界因此不同（缺省档 optimize 也不能消除）；`compare_online` 把两侧以取消上限的
`rerun rrd optimize` 归一化（每实体每组分量一个 chunk）后比较，并辅以逐实体行读出核对（`entity_digest`）。

接入：`attach(run_writer, "raw" | "truth")`；Env 运行用 `attach_env(recorder)`（`manifest.json` 写出后自动 `finalize`）；已有运行
可经 `replay_run` 按文件顺序重放（`pace` 为仿真时间相对墙钟的倍率，`None` 不限速），用于 dmax 复测。

命令行：`python -m awr.bench.recorder.rerun_online replay --run <run_dir> --out DIR [--profile review] [--mode process] [--pace 1.0]
[--compare] [--verify]`；`python -m awr.bench.recorder.rerun_online compare --run <run_dir> --online DIR [--work DIR]`。
"""

from __future__ import annotations

import contextlib
import hashlib
import heapq
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

import numpy as np

from awr.bench.recorder.channels import CAMERA_TOPIC, LIDAR_TOPIC, TRUTH_CAMERA_TOPIC, TRUTH_LIDAR_TOPIC
from awr.bench.recorder.rerun_export import (
    _APP,
    DEFAULT_FAR_M,
    EXPORT_SCHEMA,
    LAYERS,
    PROFILES,
    RERUN_LOCKED,
    TIMELINES,
    LayerWriter,
    RerunExportError,
    RunSource,
    Timebase,
    _base,
    _Calib,
    _cam_tf,
    _CameraSink,
    _cli_env,
    _gt_json,
    _job,
    _lidar_pick,
    _mount,
    _msgpack,
    _quat_from_R,
    _Rate,
    _rerun_cli,
    compare_rrd,
    deskew_world,
    export_run,
    lint_rrd,
    path_part,
    require_sdk,
    sensor_map,
    verify_rrd,
)

__all__ = ["LIDAR_PAIR_MAX", "McapTee", "OnlineRrd", "attach_env", "compare_online", "entity_digest", "main", "optimize_rrd", "replay_run"]

LIDAR_PAIR_MAX = 64                     # 真值分片每个 (机, LiDAR) 暂存的未配对观测帧上限（超出时最旧的丢弃，必要时追赶）
FINISH_TIMEOUT_S = 1800.0
CANONICAL_LIMITS = ("--max-size", "64GiB", "--max-rows", "1000000000", "--max-rows-if-unsorted", "1000000000")
_UAV = re.compile(r"^/uav/([^/]+)/")
_OBS_MISC = re.compile(r"^/uav/([^/]+)/(imu|gnss)/([A-Za-z0-9_]+)$")
_CALIB = re.compile(r"^/uav/([^/]+)/camera/([A-Za-z0-9_]+)/calib$")
_STATE = re.compile(r"^/uav/([^/]+)/state$")
_VIS = re.compile(r"^/eval/hidden/vis/([^/]+)/")


# ---------------------------------------------------------------------- 写入器


class _LiveLayer(LayerWriter):
    """在线层文件：与 `LayerWriter` 相同，段属性在 `finalize` 时写入（`manifest.json` 于运行结束时才写出）。"""

    def __init__(self, path: Path, *, app_id: str, recording_id: str, layer: str, timebase: Timebase, recording_name: str | None = None) -> None:
        import rerun as rr

        require_sdk()
        if layer not in LAYERS:
            raise ValueError(f"unknown layer {layer!r}")
        if not _APP.match(app_id):
            raise ValueError(f"application_id {app_id!r} must match drone4d-d3-<world>-<event> with [a-z0-9_-] only")
        self.rr = rr
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.layer = layer
        self.tb = timebase
        self.rec = rr.RecordingStream(app_id, recording_id=recording_id, send_properties=False)
        self.rec.save(str(self.path))
        rr.set_log_time_enabled(False, recording=self.rec)
        rr.set_log_tick_enabled(False, recording=self.rec)
        if recording_name:
            rr.send_recording_name(recording_name, recording=self.rec)
        self.entities: dict[str, dict[str, int]] = {}
        self._buf = {}
        self._series: set[str] = set()
        self.closed = False

    def send_properties(self, properties: dict) -> None:
        props = {k: v for k, v in properties.items() if v is not None}
        self.rr.send_property("d3", self.rr.AnyValues(**props), recording=self.rec)


class _LiveCamera(_CameraSink):
    """在线相机：RGB 与深度逐帧写出；`Pinhole` 与相机位姿（静态或逐帧）在 `finish` 时按全部内参与云台读数判定（与离线
    `_CameraSink.__init__` 同一规则）。"""

    def __init__(self, w: LayerWriter, ent: str, prof, far_m: float, stats: dict) -> None:
        self.w, self.ent, self.prof, self.far_m, self.st = w, ent, prof, float(far_m), stats
        self.rate_rgb, self.rate_depth = _Rate(prof.rgb_hz), _Rate(prof.depth_hz)
        self.enc = None
        self.dec = None
        self.size = None
        self.calibs: list[_Calib] = []
        self.st.update(calib_source="none", rgb_frames=0, rgb_samples=0, depth_frames=0, depth_out_of_range=0, keyframes=0)
        self._pose_dyn = False
        self._pose_t: list[int] = []

    def pose(self, t_ns: int) -> None:
        self._pose_t.append(int(t_ns))

    def finish(self, calibs: list[_Calib], how: str) -> None:
        w = self.w
        self.calibs = calibs
        self.st["calib_source"] = how
        self._pose_dyn = any(c.gimbal for c in calibs) and len({json.dumps(c.gimbal, sort_keys=True) for c in calibs}) > 1
        if calibs:
            self._pinholes()
        if not self._pose_dyn:
            c0 = calibs[0] if calibs else None
            t, q = _cam_tf(c0.mount if c0 else None, c0.gimbal if c0 else None)
            w.static(self.ent, w.rr.Transform3D(translation=t, quaternion=q))
            self.st["cam_pose"] = "static" if c0 and (c0.mount or c0.gimbal) else "nominal_forward"
        else:
            self.st["cam_pose"] = "per_frame"
            self._ci = 0
            for t in self._pose_t:
                _CameraSink.pose(self, t)
        self._pose_t = []
        self.close()


# ---------------------------------------------------------------------- 分片


class _Shard:
    """一个层文件的在线写入（在分片进程或写线程中运行）。消息项：

    ("c", topic, info)                    通道信息（encoding、schema、metadata）
    ("map", cams, lidars)                 传感器名映射（路由器维护，变化时下发）
    ("m", topic, t_log, seq, data)        消息原样
    ("av", cam, t_log, agent_view)        相机真值帧的 agent_view 分区（只发往观测分片）
    ("gt", uav, t_log, objects)           相机真值帧的 objects（只发往评测分片）
    """

    def __init__(self, init: dict) -> None:
        self.kind = init["kind"]
        self.prof = PROFILES[init["profile"]]
        self.far_m = float(init["far_m"])
        self.uav = init.get("uav")
        self.uavs = init.get("uavs")
        layer = "obs" if self.kind == "obs" else self.kind
        name = f"{init['run_id']} obs.{path_part(self.uav)}" if self.kind == "obs" else f"{init['run_id']} {self.kind}"
        self.t_wall = time.perf_counter()
        self.w = _LiveLayer(Path(init["path"]), app_id=init["app_id"], recording_id=init["run_id"], layer=layer,
                            timebase=Timebase(int(init["main_hz"])), recording_name=name)
        _base(self.w, semantic=self.kind != "eval")
        self.chan: dict[str, dict] = {}
        self.cams: dict[str, str] = dict(init.get("cams") or {})
        self.lidars: dict[str, str] = dict(init.get("lidars") or {})
        self.n = 0
        self.busy_s = 0.0
        self.skipped_names: set[str] = set()
        self.inexact: list[str] = []
        self.st: dict = {"layer": layer, "path": str(init["path"])}
        getattr(self, f"_init_{self.kind}")()

    # ------------------------------------------------------------------ 分发
    def handle(self, item: tuple) -> None:
        t0 = time.perf_counter()
        k = item[0]
        if k == "c":
            self.chan[item[1]] = item[2]
        elif k == "map":
            self.cams, self.lidars = dict(item[1]), dict(item[2])
        elif k == "m":
            getattr(self, f"_m_{self.kind}")(item[1], int(item[2]), int(item[3]), item[4])
        elif k == "av":
            self._av(item[1], int(item[2]), item[3])
        elif k == "gt":
            self._gt(item[1], int(item[2]), item[3])
        self.n += 1
        self.busy_s += time.perf_counter() - t0

    def finish(self, fin: dict) -> dict:
        getattr(self, f"_finish_{self.kind}")(fin)
        self.w.send_properties(fin["props"])
        self.w.close()
        self.st.update(entities=self.w.entities, bytes=self.w.path.stat().st_size, wall_s=round(time.perf_counter() - self.t_wall, 3),
                       online={"pid": os.getpid(), "items": self.n, "busy_s": round(self.busy_s, 3), "skipped_names": sorted(self.skipped_names),
                               "inexact": self.inexact})
        return self.st

    def abort(self) -> None:
        with contextlib.suppress(Exception):                 # 追赶会覆盖该文件
            self.w.close()

    # ------------------------------------------------------------------ 观测层（对照 export_obs）
    def _init_obs(self) -> None:
        self.U = f"/fleet/{path_part(self.uav)}"
        self.st.update(uav=self.uav, nav=0, telemetry={}, cameras={}, lidars={}, detections=0)
        self.sinks: dict[str, _LiveCamera] = {}
        self.calib: dict[str, list[_Calib]] = {}
        self.avs: dict[str, list[_Calib]] = {}
        self.lid_rate: dict[str, _Rate] = {}
        self.lid_static: set[str] = set()

    def _sink(self, cam: str) -> _LiveCamera | None:
        s = self.sinks.get(cam)
        if s is None:
            ck = self.cams.get(cam)
            if ck is None:
                self.skipped_names.add(cam)
                return None
            cs = self.st["cameras"].setdefault(ck, {"sensor_id": cam})
            s = self.sinks[cam] = _LiveCamera(self.w, f"{self.U}/{ck}", self.prof, self.far_m, cs)
        meta = self.chan.get(f"/uav/{self.uav}/camera/{cam}/image", {}).get("metadata", {})
        s.st["period_ns"] = int(meta.get("period_ns", 100_000_000))
        return s

    def _av(self, cam: str, t: int, av: dict) -> None:
        if av.get("K"):
            self.avs.setdefault(cam, []).append(_Calib(t, [float(v) for v in av["K"]], int(av["width"]), int(av["height"]), av.get("mount_nominal"),
                                                       av.get("gimbal_reading")))

    def _m_obs(self, topic: str, t: int, seq: int, data: bytes) -> None:
        from awr.bench.recorder.foxglove import decode_image
        from awr.bench.recorder.lidar import unpack_lidar_frame

        w, rr, U, st = self.w, self.w.rr, self.U, self.st
        if topic.endswith("/obs/nav"):
            nav = _msgpack(data)
            t = int(nav.get("t_ns", t))
            p, v = nav["pos_enu_m"], nav.get("vel_enu_mps") or [0.0, 0.0, 0.0]
            w.transform(U, t, p, nav.get("q_wb_xyzw") or [0.0, 0.0, 0.0, 1.0])
            w.scalars(f"{U}/telemetry/altitude", t, [float(p[2])])
            w.scalars(f"{U}/telemetry/speed", t, [float(np.linalg.norm(v))])
            if nav.get("battery_soc") is not None:
                w.scalars(f"{U}/telemetry/battery", t, [float(nav["battery_soc"])])
            st["nav"] += 1
            return
        mc = CAMERA_TOPIC.match(topic)
        if mc:
            sink = self._sink(mc.group(2))
            if sink is None:
                return
            if mc.group(3) == "image":
                sink.rgb(t, decode_image("foxglove.CompressedImage", data)["payload"])
            else:
                info = self.chan.get(topic, {}).get("metadata", {})
                d = decode_image("foxglove.RawImage", data, info.get("compression"))
                sink.depth(t, np.frombuffer(d["payload"], "<f4").reshape(int(d["height"]), int(d["width"])))
            return
        mk = _CALIB.match(topic)
        if mk:
            from foxglove_schemas_protobuf.CameraCalibration_pb2 import CameraCalibration

            c = CameraCalibration()
            c.ParseFromString(data)
            self.calib.setdefault(mk.group(2), []).append(_Calib(t, [float(v) for v in c.K], int(c.width), int(c.height)))
            return
        ml = LIDAR_TOPIC.match(topic)
        if ml:
            name = ml.group(2)
            lk = self.lidars.get(name)
            if lk is None:
                self.skipped_names.add(name)
                return
            hdr, pts = unpack_lidar_frame(bytes(data))
            per = int(self.chan.get(topic, {}).get("metadata", {}).get("period_ns", 100_000_000))
            ent = f"{U}/{lk}"
            ls = st["lidars"].setdefault(lk, {"sensor_id": name, "frames": 0, "points": 0, "period_ns": per})
            if ent not in self.lid_static:
                self.lid_static.add(ent)
                tt, R = _mount(hdr.get("mount_nominal"))
                w.static(ent, rr.Transform3D(translation=tt, quaternion=_quat_from_R(R)))
            t_end = int(hdr["timebase_ns"]) + per
            if not self.lid_rate.setdefault(name, _Rate(self.prof.lidar_hz)).keep(t_end):
                return
            sel = _lidar_pick(len(pts), self.prof.lidar_max_points)
            if sel is not None:
                pts = pts[sel]
            xyz = np.stack([pts["x"], pts["y"], pts["z"]], axis=1)
            w.log(ent, t_end, rr.Points3D(xyz), rr.AnyValues(offset_time=np.ascontiguousarray(pts["offset_time"]),
                                                             reflectivity=np.ascontiguousarray(pts["reflectivity"])))
            ls["frames"] += 1
            ls["points"] += len(pts)
            return
        if topic.endswith("/obs/detections"):
            by: dict[tuple[str, int], list[dict]] = {}
            for d in json.loads(data) or []:
                by.setdefault((str(d.get("sensor_id")), int(d.get("t_mid_ns", t))), []).append(d)
            for (sid, tm), ds in sorted(by.items()):
                ck = self.cams.get(sid)
                if ck is None:
                    self.skipped_names.add(sid)
                    continue
                arr = np.array([d["bbox_xywh"] for d in ds], np.float32).reshape(-1, 4)
                w.log(f"{U}/{ck}/image/det", tm, rr.Boxes2D(array=arr, array_format=rr.Box2DFormat.XYWH, labels=[str(d["class"]) for d in ds]),
                      rr.AnyValues(det_id=[str(d["det_id"]) for d in ds]))
                st["detections"] += len(ds)
            return
        mi = _OBS_MISC.match(topic)
        if mi:
            o = _msgpack(data)
            ent = f"{U}/telemetry/{path_part(mi.group(3))}"
            if mi.group(2) == "imu":
                n = int(o["n"])
                ts = int(o["t0_ns"]) + np.arange(n, dtype=np.int64) * int(o["dt_ns"])
                if not n:
                    return
                v = np.concatenate([np.frombuffer(o["acc_mps2"], "<f4").reshape(n, 3), np.frombuffer(o["gyro_rps"], "<f4").reshape(n, 3)], 1)
                w.series(ent, ["acc_x_mps2", "acc_y_mps2", "acc_z_mps2", "gyro_x_rps", "gyro_y_rps", "gyro_z_rps"])
                w.scalars(ent, ts, v)
                st["telemetry"][ent] = st["telemetry"].get(ent, 0) + n
            else:
                w.series(ent, ["pos_e_m", "pos_n_m", "pos_u_m"])
                w.scalars(ent, int(o["t_ns"]), [float(x) for x in o["pos_enu_m"]])
                st["telemetry"][ent] = st["telemetry"].get(ent, 0) + 1

    def _finish_obs(self, fin: dict) -> None:
        for cam, spec in sorted(fin["cams"].items()):
            if cam not in self.sinks:                         # 只有 calib 通道、没有图像的相机：离线同样写出静态标定
                ck = spec["key"]
                cs = self.st["cameras"].setdefault(ck, {"sensor_id": cam})
                cs["period_ns"] = int(spec.get("period_ns", 100_000_000))
                self.sinks[cam] = _LiveCamera(self.w, f"{self.U}/{ck}", self.prof, self.far_m, cs)
            how = spec["calib"]
            cal = sorted(self.calib.get(cam, []) if how == "calib" else self.avs.get(cam, []) if how == "agent_view" else [],
                         key=lambda c: c.t_ns)
            self.sinks[cam].finish(cal, how)
        extra = sorted(set(self.sinks) - set(fin["cams"]))
        if extra:
            self.inexact.append(f"camera sinks {extra} are not cameras of the finished run")

    # ------------------------------------------------------------------ 真值层（对照 export_truth）
    def _init_truth(self) -> None:
        import zstandard

        self.st.update(poses=0, segmentation=0, lidar_frames=0, lidar_points=0)
        self.dz = zstandard.ZstdDecompressor()
        self.fifo: list[tuple] = []                           # 待配对的 LiDAR 真值帧（到达顺序）
        self.raw: dict[tuple[str, str], dict[int, tuple[dict, np.ndarray]]] = {}
        self.raw_max: dict[tuple[str, str], int] = {}
        self.evicted: dict[tuple[str, str], int] = {}
        self.lid_rate: dict[str, _Rate] = {}
        self.want = set(self.uavs) if self.uavs else None

    def _keep(self, uav: str) -> bool:
        return self.want is None or uav in self.want

    def _m_truth(self, topic: str, t: int, seq: int, data: bytes) -> None:
        from awr.bench.recorder.lidar import unpack_lidar_frame, unpack_lidar_gt
        from awr.bench.recorder.truth import unpack_camera_gt

        w, rr = self.w, self.w.rr
        ms = _STATE.match(topic)
        if ms:
            if not self._keep(ms.group(1)) or self.chan.get(topic, {}).get("encoding") != "msgpack":
                return
            o = _msgpack(data)
            yaw = float(o.get("yaw_rad", 0.0))
            q = o.get("q_wb_xyzw") or [0.0, 0.0, float(np.sin(yaw / 2)), float(np.cos(yaw / 2))]
            w.transform(f"/truth/fleet/{path_part(ms.group(1))}", int(o.get("t_ns", t)), o["pos_enu_m"], q)
            self.st["poses"] += 1
            return
        mc = TRUTH_CAMERA_TOPIC.match(topic)
        if mc:
            uav, cam = mc.group(1), mc.group(2)
            ck = self.cams.get(cam)
            if not self._keep(uav):
                return
            if ck is None:
                self.skipped_names.add(cam)
                return
            rec, blocks = unpack_camera_gt(bytes(data))
            av, gt = rec.get("agent_view") or {}, rec.get("ground_truth") or {}
            hh, ww = int(av.get("height", 0)), int(av.get("width", 0))
            base = f"/fleet/{path_part(uav)}/{ck}/image"
            for key, dt in (("instance", "<u2"), ("semantic", "u1")):
                ent = gt.get(key)
                if not isinstance(ent, dict) or ent.get("ref") not in blocks or not hh:
                    continue
                b = blocks[ent["ref"]]
                if ent.get("codec") == "zstd":
                    b = self.dz.decompress(b)
                w.log(f"{base}/{key}", t, rr.SegmentationImage(np.frombuffer(b, dt).reshape(hh, ww)))
                self.st["segmentation"] += 1
            return
        ml = TRUTH_LIDAR_TOPIC.match(topic)
        if ml:
            uav, name = ml.group(1), ml.group(2)
            if not self._keep(uav):
                return
            if name not in self.lidars:
                self.skipped_names.add(name)
                return
            hdr, _inst, flags = unpack_lidar_gt(bytes(data))
            self.fifo.append((uav, name, int(hdr.get("frame_seq", seq)), hdr, flags))
            self._drain(final=False)
            return
        mr = LIDAR_TOPIC.match(topic)
        if mr:                                                 # 观测 LiDAR 帧（raw.mcap）：按 seq 与真值帧配对
            key = (mr.group(1), mr.group(2))
            if not self._keep(key[0]):
                return
            h, p = unpack_lidar_frame(bytes(data))
            s = int(h.get("frame_seq", seq))
            buf = self.raw.setdefault(key, {})
            buf[s] = (h, p)
            self.raw_max[key] = max(self.raw_max.get(key, s), s)
            while len(buf) > LIDAR_PAIR_MAX:
                old = min(buf)
                del buf[old]
                self.evicted[key] = max(self.evicted.get(key, old), old)
            self._drain(final=False)

    def _drain(self, *, final: bool) -> None:
        """按到达顺序处理 LiDAR 真值帧：同 seq 观测帧已到达时处理；该机该 LiDAR 已出现更大 seq 的观测帧（离线按 seq 单调读取时
        判为缺帧）或运行已结束时计为跳过；否则等待。"""
        while self.fifo:
            uav, name, seq, hdr, flags = self.fifo[0]
            key = (uav, name)
            buf = self.raw.get(key, {})
            got = buf.pop(seq, None)
            if got is None:
                if seq <= self.evicted.get(key, -1):
                    self.inexact.append(f"lidar pairing buffer evicted {uav}/{name} seq {seq}")
                if not final and self.raw_max.get(key, -1) <= seq:
                    return
            self.fifo.pop(0)
            for s in [s for s in buf if s < seq]:                 # 离线按 seq 单调前进；逆序请求已丢弃的帧时追赶
                del buf[s]
                self.evicted[key] = max(self.evicted.get(key, s), s)
            self._truth_lidar(uav, name, hdr, flags, got)

    def _truth_lidar(self, uav: str, name: str, hdr: dict, flags: np.ndarray, got) -> None:
        nodes = hdr.get("T_world_sensor_nodes")
        if not nodes or got is None:
            self.st["lidar_skipped"] = self.st.get("lidar_skipped", 0) + 1
            return
        oh, pts = got
        per = int(self.chan.get(f"/uav/{uav}/lidar/{name}", {}).get("metadata", {}).get("period_ns", 100_000_000))
        t_end = int(oh["timebase_ns"]) + per
        if not self.lid_rate.setdefault(name, _Rate(self.prof.lidar_hz)).keep(t_end):
            return
        sel = _lidar_pick(len(pts), self.prof.lidar_max_points)
        if sel is not None:
            pts = pts[sel]
        xyz = np.stack([pts["x"], pts["y"], pts["z"]], axis=1)
        world = deskew_world(xyz, pts["offset_time"], int(oh["timebase_ns"]), nodes)
        kw = {}
        dtp = int(oh.get("point_dt_ns") or 0)
        if dtp > 0:
            slot = (pts["offset_time"].astype(np.int64) // dtp).clip(0, len(flags) - 1)
            kw["class_ids"] = (flags[slot] & 0x3F).astype(np.uint16)
        self.w.log(f"/truth/fleet/{path_part(uav)}/{self.lidars[name]}", t_end, self.w.rr.Points3D(world, **kw))
        self.st["lidar_frames"] += 1
        self.st["lidar_points"] += len(pts)

    def _finish_truth(self, fin: dict) -> None:
        self._drain(final=True)

    # ------------------------------------------------------------------ 评测层（对照 export_eval）
    def _init_eval(self) -> None:
        self.st.update(visibility=0, comm=0)
        self.rows: list[tuple] = []          # (log_time, 到达序, 实体, 系列名 | None, 写入时刻, 值)

    def _gt(self, uav: str, t: int, objects: list) -> None:
        for o in objects or []:
            self.rows.append((t, len(self.rows), f"/eval/visibility/{path_part(uav)}/{path_part(o['actor'])}", ("px_visible", "px_unoccluded"), t,
                              [float(o.get("px_visible", 0)), float(o.get("px_unoccluded", 0))]))

    def _m_eval(self, topic: str, t: int, seq: int, data: bytes) -> None:
        if topic == "/net/records":
            r = json.loads(data)
            tt = int(r.get("t_ns", t))
            link = f"{path_part(r['src'])}_{path_part(r['dst'])}"
            self.rows.append((t, len(self.rows), f"/eval/metrics/comm_ok_{link}", None, tt, [1.0 if r.get("ok") else 0.0]))
            if r.get("ok") and r.get("lat_ms") is not None:
                self.rows.append((t, len(self.rows), f"/eval/metrics/comm_latency_ms_{link}", None, tt, [float(r["lat_ms"])]))
            self.st["comm"] += 1
            return
        r = json.loads(data)
        self.rows.append((t, len(self.rows), f"/eval/visibility/{path_part(r['uav'])}/{path_part(r['actor'])}", ("h_unocc_px", "vis"),
                          int(r.get("t_ns", t)), [float(r.get("h_unocc_px", 0)), float(r.get("vis", 0.0))]))

    def _finish_eval(self, fin: dict) -> None:
        """离线按 log_time 读取（相等时按文件顺序）；在线按 (log_time, 到达序) 排序后写出，可见度与链路记录分属不同实体。"""
        w = self.w
        for _t, _i, ent, names, t_w, vals in sorted(self.rows, key=lambda r: (r[0], r[1])):
            if names is not None:
                w.series(ent, list(names))
                self.st["visibility"] += 1
            w.scalars(ent, t_w, vals)
        self.rows = []


def _worker(init: dict, q_in, q_out) -> None:
    """分片进程：逐项处理直至 ("finish", fin) 或 ("abort",)；结果经 q_out 返回。"""
    try:
        sh = _Shard(init)
    except BaseException as e:
        q_out.put(("err", f"{e!r}\n{traceback.format_exc()}"))
        return
    failed = None
    while True:
        it = q_in.get()
        if it[0] == "finish":
            if failed:
                sh.abort()
                q_out.put(("err", failed))
                return
            try:
                q_out.put(("ok", sh.finish(it[1])))
            except BaseException as e:
                q_out.put(("err", f"{e!r}\n{traceback.format_exc()}"))
            return
        if it[0] == "abort":
            sh.abort()
            q_out.put(("aborted", None))
            return
        if failed:
            continue                                         # 继续取空队列，使生产者不因异常分片而阻塞
        try:
            sh.handle(it)
        except BaseException as e:
            failed = f"{e!r}\n{traceback.format_exc()}"


class _Inline:
    """写线程内直接处理的分片句柄。"""

    def __init__(self, init: dict) -> None:
        self.err: str | None = None
        self.sh: _Shard | None = None
        try:
            self.sh = _Shard(init)
        except Exception as e:
            self.err = f"{e!r}\n{traceback.format_exc()}"

    def put(self, item: tuple) -> bool:
        if self.err is None:
            try:
                self.sh.handle(item)
            except Exception as e:
                self.err = f"{e!r}\n{traceback.format_exc()}"
        return True

    def finish(self, fin: dict | None) -> tuple[str, object]:
        if self.err is not None or fin is None:
            if self.sh is not None:
                self.sh.abort()
            return ("err", self.err) if self.err else ("aborted", None)
        try:
            return "ok", self.sh.finish(fin)
        except Exception as e:
            return "err", f"{e!r}\n{traceback.format_exc()}"

    def stats(self) -> dict:
        return {"blocked": 0, "block_s": 0.0}


class _Remote:
    """分片进程句柄（spawn）：有界输入队列；`put` 在队列满时按 `on_full` 阻塞或返回 False。"""

    def __init__(self, init: dict, *, queue_msgs: int, on_full: str) -> None:
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        self.q_in = ctx.Queue(maxsize=max(1, int(queue_msgs)))
        self.q_out = ctx.Queue()
        self.p = ctx.Process(target=_worker, args=(init, self.q_in, self.q_out), name=f"rrd-online:{init['key']}", daemon=True)
        self.p.start()
        self.on_full = on_full
        self.blocked, self.block_s = 0, 0.0

    def put(self, item: tuple) -> bool:
        try:
            self.q_in.put(item, block=False)
            return True
        except queue.Full:
            if self.on_full != "block":
                return False
        t0 = time.perf_counter()
        self.blocked += 1
        while True:
            try:
                self.q_in.put(item, timeout=1.0)
                self.block_s += time.perf_counter() - t0
                return True
            except queue.Full:
                if not self.p.is_alive():
                    self.block_s += time.perf_counter() - t0
                    return False

    def finish(self, fin: dict | None) -> tuple[str, object]:
        if fin is None:                                      # 放弃：文件由离线追赶覆盖，不必排空队列
            self.p.kill()
            self.p.join(timeout=30)
            return "aborted", None
        msg = ("finish", fin)
        try:
            deadline = time.monotonic() + FINISH_TIMEOUT_S
            while True:
                try:
                    self.q_in.put(msg, timeout=1.0)
                    break
                except queue.Full:
                    if not self.p.is_alive() or time.monotonic() > deadline:
                        raise
            while True:
                try:
                    out = self.q_out.get(timeout=1.0)
                    break
                except queue.Empty:
                    if not self.p.is_alive() or time.monotonic() > deadline:
                        return "err", f"shard process exited (code {self.p.exitcode}) without a result"
            return out
        except queue.Full:
            return "err", "shard process did not accept the finish message"
        finally:
            self.p.join(timeout=30)
            if self.p.is_alive():
                self.p.kill()
                self.p.join(timeout=5)

    def stats(self) -> dict:
        return {"blocked": self.blocked, "block_s": round(self.block_s, 6)}


# ---------------------------------------------------------------------- MCAP 旁路


class McapTee:
    """mcap `Writer` 的包装：先写 MCAP，再把同一事件转发给 `OnlineRrd`；转发异常只记录，不影响 MCAP。"""

    def __init__(self, inner, sink: OnlineRrd, source: str) -> None:
        self._inner, self._sink, self._source = inner, sink, source
        self._schemas: dict[int, str] = {}

    def register_schema(self, name: str, encoding: str, data: bytes) -> int:
        sid = self._inner.register_schema(name=name, encoding=encoding, data=data)
        self._schemas[sid] = name
        return sid

    def register_channel(self, topic: str, message_encoding: str, schema_id: int, metadata: dict | None = None) -> int:
        meta = dict(metadata or {})
        cid = self._inner.register_channel(topic=topic, message_encoding=message_encoding, schema_id=schema_id, metadata=meta)
        self._sink._safe(self._sink._tap_channel, self._source, cid, topic, message_encoding, self._schemas.get(schema_id) or meta.get("schema"),
                         meta)
        return cid

    def add_message(self, channel_id: int, log_time: int, data: bytes, publish_time: int, sequence: int = 0) -> None:
        self._inner.add_message(channel_id=channel_id, log_time=log_time, data=data, publish_time=publish_time, sequence=sequence)
        self._sink._safe(self._sink._tap_message, self._source, channel_id, int(log_time), int(sequence), data)

    def add_attachment(self, create_time: int, log_time: int, name: str, media_type: str, data: bytes) -> None:
        self._inner.add_attachment(create_time=create_time, log_time=log_time, name=name, media_type=media_type, data=data)
        self._sink._safe(self._sink._tap_attachment, self._source, name, data)

    def finish(self) -> None:
        self._inner.finish()
        self._sink._safe(self._sink._tap_close, self._source)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


# ---------------------------------------------------------------------- 路由器


class OnlineRrd:
    """运行期 `.rrd` 写入（路由器 + 分片）。`run_id`、`world`、`event` 与离线 `RunSource` 的身份一致（`application_id`、
    `recording_id`）；`main_hz` 缺省取 `run_spec.json` 附件的 `clock_profile.main_hz`（首条消息前未见附件时为 250）；
    `cams` / `lidars` 为传感器名全集时映射固定，缺省在线累积（名称已是 `cam<k>` / `lidar<k>` 形式或只有一个时与离线一致，否则在
    `finalize` 中检出并追赶）。"""

    def __init__(self, out_dir: Path, *, run_id: str, world: str, event: str, profile: str = "review", eval_out: Path | None = None,
                 layers: Iterable[str] | None = None, uavs: Iterable[str] | None = None, main_hz: int | None = None,
                 cams: Iterable[str] | None = None, lidars: Iterable[str] | None = None, mode: str = "process", queue_msgs: int = 256,
                 on_full: str = "catchup", far_m: float = DEFAULT_FAR_M, optimize: bool = True) -> None:
        self.sdk = require_sdk()
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}; one of {sorted(PROFILES)}")
        if mode not in ("process", "inline"):
            raise ValueError(f"mode must be 'process' or 'inline', not {mode!r}")
        if on_full not in ("catchup", "block"):
            raise ValueError(f"on_full must be 'catchup' or 'block', not {on_full!r}")
        self.prof = PROFILES[profile]
        self.layers = tuple(layers) if layers else self.prof.layers
        bad = [x for x in self.layers if x not in self.prof.layers]
        if bad:
            raise ValueError(f"profile {profile!r} does not carry layers {bad} (allowed {list(self.prof.layers)})")
        self.out = Path(out_dir)
        self.ev_out = Path(eval_out) if eval_out else self.out
        self.run_id, self.world, self.event = str(run_id), str(world), str(event)
        from awr.bench.recorder.rerun_export import application_id

        self.app_id = application_id(self.world, self.event)
        self.only_uavs = set(uavs) if uavs else None
        self.main_hz = int(main_hz) if main_hz else None
        self.fixed = {"cam": sensor_map(cams, "cam") if cams else None, "lidar": sensor_map(lidars, "lidar") if lidars else None}
        self.names: dict[str, set[str]] = {"cam": set(), "lidar": set()}
        self.maps: dict[str, dict[str, str]] = {"cam": dict(self.fixed["cam"] or {}), "lidar": dict(self.fixed["lidar"] or {})}
        self.map_history: list[dict[str, dict[str, str]]] = []
        self.mode, self.queue_msgs, self.on_full, self.far_m, self.optimize = mode, int(queue_msgs), on_full, float(far_m), bool(optimize)
        self._lock = threading.RLock()
        self._cid: dict[tuple[str, int], str] = {}
        self._info: dict[tuple[str, str], dict] = {}
        self._shards: dict[str, _Inline | _Remote] = {}
        self._sent_chan: dict[str, set[tuple[str, str]]] = {}
        self._topics_of: dict[str, set[tuple[str, str]]] = {}
        self._last_t: dict[tuple[str, str], int] = {}
        self._ooo: set[tuple[str, str]] = set()
        self.catchup: dict[str, str] = {}
        self.errors: list[str] = []
        self._all_catchup: str | None = None
        self.error: str | None = None
        self.attachments: dict[str, bytes] = {}
        self.closed_sources: set[str] = set()
        self.attached: list[str] = []
        self.n_messages = 0
        self.t_wall0 = time.perf_counter()
        self.finalized: dict | None = None

    # ------------------------------------------------------------------ 接入
    def attach(self, run_writer, source: str) -> OnlineRrd:
        """`source` 为 "raw"（raw.mcap）或 "truth"（truth/run_<id>.mcap）；须在该写入器写出第一条消息前接入。"""
        if source not in ("raw", "truth"):
            raise ValueError(f"source must be 'raw' or 'truth', not {source!r}")
        run_writer._w = McapTee(run_writer._w, self, source)
        self.attached.append(source)
        return self

    def _safe(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:                               # 旁路异常不影响 MCAP；全部分片改由离线追赶
            with self._lock:
                self.errors.append(f"{fn.__name__}: {e!r}")
                self._all_catchup = self._all_catchup or f"tap error: {e!r}"

    # ------------------------------------------------------------------ 旁路事件（写线程，持锁）
    def _tap_attachment(self, source: str, name: str, data: bytes) -> None:
        with self._lock:
            if name == "run_spec.json" and self.main_hz is None:
                try:
                    hz = (json.loads(bytes(data)).get("clock_profile") or {}).get("main_hz")
                    self.main_hz = int(hz) if hz else None
                except (ValueError, AttributeError):
                    pass
            self.attachments[name] = b"" if len(data) > 1 << 20 else bytes(data)

    def _tap_channel(self, source: str, cid: int, topic: str, encoding: str, schema: str | None, meta: dict) -> None:
        with self._lock:
            self._cid[(source, cid)] = topic
            self._info[(source, topic)] = {"encoding": encoding, "schema": schema, "metadata": meta}
            if source == "raw":
                mc = CAMERA_TOPIC.match(topic)
                if mc:
                    self._name("cam", mc.group(2))
                ml = LIDAR_TOPIC.match(topic)
                if ml:
                    self._name("lidar", ml.group(2))
                mu = _UAV.match(topic)
                if mu and "obs" in self.layers and self._want(mu.group(1)):
                    self._shard(f"obs.{path_part(mu.group(1))}", uav=mu.group(1))

    def _want(self, uav: str) -> bool:
        return self.only_uavs is None or uav in self.only_uavs

    def _name(self, kind: str, name: str) -> None:
        if name in self.names[kind]:
            return
        self.names[kind].add(name)
        if self.fixed[kind] is not None:
            return
        new = sensor_map(self.names[kind], kind)
        if new != self.maps[kind]:
            self.maps[kind] = new
            self.map_history.append({"cam": dict(self.maps["cam"]), "lidar": dict(self.maps["lidar"])})
            for key in list(self._shards):
                self._put(key, ("map", self.maps["cam"], self.maps["lidar"]))

    def _hz(self) -> int:
        if self.main_hz is None:
            self.main_hz = 250
        return self.main_hz

    def _shard(self, key: str, *, uav: str | None = None) -> _Inline | _Remote:
        h = self._shards.get(key)
        if h is not None:
            return h
        kind = "obs" if key.startswith("obs.") else key
        path = (self.out / f"{key}.rrd") if kind == "obs" else (self.ev_out / f"{kind}.rrd")
        init = {"key": key, "kind": kind, "path": str(path), "app_id": self.app_id, "run_id": self.run_id, "main_hz": self._hz(),
                "profile": self.prof.name, "far_m": self.far_m, "uav": uav, "uavs": sorted(self.only_uavs) if self.only_uavs else None, "cams": self.maps["cam"],
                "lidars": self.maps["lidar"]}
        h = _Inline(init) if self.mode == "inline" else _Remote(init, queue_msgs=self.queue_msgs, on_full=self.on_full)
        self._shards[key] = h
        self._sent_chan[key] = set()
        self._topics_of[key] = set()
        return h

    def _put(self, key: str, item: tuple) -> None:
        if key in self.catchup:
            return
        if not self._shards[key].put(item):
            self.catchup[key] = f"online queue full ({self.queue_msgs} items); derived from MCAP at finalize"

    def _send(self, key: str, source: str, topic: str, item: tuple, *, uav: str | None = None) -> None:
        self._shard(key, uav=uav)
        if (source, topic) not in self._sent_chan[key]:
            self._sent_chan[key].add((source, topic))
            self._put(key, ("c", topic, self._info.get((source, topic), {})))
        self._topics_of[key].add((source, topic))
        self._put(key, item)

    def _tap_message(self, source: str, cid: int, t: int, seq: int, data: bytes) -> None:
        with self._lock:
            topic = self._cid.get((source, cid))
            if topic is None:
                return
            self.n_messages += 1
            k = (source, topic)
            if t < self._last_t.get(k, t):
                self._ooo.add(k)
            self._last_t[k] = t
            if source == "raw":
                self._route_raw(topic, t, seq, data)
            else:
                self._route_truth(topic, t, seq, data)

    def _route_raw(self, topic: str, t: int, seq: int, data: bytes) -> None:
        m = ("m", topic, t, seq, data)
        if topic == "/net/records":
            if "eval" in self.layers:
                self._send("eval", "raw", topic, m)
            return
        mu = _UAV.match(topic)
        if not mu:
            return
        uav = mu.group(1)
        if topic.endswith("/obs/detections"):
            for d in json.loads(data) or []:
                if isinstance(d, dict) and d.get("sensor_id"):
                    self._name("cam", str(d["sensor_id"]))
        obs = (topic.endswith(("/obs/nav", "/obs/detections")) or CAMERA_TOPIC.match(topic) or LIDAR_TOPIC.match(topic)
               or _OBS_MISC.match(topic) or _CALIB.match(topic))
        if obs and "obs" in self.layers and self._want(uav):
            self._send(f"obs.{path_part(uav)}", "raw", topic, m, uav=uav)
        if LIDAR_TOPIC.match(topic) and "truth" in self.layers:
            self._send("truth", "raw", topic, m)

    def _route_truth(self, topic: str, t: int, seq: int, data: bytes) -> None:
        m = ("m", topic, t, seq, data)
        if _STATE.match(topic) or TRUTH_LIDAR_TOPIC.match(topic):
            if "truth" in self.layers:
                self._send("truth", "truth", topic, m)
            return
        mc = TRUTH_CAMERA_TOPIC.match(topic)
        if mc:
            uav, cam = mc.group(1), mc.group(2)
            if "truth" in self.layers:
                self._send("truth", "truth", topic, m)
            if "eval" in self.layers or "obs" in self.layers:
                rec = _gt_json(bytes(data))
                if "eval" in self.layers:
                    self._send("eval", "truth", topic, ("gt", uav, t, (rec.get("ground_truth") or {}).get("objects") or []))
                if "obs" in self.layers and self._want(uav):        # 观测分片只取 agent_view（交付给 Agent 的字段）
                    self._send(f"obs.{path_part(uav)}", "truth", topic, ("av", cam, t, rec.get("agent_view") or {}), uav=uav)
            return
        if _VIS.match(topic) and "eval" in self.layers:
            self._send("eval", "truth", topic, m)

    def _tap_close(self, source: str) -> None:
        with self._lock:
            self.closed_sources.add(source)

    # ------------------------------------------------------------------ 结束
    def _check(self, src: RunSource) -> None:
        """以运行结束后的 `RunSource` 复核在线所用的身份、tick 频率与传感器映射；不一致的分片改由离线追赶。"""
        def all_(why: str) -> None:
            for k in self._expected(src):
                self.catchup.setdefault(k, why)

        if self._all_catchup:
            all_(self._all_catchup)
        if src.app_id != self.app_id or src.run_id != self.run_id:
            all_(f"identity mismatch: online {self.app_id}/{self.run_id}, run {src.app_id}/{src.run_id}")
        if self.main_hz is not None and src.timebase.main_hz != self.main_hz:
            all_(f"tick rate mismatch: online {self.main_hz} Hz, run spec {src.timebase.main_hz} Hz")
        final = {"cam": src.cams, "lidar": src.lidars}
        for snap in [*self.map_history, self.maps]:
            for kind in ("cam", "lidar"):
                diff = {n: k for n, k in snap[kind].items() if final[kind].get(n) != k}
                if diff:
                    all_(f"sensor map changed during the run ({kind}: online {diff}, final {final[kind]})")
        for key, topics in self._topics_of.items():                # 评测分片排序后写出，不受逆序影响
            bad = sorted(t for s, t in topics if (s, t) in self._ooo)
            if bad and key != "eval":
                self.catchup.setdefault(key, f"log_time decreased on {bad[:4]}")

    def _expected(self, src: RunSource) -> list[str]:
        keys = []
        if "obs" in self.layers:
            keys += [f"obs.{path_part(u)}" for u in src.uavs if self._want(u)]
        keys += [k for k in ("truth", "eval") if k in self.layers]
        return keys

    def _fin(self, src: RunSource, key: str) -> dict:
        if key.startswith("obs."):
            uav = next(u for u in src.uavs if f"obs.{path_part(u)}" == key)
            cams = {}
            for cam, ck in sorted(src.cams.items()):
                if not any(t.startswith(f"/uav/{uav}/camera/{cam}/") for t in src.channels):
                    continue
                if f"/uav/{uav}/camera/{cam}/calib" in src.channels:
                    how = "calib"
                elif src.truth is not None and f"/eval/hidden/camera/{uav}/{cam}" in src.truth_channels:
                    how = "agent_view"
                else:
                    how = "none"
                meta = src.channels.get(f"/uav/{uav}/camera/{cam}/image", {}).get("metadata", {})
                cams[cam] = {"key": ck, "calib": how, "period_ns": int(meta.get("period_ns", 100_000_000))}
                if how == "none" or (how == "agent_view" and not self._has_av(src, uav, cam)):
                    msg = f"{uav}/{cam}: no camera calibration (calib channel or agent_view); Pinhole omitted"
                    if msg not in src.warnings:
                        src.warnings.append(msg)
            return {"props": src.properties("obs", uav), "cams": cams}
        return {"props": src.properties(key)}

    @staticmethod
    def _has_av(src: RunSource, uav: str, cam: str) -> bool:
        return src.truth_channels.get(f"/eval/hidden/camera/{uav}/{cam}", {}).get("count", 0) > 0

    def abort(self) -> None:
        """运行失败时关闭全部分片（不写清单）。"""
        with self._lock:
            for h in self._shards.values():
                h.finish(None)
            self._shards.clear()

    def finalize(self, run_dir: Path, *, verify: bool = False) -> dict:
        """运行结束（两个 MCAP 已关闭、`manifest.json` 已写出）后调用：补写段属性与相机标定、关闭分片、按需离线追赶、`rerun rrd
        optimize`，写 `export.json`；`verify` 时逐文件执行 `lint_rrd` 与 `rerun rrd verify`。可重复调用（返回首次结果）。"""
        if self.finalized is not None:
            return self.finalized
        t0 = time.perf_counter()
        run_dir = Path(run_dir)
        with self._lock:
            try:
                src = RunSource.open(run_dir)
            except RerunExportError:
                src = RunSource.open(run_dir, world=self.world, event=self.event, run_id=self.run_id)
            self._check(src)
            expected = self._expected(src)
            for key in expected:                              # 没有收到任何消息的层（如无真值文件）：与离线一样只含静态实体
                if key not in self._shards and key not in self.catchup:
                    uav = next((u for u in src.uavs if f"obs.{path_part(u)}" == key), None)
                    self._shard(key, uav=uav)
            files: dict[str, dict] = {}
            for key in sorted(self._shards):
                h = self._shards[key]
                if key not in expected:
                    h.finish(None)
                    self.catchup.pop(key, None)
                    continue
                if key in self.catchup:
                    h.finish(None)
                    continue
                status, res = h.finish(self._fin(src, key))
                if status != "ok":
                    self.catchup[key] = f"shard failed: {str(res).strip().splitlines()[0] if res else status}"
                    continue
                res["online"].update(h.stats())
                if res["online"]["skipped_names"] and any(n in src.cams or n in src.lidars for n in res["online"]["skipped_names"]):
                    self.catchup[key] = f"sensors {res['online']['skipped_names']} were unknown when their messages arrived"
                elif res["online"]["inexact"]:
                    self.catchup[key] = "; ".join(res["online"]["inexact"])[:400]
                else:
                    files[key] = res
            self._shards.clear()
        ident = {"world": src.world, "event": src.event, "run_id": src.run_id}
        sel = [u for u in src.uavs if self._want(u)]
        for key in sorted(self.catchup):
            if key not in expected:
                continue
            kind = "obs" if key.startswith("obs.") else key
            uav = next((u for u in src.uavs if f"obs.{path_part(u)}" == key), None)
            path = (self.out / f"{key}.rrd") if kind == "obs" else (self.ev_out / f"{kind}.rrd")
            st, wn = _job((kind, str(run_dir), ident, uav, str(path), self.prof.name, None, self.far_m, sel))
            st["online"] = {"catchup": self.catchup[key]}
            files[key] = st
            src.warnings += [x for x in wn if x not in src.warnings]
        self.catchup = {k: v for k, v in self.catchup.items() if k in expected}
        if self.optimize:
            for f in files.values():
                t_o = time.perf_counter()
                optimize_rrd(Path(f["path"]))
                f["bytes"] = Path(f["path"]).stat().st_size
                f["optimize_s"] = round(time.perf_counter() - t_o, 3)
        sim_s = max(src.t_range[1] - src.t_range[0], 0) / 1e9
        wall = time.perf_counter() - self.t_wall0
        man = {"schema": EXPORT_SCHEMA, "writer": "online", "run_id": src.run_id, "application_id": src.app_id, "recording_id": src.run_id,
               "world": src.world, "event": src.event, "profile": asdict(self.prof), "rerun_version": self.sdk, "rerun_locked": RERUN_LOCKED,
               "layers": list(self.layers), "uavs": sel, "window_ns": None, "timelines": list(TIMELINES), "tick_hz": src.timebase.main_hz,
               "sensor_map": {"cam": src.cams, "lidar": src.lidars}, "files": files, "warnings": list(src.warnings), "sim_s": round(sim_s, 3),
               "wall_s": round(wall, 3), "realtime_factor_min_shard": None, "created_unix_ns": time.time_ns(),
               "online": {"mode": self.mode, "queue_msgs": self.queue_msgs, "on_full": self.on_full, "attached": sorted(set(self.attached)),
                          "messages": self.n_messages, "catchup": dict(sorted(self.catchup.items())), "errors": self.errors,
                          "optimized": self.optimize, "finalize_s": round(time.perf_counter() - t0, 3)}}
        if verify:
            problems: dict[str, list[str]] = {}
            for k, f in files.items():
                p = Path(f["path"])
                errs = lint_rrd(p, "obs" if k.startswith("obs.") else k, uav=f.get("uav"), run_id=src.run_id, app_id=src.app_id)
                ok, msg = verify_rrd([p])
                if not ok:
                    errs.append(f"rerun rrd verify: {msg.strip()}")
                if errs:
                    problems[k] = errs
            man["verify"] = {"ok": not problems, "problems": problems}
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "export.json").write_text(json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
        self.finalized = man
        if verify and not man["verify"]["ok"]:
            raise RerunExportError(f"online rrd failed verification: {man['verify']['problems']}", man["verify"])
        return man


# ---------------------------------------------------------------------- Env 接入与重放


def attach_env(recorder, out_dir: Path | None = None, *, auto_finalize: bool = True, verify: bool = False, **kw) -> OnlineRrd:
    """为 `EnvRecorder` 接入在线写入：身份与 tick 频率取自 EnvSpec 与磁带（与运行清单一致），接到 raw 与 truth 两个写入器；
    `auto_finalize` 时在 `manifest.json` 写出后执行 `finalize`（异常记入 `online.error`，不影响运行收尾）。须在第一次观测交付前调用。"""
    sp, tape = recorder.spec, recorder.tape
    if recorder.dir is None or recorder._raw is None:
        raise ValueError("EnvRecorder has no MCAP writers (run_dir or AWR_REC_MCAP disabled)")
    world = Path(sp.world_dir).name
    event = tape.header["event_id"] if tape is not None else kw.pop("event")
    hz = int((sp.resolved().get("clock_profile") or {}).get("main_hz") or 250)
    on = OnlineRrd(Path(out_dir) if out_dir else recorder.dir / "derived" / "rrd", run_id=recorder.run_id, world=world, event=event,
                   main_hz=hz, **kw)
    on.attach(recorder._raw, "raw")
    on.attach(recorder._truth, "truth")
    if auto_finalize:
        orig = recorder.write_json

        def write_json(rel: str, obj) -> None:
            orig(rel, obj)
            if rel == "manifest.json":
                try:
                    on.finalize(recorder.dir, verify=verify)
                except Exception as e:                       # 在线派生失败不影响运行；可由 --from-mcap 离线导出
                    on.error = f"{e!r}"

        recorder.write_json = write_json
    return on


def replay_run(run_dir: Path, online: OnlineRrd, *, pace: float | None = None) -> dict:
    """按文件顺序把已有运行的 raw 与真值 MCAP 重放到在线写入器（两文件按 log_time 交错，模拟两个写线程并发）；`pace` 为仿真时间
    相对墙钟的倍率（1.0 = 实时，None = 不限速）。返回重放统计。"""
    from mcap.reader import make_reader

    run_dir = Path(run_dir)
    src = RunSource.open(run_dir)
    files = [("raw", src.raw)] + ([("truth", src.truth)] if src.truth is not None else [])
    with contextlib.ExitStack() as stack:
        readers = [make_reader(stack.enter_context(Path(p).open("rb"))) for _s, p in files]
        for (s, _p), r in zip(files, readers, strict=True):
            online.attached.append(f"replay:{s}")
            for a in r.iter_attachments():
                online._tap_attachment(s, a.name, bytes(a.data))

        def stream(source: str, r):
            for sch, ch, m in r.iter_messages(log_time_order=False):
                yield int(m.log_time), source, sch, ch, m

        seen: set[tuple[str, int]] = set()
        n, t0w, t0s = 0, time.perf_counter(), None
        for t, source, sch, ch, m in heapq.merge(*(stream(s, r) for (s, _p), r in zip(files, readers, strict=True)), key=lambda x: x[0]):
            if (source, ch.id) not in seen:
                seen.add((source, ch.id))
                online._tap_channel(source, ch.id, ch.topic, ch.message_encoding, sch.name if sch else ch.metadata.get("schema"),
                                    dict(ch.metadata))
            if pace:
                t0s = t if t0s is None else t0s
                lag = (t - t0s) / 1e9 / pace - (time.perf_counter() - t0w)
                if lag > 0:
                    time.sleep(lag)
            online._tap_message(source, ch.id, t, int(m.sequence), bytes(m.data))
            n += 1
        for s, _p in files:
            online._tap_close(s)
        return {"messages": n, "wall_s": round(time.perf_counter() - t0w, 3), "pace": pace}


# ---------------------------------------------------------------------- 归一化与对比


def optimize_rrd(path: Path, out: Path | None = None, *, canonical: bool = False) -> Path:
    """`rerun rrd optimize`（§6.1.2 运行结束步骤；缺省 object-store 档）；`out` 缺省时原地替换。

    `canonical` 时取消压实上限（`CANONICAL_LIMITS`），每个实体的同一分量与时间轴组合合并为一个 chunk（视频按 GOP 重新分批），结果与
    输入的 chunk 划分无关，作为 `rerun rrd compare` 的归一化：缺省上限下体量接近上限的行（如 L2-depth 深度图）的分组取决于输入顺序，
    实测在线与离线的深度实体在缺省上限下为 12 与 14 个 chunk。"""
    path = Path(path)
    dst = Path(out) if out else path.with_suffix(".opt.tmp.rrd")
    dst.parent.mkdir(parents=True, exist_ok=True)
    lim = list(CANONICAL_LIMITS) if canonical else []
    p = subprocess.run([_rerun_cli(), "rrd", "optimize", *lim, str(path), "-o", str(dst)], capture_output=True, text=True, timeout=3600,
                       env=_cli_env())
    if p.returncode != 0 or not dst.is_file():
        raise RerunExportError(f"rerun rrd optimize failed for {path}: {(p.stdout + p.stderr).strip()[-500:]}")
    if out is None:
        dst.replace(path)
        return path
    return dst


def _cell_digest(a) -> str:
    """一个分量单元（长度 1 的 Arrow 数组切片）的内容摘要：列表长度、结构字段名与叶子缓冲按序计入；不经 Python 对象（大图像与点云
    逐元素转换过慢）。"""
    import pyarrow as pa
    import pyarrow.compute as pc

    h = hashlib.sha256()

    def walk(x) -> None:
        t = x.type
        h.update(str(t).encode())
        if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_fixed_size_list(t):
            h.update(np.asarray(pc.list_value_length(x).fill_null(-1), np.int64).tobytes())
            walk(x.flatten())
        elif pa.types.is_struct(t):
            for f, c in zip(t, x.flatten(), strict=True):
                h.update(f.name.encode())
                walk(c)
        elif pa.types.is_primitive(t) and not pa.types.is_boolean(t):
            h.update(np.asarray(x.is_valid(), bool).tobytes())
            h.update(x.fill_null(0).to_numpy(zero_copy_only=False).tobytes())
        else:
            h.update(json.dumps(x.to_pylist(), sort_keys=True, default=repr).encode())

    walk(a)
    return h.hexdigest()


def entity_digest(path: Path, entities: Iterable[str] | None = None) -> dict[str, dict]:
    """逐实体行摘要：{实体: {"rows": 行数, "sha256": 全部行（sim_time 与各非空分量的内容摘要，排序后）的摘要}}。同一实体在两个
    文件中的摘要相同即该实体的行集合相同（与 chunk 划分及行序无关；RowId 与 chunk id 不计入）。"""
    from rerun.chunk import RrdReader

    want = set(entities) if entities is not None else None
    rows: dict[str, list[str]] = {}
    for ch in RrdReader(str(path)).store().stream().to_chunks():
        e = ch.entity_path
        if e == "/__properties" or e.startswith("/__properties/") or (want is not None and e not in want):
            continue
        rb = ch.to_record_batch()
        names = [n for n in rb.schema.names if n not in ("rerun.controls.RowId", "sim_time", "tick")]
        ts = rb.column("sim_time").cast("int64").to_pylist() if "sim_time" in rb.schema.names else [None] * rb.num_rows
        tk = rb.column("tick").cast("int64").to_pylist() if "tick" in rb.schema.names else [None] * rb.num_rows
        cols = [(n, rb.column(n)) for n in names]
        out = rows.setdefault(e, [])
        for i in range(rb.num_rows):
            cells = {n: _cell_digest(c.slice(i, 1)) for n, c in cols if c[i].is_valid}
            if cells:
                out.append(json.dumps([ts[i], tk[i], cells], sort_keys=True, separators=(",", ":")))
    res = {}
    for e in sorted(rows if want is None else want):
        r = sorted(rows.get(e, []))
        res[e] = {"rows": len(r), "sha256": hashlib.sha256("\n".join(r).encode()).hexdigest()}
    return res


def compare_online(run_dir: Path, online_dir: Path, *, work_dir: Path, entities: bool = True, eval_dir: Path | None = None) -> dict:
    """D3-AC-20 判据：以在线清单的档位、层与机队从同一 MCAP 离线导出，两侧经 `optimize_rrd(canonical=True)` 归一化 chunk 后逐文件
    `rerun rrd compare --unordered`；`entities` 时再按实体比较行读出摘要（与 chunk 划分无关的第二判据）。返回
    {"ok", "missing", "files": {键: {...}}}。"""
    online_dir, work_dir = Path(online_dir), Path(work_dir)
    on = json.loads((online_dir / "export.json").read_text(encoding="utf-8"))
    off = export_run(Path(run_dir), work_dir / "offline", profile=on["profile"]["name"], layers=on["layers"], uavs=on["uavs"])
    res: dict[str, dict] = {}
    for key, f in sorted(on["files"].items()):
        a = Path(f["path"]) if Path(f["path"]).is_file() else (eval_dir or online_dir) / Path(f["path"]).name
        a = optimize_rrd(a, work_dir / "online-canon" / a.name, canonical=True)
        b = optimize_rrd(Path(off["files"][key]["path"]), work_dir / "offline-canon" / Path(off["files"][key]["path"]).name, canonical=True)
        ok, msg = compare_rrd(a, b)
        r = {"compare": ok, "online": str(a), "offline": str(b), "catchup": on["online"]["catchup"].get(key)}
        if not ok:
            r["compare_output"] = msg[-2000:]
        if entities:
            da, db = entity_digest(a), entity_digest(b)
            diff = sorted(e for e in set(da) | set(db) if da.get(e) != db.get(e))
            r.update(entities=len(set(da) | set(db)), entities_equal=len(set(da) | set(db)) - len(diff), entity_mismatch=diff[:50],
                     rows=sum(x["rows"] for x in da.values()))
        res[key] = r
    missing = sorted(set(off["files"]) - set(on["files"]))
    ok = not missing and all(r["compare"] and not r.get("entity_mismatch") for r in res.values())
    return {"ok": ok, "missing": missing, "files": res, "offline_export_wall_s": off["wall_s"]}


# ---------------------------------------------------------------------- 命令行


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m awr.bench.recorder.rerun_online", description="online .rrd writer (AWR-05 §6.1.2)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("replay", help="replay an existing run through the online writer")
    rp.add_argument("--run", type=Path, required=True)
    rp.add_argument("--out", type=Path, required=True)
    rp.add_argument("--profile", default="review", choices=sorted(PROFILES))
    rp.add_argument("--layers", default=None)
    rp.add_argument("--mode", default="process", choices=("process", "inline"))
    rp.add_argument("--queue-msgs", type=int, default=256)
    rp.add_argument("--on-full", default="catchup", choices=("catchup", "block"))
    rp.add_argument("--pace", type=float, default=None, help="sim-time / wall-time ratio (1.0 = real time; default unpaced)")
    rp.add_argument("--compare", action="store_true", help="export offline from the same MCAP and compare (D3-AC-20)")
    rp.add_argument("--work", type=Path, default=None)
    rp.add_argument("--verify", action="store_true")
    cp = sub.add_parser("compare", help="compare an online output directory with an offline export of the same run")
    cp.add_argument("--run", type=Path, required=True)
    cp.add_argument("--online", type=Path, required=True)
    cp.add_argument("--work", type=Path, default=None)
    cp.add_argument("--no-entities", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "replay":
            src = RunSource.open(a.run)
            on = OnlineRrd(a.out, run_id=src.run_id, world=src.world, event=src.event, profile=a.profile,
                           layers=a.layers.split(",") if a.layers else None, main_hz=src.timebase.main_hz, mode=a.mode,
                           queue_msgs=a.queue_msgs, on_full=a.on_full)
            rep = replay_run(a.run, on, pace=a.pace)
            man = on.finalize(a.run, verify=a.verify)
            out = {"ok": True, "replay": rep, "online": man["online"], "files": {k: {"path": f["path"], "bytes": f["bytes"]} for k, f in man["files"].items()},
                   "sim_s": man["sim_s"]}
            if a.compare:
                work = a.work or a.out.parent / f"{a.out.name}-compare"
                out["compare"] = compare_online(a.run, a.out, work_dir=work)
                out["ok"] = out["compare"]["ok"]
                if a.work is None:
                    shutil.rmtree(work, ignore_errors=True)
        else:
            work = a.work or a.online.parent / f"{a.online.name}-compare"
            out = {"compare": compare_online(a.run, a.online, work_dir=work, entities=not a.no_entities)}
            out["ok"] = out["compare"]["ok"]
            if a.work is None:
                shutil.rmtree(work, ignore_errors=True)
    except RerunExportError as e:
        print(json.dumps({"ok": False, "code": e.code, "error": str(e), "detail": e.detail}, ensure_ascii=False, default=str), file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0 if out["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
