"""EnvCache 同版本心跳（FX-GW；M07-to-M11 第 1 条；M07-FR-032、M07-AC-010、M07-AC-025；AWR-17 §9.5）：
- 单元：同纪元同版本的心跳照常发布（channel seq 前进、载荷换成最新锚点），但不计入 applied、不前进版本；更旧版本丢弃；
  新纪元与 reset 之后无条件生效；
- 端到端（FakeSim + 真实 Gateway + WS）：环境版本不变的稳态下，客户端每约 1 s 收到一帧 `env/state`，锚点 `t_ns` 单调前进，
  相邻两帧间隔远小于客户端 3 s 的 STALE 门槛；迟到的客户端拿到的是最新心跳的锚点，而不是最后一次变化帧；
  `GET /api/env/state` 同样返回最新锚点。
"""

from __future__ import annotations

import asyncio
import itertools
import time
from types import SimpleNamespace

import fakesim
import httpx
import msgpack
import pytest
import rtc

from awr.api.rt.channels import Channel
from awr.api.rt.detail import EnvCache
from awr.contracts.presets import PRESETS_SHA256


def _frame(version: int, t_ns: int, epoch: int = 1) -> bytes:
    return msgpack.packb({"version": version, "epoch": epoch, "t_ns": t_ns, "config": {"presets_sha256": PRESETS_SHA256}},
                         use_bin_type=True)


def _t(ch: Channel) -> int:
    return msgpack.unpackb(bytes(ch.payload), raw=False)["t_ns"]


def test_env_cache_same_version_heartbeat_republishes() -> None:
    gw = SimpleNamespace(frame_t_sim_ns=0, set_status=lambda st: None, clear_status=lambda sid: None)
    ch = Channel(40, "env/state")
    env = EnvCache(gw, ch)  # type: ignore[arg-type]
    env.on_heartbeat(_frame(3, 1_000_000_000))
    assert (env.version, env.stats["applied"], ch.seq, _t(ch)) == (3, 1, 1, 1_000_000_000)
    # 同版本心跳：照常下发最新锚点（客户端据此不进入 STALE），不计入 applied
    env.on_heartbeat(_frame(3, 2_000_000_000))
    assert (env.version, env.stats["applied"], ch.seq, _t(ch)) == (3, 1, 2, 2_000_000_000)
    assert ch.t_sim_ns == 2_000_000_000
    # 更旧的版本丢弃
    env.on_heartbeat(_frame(2, 3_000_000_000))
    assert (ch.seq, _t(ch)) == (2, 2_000_000_000)
    # 变化帧（可靠事件）前进版本
    env.on_keyframe({"data": msgpack.unpackb(_frame(4, 3_500_000_000), raw=False), "t_sim_ns": 3_500_000_000})
    assert (env.version, env.stats["applied"], ch.seq) == (4, 2, 3)
    env.on_heartbeat(_frame(3, 4_000_000_000))  # 迟到的旧版本心跳
    assert ch.seq == 3
    env.on_heartbeat(_frame(4, 4_000_000_000))
    assert (ch.seq, _t(ch), env.stats["applied"]) == (4, 4_000_000_000, 2)
    # 新纪元（sim-core 重启或 checkpoint 恢复）版本从 1 重新开始：无条件生效
    env.on_heartbeat(_frame(1, 5_000_000_000, epoch=2))
    assert (env.epoch, env.version, ch.seq) == (2, 1, 5)
    # reset（回放 open/seek/close）之后下一帧无条件生效
    env.reset()
    env.on_heartbeat(_frame(1, 100, epoch=9))
    assert (env.epoch, env.version, ch.seq, _t(ch)) == (9, 1, 6, 100)
    assert env.stats["heartbeats"] == 7


@pytest.fixture(scope="module")
def st():
    s = fakesim.GwStack(n=2, sim_kw={"sensors": False})
    yield s
    s.close()


def test_steady_state_heartbeats_and_late_joiner(st) -> None:
    tok = rtc.token(st.base, "viewer", rtc.hint_of("envhbsteady"))

    async def run() -> None:
        a = await rtc.open_client(st, tok["token"])
        eid = a.topic_ids["env/state"]
        await a.send({"op": "subscribe", "subs": [{"id": 1, "topic": "env/state", "rate": 10, "mode": "latest"}]})
        await a.until(lambda k, x: k == "batch" and x.by_channel(eid) is not None, 5)
        st.sim.emit_keyframe()  # 最后一次变化帧；此后版本不变
        await a.until(lambda k, x: k == "batch" and x.by_channel(eid) is not None
                      and a.latest_msgpack(eid)["version"] == st.sim.env_version, 5)
        v = st.sim.env_version
        kf_t = a.latest_msgpack(eid)["t_ns"]
        # 稳态：同版本心跳每约 1 s 一帧，锚点前进
        arrivals: list[tuple[float, int]] = []
        end = time.monotonic() + 4.5
        while time.monotonic() < end:
            try:
                k, x = await a.recv(max(0.05, end - time.monotonic()))
            except TimeoutError:
                break
            if k == "batch" and x.by_channel(eid) is not None:
                f = msgpack.unpackb(x.payload(x.by_channel(eid)), raw=False)
                assert f["version"] == v
                arrivals.append((time.monotonic(), f["t_ns"]))
        assert len(arrivals) >= 3, arrivals  # 修复前：版本不变时 0 帧，客户端 3 s 后 STALE
        ts = [t for _, t in arrivals]
        assert ts == sorted(ts) and ts[-1] > kf_t
        gaps = [y[0] - x[0] for x, y in itertools.pairwise(arrivals)]
        assert max(gaps) < 2.5, gaps  # 远小于 3 s 的 STALE 门槛（1 Hz 心跳 + 负载余量）
        # 迟到客户端：首帧即最新心跳的锚点，而不是变化帧时刻
        b = await rtc.open_client(st, tok["token"])
        await b.send({"op": "subscribe", "subs": [{"id": 1, "topic": "env/state", "rate": 10, "mode": "latest"}]})
        await b.until(lambda k, x: k == "batch" and x.by_channel(eid) is not None, 5)
        first = b.latest_msgpack(eid)
        assert first["version"] == v and first["t_ns"] >= ts[-1] > kf_t
        # REST R27 同一份最新字节
        h = {"authorization": f"Bearer {tok['token']}"}
        r = httpx.get(f"{st.base}/api/env/state", headers=h, timeout=10)
        assert r.status_code == 200 and r.json()["version"] == v and r.json()["t_ns"] >= ts[-1]
        assert st.gw.env.stats["applied"] >= 1 and st.gw.env.stats["heartbeats"] > st.gw.env.stats["applied"]
        await a.ws.close()
        await b.ws.close()

    asyncio.run(run())
