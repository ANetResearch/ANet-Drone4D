# d05 研究笔记：ANet（Agent Network）→ Agent Runtime 集成设计与品牌素材规范

> 研究单元：d05 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §3（总体架构）、§28（DroneState）、§29–30（多机与控制模式）、§31–32（Agent Network 与 Multi-Agent Workflow）、§33（Backend 栈）、§36–37（实时通信与频率）、§38–40（UI）、§42（Repo）、§50（V1.0）
> 仓库快照：`refs/design/ANet` @ `840b8ea`（2026-09-06，★6，Go 1.26，依赖 `ANetCore v0.14.0`，版本号 `0.1.10`）。文中路径都相对仓库根目录。
> 读码范围：除 ANet 本仓外，还读了 Go 模块缓存中的 `ANetCore@v0.14.0`（`effect/ tsir/ adp/ evidence/ delegation/ identity/ relayauth/ anetcid/`），以及本机 `/data/projs/anet-oss/` 下的 `ANetHub`（能力检索与任务板）、`ANetLink`（设备信任阶梯与 sim 适配器）。另外读了本机 ANet 工作区里**尚未推送**的 `docs/A2A-DESIGN-zh.md`（r3，2026-09-26，v0.2 系列设计）。以上都只读，没有修改。
> 实测：用 refs 源码构建 `anet`，用 `ANetHub@c4d08da`（与 ANet 840b8ea 同为 wire 1 且同钉 ANetCore v0.14.0）构建 `anet-hub`，在本机起一个 hub 和若干 daemon，把无人机能力经 `service` 模块挂到网络上做端到端委派。另写了一份 Agent Runtime 原型（TSIR 谓词、黑板、证据链、报价打分的 Python 移植）。脚本与输出都在 `/data/projs/anet-drone/.cache/research/d05/`：`run_joint.sh`、`run_scale.sh`、`mock_drone_svc.py`、`agent_runtime_proto.py`、`resolvetest/`、`logo.png`、`usage.png`。
> 相关单元：r21（Gateway 控制租约 Control Lease）、r25（Agent 层只输出目标，轨迹由 Planning 生成）、r26（插入验证航段、按能力加权切分）、r27（实时协议与 zenoh 总线）、d01（ANet Graphite 色卡）。

---

## 0. 结论速览

| 仓库 / 子模块 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **ANet**（整体） | 面向异构 AI agent 的"委派网络"：自证明身份（AID + KEL）、按能力发现、签名任务合同、store-and-forward 中继、可离线验证的回执与评价。纯 Go 单二进制，不运行任何模型 | V0.6 **port** 其语义做进程内 Mock；V1.0 **adopt**：每架无人机一个 daemon，外加自建 hub | V0.6（Mock）/ V1.0（真网） | ★★★★☆ |
| `provider/`（C1 CapabilityProvider） | `ID / Capabilities / Describe / Invoke / Health`，可选 `Priced`、`LongRunning`；`Registry.Resolve` 先精确匹配，再按 `.` 逐级回退到父能力 | **port**：Python 版 `CapabilityProvider` 协议，作为 Drone Agent Adapter 的内核接口 | V0.6 | ★★★★★ |
| `module/service` | 把本机 HTTP 端点声明成能力：POST JSON 参数，回复里的顶层数字自动成为 metrics，信任固定为 V1 | **adopt**：V1.0 由 FastAPI 为每架机、每个能力提供一个端点（已实测跑通） | V1.0 | ★★★★★ |
| `ANetCore/effect` | 五种"诚实效果状态" `OK / UNVERIFIED / FAILED / UNAVAILABLE / PAYMENT_REQUIRED`，加 8 字段 Evidence 和 V0–V4 / A0–A4 两条信任轴 | **port**：直接作为全系统"命令效果"数据模型，UI 和任务判定都用它 | V0.2 起（命令回执）/ V0.6 | ★★★★★ |
| `ANetCore/tsir` | TaskDoc 任务合同 + 封闭谓词演算（非图灵完备、有界、fail-closed） | **port**：谓词求值器（约 80 行 Python）用作任务验收判据；TaskDoc 字段作为任务规格的参考 | V0.6 | ★★★★☆ |
| `module/blackboard` | 共脑黑板：签名 CogUnit + HLC 混合逻辑时钟 + 只增 OR-Set + 任务相位（active/concluded/archived） | **port**：多机"目标假设 / 证据 / 结论"的共享认知层 | V0.6（Mock）/ V1.0 | ★★★★☆ |
| `internal/daemon/ledger.go` + `ANetCore/ael` | 每个节点一条仅追加、签名、哈希链接的证据链，能处理半截写入 | **port**（简化版）：任务审计链，同时作为 Timeline 回放的事件源 | V0.6 | ★★★★☆ |
| `internal/runtime/interactions` + `ANetCore/delegation` | 委派账本（SQLite）、多轮对话、结束协商、Receipt/Review 互锁 | **reference**：状态机按 v0.2 的 A2A 七态重做，不照搬 v0.1 的 queued/ending/done/failed | V1.0 | ★★★☆☆ |
| `ANetCore/adp`（AgentCard） | 签名名片，包括 capabilities[]、tools[]（带 input/output schema）、endpoints、extensions；seq 高水位防回滚 | **reference**：Capability Manifest 的字段对齐它和 A2A AgentSkill | V1.0 | ★★★☆☆ |
| `module/taskboard`（客户端）+ ANetHub 任务板 | 7 列看板 FSM（created→claimed→submitted→accepted），WIP≤3 | **reference**：给人用的任务看板；秒级延迟，不进实时链路 | V1.0+ | ★★☆☆☆ |
| `module/transport.go` + `module/p2p` + `tools/anetpeer` | 传输列表（附加传输优先，hub 兜底），p2p 走 UDS/TCP 上的 newline-JSON | **reference**：野外无公网时的机间直连思路 | V1.x | ★★☆☆☆ |
| `internal/mcpserv`（`anet mcp`） | 9 个 MCP 工具，让 LLM agent 自己找人、委派、读结果 | **reference**：V1.x 的"LLM 任务指挥官"实验入口 | V1.x | ★★★☆☆ |
| `module/x402`、`module/shell`、`module/org`、`module/cas` | 付费结算、远程执行命令、组织凭证、内容寻址存储 | **skip**（`cas` 以后可作为大块证据的存储参考） | — | ★☆☆☆☆ |
| ANetLink `profile/trust.go`、`adapters/sim`、`sdk/effect.go` | 设备信任阶梯 T0–T4 与"钳制不抬"规则；EffectBuilder 默认 UNVERIFIED | **port**：规则进 Agent Runtime；V1.x 可考虑写一个 `uav` 适配器 | V0.6 规则 / V1.x 适配器 | ★★★☆☆ |
| `docs/media/anet-logo.svg` + GitHub 头像 | 黑底 10° 斜切圆角徽章 + 红色内框 `#E93024` + 像素斜体 "AGENT-NETWORK" 横纹字 + "ROUTE • TRUST • EXECUTE"；头像是黑底红边对话气泡 + 节点图 + 笑脸 | **adopt**：品牌资产，按 §3.11 的规范使用 | V0.1 | ★★★★★ |

**实现者先读这 12 条（都有源码或实测依据）：**

1. **ANet 是"协作平面"，不是控制平面。** 实测一次能力委派的往返延迟是 **921–1016 ms**（5 次，中位 1012 ms）。延迟主要来自 provider 端的中继轮询周期：`relay.go` 里 `relayPollInterval = 1 * time.Second`；requester 端的 `/results` 每次都会触发一次 `pollFresh`，几乎不增加延迟。5 次是背靠背连续发起的，与轮询相位锁定，所以都接近 1 s；随机时刻发起时，期望往返约 0.5–1 s，外加服务执行时间。因此 ANet 只承载任务级事件（发现、委派、验收、证据），频率在秒级。遥测（10–50 Hz）、控制（Offboard 20 Hz）、点云流都留在 Gateway 的 WebSocket / zenoh 上（r21、r27）。
2. **一架无人机对应一个 ANet 身份（AID）。** 理由有三。① `provider.Registry.Register` 不允许同一 daemon 内两个 provider 声明同一能力（`ErrCapabilityConflict`），所以一个网关节点无法为两架机同时提供 `thermal.imaging`。② 被委派方的身份就是 AID，签回执的也是它，"谁做的"天然可归属。③ 成本低：实测 10 个 daemon 的 RSS 共 149 MB（每个约 14 MB），空闲轮询 CPU 每个 0.3%，hub 15 MB。
3. **能力 id 不要带 `@device` 后缀。** 实测 hub 的 `?cap=thermal.imaging` 是**字节级精确匹配**，查不到 `thermal.imaging@uav/p600-02`，只有前缀查询 `thermal.*` 能命中（`ANetHub internal/aghub/aghub.go: FindByCapability`）。registry 的父级回退只按 `.` 切分：`flight.goto@uav/p600-02` 能回退到 `flight`，`thermal.imaging@uav/p600-02` 却回退到 `thermal`，结果解析失败（`resolvetest/` 实测）。结论：采用第 2 条的一机一 AID，能力 id 用裸形式 `thermal.imaging`，设备 id 里也不要出现 `.`。
4. **`service` 模块是 V1.0 最省事的接入方式（已跑通）。** 在 `config.json` 的 `modules.service.capabilities[]` 里声明 `{id, url}`，daemon 会把参数 POST 给我们的 FastAPI。回复中顶层的数字进入 `metrics`（可被 TSIR 谓词求值），`evidence.observed_state/protocol/quirk/native_ack` 进入溯源信息，**`verify_trust` 固定为 1，服务端无法自行抬高**（`module/service/service.go: applyDeclared`）。
5. **效果状态和任务状态是两根轴。** 这是 ANet 的铁律（v0.2 设计里的 SI-6）："A2A `COMPLETED` 不蕴含效果 `OK`"。我们的 DroneCommand、AgentTask 和 UI 都分开两列显示："命令发出去了"（UNVERIFIED / V1）和"读回确认了"（OK / ≥V2）不能合并。
6. **实测到一处信任口径不一致，Agent Runtime 要自己钳制。** `service` 模块返回的是 `status=OK, verifiable=true, verify_trust=1`；而 ANetLink 的规则是 MaxTrust < T2 时把 OK 降为 UNVERIFIED（`ANetLink profile/trust.go: VerifiableFrom = V2`）。daemon 本身不做钳制，所以我们的求值器按"`OK` 且 `verify_trust ≥ 2`（仿真中为 V4 并标记 `simulated`）"才算"已验证"。
7. **daemon 不评估验收谓词。** `tsir.Compile` 返回的 `AcceptancePredicate` 是 `nil`，源码注明"built from accepts by the projector (follow-up)"。验收由 requester 侧的 Agent Runtime 负责，用 §3.6 移植的求值器对 metrics 和 artifacts 求值。
8. **"发布任务 / 抢单"在 ANet v0.1 里没有广播原语。** 只有 `find(按能力)` 和 `delegate(点对点)`。§32 里"发布任务 → ANet → 发现 Drone B → 接受"，要落成 **find → 并行 `task.quote` → 打分 → delegate 给最优者**（一次合同网，§3.5）。hub 任务板是给人用的看板，默认 hub 不带（v0.2 里改成加法 tag），不适合实时抢单。
9. **没有 provider 的能力会诚实地回 `UNAVAILABLE`，但前提是没开 auto-reply。** 实测委派 `lidar.mapping` 给不提供它的节点，立即得到 `{"status":"UNAVAILABLE","message":"this node does not serve lidar.mapping"}`。如果该节点配了 `auto_reply`，请求会被交给 LLM 自由作答（`capability.go: tryCapabilityPaid` 的注释）。无人机节点**不要**配置 auto_reply。
10. **ANet 正在重做成 v0.2（A2A 对齐）。** 本机工作区有 2026-09-27 未推送的检查点：hub 只做 HPKE 端到端加密传输、入站默认 `closed`、任务状态改为 A2A 七态（submitted / working / input-required / completed / failed / canceled / rejected）、wire 升到 2。**我们的接口按 v0.2 的语义设计，v0.1 只作为适配层**（§4.2），避免 V1.0 时返工。
11. **不要用官方公网 hub 跑科研数据。** v0.1 中 hub 可以读取委派内容（v0.2 设计文档的"对外陈述更正表"自己也承认这一点）。V1.0 用本机或局域网自建的 ANetHub（纯 Go，`CGO_ENABLED=0` 构建 6.7 s），可带 `-tags no_federation`。
12. **Logo 的黑色徽章底是必需的。** 字标的白色横纹和 tagline 都依赖黑底才看得见。暗色 UI 上黑底和背景融为一体（`#000` 对 `#0A0B0D` 的对比度只有 1.07:1），此时由红框 `#E93024`（对 `#0A0B0D` 4.61:1）勾出轮廓。浅色 UI 上黑底反而形成强对比（18.9:1）。宽度低于 480 px 时 tagline 不可读，顶栏改用头像标加产品名（§3.11）。

---

## 1. 仓库概览

| 项 | 内容 |
|---|---|
| 地址 | https://github.com/ANetResearch/ANet （官网 agentnetwork.org.cn，公网 hub `hub.agentnetwork.org.cn`） |
| 快照 | `840b8ea` 2026-09-06（"证据账本：半截写入与篡改按位置区分，并让 Append 落盘；ANetCore 升 v0.14.0"）；shallow clone；★6。本机工作区另有 2026-09-27 的 A2A/x402 重设计检查点（未推送） |
| 语言与构建 | Go 1.26（go.mod 写 1.26.6，本机 1.26.7），`CGO_ENABLED=0`，纯 Go。`./build.sh` 或 `./build.sh --check`（gofmt、vet、两个 tag 方向的 test）。本机构建 6.7 s，默认构建二进制 21.5 MB（保留符号表） |
| 直接依赖 | `ANetCore v0.14.0`、`ipfs/go-cid`、`multiformats/go-multihash`、`modelcontextprotocol/go-sdk v1.7.0`、`modernc.org/sqlite`（纯 Go SQLite） |
| 规模 | 实现 18,889 行 / 测试 13,068 行 / 298 个 `Test*`（本仓）；ANetCore v0.14.0 实现 5,267 行 |
| 套件拓扑 | ANetCore（协议内核，零 I/O）← ANet（daemon）/ ANetHub（目录 + 中继 + 评价）/ ANetLink（物理设备运行时 + 14 个适配器）；ANetMock 用真实线协议模拟 148 台设备（`docs/DESIGN-zh.md §3`） |
| 许可 | ANet Community License（非商用免费）；本项目科研用途，按要求忽略 |
| 研究背景 | ANet Patu-1（arXiv:2607.15053）：廉价异构 agent 组成的网络在 n\* ≈ 2.6 个 agent 时超过远强于它们的同构模型。这为"异构传感无人机协同"提供了立论参考 |

**一句话定位**（`docs/ARCHITECTURE-zh.md` §一）："anet 是 AI Agent 能力互联的基础设施，本身不跑任何模型。"它提供五样东西：**身份**、**发现与投递**、**委派账本**、**可验证证据**、**能力面**（`docs/DESIGN-zh.md §1`）。

**与本项目的对应关系**：01-design §31 说"每架无人机成为 Physical Agent，对外描述自己的 capability"，§32 描述"发现疑似目标 → 发布任务 → ANet → 发现 Drone B → 接受 → 观测 → 回传"。在 ANet 的术语里：

| 01-design 概念 | ANet 概念 | 源码位置 |
|---|---|---|
| Physical Agent | 一个 daemon 身份（AID = KEL inception 事件 CID，形如 `bafyrei…`） | `ANetCore/identity`、`internal/daemon/identity.go` |
| capability `thermal.imaging` | 能力 id（字符串），挂在签名 AgentCard 的 `capabilities[]` 上 | `provider/registry.go`、`internal/daemon/card.go` |
| 能力发现 | `GET /agents?cap=thermal.*`（hub 索引表 `agent_cap`） | `internal/daemon/relay.go: FindByCapability`；ANetHub `FindByCapability` |
| 发布任务 / 接受 | 签名 TaskDoc → `DelegateReq` → 中继信箱；provider 解析能力后确定性执行 | `internal/daemon/capability.go`、`delegation.go: ingestDelegate` |
| 结果回传 | `ResultResp{deliverable, receipt, KEL}`；requester 调 `delegation.VerifyResult` 做 7 项绑定校验 | `ANetCore/delegation`、`evidence` |
| 协作认知 | 共脑黑板 `blackboard.add/snapshot/conclude` | `module/blackboard/` |

---

## 2. 源码结构与关键模块

### 2.1 目录

| 路径 | 内容 | 本项目关注度 |
|---|---|---|
| `cmd/anet/main.go` | 单二进制：`daemon` 常驻进程，其他子命令是控制 API 的薄客户端（`find/delegate/inbox/thread/message/end/results/review/verify/evidence/mcp…`） | 中：CLI 用法和参数约定 |
| `internal/daemon/daemon.go` | `New(layout)`：载入配置、身份、账本，启动模块，按配置起中继轮询；`refreshRegistration` 在启动时把已服务的能力重新折进注册 | 高 |
| `internal/daemon/capability.go` | C1 能力委派：`capabilityCall(td)` 识别约定，`DelegateCapability`，`tryCapabilityPaid`，`deliverCapabilityResult`（签 Receipt、上链、回传） | **最高** |
| `internal/daemon/relay.go` | Hub 客户端与轮询：`HubRegister`、`Find/FindByCapability`、`Delegate`、`relayLoop`（1 s）、`pollOnce`、`withServedCapabilities` | 高 |
| `internal/daemon/delegation.go` | 生命周期：`ingestDelegate`、`runCapabilityCall`（长任务移出轮询环，并发上限 4）、`ingestMessage`、`maybeFinalize`（双方同意结束后由 provider 签回执）、`SubmitReview` | 高 |
| `internal/daemon/transport.go` | 传输列表：附加传输优先、hub 兜底；`relaySend` 逐个试 `Reachable/Send` | 中 |
| `internal/daemon/ledger.go` | 证据链：base64 CoreDet-CBOR 行，签名，`prev_id` 链接；打开时校验，末尾半截行截掉并补一条 `anet.evidence.gap` | 中 |
| `internal/daemon/card.go` | 签名 AgentCard：`cardSeq` 以时钟为种子、持久化、严格递增；价格和兑付端点放在签名内 | 中 |
| `internal/daemon/control_api.go` | 回环控制面，默认 `127.0.0.1:39811`，Bearer token 做常量时间比较，约 29 个路由 | **最高**（V1.0 的集成面） |
| `internal/runtime/interactions/` | SQLite 表 `interaction / message / attachment`；Role inbound/outbound；Status queued/ending/done/failed | 中 |
| `internal/mcpserv/` | `anet mcp`：9 个工具 | 低到中 |
| `internal/hubapi/` | 与 hub 共享的 wire 类型；`WireVersion = 1`，头 `X-ANet-Wire` | 中 |
| `provider/` | C1 合同：`provider.go`、`registry.go`、`provenance.go`；`provider/anetlink` 是 C1-over-UDS 客户端 | **最高** |
| `module/module.go` | 模块接缝：`Host` 接口（`AID / Providers / RecordEvidence / ResolveKEL / PaymentSeam / HubSeam`），`Register` 在带 build tag 的 `init()` 中调用 | 高（设计思想） |
| `module/service/` | HTTP 服务即能力 | **最高** |
| `module/blackboard/` | `crdt.go`（HLC、OR-Set）、`cogunit.go`、`blackboard.go`、`phase.go`、`module.go` | 高 |
| `module/taskboard/` | hub 任务板客户端：`task.board / task.create / task.claim` | 低 |
| `module/p2p/`、`tools/anetpeer/` | 直连传输及其参考 peer（rendezvous 是共享目录，没有 NAT 穿透） | 低 |
| `module/{x402,shell,org,cas,inv1,inv2}` | 付费、远程命令（加法 tag）、组织凭证、CAS、不变式守卫 | skip |
| `docs/` | `ARCHITECTURE / DESIGN / CONTRACTS / CAPABILITIES / GUIDE / SUITE-TODO / DISTRIBUTIONS / SHELL / PAYMENT…`（中文） | 高（`CONTRACTS-zh.md` 的五合同） |

### 2.2 C1 合同（`provider/provider.go`）

```go
type Call struct { Capability string; Args map[string]any; CallID string; CallerAID string }
type CapabilityProvider interface {
    ID() string
    Capabilities(ctx) ([]string, error)
    Describe(ctx) (string, error)          // 描述对象的 CAS CID，可为空
    Invoke(ctx, Call) (effect.Effect, error) // 可达但无法验证 ≠ error，返回 Unverified
    Health(ctx) error                      // 非 nil ⇒ 该 provider 全部能力暂不可用
}
type Priced interface     { Price(capability string) (uint64, bool) }          // 可选
type LongRunning interface { InvokeTimeout(capability string) (time.Duration, bool) } // 可选
```

"红线"（包注释）是：**daemon 永远不知道 provider 背后是什么，尤其不知道"设备"这个概念**。对我们来说，这恰好就是"Web UI、Agent Runtime 与飞控后端解耦"原则（01-design §4.1）在 agent 层的对应。

### 2.3 能力委派线格式（`internal/daemon/capability.go` 头注释 + `DelegateCapability`）

```text
TaskDoc{Version{1}, Tasks[0]{
   Intent{Summary/Body: "invoke capability <id>"},
   Requires[ {ID:<capability-id>, Type:"capability", Necessity:"must"} ],
   Contexts[ {Key:"args", Value:<JSON>, Format:"json"} ] }}
 → td.Sign(self)          // 签在 CoreDet-CBOR 规范原像上；CID = CIDv1(dag-cbor, sha2-256)
 → DelegateReq{TaskDoc bytes, Envelope, KEL(内联), InteractionID "ix_<32hex>", Payment?}
 → relaySend(provider, kind="delegate")
provider: pollOnce → ingestDelegate → VerifyDelegateReq → capabilityCall(td) → Resolve → Invoke
 → capabilityResult{capability,status,verifiable,metrics,message,evidence} (JSON，即 deliverable)
 → Receipt{ix, requester, provider, request_cid, result_cid=CID(deliverable), completed_at} 签名
 → ledger.Append("anet.capability.effect", …) → ResultResp{done, deliverable, receipt, KEL}
requester: ingestResult → VerifyResult（签名、provider==预期、requester==自己、ix 一致、result_cid==hash(deliverable)）
```

关键常量：`capabilityInvokeTimeout = 60 s`（provider 可通过 `LongRunning` 延长，超过 60 s 的调用移出轮询环）；`maxConcurrentLongCalls = 4`，满额时立即回 `UNAVAILABLE` 并说明原因，不排队；`relayCallTimeout = 15 min`；`freshPollTimeout = 12 s`。

### 2.4 效果与信任（`ANetCore/effect/effect.go` + ANetLink `profile/trust.go`）

| 状态 | 含义 | 本项目中的无人机例子 |
|---|---|---|
| `OK` | 执行了且有可验证记录（`Record != nil`） | 热成像帧已回传，metrics 中有 confidence |
| `UNVERIFIED` | 按要求执行了，但当前信任级别下没有可验证的效果信号；**不是失败** | GoTo 已被飞控 ACK，还没到达 |
| `FAILED` | 执行失败 | 起飞被 preflight 拒绝 |
| `UNAVAILABLE` | 调用时目标不可达，远端什么也没做 | 电量不足拒单、未提供该能力、长任务并发满 |
| `PAYMENT_REQUIRED` | 付费后才执行 | 不用 |

Evidence 的 8 个字段：`Requested / Protocol / NativeAck / ObservedState / LatencyMS / VerifyTrust(V0–V4) / AuthTrust(A0–A4) / Quirk`。

信任阶梯（ANetLink `profile/trust.go`）：V0 黑箱、V1 传输层 ACK（云 API 最高到这里）、V2 设备读回、V3 独立旁路确认、V4 物理闭环实测；A0 未知、A1 网关担保、A2 协议认证、A3 设备密钥、A4 硬件根。规则：`VerifiableFrom = V2`；`Clamp(declared, ceiling)` 只钳制不抬升；quirk（厂商偏差修正）无权抬高信任。

### 2.5 TSIR 谓词（`ANetCore/tsir/predicate.go`）

封闭文法：`AND(1) / OR(2) / NOT(3) / ARTIFACT(10) / TEST(11) / THRESHOLD(12) / SCOPE(13)`；上限 `MaxClausesPerPredicate = 64`、`MaxPredicateDepth = 16`；未知 op 直接判 `MALFORMED`（fail-closed）。求值域是 `EffectRecord{Artifacts, Tests, Metrics map[string]float64, Resources, Effects}`；THRESHOLD 在 metric 缺失时判 false。`EvaluateScope(negativeScope, committed)` 是"禁止动作"硬闸门。

### 2.6 共脑黑板（`module/blackboard/`）

- `CogUnit{Author, TaskID, Scope, Type(claim/evidence/conclusion/intent/retraction…), Stamp HLC, Body, BodyCID, Envelope}`，签名覆盖除信封外的全部字段，`ID = CID(规范原像)`。
- `Add`：先按 id 去重（重复写入 3.4 µs），再解析作者 KEL 并验签（首次写入 281 µs），合并 HLC，写入 OR-Set。任务相位不是 active 时拒绝写入（`ErrTaskNotActive`）。
- OR-Set **只有 Add，没有 Remove**；撤回用一个 `retraction` 类型的新单元表达。`Snapshot` 按 (HLC, id) 全序排列，快照 4096 个单元耗时 2.5 ms。
- 相位：active →(Conclude) concluded →(Archive) archived；从 active 直接 Archive 非法。

### 2.7 其他

- **AgentCard**（`ANetCore/adp`）：JCS + detached JWS；`seq` 高水位防回滚；`ADPCardTTL = 7 d`；`MaxSkills = 50`；`Tools[]` 带 `input_schema / output_schema`，但 **v0.1 daemon 签发名片时只填 `capabilities / name / endpoints / extensions(anet.pricing)`**，模块无法向名片写入 schema。
- **Hub 检索**（ANetHub `FindByCapability`）：逗号表示 OR；结尾 `*` 是字节前缀匹配；其余为字节精确匹配，区分大小写；每个 agent 最多 256 个能力，每个 id 最多 256 字节；结果按平均评分、评价数、注册时间排序。
- **已知缺口**（`docs/DESIGN-zh.md §10`）：#1 改了模块配置重启后，daemon 不会自动重新注册，hub 上的 caps 保持旧值。注意：本快照的 `daemon.go: refreshRegistration` 已在启动时重新折叠能力，但仍建议显式调用 `/hub-register`。#2 能力解析失败且配置了 auto-reply 时，请求会落到 LLM 路径。

---

## 3. 可复用算法与实现（含伪代码 / 参数）

### 3.1 能力 id 分类法（Drone Capability Taxonomy v1）

语法：`<family>.<action>[.<variant>]`，只用小写 ASCII、数字、`_`，用 `.` 分段；**不带 `@` 后缀，不含设备 id**（身份由 AID 表达）。每个节点最多 256 个能力，每个 id ≤ 64 字节（hub 上限是 256，留余量）。

| family | 能力 id | 访问方式 | 说明 |
|---|---|---|---|
| `flight` | `flight.takeoff` `flight.land` `flight.goto` `flight.hover` `flight.orbit` `flight.rtl` `flight.follow_path` | SET | 对应 01-design §30 第一阶段；执行前必须拿到控制租约（r21） |
| `mission` | `mission.coverage` `mission.search` `mission.insert_leg` `mission.abort` | SET | `insert_leg` 就是 r26 的"插入验证航段"标准动作 |
| 传感 | `rgb.zoom` `rgb.capture` `thermal.imaging` `lidar.mapping` `lidar.scan` | GET/SET | 沿用 01-design §31 的原始命名 |
| `relay` | `relay.communication` | SET | 中继位 |
| 元能力 | `agent.describe` `agent.state` `task.quote` | GET | 每个无人机节点**必须**提供：返回能力清单、状态快照、报价 |
| 协作 | `blackboard.add` `blackboard.snapshot` `blackboard.conclude` | SET/GET | 只在协调节点（GCS）上提供 |

发现用 family 前缀：`find(cap="thermal.*")`。在 provider 内部可以只注册 family（如 `flight`），通过 registry 的父级回退去服务 `flight.goto / flight.orbit`（实测 `flight.goto → flight` 可以解析）。

### 3.2 Capability Schema（能力清单，由 `agent.describe` 返回）

v0.1 的名片装不下 schema（§2.7），所以清单通过**元能力 `agent.describe`** 下发：它是确定性调用，deliverable 的 CID 被回执签名，内容不可伪造。字段与 A2A AgentSkill（v0.2 的 `provider.Described.SkillInfo`）对齐，便于迁移：

```json
{
  "schema": "awr.capability-manifest/1",
  "agent": {"aid": "bafyrei…", "name": "P600-02", "kind": "uav", "model": "amov.p600",
            "vehicle_id": "p600-02", "world": "sf-urbanscene", "network": "anet|mock"},
  "skills": [{
    "id": "thermal.imaging", "name": "热成像复核", "description": "对地面点做热成像确认",
    "tags": ["sensor", "thermal", "verification"],
    "input_schema":  {"type": "object", "required": ["x","y"],
                      "properties": {"x":{"type":"number"},"y":{"type":"number"},"z":{"type":"number","default":60},
                                     "dwell_s":{"type":"number","default":10,"maximum":120}}},
    "output_metrics": ["confidence", "max_temp_c", "eta_s", "range_m"],
    "output_artifacts": ["thermal/*.tiff"],
    "physical": {"sensor": {"type": "thermal", "hfov_deg": 42, "res": [640, 512], "range_m": 400},
                 "alt_m": [30, 120], "limits": {"wind_ms": 12, "rain_mm_h": 10, "visibility_m": 200}},
    "trust": {"verify_max": 2, "auth": 3, "simulated_verify": 4},
    "requires_lease": true, "timeout_s": 600, "long_running": true, "price": 0
  }],
  "state_capability": "agent.state"
}
```

`physical.limits` 与 Environment 场 `E(x,y,z,t)` 联动：报价时调用 `environment.query` 做可行性判断（§3.5）。

### 3.3 Drone Agent Adapter（三种接入形态）

```text
                 ┌───────────── Agent Runtime（Python，apps/api/agent_runtime）────────────────┐
 UI ◄─WS agent/*─┤ TaskManager(A2A 七态) · Allocator(合同网) · Evaluator(TSIR) · Blackboard · Evidence │
                 │                     AgentNetwork 接口（§4.2）                                   │
                 │        ┌───────────────┴────────────────┐                                     │
                 │   MockNetwork(V0.6)             AnetDaemonNetwork(V1.0)                        │
                 └────────┼───────────────────────────────┼────────────────────────────────────┘
                          │ 进程内调用                     │ HTTP 127.0.0.1:398xx（每机一个 daemon 的控制面）
                 DroneAgent[i]（CapabilityProvider）      anet daemon[i] ── ANetHub（自建，LAN）
                          │                                │ modules.service → POST /anet/cap/{vid}/{cap}
                          └──────────── FastAPI 能力端点（同一份 DroneAgent 实现）◄──┘
                                             │ 需要飞行时：Gateway.acquire_lease(owner="anet:<ix>")
                                             ▼
                               Gateway / Mock 动力学 / PX4 SITL（r21）
```

| 形态 | 做法 | 信任上限 | 版本 |
|---|---|---|---|
| A. Mock | 进程内按 ANet 语义实现 AID、registry、find、delegate、receipt 和证据链；可配 `relay_latency_ms`（默认 1000，模拟真实网络；性能测试时设 0） | 仿真内 V4（标记 `simulated`） | V0.6 |
| B. 真 ANet + `service` | 每架机一个 daemon（`ANET_DATA_DIR=worlds/<w>/agents/<vid>`），`service` 指向 FastAPI；Agent Runtime 通过各节点控制面发起委派 | V1（服务模块封顶） | V1.0 |
| C. ANetLink `uav` 适配器 | Go 写 AdapterSDK 适配器，走 MAVLink 读回 → V2，RTK/独立传感 → V3，带 GPS 戳的热成像帧 CID → V4；AuthTrust 用 MAVLink2 signing（A2）或 Jetson 设备密钥（A3/A4） | V2–V4 | V1.x |

形态 B 的 daemon 配置（已实测，`run_joint.sh`）：

```json
{"control_addr":"127.0.0.1:39812","accept_delegations":true,
 "modules":{"service":{"timeout_ms":600000,"capabilities":[
   {"id":"thermal.imaging","url":"http://127.0.0.1:8000/anet/cap/p600-02/thermal.imaging","protocol":"awr.sim"},
   {"id":"agent.describe","url":"http://127.0.0.1:8000/anet/cap/p600-02/agent.describe"},
   {"id":"task.quote","url":"http://127.0.0.1:8000/anet/cap/p600-02/task.quote"}]}}}
```

FastAPI 端点约定：请求体就是 `args`。回复是 JSON 对象，顶层数字会成为 metrics，嵌套对象进 `observed_state`（字符串），可选 `evidence{protocol, observed_state, quirk, native_ack}`；非 2xx 映射为 FAILED，连接失败映射为 UNAVAILABLE（`service.go`）。**长任务**（飞过去再观测）不要让 HTTP 挂几分钟：`service` 模块的超时默认 2 min，而且它没有实现 `LongRunning`，daemon 会用 60 s 上限截断（`invokeBound`）。做法是**两段式**：`thermal.imaging` 立即返回 `UNVERIFIED + {accepted:1, eta_s, task_ref}`，完成后 requester 再调 `agent.state` 或 `task.result` 取结果（第二次委派）。V0.2 之后改为 A2A 的 working → completed 流。

### 3.4 任务生命周期（按 A2A 七态设计，兼容 v0.1）

```text
          ┌────────────── cancel ──────────────┐
submitted ─► working ─► completed  (effect_status ∈ {OK, UNVERIFIED}，另记 accepted=谓词结果)
    │           │  ▲ └─► failed    (FAILED / 中断：effect_status=UNVERIFIED, reason=interrupted)
    │           ▼  │
    │     input-required (缺参数、租约被抢占、需人工批准)
    └─► rejected (UNAVAILABLE：不提供 / 拒单 / 电量不足；带 retry_after_ms 或 reason)
终态：completed / failed / canceled / rejected；状态迁移用 CAS：UPDATE … WHERE state NOT IN 终态
```

物理执行子阶段放在元数据里，不单独作为状态（A2A 允许）：`awr.phase ∈ {queued, lease, enroute, on_station, executing, returning}`、`awr.progress 0..1`、`awr.eta_s`。v0.1 的映射：`queued → submitted/working`，`done → completed`，`failed → failed`（v0.2 设计 §4.1 的迁移规则）。

参数默认值：报价截止 `T_quote = 3 s`（Mock）/ `2.5 × relay_poll + 1 s ≈ 3.5 s`（真 ANet）；执行超时 `T_exec = 1.5 × eta_s + dwell_s + 60 s`；租约 TTL 等于 `T_exec`，每 5 s 续约。`rejected` 或 `UNAVAILABLE` 时换下一个候选（最多重试 2 次）；`FAILED` 上报操作员；UI 手动接管（租约优先级 3）会使任务进入 `input-required(reason=lease_preempted)`。

### 3.5 §32 搜救工作流：合同网分配算法（ANet v0.1 上可实现）

```python
def verify_target(detection):                    # Drone A 的检测事件，conf=0.42 < τ_verify=0.8
    T = board.add(author=A, task=new_task(), type="claim",
                  body={"at": p, "conf": 0.42, "sensor": "rgb.zoom"})
    spec = TaskSpec(cap="thermal.imaging", args={**p, "dwell_s": 10},
                    accept=AND(THRESH("confidence", GE, 0.8), ARTIFACT("thermal/**", min_size=1024)),
                    negative_scope=SCOPE(no_fly_zones))
    cands = net.find(cap="thermal.*")             # 前缀发现（hub 字节前缀）
    quotes = gather([net.delegate(c, "task.quote", {"cap": spec.cap, **spec.args}) for c in cands],
                    timeout=T_quote)              # 每个候选返回 metrics
    ranked = sorted(((U(q.metrics, risk(env, p)), c) for c, q in quotes if q.status == "OK"), reverse=True)
    for score, c in ranked[:3]:                   # 最多两次重试
        if score == -inf: break
        ix = net.delegate(c, spec.cap, spec.args) # 签名合同，可归属
        r = await net.result(ix, timeout=T_exec)
        if r.state == "completed" and trusted(r) and evaluate(spec.accept, r.record):
            board.add(c, T, "evidence", {...}); board.add(coord, T, "conclusion", {"verified": True})
            board.conclude(T); evidence.append("agent.task.accepted", {...}); return r
        evidence.append("agent.task.rejected_or_failed", {...})
    raise Escalate(T)                             # 交给操作员
```

报价模型（`task.quote`，Mock 用 P600 级参数，接入数字孪生 §27 后替换）：

```text
P_hover = (m·g)^{3/2} / sqrt(2·ρ·A) / η,   A = n·π·r²
          m=4.5 kg, r=0.19 m, n=4, ρ=1.225, η=0.70  ⇒  P_hover ≈ 397.5 W；电池 222 Wh，可用 80% ⇒ 26.8 min
v_eff   = max(v_cruise − 0.5·|wind(x,y,z,t)|, 2)      v_cruise = 8 m/s，climb = 2.5 m/s
t_out   = d(pos, tgt)/v_eff + |Δz|/v_climb;  t_home = d(tgt, home)/v_eff
E       = P_hover·(t_out + dwell + t_home)/3600  (Wh);  soc_after = soc − E/E_batt
feasible = soc_after ≥ rtl_reserve(0.20) ∧ 环境在 limits 内 ∧ 传感器可达（range_m、alt_m）
```

效用函数（权重可在 UI 调整，默认值如下）：

```text
U = w_c·conf_expected − w_t·eta_s/120 − w_e·energy_wh/10 − w_l·load − w_r·risk
    w_c=1.0  w_t=0.6  w_e=0.3  w_l=0.2  w_r=0.5；  不可行 ⇒ U = −∞
risk = clamp(0.5·wind/wind_max + 0.3·rain/rain_max + 0.2·(1 − vis/vis_min_ok), 0, 1)
```

原型实测（`agent_runtime_proto.py`，风速 6 m/s，目标 (180, 20)）：

| 候选 | eta_s | energy_wh | soc_after | U |
|---|---|---|---|---|
| P600-02 thermal（150,−80），SOC 0.64 | 20.9 | 7.41 | 0.607 | **0.573**（中标） |
| P600-03 thermal（−420,300），SOC 0.92 | 140.4 | 20.61 | 0.827 | −0.420 |
| P600-05 thermal（900,900），SOC 0.28 | 227.4 | 30.21 | 0.144 | −∞（低于 RTL 余量） |

中标后得到 `confidence=0.869`，谓词求值为 true；黑板序列 claim → evidence → conclusion；conclude 之后的新写入被拒；证据链 5 条，校验通过，篡改其中一条后校验失败。

### 3.6 TSIR 谓词求值器（Python 移植要点）

```python
def evaluate(p, rec):                  # rec = {"metrics":{}, "artifacts":[{"path","size"}], "tests":[]}
    match p["op"]:
        case 1: return all(evaluate(c, rec) for c in p["children"])   # AND，2..64 个子项
        case 2: return any(evaluate(c, rec) for c in p["children"])   # OR
        case 3: return not evaluate(p["children"][0], rec)            # NOT，恰好 1 个子项
        case 12: v = rec["metrics"].get(p["thresh"]["metric"]); return v is not None and CMP[op](v, value)
        case 10: return any(glob(a.path_glob, x.path) and x.size >= a.min_size for x in rec["artifacts"])
        case 11: return next((t.status == p.expect for t in rec["tests"] if t.id == p.test_id), False)
validate(): 深度 ≤ 16；AND/OR 子项 2..64；未知 op、ARTIFACT 带 schema_ref/contains ⇒ Malformed（fail-closed）
glob: '*' 不跨 '/'，'**' 跨 '/'（C-D4 方言）
```

谓词以 JSON 形式存在 AgentTask 里，UI 用"验收条件"编辑器生成（metric 下拉 + 比较符 + 阈值）。

### 3.7 黑板（HLC + 只增 OR-Set）移植要点

```text
HLC.now(t):   if t > last.wall: last=(t,0,node) else: last=(last.wall, last.logical+1, node)
HLC.merge(r): m=max(t,last.wall,r.wall); logical = max(last.l, r.l)+1 if m==last.wall==r.wall
              else last.l+1 if m==last.wall else r.l+1 if m==r.wall else 0
unit_id = CID(规范原像)；Add 先去重，再验签（V1.0 用 Ed25519，Mock 可以只验 author）
全序 = (wall, logical, node, unit_id)；撤回 = 新增 type="retraction" 且 body.ref=unit_id
Phase: active →conclude→ concluded →archive→ archived；非 active 时拒绝 Add
```

单元类型用于无人机：`claim`（疑似目标）、`evidence`（热成像帧 CID、置信度）、`conclusion`（确认 / 否定）、`intent`（"我去复核"，防止重复出动）、`retraction`。

### 3.8 证据链（Timeline 事件源）

```text
rec = {chain, seq, prev, type, payload, ts};  id = sha256(canonical_json(rec))；V1.0 加 Ed25519 签名
打开时逐条校验 seq 连续、prev 链接、id 一致；末尾半截行截断并补一条 "evidence.gap"（同 ledger.go）
事件类型：agent.task.{submitted,quote,awarded,state,effect,accepted,rejected}，agent.board.unit，agent.lease.{acquired,preempted}
```

### 3.9 UI 的 WebSocket 事件

事件驱动，UI 侧合并到 ≤ 4 Hz 刷新（与 d01 的 HUD 4 Hz 对齐）。channel 名为 `agent/registry`、`agent/tasks`、`agent/board`、`agent/evidence`。消息体：

```json
{"op":"event","ch":"agent/tasks","t":1790000000123,
 "d":{"task":"T-0001","ix":"ix_…","state":"working","awr.phase":"enroute","awr.eta_s":14.2,
      "provider":{"aid":"bafyrei…","name":"P600-02"},"cap":"thermal.imaging",
      "effect":null,"trust":{"verify":null,"auth":null}}}
```

### 3.10 延迟与容量参数（实测）

| 项 | 数值 | 来源 |
|---|---|---|
| 能力委派往返（经 hub） | 921–1016 ms（5 次） | `run_joint.sh` |
| 其中服务端执行 | 65 ms（evidence.latency_ms，mock 睡眠 50–100 ms） | 同上 |
| 未提供的能力 | 约 1 s 后得到 UNAVAILABLE | 同上 |
| daemon 常驻内存 | 约 14 MB/个（10 个共 149 MB），空闲 CPU 0.3%/个 | `run_scale.sh` |
| hub 常驻内存 | 15 MB（10 个 agent，每秒 10 次 poll） | 同上 |
| 结论 | ≤ 50 架时"一机一 daemon"没有压力；更大规模用 Mock，或等 v0.2 的 p2p 与流式传输 | — |

### 3.11 品牌素材：Logo 与头像使用规范

**资产解析（从 SVG 源码测得，并用 headless Chromium 渲染确认，见 `.cache/research/d05/logo.png`、`usage.png`）：**

| 元素 | 规格 |
|---|---|
| 画布 | viewBox 2846 × 493，宽高比 **5.773 : 1** |
| 外形 | 黑色（`#000`）圆角平行四边形徽章，**斜切 10°**（字标 mask 用 `matrix(1 0 −0.173648 0.984808)` = skewX 10°；外框左边斜率实测 10.19°），外描边黑 21.19u（4.3%H） |
| 红框 | 内圈描边 `#E93024`，线宽 42.5u（**8.6%H**），**底边右侧留缺口**（x 59.1%–89.1%），tagline 放在缺口里 |
| 字标 | 像素风斜体 "AGENT-NETWORK"，字高 165u（33.5%H）；填充为红 `#ED2D28 / #EE342E`，下半部有 11 条白色横向扫描纹（复古速度线） |
| tagline | "ROUTE • TRUST • EXECUTE"，白色无衬线，字高 42u（8.6%H） |
| 头像 | GitHub 组织头像（`u/305781773`，有 96 px 和 460 px 两种）：透明底，黑色（`#020200`）圆角方形对话气泡，底部居中有尾巴，红边约 `#F4271C`，内部是白线连接的红色方块节点图和一张白色笑脸 |

**尺寸与可读性（按宽度换算）：**

| 渲染宽度 | 徽章高 | 字标字高 | tagline 字高 | 用法 |
|---|---|---|---|---|
| 120 px | 20.8 | 7.0 | 1.8 | 最小可用（只看字标），不推荐 |
| 185 px | 32.0 | 10.7 | 2.7 | 顶栏高 32 时的上限；tagline 退化为纹理 |
| 320 px | 55.4 | 18.6 | 4.7 | 侧栏页眉、关于页 |
| **480 px** | 83.1 | 27.9 | **7.1** | **tagline 可读下限**：启动页、登录页、报告封面 |
| 640 px | 110.9 | 37.2 | 9.5 | 大屏启动页、演示封面 |

**使用规则：**

1. **两套标志分工。** 完整徽章（wordmark badge）用于"品牌时刻"：启动加载、登录页、关于页、空状态、导出报告封面，宽度 ≥ 480 px（至少 320 px）。**头像标**（avatar mark）用于"界面时刻"：顶栏（24 px）、favicon（16/32 px）、PWA 图标（192/512 px，用 460 px 源图或矢量化版本）、agent 默认头像占位。
2. **顶栏锁定组合：** `[头像 24 px] 10 px 间距 [ANet Drone（600 字重，g50 / g900）] [1 px 分隔线 20 px 高，g700 / g300] [World Runtime（g400 / g600）]`，高 48 px。
3. **安全留白：** 徽章四周 ≥ 0.25 H（约等于 3 倍红框线宽）；头像 ≥ 0.2 × 边长。
4. **暗色 UI（g950 `#0A0B0D` / g900 `#111214`）**：原样使用。黑底与背景融合（1.07:1），轮廓由红框（4.61:1 / 4.38:1）承担，满足非文本图形 ≥ 3:1 的要求。
5. **浅色 UI（g50 `#F2F3F5` / `#FFFFFF`）**：原样使用，黑徽章对背景 18.9:1。**不允许去掉黑底**，否则白色横纹和 tagline 会完全消失。
6. **放在 3D 场景上方时**，徽章不能直接浮在点云上（背景杂乱，红框和点云红色图层冲突）。必须放在实色 chrome（顶栏或侧栏）里，或衬一块实色 g950 圆角底板（圆角 12，内边距 0.25 H）。不用 `backdrop-filter`（d01：软件渲染下很贵）。
7. **禁止：** 改色（包括把红改成 r400 / r700 等文字用红）、描边外发光或阴影、旋转、二次斜切、拉伸、裁掉 tagline 区域（红框缺口会显得残缺）、放在品牌红或任何红色底上、在 logo 内部或周围加 emoji。
8. **品牌色 token：** `--brand: #E93024`（与 d01 的 r500 一致）是唯一的品牌红。头像栅格里的 `#F4271C` 和字标 mask 里的 `#ED2D28 / #EE342E` 是资产内部色，**不作为 UI token**；矢量化头像时统一改为 `#E93024`。
9. **动效（transitions.dev 语言）：** logo 只允许 opacity 0→1 加 translateY 4 px→0，220 ms，`cubic-bezier(.2,.8,.2,1)`；不做颜色、斜切、扫描纹动画；`prefers-reduced-motion` 时直接显示。启动页可以让加载进度条出现在红框缺口处，与 tagline 同基线。
10. **待办：** 向 ANetResearch 要头像的矢量源文件和单色版（白色反白、黑色）。单色水印（截图、PDF 页脚）不能自行从彩色版派生。

---

## 4. 在本项目中的落点与复用方式

### 4.1 复用清单

| 条目 | 用途 | 落点模块 | 版本 | 方式 |
|---|---|---|---|---|
| C1 `CapabilityProvider` 协议 | DroneAgent 的能力内核 | `agent/runtime/provider.py` | V0.6 | port |
| `Registry.Resolve`（精确 + `.` 回退） | 能力解析 | 同上 | V0.6 | port |
| 效果五态 + Evidence + V/A 信任轴 + 钳制规则 | 命令效果、任务结果、UI 徽标 | `agent/runtime/effect.py`；V0.2 起用于 Gateway 命令回执 | V0.2 / V0.6 | port |
| TSIR 谓词 validate/evaluate | 任务验收 | `agent/runtime/tsir.py` | V0.6 | port |
| 黑板 CogUnit / HLC / OR-Set / 相位 | 共享认知 | `agent/runtime/blackboard.py` | V0.6 | port |
| 证据链 | 审计与 Timeline 事件源 | `agent/runtime/evidence.py` | V0.6 | port（简化） |
| `module/service` | 真 ANet 接入 | `agent/anet_bridge/`（FastAPI 路由 + daemon 编排 + 配置生成） | V1.0 | adopt |
| anet 与 ANetHub 二进制 | 身份、发现、中继、回执 | `tools/anet/`（构建脚本，从 refs 与自建 hub 编译） | V1.0 | adopt |
| 控制面 API（`/hub-register /find /delegate /results /evidence /inbox /thread`） | AnetDaemonNetwork 的实现 | `agent/runtime/net_anet.py` | V1.0 | adopt |
| Receipt 校验语义（7 项绑定） | 结果可信度 | `net_anet.py` 读取 `receipt_verified` | V1.0 | adopt |
| AgentCard / A2A AgentSkill 字段 | Capability Manifest | `agent/capabilities/*.json` | V0.6 | reference |
| hub 任务板 | 人工看板 | UI 的"任务看板"只做视图 | V1.0+ | reference |
| MCP（`anet mcp`） | LLM 任务指挥官实验 | `agent/llm_commander/` | V1.x | reference |
| ANetLink 适配器 SDK | 高信任接入 | `agent/anetlink_uav/`（Go） | V1.x | reference |
| logo SVG + 头像 | 品牌 | `apps/web/public/brand/` | V0.1 | adopt |

### 4.2 Agent Runtime 与 ANet 的集成接口（Python）

```python
# agent/runtime/types.py
class EffectStatus(StrEnum): OK="OK"; UNVERIFIED="UNVERIFIED"; FAILED="FAILED"; UNAVAILABLE="UNAVAILABLE"; PAYMENT_REQUIRED="PAYMENT_REQUIRED"
class TaskState(StrEnum): SUBMITTED="submitted"; WORKING="working"; INPUT_REQUIRED="input-required"; COMPLETED="completed"; FAILED="failed"; CANCELED="canceled"; REJECTED="rejected"

@dataclass
class Evidence:  requested:str=""; protocol:str=""; native_ack:bool=False; observed_state:str=""
                 latency_ms:int=0; verify_trust:int=0; auth_trust:int=0; quirk:str=""; simulated:bool=False
@dataclass
class Effect:    status:EffectStatus; metrics:dict[str,float]=field(default_factory=dict)
                 artifacts:list[dict]=field(default_factory=list); message:str=""; evidence:Evidence|None=None
                 def verified(self) -> bool:  return self.status==EffectStatus.OK and self.evidence and (self.evidence.verify_trust>=2 or self.evidence.simulated)
@dataclass
class CapabilityCall: capability:str; args:dict; call_id:str; caller_aid:str=""

# agent/runtime/provider.py —— C1 镜像
class CapabilityProvider(Protocol):
    id: str
    def capabilities(self) -> list[str]: ...
    def describe(self) -> dict: ...                        # awr.capability-manifest/1
    async def invoke(self, call: CapabilityCall) -> Effect: ...
    def health(self) -> str | None: ...                    # None=健康，否则为原因
    def invoke_timeout(self, cap: str) -> float | None: ... # LongRunning

# agent/runtime/network.py —— 两个实现：MockNetwork（V0.6） / AnetDaemonNetwork（V1.0）
class AgentNetwork(Protocol):
    async def register(self, agent: "DroneAgent") -> str: ...                  # 返回 AID
    async def find(self, *, capability: str|None=None, query: str|None=None) -> list[AgentView]: ...
    async def delegate(self, requester: str, provider: str, capability: str, args: dict) -> str: ...  # ix
    async def result(self, requester: str, ix: str, timeout: float) -> TaskResult: ...
    async def cancel(self, requester: str, ix: str) -> None: ...               # v0.1 用 end 近似
    def events(self) -> AsyncIterator[AgentEvent]: ...
```

`AnetDaemonNetwork` 的映射：`register → POST /hub-register {hub,name,caps}`；`find → POST /find {capability}`；`delegate → POST /delegate {provider,capability,args}`；`result` 轮询 `POST /results`，按 `interaction_id` 过滤，解析 `result`（deliverable JSON）得到 `Effect`，用 `receipt_verified` 作为结果可信度；审计用 `POST /evidence`。v0.2 上线后换到 `/tasks/send|get|wait|cancel`，接口签名不变。

### 4.3 REST 与页面落点

| API | 用途 |
|---|---|
| `GET /api/agents`、`GET /api/agents/{aid}/manifest` | Agent 列表与能力清单（UI 右栏 DRONES 下的 AGENT 分区） |
| `POST /api/agent-tasks {capability,args,accept,strategy:"auction"|"direct",provider?}` | 发起协作任务 |
| `GET /api/agent-tasks?state=`、`POST /api/agent-tasks/{id}/cancel` | 任务表、取消 |
| `GET /api/blackboard/{task}`、`GET /api/evidence?since=` | 黑板与证据链 |
| WS `agent/*` | §3.9 |

UI（全部 shadcn 组件，图标用 morphicons，禁止 emoji）：AGENTS 面板用 `Card + Badge`（能力 chip，V/A 信任徽标；只有"需要关注"的状态 failed / rejected / input-required 用品牌红）；任务表用 `Table` 套 lieflat 的 `table.log` 样式（七态列与效果状态列分开）；证据用 `Sheet` 抽屉展示证据链、回执 CID 和"已验证"标记；V1.0 的网络图参考 d01 的 lieflat B3 Threads。

---

## 5. 对比与推荐

本单元只有一个仓库，因此对比的是**接入形态**以及**同类替代方案**：

| 排名 | 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| 1 | **Mock AgentNetwork（移植 ANet 语义）** | 零外部依赖；延迟可调（0 或 1000 ms）；可跑 100+ 架；UI 和逻辑提前成型 | 身份和证据是模拟的 | V0.6 **首选** |
| 2 | **真 ANet（一机一 daemon + `service` + 自建 hub）** | 已实测跑通；真实 AID、签名合同、回执和证据链；与论文和生态一致 | 1 s 轮询；v0.1 → v0.2 正在变更；信任封顶 V1 | V1.0 **首选** |
| 3 | ANetLink `uav` 适配器（Go） | 信任可达 V2–V4，有设备模型和 quirk | 要写 Go；需要 anetlinkd 与 daemon 两层 | V1.x |
| 4 | `anet mcp` + LLM 指挥官 | 自然语言任务拆解 | 不确定性大，需要人审 | V1.x 实验 |
| 5 | zenoh liveliness + queryable（r27） | 毫秒级延迟，发现能力强 | 没有身份、合同、回执和信誉 | 作为**遥测和状态总线**，不替代 ANet |
| 6 | hub 任务板 | 人工看板 | 默认 hub 不带；秒级延迟 | 仅作视图参考 |
| — | x402 / shell / org | — | 与本项目无关，且 shell 有安全风险 | skip |

"2026 活跃度 + star + 契合度"综合评估：ANet 的 star 很少（★6），但 2026-08 到 09 月高强度迭代（v0.1.5 → 0.1.10，ANetCore v0.1 → v0.14），并且是**用户指定的 agent 网络**、与 01-design §31–32 语义完全对应。因此是必选项，只是 V1.0 前要锁定版本（见 §6）。

---

## 6. 风险与注意事项

1. **协议正在迁移（最高风险）。** 本机未推送的检查点（2026-09-27）会把 wire 升到 2：hub 采用 relay v2、HPKE 封装、发送方认证；入站默认 `closed`，`anet accept on` 会直接报错；任务状态改为 A2A 七态；能力调用不再进入对话轮次。refs 快照（wire 1）的 daemon 与新 hub 不兼容。**对策：** V1.0 冻结一组配套版本（anet、ANetHub、ANetCore 三者同一 wire），写入 `tools/anet/versions.lock`；Agent Runtime 只依赖 §4.2 的抽象接口。
2. **延迟与频率。** 轮询间隔 1 s，任务级往返约 1 s，多轮报价约 3 s。不要把 ANet 放进任何控制回路；紧急避让、编队保持都在 Gateway 或 Planner 里做（r25、r26）。
3. **信任口径。** `service` 模块返回 `OK + verify_trust=1`，而 ANetLink 的规则要求 ≥ V2 才能称 OK。Agent Runtime 必须自己钳制（§4.2 的 `Effect.verified()`），否则 UI 会把"飞控 ACK"显示成"已确认"。
4. **能力 id 的坑。** hub 是精确字节匹配、区分大小写，不做 `@` 语义；registry 按 `.` 回退；同一 daemon 内能力不能重复。沿用 §3.1 的规则可以全部规避。
5. **auto-reply 串线。** 无人机节点如果配置了 `auto_reply`，解析不了的能力调用会交给 LLM 自由作答（也就是"幻觉执行"）。无人机节点禁止配置；LLM 指挥官节点单独部署。
6. **长任务超时。** `service` 模块没有实现 `LongRunning`，daemon 按 60 s 截断。飞行和观测类任务必须按 §3.3 做成两段式。
7. **hub 是单点，而且 v0.1 下能读内容。** 必须自建 hub（放在局域网或 GCS），不用公网 hub；野外断网时 hub 放在 GCS 上。p2p（anetpeer）的 rendezvous 是共享目录，没有 NAT 穿透，**不能**当作机间链路。
8. **身份与密钥。** 每个 daemon 的 `identity.kel` 和 `control_token.txt` 放在 `ANET_DATA_DIR` 下（0600）。仿真时 N 个身份都放在 `worlds/<w>/agents/` 下，要加入 `.gitignore`。**绝不能使用 `~/.anet`**，那里是用户本人的身份（本机已存在）。另外，daemon 会写 `/tmp/anet-<uid>/daemon.json` 与 `daemons/`，影响 CLI 的回退发现，编排脚本退出时要清理。
9. **重新注册。** DESIGN §10 缺口 #1（改了能力配置重启后 hub 上的 caps 仍是旧值）在本快照里已由 `daemon.go: refreshRegistration` 部分修复：启动后在后台重新注册，但失败时只记日志、不报错。编排脚本在 daemon 启动后仍应显式调用一次 `/hub-register`，再用 `find --cap` 确认；`cardSeq` 同一秒内冲突的问题已修复并持久化。
10. **命名冲突。** r27 把实时协议命名为 `anet.rt.v1`，与 ANet 的 `anet.*` 证据事件类型、v0.2 的 `anet.effect_status` 元数据键同名空间，容易混淆。建议改为 `awr.rt.v1`（awr 即 A World Runtime），ANet 的名字留给 Agent Network。
11. **品牌。** logo 上的字是 "AGENT-NETWORK"（组织品牌），不是产品名。产品名 "ANet Drone / World Runtime" 必须用 UI 字体另排（§3.11 第 2 条）。README 里用了 emoji，我们的文档和 UI 不照搬。
12. **构建。** 纯 Go 无 CGO，arm64 有发布包（Jetson 可用）。hub 构建需要 `internal/aghub/web` 的内嵌资源已存在（快照里已包含）。本机无 GPU 不影响任何环节。

---

## 7. 对设计文档的优化建议

1. **§3 总体架构图：把"Agent Runtime（ANet / Task Planner / Collaboration / Capability Discovery）"拆成两层。** ① **Agent Runtime**（我们自研，Python，服务端）：任务状态机、合同网分配、验收求值、黑板、证据链、控制租约申请。② **ANet**（外部网络，每个 agent 一个 daemon，加自建 hub）。ANet 不是服务端里的一个库，而是一张叠加网络，经 Drone Agent Adapter 接入。图中应画出 Adapter 与 Gateway 之间的**控制租约**连线。
2. **§31 能力描述：** 补充能力 id 语法与分类法（§3.1），规定**一机一 AID、不带 `@device`**；增加元能力 `agent.describe / agent.state / task.quote`；补充 Capability Manifest（§3.2）和 V0–V4 / A0–A4 信任轴。原文的 `thermal.imaging / rgb.zoom / lidar.mapping / relay.communication` 命名正好可以直接沿用。
3. **§32 工作流需要补齐的工程要素：** ① "发布任务"改写为 **find → quote → score → delegate**（ANet 没有广播原语）；② 每个任务带**验收谓词**（如 `confidence ≥ 0.8 ∧ 存在热成像帧`）和负向范围（禁飞区）；③ 超时、重试、失败升级路径；④ 与控制租约的冲突处理（人工接管优先）；⑤ 黑板上的 claim / intent / evidence / conclusion 序列，防止两架机重复出动；⑥ 结果回传要附带**回执与证据链**，而不仅是"结果"；⑦ 分配依据接入 Environment 场（风、雾、雨对传感器和续航的影响），这正是本项目区别于通用多 agent 框架的地方。
4. **§28 DroneState：** 增加 `agent{aid, network: mock|anet, caps[], lease_owner, current_task, trust{verify,auth}}`；命令结果使用效果五态（V0.2 起：`flight.goto` 回 UNVERIFIED，到达后才算 OK）。
5. **§33 Backend 栈表：** "Agent | ANet" 改为两行："Agent Runtime | 自研 Python asyncio（TSIR、黑板、证据链移植）"和"Agent Network | ANet（v0.1 wire 1 锁定，V1.0 评估 v0.2/A2A）+ 自建 ANetHub"。
6. **§36–37 通信与频率：** 增加一行"Agent 协作平面：事件驱动，ANet 往返约 1 s，UI 合并到 ≤ 4 Hz；不承载控制和遥测"。
7. **§42 Repo：** `agent/anet` 改为 `agent/runtime`（Python 核心）、`agent/anet_bridge`（FastAPI 能力路由、daemon 编排、配置生成、版本锁）、`agent/capabilities`（分类法与 manifest 的 JSON Schema）、`tools/anet`（二进制构建）。
8. **§44–50 路线图：** ① V0.2 就引入"效果五态"作为命令回执模型（成本低，收益贯穿全局）；② V0.6（Multi-UAV）同步交付 **MockAgentNetwork + 合同网分配 + 黑板**，让 UI 的 AGENTS / TASKS 面板和 §32 演示在 V0.6 就能跑；③ V1.0 再换成真 ANet（一机一 daemon + 自建 hub），同时评估 v0.2（E2E、A2A），加入身份密钥管理；④ V1.x 做 ANetLink `uav` 适配器，把信任提升到 V2–V4。
9. **§38–40 UI：** 右栏 DRONES 下增加 AGENT 分区（AID 缩写形如 `bafyrei…gidt3i`、能力 chip、信任徽标、当前任务）；底部 Timeline 增加 agent 事件轨道；新增"任务 / 证据"抽屉；V1.0 增加网络图视图。全部遵循 d01 的 mono+accent：只有"需要关注"的状态用红色。
10. **品牌章节（新增）：** 在 UI 交互 PRD 中写入 §3.11 的 Logo 与头像规范、顶栏锁定组合、禁用清单；`--brand: #E93024` 与 d01 的 ANet Graphite 色卡统一。
11. **文字订正：** §31 "多机仿真成熟之再引入"应为"成熟之后再引入"；§51 结论中"ANet 负责多个 Physical Agent 在这个世界中的自主发现、协同和任务执行"要改准确：**ANet 负责身份、发现、签名委派与证据；协同策略和任务执行由 Agent Runtime 与 Planner / Gateway 负责。**
12. **安全默认值（呼应 ANet v0.2 的"默认安全"）：** 无人机节点只接受机队 AID 白名单的委派（v0.1：部署在自建 hub 且关闭访客配额 `--guest-messages 0`；v0.2：`inbound.policy=closed` 加 `peers.allow`）；绝不编译 `-tags shell`；绝不在飞行节点上配置 auto-reply。
