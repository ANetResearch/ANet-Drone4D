"""CLIENT_DATA 遥操作（M11-AC-029 的网关部分；M11-FR-062；AWR-17 §6.4、§9.4）：客户端 advertise `uav/{id}/setpoint`，本连接
对该机有已准入的活动 velocity 调用时，有效包立即转为 32 B raw 发布到 `ctl/sim-core/setpoint`；过期（`t_client_sim_ns <
simNow − 200 ms`）或乱序（seq 不递增）的包丢弃；无活动 velocity 调用返回一次 `error 322`；viewer 不能 advertise；二进制帧
> 4 KiB 以 1009 关闭，未知 opcode 计格式错误（10 s 内 3 次 1002）。
"""

from __future__ import annotations

import asyncio
import struct

import fakesim
import pytest
import rtc
from websockets.exceptions import ConnectionClosed

from awr.contracts import frame as F


@pytest.fixture(scope="module")
def st():
    s = fakesim.GwStack(n=2)
    yield s
    s.close()


def _cd(chid: int, seq: int, t_ns: int, vel=(1.0, 0.0, 0.0), yaw=0.0) -> bytes:
    return F.CLIENT_DATA_HDR.pack(F.OP_CLIENT_DATA, 0, chid, seq, t_ns) + struct.pack("<4f", *vel, yaw)


def test_setpoint_forwarding_rules(st) -> None:
    tok = rtc.token(st.base, "operator", rtc.hint_of("teleop"))

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "advertise", "channels": [{"id": 1, "topic": "uav/f001/setpoint", "encoding": "raw",
                                                       "schemaName": "awr.VelSetpoint16.v1"}]})
        now = st.call_in_loop(lambda: st.gw.clock.sim_now_ns())
        await c.ws.send(_cd(1, 1, now))
        await c.until(lambda k, x: k == "json" and x["op"] == "error" and x["code"] == 322, 3)
        await c.ws.send(_cd(1, 2, now))
        await c.drain(0.2)
        assert sum(1 for m in c.texts if m["op"] == "error" and m["code"] == 322) == 1  # 只发一次
        await c.send({"op": "call", "id": "tele-vel-0001", "service": "uav/f001/cmd/velocity",
                      "args": {"frame": "body"}})
        await c.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "tele-vel-0001", 3)
        n0 = len(st.sim.setpoints)
        now = st.call_in_loop(lambda: st.gw.clock.sim_now_ns())
        for seq in (10, 11, 11, 9, 12):  # 11 重复、9 乱序被丢弃
            await c.ws.send(_cd(1, seq, now))
        await c.ws.send(_cd(1, 13, now - 1_000_000_000))  # 过期
        await asyncio.sleep(0.3)
        got = st.sim.setpoints[n0:]
        seqs = [F.SETPOINT_BUS.unpack_from(p, 0)[3] for p in got]
        assert seqs == [10, 11, 12], seqs
        no, _flags, frame, _seq, t = F.SETPOINT_BUS.unpack_from(got[0], 0)
        assert len(got[0]) == 32 and no == 0 and frame == 1 and t == now
        assert struct.unpack_from("<4f", got[0], 16)[0] == 1.0
        assert st.gw.cpub.stats["dropped_seq"] >= 2 and st.gw.cpub.stats["dropped_late"] >= 1
        # 未知 opcode：格式错误，3 次后 1002
        for _ in range(3):
            await c.ws.send(b"\x55" + b"\0" * 20)
        with pytest.raises(ConnectionClosed) as ei:
            await c.drain(2.0)
            await c.ws.recv()
        assert ei.value.rcvd.code == 1002

    asyncio.run(run())


def test_viewer_denied_and_binary_limit(st) -> None:
    vtok = rtc.token(st.base, "viewer")

    async def run() -> None:
        v = await rtc.open_client(st, vtok["token"])
        await v.send({"op": "advertise", "channels": [{"id": 1, "topic": "uav/f001/setpoint", "encoding": "raw",
                                                       "schemaName": "awr.VelSetpoint16.v1"}]})
        _, e = await v.until(lambda k, x: k == "json" and x["op"] == "error", 3)
        assert e["code"] == 322
        await v.ws.send(b"\x20" + b"\0" * 5000)
        with pytest.raises(ConnectionClosed) as ei:
            await v.drain(2.0)
            await v.ws.recv()
        assert ei.value.rcvd.code == 1009

    asyncio.run(run())
