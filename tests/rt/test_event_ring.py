"""EventRing 存储（M11-FR-065；M11 §6.3.4；D1-AC-29，FX2-R3-gateway）：事件编码一次为 WS JSON、每 256 条封块 zlib 压缩、
按块淘汰且至少保留最近 maxlen 条、压缩字节上限、按 (type, level) 索引过滤时不解压无命中块。纯单元用例，不起服务。"""

from __future__ import annotations

import json

import numpy as np

from awr.api.rt.events import EVENT_BLOCK, EVENT_RING, EventRing, encode_event


def _item(seq: int, kind: str = "track.state", level: int = 0, **data) -> bytes:
    return encode_event({"seq": seq, "t_sim_ns": seq * 4_000_000, "t_wall_ns": str(1_790_000_000_000_000_000 + seq),
                         "type": kind, "level": level, "producer": "sim-core", "uav": f"sim{seq % 200:04d}", "cid": None,
                         "data": {"from": "PENDING", "to": "TRANSIT", "mid": f"m-{seq % 7}", "item": seq % 13, **data}})


def _fill(r: EventRing, n: int, start: int = 1, **kw) -> None:
    for s in range(start, start + n):
        r.append(s, kw.get("kind", "track.state"), kw.get("level", 0), _item(s, kw.get("kind", "track.state")))


def test_append_since_and_block_sealing() -> None:
    r = EventRing()
    _fill(r, 3 * EVENT_BLOCK + 10)
    assert len(r) == 3 * EVENT_BLOCK + 10 and r.first == 1 and r.last == 3 * EVENT_BLOCK + 10
    assert r.stats["sealed"] == 3 and r.zbytes > 0
    got = [json.loads(b)["seq"] for b in r.since(EVENT_BLOCK - 3, limit=20)]
    assert got == list(range(EVENT_BLOCK - 2, EVENT_BLOCK + 18))  # 跨块边界
    assert [json.loads(b)["seq"] for b in r.since(3 * EVENT_BLOCK + 5)] == list(range(3 * EVENT_BLOCK + 6,
                                                                                        3 * EVENT_BLOCK + 11))
    assert r.since(r.last) == []
    # 解压后的字节与编码时逐字节一致
    assert r.since(99, limit=1)[0] == _item(100)


def test_keeps_at_least_maxlen_and_evicts_by_block() -> None:
    r = EventRing(1024, block=64)
    _fill(r, 5000)
    assert 1024 <= len(r) < 1024 + 64 and r.last == 5000
    assert r.first == r.last - len(r) + 1
    assert json.loads(r.since(r.first - 1, limit=1)[0])["seq"] == r.first
    assert r.since(0, limit=1) == r.since(r.first - 1, limit=1)  # since 早于环：从最早一条开始


def test_compressed_size_of_full_ring() -> None:
    r = EventRing()
    _fill(r, EVENT_RING + 3 * EVENT_BLOCK)
    assert len(r) >= EVENT_RING
    raw = sum(len(_item(s)) for s in range(1, 1001)) / 1000 * len(r)
    # 明文约 300 B/条、65,536 条约 19 MB；压缩后应在明文的 1/4 以下（实测 soak 事件约 1/9）
    assert r.nbytes < raw / 4, (r.nbytes, raw)


def test_bytes_cap_evicts_early() -> None:
    rng = np.random.default_rng(1)
    r = EventRing(65536, block=16, max_bytes=64 * 1024)
    for s in range(1, 2001):  # 不可压缩的大事件
        r.append(s, "x.big", 0, encode_event({"seq": s, "data": {"blob": rng.bytes(600).hex()}}))
    assert r.zbytes <= 64 * 1024 + 16 * 1300
    assert len(r) < 2000 and r.stats["evicted_bytes_cap"] > 0
    assert json.loads(r.since(r.first - 1, limit=1)[0])["seq"] == r.first


def test_filter_by_type_and_level_skips_blocks() -> None:
    r = EventRing(65536, block=32)
    s = 0
    for k in range(10):
        for _ in range(32):
            s += 1
            kind = "cmd.rejected" if k == 7 else "track.state"
            r.append(s, kind, 1 if k == 7 else 0, _item(s, kind))
    before = r.stats["decompressed"]
    hits = list(r.iter_since(0, ["cmd."], 1))
    assert len(hits) == 32 and all(t == "cmd.rejected" and lv == 1 for _s, t, lv, _b in hits)
    assert r.stats["decompressed"] - before == 1  # 只解压命中的那一块
    assert list(r.iter_since(0, ["nope."])) == []
    assert list(r.iter_since(0, None, 2)) == []


def test_set_capacity_and_discontinuity() -> None:
    r = EventRing()
    _fill(r, 500)
    r.set_capacity(8)
    assert 8 <= len(r) < 8 + r.block and r.last == 500
    _fill(r, 12, start=501)
    assert r.last == 512 and len(r) < 8 + r.block
    assert [json.loads(b)["seq"] for b in r.since(510)] == [511, 512]
    r.append(600, "x", 0, _item(600))  # 不连续（不应发生）：从该 seq 重新开始
    assert r.first == 600 and r.last == 600 and len(r) == 1


def test_encode_event_non_json_values() -> None:
    b = encode_event({"seq": 1, "data": {"bin": b"ab", "n": np.float32(1.5), "a": np.arange(2), "s": {3}, "zh": "中文"}})
    d = json.loads(b)
    assert d["data"] == {"bin": "ab", "n": 1.5, "a": [0, 1], "s": [3], "zh": "中文"}
    assert "中文".encode() in b  # ensure_ascii=False，与控制面 jdump 一致
    d = json.loads(encode_event({"seq": 2, "data": {"x": float("nan"), "y": [1.0, float("inf")], "z": np.float64("nan"),
                                                   "w": np.float32("inf"), "a": np.array([np.nan, 2.0])}}))
    assert d["data"] == {"x": None, "y": [1.0, None], "z": None, "w": None, "a": [None, 2.0]}  # 非有限数写成 null（合法 JSON）
