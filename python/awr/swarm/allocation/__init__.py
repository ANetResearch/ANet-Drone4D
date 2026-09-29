"""多机分配：Hungarian（D1-ext）；统一打分与 SSI 为 V0.6（`score.py` 桩），CBBA 为 V1.0。

所有者：M10（AWR-03 §4.3）。
"""

from .hungarian import hungarian

__all__ = ["hungarian"]
