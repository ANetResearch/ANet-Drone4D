"""`IngestFromArrays` 与 `ArraysAdapter`（M03-FR-021，D1-ext；M03 §6.13 (3)、§7.2；M01 §6.8 字段映射；M01-to-M03 第 1 条）。

M01 的 recon 任务在 GEOREFERENCING 之后把融合点按 `T_world_engine` 变到 world 帧，组装 `IngestFromArrays`，以
`build_world(ArraysAdapter(spec), worlds_dir, ctx=job_ctx)` 走与六城相同的切片、派生、打包、`--deep` 校验与原子发布。
与六城 ingest 的差别只在 ingest 一步：派生世界沿用源世界的坐标帧（AWR-16 §14.1"派生世界"：不重选原点、跳过第 2–5 步，
`anchor`、`T_ecef_world`、`trueNorth` 逐字段复制源世界），清单写入重建字段（`coordinate.source.kind = reconstruction`、
`unitsToMeters = s`、`registration`、`scaleStatus`、`reconstruction.<session_id>` 图层、`tags`、`dataset.notice`、
`generator.params.recon`、`camera.home`）。实现取自 M01 的过渡实现 `pipeline/m03_bridge.py`（行为参照，逐项对齐；M03 交付后该过渡实现已移除），
流水线子类见 `awr.world.package.arrays_build`。

在 M03 §7.2 冻结字段之后追加的可选字段（M03 PRD §7.2 修订）：`anchor_json`（源世界 coordinate.json 的 anchor 对象原样，
`Anchor` 类型不含 label、uncertaintyM 等字段）、`T_ecef_world`、`session_id`、`tags`、`staging_nonce`（staging 目录名
`<target>-<nonce>`，M01 取 job_id 以便续跑保留）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .types import ConfigError, IngestConfig, RawCloud

__all__ = ["MAX_POINTS", "ArraysAdapter", "IngestFromArrays"]

MAX_POINTS = 50_000_000  # D1 单遍切片上限（M03-NFR-017）


@dataclass(frozen=True)
class IngestFromArrays:
    world_id: str
    name: str
    name_zh: str | None
    xyz_world: np.ndarray                     # float64 (N, 3)，已在 world 帧，m
    normals: np.ndarray | None                # float32 (N, 3)，只旋转到 world
    class_index: np.ndarray | None            # uint8 (N,)；None 时按规则分类
    anchor: Any                               # awr.world.georef.types.Anchor（源世界）
    true_north: dict                          # {yawOffsetDeg, confidence, evidence[]}
    scale_status: str
    source_kind: str                          # reconstruction | lio-map
    registration: dict                        # {method, T_world_map: Sim3 JSON, rmseM?, inliers?, alignmentRef}
    source_origin_engine: tuple[float, float, float] | None
    dataset: dict
    generator_params: dict | None = None      # world.json generator.params.recon
    camera_home: dict | None = None
    recon_session_dir: Path | None = None     # 复制到 reconstruction/<session_id>/ 并登记图层
    # ---- 追加的可选字段（见模块文档）
    anchor_json: dict | None = None
    T_ecef_world: list | None = None
    session_id: str = ""
    tags: tuple[str, ...] = ("recon", "synthetic")
    staging_nonce: str | None = None

    def validate(self) -> None:
        import re

        if not re.fullmatch(r"[a-z0-9-]{1,63}", self.world_id or ""):
            raise ConfigError(f"IngestFromArrays.world_id 非法：{self.world_id!r}")
        X = np.asarray(self.xyz_world)
        if X.ndim != 2 or X.shape[1] != 3 or not 1 <= len(X) <= MAX_POINTS:
            raise ConfigError(f"IngestFromArrays.xyz_world 形状 {X.shape} 不在 (1..{MAX_POINTS}, 3)")
        if self.normals is not None and np.asarray(self.normals).shape != X.shape:
            raise ConfigError("IngestFromArrays.normals 形状与 xyz_world 不一致")
        if self.class_index is not None and np.asarray(self.class_index).shape != (len(X),):
            raise ConfigError("IngestFromArrays.class_index 长度与点数不一致")
        if self.source_kind not in ("reconstruction", "lio-map"):
            raise ConfigError(f"IngestFromArrays.source_kind 非法：{self.source_kind!r}")
        S = (self.registration or {}).get("T_world_map")
        if not isinstance(S, dict) or not {"s", "q", "t"} <= set(S):
            raise ConfigError("IngestFromArrays.registration.T_world_map 缺少 s、q、t")
        d = self.recon_session_dir
        if d is not None:
            p = Path(d)
            if p.is_symlink() or not p.is_dir():
                raise ConfigError(f"IngestFromArrays.recon_session_dir 不是目录或为符号链接：{p}")


class ArraysAdapter:
    """IngestAdapter over an `IngestFromArrays`（M03 §7.2）；`build_world` 据 `pipeline_cls` 使用派生世界流水线。"""

    kind = "arrays"

    def __init__(self, spec: IngestFromArrays) -> None:
        spec.validate()
        self.spec = spec

    @property
    def pipeline_cls(self) -> type:
        from awr.world.package.arrays_build import ArraysPipeline

        return ArraysPipeline

    @property
    def staging_nonce(self) -> str | None:
        s = self.spec
        return s.staging_nonce or ((s.generator_params or {}).get("job_id"))

    def build_params(self, base: Any) -> Any:
        from awr.world.package.arrays_build import with_recon

        return with_recon(base, self.spec.generator_params)

    def config(self) -> IngestConfig:
        s = self.spec
        tn = s.true_north or {}
        return IngestConfig(world_id=s.world_id, source_kind=s.source_kind,
                            units_to_m=float(s.registration["T_world_map"]["s"]), up_axis="+z", level=False, yaw_deg=0.0,
                            true_north=tn.get("confidence", "assumed"), scale_status=s.scale_status, landmark=None,
                            fallback_anchor=None, evidence=(s.session_id, f"registration {s.registration.get('method')}"),
                            north_evidence=tuple(tn.get("evidence") or ()))

    def load(self) -> RawCloud:
        return RawCloud(xyz=self.spec.xyz_world, normal=self.spec.normals, files=[])

    def provenance(self) -> dict:
        return self.spec.dataset

    def manifest_extras(self) -> dict:
        s = self.spec
        ex: dict[str, Any] = {"name": s.name, "nameZh": s.name_zh, "tags": list(s.tags), "source_dataset": None}
        if s.camera_home:
            ex["camera_home"] = s.camera_home
        return ex
