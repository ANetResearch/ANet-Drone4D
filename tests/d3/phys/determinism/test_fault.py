"""gz 故障注入、看门狗与 reset 复用（D3-AC-17 故障注入条款、D3-AC-35 gz 健壮性条款；M24-FR-078、FR-079、FR-060，M24-AC-013、027）。

两组用例：

- 离线（本机即可运行）：`_FakeGz` 以线程模拟 Drone4DClock 的 REP 端（按速度设定积分的确定性机体），支持杀死、挂起与重启，
  覆盖 SupervisedGzBackend 的重启重放、逐 tick 比对、平台失败判定、连续 3 次失败与 reuse_policy 拒绝。
- dmax（d3-gz 容器内 `launcher = local`）：真实 gz 实例上的 `kill -9`、SIGSTOP 挂起与运行中（步进期间）杀死，重放后与无故障运行
  逐位一致；连续 3 次挂起失败（632）；同种子 reset 复用验证（判定 per_run）。`AWR_D3_FAULT_SOAK=1` 时追加 M24-AC-027 的浸泡用例
  （第 1,000 tick 起注入 kill -9 与步进超时各 10 次）。`AWR_D3_FAULT_OUT=<dir>` 时把恢复记录与验证结论写成 JSON。

运行：tools/deploy/dmax/run_gz_tests.sh /repo/tests/d3/phys/determinism/test_fault.py（或在 d3-gz 容器内直接 pytest）。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from awr.phys import PhysError
from awr.phys.adapter import TierCommand, VehicleStatus
from awr.phys.gz import protocol as P

zmq = pytest.importorskip("zmq")

STEP_NS = 4_000_000
G = 9.80665


# ---------------------------------------------------------------------------------------------------------------- 离线替身
class _FakeGz:
    """GzInstance 替身：线程内的最小 Drone4DClock（REP），机体按最近一次速度设定逐步积分（与 gz 一样对同一控制序列逐位确定）。

    `faults[life] = (kind, tick)`：第 life 次启动（0 为首次）的实例在收到 tick 的 Control（tick = -1 为 Hello）时 `kill`（不回复且
    退出）或 `hang`（不回复、保持存活）；`diverge_from_life` 起每个样本加 1e-12 m（模拟重启后平台不一致）。
    """

    def __init__(self, tmp: Path, n_uav: int, *, faults: dict[int, tuple[str, int]] | None = None, diverge_from_life: int | None = None) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.endpoint = f"ipc://{tmp / 'fake-ctl.sock'}"
        self.n_uav, self.faults, self.diverge_from_life = n_uav, dict(faults or {}), diverge_from_life
        self.restarts = 0
        self.lives = 0
        self._th: threading.Thread | None = None
        self._dead = threading.Event()
        self._hung = threading.Event()
        self._stop = threading.Event()
        self.start()

    # GzInstance 接口
    def start(self) -> None:
        self._dead.clear()
        self._hung.clear()
        self._stop.clear()
        ready = threading.Event()
        life = self.lives
        self.lives += 1
        self._th = threading.Thread(target=self._serve, args=(life, ready), daemon=True)
        self._th.start()
        ready.wait(5)

    def alive(self) -> bool:
        return not self._dead.is_set()

    def stop(self, force: bool = False) -> None:
        self._stop.set()
        if self._th is not None:
            self._th.join(5)
        self._dead.set()

    def restart(self, ready_timeout_s: float = 60.0) -> None:
        self.stop(force=True)
        self.restarts += 1
        self.start()

    def kill(self) -> None:
        self._stop.set()
        self._th.join(5)
        self._dead.set()

    def freeze(self) -> None:
        self._hung.set()

    def _serve(self, life: int, ready: threading.Event) -> None:
        sock = zmq.Context.instance().socket(zmq.REP)
        sock.setsockopt(zmq.LINGER, 0)
        sock.bind(self.endpoint)
        ready.set()
        pos = np.tile([0.0, 0.0, 0.2], (self.n_uav, 1))
        vel = np.zeros((self.n_uav, 3))
        seq, t = 0, 0
        fault = self.faults.get(life)
        eps = 1e-12 if self.diverge_from_life is not None and life >= self.diverge_from_life else 0.0
        try:
            while not self._stop.is_set():
                if not sock.poll(20):
                    continue
                if self._hung.is_set():                                # 挂起：已收到的请求不回复
                    self._stop.wait()
                    break
                body, m = P.decode(sock.recv())
                tick = int(m.tick) if body == "control" else -1
                if fault is not None and tick == fault[1] and body in ("hello", "control"):
                    if fault[0] == "kill":
                        self._dead.set()
                        break
                    self._hung.set()
                    self._stop.wait()
                    break
                if body == "hello":
                    st = np.zeros((self.n_uav, P.STATE_N))
                    st[:, 0:3], st[:, 6], st[:, 15] = pos, 1.0, G
                    sock.send(P.encode("hello_ack", P.msg("HelloAck", n_uav=self.n_uav, t_ns=t, initial_state=st.tobytes())))
                    continue
                if body != "control":
                    sock.send(P.encode("bye", P.msg("Bye", iterations=t // STEP_NS)))
                    continue
                if m.seq != seq + 1:
                    sock.send(P.encode("error", P.msg("Error", code=635, detail=f"seq {m.seq} after {seq}")))
                    continue
                seq = int(m.seq)
                for sp in m.setpoints:
                    vel[sp.uav_index] = (sp.vx, sp.vy, sp.vz) if sp.enabled else (0.0, 0.0, 0.0)
                ts, rows = [], []
                for _ in range(m.grant_steps):
                    pos = pos + vel * (STEP_NS * 1e-9) + eps
                    t += STEP_NS
                    st = np.zeros((self.n_uav, P.STATE_N))
                    st[:, 0:3], st[:, 6], st[:, 7:10], st[:, 15] = pos, 1.0, vel, G
                    ts.append(t)
                    rows.append(st)
                sd = P.msg("StepDone", seq=seq, tick=m.tick, n_steps=m.grant_steps, n_uav=self.n_uav, t_ns=ts,
                           samples=np.stack(rows).astype("<f8").tobytes())
                sock.send(P.encode("step_done", sd))
        finally:
            sock.close(0)


PLAN = {2: [TierCommand("uav-01", "takeoff", 2, alt_m=5.0), TierCommand("uav-02", "takeoff", 3, alt_m=3.0)],
        20: [TierCommand("uav-01", "goto", 20, target_enu_m=(10.0, 4.0, 5.0), speed_mps=3.0)],
        45: [TierCommand("uav-02", "velocity", 45, vel_enu_mps=(1.0, -0.5, 0.0))]}


def _drive(be, n_ticks: int, plan=PLAN, keep: list | None = None) -> list:
    be.open()
    if keep is not None:
        keep.append(be.history)
    trace = []
    for k in range(n_ticks):
        be.submit(plan.get(k, []))
        r = be.advance(k)
        s = {u: be.history.last_sample(u) for u in be.uavs}
        trace.append((r.t_start_ns, r.t_end_ns, tuple(r.contacts), dict(r.status),
                      {u: (x.t_ns, tuple(np.asarray(x.pos_enu_m))) for u, x in s.items()}))
    return trace


def _supervised(fake, **kw):
    from awr.phys.gz.backend import SupervisedGzBackend

    kw.setdefault("step_timeout_s", 0.5)
    kw.setdefault("timeout_s", 1.0)
    return SupervisedGzBackend(fake, ["uav-01", "uav-02"], grounded=["uav-01", "uav-02"], poll_s=0.05, **kw)


def test_fake_kill_and_hang_replay_bitwise(tmp_path: Path) -> None:
    ref = _supervised(_FakeGz(tmp_path / "ref", 2))
    t_ref = _drive(ref, 80)
    info_ref = ref.close()
    fake = _FakeGz(tmp_path / "f", 2, faults={0: ("kill", 30), 1: ("hang", 61)})
    be = _supervised(fake)
    t_f = _drive(be, 80)
    info = be.close()
    assert info["pose_sha256"] == info_ref["pose_sha256"]                         # 与无故障运行逐位一致
    assert t_f == t_ref                                                            # Env 可见的逐 tick 输出相同
    assert [(e["tick"], e["code"]) for e in info["recoveries"]] == [(30, 638), (61, 632)]
    assert [e["replayed_ticks"] for e in info["recoveries"]] == [30, 61]
    assert info["recoveries"][0]["detect_s"] <= 1.0                               # kill -9 ≤ 1 s 识别为 638（M24-AC-013）
    assert 0.5 <= info["recoveries"][1]["detect_s"] < 0.5 + 0.3                    # 看门狗按 step_timeout_s 触发
    assert info["restarts"] == 2 and fake.restarts == 2 and info["ticks"] == 80 and info["replayed_ticks"] == 30 + 61
    assert be.status() == ref.status()


def test_fake_fault_during_open_and_replay(tmp_path: Path) -> None:
    """Hello 阶段的故障只重启不重放；重放途中再次故障时重新重建（连续计数），仍然交接原 PoseHistoryBuffer。"""
    ref = _supervised(_FakeGz(tmp_path / "ref", 2))
    t_ref = _drive(ref, 50)
    ref.close()
    fake = _FakeGz(tmp_path / "f", 2, faults={0: ("hang", -1), 1: ("kill", 40), 2: ("kill", 12)})
    be = _supervised(fake)
    keep: list = []
    trace = _drive(be, 50, keep=keep)
    info = be.close()
    assert be.history is keep[0]                                                   # 调用方持有的缓冲不变
    assert trace == t_ref
    assert [(e["tick"], e["code"], e["consecutive"], e["completed"], e["replayed_ticks"]) for e in info["recoveries"]] == [
        (None, 632, 1, True, 0), (40, 638, 1, False, 0), (40, 638, 2, True, 40)]
    assert fake.restarts == 3


def test_fake_replay_divergence_is_platform_failure(tmp_path: Path) -> None:
    fake = _FakeGz(tmp_path, 2, faults={0: ("kill", 25)}, diverge_from_life=1)
    be = _supervised(fake)
    be.open()
    with pytest.raises(PhysError) as e:
        for k in range(40):
            be.submit(PLAN.get(k, []))
            be.advance(k)
    assert e.value.code == 638 and e.value.detail["platform_failure"] is True
    assert e.value.detail["reason"] == "replay_diverged" and e.value.detail["diverged_tick"] >= 0
    be.close()


def test_fake_three_consecutive_timeouts_fail_632(tmp_path: Path) -> None:
    fake = _FakeGz(tmp_path, 2, faults={0: ("hang", 10), 1: ("hang", 10), 2: ("hang", 10)})
    be = _supervised(fake)
    be.open()
    with pytest.raises(PhysError) as e:
        for k in range(20):
            be.submit(PLAN.get(k, []))
            be.advance(k)
    assert e.value.code == 632 and e.value.detail["platform_failure"] is True and e.value.detail["consecutive"] == 3
    assert fake.restarts == 2 and [r["consecutive"] for r in be.recoveries] == [1, 2]
    be.close()


def test_fake_watchdog_without_supervisor(tmp_path: Path) -> None:
    """GzSimBackend 单独使用：挂起 → 632（套接字作废，close 不再发 Shutdown）；实例退出 → 638。"""
    from awr.phys.gz.backend import GzSimBackend

    fake = _FakeGz(tmp_path, 1, faults={0: ("hang", 3)})
    be = GzSimBackend(["uav-01"], fake.endpoint, step_timeout_s=0.4, poll_s=0.05, alive=fake.alive)
    assert be.step_timeout_s == 0.4 and GzSimBackend.step_timeout_s == 5.0           # 缺省 5 s（physics_opts.step_timeout_s）
    be.open()
    for k in range(3):
        be.advance(k)
    t0 = time.monotonic()
    with pytest.raises(PhysError) as e:
        be.advance(3)
    assert e.value.code == 632 and 0.4 <= time.monotonic() - t0 < 0.7
    assert "close_error" not in be.close()
    fake.stop()
    (tmp_path / "k").mkdir()
    fake = _FakeGz(tmp_path / "k", 1)
    be = GzSimBackend(["uav-01"], fake.endpoint, step_timeout_s=5.0, poll_s=0.05, alive=fake.alive)
    be.open()
    be.advance(0)
    fake.kill()
    t0 = time.monotonic()
    with pytest.raises(PhysError) as e:
        be.advance(1)
    assert e.value.code == 638 and time.monotonic() - t0 < 1.0
    be.close()


def test_reuse_policy_and_instance_hooks(tmp_path: Path) -> None:
    from awr.phys.gz.backend import REUSE_POLICY, SupervisedGzBackend
    from awr.phys.gz.instance import GzInstance

    assert REUSE_POLICY == "per_run"
    with pytest.raises(PhysError) as e:
        SupervisedGzBackend(_FakeGz(tmp_path, 1), ["uav-01"], reuse_policy="same_seed")
    assert e.value.code == 630
    with pytest.raises(TypeError):
        SupervisedGzBackend(_FakeGz(tmp_path, 1), ["uav-01"], endpoint="ipc:///x")
    d = GzInstance(tmp_path / "d", "d3-gz-r1", dry_run=True)
    assert d.world_reset_command()[:4] == ["docker", "exec", "d3-gz-r1", "gz"]
    loc = GzInstance(tmp_path / "l", "x", launcher="local", dry_run=True)
    cmd = loc.world_reset_command("w1")
    assert cmd[:4] == ["gz", "service", "-s", "/world/w1/control"] and cmd[-1] == "reset: {all: true}"
    loc.start("<sdf/>")
    loc.restart()                                                                  # dry_run：只重写 run.sdf，计数
    assert loc.restarts == 1 and (tmp_path / "l" / "run.sdf").read_text() == "<sdf/>"


# ---------------------------------------------------------------------------------------------------------------- dmax：真实 gz
_HAS_GZ = shutil.which("gz") is not None and any(
    (Path(p) / "libDrone4DGz.so").exists() for p in os.environ.get("GZ_SIM_SYSTEM_PLUGIN_PATH", "").split(":") if p)
needs_gz = pytest.mark.skipif(not _HAS_GZ, reason="[platform] needs gz-sim with libDrone4DGz.so (d3-gz image)")

GZ_PLAN = {5: [TierCommand("uav-01", "takeoff", 5, alt_m=6.0), TierCommand("uav-02", "takeoff", 5, alt_m=4.0)],
           60: [TierCommand("uav-01", "goto", 60, target_enu_m=(15.0, 8.0, 7.0), speed_mps=4.0)],
           140: [TierCommand("uav-02", "goto", 140, target_enu_m=(-6.0, 3.0, 4.0), speed_mps=3.0)],
           190: [TierCommand("uav-01", "land", 190), TierCommand("uav-02", "land", 190)]}


def _gz_spawns():
    from awr.phys.gz.compose import UavSpawn

    return [UavSpawn("uav-01", (0.0, 0.0, 0.3)), UavSpawn("uav-02", (6.0, 0.0, 0.3), yaw_rad=1.2)]


def _gz_run(tmp: Path, n_ticks: int, inject=None, *, plan=GZ_PLAN, on_recovered=None):
    """一个 Run：GzInstance（local）+ SupervisedGzBackend。inject(k, inst) 在 advance(k) 之前调用（故障注入）。"""
    from awr.phys.gz.backend import SupervisedGzBackend
    from awr.phys.gz.compose import compose_run
    from awr.phys.gz.instance import GzInstance

    spawns = _gz_spawns()
    inst = GzInstance(tmp, f"p04-{tmp.name}", launcher="local")
    inst.start(compose_run(spawns, ground_plane=True, endpoint=inst.gz_endpoint))
    trace = []
    try:
        inst.wait_socket(60)
        be = SupervisedGzBackend(inst, [s.uav for s in spawns], on_recovered=on_recovered)
        be.open()
        for k in range(n_ticks):
            if inject is not None:
                inject(k, inst)
            be.submit(plan.get(k, []))
            r = be.advance(k)
            trace.append((r.t_end_ns, tuple(r.contacts), dict(r.status)))
        info = be.close()
    finally:
        inst.stop(force=True)
    return trace, info


def _dump(name: str, obj) -> None:
    out = os.environ.get("AWR_D3_FAULT_OUT")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"[{name}]", json.dumps(obj, ensure_ascii=False, default=str)[:4000])


@needs_gz
@pytest.mark.chaos
@pytest.mark.perf
def test_gz_kill_hang_and_midstep_kill_replay_bitwise(tmp_path: Path) -> None:
    """kill -9（tick 起点）、SIGSTOP 挂起（看门狗 5 s）与步进期间 kill -9 之后重启重放，位姿摘要与逐 tick 输出同无故障运行逐位一致。"""
    n = 330
    ref, info_ref = _gz_run(tmp_path / "ref", n)

    def inject(k: int, inst) -> None:
        if k == 70:
            inst.kill()
        elif k == 150:
            inst.freeze()
        elif k == 200:
            threading.Timer(0.004, inst.kill).start()                             # 落在 Control 发出后、StepDone 之前或之后

    got, info = _gz_run(tmp_path / "fault", n, inject)
    _dump("p04_gz_fault_recovery", {"ref": {k: v for k, v in info_ref.items() if k != "recoveries"}, "fault": info})
    assert info["pose_sha256"] == info_ref["pose_sha256"]
    assert got == ref
    ev = info["recoveries"]
    assert [(e["tick"], e["code"]) for e in ev[:2]] == [(70, 638), (150, 632)]
    assert ev[0]["detect_s"] <= 1.0                                               # M24-AC-013：kill -9 ≤ 1 s 识别为 638
    assert 5.0 <= ev[1]["detect_s"] <= 5.5                                         # 看门狗：步进超时 5 s
    assert all(e["code"] in (632, 638) and e["replayed_ticks"] == e["tick"] for e in ev)
    assert info["restarts"] == 3 and ev[2]["tick"] in (200, 201)                    # 第三次 kill 若在 StepDone 之后，下一 tick 才识别
    assert info["ticks"] == n and info["replayed_ticks"] == sum(e["tick"] for e in ev)
    assert all(e["restart_s"] + e["replay_s"] < 30 for e in ev)
    assert ref[-1][2] == {"uav-01": VehicleStatus.GROUNDED, "uav-02": VehicleStatus.GROUNDED}


@needs_gz
@pytest.mark.chaos
@pytest.mark.perf
def test_gz_three_consecutive_hangs_fail_632(tmp_path: Path) -> None:
    """M24-FR-079：同一 tick 连续 3 次步进超时 → 632（platform_failure），只重启两次。"""
    holder: dict = {}

    def inject(k: int, inst) -> None:
        holder["inst"] = inst
        if k == 30:
            inst.freeze()

    with pytest.raises(PhysError) as e:
        _gz_run(tmp_path / "hang3", 40, inject, on_recovered=lambda ev: holder["inst"].freeze())
    assert e.value.code == 632 and e.value.detail["platform_failure"] is True and e.value.detail["consecutive"] == 3
    assert holder["inst"].restarts == 2


@needs_gz
@pytest.mark.chaos
@pytest.mark.perf
def test_gz_reset_reuse_verdict(tmp_path: Path) -> None:
    """M24-FR-060：同种子 reset 复用验证。插件未实现 Reset、世界复位不回到回合起点时判定每 Run 重启；每 Run 重启本身逐位一致。"""
    from awr.phys.gz.backend import REUSE_POLICY, verify_reset_reuse
    from awr.phys.gz.compose import compose_run
    from awr.phys.gz.instance import GzInstance

    spawns = _gz_spawns()
    out = verify_reset_reuse(lambda tag: GzInstance(tmp_path / tag, f"p04-reset-{tag}", launcher="local"),
                             lambda ep: compose_run(spawns, ground_plane=True, endpoint=ep), [s.uav for s in spawns], GZ_PLAN, 300,
                             grounded=[s.uav for s in spawns])
    _dump("p04_reset_reuse_verdict", out)
    assert out["restart_bitwise"] is True                                          # 每 Run 重启：新实例与首个回合逐位相同
    assert out["reuse_policy"] == REUSE_POLICY == "per_run"
    assert out["reasons"]                                                          # 判定依据逐条记录


@needs_gz
@pytest.mark.chaos
@pytest.mark.perf
@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("AWR_D3_FAULT_SOAK") != "1", reason="[optin] set AWR_D3_FAULT_SOAK=1 (M24-AC-027 soak)")
def test_gz_soak_twenty_faults_from_tick_1000(tmp_path: Path) -> None:
    """M24-AC-027：第 1,000 tick 起注入 kill -9 与步进超时各 10 次，每次重放后继续，最终位姿摘要与无故障运行相同。"""
    n = 1200
    plan = {5: [TierCommand("uav-01", "takeoff", 5, alt_m=6.0), TierCommand("uav-02", "takeoff", 5, alt_m=4.0)],
            60: [TierCommand("uav-01", "goto", 60, target_enu_m=(40.0, 20.0, 8.0), speed_mps=3.0)],
            400: [TierCommand("uav-02", "goto", 400, target_enu_m=(-30.0, 10.0, 5.0), speed_mps=2.0)],
            900: [TierCommand("uav-01", "goto", 900, target_enu_m=(0.0, 0.0, 6.0), speed_mps=4.0)]}
    faults = {1000 + 10 * i: ("kill" if i < 10 else "hang") for i in range(20)}
    ref, info_ref = _gz_run(tmp_path / "ref", n, plan=plan)

    def inject(k: int, inst) -> None:
        f = faults.get(k)
        if f == "kill":
            inst.kill()
        elif f == "hang":
            inst.freeze()

    got, info = _gz_run(tmp_path / "soak", n, inject, plan=plan)
    _dump("p04_gz_fault_soak", {"ref_pose_sha256": info_ref["pose_sha256"], "fault": info})
    assert info["pose_sha256"] == info_ref["pose_sha256"] and got == ref
    assert [(e["tick"], e["code"]) for e in info["recoveries"]] == [(k, 638 if f == "kill" else 632) for k, f in sorted(faults.items())]
    assert max(e["detect_s"] for e in info["recoveries"] if e["code"] == 638) <= 1.0
    assert all(5.0 <= e["detect_s"] <= 5.5 for e in info["recoveries"] if e["code"] == 632)
