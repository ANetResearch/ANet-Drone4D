# Python SDK

`awr_sandbox` 是沙盒接口的单文件 Python SDK（版本 1.1.0，接口 `/api/sandbox/v1`）。它不依赖本仓库的 `awr` 包，可以单独复制使用，
负责排队、工作量证明、续期、节能后的恢复与实时二进制帧的解码。

## 安装

支持 Python 3.9 至 3.13，依赖 httpx、websockets 与 msgpack：

```sh
pip install "httpx>=0.24" "websockets>=12" "msgpack>=1.0"
curl -O https://drone4d.agentnetwork.org.cn/sdk/awr_sandbox.py
```

在仓库内运行时，文件位于 `tools/sdk/awr_sandbox.py`，`make setup` 创建的 `.venv` 已包含上述依赖（见 {doc}`../intro/quickstart`）。

## 最小示例

```python
from awr_sandbox import Sandbox

sb = Sandbox("https://drone4d.agentnetwork.org.cn", timeout_s=30)
with sb.create("synthcity", "t5_area_guard") as s:   # 满员时自动排队；需要时自动求解工作量证明
    print(s.state()["state"])
    s.clock("speed", rate=5)                          # 受限时返回授予值
    for msg in s.stream(["sandbox/session"], seconds=3):
        print(msg.topic, msg.data)
```

离开 `with` 块时会话结束；结束前 SDK 导出一次配置快照，保存在 `Session` 对象上。

## 对象模型

| 对象 | 获取方式 | 职责 |
|---|---|---|
| `Sandbox(base_url, *, timeout_s=10.0, auto_keepalive=True, verify_tls=True, principal_hint=None)` | 构造 | 连接一个部署；构造时读取 `GET /meta` 并校验接口与契约主版本 |
| `Session` | `Sandbox.create()`、`Sandbox.attach(sid, token)` | 一个沙盒会话；上下文管理器 |
| `Session.vehicles`、`targets`、`nests`、`tasks`、`models` | 属性 | 资源集合，统一提供 `list`、`get`、`create`、`update`、`remove` |
| `Session.env`、`Session.tasking` | 属性 | 环境与外部分配器 |
| `Lease` | `Session.acquire(vid)` | 无人机租约；上下文管理器，退出时释放 |
| `Msg(topic, t_sim_ns, data)` | `Session.stream()` 的元素 | 一条实时消息 |

## 调用一览

| 能力 | 调用 | 端点 |
|---|---|---|
| 世界、模板、概况、公开容量 | `Sandbox.worlds()`、`templates(world)`、`meta()`、`capacity()` | SB-00 至 SB-02、SB-71 |
| 创建（工作量证明、排队长轮询与认领） | `Sandbox.create(world, template, *, snapshot=None, calendar=None, seed=None, queue=True, force_new=False, on_queue=None)` | SB-03 至 SB-06、SB-08 |
| 接回已有会话 | `Sandbox.attach(sid, token)` | SB-08 |
| 状态与等待就绪 | `Session.state()`、`wait_ready()` | SB-08 |
| 续期、结束 | `Session.keepalive()`、`close()` | SB-10、SB-09 |
| 快照、恢复、模板、切换底图 | `Session.snapshot()`、`restore(snapshot)`、`load_template(t)`、`switch_world(world, template=None)` | SB-11 至 SB-14 |
| 时钟 | `Session.clock(op, rate=None, ticks=None)` | SB-15 |
| 事件补拉 | `Session.events(since_seq=0, limit=1000, types=None)` | SB-16 |
| 机队 | `vehicles.add(model_id, **kw)`、`state(vid)`、`sensors(vid)`、`perception(vid)`、`radar_tracks(vid)`、`acoustic_bearings(vid)` | SB-28 至 SB-38 |
| 识别物 | `targets.create(body)`、`state(tid)`、`suggested_radius(tid, model_id)` | SB-40 至 SB-46 |
| 机巢 | `nests.list()`、`create(body)`、`update(id, patch)`、`remove(id)` | SB-47 至 SB-51 |
| 任务 | `tasks.create(body, dry_run=False)`、`start`、`pause`、`resume`、`cancel`、`sustainability(tid)`、`relay_stats(tid)` | SB-52 至 SB-56、SB-59 |
| 环境 | `env.state()`、`preset(name, duration_s)`、`calendar(start_utc, tz)`、`query(points, fields)` | SB-60 至 SB-63 |
| 外部分配器 | `tasking.snapshot()`、`tasking.submit(plan)` | SB-57、SB-58 |
| 机型目录 | `Sandbox.catalog.models()`、`model(model_id)`、`sensors()` | SB-21 至 SB-23 |
| 命令与租约 | `Session.command(vid, name, **args)`、`with Session.acquire(vid):` | SB-35 至 SB-37 |
| 速度控制 | `Session.velocity(vid, vx, vy, vz, yaw_rate=0.0)`、`velocity_stop(vid)`（至多 50 Hz） | WebSocket `call` 与客户端数据通道 |
| 实时流 | `for m in Session.stream([topic, ...], seconds=None):` | SB-64 |
| 节能驻留与恢复 | `Session.park(reason)`、`resume(wait=True)` | SB-69、SB-70 |
| 内部档会话 | `Sandbox.create(..., identity_token=...)`（令牌由部署方的身份入口签发） | SB-04 |

端点编号与路径的对应见 {doc}`rest`。`Session.command()` 在有实时连接时经 WebSocket `call` 发送，否则经 `POST .../commands`，
同一调用编号幂等重试。

## 错误处理

所有错误统一为 `AwrError(code, name, message, detail, remedy, status, retry_after_s)`，`str(e)` 的形式为 `"<code> <NAME>: <message>"`。
会话已结束时抛出子类 `SandboxGone`，其 `snapshot` 属性为最近一次导出的配置快照。

```python
from awr_sandbox import AwrError, Sandbox, SandboxGone

try:
    s.vehicles.add("p600_mid360", nest_id="nest-a")
except AwrError as e:
    print(e.code, e.name, e.detail, e.remedy)   # 例如 525 NEST_INCOMPATIBLE {'why': 'slots', 'slots': 6}
```

- 限流（429，原因码 111）与服务暂不可用时，SDK 按 `retry_after_s`（取自 `retry_after_ms` 或 `Retry-After`）自动重试，抖动随等待缩放。
- 只用 REST、长时间不调用的会话会因无连接空闲而被驻留；下一次需要 sim-core 的调用收到 409 与原因码 515 时，SDK 自动恢复一次。

## 排队、续期与节能

- **排队。** `create()` 收到 202 时进入排队，以单次最长 25 s 的长轮询等待；`on_queue(kind, view)` 回调接收位次与预计等待。由于长轮询的单次时长为
  25 s，请把 `timeout_s` 设为大于 25 s 的值（例如 30）。`queue=False` 时满员立即失败。
- **续期。** `auto_keepalive=True` 时后台线程在寿命将尽时续期并换发令牌。续期**不会**为保持运行而发出调用，也不会唤醒驻留中的会话。
- **节能。** 没有实时连接、且在 120 s（沙盒队列非空时 60 s，见 `GET /sessions/{sid}` 的 `idle_ttl_s`）内没有任何会话内调用的会话会被驻留。
  需要会话在长时间无操作时继续推进仿真，请保持一个 `stream()` 实时连接，或以短于 `idle_ttl_s` 的间隔调用一次 `Session.state()`。
  驻留后的下一次会话请求会自动恢复（满员时可能排队）。
- **主体提示。** `principal_hint` 保存在 `~/.config/awr_sandbox/principal`（0600），只用于幂等与配额聚合，不是凭据。

## 手动控制示例

```python
with sb.create("synthcity", "t6_manual") as s:
    v = s.vehicles.add("p600_mid360", home_enu_m=[0.0, 0.0, None], label="manual")
    vid = v["id"]
    s.command(vid, "arm")
    s.command(vid, "takeoff", alt_m=30.0)
    with s.acquire(vid, priority="normal"):
        t_end = time.monotonic() + 5.0
        while time.monotonic() < t_end:
            s.velocity(vid, 3.0, 0.0, 0.0)   # 向东 3 m/s（World ENU）
            time.sleep(0.04)                  # 25 Hz
        s.velocity_stop(vid)
    s.command(vid, "land")
```

速度控制受服务端软围栏保护：朝识别物敏感范围方向的速度分量被钳为 0，并返回警告 `KEEPOUT_CLAMPED`（见 {doc}`../guide/sandbox`）。

## 示例程序

仓库的 `tools/sdk/examples/` 包含以下示例，均只使用公开接口；地址取第一个命令行参数或环境变量 `AWR_SANDBOX_URL`，缺省为 `http://127.0.0.1:8002`：

| 示例 | 内容 |
|---|---|
| `01_quickstart.py` | 创建沙盒、读会话状态、申请倍速、订阅会话状态 5 s、导出快照、结束 |
| `02_manual_control.py` | 添加一架 P600、起飞、申请租约后以速度指令向东飞 5 s、停止、释放租约、降落 |
| `03_sensors_status.py` | 逐机型、逐传感器发现全部数据产品，并经 REST 与 WebSocket 各取一个样本 |
| `04_targets_capture.py` | 放置一个人，派一架机定点盯看，等待 `perception.capture` 事件 |
| `05_gsd_scan_dryrun.py` | 给出区域与所需 GSD，打印航高、条带、架数与完工时间曲线 |
| `06_external_allocator.py` | 读取分配快照，按"最近的空闲机"贪心分配工作项，提交外部计划 |
| `scenario2_e2e.py` | 区域值守与接力的端到端验收脚本 |

```sh
python tools/sdk/examples/01_quickstart.py https://drone4d.agentnetwork.org.cn
```
