"""热成像通道（M26 §14.3 `ref_thermal_blob_v1`，P0b；`ThermalObs` 见 M26-FR-124）：表观温差阈值连通域检测与到 L1 同形检测的换算。

解码（`decode_temp_ck`）：`radiometric_u16` 的 `temp_ck` 为单个 zstd 帧，解压为 2·W·H 字节的 u16 小端表观温度（0.01 K/LSB，
开尔文 = 值 / 100），与 `awr.agentsdk.schemas.decode_thermal_obs` 同一公式；`agc_jpeg` 只是 8 位预览，不做辐射测量，检测跳过。

检测（`blobs`，全部为整数运算，逐字节确定）：
1. 局部背景：以 8 px 为步长的格心取 31 × 31 窗口（边缘按边界值延拓）的中值，格内像素取所在格心的值；
2. 表观温差 ΔT = T − 背景（0.01 K），ΔT ≥ 200（2 K）为热像素；只取正温差（人员与行驶或刚停的车辆高于背景）；
3. 8 邻接连通域（逐行游程 + 并查集，按首游程的行优先次序编号），统计外接框、面积、峰值与 ΔT 加权质心；
4. 尺寸先验筛选：以框底中点的地面斜距 R 把像素尺寸换算为米（尺寸 × R / f），人员长边 0.25–2.5 m、短边 ≤ 1.2 m、
   地面面积 0.05–3 m²；车辆长边 2.5–8 m、短边 1.0–3.5 m、面积 2.5–24 m²，长宽比 ≤ 4.5；面积 < 2 px 的连通域丢弃。

适配（`ThermalPerception.detect(obs)`）：每个控制步只处理该步交付的最新一帧 `radiometric_u16` 热成像帧（640 × 512 单帧约 0.1 s，
热像仪 30 Hz 时逐帧处理会超出 L2 每步墙钟预算 0.5 s，M26-FR-126）；与 `L2Perception` 相同的相机位姿（导航解位置与航向、热像仪交付帧的云台读数、
挂载平移；三轴增稳），人员取框底中点、车辆取框内 3/4 高度处的视线与 `ground_hint.dtm_m` 平面求交得 `ground_xy_noisy`；
`det_id = sha256("thermal" | uav | seq | 序号)` 前 16 位；`l2 = true`、`thermal = true`，report 证据为热像仪帧的 `bbox_xywh`。
热成像检测不带外观（无颜色），只参与轨迹关联与 report 判定，不参与目标外观比对。
"""

from __future__ import annotations

import hashlib
import math

__all__ = ["BG_STEP", "BG_WIN", "DT_CK", "VERSION", "ThermalPerception", "blobs", "decode_temp_ck", "local_background"]

VERSION = "ref_thermal_blob_v1"
BG_WIN, BG_STEP = 31, 8
DT_CK = 200                      # 2 K（0.01 K/LSB）
MIN_AREA_PX = 2
PERSON = {"long": (0.25, 2.5), "short": (0.0, 1.2), "area": (0.05, 3.0)}
VEHICLE = {"long": (2.5, 8.0), "short": (1.0, 3.5), "area": (2.5, 24.0), "aspect": 4.5}


def decode_temp_ck(obs: dict):
    """`radiometric_u16` 的 ThermalObs → (H, W) uint16 表观温度（0.01 K/LSB）。需要 `zstandard`（`pip install zstandard`）。"""
    import numpy as np

    try:
        import zstandard
    except ImportError as e:                                   # pragma: no cover - 依赖缺失时给出明确提示
        raise ImportError("the thermal channel needs the zstandard package (pip install zstandard)") from e
    if obs.get("codec") != "radiometric_u16":
        raise ValueError(f"codec {obs.get('codec')!r} is not radiometric")
    w, h = int(obs["width"]), int(obs["height"])
    raw = zstandard.ZstdDecompressor().decompress(bytes(obs["temp_ck"]), max_output_size=2 * w * h)
    if len(raw) != 2 * w * h:
        raise ValueError(f"temp_ck payload has {len(raw)} bytes, expected {2 * w * h}")
    return np.frombuffer(raw, "<u2").reshape(h, w).astype(np.uint16)


def local_background(t):
    """(H, W) u16 → (H, W) int32 局部背景：8 px 格心的 31 × 31 窗口中值，格内取所在格心的值。"""
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    h, w = t.shape
    r = BG_WIN // 2
    pad = np.pad(t, r, mode="edge")
    cy = np.minimum(np.arange(-(-h // BG_STEP)) * BG_STEP + BG_STEP // 2, h - 1)
    cx = np.minimum(np.arange(-(-w // BG_STEP)) * BG_STEP + BG_STEP // 2, w - 1)
    win = sliding_window_view(pad, (BG_WIN, BG_WIN))[np.ix_(cy, cx)]             # (ny, nx, 31, 31)，窗口以格心为中心
    med = np.partition(win.reshape(len(cy), len(cx), -1), BG_WIN * BG_WIN // 2, axis=2)[:, :, BG_WIN * BG_WIN // 2].astype(np.int32)
    return np.repeat(np.repeat(med, BG_STEP, axis=0), BG_STEP, axis=1)[:h, :w]


def blobs(t) -> list[dict]:
    """热像素（ΔT ≥ DT_CK）的 8 邻接连通域：[{x0, y0, x1, y1（含端点）, area, peak_ck, cx, cy}]，按首游程的行优先次序。"""
    import numpy as np

    dt = t.astype(np.int32) - local_background(t)
    hot = dt >= DT_CK
    if not hot.any():
        return []
    d = np.diff(np.pad(hot.astype(np.int8), ((0, 0), (1, 1))), axis=1)
    st, en = np.argwhere(d == 1), np.argwhere(d == -1)                           # 行优先；同一行起止一一对应
    runs = [(int(a[0]), int(a[1]), int(b[1])) for a, b in zip(st, en, strict=True)]   # (y, x 起, x 止（不含）)
    parent = list(range(len(runs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    prev: list[int] = []
    cur: list[int] = []
    row = -1
    for i, (y, s, e) in enumerate(runs):
        if y != row:
            prev, cur = (cur if y == row + 1 else []), []
            row = y
        for j in prev:                                                            # 8 邻接：上一行游程与本游程在 ±1 px 内重叠
            _y, s2, e2 = runs[j]
            if s2 <= e and s <= e2:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
        cur.append(i)
    acc: dict[int, list] = {}
    for i, (y, s, e) in enumerate(runs):
        seg = dt[y, s:e]
        a = acc.setdefault(find(i), [s, y, e - 1, y, 0, 0, 0, 0, 0])
        a[0], a[1], a[2], a[3] = min(a[0], s), min(a[1], y), max(a[2], e - 1), max(a[3], y)
        a[4] += e - s
        a[5] = max(a[5], int(seg.max()))
        a[6] += int(seg.sum())
        a[7] += int((seg * np.arange(s, e)).sum())
        a[8] += int(seg.sum()) * y
    out = []
    for k in sorted(acc):
        x0, y0, x1, y1, area, peak, wsum, wx, wy = acc[k]
        out.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "area": area, "peak_ck": peak, "cx": wx / wsum, "cy": wy / wsum})
    return out


def _within(v: float, lo_hi: tuple[float, float]) -> bool:
    return lo_hi[0] <= v <= lo_hi[1]


def classify(b: dict, range_m: float, fx: float, fy: float) -> str | None:
    """按尺寸先验把连通域判为 person、vehicle 或 None（丢弃）。"""
    if b["area"] < MIN_AREA_PX:
        return None
    wm = (b["x1"] - b["x0"] + 1) * range_m / fx
    hm = (b["y1"] - b["y0"] + 1) * range_m / fy
    lng, sht = max(wm, hm), min(wm, hm)
    am = b["area"] * range_m * range_m / (fx * fy)
    if _within(lng, PERSON["long"]) and _within(sht, PERSON["short"]) and _within(am, PERSON["area"]):
        return "person"
    if (_within(lng, VEHICLE["long"]) and _within(sht, VEHICLE["short"]) and _within(am, VEHICLE["area"])
            and lng / max(sht, 1e-6) <= VEHICLE["aspect"]):
        return "vehicle"
    return None


def _rz(a: float):
    import numpy as np

    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _gimbal(az: float, el: float):
    """Rz(az)·Ry(−el)（FLU 相机系：x 前、y 左、z 上；同 `drone4d.perception.ref_det`）。"""
    import numpy as np

    ca, sa, ce, se = math.cos(az), math.sin(az), math.cos(el), math.sin(el)
    return np.array([[ca * ce, -sa, -ca * se], [sa * ce, ca, -sa * se], [se, 0.0, ce]])


def _yaw(q) -> float:
    x, y, z, w = (float(v) for v in q)
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


class ThermalPerception:
    """热成像帧 → 与 L1 检测同形的字典（`thermal = true`）。`sensor_id` 缺省取 agent_config 中第一个 `kind = thermal` 的传感器。"""

    def __init__(self, config: dict, sensor_id: str | None = None) -> None:
        th = [s for s in config["sensors"] if s["kind"] == "thermal" and (sensor_id is None or s["sensor_id"] == sensor_id)]
        if not th:
            raise ValueError("agent_config has no thermal sensor")
        self.sensor = th[0]["sensor_id"]
        self.uav = config["uav_id"]

    def detect(self, obs: dict) -> list[dict]:
        import numpy as np

        out: list[dict] = []
        frames = obs["sensors"].get(self.sensor, [])
        if not frames:
            return out
        nav = obs["nav"]
        p_b = np.asarray(nav["pos_enu_m"], np.float64)
        Rz = _rz(_yaw(nav["q_wb_xyzw"]))
        zg = float(obs["ground_hint"]["dtm_m"])
        for f in [x for x in frames if x.get("codec") == "radiometric_u16"][-1:]:      # 每步只处理最新一帧（墙钟预算）
            K = f["K"]
            fx, cx, fy, cy = float(K[0]), float(K[2]), float(K[4]), float(K[5])
            cam_p = p_b + Rz @ np.asarray(f["mount_nominal"]["t_bs_m"], np.float64)
            g = f["gimbal_reading"]
            R = Rz @ _gimbal(math.radians(g["yaw_deg"]), math.radians(g["pitch_deg"]))
            n = 0
            for b in blobs(decode_temp_ck(f)):
                X, Y, Wd, Hd = float(b["x0"]), float(b["y0"]), float(b["x1"] - b["x0"] + 1), float(b["y1"] - b["y0"] + 1)
                gp = self._ground(R, cam_p, zg, X + Wd / 2, Y + Hd, fx, fy, cx, cy)
                if gp is None:
                    continue
                rng = float(np.linalg.norm(gp - cam_p))
                cls = classify(b, rng, fx, fy)
                if cls is None:
                    continue
                if cls == "vehicle":
                    gv = self._ground(R, cam_p, zg, X + Wd / 2, Y + 0.75 * Hd, fx, fy, cx, cy)
                    gp = gv if gv is not None else gp
                det_id = hashlib.sha256(f"thermal|{self.uav}|{f['seq']}|{n}".encode()).hexdigest()[:16]
                n += 1
                out.append({"det_id": det_id, "sensor_id": self.sensor, "seq": int(f["seq"]), "t_mid_ns": int(f["t_mid_ns"]), "class": cls,
                            "bbox_xywh": [X, Y, Wd, Hd], "h_px": Hd, "slant_range_m": round(rng, 2),
                            "ground_xy_noisy": [round(float(gp[0]), 3), round(float(gp[1]), 3)],
                            "dT_k": round(b["peak_ck"] / 100.0, 2), "l2": True, "thermal": True})
        return out

    @staticmethod
    def _ground(R, cam_p, zg: float, u: float, v: float, fx: float, fy: float, cx: float, cy: float):
        import numpy as np

        d = R @ np.array([1.0, -(u - cx) / fx, -(v - cy) / fy])
        if d[2] >= -1e-6:
            return None
        return cam_p + (zg - cam_p[2]) / d[2] * d
