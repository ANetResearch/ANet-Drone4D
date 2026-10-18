"""参考检测器 `ref_det_v1` 的 numpy 推理与 L2 感知适配层（M26-FR-121）：卷积算子与朴素实现一致、推理逐字节确定、热图解码、
视线与地面求交、外观取色与证据形式。网络权重以随机值代替（训练好的权重另在 dmax 上与 torch 比对）。"""

from __future__ import annotations

import io
import math
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PIL")

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/sdk/drone4d/src"))

from drone4d.perception import ref_det as R  # noqa: E402


def _weights(path: Path, seed: int = 0) -> Path:
    rng = np.random.default_rng(seed)
    w = {"stem.w": rng.normal(0, 0.3, (32, 3, 4, 4)), "stem.b": rng.normal(0, 0.1, 32)}
    ch = {"b2": (32, 48), "b3": (48, 48), "b4": (48, 48), "b5": (48, 48), "b6": (48, 48)}
    for n, (ci, co) in ch.items():
        w[f"{n}.dw.w"], w[f"{n}.dw.b"] = rng.normal(0, 0.3, (ci, 1, 3, 3)), rng.normal(0, 0.1, ci)
        w[f"{n}.pw.w"], w[f"{n}.pw.b"] = rng.normal(0, 0.2, (co, ci, 1, 1)), rng.normal(0, 0.1, co)
    w["h1.w"], w["h1.b"] = rng.normal(0, 0.2, (32, 80, 1, 1)), rng.normal(0, 0.1, 32)
    w["h2.w"], w["h2.b"] = rng.normal(0, 0.2, (6, 32, 1, 1)), rng.normal(0, 0.1, 6)
    np.savez(path, **{k: v.astype(np.float32) for k, v in w.items()})
    return path


def _naive_dw(x, w, b, s, d):
    H, W, C = x.shape
    Ho, Wo = (H - 1) // s + 1, (W - 1) // s + 1
    xp = np.pad(x, ((d, d), (d, d), (0, 0)))
    out = np.zeros((Ho, Wo, C), np.float64)
    for i in range(Ho):
        for j in range(Wo):
            for ky in range(3):
                for kx in range(3):
                    out[i, j] += xp[i * s + ky * d, j * s + kx * d] * w[:, 0, ky, kx]
    return out + b


@pytest.mark.parametrize(("s", "d"), [(1, 1), (2, 1), (1, 4), (1, 8)])
def test_depthwise_matches_naive(s: int, d: int) -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=(17, 23, 5)).astype(np.float32)
    w, b = rng.normal(size=(5, 1, 3, 3)).astype(np.float32), rng.normal(size=5).astype(np.float32)
    assert np.allclose(R._dw(x, w, b, s, d), _naive_dw(x, w, b, s, d), atol=1e-4)


def test_forward_shape_determinism_and_stem(tmp_path: Path) -> None:
    det = R.RefDet(_weights(tmp_path / "w.npz"))
    img = np.random.default_rng(2).integers(0, 256, (R.H_IN, R.W_IN, 3), dtype=np.uint8)
    a, b = det.forward(img), det.forward(img)
    assert a.shape == (6, R.HM_H, R.HM_W) and a.dtype == np.float32
    assert a.tobytes() == b.tobytes()
    x = np.zeros((R.H_PAD, R.W_IN, 3), np.float64)                                  # 一个 stem 输出位置的朴素卷积（4×4/4）
    x[:R.H_IN] = img / 255.0 - 0.5
    i, j = 37, 101
    patch = x[4 * i:4 * i + 4, 4 * j:4 * j + 4]                                     # (ky, kx, ci)
    ref = np.maximum(np.einsum("yxc,ocyx->o", patch, det.p["stem.w"]) + det.p["stem.b"], 0.0)
    Ho, Wo = R.H_PAD // 4, R.W_IN // 4
    xs = x.astype(np.float32)
    got = np.maximum(xs.reshape(Ho, 4, Wo, 4, 3).transpose(0, 2, 4, 1, 3).reshape(Ho * Wo, 48) @ det.p["stem.w"].reshape(32, 48).T
                     + det.p["stem.b"], 0.0)
    assert np.allclose(got.reshape(Ho, Wo, 32)[i, j], ref, atol=1e-4)


def _jpeg(rgb: np.ndarray) -> bytes:
    from PIL import Image

    b = io.BytesIO()
    Image.fromarray(rgb).save(b, format="JPEG", quality=95, subsampling=0)
    return b.getvalue()


class _OneBox:
    """固定检测：960×540 输入中心正下方的一个人员框。"""

    def __init__(self, box):
        self.box = box

    def detect(self, img):
        return [(0, 0.9, *self.box)]


def test_perception_ground_intersection_and_appearance() -> None:
    W, H, f = 1920, 1080, 1662.77
    rgb = np.full((H, W, 3), 90, np.uint8)
    rgb[440:500, 950:970] = (200, 20, 20)                       # 上衣
    rgb[520:560, 950:970] = (20, 20, 200)                       # 下装
    cfg = {"uav_id": "uav-01", "sensors": [{"sensor_id": "eo", "kind": "camera", "K": [f, 0, W / 2, 0, f, H / 2, 0, 0, 1],
                                            "mount_nominal": {"t_bs_m": [0.0, 0.0, 0.0], "q_bs_xyzw": [0, 0, 0, 1]}}]}
    box = (950 / 2, 420 / 2, 20 / 2, 160 / 2)                    # 输入像素；底边中点在交付分辨率 (960, 580)
    per = R.L2Perception(cfg, det=_OneBox(box))
    obs = {"nav": {"pos_enu_m": [10.0, 20.0, 60.0], "q_wb_xyzw": [0.0, 0.0, 0.0, 1.0]}, "ground_hint": {"dtm_m": 0.0},
           "sensors": {"eo": [{"seq": 7, "t_mid_ns": 700_000_000, "gimbal_reading": {"roll_deg": 0.0, "pitch_deg": -90.0, "yaw_deg": 0.0},
                               "rgb": {"codec": "jpeg", "data": _jpeg(rgb)}}]}}
    d1, d2 = per.detect(obs), per.detect(obs)
    assert d1 == d2 and len(d1) == 1
    d = d1[0]
    assert d["class"] == "person" and d["l2"] and d["bbox_xywh"] == [950.0, 420.0, 20.0, 160.0] and d["h_px"] == 160.0
    # 正下视：图像向下（v 增大）对应机头后方（−x），向右（u 增大）对应机体右侧（−y）
    gx, gy = d["ground_xy_noisy"]
    assert gx == pytest.approx(10.0 - 60.0 * (580 - 540) / f, abs=1e-3) and gy == pytest.approx(20.0, abs=1e-3)
    assert d["slant_range_m"] == pytest.approx(math.hypot(60.0, 60.0 * 40 / f), abs=0.01)
    top, bot = d["appearance"]["top_rgb"], d["appearance"]["bottom_rgb"]
    assert top[0] > 150 and top[2] < 80 and bot[2] > 150 and bot[0] < 80
    assert len(d["det_id"]) == 16 and int(d["det_id"], 16) >= 0


def test_evidence_uses_bbox_at_l2() -> None:
    from drone4d.baselines.common import Tracks

    tr = Tracks("uav-01")
    det = {"det_id": "0" * 16, "sensor_id": "eo", "seq": 3, "t_mid_ns": 300_000_000, "class": "person", "bbox_xywh": [1, 2, 3, 4],
           "ground_xy_noisy": [0.0, 0.0], "l2": True}
    tr.update([det], 300_000_000)
    assert tr.items[0].ev == [{"uav": "uav-01", "sensor": "eo", "frame_seq": 3, "bbox_xywh": [1, 2, 3, 4]}]
    tr.update([{**det, "seq": 4, "t_mid_ns": 400_000_000, "l2": False}], 400_000_000)
    assert tr.items[0].ev[-1] == {"uav": "uav-01", "sensor": "eo", "frame_seq": 4, "det_id": "0" * 16}
