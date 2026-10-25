"""共享几何服务 geosvc（AWR-05 §5.1.6、ADR-167；M22-FR-048、FR-050、FR-051、FR-053、NFR-005；M22-AC-013、AC-014；D3-AC-05）。

覆盖：帧协议；服务与 inproc 逐位一致（100 万条射线按 1、7、64、4096 条分批、乱序、2 与 16 线程，及全部 3D 方法）；
0.5 m 精细 DSM 只读 memmap；绑定不一致 605；kill -9 后 2 s 内 604 与重启后自动恢复；SIGTERM 与空闲退出；装载失败退出码 3、
重复实例退出码 4；auto/service/inproc 模式；会话打开 MeshWorldQuery 并完成 10,000 次查询后匿名内存（Pss_Anon）与
PSS 非文件部分增量 ≤ 20 MB（文件页不计），inproc 对照组 > 20 MB。基线为 D2 会话状态（已导入 numpy、numba 与 M04，已打开
DsmWorldQuery 的 2 m 栅格），增量只计 MeshWorldQuery 带来的部分（M22-NFR-005"因 MeshWorldQuery 增加的匿名内存"）。crop 世界上的同一度量在设置
`AWR_GEOSVC_CROP_WORLD=<世界目录>` 时运行。
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("open3d")
pytest.importorskip("PIL")
pytest.importorskip("msgpack")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from glbfix import png, write_glb
from test_glb_build import _scene

import awr
from awr.world.geometry.types import GeoError
from awr.world.mesh import geosvc as G

MEM_LIMIT_B = 20_000_000                     # D3-AC-05：≤ 20 MB（十进制兆字节）
N_RAYS = 1 << 20
SLOT_BYTES = 1 << 20                         # 测试用小槽：100 万条射线切成 80 块，覆盖切块与多槽流水
PY_ROOT = str(Path(awr.__file__).resolve().parents[1])


def _env(**extra: str) -> dict[str, str]:
    e = dict(os.environ)
    e["PYTHONPATH"] = PY_ROOT + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    e.pop("AWR_GEOSVC", None)
    e.update(extra)
    return e


@pytest.fixture(scope="module")
def gworld(tmp_path_factory: pytest.TempPathFactory):
    """合成 GLB 世界包（300 m 平地 + 20 × 30 × 20 m 盒子，tiling 128 m），模块内只构建一次。"""
    from awr.world.glb import recipe as recipe_mod
    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.glb.recipe import file_sha256, load_recipe
    from awr.world.glb.register import register_manual, write_registration
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world

    tmp = tmp_path_factory.mktemp("gs")
    raw = tmp / "raw"
    (raw / "synth").mkdir(parents=True)
    V, F, UV = _scene()
    g = write_glb(raw / "synth" / "synth.glb", V=V, F=F, UV=UV, tex=png(8, 8, (200, 30, 30)))
    man = tmp / "raw-manifest.json"
    man.write_text(json.dumps({"files": [{"name": "synth/synth.glb", "bytes": g.stat().st_size, "sha256": file_sha256(g)}]}))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(recipe_mod, "RAW_DIR", raw)
        mp.setattr(recipe_mod, "RAW_MANIFEST", man)
        rd = tmp / "recipes" / "synth-glb"
        rd.mkdir(parents=True)
        (rd / "recipe.yaml").write_text(
            "schema: awr.glb.recipe.v1\nworld_id: synth-glb\nname: Synthetic GLB test world\nrole: event-main\n"
            f"source:\n  glb: {{path: synth/synth.glb, sha256: {file_sha256(g)}}}\n"
            "  sketchfab: {uid: '', author: test, license: CC0-1.0}\nregistration: registration.json\n"
            "build:\n  tiling_m: 128\n  pointcloud: {target_points: 20000, seed: 7}\n")
        rec = load_recipe("synth-glb", tmp / "recipes")
        doc = register_manual(rec, {"s": 1.0, "yaw_deg": 0.0, "g": (0.0, 0.0), "t_osm_m": (0.0, 0.0), "osm_ref": (43.73, 23.96)},
                              h_msl_m=100.0, evidence=["synthetic fixture"])
        write_registration(rec, doc, accept_by="test")
        assert build_world(GlbAdapter("synth-glb", recipe=rec), tmp / "worlds", ctx=StageContext(world_id="synth-glb")).exit_code == 0
    w = tmp / "worlds" / "synth-glb"
    T = np.asarray(json.loads((w / "coordinate.json").read_text())["source"]["T_world_source"])

    def to_world(p_gltf) -> np.ndarray:
        return T[:3, :3] @ np.asarray(p_gltf, np.float64) + T[:3, 3]

    return {"dir": w, "to_world": to_world, "cache": tmp / "cache", "run": tmp / "run", "tmp": tmp}


def _spawn(gw: dict, name: str, **kw) -> tuple[subprocess.Popen, Path]:
    sock = gw["run"] / f"{name}.sock"
    kw.setdefault("slot_bytes", SLOT_BYTES)
    with open(gw["tmp"] / f"{name}.log", "ab") as log:
        p = G.spawn_geosvc(gw["dir"], socket_path=sock, cache_root=gw["cache"], env=_env(), stderr=log, **kw)
    return p, sock


def _stop(p: subprocess.Popen) -> int:
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    return p.returncode


@pytest.fixture(scope="module")
def svc(gworld):
    p, sock = _spawn(gworld, "main", threads=2)
    yield p, sock
    assert _stop(p) == 0


@pytest.fixture(scope="module")
def pair(gworld, svc):
    _p, sock = svc
    qs = G.open_geosvc_world_query(gworld["dir"], gworld["cache"], geosvc="service", socket_path=sock)
    qi = G.open_geosvc_world_query(gworld["dir"], gworld["cache"], geosvc="inproc")
    yield qs, qi
    qs.close()


def _rays(q, n: int, seed: int = 11) -> tuple[np.ndarray, np.ndarray]:
    """世界范围内随机起点（地面以上 0–80 m）、全向方向（含水平与向上）。"""
    rng = np.random.default_rng(seed)
    lo, hi = q.bounds_m
    Org = np.column_stack([rng.uniform(lo[0], hi[0], n), rng.uniform(lo[1], hi[1], n), rng.uniform(lo[2] + 0.5, lo[2] + 80.0, n)])
    D = rng.normal(size=(n, 3))
    D[: n // 8, 2] = 0.0                                                       # 水平射线
    return Org, D


def _eq(a, b) -> bool:
    """逐位相等（数组与浮点按字节比较，NaN 与 NaN 相等）。"""
    if isinstance(a, np.ndarray | float | np.floating):
        return np.asarray(a, np.float64).tobytes() == np.asarray(b, np.float64).tobytes()
    return a == b


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    return a.dtype == b.dtype and a.shape == b.shape and a.tobytes() == b.tobytes()


# ==================================================================== 协议


def test_frames_roundtrip_and_limits() -> None:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        ia, ib = G._FrameIO(a), G._FrameIO(b)
        ia.write({"op": "ping", "seq": 1})
        ia.write({"op": "stats", "seq": 2, "x": 1.5})
        assert ib.read() == {"op": "ping", "seq": 1} and ib.read() == {"op": "stats", "seq": 2, "x": 1.5}
        with pytest.raises(GeoError) as e:
            G._pack({"blob": b"\0" * (G.MAX_FRAME + 1)})
        assert e.value.code == 300
        a.sendall((G.MAX_FRAME + 1).to_bytes(4, "little"))
        with pytest.raises(G._ProtocolError):
            ib.read()
        a.close()
        assert G._FrameIO(b).read() is None                                   # 对端关闭
    finally:
        a.close()
        b.close()
    p = G.geosvc_socket_path("crop", "abc123", "/run/x")
    assert p == Path("/run/x/geosvc/crop-abc123.sock")
    assert G.CAST_ITEM_B == 80 and G.CLOSEST_ITEM_B == 28 and G.WALK_ITEM_B == 40


# ==================================================================== 逐位一致


def test_raycast_bitwise_equal_across_batching_order_and_threads(gworld, svc, pair) -> None:
    qs, qi = pair
    Org, D = _rays(qi, N_RAYS)
    t_ref, p_ref, i_ref = qi.raycast_batch(Org, D, 500.0)
    hits = np.isfinite(t_ref)
    assert 0.2 < hits.mean() < 0.9 and (p_ref[~hits] == 0xFFFFFFFF).all()
    t, p, i = qs.raycast_batch(Org, D, 500.0)                                   # 整批：80 块在 8 个槽上流水
    assert _same(t, t_ref) and _same(p, p_ref) and _same(i, i_ref)
    rng = np.random.default_rng(3)
    for bs, n in ((4096, N_RAYS), (64, 1 << 16), (7, 7000), (1, 1000)):
        starts = np.arange(0, n, bs)
        rng.shuffle(starts)                                                    # 乱序到达
        t2 = np.empty(n, np.float32)
        p2 = np.empty(n, np.uint32)
        for s in starts:
            tt, pp, _ = qs.raycast_batch(Org[s:s + bs], D[s:s + bs], 500.0)
            t2[s:s + bs], p2[s:s + bs] = tt, pp
        assert t2.tobytes() == t_ref[:n].tobytes() and p2.tobytes() == p_ref[:n].tobytes(), bs
    p16, sock16 = _spawn(gworld, "t16", threads=16)
    try:
        q16 = G.open_geosvc_world_query(gworld["dir"], gworld["cache"], geosvc="service", socket_path=sock16)
        t3, p3, _ = q16.raycast_batch(Org, D, 500.0)
        assert _same(t3, t_ref) and _same(p3, p_ref)
        q16.close()
    finally:
        assert _stop(p16) == 0


def test_all_3d_methods_match_inproc(gworld, pair) -> None:
    qs, qi = pair
    assert isinstance(qs, G.GeoSvcMeshWorldQuery) and isinstance(qi, G.InprocMeshWorldQuery)
    assert "open3d" in sys.modules                                             # 本进程有 inproc 对照；会话进程见内存用例
    to_world = gworld["to_world"]
    top = to_world([40.0, 30.0, 40.0])
    side = to_world([40.0, 10.0, 40.0])
    west = side - np.array([60.0, 0, 0])
    cases = [(top + np.array([0, 0, 70.0]), [0, 0, -1]), (west, [1, 0, 0]), (side, [1, 0, 0]), (top - np.array([0, 0, 5.0]), [0, 0, 1]),
             (west, [0, 0, 1]), (west + np.array([0, 0, 5.0]), [1, 0.2, -0.1])]
    for o, d in cases:
        hs, hi = qs.ray_hit(o, d, 500.0), qi.ray_hit(o, d, 500.0)
        for f in dataclasses.fields(hs):
            a, b = getattr(hs, f.name), getattr(hi, f.name)
            assert _eq(a, b), (f.name, a, b)
    assert any(qs.ray_hit(o, d, 500.0).hit for o, d in cases) and any(qs.ray_hit(o, d, 500.0).origin_inside for o, d in cases)
    rng = np.random.default_rng(5)
    Org, D = _rays(qi, 64, seed=7)
    B = Org + rng.normal(size=(64, 3)) * 80.0
    assert _same(qs.los_batch(Org, B), qi.los_batch(Org, B)) and not qs.los_batch(Org, B).all()
    assert qs.segment_los(west, west + np.array([120.0, 0, 0])) is qi.segment_los(west, west + np.array([120.0, 0, 0])) is False
    assert _same(qs.free_distance(Org[:16], D[:16], 200.0), qi.free_distance(Org[:16], D[:16], 200.0))
    assert _same(qs.closest_batch(Org, 50.0), qi.closest_batch(Org, 50.0))
    _t, prim, _ = qi.raycast_batch(Org, D, 500.0)
    assert _same(qs.semantic_of(prim), qi.semantic_of(prim))
    xy = Org[:, :2]
    assert _same(qs.walkable_height(xy), qi.walkable_height(xy)) and np.isfinite(qs.walkable_height(xy)).all()
    zs, ps = qs.walkable_surface(xy, Org[:, 2])
    zi, pi = qi.walkable_surface(xy, Org[:, 2])
    assert _same(zs, zi) and _same(ps, pi)
    assert _same(qs.height_dsm(xy), qi.height_dsm(xy))                         # 2.5D 方法：本地 2 m 栅格，同 D2
    for q in (qs, qi):                                                         # 错误码与 inproc 相同
        with pytest.raises(GeoError) as e:
            q.raycast_batch(Org, D, 0.0)
        assert e.value.code == 301
        with pytest.raises(GeoError) as e:
            q.free_distance(Org[:17], D[:17], 10.0)
        assert e.value.code == 110
    assert qs.mesh_info()["geosvc"]["mode"] == "service" and qs.mesh_info()["tiles"] == qi.mesh_info()["tiles"] >= 1
    assert qs.mesh_info()["triangles"] == qi.n_triangles and all(t.scene is None for t in qs.tiles)
    with pytest.raises(GeoError) as e:
        qs.walk_index()
    assert e.value.code == G.GEOSVC_UNAVAILABLE


def test_fine_dsm_is_readonly_memmap_shared_by_service_and_inproc(gworld, pair) -> None:
    qs, qi = pair
    gs, gi = qs.dsm_fine_grid(), qi.dsm_fine_grid()
    assert gs.a.shape == (qs.dsm_raw.h * 4, qs.dsm_raw.w * 4) and gs.cell_m == 0.5
    assert (gs.x0_m, gs.y0_m) == (qs.dsm_raw.x0, qs.dsm_raw.y0) and not gs.a.flags.writeable
    assert gs.a.tobytes() == gi.a.tobytes()
    assert isinstance(qs._fine_grid.a, np.memmap) and qs._fine_grid.a.mode == "r"
    assert Path(qs._fine_grid.a.filename) == Path(qi._fine_grid.a.filename)
    sc = json.loads(Path(qs.client.info["fine"]["sidecar"]).read_text())
    assert sc["source"]["content_version"] == qs.content_version and sc["nodata"] is None
    to_world = gworld["to_world"]
    box_top = to_world([40.0, 30.0, 40.0])
    ground = to_world([-100.0, 0.0, -100.0])
    off = to_world([51.0, 30.0, 40.0])                                         # 盒子东缘外 1 m
    h = qs.height_dsm_fine(np.array([box_top[:2], ground[:2], off[:2]]))
    assert h.dtype == np.float32
    assert h[0] == pytest.approx(box_top[2], abs=1e-3) and h[1] == pytest.approx(ground[2], abs=1e-3)
    assert h[2] == pytest.approx(ground[2], abs=1e-3)                          # 0.5 m 栅格分辨出盒缘
    assert np.isnan(qs.height_dsm_fine(np.array([[1e6, 1e6]]), oob="nan")[0])


# ==================================================================== 绑定、故障与生命周期


def test_binding_mismatch_returns_605(gworld, svc, pair) -> None:
    _p, sock = svc
    qs, _ = pair
    for bad in ({"content_version": "000000000000"}, {"coordinate_sha256": "0" * 64}, {"world_id": "other"}):
        b = {"world_id": qs.world_id, "content_version": qs.content_version, "coordinate_sha256": qs.coordinate_sha256, **bad}
        with pytest.raises(GeoError) as e:
            G.GeoServiceClient(sock, **b).connect()
        assert e.value.code == G.GEOSVC_BINDING_MISMATCH


def test_kill9_reports_604_within_2s_and_recovers_after_restart(gworld) -> None:
    p, sock = _spawn(gworld, "k9")
    q = G.open_geosvc_world_query(gworld["dir"], gworld["cache"], geosvc="service", socket_path=sock)
    Org, D = _rays(q, 4096, seed=2)
    ref = q.raycast_batch(Org, D, 500.0)
    os.kill(p.pid, signal.SIGKILL)
    p.wait(10)
    t0 = time.monotonic()
    with pytest.raises(GeoError) as e:
        q.raycast_batch(Org, D, 500.0)
    assert e.value.code == G.GEOSVC_UNAVAILABLE and time.monotonic() - t0 <= 2.0
    assert sock.exists()                                                       # kill -9 遗留的套接字由新实例清理
    p2, _ = _spawn(gworld, "k9")
    try:
        t, prim, _ = q.raycast_batch(Org, D, 500.0)                              # 重新握手，自动恢复
        assert _same(t, ref[0]) and _same(prim, ref[1]) and q.client.reconnects >= 1
        assert q.client.info["pid"] == p2.pid
        q.close()
    finally:
        assert _stop(p2) == 0


def test_sigterm_drains_and_idle_timeout_exits(gworld) -> None:
    p, sock = _spawn(gworld, "term")
    q = G.open_geosvc_world_query(gworld["dir"], gworld["cache"], geosvc="service", socket_path=sock)
    assert q.client.ping()["pid"] == p.pid
    p.send_signal(signal.SIGTERM)
    assert p.wait(15) == 0
    assert not sock.exists() and not Path(str(sock) + ".ready").exists()
    with pytest.raises(GeoError) as e:
        q.raycast_batch(np.zeros((1, 3)), np.array([[0, 0, -1.0]]), 10.0)
    assert e.value.code == G.GEOSVC_UNAVAILABLE
    p, sock = _spawn(gworld, "idle", idle_timeout_s=1.0)
    t0 = time.monotonic()
    assert p.wait(20) == 0 and time.monotonic() - t0 < 10.0
    assert not sock.exists()
    log = (gworld["tmp"] / "idle.log").read_text()
    states = [json.loads(L)["state"] for L in log.splitlines() if '"geosvc.state"' in L]
    assert states == ["STARTING", "LOADING", "READY", "DRAINING", "STOPPED"]
    assert '"reason": "idle"' in log


def test_load_failure_exit_3_and_second_instance_exit_4(gworld, svc) -> None:
    cmd = [sys.executable, "-m", "awr.world.mesh.geosvc", "--world-dir", str(gworld["tmp"] / "nope"),
           "--socket", str(gworld["run"] / "nope.sock")]
    r = subprocess.run(cmd, env=_env(), capture_output=True, timeout=60)
    assert r.returncode == 3 and b'"FAILED"' in r.stderr
    _p, sock = svc
    cmd = [sys.executable, "-m", "awr.world.mesh.geosvc", "--world-dir", str(gworld["dir"]), "--cache-dir", str(gworld["cache"]),
           "--socket", str(sock)]
    r = subprocess.run(cmd, env=_env(), capture_output=True, timeout=120)
    assert r.returncode == 4 and b"already_serving" in r.stderr


def test_modes_auto_service_inproc(gworld, monkeypatch: pytest.MonkeyPatch) -> None:
    w, c = gworld["dir"], gworld["cache"]
    missing = gworld["run"] / "absent.sock"
    monkeypatch.delenv("AWR_GEOSVC", raising=False)
    monkeypatch.setenv("AWR_PROFILE", "dev")
    q = G.open_geosvc_world_query(w, c, socket_path=missing)                   # auto + 开发 profile：回退 inproc
    assert isinstance(q, G.InprocMeshWorldQuery)
    monkeypatch.setenv("AWR_PROFILE", "d3")                                    # d3 生产 profile：不回退
    with pytest.raises(GeoError) as e:
        G.open_geosvc_world_query(w, c, socket_path=missing)
    assert e.value.code == G.GEOSVC_UNAVAILABLE
    monkeypatch.setenv("AWR_PROFILE", "dev")
    with pytest.raises(GeoError) as e:
        G.open_geosvc_world_query(w, c, geosvc="service", socket_path=missing)
    assert e.value.code == G.GEOSVC_UNAVAILABLE
    monkeypatch.setenv("AWR_GEOSVC", "inproc")
    assert isinstance(G.open_geosvc_world_query(w, c, socket_path=missing), G.InprocMeshWorldQuery)
    monkeypatch.setenv("AWR_GEOSVC", "bogus")
    with pytest.raises(ValueError):
        G.open_geosvc_world_query(w, c)


def test_ping_stats_and_error_replies(svc, pair) -> None:
    p, _sock = svc
    qs, _ = pair
    cl = qs.client
    assert cl.ping()["pid"] == p.pid
    qs.raycast_batch(np.zeros((3, 3)), np.tile([0.0, 0.0, -1.0], (3, 1)), 10.0)
    st = cl.stats()
    assert st["state"] == "READY" and st["connections"] >= 1 and st["rays_total"] > 0 and 0 < st["rss_mib"] < 700
    for op, code in (("nav_path", 604), ("walkable", 604), ("bogus", 300)):
        with pytest.raises(GeoError) as e:
            cl.request(op)
        assert e.value.code == code
    with pytest.raises(GeoError) as e:
        cl.request("cast", slot=99, n=1)
    assert e.value.code == 110
    with pytest.raises(GeoError) as e:
        cl.request("cast", slot=0, n=SLOT_BYTES)
    assert e.value.code == 110
    assert cl.ping()["pid"] == p.pid                                           # 错误应答后连接仍可用


# ==================================================================== 会话内存（D3-AC-05）

_SESSION = r'''
import gc, json, sys
import numpy as np
if sys.argv[5] == "1":
    import numba  # noqa: F401  D2 sim-core 的 M08 插件已加载 numba 核，基线与之对齐
import awr.world.geometry.query  # noqa: F401  D2 会话已有的 M04
def mem():
    d = {}
    for line in open("/proc/self/smaps_rollup"):
        p = line.split()
        if len(p) == 3 and p[2] == "kB":
            d[p[0].rstrip(":")] = int(p[1]) * 1024
    return d
world, cache, sock, mode, _numba, n = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], int(sys.argv[6])
gc.collect()
m_pre = mem()
base = awr.world.geometry.query.open_dsm_world_query(world, cache or None)   # D2 会话同样持有的 DsmWorldQuery（2 m 栅格）
gc.collect()
m0 = mem()                                                                  # 基线：D2 会话状态；增量只计 MeshWorldQuery 带来的部分
from awr.world.mesh import geosvc as G
if mode == "service":
    q = G.GeoSvcMeshWorldQuery.connect(base, world, sock)
else:
    q = G._open_inproc(base, world, cache or None)
rng = np.random.default_rng(1)
lo, hi = q.bounds_m
def pts(k, z0=0.5, z1=80.0):
    return np.column_stack([rng.uniform(lo[0], hi[0], k), rng.uniform(lo[1], hi[1], k), rng.uniform(lo[2] + z0, lo[2] + z1, k)])
Org, D = pts(256), rng.normal(size=(256, 3))
A, B = pts(64), pts(64)
prim = np.zeros(256, np.uint32)
for i in range(n):
    k = i % 8
    if k == 0:
        _t, prim, _ = q.raycast_batch(Org, D, 500.0)
    elif k == 1:
        q.los_batch(A, B)
    elif k == 2:
        q.ray_hit(Org[i % 256], D[i % 256], 500.0)
    elif k == 3:
        q.free_distance(Org[:16], D[:16], 200.0)
    elif k == 4:
        q.closest_batch(Org[:32], 50.0)
    elif k == 5:
        q.height_dsm_fine(Org[:, :2])
    elif k == 6:
        q.walkable_height(Org[:64, :2])
    else:
        q.semantic_of(prim)
        q.height_dsm(Org[:, :2])
gc.collect()
m1 = mem()
print(json.dumps({"kind": type(q).__name__, "queries": n, "open3d": "open3d" in sys.modules, "pre": m_pre, "before": m0, "after": m1}))
'''


def _session(gw: dict, world: Path, cache: Path | None, sock: Path | None, mode: str, *, numba: bool = True,
             n: int = 10_000) -> dict:
    script = gw["tmp"] / "session.py"
    script.write_text(_SESSION)
    r = subprocess.run([sys.executable, str(script), str(world), str(cache or ""), str(sock or ""), mode, "1" if numba else "0", str(n)],
                       env=_env(AWR_PROFILE="dev"), capture_output=True, text=True, timeout=1200)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    b, a = out["before"], out["after"]
    out["delta"] = {k: a.get(k, 0) - b.get(k, 0) for k in ("Rss", "Pss", "Pss_Anon", "Pss_File", "Pss_Shmem", "Anonymous")}
    out["delta"]["Pss_nonfile"] = out["delta"]["Pss"] - out["delta"]["Pss_File"]
    out["d2_dsm_open"] = {k: b.get(k, 0) - out["pre"].get(k, 0) for k in ("Pss_Anon", "Pss_File")}
    return out


def _report(tag: str, r: dict) -> None:
    d = {k: round(v / 1e6, 2) for k, v in r["delta"].items()}
    d2 = {k: round(v / 1e6, 2) for k, v in r["d2_dsm_open"].items()}
    print(f"[geosvc-mem] {tag}: kind={r['kind']} queries={r['queries']} open3d={r['open3d']} delta_MB={json.dumps(d)} "
          f"(D2 DsmWorldQuery open, in baseline: {json.dumps(d2)})")


def test_session_anonymous_memory_delta_within_20mb(gworld, svc) -> None:
    _p, sock = svc
    r = _session(gworld, gworld["dir"], gworld["cache"], sock, "service")
    _report("synth service", r)
    assert r["kind"] == "GeoSvcMeshWorldQuery" and r["queries"] == 10_000 and r["open3d"] is False
    assert r["delta"]["Pss_Anon"] <= MEM_LIMIT_B and r["delta"]["Anonymous"] <= MEM_LIMIT_B
    assert r["delta"]["Pss_nonfile"] <= MEM_LIMIT_B                            # 匿名 + 共享内存槽，文件页不计
    ctl = _session(gworld, gworld["dir"], gworld["cache"], None, "inproc", n=2000)
    _report("synth inproc control", ctl)
    assert ctl["kind"] == "InprocMeshWorldQuery" and ctl["open3d"] is True
    assert ctl["delta"]["Pss_Anon"] > MEM_LIMIT_B                              # 对照组：度量有判别力


@pytest.mark.skipif(not os.environ.get("AWR_GEOSVC_CROP_WORLD"), reason="set AWR_GEOSVC_CROP_WORLD=<world dir> (crop)")
def test_crop_session_memory_and_service_rss(tmp_path: Path) -> None:
    w = Path(os.environ["AWR_GEOSVC_CROP_WORLD"])
    gw = {"tmp": tmp_path}
    sock = tmp_path / "crop.sock"
    t0 = time.monotonic()
    with open(tmp_path / "crop.log", "ab") as log:
        p = G.spawn_geosvc(w, socket_path=sock, env=_env(), stderr=log, wait_s=1200.0)
    print(f"[geosvc-mem] crop service ready in {time.monotonic() - t0:.1f} s")
    try:
        r = _session(gw, w, None, sock, "service")
        _report("crop service", r)
        st = G.GeoServiceClient(sock, **{k: v for k, v in _binding(w).items()}).stats()
        rss = G.smaps_rollup(p.pid)
        print(f"[geosvc-mem] crop service stats={json.dumps(st)} rss_MiB={rss['Rss'] / 2**20:.1f} pss_MiB={rss['Pss'] / 2**20:.1f}")
        assert r["kind"] == "GeoSvcMeshWorldQuery" and r["open3d"] is False
        assert r["delta"]["Pss_Anon"] <= MEM_LIMIT_B and r["delta"]["Pss_nonfile"] <= MEM_LIMIT_B
        assert rss["Rss"] <= 700 * 2**20                                       # M22-NFR-005（不含 Detour 工作进程）
        ctl = _session(gw, w, None, None, "inproc", n=1000)
        _report("crop inproc control", ctl)
        assert ctl["delta"]["Pss_Anon"] > 10 * MEM_LIMIT_B
    finally:
        assert _stop(p) == 0


def _binding(w: Path) -> dict:
    from awr.world.geometry.status import read_world_header

    h = read_world_header(w)
    return {"world_id": h.world_id, "content_version": h.content_version, "coordinate_sha256": h.coordinate_sha256}
