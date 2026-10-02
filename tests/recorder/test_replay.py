"""录制回放端到端（功能部分）：D1-AC-18、M12-AC-041、AC-044、AC-049、AC-050（M12 §6.7、§7.4；ADR-040、ADR-049 G6a）。

合成录制 → 真实 replay-worker 宿主（ReplayHost，后台线程 ≤ 250 Hz）→ 真实 Gateway（tests/rt 的 GwStack）→ WS 客户端：
- open：实时须 PAUSED；open 后 serverInfo{mode: replay}、TIME bit7、SNAPSHOT（BATCH 置 REPLAY）；
- seek：TIME → playbackState{did_seek} → SNAPSHOT；SNAPSHOT 中 `uav/{id}/state`（Full64）、`swarm/state`（Lite32）与
  `env/state` 的载荷与录制消息逐字节一致（G6a）；首个 backfill 帧时延记录在 `__rt_seek_ms`（500 ms 门槛属性能验收，
  本用例只断言 ≤ 2 s 的功能上界）；
- play：WS 事件与录制事件除 seq、epoch、producer 外相同；R68 事件分页在回放中可用；
- close 回到实时。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rt"))

import fakesim
import rechelp
import rtc

from awr.contracts import LAYOUT_ID
from awr.contracts import frame as F
from awr.contracts.layouts import DRONE_STATE64
from awr.recorder.formats import BLOCK_HDR, split_prefix

pytestmark = pytest.mark.ext
SEEK_MS: list[float] = []


class HostThread:
    def __init__(self, st, runs: Path, cv: str) -> None:
        from awr.recorder.replay import ReplayHost
        from awr.runtime.bus import LocalBus
        from awr.runtime.statering import LocalRing

        self.bus = LocalBus.open("replay-worker", namespace=st.settings.namespace)
        self.h = ReplayHost(self.bus, run_dir=Path(st.settings.run_dir), runs_dir=runs, world_id=st.settings.world_id,
                            current_binding={"world_id": st.settings.world_id, "layout_id": LAYOUT_ID, "content_version": cv},
                            ring_cls=LocalRing)
        self.stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self) -> None:
        while not self.stop.is_set():
            self.h.step()
            time.sleep(0.004)

    def close(self) -> None:
        self.stop.set()
        self.t.join(5)
        self.h.close()
        self.bus.close()


@pytest.fixture(scope="module")
def st(tmp_path_factory: pytest.TempPathFactory):
    from awr.recorder.synth import synthesize

    runs = tmp_path_factory.mktemp("runsrep")
    old = os.environ.get("AWR_RUNS_DIR")
    os.environ["AWR_RUNS_DIR"] = str(runs)
    s = fakesim.GwStack(n=2)
    cv = (s.ctx.world_info or {}).get("contentVersion") or "synth"
    s.rec = synthesize(runs / rechelp.RUN, n=60, sim_s=30, marked=["sim-0001", "sim-0002"], critical_every_s=7, content_version=cv)
    s.runs = runs
    s.host = HostThread(s, runs, cv)
    yield s
    s.host.close()
    s.close()
    if old is None:
        os.environ.pop("AWR_RUNS_DIR", None)
    else:
        os.environ["AWR_RUNS_DIR"] = old


def _recorded(st) -> dict[str, list[tuple[int, bytes]]]:
    out: dict[str, list[tuple[int, bytes]]] = {}
    for t, lt, d in rechelp.messages(Path(st.rec["dir"]) / "rec-000.mcap"):
        out.setdefault(t, []).append((lt, bytes(split_prefix(d)[3])))
    return out


def _last_le(rows: list[tuple[int, bytes]], t: int) -> tuple[int, bytes]:
    best = rows[0]
    for x in rows:
        if x[0] <= t:
            best = x
    return best


def test_replay_end_to_end(st) -> None:
    hint = rtc.hint_of("replaye2e")
    tok = rtc.token(st.base, "operator", hint)
    rec = _recorded(st)

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "subscribe", "subs": [
            {"id": 1, "topic": "swarm/state", "rate": 10, "mode": "latest"},
            {"id": 2, "topic": "env/state", "rate": 10, "mode": "latest"},
            {"id": 3, "topic": "event", "rate": 0, "mode": "all"}]})
        await c.until(lambda k, x: k == "batch", 5)
        await c.send({"op": "call", "id": "rep-pause-0001", "service": "sim/pause", "args": {}})
        await c.result("rep-pause-0001")
        await c.until(lambda k, x: k == "time" and (x.state & 0x0F) == 2, 5)
        e0 = st.gw.clock.global_epoch
        await c.send({"op": "playback", "cmd": "open", "run": rechelp.RUN, "segment": 0, "request_id": "o1"})
        _, pb = await c.until(lambda k, x: k == "json" and x["op"] == "playbackState" and x["request_id"] == "o1"
                              and x["status"] in ("paused", "error"), 10)
        assert pb["status"] == "paused", pb
        assert pb["dataEnd_ns"] == 30_000_000_000 and pb["speed_max"] == 20.0
        await c.until(lambda k, x: k == "batch" and x.header.flags & F.BATCH_REPLAY, 5)
        assert st.gw.mode == "replay" and st.gw.clock.global_epoch == e0 + 1
        # 选中机的 60 Hz 通道：订阅在回放 roster 装入之后（open 时在途的实时 roster 回复由网关丢弃，FX-GW）
        end = time.monotonic() + 5
        while "sim-0001" not in st.gw.roster and time.monotonic() < end:
            await c.drain(0.1)
        assert st.gw.roster and all(v.startswith("sim-") for v in st.gw.roster), sorted(st.gw.roster)[:5]  # 没有实时名册 f001…
        ids = c.topic_ids
        assert "uav/sim-0001/state" in ids, sorted(ids)[:10]
        await c.send({"op": "subscribe", "subs": [{"id": 4, "topic": "uav/sim-0001/state", "rate": 60, "mode": "latest"}]})
        await c.drain(0.3)
        ids = c.topic_ids
        # seek：TIME → playbackState{did_seek} → SNAPSHOT，载荷逐字节一致
        for k, t in enumerate((12_345_000_000, 3_210_000_000, 27_777_000_000)):
            e_before = st.gw.clock.global_epoch
            t0 = time.monotonic()
            await c.send({"op": "playback", "cmd": "seek", "seek_ns": t, "request_id": f"s{k}"})
            want_epoch = (e_before + 1) & 0xFFFF
            await c.until(lambda k_, x, ep=want_epoch: k_ == "batch" and x.header.epoch == ep and x.header.flags & F.BATCH_SNAPSHOT, 5)
            SEEK_MS.append((time.monotonic() - t0) * 1000)
            assert SEEK_MS[-1] < 2000
            await c.drain(0.25)
            tb, blk = _last_le(rec["/swarm/uav/state_block"], t)
            full = c.latest_full(ids["uav/sim-0001/state"])
            want = [d for tt, d in rec["/uav/sim-0001/state"] if tt == tb]
            assert full is not None and want and full.tobytes() == want[0], k
            lite = c.latest_lite(ids["swarm/uav/state"])
            n = BLOCK_HDR.unpack_from(blk, 0)[0]
            assert lite is not None and lite.tobytes() == blk[16:16 + n * 32]
            _env_t, env_raw = _last_le(rec["/env/state"], t)
            for bb in reversed(c.batches):
                r = bb.by_channel(ids["env/state"])
                if r is not None:
                    assert bb.payload(r) == env_raw
                    break
        # play ×5：WS 事件与录制一致（除 seq、epoch、producer）
        await c.send({"op": "playback", "cmd": "seek", "seek_ns": 6_000_000_000, "request_id": "s9"})
        await c.until(lambda k_, x: k_ == "json" and x["op"] == "playbackState" and x.get("request_id") == "s9"
                      and x.get("did_seek"), 5)
        n_before = len(c.events())
        await c.send({"op": "playback", "cmd": "speed", "speed": 5, "request_id": "sp"})
        _, sp = await c.until(lambda k_, x: k_ == "json" and x["op"] == "playbackState" and x.get("request_id") == "sp", 5)
        assert sp["speed"] == 5.0
        await c.send({"op": "playback", "cmd": "play", "request_id": "pl"})
        await c.until(lambda k_, x: k_ == "time" and (x.state & 0x0F) == 1 and x.replay, 5)
        await c.drain(1.5)
        got = [e for e in c.events()[n_before:] if not str(e.get("type", "")).startswith(("replay.", "proc."))]
        assert got, "no replay events"
        import msgpack

        recorded = {(e["t_sim_ns"], e["kind"]): e for e in (msgpack.unpackb(d, raw=False) for _t, d in rec["/event"])}
        for e in got:
            o = recorded[(e["t_sim_ns"], e["type"])]
            assert (e["level"], e.get("uav"), e.get("data")) == (o["severity"], o["uav"], o["data"])
        # R68：回放中事件分页
        r = httpx.get(f"{st.base}/api/runs/{rechelp.RUN}/events", params={"from_ns": 0, "to_ns": 10_000_000_000, "level_min": 3},
                      headers={"authorization": f"Bearer {tok['token']}"}, timeout=10)
        assert r.status_code == 200 and [x["t_sim_ns"] for x in r.json()["items"]] == [7_000_000_000]
        await c.send({"op": "playback", "cmd": "close", "request_id": "cl"})
        await c.until(lambda k_, x: k_ == "json" and x["op"] == "serverInfo" and x["mode"] == "live", 5)
        assert st.gw.mode == "live"
        await c.ws.close()

    asyncio.run(run())
    print(f"seek first backfill frame ms: {[round(x, 1) for x in SEEK_MS]}")
    assert np.all(np.asarray(SEEK_MS) < 2000)
    assert DRONE_STATE64.itemsize == 64
