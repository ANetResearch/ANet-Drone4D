# ANet Drone4D 文档

ANet Drone4D 是面向自主多智能体无人机的真实世界 4D World Runtime。它把采集或重建得到的城市点云整理为带版本的 World，
让环境场、无人机机群与智能体在同一个服务器时钟下运行，并把结果流式传输到任意支持 WebGL2 的浏览器。服务器不需要 GPU。

本文档对应 **0.2.0**（V0.2-demo，即 D2 与 D2.1），发布于 2026-10-13。公开实例位于
[drone4d.agentnetwork.org.cn](https://drone4d.agentnetwork.org.cn)，源代码位于
[GitHub](https://github.com/ANetResearch/ANet-Drone4D)。

## 获取帮助

遇到问题时，可以按以下顺序查找：

- 先查阅 {doc}`project/faq` 与 {doc}`intro/quickstart` 的排错一节。
- 在 [GitHub Issues](https://github.com/ANetResearch/ANet-Drone4D/issues) 检索或提交问题，中文与英文均可。
- 涉及契约（文件格式、`awr.rt.v1`、REST 接口、原因码）的变更建议，请先开 issue 讨论，见
  [CONTRIBUTING.md](https://github.com/ANetResearch/ANet-Drone4D/blob/main/CONTRIBUTING.md)。
- 安全问题请发送至 hi@anet0.com，见 [SECURITY.md](https://github.com/ANetResearch/ANet-Drone4D/blob/main/SECURITY.md)。

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
: D1 与 D2 的发布内容，D3 的设计要点与阶段计划，以及后续版本主题。

{doc}`project/acceptance`
: D2 第 2 轮验收的结果、未通过项的根因与完整报告的链接。

{doc}`project/faq`
: 常见问题。

{doc}`project/citation`
: 引用 ANet Drone4D 与 UrbanScene3D，许可证与第三方声明。
