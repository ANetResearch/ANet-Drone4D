"""Sandbox REST v1 endpoint table SB-00 to SB-78 and its design OpenAPI 3.1 document (AWR-17 §17.5; AWR-04 §11.1; API-AC-046).

The table is the frozen route list of `/api/sandbox/v1` (WP-01, D2-MS1). `gen_openapi.py --app sandbox` writes
packages/contracts/rest/sandbox.openapi.snapshot.json from it while the sandbox-api is not importable; once
`awr.api.sandbox_main.openapi_for_snapshot()` exists (M11, WP-03) the snapshot is exported from the live app, the same mechanism as
the D1 snapshot, and tests/contracts/test_sandbox_openapi.py keeps checking that every route of this table is present.

Request and response bodies reference the contract schemas by their `$id` (https://schemas.anet-drone.dev/...), so the document
does not copy field definitions. Not importable as a package (tools/ has no __init__.py); scripts add this directory to sys.path.
"""

from __future__ import annotations

import re
from typing import NamedTuple

PREFIX = "/api/sandbox/v1"
HOST = "https://schemas.anet-drone.dev"


class Endpoint(NamedTuple):
    sb: str                 # SB-00 ... SB-78 (SB-55 has one row per action: SB-55.start ...)
    method: str
    path: str               # relative to PREFIX
    summary: str
    auth: str               # public | ticket | session | body_token
    idem: str               # yes | key | cid | no
    rate: str               # pub | create | ticket | rest | entity | task | clock | call | -
    owner: str              # rest/<domain>.py (module) or sandbox-api entry
    priority: str           # P0 | P1
    request: str | None     # schema ref (domain/name[#/$defs/x]) or None
    responses: dict          # {status: schema ref or None}
    query: tuple = ()        # query parameter names


def S(rel: str) -> str:
    """'sandbox/session#/$defs/meta' -> absolute schema URI."""
    path, _, frag = rel.partition("#")
    dom, name = path.split("/")
    return f"{HOST}/{dom}/1/{name}.schema.json" + (f"#{frag}" if frag else "")


SESSION = "sandbox/session"
CALL = SESSION + "#/$defs/call_result"
ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("SB-00", "get", "/meta", "接口版本、契约版本、访问模式、会话与队列概况", "public", "yes", "pub", "rest/sandbox.py (M17)", "P0", None,
             {200: SESSION + "#/$defs/meta"}),
    Endpoint("SB-01", "get", "/worlds", "七个世界的可用性与准入", "public", "yes", "pub", "rest/sandbox.py (M17)", "P0", None, {200: SESSION + "#/$defs/worlds"}),
    Endpoint("SB-02", "get", "/templates", "场景模板", "public", "yes", "pub", "rest/sandbox.py (M17)", "P0", None, {200: SESSION + "#/$defs/templates"},
             ("world",)),
    Endpoint("SB-03", "get", "/challenge", "领取工作量证明题目", "public", "no", "create", "rest/sandbox.py (M17)", "P0", None,
             {200: SESSION + "#/$defs/challenge"}),
    Endpoint("SB-04", "post", "/sessions", "创建、排队或幂等返回", "public", "key", "create", "rest/sandbox.py (M17)", "P0", SESSION + "#/$defs/create_request",
             {201: SESSION + "#/$defs/created", 200: SESSION + "#/$defs/created", 202: SESSION + "#/$defs/queued", 409: None, 429: None}),
    Endpoint("SB-05", "get", "/queue/{ticket_id}", "排队状态长轮询", "ticket", "yes", "ticket", "rest/sandbox.py (M17)", "P0", None,
             {200: "sandbox/ticket", 410: None}, ("wait_s",)),
    Endpoint("SB-06", "post", "/queue/{ticket_id}/claim", "领取空位", "ticket", "yes", "ticket", "rest/sandbox.py (M17)", "P0", None,
             {201: SESSION + "#/$defs/created", 409: None, 410: None}),
    Endpoint("SB-07", "delete", "/queue/{ticket_id}", "离开队列", "ticket", "yes", "ticket", "rest/sandbox.py (M17)", "P0", None, {204: None}),
    Endpoint("SB-08", "get", "/sessions/{sid}", "会话对象（可长轮询到指定状态）", "session", "yes", "rest", "rest/sandbox.py (M17)", "P0", None,
             {200: SESSION}, ("wait_state", "wait_s")),
    Endpoint("SB-09", "delete", "/sessions/{sid}", "结束会话", "session", "yes", "rest", "rest/sandbox.py (M17)", "P0", None, {202: None}),
    Endpoint("SB-10", "post", "/sessions/{sid}/keepalive", "续期并换发 token", "session", "yes", "rest", "rest/sandbox.py (M17)", "P0", None,
             {200: SESSION + "#/$defs/keepalive_reply"}),
    Endpoint("SB-11", "get", "/sessions/{sid}/snapshot", "导出配置快照（结束后 10 min 内可用）", "session", "yes", "rest", "rest/sandbox.py (M17)", "P0", None,
             {200: "sandbox/snapshot", 410: None}),
    Endpoint("SB-12", "post", "/sessions/{sid}/restore", "导入配置快照（同一世界）", "session", "key", "entity", "rest/sandbox.py (M17)", "P0",
             SESSION + "#/$defs/restore_request", {202: SESSION, 422: None}),
    Endpoint("SB-13", "post", "/sessions/{sid}/template", "载入场景模板", "session", "key", "entity", "rest/sandbox.py (M17)", "P0",
             SESSION + "#/$defs/template_request", {202: SESSION}),
    Endpoint("SB-14", "post", "/sessions/{sid}/world", "切换底图", "session", "key", "entity", "rest/sandbox.py (M17)", "P0",
             SESSION + "#/$defs/world_request", {202: SESSION, 409: None}),
    Endpoint("SB-15", "post", "/sessions/{sid}/clock", "播放、暂停、倍速（经治理器）、单步", "session", "cid", "clock", "rest/sandbox.py (M17)", "P0",
             SESSION + "#/$defs/clock_request", {200: CALL}),
    Endpoint("SB-16", "get", "/sessions/{sid}/events", "事件补拉", "session", "yes", "rest", "rest/sandbox.py (M17)", "P0", None,
             {200: SESSION + "#/$defs/events_page", 410: None}, ("since_seq", "since", "limit", "types", "level_min")),
    Endpoint("SB-17", "post", "/sessions/{sid}/end-beacon", "关闭页面时结束会话（token 在请求体）", "body_token", "yes", "rest", "rest/sandbox.py (M17)", "P1",
             SESSION + "#/$defs/end_beacon", {204: None}),
    Endpoint("SB-18", "post", "/sessions/{sid}/batch", "批处理评估（本机与局域网；公开站 404）", "session", "key", "task", "rest/sandbox.py (M17)", "P0",
             SESSION + "#/$defs/batch_request", {202: SESSION + "#/$defs/batch_status", 404: None}),
    Endpoint("SB-19", "get", "/sessions/{sid}/batch/{batch_id}", "批处理进度与结果", "session", "yes", "rest", "rest/sandbox.py (M17)", "P0", None,
             {200: SESSION + "#/$defs/batch_status"}),
    Endpoint("SB-20", "get", "/openapi.json", "沙盒 OpenAPI 3.1", "public", "yes", "pub", "sandbox-api entry (M11)", "P0", None, {200: None}),
    Endpoint("SB-21", "get", "/catalog/models", "机型目录（预置）", "public", "yes", "pub", "rest/catalog.py (M21)", "P0", None,
             {200: "vehicle/catalog#/$defs/models"}),
    Endpoint("SB-22", "get", "/catalog/models/{model_id}", "机型完整参数、参数表与派生量", "public", "yes", "pub", "rest/catalog.py (M21)", "P0", None,
             {200: "vehicle/catalog#/$defs/model_detail", 404: None}),
    Endpoint("SB-23", "get", "/catalog/sensors", "传感器目录", "public", "yes", "pub", "rest/catalog.py (M21)", "P0", None, {200: "vehicle/catalog#/$defs/sensors"}),
    Endpoint("SB-24", "get", "/sessions/{sid}/models", "本会话克隆机型", "session", "yes", "rest", "rest/catalog.py (M21)", "P0", None,
             {200: "vehicle/catalog#/$defs/models"}),
    Endpoint("SB-25", "post", "/sessions/{sid}/models", "克隆并编辑机型", "session", "key", "entity", "rest/catalog.py (M21)", "P0",
             "vehicle/clone#/$defs/rest_request", {201: "vehicle/catalog#/$defs/model_detail", 422: None}),
    Endpoint("SB-26", "patch", "/sessions/{sid}/models/{model_id}", "修改克隆机型", "session", "yes", "entity", "rest/catalog.py (M21)", "P0",
             "vehicle/clone#/$defs/rest_request", {200: "vehicle/catalog#/$defs/model_detail", 409: None, 422: None}),
    Endpoint("SB-27", "delete", "/sessions/{sid}/models/{model_id}", "删除克隆机型", "session", "yes", "entity", "rest/catalog.py (M21)", "P0", None, {204: None}),
    Endpoint("SB-28", "get", "/sessions/{sid}/vehicles", "机队实例列表", "session", "yes", "rest", "rest/fleet.py (M08)", "P0", None, {200: None}),
    Endpoint("SB-29", "post", "/sessions/{sid}/vehicles", "添加无人机", "session", "key", "entity", "rest/fleet.py (M08)", "P0",
             "vehicle/instance#/$defs/create_request", {201: "vehicle/instance#/$defs/created"}),
    Endpoint("SB-30", "get", "/sessions/{sid}/vehicles/{vid}", "实例与状态快照", "session", "yes", "rest", "rest/fleet.py (M08)", "P0", None,
             {200: "vehicle/instance"}),
    Endpoint("SB-31", "patch", "/sessions/{sid}/vehicles/{vid}", "修改标签、机巢、挂载、限速档", "session", "yes", "entity", "rest/fleet.py (M08)", "P0",
             "vehicle/instance#/$defs/patch", {200: "vehicle/instance", 409: None}),
    Endpoint("SB-32", "delete", "/sessions/{sid}/vehicles/{vid}", "移除", "session", "yes", "entity", "rest/fleet.py (M08)", "P0", None, {202: None}),
    Endpoint("SB-33", "get", "/sessions/{sid}/vehicles/{vid}/state", "全部状态", "session", "yes", "rest", "rest/fleet.py (M08)", "P0", None,
             {200: "vehicle/instance#/$defs/state"}),
    Endpoint("SB-34", "get", "/sessions/{sid}/vehicles/{vid}/sensors", "挂载、规格、工作状态、云台与变焦", "session", "yes", "rest", "rest/fleet.py (M08)", "P0",
             None, {200: "vehicle/instance#/$defs/sensors"}),
    Endpoint("SB-35", "post", "/sessions/{sid}/vehicles/{vid}/commands", "发起命令（同 WS call）", "session", "cid", "call", "rest/fleet.py (M08)", "P0",
             "vehicle/instance#/$defs/command_request", {200: CALL}),
    Endpoint("SB-36", "post", "/sessions/{sid}/vehicles/{vid}:acquire", "申请租约", "session", "cid", "call", "rest/fleet.py (M08)", "P0",
             "vehicle/instance#/$defs/lease_request", {200: CALL}),
    Endpoint("SB-37", "post", "/sessions/{sid}/vehicles/{vid}:release", "释放租约", "session", "cid", "call", "rest/fleet.py (M08)", "P0",
             "vehicle/instance#/$defs/lease_request", {200: CALL}),
    Endpoint("SB-38", "get", "/sessions/{sid}/vehicles/{vid}/perception", "当前感知结果", "session", "yes", "rest", "rest/perception.py (M19)", "P0", None,
             {200: "sensor/perception"}),
    Endpoint("SB-39", "get", "/sessions/{sid}/detections", "检测历史补拉", "session", "yes", "rest", "rest/perception.py (M19)", "P0", None,
             {200: "sensor/detection#/$defs/page", 410: None}, ("since_seq", "uav", "target", "sensor", "level_min", "limit")),
    Endpoint("SB-40", "get", "/sessions/{sid}/targets", "识别物列表", "session", "yes", "rest", "rest/targets.py (M18)", "P0", None,
             {200: "target/target#/$defs/list"}),
    Endpoint("SB-41", "post", "/sessions/{sid}/targets", "新增识别物", "session", "key", "entity", "rest/targets.py (M18)", "P0",
             "target/target#/$defs/create_request", {201: "target/target#/$defs/created", 422: None}),
    Endpoint("SB-42", "get", "/sessions/{sid}/targets/{id}", "识别物完整配置", "session", "yes", "rest", "rest/targets.py (M18)", "P0", None,
             {200: "target/target"}),
    Endpoint("SB-43", "patch", "/sessions/{sid}/targets/{id}", "修改识别物（JSON Merge Patch）", "session", "yes", "entity", "rest/targets.py (M18)", "P0",
             "target/target", {200: "target/target", 422: None}),
    Endpoint("SB-44", "delete", "/sessions/{sid}/targets/{id}", "删除识别物", "session", "yes", "entity", "rest/targets.py (M18)", "P0", None, {204: None}),
    Endpoint("SB-45", "get", "/sessions/{sid}/targets/{id}/state", "感知态、有效敏感体、计数、声源相位", "session", "yes", "rest", "rest/targets.py (M18)", "P0",
             None, {200: "rt/target_detail"}),
    Endpoint("SB-46", "get", "/sessions/{sid}/targets/{id}/suggested_radius", "physical 模型的建议半径", "session", "yes", "rest", "rest/targets.py (M18)",
             "P0", None, {200: "target/target#/$defs/suggested_radius"}, ("model_id", "light", "bg")),
    Endpoint("SB-47", "get", "/sessions/{sid}/nests", "机巢列表", "session", "yes", "rest", "rest/tasking.py (M20)", "P0", None,
             {200: "tasking/nest#/$defs/list"}),
    Endpoint("SB-48", "post", "/sessions/{sid}/nests", "新增机巢（可同时放入无人机）", "session", "key", "entity", "rest/tasking.py (M20)", "P0",
             "tasking/nest", {201: "tasking/nest"}),
    Endpoint("SB-49", "get", "/sessions/{sid}/nests/{id}", "机巢配置与状态", "session", "yes", "rest", "rest/tasking.py (M20)", "P0", None,
             {200: "tasking/nest"}),
    Endpoint("SB-50", "patch", "/sessions/{sid}/nests/{id}", "修改机巢", "session", "yes", "entity", "rest/tasking.py (M20)", "P0", "tasking/nest",
             {200: "tasking/nest", 422: None}),
    Endpoint("SB-51", "delete", "/sessions/{sid}/nests/{id}", "删除机巢", "session", "yes", "entity", "rest/tasking.py (M20)", "P0", None,
             {204: None, 409: None}),
    Endpoint("SB-52", "get", "/sessions/{sid}/tasks", "任务列表", "session", "yes", "rest", "rest/tasking.py (M20)", "P0", None,
             {200: "tasking/task#/$defs/list"}),
    Endpoint("SB-53", "post", "/sessions/{sid}/tasks", "创建任务或预览分配（dry-run）", "session", "key", "task", "rest/tasking.py (M20)", "P0",
             "tasking/task#/$defs/request", {201: "tasking/task", 200: "tasking/task#/$defs/preview", 202: None, 422: None}, ("dry_run",)),
    Endpoint("SB-54", "get", "/sessions/{sid}/tasks/{tid}", "任务、状态与计划几何", "session", "yes", "rest", "rest/tasking.py (M20)", "P0", None,
             {200: "tasking/task"}, ("include",)),
    *(Endpoint(f"SB-55.{op}", "post", f"/sessions/{{sid}}/tasks/{{tid}}:{op}", f"任务控制 {op}", "session", "cid", "call", "rest/tasking.py (M20)", "P0",
               None, {200: CALL}) for op in ("start", "pause", "resume", "cancel")),
    Endpoint("SB-56", "get", "/sessions/{sid}/tasks/{tid}/sustainability", "7x24 可持续性评估", "session", "yes", "task", "rest/tasking.py (M20)", "P0", None,
             {200: "tasking/sustainability"}),
    Endpoint("SB-57", "get", "/sessions/{sid}/tasking/snapshot", "Allocator 快照", "session", "yes", "task", "rest/tasking.py (M20)", "P0", None,
             {200: "tasking/snapshot"}),
    Endpoint("SB-58", "post", "/sessions/{sid}/tasking/plans", "提交外部分配计划", "session", "key", "task", "rest/tasking.py (M20)", "P0", "tasking/plan",
             {200: "tasking/plan#/$defs/reply", 409: None, 422: None}),
    Endpoint("SB-59", "get", "/sessions/{sid}/relay/{tid}/stats", "接力统计", "session", "yes", "rest", "rest/tasking.py (M20)", "P0", None,
             {200: "tasking/relay_stats"}),
    Endpoint("SB-60", "get", "/sessions/{sid}/env/state", "EnvKeyframe（含日历、太阳、照度档）", "session", "yes", "rest", "rest/env.py (M07)", "P0", None,
             {200: "env/env_state"}),
    Endpoint("SB-61", "post", "/sessions/{sid}/env/preset", "天气预设（同 call env/preset）", "session", "cid", "clock", "rest/env.py (M07)", "P0", None,
             {200: CALL}),
    Endpoint("SB-62", "post", "/sessions/{sid}/env/calendar", "仿真日历（call env/calendar）", "session", "cid", "clock", "rest/env.py (M07)", "P0", None,
             {200: CALL}),
    Endpoint("SB-63", "post", "/sessions/{sid}/env/query", "环境场点查询", "session", "yes", "rest", "rest/env.py (M07)", "P0", None, {200: None}),
    Endpoint("SB-64", "get", "/sessions/{sid}/rt", "沙盒实时连接（WebSocket Upgrade，子协议 awr.rt.v1 + bearer.<token>）", "session", "no", "-",
             "sandbox-api rt/hub.py (M11)", "P0", None, {101: None}),
    Endpoint("SB-65", "get", "/target_classes", "识别物类别模板", "public", "yes", "pub", "rest/targets.py (M18)", "P1", None,
             {200: "target/class_defaults"}),
    Endpoint("SB-66", "get", "/sessions/{sid}/tracks", "识别物航迹（不外露真值）", "session", "yes", "rest", "rest/perception.py (M19)", "P1", None,
             {200: "sensor/perception#/$defs/tracks"}, ("target",)),
    Endpoint("SB-67", "post", "/sessions/{sid}/perception/feasibility", "服务端可行窗口查询", "session", "yes", "rest", "rest/perception.py (M19)", "P1",
             "sensor/perception#/$defs/feasibility_request", {200: "sensor/perception#/$defs/feasibility_reply", 422: None}),
    Endpoint("SB-68", "get", "/sessions/{sid}/tasks/previews/{preview_id}", "dry-run 排队超时后的异步取回", "session", "yes", "task", "rest/tasking.py (M20)",
             "P0", None, {200: "tasking/task#/$defs/preview", 202: None, 404: None}),
    # D2.1（AWR-17 §17.20；契约 1.2.0，D2-INT 合入 WP-04b 与 WP-19b 草案）
    Endpoint("SB-69", "post", "/sessions/{sid}/park", "节能驻留（checkpoint 落盘、释放槽位）", "session", "yes", "rest", "rest/sandbox.py (M17)", "P0",
             "sandbox/park#/$defs/park_request", {202: "sandbox/park#/$defs/park_reply", 200: "sandbox/park#/$defs/park_reply", 409: None}),
    Endpoint("SB-70", "post", "/sessions/{sid}/resume", "从驻留恢复（满员时恢复票）", "session", "yes", "create", "rest/sandbox.py (M17)", "P0", None,
             {201: "sandbox/park#/$defs/resume_created", 200: "sandbox/park#/$defs/resume_created", 202: "sandbox/park#/$defs/resume_queued",
              410: None}),
    Endpoint("SB-71", "get", "/capacity", "公开容量（载入期排队可见）", "public", "yes", "pub", "rest/sandbox.py (M17)", "P0", None,
             {200: "sandbox/capacity"}),
    Endpoint("SB-72", "post", "/sessions/{sid}/park-beacon", "驻留信标（pagehide，token 在请求体）", "body_token", "yes", "rest", "rest/sandbox.py (M17)",
             "P0", "sandbox/park#/$defs/park_beacon", {204: None}),
    Endpoint("SB-73", "get", "/sessions/{sid}/perception/quant", "逐对感知量化读数", "session", "yes", "rest", "rest/perception.py (M19)", "P0", None,
             {200: "rt/target_quant", 404: None}, ("target", "uav")),
    Endpoint("SB-74", "get", "/sessions/{sid}/perception/field/{kind}", "辐射场剖面", "session", "yes", "rest", "rest/perception.py (M19)", "P0", None,
             {200: "rt/field_profile", 422: None}),
    Endpoint("SB-75", "get", "/sessions/{sid}/perception/volumes", "识别物感知体表", "session", "yes", "rest", "rest/perception.py (M19)", "P0", None,
             {200: "rt/field_volumes"}),
    Endpoint("SB-76", "post", "/sessions/{sid}/pair-code", "指挥器配对码（P1）", "session", "yes", "rest", "rest/sandbox.py (M17)", "P1", None,
             {201: None}),
    Endpoint("SB-77", "post", "/sessions/{sid}/pair", "以配对码加入同一沙盒（P1）", "public", "yes", "create", "rest/sandbox.py (M17)", "P1", None,
             {201: None, 404: None, 410: None}),
    Endpoint("SB-78", "post", "/sessions/{sid}/perception/field/{kind}/samples", "场的显式采样点（对拍）", "session", "yes", "task",
             "rest/perception.py (M19)", "P1", None, {200: None, 422: None}),
)

ENV_REQ = {
    "SB-61": {"type": "object", "properties": {"name": {"type": "string"}, "duration_s": {"type": "number", "minimum": 0, "maximum": 600}}, "required": ["name"]},
    "SB-62": {"type": "object", "properties": {"start_utc": {"type": "string"}, "tz": {"type": "string"}, "jump": {"type": "boolean"}},
              "required": ["start_utc", "tz"]},
    "SB-63": {"type": "object", "properties": {"points": {"type": "array"}, "fields": {"type": "array"}}, "required": ["points"]},
}
STATUS_TEXT = {101: "Switching Protocols", 200: "OK", 201: "Created", 202: "Accepted", 204: "No Content", 404: "Not Found",
               409: "Conflict", 410: "Gone", 422: "Unprocessable Content", 429: "Too Many Requests"}
SECURITY = {"public": [], "ticket": [{"ticket": []}], "session": [{"sandbox": []}], "body_token": []}


def sb_ids() -> list[str]:
    """SB-00 ... SB-78 (SB-55 once)."""
    return sorted({e.sb.split(".")[0] for e in ENDPOINTS})


def route_keys() -> set[tuple[str, str]]:
    """(method, path relative to PREFIX) of every frozen route."""
    return {(e.method, e.path) for e in ENDPOINTS}


def _content(ref: str | None, problem: bool = False) -> dict:
    if problem:
        return {"application/problem+json": {"schema": {"$ref": f"{HOST}/rest/1/problem.schema.json"}}}
    return {"application/json": {"schema": {"$ref": S(ref)} if ref else {"type": "object"}}}


def design_openapi(contracts_version: str) -> dict:
    """OpenAPI 3.1 document of the frozen route table (paths relative to the server URL /api/sandbox/v1)."""
    paths: dict[str, dict] = {}
    for e in ENDPOINTS:
        params = [{"name": n, "in": "path", "required": True, "schema": {"type": "string"}}
                  for n in re.findall(r"\{(\w+)\}", e.path)]
        params += [{"name": q, "in": "query", "required": False, "schema": {"type": "string"}} for q in e.query]
        if e.idem == "key":
            params.append({"name": "Idempotency-Key", "in": "header", "required": False, "schema": {"type": "string"}})
        op = {"operationId": e.sb.replace(".", "_").replace("-", "_").lower(), "summary": e.summary, "tags": [e.owner.split(" ")[0]],
              "security": SECURITY[e.auth],
              "x-awr": {"sb": e.sb, "auth": e.auth, "idempotent": e.idem, "rate": e.rate, "owner": e.owner, "priority": e.priority}}
        if params:
            op["parameters"] = params
        if e.request or e.sb in ENV_REQ:
            schema = {"$ref": S(e.request)} if e.request else ENV_REQ[e.sb]
            ctype = "application/merge-patch+json" if e.method == "patch" else "application/json"
            op["requestBody"] = {"required": True, "content": {ctype: {"schema": schema}}}
        resp = {}
        for st, ref in sorted(e.responses.items()):
            r = {"description": STATUS_TEXT.get(st, str(st))}
            if st >= 400:
                r["content"] = _content(None, problem=True)
            elif st not in (101, 204) and (ref or st in (200, 201)):
                r["content"] = _content(ref)
            resp[str(st)] = r
        resp["default"] = {"description": "problem+json with a reason code (rt/reasons.json; codes 500-599 are not HTTP 5xx)",
                           "content": _content(None, problem=True)}
        op["responses"] = resp
        paths.setdefault(e.path, {})[e.method] = op
    return {
        "openapi": "3.1.0",
        "info": {"title": "AWR sandbox REST v1", "version": "1",
                 "description": "Design snapshot of /api/sandbox/v1 (AWR-17 §17.5, SB-00 to SB-68). Generated from tools/contracts/sandbox_api.py "
                                "until the sandbox-api exports its live schema.",
                 "x-awr-source": "design", "x-awr-contracts": contracts_version},
        "servers": [{"url": PREFIX}],
        "components": {"securitySchemes": {
            "sandbox": {"type": "http", "scheme": "bearer", "description": "sandbox token v1.<payload>.<sig> (scope sandbox, sid = path sid)"},
            "ticket": {"type": "http", "scheme": "bearer", "description": "queue ticket token q1.<...> (queue endpoints only)"}}},
        "paths": dict(sorted(paths.items())),
    }
