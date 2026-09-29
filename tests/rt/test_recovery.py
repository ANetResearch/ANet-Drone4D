"""进程恢复语义的网关部分（M11-AC-047 功能口径；D1-AC-11a；M11-FR-023、FR-048、FR-059、FR-064；AWR-17 §6.10、§7.5）。

api 重启（FakeSim 与总线命名空间不变，等价于 supervisor 重启 api）：
- 全局 epoch 沿用（gw.epoch），sessionId 变化；席位持有者沿用（gw.seat）；
- 客户端以同一 token 重连，对未终结调用以同一 call id 重发 → `duplicate: true`（当前状态 running），生产者执行计数为 1；
- 在途表改绑到新连接：此后该调用的 result（cancel → canceled 6）发往新连接；
- 生产者主循环不受影响（FakeSim 步序号连续递增）。
真实进程下 kill -9 api 的 ≤ 3 s 重连判据在 `test_chaos_api_kill.py`（chaos + perf 标记，验收阶段执行）。
"""

from __future__ import annotations

import asyncio

import fakesim
import rtc


def test_api_restart_resend_same_cid_duplicate() -> None:
    st = fakesim.GwStack(n=2)
    try:
        tok = rtc.token(st.base, "operator", rtc.hint_of("recovery"))
        e0 = st.gw.clock.global_epoch

        async def phase1() -> tuple[str, rtc.Client]:
            c = await rtc.open_client(st, tok["token"])
            info = next(m for m in c.texts if m["op"] == "serverInfo")
            await c.send({"op": "call", "id": "rec-vel-00001", "service": "uav/f001/cmd/velocity", "args": {}})
            await c.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "rec-vel-00001"
                          and x["status"] == "running", 3)
            return info["sessionId"], c

        sid0, _c0 = asyncio.run(phase1())
        k0 = st.sim.k
        st.restart_api()

        async def phase2() -> None:
            c = await rtc.open_client(st, tok["token"], resume={"sessionId": sid0, "lastEventSeq": 0})
            info = next(m for m in c.texts if m["op"] == "serverInfo")
            assert info["sessionId"] != sid0 and info["seat"] == "held"
            assert c.times[0].epoch == e0 & 0xFFFF
            await c.send({"op": "call", "id": "rec-vel-00001", "service": "uav/f001/cmd/velocity", "args": {}})
            _, r = await c.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "rec-vel-00001", 3)
            assert r.get("duplicate") is True and r["status"] in ("accepted", "running") and not r.get("final")
            await c.send({"op": "cancel", "id": "rec-vel-00001"})
            fin = await c.result("rec-vel-00001")
            assert fin["status"] == "canceled" and fin["code"] == 6
            await c.ws.close()

        asyncio.run(phase2())
        assert st.sim.executions["rec-vel-00001"] == 1
        assert st.gw.clock.global_epoch == e0 and st.sim.k > k0
    finally:
        st.close()
