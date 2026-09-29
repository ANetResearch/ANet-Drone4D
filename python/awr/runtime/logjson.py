"""JSON 行日志（AWR-19 §11.1；configs/logging.yaml）。

各进程只向 stderr 写 JSON 行，落盘与轮转由 supervisor 负责。字段：`t_wall_ns`、`t_mono_ns`、`lvl`、`proc`、`pid`、`run`、
`logger`、`msg`（固定模板文本）、可选 `t_sim_ns`、`epoch`、`kv`、`rep`。规则：
1. 不阻塞：配置中 `non_blocking.queue_handler_procs` 列出的进程（sim-core、api）用 QueueHandler + 后台线程输出；
   队列满时丢弃 DEBUG 与 INFO 并计数，WARN 以上保留。
2. 限速：同一 (logger, msg) 每秒至多 1 条，其余累加到下一条的 `rep`。
3. 掩码：键名匹配 secret、admin_secret、token、authorization、*_key 的值，以及以 `bearer.` 开头的字符串，一律写为 `***`。
4. 级别：`AWR_LOG_LEVEL` 覆盖 level.default；按 logger 名前缀覆盖（最长前缀优先）。

用法：`logger.info("固定文本", extra={"kv": {"cid": cid}})`；变量一律放 kv，不拼进 msg。
"""

from __future__ import annotations

import contextlib
import copy
import fnmatch
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

__all__ = ["JsonFormatter", "load_logging_config", "mask", "setup_logging"]

_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARN": logging.WARNING, "WARNING": logging.WARNING,
           "ERROR": logging.ERROR, "FATAL": logging.CRITICAL, "CRITICAL": logging.CRITICAL}
_LVL_NAME = {logging.DEBUG: "DEBUG", logging.INFO: "INFO", logging.WARNING: "WARN", logging.ERROR: "ERROR",
             logging.CRITICAL: "FATAL"}

_DEFAULTS: dict[str, Any] = {
    "level": {"default": "INFO"},
    "loggers": {"awr": "INFO", "zenoh": "WARN", "uvicorn.error": "INFO", "uvicorn.access": "WARN", "asyncio": "WARN",
                "numba": "WARN", "httpx": "WARN"},
    "rate_limit": {"max_per_s": 1},
    "non_blocking": {"queue_handler_procs": ["sim-core", "api"], "queue_max_records": 10000},
    "mask": {"keys": ["secret", "admin_secret", "token", "authorization", "*_key"], "value_prefixes": ["bearer."],
             "replacement": "***"},
}

_ROOT = Path(__file__).resolve().parents[3]


def load_logging_config(path: Path | None = None) -> dict[str, Any]:
    """读取 configs/logging.yaml（M00）；缺失或缺键时用同值内置默认。"""
    cfg: dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _DEFAULTS.items()}
    p = path or Path(os.environ.get("AWR_LOGGING_CONFIG", _ROOT / "configs" / "logging.yaml"))
    try:
        import yaml

        data = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, ImportError):
        data = {}
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = {**cfg[k], **v}
        else:
            cfg[k] = v
    return cfg


def _key_masked(key: str, patterns: list[str]) -> bool:
    k = key.lower()
    return any(fnmatch.fnmatchcase(k, p) for p in patterns)


def mask(obj: Any, keys: list[str] | None = None, prefixes: list[str] | None = None, repl: str = "***") -> Any:
    """递归掩码：键名匹配或字符串以 bearer. 开头时替换为 ***（OPS-FR-030）。"""
    keys = _DEFAULTS["mask"]["keys"] if keys is None else keys
    prefixes = _DEFAULTS["mask"]["value_prefixes"] if prefixes is None else prefixes
    if isinstance(obj, dict):
        return {k: (repl if isinstance(k, str) and _key_masked(k, keys) else mask(v, keys, prefixes, repl))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask(v, keys, prefixes, repl) for v in obj]
    if isinstance(obj, str) and any(obj.lower().startswith(p) for p in prefixes):
        return repl
    return obj


class _RateLimit(logging.Filter):
    """同一 (logger, msg) 每秒至多 max_per_s 条；被抑制的条数累加到下一条的 rep。"""

    def __init__(self, max_per_s: int = 1) -> None:
        super().__init__()
        self.max_per_s = max(1, int(max_per_s))
        self._state: dict[tuple[str, str], list] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.name, str(record.msg))
        now = time.monotonic()
        with self._lock:
            st = self._state.get(key)
            if st is None or now - st[0] >= 1.0:
                rep = st[2] if st is not None else 0
                self._state[key] = [now, 1, 0]
                if rep:
                    record.rep = rep
                if len(self._state) > 4096:
                    self._state.clear()
                return True
            if st[1] < self.max_per_s:
                st[1] += 1
                return True
            st[2] += 1
            return False


class JsonFormatter(logging.Formatter):
    def __init__(self, proc: str, run: str | None, cfg: dict[str, Any]) -> None:
        super().__init__()
        self.proc = proc
        self.run = run
        m = cfg.get("mask", _DEFAULTS["mask"])
        self._mkeys = list(m.get("keys", []))
        self._mpre = list(m.get("value_prefixes", []))
        self._mrepl = str(m.get("replacement", "***"))

    def format(self, record: logging.LogRecord) -> str:
        msg = record.msg if isinstance(record.msg, str) else str(record.msg)
        if record.args:
            with contextlib.suppress(TypeError, ValueError):
                msg = msg % record.args
        out: dict[str, Any] = {
            "t_wall_ns": time.time_ns(),
            "t_mono_ns": time.monotonic_ns(),
            "lvl": _LVL_NAME.get(record.levelno, record.levelname),
            "proc": self.proc,
            "pid": os.getpid(),
            "run": self.run,
            "logger": record.name,
            "msg": mask(msg, self._mkeys, self._mpre, self._mrepl),
        }
        for k in ("t_sim_ns", "epoch"):
            v = getattr(record, k, None)
            if v is not None:
                out[k] = v
        kv = getattr(record, "kv", None)
        if kv:
            out["kv"] = mask(dict(kv), self._mkeys, self._mpre, self._mrepl)
        rep = getattr(record, "rep", None)
        if rep:
            out["rep"] = rep
        if record.exc_info:
            out.setdefault("kv", {})["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            out.setdefault("kv", {})["exc"] = record.exc_text
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"), default=str)


class _DropQueueHandler(logging.handlers.QueueHandler):
    """队列满时丢弃 DEBUG 与 INFO 并计数，WARN 以上阻塞写入（不丢）。"""

    dropped = 0

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # 保留 msg 模板与 kv：只把 args 合并、异常转为文本（默认 prepare 会把整条格式化进 msg）
        r = copy.copy(record)
        if r.args:
            try:
                r.msg = str(r.msg) % r.args
            except (TypeError, ValueError):
                r.msg = str(r.msg)
            r.args = None
        if r.exc_info:
            r.exc_text = logging.Formatter().formatException(r.exc_info)
            r.exc_info = None
        return r

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            if record.levelno < logging.WARNING:
                _DropQueueHandler.dropped += 1
                return
            self.queue.put(record)


_listener: logging.handlers.QueueListener | None = None


def setup_logging(proc: str, run: str | None = None, *, stream=None, config_path: Path | None = None,
                  non_blocking: bool | None = None) -> logging.Logger:
    """配置根 logger：JSON 行写 stderr；返回 `awr` logger。可重复调用（替换已有处理器）。"""
    global _listener
    cfg = load_logging_config(config_path)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    if _listener is not None:
        _listener.stop()
        _listener = None
    default = os.environ.get("AWR_LOG_LEVEL") or cfg.get("level", {}).get("default", "INFO")
    root.setLevel(_LEVELS.get(str(default).upper(), logging.INFO))
    for name, lvl in (cfg.get("loggers") or {}).items():
        if name == "awr" and os.environ.get("AWR_LOG_LEVEL"):
            lvl = os.environ["AWR_LOG_LEVEL"]
        logging.getLogger(name).setLevel(_LEVELS.get(str(lvl).upper(), logging.INFO))
    sh = logging.StreamHandler(stream or sys.stderr)
    sh.setFormatter(JsonFormatter(proc, run, cfg))
    rl = _RateLimit((cfg.get("rate_limit") or {}).get("max_per_s", 1))
    nb = cfg.get("non_blocking") or {}
    if non_blocking is None:
        non_blocking = proc in (nb.get("queue_handler_procs") or [])
    if non_blocking:
        q: queue.Queue = queue.Queue(maxsize=int(nb.get("queue_max_records", 10000)))
        qh = _DropQueueHandler(q)
        qh.addFilter(rl)
        root.addHandler(qh)
        _listener = logging.handlers.QueueListener(q, sh, respect_handler_level=False)
        _listener.start()
    else:
        sh.addFilter(rl)
        root.addHandler(sh)
    logging.captureWarnings(True)
    return logging.getLogger("awr")


def shutdown_logging() -> None:
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None
