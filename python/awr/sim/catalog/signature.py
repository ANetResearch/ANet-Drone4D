"""机型签名：1/3 倍频程缺省谱形、声功率推力律、签名摘要与 SG-1 至 SG-3（CAT-FR-007；AWR-20 §4.4、§8.2；ADR-097）。

纯函数，不抽随机数（CAT-NFR-002）。谱形 `band_shape_db[b]` 的 A 计权能量和为 0 dB，带 b 的声功率 `L_W,b = L_WA + band_shape_db[b]`。

- `default_band_shape(bpf_hz, tonal_db, f_pk_hz)`：宽带为以 `f_pk` 为峰的对数高斯（1 dB/oct²），BPF 与前 4 个谐波所在带叠加
  `tonal_db − 3·(h − 1)` dB，归一化到 A 计权和 0 dB（AWR-20 §4.4 伪代码）；
- `lwa_at_thrust(L_ref, T, T_ref, k)`：`L_WA(T) = L_WA,ref + 10·k·lg(T/T_ref)`；
- `band_shapes(doc)`：按构型给出 `hover`（多旋翼、VTOL 升力）与 `cruise`（固定翼、VTOL 推进）谱形；文件给出 `band_shape_db`
  时取文件值。VTOL 的推进谱形取 AWR-20 §4.4 的推进参数（tonal 10 dB、峰 1000 Hz）：schema 1.1 的 `acoustic` 只有一组
  `tonal_db`、`f_pk_hz`，用于升力谱形。
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["A_WEIGHT_DB", "BANDS", "FC_HZ", "PUSHER_DEFAULTS", "a_weighted_sum_db", "band_center_hz", "band_of",
           "band_shapes", "default_band_shape", "lwa_at_thrust", "sg_checks", "signature_summary", "unweighted_total_db"]

BANDS = 24
FC_HZ = (50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300,
         8000, 10000)
A_WEIGHT_DB = (-30.2, -26.2, -22.5, -19.1, -16.1, -13.4, -10.9, -8.6, -6.6, -4.8, -3.2, -1.9, -0.8, 0.0, 0.6, 1.0, 1.2, 1.3,
               1.2, 1.0, 0.5, -0.1, -1.1, -2.5)  # IEC 61672 A 计权（标称中心频率）
K_BB = 1.0  # dB/oct²
N_HARM = 5
DEC_DB = 3.0
PUSHER_DEFAULTS = {"tonal_db": 10.0, "f_pk_hz": 1000.0}  # AWR-20 §4.4：推进桨谱形（F1、V1 推进）
SG1_TOL_DB = 0.05
SG3_ROUND_M2 = 0.005  # 可见截面按两位小数取值（P600 top 0.22 对 0.469² = 0.21996）的舍入余量
FRAME_FW = frozenset({"fixed_wing", "vtol_quadplane"})
FRAME_MR = frozenset({"quad_x", "hex_x", "vtol_quadplane"})


def band_center_hz(b: int) -> float:
    """带 b 的精确中心频率 `1000·10^((b − 13)/10)`。"""
    return 1000.0 * 10.0 ** ((b - 13) / 10.0)


def band_of(f_hz: float) -> int | None:
    """频率所在的 1/3 倍频程带（边界 `f_b·10^(±0.05)`）；不在 50 Hz–10 kHz 的 24 带内时 None。"""
    if not (f_hz > 0.0 and math.isfinite(f_hz)):
        return None
    b = round(13.0 + 10.0 * math.log10(f_hz / 1000.0))
    for k in (b - 1, b, b + 1):
        if 0 <= k < BANDS:
            fc = band_center_hz(k)
            if fc * 10.0 ** -0.05 <= f_hz < fc * 10.0 ** 0.05:
                return k
    return None


def default_band_shape(bpf_hz: float, tonal_db: float, f_pk_hz: float, *, k_bb: float = K_BB, n_h: int = N_HARM,
                       dec_db: float = DEC_DB) -> list[float]:
    """缺省 1/3 倍频程谱形（dB，按带序 24 个）。"""
    base = [-k_bb * math.log2(band_center_hz(b) / f_pk_hz) ** 2 for b in range(BANDS)]
    lin = [10.0 ** (x / 10.0) for x in base]
    for h in range(1, n_h + 1):
        b = band_of(h * bpf_hz)
        if b is not None:
            lin[b] += 10.0 ** ((base[b] + tonal_db - dec_db * (h - 1)) / 10.0)
    s = [10.0 * math.log10(x) for x in lin]
    c = -10.0 * math.log10(sum(10.0 ** ((s[b] + A_WEIGHT_DB[b]) / 10.0) for b in range(BANDS)))
    return [x + c for x in s]


def a_weighted_sum_db(shape: list[float]) -> float:
    """谱形的 A 计权能量和（dB）；缺省谱形为 0。"""
    return 10.0 * math.log10(sum(10.0 ** ((shape[b] + A_WEIGHT_DB[b]) / 10.0) for b in range(BANDS)))


def unweighted_total_db(shape: list[float]) -> float:
    """未计权总声功率相对 L_WA 的差（dB）；P600 为 1.6 dB（AWR-20 §4.4）。"""
    return 10.0 * math.log10(sum(10.0 ** (x / 10.0) for x in shape))


def lwa_at_thrust(lwa_ref_dba: float, thrust_n: float, thrust_ref_n: float, k: float = 1.5) -> float:
    """声功率推力律（ADR-097）：`L_WA(T) = L_WA,ref + 10·k·lg(T/T_ref)`。"""
    return float(lwa_ref_dba) + 10.0 * float(k) * math.log10(max(float(thrust_n), 1e-9) / max(float(thrust_ref_n), 1e-9))


def _num(x: Any) -> float | None:
    v = x.get("value") if isinstance(x, dict) and "value" in x else x
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _ft(doc: dict) -> str:
    return str((doc.get("frame") or {}).get("type") or "quad_x")


def band_shapes(doc: dict) -> dict[str, list[float]]:
    """按构型的谱形：`hover`（多旋翼、VTOL 升力）与 `cruise`（固定翼、VTOL 推进）。文件的 `band_shape_db` 用于该构型的
    主谱形（多旋翼与 VTOL 为 hover，固定翼为 cruise）。"""
    ac = doc.get("acoustic") or {}
    ft = _ft(doc)
    out: dict[str, list[float]] = {}
    tonal = _num(ac.get("tonal_db"))
    fpk = _num(ac.get("f_pk_hz"))
    file_shape = ac.get("band_shape_db")
    if ft in FRAME_MR and _num(ac.get("bpf_hz")) is not None:
        out["hover"] = list(map(float, file_shape)) if isinstance(file_shape, list) and len(file_shape) == BANDS else \
            default_band_shape(float(ac["bpf_hz"]), tonal if tonal is not None else 12.0, fpk if fpk is not None else 1414.0)
    if ft in FRAME_FW:
        bpf_c = _num(ac.get("bpf_cruise_hz")) or _num(ac.get("bpf_hz"))
        if bpf_c is not None:
            if ft == "fixed_wing":
                if isinstance(file_shape, list) and len(file_shape) == BANDS:
                    out["cruise"] = list(map(float, file_shape))
                else:
                    out["cruise"] = default_band_shape(bpf_c, tonal if tonal is not None else PUSHER_DEFAULTS["tonal_db"],
                                                       fpk if fpk is not None else PUSHER_DEFAULTS["f_pk_hz"])
            else:
                out["cruise"] = default_band_shape(bpf_c, PUSHER_DEFAULTS["tonal_db"], PUSHER_DEFAULTS["f_pk_hz"])
    return out


def signature_summary(doc: dict) -> dict[str, Any]:
    """`GET /catalog/models/{id}` 的 `signature{acoustic, visual, rcs_m2, ir}`（AWR-20 §9）；谱形为生成值（保留 2 位小数）。"""
    ac = dict(doc.get("acoustic") or {})
    shapes = {k: [round(x, 2) for x in v] for k, v in band_shapes(doc).items()}
    acoustic = {k: ac[k] for k in ("lwa_hover_dba", "lwa_cruise_dba", "thrust_exp", "bpf_hz", "bpf_cruise_hz", "blades", "tonal_db",
                                   "f_pk_hz", "conf", "src") if k in ac}
    acoustic["band_shape_db"] = shapes
    acoustic["fc_hz"] = list(FC_HZ)
    return {"acoustic": acoustic, "visual": dict(doc.get("visual") or {}), "rcs_m2": _num(doc.get("rcs_m2")),
            "ir": dict(doc.get("ir") or {})}


def _row(cid: str, value: Any, rng: Any, ok: bool, msg: str | None = None) -> dict:
    r = {"id": cid, "value": value, "range": rng, "pass": bool(ok)}
    if msg:
        r["msg"] = msg
    return r


def sg_checks(doc: dict) -> list[dict]:
    """SG-1（谱形长度 24、A 计权和 0 ± 0.05 dB；文件 `derived.band_shape_db` 与生成值 ±0.1 dB）、SG-2（BPF 前 5 次谐波至少 1 个
    落在 50 Hz–10 kHz；tonal_db ∈ [0, 20]）、SG-3（可见截面不超过包围盒投影，允许两位小数的舍入余量 0.005 m²；rcs_m2 > 0）。没有签名字段的机型（x500）
    只给出通过的空行。"""
    out: list[dict] = []
    ac = doc.get("acoustic")
    if isinstance(ac, dict) and ac:
        shapes = band_shapes(doc)
        sums = {k: round(a_weighted_sum_db(v), 4) for k, v in shapes.items()}
        ok = bool(shapes) and all(len(v) == BANDS for v in shapes.values()) and all(abs(s) <= SG1_TOL_DB for s in sums.values())
        msg = None
        der = (doc.get("derived") or {}).get("band_shape_db")
        if isinstance(der, list) and shapes:
            main = shapes.get("hover") or shapes.get("cruise") or []
            diff = max((abs(float(a) - b) for a, b in zip(der, main, strict=False)), default=0.0)
            if len(der) != BANDS or diff > 0.1:
                ok, msg = False, f"derived.band_shape_db 与生成值相差 {diff:.3f} dB（> 0.1）"
        out.append(_row("SG-1", sums, [-SG1_TOL_DB, SG1_TOL_DB], ok, msg))
        bpfs = [x for x in (_num(ac.get("bpf_hz")), _num(ac.get("bpf_cruise_hz"))) if x is not None]
        in_band = all(any(50.0 * 10 ** -0.05 <= h * f < 10000.0 * 10 ** 0.05 for h in range(1, N_HARM + 1)) for f in bpfs)
        tonal = _num(ac.get("tonal_db"))
        ok2 = bool(bpfs) and in_band and (tonal is None or 0.0 <= tonal <= 20.0)
        out.append(_row("SG-2", {"bpf_hz": bpfs, "tonal_db": tonal}, [0, 20], ok2))
    vis = doc.get("visual")
    rcs = _num(doc.get("rcs_m2"))
    if isinstance(vis, dict) or rcs is not None:
        sz = ((doc.get("geometry") or {}).get("size_m") or {})
        sz = sz.get("value") if isinstance(sz, dict) and "value" in sz else sz
        a = (vis or {}).get("area_m2") or {}
        ok3 = rcs is None or rcs > 0.0
        msg3 = None
        if isinstance(sz, dict) and a:
            lim = {"top": sz["l"] * sz["w"], "side": sz["l"] * sz["h"], "front": sz["w"] * sz["h"]}
            over = [k for k in ("top", "side", "front") if k in a and float(a[k]) > lim[k] + SG3_ROUND_M2]
            if over:
                ok3, msg3 = False, f"可见截面超过包围盒投影：{over}"
        out.append(_row("SG-3", {"area_m2": a or None, "rcs_m2": rcs}, None, ok3, msg3))
    return out
