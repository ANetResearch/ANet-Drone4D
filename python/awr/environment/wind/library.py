"""风场库：扇区选槽纯函数（M07-FR-052，D1 桩；M07 §6.3.13；g06 §5.3、§7.2）。

`sector_slots(theta_from, sectors_deg, antisymmetric)` -> [(file_index, weight, sign, a_deg)] × 2：反对称库只存
[0°, 180°)，展开为 12 个虚拟扇区并把符号折入。V0.3 起由运行时两扇区 flat-gather 采样使用；D1 只交付纯函数与 golden。
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["sector_slots"]


def sector_slots(theta_from: float, sectors_deg: Sequence[float], antisymmetric: bool) -> list[tuple[int, float, float, float]]:
    full = []
    for i, a in enumerate(sectors_deg):
        full.append((i, float(a), 1.0))
        if antisymmetric:
            full.append((i, (a + 180.0) % 360.0, -1.0))
    full.sort(key=lambda x: x[1])
    dd = theta_from % 360.0
    angs = [x[1] for x in full]
    j = next((k for k, a in enumerate(angs) if a > dd), 0)
    lo, hi = full[j - 1], full[j]
    span = (hi[1] - lo[1]) % 360.0 or 360.0
    w = ((dd - lo[1]) % 360.0) / span
    return [(lo[0], 1.0 - w, lo[2], lo[1]), (hi[0], w, hi[2], hi[1])]
