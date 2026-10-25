"""三级容差：跨 GPU 型号、驱动或镜像版本的传感器帧比较（docs/05 §4.5 复现等级表"三级：容差"；M25-FR-172；D3-AC-17）。

同镜像、同 CPU ISA、同型号 GPU 的两次运行要求逐字节一致（一级 b、二级 a/b，由摘要对拍）；跨环境只要求以下容差：

| 量 | 口径 | 阈值 |
|---|---|---|
| f32 原始深度 | 两帧均有效（有限且 > 0）的像素上 abs(d_a − d_b) 的 p90 | ≤ 1 mm |
| 像素偏移 | 灰度图（无 RGB 时为逆深度）Hann 窗相位相关，上采样 DFT 细化（1/100 px）的整体平移模长 | ≤ 0.1 px |
| 解码 RGB | PSNR（8 位，三通道合并） | ≥ 40 dB |
| 实例 | 每个演员实例的 IoU（任一帧出现的非 0 实例） | ≥ 0.98 |
| LiDAR 逐射线距离差 | 两帧均有回波的射线（以 `offset_time_ns` 为射线键）上 abs(r_a − r_b) 的 p99 / 最大 | ≤ 1 mm / ≤ 1 cm |
| LiDAR 命中判定 | 只在一帧有回波的射线数 / 每帧射线数 | ≤ 1e-4 |

相位相关与 r3 §4.3 的 `cv2.phaseCorrelate` 同口径（实现只用 numpy；噪声底约 0.06 px 时仍可判定 0.1 px）。

比较脚本（两次运行各自导出的帧目录，`FrameDump` 写出 `<uav>/<sensor>/<seq:06d>.npz`）：

    python -m awr.agentsdk.env.tolerance <dump_a> <dump_b> [--out report.json]

逐帧报告与汇总写到标准输出（或 `--out`），任一帧越限退出码为 1，帧集合不一致退出码为 2。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

__all__ = ["TIER3", "FrameDump", "Tier3", "camera_arrays", "compare_camera", "compare_dumps", "compare_lidar", "depth_stats",
           "image_shift_px", "instance_iou", "lidar_arrays", "main", "psnr_db"]


@dataclass(frozen=True)
class Tier3:
    depth_p90_m: float = 1e-3
    shift_px: float = 0.1
    rgb_psnr_db: float = 40.0
    instance_iou: float = 0.98
    lidar_p99_m: float = 1e-3
    lidar_max_m: float = 1e-2
    hit_mismatch: float = 1e-4


TIER3 = Tier3()


# ---------------------------------------------------------------------- 单项度量
def depth_stats(a: np.ndarray, b: np.ndarray) -> dict:
    """f32 原始深度（米，0 或非有限为无效）：两帧均有效像素上的差值分位数，另报有效性不一致的像素比例。"""
    a = np.asarray(a, np.float32)
    b = np.asarray(b, np.float32)
    if a.shape != b.shape:
        raise ValueError(f"depth shapes differ: {a.shape} vs {b.shape}")
    va, vb = np.isfinite(a) & (a > 0), np.isfinite(b) & (b > 0)
    both = va & vb
    n = int(both.sum())
    d = np.abs(a[both].astype(np.float64) - b[both].astype(np.float64))
    q = (lambda p: float(np.percentile(d, p))) if n else (lambda p: 0.0)
    return {"n": n, "p50_m": q(50), "p90_m": q(90), "max_m": float(d.max()) if n else 0.0,
            "valid_mismatch": float((va ^ vb).sum()) / max(1, a.size)}


def _gray(img: np.ndarray) -> np.ndarray:
    x = np.asarray(img, np.float64)
    if x.ndim == 3:
        x = x[..., :3] @ np.array([0.299, 0.587, 0.114])
    return x


def _upsampled_dft(data: np.ndarray, size: int, factor: int, offsets: np.ndarray) -> np.ndarray:
    """矩阵乘法 DFT：只在峰值附近 size × size 的区域按 1/factor 像素求逆变换（Guizar-Sicairos 2008）。"""
    for n, off in zip(data.shape[::-1], offsets[::-1], strict=True):
        kern = np.exp(-2j * np.pi * (np.arange(size) - off)[:, None] * np.fft.fftfreq(n, factor)[None, :])
        data = np.tensordot(kern, data, axes=(1, -1))
    return data


def image_shift_px(a: np.ndarray, b: np.ndarray, *, upsample: int = 100) -> tuple[float, float]:
    """b 相对 a 的整体平移 (dy, dx)（像素；b(y, x) ≈ a(y − dy, x − dx)）。灰度化、去均值、Hann 窗后做相位相关，峰值经上采样
    DFT 细化到 1/upsample 像素。"""
    fa, fb = _gray(a), _gray(b)
    if fa.shape != fb.shape:
        raise ValueError(f"image shapes differ: {fa.shape} vs {fb.shape}")
    w = np.outer(np.hanning(fa.shape[0]), np.hanning(fa.shape[1]))
    A = np.fft.fft2((fa - fa.mean()) * w)
    B = np.fft.fft2((fb - fb.mean()) * w)
    P = A * B.conj()
    P /= np.maximum(np.abs(P), 1e-3 * float(np.abs(P).max()))     # 正则化相位归一：近零幅值频点不放大为噪声
    cc = np.fft.ifft2(P)
    peak = np.array(np.unravel_index(int(np.argmax(np.abs(cc))), cc.shape), np.float64)
    shape = np.array(cc.shape, np.float64)
    mid = np.fix(shape / 2)
    peak[peak > mid] -= shape[peak > mid]
    if upsample > 1:
        peak = np.round(peak * upsample) / upsample
        size = math.ceil(upsample * 1.5)
        c = np.fix(size / 2.0)
        up = _upsampled_dft(P.conj(), size, upsample, c - peak * upsample).conj()
        m = np.array(np.unravel_index(int(np.argmax(np.abs(up))), up.shape), np.float64) - c
        peak = peak + m / upsample
    return float(-peak[0]), float(-peak[1])


def psnr_db(a: np.ndarray, b: np.ndarray) -> float:
    """8 位图像的 PSNR（dB）；逐字节相同为 +inf。"""
    a = np.asarray(a)
    b = np.asarray(b)
    if a.shape != b.shape:
        raise ValueError(f"rgb shapes differ: {a.shape} vs {b.shape}")
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return math.inf if mse == 0 else 10.0 * math.log10(255.0 ** 2 / mse)


def instance_iou(a: np.ndarray, b: np.ndarray) -> dict[int, float]:
    """实例图（u16，0 为背景）：任一帧出现的每个实例的 IoU。"""
    a = np.asarray(a)
    b = np.asarray(b)
    if a.shape != b.shape:
        raise ValueError(f"instance shapes differ: {a.shape} vs {b.shape}")
    out = {}
    for i in np.union1d(np.unique(a), np.unique(b)):
        if i == 0:
            continue
        ma, mb = a == i, b == i
        out[int(i)] = float((ma & mb).sum()) / float((ma | mb).sum())
    return out


def lidar_arrays(obs: Mapping) -> tuple[np.ndarray, np.ndarray]:
    """LidarObs（`xyz_m` f32 字节、`offset_time_ns` u4 字节）或已解码数组 → (射线键 offset_time_ns, 距离 m)。"""
    xyz = obs["xyz_m"]
    t = obs["offset_time_ns"]
    xyz = np.frombuffer(xyz, "<f4").reshape(-1, 3) if isinstance(xyz, bytes | bytearray | memoryview) else np.asarray(xyz, np.float32)
    t = np.frombuffer(t, "<u4") if isinstance(t, bytes | bytearray | memoryview) else np.asarray(t)
    return t.astype(np.int64), np.linalg.norm(xyz.astype(np.float64), axis=1)


def compare_lidar(a: Mapping, b: Mapping, n_rays: int, tol: Tier3 = TIER3) -> dict:
    """同一帧（同 uav、sensor、seq）的两份 LiDAR 观测：逐射线距离差与命中判定不一致率（以 offset_time_ns 为射线键）。"""
    return _lidar_diff(*lidar_arrays(a), *lidar_arrays(b), n_rays, tol)


def _lidar_diff(ka: np.ndarray, ra: np.ndarray, kb: np.ndarray, rb: np.ndarray, n_rays: int, tol: Tier3) -> dict:
    if len(np.unique(ka)) != len(ka) or len(np.unique(kb)) != len(kb):
        raise ValueError("offset_time_ns is not a unique ray key in this frame")
    common, ia, ib = np.intersect1d(ka, kb, assume_unique=True, return_indices=True)
    d = np.abs(np.asarray(ra, np.float64)[ia] - np.asarray(rb, np.float64)[ib])
    only = len(ka) + len(kb) - 2 * len(common)
    rep = {"n_a": len(ka), "n_b": len(kb), "n_common": len(common), "hit_mismatch": only / max(1, int(n_rays)),
           "p99_m": float(np.percentile(d, 99)) if len(d) else 0.0, "max_m": float(d.max()) if len(d) else 0.0}
    rep["fail"] = [k for k, bad in (("lidar_p99", rep["p99_m"] > tol.lidar_p99_m), ("lidar_max", rep["max_m"] > tol.lidar_max_m),
                                    ("hit_mismatch", rep["hit_mismatch"] > tol.hit_mismatch)) if bad]
    return rep


def compare_camera(a: Mapping, b: Mapping, tol: Tier3 = TIER3) -> dict:
    """同一帧的两份相机输出（键 `rgb` u8 H×W×3、`depth` f32 H×W、`instance` u16 H×W，均可缺省）。"""
    rep: dict = {}
    fail: list[str] = []
    if "depth" in a and "depth" in b:
        rep["depth"] = depth_stats(a["depth"], b["depth"])
        if rep["depth"]["p90_m"] > tol.depth_p90_m:
            fail.append("depth_p90")
    if "rgb" in a and "rgb" in b:
        rep["psnr_db"] = psnr_db(a["rgb"], b["rgb"])
        if rep["psnr_db"] < tol.rgb_psnr_db:
            fail.append("rgb_psnr")
        src = (a["rgb"], b["rgb"])
    elif "depth" in a and "depth" in b:
        inv = [np.where(np.isfinite(x) & (x > 0), 1.0 / np.maximum(np.asarray(x, np.float64), 1e-6), 0.0) for x in (a["depth"], b["depth"])]
        src = (inv[0], inv[1])
    else:
        src = None
    if src is not None:
        dy, dx = image_shift_px(*src)
        rep["shift_px"] = math.hypot(dy, dx)
        rep["shift_yx_px"] = [dy, dx]
        if rep["shift_px"] > tol.shift_px:
            fail.append("shift")
    if "instance" in a and "instance" in b:
        iou = instance_iou(a["instance"], b["instance"])
        rep["instance_iou_min"] = min(iou.values()) if iou else 1.0
        rep["instance_iou"] = {str(k): v for k, v in sorted(iou.items())}
        if rep["instance_iou_min"] < tol.instance_iou:
            fail.append("instance_iou")
    rep["fail"] = fail
    return rep


def camera_arrays(obs: Mapping) -> dict:
    """Agent 收到的 CameraObs → 比较用数组：解码 JPEG（`rgb.data`）与 f32le 深度（L2-depth，`depth.data`）。"""
    out = {}
    h, w = int(obs["height"]), int(obs["width"])
    if "rgb" in obs:
        import io

        from PIL import Image

        out["rgb"] = np.asarray(Image.open(io.BytesIO(obs["rgb"]["data"])).convert("RGB"))
    if "depth" in obs:
        out["depth"] = np.frombuffer(obs["depth"]["data"], "<f4").reshape(h, w)
    return out


# ---------------------------------------------------------------------- 帧导出与目录比较
class FrameDump:
    """跨环境比较用的帧导出：`<root>/<uav>/<sensor>/<seq:06d>.npz`（相机 rgb/depth/instance，LiDAR key/range_m/n_rays）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, uav: str, sensor: str, seq: int) -> Path:
        p = self.root / uav / sensor / f"{int(seq):06d}.npz"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def camera(self, uav: str, sensor: str, seq: int, **arrays: np.ndarray) -> None:
        bad = set(arrays) - {"rgb", "depth", "instance"}
        if bad:
            raise ValueError(f"unknown camera arrays {sorted(bad)}")
        np.savez_compressed(self._path(uav, sensor, seq), kind=np.array("camera"), **arrays)

    def lidar(self, uav: str, sensor: str, seq: int, obs: Mapping, n_rays: int) -> None:
        k, r = lidar_arrays(obs)
        np.savez_compressed(self._path(uav, sensor, seq), kind=np.array("lidar"), key=k, range_m=r, n_rays=np.int64(n_rays))

    def obs(self, uav: str, sensors: Mapping[str, list[Mapping]], n_rays: Mapping[str, int] | None = None) -> None:
        """一次交付观测的 `sensors{}`：相机帧（含 `rgb` 或 `depth`）与 LiDAR 帧（含 `xyz_m`）；IMU、GNSS 不在三级容差范围内。"""
        for sid, frames in sensors.items():
            for f in frames:
                if "xyz_m" in f:
                    self.lidar(uav, sid, f["seq"], f, (n_rays or {}).get(sid, int(f.get("n", 0)) or 1))
                elif "rgb" in f or "depth" in f:
                    self.camera(uav, sid, f["seq"], **camera_arrays(f))


def _frames(root: Path) -> dict[tuple[str, str, int], Path]:
    return {(p.parent.parent.name, p.parent.name, int(p.stem)): p for p in sorted(Path(root).glob("*/*/*.npz"))}


def compare_dumps(dir_a: Path, dir_b: Path, tol: Tier3 = TIER3) -> dict:
    fa, fb = _frames(dir_a), _frames(dir_b)
    keys = sorted(set(fa) & set(fb))
    frames, worst = [], {"depth_p90_m": 0.0, "shift_px": 0.0, "rgb_psnr_db": math.inf, "instance_iou": 1.0, "lidar_p99_m": 0.0,
                         "lidar_max_m": 0.0, "hit_mismatch": 0.0}
    for key in keys:
        with np.load(fa[key]) as za, np.load(fb[key]) as zb:
            kind = str(za["kind"])
            if kind == "lidar":
                n = int(max(za["n_rays"], zb["n_rays"]))
                rep = _lidar_diff(za["key"], za["range_m"], zb["key"], zb["range_m"], n, tol)
                worst["lidar_p99_m"] = max(worst["lidar_p99_m"], rep["p99_m"])
                worst["lidar_max_m"] = max(worst["lidar_max_m"], rep["max_m"])
                worst["hit_mismatch"] = max(worst["hit_mismatch"], rep["hit_mismatch"])
            else:
                rep = compare_camera({k: za[k] for k in za.files if k != "kind"}, {k: zb[k] for k in zb.files if k != "kind"}, tol)
                if "depth" in rep:
                    worst["depth_p90_m"] = max(worst["depth_p90_m"], rep["depth"]["p90_m"])
                worst["shift_px"] = max(worst["shift_px"], rep.get("shift_px", 0.0))
                worst["rgb_psnr_db"] = min(worst["rgb_psnr_db"], rep.get("psnr_db", math.inf))
                worst["instance_iou"] = min(worst["instance_iou"], rep.get("instance_iou_min", 1.0))
        frames.append({"uav": key[0], "sensor": key[1], "seq": key[2], "kind": kind, **rep})
    only_a = [list(k) for k in sorted(set(fa) - set(fb))]
    only_b = [list(k) for k in sorted(set(fb) - set(fa))]
    return {"schema": "drone4d.bench.tolerance_report.v1", "tolerance": asdict(tol), "n_frames": len(keys), "only_a": only_a, "only_b": only_b,
            "worst": worst,
            "n_fail": sum(1 for f in frames if f["fail"]), "pass": not only_a and not only_b and all(not f["fail"] for f in frames),
            "frames": frames}


def _finite(x):
    """JSON 不含 inf：PSNR 逐字节相同（+inf）记为 null。"""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_finite(v) for v in x]
    return x


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m awr.agentsdk.env.tolerance", description="跨环境三级容差比较（D3-AC-17、M25-FR-172）")
    ap.add_argument("dump_a", type=Path)
    ap.add_argument("dump_b", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="报告 JSON 路径（缺省写标准输出）")
    a = ap.parse_args(argv)
    rep = compare_dumps(a.dump_a, a.dump_b)
    txt = json.dumps(_finite(rep), ensure_ascii=False, indent=1, sort_keys=True, allow_nan=False)
    if a.out:
        a.out.write_text(txt + "\n", encoding="utf-8")
    else:
        sys.stdout.write(txt + "\n")
    if rep["only_a"] or rep["only_b"]:
        return 2
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
