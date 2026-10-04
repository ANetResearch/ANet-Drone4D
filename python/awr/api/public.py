"""公开演示访问模式（`AWR_ACCESS_MODE=public`，AWR-17 §3.3 访问模式表第 3 行；ADR-082）的请求策略与客户端地址。

公开模式部署在反向代理之后（api 只监听回环），面向匿名访客：匿名只签发 viewer，operator 与 admin 必须提供部署时配置的
管理口令（`AWR_ADMIN_SECRET_FILE`，缺省未配置则一律拒绝）。本模块是纯函数，供 `middleware`、`rest/auth` 与 `rt/ws` 共用：

- `client_ip()`：对端地址属于可信代理（`AWR_TRUSTED_PROXIES`，IP 或 CIDR）时，自 `X-Forwarded-For` 从右向左取第一个不可信
  地址（缺失时取 `X-Real-IP`），否则就是对端地址。只用于限流键与日志、审计，绝不参与鉴权；
- `world_of_path()`：从 `/worlds/<id>/…`、`/api/worlds/<id>`、`/api/world/<id>/…` 取世界 id（`_shared` 共享资产不算世界），
  供世界白名单（`AWR_WORLDS_ALLOW`）判定；不在白名单的世界一律 404 `305`（静态与 REST 相同）；
- `public_policy()`：`/api/**` 的角色策略。演示站不提供的功能族（录制与回放、重建与任务、智能体、性能报告、rt 调试、
  进程表、OpenAPI）对 admin 以外的角色 404 `305`；写类请求（POST、PUT、PATCH、DELETE）对 operator 以下的角色只放行
  token 签发、`env/query` 与 world query 三个只读查询，其余 403 `115`；
- `public_categories()`：公开模式追加的按客户端地址限流类别（`ratelimit.CATEGORIES` 的 public_api、auth、world_query）。
"""

from __future__ import annotations

import contextlib
import ipaddress
import re
from collections.abc import Iterable

__all__ = ["HIDDEN_PREFIXES", "VIEWER_WRITES", "client_ip", "parse_networks", "public_categories", "public_policy",
           "world_of_path"]

Net = ipaddress.IPv4Network | ipaddress.IPv6Network

# 演示站不提供的功能族：admin 以外 404（录制与回放、重建与任务、智能体、性能报告、rt 调试、进程表、OpenAPI）
HIDDEN_PREFIXES = ("/api/runs", "/api/jobs", "/api/recon", "/api/agents", "/api/agent-tasks", "/api/sys/perf-report",
                   "/api/sys/perf-reports", "/api/sys/procs", "/api/sys/audit", "/api/rt/inspect", "/api/rt/topics",
                   "/api/openapi.json")
# operator 以下角色唯一允许的写类请求（都是只读查询或 token 签发）
VIEWER_WRITES = (re.compile(r"^/api/auth/token$"), re.compile(r"^/api/env/query$"),
                 re.compile(r"^/api/world/[a-z0-9-]{1,63}/query$"))
_WORLD_PATHS = (re.compile(r"^/worlds/([^/]+)/"), re.compile(r"^/api/worlds/([^/]+)/?$"),
                re.compile(r"^/api/worlds/([^/]+)/"), re.compile(r"^/api/world/([^/]+)/"))
_RANK = {"viewer": 0, "operator": 1, "admin": 2}
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def parse_networks(items: Iterable[str]) -> list[Net]:
    """`127.0.0.1`、`10.0.0.0/8`、`::1` → 网络列表；非法项忽略（配置校验在 runtime.config 中报错）。"""
    out: list[Net] = []
    for it in items:
        with contextlib.suppress(ValueError):
            out.append(ipaddress.ip_network(it.strip(), strict=False))
    return out


def _ip(s: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    s = s.strip()
    if s.startswith("[") and "]" in s:
        s = s[1:s.index("]")]
    elif s.count(":") == 1:  # IPv4:port
        s = s.split(":", 1)[0]
    try:
        return ipaddress.ip_address(s)
    except ValueError:
        return None


def _trusted(addr: ipaddress.IPv4Address | ipaddress.IPv6Address | None, nets: list[Net]) -> bool:
    return addr is not None and any(addr in n for n in nets)


def client_ip(peer: str | None, xff: str | None, x_real_ip: str | None, trusted: list[Net]) -> str:
    """客户端地址（限流与日志用）。对端不可信或未配置可信代理时就是对端地址；可信时按 X-Forwarded-For 自右向左取第一个
    不可信地址（全部可信时取最左一个），没有可解析的转发头时取 X-Real-IP，仍没有则为对端地址。"""
    p = peer or "?"
    if not trusted or not _trusted(_ip(p), trusted):
        return p
    if xff:
        hops = [h for h in (x.strip() for x in xff.split(",")) if h]
        parsed = [(h, _ip(h)) for h in hops]
        for _h, a in reversed(parsed):
            if a is None:
                break  # 不可解析的条目之前的内容不可信（可能由客户端伪造）
            if not _trusted(a, trusted):
                return str(a)
        valid = [a for _h, a in parsed if a is not None]
        if valid and all(_trusted(a, trusted) for a in valid):
            return str(valid[0])
    if x_real_ip:
        a = _ip(x_real_ip)
        if a is not None:
            return str(a)
    return p


def world_of_path(path: str) -> str | None:
    for rx in _WORLD_PATHS:
        m = rx.match(path)
        if m:
            wid = m.group(1)
            return None if wid == "_shared" else wid
    return None


def public_policy(method: str, path: str, role: str | None) -> tuple[int, int] | None:
    """`/api/**` 在公开模式下的策略：返回 (HTTP 状态, 原因码) 表示拒绝，None 表示交给路由（路由依赖仍照常校验角色）。"""
    rank = _RANK.get(role or "", -1)
    if rank < _RANK["admin"] and any(path == p or path.startswith(p + "/") for p in HIDDEN_PREFIXES):
        return 404, 305
    if method.upper() in WRITE_METHODS and rank < _RANK["operator"] and not any(rx.match(path) for rx in VIEWER_WRITES):
        return 403, 115
    return None


def public_categories(method: str, path: str) -> list[str]:
    """公开模式按客户端地址追加的限流类别：全部 `/api/**` 计 public_api；token 签发另计 auth；world query 另计 world_query。
    健康检查（反向代理与 systemd 探活）不计。"""
    if path.startswith("/api/health/"):
        return []
    cats = ["public_api"]
    m = method.upper()
    if m == "POST" and path == "/api/auth/token":
        cats.append("auth")
    elif m == "POST" and path.startswith("/api/world/") and path.endswith("/query"):
        cats.append("world_query")
    return cats
