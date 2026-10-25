"""快速上手示例在临时 venv 中经线协议运行（D3-AC-58：`pip install anet4d` 后按文档在 15 min 内以 `make(..., physics=...)` 运行示例
Agent，同一 Agent 代码在各物理档与 L1、L2 上不改动；三个基线可运行，同一种子动作流逐字节一致）。

Env 在本测试进程中构造（仓库环境，`awr`）；Agent 是 venv 中 `python -m anet4d.serve` 的独立进程（只装了 wheel，无 `awr`、
无 `PYTHONPATH`），经 docs/sdk/examples/run_quickstart.py 的 `host_sockets` 接入，与文档第 4 节的套接字方式相同。

- 运动学开发档（本机，合成世界与事件）：示例 Agent 的 L1 回合完成；动作全部通过 `bench/act.v1`；与源码树进程内运行的动作流、
  位姿摘要逐字节一致；从新建 venv 到首个回合结束的墙钟 ≤ 15 min；
- L2（CPU 替身光栅相机）：同一示例文件以 wheel 中的 `ref_det_v1` 权重做推理，回合完成，动作流与进程内运行逐字节一致；
- 三个基线（`static_guard`、`coverage_scan`、`track_share`）以 wheel 中的 `--baseline` 运行，动作流与源码树进程内运行逐字节一致；
- Realistic 档（d3-gz 镜像内，crop 世界与 crop-handoff-001）：`AWR_D3_SDK_WORLDS_ROOT`、`AWR_D3_SDK_EVENTS_ROOT` 指向世界与
  事件根目录时运行 3 机 L1 回合（`run_quickstart.py` 同一路径，gz `launcher = local`；世界目录只读时以
  `AWR_D3_SDK_GZ_EXPORT_ROOT` 指定 gz 导出目录，`AWR_D3_SDK_REALISTIC_S` 覆盖仿真时长，缺省 30 s）；
- Fast 档（P0b，P07 交付 `awr.phys.fast` 后启用）：同一示例在 `physics = fast` 上运行。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from pkgfix import EXAMPLES, ROOT, clean_env

FIFTEEN_MIN_S = 900.0


def _quickstart():
    spec = importlib.util.spec_from_file_location("run_quickstart", EXAMPLES / "run_quickstart.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _example_factory():
    spec = importlib.util.spec_from_file_location("plaza_watch_src", EXAMPLES / "plaza_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PlazaWatch


def _lines(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _workdir(tmp_path: Path) -> Path:
    """研究者的工作目录：只放示例 Agent 文件（文档第 3 步）。"""
    w = tmp_path / "work"
    w.mkdir()
    shutil.copy(EXAMPLES / "plaza_watch.py", w / "plaza_watch.py")
    return w


def _remote(env, venv, sock_root: Path, work: Path, agent_args: list[str], logs: Path) -> tuple[dict, dict, list]:
    """venv 中每架机一个 `python -m anet4d.serve` 进程接入 Env；→ (运行清单, 线协议统计, [(returncode, stderr)])。"""
    qs = _quickstart()
    procs: list[subprocess.Popen] = []
    logs.mkdir(parents=True, exist_ok=True)

    def launch(paths: dict[str, Path]) -> None:
        for u, p in paths.items():
            err = open(logs / f"{u}.stderr", "w", encoding="utf-8")    # noqa: SIM115  子进程继承句柄，随进程退出关闭
            procs.append(subprocess.Popen([str(venv.python), "-m", "anet4d.serve", *agent_args, "--sock", str(p)], cwd=work,
                                          env=clean_env(), stdout=subprocess.DEVNULL, stderr=err))

    try:
        manifest, stats = qs.host_sockets(env, sock_root, hello_timeout_s=60.0, step_budget_s=30.0, on_listen=launch)
    finally:
        for p in procs:
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                p.kill()
    return manifest, stats, [(p.returncode, (logs / f"{u}.stderr").read_text(encoding="utf-8")) for p, u in zip(procs, env.possible_agents, strict=True)]


def _local(env, factory) -> dict:
    from anet4d.runner import run_episode

    return run_episode(env, lambda _u: factory())


def _check_run(out: Path, manifest: dict, stats: dict, procs: list, n_ticks: int) -> None:
    from awr.agentsdk.schemas import bench_validator

    assert all(rc == 0 for rc, _err in procs), [err[-2000:] for _rc, err in procs]
    assert manifest["n_ticks"] == n_ticks
    assert all(a["timeouts"] == 0 for a in manifest["agents"].values()), manifest["agents"]
    assert all(s["exited"] is None and s["timeouts"] == 0 and s["protocol_errors"] == 0 for s in stats.values()), stats
    val = bench_validator("act")
    for u in manifest["agents"]:
        rows = _lines(out / "agents" / u / "acts.jsonl")
        assert len(rows) == n_ticks
        for row in rows:
            assert not [e.message for e in val.iter_errors(row["act"])], row
        assert all(x["result"] == "applied" for row in rows for x in row["acks"]), u


def _same_acts(a: Path, b: Path, uavs) -> None:
    for u in uavs:
        assert (a / "agents" / u / "acts.jsonl").read_bytes() == (b / "agents" / u / "acts.jsonl").read_bytes(), u


# ------------------------------------------------------------------ 运动学开发档：示例 Agent（L1）
def _fleet2():
    from awr.agentsdk.env.spec import FleetEntry

    return (FleetEntry("uav-01", (-10.0, 50.0, 0.0)), FleetEntry("uav-02", (10.0, 50.0, 0.0)))


def _synth_spec(synth_event, out: Path, *, physics: str = "kinematic", dur: float = 30.0, **kw):
    from awr.agentsdk.env.spec import EnvSpec

    w, ev = synth_event
    return EnvSpec("qs-5", 5, w, ev, kw.pop("fleet", None) or _fleet2(), physics=physics, duration_s=dur, out_dir=out, **kw)


def test_example_l1_from_clean_venv(clean_venv, synth_event, tmp_path: Path) -> None:
    from awr.agentsdk.env.core import BenchEnv

    work = _workdir(tmp_path)
    m1, stats, procs = _remote(BenchEnv(_synth_spec(synth_event, tmp_path / "remote")), clean_venv, tmp_path / "sock", work,
                               ["plaza_watch:PlazaWatch"], tmp_path / "logs")
    elapsed = time.monotonic() - clean_venv.t0
    _check_run(tmp_path / "remote", m1, stats, procs, 300)
    assert all("k=100 detections person=" in err for _rc, err in procs)           # 示例的标准错误输出（文档第 4 步）
    m0 = _local(BenchEnv(_synth_spec(synth_event, tmp_path / "local")), _example_factory())
    assert m1["pose_sha256"] == m0["pose_sha256"]
    _same_acts(tmp_path / "remote", tmp_path / "local", m1["agents"])
    s = _quickstart().summarize(m1, tmp_path / "remote", stats)
    assert s["n_ticks"] == 300 and s["physics"] == "kinematic" and s["obs_level"] == "L1"
    assert s["acts_sha256"] == _quickstart().summarize(m0, tmp_path / "local")["acts_sha256"]
    # 上手计时：新建 venv、安装 wheel、示例 Agent 完成首个回合（本机；dmax 干净容器的正式演练由 A11 记录）
    assert elapsed <= FIFTEEN_MIN_S, elapsed
    (tmp_path / "timing.json").write_text(json.dumps({"install_s": clean_venv.install_s, "first_run_s": elapsed}), encoding="utf-8")


# ------------------------------------------------------------------ L2：同一示例文件，wheel 中的参考检测器
class _FakeRaster:
    """EGL 光栅相机的 CPU 替身（同 tests/d3/env/test_l2.py、tests/d3/baselines/test_act_reports.py）：按位姿生成确定性的渐变图。"""

    def render(self, cm, R_ws, p_w, *, fx=None, rs_nodes=None, actors=None, linear=False):
        from awr.sensorsvc.camera.raster import RenderOut

        h, w = cm.height, cm.width
        v = int(abs(float(p_w[0]) * 7 + float(p_w[1]) * 3)) % 200
        rgb = np.broadcast_to((np.arange(w, dtype=np.uint16)[None, :, None] * 255 // w + v).astype(np.uint8) % 255, (h, w, 3)).copy()
        z16, z8 = np.zeros((h, w), np.uint16), np.zeros((h, w), np.uint8)
        return RenderOut(rgb, np.zeros((h, w), np.float32), z16, z8, 0.0, 0.0, 0, instance_unocc=z16)


def _l2_env(synth_event, out: Path, cache: Path):
    from awr.agentsdk.env.core import BenchEnv
    from awr.agentsdk.env.l2 import L2Config, L2Sensors
    from awr.agentsdk.env.spec import FleetEntry

    spec = _synth_spec(synth_event, out, dur=8.0, obs_level="L2", fleet=(FleetEntry("uav-01", (-10.0, 50.0, 30.0), 0.0, airborne=True),))

    def l2(e):
        return L2Sensors(world_dir=Path(e.spec.world_dir), tape=e.tape, uavs=e.uavs, seed=e.spec.seed, noise=e.spec.noise,
                         obs_level=e.spec.obs_level, nav=e.nav, mount_t_bs_m=e.cam.mount_t_bs_m, hfov_deg=e.cam.hfov_deg,
                         cfg=L2Config(camera_profile="rgb720-gs", lidar=False, cache_dir=cache), renderer=_FakeRaster())

    return BenchEnv(spec, l2=l2)


def test_example_l2_from_clean_venv(clean_venv, synth_event, tmp_path: Path) -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("foxglove_schemas_protobuf")                   # raw.mcap 的相机帧通道（录制器写线程）
    work = _workdir(tmp_path)
    m1, stats, procs = _remote(_l2_env(synth_event, tmp_path / "remote", tmp_path / "wq"), clean_venv, tmp_path / "sock", work,
                               ["plaza_watch:PlazaWatch"], tmp_path / "logs")
    _check_run(tmp_path / "remote", m1, stats, procs, 80)
    assert m1["obs_level"] == "L2"
    seen: list[int] = []
    base = _example_factory()

    class Counting(base):                                    # 进程内对照：记录交付的相机帧数，确认 L2 走参考检测器
        def step(self, obs, inbox):
            seen.append(len(obs["sensors"].get(self.cam, [])))
            assert type(self.perception).__name__ == "L2Perception"
            return super().step(obs, inbox)

    m0 = _local(_l2_env(synth_event, tmp_path / "local", tmp_path / "wq"), Counting)
    assert sum(seen) > 0
    _same_acts(tmp_path / "remote", tmp_path / "local", m1["agents"])
    assert m1["pose_sha256"] == m0["pose_sha256"]


# ------------------------------------------------------------------ 三个基线：wheel 中的 --baseline
@pytest.mark.parametrize("policy", ["static_guard", "coverage_scan", "track_share"])
def test_baselines_from_wheel_match_source(clean_venv, synth_event, tmp_path: Path, policy: str) -> None:
    from anet4d.baselines import BASELINES

    from awr.agentsdk.env.core import BenchEnv

    m1, stats, procs = _remote(BenchEnv(_synth_spec(synth_event, tmp_path / "remote", dur=25.0)), clean_venv, tmp_path / "sock",
                               _workdir(tmp_path), ["--baseline", policy], tmp_path / "logs")
    _check_run(tmp_path / "remote", m1, stats, procs, 250)
    m0 = _local(BenchEnv(_synth_spec(synth_event, tmp_path / "local", dur=25.0)), BASELINES[policy])
    _same_acts(tmp_path / "remote", tmp_path / "local", m1["agents"])
    assert m1["pose_sha256"] == m0["pose_sha256"]


# ------------------------------------------------------------------ Realistic 档（d3-gz 镜像，crop 世界）
def _has_gz() -> bool:
    return shutil.which("gz") is not None and any((Path(p) / "libDrone4DGz.so").exists()
                                                  for p in os.environ.get("GZ_SIM_SYSTEM_PLUGIN_PATH", "").split(":") if p)


def test_example_realistic_crop_from_clean_venv(clean_venv, tmp_path: Path) -> None:
    if not _has_gz():
        pytest.skip("needs gz-sim with libDrone4DGz.so (d3-gz image)")
    worlds, events = os.environ.get("AWR_D3_SDK_WORLDS_ROOT"), os.environ.get("AWR_D3_SDK_EVENTS_ROOT")
    if not worlds or not events or not (Path(worlds) / "oryahovo-crop1").is_dir() or not (Path(events) / "crop-handoff-001" / "v1").is_dir():
        pytest.skip("[data] needs worlds/oryahovo-crop1 and the compiled crop-handoff-001 (set AWR_D3_SDK_WORLDS_ROOT, AWR_D3_SDK_EVENTS_ROOT)")
    pytest.importorskip("zmq")
    from anet4d.local import make  # = anet4d.bench.make（d3-gz 镜像不含 httpx）

    rs = json.loads((ROOT / "configs/bench.d3/crop-handoff-001.l1-3uav.run_spec.json").read_text(encoding="utf-8"))
    dur = float(os.environ.get("AWR_D3_SDK_REALISTIC_S", "30"))
    rs.update(ranked=False, duration_s=dur)
    env = make(rs, run_id="quickstart-1", seed=1, worlds_root=worlds, events_root=events, out_dir=tmp_path / "remote", physics="realistic",
               gz={"launcher": "local", **({"export_root": x} if (x := os.environ.get("AWR_D3_SDK_GZ_EXPORT_ROOT")) else {})})
    m1, stats, procs = _remote(env, clean_venv, tmp_path / "sock", _workdir(tmp_path), ["plaza_watch:PlazaWatch"], tmp_path / "logs")
    elapsed = time.monotonic() - clean_venv.t0
    _check_run(tmp_path / "remote", m1, stats, procs, round(dur * 10))
    assert m1["physics"] == "realistic" and m1["tier"] and m1["pose_sha256"]
    s = _quickstart().summarize(m1, tmp_path / "remote", stats)
    print(json.dumps({**s, "first_run_s": round(elapsed, 1), "install_s": round(clean_venv.install_s, 1)}, sort_keys=True))
    assert elapsed <= FIFTEEN_MIN_S, elapsed


# ------------------------------------------------------------------ Fast 档（P0b，P07）
def test_example_fast_from_clean_venv(clean_venv, synth_event, tmp_path: Path) -> None:
    if importlib.util.find_spec("awr.phys.fast") is None:
        pytest.skip("[platform] physics=fast (FleetSim) is delivered by P07; the example is added to this tier when it lands")
    from awr.agentsdk.env.core import BenchEnv

    work = _workdir(tmp_path)
    m1, stats, procs = _remote(BenchEnv(_synth_spec(synth_event, tmp_path / "remote", physics="fast")), clean_venv, tmp_path / "sock", work,
                               ["plaza_watch:PlazaWatch"], tmp_path / "logs")
    _check_run(tmp_path / "remote", m1, stats, procs, 300)
    assert m1["physics"] == "fast"
    m0 = _local(BenchEnv(_synth_spec(synth_event, tmp_path / "local", physics="fast")), _example_factory())
    assert m1["pose_sha256"] == m0["pose_sha256"]
    _same_acts(tmp_path / "remote", tmp_path / "local", m1["agents"])
