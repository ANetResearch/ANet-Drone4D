"""GzSimBackend 发散守卫（K18，B21 遗留 5）：机间碰撞后 gz 中已坠毁机体的状态发散（位置约 1e124、速度溢出、非有限值）时，宿主侧冻结
该机体，PoseHistory 与录制只见有界值；未坠毁机体发散按坠毁处理；守卫只取决于样本值，两次运行样本流摘要逐位一致。不需要 gz：REP 端为
按脚本回复的最小 Drone4DClock 替身。"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("google.protobuf")
pytest.importorskip("zmq")

from awr.phys.adapter import VehicleStatus
from awr.phys.gz import protocol as P

STEP = 4_000_000


class _DivergingClock:
    """两机悬停于 z = 10 m。`collide_tick` 的第 3 步报告 uav0 的 COLLISION_UAV；此后 uav0 的速度与位置按步指数增长直至 1e124 量级，
    `nan_tick` 起出现 NaN。`live_nan_tick` 给出时 uav1（未坠毁）自该 tick 起状态为 NaN。"""

    def __init__(self, endpoint: str, *, collide_tick: int = 2, nan_tick: int = 6, live_nan_tick: int | None = None) -> None:
        import zmq

        self.sock = zmq.Context.instance().socket(zmq.REP)
        self.sock.bind(endpoint)
        self.collide, self.nan, self.live_nan = collide_tick, nan_tick, live_nan_tick
        self.t = 0
        self.growth = 1.0
        self.th = threading.Thread(target=self.run, daemon=True)
        self.th.start()

    def _row(self, x: float) -> np.ndarray:
        r = np.zeros(P.STATE_N)
        r[0], r[2], r[6], r[15] = x, 10.0, 1.0, 9.80665
        return r

    def run(self) -> None:
        base = np.stack([self._row(0.0), self._row(5.0)])
        while True:
            body, m = P.decode(self.sock.recv())
            if body == "hello":
                self.sock.send(P.encode("hello_ack", P.msg("HelloAck", n_uav=2, t_ns=0, initial_state=base.tobytes())))
                continue
            if body != "control":
                self.sock.send(P.encode("bye", P.msg("Bye", iterations=self.t // STEP)))
                self.sock.close(0)
                return
            k, n = int(m.tick), int(m.grant_steps)
            s = np.tile(base, (n, 1, 1))
            contacts = []
            if k == self.collide:
                contacts.append(P.msg("Contact", uav_index=0, step=3, kind=5, px=0.5, py=0.0, pz=10.0, nz=1.0, speed=6.0))
            if k > self.collide:
                with np.errstate(over="ignore", invalid="ignore"):
                    for j in range(n):
                        self.growth *= 1e3
                        s[j, 0, 0] = s[j, 0, 7] = self.growth          # 位置与速度溢出（1e3 → 1e124 → inf）
                        if k >= self.nan:
                            s[j, 0, 0:13] = np.nan
            if self.live_nan is not None and k >= self.live_nan:
                s[:, 1, 0:3] = np.nan
            t = [self.t + (j + 1) * STEP for j in range(n)]
            self.t += n * STEP
            self.sock.send(P.encode("step_done", P.msg("StepDone", seq=m.seq, tick=k, n_steps=n, n_uav=2, t_ns=t, samples=s.tobytes(),
                                                       contacts=contacts)))


def _run(tmp_path: Path, tag: str, n_ticks: int = 10, **kw):
    from awr.phys.gz.backend import GzSimBackend

    ep = f"ipc://{tmp_path / f'{tag}.sock'}"
    _DivergingClock(ep, **kw)
    be = GzSimBackend(["uav-01", "uav-02"], ep, timeout_s=5.0, grounded=[], retain_s=None, history_s=4.0)
    be.open()
    results = [be.advance(k) for k in range(n_ticks)]
    states = be.history.slice("uav-01", 0, be.t_k_ns).state.copy(), be.history.slice("uav-02", 0, be.t_k_ns).state.copy()
    info = be.close()
    return be, results, states, info


def test_crashed_body_is_frozen_and_bounded(tmp_path: Path) -> None:
    from awr.phys.gz.backend import GUARD_POS_M, GUARD_V_MPS

    be, res, (s0, s1), info = _run(tmp_path, "a")
    assert res[2].contacts and res[2].contacts[0].kind == "COLLISION_UAV"
    assert be.status()["uav-01"] == VehicleStatus.CRASHED and be.status()["uav-02"] == VehicleStatus.AIRBORNE
    assert np.isfinite(s0).all() and np.abs(s0[:, 0:3]).max() < GUARD_POS_M          # 录制与 PoseHistory 只见有界值
    assert np.linalg.norm(s0[:, 7:10], axis=1).max() <= GUARD_V_MPS
    frozen = s0[-1]
    assert np.array_equal(frozen[7:13], np.zeros(6)) and frozen[2] == 10.0          # 冻结为最后一个合理样本，速度与角速度置零
    assert (s0[-50:] == frozen).all()                                                # 此后各 tick 保持冻结值
    ev = info["crash_guard"]
    assert len(ev) == 1 and ev[0]["uav"] == "uav-01" and ev[0]["crashed_before"] and ev[0]["reason"] in ("speed", "drift")
    assert ev[0]["t_ns"] > 2 * 100_000_000                                          # 坠毁 tick 之后才触发
    assert np.isfinite(s1).all() and (s1[:, 0] == 5.0).all()                         # 另一架机体不受影响


def test_guard_is_deterministic(tmp_path: Path) -> None:
    a = _run(tmp_path, "a")
    b = _run(tmp_path, "b")
    assert a[3]["pose_sha256"] == b[3]["pose_sha256"] and a[0].last_block_sha256 == b[0].last_block_sha256


def test_live_vehicle_divergence_is_a_crash(tmp_path: Path) -> None:
    """未坠毁机体出现非有限状态：按坠毁处理（IMPACT 接触、CRASHED），状态冻结为发散前的样本。"""
    _be, res, (_s0, s1), info = _run(tmp_path, "c", live_nan_tick=4, n_ticks=6)
    imp = [c for c in res[4].contacts if c.uav == "uav-02"]
    assert len(imp) == 1 and imp[0].kind == "IMPACT" and imp[0].pos_enu_m == (5.0, 0.0, 10.0)
    assert res[4].status["uav-02"] == VehicleStatus.CRASHED
    assert np.isfinite(s1).all() and (s1[:, 0] == 5.0).all()
    assert {k: v for k, v in info["crash_guard"][-1].items() if k != "t_ns"} == {"uav": "uav-02", "reason": "nonfinite", "crashed_before": False}


def test_no_guard_without_divergence(tmp_path: Path) -> None:
    """未发散的运行：样本块原样写入（守卫不复制、不改值），`close()` 不带 `crash_guard`。"""
    _be, _res, (s0, _s1), info = _run(tmp_path, "d", collide_tick=100, n_ticks=4)
    assert "crash_guard" not in info and (s0[:, 0] == 0.0).all()
