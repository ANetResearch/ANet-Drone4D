# ANet4D 文档

**ANet4D：新一代具身智能体协同模拟基础设施。** ANet4D 的目标是成为具身智能体协同研究的标准实验基础设施，如同 NS-3 之于网络研究：
在同一个世界、同一个时钟与同一套评测协议下研究由具身智能体与软件智能体组成的协同团队。规划覆盖的实体包括多无人机、无人车（UGV）、
无人船（USV）、地面机器人、固定传感器以及无实体的软件智能体。

当前基础是 V0.1 至 V0.3 已交付的能力：

- **真实世界 4D 数字孪生。** 采集或重建得到的城市点云与摄影测量网格整理为带版本的 World，同时服务于物理、查询与显示；
  4D 环境场（风、能见度、降水）与仿真共用同一个时钟。
- **交互沙盒（V0.2）。** 每位访客一个隔离的仿真核心，包含机群仿真、识别物与感知量化、机巢与任务编排、REST 接口与 Python SDK；
  服务器无需 GPU，场景由浏览器渲染。
- **事件沙盘（V0.3）。** 由声明式事件规格编译的事件磁带、以 Gazebo Sim 锁步作为高保真物理档、网格渲染的相机、热成像与 LiDAR 传感器，
  以及附 Agent SDK 与基线策略的评测运行时；传感器生成需要服务器配备 NVIDIA GPU。

目前已交付的实体类型为无人机（多旋翼、固定翼与垂起机型）。后续方向是跨实体的协同实验平台：声明式实验规格、可替换的协同算法与可比较的
实验证据，见 {doc}`project/roadmap`。

本文档对应 **0.3.2**（V0.3.2 · ANet4D）。公开实例位于
[anet4d.agentnetwork.org.cn](https://anet4d.agentnetwork.org.cn)，源代码位于 {gh}`GitHub </>`。ANet4D 曾以 ANet Drone4D
之名发布；原站点与原文档地址均跳转到新地址，少量遗留标识符仍保留原名（见 {doc}`project/faq`）。

## 获取帮助

遇到问题时，可以按以下顺序查找：

- 先查阅 {doc}`project/faq` 与 {doc}`intro/quickstart` 的排错一节。
- 在 {gh}`GitHub Issues </issues>` 检索或提交问题，中文与英文均可。
- 涉及契约（文件格式、`awr.rt.v1`、REST 接口、原因码）的变更建议，请先开 issue 讨论，见
  {gh}`CONTRIBUTING.md </blob/main/CONTRIBUTING.md>`。
- 安全问题请发送至 hi@anet0.com，见 {gh}`SECURITY.md </blob/main/SECURITY.md>`。

## 第一步

```{toctree}
:caption: 第一步
:hidden:

intro/overview
intro/install
intro/quickstart
```

{doc}`intro/overview`
: 定位、核心能力与各项能力的交付状态（已交付、开发中、研究）。

{doc}`intro/install`
: 服务器与浏览器要求、端口、目录、环境变量与运行档。

{doc}`intro/quickstart`
: 从 `git clone` 开始，在本机运行共享展示与一个沙盒会话，并用 Python SDK 创建会话、添加无人机、读取状态、结束会话。

## 使用指南

```{toctree}
:caption: 使用指南
:hidden:

guide/showcase
guide/sandbox
guide/targets
guide/tasking
guide/mobile
guide/identity
guide/sdk-quickstart
```

{doc}`guide/showcase`
: 只读的共享展示会话：剧本 S7、相机与传感器视图、识别物与接力统计；其余六城各自播放自己的模拟监测展示。

{doc}`guide/sandbox`
: 每位访客一个隔离沙盒：世界与场景模板、管理面板、时钟与倍速、手动控制、快照与切换底图、寿命与节能。

{doc}`guide/targets`
: 识别物的类别、属性与敏感范围，"被发现"判据，检测、识别、确认三级感知与明确捕获。

{doc}`guide/tasking`
: 六类任务与区域值守、机巢与电池、GSD 扫描反推、7×24 接力监控与可持续性评估。

{doc}`guide/mobile`
: 手机与平板的设备档、布局、触控手势与画质。

{doc}`guide/identity`
: 面向内部人员的身份入口：位置与用途。

{doc}`guide/sdk-quickstart`
: 事件沙盘（V0.3）的 Python SDK `anet4d`：安装、编写示例 Agent，在高保真仿真档与快速仿真档上运行 3 机回合。

## 开放接口

```{toctree}
:caption: 开放接口
:hidden:

api/rest
api/sdk
```

{doc}`api/rest`
: 沙盒 REST v1 与本机运行时接口的端点表（由 OpenAPI 快照生成）、鉴权、原因码、配额与实时协议。

{doc}`api/sdk`
: 单文件 Python SDK `awr_sandbox` 的安装、对象模型、调用一览、错误处理与示例。

## 系统与数据

```{toctree}
:caption: 系统与数据
:hidden:

topics/architecture
topics/worlds
topics/deployment
```

{doc}`topics/architecture`
: 进程拓扑、数据流、时钟与沙盒层；World ENU、机体 FLU、渲染帧与 PX4 边界等坐标约定。

{doc}`topics/worlds`
: World Package v1、七个世界、合成演示城市与 UrbanScene3D 的授权与引用。

{doc}`topics/deployment`
: 公开演示站的部署套件、资源上限、路径分流与本文档站的发布方式。

## 其余

```{toctree}
:caption: 其余
:hidden:

project/roadmap
project/acceptance
project/faq
project/citation
```

{doc}`project/roadmap`
: V0.1 至 V0.3 的交付内容、V0.3.2 的范围，以及 ANet4D 的后续方向（D4）。

{doc}`project/acceptance`
: D2 第 2 轮验收的结果、未通过项的根因与完整报告的链接。

{doc}`project/faq`
: 常见问题。

{doc}`project/citation`
: 引用 ANet4D 与 UrbanScene3D，许可证与第三方声明。
