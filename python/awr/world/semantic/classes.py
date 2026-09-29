"""类别码表副本（M03-FR-028；AWR-16 §5 V-K-04）：`semantic/anet-classes@1.json` 为 contracts 真源的逐字节拷贝。"""

from __future__ import annotations

import json

from ..package.derivers import DeriveContext, LayerSpec
from ..package.schemas import classes_source_path

CLASSES_HREF = "semantic/anet-classes@1.json"


def class_count() -> int:
    return len(json.loads(classes_source_path().read_text(encoding="utf-8"))["classes"])


def copy_class_table(ctx: DeriveContext) -> list[LayerSpec]:
    src = classes_source_path()
    dst = ctx.stage / CLASSES_HREF
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return [LayerSpec("semantic.classes", "class-table", "semantic", "anet-classes@1", CLASSES_HREF, True,
                      files=[CLASSES_HREF])]
