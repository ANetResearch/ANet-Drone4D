# 概览

## 定位

ANet4D 是新一代具身智能体协同模拟基础设施，用于研究多个智能体在动态的真实地点中如何共同决定在何处、何时、以何种方式感知与行动：
当前为多无人机机群，后续方向还包括无人车（UGV）、无人船（USV）、地面机器人、固定传感器与软件智能体在同一实验中协同。
实验以可复现、可审计为目标，使一个团队能够复跑、核查并逐项替换另一个团队的实验设置。

机器人仿真器通常从载具出发，再配一张手工搭建的地图；数字孪生通常能展示一个地方，却不能在其中运行任务。
ANet4D 从地方出发：采集或重建得到的点云成为一个带版本的 **World**，环境场、无人机机群与它们的智能体都运行在这个
World 上、共用同一个仿真时钟，浏览器则在任意位置观看与操作。整条链路是：

```text
Reality → Reconstruction → World → Environment → Simulation → Agent
```

"4D" 指三维空间加时间：无人机、天气与时间轴共用一个仿真时钟，任何时刻都可以暂停、单步、录制与回放。

设计上的四条原则：

- **World 是核心。** 采集、重建与仿真汇入同一个 World Package：供物理与查询使用的几何、供显示的点云、语义与禁飞区，锚定在真实地点上。
- **浏览器看世界，服务器算世界。** 物理、安全、规划、感知与环境运行在无 GPU 的 Linux 服务器上；浏览器只负责流式加载与绘制。
- **环境是一个 4D 场。** 风、湍流、能见度与降水构成同一个场 E(x, y, z, t)，同一组数值既作用于机体，也用于绘制天空。
- **实体是 Physical Agent。** 每架无人机（当前已交付的实体类型）公布自身能力，其他智能体发现它、询价并委派任务；每一条飞行命令仍要通过仿真器的准入与安全检查。

```{image} ../../../docs/media/screenshot-hero.jpg
:alt: 合成演示城市 ANet Synthetic City 中的 ANet4D 沙盘：世界、图层与天气面板，机群列表，时间轴与性能 HUD
:width: 100%
```

上图为 V0.1（D1）在合成演示城市 ANet Synthetic City 中的真实截图（无 GPU 的 8 核机器，软件渲染）。

## 当前版本

| 项 | 内容 |
|---|---|
| 版本 | 0.3.2（V0.3.2 · ANet4D）：包含 2026-10-25 以标签 v0.3.0 封版的 V0.3 事件沙盘、改名为 ANet4D 以及实验性的场景目标基础；各版本的 GitHub Release 附已知问题 |
| 公开实例 | [anet4d.agentnetwork.org.cn](https://anet4d.agentnetwork.org.cn)：共享展示、每位访客一个沙盒与只读的事件沙盘样例，无需安装与登录 |
| 服务器 | 交互沙盒：Linux x86-64，Python 3.12，无需 GPU；公开实例运行在 4 核、无 GPU 的服务器上。事件沙盘：Linux x86-64，传感器生成需要 NVIDIA GPU |
| 浏览器 | 支持 WebGL2 的桌面、平板或手机浏览器；画质与帧率取决于访客设备的 GPU |
| 许可 | ANet 开源许可证（改版 Apache-2.0），见 {doc}`../project/citation` |

## 能力与交付状态

状态标注的含义：

- [已交付]{.anet4d-status}：已随正式版本发布，并通过对应验收或以已知问题登记。
- [开发中]{.anet4d-status}：设计已定稿并列入当前阶段计划，接口与验收已定义，尚未发布。
- [研究]{.anet4d-status}：处于评估或原型阶段，是否进入产品以评估结论为准。

| 能力 | 说明 | 状态 |
|---|---|---|
| World Package v1 | 点云规范化（单位、上方向、调平、北向、原点、法线、地面模型）、Potree 2.0 兼容八叉树与 ANET_Q16 编码、DSM 与 DTM、语义与区域；`validate --deep` | [已交付]{.anet4d-status} V0.1 |
| 七个世界 | 合成演示城市 ANet Synthetic City 与 UrbanScene3D 六城（经数据集权利方授权用于本项目部署） | [已交付]{.anet4d-status} V0.1 / V0.2 |
| 城市级点云流式加载 | 每个八叉树根一次 Range 请求取回首屏层级；APH 选择器与 CAS 控制器在点预算与 7 档画质之间闭环调节 | [已交付]{.anet4d-status} V0.1 |
| 4D 环境场 | 风（高度廓线与湍流）、能见度与降水，12 个天气预设；仿真日历、太阳位置、照度与按波长消光 | [已交付]{.anet4d-status} V0.1 / V0.2 |
| 多机同钟 FleetSim | 1 至 1,000 架共享 250 Hz 时钟；PX4 风格级联由 numba 编译；1,000 架约用半个 CPU 核保持实时 | [已交付]{.anet4d-status} V0.1 |
| 安全与任务 | 14 态 Flight FSM、地理围栏、能量感知返航、机间间距守护；8 类任务生成器 | [已交付]{.anet4d-status} V0.1 |
| 时间轴、录制与回放 | 暂停、单步、倍速 ×0.25 至 ×10；MCAP 录制与逐字节一致的回放（本机运行；公开实例不提供） | [已交付]{.anet4d-status} V0.1 |
| 智能体协作 | 进程内 Mock ANet：能力发现、询价、委派与回执核验（S3 搜救剧本） | [已交付]{.anet4d-status} V0.1 |
| 交互式沙盒 | 每位访客一个隔离会话与独立 sim-core；准入、排队、续期、快照与切换底图 | [已交付]{.anet4d-status} V0.2 |
| 识别物与感知量化 | 六类识别物与敏感范围；Johnson 准则与 TTPF 统一的检测、识别、确认三级；逐对感知读数与场视图 | [已交付]{.anet4d-status} V0.2 |
| 机巢、任务编排与接力 | 六类任务与区域值守、贪心分配器与 Allocator 接口、GSD 扫描反推、7×24 接力监控与可持续性评估 | [已交付]{.anet4d-status} V0.2（白天接力短间隙为已知问题） |
| 开放接口与 Python SDK | 沙盒 REST v1、`awr.rt.v1` 实时协议、OpenAPI 3.1 与单文件 SDK | [已交付]{.anet4d-status} V0.2 |
| 机型目录 | P600 数字孪生（MID-360 配置）与四款通用参考机型，含固定翼与垂起 | [已交付]{.anet4d-status} V0.2 |
| 启动、容量与多端 | 版本与进度启动屏、观看席位与沙盒槽位排队、节能模式、手机与平板布局 | [已交付]{.anet4d-status} V0.2 |
| 网格世界 | 摄影测量 GLB 网格经米制尺度、真北与调平标定，派生几何、导航与质量图层 | [已交付]{.anet4d-status} V0.3 |
| 事件仿真与主动观测基准 | 同一世界与同一事件下比较不同机队策略的观测；事件 DSL 与事件磁带、评测与内部排行榜 | [已交付]{.anet4d-status} V0.3 |
| 高保真物理档 | gz-sim 锁步档作为正式成绩的物理档；FleetSim 保留为快速档 | [已交付]{.anet4d-status} V0.3 |
| 传感器生成服务 | 网格光栅化相机（RGB、深度、实例、语义、卷帘快门、热成像）、按 Livox MID-360 扫描模式的网格 LiDAR 与 IMU 合成 | [已交付]{.anet4d-status} V0.3 |
| Agent SDK 与基线策略 | Python SDK `anet4d`、经 Unix 套接字的 Agent 线协议、隔离的 Agent 容器与三个基线策略 | [已交付]{.anet4d-status} V0.3 |
| 场景目标基础 | 目标外观、活动与群体绑定，以契约中新增的可选字段表达；缺省关闭，公开实例不提供 | [开发中]{.anet4d-status} V0.3.2 |
| 大模型指挥层 | 经推理网关接入大模型指挥层与 ANet 多智能体委派（已移出 V0.3 封版范围） | [研究]{.anet4d-status} D3.x / D4 |
| 3DGS 外观层 | 网格蒸馏与真实航拍资产的高斯外观层；以 G-3DGS 评估门决定是否进入产品 | [研究]{.anet4d-status} D3.x / D4 |
| 跨实体协同实验 | 异构实体（无人机、无人车、无人船、地面机器人、固定传感器、软件智能体）在同一实验中协同；声明式实验规格、可替换的协同算法与协作排行榜 | [研究]{.anet4d-status} D4 |
| GPU 重建 | LingBot-Map、DA3-Streaming 与 MapAnything 等重建引擎接入（Mock 引擎链路已打通） | [研究]{.anet4d-status} V0.5 |
| 真 ANet 网络 | 自建 ANetHub、每机一个 daemon、断网下的 CBBA 分配 | [研究]{.anet4d-status} V1.0 |

## 文档地图

| 你想要 | 从这里开始 |
|---|---|
| 不安装，直接体验 | [公开实例](https://anet4d.agentnetwork.org.cn)，以及 {doc}`../guide/showcase` 与 {doc}`../guide/sandbox` |
| 在本机运行 | {doc}`install` 与 {doc}`quickstart` |
| 用程序控制沙盒 | {doc}`../api/sdk` 与 {doc}`../api/rest` |
| 理解系统内部 | {doc}`../topics/architecture` 与 {doc}`../topics/worlds` |
| 部署自己的实例 | {doc}`../topics/deployment` |
| 了解进度与质量 | {doc}`../project/roadmap` 与 {doc}`../project/acceptance` |

设计文档以中文撰写，位于仓库 {gh}`docs/ </tree/main/docs>`：设计基线与 85 条 ADR（docs/03）、
D2 设计增补（docs/04）、D3 设计增补（docs/05）、各模块 PRD（docs/modules/）与实现和验收报告（docs/impl/）。本文档站面向使用者，
概括其中与使用相关的部分；两者不一致时以设计文档为准。
