"""M03-AC-013（结构部分）、AC-015、AC-016、AC-027（分页）：容器结构、子树包围盒、打散前缀均匀性、首屏前缀、字节确定性。"""

from __future__ import annotations

import json

import numpy as np
import pytest
from m03_common import load
from scipy.stats import chisquare

from awr.world.pointcloud import iter_nodes, read_container, tile
from awr.world.pointcloud.morton import name_of, name_to_key
from awr.world.pointcloud.reader import PROXY, REC, parse_hierarchy
from awr.world.pointcloud.writer import HIER_DTYPE, _write_hierarchy_paged


@pytest.fixture(scope="module")
def container(tiny_built):
    return read_container(tiny_built["dir"] / "visual/pointcloud")


def test_hierarchy_layout(container):
    c = container
    assert c.hierarchy_bytes % REC == 0
    assert c.metadata["hierarchy"]["firstChunkSize"] == c.hierarchy_bytes      # 单 chunk
    assert all(r.type != PROXY for r in c.records)
    for n in c.nodes:
        assert (n.type == 0) == (n.mask != 0)
        if n.n == 0:
            assert n.size == 0
    assert c.octree_bytes == 12 * c.metadata["points"]
    order = sorted(c.nodes, key=lambda n: (n.level, n.name))
    pos = 0
    for n in order:
        if n.n:
            assert n.off == pos and n.off % 4 == 0 and n.size == 12 * n.n
            pos += n.size
    assert pos == c.octree_bytes
    assert [n.name for n in c.records] == [n.name for n in order]       # 文件记录顺序即 BFS


def test_ext_mirror_and_subtree_boxes(container):
    c = container
    assert c.ext is not None and len(c.ext) == len(c.records)
    by = {n.name: n for n in c.nodes}
    boxes = {}
    for v in iter_nodes(c, decode=True):
        e = c.ext[v.rec].astype(np.float64)
        bmin = v.node_min + e[:3] / 65535 * v.node_size
        bmax = v.node_min + e[3:] / 65535 * v.node_size
        boxes[v.name] = (bmin, bmax)
        if v.num_points:
            q = v.pos_u16[:, :3]
            assert np.all(q >= c.ext[v.rec][:3]) and np.all(q <= c.ext[v.rec][3:])
    for name, (bmin, bmax) in boxes.items():                     # 子树盒包含全部子孙
        if len(name) > 1:
            pmin, pmax = boxes[name[:-1]]
            assert np.all(pmin <= bmin + 1e-9) and np.all(pmax >= bmax - 1e-9)
    root = c.ext[by["r"].rec].astype(np.float64)
    tb = c.metadata["anet"]["tightBounds"]
    q = c.cube_size / 65535 * 1.01
    assert np.all(np.abs(c.cube_min + root[:3] / 65535 * c.cube_size - tb["min"]) <= q)


def test_first_screen_prefix_decodes_exact_points(container):
    c = container
    a = c.metadata["anet"]
    data = (c.root_dir / "octree.bin").read_bytes()
    for L, end in enumerate(a["levelsByteEnd"]):
        pts = sum(n.n for n in c.nodes if n.level <= L)
        assert pts == a["levelsPoints"][L]
        assert sum(n.size for n in c.nodes if n.level <= L) == end
        assert len(data[:end]) == 12 * a["levelsPoints"][L]


def test_prefix_uniformity(container):
    """AC-016：点数 ≥ 2000 的节点，1000 个（节点, 前缀 ∈ [10%, 100%)）组合的八分体卡方检验 p > 0.01 的比例 ≥ 98%。"""
    c = container
    rng = np.random.default_rng(16)
    nodes = [v for v in iter_nodes(c, raw=True) if v.num_points >= 2000]
    assert nodes
    ok = total = 0
    for _ in range(1000):
        v = nodes[int(rng.integers(len(nodes)))]
        q = v.pos_u16[:, :3].astype(np.int64)
        oct_ = ((q[:, 0] >= 32768).astype(int) << 2) | ((q[:, 1] >= 32768).astype(int) << 1) | (q[:, 2] >= 32768).astype(int)
        k = int(rng.uniform(0.1, 1.0) * len(q))
        full = np.bincount(oct_, minlength=8).astype(float)
        pre = np.bincount(oct_[:k], minlength=8).astype(float)
        exp = full / full.sum() * k
        keep = exp >= 5                                               # 期望数 < 5 的八分体合并
        merged = (~keep) & (exp > 0)
        obs_m = np.r_[pre[keep], pre[merged].sum()] if merged.any() else pre[keep]
        exp_m = np.r_[exp[keep], exp[merged].sum()] if merged.any() else exp[keep]
        if merged.any() and exp_m[-1] == 0:
            obs_m, exp_m = obs_m[:-1], exp_m[:-1]
        exp_m = exp_m * obs_m.sum() / exp_m.sum()
        if len(obs_m) < 2:
            ok += 1
            total += 1
            continue
        p = chisquare(obs_m, exp_m).pvalue
        ok += int(p > 0.01)
        total += 1
    assert ok / total >= 0.98


def test_tile_bytes_deterministic(tmp_path):
    rng = np.random.default_rng(5)
    P = rng.random((80_000, 3)) * [600, 400, 100]
    N = rng.normal(size=(80_000, 3))
    C = rng.integers(0, 8, 80_000).astype(np.uint8)
    outs = []
    for k in range(2):
        d = tmp_path / f"t{k}"
        tile(P, N, C, d, G=16, leaf=3000, z_range=(0.0, 100.0), hag_range=(0.0, 100.0), nn_median_m=1.0, name="x")
        outs.append({f.name: f.read_bytes() for f in d.iterdir()})
    assert outs[0] == outs[1]
    md = json.loads(outs[0]["metadata.json"])
    assert "seconds" not in md["anet"]["generator"]


def test_forest_roots_do_not_overlap(tmp_path):
    rng = np.random.default_rng(6)
    P = rng.random((30_000, 3)) * [4000, 600, 100]                   # 长宽比 6.7 → 6 根
    roots = tile(P, None, np.zeros(len(P), np.uint8), tmp_path, G=16, leaf=2000, z_range=(0.0, 100.0), hag_range=None,
                 nn_median_m=1.0)
    assert len(roots) == 6
    for i in range(6):
        for j in range(i + 1, 6):
            a0, s0 = roots[i].cube_min, roots[i].cube_size
            a1, s1 = roots[j].cube_min, roots[j].cube_size
            assert not np.all(np.minimum(a0 + s0, a1 + s1) - np.maximum(a0, a1) > 1e-3)
    assert sum(r.points for r in roots) == len(P)
    md = load(tmp_path / "r-3" / "metadata.json")
    assert md["anet"]["root"] == {"forestIndex": 3, "forestSize": 6} and md["anet"]["pos"]["w"] == "zero"


def test_hierarchy_paging_roundtrip():
    """AC-027（分页）：> 40,000 节点的合成树按 step = 4 分页写出后，按 Potree 语义解析往返一致。"""
    keys = [(0, 0)]
    frontier = [(0, 0)]
    for L in range(1, 6):
        frontier = [(L, (n << 3) | c) for _, n in frontier for c in range(8)]
        keys += frontier
    keys += [(6, (n << 3) | c) for _, n in frontier[:1800] for c in (0, 5)]
    keys.sort()
    assert len(keys) > 40_000
    child: dict = {}
    for L, n in keys:
        if L:
            child[(L - 1, n >> 3)] = child.get((L - 1, n >> 3), 0) | (1 << (n & 7))
    recs = np.zeros(len(keys), HIER_DTYPE)
    ext = np.zeros((len(keys), 6), "<u2")
    off = 0
    for i, k in enumerate(keys):
        m = child.get(k, 0)
        recs[i] = (0 if m else 1, m, i % 7 + 1, off, 12 * (i % 7 + 1))
        ext[i] = [i % 65536, 0, 0, 65535, 65535, 65535]
        off += 12 * (i % 7 + 1)
    hier, ext_bytes, first = _write_hierarchy_paged(keys, recs, ext, child)
    records, chunks = parse_hierarchy(hier, first)
    assert sum(s for _, s in chunks) == len(hier) and len(ext_bytes) == len(hier) // 22 * 12
    real = [r for r in records if r.type != PROXY]
    assert len(real) == len(keys) and len({r.name for r in real}) == len(keys)
    idx = {name_of(n, L): i for i, (L, n) in enumerate(keys)}
    e = np.frombuffer(ext_bytes, "<u2").reshape(-1, 6)
    for r in real:
        i = idx[r.name]
        assert (r.n, r.off, r.size) == (int(recs[i]["n"]), int(recs[i]["o"]), int(recs[i]["s"]))
        assert int(e[r.rec][0]) == i % 65536
        assert name_to_key(r.name)[0] == keys[i][0]
    assert any(r.type == PROXY for r in records)
