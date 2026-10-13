"""地址前缀与聚合前缀（M17-FR-012；17 §17.3.4；M17 PRD §6.6.7）。

客户端地址只由 sandbox-api 的可信代理规则得到（D1 `public.client_ip`），池收到的是文本地址或其前缀。IPv4 按 /32 与 /24，
IPv6 按 /64 与 /48（位数可配），IPv4-mapped IPv6 按 IPv4 计。状态文件与审计中前缀以 `HMAC-SHA256(K_state, prefix)` 的前
16 字节（十六进制）表示，不落明文。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress

__all__ = ["hash_prefix", "prefixes"]


def prefixes(addr: str | None, *, v4: int = 32, v6: int = 64, agg_v4: int = 24, agg_v6: int = 48) -> tuple[str, str]:
    """(前缀, 聚合前缀) 的网络文本形式；地址无法解析时按原文（两者相同）计，空地址为 `unknown`。

    输入可以是地址（`198.51.100.7`）或已经是网络（`2001:db8::/64`，sandbox-api 中间件写入的 `client_prefix`）。"""
    if not addr:
        return "unknown", "unknown"
    s = addr.strip()
    try:
        if "/" in s:
            net = ipaddress.ip_network(s, strict=False)
            a = net.network_address
        else:
            a = ipaddress.ip_address(s)
    except ValueError:
        return s, s
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    if a.version == 4:
        return (str(ipaddress.ip_network(f"{a}/{v4}", strict=False)), str(ipaddress.ip_network(f"{a}/{agg_v4}", strict=False)))
    return (str(ipaddress.ip_network(f"{a}/{v6}", strict=False)), str(ipaddress.ip_network(f"{a}/{agg_v6}", strict=False)))


def hash_prefix(k_state: bytes, prefix: str) -> str:
    """`HMAC-SHA256(K_state, prefix)[:16]` 的十六进制（32 个字符）。"""
    return hmac.new(k_state, prefix.encode("utf-8"), hashlib.sha256).digest()[:16].hex()
