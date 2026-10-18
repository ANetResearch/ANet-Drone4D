"""catalog 服务进程（M27 §8.4 `catalog-eval`、`catalog-dist`；M27-FR-080）：Rerun OSS catalog 服务（rerun-sdk 0.38.1 内置
`rr.server.Server`）启动后立即重放注册清单；元数据只在内存中，重启后由 `registry.json` 恢复。

部署：容器 `d3-catalog-eval`、`d3-catalog-dist`，只监听 dmax 内网地址，浏览器经 rerun-proxy 访问（AWR-05 §6.1.4、M27-FR-155）；
注册清单与 `.rrd` 以相同绝对路径挂载（`/data/d3`），清单中的路径即服务可见路径。启动时写 `<root>/<catalog>/service.json`
（URL、进程号、启动时刻与重放报告），注册作业据此连接在线服务。
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

from awr.bench.catalog.registry import CATALOGS, Registry, replay

__all__ = ["CatalogService", "live_client"]


class CatalogService:
    """进程内 catalog 服务：`start()` 启动并重放注册清单，`client()` 返回 CatalogClient，`shutdown()` 停止。"""

    def __init__(self, root: Path, catalog: str, *, host: str = "127.0.0.1", port: int | None = None) -> None:
        if catalog not in CATALOGS:
            raise ValueError(f"catalog {catalog!r} not in {CATALOGS}")
        self.root, self.catalog, self.host, self.port = Path(root), catalog, host, port
        self.server = None
        self.report: dict = {}

    def start(self) -> CatalogService:
        import rerun as rr

        t0 = time.perf_counter()
        self.server = rr.server.Server(host=self.host, port=self.port)
        self.report = replay(self.server.client(), self.root, self.catalog)
        self.report["startup_s"] = round(time.perf_counter() - t0, 3)
        d = Registry(self.root, self.catalog).dir
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"service.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"catalog": self.catalog, "url": self.url, "pid": os.getpid(), "started_unix_ns": time.time_ns(),
                                   "replay": self.report}, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, d / "service.json")
        return self

    @property
    def url(self) -> str:
        if self.server is None:
            raise RuntimeError("catalog service is not running")
        return self.server.url()

    def client(self):
        if self.server is None:
            raise RuntimeError("catalog service is not running")
        return self.server.client()

    def shutdown(self) -> None:
        if self.server is not None and self.server.is_running():
            self.server.shutdown()
        self.server = None

    def __enter__(self) -> CatalogService:
        return self.start() if self.server is None else self

    def __exit__(self, *exc) -> None:
        self.shutdown()

    def serve_forever(self) -> None:
        """阻塞到 SIGTERM / SIGINT（容器停止）。"""
        stop = threading.Event()
        for s in (signal.SIGTERM, signal.SIGINT):
            signal.signal(s, lambda *_: stop.set())
        while not stop.wait(1.0):
            if self.server is None or not self.server.is_running():
                break
        self.shutdown()


def live_client(root: Path, catalog: str, url: str | None = None, token: str | None = None):
    """连接在线 catalog：`url` 缺省取 `<root>/<catalog>/service.json`，此时服务未启动或不可达返回 None（注册只写清单，启动时
    重放）；显式给出的 `url` 不可达时抛出异常。"""
    explicit = url is not None
    if url is None:
        p = Registry(Path(root), catalog).dir / "service.json"
        if not p.is_file():
            return None
        url = json.loads(p.read_text(encoding="utf-8")).get("url")
        if not url:
            return None
    from rerun.catalog import CatalogClient

    try:
        c = CatalogClient(url, token=token)
        c.dataset_names()
    except Exception:                                 # 服务不可达：只写清单
        if explicit:
            raise
        return None
    return c
