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
