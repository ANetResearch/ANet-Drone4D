# R27 研究笔记：rosbridge_suite / Foxglove ws-protocol / Zenoh：实时通信与 Realtime Gateway 协议设计

> 研究单元：r27 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §3–4（前后端分离）、§28（DroneState）、§29（多机）、§31–32（ANet）、§33（Backend 栈）、§36（前后端实时通信）、§37（更新频率）、§39（Timeline）、§40（Drone Interaction）
>
> 仓库快照（路径相对 `refs/backend/`，只读）：
> - `rosbridge_suite` @ `aa9a7a3`（2026-08-17，★1246）。协议规范 v2.1.0，包版本 4.2.1。主干是 `ros2` 分支（rclpy + tornado）。ROS1 分支 `ros1`（0.11.18）最后一次提交在 2025-09-10，之后只做维护。
> - `ws-protocol` @ `f3b4135`（2025-07-20，★150）。**GitHub 已 archived**，README 声明"库已弃用，规范已过时，请迁移到 foxglove-sdk"。
> - `zenoh` @ `9fcd9cb`（2026-09-15，★3216，1.10.1，Rust workspace）。
>
> 另外读了两个仓库（放在 `.cache/research/`，只读）：
> - **eclipse-zenoh/zenoh-ts** @ `3603f28`（2026-09-15，★49，1.10.1）：浏览器接入 zenoh 的 TS 库，配套 `zenoh-plugin-remote-api`。本单元为读它单独 clone 到 `.cache/research/r27/zenoh-ts/`。
> - **foxglove/foxglove-sdk** @ `dcbc667`（2026-09-24，★311）：r15 已 clone 到 `.cache/research/r15/foxglove-sdk/`。ws-protocol 在这里演进出 **v2** 和"控制/数据平面分离"。
>
> 本机实测产物在 `.cache/research/r27/`。测试机是 Xeon E5-2603 v4 @1.7 GHz（无睿频，偏保守），Python 3.12，Node 22.12，Chromium 151 headless。
>
> | 脚本 | 内容 | 输出 |
> |---|---|---|
> | `bench_encode.py` | 14 种线格式编码同一批 DroneState 的尺寸与耗时（N=1/10/100/1000） | `bench_encode.out` |
> | `node/bench_decode.mjs` | V8 纯 JS 解码并写入渲染 buffer 的耗时，含"未对齐 payload"对照 | `bench_decode.out` |
> | `bench_ws_backpressure.py` | 真实 Chromium：主线程慢于遥测频率时的 push / credit / worker 三种模式对比 | `bench_ws_backpressure.out`、`bench_ws_bp_heavy.out` |
> | `z_bench.py` | zenoh 回环 RTT 与吞吐；downsampling 陷阱；liveliness 掉线检测；query RTT | `z_bench.out` |
> | `gw_proto.py` + `node/gw_client.mjs` | 本文协议 `anet.rt.v1` 的 FastAPI 原型与负载客户端（1/10/50 客户端） | `gw_bench*.out`、`gw_server*.log` |
> | `rosbridge_cbor_hook.py` | rosbridge CBOR typed-array tag 转 numpy 的解码器（cbor2 ≥ 6） | `rosbridge_cbor_hook.out` |
>
> 与已有笔记的分工：
> - r15 定了 Foxglove/Lichtblick 的 Player、MCAP 调试旁路，以及 ws-protocol 的概念映射（§3.19）。
> - r19 提出 40 B 二进制帧，r21 提出 30 B swarm-lite 帧和 MAVLink → DroneState 映射，r14 定了客户端插值，r24 定了 `safety` 通道。
>
> **本文是 Realtime Gateway WebSocket 协议的权威定义**。它统一并修订上述几处零散的帧格式：r19 的 40 B、r21 的 30 B 和 r15 的约 100 B，合并为 **Full64 / Lite32** 两种对齐布局；帧头改为 **16 B 对齐**。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **Zenoh**（eclipse-zenoh/zenoh，★3216，1.10.1，2026-09 活跃） | 零开销 pub/sub/query/存储统一总线。Rust 核心，提供 Python/C/TS 绑定。8 级优先级、拥塞控制（Drop/Block）、express、SHM、liveliness、ACL/降采样拦截器 | **reference（V0.1 起）**：key expression 命名语法；Priority/CongestionControl/express 语义；`zenoh-ext` AdvancedPublisher/Subscriber 的"序号 + 心跳 + 缓存 + `_sn=` 区间补发"算法。这些直接用于我们 WS 协议的 topic 命名、QoS 分级和 seek backfill。<br>**adopt（V0.5+）**：`eclipse-zenoh` Python 包作为 Gateway、仿真 worker、PX4/ROS2（rmw_zenoh）和 ANet agent 之间的**内部总线**；liveliness token 做在线状态与能力发现。<br>**skip**：浏览器直连 zenoh（zenoh-ts/remote-api） | V0.1 ref；V0.5 adopt；V1.0 ANet | ★★★★★ |
| **Foxglove ws-protocol**（★150，已归档）→ **foxglove-sdk**（★311，活跃） | 机器人可视化的 WebSocket 协议 v1：JSON 控制消息 + 二进制数据帧，包括 channel 广告（含 schema）、按 channel 订阅、time、services、parameters、assets。SDK 中已演进出 v2 和控制/数据平面分离 | **port（MVP）**：`serverInfo` 的 capabilities/sessionId、`advertise`（channel + schema）、`subscribe`/`unsubscribe`、二进制 MessageData 带时间戳、`time`、`status`/`removeStatus`、PlaybackControl/PlaybackState 语义，以及"首个订阅者出现才开始生产"的懒生产模式。<br>**port**：foxglove-sdk 的**控制平面满则断开、数据平面满则丢最旧**双队列。<br>**adopt**：foxglove-sdk 做调试旁路和 MCAP 录制（r15 已定）。<br>**skip**：把 Foxglove 协议当作主协议（规范自称过时，v2 仍在变；13/17 B 头部导致 payload 不对齐） | V0.2 | ★★★★☆ |
| **rosbridge_suite**（★1246，4.2.1，ros2 分支 2026-08 活跃；ros1 分支 2025-09 后仅维护） | ROS 与 JSON/CBOR over WebSocket 的通用桥：`op` 信封；publish/subscribe/call_service/action；throttle/queue/fragment/png/cbor/cbor-raw；glob 白名单 | **adopt（V0.5–V0.6）**：P600 的 Prometheus 是 ROS1 Noetic，只能在机载或地面端跑 `rosbridge_server`（ros1 分支）。Gateway 用**自研 asyncio 客户端**（websockets + cbor2）订阅：`compression:"cbor"`、`throttle_rate:T`、`queue_length:1`。<br>**port**：`id` 关联一次交互、glob 白名单 ACL、OutgoingMessage 编码缓存、CBOR typed-array tag。<br>**skip**：作为浏览器主协议（JSON 路径比二进制慢约 470 倍；`queue_length=0` 时节流为 leading-edge，会丢最后一帧）；**不要用 roslibpy**（不支持二进制和 CBOR，并依赖 Twisted） | V0.5 adapter | ★★★☆☆ |
| zenoh-ts / remote-api（额外 clone，★49） | 浏览器经 zenohd 的 remote-api 插件（WebSocket + ZSerializer 二进制）使用 zenoh | **reference**：它反证了"浏览器直连总线"不可取（每个 socket 一个无界发送队列、每个 tab 一个 zenoh session、没有限速和兴趣管理）。MVP 阶段 **skip** | — | ★★☆☆☆ |

**关键结论（实现者先读这几条）**

1. **主协议自研 `anet.rt.v1`，概念沿用 Foxglove，线格式重新设计（§3.3–3.5）**。沿用的概念有：`op` 信封、channel 广告带 schema、按 channel 订阅、二进制数据帧、TIME、Playback。线格式有三处关键改动：
   - **(a) 帧头和每条记录头都是 16 B，payload 保证 8 字节对齐**。JS 端因此可以直接用 `Float32Array` 零拷贝视图读取。Foxglove v1 的 MessageData 头是 13 B（1+4+8），v2 是 17 B（1+8+8），payload 不对齐。`new Float32Array(buf, 13)` **直接抛 RangeError**，只能先 `slice` 复制：1000 架时要 40.6 µs，对齐视图只要 15.5 µs。
   - **(b) 一个 tick 只发一个 BATCH 帧**，把多个 channel 的记录合并在一起，浏览器每个 tick 只触发一次 `onmessage`。
   - **(c) 订阅时带 rate/mode 参数，另加 credit 窗口**。
2. **编码选型（§3.4，实测）**：
   - 高频同构遥测用 **raw struct**：服务端用 numpy structured array 的 `tobytes()`，浏览器用 DataView 或 TypedArray 读取。1000 架时编码 **5.7 µs**，解码加写入渲染 buffer **15 µs**，64 KB。
   - 同样数据走 JSON 字典：编码 **40.7 ms**，解码 **7.08 ms**，388 KB。
   - MessagePack 数组：编码 19.5 ms，解码 0.6–0.9 ms，74 KB。
   - Protobuf：编码 17.8 ms，JS 解码 1.86 ms。
   - FlatBuffers 逐字段构建：52 ms；bulk 拷贝方式本质上就是 raw struct。
   - 慢的真正原因是 **Python 端逐对象构造**，约 20 µs/架。结论是任何"逐条 dict 或 message"的格式都进不了 1000 架 × 30 Hz 的热路径。
   - 低频异构数据用 MessagePack，控制面用 JSON。
   - permessage-deflate 对 float 数据几乎无效：raw64 只省 13%，lite32 只省 7.6%。**一律关闭**。
3. **浏览器没有接收背压，TCP 背压信号对服务端不可见（§3.9，Chromium 实测）**。测试条件：30 Hz × 32 KiB，主线程每帧处理 70 ms（最多 14.3 fps），持续 8 s。
   - **push 模式**：显示状态的延迟线性增长到 **4.2 s**，服务端 write buffer 峰值却是 **0 KB**。Chromium 在内部吞下了 7.8 MB；把负载换成 256 KiB × 30 Hz、共 62.7 MB，结果相同。
   - **credit 窗口（W=2）**：延迟稳定在 mean 123 ms / p95 138 ms，带宽减半。
   - **Worker 持有 WebSocket 并按渲染 tick 拉取最新一帧**：mean 88 ms / p95 104 ms，但有 53% 的帧白传。
   - **结论**：应用层 credit/ack 加 Worker 两者都要。
4. **频率控制必须用"对齐 tick 的最新值采样 + 尾帧保证"（§3.8）**。rosbridge 的 `ThrottleMessageHandler`（`queue_length=0`）和 zenoh 的 `DownsamplingInterceptor` 都是 leading-edge 丢弃，停止发布前的**最后一个值永远不会送达**：实测发布到 148，收端停在 145。更严重的是，zenoh 降采样的时间戳**按规则共享**：一条通配规则 `uav/*/state @10Hz`、两个 key 各 50 Hz，结果 `uav/01` 收到 30 条，**`uav/02` 一条都没收到**。
5. **控制平面和数据平面分离（§3.9，移植自 foxglove-sdk `connected_client.rs`）**：
   - 控制平面（JSON：订阅应答、RPC 结果、事件、TIME）用有界 FIFO，满了就**断开**，因为不能丢。
   - 数据平面用**单槽 mailbox**，新帧覆盖尚未发出的旧帧。
   - 每个客户端一个 sender task。foxglove Python 版 server 是串行 await 各客户端，一个慢客户端会阻塞所有人。
6. **时间同步（§3.10）**：
   - 服务端仿真时钟是权威。TIME 消息 10 Hz 下发，内容为 `(t_sim_ns, t_wall_ns, rate, state, epoch)`。
   - 客户端用 ping/pong 取 **最小 RTT 样本**估计墙钟偏移，再推算当前 simTime。渲染时刻取 `t_sim − D`（沿用 r14 的 D 公式）。
   - seek 或重置时 epoch 加 1，旧 epoch 的帧一律丢弃。每个 BATCH 头都带 epoch，因为控制平面和数据平面之间不保证顺序。
7. **回放（§3.11）**：
   - Gateway 按**原生频率**把所有 channel 录成 MCAP，`log_time = t_sim_ns`。
   - 回放源与实时仿真源实现同一个 `Source` 接口。
   - seek 的做法：先 backfill，即每个 channel 取 `≤ t` 的最后一条，带 SNAPSHOT 标志下发；再 epoch 加 1；然后按 `log_time` 推进。控制语义照搬 Foxglove 的 `PlaybackControlRequest`/`PlaybackState`。
   - Mock 仿真每 1 s 存一次 checkpoint，可以实现"从过去某时刻分叉重跑"，属于科研上的 what-if，放 V0.4。
8. **topic 采用 zenoh key expression 语法（§3.6）**：
   - 不以 `/` 开头，chunk 用 `[a-z0-9_-]`，订阅时可以用通配 `*` 和 `**`。例如 `uav/p600-01/state`、`swarm/state`、`env/wind/field`。
   - 映射规则：ROS 名为 `"/" + key`，其中 `-` 换成 `_`；Foxglove 名为 `"/" + key`；zenoh 上再加 namespace 前缀 `anet/{world}/{run}`。**同一个名字端到端贯通**。
9. **FastAPI 原型的容量（§3.13，1.7 GHz 老 Xeon，单核 asyncio）**。场景：200 架，每个客户端订阅 `swarm/state@20Hz` 和 10 架 `uav/*/state@30Hz`，约 149 KiB/s。
   - 1 个客户端：CPU 7.5%，延迟 p50 1.7 ms。
   - 10 个客户端：CPU 34%（ack 合并后 29%），p95 17 ms。
   - 50 个客户端：CPU 打满，但 credit 窗口让它**平滑降级**：每客户端从 34 fps 降到 24 fps（ack 合并后 29 fps），p95 63 ms（47 ms），**没有队列膨胀**。
   - MVP 只需 1–5 个客户端，够用；V1.0 之后可以多进程分片，或换 Rust 网关。
10. **Zenoh 做内部总线的实测（§3.15）**：
    - Python 回环 RTT p50 0.40 ms。对 64 B 消息打开 express，p95 从 2.3 ms 降到 0.6 ms。
    - 单个 Python 线程 put 吞吐 15.6 万 msg/s，零丢失。
    - liveliness：token 优雅注销后 **0.5 ms** 收到 DELETE；进程被 kill -9 后 **6–8 ms** 收到（本机内核会立即发 RST）。静默断链时要等到 lease，默认 10 s，建议改为 2–3 s。
    - query RTT p50 0.59 ms。
    - 结论：可以直接作为 ANet capability discovery 的底座。
11. **rosbridge 只做 P600（ROS1）适配器（§3.16）**。
    - ros1 分支 0.11.18 支持 `cbor`/`cbor-raw`，但 **roslibpy 2.1.0 只支持 `png`/`none`，收到二进制帧直接 `NotImplementedError`**。因此自研一个约 150 行的 asyncio 客户端，用 cbor2 的 tag_hook 把 typed array 转成 numpy：1000 架的 57 KB CBOR 解码 **27 µs**。
    - 订阅必须设 `queue_length:1`，它会切到 `QueueMessageHandler`，行为变成 trailing 的最新值；设 0 就是 leading-edge 丢弃。
12. **大块数据走 HTTP，WS 只推"版本变了"的通知**：点云八叉树节点、风场网格、轨迹历史、MCAP 片段都属于这一类。这样可以用上 HTTP 缓存、Range 请求、并发和 CDN，WS 保持"小而热"。

---

## 1. 仓库概览

| 维度 | rosbridge_suite | Foxglove ws-protocol（+ foxglove-sdk） | Zenoh（+ zenoh-ts） |
|---|---|---|---|
| 版本/提交 | 4.2.1（协议 2.1.0）@ aa9a7a3，2026-08-17 | ws-protocol @ f3b4135，2025-07-20（**archived**）；foxglove-sdk @ dcbc667，2026-09-24 | zenoh 1.10.1 @ 9fcd9cb，2026-09-15；zenoh-ts 1.10.1 @ 3603f28 |
| Stars（2026-09） | 1246 | 150（SDK 311） | 3216（zenoh-ts 49） |
| 语言 | Python（rclpy + tornado）；rosapi 为 Python | 规范 Markdown；TS、C++（websocketpp）、Python（websockets）三套参考实现；SDK 为 Rust 内核，绑定 Python/C/C++/TS | Rust（tokio）；Python 绑定 `eclipse-zenoh`（PyO3 wheel）；C 绑定 zenoh-c；嵌入式用 zenoh-pico；TS 通过 remote-api |
| 定位 | ROS 与 WebSocket 的通用桥（任何 topic、service、action） | 可视化工具（Foxglove/Lichtblick）的数据接入协议 | 分布式 pub/sub、query、存储、计算协议与实现；ROS2 的 RMW 之一（rmw_zenoh，Kilted 起 Tier-1） |
| 传输 | WebSocket（tornado），可选 TLS 和 permessage-deflate | WebSocket，子协议 `foxglove.websocket.v1`（v2 在 SDK 中） | TCP、UDP、TLS、QUIC、QUIC-datagram、WS、serial、unix socket、unix pipe、vsock；支持多播 scouting 与 gossip |
| 编码 | JSON（默认）、PNG（JSON 塞进 RGB 图再 base64）、CBOR（typed-array tag）、CBOR-RAW（ROS2 CDR 原样透传）；BSON 在 4.1.0 已移除 | 控制面 JSON；数据面二进制，`encoding` 由 channel 声明（json/protobuf/ros1/cdr/flatbuffer） | 负载是任意字节，`Encoding` 为 MIME 风格标注；zenoh-ext 自带 ZSerializer（LEB128 变长） |
| 构建与依赖 | ROS2（ament）；运行依赖 cbor2、PIL、ujson、numpy；ROS1 分支用 catkin | 纯库，npm/pip/conan 均可装；SDK 有 wheel，`pip install foxglove-sdk` 本机即可用 | 纯 wheel，`pip install eclipse-zenoh` 本机即可用（已验证 1.10.1）；zenohd 和 remote-api 需要 Rust 构建或下载预编译包 |
| 2026 活跃度 | 活跃：4.0（2026-03）加 EventsExecutor；4.1 修竞态并移除 BSON；4.2 增加独立的 pub/sub glob；4.2.1 为防止事件循环饥饿加了写队列 | 规范仓库停更；SDK 活跃（v2、PlaybackControl、Ping/Pong、remote access/LiveKit） | 很活跃：1.x 每月发版；rmw_zenoh 自 Kilted 起为 ROS2 Tier-1 RMW（Jazzy 也有二进制包） |

**逐仓库一句话**：

- **rosbridge_suite**：它的"协议"是一套**以 op 为中心、按需扩展能力**的 JSON 消息集合，服务端通过 Capability 插件注册 op 处理函数。它最大的价值在于**生态兼容**：roslibjs、roslibpy 和各类 Web 机器人工具都在用。代价是 JSON 转换开销、线程开销和语义上的一些坑（见 §2.1）。对我们来说，它是**连接 ROS1 机体（P600）的唯一现实路径**。
- **Foxglove ws-protocol**：设计非常干净，"控制面 JSON + 数据面带时间戳的二进制帧 + channel 广告 schema"是可视化协议的业界范式，r15 的 Lichtblick Player 就基于它。但**规范仓库已归档**，官方明确说"不要自己实现 server"。协议本身在 foxglove-sdk 中继续演进：v2 去掉了 subscription id，改为 u64 channel id；新增 Ping/Pong 和二进制 PlaybackState；新增 remote access（LiveKit/WebRTC）的 `requestVideoTrack`。我们**借概念、不绑实现**。
- **Zenoh**：它不是 Web 协议，而是**机器人与边缘计算的数据总线**。key expression 地址空间、8 级优先级、拥塞控制、SHM 零拷贝、liveliness、queryable、存储插件、ACL 和降采样拦截器，覆盖了我们从仿真 worker 到真机、再到 ANet agent 的全部内部通信需求。浏览器侧的 zenoh-ts 需要 zenohd 加 remote-api 插件，它自己也承认 Rust 版目前无法编译到 WASM，而且缺少背压和兴趣管理，**不适合直接给浏览器用**。

---
## 2. 源码结构与关键模块

### 2.1 rosbridge_suite

```text
rosbridge_suite/
├── ROSBRIDGE_PROTOCOL.md                  # 协议规范 v2.1.0（op 信封、编码、QoS、各 op 字段）
├── rosbridge_library/src/rosbridge_library/
│   ├── protocol.py                        # Protocol：incoming/send/serialize/deserialize/register_operation
│   ├── rosbridge_protocol.py              # RosbridgeProtocol：装配全部 Capability
│   ├── capability.py                      # Capability 基类 + basic_type_check
│   ├── capabilities/                      # 每个 op 一个插件
│   │   ├── subscribe.py                   # Subscription（同一客户端对同一 topic 的多次订阅合并）+ Subscribe
│   │   ├── publish.py / advertise.py      # client→ROS 发布
│   │   ├── call_service.py / advertise_service.py / service_response.py
│   │   ├── send_action_goal.py / advertise_action.py / action_feedback.py / action_result.py
│   │   └── fragmentation.py / defragmentation.py
│   ├── internal/
│   │   ├── subscribers.py                 # MultiSubscriber（每个 topic 一个 rclpy 订阅，对全部客户端扇出）
│   │   ├── subscription_modifiers.py      # MessageHandler / ThrottleMessageHandler / QueueMessageHandler
│   │   ├── outgoing_message.py            # OutgoingMessage：同一条 ROS 消息的 JSON/CBOR 编码缓存
│   │   ├── cbor_conversion.py             # typed-array tag（69..86）打包
│   │   ├── pngcompression.py              # JSON→RGB PNG→base64
│   │   ├── message_conversion.py          # ROS msg ↔ dict（NaN/Inf→null，uint8[]→base64）
│   │   ├── publishers.py                  # MultiPublisher + unregister_timeout
│   │   ├── services.py                    # ServiceCaller(Thread)，每次调用新建 client
│   │   └── qos_extraction.py              # 2.1.0 新增的 qos 对象 → rclpy QoSProfile
├── rosbridge_server/
│   ├── scripts/rosbridge_websocket.py     # 入口：tornado HTTPServer + rclpy executor；参数表
│   └── src/rosbridge_server/websocket_handler.py   # RosbridgeWebSocket：IncomingQueue 线程、写队列
└── rosapi/                                # 图查询服务：/rosapi/topics、topics_and_raw_types、nodes、params…
```

**关键实现与可借鉴点**

1. **op 信封与 `id` 语义**（`ROSBRIDGE_PROTOCOL.md` §1；`protocol.py::Protocol.incoming`）：
   - 任何带 `op` 的对象都是合法消息。`id` 标识的是**一次交互**，而不是一条消息：`call_service` 与其 `service_response`、`send_action_goal` 与后续的 feedback/result 共用同一个 `id`。
   - 客户端还可以在任意消息里夹带 `fragment_size`/`png` 等参数，修改连接级设置。这个做法过于随意，**不要学**。
   - 我们的 `call`/`result`/`progress` 沿用"id 标识交互"的语义。
2. **订阅参数合并**（`capabilities/subscribe.py::Subscription.update_params`）：
   - 同一客户端对同一 topic 的多个订阅，按"最小公倍数"合并：`throttle_rate`、`queue_length`、`fragment_size` 都取 min；`compression` 按 `cbor-raw > cbor > png > none` 取最强。
   - 这说明 rosbridge **按 topic 而不是按订阅**做节流，所以同一页面里的不同组件无法各自拿到不同频率。
   - 我们的设计改为**每个订阅独立的 rate 和 next_due**，服务端对同一 channel 只编码一次（§3.8）。
3. **节流/队列状态机**（`internal/subscription_modifiers.py`）：
   - `MessageHandler`：直通。
   - `ThrottleMessageHandler`：`throttle_rate>0` 且 `queue_length=0` 时启用。`handle_message` 只在 `time_remaining()==0` 时发送，否则**直接丢弃**。这就是 leading-edge 节流，最后一帧会丢。
   - `QueueMessageHandler(Thread)`：`queue_length>0` 时启用。用 `deque(maxlen)` 丢弃最旧的消息，由一个线程在 `time_remaining()==0` 时从**队首**取出发送。
     - `queue_length=1` 时就变成"单槽最新值 + 尾帧保证"，这正是状态类数据想要的语义。代价是每个订阅一个 Python 线程。
     - `queue_length>1` 时是 FIFO，会发送过时数据，**不适合状态类数据**。
4. **扇出编码缓存**（`internal/subscribers.py::MultiSubscriber.callback`、`internal/outgoing_message.py::OutgoingMessage`）：
   - 每条 ROS 消息只构造**一个** `OutgoingMessage`，传给所有客户端的回调。
   - `get_json_values()` 和 `get_cbor()` 带缓存，但 JSON 字符串化（`Protocol.serialize → json.dumps`）仍然**每个客户端各做一次**，只有 CBOR 字节是真正共享的。
   - 我们的 `Channel.record()` 做到"每个 `(channel, seq)` 只编码一次，字节对象直接共享"（§3.13）。
5. **CBOR typed array**（`internal/cbor_conversion.py`）：
   - `sequence<float>` 用 tag 85，`sequence<double>` 用 86，`uint16` 用 69，`int16` 用 77，依此类推（遵循 draft-ietf-cbor-array-tags-00）。只支持小端。`uint8[]` 直接编成 bytes。
   - 数值数组因此可以在 JS 端零拷贝，Python 端直接用 numpy 接住（见 `rosbridge_cbor_hook.py`，1000 架 57 KB 解码 27 µs）。
   - `cbor-raw` 则把 ROS2 CDR 字节原样放进 `msg.bytes`，另附 `secs/nsecs`。
6. **PNG "压缩"**（`internal/pngcompression.py::encode`）：
   - 流程是：JSON 文本转成字节 → 用 `\n` 填充为 `w×h×3` → 作为 RGB 图做 PNG（deflate）→ base64。浏览器端要先用 `<img>` 加 canvas 的 `getImageData` 解出来。
   - 这是 WebSocket 还没有二进制帧那个年代的遗留手段：base64 带来 33% 膨胀，还要经过 canvas 往返。**skip**。
7. **分片**（`capabilities/fragmentation.py`、`defragmentation.py`）：
   - 格式为 `{op:"fragment", id, data, num, total}`，默认超时 600 s。
   - 重组时用 `"".join(dict.values())`，依赖插入顺序而不是 `num`；乱序到达会拼错，只是 TCP 下不会触发。
   - WebSocket 本身支持大帧，**不需要应用层分片**。大块数据走 HTTP。
8. **WebSocket 服务端的队列**（`rosbridge_server/websocket_handler.py`）：
   - `IncomingQueue(Thread)`：`deque(maxlen=incoming_queue_size=1000)`，满了**丢最旧**，并限频打印警告。
   - `send_message`：用 `threading.Semaphore(write_queue_size=1000)` 做非阻塞 acquire，**拿不到就丢当前这条**（包括 service_response 和 action_result 这类关键消息）；拿到了就 `call_soon_threadsafe(write_queue.put_nowait)`。
   - 单独的 `_drain_write_queue` 协程串行调用 `write_message`，这是 4.2.1 为防止事件循环饥饿而加的（#1290）。
   - 连接建立时 `set_nodelay(True)`。`check_origin` 恒返回 True，意味着**不校验跨源**。
   - **启示**：
     - ① 控制类消息和数据类消息必须分队列，丢弃策略也要不同（foxglove-sdk 的做法见 §2.2）。
     - ② 必须自己做 Origin/Token 校验。
9. **参数面**（`scripts/rosbridge_websocket.py`，`SERVER_PARAMETERS`、`PROTOCOL_PARAMETERS`）：
   - `port 9090`、`address`、`certfile/keyfile`、`incoming_queue_size 1000`、`write_queue_size 1000`、`use_compression false`。
   - `delay_between_messages 0.0`：在 `Protocol.send` 里用 `time.sleep` 实现，**阻塞**。
   - `max_message_size 1e6`、`unregister_timeout 10 s`。
   - `topics_pub_glob`/`topics_sub_glob`/`services_glob`/`actions_glob` 为 fnmatch 白名单，`services_glob` 会自动追加 `/rosapi/*`。
10. **服务调用**（`internal/services.py::call_service`、`capabilities/call_service.py`）：
    - 每次 `call_service` 新建一个 `ServiceCaller` 线程，并新建一个 rclpy client。流程为 `wait_for_service(1.0)` → 调用 → 默认超时 5 s → 异步销毁 client。
    - 这对偶发配置调用没问题，但**不适合每秒几十次的控制指令**。控制指令应走 topic，或走我们自己的 `call` 通道（常驻 handler）。
11. **JSON 数值坑**（`internal/message_conversion.py::_from_primitive_inst`）：NaN/Inf 会被转成 `null`。我们如果用 NaN 表示"未知"，走 JSON 就会变成 null，走二进制则保持 NaN。**协议里要统一约定**，见 §3.5。
12. **rosapi**（`rosapi/scripts/rosapi_node`）：提供 `get_topics`、`get_topics_and_raw_types`（给 cbor-raw 客户端拿 msg 定义）、`get_services`、`get_param*`（受 `params_glob` 约束）等服务。它说明协议本身**不做图发现**，而是交给服务完成。我们用 `advertise`（推送）加 REST `/api/rt/channels`（拉取）两条路径覆盖。

### 2.2 Foxglove ws-protocol（及 foxglove-sdk 中的演进）

```text
ws-protocol/
├── docs/spec.md                         # v1 规范（JSON op + 二进制 opcode）
├── typescript/ws-protocol/src/
│   ├── FoxgloveClient.ts                # 浏览器客户端：binaryType='arraybuffer'；subscribe/advertise/sendMessage
│   ├── FoxgloveServer.ts                # Node 服务端：channels/clients/subscriptionsByChannel；首订阅/末退订事件
│   ├── parse.ts                         # parseServerMessage/parseClientMessage：DataView 小端解析
│   └── types.ts                         # BinaryOpcode、ServerCapability、Channel、Service、Parameter…
├── typescript/ws-protocol-examples/src/examples/
│   ├── mcap-play.ts                     # MCAP → ws 回放（按 log_time 节拍）
│   ├── perf-test-client.ts / sysmon.ts / image-server.ts / param-*.ts / service-*.ts
├── python/src/foxglove_websocket/server/__init__.py   # asyncio + websockets 服务端
├── cpp/foxglove-websocket/include/foxglove/websocket/websocket_server.hpp  # websocketpp 服务端（sendBufferLimitBytes）
└── test-client-web-app/src/{WorkerSocketAdapter.ts, worker.js}           # WebSocket 放进 Web Worker
```

**v1 消息全表**（`docs/spec.md`）

| 方向 | 类型 | op/opcode | 要点 |
|---|---|---|---|
| S→C | JSON | `serverInfo` | `name`、`capabilities`（clientPublish/parameters/parametersSubscribe/time/services/connectionGraph/assets）、`supportedEncodings`、`metadata`、`sessionId`（客户端据此判断是否为同一服务端实例的重连） |
| S→C | JSON | `status` / `removeStatus` | level 0/1/2；可带 `id`，同 id 覆盖，可移除。**非常适合 UI 的持久告警条** |
| S→C | JSON | `advertise` / `unadvertise` | `channels:[{id, topic, encoding, schemaName, schema, schemaEncoding?}]`；**id 只有在 topic、encoding 和 schema 完全相同时才允许复用**（客户端按 id 缓存反序列化器） |
| S→C | JSON | `parameterValues`、`advertiseServices`、`unadvertiseServices`、`connectionGraphUpdate`、`serviceCallFailure` | — |
| S→C | bin | `0x01 MessageData` | `u8 op | u32 subscriptionId | u64 log/receive ts(ns) | payload` → 头 13 B |
| S→C | bin | `0x02 Time` | `u8 op | u64 ts(ns)`；如果服务端发 time，**所有消息的时间戳必须来自同一个时钟源** |
| S→C | bin | `0x03 ServiceCallResponse`、`0x04 FetchAssetResponse` | — |
| C→S | JSON | `subscribe`/`unsubscribe` | `subscriptions:[{id(客户端选), channelId}]`；每个客户端对每个 channel 只能有一个订阅 |
| C→S | JSON | `advertise`/`unadvertise`（客户端发布）、`getParameters`/`setParameters`/`subscribeParameterUpdates`、`subscribeConnectionGraph`、`fetchAsset` | — |
| C→S | bin | `0x01 ClientMessageData`（`u32 channelId`）、`0x02 ServiceCallRequest` | — |

**实现层要点**

1. **懒生产**（`FoxgloveServer.ts` 的 `#handleClientMessage` case `subscribe`，`#anySubscribed`）：某个 channel 的**第一个**订阅者出现时发出 `subscribe(channelId)` 事件，**最后一个**订阅者退订时发出 `unsubscribe` 事件。生产方（中间件）据此开始或停止生产和序列化。我们的 Gateway 对每个 channel 维护 `subscriber_count`，为 0 时仿真侧不做编码。
2. **没有背压**：
   - TS 版的 `sendMessage` 遍历所有客户端，直接调用 `ws.send`。
   - **Python 版**的 `send_message` 对每个客户端依次 `await connection.send(...)`（`server/__init__.py`）。一旦某个客户端的 TCP 写缓冲超过 websockets 的高水位，`send` 就会挂起，**所有后续客户端都被队头阻塞**。
   - **C++ 版**是唯一有背压的：`websocket_server.hpp::sendMessage` 检查 `con->get_buffered_amount() + payloadSize >= sendBufferLimitBytes`（默认 10 MB，`server_interface.hpp::DEFAULT_SEND_BUFFER_LIMIT_BYTES`），超过就**丢弃本条**，并以 2.5 s 去抖发 status "Send buffer limit reached"。但如 §3.1.3 所示，浏览器会先吞掉几十 MB，这个阈值基本不会触发。
3. **Worker 中的 WebSocket**（`test-client-web-app/src/WorkerSocketAdapter.ts`、`worker.js`）：实现了 `IWebSocket` 接口，WebSocket 建在 Worker 里，二进制消息通过 `postMessage(data, [data])` **转移**给主线程。我们在它的基础上增加"Worker 只保留最新值，主线程按渲染 tick 拉取"，并把解码也放进 Worker（§3.14）。
4. **回放节拍**（`ws-protocol-examples/src/examples/mcap-play.ts`）：以第一条消息为起点，比较 `elapsedMessageTime/rate` 和 `elapsedWallTime`，差值即为 `delay`。这种写法简单，但做不了 seek，也不支持倍速的精确切换。完整的 Player 状态机见 r15 §3.7（Lichtblick IterablePlayer）。
5. **foxglove-sdk 里的服务端工程化**（`.cache/research/r15/foxglove-sdk/rust/foxglove/src/websocket/`）：
   - `connected_client.rs`：每个客户端两条 `flume::bounded(message_backlog_size=1024)` 队列：
     - `send_data_lossy(msg, retries)` 在队列满时**弹出最旧的一条再重试**（`connected_client/send_lossy.rs`）。
     - `send_control_msg` 在队列满时 `shutdown(ControlPlaneQueueFull)`，先发一条 error status（"Disconnected because the message backlog on the server is full"）再关闭连接。
     - 另有每客户端并发上限：服务调用、资产获取、参数操作各 32 个信号量。
   - `connected_client/poller.rs`：一个 `tokio::select!` 同时等待控制队列和数据队列，写入 `ws_tx`。读循环和写循环分离，任一方结束就关闭连接。
6. **v2 协议变化**（`foxglove-sdk/rust/foxglove/src/protocol/v2/`）：
   - `MessageData` 改为 `u8 op | u64 channel_id | u64 log_time | payload`，**去掉了 subscription id 这层间接**，头部 17 B。
   - `Subscribe{channels:[{id, requestVideoTrack}]}`，按 channel 直接订阅，可请求 WebRTC 视频轨。
   - 新增二进制 opcode：`PlaybackState=5`（仍标记为 `#[doc(hidden)]`）和 `Pong=6`；客户端新增 `PlaybackControlRequest=3`（布局 `u8 cmd(0 play/1 pause) | f32 speed | u8 had_seek | u64 seek_time | u32 len | request_id`）。
   - `PlaybackState` 布局为 `u8 status(0 playing/1 paused/2 buffering/3 ended) | u64 current_time | f32 speed | u8 did_seek | u32 len | request_id`。
   - `Ping` 的 payload 至少 8 字节，原样回显。
   - `ServerInfo` 新增 `data_start_time`/`data_end_time`，供时间轴显示总范围。
   - **结论**：我们协议里的 playback、ping 和时间范围字段**逐一对齐 v2 语义**，以后写 Foxglove 调试桥时一一映射即可。

### 2.3 Zenoh

```text
zenoh/
├── commons/
│   ├── zenoh-keyexpr/        # key expression：语法、canon 化、intersects/includes、KeTree（通配匹配树）、KeFormat
│   ├── zenoh-protocol/       # 线协议：core/mod.rs 中的 Priority(0..7)、CongestionControl、Reliability…
│   ├── zenoh-codec/ zenoh-buffers/ zenoh-shm/ zenoh-config/ …
├── io/zenoh-links/           # tcp/udp/tls/quic/quic_datagram/ws/serial/unixsock_stream/unixpipe/vsock
├── io/zenoh-transport/       # 会话、批处理、优先级队列、分片、lease/keep-alive
├── zenoh/src/api/            # 用户 API：session, publisher, subscriber, querier, queryable, liveliness,
│                             #          matching, handlers/{fifo,ring,callback}, bytes, encoding, sample
├── zenoh/src/net/routing/interceptor/  # downsampling.rs, low_pass.rs, qos_overwrite.rs, access_control.rs
├── zenoh-ext/src/            # advanced_publisher.rs / advanced_subscriber.rs / advanced_cache.rs / serialization.rs
├── plugins/zenoh-plugin-rest/          # HTTP REST + SSE（GET + Accept: text/event-stream）
├── plugins/zenoh-plugin-storage-manager/ # 存储插件（memory/rocksdb/influx 等 backend）
├── examples/examples/        # z_pub/z_sub/z_get/z_queryable/z_liveliness/z_pub_shm/z_ping/z_pong…
├── zenohd/                   # 路由器守护进程
└── DEFAULT_CONFIG.json5      # 全部配置项及注释（最好的文档）
```

**核心概念与我们用得上的点**

1. **Key expression**（`commons/zenoh-keyexpr/src/key_expr/borrowed.rs` 的文档注释）：
   - 以 `/` 分隔的非空 UTF-8 chunk，**不能以 `/` 开头或结尾**，不能含 `#$?`，必须是 canon 形式。
   - 通配符：`*` 匹配单个 chunk，`**` 匹配任意多个 chunk，`$*` 用于 chunk 内部通配。
   - 集合关系分为 Disjoint、Intersects、Includes、Equals。
   - 这套语法就是我们的 topic 命名规范（§3.6）。服务端可以用 `KeBoxTree` 的思路，在 Python 里用前缀 trie 加 fnmatch 做订阅匹配。
2. **优先级与拥塞控制**：
   - `Priority`（`commons/zenoh-protocol/src/core/mod.rs:332`）共 8 级：Control=0、RealTime=1、InteractiveHigh=2、InteractiveLow=3、DataHigh=4、**Data=5（默认）**、DataLow=6、Background=7。
   - `CongestionControl`（同文件 :617）：**Drop（默认）**、Block、BlockFirst（unstable，只阻塞第一条）。
   - 发布端还可设 `express(true)`（`zenoh/src/api/builders/publisher.rs:133` 的注释："message will not be batched"），用于低时延。
3. **传输队列**（`DEFAULT_CONFIG.json5` 的 `transport/link/tx`）：
   - `batch_size 65535`；每个优先级队列 2 个 batch（`queue.size.*`）。
   - Drop 类消息最多等 `wait_before_drop 1000 µs`，拿不到 batch 就丢；Block 类最多等 `wait_before_close 5 s`，超时则关闭会话。
   - 自适应批处理 `batching.time_limit 1 ms`。
   - `lease 10000 ms`、`keep_alive 4`（即每 2.5 s 一次）。
   - `rx.max_message_size 1 GiB`。
   - **启示**：遥测用 Drop，命令和事件用 Block；给真机链路配置较短的 lease（2–3 s）。
4. **拦截器**（`zenoh/src/net/routing/interceptor/`）：
   - `downsampling.rs`：按 `interfaces`/`link_protocols`/`flows`/`messages` 过滤，`rules:[{key_expr, freq}]`。实现中 `ke_state: Vec<TimeState>` 按**规则**存放 `latest_message_timestamp`，`intercept()` 在 `now − latest ≥ threshold` 时放行，否则丢弃。于是：
     - ① leading-edge，没有尾帧；
     - ② 同一规则匹配到的**所有 key 共用一个时间戳**。§3.1.4 的实测显示第二个 key 被完全饿死。
   - `low_pass.rs`：按 key 限制 payload 加 attachment 的大小（`size_limit`）。适合防止大点云误入 4G 或数传链路。
   - `qos_overwrite.rs`：按 key、链路、ZID 覆盖优先级、拥塞控制和 express。
   - `access_control.rs` 与 `authorization.rs`：基于主体（接口、证书 CN、用户名）的 allow/deny 规则。
5. **zenoh-ext 高级 pub/sub**（`zenoh-ext/src/advanced_publisher.rs`、`advanced_subscriber.rs`、`advanced_cache.rs`）：
   - 发布端的 `Sequencing::SequenceNumber` 用 `AtomicU32` 给每条消息编号，也可用 HLC 时间戳。
   - `cache(CacheConfig::max_samples(n))` 在发布端缓存最近 n 条，并声明一个 queryable，挂在 `<ke>/@adv/pub/<zid>/<eid>/…` 后缀下。
   - `sample_miss_detection(heartbeat(period))` 周期性地在该后缀上发布"最后序号"；`sporadic_heartbeat` 只在序号变化时发送。
   - `publisher_detection()` 为发布者声明一个 liveliness token。
   - 订阅端的 `handle_sample()` 按 source 维护 `last_delivered` 和 `pending_samples: BTreeMap`：
     - 序号连续就投递，然后冲刷 pending；
     - 序号跳跃时，开了 retransmission 就先存入 pending，再用 `_sn={last+1}..` 区间查询补发；没开就回调 `Miss{source, nb}`，然后照常投递；
     - late-joiner 用 history 查询取回缓存。
   - 这是"**带序号的最新值流 + 丢包可感知 + 按需补发**"的完整参考。我们的事件通道（可靠、有序）和回放 backfill 借鉴其语义：`seq` 加区间补发（§3.9、§3.11）。
6. **Handlers**（`zenoh/src/api/handlers/ring.rs`、`fifo.rs`）：
   - `RingChannel(N)` 保留最新 N 条，满了丢最旧。**`RingChannel(1)` 就是最新值 mailbox**。
   - `FifoChannel` 满了会阻塞，TS 版的 FifoChannel 则丢新消息。
   - Gateway 从总线收数据时，状态类 topic 一律用 `RingChannel(1)` 或回调写入 mailbox。
7. **SHM**（`DEFAULT_CONFIG.json5` 的 `transport/shared_memory`；`examples/examples/z_pub_shm.rs`）：
   - 默认开启，`transport_optimization` 会把 **≥ 3072 B** 的消息自动放进 16 MiB 的 SHM 池，前提是双方在同一主机且都开启了 SHM。
   - 显式用法为 `ShmProviderBuilder::default_backend(size)`，再 `provider.alloc(len).with_policy::<BlockOn<GarbageCollect>>()`。
   - 仿真 LiDAR 或深度图这类大块数据在同机进程之间传输时，可以零拷贝。
8. **REST 插件**（`plugins/zenoh-plugin-rest/src/lib.rs`）：`GET /key` 并带 `Accept: text/event-stream` 时，以 SSE 推送 JSON 样本（`subscribe()` → `Sse::new(...)`）。**二进制负载会被 base64**（`JSONSample::payload_to_json`）。只适合调试或低频数据。
9. **其他**：
   - `namespace` 配置可以给会话的所有 key 自动加前缀，适合多 world、多 run 隔离。
   - `timestamping` 默认 router 为 true、peer/client 为 false，使用 HLC（uhlc）。
   - `zenoh-link-ws` 是把 zenoh 协议跑在 WebSocket 上的**节点间链路**，不是浏览器 API。

### 2.4 zenoh-ts 与 remote-api（浏览器路径）

- README 原话大意：长期计划是把 Rust 版 zenoh 编译到 WASM，**目前做不到**。现阶段 TS 库通过 WebSocket 连接 zenohd 的 `zenoh-plugin-remote-api` 插件，或者连接独立的 `zenoh-bridge-remote-api`（默认端口 10000）。库**不兼容 Node.js**。
- 协议定义在 `zenoh-plugin-remote-api/src/interface/mod.rs`：
  - `InRemoteMessage` 有 37 个 op，包括 Declare/Undeclare Publisher、Subscriber、Queryable、Querier、LivelinessToken，以及 Put、Get、Reply*、Ping 等。
  - `OutRemoteMessage` 有 15 个，包括 Sample、Query、Reply、PingAck、MatchingStatus 等。
  - 均由 `zenoh_ext::ZSerializer` 序列化为二进制。
- `zenoh-plugin-remote-api/src/lib.rs:515` 为每个 WebSocket 创建 `flume::unbounded` 发送队列，并调用 `zenoh::session::init(runtime)`，即**每个浏览器 tab 一个 zenoh session**。所有匹配的 Sample 都会原样转发：**没有限速、没有兴趣管理、没有背压**，慢客户端会导致服务端内存无界增长。
- **结论**：在浏览器面前，zenoh 需要一个"懂业务的 Gateway"来做限速、ROI、坐标换算、鉴权和编码。这正是我们的 Realtime Gateway。zenoh-ts 只适合内部调试页面。

---
## 3. 可复用算法与实现（含伪代码/参数）

### 3.1 本机实测数据（后续所有设计取舍的依据）

#### 3.1.1 服务端编码：同一批 DroneState，N 架 1 帧（`bench_encode.py`，Python 3.12，protobuf 为 upb 后端）

计时**包含**把仿真里的 numpy 状态转换为该格式所需对象的开销，这是真实热路径必须付出的代价。

| 格式 | N=10 字节 / 编码 | N=100 字节 / 编码 | N=1000 字节 / 编码 | N=1000 zlib-1 后 |
|---|---|---|---|---|
| JSON 字典（stdlib） | 3.9 KB / 400 µs | 38.9 KB / 3.95 ms | 388 KB / **40.7 ms** | 148 KB |
| orjson 字典 | 3.7 KB / 223 µs | 36.4 KB / 2.02 ms | 363 KB / 21.0 ms | 144 KB |
| orjson 位置数组 | 2.6 KB / 210 µs | 26.3 KB / 1.89 ms | 264 KB / 20.3 ms | 131 KB |
| orjson 列式 numpy（`OPT_SERIALIZE_NUMPY`） | 1.6 KB / 21 µs | 15.2 KB / 122 µs | 153 KB / 1.12 ms | 73 KB |
| MessagePack 字典（f32） | 1.5 KB / 214 µs | 15.0 KB / 2.12 ms | 150 KB / 22.9 ms | 70 KB |
| MessagePack 数组（f32） | 0.72 KB / 193 µs | 7.2 KB / 2.03 ms | 73.6 KB / 19.5 ms | 61 KB |
| ormsgpack 数组（没有 f32 选项，只能 f64） | 1.2 KB / 191 µs | 12.4 KB / 1.96 ms | 126 KB / 19.1 ms | 74 KB |
| CBOR 字典（cbor2） | 2.0 KB / 310 µs | 20.3 KB / 3.12 ms | 203 KB / 31.8 ms | 87 KB |
| **CBOR 列式 typed array（rosbridge 风格）** | 0.67 KB / 29 µs | 5.8 KB / 49 µs | 57 KB / **0.16 ms** | 52 KB |
| Protobuf（repeated message） | 0.70 KB / 175 µs | 7.0 KB / 1.73 ms | 70.9 KB / 17.8 ms | 60 KB |
| FlatBuffers 逐字段 Prepend | 0.68 KB / 610 µs | 6.4 KB / 5.32 ms | 64 KB / 52.3 ms | 56 KB |
| FlatBuffers 结构体向量 bulk memcpy | 0.68 KB / 32 µs | 6.4 KB / 35 µs | 64 KB / 68 µs | 56 KB |
| **raw Full64（numpy `tobytes`）** | 0.64 KB / **0.5 µs** | 6.4 KB / **0.8 µs** | 64 KB / **5.7 µs** | 56 KB（−13%） |
| **raw Lite32（含量化）** | 0.32 KB / 42 µs | 3.2 KB / 52 µs | 32 KB / 149 µs | 30 KB（−7.6%） |

- 读法一：**瓶颈在 Python 逐对象构造，约 17–40 µs/架**，与格式本身关系不大。1000 架 × 30 Hz 走 JSON 字典，光编码就要 1.2 核；走 raw 只要 0.02% 核。
- 读法二：Lite32 的 149 µs 几乎都是 numpy 小数组的固定开销。真正实现时，Mock 仿真直接在向量化步进里维护量化数组，这部分开销就没有了。
- 读法三：deflate 对 JSON 能省 62%，但压缩后仍是 raw 的 2.7 倍；对 raw 基本无效。结论是**关闭 permessage-deflate**。

#### 3.1.2 浏览器端解码并写入渲染 buffer（`node/bench_decode.mjs`，V8 / Node 22，msgpackr 和 cbor-x 强制走纯 JS 路径，模拟浏览器）

| 格式 | N=10 | N=100 | N=1000 |
|---|---|---|---|
| JSON 字典（`JSON.parse` + 取 position/orientation） | 58 µs | 600 µs | **7.08 ms** |
| JSON 位置数组 | 38 µs | 397 µs | 4.69 ms |
| msgpackr 字典 | 50 µs | 485 µs | 5.32 ms |
| msgpackr 数组 | 6.1 µs | 86 µs | 882 µs |
| @msgpack/msgpack 数组 | 6.8 µs | 61 µs | 604 µs |
| cbor-x 列式 typed array | 11 µs | 16.5 µs | 33.7 µs |
| protobufjs（运行时 Root） | 20 µs | 192 µs | 1.86 ms |
| FlatBuffers（手写访问器，DataView） | 0.50 µs | 1.8 µs | 14.4 µs |
| **raw64 DataView** | 0.44 µs | 1.9 µs | **15.0 µs** |
| **raw64 Float32Array 跨步视图（对齐）** | 0.31 µs | 1.8 µs | **15.5 µs** |
| **raw Lite32（f32 pos + i16 snorm quat）** | 0.44 µs | 2.4 µs | **20.0 µs** |
| raw64 **payload 不对齐**（13 B 头部）→ 先 slice 复制再建视图 | 2.8 µs | 8.1 µs | 40.6 µs |

- `new Float32Array(buffer, 13, n)` 会**抛 RangeError**（实测为 true），这就是帧头必须 8 字节对齐的原因。
- JSON 字典在 1000 架时占 7 ms/帧，30 Hz 就是**主线程 21%**；raw 是 0.05%。

#### 3.1.3 浏览器慢于数据流时会发生什么（`bench_ws_backpressure.py`，Chromium 151 headless）

条件：服务端 30 Hz × 32 KiB = 960 KiB/s；页面主线程每处理一帧忙等 70 ms，处理能力约 14.3 fps；持续 8 s。

| 模式 | 发送/接收/处理 | 服务端字节 | 显示时延（发送→处理） | 服务端 write buffer 峰值 | 服务端阻塞在 send 的总时长 |
|---|---|---|---|---|---|
| push（WS 在主线程，服务端每 tick 推） | 239 / 113 / 113 | 7.8 MB | mean **2157 ms**，p95 4044，最后一帧 **4229 ms**（线性增长） | **0 KB** | 117 ms |
| credit（W=2，ack 后才发，否则保留最新值） | 114 / 113 / 113（跳过 125） | 3.7 MB | mean **123 ms**，p95 138，max 143 | 0 KB | 60 ms |
| worker（WS 在 Worker，Worker 只留最新，主线程按 tick 拉取） | 237 / 235 / 110 | 7.8 MB | mean **88 ms**，p95 104，max 125 | 0 KB | 139 ms |
| push，重载 256 KiB × 30 Hz（7.5 MiB/s） | 239 / 109 / 109 | 62.7 MB | 最后一帧 **4356 ms** | **0 KB** | 721 ms |

**结论**：
- ① 浏览器会在内部缓冲几十 MB 未处理的消息，**服务端完全观察不到 TCP 背压**。rosbridge、foxglove C++ 以及 Python `websockets` 的高水位机制都起不了作用。
- ② 应用层 credit 窗口既能限制时延，又能省带宽。
- ③ Worker 拉取最新值的时延最低，但白白传了一半的帧。
- ④ 生产环境**两者同时使用**：WS 放在 Worker 里，ack 由"主线程实际消费"触发（§3.9、§3.14）。

#### 3.1.4 Zenoh（`z_bench.py`，eclipse-zenoh 1.10.1 Python，回环 TCP，peer 模式，关闭多播 scouting）

| 实验 | 结果 |
|---|---|
| ping-pong RTT，64 B | express 关：p50 417 µs，p95 **2299 µs**；express 开：p50 403 µs，p95 **599 µs** |
| RTT，4 KiB / 64 KiB | 关：503/3860 µs 和 526/4453 µs；开：419/997 µs 和 503/778 µs |
| 吞吐（64 B，单个 Python 线程 put，Block） | **156,536 msg/s**，200,000 条全部收到 |
| downsampling：一条通配规则 `uav/*/state` @10 Hz，两个 key 各 50 Hz，持续 3 s | `uav/01/state` 收到 30 条；**`uav/02/state` 收到 0 条**；最后收到的值为 145，实际最后发布的是 148 |
| downsampling：每个 key 一条规则 @10 Hz | 两个 key 各 30 条；最后收到 145，实际最后发布 146（依然没有尾帧） |
| liveliness：token 进程被 kill -9 | lease 10 s 时 **8 ms** 后收到 DELETE；lease 2 s 时 **6 ms**（本机内核立即发 RST。静默断链才需要等 lease） |
| liveliness：`undeclare()` | **0.5 ms** 后收到 DELETE |
| query/queryable 往返 | p50 **588 µs**，p95 5.4 ms |

#### 3.1.5 `anet.rt.v1` 原型容量（`gw_proto.py`，FastAPI 0.141 + uvicorn 0.54，`--ws websockets --loop uvloop`）

场景：200 架，仿真 50 Hz，Gateway tick 60 Hz。每个客户端订阅 `swarm/state@20Hz`（6.4 KB/帧）和 10 个 `uav/NNN/state@30Hz`。负载客户端为 Node `ws`，与服务端在同一台机器上，会抢 CPU。

| 客户端数 | 服务端 CPU（单核） | 每客户端 fps / KiB/s | 时延（t_sim→客户端解码后） | 备注 |
|---|---|---|---|---|
| 0 | 3.4%（仅仿真） | — | — | |
| 1 | 7.5% | 34 / 149 | p50 1.7 ms，p95 3.2 ms | |
| 10 | 34%（ack 每 3 帧、W=6 时 **29%**） | 39 / 149 | p50 7.6，p95 16.9（ack 合并后 5.5 / 9.4） | |
| 50 | ~100%（饱和） | **24.4** / 118（ack 合并后 **29.2** / 132） | p50 40，p95 63（ack 合并后 33 / 47） | credit 跳过的次数上升，**平滑降级，没有队列膨胀** |
| 10 / 50，`--ws websockets-sansio` | 36–37% / 饱和 | 39 / 18.6 fps | p95 21 / 88 | 这个场景下比 legacy 实现慢 |

- 单帧成本约为 (34% − 3.4%) / 392 帧/s ≈ **0.78 ms**，其中每条 ack 走一次 ASGI receive 的开销占了相当比例。所以 **ack 要合并**（每 N 帧一次，或捎带在 20 Hz 的心跳里）。
- 容量估算：在这颗 1.7 GHz 老 CPU 上，单进程可服务约 **40 个 150 KiB/s 的客户端**；现代 3+ GHz CPU 大约是 2–3 倍。

### 3.2 分层架构（Realtime 通路的全景）

```text
 Browser tab                                                    Server side
┌───────────────────────────────────────────┐        ┌──────────────────────────────────────────────────────────┐
│ Main thread (React + Three.js)            │        │ Realtime Gateway (FastAPI, apps/api/rt)                  │
│  useFrame(): read latest SoA buffers ─────┼─pull──┐│  ws /rt  ── ClientSession ×N                             │
│  Zustand store ← 10 Hz summary            │       ││    ├─ ctrl FIFO (JSON/TIME)  → sender task ─┐            │
│  RtClient facade (subs ref-count, clock)  │       ││    ├─ data single-slot mailbox ─────────────┤→ websocket │
│                                           │       ││    └─ subs{rate, next_due, last_seq}, credit, budget     │
│ Web Worker (rt.worker.ts)                 │       ││  Scheduler (tick 60 Hz, rate classes, encode-once cache) │
│  WebSocket('anet.rt.v1') ◄════════════════╪═══════╪╪══ Channel registry + latest-value store + event ring     │
│  decode BATCH → per-channel SoA buffers   │◄──────┘│  Sources: LiveSimSource(mock) | McapSource | Px4Source   │
│  ack(frame_seq) after main consumed       │        │  Recorder (MCAP, native rate)  | Foxglove debug bridge   │
└───────────────────────────────────────────┘        └───────────────┬──────────────────────────────────────────┘
              ▲  HTTP/2 GET (octree nodes, wind grid, MCAP, REST)      │ in-proc (MVP)  /  Zenoh bus (V0.5+)
              └────────────────────────────── FastAPI REST ────────────┤
                                                                       ▼
                     Sim workers (numpy mock / PX4 SITL×N via MAVSDK) · PX4/ROS2 (rmw_zenoh) · P600 ROS1 (rosbridge)
                     · Environment service · ANet agents (liveliness tokens, capability queryables)
```

**原则**：
- ① 浏览器只和 Gateway 通信，**不直连总线**。
- ② Gateway 是"兴趣管理加编码"层：决定谁、何时、以什么精度拿到什么数据。
- ③ WS 只传"小而热"的数据，大块数据走 HTTP。
- ④ 所有时间戳都用仿真时钟 `t_sim_ns`。

### 3.3 协议 `anet.rt.v1` 总览

- **端点**：`wss://host/api/rt?world=<id>`，子协议 `anet.rt.v1`（`Sec-WebSocket-Protocol`）。服务端选中该子协议即完成版本协商；不认识就拒绝，关闭码 1002。
- **鉴权**：
  - 握手前先通过 REST `/api/auth` 拿到一个短期 token。
  - token 通过 `Sec-WebSocket-Protocol: anet.rt.v1, bearer.<token>` 或 query 参数传入，不放 cookie，以避免 CSRF。
  - 服务端必须校验 `Origin`（rosbridge 的 `check_origin` 恒为 True，这是反例）。
- **两类帧**：
  - **文本帧**：JSON 对象，必须含 `op`。控制平面，可靠有序，低频。
  - **二进制帧**：首字节是 opcode，小端，**所有结构体 8 字节对齐**。数据平面走 BATCH，TIME 虽是二进制但走控制平面队列。
- **两个队列**：
  - 控制平面是有界 FIFO（1024），满了就发 status 并以 1013（Try Again Later）断开。
  - 数据平面是单槽 mailbox，新帧覆盖尚未发出的旧帧。
  - 发送 task 总是**优先排空控制平面**。
- **握手时序**：

```text
C: WebSocket(url, ['anet.rt.v1', 'bearer.xxx'])
S: {"op":"serverInfo", ...}                        # 能力、会话、窗口、tick、世界坐标约定、时间范围
S: {"op":"advertise","channels":[...]}              # 全量 channel 表（此后增量 advertise/unadvertise）
S: <TIME>                                           # 立刻发一帧时间
C: {"op":"hello","client":"web/0.2","resume":{"sessionId":"..","lastEventSeq":1234},"maxKbps":0}
C: {"op":"subscribe","subs":[...]}
S: {"op":"subscribed",...} ×n ; 然后 BATCH(flags=SNAPSHOT) 给出每个订阅 channel 的当前值
C: {"op":"ping","t":<performance.now ms>}  (连接后连发 5 次、间隔 100 ms，之后每 1 s 一次)
... 稳态：S→BATCH@tick，C→ack（合并），S→TIME@10Hz，S→event/status/result（控制面）
```

### 3.4 消息封包与编码选型

**选型对比**（表中数字取自 §3.1 的实测）

| 维度 | JSON | MessagePack | CBOR（typed array） | Protobuf | FlatBuffers | **raw struct（本方案）** |
|---|---|---|---|---|---|---|
| 1000 架编码（Py） | 20–41 ms | 19–23 ms | 0.16 ms（列式） | 17.8 ms | 52 ms（逐字段）/ 0.07 ms（bulk） | **0.006 ms** |
| 1000 架解码（JS） | 4.7–7.1 ms | 0.6–0.9 ms | 0.034 ms | 1.86 ms | 0.014 ms | **0.015 ms** |
| 尺寸（1000 架） | 264–388 KB | 74 KB | 57 KB | 71 KB | 64 KB | **64 KB / 32 KB（Lite）** |
| schema 演进 | 自由 | 自由 | 自由 | **强**（字段号） | 强（vtable） | 靠 advertise 的布局描述加版本号（§3.5） |
| 零拷贝 | 否 | 否 | 数组部分可以 | 否 | 可以 | **可以（对齐视图）** |
| 需要代码生成 | 否 | 否 | 否 | protoc 或 protobufjs | flatc | 否（由一份 `layouts.json` 同时生成 numpy dtype 和 TS 访问器） |
| JS 包体 | 0 | msgpackr 约 30 KB | cbor-x 约 40 KB | protobufjs 约 70–200 KB | 约 10 KB 加生成代码 | **0** |
| 可调试性 | 极好 | 好（DevTools 看不了） | 中 | 差 | 差 | 差（需要 inspector；Gateway 提供 `/api/rt/inspect`） |
| 生态互通 | 通用 | 通用 | rosbridge | Foxglove、gRPC | Foxglove | 自有（由调试桥转成 Foxglove schema） |

**决策**：

| 数据类别 | 编码 | 平面 | 理由 |
|---|---|---|---|
| 高频同构状态（DroneState、swarm、传感器 FOV 位姿） | **raw struct**（Full64/Lite32） | 数据 | 快 2–3 个数量级，零拷贝，尺寸最小 |
| 数值大数组（风场切片、LiDAR 扫描、规划轨迹、粒子种子） | **typed blob**（小头部加原始数组，可选 zstd）；超过 256 KB 走 HTTP | 数据，或 HTTP | 同上；大块数据交给 HTTP 缓存 |
| 低频异构（state_ext、safety、mission、weather 参数、agent 能力） | **MessagePack**（服务端 `msgpack` 的 C 扩展，浏览器 `msgpackr`） | 数据 | 1–10 Hz 时成本可忽略；无需 schema；比 JSON 小一半 |
| 控制（订阅、RPC、事件、状态、回放） | **JSON 文本** | 控制 | 可读，DevTools 可直接看，频率低 |
| 调试与录制互通 | Protobuf/JSON（Foxglove 标准 schema） | 旁路 | 由 foxglove-sdk 负责（r15） |

- **不用 FlatBuffers**：Python 构建器逐字段很慢；bulk 拷贝方式与 raw struct 等价，却多了一层 vtable 和 flatc 代码生成。
- **不用 Protobuf 走热路径**：Python 和 JS 都要逐对象处理。将来如果需要跨语言 IDL，比如给 C++/Rust 客户端用，可以只给 RPC 用 Protobuf。

**二进制帧布局**（全部小端；偏移单位字节；帧头和记录头都是 16 B，payload 按 8 字节补齐）：

```text
BATCH (S→C, opcode 0x10)
┌──────── FrameHeader 16 B ───────────────────────────────────────────────────────────────┐
│ 0  u8   op = 0x10                                                                        │
│ 1  u8   flags   bit0 SNAPSHOT(订阅/seek 后的首帧)  bit1 REPLAY(来自录制)  bit2 ZSTD(整体)  │
│                 bit3 GAP(自上一帧起有控制面事件丢失，需 REST 补拉)                        │
│ 2  u16  epoch   时间线纪元；与 TIME.epoch 不同的帧直接丢弃                                 │
│ 4  u32  frame_seq  每连接递增；用于 credit/ack 与丢帧检测                                  │
│ 8  u64  t_sim_ns   本 tick 的仿真时间                                                     │
├──────── RecordHeader 16 B（重复直到帧尾）─────────────────────────────────────────────────┤
│ 0  u16  channel_id                                                                       │
│ 2  u8   encoding   0 raw-struct  1 msgpack  2 json  3 typed-blob  (4..: 保留)            │
│ 3  u8   rflags     bit0 KEYFRAME  bit1 DELTA(相对上一 keyframe，V0.6+)  bit2 ZSTD         │
│ 4  u32  length     payload 实长（不含 padding）                                           │
│ 8  u32  seq        该 channel 的发布序号（跳号=服务端按 rate 采样跳过或数据源丢失）         │
│ 12 i32  dt_us      样本时刻 − FrameHeader.t_sim_ns（µs），允许同帧内不同采样时刻            │
│ 16 ...  payload[length]  + zero padding 到 8 的倍数                                      │
└──────────────────────────────────────────────────────────────────────────────────────────┘

TIME (S→C, opcode 0x02, 24 B)        u8 op | u8 state(0 paused,1 playing,2 buffering,3 ended,4 live)
                                      | u16 epoch | f32 rate | u64 t_sim_ns | u64 t_wall_ns(服务端 UNIX ns)

CLIENT_DATA (C→S, opcode 0x20, 16 B + payload)   # 遥操作 setpoint 等客户端发布
                                      u8 op | u8 flags | u16 channel_id | u32 seq | u64 t_client_sim_ns(估计值)
```

**JSON op 一览**

| 方向 | op | 字段 | 说明 |
|---|---|---|---|
| S→C | `serverInfo` | `name, protocol:"anet.rt.v1", sessionId, capabilities:["time","credit","rpc","playbackControl","clientPublish","events"], window, tickHz, rateClasses:[1,2,5,10,15,20,30,60], world:{id, frame:"ENU", origin:{lat,lon,h}}, dataStart_ns?, dataEnd_ns?, mode:"live"|"replay"` | 对应 Foxglove `serverInfo`，并吸收 v2 的 `data_start/end_time` |
| S→C | `advertise` / `unadvertise` | `channels:[{id, topic, encoding, schemaName, schema, layout?, nativeHz, kind:"state"|"event"|"blob", agents?}]` / `ids` | channel id 只在 topic、encoding 和 schema 完全相同时才复用（Foxglove 规则） |
| C→S | `hello` | `client, resume?{sessionId,lastEventSeq}, maxKbps?, role?:"viewer"|"operator"` | 断线重连时恢复订阅和事件 |
| C→S | `subscribe` | `subs:[{id, topic(可通配), rate, mode:"latest"|"all"|"m4", priority:0..3, fields?, keyframeEvery?}]` | §3.7 |
| S→C | `subscribed` | `id, channels:[ids], rate(实际 rate class), mode` | 通配订阅以后匹配到新 channel 时会再次下发 |
| C→S | `unsubscribe` | `ids:[...]` | |
| C→S | `ack` | `frame, fps?, decodeMs?, lagMs?` | 合并发送：最多 20 Hz，或每 N 帧一次 |
| C→S | `ping` / S→C `pong` | `t` / `t, server_ns, sim_ns, epoch` | §3.10 |
| C→S | `call` / S→C `result`、`progress` | `id, service, args, timeout_ms` / `id, status, code, message, data` | §3.12；`id` 标识一次交互（rosbridge 语义） |
| C→S | `cancel` | `id` | |
| C→S | `playback` / S→C `playbackState` | `cmd:"play"|"pause", speed, seek_ns?, request_id` / `status, current_ns, speed, did_seek, request_id, epoch` | 对应 Foxglove v2 的 PlaybackControlRequest/PlaybackState |
| S→C | `event` | `seq, t_sim_ns, type, level, uav?, data` | 可靠有序的通道；`seq` 在全局单调递增 |
| S→C | `status` / `removeStatus` | `level, message, id?` / `ids` | 同 id 覆盖（Foxglove），UI 持久告警条 |
| C→S | `advertise` / `unadvertise`（客户端发布） | `channels:[{id, topic, encoding, schemaName}]` | 遥操作 setpoint 等；需要 `clientPublish` 能力和 operator 角色 |
| C→S | `clientStats` | `fps, frameMs, decodeMs, heapMB, droppedFrames, pointBudget` | 1 Hz；Gateway 汇总到 `perf/clients`，同时供点云 LOD 的 FPS 反馈联动 |

### 3.5 数据布局（`packages/contracts/rt/layouts.json` 为唯一真源）

**Full64：`anet.DroneState64.v1`**（单机全精度，用于选中或视野内的飞机）

| off | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | u16 | agent_no | 在 advertise 的 `agents` 表里映射到 `uav_id`（如 `p600-01`） |
| 2 | u8 | flight_state | r24 的 FlightState 枚举（DISARMED…CRASHED） |
| 3 | u8 | flags | bit0 armed，1 in_air，2 odom_valid，3 failsafe，4 gcs_link，5 fcu_link，6 rtk_fix，7 simulated |
| 4 | u16 | mission_item | 当前航点序号，0xFFFF 表示无 |
| 6 | u8 | battery_pct | 0–100，255 表示未知 |
| 7 | u8 | authority | 0 none，1 viewer，2 operator，3 agent/autopilot（r19） |
| 8 | f32×3 | pos | World ENU，米（10 km 范围内 float32 误差 ≤ 0.7 mm，见 r15） |
| 20 | f32×3 | vel | ENU，m/s |
| 32 | f32×4 | q | [x,y,z,w]，WORLD←BODY(FLU)（与 r15、r21 一致） |
| 48 | f32×3 | omega | 机体 FLU 系，rad/s |
| 60 | i32 | dt_us | 样本时刻相对帧时刻的偏移（与 RecordHeader.dt_us 相加） |

**Lite32：`anet.SwarmLite32.v1`**（整群批量，远处或大规模时使用。它是 r21 swarm-lite 30 B 的对齐版）

| off | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | u16 | agent_no | |
| 2 | u8 | flight_state | |
| 3 | u8 | battery_pct | |
| 4 | f32×3 | pos | ENU 米 |
| 16 | i16×4 | q_snorm | ×(1/32767)，[x,y,z,w] |
| 24 | i16×3 | vel_cms | cm/s（±327 m/s） |
| 30 | u8 | flags | 同 Full64 |
| 31 | u8 | _reserved | 0 |

- 32 和 64 都是 8 的倍数，因此任何记录的起始位置都对齐。JS 端可以用 `Float32Array(buf, off, n*8)` 按跨步 8 读 pos（索引 1–3），用 `Int16Array` 按跨步 16 读 q（索引 8–11）。
- **约定**：
  - 未知的浮点值一律写 **NaN**，不写 0，也不用 null。这是二进制通道的优势；JSON 通道需要显式发 `null`，因为 rosbridge 会把 NaN 转成 null。
  - 所有角度单位为弧度，长度单位为米。
- **layout 描述**（随 advertise 下发，客户端据此自解释解码，不认识的字段直接跳过）：

```json
{"schemaName":"anet.SwarmLite32.v1","layout":{"size":32,"fields":[
 {"n":"agent_no","t":"u16","o":0},{"n":"flight_state","t":"u8","o":2},{"n":"battery_pct","t":"u8","o":3},
 {"n":"pos","t":"f32","c":3,"o":4},{"n":"q","t":"i16","c":4,"o":16,"scale":3.0518509475997192e-05},
 {"n":"vel","t":"i16","c":3,"o":24,"scale":0.01},{"n":"flags","t":"u8","o":30}]}}
```

- **演进规则**：
  - 只允许在末尾追加字段，同时把 `size` 增加 8 的倍数；schemaName 的版本号只在语义发生不兼容变化时递增。
  - 客户端按 `size` 跳记录，所以旧客户端能读新数据。
  - Python 用 `np.dtype(...)` 从 layout 生成 dtype；TS 由构建脚本生成 `layouts.ts`（访问器函数加 SoA 拷贝函数）。两边都有 round-trip 单测。

**其他载荷**：
- `uav/{id}/state_ext`：MessagePack，1–2 Hz，内容为 gps、电压、电流、GNSS 卫星数、RTK 状态、文本模式名。
- `uav/{id}/safety`：MessagePack，5–10 Hz，只推差量，字段采用 r24 的 SafetyStatus。
- `env/weather`：MessagePack，变化时发送：`{wind:{dir_deg,speed,gust,turb}, rain_mmh, fog_vis_m, cloud_cover, dust, version}`。
- `env/wind/field`：typed-blob，头部为 `u32 version | u16 nx,ny,nz | u16 comp | f32 origin[3] | f32 cell[3] | payload f16[nx*ny*nz*3]`。超过 256 KB 时 WS 只推 `{version, url}`，由客户端 GET 拉取。

### 3.6 Topic 命名

**语法**（zenoh key expression 的子集）：`chunk ('/' chunk)*`。
- chunk 取值 `[a-z0-9][a-z0-9_-]*`，全小写，不以 `/` 开头或结尾，不含空 chunk，也不含 `# $ ? @`（`@` 是 zenoh 的保留前缀）。
- **通配符只允许出现在订阅里**：`*` 匹配单个 chunk，`**` 匹配任意多个。
- 实体 ID 用稳定的业务 ID（`p600-01`、`sim-017`），不用数组下标；数组下标 `agent_no` 只出现在二进制记录里。

| topic | kind | 编码 | 原生频率 | 默认订阅 rate | mode | 优先级 | 说明 |
|---|---|---|---|---|---|---|---|
| `swarm/state` | state | raw Lite32（整群一条记录） | 50 | 10–20 | latest | 1 | 全体飞机，用于远景、小地图和统计 |
| `uav/{id}/state` | state | raw Full64 | 50 | 30（选中的 50） | latest | 0 | 视野内或选中的飞机（客户端做兴趣管理，§3.8） |
| `uav/{id}/state_ext` | state | msgpack | 2 | 2 | latest | 2 | |
| `uav/{id}/safety` | state | msgpack | 10 | 5 | latest | 1 | r24 |
| `uav/{id}/traj/planned` | state | typed-blob f32[N×4]（x,y,z,t） | 变化时 | — | latest | 2 | 规划轨迹 |
| `uav/{id}/sensor/{name}/pose` | state | raw（pos,q,fov） | 10 | 10 | latest | 2 | 相机/LiDAR FOV 可视化 |
| `uav/{id}/sensor/lidar/scan` | state | typed-blob（量化 i16 xyz） | 10 | 5 | latest | 3 | V0.4，只推选中机体；大块数据走 zstd |
| `uav/{id}/cmd` | — | — | — | — | — | — | **不是 topic**，是 RPC 服务名（§3.12） |
| `uav/{id}/setpoint` | client-publish | raw（vel/yawrate） | 20–50 | — | — | 0 | 遥操作，服务端 deadline 为 200 ms |
| `env/weather` | state | msgpack | 变化时 | — | latest | 2 | |
| `env/wind/field` | blob | typed-blob 或 URL | 变化时或 1 Hz | — | latest | 3 | |
| `mission/{mid}/status` | state | msgpack | 变化时 | — | latest | 2 | |
| `agent/{id}/status` | state | msgpack | 1 | 1 | latest | 2 | V1.0 ANet |
| `event` | event | JSON（控制平面） | — | — | all | — | 起飞完成、告警、任务节点、agent 协商 |
| `log` | event | JSON | — | — | all（限流 50/s） | — | |
| `perf/server`、`perf/clients` | state | msgpack | 1 | 1 | latest | 3 | 流畅性面板 |
| `sim/clock` | — | TIME 二进制 | 10 | — | — | — | 不需要订阅，自动下发 |

**命名映射**（端到端同名）：

| 位置 | 形式 | 例 |
|---|---|---|
| 浏览器、WS 协议 | key | `uav/p600-01/state` |
| Zenoh 总线（V0.5+） | `anet/{world}/{run}/` + key，用 zenoh 的 `namespace` 配置自动加前缀 | `anet/hefei-campus/r20260928a/uav/p600-01/state` |
| ROS2（rmw_zenoh） | `/` + key，`-` 换成 `_` | `/uav/p600_01/state` |
| ROS1（rosbridge）或 Prometheus | 由适配器表映射，不强求一致 | `/uav1/prometheus/state` → `uav/p600-01/state_src` |
| Foxglove 调试桥、MCAP | `/` + key | `/uav/p600-01/state` |

### 3.7 订阅 / 退订

```json
{"op":"subscribe","subs":[
  {"id":1,"topic":"swarm/state","rate":10,"mode":"latest","priority":1},
  {"id":2,"topic":"uav/p600-01/state","rate":50,"mode":"latest","priority":0},
  {"id":3,"topic":"uav/*/safety","rate":5,"mode":"latest","priority":1},
  {"id":4,"topic":"env/**","rate":2,"mode":"latest","priority":2},
  {"id":5,"topic":"event","mode":"all"}]}
```

- **订阅 id 由客户端选择**，同一连接内唯一，退订后可以复用（Foxglove 规则）。这里与 Foxglove 有两点不同：
  - 允许**同一 channel 被多个订阅以不同 rate 订阅**，服务端对每个 channel 取所有订阅中最高的 rate class 生成记录，**同一帧内同一 channel 只出现一次**；
  - topic 可以用通配，服务端展开成 channel 集合，**以后新出现且匹配的 channel 会自动加入**，并补发一条 `subscribed`（zenoh 订阅语义）。
- **rate 取值**：服务端把请求的 rate 量化到 rate class `{1,2,5,10,15,20,30,60}` Hz（`tickHz=60` 的约数），并用 `subscribed.rate` 告知实际值。`rate=0` 表示"原生频率，但不超过 tick"。
- **mode**：
  - `latest`：状态类。最新值采样，保证尾帧（§3.8）。
  - `all`：事件类。按序全量送达，每客户端配一个有界环（1024）；溢出时在下一个 BATCH 置 GAP 标志，并发 `status`，客户端据此用 REST `GET /api/events?since=seq` 补拉。
  - `m4`（V0.3）：曲线类。每个桶内的 min/max/first/last，与 r15 的 M4 降采样一致，给 Telemetry 图表用，省带宽。
- **订阅应答**：`{"op":"subscribed","id":3,"channels":[17,18,19],"rate":5,"mode":"latest"}`。随后第一个 BATCH 的 `flags` 带 SNAPSHOT，内含这些 channel 的**当前值**。这就是 late-joiner 快照，对应 zenoh AdvancedPublisher 的 history/cache 和 Foxglove 的 backfill。
- **退订**：`{"op":"unsubscribe","ids":[3]}`。某个 channel 的订阅者计数降到 0 时，Gateway 通知 Source 停止为它编码（Foxglove 的懒生产）。
- **重连恢复**：
  - 客户端把当前订阅表保存在 `RtClient` 里。
  - 断线后按 0.5 s、1 s、2 s、4 s、8 s 退避重连，并加 ±20% 抖动。
  - 重连后比较 `serverInfo.sessionId`：相同说明是同一服务端实例，重放订阅，并发送 `hello.resume.lastEventSeq` 补发事件；不同说明服务端重启过，清空缓存，重新订阅并清空时间线。

### 3.8 频率控制与降采样：对齐 tick 的最新值采样与尾帧保证

**问题回顾**：
- rosbridge 在 `queue_length=0` 时（`ThrottleMessageHandler.handle_message`）、zenoh 的 `DownsamplingInterceptor.intercept` 都是 **leading-edge**：窗口内第一条放行，其余丢弃。
- 数据源停止时（比如飞机悬停、任务结束），**最后一个值可能永远不会送达**，UI 就一直显示倒数第二个状态。
- zenoh 的时间戳按规则共享，还会**饿死**同一规则下的其他 key（§3.1.4）。

**我们的调度器**（每个客户端、每个订阅、每个 channel 各自维护状态；编码只按 channel 做一次）：

```python
TICK_HZ = 60; CLASSES = [1, 2, 5, 10, 15, 20, 30, 60]

def quantize(rate):                   # 向上取最近的 rate class
    return next((c for c in CLASSES if c >= rate), 60) if rate > 0 else 60

class SubChan:                        # 每 (订阅, channel) 一份，≈40 B
    period_ticks: int                 # = TICK_HZ // rate_class
    last_seq: int = 0                 # 已发给该客户端的最新 seq
    pending: bool = False             # 到期时因预算或 credit 没能发出

def on_tick(k, now_ns):               # k = 全局 tick 序号
    for c in clients:
        if c.frame_seq - c.acked >= c.window:           # 背压：客户端还没消费完
            c.stats.credit_skips += 1; continue         # 什么都不发，但 channel 最新值还在 store 里
        due = []
        for sc in c.subchans:                           # 已按优先级排序
            ch = sc.channel
            aligned = (k % sc.period_ticks == 0)        # 对齐全局 tick 网格，同 rate 的客户端同一 tick 到期
            if (aligned or sc.pending) and ch.seq > sc.last_seq:
                due.append(sc)
        records, budget = [], c.bucket.take_all()       # 令牌桶（字节），见 §3.9
        for sc in due:                                  # 优先级高的先放
            rec = sc.channel.record()                   # 按 (channel, seq) 缓存，所有客户端共享同一 bytes
            if len(rec) > budget and records: sc.pending = True; continue   # 超预算就顺延到下一 tick
            records.append(rec); budget -= len(rec)
            sc.last_seq = sc.channel.seq; sc.pending = False
        c.bucket.give_back(budget)
        if records:
            c.frame_seq += 1
            c.offer(FrameHeader(op=0x10, flags=0, epoch=clock.epoch, frame_seq=c.frame_seq, t_sim_ns=now_ns) + b"".join(records))
```

**性质**：
1. **尾帧保证**：只要 `ch.seq > last_seq`，就会在下一个对齐 tick 发出最新值，不依赖后续是否还有新样本。
2. **永远发最新**：`record()` 取的是 channel 当前的 latest。中间被跳过的 seq 表现为 `RecordHeader.seq` 的跳号，客户端据此统计有效率。
3. **对齐网格让编码可以共享**：rate 相同的客户端在同一 tick 拿到同一个 seq，`record()` 缓存命中。原型 `gw_proto.py` 用的是"各订阅独立相位"（`next_due` 各自推进），由于 10 个客户端相位分散，编码次数约等于发送次数；改成网格对齐后，同 rate 客户端的编码量降到 1/K。
4. **没有突发追赶**：因为按 `k % period` 对齐，客户端被 credit 挡住几个 tick 之后，恢复时不会一次补发多帧，只发当前最新值。
5. **频率上限来自数据源**：原生 50 Hz 的 channel 以 60 Hz 订阅时，每 tick 检查一次 `seq` 变化，实际约 50 Hz。

**空间降采样（兴趣管理）由客户端驱动**：
- 服务端不按 ROI 过滤 `swarm/state`，因为那样每个客户端的内容不同，编码就无法共享。
- 客户端每 250 ms 根据视锥、距离和选中状态计算"关注集合" F（上限 K=32 架），把 F 中飞机的 `uav/{id}/state@30Hz` 做差量订阅或退订，其余飞机只依靠 `swarm/state@10Hz` 插值显示。
- 判据沿用 r14 的屏幕半径分档：`r_px = R·H/(2·d·tan(fov/2))`。`r_px ≥ 8` 的飞机进入 F；被选中或处于 follow/FPV 的飞机提到 50 Hz。
- 效果：1000 架时带宽为 10 Hz × 32 KB（swarm）+ 32 × 30 Hz × 80 B ≈ **397 KB/s**；全部走 Full64 @30 Hz 则是 1.9 MB/s。

**基于 FPS 反馈的自适应**（与点云预算联动）：`clientStats.fps` 持续 3 s 低于 50 时，客户端自己把 swarm rate 降一档、把 K 减半；高于 58 时逐步恢复。这与点云 LOD 的 point budget 共用同一个"性能调节器"，但**通信档位要先于点云档位下调**，因为通信降档对画面的影响更小。

### 3.9 背压处理（五层）

| 层 | 位置 | 机制 | 参数 |
|---|---|---|---|
| L0 | 浏览器 | WS 在 **Worker** 中接收和解码，只保留每个 channel 的最新值（SoA 缓冲）；主线程在 `requestAnimationFrame` 中拉取（latest-per-render-tick，与 r15 的 Lichtblick `samplingRequest` 同义） | Worker→主线程每个渲染 tick 最多一次 postMessage（转移 ArrayBuffer） |
| L1 | 协议 | **credit 窗口**：服务端保证 `frame_seq − acked < W`，否则跳过本 tick（最新值仍保留在 store 里）。ack 由**主线程消费完**这一帧后经 Worker 发出，所以服务端节奏自动锁定到客户端的渲染节奏 | `W = clamp(ceil(max_rate·srtt) + 2, 2, 8)`，默认 3；ack 合并为"最多 20 Hz"或"每 3 帧一次，此时 W=6" |
| L2 | Gateway 调度 | 每订阅独立 rate，最新值采样加尾帧；**每客户端字节令牌桶**，按优先级装入，超出部分顺延 | 桶速率取 `hello.maxKbps`，未给时用 `min(8 MB/s, 1.5 × acked_bytes_per_s 的 EWMA)`；桶深 = 2 帧 |
| L3 | 连接 | **控制平面**：有界 FIFO（1024），满了就发 error status 后以 1013 断开（foxglove-sdk 的 `send_control_msg` 语义）。**数据平面**：单槽 mailbox，新帧覆盖旧帧（foxglove-sdk 的 `send_lossy` 取极限 backlog=1）。每客户端一个 sender task，没有跨客户端的队头阻塞 | TCP_NODELAY；关闭 permessage-deflate；websockets `write_limit=64 KiB` |
| L4 | TCP 兜底 | 单次 `send` 阻塞超过 200 ms，或 `transport.get_write_buffer_size()` 超过 1 MiB，就把该客户端标记为 congested，暂停数据平面直到缓冲排空，同时发 `status(warn,"network congested")` | 网络真正拥塞时才会触发（浏览器处理慢时不会触发，见 §3.1.3） |

**客户端（Worker）伪代码**：

```ts
// rt.worker.ts
let ws: WebSocket, lastFrame = 0, consumedFrame = 0, lastAckSent = 0, ackTimer = 0;
const latest = new Map<number, {buf: ArrayBuffer, off: number, len: number, seq: number, dtUs: number, tSim: bigint}>();
ws.binaryType = 'arraybuffer';
ws.onmessage = (e) => {
  if (typeof e.data === 'string') { postMessage({ctrl: e.data}); return; }       // 控制面透传（频率低）
  const dv = new DataView(e.data); const op = dv.getUint8(0);
  if (op === 0x02) { postMessage({time: parseTime(dv)}); return; }
  if (op !== 0x10) return;
  const epoch = dv.getUint16(2, true); if (epoch !== curEpoch) return;           // 旧纪元帧直接丢
  lastFrame = dv.getUint32(4, true); const tSim = dv.getBigUint64(8, true);
  for (let o = 16; o < dv.byteLength; ) {
    const ch = dv.getUint16(o, true), len = dv.getUint32(o + 4, true), seq = dv.getUint32(o + 8, true), dt = dv.getInt32(o + 12, true);
    latest.set(ch, {buf: e.data, off: o + 16, len, seq, dtUs: dt, tSim});        // 只记引用，不复制
    o += 16 + len + ((8 - (len & 7)) & 7);
  }
  if (pullPending) flush();                                                       // 主线程已在等待：立即给
};
onmessage = (m) => { if (m.data.pull) { pullPending = true; if (latest.size) flush(); } ... };
function flush() {                                    // 把每个 channel 的最新记录解码到 SoA，然后转移
  const out = decodeToSoA(latest);                    // 例如 swarm → Float32Array pos[3N]、quat[4N]；uav → Float32Array[16]
  latest.clear(); pullPending = false; consumedFrame = lastFrame;
  postMessage(out, out.transfer);
  maybeAck();
}
function maybeAck() {                                 // 合并 ack：每 3 帧一次，或距上次超过 50 ms
  if (consumedFrame - lastAckSent >= 3 || performance.now() - ackTimer > 50) {
    ws.send(`{"op":"ack","frame":${consumedFrame}}`); lastAckSent = consumedFrame; ackTimer = performance.now(); }
}
// 主线程：rAF → worker.postMessage({pull:1})；收到 SoA 后写入 Three 的 InstancedBufferAttribute 或插值环
```

**事件通道的可靠性**（`mode:"all"`）：
- 服务端给每个客户端维护 `EventRing(1024)`，并保存全局 `event_seq`。
- 控制平面满了就断开，所以正常情况下不会丢事件；断线期间的事件在重连后按 `lastEventSeq` 从环中补发，环里已经没有的就回 GAP，由客户端走 REST 补拉。
- 这对应 zenoh AdvancedSubscriber 的"序号跳变 → 区间补发"。

### 3.10 时间同步（simTime）

**时钟模型**：
- 服务端维护唯一的**仿真时钟** `SimClock{t_sim_ns, rate, state, epoch}`。Mock 仿真推进它；回放时由 McapSource 驱动；接入真机时 `rate=1`、`state=live`，`t_sim_ns` 由 PX4 的 `time_boot`/timesync 或 GPS 时间换算得到（r21 §3.9）。
- 所有 channel 样本都带 `t_sim_ns`（帧头时刻加 `dt_us`），与 Foxglove 规则一致：如果发 time，所有时间戳必须来自同一个时钟源。
- TIME 消息 10 Hz 发送；状态变化（播放、暂停、调速、seek）时立即补发一条。
- **墙钟偏移**：客户端以 `ping{t=performance.now()}` 发出，服务端回 `pong{t, server_ns, sim_ns}`。估计算法为 Cristian 算法加最小 RTT 过滤：

```ts
class ClockSync {                                   // 连接后先连发 5 次 ping（间隔 100 ms），之后每 1 s 一次
  private s: {rtt: number, off: number}[] = [];      // off: serverWallMs − clientMonoMs
  off = 0; srtt = 0; ready = false;
  onPong(t0: number, t1: number, serverNs: bigint) {
    const rtt = t1 - t0, off = Number(serverNs) / 1e6 - (t0 + t1) / 2;
    this.s.push({rtt, off}); if (this.s.length > 16) this.s.shift();
    const best = this.s.reduce((a, b) => (b.rtt < a.rtt ? b : a));   // RTT 最小的样本误差最小（≤ rtt/2）
    if (!this.ready || Math.abs(best.off - this.off) > 50) { this.off = best.off; this.ready = true; }  // 大偏差直接跳变
    else this.off += 0.1 * (best.off - this.off);                                                       // 小偏差平滑
    this.srtt = this.srtt ? 0.875 * this.srtt + 0.125 * rtt : rtt;                                     // 用于计算 credit 窗口 W
  }
  serverNowMs(now = performance.now()) { return now + this.off; }
}
class SimClockView {
  ref = {simNs: 0n, wallNs: 0n, rate: 1, state: 4, epoch: 0};
  onTime(t) { if (t.epoch !== this.ref.epoch) { bus.emit('epoch', t.epoch); /* 清空插值环、尾迹、事件视图 */ } this.ref = t; }
  simNowNs(): number {                                // 显示用，double 精度足够（2^53 ns ≈ 104 天）
    const {simNs, wallNs, rate, state} = this.ref;
    if (state === 0 || state === 3) return Number(simNs);                              // 暂停或结束：冻结
    return Number(simNs) + rate * (clock.serverNowMs() * 1e6 - Number(wallNs));
  }
}
// 渲染时刻（r14）：tRender = simNow − D，D = clamp(2/hz_eff + jitter_p95, 60 ms, 250 ms)
// hz_eff 为该 channel 最近 1 s 内实际收到的频率（由 seq 与到达间隔估计），jitter 为到达时刻相对 t_sim 的偏差分位数
```

**要点**：
- 一律使用 `performance.now()` 这类单调时钟，不用 `Date.now()`，后者可能被 NTP 调整。
- sim 时间用 `u64 ns` 表示，JS 端转换成相对会话起点的 double 毫秒后再交给着色器使用。r15 已论证 f32 的秒必须相对于会话起点。
- 局域网 RTT 小于 1 ms 时，偏移误差约为 0.5 ms，远小于一个渲染帧，足够用。

### 3.11 回放（Replay）

1. **录制**（`apps/api/rt/recorder.py`，V0.2）：
   - Gateway 在 Source 的发布点（降采样之前）把所有 channel 按原生频率写入 MCAP：`channel = topic`，`schema = layout JSON`（`schemaEncoding="anet-layout"`、`messageEncoding="anet-raw"` 或 `"msgpack"`），`log_time = publish_time = t_sim_ns`，使用 zstd chunk。
   - 与此同时，由 foxglove-sdk 旁路写一份 Foxglove 标准 schema（FrameTransforms、LocationFix 等，r15 §3.19），用 Lichtblick 直接打开。
   - 体积估算：20 架 × 50 Hz × 64 B ≈ 64 KB/s，zstd 之后约 45 KB/s，10 分钟约 27 MB。
2. **Source 抽象**：

```python
class Source(Protocol):
    mode: Literal["live", "replay"]
    def channels(self) -> list[ChannelSpec]: ...
    async def run(self, clock: SimClock, publish: Callable[[int, bytes, int], None]) -> None: ...  # publish(ch, payload, t_ns)
    def seek(self, t_ns: int) -> dict[int, tuple[bytes, int]]: ...   # 返回每个 channel ≤ t 的最后一条（backfill）
    def set_speed(self, r: float) -> None: ...
    def pause(self) -> None: ...; def play(self) -> None: ...

class McapSource(Source):   # MCAP 有 chunk index 和 message index，按 channel 二分查找 ≤ t 的最后一条，复杂度 O(log n)
class LiveSimSource(Source):# Mock 仿真：pause、调速都支持；seek(t < now) 需要 V0.4 的 checkpoint
```

3. **播放控制**（语义对齐 Foxglove v2）：
   - 客户端发 `{"op":"playback","cmd":"play","speed":2.0,"seek_ns":<opt>,"request_id":"r17"}`。
   - 服务端依次执行：
     1. `epoch += 1`；
     2. `backfill = source.seek(t)`，写入各 channel 的 latest store，并把 seq 置为新值；
     3. 立即发 `TIME(epoch, state)` 和 `playbackState{status, current_ns, speed, did_seek:true, request_id, epoch}`；
     4. 下一个 BATCH 带 SNAPSHOT，包含所有订阅 channel 的 backfill 值；
     5. 之后正常推进。
   - 播放状态为 `playing | paused | buffering | ended`；倍速档位沿用 r15（0.1…10×）；`serverInfo` 中给出 `dataStart_ns/dataEnd_ns`，供时间轴显示范围。
   - 客户端遇到 epoch 变化时：清空插值环和尾迹，时间轴吸附到新位置，环境效果走"无状态重建"（r16 §3.8）。
4. **实时仿真的倒带与分叉**（V0.4）：
   - LiveSimSource 每 1 s 把完整的仿真状态（numpy 数组、随机数生成器状态、任务状态机）保存到内存环里（10 分钟共 600 个；200 架约 200 KB/个，合计约 120 MB，可配置）。
   - `seek(t<now)`：先恢复 t 之前最近的 checkpoint，再**确定性地**快进到 t。前提是固定种子，外部输入（用户命令、风场变化）都记录在事件日志里并在快进时重放。
   - "从此刻分叉"：从某个 checkpoint 起用不同的参数（例如风速加 3 m/s）重跑，得到新的 `run_id`，UI 上显示为两条时间线。这是科研平台区别于普通回放器的能力。
5. **真机与仿真同构**：真实 P600 的飞行日志（ULog 或 rosbag）离线转换成同一套 channel 的 MCAP，就能用同一个 McapSource 回放（对应原设计 §39 的"回放真实飞行"）。

### 3.12 RPC 与命令

```json
C: {"op":"call","id":"c-7f3a","service":"uav/p600-01/cmd/goto","args":{"pos":[120,40,60],"speed":5,"yaw":null},"timeout_ms":3000}
S: {"op":"result","id":"c-7f3a","status":"accepted","code":0}                 # 立即回复：准入通过（r24 的指令准入）
S: {"op":"progress","id":"c-7f3a","data":{"dist_m":82.1,"eta_s":16.4}}         # 可选，≤ 2 Hz
S: {"op":"result","id":"c-7f3a","status":"succeeded","code":0,"final":true}
```

- `status` 取值：`accepted | rejected | running | succeeded | failed | canceled | timeout`。`code` 对齐 MAVSDK/r21 的 ResultStatus；`rejected` 时附带 `reason`（例如 "GEOFENCE"、"NOT_ARMED"、"AUTHORITY"）。
- **幂等**：`id` 由客户端生成 UUID。服务端 60 s 内对同一 id 只执行一次，重复请求直接返回已有结果，避免重连后重发导致重复执行。
- **权限**：
  - `hello.role`：viewer 只能订阅；operator 可以 call 或 publish。
  - 每架飞机同一时刻**只能有一个 operator 持锁**（`call uav/{id}/cmd/acquire`），UI 显示持锁人。这是多人同时访问同一沙盘的必要机制，原设计 §10 提到了"多人访问"，但没有定义控制权。
- **服务命名**：`uav/{id}/cmd/{arm,disarm,takeoff,land,goto,follow_path,orbit,hold,rtl,kill,acquire,release}`、`mission/{mid}/{start,pause,abort}`、`env/set`、`sim/{pause,play,speed,reset,checkpoint}`、`world/query/{raycast,height,los}`。V0.5 以后，`world/query/*` 由 zenoh queryable 提供。
- **遥操作 setpoint**：客户端先 `advertise{channels:[{id:1,topic:"uav/p600-01/setpoint",encoding:"raw",schemaName:"anet.VelSetpoint16.v1"}]}`，然后以二进制 CLIENT_DATA 按 20–50 Hz 发送。服务端的处理：
  - 丢弃 `t_client_sim_ns` 早于当前 sim 时间 200 ms 以上的包；
  - 超过 500 ms 没有新 setpoint 就切到 Hold（比 PX4 的 `COM_OF_LOSS_T=1s` 更保守）；
  - 这个通道不受 credit 窗口约束，因为它是上行方向。

### 3.13 Gateway 服务端结构（FastAPI，`apps/api/rt/`）

```text
apps/api/rt/
├── protocol.py      # struct 定义（FRAME_HDR "<BBHIQ"=16B：op,flags,epoch,frame_seq,t_sim_ns（原型 gw_proto.py 第 3 字段为 count，规范改为 epoch）；REC_HDR "<HBBIIi"=16B；TIME "<BBHfQQ"=24B；CLIENT_DATA）、opcode、JSON op 的 pydantic 模型
├── layouts.py       # 读 packages/contracts/rt/layouts.json → np.dtype；Full64/Lite32 校验（itemsize%8==0）
├── channels.py      # Channel{id,topic,encoding,schema,seq,t_ns,payload,_rec_cache}、ChannelRegistry（topic→id，通配匹配用前缀 trie + fnmatch）、EventRing
├── scheduler.py     # tick 循环（60 Hz）、rate class、对齐网格、令牌桶、credit
├── session.py       # ClientSession：subs、subchans、ctrl FIFO、data 单槽、sender task、统计
├── clock.py         # SimClock（t_sim、rate、state、epoch）
├── sources/live.py  # LiveSimSource：numpy mock 动力学（r21 §3.11）→ 发布 swarm/state + uav/*/state
├── sources/mcap.py  # McapSource（V0.2）
├── recorder.py      # MCAP 写入（V0.2）
├── rpc.py           # call 路由、幂等缓存、权限与锁
├── bridges/foxglove_debug.py   # V0.2（r15）
├── bridges/zenoh_bus.py        # V0.5：总线 ↔ ChannelRegistry
├── bridges/rosbridge_client.py # V0.5：P600 ROS1
└── ws.py            # @app.websocket("/api/rt")
```

**关键实现细节**（已在 `gw_proto.py` 中验证）：

```python
class Channel:
    def publish(self, payload: bytes, t_ns: int):         # Source 调用：只替换引用，O(1)
        self.seq += 1; self.t_ns = t_ns; self.payload = payload; self._rec_cache = None
    def record(self) -> bytes:                            # Scheduler 调用：按 (channel, seq) 编码一次，所有客户端共享
        if self._rec_cache is None:
            n = len(self.payload)
            self._rec_cache = REC_HDR.pack(self.id, self.encoding, 0, n, self.seq, 0) + self.payload + b"\0" * ((-n) % 8)
        return self._rec_cache

class ClientSession:
    async def sender(self):                                # 每客户端一个 task：先发控制，再发数据；没有跨客户端的队头阻塞
        while True:
            if not self.ctrl.empty(): await self._send(self.ctrl.get_nowait()); continue
            if self.slot is not None:
                f, self.slot = self.slot, None; await self.ws.send_bytes(f); continue
            await wait_any(self.ctrl_nonempty, self.slot_filled)
    def offer(self, frame: bytes): self.slot = frame; self.slot_filled.set()   # 单槽：新帧覆盖旧帧
    def send_ctrl(self, obj):
        try: self.ctrl.put_nowait(orjson.dumps(obj))
        except asyncio.QueueFull: self.close(1013, "control backlog full")    # foxglove-sdk 的语义：控制平面不能丢
```

**Mock 仿真与 Gateway 的衔接**：
- 仿真步进以 100 Hz 运行在独立的 asyncio task 里；r21 实测 1000 架每步 0.36 ms，可以留在同一进程。
- 每 2 个物理步（50 Hz）发布一次：swarm 发一次 `Lite32.tobytes()`（32 KB）；每架 `uav/{id}/state` 发布 `full.tobytes()` 的 64 B 切片。切片来自 bytes，属于复制，1000 次约 0.2 ms；优化办法是只对"有订阅者"的 uav channel 执行 publish，即懒生产。

**容量与扩展路径**：

| 规模 | 方案 |
|---|---|
| MVP（1–5 个客户端、≤ 200 架） | 单进程：FastAPI、Mock 仿真、Gateway 全在一起；实测 CPU < 20% |
| V0.6（≤ 40 个客户端或 1000 架） | 仿真与 Gateway 分成两个进程，经 zenoh 连接（同机 SHM）；Gateway 可以用 `SO_REUSEPORT` 起多个 worker，各自订阅总线 |
| V1.0+（上百客户端或公网演示） | 选项 A：多个 Gateway 进程放在 L7 负载均衡后面，按 world 分片；选项 B：数据面网关用 Rust 重写（参考 foxglove-sdk 的 `connected_client.rs` 加 tokio-tungstenite），Python 只保留控制面 |

### 3.14 浏览器客户端结构（`apps/web/src/net/rt/`）

```text
rt.worker.ts        # 持有 WebSocket；解析 BATCH/TIME；按 channel 保留最新；按需解码成 SoA；ack 合并；ping 定时器
client.ts           # RtClient：订阅引用计数（组件 A、B 都订阅同一 topic 时只发一次，取最大 rate）、重连、sessionId 判断、ClockSync、SimClockView、call() Promise
layouts.ts          # 由 layouts.json 生成：decodeSwarmLite32(view, n, outPos, outQuat, outVel…)、decodeDroneState64(...)
useRtTopic.ts       # React hook：useRtTopic('uav/p600-01/state', {rate:30}) → 返回 ref，数据不放进 React state
store.ts            # Zustand：按 10 Hz 节流写入 UI 摘要（电量、模式、告警），供右侧 shadcn 面板使用
telemetry-ring.ts   # 每架飞机的快照环 {t_sim, p, q, v}（r14 §3.6，容量 32），给 60 FPS 插值使用
```

- **React 性能原则**：高频数据**绝不**进入 React state 或 Zustand 的每帧更新；3D 层在 `useFrame` 里直接读 ref 和 SoA；UI 面板每 100 ms 从 store 读一次摘要。
- **SharedArrayBuffer**（可选，V0.6+）：Worker 可以直接写 SAB 环形缓冲，主线程零拷贝读取。但页面必须满足跨源隔离（COOP: same-origin + COEP: require-corp），所有 CDN 资源（字体、Google Fonts、图标）都要带 CORP 头。MVP 先用可转移的 ArrayBuffer，实测已足够。
- **多视图**（主视图加 FPV 画中画）：共用同一个 RtClient，兴趣集合取并集。

### 3.15 Zenoh 内部总线设计（V0.5+）

**key 空间**：会话配置 `namespace: "anet/{world}/{run}"`，所有 key 自动加前缀，业务代码只写相对 key，与 WS topic 同名。

| key（相对） | 发布者 | 优先级 | 拥塞控制 | express | 其他 |
|---|---|---|---|---|---|
| `uav/{id}/cmd/**`（queryable） | 各机 controller | InteractiveHigh(2) | Block | ✓ | 用 query/reply 表达 RPC；reply 给出准入结果 |
| `uav/{id}/setpoint` | Gateway | RealTime(1) | Drop | ✓ | 遥操作 |
| `uav/{id}/state` | 仿真 worker / PX4 适配器 | Data(5) | Drop | ✗ | 50–100 Hz；Gateway 用 `RingChannel(1)` 接收 |
| `swarm/state` | 仿真 worker | Data(5) | Drop | ✗ | 可放 SHM（≥ 3072 B 时自动走 SHM） |
| `uav/{id}/sensor/**/scan` | 传感器仿真 | Background(7) | Drop | ✗ | SHM；真机链路加 `low_pass_filter` 限制大小 |
| `env/**` | 环境服务 | DataLow(6) | Drop | ✗ | AdvancedPublisher `cache(max_samples=1)`，late joiner 可立刻拿到当前天气 |
| `event/**` | 各服务 | InteractiveLow(3) | Block | ✗ | AdvancedPublisher：seq、`cache(100)`、`sample_miss_detection(heartbeat 1 s)`；Gateway 端用 AdvancedSubscriber 做 recovery |
| `world/query/{raycast,height,los}`（queryable） | World Service | InteractiveHigh(2) | Block | ✓ | 空间查询 RPC |
| `agent/{id}/alive`（liveliness token） | 每个 agent/drone | — | — | — | 在线状态：graceful 0.5 ms，kill 6–8 ms，静默断链看 lease |
| `agent/{id}/capability`（queryable） | 每个 agent | InteractiveLow(3) | Block | — | ANet 能力描述（`thermal.imaging`、`rgb.zoom`…），用 `get("agent/*/capability")` 一次性发现全部 |

**配置要点**（`DEFAULT_CONFIG.json5` 对应项）：

```json5
{
  mode: "peer",                                  // 同机或局域网；跨网段时加 router
  namespace: "anet/hefei-campus/r20260928a",
  scouting: { multicast: { enabled: false } },   // 显式 connect，避免在实验室网络里误连
  transport: {
    link: { tx: { lease: 3000, keep_alive: 4,     // 3 s 内检测到静默断链（真机数传或 4G）
                  queue: { congestion_control: { drop: { wait_before_drop: 1000 } } } } },
    shared_memory: { enabled: true },
  },
  // 真机数传链路：每个 key 单独一条规则，不要用通配规则（否则会饿死其他 key，见 §3.1.4）
  downsampling: [{ interfaces: ["wwan0"], flows: ["egress"], messages: ["put"],
                   rules: [{ key_expr: "uav/p600-01/state", freq: 10.0 }, { key_expr: "uav/p600-02/state", freq: 10.0 }] }],
  low_pass_filter: [{ interfaces: ["wwan0"], flows: ["egress"], messages: ["put"], key_exprs: ["uav/*/sensor/**"], size_limit: 8192 }],
  access_control: { enabled: true, default_permission: "deny", /* 按证书 CN 区分 gateway、sim、agent */ },
}
```

**说明**：
- zenoh 的 downsampling 仍然是 leading-edge。对真机状态来说，50 Hz 降到 10 Hz 时丢尾帧的影响可以接受，因为数据会持续发布。如果必须保证尾帧，应在发布端自己做"最新值 + 定时发送"，即 §3.8 的算法。
- **ROS2 互通**：PX4 ROS2（uXRCE-DDS）或 ROS2 节点使用 `rmw_zenoh_cpp`，会直接出现在同一个 zenoh 网络里。它们的 key 形如 `<domain>/<topic>/<type>/<hash>`，Gateway 的 `zenoh_bus.py` 通过映射表把需要的 key 转换成我们的 channel。

**Python 用法骨架**：

```python
import zenoh, zenoh.ext as zx
s = zenoh.open(conf)
pub = s.declare_publisher("swarm/state", congestion_control=zenoh.CongestionControl.DROP,
                          priority=zenoh.Priority.DATA, express=False)
pub.put(lite.tobytes())                                              # ≥3072 B 在同机会自动走 SHM
sub = s.declare_subscriber("uav/*/state", zenoh.handlers.RingChannel(1))   # 最新值 mailbox，也可用 callback 写 Channel.publish
tok = s.liveliness().declare_token("agent/p600-01/alive")
ls  = s.liveliness().declare_subscriber("agent/*/alive", on_presence, history=True)
qa  = s.declare_queryable("agent/p600-01/capability", lambda q: q.reply(q.key_expr, caps_json))
for r in s.get("agent/*/capability", timeout=1.0): ...              # ANet 能力发现
evp = zx.declare_advanced_publisher(s, "event/sim", cache=zx.CacheConfig(max_samples=100),
                                    sample_miss_detection=zx.MissDetectionConfig(heartbeat=1.0))
```

### 3.16 rosbridge 适配器（P600 / Prometheus ROS1，V0.5）

**部署**：
- 机载 Orin NX 上已有 ROS1 Noetic，直接 `apt`/源码装 rosbridge_server 0.11.x。
- 地面端用 `ros:noetic` docker 运行 `roslaunch rosbridge_server rosbridge_websocket.launch port:=9090`。
- 同时设置 `topics_glob`/`services_glob` 白名单，只开放 Prometheus 的状态和命令 topic。

**不要用 roslibpy**：2.1.0 的 `Topic.SUPPORTED_COMPRESSION_TYPES = ("png","none")`，`comm_autobahn.py::onMessage` 收到二进制帧直接 `raise NotImplementedError`，而且依赖 Twisted/autobahn，与 FastAPI 的 asyncio 不融合。协议很简单，我们自研一个客户端：

```python
# apps/api/rt/bridges/rosbridge_client.py（约 150 行）
import asyncio, itertools, orjson, cbor2, numpy as np, websockets
TAGS = {69:"<u2",70:"<u4",71:"<u8",72:"i1",77:"<i2",78:"<i4",79:"<i8",85:"<f4",86:"<f8"}   # rosbridge cbor_conversion.py
def tag_hook(tag, immutable=False):                                                          # cbor2>=6 的签名：(tag, immutable)
    dt = TAGS.get(tag.tag); return np.frombuffer(tag.value, dt) if dt else tag

class RosbridgeClient:
    def __init__(self, url): self.url, self.ids, self.subs, self.pending = url, itertools.count(), {}, {}
    async def run(self):
        async for ws in websockets.connect(self.url, max_size=None, compression=None):  # 自动重连
            self.ws = ws
            for topic, (typ, thr, cb) in self.subs.items(): await self._send_sub(topic, typ, thr)
            try:
                async for m in ws:
                    msg = cbor2.loads(m, tag_hook=tag_hook) if isinstance(m, bytes) else orjson.loads(m)
                    op = msg.get("op")
                    if op == "publish": self.subs[msg["topic"]][2](msg["msg"])          # 回调里只写 mailbox，不做重活
                    elif op == "service_response": self.pending.pop(msg["id"]).set_result(msg)
            except websockets.ConnectionClosed: continue
    async def _send_sub(self, topic, typ, throttle_ms):
        await self.ws.send(orjson.dumps({"op":"subscribe","id":f"s:{topic}","topic":topic,"type":typ,
            "throttle_rate":throttle_ms,"queue_length":1,"compression":"cbor"}))       # queue_length:1 → 最新值 + 尾帧
    def subscribe(self, topic, typ, cb, throttle_ms=0): self.subs[topic] = (typ, throttle_ms, cb)
    async def call(self, service, args, timeout=5.0):
        i = f"c:{next(self.ids)}"; fut = asyncio.get_running_loop().create_future(); self.pending[i] = fut
        await self.ws.send(orjson.dumps({"op":"call_service","id":i,"service":service,"args":args}))
        return await asyncio.wait_for(fut, timeout)
    async def publish(self, topic, typ, msg):                                          # ROS1：publish 会自动注册发布者但忽略 type，topic 不在 master 上时须先 advertise
        await self.ws.send(orjson.dumps({"op":"publish","topic":topic,"type":typ,"msg":msg}))
```

**参数建议**：
- 状态 topic 用 `throttle_rate = 1000/目标Hz` 加 `queue_length:1`（切到 QueueMessageHandler，发送最新值并保证尾帧），`compression:"cbor"`。
- 点云（`sensor_msgs/PointCloud2`）**不走 rosbridge**，因为 data 是 `uint8[]`，JSON 下要 base64，CBOR 下是 bytes，仍然很重。机载端应先抽稀或量化再发，或者走 r04 建议的独立 Sensor Gateway。
- 命令走 topic（Prometheus 的 `UAVCommand`），或者走 `call_service`，但后者每次都新建线程和 client，适合低频配置调用。
- ROS1 分支的 `publish`（`capabilities/publish.py::Publish.publish` → `manager.register`）会自动注册发布者，但**不读 `type` 字段**，类型要从 ROS master 推断。所以当 topic 在 master 上还不存在时（比如 Prometheus 节点还没起来），必须先 `advertise` 并指明 type。ros2 分支 4.1.0（#1203）才支持在 publish 里带 `type` 自动建立注册。

---
## 4. 在本项目中的落点与复用方式

| # | 能力 | 来源（文件/函数） | 落点模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|---|
| 1 | op 信封 + JSON 控制面 + 二进制数据面；serverInfo（capabilities/sessionId）；advertise（channel+schema）；subscribe/unsubscribe；status/removeStatus | ws-protocol `docs/spec.md`；`FoxgloveServer.ts::#handleClientMessage`；`parse.ts` | `apps/api/rt/protocol.py`、`apps/web/src/net/rt/` | V0.1 | **port**（概念），线格式自研 | 成熟范式；但 13/17 B 头部会导致 payload 不对齐，规范本身也已声明过时 |
| 2 | 16 B 对齐帧头 + 16 B 记录头 + BATCH | 本文 §3.4（实测 §3.1.2） | 同上 | V0.1 | **自研** | 零拷贝 TypedArray；每 tick 只有一次 onmessage |
| 3 | Full64 / Lite32 布局 + layouts.json 代码生成 | 本文 §3.5；r19 的 40 B 与 r21 的 30 B 合并 | `packages/contracts/rt/`、`layouts.py`、`layouts.ts` | V0.1 | **自研** | 编码 5.7 µs、解码 15 µs（1000 架） |
| 4 | 懒生产（首订阅 / 末退订） | `FoxgloveServer.ts::#anySubscribed` 和 subscribe/unsubscribe 事件 | `channels.py`、`sources/*` | V0.1 | **port** | 没人看的 channel 不编码 |
| 5 | 扇出编码缓存（按 (channel, seq) 编码一次） | rosbridge `OutgoingMessage.get_cbor`；`MultiSubscriber.callback` | `channels.py::Channel.record` | V0.1 | **port**（改进为连 JSON 也缓存） | N 个客户端只编码一次 |
| 6 | 最新值采样 + 尾帧保证 + 对齐 tick 网格 + rate class | 反面教材：rosbridge `ThrottleMessageHandler`、zenoh `DownsamplingInterceptor`；正面参考：rosbridge `QueueMessageHandler(queue_length=1)` | `scheduler.py` | V0.1 | **自研** | 实测 leading-edge 丢尾帧，通配规则会饿死其他 key |
| 7 | 控制/数据平面分离；控制满则断开，数据单槽覆盖 | foxglove-sdk `connected_client.rs::send_control_msg`、`send_lossy.rs::send_lossy`、`poller.rs` | `session.py` | V0.1 | **port** | 关键消息不丢，状态数据不堆积 |
| 8 | 每客户端一个 sender task（避免队头阻塞） | 反例：ws-protocol Python `send_message` 串行 await | `session.py` | V0.1 | **自研** | 一个慢客户端不能拖累其他客户端 |
| 9 | credit 窗口 + ack 合并 | 本文 §3.9（实测 §3.1.3、§3.1.5） | `scheduler.py`、`rt.worker.ts` | V0.1 | **自研** | 浏览器没有接收背压 |
| 10 | Worker 持有 WebSocket + 转移 ArrayBuffer | ws-protocol `test-client-web-app/src/WorkerSocketAdapter.ts`、`worker.js` | `rt.worker.ts` | V0.1 | **port** | 主线程减负；latest-per-render-tick |
| 11 | TIME 消息 + ping/pong 最小 RTT 时钟同步 + epoch | ws-protocol `Time(0x02)`；foxglove-sdk v2 `Ping/Pong`；Cristian 算法 | `clock.py`、`client.ts::ClockSync` | V0.1 | **port**（语义）+ 自研 | 统一 simTime，seek 后可以安全清空缓冲 |
| 12 | Playback 控制（play/pause/speed/seek、request_id、did_seek、状态枚举） | foxglove-sdk `protocol/common/client/playback_control_request.rs`、`server/playback_state.rs` | `rt/ws.py` 的 playback op、Timeline UI | V0.2 | **port**（语义） | 与 Foxglove 调试桥直接对应 |
| 13 | seek backfill（每个 channel 取 ≤ t 的最后一条）+ SNAPSHOT | zenoh-ext `advanced_cache.rs`（history）；r15 Lichtblick backfill | `sources/mcap.py::seek` | V0.2 | **port** | 拖动时间轴即时显示 |
| 14 | 可靠事件：全局 seq + 环 + 缺口补拉 | zenoh-ext `advanced_subscriber.rs::handle_sample`（`_sn=` 区间补发、`Miss`） | `channels.py::EventRing`、REST `/api/events` | V0.2 | **port** | 事件不能丢；重连后可以补齐 |
| 15 | RPC `call/result/progress`（id 标识一次交互） | rosbridge `call_service`/`service_response`/action 系列 | `rpc.py` | V0.2 | **port** | 命令需要准入、进度和最终结果 |
| 16 | glob 白名单 ACL + Origin 校验 + token | rosbridge `topics_sub_glob`/`services_glob`（fnmatch）；反例：`check_origin` 恒为 True | `rt/ws.py`、`rpc.py` | V0.2 | **port** | 最小安全面 |
| 17 | MCAP 录制 + 调试旁路 | foxglove-sdk（r15 §3.19） | `recorder.py`、`bridges/foxglove_debug.py` | V0.2 | **adopt** | 已在 r15 定案 |
| 18 | topic 命名采用 key expression 语法 | zenoh `commons/zenoh-keyexpr`（borrowed.rs 文档） | 全系统命名规范 | V0.1 | **adopt**（规范） | 未来接入 zenoh 或 ROS2 时名字不用改 |
| 19 | 8 级优先级 / Drop-Block / express 分级表 | zenoh `zenoh-protocol/src/core/mod.rs`、`builders/publisher.rs` | §3.6 的 topic 表（WS 端表现为 priority 字段），V0.5 总线 | V0.1 表 / V0.5 实装 | **reference → adopt** | 统一 QoS 语义 |
| 20 | zenoh 内部总线（pub/sub/query/liveliness、SHM、namespace、ACL） | `eclipse-zenoh` 1.10.1 wheel | `bridges/zenoh_bus.py`、仿真 worker、环境服务 | V0.5 | **adopt** | 多进程或多机、ROS2 互通 |
| 21 | ANet 在线与能力发现（liveliness token + capability queryable） | zenoh `api/liveliness.rs`、`queryable.rs` | `agent/anet/` | V1.0 | **adopt** | 在线检测 0.5 ms（graceful）；一次 get 即可发现全部能力 |
| 22 | AdvancedPublisher cache / miss detection | `zenoh-ext/src/advanced_publisher.rs` | 事件与环境 topic | V0.5 | **adopt** | late joiner 立即拿到当前值；丢包可感知 |
| 23 | 真机链路 downsampling / low_pass | zenoh `interceptor/downsampling.rs`、`low_pass.rs` | zenoh 配置（每个 key 单独一条规则） | V0.5 | **adopt**（按规则配置） | 数传或 4G 带宽保护 |
| 24 | rosbridge ROS1 服务端 + 自研 asyncio 客户端（CBOR） | rosbridge ros1 分支 0.11.18；`cbor_conversion.py` 的 tag 表 | `bridges/rosbridge_client.py` | V0.5 | **adopt**（服务端）+ **自研**（客户端） | P600 是 ROS1；roslibpy 不支持 CBOR |
| 25 | CBOR typed-array 转 numpy 解码 | `rosbridge_cbor_hook.py`（本单元） | 同上 | V0.5 | **自研** | 1000 架 57 KB 解码 27 µs |
| 26 | REST SSE 调试流 | zenoh `plugins/zenoh-plugin-rest` | — | — | **reference** | base64 化，不适合生产 |
| 27 | zenoh-ts / remote-api 浏览器直连 | zenoh-ts、`zenoh-plugin-remote-api/src/lib.rs` | — | — | **skip** | 无界队列、每 tab 一个 session、没有兴趣管理 |
| 28 | rosbridge PNG 压缩、应用层分片、roslibpy | rosbridge `pngcompression.py`、`fragmentation.py`；roslibpy | — | — | **skip** | 旧时代的变通；不支持二进制 |
| 29 | Foxglove 协议作为主协议 | ws-protocol / foxglove-sdk | — | — | **skip**（只借语义） | 规范自称过时，v2 还在变，头部不对齐 |
| 30 | permessage-deflate | 三个仓库均支持 | — | — | **skip** | 对 float 数据只省 7–13%，却消耗 Python CPU |

**MVP（V0.1）必做清单**：第 1–11、18 项。其中第 11 项先只做 TIME 和 ping；epoch 与 seek 到 V0.2 随 Timeline 一起做。

---

## 5. 对比与推荐

| 维度 | rosbridge_suite | Foxglove ws-protocol → foxglove-sdk | Zenoh |
|---|---|---|---|
| Star / 2026 活跃度 | 1246 / 活跃（ros2 分支）；ros1 分支停更 | 150（已归档）/ SDK 311，活跃 | **3216 / 很活跃**（每月发版，rmw_zenoh） |
| 抽象层级 | ROS 图 ↔ Web（通用桥） | 可视化工具 ↔ 数据源（Web 协议） | 进程、主机、设备之间的数据总线 |
| 线格式 | JSON / CBOR / PNG；无统一时间戳 | JSON 控制 + 二进制数据（带 ns 时间戳），schema 由 channel 声明 | 紧凑二进制（varint、批处理），负载任意 |
| 订阅与降采样 | 按 topic 的 throttle/queue（leading-edge 或 FIFO），每订阅一个线程 | 没有频率控制（全速推送） | 拦截器降采样（按规则、leading-edge）+ RingChannel |
| 背压 | 读写各一个 1000 条的有界队列，满了就丢（不区分类型） | v1 实现基本没有（C++ 版 10 MB 丢弃）；SDK 有控制/数据分离 | 8 级优先级队列，Drop/Block，SHM |
| 时间 | 无（`cbor-raw` 附带 ROS time） | `time` 消息 + 每帧 ns 时间戳；v2 有 Playback | HLC 时间戳（可选） |
| 回放 | 无 | PlaybackControl（SDK）+ MCAP 生态 | 存储插件 + 查询（没有播放语义） |
| 浏览器直连 | ✓（roslibjs） | ✓（Foxglove、Lichtblick） | 需要 zenohd + remote-api（zenoh-ts） |
| 与本项目契合 | 只作 P600 ROS1 适配 | **协议设计蓝本** + 调试工具 | **内部总线** + ANet 发现 |
| 构建与部署难度 | 需要 ROS 环境（docker） | pip/npm 零成本 | pip 零成本；zenohd 可选 |

**推荐排序**（综合 star、2026 活跃度和契合度）：

1. **Zenoh**（★★★★★）：star 最多、最活跃，而且是 ROS2 的一等 RMW，承担 V0.5 以后的总线与 ANet 发现，是长期基础设施。它的 key expression 命名从 V0.1 起就作为全系统 topic 规范。
2. **Foxglove ws-protocol / foxglove-sdk**（★★★★☆）：与 Realtime Gateway 最直接相关的"设计蓝本"。概念与 v2 语义逐项移植，SDK 作为调试旁路与录制工具。仓库本身已归档，所以只借鉴，不依赖。
3. **rosbridge_suite**（★★★☆☆）：只在"接入 ROS1 的 P600 真机或 Prometheus SITL"这一件事上不可替代。协议里值得借鉴的是 `id` 交互语义、glob ACL、编码缓存和 CBOR typed array；它的 Web 协议本身不适合作为我们的主协议。

**按任务选型**：

| 任务 | 选择 |
|---|---|
| 浏览器 ↔ 服务端实时数据 | **自研 `anet.rt.v1`**（本文），运行在 FastAPI Gateway 上 |
| 服务端内部多进程或多机 | MVP 同进程；V0.5 起用 **zenoh** |
| 接 PX4 SITL 或真机 | MAVSDK（r21）→ Gateway；如果改走 ROS2，则 rmw_zenoh → zenoh 总线 |
| 接 P600 Prometheus（ROS1） | **rosbridge_server（ros1 分支）+ 自研 CBOR 客户端** |
| 调试、曲线、离线分析 | **foxglove-sdk** 旁路 + Lichtblick/Foxglove（r15） |
| 录制与回放 | MCAP（Gateway 录制）+ McapSource（本文 §3.11） |
| ANet 能力发现与协商 | zenoh liveliness + queryable（V1.0） |

---

## 6. 风险与注意事项

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| R1 | **浏览器不提供接收背压**；WS 放在主线程时，状态延迟会无界增长（实测 8 s 后达到 4.2 s），而服务端完全感知不到 | 画面"越来越滞后"，内存上涨 | credit 窗口 + Worker + 由主线程消费触发 ack（§3.9）；UI 显示"数据延迟"指标（`simNow − 最新样本 t_sim`），超过 500 ms 标黄 |
| R2 | 高频数据进入 React state 或 Zustand 的每帧更新 | 重渲染风暴，掉帧 | 3D 层直接读 ref/SoA；store 按 10 Hz 节流（§3.14） |
| R3 | leading-edge 节流丢尾帧（rosbridge 的 `queue_length=0`、zenoh 的 downsampling） | 飞机停下后 UI 显示的是旧状态 | 自研调度器保证尾帧；rosbridge 设 `queue_length:1`；zenoh 按 key 配规则或在发布端限速 |
| R4 | zenoh downsampling 通配规则**共享时间戳**，会饿死其他 key | 某些飞机在真机链路上完全"消失" | 每个 key 一条规则；或在发布端做限速（§3.15） |
| R5 | payload 未对齐（照搬 Foxglove 的 13/17 B 头） | `Float32Array` 抛错，只能复制（慢 2.6 倍） | 16 B 对齐头部；测试断言每条记录 `payloadOffset % 8 == 0` |
| R6 | Python Gateway 单核瓶颈（约 0.78 ms/帧，在 1.7 GHz 老 CPU 上约 40 个客户端） | 公开演示或多人同时访问时降级 | ack 合并（实测 −15% CPU）；编码共享（对齐网格）；多进程分片；V1.0 以后考虑 Rust 数据面 |
| R7 | uvicorn 的 `--ws websockets` 已标记为弃用；`websockets-sansio` 在本场景下更慢 | 升级 uvicorn 时性能回退 | 在 CI 里固定 uvicorn 版本，跑 `gw_bench` 性能回归；必要时直接在 uvicorn 外用 `websockets.asyncio.server` 挂载 |
| R8 | **ws-protocol 已归档**，foxglove-sdk 的 v2 仍在变（`PlaybackState` 还标着 `doc(hidden)`） | 直接绑定会被上游变化牵着走 | 主协议自研，只借语义；调试桥靠 SDK 的 Python API 隔离 |
| R9 | **rosbridge ros1 分支 2025-09 后停更**；ROS1 Noetic 已于 2025-05 EOL；roslibpy 不支持 CBOR 和二进制 | P600 集成需要维护一套旧环境 | 用 docker 运行 `ros:noetic` + rosbridge 0.11.18；自研 CBOR 客户端（§3.16）；长期推动 Prometheus 迁移到 ROS2 + rmw_zenoh |
| R10 | rosbridge 写队列满了会**丢任意消息**（包括 service_response）；每次 service 调用都新建线程和 client | 命令结果丢失，高频调用拖垮进程 | 命令走 topic 加状态回读确认；service 只用于低频配置；监控 "Write queue full" 日志 |
| R11 | rosbridge 的 JSON 会把 NaN/Inf 转成 null，`uint8[]` 会被 base64 | 语义丢失，体积膨胀 | 一律使用 `compression:"cbor"`；点云不走 rosbridge |
| R12 | zenoh-ts/remote-api 每个 socket 一个无界队列、每个 tab 一个 session | 慢客户端导致服务端 OOM；权限面过大 | 不对浏览器开放；只允许内网调试 |
| R13 | zenoh 1.x 的 API 与 unstable 特性变化（AdvancedPublisher 标注 `#[zenoh_macros::unstable]`） | 升级时编译或运行报错 | 锁定 `eclipse-zenoh==1.10.x`；同一网络里所有节点使用同一个 1.x 次版本；封装在 `zenoh_bus.py` 一层里 |
| R14 | liveliness 对静默断链（数传或 4G 掉线）依赖 lease，默认 10 s | 掉线后 10 s 才能感知 | 真机链路配 `lease: 2000–3000`；同时用 Gateway 的遥测 age（超过 1 s 置 degraded）作为双保险 |
| R15 | 控制平面和数据平面之间不保证顺序（两个队列） | seek 后可能先收到旧纪元的数据帧 | BATCH 头部带 epoch；客户端丢弃 epoch 不一致的帧 |
| R16 | SharedArrayBuffer 需要 COOP/COEP；CDN 字体和图标没有 CORP 头就会加载失败 | 页面资源报错 | MVP 不用 SAB；若要用，所有资源自托管 |
| R17 | 无鉴权或 CSRF：WS 会自动携带 cookie；rosbridge 默认不校验 Origin | 任意网页都能向飞机发命令 | token 放在子协议里、校验 Origin、operator 锁、命令审计日志 |
| R18 | 仿真时钟与墙钟混用（Date.now、服务器本地时间） | 回放或倍速下动画错乱 | 协议里只出现 `t_sim_ns`；墙钟只用于 ClockSync；约定写入 lint 规则 |
| R19 | 公网部署时 RTT 较大，W 太小会限制吞吐（W 帧 / RTT） | 高延迟链路下帧率上不去 | `W = ceil(max_rate·srtt) + 2`，由 srtt 自适应 |
| R20 | 大块数据（风场、LiDAR、轨迹）占用 WS 通道 | 挤占状态数据，出现卡顿 | 超过 256 KB 的数据 WS 只推 URL 和版本，内容走 HTTP；LiDAR 设 priority 3，只推选中机体 |

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§36 "前后端实时通信"需要从一句"WebSocket 负责 Drone State…"扩展为完整的协议章节**，建议直接引用本文 §3.3–3.12：
   - 协议名与子协议 `anet.rt.v1`；控制平面（JSON）与数据平面（二进制 BATCH）分离；
   - 帧头和记录头布局（16 B 对齐）；topic 命名规范（zenoh key expression）；
   - 订阅参数（rate/mode/priority）；credit 背压；TIME 与 epoch；playback；RPC。
   - 否则前后端各写各的，r15、r19、r21 已经出现了三种互不兼容的帧格式（约 100 B、40 B、30 B）。
2. **§36 的数据流"PX4 → ROS2 → Simulation Gateway → WebSocket → Browser"需要修正**：
   - MVP：`Mock Dynamics → SimClock/Channel store → Realtime Gateway → WS(anet.rt.v1) → Worker → Three.js`。
   - V0.2：PX4 SITL 经 MAVSDK 接入 Gateway（r21）。
   - V0.5：P600 经 rosbridge(ROS1) 接入 Gateway，PX4/ROS2 经 rmw_zenoh 接入 zenoh 总线再到 Gateway。
   - ROS2 不应作为必经环节。
3. **§33 Backend 技术栈表中的 "Message: ROS2 / DDS" 改为三行**：
   - "Internal Bus：进程内（MVP）→ **Zenoh**（V0.5+，含 SHM、liveliness、ACL）"；
   - "ROS 适配：rosbridge（ROS1 P600）/ rmw_zenoh（ROS2）"；
   - "Realtime Web：自研 anet.rt.v1（FastAPI）+ foxglove-sdk 调试旁路"。
4. **§37 "WebSocket 10~50 Hz" 应改为"按订阅协商"**：
   - 每个订阅有 rate class（1–60 Hz）；服务端 tick 60 Hz；
   - 整群 `swarm/state` 10–20 Hz，视野内单机 30 Hz，选中机 50 Hz；
   - 客户端依据 FPS 反馈自动降档，并且先于点云降档；
   - 另外加一句"**浏览器渲染 60 FPS 由插值（D = 60–250 ms）与数据频率解耦**"。
5. **§28 DroneState 需要落到字节层面**：
   - 给出 Full64/Lite32 两种布局（§3.5）、坐标约定（World ENU、FLU、四元数 x,y,z,w）、`t_sim_ns` 与 `seq` 字段、NaN 表示未知的约定；
   - 低频字段（gps、健康、任务细节）拆到 `state_ext`/`safety` 通道。
   - 现在的伪结构体里 `orientation`、`acceleration` 都没有写明类型与参考系。
6. **§39 Timeline 缺少"时间模型"**：
   - 需要写明服务端仿真时钟是权威；TIME 消息的字段（sim/wall/rate/state/epoch）；
   - seek 使用 backfill 快照与 epoch；录制格式为 MCAP（按原生频率）；
   - 实时仿真的 checkpoint 与分叉（what-if，V0.4）。
   - 这是"回放真实飞行"与"仿真回放"同构的前提。
7. **§10 "多人访问、远程演示"缺少控制权模型**：需要增加 viewer/operator 角色、每架飞机的操作锁（`cmd/acquire`）、命令幂等 id 与审计日志。否则两个人同时拖动航点会互相覆盖。
8. **§31–32 ANet 的 capability discovery 可以直接落在 zenoh 上**：
   - `agent/{id}/alive` 用 liveliness token 表示在线（实测 graceful 0.5 ms；进程崩溃 6–8 ms；静默断链看 lease）；
   - `agent/{id}/capability` 用 queryable 提供能力描述，`get("agent/*/capability")` 一次拿全；
   - 任务发布与认领走 `event/anet/**`（AdvancedPublisher 保证可靠）。
   - 这比自建注册中心简单，也天然支持异构机体和 ROS2。
9. **§14–16 的点云流式和 §21 的风场可视化应明确"大块数据走 HTTP"**：
   - 八叉树节点、风场网格、轨迹历史都用 HTTP GET（ETag、Range、并发）；
   - WS 只推"版本变了 + URL"；
   - 避免把 MB 级数据塞进实时通道挤占遥测。
10. **新增"安全与鉴权"小节**（原文没有）：WS token（放在子协议里）、Origin 校验、topic/service 白名单（类似 rosbridge glob）、zenoh ACL（按证书 CN）、真机命令二次确认。
11. **新增"可观测性"小节**：
    - Gateway 暴露 `perf/server`（CPU、每客户端 fps、credit_skips、encodes/s、字节/s）；
    - 客户端上报 `clientStats`（fps、解码耗时、数据延迟、point budget）；
    - UI 的流畅性面板（lieflat-charts 风格）直接订阅这两个 topic。
    - 这正好对应用户提出的"系统流畅性测试"。
12. **§42 Repo 结构补充**：`packages/contracts/rt/`（layouts.json、JSON op schema 及其 TS/Python 生成物）、`apps/api/rt/`（Gateway）、`apps/web/src/net/rt/`（Worker 客户端）、`tools/rt-inspect/`（抓包和解码 BATCH 的调试 CLI）、`bench/rt/`（本文 bench 脚本，进 CI 做性能回归）。
13. **§43 MVP 范围里加一句"协议先行"**：V0.1 就按最终协议实现（BATCH、credit、TIME），哪怕只有 Mock 数据源。之后再接 PX4、回放、zenoh 时只增加 Source 和 bridge，前端不用改。这正是原文 §4.1 "Web UI 不需要推倒重做"原则在通信层上的落实。

