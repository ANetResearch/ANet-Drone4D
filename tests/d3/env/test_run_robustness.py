"""正式运行稳健性与安全（K18；D3-AC-16、D3-AC-57 (d)）：排名运行种子明文不进运行目录、GPU 丢失转为 647 平台错误、L2 世界查询缓存路径
可配置。合成世界与运动学开发档（`synth_event`）；GPU 体检以替换的设备节点目录与 `os.open` 模拟宿主 systemd reload 后的设备丢失。"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

from awr.agentsdk.env.core import SEED_FILE, BenchEnv, write_run_seed
from awr.agentsdk.env.spec import EnvSpec, FleetEntry
from awr.sensorsvc import gpuhealth
from awr.sensorsvc.errors import SensorSvcError

FLEET = (FleetEntry("uav-01", (-10.0, 50.0, 0.0)), FleetEntry("uav-02", (10.0, 50.0, 0.0)))
SECRET = 0x5EED_C0DE_1234_ABCD                     # 排名运行的 CSPRNG 种子（检索其十进制与十六进制文本）


def _spec(synth_event, out: Path | None, **kw) -> EnvSpec:
    w, ev = synth_event
    return EnvSpec(kw.pop("run_id", "k18-run"), kw.pop("seed", SECRET), w, ev, FLEET, physics="kinematic", duration_s=3.0, out_dir=out, **kw)


def _hover(env: BenchEnv) -> dict:
    obs, _ = env.reset()
    while env.agents:
        obs, *_ = env.step({u: {"schema": "drone4d.bench.act.v1", "k": o["obs"]["k"]} for u, o in obs.items()})
    return env.close()


def _grep(root: Path, needles: list[bytes]) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if p.is_file():
            b = p.read_bytes()
            hits += [f"{p.relative_to(root)}:{n.decode()}" for n in needles if n in b]
    return hits


# ============================================================================================ 种子明文（B21 遗留 8）


def test_unranked_run_keeps_seed_in_truth_layer(synth_event, tmp_path: Path) -> None:
    m = _hover(BenchEnv(_spec(synth_event, tmp_path / "run", seed=11)))
    assert json.loads((tmp_path / "run" / "truth" / SEED_FILE).read_text()) == {"run_seed": 11}
    assert m["seed_store"] == "run_dir"


def test_ranked_run_writes_no_plaintext_seed(synth_event, tmp_path: Path) -> None:
    """排名运行：运行目录任何文件都不含种子明文（十进制、十六进制），`truth/run_seed.json` 不存在，清单记 `evalstore`。"""
    run = tmp_path / "run"
    m = _hover(BenchEnv(_spec(synth_event, run, ranked=True, seed_commit="hmac:" + "ab" * 32)))
    assert not list(run.rglob(SEED_FILE))
    assert m["seed_store"] == "evalstore" and "seed" not in m
    assert _grep(run, [str(SECRET).encode(), f"{SECRET:x}".encode(), f"{SECRET:X}".encode()]) == []
    assert json.loads((run / "manifest.json").read_text())["seed_store"] == "evalstore"


@pytest.mark.parametrize("explicit", [False, True])
def test_ranked_run_seed_goes_to_hidden(synth_event, tmp_path: Path, explicit: bool) -> None:
    """数据集布局的运行目录：种子只写入同一数据根的隐藏区 `hidden/<eid>/truth/runs/<run_id>/`（或显式 `seed_sink`）：新建目录 0700、
    文件 0600，运行目录仍无明文；Rerun 导出从隐藏区取种子（评测 catalog 的种子列）。"""
    from awr.bench.recorder.rerun_export import _seed_path

    data = tmp_path / "data"
    run = data / "dataset" / "ev" / "runs" / "r1"
    sink = (tmp_path / "elsewhere" / "r1") if explicit else data / "hidden" / "ev" / "truth" / "runs" / "r1"
    kw = {"seed_sink": sink} if explicit else {}
    m = _hover(BenchEnv(_spec(synth_event, run, ranked=True, seed_commit="hmac:" + "cd" * 32, run_id="r1", **kw)))
    assert m["seed_store"] == "hidden"
    assert json.loads((sink / SEED_FILE).read_text()) == {"run_id": "r1", "run_seed": SECRET}
    assert stat.S_IMODE((sink / SEED_FILE).stat().st_mode) == 0o600 and stat.S_IMODE(sink.stat().st_mode) == 0o700
    assert not list(run.rglob(SEED_FILE)) and _grep(run, [str(SECRET).encode()]) == []
    assert not [p for p in sink.iterdir() if p.name != SEED_FILE]              # 临时文件已改名
    if not explicit:
        assert stat.S_IMODE((data / "hidden").stat().st_mode) == 0o700
        assert _seed_path(run, "r1") == sink / SEED_FILE


def test_unwritable_derived_hidden_falls_back_to_evalstore(synth_event, tmp_path: Path, monkeypatch) -> None:
    """布局推导的隐藏区不可写（属主为 Evaluator 服务用户）：不写种子、不中断运行，记 `evalstore`；显式目录不可写时抛出。"""
    import awr.agentsdk.env.core as core

    def deny(_d):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(core, "_mkdir_hidden", deny)
    run = tmp_path / "data" / "dataset" / "ev" / "runs" / "r2"
    sp = _spec(synth_event, run, ranked=True, seed_commit="hmac:" + "ef" * 32, run_id="r2")
    assert write_run_seed(sp) == "evalstore" and not (tmp_path / "data" / "hidden").exists()
    with pytest.raises(PermissionError):
        write_run_seed(_spec(synth_event, run, ranked=True, seed_commit="hmac:" + "ef" * 32, seed_sink=tmp_path / "h"))


def test_seed_sink_does_not_change_run_identity(synth_event, tmp_path: Path) -> None:
    a = _spec(synth_event, None, ranked=True, seed_commit="hmac:" + "11" * 32)
    b = _spec(synth_event, None, ranked=True, seed_commit="hmac:" + "11" * 32, seed_sink=tmp_path / "hidden")
    assert a.identity() == b.identity()
    assert write_run_seed(_spec(synth_event, None)) is None                  # 不排名且不写盘


def test_from_run_spec_passes_seed_sink(tmp_path: Path) -> None:
    rs = {"world": {"id": "w"}, "event": {"id": "e", "version": 1}, "obs_level": "L1", "ranked": True,
          "fleet": [{"uav_id": "uav-01", "start": {"kind": "pose", "pos_enu_m": [0, 0, 0], "yaw_deg": 0}}]}
    sp = EnvSpec.from_run_spec(rs, run_id="r", seed=1, worlds_root=tmp_path, events_root=tmp_path, seed_commit="c" * 64,
                               seed_sink=tmp_path / "h")
    assert sp.seed_sink == tmp_path / "h"
    assert EnvSpec.from_run_spec(rs, run_id="r", seed=1, worlds_root=tmp_path, events_root=tmp_path, seed_commit="c" * 64).seed_sink is None


# ============================================================================================ GPU 丢失（B21 遗留 3）


@pytest.fixture()
def fake_dev(tmp_path: Path, monkeypatch):
    """替换的设备节点目录：nvidiactl、nvidia2、nvidia-uvm 为普通文件；`denied` 中的节点 open 报 EPERM（设备 cgroup 拒绝）。"""
    d = tmp_path / "dev"
    d.mkdir()
    for n in ("nvidiactl", "nvidia2", "nvidia-uvm", "null"):
        (d / n).write_bytes(b"")
    denied: set[str] = set()
    real_open = os.open

    def fake_open(path, flags, *a, **kw):
        if Path(path).name in denied:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(gpuhealth, "DEV_DIR", d)
    monkeypatch.setattr(gpuhealth.os, "open", fake_open)
    monkeypatch.delenv("NVIDIA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv(gpuhealth.REQUIRE_ENV, raising=False)
    monkeypatch.delenv(gpuhealth.PROBE_ENV, raising=False)
    return d, denied


def test_device_nodes_lost_after_cgroup_reset(fake_dev) -> None:
    _d, denied = fake_dev
    ok = gpuhealth.check_device_nodes()
    assert ok["ok"] and sorted(Path(x).name for x in ok["nodes"]) == ["nvidia-uvm", "nvidia2", "nvidiactl"]   # 只查 NVIDIA 节点
    denied.update({"nvidiactl", "nvidia2"})
    lost = gpuhealth.check_device_nodes()
    assert not lost["ok"] and {(Path(x["node"]).name, x["errno"]) for x in lost["lost"]} == {("nvidiactl", "EPERM"), ("nvidia2", "EPERM")}
    with pytest.raises(SensorSvcError) as e:
        gpuhealth.require_gpu(egl=True, stage="l2")                           # 节点丢失时不启动 EGL 探测子进程
    assert e.value.code == 647 and e.value.name == "SENSOR_GPU_UNAVAILABLE"
    assert e.value.extra["cause"] == "gpu_lost" and e.value.extra["platform_failure"] and e.value.extra["retryable"]
    assert "egl" not in e.value.extra["probe"] and "nvidiactl (EPERM)" in str(e.value)


def test_require_gpu_expectation_rules(fake_dev, monkeypatch, tmp_path: Path) -> None:
    _ = fake_dev
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(gpuhealth, "DEV_DIR", empty)
    assert gpuhealth.require_gpu(egl=True) == {"mode": "full", "stage": "session", "expected": False}   # 无 GPU 的机器：不体检
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "2")                         # 容器声明了 GPU 而设备节点全无
    with pytest.raises(SensorSvcError) as e:
        gpuhealth.require_gpu(egl=False)
    assert e.value.code == 647 and "no NVIDIA device node" in str(e.value)
    monkeypatch.setenv("NVIDIA_VISIBLE_DEVICES", "void")
    assert not gpuhealth.gpu_expected()
    monkeypatch.setenv(gpuhealth.PROBE_ENV, "off")
    monkeypatch.setenv(gpuhealth.REQUIRE_ENV, "1")
    assert gpuhealth.require_gpu() == {"mode": "off", "stage": "session", "expected": False}
    monkeypatch.setenv(gpuhealth.PROBE_ENV, "bogus")
    with pytest.raises(ValueError):
        gpuhealth.require_gpu()


def test_warp_without_cuda_is_gpu_loss(fake_dev, monkeypatch) -> None:
    """设备节点在、Warp 却看不到 CUDA 设备（CUDA error 100）：647，不静默退到 Warp CPU 设备。"""
    monkeypatch.setattr(gpuhealth, "_warp_cuda_count", lambda: 0)
    with pytest.raises(SensorSvcError) as e:
        gpuhealth.require_gpu(egl=False, warp=True, mode="nodes")
    assert e.value.code == 647 and "CUDA error 100" in str(e.value)
    monkeypatch.setattr(gpuhealth, "_warp_cuda_count", lambda: 1)
    assert gpuhealth.require_gpu(egl=False, warp=True, mode="nodes")["warp_cuda_devices"] == 1


def test_open_mesh_lidar_refuses_silent_cpu_fallback(fake_dev, monkeypatch) -> None:
    pytest.importorskip("open3d")
    from awr.sensorsvc.lidar import warp_mesh

    monkeypatch.setattr(warp_mesh, "warp_cuda_available", lambda: False)
    monkeypatch.setattr(gpuhealth, "_warp_cuda_count", lambda: 0)
    with pytest.raises(SensorSvcError) as e:
        warp_mesh.open_mesh_lidar(None, None, [], backend="warp")
    assert e.value.code == 647


def test_egl_probe_survives_segfault() -> None:
    """EGL 探测在子进程中进行：子进程段错误只表现为探测失败（信号 11），本进程不受影响。"""
    r = gpuhealth.probe_egl(30, code="import os, signal; os.kill(os.getpid(), signal.SIGSEGV)")
    assert r["ok"] is False and r["signal"] == 11 and r["returncode"] == -11
    r = gpuhealth.probe_egl(30, code="import json; print(json.dumps({'ok': True, 'GL_RENDERER': 'fake'}))")
    assert r == {"returncode": 0, "ok": True, "GL_RENDERER": "fake"}
    r = gpuhealth.probe_egl(30, code="import sys; print('{\"ok\": false, \"error\": \"EGL_BAD_DEVICE\"}'); sys.exit(3)")
    assert r["ok"] is False and r["error"] == "EGL_BAD_DEVICE"


def test_egl_probe_failure_raises_647(fake_dev, monkeypatch) -> None:
    monkeypatch.setattr(gpuhealth, "probe_egl", lambda timeout_s: {"ok": False, "returncode": -11, "signal": 11,
                                                                   "error": "EGL probe killed by signal 11"})
    with pytest.raises(SensorSvcError) as e:
        gpuhealth.require_gpu(egl=True, stage="l2")
    assert e.value.code == 647 and "signal 11" in str(e.value)
    assert gpuhealth.require_gpu(egl=True, mode="nodes")["device_nodes"]["ok"]          # nodes 档不启动子进程
    monkeypatch.setattr(gpuhealth, "probe_egl", lambda timeout_s: {"ok": True, "returncode": 0, "GL_VENDOR": "Mesa",
                                                                   "GL_RENDERER": "llvmpipe (LLVM 19.1.7, 256 bits)"})
    with pytest.raises(SensorSvcError) as e:                                           # 软件渲染回退（dmax 复现）
        gpuhealth.require_gpu(egl=True, stage="l2")
    assert e.value.code == 647 and "software renderer" in str(e.value)
    monkeypatch.setattr(gpuhealth, "probe_egl", lambda timeout_s: {"ok": True, "returncode": 0, "GL_VENDOR": "NVIDIA Corporation",
                                                                   "GL_RENDERER": "NVIDIA A40/PCIe/SSE2"})
    assert gpuhealth.require_gpu(egl=True, stage="l2")["egl"]["GL_RENDERER"].startswith("NVIDIA")


def test_gpu_errors_classification(fake_dev) -> None:
    _, denied = fake_dev
    with pytest.raises(SensorSvcError) as e, gpuhealth.gpu_errors("lidar"):
        raise RuntimeError("Warp CUDA error 100: no CUDA-capable device is detected (in function init_cuda_driver)")
    assert e.value.code == 647 and isinstance(e.value.__cause__, RuntimeError)
    with pytest.raises(SensorSvcError) as e, gpuhealth.gpu_errors("lidar worker"):    # 工作进程回溯中的 647
        raise RuntimeError("lidar worker failed:\nawr.sensorsvc.errors.SensorSvcError: 647 SENSOR_GPU_UNAVAILABLE: lidar: ...")
    assert e.value.code == 647
    with pytest.raises(ValueError), gpuhealth.gpu_errors("camera"):                  # 与 GPU 无关的异常原样抛出
        raise ValueError("bad pose")
    denied.add("nvidia2")                                                             # 进程崩溃时设备节点已丢失
    with pytest.raises(SensorSvcError) as e, gpuhealth.gpu_errors("lidar worker"):
        raise RuntimeError("lidar worker exited (code -11)")
    assert e.value.code == 647 and "device nodes lost" in str(e.value)
    with pytest.raises(SensorSvcError) as e, gpuhealth.gpu_errors("x"):
        raise SensorSvcError(648, "render proc 0 exited")                           # 其他 M25 错误：设备节点不参与判定
    assert e.value.code == 648


def test_render_group_gpu_loss_is_647(fake_dev) -> None:
    """M25 渲染进程组（K13）的 GPU 丢失：回溯带 CUDA error 100 → 647；渲染进程在 EGL 中段错误而设备节点已被拒绝 → 647（exitcode -11）；
    设备节点正常时进程退出与 GPU 无关的异常仍为 648（K18，B21 遗留 3）。"""
    from awr.sensorsvc.procgroup import WORKER_LOST, RenderGroup

    _, denied = fake_dev
    target = f"{Path(__file__).with_name('_gpu_lost_proc.py')}:GpuLostProc"
    with RenderGroup(target, [{}, {}, {}]) as g:
        assert g.request(0, ("ping",)) == ("ping",)
        with pytest.raises(SensorSvcError) as e:
            g.request(0, ("cuda100",))
        assert e.value.code == 647 and e.value.extra["cause"] == "gpu_lost" and e.value.extra["platform_failure"] is True
        assert e.value.extra["retryable"] is True and e.value.extra["proc"] == 0
        with pytest.raises(SensorSvcError) as e:                                      # 与 GPU 无关的处理器异常：648
            g.request(0, ("boom",))
        assert e.value.code == WORKER_LOST
        with pytest.raises(SensorSvcError) as e:                                      # 设备节点正常时段错误：648
            g.request(1, ("segv",), timeout_s=60)
        assert e.value.code == WORKER_LOST and "exitcode -11" in str(e.value)
        denied.add("nvidia2")                                                         # 宿主 systemd reload：设备 cgroup 拒绝
        with pytest.raises(SensorSvcError) as e:
            g.request(2, ("segv",), timeout_s=60)
        assert e.value.code == 647 and e.value.extra["exitcode"] == -11 and "device nodes lost" in str(e.value)
        with pytest.raises(SensorSvcError) as e:                                      # 已退出进程再发送：同样按设备节点归类
            g.request(2, ("ping",), timeout_s=5)
        assert e.value.code == 647


def test_env_l2_gpu_loss_is_platform_error(synth_event, tmp_path: Path) -> None:
    """Env 建立 L2 传感器时 GPU 已丢失：reset 抛 647（平台失败、可重跑），而不是让驱动崩溃带走进程。"""
    def lost(_env):
        raise RuntimeError("Warp CUDA error 100: no CUDA-capable device is detected")

    env = BenchEnv(_spec(synth_event, tmp_path / "run", obs_level="L2"), l2=lost)
    with pytest.raises(SensorSvcError) as e:
        env.reset()
    assert e.value.code == 647 and e.value.extra["platform_failure"]


def test_l2_sensors_probe_before_egl(fake_dev, synth_event, tmp_path: Path) -> None:
    """L2Sensors 缺省渲染器（EGL）：设备节点打不开时在建立 EGL 上下文之前抛 647。"""
    from awr.agentsdk.env.l2 import L2Config, L2Sensors
    from awr.sim.events.tape import open_tape

    _, denied = fake_dev
    denied.add("nvidiactl")
    w, ev = synth_event
    with pytest.raises(SensorSvcError) as e:
        L2Sensors(world_dir=w, tape=open_tape(ev), uavs=["uav-01"], seed=1, noise="realistic", obs_level="L2", nav=None,
                  mount_t_bs_m=(0.1, 0.0, -0.05), hfov_deg=80.0, cfg=L2Config(lidar=False, imu=False, gnss=False))
    assert e.value.code == 647


# ============================================================================================ L2 世界查询缓存（B21 遗留 9）


def test_worldquery_cache_dir_precedence(tmp_path: Path, monkeypatch) -> None:
    from awr.agentsdk.env.l2 import WQ_CACHE_ENV, worldquery_cache_dir

    world = tmp_path / "d3dev" / "worlds" / "w1"
    world.mkdir(parents=True)
    monkeypatch.delenv(WQ_CACHE_ENV, raising=False)
    assert worldquery_cache_dir(world) == (tmp_path / "d3dev" / "cache" / "worldquery").resolve()     # 缺省：部署布局推导
    monkeypatch.setenv(WQ_CACHE_ENV, str(tmp_path / "wq-env"))
    assert worldquery_cache_dir(world) == tmp_path / "wq-env"
    assert worldquery_cache_dir(world, tmp_path / "wq-arg") == tmp_path / "wq-arg"


def test_bench_env_passes_l2_cache_dir(synth_event, tmp_path: Path) -> None:
    seen = {}

    class _Stop(Exception):
        pass

    import awr.agentsdk.env.l2 as l2mod

    orig = l2mod.L2Sensors.__init__

    def spy(self, **kw):
        seen["cache_dir"] = kw["cfg"].cache_dir
        raise _Stop

    l2mod.L2Sensors.__init__ = spy
    try:
        env = BenchEnv(_spec(synth_event, None, obs_level="L2"), l2_cache_dir=tmp_path / "wq")
        with pytest.raises(_Stop):
            env.reset()
    finally:
        l2mod.L2Sensors.__init__ = orig
    assert seen["cache_dir"] == tmp_path / "wq"
