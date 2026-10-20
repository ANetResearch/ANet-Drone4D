"""演员代理几何与可见度归约（M25 §6.4.4、§6.6.1）：尺寸、法线、颜色与整数归约（本机可运行，不需要 GPU）。"""

from __future__ import annotations

import numpy as np
import pytest

from awr.sensorsvc.camera.actors import SEM_CODE, proxy_mesh, sun_dir, visibility_objects


@pytest.mark.parametrize(("cls", "size"), [("person", (500, 300, 1750)), ("vehicle", (4500, 1800, 1500)), ("dog", (900, 300, 600)),
                                          ("machine", (1000, 700, 800)), ("object", (600, 400, 400))])
def test_proxy_extent_normals_and_colour(cls: str, size: tuple[int, int, int]) -> None:
    P, N, C = proxy_mesh({"class": cls, "size_mm": list(size), "appearance": {"top_rgb": [42, 93, 176], "body_rgb": [210, 210, 214]}})
    assert P.dtype == np.float32 and len(P) % 3 == 0 and P.shape == N.shape == C.shape
    lo, hi = P.min(0), P.max(0)
    ln, w, h = (s / 1000.0 for s in size)
    assert lo[2] == pytest.approx(0.0, abs=1e-6) and hi[2] == pytest.approx(h, rel=1e-5)
    assert hi[0] - lo[0] <= ln + 1e-5 and hi[1] - lo[1] <= w + 1e-5 and hi[0] - lo[0] >= 0.5 * ln
    assert np.allclose(np.linalg.norm(N, axis=1), 1.0, atol=1e-5) and (C >= 0).all() and (C <= 1).all()
    tri = P.reshape(-1, 3, 3)                                                   # 绕序与外法线一致（逆时针为正面）
    cr = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    assert (np.einsum("ij,ij->i", cr, N[::3]) > 0).all()
    if cls == "person":
        top = C[P[:, 2] > 0.6 * h][:, 2]
        assert top.max() > 0.08                                                  # 上装蓝色（线性）


def test_visibility_reduction_counts_bbox_and_occluders() -> None:
    inst = np.zeros((6, 8), np.uint16)
    unocc = np.zeros((6, 8), np.uint16)
    sem = np.full((6, 8), 1, np.uint8)
    unocc[1:5, 2:4] = 1                                                          # 演员 1：8 像素，下半被静态（语义 6）挡住
    inst[1:3, 2:4] = 1
    sem[1:3, 2:4] = 32
    sem[3:5, 2:4] = 6
    unocc[0:6, 7] = 2                                                            # 演员 2：触边，被演员 1 以外的车（33）挡 1 像素
    inst[0:6, 7] = 2
    inst[2, 7] = 3
    sem[2, 7] = 33
    inval = np.zeros((6, 8), np.uint8)
    inval[5, 7] = 1                                                              # 无效像素不计
    acts = [{"id": "a", "class": "person"}, {"id": "b", "class": "dog"}, {"id": "c", "class": "vehicle"}]
    o1, o2 = visibility_objects(inst, unocc, sem, inval, acts)
    assert (o1["px_unoccluded"], o1["px_visible"], o1["h_px_unocc"], o1["h_px_visible"]) == (8, 4, 4, 2)
    assert o1["bbox_unocc_xywh"] == [2, 1, 2, 4] and o1["bbox_xywh"] == [2, 1, 2, 2] and o1["occluder_sem_px"] == {"6": 4}
    assert o1["visible_ratio"] == 0.5 and o1["flags"] == [] and o1["class"] == SEM_CODE["person"]
    assert o2["px_unoccluded"] == 5 and o2["px_visible"] == 4 and o2["occluder_sem_px"] == {"33": 1} and o2["flags"] == ["truncated"]
    assert sum(o1["occluder_sem_px"].values()) == o1["px_unoccluded"] - o1["px_visible"]


def test_sun_direction() -> None:
    s = sun_dir((90.0, 30.0))
    assert s[0] == pytest.approx(np.cos(np.radians(30))) and s[2] == pytest.approx(0.5) and abs(s[1]) < 1e-12


def _visibility_reference(inst, inst_unocc, sem, invalid, actors):
    """逐实例扫描像素的直接实现（M25 §6.6.1 原式），用于核对向量化的 `visibility_objects`。"""
    H, W = inst.shape
    fg = (inst_unocc > 0) | (inst > 0)
    if invalid is not None:
        fg &= invalid == 0
    pix = np.flatnonzero(fg)
    iu, iv, sm = inst_unocc.ravel()[pix].astype(np.int64), inst.ravel()[pix].astype(np.int64), sem.ravel()[pix].astype(np.int64)
    yy, xx = np.divmod(pix, W)
    out = []
    for i in np.unique(iu[iu > 0]):
        mu, mv = iu == i, iv == i
        bu = (int(xx[mu].min()), int(yy[mu].min()), int(xx[mu].max()), int(yy[mu].max()))
        if mv.any():
            bv = (int(xx[mv].min()), int(yy[mv].min()), int(xx[mv].max()), int(yy[mv].max()))
            bbox_v, hv = [bv[0], bv[1], bv[2] - bv[0] + 1, bv[3] - bv[1] + 1], bv[3] - bv[1] + 1
        else:
            bbox_v, hv = [0, 0, 0, 0], 0
        hist = np.bincount(sm[mu & (iv != i)], minlength=256)
        a = actors[int(i) - 1]
        out.append({"inst": int(i), "actor": a["id"], "class": SEM_CODE.get(a["class"], 13), "px_visible": int(mv.sum()), "px_unoccluded": int(mu.sum()),
                    "h_px_unocc": bu[3] - bu[1] + 1, "h_px_visible": hv, "visible_ratio": round(float(mv.sum()) / float(mu.sum()), 6),
                    "bbox_xywh": bbox_v, "bbox_unocc_xywh": [bu[0], bu[1], bu[2] - bu[0] + 1, bu[3] - bu[1] + 1],
                    "occluder_sem_px": {str(k): int(v) for k, v in enumerate(hist) if v},
                    "flags": ["truncated"] if bu[0] == 0 or bu[1] == 0 or bu[2] == W - 1 or bu[3] == H - 1 else []})
    return out


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_visibility_objects_vectorized_matches_reference(seed: int) -> None:
    """向量化可见度归约与逐实例扫描的原式逐字段相同：随机矩形演员（含互相遮挡、被静态遮挡、只在可见通道出现、贴边截断与无效像素）。"""
    rng = np.random.default_rng(seed)
    H, W = 90, 160
    inst = np.zeros((H, W), np.uint16)
    unocc = np.zeros((H, W), np.uint16)
    sem = rng.integers(0, 40, (H, W)).astype(np.uint8)
    n = 12
    actors = [{"id": f"a{k}", "class": ("person", "vehicle", "dog", "machine")[k % 4]} for k in range(n)]
    for k in range(n):
        y0, x0 = int(rng.integers(-1, H - 5)), int(rng.integers(-1, W - 5))
        h, w = int(rng.integers(2, 30)), int(rng.integers(2, 40))
        ys, xs = slice(max(y0, 0), min(y0 + h, H)), slice(max(x0, 0), min(x0 + w, W))
        unocc[ys, xs] = k + 1
        vis = rng.random((ys.stop - ys.start, xs.stop - xs.start)) < 0.7
        inst[ys, xs] = np.where(vis, k + 1, inst[ys, xs])
    inst[rng.random((H, W)) < 0.01] = n                                       # 只在可见通道出现的像素
    invalid = (rng.random((H, W)) < 0.05).astype(np.uint8)
    for inv in (None, invalid):
        got = visibility_objects(inst, unocc, sem, inv, actors)
        assert got == _visibility_reference(inst, unocc, sem, inv, actors)
    assert visibility_objects(np.zeros((4, 5), np.uint16), np.zeros((4, 5), np.uint16), np.zeros((4, 5), np.uint8), None, actors) == []
