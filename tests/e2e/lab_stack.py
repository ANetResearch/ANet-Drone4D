"""D3 数据检视的 Playwright 测试栈（R07：`lab/inspect`、`lab/public-samples`；D3-AC-25、D3-AC-21）。

子命令（由 tests/e2e/labStack.ts 以 `.venv-d3` 拉起，结束时以 SIGTERM 停止并删除临时目录）：

- `serve-internal`：内部工作区同源栈，一个端口上同时提供
  - 测试构建的静态页面（`VITE_AWR_TEST_SWITCHES=1`、lab 路由组；SPA 回退到 index.html）；
  - bench-api（`/api/bench/v1/**`，真实应用：会话 Cookie、BN-43 运行详情、BN-52 事件磁带、BN-57 viewer_url 下发 `d3_rr`）；
  - rerun-proxy（`/rerun/**` 与 `/rerun.cloud.v1alpha1.RerunCloudService/**`，真实应用：只认 `d3_rr` Cookie 与 Bearer 短期令牌）；
  - 上游为两个真实的 Rerun 0.38.1 OSS catalog 服务（分发与评测），运行以 `register_run` 注册（分发只含观测层，评测另含真值层）。
  运行数据：`--export <目录>` 给出真实导出（`export.json` 与 `obs.<uav>.rrd`，如 dmax lab 部署的 cap-crop-3uav-demo），缺省以
  `LayerWriter` 合成 3 机 30 s 观测层。评测 catalog 的真值层取 golden 磁带的脚本演员（`/truth/actors/<id>`）。
  就绪后写 `<root>/ready.json`（0600）：`{url, run_id, actors[], uavs[], tokens{researcher, evaluator}}`。令牌只在本临时数据根有效。
- `make-public-site`：/lab-static/ 夹具（lab_static_index.v1）：golden 磁带、3 机 × [RGB、热成像] H.264 视频（帧顶 16 位条码编码帧号）、
  `tracks_obs.bin`、public 档切片与 basemap.rrd。`--slices <目录>` 给出 R04 `public_slice` 的真实输出（`slices.json`），缺省合成。
- `serve-public`：/lab/ 公开构建（SPA 回退）与 /lab-static/（单区间 Range）；全部请求逐行写入 `--log`（方法、路径、状态），
  其余路径一律 404（与 emax nginx 的 /lab/ 站点同一口径：没有 API 与 catalog）。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
from contextlib import AsyncExitStack
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
TAPE = ROOT / "packages" / "contracts" / "golden" / "event" / "tape-replay.bin"
EVENT = "crop-handoff-001"
WORLD = "oryahovo-crop1"
UAVS = ("uav-01", "uav-02", "uav-03")
SYN_RUN = "run-r07synth01"
SYN_APP = "drone4d-d3-oryahovo-crop1-r07-synth"
PUBLIC_RUN_REAL = "run-rsevenreal"        # lab_static_index.v1 的运行 id 形如 run-[a-z2-7]{10}
PUBLIC_RUN_SYNTH = "run-rsevensynt"
ORPHAN_RUN = "run-r07nocat01"          # 有运行目录、未注册到 catalog：BN-57 404 305（面板 ERROR 外壳）
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
         ".json": "application/json", ".wasm": "application/wasm", ".mp4": "video/mp4", ".png": "image/png", ".svg": "image/svg+xml",
         ".rrd": "application/octet-stream", ".bin": "application/octet-stream", ".woff2": "font/woff2", ".ico": "image/x-icon",
         ".webmanifest": "application/manifest+json"}


def _orbit(i: int, t: float) -> tuple[float, float]:
    a = 2 * math.pi * t / 60 + i * 2 * math.pi / 3
    return 60 * math.cos(a), 60 * math.sin(a)


# ── 内部工作区栈 ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _synth_export(d: Path) -> Path:
    """合成运行：3 机 × 30 s 观测层（10 Hz 位姿、`lidar0` 点云与 1 Hz 的 `cam0` 纹理图像），`recording_id = run_id`。实体路径按
    AWR-05 §6.1.3 的观测层白名单（注册时校验）。"""
    import numpy as np
    import rerun as rr

    from awr.bench.recorder import rerun_export as rx

    d.mkdir(parents=True, exist_ok=True)
    files = {}
    for i, u in enumerate(UAVS):
        p = d / f"obs.{u}.rrd"
        w = rx.LayerWriter(p, app_id=SYN_APP, recording_id=SYN_RUN, layer="obs",
                           properties={"run_id": SYN_RUN, "layer": "obs", "uav": u, "rerun_version": rx.sdk_version()}, timebase=rx.Timebase(250))
        w.static("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP)
        ring = np.array([[3 * math.cos(a), 3 * math.sin(a), 0.0] for a in np.linspace(0, 2 * math.pi, 24, endpoint=False)], np.float32)
        w.static(f"/fleet/{u}/lidar0", rr.Points3D(ring, radii=0.5))
        rng = np.random.default_rng(i)
        for k in range(301):
            x, y = _orbit(i, k / 10)
            w.transform(f"/fleet/{u}", k * 100_000_000, [x, y, 40.0], [0, 0, 0, 1])
            if k % 10 == 0:
                img = rng.integers(0, 255, (90, 160, 3), dtype=np.uint8)
                w.log(f"/fleet/{u}/cam0/image/rgb", k * 100_000_000, rr.Image(img))
        w.close()
        files[f"obs.{u}"] = {"path": str(p), "bytes": p.stat().st_size}
    man = {"schema": rx.EXPORT_SCHEMA, "run_id": SYN_RUN, "recording_id": SYN_RUN, "application_id": SYN_APP, "world": WORLD,
           "event": "r07-synth", "files": files, "sensor_map": {"cam": {}, "lidar": {}}}
    (d / "export.json").write_text(json.dumps(man), encoding="utf-8")
    return d / "export.json"


def _copy_export(src: Path, d: Path) -> Path:
    """真实导出复制到临时目录（注册会就地优化文件，不改动来源），文件路径改为副本。"""
    man = json.loads((src / "export.json").read_text(encoding="utf-8"))
    d.mkdir(parents=True, exist_ok=True)
    for k, f in man["files"].items():
        p = Path(f["path"])
        p = p if p.is_file() else src / p.name
        dst = d / p.name
        shutil.copy2(p, dst)
        opt = p.parent / f".{p.name}.optimized.json"
        if opt.is_file():
            shutil.copy2(opt, d / opt.name)
        man["files"][k] = {**f, "path": str(dst)}
    (d / "export.json").write_text(json.dumps(man), encoding="utf-8")
    return d / "export.json"


def _truth_layer(export: Path, actors: list[str], duration_s: float) -> None:
    """评测 catalog 的真值层 `truth.rrd`：golden 磁带脚本演员在运行时刻的位置（10 Hz，事件偏移 0）。"""
    import rerun as rr

    from awr.bench.recorder import rerun_export as rx
    from awr.sim.events.tape import TapeReader

    man = json.loads(export.read_text(encoding="utf-8"))
    rid = str(man.get("recording_id") or man["run_id"])
    r = TapeReader(TAPE)
    info = r.actors_info(scripted_only=True)
    p = export.parent / "truth.rrd"
    w = rx.LayerWriter(p, app_id=man["application_id"], recording_id=rid, layer="truth",
                       properties={"run_id": rid, "layer": "truth", "rerun_version": rx.sdk_version()}, timebase=rx.Timebase(250))
    w.static("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP)
    for a in info:
        w.static(f"/truth/actors/{a['id']}/body", rr.Points3D([[0.0, 0.0, 0.9]], radii=0.6))
    n = int(duration_s * 10) + 1
    for k in range(n):
        t_ns = k * 100_000_000
        st = r.actor_states(t_ns, [a["idx"] for a in info])
        for j, a in enumerate(info):
            w.transform(f"/truth/actors/{a['id']}", t_ns, [float(v) for v in st.pos_enu_m[j]], [0, 0, 0, 1])
    w.close()
    actors.extend(a["id"] for a in info)
    man["files"]["truth"] = {"path": str(p), "bytes": p.stat().st_size}
    export.write_text(json.dumps(man), encoding="utf-8")


def _seed_data_root(settings, rid: str, uavs: list[str], duration_s: float) -> None:
    """bench-api 数据根：事件目录（golden 磁带与 tape.json）与运行目录（run_manifest：机队航迹与事件引用）；另建一个未注册到
    catalog 的运行目录（ORPHAN_RUN）。"""
    tape = TAPE.read_bytes()
    from awr.sim.events.tape import TapeReader

    sha = TapeReader(TAPE).hashes()["content_sha256"]
    ev = Path(settings.data_root) / "events" / EVENT / "v1"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "tape.bin").write_bytes(tape)
    (ev / "tape.json").write_text(json.dumps({"content_sha256": sha}), encoding="utf-8")
    (ev / "event.json").write_text(json.dumps({"event_id": EVENT, "world": {"id": WORLD}}), encoding="utf-8")
    fleet = [{"uav": u, "path": {"kind": "orbit", "center": [0.0, 0.0], "radius_m": 60.0, "period_s": 60.0, "phase_deg": 120.0 * i, "ccw": True}}
             for i, u in enumerate(uavs)]
    doc = {"state": "DONE", "kind": "capture", "world_id": WORLD, "fleet": fleet, "duration_s": duration_s,
           "event": {"event_id": EVENT, "version": 1, "offset_s": 0.0, "content_sha256": sha}}
    for r in (rid, ORPHAN_RUN):
        rd = Path(settings.dataset_dir) / EVENT / "runs" / r
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "run_manifest.json").write_text(json.dumps(doc), encoding="utf-8")


class _Front:
    """同源前端：/api/bench/** → bench-api；/rerun/**、/rerun.cloud.v1alpha1.RerunCloudService/** → rerun-proxy；其余为静态页面。"""

    def __init__(self, bench, proxy, dist: Path) -> None:
        self.bench, self.proxy, self.dist = bench, proxy, dist.resolve()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            stack = AsyncExitStack()
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    for a in (self.bench, self.proxy):
                        await stack.enter_async_context(a.router.lifespan_context(a))
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await stack.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        path = scope.get("path", "")
        if path.startswith("/api/bench/"):
            return await self.bench(scope, receive, send)
        if path.startswith(("/rerun/", "/rerun.cloud.")):
            return await self.proxy(scope, receive, send)
        if path.startswith("/api/") or scope["type"] != "http":
            return await self._plain(send, 404, b"not found")
        return await self._static(scope, send, path)

    async def _plain(self, send, status: int, body: bytes, ctype: str = "text/plain") -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def _static(self, scope, send, path: str) -> None:
        p = (self.dist / unquote(path).lstrip("/")).resolve()
        if not (str(p).startswith(str(self.dist)) and p.is_file()):
            if "." in path.rsplit("/", 1)[-1]:
                return await self._plain(send, 404, b"not found")
            p = self.dist / "index.html"                                   # SPA 回退（与 lab nginx 的 try_files 相同）
        body = p.read_bytes() if scope["method"] != "HEAD" else b""
        headers = [(b"content-type", TYPES.get(p.suffix, "application/octet-stream").encode()), (b"content-length", str(p.stat().st_size).encode()),
                   (b"cache-control", b"no-store" if p.suffix == ".html" else b"public, max-age=3600"), (b"x-content-type-options", b"nosniff")]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def serve_internal(a: argparse.Namespace) -> None:
    import uvicorn

    from awr.api.bench_main import create_app
    from awr.bench.api.rerun_proxy import create_proxy_app
    from awr.bench.catalog.registry import register_run
    from awr.bench.catalog.service import CatalogService
    from awr.bench.settings import BenchSettings

    root = Path(a.root)
    origin = f"http://{a.host}:{a.port}"
    settings = BenchSettings(data_root=root / "d3", allowed_origins=(origin,), secure_cookie=False, extra={"rerun_proxy_base": origin})
    export = _copy_export(Path(a.export), root / "export") if a.export else _synth_export(root / "export")
    man = json.loads(export.read_text(encoding="utf-8"))
    rid = str(man.get("recording_id") or man["run_id"])
    uavs = sorted(k.split(".", 1)[1] for k in man["files"] if k.startswith("obs."))
    duration_s = float(man.get("sim_s") or 30.0)
    actors: list[str] = []
    _truth_layer(export, actors, duration_s)
    _seed_data_root(settings, rid, uavs, duration_s)
    svc = {c: CatalogService(root / "d3" / "rerun", c).start() for c in ("eval", "dist")}
    for c in ("eval", "dist"):
        register_run(export, catalog=c, root=root / "d3" / "rerun", client=svc[c].client())
    bench = create_app(settings)
    bench.state.rerun_catalogs = {c: svc[c].client() for c in ("eval", "dist")}
    proxy = create_proxy_app(settings, upstreams={c: svc[c].url for c in ("eval", "dist")})
    auth = bench.state.auth
    tokens = {role: auth.issue_token(name, role, projs)[1] for role, name, projs in
              (("researcher", "alice", ("d3",)), ("evaluator", "eve", ()))}
    server = uvicorn.Server(uvicorn.Config(_Front(bench, proxy, Path(a.dist)), host=a.host, port=a.port, log_level="warning",
                                           lifespan="on", timeout_graceful_shutdown=2))

    def _ready() -> None:
        while not server.started:
            threading.Event().wait(0.05)
        ready = root / "ready.json"
        tmp = root / ".ready.json"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"url": origin, "run_id": rid, "orphan_run": ORPHAN_RUN, "uavs": uavs, "actors": actors, "duration_s": duration_s,
                       "real": bool(a.export), "tokens": tokens}, f)
        tmp.replace(ready)

    threading.Thread(target=_ready, daemon=True).start()
    try:
        server.run()
    finally:
        for s in svc.values():
            s.shutdown()


# ── 公开样例夹具与静态服务 ────────────────────────────────────────────────────────────────────────────────────────────

W, H, FPS = 640, 360, 10


def _video(path: Path, n: int, kind: str, uav_no: int) -> None:
    import av
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w", format="mp4") as c:
        s = c.add_stream("libx264", rate=FPS)
        s.width, s.height, s.pix_fmt = W, H, "yuv420p"
        s.options = {"crf": "28", "bf": "0", "g": "10", "keyint_min": "10", "sc_threshold": "0", "preset": "veryfast"}
        s.codec_context.time_base = Fraction(1, FPS)
        for k in range(n):
            img = np.zeros((H, W, 3), np.uint8)
            if kind == "rgb":
                img[:] = (40 + 50 * uav_no, 90, 140 - 30 * uav_no)
            else:
                img[:] = np.linspace(30, 220, W, dtype=np.uint8)[None, :, None]
            img[0:48, :] = 0
            for b in range(16):                                               # 条码：16 位帧号，位 15 在左
                if (k >> (15 - b)) & 1:
                    img[4:44, b * 40 + 4:(b + 1) * 40 - 4] = 255
            img[300:320, : int(W * (k + 1) / n)] = 250
            f = av.VideoFrame.from_ndarray(img, format="rgb24")
            f.pts = k
            f.time_base = Fraction(1, FPS)
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode():
            c.mux(p)


def _tracks(path: Path, run_id: str, seconds: float) -> None:
    """tracks.v1 观测层（D3TK）：3 机圆轨迹，10 Hz。"""
    n = int(seconds * 10) + 1
    head = json.dumps({"run_id": run_id, "hz": 10, "t0_ns": 0, "n_samples": n,
                       "uavs": [{"uav_id": u, "primary_camera": "cam0"} for u in UAVS]}).encode()
    out = bytearray(b"D3TK" + struct.pack("<HHI", 1, 0, len(head)) + head)
    for i in range(len(UAVS)):
        for k in range(n):
            x, y = _orbit(i, k / 10)
            out += struct.pack("<dddfffffffBBH", x, y, 40.0, 0, 0, 0, 1, -45.0, 0.0, 1.0, 3, 0, 0)
    out += hashlib.sha256(out).digest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def _synth_slices(d: Path, run_id: str) -> dict:
    """合成 public 档切片（17.5–27.5 s，3 机位姿与点）与静态 basemap.rrd（同一 recording_id）。"""
    import numpy as np
    import rerun as rr

    d.mkdir(parents=True, exist_ok=True)
    app = "drone4d-d3-oryahovo-crop1-crop-handoff-001"
    rec = rr.RecordingStream(app, recording_id=run_id)
    rec.save(str(d / "encounter_ab.rrd"))
    rec.log("/world", rr.ViewCoordinates.RIGHT_HAND_Z_UP, static=True)
    for k in range(175, 276):
        rec.set_time("sim_time", duration=k / 10)
        for i, u in enumerate(UAVS):
            x, y = _orbit(i, k / 10)
            rec.log(f"/fleet/{u}", rr.Transform3D(translation=[x, y, 40.0]))
            rec.log(f"/fleet/{u}/body", rr.Points3D([[0.0, 0.0, 0.0]], radii=1.5))
    rec.flush()
    del rec
    bm = rr.RecordingStream(app, recording_id=run_id)
    bm.save(str(d / "basemap.rrd"))
    g = np.mgrid[-80:80:2, -80:80:2].reshape(2, -1).T.astype(np.float32)
    bm.log("/world/basemap", rr.Points3D(np.c_[g, np.zeros(len(g), np.float32)], radii=0.5), static=True)
    bm.flush()
    del bm
    sl = d / "encounter_ab.rrd"
    return {"run_id": run_id, "slices": [{"event_id": "encounter_ab", "t_from_s": 17.5, "t_to_s": 27.5, "rrd": sl.name, "bytes": sl.stat().st_size}],
            "basemap": "basemap.rrd"}


def make_public_site(a: argparse.Namespace) -> None:
    out = Path(a.out)
    ver = "2026.10.19-7"
    v = out / "lab-static" / "v" / ver
    if v.exists():
        shutil.rmtree(v)
    v.mkdir(parents=True)
    (v / "LICENSE").write_text("CC-BY-4.0\n", encoding="utf-8")
    (v / "NOTICE").write_text("OpenStreetMap contributors (ODbL-1.0)\n", encoding="utf-8")
    (v / "events" / EVENT).mkdir(parents=True)
    shutil.copy(TAPE, v / "events" / EVENT / "tape.bin")
    if a.slices:
        # 公开站运行 id 为 run-<10 位 base32>（lab_static_index.v1）；真实切片的 recording_id 以 `rerun rrd route` 改为该 id
        #（只改元数据，不解码载荷），切片与底图保持同一 recording_id
        src = Path(a.slices)
        meta = json.loads((src / "slices.json").read_text(encoding="utf-8"))
        run_id = PUBLIC_RUN_REAL
        sd = v / "runs" / run_id / "slices"
        sd.mkdir(parents=True)
        idx = meta["index"]
        rerun = Path(sys.executable).parent / "rerun"
        for f in [*(s["rrd"] for s in idx["slices"]), idx["basemap"]]:
            subprocess.run([str(rerun), "rrd", "route", "--recording-id", run_id, "-o", str(sd / f), str(src / f)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        slices = [{"event_id": s["event_id"].lower(), "t_from_s": s["t_from_s"], "t_to_s": s["t_to_s"], "rrd": f"runs/{run_id}/slices/{s['rrd']}",
                   "bytes": (sd / s["rrd"]).stat().st_size} for s in idx["slices"]]
        basemap = f"runs/{run_id}/slices/{idx['basemap']}"
    else:
        run_id = PUBLIC_RUN_SYNTH
        sd = v / "runs" / run_id / "slices"
        meta = _synth_slices(sd, run_id)
        slices = [{**s, "rrd": f"runs/{run_id}/slices/{s['rrd']}"} for s in meta["slices"]]
        basemap = f"runs/{run_id}/slices/{meta['basemap']}"
    n = int(a.seconds * FPS)
    vids = []
    for i, u in enumerate(UAVS):
        for cam, kind in (("cam0", "rgb"), ("therm0", "thermal")):
            p = v / "runs" / run_id / "video" / f"{u}_{cam}.mp4"
            _video(p, n, kind, i)
            vids.append({"uav": u, "cam": cam, "kind": kind, "path": str(p.relative_to(v)), "bytes": p.stat().st_size})
    _tracks(v / "runs" / run_id / "tracks_obs.bin", run_id, 150)
    bm = v / basemap
    run = {"run_id": run_id, "policy": "track_share", "n_uav": 3, "obs_level": "L2", "videos": vids, "tracks_obs": f"runs/{run_id}/tracks_obs.bin",
           "slices": slices, "basemap": basemap, "basemap_bytes": bm.stat().st_size}
    index = {"schema": "drone4d.lab_static_index.v1", "version": ver, "built_unix_ns": "1792368000000000000",
             "license": {"files": ["LICENSE", "NOTICE"], "attribution": ["OpenStreetMap contributors（ODbL-1.0）"]},
             "worlds": [{"id": WORLD}],
             "events": [{"id": EVENT, "version": 1, "world_id": WORLD, "tape": f"events/{EVENT}/tape.bin", "runs": [run]}],
             "files_sha256": "SHA256SUMS", "files_bytes_total": 0,
             "features": {"inspect": True, "compare": False, "leaderboard": False, "submit": False}}
    (out / "lab-static" / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"run_id": run_id, "real": bool(a.slices), "slices": [(s["rrd"], s["bytes"]) for s in slices], "basemap": run["basemap_bytes"]}))


def serve_public(a: argparse.Namespace) -> None:
    dist, static, log = Path(a.dist).resolve(), Path(a.static).resolve(), Path(a.log)
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _log(self, status: int) -> None:
            with lock, log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"m": self.command, "p": self.path, "s": status}) + "\n")

        def _resolve(self) -> Path | None:
            path = unquote(urlsplit(self.path).path)
            if path.startswith("/lab-static/"):
                p = (static / path[len("/lab-static/"):]).resolve()
                return p if str(p).startswith(str(static)) and p.is_file() else None
            if path == "/lab" or path.startswith("/lab/"):
                p = (dist / path[len("/lab/"):]).resolve() if path.startswith("/lab/") else dist / "index.html"
                if str(p).startswith(str(dist)) and p.is_file():
                    return p
                return dist / "index.html" if "." not in path.rsplit("/", 1)[-1] else None
            return None

        def _deny(self) -> None:
            self._log(405)
            self.send_error(405)

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _deny        # http.server 按方法名分派

        def do_HEAD(self) -> None:
            self.do_GET(head=True)

        def do_GET(self, head: bool = False) -> None:
            p = self._resolve()
            if p is None:
                self._log(404)
                self.send_error(404)
                return
            data = p.read_bytes()
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                lo, _, hi = rng[6:].partition("-")
                start = int(lo) if lo else len(data) - int(hi)
                end = min(int(hi) if (lo and hi) else len(data) - 1, len(data) - 1)
                body, status = data[start:end + 1], 206
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            else:
                body, status = data, 200
                self.send_response(200)
            self.send_header("Content-Type", TYPES.get(p.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self._log(status)
            if not head:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)

    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(json.dumps({"url": f"http://{a.host}:{a.port}"}), flush=True)
    srv.serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lab_stack.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve-internal")
    s.add_argument("--dist", required=True)
    s.add_argument("--root", required=True)
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--export", default=None)
    s.add_argument("--host", default="127.0.0.1", help="监听地址（远端 Tier B 设备上的浏览器访问时为本机对该设备可达的地址）")
    m = sub.add_parser("make-public-site")
    m.add_argument("--out", required=True)
    m.add_argument("--slices", default=None)
    m.add_argument("--seconds", type=float, default=60.0)
    p = sub.add_parser("serve-public")
    p.add_argument("--dist", required=True)
    p.add_argument("--static", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args(argv)
    {"serve-internal": serve_internal, "make-public-site": make_public_site, "serve-public": serve_public}[a.cmd](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
