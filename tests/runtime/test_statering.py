"""StateRing 与 LocalRing（M11-AC-001；M11-FR-001 至 FR-005；AWR-17 §9.2）。"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import rtlib

from awr.contracts import LAYOUT_ID
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32
from awr.runtime import statering as S
from awr.runtime.statering import (
    LOSSLESS,
    LOSSY,
    LayoutMismatch,
    LocalRing,
    PlatformUnsupported,
    RingHeader,
    RingNotReady,
    StateRing,
    file_bytes,
    slot_bytes,
)


def rows(n: int, tag: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    full = np.zeros(n, DRONE_STATE64)
    lite = np.zeros(n, SWARM_LITE32)
    full["agent_no"] = np.arange(n)
    lite["agent_no"] = np.arange(n)
    full["pos"][:, 0] = tag
    lite["pos"][:, 0] = tag
    return full, lite


def test_layout_constants_and_file(shm_dir: Path) -> None:
    assert slot_bytes(1024) == 98_368
    assert file_bytes(1024, 32) == 3_148_288
    p = shm_dir / "state.sim-core"
    r = StateRing.create(p, layout_id=LAYOUT_ID)
    try:
        st = os.stat(p)
        assert st.st_size == 3_148_288
        assert st.st_mode & 0o777 == 0o600
        assert p.read_bytes()[:4] == (0x31525741).to_bytes(4, "little")  # "AWR1"
        h = r.header()
        assert (h.version, h.slot_count, h.capacity, h.layout_id, h.epoch, h.segment) == (1, 32, 1024, LAYOUT_ID, 1, 0)
        assert h.writer_pid == os.getpid() and h.head == 0 and h.rate_milli == 1000
        assert not list(shm_dir.glob("*.tmp"))  # tmp 文件已原子替换
    finally:
        r.close()


def test_min_slots_and_capacity(shm_dir: Path) -> None:
    with pytest.raises(ValueError):
        StateRing.create(shm_dir / "a", slots=15, layout_id=LAYOUT_ID)
    with pytest.raises(ValueError):
        StateRing.create(shm_dir / "b", capacity=0, layout_id=LAYOUT_ID)


def test_publish_read_roundtrip_and_header(shm_dir: Path) -> None:
    p = shm_dir / "state.sim-core"
    w = StateRing.create(p, capacity=1024, layout_id=LAYOUT_ID, id_base=0, id_count=1024)
    r = StateRing.attach(p, expect_layout_id=LAYOUT_ID)
    try:
        assert r.read_latest() is None
        full, lite = rows(1000, 7.5)
        w.heartbeat(123_000_000, 9, 2000, step_seq=42)
        w.set_step_stats(900, 2500, 4000, 4000, 998, 0)
        fs = w.publish(full, lite, 123_000_000, 3, S.SLOT_RESET)
        f = r.read_latest()
        assert f is not None and f.frame_seq == fs == 1
        assert (f.t_sim_ns, f.epoch, f.roster_version, f.n_rows, f.flags) == (123_000_000, 1, 3, 1000, S.SLOT_RESET)
        assert f.full == full.tobytes() and f.lite == lite.tobytes()
        assert r.read_latest(f.frame_seq) is None  # 未变化
        h = r.header()
        assert (h.t_sim_ns, h.clock_state, h.rate_milli, h.step_seq) == (123_000_000, 9, 2000, 42)
        assert (h.step_p50_us, h.step_p99_us, h.step_max_us, h.step_budget_us, h.rtf_milli, h.catchup_saturated) == (
            900, 2500, 4000, 4000, 998, 0)
        assert h.roster_version == 3 and h.head == 1
        w.set_epoch(5)
        w.set_segment(2)
        assert r.identity()[1:] == (os.getpid(), 5, 2)
        assert r.identity()[0] == r.mapped_ino
        assert r.writer_age_ms() < 1000
    finally:
        r.close()
        w.close()


def test_begin_commit_zero_copy(shm_dir: Path) -> None:
    w = StateRing.create(shm_dir / "r", capacity=64, slots=16, layout_id=LAYOUT_ID)
    r = StateRing.attach(shm_dir / "r", expect_layout_id=LAYOUT_ID)
    try:
        fv, lv, t = w.begin_publish()
        assert fv.shape == (64,) and lv.shape == (64,)
        with pytest.raises(RuntimeError):
            w.begin_publish()  # 未提交前不得再次 begin
        assert r.read_latest() is None  # 写入中的槽不可见
        fv["agent_no"][:10] = np.arange(10)
        fv["pos"][:10, 2] = 30.0
        lv["agent_no"][:10] = np.arange(10)
        fs = w.commit_publish(t, 10, 5, 1)
        f = r.read_latest()
        got = np.frombuffer(f.full, DRONE_STATE64)
        assert f.frame_seq == fs and f.n_rows == 10 and list(got["agent_no"]) == list(range(10))
        assert float(got["pos"][3, 2]) == 30.0
        with pytest.raises(RuntimeError):
            w.commit_publish(t, 10, 5, 1)  # 票据已用
    finally:
        r.close()
        w.close()


def test_layout_mismatch_312(shm_dir: Path) -> None:
    p = shm_dir / "state.sim-core"
    w = StateRing.create(p, capacity=16, slots=16, layout_id=LAYOUT_ID)
    try:
        with pytest.raises(LayoutMismatch) as ei:
            StateRing.attach(p, expect_layout_id=LAYOUT_ID ^ 1)
        assert ei.value.code == 312
    finally:
        w.close()
    # g05 原型布局（4 游标，头区 384 B）：尺寸不符即拒绝复用
    proto = shm_dir / "proto"
    good = bytearray(Path(p).read_bytes())
    proto.write_bytes(bytes(good[: S.HDR_BYTES]) + b"\0" * (128 + 16 * slot_bytes(16)))
    with pytest.raises(LayoutMismatch):
        StateRing.attach(proto, expect_layout_id=LAYOUT_ID)
    with pytest.raises(RingNotReady):
        StateRing.attach(shm_dir / "missing", expect_layout_id=LAYOUT_ID)
    empty = shm_dir / "empty"
    empty.write_bytes(b"\0" * file_bytes(16, 16))
    with pytest.raises(RingNotReady):
        StateRing.attach(empty, expect_layout_id=LAYOUT_ID)


def test_platform_unsupported(shm_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "_machine", lambda: "aarch64")
    with pytest.raises(PlatformUnsupported):
        StateRing.create(shm_dir / "x", layout_id=LAYOUT_ID)
    with pytest.raises(PlatformUnsupported):
        StateRing.open_or_create(shm_dir / "x", capacity=16, slots=16, layout_id=LAYOUT_ID)


def test_eight_reader_cursors(shm_dir: Path) -> None:
    p = shm_dir / "r"
    w = StateRing.create(p, capacity=16, slots=16, layout_id=LAYOUT_ID)
    readers = [StateRing.attach(p, expect_layout_id=LAYOUT_ID) for _ in range(9)]
    try:
        idx = [readers[i].register(LOSSY, f"reader-{i}") for i in range(8)]
        assert sorted(idx) == list(range(8))
        with pytest.raises(RuntimeError):
            readers[8].register(LOSSY, "reader-8")
        assert readers[0].register(LOSSY, "reader-0") == idx[0]  # 同 pid 同名复用原行
        readers[3].unregister()
        assert readers[8].register(LOSSY, "reader-8") == idx[3]
        # 进程已退出的读者行可回收
        readers[8].unregister()
        w._cur[idx[3]]["pid"] = 999_999_999 & 0x7FFFFFFF
        w._cur[idx[3]]["mode"] = LOSSY
        assert readers[8].register(LOSSY, "reader-8b") == idx[3]
    finally:
        for r in readers:
            r.close()
        w.close()


def test_drain_overrun(shm_dir: Path) -> None:
    p = shm_dir / "r"
    w = StateRing.create(p, capacity=8, slots=16, layout_id=LAYOUT_ID)
    r = StateRing.attach(p, expect_layout_id=LAYOUT_ID)
    try:
        r.register(LOSSY, "recorder")
        full, lite = rows(8)
        for k in range(5):
            w.publish(full, lite, k, 1)
        frames, lost = r.drain()
        assert [f.frame_seq for f in frames] == [1, 2, 3, 4, 5] and lost == 0
        for k in range(16 + 7):
            w.publish(full, lite, k, 1)
        frames, lost = r.drain()
        assert lost == 7 and len(frames) == 16 and frames[-1].frame_seq == 5 + 23
        assert r.drain() == ([], 0)
    finally:
        r.close()
        w.close()


def test_lossless_backpressure_demotes_after_250ms(shm_dir: Path) -> None:
    p = shm_dir / "r"
    w = StateRing.create(p, capacity=8, slots=16, layout_id=LAYOUT_ID)
    r = StateRing.attach(p, expect_layout_id=LAYOUT_ID)
    try:
        r.register(LOSSLESS, "recorder")
        full, lite = rows(8)
        for k in range(16):
            w.publish(full, lite, k, 1)
        t0 = time.monotonic()
        w.publish(full, lite, 16, 1)  # 第 17 帧会覆盖读者未消费的第 1 帧：写者等待 ≤ 250 ms 后降级该读者
        dt = time.monotonic() - t0
        assert 0.2 <= dt < 1.0
        assert w.lossless_demotions == 1 and int(w._cur[r.reader_index]["mode"]) == LOSSY
        # 读者按时 drain 时写者不等待
        r2 = StateRing.attach(p, expect_layout_id=LAYOUT_ID)
        r2.register(LOSSLESS, "recorder-2")
        t0 = time.monotonic()
        for k in range(40):
            w.publish(full, lite, k, 1)
            r2.drain()
        assert time.monotonic() - t0 < 0.2 and w.lossless_demotions == 1
        r2.close()
    finally:
        r.close()
        w.close()


def test_open_or_create_reuse_and_replace(shm_dir: Path) -> None:
    p = shm_dir / "state.sim-core"
    w1, reused = StateRing.open_or_create(p, capacity=32, slots=16, layout_id=LAYOUT_ID)
    assert reused is False
    r = StateRing.attach(p, expect_layout_id=LAYOUT_ID)
    full, lite = rows(4)
    for k in range(3):
        w1.publish(full, lite, k, 1)
    ino = os.stat(p).st_ino
    w1.close()
    w2, reused = StateRing.open_or_create(p, capacity=32, slots=16, layout_id=LAYOUT_ID, id_base=0, id_count=32)
    try:
        assert reused is True and os.stat(p).st_ino == ino
        assert w2.publish(full, lite, 9, 1) == 4  # head 继续递增
        assert r.read_latest().frame_seq == 4  # 读者 mmap 仍有效
        assert r.identity()[0] == r.mapped_ino
    finally:
        w2.close()
    w3, reused = StateRing.open_or_create(p, capacity=64, slots=16, layout_id=LAYOUT_ID)  # 不兼容：替换文件
    try:
        assert reused is False
        assert r.identity()[0] != r.mapped_ino  # 读者据 inode 变化重新 attach
        r2 = StateRing.attach(p, expect_layout_id=LAYOUT_ID)
        assert r2.capacity == 64 and r2.header().head == 0
        r2.close()
    finally:
        w3.close()
        r.close()


def test_header_seqlock_retry(shm_dir: Path) -> None:
    w = StateRing.create(shm_dir / "r", capacity=8, slots=16, layout_id=LAYOUT_ID)
    r = StateRing.attach(shm_dir / "r", expect_layout_id=LAYOUT_ID)
    try:
        w.heartbeat(1000, 1)
        h1 = r.header()
        # 模拟写者停在时钟组写入中间（clock_seq 为奇数）：读者重试后沿用上一次读数并计数
        ci = S._I64["clock_seq"]
        w._u64[ci] = int(w._u64[ci]) + 1
        w._i64[S._I64["t_sim_ns"]] = 999
        h2 = r.header()
        assert h2 == h1 and r.header_retries == 1
    finally:
        r.close()
        w.close()


def _hdr_writer(path: str, stop_at: float) -> None:
    w = StateRing.attach(Path(path), expect_layout_id=LAYOUT_ID)  # 测试中以 attach 句柄充当写者
    k = 0
    while time.monotonic() < stop_at:
        k += 1
        w.heartbeat(k * 4_000_000, k % 10, k % 1000 + 1, step_seq=k)


def test_header_consistent_under_concurrent_writer(shm_dir: Path) -> None:
    """写者进程全速写时钟组，读者 10 万次 header()：(t_sim_ns, clock_state, rate_milli) 不同刻 0 次，heartbeat_ns 单调。"""
    p = shm_dir / "r"
    w = StateRing.create(p, capacity=8, slots=16, layout_id=LAYOUT_ID)
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=_hdr_writer, args=(str(p), time.monotonic() + 30))
    proc.start()
    r = StateRing.attach(p, expect_layout_id=LAYOUT_ID)
    try:
        assert rtlib.wait_until(lambda: r.header().t_sim_ns > 0, 10)
        bad = 0
        last_hb = 0
        for _ in range(100_000):
            h = r.header()
            k = h.t_sim_ns // 4_000_000
            if k and (h.clock_state != k % 10 or h.rate_milli != k % 1000 + 1):
                bad += 1
            if h.heartbeat_ns < last_hb:
                bad += 1
            last_hb = h.heartbeat_ns
        assert bad == 0
    finally:
        proc.terminate()
        proc.join(5)
        r.close()
        w.close()


def _tear_writer(path: str, n: int, stop_at: float) -> None:
    w = StateRing.attach(Path(path), expect_layout_id=LAYOUT_ID)
    while time.monotonic() < stop_at:
        fv, lv, t = w.begin_publish()
        fv["pos"][:n, 0] = float(t.frame_seq)
        lv["pos"][:n, 0] = float(t.frame_seq)
        w.commit_publish(t, n, t.frame_seq, 1)


def _tear_reader(path: str, n: int, reads: int, out) -> None:
    r = StateRing.attach(Path(path), expect_layout_id=LAYOUT_ID)
    r.register(LOSSY, f"tear-{os.getpid()}")
    ok = torn = 0
    last = 0
    while ok < reads:
        f = r.read_latest(last)
        if f is None:
            continue
        last = f.frame_seq
        fp = np.frombuffer(f.full, DRONE_STATE64)["pos"][:, 0]
        lp = np.frombuffer(f.lite, SWARM_LITE32)["pos"][:, 0]
        if len(fp) != n or not (np.all(fp == f.frame_seq) and np.all(lp == f.frame_seq)):
            torn += 1  # 撕裂漏检：内容与帧号不一致
        ok += 1
    out.put((ok, torn, r.torn_reads))
    r.close()


def test_torn_read_detection_1_writer_3_readers(shm_dir: Path) -> None:
    """1 写 3 读并发 10^5 次读取，撕裂漏检 0（M11-AC-001）。"""
    n = 256
    p = shm_dir / "r"
    w = StateRing.create(p, capacity=n, slots=16, layout_id=LAYOUT_ID)
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    wp = ctx.Process(target=_tear_writer, args=(str(p), n, time.monotonic() + 120))
    wp.start()
    try:
        assert rtlib.wait_until(lambda: w.head > 100, 20)
        readers = [ctx.Process(target=_tear_reader, args=(str(p), n, 34_000, q)) for _ in range(3)]
        for rp in readers:
            rp.start()
        results = [q.get(timeout=180) for _ in readers]
        for rp in readers:
            rp.join(10)
        assert sum(x[0] for x in results) >= 100_000
        assert sum(x[1] for x in results) == 0
    finally:
        wp.terminate()
        wp.join(5)
        w.close()


def test_reader_survives_writer_kill9(shm_dir: Path) -> None:
    p = shm_dir / "state.sim-core"
    code = ("import sys,time,numpy as np;from pathlib import Path;from awr.contracts import LAYOUT_ID;"
            "from awr.contracts.layouts import DRONE_STATE64,SWARM_LITE32;from awr.runtime.statering import StateRing;"
            f"w=StateRing.create(Path({str(p)!r}),capacity=16,slots=16,layout_id=LAYOUT_ID);"
            "f=np.zeros(16,DRONE_STATE64);l=np.zeros(16,SWARM_LITE32);k=0;print('READY',flush=True)\n"
            "while True:\n k+=1;w.heartbeat(k,1);f['pos'][:,0]=k;w.publish(f,l,k,1);time.sleep(0.004)")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "READY"
        r = StateRing.attach(p, expect_layout_id=LAYOUT_ID)
        assert rtlib.wait_until(lambda: r.head > 20, 5)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(5)
        head = r.head
        f = r.read_latest()
        assert f is not None and f.frame_seq == head
        assert float(np.frombuffer(f.full, DRONE_STATE64)["pos"][0, 0]) == float(f.t_sim_ns)
        time.sleep(0.3)
        assert r.writer_age_ms() >= 250  # 心跳停止：Gateway 据此判 STALLED
        r.close()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_header_placeholder() -> None:
    h = RingHeader.placeholder(heartbeat_ns=77)
    assert h.heartbeat_ns == 77 and h.clock_state == 0 and h.rate_milli == 1000 and h.t_sim_ns == 0
    assert len(RingHeader._fields) == 24


def test_local_ring_same_interface() -> None:
    path = Path("/inproc/state.sim-core")
    w, reused = LocalRing.open_or_create(path, capacity=32, slots=16, layout_id=LAYOUT_ID)
    try:
        assert reused is False
        r = LocalRing.attach(path, expect_layout_id=LAYOUT_ID)
        r2 = LocalRing.attach(path, expect_layout_id=LAYOUT_ID)
        assert r.register(LOSSY, "api") != r2.register(LOSSY, "api")  # 同进程两个句柄占不同游标
        full, lite = rows(32, 3.0)
        w.heartbeat(10, 1)
        w.publish(full, lite, 10, 2)
        f = r.read_latest()
        assert f.full == full.tobytes() and f.roster_version == 2
        assert r.drain()[0] == [] and r2.drain()[0][0].frame_seq == 1
        with pytest.raises(LayoutMismatch):
            LocalRing.attach(path, expect_layout_id=LAYOUT_ID ^ 1)
        ident = r.identity()
        w2, reused = LocalRing.open_or_create(path, capacity=64, slots=16, layout_id=LAYOUT_ID)
        assert reused is False and r.identity()[0] != ident[0]
        w2.close()
        r.close()
        r2.close()
    finally:
        w.close()
        LocalRing.remove(path)
