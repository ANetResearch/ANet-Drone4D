"""D2 运行时不改代码加载 GLB 世界包的点云、DSM 与 DTM（D3-AC-02；docs/05 §5.1.4"D1 必需图层全部保留"；W07）。

对已发布的 `worlds/oryahovo-crop1`（缺失时按 data 层跳过），在 D3 开关关闭（`AWR_D3_ENABLED` 不为 1，即 D2 进程环境）下逐项走
D2 原有代码路径，不打补丁：

1. sim-core：`SimCore._load_world`（M08，`open_world_query` + `GeoProbeServer`）装载为 D2 的 `DsmWorldQuery`（不是 D3 的
   MeshWorldQuery），探针服务的 `height_dsm`、`ground_dtm`、`ray_hit` 应答与世界包栅格逐值一致；
2. WorldQuery（M04）：DSM、DTM 栅格与世界包文件逐格相同，`grid25` 派生、`agl` 与竖直射线可用，派生缓存写在临时目录；
3. 沙盒准入（M17）：公开模式（`allow_derive = false`）以 `cache_status` 探测，派生缓存就绪后世界可用并可准入；
4. 点云：D2 的 ANET_Q16 读取器（语义同 M05 parseHierarchy）遍历全部节点、点数与 `metadata.points` 相同、坐标在世界包围盒内；
   源点云（`read_source`）点数与 `source.json` 一致；
5. M05：以 Node 直接执行 apps/web 的 `openWorld`（M05 源码不改，只以解析钩子补 bundler 约定：无扩展名导入、`@/` 别名、
   `import.meta.env`），本地文件代替 HTTP，读出 world.json、点云清单、层级与首屏节点。缺 Node 22 或 apps/web 源码时按 platform 层跳过。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
WORLDS = Path(os.environ.get("AWR_WORLDS_DIR") or ROOT / "worlds")
WID = "oryahovo-crop1"
CROP = WORLDS / WID
WEB = ROOT / "apps" / "web"

pytestmark = pytest.mark.skipif(not (CROP / "world.json").is_file(),
                                reason="needs worlds/oryahovo-crop1 (python -m awr.world.glb build oryahovo-crop1)")


def _world() -> dict:
    return json.loads((CROP / "world.json").read_text(encoding="utf-8"))


def _layer(lid: str) -> dict:
    return next(L for L in _world()["layers"] if L["id"] == lid)


def _grid(lid: str):
    from awr.world.terrain.grids import Grid, read_grid

    sc, a = read_grid(CROP / _layer(lid)["href"])
    return Grid(a, (float(sc["originXY"][0]), float(sc["originXY"][1])), float(sc["cellM"]))


@pytest.fixture(scope="module")
def d2_env(tmp_path_factory: pytest.TempPathFactory):
    """D2 进程环境：D3 开关关闭，派生缓存写在临时目录（不污染 worlds/.geo-cache）。"""
    mp = pytest.MonkeyPatch()
    mp.delenv("AWR_D3_ENABLED", raising=False)
    cache = tmp_path_factory.mktemp("geo-cache")
    mp.setenv("AWR_GEO_CACHE_DIR", str(cache))
    mp.setenv("AWR_GEO_ALLOW_DERIVE", "1")
    yield cache
    mp.undo()


@pytest.fixture(scope="module")
def simcore(d2_env: Path):
    """M08 sim-core 的世界装载方法本体（`SimCore._load_world`），以最小宿主对象调用。"""
    from awr.sim.runtime.main import SimCore

    host = SimpleNamespace(cfg=SimpleNamespace(worlds_dir=WORLDS, world_id=WID), profile=SimpleNamespace(manual_lock=True),
                           world=None, geo=None, world_error=None)
    SimCore._load_world(host)
    assert host.world is not None, host.world_error
    return host


def _cells(g, n: int, seed: int) -> np.ndarray:
    """随机格中心（世界帧 E, N）。"""
    rng = np.random.default_rng(seed)
    H, W = g.a.shape
    r, c = rng.integers(0, H, n), rng.integers(0, W, n)
    return np.c_[g.origin_xy[0] + (c + 0.5) * g.cell_m, g.origin_xy[1] + (r + 0.5) * g.cell_m], r, c


class _Q:
    def __init__(self, qid: str, op: str, args: dict) -> None:
        self.id, self.op, self.args, self.replies = qid, op, args, []

    def reply(self, payload: dict) -> None:
        self.replies.append(payload)


def test_simcore_loads_crop_as_d2_dsm_world(simcore) -> None:
    from awr.world.geometry.query import DsmWorldQuery

    wq = simcore.world
    w = _world()
    assert type(wq) is DsmWorldQuery                                    # D2 类型，不经 D3 分派
    assert wq.world_id == WID and wq.content_version == w["contentVersion"] and wq.coordinate_sha256 == w["coordinate"]["sha256"]
    assert w["profile"] == "d3-glb" and w["schemaVersion"] == "1.1.0"
    lo, hi = np.asarray(w["bounds"]["min"]), np.asarray(w["bounds"]["max"])
    srv = simcore.geo
    dsm, dtm = _grid("terrain.dsm"), _grid("terrain.dtm")
    xy, r, c = _cells(dsm, 64, 1)
    q1 = _Q("h", "height_dsm", {"points": xy.tolist()})
    q2 = _Q("g", "ground_dtm", {"points": xy.tolist()})
    top = float(dsm.a.max())
    q3 = _Q("r", "ray_hit", {"origin_enu_m": [float(xy[0, 0]), float(xy[0, 1]), top + 50.0], "dir": [0, 0, -1], "max_range_m": 5000})
    srv.enqueue("height", q1)
    srv.enqueue("height", q2)
    srv.enqueue("ray_hit", q3)
    for _ in range(10_000):
        if not srv.q:
            break
        srv.run(2_000)
    a1, a2, a3 = q1.replies[0], q2.replies[0], q3.replies[0]
    assert a1["ok"] and a2["ok"] and a3["ok"] and a1["content_version"] == w["contentVersion"]
    assert np.allclose(np.asarray(a1["result"]["z_m"], np.float64), wq.height_dsm(xy).astype(np.float64), atol=1e-4)
    assert np.allclose(np.asarray(a2["result"]["z_m"], np.float64), dtm.bilinear(xy[:, 0], xy[:, 1]), atol=1e-3)
    assert np.allclose(wq.height_dsm(xy, raw=True), dsm.a[r, c], atol=0)            # 原始 DSM 即世界包 dsm_2m
    assert a3["result"]["hit"] and abs(a3["result"]["point_enu_m"][2] - float(wq.height_dsm(xy[:1])[0])) < 0.5
    assert float(lo[2]) - 1.0 <= float(dsm.a.min()) and float(dsm.a.max()) <= float(hi[2]) + 1.0


def test_d2_world_query_grids_and_derived_cache(simcore, d2_env: Path) -> None:
    wq = simcore.world
    dsm, dtm = _grid("terrain.dsm"), _grid("terrain.dtm")
    gv, gt = wq.dsm_grid(), wq.dtm_grid()                  # dsm_grid 为装载期派生的 dsm_eff（观测感知闭运算，M04 §6.4.1），不低于原始 DSM
    assert gv.a.shape == dsm.a.shape and (gv.x0_m, gv.y0_m, gv.cell_m) == (*dsm.origin_xy, dsm.cell_m)
    assert np.all(np.asarray(gv.a) >= dsm.a - 1e-4)
    assert np.array_equal(np.asarray(gt.a), dtm.a) and (gt.x0_m, gt.y0_m, gt.cell_m) == (*dtm.origin_xy, dtm.cell_m) and gt.cell_m == 10.0
    xy, _r, _c = _cells(dtm, 200, 2)
    g = wq.ground_dtm(xy)
    assert np.all(np.isfinite(g)) and np.allclose(g, dtm.bilinear(xy[:, 0], xy[:, 1]).astype(np.float32))
    z = wq.height_dsm(xy)
    agl = wq.agl(np.c_[xy, z + 30.0])
    assert np.all(np.isfinite(agl)) and np.all(agl > 0)
    grid, meta = wq.grid_2p5d(4.0)
    assert grid.size > 0 and np.isfinite(grid).any() and meta
    entries = [p for p in (d2_env / WID).iterdir() if p.is_dir()]
    assert entries, "derived cache entry missing"


def test_sandbox_admission_public_mode(simcore, d2_env: Path) -> None:
    """公开模式只认已派生的缓存（`cache_status`）：装载后世界可用、有平面边界，并按缺省成本准入。"""
    from awr.sandbox.admission import world_admit
    from awr.sandbox.worlds import WorldTable
    from awr.world.geometry import cache_status

    st = cache_status(CROP)
    assert st.available and st.content_version == _world()["contentVersion"], st
    tab = WorldTable(WORLDS, {}, allow_derive=False, ids=[WID])
    info = tab.get(WID)
    assert info is not None and info.available and info.reason is None and info.allowed
    row = info.item(admit_now=True)
    b = _world()["bounds"]
    assert row["available"] and row["admit_now"] and row["bounds_m"] == {"xmin": b["min"][0], "xmax": b["max"][0], "ymin": b["min"][1],
                                                                          "ymax": b["max"][1]}
    assert world_admit(tab.costs, [], WID, n_max=4, m_sb=4096.0)


def test_d2_pointcloud_reader_reads_every_node() -> None:
    from awr.world.pointcloud.reader import iter_nodes, read_container
    from awr.world.pointcloud.source import read_source

    w = _world()
    roots = _layer("pointcloud.visual")["roots"]
    lo, hi = np.asarray(w["bounds"]["min"]) - 0.01, np.asarray(w["bounds"]["max"]) + 0.01
    total = 0
    for root in roots:
        c = read_container(CROP / root["href"])
        md = c.metadata
        assert md["encoding"] == "ANET_Q16" and md["anet"]["col"]["rgb"] == "srgb"
        n_pts = 0
        for v in iter_nodes(c, decode=True):
            n_pts += v.num_points
            if v.num_points:
                assert np.all(v.positions >= lo) and np.all(v.positions <= hi), v.name
        assert n_pts == md["points"]
        total += n_pts
    assert total == sum(int(r["points"]) for r in roots) > 0
    src = read_source(CROP)
    assert len(src.xyz) == src.sidecar["count"] == len(src.cls) == total
    assert np.all(src.xyz.min(0) >= lo) and np.all(src.xyz.max(0) <= hi)


HOOKS = r"""
import { existsSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
let src = null
export async function initialize(data) { src = pathToFileURL(data.web + '/src/').href }
export async function resolve(spec, ctx, next) {
  let base = null, rest = spec
  if (spec.startsWith('@/')) { base = src; rest = spec.slice(2) } else if ((spec.startsWith('./') || spec.startsWith('../')) && ctx.parentURL) base = ctx.parentURL
  if (base && !/\.[cm]?[jt]s$/.test(rest)) {
    for (const ext of ['.ts', '/index.ts']) {
      const u = new URL(rest + ext, base)
      if (existsSync(fileURLToPath(u))) return next(u.href, ctx)
    }
  }
  return next(spec, ctx)
}
export async function load(url, ctx, next) {
  const r = await next(url, ctx)
  if (url.endsWith('.ts') && r.source && String(r.source).includes('import.meta.env')) {
    r.source = "if (!import.meta.env) import.meta.env = { DEV: false, PROD: true, MODE: 'production' };\n" + String(r.source)
  }
  return r
}
"""

REGISTER = r"""
import { register } from 'node:module'
register('./hooks.mjs', import.meta.url, { data: { web: process.env.AWR_WEB_DIR } })
"""

OPEN = r"""
import { readFileSync } from 'node:fs'
const web = process.argv[2], world = process.argv[3]
const { openWorld } = await import(web + '/src/engine/pointcloud/io/openWorld.ts')
const { Fetcher } = await import(web + '/src/engine/pointcloud/io/fetcher.ts')
const f = async (url: string, init: any = {}) => {
  let b: Buffer
  try { b = readFileSync(decodeURIComponent(new URL(url).pathname)) } catch { return new Response(null, { status: 404 }) }
  const h = init.headers
  const r = (h instanceof Headers ? h.get('Range') : h?.Range ?? h?.range) as string | undefined
  if (!r) return new Response(b, { status: 200 })
  const m = /bytes=(\d+)-(\d+)/.exec(r)!
  const total = b.length
  b = b.subarray(+m[1], +m[2] + 1)
  return new Response(b, { status: 206, headers: { 'Content-Range': `bytes ${m[1]}-${+m[1] + b.length - 1}/${total}` } })
}
const fetcher = new Fetcher({ workers: 1, limit: 4, useWorker: false, f })
const o = await openWorld('file://' + world + '/', { fetcher, poolCapacityPts: 2_000_000, startHi: 1, now: () => performance.now(), f, protocolOf: () => 'h2' })
let pts = 0
for (let i = 0; i < o.store.N; i++) pts += o.store.numPoints[i]
console.log(JSON.stringify({ contentVersion: o.world.contentVersion, roots: o.metas.length, nodes: o.store.N, points: pts,
  firstScreenNodes: o.firstScreen.length, firstScreenBytes: o.firstScreenBytes, bpp: o.bpp, compression: o.compression }))
"""


def _node_ok() -> bool:
    node = shutil.which("node")
    if node is None or not (WEB / "src/engine/pointcloud/io/openWorld.ts").is_file():
        return False
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip().lstrip("v")
        major, minor = (int(x) for x in v.split(".")[:2])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return (major, minor) >= (22, 7)


@pytest.mark.skipif(not _node_ok(), reason="[platform] Node >= 22.7 (--experimental-transform-types) and apps/web sources needed")
def test_m05_open_world_reads_crop_unchanged(tmp_path: Path) -> None:
    for name, text in (("hooks.mjs", HOOKS), ("register.mjs", REGISTER), ("open.ts", OPEN)):
        (tmp_path / name).write_text(text, encoding="utf-8")
    env = {**os.environ, "AWR_WEB_DIR": str(WEB)}
    p = subprocess.run(["node", "--experimental-transform-types", "--no-warnings", "--import", "./register.mjs", "open.ts", str(WEB), str(CROP)],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-2000:]
    out = json.loads(p.stdout.strip().splitlines()[-1])
    w = _world()
    md = json.loads((CROP / next(L for L in w["layers"] if L["type"] == "pointcloud")["roots"][0]["href"] / "metadata.json").read_text())
    assert out["contentVersion"] == w["contentVersion"] and out["roots"] == 1 and out["bpp"] == 12 and out["compression"] == "none"
    assert out["points"] == md["points"] and out["nodes"] > 1 and out["firstScreenNodes"] >= 1 and out["firstScreenBytes"] > 0
