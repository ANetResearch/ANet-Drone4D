"""QA 门禁 G-01 至 G-10（AWR-16 §9；M03 §6.4 计算口径；M03-FR-018）。"""

from __future__ import annotations

from .types import GateResult


def run_gates(*, source_kind: str, peak_hag_m: float, precision: dict, leveled: bool, ground_plane_mad_m: float | None,
              tilt_after_deg: float, zero_normals_frac: float, ground_frac: float, true_north: str,
              evidence: tuple[str, ...], n_out: int, n_raw: int, n_nonfinite: int, n_dedup: int,
              raw_ok: bool, raw_detail: dict) -> list[GateResult]:
    g01_sev = "error" if source_kind == "dataset" else "warn"   # 尺度来自配准的数据降为 warn（M03 §14 交叉意见第 6 条）
    gates = [
        GateResult("G-01", "max building HAG in [100, 700] m", round(peak_hag_m, 1), [100, 700],
                   100.0 <= peak_hag_m <= 700.0, g01_sev),
        GateResult("G-02", "float32UlpMm < 1", precision["float32UlpMm"], 1, precision["float32UlpMm"] < 1.0, "warn"),
        GateResult("G-03", "maxRadiusM <= 10000", precision["maxRadiusM"], 10000, precision["maxRadiusM"] <= 10000.0, "warn"),
        GateResult("G-04", "ground plane residual MAD < 5 m after levelling",
                   None if ground_plane_mad_m is None else round(ground_plane_mad_m, 3), 5,
                   (not leveled) or (ground_plane_mad_m is not None and ground_plane_mad_m < 5.0), "error"),
        GateResult("G-05", "tilt after levelling < 0.5 deg", round(tilt_after_deg, 3), 0.5,
                   (not leveled) or tilt_after_deg < 0.5, "error"),
        GateResult("G-06", "zero normals fraction <= 1%", round(zero_normals_frac, 5), 0.01, zero_normals_frac <= 0.01, "warn"),
        GateResult("G-07", "ground fraction > 10% (else synthetic ground)", round(ground_frac, 3), 0.1, ground_frac > 0.10, "info"),
        GateResult("G-08", "landmark evidence when trueNorth is verified or exact", len(evidence), 1,
                   true_north not in ("verified", "exact") or len(evidence) > 0, "error"),
        GateResult("G-09", "points conserved (raw - non-finite - duplicates)", int(n_out),
                   int(n_raw - n_nonfinite - n_dedup), int(n_out) == int(n_raw - n_nonfinite - n_dedup), "error"),
        GateResult("G-10", "raw file bytes = header + N x 24 and sha256 matches configs/data.yaml", raw_detail, None,
                   bool(raw_ok), "error"),
    ]
    return gates


def gate_status(gates: list[GateResult]) -> str:
    if any((not g.passed) and g.severity == "error" for g in gates):
        return "fail"
    if any((not g.passed) and g.severity == "warn" for g in gates):
        return "warn"
    return "pass"
