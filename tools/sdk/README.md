# awr_sandbox：ANet Drone4D 沙盒 Python SDK

单文件 SDK（`awr_sandbox.py`，版本 1.1.0，接口 `/api/sandbox/v1`），依赖 `httpx>=0.24`、`websockets>=12`、`msgpack>=1.0`，
支持 Python 3.9 至 3.13，不依赖本仓库的 `awr` 包（M17-FR-056；AWR-17 §17.13.1）。

```bash
pip install "httpx>=0.24" "websockets>=12" "msgpack>=1.0"
curl -O https://drone4d.agentnetwork.org.cn/sdk/awr_sandbox.py
python examples/01_quickstart.py https://drone4d.agentnetwork.org.cn
```

| 能力 | 调用 | 端点 |
|---|---|---|
| 世界、模板、概况 | `Sandbox.worlds()`、`templates(world)`、`meta()` | SB-00 至 SB-02 |
| 创建（自动工作量证明、排队长轮询与认领） | `Sandbox.create(world, template, ...)` | SB-03 至 SB-06、SB-08 |
| 续期、结束、快照、恢复、模板、底图 | `Session.keepalive/close/snapshot/restore/load_template/switch_world` | SB-09 至 SB-14 |
| 时钟（受限时返回授予值） | `Session.clock(op, rate=, ticks=)` | SB-15 |
| 事件补拉 | `Session.events(since_seq)` | SB-16 |
| 机队、识别物、机巢、任务、环境、外部分配器 | `Session.vehicles/targets/nests/tasks/env/tasking` | SB-21 至 SB-63 |
| 命令与租约 | `Session.command(vid, op, **args)`、`with Session.acquire(vid):` | SB-35 至 SB-37 |
| 速度控制（<= 50 Hz） | `Session.velocity(vid, vx, vy, vz, yaw_rate)`、`velocity_stop(vid)` | WS `call` + CLIENT_DATA |
| 实时流（raw 按布局解码、msgpack 解包、自动 ack） | `for m in Session.stream([topic, ...], seconds=)` | WS `/sessions/{sid}/rt` |
| 节能驻留与恢复（1.1.0；`515` 与 WS 4430 时自动恢复一次，满员按恢复票排队） | `Session.park(reason)`、`Session.resume(wait=True)` | SB-69、SB-70 |
| 公开容量（实时观看席位、沙盒槽位、排队与预计等待、构建信息） | `Sandbox.capacity()` | SB-71 |
| 内部档会话（身份令牌由部署方的身份入口签发） | `Sandbox.create(..., identity_token=...)` | SB-04 + `X-AWR-Identity` |

错误统一为 `AwrError(code, name, message, detail, remedy, status, retry_after_s)`；会话结束为 `SandboxGone`（附最近一次快照）。
`retry_after_s` 取自 problem+json 的 `retry_after_ms`（精确到毫秒，可为小数秒），没有时取 `Retry-After` 头（整秒）；SDK 对 429 `111`
按该值自动重试（抖动随等待缩放）。只用 REST、长时间不调用的会话会按无连接空闲规则驻留（ADR-121），下一次需要 sim-core 的调用
收到 409 `515` 时 SDK 自动恢复一次。
节能驻留（服务端在无交互超过阈值时驻留会话，或客户端调用 `park()`）不是结束：会话保存在服务器磁盘上，`resume()` 或任何会话
请求都会把它恢复（仿真时间从驻留时刻继续，epoch + 1；后台续期线程不会唤醒驻留中的会话）。
只用 REST 的客户端须知：没有实时连接、且 120 s（沙盒队列非空时 60 s；`GET /sessions/{sid}` 的 `idle_ttl_s`）内没有任何会话内调用
的会话会被驻留（AWR-04 §15.7.2 `sandbox.park.idle_disconnected_s`）。`auto_keepalive` 只在寿命将尽时续期，**不会**为保持运行而发
调用（续期不应阻止节能）；需要会话在长时间无操作时继续推进仿真（例如只在结束时读统计的长程任务）时，保持一个 `stream()` 实时连接，
或以短于 `idle_ttl_s` 的间隔发一次轻量读取（如 `Session.state()`）。被驻留后下一次会话请求会自动恢复（可能排队）。
`principal_hint` 保存在 `~/.config/awr_sandbox/principal`（0600），只用于幂等与配额聚合，不是凭据。

示例（`examples/`）：`01_quickstart`、`02_manual_control`、`03_sensors_status`、`04_targets_capture`、`05_gsd_scan_dryrun`、
`06_external_allocator`、`scenario2_e2e`（D2-AC-36 的验收脚本）。示例只使用公开接口；识别物、任务、机巢等领域端点由
M18 至 M21 交付，未部署的端点返回 404。
