"""daemon 配置生成（M14 §6.15；M14-FR-068；d05 §6 第 5、7、8 条、§7 第 12 条）。D1 为桩（P2），V0.2 转 P1。

规则：`service` 模块能力列表的 `url` 指向 agent-runtime 的能力 HTTP 端点（127.0.0.1:8790）；禁 auto-reply；不编译 `shell`；
v0.1 自建 hub 且 `guest_messages = 0`，v0.2 `inbound.policy = closed` 加机队 AID 白名单；身份目录 `runs/.anet/<world>/<vehicle>/`
（0700），绝不使用 `~/.anet`；hub 只接受回环或局域网地址（R-08）。端口：hub 18088、gcs 控制面 39811、机体 39812 起。
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

__all__ = ["CAP_ENDPOINT", "CONTROL_PORT_BASE", "HUB_PORT", "ConfigError", "check_config", "daemon_config", "hub_url_ok",
           "load_versions"]

HUB_PORT = 18088
CONTROL_PORT_BASE = 39811
CAP_ENDPOINT = "http://127.0.0.1:8790/anet/cap"


class ConfigError(ValueError):
    pass


def hub_url_ok(url: str) -> bool:
    """只接受回环或私有局域网地址（禁止公网 hub）。"""
    try:
        host = urlparse(url).hostname or ""
        if host in ("localhost",):
            return True
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def daemon_config(*, world_id: str, vehicle_id: str, aid_hint: str, capabilities: Iterable[str], index: int,
                  fleet_aids: Iterable[str], runs_root: Path = Path("runs"), hub_url: str = f"http://127.0.0.1:{HUB_PORT}",
                  long_running: Iterable[str] = ()) -> dict[str, Any]:
    if not hub_url_ok(hub_url):
        raise ConfigError(f"hub must be loopback or LAN: {hub_url}")
    data_dir = runs_root / ".anet" / world_id / vehicle_id
    lr = set(long_running)
    caps = [{"id": c, "url": f"{CAP_ENDPOINT}/{vehicle_id}/{c}", "long_running": c in lr} for c in capabilities]
    return {
        "data_dir": str(data_dir), "data_dir_mode": "0700", "aid_hint": aid_hint,
        "control": {"listen": f"127.0.0.1:{CONTROL_PORT_BASE + index}", "token_file": "control_token.txt"},
        "hub": {"url": hub_url, "guest_messages": 0},
        "relay": {"poll_s": 1.0},
        "inbound": {"policy": "closed", "allow": sorted(set(fleet_aids))},
        "auto_reply": {"enabled": False},
        "build": {"tags": []},
        "modules": {"service": {"capabilities": caps}},
    }


def check_config(cfg: Mapping[str, Any]) -> list[str]:
    """安全检查：返回违规项（空列表为通过）。"""
    bad = []
    if (cfg.get("auto_reply") or {}).get("enabled"):
        bad.append("auto_reply enabled")
    if "shell" in ((cfg.get("build") or {}).get("tags") or []):
        bad.append("shell tag")
    inbound = cfg.get("inbound") or {}
    if inbound.get("policy") != "closed" or not inbound.get("allow"):
        bad.append("inbound not whitelisted")
    if not hub_url_ok(str((cfg.get("hub") or {}).get("url", ""))):
        bad.append("hub not loopback/LAN")
    if (cfg.get("hub") or {}).get("guest_messages", 1) != 0:
        bad.append("guest messages allowed")
    dd = str(cfg.get("data_dir", ""))
    if "~/.anet" in dd or dd.startswith(str(Path.home() / ".anet")) or "/.anet/" not in dd.replace("\\", "/"):
        bad.append("data_dir not under runs/.anet")
    ctl = str((cfg.get("control") or {}).get("listen", ""))
    if not ctl.startswith("127.0.0.1:"):
        bad.append("control plane not loopback")
    caps = ((cfg.get("modules") or {}).get("service") or {}).get("capabilities") or []
    bad.extend(f"capability {c.get('id')} endpoint not loopback" for c in caps
               if not str(c.get("url", "")).startswith("http://127.0.0.1:"))
    return bad


def load_versions(path: Path | None = None) -> dict[str, str]:
    """`tools/anet/versions.lock`（`key = value` 行，# 注释）。"""
    p = path or Path(__file__).resolve().parents[4] / "tools" / "anet" / "versions.lock"
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip()
        if "=" in s:
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def dump(cfg: Mapping[str, Any]) -> str:
    return json.dumps(cfg, indent=2, sort_keys=True)
