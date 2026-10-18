"""参考检测器 `ref_det_v1`（M26-FR-121）的 numpy 推理与 L2 感知适配层：只用 CPU，单线程 BLAS 下逐字节确定。

推理：JPEG 以 DCT 域 1/2 缩放解码为 960×540（PIL `draft`；其它尺寸再双线性缩放）→ 底部补 4 行 0 → `x/255 − 0.5` → 网络（结构见
tools/bench/d3/refdet/train.py，BN 已折叠；4×4 步长 4 分块 stem，扩张块在步长 8，上采样后与 stem 特征拼接出头部）→ 两类中心热图 3×3 极大值抑制（阈值 0.3）
→ 框（输入像素，× 交付分辨率 / 960 回到交付分辨率）。外观取色才全分辨率解码。

适配（`L2Perception.detect(obs)`）：把观测中每帧相机图像的检测换算为与 L1 检测同形的字典，基线策略无须区分观测档：
- `ground_xy_noisy`：框底边中点的视线与地面高度 `ground_hint.dtm_m` 平面的交点（相机位姿取导航解位置、航向与云台读数；三轴增稳）；
- `slant_range_m`：相机到该地面点的距离；`h_px`：框高（交付分辨率）；
- `appearance`：框高 ≥ 16 px 时取框上部、下部中央区域的 RGB 中位数（上衣、下装）；
- `det_id`：`sha256(uav | seq | 序号)` 前 16 位十六进制（同一输入逐字节相同）；`l2 = true`，report 证据改用 `bbox_xywh`。
"""

from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path

import numpy as np

__all__ = ["L2Perception", "RefDet", "decode_full", "decode_small"]

W_IN, H_IN, H_PAD, STRIDE = 960, 540, 544, 4
HM_W, HM_H = W_IN // STRIDE, H_PAD // STRIDE
BLOCKS = (("b2", 2, 1), ("b3", 1, 1), ("b4", 1, 2), ("b5", 1, 4), ("b6", 1, 1))     # 步长 8 段（stem 输出为步长 4 的跳连特征）
CLASSES = ("person", "vehicle")
WEIGHTS = Path(__file__).with_name("ref_det_v1.npz")
IDENT_PX = 16


def decode_small(data: bytes) -> tuple[np.ndarray, tuple[int, int]]:
    """→ (960×540 网络输入 uint8, 交付分辨率 (宽, 高))。训练数据的预处理用同一函数。"""
    from PIL import Image

    im = Image.open(io.BytesIO(bytes(data)))
    size = im.size
    im.draft("RGB", (W_IN, H_IN))
    im = im.convert("RGB")
    if im.size != (W_IN, H_IN):
        im = im.resize((W_IN, H_IN), Image.BILINEAR)
    return np.asarray(im, np.uint8), size


def decode_full(data: bytes) -> np.ndarray:
    from PIL import Image

    return np.asarray(Image.open(io.BytesIO(bytes(data))).convert("RGB"), np.uint8)


def _dw(x: np.ndarray, w: np.ndarray, b: np.ndarray, s: int, d: int) -> np.ndarray:
    """深度卷积 3×3（HWC），padding = d。"""
    H, W, _C = x.shape
    Ho, Wo = (H + 2 * d - 2 * d - 1) // s + 1, (W + 2 * d - 2 * d - 1) // s + 1
    xp = np.pad(x, ((d, d), (d, d), (0, 0)))
    out = np.zeros((Ho, Wo, x.shape[2]), np.float32)
    for ky in range(3):
        for kx in range(3):
            out += xp[ky * d:ky * d + s * (Ho - 1) + 1:s, kx * d:kx * d + s * (Wo - 1) + 1:s] * w[:, 0, ky, kx]
    out += b
    return out


def _pw(x: np.ndarray, w: np.ndarray, b: np.ndarray) -> np.ndarray:
    H, W, C = x.shape
    return (x.reshape(-1, C) @ w.reshape(w.shape[0], C).T + b).reshape(H, W, -1)


class RefDet:
    def __init__(self, path: Path | str | None = None) -> None:
        z = np.load(Path(path) if path else WEIGHTS)
        self.p = {k: np.asarray(z[k], np.float32) for k in z.files}

    def forward(self, img: np.ndarray) -> np.ndarray:
        """img (540, 960, 3) uint8 → (6, 136, 240) 输出（热图为 logit）。"""
        p = self.p
        x = np.zeros((H_PAD, W_IN, 3), np.float32)
        x[:H_IN] = img.astype(np.float32) / 255.0 - 0.5
        Ho, Wo = H_PAD // STRIDE, W_IN // STRIDE
        patches = x.reshape(Ho, STRIDE, Wo, STRIDE, 3).transpose(0, 2, 4, 1, 3).reshape(Ho * Wo, 3 * STRIDE * STRIDE)   # (ci, ky, kx)
        w = p["stem.w"]
        f4 = np.maximum(patches @ w.reshape(w.shape[0], -1).T + p["stem.b"], 0.0).reshape(Ho, Wo, w.shape[0])
        x = f4
        for name, s, d in BLOCKS:
            x = self._block(x, name, s, d)
        up = np.repeat(np.repeat(x, 2, axis=0), 2, axis=1)
        x = np.maximum(_pw(np.concatenate([up, f4], axis=2), p["h1.w"], p["h1.b"]), 0.0)
        return _pw(x, p["h2.w"], p["h2.b"]).transpose(2, 0, 1)

    def _block(self, x: np.ndarray, name: str, s: int, d: int) -> np.ndarray:
        p = self.p
        h = np.maximum(_dw(x, p[f"{name}.dw.w"], p[f"{name}.dw.b"], s, d), 0.0)
        h = np.maximum(_pw(h, p[f"{name}.pw.w"], p[f"{name}.pw.b"]), 0.0)
        return x + h if h.shape == x.shape and s == 1 else h

    def detect(self, img: np.ndarray, thr: float = 0.3, k: int = 64) -> list[tuple[int, float, float, float, float, float]]:
        """→ [(类别号, 分数, x, y, w, h)]，输入像素（960×540）。"""
        out = self.forward(img)
        hm = 1.0 / (1.0 + np.exp(-out[:2]))
        pad = np.pad(hm, ((0, 0), (1, 1), (1, 1)))
        mx = np.max(np.stack([pad[:, dy:dy + HM_H, dx:dx + HM_W] for dy in range(3) for dx in range(3)]), axis=0)
        c, iy, ix = np.nonzero((hm >= mx) & (hm >= thr))
        sc = hm[c, iy, ix]
        res = []
        for j in np.argsort(-sc, kind="stable")[:k]:
            cj, y, x = int(c[j]), int(iy[j]), int(ix[j])
            h, w = math.exp(float(out[2, y, x])), math.exp(float(out[3, y, x]))
            cx, cy = (x + float(out[4, y, x])) * STRIDE, (y + float(out[5, y, x])) * STRIDE
            if cy - h / 2 >= H_IN:
                continue
            res.append((cj, float(sc[j]), cx - w / 2, cy - h / 2, w, h))
        return res


def _rz(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _gimbal(az: float, el: float) -> np.ndarray:
    """Rz(az)·Ry(−el)（FLU 相机系：x 前、y 左、z 上）。"""
    ca, sa, ce, se = math.cos(az), math.sin(az), math.cos(el), math.sin(el)
    return np.array([[ca * ce, -sa, -ca * se], [sa * ce, ca, -sa * se], [se, 0.0, ce]])


def _yaw(q) -> float:
    x, y, z, w = (float(v) for v in q)
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def _median_rgb(a: np.ndarray) -> list[int]:
    if a.size == 0:
        return [0, 0, 0]
    return [int(v) for v in np.median(a.reshape(-1, 3), axis=0).round()]


class L2Perception:
    def __init__(self, config: dict, det: RefDet | None = None) -> None:
        cam = next(s for s in config["sensors"] if s["kind"] == "camera")
        self.cam = cam["sensor_id"]
        self.K = cam["K"]
        self.mount = np.asarray(cam["mount_nominal"]["t_bs_m"], np.float64)
        self.uav = config["uav_id"]
        self.det = det or RefDet()

    def detect(self, obs: dict) -> list[dict]:
        out: list[dict] = []
        nav = obs["nav"]
        p_b = np.asarray(nav["pos_enu_m"], np.float64)
        Rz = _rz(_yaw(nav["q_wb_xyzw"]))
        cam_p = p_b + Rz @ self.mount
        zg = float(obs["ground_hint"]["dtm_m"])
        fx, cx, fy, cy = self.K[0], self.K[2], self.K[4], self.K[5]
        for f in obs["sensors"].get(self.cam, []):
            small, (fw, fh) = decode_small(f["rgb"]["data"])
            full = None
            sx, sy = fw / W_IN, fh / H_IN
            g = f["gimbal_reading"]
            R = Rz @ _gimbal(math.radians(g["yaw_deg"]), math.radians(g["pitch_deg"]))
            for i, (c, score, x, y, w, h) in enumerate(self.det.detect(small)):
                X, Y, Wd, Hd = x * sx, y * sy, w * sx, h * sy
                u, v = X + Wd / 2, Y + Hd
                d_flu = np.array([1.0, -(u - cx) / fx, -(v - cy) / fy])
                d = R @ d_flu
                if d[2] >= -1e-6:
                    continue
                t = (zg - cam_p[2]) / d[2]
                gp = cam_p + t * d
                det = {"det_id": hashlib.sha256(f"{self.uav}|{f['seq']}|{i}".encode()).hexdigest()[:16], "sensor_id": self.cam, "seq": f["seq"],
                       "t_mid_ns": f["t_mid_ns"], "class": CLASSES[c], "bbox_xywh": [round(X, 1), round(Y, 1), round(Wd, 1), round(Hd, 1)],
                       "h_px": round(Hd, 1), "slant_range_m": round(float(np.linalg.norm(gp - cam_p)), 2),
                       "ground_xy_noisy": [round(float(gp[0]), 3), round(float(gp[1]), 3)], "score": round(score, 4), "l2": True}
                if Hd >= IDENT_PX:
                    if full is None:
                        full = decode_full(f["rgb"]["data"])
                    x0, x1 = int(X + 0.2 * Wd), int(X + 0.8 * Wd) + 1
                    top = full[int(Y + 0.15 * Hd):int(Y + 0.45 * Hd) + 1, x0:x1]
                    bot = full[int(Y + 0.6 * Hd):int(Y + 0.9 * Hd) + 1, x0:x1]
                    det["appearance"] = {"top_rgb": _median_rgb(top), "bottom_rgb": _median_rgb(bot)}
                out.append(det)
        return out
