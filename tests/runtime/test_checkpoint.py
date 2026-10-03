"""checkpoint 文件格式与 CheckpointStore（M11-AC-007；M11-FR-016、FR-017；ADR-019；M11 §6.3.7）。"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from awr.contracts import LAYOUT_ID
from awr.contracts.layouts import DRONE_STATE64
from awr.runtime import checkpoint as CK
from awr.runtime.checkpoint import CheckpointError, CheckpointStore, read_checkpoint, serialize


def soa(n: int = 1000, seed: int = 1) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    d = {"p": rng.random((n, 3)), "v": rng.random((n, 3)).astype(np.float32), "q": rng.random((n, 4)),
         "omega": rng.random((n, 3)), "fs": rng.integers(0, 14, n).astype(np.uint8), "soc": rng.random(n),
         "full": np.zeros(n, DRONE_STATE64), "t_scalar": np.array(12.5), "empty": np.zeros((0, 3))}
    for i in range(24):
        d[f"state{i}"] = rng.random(n)
    return d


def meta() -> dict:
    return {"clock": {"t_ns": 123, "rate": 1.0, "state": 1}, "rng": [1, 2, 3],
            "cids": {f"c{i}": {"status": "accepted", "t": i} for i in range(4096)}, "contracts": "1.0.0"}


def eq(a: dict, b: dict) -> bool:
    for k, v in a.items():
        w = b[k]
        if v.dtype.names:
            w = w.view(v.dtype)
        if v.shape != w.shape or v.tobytes() != w.tobytes():
            return False
    return set(a) == set(b)


def test_format_header_and_alignment(tmp_path: Path) -> None:
    arrays = soa(10)
    data = serialize(7, 2, 3, LAYOUT_ID, arrays, {"a": 1})
    assert data[:4] == b"AWRC" and int.from_bytes(data[4:6], "little") == 1
    toc = np.frombuffer(data, CK._TOC, count=len(arrays), offset=64)
    assert all(int(t["offset"]) % 64 == 0 for t in toc)
    p = tmp_path / "x.bin"
    p.write_bytes(data)
    c = read_checkpoint(p, layout_id=LAYOUT_ID)
    assert (c.t_sim_ns, c.epoch, c.segment, c.layout_id) == (7, 2, 3, LAYOUT_ID) and eq(arrays, c.arrays)
    with pytest.raises(CheckpointError):
        read_checkpoint(p, layout_id=LAYOUT_ID ^ 1)


def test_serialize_into_reused_buffer_is_identical() -> None:
    """`serialize(out=...)` 在复用缓冲中编码（写线程不再每代新分配并拷贝约 3.5 MB，ADR-070）：与不复用时逐字节相同，缓冲里
    残留更长的上一代内容（含对齐填充处的非零字节）时也相同；含 0 维数组。"""
    arrays = soa(10)
    arrays["zero_d"] = np.array(3.5)
    ref = serialize(7, 2, 3, LAYOUT_ID, arrays, {"a": 1, "b": [1, 2, 3]})
    buf = bytearray(b"\xab" * (len(ref) + 4096))
    got = serialize(7, 2, 3, LAYOUT_ID, arrays, {"a": 1, "b": [1, 2, 3]}, out=buf)
    assert isinstance(got, memoryview) and bytes(got) == ref
    got.release()
    small = bytearray(16)
    got = serialize(7, 2, 3, LAYOUT_ID, arrays, {"a": 1, "b": [1, 2, 3]}, out=small)
    assert bytes(got) == ref and len(small) >= len(ref)


def test_gate_defers_encoding_until_set(shm_dir: Path) -> None:
    """CheckpointStore `gate`（ADR-070）：gate 未置位时写线程停在让出点（每个至多 gate_timeout_s），置位后很快写完；
    写出的内容与不带 gate 时逐字节相同。"""
    import threading

    gate = threading.Event()
    st = CheckpointStore(shm_dir / "ckg", layout_id=LAYOUT_ID, gate=gate, gate_timeout_s=5.0)
    try:
        arrays, m = soa(10), {"big": list(range(1000))}
        assert st.save(1_000_000_000, 1, 0, arrays, m)
        time.sleep(0.3)
        assert st.stats["written"] == 0  # 停在第一个让出点
        gate.set()
        t0 = time.monotonic()
        while st.stats["written"] == 0 and time.monotonic() - t0 < 5.0:
            time.sleep(0.01)
        assert st.stats["written"] == 1 and time.monotonic() - t0 < 2.0
        got = next((shm_dir / "ckg").glob("*.bin")).read_bytes()
        assert got == serialize(1_000_000_000, 1, 0, LAYOUT_ID, arrays, m)
    finally:
        st.close()


def test_roundtrip_1000_agents_and_meta(shm_dir: Path, tmp_path: Path) -> None:
    st = CheckpointStore(shm_dir / "ckpt", layout_id=LAYOUT_ID, mirror=tmp_path / "ckpt", mirror_every_s=0.05)
    try:
        arrays, m = soa(), meta()
        assert st.save(1_000_000_000, 4, 2, arrays, m)
        assert st.flush()
        c = st.load_latest()
        assert c is not None and c.t_sim_ns == 1_000_000_000 and (c.epoch, c.segment) == (4, 2)
        assert eq(arrays, c.arrays) and c.meta == m
        assert (shm_dir / "ckpt" / f"{1_000_000_000:020d}.bin").exists()
    finally:
        st.close()
    assert (tmp_path / "ckpt" / f"{1_000_000_000:020d}.bin").exists()  # close(final) 同步镜像


def test_keep_three_generations_poison_and_fallback(shm_dir: Path, tmp_path: Path) -> None:
    st = CheckpointStore(shm_dir / "ckpt", layout_id=LAYOUT_ID, keep=3, mirror=tmp_path / "m", mirror_every_s=1e9)
    try:
        for k in range(5):
            st.save(k * 1_000_000_000, 1, 1, {"x": np.full(4, k)}, {"k": k})
            st.flush()
        gens = sorted(p.name for p in (shm_dir / "ckpt").glob("*.bin"))
        assert gens == [f"{k * 1_000_000_000:020d}.bin" for k in (2, 3, 4)]
        # crc 损坏：视为毒性并回退上一代
        newest = shm_dir / "ckpt" / f"{4_000_000_000:020d}.bin"
        b = bytearray(newest.read_bytes())
        b[-1] ^= 0xFF
        newest.write_bytes(bytes(b))
        c = st.load_latest()
        assert c.t_sim_ns == 3_000_000_000 and newest.with_suffix(".poison").exists()
        # 恢复后 5 s 内再次崩溃：生产者 poison 当前代，改用上一代
        st.poison(3_000_000_000)
        assert st.load_latest().t_sim_ns == 2_000_000_000
        assert st.poisoned_generations() == 2
        st.poison(2_000_000_000)
        assert st.poisoned_generations() == 3  # 连续 3 代中毒：通知生产者从剧本起点重开
        assert st.load_latest() is None or st.load_latest().t_sim_ns != 2_000_000_000
        assert st.load_latest(skip_poisoned=False) is not None
    finally:
        st.close(final=False)


def test_mirror_used_when_tmpfs_empty(shm_dir: Path, tmp_path: Path) -> None:
    st = CheckpointStore(shm_dir / "ckpt", layout_id=LAYOUT_ID, mirror=tmp_path / "m", mirror_every_s=0.01)
    st.save(5, 1, 1, {"x": np.arange(3)}, {})
    st.close()
    for p in (shm_dir / "ckpt").iterdir():
        p.unlink()
    st2 = CheckpointStore(shm_dir / "ckpt", layout_id=LAYOUT_ID, mirror=tmp_path / "m")
    try:
        c = st2.load_latest()
        assert c is not None and c.t_sim_ns == 5 and list(c.arrays["x"]) == [0, 1, 2]
    finally:
        st2.close(final=False)


def test_save_skips_when_both_buffers_busy(shm_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = CheckpointStore(shm_dir / "ckpt", layout_id=LAYOUT_ID)
    orig = st._write

    def slow(slot):
        time.sleep(0.3)
        orig(slot)

    monkeypatch.setattr(st, "_write", slow)
    try:
        assert st.save(1, 1, 1, {"x": np.zeros(4)}, {})
        assert st.save(2, 1, 1, {"x": np.zeros(4)}, {})
        assert st.save(3, 1, 1, {"x": np.zeros(4)}, {}) is False  # 不阻塞主循环：跳过并计数
        assert st.stats["skipped"] == 1
        assert st.flush(5)
    finally:
        st.close(final=False)


def test_save_main_thread_cost(shm_dir: Path) -> None:
    """功能级上界（主线程只做 numpy 拷贝）；p99 ≤ 2 ms 的严格判定见 perf 用例。"""
    st = CheckpointStore(shm_dir / "ckpt", layout_id=LAYOUT_ID)
    arrays, m = soa(), meta()
    ts = []
    try:
        for k in range(30):
            t = time.perf_counter()
            st.save(k, 1, 1, arrays, m)
            ts.append((time.perf_counter() - t) * 1e3)
            st.flush()
        assert float(np.percentile(ts, 99)) < 20.0
    finally:
        st.close(final=False)


@pytest.mark.perf
def test_save_main_thread_p99_under_2ms(shm_dir: Path) -> None:
    st = CheckpointStore(shm_dir / "ckpt", layout_id=LAYOUT_ID)
    arrays, m = soa(), meta()
    ts = []
    try:
        for k in range(200):
            t = time.perf_counter()
            st.save(k, 1, 1, arrays, m)
            ts.append((time.perf_counter() - t) * 1e3)
            time.sleep(0.02)
        assert float(np.percentile(ts, 99)) <= 2.0
    finally:
        st.close(final=False)


def test_restore_for_restart_poison_policy(tmp_path: Path) -> None:
    """FR-017（ext）：恢复后 5 s 内再次崩溃 → 该代中毒改用上一代；连续 3 代中毒 → None（剧本起点重开）；冷启动不恢复。"""
    st = CheckpointStore(tmp_path / "ckpt", layout_id=LAYOUT_ID, keep=5)
    for t in (1_000, 2_000, 3_000, 4_000):
        assert st.save(t, 1, 0, {"x": np.full(4, t, np.int64)}, {"t": t})
        assert st.flush(5.0)
    assert st.restore_for_restart(0) is None
    ck = st.restore_for_restart(1)
    assert ck is not None and ck.t_sim_ns == 4_000
    ck = st.restore_for_restart(2)  # 恢复后立即再崩：4000 中毒
    assert ck is not None and ck.t_sim_ns == 3_000
    ck = st.restore_for_restart(3)
    assert ck is not None and ck.t_sim_ns == 2_000
    assert st.restore_for_restart(4) is None  # 4000、3000、2000 连续中毒
    assert st.poisoned_generations() == 3
    st.close(final=False)


def test_restore_after_window_does_not_poison(tmp_path: Path, monkeypatch) -> None:
    import awr.runtime.checkpoint as ckm

    st = CheckpointStore(tmp_path / "ckpt", layout_id=LAYOUT_ID)
    assert st.save(5_000, 1, 0, {"x": np.zeros(2)}, {})
    assert st.flush(5.0)
    assert st.restore_for_restart(1).t_sim_ns == 5_000
    real = ckm.time.monotonic_ns
    monkeypatch.setattr(ckm.time, "monotonic_ns", lambda: real() + 6_000_000_000)
    assert st.restore_for_restart(2).t_sim_ns == 5_000  # 稳定运行超过 5 s 后再崩：不中毒
    st.close(final=False)


def test_poison_skips_generations_written_after_restore(tmp_path: Path) -> None:
    """恢复后 5 s 内再崩：恢复点之后由崩溃进程写出的各代同样跳过（stale），改用恢复点的上一代；stale 不计入连续中毒代数
    （D1-AC-11b；D1 验收第 1 轮：第二次恢复点 4.104 s 晚于第一次 4.064 s）。"""
    st = CheckpointStore(tmp_path / "ckpt", layout_id=LAYOUT_ID, keep=8)
    for t in (1_000, 2_000, 3_000):
        assert st.save(t, 1, 0, {"x": np.full(4, t, np.int64)}, {"t": t})
        assert st.flush(5.0)
    ck = st.restore_for_restart(1)
    assert ck is not None and ck.t_sim_ns == 3_000
    for t in (3_500, 4_000):  # 恢复后的新进程继续写 checkpoint，随后在 5 s 内崩溃
        assert st.save(t, 2, 0, {"x": np.full(4, t, np.int64)}, {"t": t})
        assert st.flush(5.0)
    ck = st.restore_for_restart(2)
    assert ck is not None and ck.t_sim_ns == 2_000
    assert st.poisoned_generations() == 1
    st.close(final=False)


def test_idle_gate_window_deadline_and_compat(shm_dir: Path) -> None:
    """IdleGate（ADR-073 第 1 条）：窗口打开且距截止不少于余量时 wait_slot 立即返回 True；余量不足或窗口关闭时等下一个
    窗口（open 通知），超时返回 False；兼容 Event 的 set/clear/is_set/wait；作 CheckpointStore 的 gate 时写出的内容与
    不带 gate 时逐字节相同。"""
    import threading

    from awr.runtime.checkpoint import IdleGate

    t = [1_000_000_000]
    g = IdleGate(margin_ns=400_000, clock=lambda: t[0])
    assert not g.is_set() and not g.wait_slot(0.0)
    g.open(t[0] + 2_000_000)
    assert g.is_set() and g.wait_slot(0.0)
    t[0] += 1_700_000  # 只剩 0.3 ms < 余量
    assert not g.wait_slot(0.0)
    g.close()
    assert not g.is_set()
    got: list[bool] = []
    th = threading.Thread(target=lambda: got.append(g.wait_slot(5.0)))
    th.start()
    time.sleep(0.05)
    assert not got  # 等下一个窗口
    g.open(t[0] + 5_000_000)
    th.join(2.0)
    assert got == [True]
    g.open(t[0] + 2_000_000, slack_ns=1_500_000)  # 主循环等待余量（ADR-073 第 2 条）：只计入 slack=True 的等待方
    assert not g.wait_slot(0.0, need_ns=3_000_000)
    assert g.wait_slot(0.0, need_ns=3_000_000, slack=True)
    assert g.last_slot_remain_ns == 2_000_000 and g.last_slot_slack_ns == 1_500_000
    assert not g.wait_slot(0.0, need_ns=4_000_000, slack=True)
    g.set()  # Event 兼容：不限时窗口
    assert g.wait(0.0) and g.wait_slot(0.0)
    g.clear()
    assert not g.wait(0.01)
    gate = IdleGate()
    st = CheckpointStore(shm_dir / "ckig", layout_id=LAYOUT_ID, gate=gate, gate_timeout_s=5.0)
    try:
        arrays, m = soa(10), {"big": list(range(1000))}
        assert st.save(1_000_000_000, 1, 0, arrays, m)
        time.sleep(0.2)
        assert st.stats["written"] == 0  # 没有空闲窗口：停在第一个让出点
        stop = threading.Event()

        def windows() -> None:  # 模拟主循环：每 8 ms 一个约 4 ms 的空闲窗口
            while not stop.is_set():
                gate.open(time.monotonic_ns() + 4_000_000)
                time.sleep(0.004)
                gate.close()
                time.sleep(0.004)

        w = threading.Thread(target=windows, daemon=True)
        w.start()
        t0 = time.monotonic()
        while st.stats["written"] == 0 and time.monotonic() - t0 < 5.0:
            time.sleep(0.01)
        stop.set()
        w.join(1.0)
        assert st.stats["written"] == 1
        got_b = next((shm_dir / "ckig").glob("*.bin")).read_bytes()
        assert got_b == serialize(1_000_000_000, 1, 0, LAYOUT_ID, arrays, m)
    finally:
        st.close()
