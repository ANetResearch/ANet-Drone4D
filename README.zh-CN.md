<div align="center">

<img src="docs/media/drone4d-banner.jpg" alt="ANet-Drone4D：面向多智能体无人机的四维世界运行时" width="100%" />

<h3>从真实世界，到无人机集群。</h3>

真实世界驱动的多智能体无人机四维仿真平台：城市级点云数字孪生、机群仿真、集群控制与目标感知在同一仿真时钟下运行，无需服务端 GPU 即可在任意浏览器中实时渲染。

[![Release](https://img.shields.io/badge/release-v0.2.0-1f1f1f)](https://github.com/ANetResearch/ANet-Drone4D/releases/tag/v0.2.0)
[![CI](https://github.com/ANetResearch/ANet-Drone4D/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ANetResearch/ANet-Drone4D/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-Read%20the%20Docs%20layout-1f1f1f)](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/)
[![Live demo](https://img.shields.io/badge/live%20demo-drone4d.agentnetwork.org.cn-e0322d)](https://drone4d.agentnetwork.org.cn)
[![License](https://img.shields.io/badge/license-modified%20Apache--2.0-1f1f1f)](LICENSE)

**[在线演示](https://drone4d.agentnetwork.org.cn)** · **[文档](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/)** ·
[快速开始](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/intro/quickstart.html) ·
[开放接口](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/api/rest.html) ·
[发布记录](https://github.com/ANetResearch/ANet-Drone4D/releases) · [ANet](https://github.com/ANetResearch/ANet)

[English](README.md) · **简体中文**

</div>

<br/>

<img src="docs/media/demo-flight.webp" alt="ANet 合成城市中的场景 S0：无人机环绕 ANet Tower 螺旋爬升、覆盖街区，并沿河道以 V 形编队飞行" width="100%" />

<sub>ANet 合成城市中的场景 S0，在无 GPU 的机器上录制，以两倍实时速度播放。</sub>

## 要点

- **点云数字孪生。** 七个世界——ANet 合成城市与 UrbanScene3D 六座城市——以八叉树点云流式载入浏览器，点密度随视点与帧预算自适应调节。
- **无人机集群仿真与集群控制。** FleetSim 在 250 Hz 共享时钟下推进 1 至 1,000 架飞行器，在同一回路中运行 PX4 风格飞行模型、任务生成器、电子围栏与机间间距保护。
- **目标物、感知与任务。** 六类目标物按 Johnson/TTPF 判据在 EO、NIR、LWIR、雷达与声学传感器上执行探测；机巢与贪心任务分配驱动无人机在各目标物敏感范围外的驻点执行 7x24 接力监视。
- **每位访客一个交互沙盒。** 每个会话运行隔离的仿真核心，可通过桌面浏览器、移动设备、REST v1 或 Python SDK 访问。公开演示站上的每座城市还持续播放录制的无人机模拟监测展示。
- **面向下一阶段。** D3 事件实验室引入 Gazebo Sim 锁步物理、网格相机与 LiDAR、FAST-LIVO2 重建对比，以及面向多智能体观测策略的事件级基准评测。

## 能力

| | 能力 | 状态 |
|---|---|---|
| 数字孪生 | 城市级点云、渐进加载、自动点密度 | 已交付 |
| 无人机集群 | FleetSim，同一时钟下最多 1,000 架飞行器 | 已交付 |
| PX4 | PX4 风格位置与姿态级联（PX4-lite），以 PX4 SIH 飞行记录校验 | 已交付 |
| 集群控制 | 编队、区域覆盖、电子围栏、间距保护、14 状态飞行状态机 | 已交付 |
| 目标感知 | 六类目标物，覆盖 EO、NIR、LWIR、雷达与声学传感器 | 已交付 |
| 智能体集群 | 基于 Mock ANet 的能力发现与任务委派（公开站未启用） | 已交付 |
| Gazebo | Gazebo Sim 锁步，作为高保真物理档 | 开发中 |
| LiDAR 与相机 | 网格 LiDAR（Livox MID-360 扫描模式）与照片级网格相机 | 开发中 |
| 四维重建 | 以仿真数据驱动 FAST-LIVO2，并与网格真值对比评估 | 开发中 |
| Rerun | 录制数据导出至 Rerun 检视 | 开发中 |
| 3DGS | 叠加在网格几何之上的高斯泼溅外观层 | 研究 |
| PX4 SITL | 与 Gazebo 联合的飞控软件在环验证 | 研究 |

状态反映 [v0.2.0 发布](https://github.com/ANetResearch/ANet-Drone4D/releases/tag/v0.2.0)与[D3 设计基线](docs/05-D3-设计增补与决策记录.md)。

## 开始使用

无需注册即可访问**[在线演示](https://drone4d.agentnetwork.org.cn)**，站点提供只读的共享展示以及为每位访客隔离的沙盒。在 Linux 上本地运行平台：

```sh
git clone https://github.com/ANetResearch/ANet-Drone4D.git && cd ANet-Drone4D
make setup        # 安装 Python 与 Node 依赖
make demo-world   # 生成 ANet 合成城市
make run          # 随后打开 http://127.0.0.1:8000/world/synthcity
```

[快速开始](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/intro/quickstart.html)指南包含每一步的真实输出，并介绍如何通过 Python SDK 驱动沙盒会话。

## 功能导览

> [!NOTE]
> 各功能小节开篇的插图为概念图，专为本 README 生成并统一调色至 ANet Graphite 色板。标注为**实景截图**的图像直接截取自通过 `make demo-world` 生成的 ANet 合成城市。

### 从真实世界到世界包

<img src="docs/media/feature-reality.jpg" alt="由航测数据数字化为点云孪生的城市街区" width="100%" />

`worldpkg` 将原始点云归一化——统一单位、上方向轴、水平校准、正北朝向、原点、法线、地面模型、离地高度与分类标签——构建为 World Package v1。该格式将采用 12 字节 ANET_Q16 点编码、兼容 Potree 2.0 的八叉树与 DSM/DTM 栅格、语义图层及分区数据相结合。UrbanScene3D 的六座城市（每座约 500 万点）仅用 CPU 即可在 27 至 35 s 内完成构建，并以零错误通过 `validate --deep` 校验。ANet 合成城市由固定随机种子生成（1.2 km × 1.2 km，约 390 万点，零第三方数据），构建耗时约 25 s。

### 流式加载城市级点云

<img src="docs/media/feature-streaming.jpg" alt="点云瓦片八叉树，近处密集、远处稀疏" width="100%" />

八叉树初始层级对每个根节点发起单次 HTTP 范围请求完成加载，其余节点在 Web Worker 中流式读取。APH 选择器在两级点预算内按最佳优先策略排列八叉树节点优先级。同时，CAS 控制器根据实测帧耗时动态调整点预算与七级质量阶梯，使点密度与当前视口及设备性能相匹配。渲染引擎通过分页式 GPU 点池以单次绘制调用（draw call）渲染全部点云，所有渲染路径均在 WebGL2 规范最小上限下通过验证。

<img src="docs/media/screenshot-streaming.jpg" alt="ANet 合成城市中八叉树渐进细化的三个阶段：粗粒度节点先行、细化中、完成" width="100%" />

<sub>**实景截图。** 跳转至近景视角时，常驻内存的粗粒度节点立即完成渲染（724 K 点），随后在点预算内逐步细化（1.04 M 点），最终在预算上限处完成加载（1.50 M 点）。</sub>

### 作为四维场的环境

<img src="docs/media/feature-environment.jpg" alt="点云塔楼周围的风场流线、降雨与低空雾气，以及单架无人机的阵风响应" width="100%" />

环境服务在从晴空到沙尘暴的 12 种天气预设下计算风场（由高度剖面与湍流盒叠加）、能见度与降水。物理引擎以 50 Hz 采样该四维场，通过相对空速将风力作用于每架机体。浏览器端求解完全相同的公式以渲染雨、雪、沙尘、雾、云与风场流线。10,261 个基准测试用例（golden test cases）保证服务端物理计算与客户端渲染保持一致。

<img src="docs/media/screenshot-weather.jpg" alt="ANet 合成城市同一视角在小雨、雾气涌入与浓雾下的表现" width="100%" />

<sub>**实景截图。** 同一视点下由小雨向浓雾过渡：在 40 s 过渡期的中段，能见度由 6.0 km 降至 690 m，最终稳定在 150 m。</sub>

### 多机集群，统一时钟

<img src="docs/media/feature-swarm.jpg" alt="点云城市上空的无人机编队与覆盖航线" width="100%" />

FleetSim 将全部无人机组织为单一的数组结构体（SoA），在 250 Hz 共享时钟下同步推进。其 PX4 风格的位置与姿态级联控制器经 numba 编译，通过 numpy 参考实现校验，并与 PX4 SIH 飞行记录比对验证。在 1,000 架无人机规模下，FleetSim 仅占用约半颗 CPU 核心即可保持实时运行。安全逻辑在同一回路内执行，涵盖 14 状态飞行状态机、电子围栏、能量感知自动返航、机间间距保护与失联处置策略。八种任务生成器提供从建筑立面螺旋扫描到编队飞行与区域覆盖的各类轨迹。

<table>
<tr>
<td width="50%"><img src="docs/media/screenshot-swarm.jpg" alt="三架无人机组成的 V 形编队在河道东端转弯，以及执行街区覆盖任务的无人机往复航线" width="100%" /></td>
<td width="50%"><img src="docs/media/screenshot-follow.jpg" alt="编队领航机后方的第三人称跟随视角，显示三架编队无人机的标签与轨迹" width="100%" /></td>
</tr>
</table>

<sub>**实景截图。** 左：三机 V 形编队在河道东端转向，同时一架覆盖任务无人机沿往复航线（lawnmower lanes）扫描两个城市街区。右：编队领航机后方的第三人称跟随视角。</sub>

### 作为物理智能体的无人机

<img src="docs/media/feature-agents.jpg" alt="三架不同型号的无人机协同搜索地形，其中一架热成像无人机发现目标" width="100%" />

每架无人机在智能体网络上注册自身能力。在 S3 搜救场景中，光学相机无人机探测到候选目标物后，通过能力发现机制在网络中查询具备热成像能力的无人机，收集由仿真器内部估算器定价的报价，并将确认任务委派给最优响应方。任务结果通过回执与验收谓词进行校验。V0.1 在进程内 Mock ANet 上执行该工作流；完整 ANet 集成规划于 V1.0 交付。

### 交互沙盒、目标物与接力监视

每位访客均可获得由独立仿真核心驱动的隔离沙盒。单个会话支持最多 12 架无人机、30 个目标物与 4 座机巢，由特定世界的场景模板初始化，并可通过桌面浏览器、移动设备或开放接口进行控制。目标物涵盖六个类别，按尺寸、运动特征、热特征、雷达散射截面积（RCS）与周期性声发射进行参数化建模。感知管线在 EO、夜视、LWIR、LiDAR、雷达与声学传感器上应用 Johnson 判据与 TTPF，实时计算每一对“无人机-目标物”的接收声压、张角尺寸、等效像素数与探测概率。机巢、贪心任务分配器以及七类任务（从定点监视、按 GSD 区域扫描，到目标物敏感范围外的 7x24 接力监视）均在共享仿真时钟下运行。准入控制、显式排队与自动节能模式确保公开站始终运行在容量上限之内。

## 实景

<table>
<tr>
<td width="50%"><img src="docs/media/landing-desktop.jpg" alt="ANet Drone4D 公开演示站首页" /></td>
<td width="50%"><img src="docs/media/d2-mobile.jpg" alt="两部手机上的共享展示与底部标签栏" /></td>
</tr>
<tr>
<td><sub>公开演示站首页。</sub></td>
<td><sub>支持窗口切换的手机端布局。</sub></td>
</tr>
<tr>
<td><img src="docs/media/city-shanghai.jpg" alt="UrbanScene3D 上海点云" /></td>
<td><img src="docs/media/city-newyork.jpg" alt="UrbanScene3D 纽约点云" /></td>
</tr>
<tr>
<td><img src="docs/media/city-shenzhen.jpg" alt="UrbanScene3D 深圳点云" /></td>
<td><img src="docs/media/city-sanfrancisco.jpg" alt="UrbanScene3D 旧金山点云" /></td>
</tr>
<tr>
<td><img src="docs/media/city-chicago.jpg" alt="UrbanScene3D 芝加哥点云" /></td>
<td><img src="docs/media/city-suzhou.jpg" alt="UrbanScene3D 苏州点云" /></td>
</tr>
<tr>
<td colspan="2"><sub>公开演示站上的 UrbanScene3D 六座城市，在浏览器中实时渲染。</sub></td>
</tr>
</table>

<details>
<summary><b>工作原理</b></summary>

<br/>

- **世界。** 每个世界包将八叉树点云、地形模型、语义图层与派生缓存封装在统一的米制东北天（ENU）坐标系中。
- **仿真。** `sim-core` 在单一的整数纳秒时钟下推进机群、环境、目标物、感知与任务编排。
- **实时协议。** `awr.rt.v1` 将 JSON 控制面与二进制批量传输层相结合，并引入基于信用（credit-based）的背压机制，确保慢速客户端不会阻塞服务端。
- **沙盒。** `sandbox-pool` 在 cgroup 资源配额下隔离每个会话的仿真核心，执行准入控制、可见队列以及自动回收空闲会话的节能模式。
- **浏览器。** 客户端基于 React 与命令式 three.js 引擎构建，状态按世界隔离作用域，切换城市时不会产生跨世界的实体残留。
- **契约先行。** 由 JSON Schema 统一生成 Python 与 TypeScript 类型定义，以基准文件（golden files）保持多端实现对齐，并通过进程监管器执行心跳检测、退避重试与熔断保护。

详见：[架构](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/topics/architecture.html) ·
[世界与数据](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/topics/worlds.html) ·
[设计文档](docs/)。

</details>

<details>
<summary><b>开放接口与 Python SDK</b></summary>

<br/>

沙盒在 `/api/sandbox/v1` 暴露 REST v1 接口，并与 `awr.rt.v1` 实时协议配合使用。Web 界面与 API 共享完全一致的权限，所有指令均经过相同的准入与安全检查。附带可复制示例的交互式 OpenAPI 文档位于 [`/sandbox/api`](https://drone4d.agentnetwork.org.cn/sandbox/api)。单文件 Python SDK（[`tools/sdk/awr_sandbox.py`](tools/sdk/awr_sandbox.py)）内置排队等待、会话续期、从节能模式恢复以及二进制帧解码逻辑：

```sh
pip install "httpx>=0.24" "websockets>=12" "msgpack>=1.0"
curl -O https://drone4d.agentnetwork.org.cn/sdk/awr_sandbox.py
```

```python
from awr_sandbox import Sandbox

sb = Sandbox("https://drone4d.agentnetwork.org.cn", timeout_s=30)
with sb.create("synthcity", "t5_area_guard") as s:   # 公开站满载时自动排队
    print(s.state()["state"])
    for msg in s.stream(["sandbox/session"], seconds=3):
        print(msg.topic, msg.data)
```

参考：[REST 接口](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/api/rest.html) ·
[Python SDK](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/api/sdk.html) ·
[示例](tools/sdk/examples/)。

</details>

<details>
<summary><b>机型目录</b></summary>

<br/>

机型目录包含搭载 MID-360 配置的 P600 数字孪生机型，以及四款通用参考机型：两款四旋翼（AWR-Q1 与 AWR-Q9）、一款长航时固定翼（AWR-F1）与一款垂起固定翼（AWR-V1）。固定翼飞行模型对协调转弯运动学、能耗以及随风场调整的盘旋半径进行建模，垂起固定翼模型则施加过渡与悬停限制。支持的载荷涵盖可见光、近红外、热成像、雷达与声学传感器。所有参考数值均为仿真参数，不代表商业硬件规格。

</details>

<details>
<summary><b>更多能力</b></summary>

<br/>

- **时间作为维度。** 支持暂停、单步推进以及 x0.25 至 x10 倍速回放；支持基于 MCAP 的会话录制与回放；支持由输入日志驱动的确定性重仿真。
- **任务与集群。** 生成螺旋扫描、环绕盘旋、扩展方波、航路走廊、地形跟随、往复扫描（lawnmower）、编队与路径跟随航线；支持基于高度图的安全转场、2.5D A* 路径规划、编队位置分配与起飞前电量检查。
- **几何查询。** 在安全、规划与 UI 模块间统一评估海拔高度、离地高度（AGL）、障碍物净空、视距（LoS）、射线投射与路径有效性。
- **场视图。** 将声学、视觉、热辐射与雷达发射渲染为带等值面壳体的球形点场，并同步渲染指示探测距离的感知包络体。
- **多端适配。** 提供带版本号与加载进度的启动画面、平板端指挥布局、支持窗口切换的精简手机端界面，以及全屏 PWA 支持。

</details>

<details>
<summary><b>版本路线</b></summary>

<br/>

| 版本 | 范围 | 状态 |
|---|---|---|
| V0.1 | 点云数字孪生、渐进加载、FleetSim 与 PX4-lite、实时协议、Mock ANet | 已发布 |
| V0.2.0 | 交互沙盒、目标物与量化感知、机巢与接力监视、开放接口、手机与平板适配、城市模拟监测展示 | 已发布 |
| V0.3 | D3 事件实验室：GLB 世界、事件磁带、Gazebo Sim 锁步、网格相机与 LiDAR、FAST-LIVO2、基准评测 | 开发中 |
| P3 与 V1.0 | 3DGS 外观层、PX4 SITL、配备自托管 ANetHub 的真实 ANet | 规划中 |

进度以 [GitHub Milestone](https://github.com/ANetResearch/ANet-Drone4D/milestones) 与置顶的[路线图 Issue](https://github.com/ANetResearch/ANet-Drone4D/issues/1) 为准。参见[版本路线](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/project/roadmap.html)与[D3 设计](docs/05-D3-设计增补与决策记录.md)。

</details>

<details>
<summary><b>V0.2.0 验收</b></summary>

<br/>

V0.2.0 依据 55 项验收标准（D2-AC-01 至 D2-AC-55）进行评估。第 2 轮验收结果（2026-10-12 至 10-13）：

| 项目 | 通过 | 不通过 | 未测 |
|---|---|---|---|
| P0 (50) | 41 | 9 | 0 |
| P1 (5) | 3 | 0 | 2 |

24 h 浸泡测试以零重启完成，内存增长率为 0.48 MB/h。其余未通过项作为已知问题记录于[发布说明](https://github.com/ANetResearch/ANet-Drone4D/releases/tag/v0.2.0)；完整报告见 [docs/impl/D2-验收报告-第2轮.md](docs/impl/D2-验收报告-第2轮.md)。

</details>

<details>
<summary><b>设计系统与技术栈</b></summary>

<br/>

服务端技术栈运行 Python 3.12、numba、FastAPI 与 zenoh；浏览器客户端采用 React 19、TypeScript 与 three.js。用户界面结合了 shadcn/ui 组件、Transitions.dev 动效令牌、morphicons 图标，以及用于图表与数据表的 lieflat 视觉语言，整体采用灰、黑、白、红构成的 ANet Graphite 色板。文档基于 Sphinx 构建，采用 Read the Docs 布局。

<img src="docs/media/screenshot-perf.jpg" alt="性能面板：呈现间隔、帧间隔直方图、点预算仪表、各层级点数、流式传输、图层预算表、降级阶梯与控制器状态" width="100%" />

<sub>**实景截图。** 在无 GPU 机器上以 lieflat 视觉语言渲染的性能面板。</sub>

视觉规范详见 [docs/15](docs/15-视觉设计规范与色卡.md)，第三方组件许可详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

</details>

<details>
<summary><b>与 ANet 的关系</b></summary>

<br/>

ANet-Drone4D 是 Agent Network Research 旗下 ANet 生态的组成部分，与 [ANet](https://github.com/ANetResearch/ANet)、[ANetHub](https://github.com/ANetResearch/ANetHub) 及 [ANetCore](https://github.com/ANetResearch/ANetCore) 并列。无人机在此作为第一类物理智能体：发布自身能力、发现对等节点、委派任务并依据回执校验执行结果。ANet 作为协作面而非飞行控制面运行——委派的任务会被转换为标准控制指令，与人工操作员输入的指令一样接受同等的准入与安全检查。

</details>

<details>
<summary><b>数据与引用</b></summary>

<br/>

六座城市世界衍生自 UrbanScene3D，在公开演示站经数据集权利方授权使用。本仓库不分发原始数据集，详见 [NOTICE](NOTICE)。使用这些世界时请引用 UrbanScene3D（ECCV 2022）：

```bibtex
@inproceedings{UrbanScene3D,
  title     = {Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset},
  author    = {Liqiang Lin and Yilin Liu and Yue Hu and Xingguang Yan and Ke Xie and Hui Huang},
  booktitle = {ECCV},
  year      = {2022}
}
```

引用 ANet-Drone4D 请使用 GitHub 的 "Cite this repository" 功能（[CITATION.cff](CITATION.cff)）或以下 BibTeX 条目：

```bibtex
@software{anet_drone4d_2026,
  title   = {ANet-Drone4D: A Real-World Grounded 4D World Runtime for Autonomous Multi-Agent Drones},
  author  = {{Agent Network Research}},
  year    = {2026},
  version = {0.2.0},
  url     = {https://github.com/ANetResearch/ANet-Drone4D}
}
```

</details>

## 致谢

ANet-Drone4D 基于 [three.js](https://threejs.org)、[React Three Fiber](https://github.com/pmndrs/react-three-fiber)、[Potree](https://github.com/potree/potree)、[zenoh](https://zenoh.io)、[numba](https://numba.pydata.org)、[PX4](https://github.com/PX4/PX4-Autopilot)、[Prometheus](https://github.com/amov-lab/Prometheus) 与 UrbanScene3D 构建。

## 参与贡献

欢迎以英文或中文提交 Issue 与 Pull Request，指南详见 [CONTRIBUTING.md](CONTRIBUTING.md)。涉及系统契约（包括文件格式、`awr.rt.v1`、REST 接口或原因码）的变更须先创建 Issue 讨论，并以 `make ci` 作为合并门禁。安全漏洞请报告至 hi@anet0.com（[SECURITY.md](SECURITY.md)）。

## 许可证

本项目以 **ANet Open Source License (ANet-Drone4D)** 发布。该许可证基于 Apache License 2.0 修改，允许商业使用，但须满足两项条件：面向无关第三方运营多租户托管仿真或数字孪生服务须获得书面授权；必须保留所有 ANet 标识与版权声明。完整条款见 [LICENSE](LICENSE)。

<div align="center">
<br/>
<img src="docs/media/anet-avatar.png" alt="Agent Network Research" width="48" />
<br/>
<sub>Agent Network Research</sub>
</div>
