"""场景模板目录与会话配置 `session.json` 的组装（M17-FR-009、FR-010；AWR-04 §10.4、ADR-112；17 §17.4.2、§17.6.1）。

模板文件 `scenarios/sandbox/<world>/<template>.json`（`awr.sandbox.template.v1`，M16 的 `tools/sandbox/gen_templates.py` 生成）：
配置部分与配置快照同构。池写给沙盒 sim-core 的 `session.json`（M08 `SessionConfig` 冷启动装配，`sim/reset{sandbox_config}`
同一形状）= 模板或快照的配置段 + `world_id`、`template_id`、`calendar`、`acoustic_bg`、`seed`、`limits`。

日历缺省：请求给出的 `calendar` > 模板 `calendar_default`（"当日 HH:MM"，世界时区）> 世界时区当日 10:00（17 §17.4.2）。
模板缺失时（M16 尚未交付该世界的模板）：显式请求的模板返回未知；缺省模板（`t5_area_guard`）退化为空场景并在日志中告警。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

__all__ = ["CONFIG_KEYS", "DEFAULT_TEMPLATE", "EMPTY_TEMPLATES", "TemplateCatalog", "build_session_config", "default_calendar"]

log = logging.getLogger("awr.sandbox.templates")

DEFAULT_TEMPLATE = "t5_area_guard"
EMPTY_TEMPLATES = ("empty",)
TEMPLATE_RE = re.compile(r"^t[1-9][0-9]*_[a-z0-9_]+$")
CONFIG_KEYS = ("acoustic_bg", "surface_class", "env", "models", "nests", "vehicles", "targets", "tasks")
ITEM_KEYS = ("template_id", "world_id", "name_zh", "summary_zh", "first_feedback_zh", "calendar_default", "recommended")


def default_calendar(tz: str | None, *, start_local: str = "10:00", now: float | None = None) -> dict[str, str]:
    """世界时区"当日 HH:MM" → `{start_utc, tz}`。"""
    zone = tz or "Asia/Shanghai"
    try:
        z = ZoneInfo(zone)
    except Exception:
        zone, z = "Asia/Shanghai", ZoneInfo("Asia/Shanghai")
    t = datetime.now(UTC) if now is None else datetime.fromtimestamp(now, UTC)
    local = t.astimezone(z)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", start_local or "10:00")
    hh, mm = (int(m.group(1)), int(m.group(2))) if m else (10, 0)
    loc = datetime(local.year, local.month, local.day, min(hh, 23), min(mm, 59), tzinfo=z)
    return {"start_utc": loc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "tz": zone}


class TemplateCatalog:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._cache: dict[tuple[str, str], tuple[float, dict]] = {}

    def ids(self, world: str) -> list[str]:
        d = self.root / world
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.json") if TEMPLATE_RE.match(p.stem))

    def get(self, world: str, tid: str) -> dict | None:
        if not TEMPLATE_RE.match(tid or "") or not re.fullmatch(r"[a-z0-9-]{1,63}", world or ""):
            return None
        p = self.root / world / f"{tid}.json"
        try:
            mt = p.stat().st_mtime
        except OSError:
            return None
        hit = self._cache.get((world, tid))
        if hit is not None and hit[0] == mt:
            return hit[1]
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("template unreadable", extra={"kv": {"path": str(p), "err": str(e)}})
            return None
        if not isinstance(d, dict) or d.get("world_id") not in (None, world):
            return None
        self._cache[(world, tid)] = (mt, d)
        return d

    def items(self, world: str | None, worlds: list[str]) -> list[dict[str, Any]]:
        """`GET /templates?world=` 的行（`session.schema.json#/$defs/template_item`）。"""
        out = []
        for w in [world] if world else worlds:
            for tid in self.ids(w):
                d = self.get(w, tid)
                if d is None:
                    continue
                it = {k: d[k] for k in ITEM_KEYS if k in d}
                it["template_id"], it["world_id"] = tid, w
                it.setdefault("name_zh", tid)
                it["entities"] = {k: len(d.get(k) or []) for k in ("vehicles", "targets", "nests", "tasks")}
                it.setdefault("recommended", tid == DEFAULT_TEMPLATE)
                out.append(it)
        return out


def build_session_config(world: str, *, template_id: str | None, template: dict | None, snapshot: dict | None,
                         calendar: dict | None, acoustic_bg: str | None, seed: int, limits: dict, tz: str | None,
                         now: float | None = None) -> dict[str, Any]:
    """`session.json`（`awr.sandbox.snapshot.v1` 同构 + `limits`）。"""
    src = snapshot if snapshot is not None else (template or {})
    doc: dict[str, Any] = {"schema": "awr.sandbox.snapshot.v1", "world_id": world,
                           "template_id": None if snapshot is not None else template_id}
    for k in CONFIG_KEYS:
        if k in src and src[k] is not None:
            doc[k] = src[k]
    for k in ("nests", "vehicles", "targets", "tasks"):
        doc.setdefault(k, [])
    if calendar:
        doc["calendar"] = {"start_utc": str(calendar["start_utc"]), "tz": str(calendar["tz"])}
    elif snapshot is not None and isinstance(snapshot.get("calendar"), dict):
        doc["calendar"] = dict(snapshot["calendar"])
    else:
        cd = (template or {}).get("calendar_default") or {}
        doc["calendar"] = default_calendar(cd.get("tz") or tz, start_local=cd.get("start_local") or "10:00", now=now)
    if acoustic_bg:
        doc["acoustic_bg"] = acoustic_bg
    doc["seed"] = int(seed)
    doc["limits"] = {"vehicles": int(limits["vehicles_max"]), "targets": int(limits["targets_max"]),
                     "clones": int(limits.get("cloned_models_max", 8)), "nests": int(limits["nests_max"]),
                     "tasks_active": int(limits["tasks_active_max"])}
    return doc
