#!/usr/bin/env python3
"""tools/bench/d3/publish_static.py：公开样例版本目录的制品生产（M27 publish_static；AWR-17 §18.8.5；D3-AC-21；ADR-194 决策 (5) ⑤、
ADR-201）。

公开样例只取 dev 事件（`configs/bench.d3/split.yaml` 中 `split = dev`）的已评测 Env 运行，输入为 B20 样例包安装后的运行目录（`raw.mcap`、
`derived/tracks_obs.bin`、`derived/event/`）与该运行的真值目录（`truth/fleet.npz`，只在 dmax 上读取，不进入版本目录）。四个子命令：

1. `thermal`（dmax，d3-sensorsvc 镜像，绑定一张卡）：按机体真值位姿（`truth/fleet.npz`，10 Hz，线性插值）与 Env 云台状态（`tracks_obs.bin`
   的逐 tick 俯仰与相对偏航，即 `sample_pack.gimbal_series` 的 Env 云台重放），以 M25 `therm512`（`awr.sensorsvc.thermal.render.ThermalCamera`，
   640 × 512，S 个子帧、演员分区温度、realistic 噪声档；晴天、气温 15 °C、云量 0.3、相对湿度 0.6）离线渲染每机的热成像视角，白热调色板
   编码为 H.264。相机位姿与 Env L2 可见光同一约定（`awr.agentsdk.env.l2` 的 `_cam_pose`：机体位置 + R_z(偏航)·挂载偏置，姿态
   R_z(偏航)·gimbal_R(相对偏航, 俯仰)）。表观温度窗口取全部机体抽样帧（每 `--probe-every` 帧 1 帧）的 1 % 与 99.5 % 分位，全运行固定。
   D3 的 Env L2 档不向 Agent 交付热成像（AWR-05 §10.2 P0a），因此该视频是同一飞行的热成像观测样例，不是 Agent 实际收到的观测，
   生成记录与公开页说明按此标注。噪声键取公开种子 0，不使用运行的秘密种子。
2. `preview`（同上）：Agent 收到的可见光 JPEG（`raw.mcap` 的 `/uav/<uav>/camera/<cam>/image`）缩放为 640 × 360 预览视频。
3. `assemble`：按 §18.8.5 布局组装版本目录（`LICENSE`、`NOTICE`、`events/<eid>/tape.{bin,json}`、`runs/<rid>/tracks_obs.bin`、
   `runs/<rid>/video/*.mp4`），轨迹头的 `run_id` 改写为公开运行 id，并写 `labctl.py index` 的索引草稿 `draft.json`；切片与底图由
   `public_slice.py --run-id <公开运行 id> --out <版本目录>/runs/<rid>/slices` 生成，`labctl.py index --slices` 并入。
4. `public-id`：打印内部运行 id 对应的公开运行 id：`run-` + base32(sha256("drone4d-public-run:" ‖ 内部 id)) 小写前 10 位
   （`run-[a-z2-7]{10}`，与前端解析器一致）。

视频统一为 10 Hz、PTS 为 tick 序号（自仿真时刻 0 起，无编辑表）、GOP 10 帧、无 B 帧、yuv420p、faststart（§18.8.5 规则 5）。生成记录
`<out>/publish_static.json`（`drone4d.bench.publish_static.v1`）记录输入摘要、编码参数、温度窗口、帧数与耗时。

用法：
    python tools/bench/d3/publish_static.py thermal --run RUN --truth TRUTH --world-dir W --tape TAPE --out OUT [--uav uav-01 …]
    python tools/bench/d3/publish_static.py preview --run RUN --out OUT [--size 640x360]
    python tools/bench/d3/publish_static.py assemble --run RUN --media OUT --vdir VDIR --event crop-handoff-001 --world oryahovo-crop1 \\
        --policy track_share --obs-level L2 --license LICENSE --notice NOTICE
    python tools/bench/d3/publish_static.py public-id b10-formal-r2-l2-track_share-k04-p1
退出码：0 成功；1 生成或核对失败；2 参数或输入错误。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import itertools
import json
import math
import os
import shutil
import sys
import time
from fractions import Fraction
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
for _p in (ROOT / "python", Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SCHEMA = "drone4d.bench.publish_static.v1"
NS = 1_000_000_000
TICK_NS = 100_000_000
FPS = 10
GOP = 10
MOUNT_T_BS_M = (0.12, 0.0, -0.08)                     # Env L2 相机挂载偏置（awr.agentsdk.env.camera.CameraModel.mount_t_bs_m）
THERMAL_SENSOR = "therm0"
PUBLIC_NOISE_SEED = 0
THERMAL_ENV = {"t_air_k": 288.15, "cloud": 0.3, "rh": 0.6, "fog_visibility_km": None, "rain_mmh": 0.0}
WINDOW_Q = (1.0, 99.5)
WINDOW_MIN_CK = 1000.0                                # 温度窗口最小跨度 10 K：近乎均温的画面不放大 NETD 噪声


class PublishError(Exception):
    pass


def public_run_id(internal_id: str) -> str:
    d = hashlib.sha256(f"drone4d-public-run:{internal_id}".encode()).digest()
    return "run-" + base64.b32encode(d).decode().lower()[:10]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _write_json(p: Path, doc: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


# ------------------------------------------------------------------------------------------------------------------ 编码
class FrameEncoder:
    """numpy 或 av 帧 → libx264 MP4（10 Hz、PTS 为 tick 序号、GOP 10、无 B 帧、yuv420p、faststart）。"""

    def __init__(self, path: Path, width: int, height: int, *, crf: int, preset: str, threads: int) -> None:
        import av

        self.av = av
        self.path = Path(path)
        self.tmp = self.path.with_name(self.path.name + ".part")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.size = (width - width % 2, height - height % 2)
        self.out = av.open(str(self.tmp), "w", format="mp4", options={"movflags": "+faststart"})
        st = self.out.add_stream("libx264", rate=FPS)
        st.width, st.height, st.pix_fmt = self.size[0], self.size[1], "yuv420p"
        st.codec_context.time_base = Fraction(1, FPS)
        st.codec_context.gop_size = GOP
        st.codec_context.max_b_frames = 0
        st.codec_context.thread_count = threads
        st.options = {"crf": str(crf), "preset": preset, "threads": str(threads),
                      "x264-params": f"keyint={GOP}:min-keyint={GOP}:scenecut=0:bframes=0:threads={threads}"}
        self.st = st
        self.last_pts = -1
        self.frames = 0
        self.first_pts: int | None = None

    def push(self, pts: int, frame) -> None:
        if isinstance(frame, np.ndarray):
            frame = self.av.VideoFrame.from_ndarray(frame, format="gray" if frame.ndim == 2 else "rgb24")
        pts = max(int(pts), self.last_pts + 1)                                      # 重复时刻：顺延，保持单调
        f = frame.reformat(width=self.size[0], height=self.size[1], format="yuv420p")
        f.pts, f.time_base = pts, Fraction(1, FPS)
        for pkt in self.st.encode(f):
            self.out.mux(pkt)
        if self.first_pts is None:
            self.first_pts = pts
        self.last_pts = pts
        self.frames += 1

    def close(self) -> dict:
        if not self.frames:
            raise PublishError(f"{self.path.name}: no frame")
        for pkt in self.st.encode():
            self.out.mux(pkt)
        self.out.close()
        os.replace(self.tmp, self.path)
        return {"path": self.path.name, "frames": self.frames, "first_pts": self.first_pts, "width": self.size[0], "height": self.size[1],
                "duration_s": round((self.last_pts + 1) / FPS, 3), "bytes": self.path.stat().st_size}

    def abort(self) -> None:
        with contextlib.suppress(Exception):                                       # 中止路径只做清理
            self.out.close()
        self.tmp.unlink(missing_ok=True)


def probe(path: Path) -> dict:
    """MP4 复核：帧数、尺寸、首包为关键帧、关键帧间隔与 PTS 单调。"""
    import av

    with av.open(str(path)) as c:
        st = c.streams.video[0]
        pkts = [p for p in c.demux(st) if p.size]
        pts = sorted(int(p.pts) for p in pkts)
        keys = [i for i, p in enumerate(sorted(pkts, key=lambda p: p.pts)) if p.is_keyframe]
        gaps = [b - a for a, b in itertools.pairwise(keys)]
        return {"codec": st.codec_context.name, "width": st.codec_context.width, "height": st.codec_context.height, "frames": len(pkts),
                "first_keyframe": bool(keys and keys[0] == 0), "max_gop": max(gaps) if gaps else None, "first_pts": pts[0] if pts else None,
                "monotonic": pts == sorted(set(pts))}


# ------------------------------------------------------------------------------------------------------------------ 位姿
def _rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


class FleetPoses:
    """`truth/fleet.npz`（t_ns、pos、yaw）与 `tracks_obs.bin` 的云台角 → 任意时刻的相机 (R_ws, p_w)。"""

    def __init__(self, fleet: Path, tracks: Path) -> None:
        import sample_pack as sp

        z = np.load(fleet)
        self.uavs = [str(u) for u in z["uavs"]]
        self.t = z["t_ns"].astype(np.int64)
        self.pos = z["pos"].astype(np.float64)                                      # (n, uav, 3)
        self.yaw = np.unwrap(z["yaw"].astype(np.float64), axis=0)                    # (n, uav)
        tr = sp.parse_tracks(tracks.read_bytes())
        hz = int(tr["header"]["hz"])
        if hz != FPS:
            raise PublishError(f"tracks hz {hz} != {FPS}")
        self.gt = np.arange(int(tr["header"]["n_samples"]), dtype=np.int64) * (NS // hz)
        self.gim = {u: np.array([(r[7], r[8]) for r in rows], np.float64) for u, rows in tr["rows"].items()}   # 俯仰、相对偏航（度）
        miss = [u for u in self.uavs if u not in self.gim]
        if miss:
            raise PublishError(f"tracks has no rows for {miss}")

    def pose(self, uav: str, t_ns: int) -> tuple[np.ndarray, np.ndarray]:
        from awr.sim.sensors.frames import gimbal_R

        j = self.uavs.index(uav)
        t = float(min(max(t_ns, self.t[0]), self.t[-1]))
        p = np.array([np.interp(t, self.t, self.pos[:, j, i]) for i in range(3)])
        yaw = float(np.interp(t, self.t, self.yaw[:, j]))
        g = self.gim[uav]
        pitch = float(np.interp(t, self.gt, g[:, 0]))
        yrel = float(np.interp(t, self.gt, g[:, 1]))
        Rz = _rz(yaw)
        return Rz @ gimbal_R(math.radians(yrel), math.radians(pitch)), p + Rz @ np.asarray(MOUNT_T_BS_M)


# ------------------------------------------------------------------------------------------------------------------ 热成像
class ThermalRenderer:
    """M25 `therm512` 离线渲染：演员分区温度与发动机状态沿用 `camera_throughput._ThermalRig`（同一口径）。"""

    def __init__(self, world_dir: Path, tape_path: Path) -> None:
        from types import SimpleNamespace

        import camera_throughput as ct

        from awr.sensorsvc.camera.scene import load_appearance
        from awr.sensorsvc.capture import ActorClock
        from awr.sensorsvc.thermal import render as tr
        from awr.sim.events.tape import open_tape

        world = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
        self.tape = open_tape(tape_path.parent if tape_path.is_file() else tape_path)     # open_tape 读取目录下的 tape.bin
        self.tape.check_world(world["contentVersion"])
        scene = load_appearance(world_dir)
        sun = tuple((world.get("sensorHints") or {}).get("bakedSunAzElDeg") or ()) or None
        self.tr = tr
        self.clock = ActorClock(self.tape, 0)
        job = SimpleNamespace(noise="realistic", tier="clear", t_air_k=THERMAL_ENV["t_air_k"], seed=PUBLIC_NOISE_SEED, index=0)
        w = SimpleNamespace(job=job, tape=self.tape, clock=self.clock)
        self.rig = ct._ThermalRig(w, scene, world_dir, None, sun, slots=4)
        e = THERMAL_ENV
        self.env = tr.thermal_env(self.rig.table, e["t_air_k"], cloud=e["cloud"], rh=e["rh"], fog_visibility_km=e["fog_visibility_km"],
                                  rain_mmh=e["rain_mmh"])
        self.model = self.rig.model
        self.offs = self.rig.offs
        self.n_ticks = int(self.tape.n_ticks)
        self.world = {"id": world.get("id"), "contentVersion": world["contentVersion"]}

    def frame(self, poses: FleetPoses, uav: str, uav_no: int, k: int) -> np.ndarray:
        """第 k 帧（采集窗口结束于 k·tick）的 temp_ck（u16，0.01 K）。"""
        tr = self.tr
        t_end = k * TICK_NS
        t_sub = [max(t_end + o, 0) for o in self.offs]
        ps = [poses.pose(uav, t) for t in t_sub]
        frames = [self.clock.frame(t) for t in t_sub]
        th = self.rig.actor_th(t_end)
        out = self.rig.cam.render(self.model, ps, np.asarray(t_sub, np.float64) / NS, t_end / NS, self.env, actor_frames=frames,
                                  actor_th=th, noise=tr.NoiseKeys(PUBLIC_NOISE_SEED, uav_no, 0, int(k)))
        return out.temp_ck


def white_hot(temp_ck: np.ndarray, lo: float, hi: float) -> np.ndarray:
    g = (temp_ck.astype(np.float32) - lo) / max(hi - lo, 1.0)
    return (np.clip(g, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def cmd_thermal(a) -> int:
    t0 = time.perf_counter()
    poses = FleetPoses(a.truth / "fleet.npz", a.run / "derived" / "tracks_obs.bin")
    rnd = ThermalRenderer(a.world_dir, a.tape)
    uavs = a.uav or poses.uavs
    n = min(rnd.n_ticks, len(poses.t)) if a.frames is None else int(a.frames)
    pv = np.asarray([np.percentile(rnd.frame(poses, u, poses.uavs.index(u), k), WINDOW_Q) for u in uavs for k in range(0, n, a.probe_every)])
    lo, hi = float(np.median(pv[:, 0])), float(np.median(pv[:, 1]))
    if hi - lo < WINDOW_MIN_CK:
        mid = (lo + hi) / 2.0
        lo, hi = mid - WINDOW_MIN_CK / 2.0, mid + WINDOW_MIN_CK / 2.0
    vids = {}
    for u in uavs:
        no = poses.uavs.index(u)
        enc = FrameEncoder(a.out / "video" / f"{u}_{THERMAL_SENSOR}.mp4", 640, 512, crf=a.crf, preset=a.preset, threads=a.threads)
        try:
            for k in range(n):
                enc.push(k, white_hot(rnd.frame(poses, u, no, k), lo, hi))
            vids[u] = enc.close()
        except BaseException:
            enc.abort()
            raise
        vids[u]["probe"] = probe(enc.path)
        vids[u]["sha256"] = _sha256(enc.path)
    doc = {"schema": SCHEMA, "stage": "thermal", "run": a.run.name, "world": rnd.world, "tape_sha256": _sha256(a.tape),
           "fleet_sha256": _sha256(a.truth / "fleet.npz"), "tracks_sha256": _sha256(a.run / "derived" / "tracks_obs.bin"),
           "sensor": {"profile": "therm512", "size": [640, 512], "noise": "realistic", "noise_seed": PUBLIC_NOISE_SEED, "env": THERMAL_ENV,
                      "subframe_offsets_ns": list(rnd.offs), "mount_t_bs_m": list(MOUNT_T_BS_M), "palette": "white-hot",
                      "window_ck": [lo, hi], "window_quantiles": list(WINDOW_Q), "window_min_ck": WINDOW_MIN_CK, "probe_every": a.probe_every,
                      "delivered_to_agent": False,
                      "note": "offline therm512 render along the run's truth body poses and Env gimbal state; the D3 Env L2 tier does not deliver thermal frames to agents"},
           "encoder": {"codec": "libx264", "crf": a.crf, "preset": a.preset, "gop": GOP, "bframes": 0, "fps": FPS, "pix_fmt": "yuv420p"},
           "videos": vids, "wall_s": round(time.perf_counter() - t0, 1)}
    _write_json(a.out / "thermal.json", doc)
    bad = [u for u, v in vids.items() if not (v["probe"]["first_keyframe"] and v["probe"]["monotonic"] and (v["probe"]["max_gop"] or GOP) <= GOP)]
    print(json.dumps({"ok": not bad, "window_ck": [lo, hi], "videos": {u: (v["frames"], v["bytes"]) for u, v in vids.items()}}))
    return 1 if bad else 0


# ------------------------------------------------------------------------------------------------------------------ 可见光预览
def cmd_preview(a) -> int:
    import av
    from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
    from mcap.reader import make_reader

    from awr.bench.recorder.channels import CAMERA_TOPIC

    t0 = time.perf_counter()
    w, h = (int(x) for x in a.size.split("x"))
    raw = a.run / "raw.mcap"
    enc: dict[str, FrameEncoder] = {}
    dec: dict[str, object] = {}
    names: dict[str, str] = {}
    with open(raw, "rb") as fh:
        r = make_reader(fh)
        s = r.get_summary()
        for c in s.channels.values():
            m = CAMERA_TOPIC.match(c.topic)
            if m and m.group(3) == "image" and c.metadata.get("agent_delivered", "true") == "true" and c.metadata.get("codec", "jpeg") == "jpeg":
                if int(c.metadata.get("period_ns") or TICK_NS) != TICK_NS:
                    raise PublishError(f"{c.topic}: period {c.metadata.get('period_ns')} ns != {TICK_NS}")
                names[c.topic] = f"{m.group(1)}_{m.group(2)}.mp4"
        try:
            for t in sorted(names):
                enc[t] = FrameEncoder(a.out / "video" / names[t], w, h, crf=a.crf, preset=a.preset, threads=a.threads)
                dec[t] = av.CodecContext.create("mjpeg", "r")
            for _sch, ch, msg in r.iter_messages(topics=sorted(names), log_time_order=True):
                ci = CompressedImage()
                ci.ParseFromString(msg.data)
                for fr in dec[ch.topic].decode(av.Packet(bytes(ci.data))):
                    enc[ch.topic].push(round(int(msg.log_time) / TICK_NS), fr)
            vids = {names[t]: e.close() for t, e in enc.items()}
        except BaseException:
            for e in enc.values():
                e.abort()
            raise
    for k, v in vids.items():
        v["probe"] = probe(a.out / "video" / k)
        v["sha256"] = _sha256(a.out / "video" / k)
    doc = {"schema": SCHEMA, "stage": "preview", "run": a.run.name, "raw_bytes": raw.stat().st_size, "size": [w, h],
           "encoder": {"codec": "libx264", "crf": a.crf, "preset": a.preset, "gop": GOP, "bframes": 0, "fps": FPS, "pix_fmt": "yuv420p"},
           "videos": vids, "wall_s": round(time.perf_counter() - t0, 1)}
    _write_json(a.out / "preview.json", doc)
    bad = [k for k, v in vids.items() if not (v["probe"]["first_keyframe"] and v["probe"]["monotonic"] and (v["probe"]["max_gop"] or GOP) <= GOP)]
    print(json.dumps({"ok": not bad, "videos": {k: (v["frames"], v["bytes"]) for k, v in vids.items()}}))
    return 1 if bad else 0


# ------------------------------------------------------------------------------------------------------------------ 组装
def _retag_tracks(src: Path, run_id: str) -> bytes:
    import sample_pack as sp

    t = sp.parse_tracks(src.read_bytes())
    head = t["header"]
    uavs = [{"uav_id": u["uav_id"], "primary_camera": u["primary_camera"], "rows": [r[:-1] for r in t["rows"][u["uav_id"]]]}
            for u in head["uavs"]]
    return sp.build_tracks(run_id, int(head["hz"]), int(head["n_samples"]), uavs)


def _internal_run_id(run: Path) -> str:
    """运行清单的 `run_id`（目录可能以其他名字挂载）；没有清单时取目录名。"""
    p = run / "run_manifest.json"
    if p.is_file():
        rid = json.loads(p.read_text(encoding="utf-8")).get("run_id")
        if rid:
            return str(rid)
    return run.name


def cmd_assemble(a) -> int:
    run_id = a.run_id or public_run_id(_internal_run_id(a.run))
    v = a.vdir
    if v.exists() and any(v.iterdir()) and not a.force:
        raise PublishError(f"{v} is not empty (use --force)")
    rd = v / "runs" / run_id
    ed = v / "events" / a.event
    (rd / "video").mkdir(parents=True, exist_ok=True)
    ed.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(a.license, v / "LICENSE")
    shutil.copyfile(a.notice, v / "NOTICE")
    der = a.run / "derived"
    shutil.copyfile(der / "event" / "tape.bin", ed / "tape.bin")
    shutil.copyfile(der / "event" / "tape.json", ed / "tape.json")
    (rd / "tracks_obs.bin").write_bytes(_retag_tracks(der / "tracks_obs.bin", run_id))
    videos = []
    for p in sorted((a.media / "video").glob("*.mp4")):
        uav, cam = p.stem.rsplit("_", 1)
        shutil.copyfile(p, rd / "video" / p.name)
        videos.append({"uav": uav, "cam": cam, "kind": "thermal" if cam.startswith("therm") else "rgb", "path": f"runs/{run_id}/video/{p.name}"})
    if not videos:
        raise PublishError(f"no video under {a.media / 'video'}")
    run = {"run_id": run_id, "policy": a.policy, "n_uav": len({x["uav"] for x in videos}), "obs_level": a.obs_level,
           "tracks_obs": f"runs/{run_id}/tracks_obs.bin", "videos": videos}
    if (rd / "slices" / "basemap.rrd").is_file():
        run["basemap"] = f"runs/{run_id}/slices/basemap.rrd"
    draft = {"license": {"files": ["LICENSE", "NOTICE"], "attribution": list(a.attribution)},
             "worlds": [{"id": a.world}],
             "events": [{"id": a.event, "version": a.event_version, "world_id": a.world, "tape": f"events/{a.event}/tape.bin",
                         "tape_json": f"events/{a.event}/tape.json", "runs": [run]}]}
    _write_json(a.draft or (v.parent / f"{v.name}.draft.json"), draft)
    print(json.dumps({"ok": True, "run_id": run_id, "videos": len(videos), "draft": str(a.draft or (v.parent / f"{v.name}.draft.json"))}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/bench/d3/publish_static.py", description="public sample artifacts for /lab-static/ (AWR-17 §18.8.5)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    enc = argparse.ArgumentParser(add_help=False)
    enc.add_argument("--crf", type=int, default=30)
    enc.add_argument("--preset", default="medium")
    enc.add_argument("--threads", type=int, default=4)
    p = sub.add_parser("thermal", parents=[enc])
    p.add_argument("--run", type=Path, required=True, help="installed sample run directory (derived/tracks_obs.bin)")
    p.add_argument("--truth", type=Path, required=True, help="truth directory of the source run (fleet.npz)")
    p.add_argument("--world-dir", type=Path, required=True)
    p.add_argument("--tape", type=Path, required=True, help="event tape.bin of the run")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--uav", action="append", default=None)
    p.add_argument("--frames", type=int, default=None, help="only the first N frames (smoke)")
    p.add_argument("--probe-every", type=int, default=50)
    p = sub.add_parser("preview", parents=[enc])
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--size", default="640x360")
    p = sub.add_parser("assemble")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--media", type=Path, required=True, help="directory holding video/*.mp4 from thermal and preview")
    p.add_argument("--vdir", type=Path, required=True)
    p.add_argument("--run-id", default=None)
    p.add_argument("--event", required=True)
    p.add_argument("--event-version", type=int, default=1)
    p.add_argument("--world", required=True)
    p.add_argument("--policy", required=True)
    p.add_argument("--obs-level", required=True)
    p.add_argument("--license", type=Path, required=True)
    p.add_argument("--notice", type=Path, required=True)
    p.add_argument("--attribution", action="append", default=[])
    p.add_argument("--draft", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("public-id")
    p.add_argument("internal_id")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "public-id":
            print(public_run_id(a.internal_id))
            return 0
        return {"thermal": cmd_thermal, "preview": cmd_preview, "assemble": cmd_assemble}[a.cmd](a)
    except (PublishError, FileNotFoundError, KeyError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
