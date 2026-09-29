"""RPC 生命周期、入口拒绝、幂等、cancel、批量与 REST 镜像（M11-AC-025、AC-027（功能部分）；M11-FR-055 至 FR-064、FR-066、
FR-070；AWR-17 §7.1、§7.2、§7.5）。FakeSim + 真实 Gateway。

- 10 个主命令各走完生命周期（accepted → running → succeeded），result 只发往发起连接且都带 effect；
- 同 cid 60 s 内重发得 `duplicate: true`，生产者执行计数为 1（已终态由 Gateway 缓存回放；未终态由生产者幂等表裁决）；
- 入口拒绝码 115、116、118、111（带 retry_after_ms）、300、110、107，均产生 api 事件 `cmd.rejected`；
- `cancel`：机体调用转为 `uav/{id}/cmd/cancel`，目标以 canceled 6 结束；已终态或非机体调用 `error 105`；
- 批量 `fleet/cmd/rtl`（`vehicles: "*"`）：1 条汇总 result → progress（≤ 2 Hz）与 `fleet.batch.progress` → final；生产者
  不支持批量（109）时 Gateway 逐机展开；
- REST 镜像 `POST /api/commands` 与 WS 共用在途表（同 cid 结果一致）。
"""

from __future__ import annotations

import asyncio
import time

import fakesim
import httpx
import pytest
import rtc

HINT = rtc.hint_of("rpcoperator")
MAIN_OPS = [("takeoff", {"alt_m": 5}), ("land", {}), ("goto", {"pos": [10, 20, 30]}),
            ("follow_path", {"waypoints": [[0, 0, 10], [5, 5, 10]]}), ("orbit", {"center": [0, 0, 20], "radius_m": 10}),
            ("hover", {}), ("rtl", {}), ("safety_stop", {}), ("pause", {}), ("resume", {})]


@pytest.fixture(scope="module")
def st():
    s = fakesim.GwStack(n=3)
    s.call_in_loop(setattr, s.gw, "seat_grace_s", 0.5)
    yield s
    s.close()


def _op_tok(st) -> dict:
    return rtc.token(st.base, "operator", HINT)


def test_main_commands_lifecycle_and_isolation(st) -> None:
    tok = _op_tok(st)
    vtok = rtc.token(st.base, "viewer")

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        other = await rtc.open_client(st, vtok["token"])
        for i, (op, args) in enumerate(MAIN_OPS):
            cid = f"life-{op}-{i:02d}"
            await c.send({"op": "call", "id": cid, "service": f"uav/f001/cmd/{op}", "args": args})
            r = await c.result(cid, timeout=10)
            assert r["status"] == "succeeded" and r["final"] and r["effect"]["status"] == "OK", r
            seq = [m["status"] for m in c.results(cid) if m["op"] == "result"]
            assert seq[0] == "accepted" and "running" in seq and seq[-1] == "succeeded", seq
            assert all("effect" in m for m in c.results(cid) if m["op"] == "result")
        await other.drain(0.3)
        assert not [m for m in other.texts if m["op"] in ("result", "progress")]
        for m in c.texts:
            assert rtc.ops_errors(m) == [], (m, rtc.ops_errors(m))
        await c.ws.close()
        await other.ws.close()

    asyncio.run(run())


def test_duplicates_count_once(st) -> None:
    tok = _op_tok(st)

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "call", "id": "dup-goto-0001", "service": "uav/f002/cmd/goto", "args": {"pos": [1, 2, 3]}})
        await c.result("dup-goto-0001")
        await c.send({"op": "call", "id": "dup-goto-0001", "service": "uav/f002/cmd/goto", "args": {"pos": [1, 2, 3]}})
        _, r = await c.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "dup-goto-0001"
                             and x.get("duplicate"), 5)
        assert r["status"] == "succeeded" and r["final"]
        # 未终态（velocity 保持 running）：生产者幂等表返回 duplicate + call_state
        await c.send({"op": "call", "id": "dup-vel-0001", "service": "uav/f002/cmd/velocity", "args": {}})
        await c.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "dup-vel-0001"
                      and x["status"] == "running", 5)
        await c.send({"op": "call", "id": "dup-vel-0001", "service": "uav/f002/cmd/velocity", "args": {}})
        _, r2 = await c.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "dup-vel-0001"
                              and x.get("duplicate"), 5)
        assert r2["status"] in ("accepted", "running") and not r2.get("final")
        await c.send({"op": "cancel", "id": "dup-vel-0001"})
        r3 = await c.result("dup-vel-0001")
        assert r3["status"] == "canceled" and r3["code"] == 6
        await c.send({"op": "cancel", "id": "dup-vel-0001"})
        _, e = await c.until(lambda k, x: k == "json" and x["op"] == "error" and x["ref"]["op"] == "cancel", 5)
        assert e["code"] == 105
        await c.ws.close()

    asyncio.run(run())
    assert st.sim.executions["dup-goto-0001"] == 1 and st.sim.executions["dup-vel-0001"] == 1


def test_entry_rejections(st) -> None:
    tok = _op_tok(st)
    vtok = rtc.token(st.base, "viewer")

    async def run() -> None:
        v = await rtc.open_client(st, vtok["token"])
        await v.send({"op": "call", "id": "rej-viewer-01", "service": "uav/f001/cmd/hover", "args": {}})
        assert (await v.result("rej-viewer-01"))["code"] == 115
        await v.send({"op": "call", "id": "q-viewer-001", "service": "env/query", "args": {"points": [[0, 0, 1]]}})
        q = await v.result("q-viewer-001")
        assert q["status"] == "succeeded" and q["data"]["wind_mps"] == [[1.0, 2.0, 0.0]]
        c = await rtc.open_client(st, tok["token"])
        cases = [("rej-svc-0001", "uav/f001/cmd/nope", {}, 300), ("rej-type-001", "uav/f001/cmd/goto", {"pos": "x"}, 300),
                 ("rej-range-01", "uav/f001/cmd/takeoff", {"alt_m": 500}, 110),
                 ("rej-veh-0001", "uav/zz9/cmd/hover", {}, 107), ("rej-fleet-01", "fleet/cmd/explode", {"vehicles": "*"}, 300)]
        for cid, svc, args, code in cases:
            await c.send({"op": "call", "id": cid, "service": svc, "args": args})
            r = await c.result(cid)
            assert r["status"] == "rejected" and r["code"] == code and r["final"], r
        # 118：回放等只读模式
        st.call_in_loop(setattr, st.gw, "mode", "replay")
        await c.send({"op": "call", "id": "rej-ro-00001", "service": "uav/f001/cmd/hover", "args": {}})
        assert (await c.result("rej-ro-00001"))["code"] == 118
        st.call_in_loop(setattr, st.gw, "mode", "live")
        await c.drain(0.2)
        # api 事件 cmd.rejected
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "event", "rate": 0, "mode": "all"}]})
        await c.send({"op": "call", "id": "rej-ev-00001", "service": "uav/f001/cmd/nope", "args": {}})
        await c.until(lambda k, x: any(e["type"] == "cmd.rejected" and e["cid"] == "rej-ev-00001" for e in c.events()), 3)
        await c.ws.close()
        await v.ws.close()

    asyncio.run(run())


def test_rate_limit_111(st) -> None:
    tok = rtc.token(st.base, "operator", HINT)

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await asyncio.sleep(2.2)  # 令牌桶回满（突发 100）
        for i in range(150):
            await c.send({"op": "call", "id": f"rl-{i:05d}", "service": "uav/f003/cmd/hover", "args": {}})
        firsts: dict[str, dict] = {}
        await c.until(lambda k, x: (k == "json" and x["op"] == "result" and x["id"].startswith("rl-")
                                    and firsts.setdefault(x["id"], x) is not None and len(firsts) >= 150), 20)
        limited = sorted(int(cid[3:]) for cid, r in firsts.items() if r["code"] == 111)
        assert limited and min(limited) >= 95, limited[:5]  # 突发 100（发送期间按 50/s 回补少量）
        r = firsts[f"rl-{limited[0]:05d}"]
        assert r["retry_after_ms"] > 0 and r["status"] == "rejected"
        await c.ws.close()

    asyncio.run(run())


def test_batch_rtl_summary_progress_final(st) -> None:
    tok = _op_tok(st)

    async def run() -> None:
        await asyncio.sleep(2.2)
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "event", "rate": 0, "mode": "all"}]})
        await c.send({"op": "call", "id": "batch-rtl-001", "service": "fleet/cmd/rtl",
                      "args": {"vehicles": "*", "args": {}}})
        _, first = await c.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "batch-rtl-001", 5)
        assert first["status"] == "accepted" and first["data"]["accepted_n"] == 3 and first["data"]["rejected_n"] == 0
        fin = await c.result("batch-rtl-001", timeout=10)
        assert fin["status"] == "succeeded" and fin["data"]["counts"]["succeeded"] == 3
        results = [m for m in c.texts if m["op"] == "result" and m["id"] == "batch-rtl-001"]
        assert len(results) == 2  # 汇总 + final
        progs = [m for m in c.texts if m["op"] == "progress" and m["id"] == "batch-rtl-001"]
        assert progs and all("counts" in p["data"] for p in progs)
        evs = [e for e in c.events() if e["type"] == "fleet.batch.progress"]
        assert evs and evs[-1]["data"]["batch_id"] == "batch-rtl-001"
        assert not [e for e in c.events() if e["type"].startswith("cmd.") and (e.get("cid") or "").startswith("batch-rtl")]
        await c.ws.close()

    asyncio.run(run())


def test_batch_fallback_expand_and_rest_mirror() -> None:
    s = fakesim.GwStack(n=2, sim_kw={"batch": False})
    try:
        tok = rtc.token(s.base, "operator", rtc.hint_of("batchfallback"))

        async def run() -> None:
            c = await rtc.open_client(s, tok["token"])
            await c.send({"op": "call", "id": "batch-land-01", "service": "fleet/cmd/land",
                          "args": {"vehicles": ["f001", "f002", "zz9"], "args": {}}})
            _, first = await c.until(lambda k, x: k == "json" and x["op"] == "result" and x["id"] == "batch-land-01", 5)
            assert first["data"]["accepted_n"] == 2 and first["data"]["rejected"] == [["zz9", 107]]
            fin = await c.result("batch-land-01", timeout=10)
            assert fin["status"] == "succeeded"
            await c.ws.close()

        asyncio.run(run())
        assert s.sim.executions["batch-land-01:f001"] == 1 and s.gw.rpc.stats["batch_fallbacks"] == 1
        h = {"Authorization": f"Bearer {tok['token']}"}
        r = httpx.post(f"{s.base}/api/commands", json={"id": "rest-hover-01", "service": "uav/f001/cmd/hover", "args": {}},
                       headers=h, timeout=10)
        assert r.status_code == 200 and r.json()["status"] == "accepted" and r.headers["location"].endswith("rest-hover-01")
        g = httpx.get(f"{s.base}/api/commands/rest-hover-01?wait_final_ms=5000", headers=h, timeout=10).json()
        assert g["final"] and g["result"]["status"] == "succeeded" and g["history"][0]["status"] == "accepted"
        r = httpx.post(f"{s.base}/api/commands", json={"id": "rest-hover-01", "service": "uav/f001/cmd/hover", "args": {}},
                       headers=h, timeout=10)
        assert r.status_code == 200 and r.json()["duplicate"] is True
        r = httpx.post(f"{s.base}/api/commands", json={"service": "uav/zz9/cmd/hover", "args": {}}, headers=h, timeout=10)
        assert r.status_code == 404 and r.json()["code"] == 107
        assert httpx.get(f"{s.base}/api/commands/nope-0000", headers=h, timeout=10).status_code == 404
        t0 = time.monotonic()
        while s.sim.executions["rest-hover-01"] != 1 and time.monotonic() - t0 < 2:
            time.sleep(0.05)
        assert s.sim.executions["rest-hover-01"] == 1
    finally:
        s.close()
