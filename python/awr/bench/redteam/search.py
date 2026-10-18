"""泄漏红队：结构化检索（R1）与高熵字节检索（R2）（M26 §8.3、FR-101、FR-102；D3-AC-16）。

输入：`CaptureAudit` 录下的全部下发帧（CONFIG、OBS 含 inbox、ERROR、CLOSE，线上字节）与 `TruthSet`。输出 `leak_audit_report`：
配置、用例、判据、统计量、命中计数、命中帧数与字段路径；报告正文不写任何真值取值与标识明文。

**时间窗**：帧时刻 t 取 OBS 的 `t_ns`、CONFIG 为 0、ERROR 与 CLOSE 取该机上一帧 OBS 的时刻；真值在 [t − 1 s, t + 1 s] 内按
10 Hz 网格取样，另加帧内落在窗内的全部时间戳（`t_ns`、`t_mid_ns`、`t_start_ns`、`t0_ns`、`t_sent_ns`、`t_recv_ns`）处的插值。
不在连续时间上扫掠：带噪的合法测量（L1 地面位置、导航解）在连续扫掠下会以可观概率落在真值轨迹的 1 mm 管内而误报。

**R1 结构化检索**（以 schema 解码后的对象为准）：

- 数值：每个容器（数值数组，或对象的数值标量子字段集合）与真值向量做"成对匹配"——容器中两个不同取值分别在容差内等于同一时刻
  真值向量的两个不同分量即为命中（与容器内取值的次序无关；单个分量的偶然吻合不计）。位置容差 1 mm（浮点按米，非计数整数另按
  毫米定点解释），角度容差 1e-4 rad（度制字段按度换算；四元数与旋转矩阵按绝对值比较，覆盖 q ≡ −q、共轭与转置）。真值分量中
  |v| < 2 × 容差的零值与单位旋转中 ||v| − 1| < 1e-3 的取值不参与（与公开的零值和单位四元数吻合不构成信息）。计数器（`k`、`seq`、`n`、
  `index`、`sent`、`dropped_budget`、`rejected`、`code`、`bucket_B`）与 `*_ns` 时间戳不参与数值比对。
- 判据：本机真值位置、本机真值姿态、真值外参命中 0；真值云台角的命中数不显著高于云台编码器标称噪声（AWR-20 §15.6.5，realistic
  0.05°）下的期望（Poisson 单侧 p ≥ 0.01；未知噪声档时命中须为 0）；演员位置命中数不显著高于时间错位 ±60 s 的基线（条件二项
  单侧 p ≥ 0.01；CONFIG `brief/**` 中与公开锚点重合的命中单列为 `public_overlap`，见 L-16）。
- 字符串：全部字符串取值与演员 id、事件 id、划分名、`run_id`、磁带与事件哈希、种子及派生键的十进制与十六进制文本精确比对，命中 0。
- 整数：全部整数字段（含计数器）与 `run_seed` 及派生键（u64 与 i64 两种解释）精确比对，命中 0；L1 检测对象内的非计数整数与该检测
  对应的磁带演员序号比对（基线为相邻演员序号），命中数不显著高于基线（p ≥ 0.01）。

**R2 高熵字节检索**（帧体原始字节）：

- 令牌（全部字节，含图像载荷）：`run_seed` 的 u64 小端与大端、十进制与十六进制文本；SplitMix64 第一级派生键与流级键的同四种形式；
  磁带与事件文件哈希的原始 32 字节与十六进制（大小写）；`run_id`、事件 id、长度 ≥ 6 的演员 id 与划分名。短于 6 字节的令牌只在 R1
  字符串比对中检查（报告只列类别与个数）。`agent_seed`（HMAC 派生、合法下发）不在令牌集内。命中 0。
- 数值窗口（非图像载荷：去掉 `rgb.data`、`depth.data`、`temp_ck` 后的字节）：窗口时刻的真值量按三种编码、小端与大端检索——
  float64（单分量 8 字节，只取高熵取值：非零、不能被 float32 精确表示且不是整毫米；另以两个分量相邻的 16 字节与 msgpack 数组元素
  `cb ‖ 大端 8 字节` 相邻的 18 字节检索）、float32 与毫米定点 int32（两个分量相邻，8 字节）、毫米定点 int64（两个分量，16 字节）。
  4 字节编码以分量对构成 8 字节窗口，避免单个 4 字节值在 LiDAR 与 IMU 载荷中的偶然吻合；整毫米与短小数（磁带网格上的演员位置、
  公开常量）只按分量对检索。命中 0。
- 图像载荷只检查 JPEG 段白名单（SOI、APP0 JFIF、DQT、SOF0、DHT、DRI、SOS、RSTn、EOI；不得有其他 APPn 与 COM），违规 0。
"""

from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from collections.abc import Iterable

import msgpack
import numpy as np

from awr.agentsdk.env.core import TICK_NS
from awr.agentsdk.env.obs_audit import jpeg_segments_ok, lidar_tag_ok
from awr.bench.redteam.capture import CapturedFrame
from awr.bench.redteam.truth import TruthSet

__all__ = ["COUNTER_KEYS", "P_MIN", "REPORT_SCHEMA", "SHIFTS_S", "TOL_ANG_RAD", "TOL_POS_M", "WINDOW_NS", "audit_frames",
           "binom_sf", "jpeg_markers", "pair_hits", "poisson_sf"]

REPORT_SCHEMA = "drone4d.bench.leak_audit_report.v1"
TOL_POS_M = 1e-3
TOL_ANG_RAD = 1e-4
WINDOW_NS = 1_000_000_000
SHIFTS_S = (-60.0, 60.0)
P_MIN = 0.01
COUNTER_KEYS = frozenset({"k", "seq", "n", "index", "sent", "dropped_budget", "rejected", "code", "bucket_B"})
TS_KEYS = frozenset({"t_ns", "t_mid_ns", "t_start_ns", "t0_ns", "t_sent_ns", "t_recv_ns"})
_TS_ORDER = ("t_mid_ns", "t_ns", "t_start_ns", "t0_ns", "t_recv_ns")     # 对象自身时刻的取用次序（相机帧取曝光中点）
IMAGE_KEYS = (("rgb", "data"), ("depth", "data"), (None, "temp_ck"))
PUBLIC_PREFIXES = ("brief/",)                           # CONFIG 中的公开先验（简报锚点、任务区）；命中单列
GROUPS = ("own_position", "own_attitude", "extrinsics", "gimbal", "actors")
_UNIT = 1e-3                                            # 四元数与旋转矩阵：||v| − 1| < 1e-3 视为平凡取值


# ---------------------------------------------------------------------------------------------------------------- 统计


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X ≥ k)，X ~ Binom(n, p)。"""
    if k <= 0:
        return 1.0
    if k > n or p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    i = np.arange(k, n + 1, dtype=np.float64)
    lg = np.array([math.lgamma(n + 1)]) - np.vectorize(math.lgamma)(i + 1) - np.vectorize(math.lgamma)(n - i + 1)
    lp = lg + i * math.log(p) + (n - i) * math.log1p(-p)
    m = float(lp.max())
    return float(min(1.0, math.exp(m) * float(np.exp(lp - m).sum())))


def poisson_sf(k: int, lam: float) -> float:
    """P(X ≥ k)，X ~ Poisson(λ)。"""
    if k <= 0:
        return 1.0
    if lam <= 0.0:
        return 0.0
    i = np.arange(0, k, dtype=np.float64)
    lp = -lam + i * math.log(lam) - np.vectorize(math.lgamma)(i + 1)
    return float(max(0.0, 1.0 - float(np.exp(lp).sum())))


def _p_gimbal(rows: np.ndarray, g0: np.ndarray, tol: float, sig: float | None) -> float:
    """读数 r ~ N(g0, sigma² I)（编码器白噪声）落在任一取样真值 `rows` (S, 2) 的 ±tol 方框内的概率上界（并集界）。"""
    if not sig:
        return 0.0
    k = 1.0 / (sig * math.sqrt(2.0))
    d = rows - np.asarray(g0, np.float64)[None, :]
    erf = np.vectorize(math.erf)
    p = 0.5 * (erf((d + tol) * k) - erf((d - tol) * k))
    return float(min(1.0, p.prod(1).sum()))


# ---------------------------------------------------------------------------------------------------------------- 解码对象扫描


class _Scan:
    """一帧解码对象的叶子：数值容器（浮点值、毫米定点解释的非计数整数、所在对象的时间戳）、字符串、整数、二进制载荷、时间戳。"""

    __slots__ = ("bins", "conts", "ints", "strs", "times")

    def __init__(self, doc) -> None:
        self.conts: list[tuple[str, np.ndarray, np.ndarray, int | None]] = []   # (路径, 浮点值, 整数/1000, 最近祖先对象的时间戳)
        self.strs: list[tuple[str, str]] = []
        self.ints: list[tuple[str, int]] = []
        self.bins: list[tuple[str, bytes, bool]] = []                            # (路径, 字节, 是否图像载荷)
        self.times: set[int] = set()
        self._walk(doc, "", None)

    def _walk(self, x, path: str, t_ctx: int | None) -> None:
        if isinstance(x, dict):
            for tk in _TS_ORDER:
                v = x.get(tk)
                if isinstance(v, int) and not isinstance(v, bool):
                    t_ctx = int(v)
                    break
            fl, mm = [], []
            for k, v in x.items():
                p = f"{path}/{k}" if path else str(k)
                if isinstance(v, bool):
                    continue
                if isinstance(v, int):
                    self.ints.append((p, v))
                    if k in TS_KEYS:
                        self.times.add(int(v))
                    if k not in COUNTER_KEYS and not k.endswith("_ns"):
                        mm.append(v / 1000.0)
                elif isinstance(v, float):
                    fl.append(v)
                elif isinstance(v, str):
                    self.strs.append((p, v))
                elif isinstance(v, bytes | bytearray):
                    img = any((pk is None or pk == parent_key(path)) and k == kk for pk, kk in IMAGE_KEYS)
                    self.bins.append((p, bytes(v), img))
                else:
                    self._walk(v, p, t_ctx)
            if len(fl) + len(mm) >= 2:
                self.conts.append((path or "/", np.asarray(fl, np.float64), np.asarray(mm, np.float64), t_ctx))
        elif isinstance(x, list | tuple):
            p = f"{path}/*"
            if x and all(isinstance(v, int | float) and not isinstance(v, bool) for v in x):
                fl = [float(v) for v in x if isinstance(v, float)]
                mm = [v / 1000.0 for v in x if isinstance(v, int)]
                for v in x:
                    if isinstance(v, int):
                        self.ints.append((p, v))
                if len(x) >= 2:
                    self.conts.append((path, np.asarray(fl, np.float64), np.asarray(mm, np.float64), t_ctx))
                return
            for v in x:
                if isinstance(v, bool):
                    continue
                if isinstance(v, int):
                    self.ints.append((p, v))
                elif isinstance(v, str):
                    self.strs.append((p, v))
                elif isinstance(v, bytes | bytearray):
                    self.bins.append((p, bytes(v), False))
                elif isinstance(v, float):
                    continue
                else:
                    self._walk(v, p, t_ctx)


def parent_key(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else ""


# ---------------------------------------------------------------------------------------------------------------- R1 成对匹配


def pair_hits(vals: np.ndarray, Q: np.ndarray, tol: float) -> np.ndarray:
    """容器取值 `vals` (m,) 与真值样本 `Q` (N, d)（NaN 为不参与的分量）→ (N,) 命中：存在两个不同取值分别在 `tol` 内等于该行两个
    不同分量（二部图最大匹配 ≥ 2 ⇔ 匹配到的取值数 ≥ 2 且分量数 ≥ 2）。"""
    N = Q.shape[0]
    if len(vals) < 2 or N == 0:
        return np.zeros(N, bool)
    a = np.sort(vals)
    lo = np.searchsorted(a, Q - tol, side="left")
    hi = np.searchsorted(a, Q + tol, side="right")
    cnt = np.where(np.isnan(Q), 0, hi - lo)
    m = cnt > 0
    nj = m.sum(1)
    cand = nj >= 2
    if not cand.any():
        return cand
    multi = (m & (cnt >= 2)).any(1)
    big = len(a) + 1
    lo_min = np.where(m, lo, big).min(1)
    lo_max = np.where(m, lo, -1).max(1)
    return cand & (multi | (lo_max != lo_min))


def _trivial(Q: np.ndarray, tol: float, unit: bool) -> np.ndarray:
    Q = np.array(Q, np.float64, copy=True)
    bad = np.abs(Q) < 2 * tol
    if unit:
        bad |= np.abs(np.abs(Q) - 1.0) < _UNIT
    Q[bad] = np.nan
    return Q


def _class_spec(name: str) -> tuple[str, float, bool, bool]:
    """真值量名 → (组, 容差, 绝对值比较, 位置类)。"""
    leaf = name.rsplit("/", 1)[-1]
    if name == "own_pose/position":
        return "own_position", TOL_POS_M, False, True
    if name.startswith("own_pose/"):
        tol = math.degrees(TOL_ANG_RAD) if leaf.endswith("deg") else TOL_ANG_RAD
        return "own_attitude", tol, leaf == "attitude_q", False
    if name.startswith("gimbal/"):
        return "gimbal", math.degrees(TOL_ANG_RAD) if leaf == "deg" else TOL_ANG_RAD, False, False
    if name.startswith("extrinsics/"):
        if leaf in ("R", "q"):
            return "extrinsics", TOL_ANG_RAD, True, False
        return "extrinsics", TOL_POS_M, False, True
    raise KeyError(name)


# ---------------------------------------------------------------------------------------------------------------- R2 字节索引


def _windows(segments: Iterable[bytes]) -> np.ndarray:
    """全部 8 字节窗口（逐字节起点）→ 排序去重的 '<u8' 数组。"""
    parts = []
    for s in segments:
        if len(s) < 8:
            continue
        a = np.frombuffer(s, np.uint8)
        w = np.lib.stride_tricks.sliding_window_view(a, 8)
        parts.append(np.ascontiguousarray(w).view("<u8").ravel())
    return np.unique(np.concatenate(parts)) if parts else np.zeros(0, np.uint64)


def _member(x: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """x 中每个值是否在已排序去重的 `idx` 中（二分查找；不重复排序大索引）。"""
    if not len(idx) or not len(x):
        return np.zeros(len(x), bool)
    pos = np.minimum(np.searchsorted(idx, x), len(idx) - 1)
    return idx[pos] == x


def _u64(b: np.ndarray) -> np.ndarray:
    return np.frombuffer(np.ascontiguousarray(b).tobytes(), "<u8")


def _hi_entropy_f64(x: np.ndarray, tol: float) -> np.ndarray:
    """单分量 float64 窗口只取高熵取值：非零（|x| ≥ 2 tol）、不能被 float32 精确表示（低 29 位尾数非零），且不是整毫米（短小数与
    公开常量如 0.1、−10.0 的偶然吻合不计；整毫米取值仍由分量对编码检索）。"""
    x = np.asarray(x, np.float64)
    bits = x.astype("<f8").view("<u8")
    ok = np.isfinite(x) & (np.abs(x) >= 2 * tol)
    with np.errstate(invalid="ignore"):
        off_mm = np.abs(x * 1000.0 - np.round(x * 1000.0)) > 1e-6
    return ok & ((bits & np.uint64((1 << 29) - 1)) != 0) & off_mm


def _pairs(d: int) -> list[tuple[int, int]]:
    return [(i, j) for i in range(d) for j in range(d) if i != j]


def _rows(*cols: np.ndarray) -> np.ndarray:
    """各列（同长度、各自已按目标字节序转换的数组或 (N, w) uint8）按行拼接 → (N, L) uint8。"""
    parts = [np.ascontiguousarray(c).reshape(len(c), -1) for c in cols]
    parts = [p if p.dtype == np.uint8 else p.view(np.uint8) for p in parts]
    return np.ascontiguousarray(np.concatenate(parts, 1))


def _patterns(Q: np.ndarray, tol: float, position: bool) -> dict[str, tuple[np.ndarray, np.ndarray | None]]:
    """真值样本 (N, d) → 编码名 → (8 字节前缀 u64 数组, 完整模式 (N, L) uint8；L = 8 时为 None)。

    单分量：float64 小端与大端（只取高熵取值）。分量对（两个不同分量按次序相邻）：float64（16 字节）、msgpack float64 数组元素
    （`cb` + 大端 8 字节，两个相邻，18 字节）、float32（8 字节）、毫米定点 int32（8 字节）与 int64（16 字节），小端与大端。"""
    Q = np.asarray(Q, np.float64).reshape(-1, Q.shape[-1])
    Q = Q[np.isfinite(Q).all(1)]
    out: dict[str, tuple[np.ndarray, np.ndarray | None]] = {}
    if not len(Q):
        return out
    flat = Q.ravel()
    flat = flat[_hi_entropy_f64(flat, tol)]
    for end, nm in (("<", "le"), (">", "be")):
        out[f"f64{nm}"] = (_u64(flat.astype(f"{end}f8")), None)
    nz = np.abs(Q) >= 2 * tol
    ij = [(i, j) for i, j in _pairs(Q.shape[1]) if (nz[:, i] & nz[:, j]).any()]
    if not ij:
        return out
    X = np.concatenate([Q[nz[:, i] & nz[:, j]][:, [i, j]] for i, j in ij])        # (M, 2) 分量对
    cb = np.full((len(X), 1), 0xCB, np.uint8)
    enc: dict[str, np.ndarray] = {"mp_f64_pair": _rows(cb, X[:, :1].astype(">f8"), cb, X[:, 1:].astype(">f8"))}
    for end, nm in (("<", "le"), (">", "be")):
        enc[f"f64{nm}_pair"] = _rows(X.astype(f"{end}f8"))
        enc[f"f32{nm}_pair"] = _rows(X.astype(f"{end}f4"))
        if position:
            mm = np.round(X * 1000.0)
            ok = (np.abs(mm) < 2**31).all(1)
            enc[f"mm_i32{nm}_pair"] = _rows(mm[ok].astype(f"{end}i4"))
            enc[f"mm_i64{nm}_pair"] = _rows(mm.astype(f"{end}i8"))
    for name, rows in enc.items():
        if not len(rows):
            continue
        rows = np.unique(rows, axis=0)
        out[name] = (_u64(rows[:, :8]), rows if rows.shape[1] > 8 else None)
    return out


def jpeg_markers(b: bytes) -> list[int]:
    """JPEG 至首个 SOS 的段标记序列（APPn 0xE0–0xEF、COM 0xFE 等）。"""
    out, i, n = [], 2, len(b)
    if b[:2] != b"\xff\xd8":
        return out
    while i + 3 < n and b[i] == 0xFF:
        m = b[i + 1]
        if m == 0xFF:
            i += 1
            continue
        out.append(m)
        if m in (0xD9, 0xDA):
            break
        i += 2 + int.from_bytes(b[i + 2:i + 4], "big")
    return out


# ---------------------------------------------------------------------------------------------------------------- 主流程


def _frame_time(fr: CapturedFrame, last: dict[str, int]) -> int:
    d = fr.doc
    if fr.kind == "obs":
        t = int(d["obs"]["t_ns"])
        last[fr.uav] = t
        return t
    if fr.kind == "config":
        return 0
    return last.get(fr.uav, 0)


def _sample_times(t: int, extra: Iterable[int], lo: int, hi: int) -> np.ndarray:
    g0 = -(-(t - WINDOW_NS) // TICK_NS) * TICK_NS
    grid = range(g0, t + WINDOW_NS + 1, TICK_NS)
    ts = {x for x in grid if lo <= x <= hi} | {int(x) for x in extra if abs(int(x) - t) <= WINDOW_NS and lo <= int(x) <= hi}
    return np.array(sorted(ts), np.int64)


def _non_image_segments(body: bytes, imgs: list[bytes]) -> list[bytes]:
    segs, pos = [], 0
    for p in imgs:
        i = body.find(p, pos)
        if i < 0:
            i = body.find(p)
            if i < 0:
                continue
        if i >= pos:
            segs.append(body[pos:i])
            pos = i + len(p)
    segs.append(body[pos:])
    return segs


class _Tally:
    """按类别计命中：次数、字段路径（或编码名）、帧类型与命中帧数（泄漏逐帧重复，偶然吻合孤立出现）。"""

    def __init__(self) -> None:
        self.hits: Counter = Counter()
        self.paths: dict[str, Counter] = defaultdict(Counter)
        self.kinds: dict[str, Counter] = defaultdict(Counter)
        self.frames: dict[str, set] = defaultdict(set)

    def add(self, cls: str, path: str, fr: CapturedFrame, n: int = 1) -> None:
        self.hits[cls] += n
        self.paths[cls][path] += n
        self.kinds[cls][fr.kind] += n
        self.frames[cls].add((fr.uav, fr.idx))

    def total(self, prefix: str = "") -> int:
        return sum(v for k, v in self.hits.items() if k.startswith(prefix))

    def doc(self, prefix: str = "") -> dict:
        ks = sorted(k for k in self.hits if k.startswith(prefix))
        return {"hits": self.total(prefix), "by_class": {k: self.hits[k] for k in ks}, "frames": {k: len(self.frames[k]) for k in ks},
                "paths": {k: dict(sorted(self.paths[k].items())) for k in ks}, "by_kind": {k: dict(sorted(self.kinds[k].items())) for k in ks}}


def audit_frames(frames: Iterable[CapturedFrame], truth: TruthSet, *, audit_summary: dict | None = None, config: dict | None = None,
                 shifts_s: tuple[float, ...] = SHIFTS_S, token_min_len: int = 6) -> dict:
    """全部下发帧 × 真值 → `leak_audit_report`（只含计数、路径、统计量与判据，不含真值取值）。"""
    t_start = time.perf_counter()
    sec = truth.secrets
    strings, integers = sec.strings(), sec.integers()
    tokens, short_tokens = sec.tokens(token_min_len)
    tok_prefix = {}
    for tok, cls in tokens.items():
        if len(tok) >= 8:
            tok_prefix.setdefault(int(np.frombuffer(tok[:8], "<u8")[0]), []).append((tok, cls))
    tok_short = [(t, c) for t, c in tokens.items() if len(t) < 8]
    tok_keys = np.array(sorted(tok_prefix), np.uint64)

    r1v, r1pub, r1s, r1i, r2t, r2n, r2pub = _Tally(), _Tally(), _Tally(), _Tally(), _Tally(), _Tally(), _Tally()
    act = {"hits": 0, "baseline_hits": 0, "windows": 0, "baseline_windows": 0, "public_overlap": 0}
    gim = {"hits": 0, "expected": 0.0, "trials": 0}
    actor_no = {"trials": 0, "hits": 0, "baseline_hits": 0}
    jpeg = {"frames": 0, "violations": 0, "markers": Counter()}
    ltag = {"frames": 0, "violations": 0}
    kinds: Counter = Counter()
    n_bytes = n_img = n_searched = 0
    last: dict[str, int] = {}
    sig = truth.gimbal_sigma_deg

    for fr in frames:
        kinds[fr.kind] += 1
        n_bytes += len(fr.body)
        doc = fr.doc
        sc = _Scan(doc)
        t = _frame_time(fr, last)
        public_doc = doc.get("brief") if fr.kind == "config" and isinstance(doc, dict) else None
        public_bytes = msgpack.packb(public_doc, use_bin_type=True) if public_doc is not None else b""

        # ---------------------------------------------------------------- R1 字符串与整数
        for p, s in sc.strs:
            c = strings.get(s)
            if c is not None:
                r1s.add(c, p, fr)
        for p, v in sc.ints:
            c = integers.get(v)
            if c is not None:
                r1i.add(c, p, fr)
        if fr.kind == "obs" and truth.det_actor:
            for det in (doc["obs"].get("sensors") or {}).get("detections") or []:
                a = truth.det_actor.get(det.get("det_id"))
                if a is None:
                    continue
                vals = [v for p, v in _Scan(det).ints if parent_key(p.replace("/*", "")) not in COUNTER_KEYS
                        and not parent_key(p.replace("/*", "")).endswith("_ns")]
                actor_no["trials"] += 1
                actor_no["hits"] += int(a in vals)
                actor_no["baseline_hits"] += int((a + 1) % max(truth.n_actors, 1) in vals)

        # ---------------------------------------------------------------- R1 数值（成对匹配）
        own = None
        if fr.uav in truth.own:
            lo, hi = truth.own_range_ns(fr.uav)
            ts = _sample_times(t, sc.times, lo, hi)
            own = truth.own_samples(fr.uav, ts) if len(ts) else {}
        ts_a = _sample_times(t, sc.times, 0, truth.tape_t_max_ns)
        A = truth.actor_samples(ts_a) if len(ts_a) else np.zeros((0, truth.n_actors, 3))
        shifted = []
        for s in shifts_s:
            dt = round(s * 1e9)
            tsb = ts_a + dt
            if len(tsb) and tsb.min() >= 0 and tsb.max() <= truth.tape_t_max_ns:
                shifted.append(truth.actor_samples(tsb))
        act["windows"] += 1
        act["baseline_windows"] += len(shifted)
        Aq = _trivial(A.reshape(-1, 3), TOL_POS_M, False)
        Bq = [_trivial(B.reshape(-1, 3), TOL_POS_M, False) for B in shifted]
        own_q = {}
        for name, Q in (own or {}).items():
            grp, tol, absq, posn = _class_spec(name)
            own_q[name] = (grp, tol, absq, posn, _trivial(np.abs(Q) if absq else Q, tol, absq))
        for path, fl, mm, t_ctx in sc.conts:
            allv = np.concatenate([fl, mm])
            pub = fr.kind == "config" and path.startswith(PUBLIC_PREFIXES)
            if len(allv) >= 2 and len(Aq):
                n_hit = int(pair_hits(allv, Aq, TOL_POS_M).reshape(len(ts_a), -1).any(0).sum())
                if n_hit:
                    if pub:
                        act["public_overlap"] += n_hit
                        r1pub.add("actors", path, fr, n_hit)
                    else:
                        act["hits"] += n_hit
                        r1v.add("actors", path, fr, n_hit)
                for Bqi in Bq:
                    if not pub:
                        act["baseline_hits"] += int(pair_hits(allv, Bqi, TOL_POS_M).reshape(len(ts_a), -1).any(0).sum())
            for name, (grp, tol, absq, posn, Qn) in own_q.items():
                v = allv if posn else fl
                if len(v) < 2:
                    continue
                v = np.abs(v) if absq else v
                h = pair_hits(v, Qn, tol)
                if name == "gimbal/deg" and path.endswith("gimbal_reading"):   # 带噪编码器读数：与标称噪声下的期望比较
                    rows = Qn[np.isfinite(Qn).all(1)]
                    if len(rows):
                        g0 = truth.own_samples(fr.uav, np.array([t_ctx if t_ctx is not None else t], np.int64))["gimbal/deg"][0]
                        gim["trials"] += 1
                        gim["expected"] += _p_gimbal(np.unique(np.round(rows, 9), axis=0), g0, tol, sig)
                        gim["hits"] += int(h.any())
                    continue
                if h.any():
                    r1v.add(f"{grp}:{name}", path, fr)

        # ---------------------------------------------------------------- R2 令牌（全部字节）
        body = fr.body
        idx_all = _windows([body])
        if len(tok_keys):
            for key in tok_keys[_member(tok_keys, idx_all)].tolist():
                for tok, cls in tok_prefix[key]:
                    if tok in body:
                        r2t.add(cls, "<frame>", fr)
        for tok, cls in tok_short:
            if tok in body:
                r2t.add(cls, "<frame>", fr)

        # ---------------------------------------------------------------- R2 数值窗口（非图像载荷）与图像段白名单
        imgs = [b for _p, b, img in sc.bins if img]
        n_img += sum(len(b) for b in imgs)
        segs = _non_image_segments(body, imgs)
        n_searched += sum(len(s) for s in segs)
        idx = _windows(segs)
        enc_sets: list[tuple[str, np.ndarray, float, bool]] = [("actors", A, TOL_POS_M, True)]
        for name, Q in (own or {}).items():
            grp, tol, absq, posn = _class_spec(name)
            enc_sets.append((f"{grp}:{name}", Q, tol, posn))
        for cls, Q, tol, posn in enc_sets:
            if not len(Q):
                continue
            for enc, (pref, full) in _patterns(Q, tol, posn).items():
                m = _member(pref, idx)
                if not m.any():
                    continue
                if full is None:
                    found = [pref[i].tobytes() for i in np.nonzero(m)[0]]
                else:
                    found = [b for b in (full[i].tobytes() for i in np.nonzero(m)[0]) if any(b in sg for sg in segs)]
                for pat in dict.fromkeys(found):
                    if public_bytes and pat in public_bytes:
                        r2pub.add(cls, enc, fr)
                    else:
                        r2n.add(cls, enc, fr)
        for p, b, img in sc.bins:
            if img and p.endswith("rgb/data") and b[:2] == b"\xff\xd8":
                jpeg["frames"] += 1
                jpeg["violations"] += int(not jpeg_segments_ok(b))
                for mk in jpeg_markers(b):
                    if 0xE0 <= mk <= 0xEF or mk == 0xFE:
                        jpeg["markers"][f"0x{mk:02X}"] += 1
            elif p.endswith("/tag"):
                ltag["frames"] += 1
                ltag["violations"] += int(not lidar_tag_ok(b))

    n_base = act["baseline_windows"]
    pi = act["windows"] / (act["windows"] + n_base) if n_base else None
    act["p_value"] = binom_sf(act["hits"], act["hits"] + act["baseline_hits"], pi) if pi is not None else (1.0 if act["hits"] == 0 else 0.0)
    gim["expected"] = round(gim["expected"], 4)
    gim["p_value"] = poisson_sf(gim["hits"], gim["expected"]) if sig else (1.0 if gim["hits"] == 0 else 0.0)
    gim["encoder_sigma_deg"] = sig
    actor_no["p_value"] = binom_sf(actor_no["hits"], actor_no["hits"] + actor_no["baseline_hits"], 0.5)
    jpeg["markers"] = dict(sorted(jpeg["markers"].items()))

    v = {
        "r1.own_position": r1v.total("own_position:") == 0,
        "r1.own_attitude": r1v.total("own_attitude:") == 0,
        "r1.extrinsics": r1v.total("extrinsics:") == 0,
        "r1.gimbal": gim["p_value"] >= P_MIN and r1v.total("gimbal:") == 0,
        "r1.actors": act["p_value"] >= P_MIN,
        "r1.strings": r1s.total() == 0,
        "r1.integers": r1i.total() == 0,
        "r1.actor_no": actor_no["p_value"] >= P_MIN,
        "r2.tokens": r2t.total() == 0,
        **{f"r2.{g}": r2n.total(g) == 0 for g in GROUPS},
        "r2.jpeg_segments": jpeg["violations"] == 0,
        "r2.lidar_tag": ltag["violations"] == 0,
    }
    if audit_summary is not None:
        v["schema.whitelist"] = audit_summary.get("unknown_fields", 1) == 0 and audit_summary.get("schema_violations", 1) == 0
    failed = sorted(k for k, ok in v.items() if not ok)
    return {
        "schema": REPORT_SCHEMA,
        "config": {**(config or {}), "obs_level": truth.obs_level, "noise": truth.noise, "uavs": sorted(truth.own), "duration_s": truth.duration_s,
                   "n_actors": truth.n_actors, "frames": dict(sorted(kinds.items())), "bytes": n_bytes, "image_bytes": n_img,
                   "numeric_bytes_searched": n_searched, "wall_s": round(time.perf_counter() - t_start, 2)},
        "criteria": {"window_s": WINDOW_NS / 1e9, "tol_pos_m": TOL_POS_M, "tol_ang_rad": TOL_ANG_RAD, "baseline_shifts_s": list(shifts_s),
                     "p_min": P_MIN, "token_min_len": token_min_len,
                     "zero_hit": ["r1.own_position", "r1.own_attitude", "r1.extrinsics", "r1.strings", "r1.integers", "r2.*"],
                     "statistical": {"r1.actors": "conditional binomial vs ±60 s shifted baseline", "r1.gimbal": "Poisson vs encoder noise",
                                     "r1.actor_no": "binomial vs neighbour actor number"}},
        "r1": {"vector": r1v.doc(), "public_overlap": r1pub.doc(), "actors": act, "gimbal": gim, "strings": r1s.doc(), "integers": r1i.doc(),
               "actor_no": actor_no},
        "r2": {"tokens": {**r2t.doc(), "short_tokens_r1_only": short_tokens, "n_tokens": len(tokens)}, "numeric": r2n.doc(),
               "public_overlap": r2pub.doc(), "jpeg": jpeg, "lidar_tag": ltag},
        "audit": {k: audit_summary.get(k) for k in ("schema_checked", "schema_violations", "unknown_fields", "jpeg_segment_violations",
                                                    "lidar_tag_violations", "passed")} if audit_summary is not None else None,
        "verdict": v, "failed": failed, "passed": not failed,
    }
