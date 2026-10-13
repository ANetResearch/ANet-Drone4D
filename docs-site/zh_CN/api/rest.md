# 开放接口

ANet Drone4D 对外提供两组 HTTP 接口与一个实时协议：

| 接口 | 前缀 | 监听 | 面向 |
|---|---|---|---|
| 沙盒 REST v1 | `/api/sandbox/v1` | sandbox-api（本机 8002；公开实例经 nginx 按路径分流） | 访客、脚本与外部智能体：创建并操作自己的沙盒 |
| 本机运行时接口 | `/api` | api（本机 8000） | 本机共享展示的操作员与运维：世界、机队、命令、剧本、运行与系统 |
| 实时协议 `awr.rt.v1` | WebSocket | 两个 api 均提供 | 状态流、命令调用、速度设定点与事件 |

界面与接口同权：浏览器中的每个操作都对应同一套接口，经过相同的准入与安全检查。沙盒接口的 OpenAPI 3.1 文档由服务端在
`/api/sandbox/v1/openapi.json` 提供，公开实例在 [`/sandbox/api`](https://drone4d.agentnetwork.org.cn/sandbox/api) 页面渲染并附可复制的示例。
Python 程序建议使用 {doc}`sdk`，它处理排队、工作量证明、续期、节能恢复与二进制帧解码。

## 鉴权

| 方式 | 适用 | 说明 |
|---|---|---|
| 公开 | `meta`、`worlds`、`templates`、`capacity`、机型与传感器目录、`challenge` 等 | 无需凭据，可缓存 |
| 会话令牌 | `/sessions/{sid}/...` | 创建会话时签发，`Authorization: Bearer v1.<payload>.<sig>`，只对该 sid 有效；续期时换发 |
| 排队票 | `/queue/{ticket_id}...` | 进入排队时签发，`Authorization: Bearer q1.<...>`，只用于排队端点 |
| 请求体令牌 | `end-beacon`、`park-beacon` | 页面关闭时由浏览器以信标发送，令牌放在请求体中 |
| 管理令牌 | 本机运行时接口的受保护端点 | 每次运行生成于 `runs/<run>/admin.token`（0600），经 `POST /api/auth/token` 换取访问令牌 |

公开模式下，创建会话可能要求工作量证明：`GET /challenge` 取题，求解后随创建请求提交；需要时服务端以原因码 509 返回题目。

## 错误格式

错误以 `application/problem+json` 返回，`code` 为项目原因码（`packages/contracts/rt/reasons.json`）；沙盒专用原因码在 500 至 599 段，
与 HTTP 5xx 无关。以下为本机共享展示对未带令牌请求 `GET /api/worlds` 的真实响应：

```text
{"type":"urn:awr:reason:301","title":"AUTH_REQUIRED","status":401,"code":301,"name":"AUTH_REQUIRED","reason":"AUTH_REQUIRED","message":"缺少 token","detail":null,"remedy":"先签发 token","request_id":"01a135ae-3540-7d01-88af-813820ac3b51"}
```

| 字段 | 含义 |
|---|---|
| `code`、`name` | 原因码与名称，例如 110 `PARAM_OUT_OF_RANGE`、506 `SANDBOX_GONE`、525 `NEST_INCOMPATIBLE`、581 `NO_FEASIBLE_STATION`、585 `PLAN_INPUT_TOO_LARGE` |
| `message` | 面向人的说明 |
| `detail` | 结构化细节，例如 `{"field": "nest_id|home_enu_m", "why": "EXACTLY_ONE"}` |
| `remedy` | 建议的处理方式 |
| `retry_after_ms` | 限流时的精确等待（毫秒）；同时提供 `Retry-After` 头（整秒） |
| `request_id` | 请求编号，报告问题时请附上 |

## 幂等与限流

创建类请求（会话、无人机、识别物、机巢、任务）接受 `Idempotency-Key` 头，同一键的重试返回同一结果；SDK 自动生成。公开模式下各类请求按会话
使用令牌桶限流：

| 类别 | 速率 | 突发 |
|---|---|---|
| 一般 REST | 10 次/s | 30 |
| 命令调用 | 20 次/s | 40 |
| 时钟 | 2 次/s | — |
| 实体增删改 | 5 次/s | — |
| 任务 | 1 次/s | — |
| 速度设定点 | 50 Hz | — |

超限返回 429 与原因码 111，SDK 按 `retry_after_ms` 自动重试。任务的输入规模另有配额，见 {doc}`../guide/sandbox`。

## 实时协议

沙盒的实时连接为 `GET /api/sandbox/v1/sessions/{sid}/rt` 上的 WebSocket，子协议为 `awr.rt.v1` 与 `bearer.<token>`。连接后客户端订阅主题
（例如会话状态 `sandbox/session`、机群状态与单机感知），服务端按每个客户端的兴趣集与配额发送：机群以 32 字节的 Lite32 记录、焦点无人机以
64 字节的 Full64 记录批量下发，结构化消息以 msgpack 编码。命令以 `call` 发起，依次返回受理、执行中与结果；速度控制在 `call` 之后经客户端数据通道
以至多 50 Hz 发送设定点。字节布局见设计文档
[docs/17 接口与实时协议规范](https://github.com/ANetResearch/ANet-Drone4D/blob/main/docs/17-接口与实时协议规范.md)。

会话结束时连接以关闭码 4410 关闭；节能驻留时以关闭码 4430 断开，客户端恢复会话后重新订阅；切换底图时以 1012 断开，客户端按新世界重连。

## 沙盒 REST v1 端点

下表由 `packages/contracts/rest/sandbox.openapi.snapshot.json` 在构建文档时生成。路径相对于前缀 `/api/sandbox/v1`；"优先级"中 P1 为可选能力，
未部署的端点返回 404。

```{drone4d-endpoints} packages/contracts/rest/sandbox.openapi.snapshot.json
:kind: sandbox
```

## 本机运行时接口端点

下表由 `packages/contracts/rest/openapi.snapshot.json` 生成。这些端点服务于本机共享展示（回环访问），除健康、就绪与版本信息外均需要令牌；
公开实例以只读方式提供其中与观看相关的部分。

```{drone4d-endpoints} packages/contracts/rest/openapi.snapshot.json
:kind: runtime
```
