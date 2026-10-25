"""Rerun catalog（M27 §4.7 FR-079–085；AWR-05 §6.1.4；D3-AC-20、D3-AC-16）：评测与分发两个 catalog 的注册、持久化清单与启动重放、
保留期清理前的注销，以及跨 Run 查询封装 `D3Catalog`。

- `registry`：层策略（评测 catalog 注册全部层；分发 catalog 只注册 `obs.*` 与 `agent.*`）、注册前校验（694 版本、路径校验、
  分发 catalog 无真值实体与种子）、`registry.json` 清单、`replay`、`rebuild`、`deregister_callback`；
- `service`：catalog 服务进程（rerun-sdk 内置 OSS catalog 服务），启动时重放清单；
- `query`：`D3Catalog.runs / poses / query / frames / visibility / viewer_url`（694 迁移提示、Viewer `memory_limit` ≤ 1.5 GB）；
- `perf`：跨 3 个 Run 位姿查询的规模测量。

命令行：`python -m awr.bench.catalog register|rebuild|deregister|list|serve|perf ...`（见 `__main__`）。依赖 rerun-sdk 0.38.1 与
datafusion 54（requirements-d3.lock），只在 .venv-d3 与 d3-bench 镜像中可用。
"""

from __future__ import annotations

from awr.bench.catalog.query import VIEWER_MEMORY_LIMIT, D3Catalog, Frame, ViewerLink, viewer_options
from awr.bench.catalog.registry import (
    CATALOG_LAYERS,
    CATALOGS,
    CatalogError,
    CatalogVersionError,
    Entry,
    Registry,
    dataset_name,
    deregister_callback,
    layer_allowed,
    rebuild,
    register_asset,
    register_run,
    replay,
)
from awr.bench.catalog.service import CatalogService, live_client

__all__ = ["CATALOGS", "CATALOG_LAYERS", "VIEWER_MEMORY_LIMIT", "CatalogError", "CatalogService", "CatalogVersionError", "D3Catalog", "Entry",
           "Frame", "Registry", "ViewerLink", "dataset_name", "deregister_callback", "layer_allowed", "live_client", "rebuild", "register_asset",
           "register_run", "replay", "viewer_options"]
