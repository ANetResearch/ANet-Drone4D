"""numba 预热签名覆盖运行期签名（M08-FR-005；D1 验收第 1 轮 4.1；D1-AC-15）。

sim-core 在 ready 之前完成全部 njit 核的编译或读缓存（M08 融合核，以及插件装配时预热的 M09 安全核、M10 跟踪核、M07 湍流
采样核）。运行期若出现预热未覆盖的签名（只读 ENU 视图、只读 DSM/DTM 网格、float32 记分板），numba 会在主循环内编译
3–4 s，被 supervisor 的 2 s 活性阈值杀掉，编译结果写不进缓存，冷缓存下形成崩溃循环。

本用例在独立子进程中以全部插件与真实城市世界运行 sim-core（进程内、假墙钟全速推进）：S1 剧本起飞、立面跟踪与双机间距
扫描，随后全机 RTL；比较 `start()` 之后与运行结束时各 numba 调度器的签名集合，运行期不得新增签名。另有不依赖世界数据的
快速用例：直接以运行期类型（只读视图、float32 记分板）调用 M09 与 M10 的核，签名数不得增加。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]

_SCRIPT = r"""
import json, os, secrets, sys
sys.path.insert(0, os.path.join(sys.argv[1], "python"))
os.environ["AWR_SCENARIO_PROFILE"] = "ci"
os.environ["AWR_PLAN_POOL"] = "process"  # 与 sim-core 相同：规划核在 plan-pool 工作进程中编译
from numba.core.registry import CPUDispatcher
from awr.contracts import LAYOUT_ID
from awr.runtime.bus import LocalBus
from awr.runtime.statering import LocalRing
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.runtime.config import SimConfig
from awr.sim.runtime.main import SimCore
from awr.sim.runtime.warm import DEFAULT_PLUGINS

def snap():
    out = {}
    for mn, m in list(sys.modules.items()):
        if not mn.startswith("awr") or m is None:
            continue
        for v in list(vars(m).values()):
            if isinstance(v, CPUDispatcher):
                out[v.py_func.__module__ + "." + v.py_func.__name__] = sorted(map(str, v.signatures))
    return out

def main():
    run = "ns" + secrets.token_hex(4)
    path = "/nsig/" + run + "/state.sim-core"
    ring, _ = LocalRing.open_or_create(path, capacity=1024, slots=32, layout_id=LAYOUT_ID, id_base=0, id_count=1024)
    bus = LocalBus.open("sim-core", namespace="awr/test/" + run)
    W = [1_000_000_000]
    cfg = SimConfig(run_id=run, world_id="shenzhen", n_vehicles=1, autoplay=True, plugins=DEFAULT_PLUGINS,
                    scenario="s1-shenzhen-facade")
    core = SimCore(cfg, bus, ring, secret=None, wall_ns=lambda: W[0])
    core.start()
    s0 = snap()
    OP = {"principal_id": "p-op", "role": "operator", "entry": "api", "seat": True}
    core._lease_op({"v": 1, "cid": "seat", "op": "seat_claim", "principal": OP})
    dur, rtl_at = float(sys.argv[2]), float(sys.argv[3])
    rtl = False
    tracked = 0
    while core.clock.t_ns < dur * 1e9:
        W[0] += 5 * TICK_NS * max(1, int(core.clock.rate))
        core.iterate()
        tracked = max(tracked, int((core.fleet.S.ctrl_mode == 15).sum()))
        if not rtl and core.clock.t_ns >= rtl_at * 1e9:
            rtl = True
            core.engine.handle({"v": 1, "cid": "rtl-all", "op": "rtl", "uav": "*", "args": {}, "principal": OP, "lease": None,
                                "t_wall_ns": 0, "epoch_seen": 1, "batch_id": None})
    s1 = snap()
    new = {k: sorted(set(v) - set(s0.get(k, []))) for k, v in s1.items() if set(v) - set(s0.get(k, []))}
    print("RESULT " + json.dumps({"new": new, "tracked": tracked, "n": len(core.roster.by_slot),
                                  "dispatchers": len(s1)}), flush=True)
    core.stop()
    import awr.sim.mission as M10
    rt = M10.installed_runtime()
    if rt is not None:
        rt.close()


if __name__ == "__main__":  # plan-pool 以 spawn 启动工作进程，会重新导入主模块
    main()
"""


def _world_ready() -> bool:
    base = Path(os.environ.get("AWR_WORLDS_DIR") or ROOT / "worlds")
    return (base / "shenzhen" / "world.json").exists()


@pytest.mark.needs_data
def test_runtime_signatures_covered_by_warmup(tmp_path: Path) -> None:
    if not _world_ready():
        pytest.skip("worlds/shenzhen not built（make worlds）")
    if os.environ.get("AWR_KERNEL", "").strip().lower() == "numpy":
        pytest.skip("numpy kernel")
    pytest.importorskip("numba")
    script = tmp_path / "sig.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    env = dict(os.environ, AWR_RUN_DIR=str(tmp_path))
    r = subprocess.run([sys.executable, str(script), str(ROOT), "160", "140"], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=900)
    line = next((x for x in r.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line is not None, r.stdout[-2000:] + r.stderr[-4000:]
    res = json.loads(line[len("RESULT "):])
    assert res["n"] == 2 and res["tracked"] >= 1, res      # 双机在场、M10 跟踪核（TRAJ）确实运行过
    assert res["dispatchers"] >= 10, res
    assert res["new"] == {}, json.dumps(res["new"], indent=1)


def test_safety_and_tracker_warmup_cover_runtime_types() -> None:
    """不依赖世界：M09 `fleet_scan`、`geofence_scan` 与 M10 `track_step` 以运行期类型调用时不再编译。"""
    nb = pytest.importorskip("numba")
    _ = nb
    from awr.sim.planning import kernels_track as KT

    fresh = "awr.sim.safety" not in sys.modules
    from awr.sim.safety import kernels as KN

    if fresh:  # 导入 awr.sim.safety 即装配到全局登记表（组合根约定）；本用例只用核，撤销装配以免影响同进程的其他用例
        import awr.sim.safety as m09

        m09.uninstall()

    if not (KN.HAVE_NUMBA and KT.HAVE_NUMBA):
        pytest.skip("numba kernels disabled")
    KN.warmup()
    KT.warmup()
    n0 = {f: len(f.signatures) for f in (KN.fleet_scan, KN.geofence_scan, KT.track_step)}
    p = np.zeros((8, 3))
    p[1] = (3.0, 0.0, 0.0)
    p.flags.writeable = False
    v = np.zeros((8, 3))
    v.flags.writeable = False
    idx = np.array([0, 1], np.int64)
    sep = np.full(8, 1e9, np.float32)
    mate = np.full(8, -1, np.int32)
    pairs = np.zeros((8, KN.PAIR_COLS))
    KN.fleet_scan(p, v, idx, 0, 4, 10.0, 3.0, 3.0, 10.0, 10.0, np.full(8, 0.5), 0.1, sep, mate, sep.copy(), mate.copy(),
                  pairs, 8)
    ea = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]])
    o, om = np.full(8, 1e9), np.full(8, -1, np.int64)
    KN.geofence_scan(p, idx, ea, np.roll(ea, -1, axis=0), ea, np.roll(ea, -1, axis=0), np.array([0], np.int64),
                     np.array([4], np.int64), np.array([-1e9]), np.array([1e9]), np.array([[0.0, 0.0, 10.0, 10.0]]),
                     np.array([1], np.int64), np.array([True]), 50.0, o, o.copy(), om, om.copy(), om.copy(), o.copy())
    assert {f: len(f.signatures) for f in n0} == n0
