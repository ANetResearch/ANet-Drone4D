"""R08 `GET /api/sessions/current`（AWR-17 §4.3.3；AWR-12 §3.3.4、§4.1.1；M11-FR-081）。

Session 实体由 api 已知信息合成：run、world（Catalog 摘要）、剧本 id（`AWR_SCENARIO`）、模式、会话状态（由环健康与
TIME.state 推导：未接上 → CREATED；停滞、下线、重启 → RECOVERING；熔断 → FAILED；STOPPED → READY；PLAYING、PAUSED、
STEPPING、LIVE → ACTIVE.*）、clock `{state, rate, t_sim_ns, epoch}`、segment、caps_clock（roster 中在场后端的 `caps.clock` 合成，FR-054）
与席位 `{state, holder_principal}`。`scenario_sha256` 与 `world_seed` 待 M10 剧本加载接入后填写（骨架为 null 与 "0"）。
R09 `POST /api/sessions` 为 ext。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from awr.contracts.enums import TimeState

from ..deps import Viewer, app_ctx
from ..problem import ApiProblem
from ..rt.clock import Health

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

_ACTIVE = {TimeState.PLAYING: "ACTIVE.PLAYING", TimeState.PAUSED: "ACTIVE.PAUSED",
           TimeState.STEPPING: "ACTIVE.STEPPING", TimeState.LIVE: "ACTIVE.LIVE", TimeState.STOPPED: "READY"}


def session_state(health: str, time_state: int) -> str:
    if health == Health.UNATTACHED:
        return "CREATED"
    if health == Health.FAILED:
        return "FAILED"
    if health != Health.OK:
        return "RECOVERING"
    if time_state & 0x80:
        return "REPLAY"
    return _ACTIVE.get(TimeState(time_state & 0x0F), "RECOVERING")


@router.get("/current")
async def current(request: Request, _p: Viewer) -> dict[str, Any]:
    ctx = app_ctx(request)
    gw = ctx.gateway
    if gw is None:
        raise ApiProblem(211, status=503)
    s = ctx.settings
    wi = gw.world_info
    c = gw.clock
    p = gw.source.p
    return {"run_id": s.run_id, "world_id": s.world_id, "content_version": wi.get("contentVersion"),
            "coordinate_sha256": wi.get("coordinateSha256"), "scenario_id": s.scenario_id, "scenario_sha256": None,
            "mode": gw.mode, "state": session_state(gw.last_health, c.state),
            "clock": {"state": TimeState(c.state & 0x0F).name, "rate": c.rate, "t_sim_ns": c.sim_now_ns(),
                      "epoch": c.epoch_u16},
            "world_seed": "0", "segment": int(p.segment or 0),
            "caps_clock": dict(gw.clock_caps),
            "keep": False, "seat": {"state": gw.seat.get("state", "FREE"), "holder_principal": gw.seat.get("holder")}}
