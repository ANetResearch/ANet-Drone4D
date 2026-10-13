<div align="center">

<img src="docs/media/drone4d-banner.jpg" alt="ANet-Drone4D：面向多智能体无人机的四维世界运行时" width="100%" />

<h3>从真实世界，到无人机集群。</h3>

真实世界驱动的多智能体无人机四维仿真平台：以城市级点云数字孪生为底座，机群仿真、集群控制与目标感知在同一仿真时钟下运行，
并在浏览器中实时呈现。服务端无需 GPU。

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

<sub>ANet 合成城市中的场景 S0，取自 V0.1 构建，在无 GPU 的机器上录制，以两倍实时速度播放。</sub>

## 要点

- **点云数字孪生。** 七个世界（ANet 合成城市与 UrbanScene3D 六座城市）以八叉树点云流式载入浏览器，点密度随视点与帧预算自动调节。
- **无人机集群仿真与集群控制。** FleetSim 在 250 Hz 共享时钟下推进 1 至 1,000 架飞行器，PX4 风格飞行模型、任务生成器、
  电子围栏与机间间距保护在同一仿真回路中执行。
- **目标、感知与任务。** 六类目标物，按 Johnson/TTPF 判据对可见光、近红外、长波红外、雷达与声阵列判定探测；
  机巢、贪心任务分配与在目标敏感范围外进行的 7×24 小时接力监视。
- **每位访客一个交互沙盒。** 每个会话运行独立的仿真核心，可在浏览器、手机与平板上操作，也可通过 REST v1 与 Python SDK 操作。
- **面向下一阶段。** D3 事件实验室引入 Gazebo Sim 锁步物理、网格相机与 LiDAR、FAST-LIVO2 重建对比，以及面向多智能体观测策略的事件级基准。

## 能力

| | 能力 | 状态 |
|---|---|---|
| 数字孪生 | 城市级点云、渐进加载、自动点密度 | 已交付 |
| 无人机集群 | FleetSim，同一时钟下最多 1,000 架 | 已交付 |
| PX4 | PX4 风格位置与姿态级联（PX4-lite），以 PX4 SIH 飞行记录校验 | 已交付 |
| 集群控制 | 编队、区域覆盖、电子围栏、间距保护、14 状态飞行状态机 | 已交付 |
| 目标感知 | 六类目标物，可见光、近红外、长波红外、雷达与声阵列 | 已交付 |
| 智能体集群 | 基于 Mock ANet 的能力发现与任务委派（公开站未启用） | 已交付 |
| Gazebo | Gazebo Sim 锁步，作为高保真物理档 | 开发中 |
| LiDAR 与相机 | 网格 LiDAR（Livox MID-360 扫描模式）与照片级网格相机 | 开发中 |
| 四维重建 | 以仿真数据驱动 FAST-LIVO2，并与网格真值对比 | 开发中 |
| Rerun | 录制数据导出到 Rerun 检视 | 开发中 |
| 3DGS | 叠加在网格几何之上的高斯泼溅外观层 | 研究 |
| PX4 SITL | 与 Gazebo 联合的飞控软件在环验证 | 研究 |

状态依据 [v0.2.0 发布](https://github.com/ANetResearch/ANet-Drone4D/releases/tag/v0.2.0)与
[D3 设计基线](docs/05-D3-设计增补与决策记录.md)。

## 开始使用

打开 **[在线演示](https://drone4d.agentnetwork.org.cn)**：无需注册，共享展示为只读，每位访客可开启一个独立沙盒。
在 Linux 上本地运行：

```sh
git clone https://github.com/ANetResearch/ANet-Drone4D.git && cd ANet-Drone4D
make setup        # 安装 Python 与 Node 依赖
make demo-world   # 生成 ANet 合成城市
make run          # 随后打开 http://127.0.0.1:8000/world/synthcity
```

[快速开始](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/intro/quickstart.html)以真实输出逐步说明每一步，
包括用 Python SDK 驱动一个沙盒会话。

## 实景

<table>
<tr>
<td width="50%"><img src="docs/media/landing-desktop.jpg" alt="ANet Drone4D 公开演示站首页" /></td>
<td width="50%"><img src="docs/media/d2-mobile.jpg" alt="两部手机上的共享展示与底部标签栏" /></td>
</tr>
<tr>
<td><sub>公开演示站首页。</sub></td>
<td><sub>手机布局，可切换窗口。</sub></td>
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
<td colspan="2"><sub>公开演示站上的 UrbanScene3D 六座城市，在浏览器中渲染。</sub></td>
</tr>
</table>

<details>
<summary><b>工作原理</b></summary>

<br/>

- **世界。** 世界是一个世界包：八叉树点云、地形模型、语义与派生缓存，统一在米制东北天坐标系中。浏览器以 HTTP 分段请求流式读取
  八叉树节点，并通过按帧预算分配的点池以单次绘制调用渲染。
- **仿真。** `sim-core` 在同一整数纳秒时钟下推进机队、环境（风、能见度、天气）、目标物、感知与任务编排；飞行模型移植 PX4 的
  位置与姿态级联，以 numba 内核执行。
- **实时协议。** `awr.rt.v1` 由 JSON 控制面与带信用背压的二进制批量层组成，慢速客户端不会拖住服务端。
- **沙盒。** `sandbox-pool` 在 cgroup 配额下为每个会话运行一个仿真核心，提供准入控制、可见排队与释放空闲会话的节能模式。
- **浏览器。** React 与命令式 three.js 引擎；所有渲染路径都在 WebGL2 规范最小上限下测试，以保证集成显卡与移动端 GPU 可用。

详见[架构](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/topics/architecture.html) ·
[世界与数据](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/topics/worlds.html) ·
[设计文档](docs/)。

</details>

<details>
<summary><b>开放接口与 Python SDK</b></summary>

<br/>

沙盒的全部操作均可经 `/api/sandbox/v1` 的 REST v1 完成，权限与界面一致。单文件 SDK
[`tools/sdk/awr_sandbox.py`](tools/sdk/awr_sandbox.py) 对其进行了封装：

```python
from awr_sandbox import Sandbox

sb = Sandbox("http://127.0.0.1:8002", timeout_s=30)
with sb.create("synthcity", "t5_area_guard") as s:
    v = s.vehicles.add("p600_mid360", nest_id="nest-b", label="docs-1")
    print(s.vehicles.state(v["id"])["state"]["flight_state"])
```

端点参考：[REST 接口](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/api/rest.html) ·
[Python SDK](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/api/sdk.html)。

</details>

<details>
<summary><b>版本路线</b></summary>

<br/>

| 版本 | 范围 | 状态 |
|---|---|---|
| V0.1 | 点云数字孪生、渐进加载、FleetSim 与 PX4-lite、实时协议、Mock ANet | 已发布 |
| V0.2.0 | 交互沙盒、目标物与感知量化、机巢与接力监视、开放接口、手机与平板 | 已发布 |
| V0.3 | D3 事件实验室：GLB 世界、事件磁带、Gazebo Sim 锁步、网格相机与 LiDAR、FAST-LIVO2、基准评测 | 开发中 |
| P3 与 V1.0 | 3DGS 外观层、PX4 SITL、自托管 ANetHub 的真实 ANet | 规划中 |

参见[版本路线](https://docs.agentnetwork.org.cn/drone4d/zh-cn/latest/project/roadmap.html)与
[D3 设计](docs/05-D3-设计增补与决策记录.md)。

</details>

<details>
<summary><b>V0.2.0 验收</b></summary>

<br/>

V0.2.0 按 55 项验收标准（D2-AC-01 至 D2-AC-55）验收。第 2 轮（2026-10-12 至 10-13）：

| 项目 | 通过 | 不通过 | 未测 |
|---|---|---|---|
| P0（50 项） | 41 | 9 | 0 |
| P1（5 项） | 3 | 0 | 2 |

24 小时浸泡测试按原口径完整运行，无进程重启，内存增长 0.48 MB/h。其余项作为已知问题列在
[发布说明](https://github.com/ANetResearch/ANet-Drone4D/releases/tag/v0.2.0)中；完整报告见
[docs/impl/D2-验收报告-第2轮.md](docs/impl/D2-验收报告-第2轮.md)。

</details>

<details>
<summary><b>技术栈</b></summary>

<br/>

服务端为 Python 3.12、numba、FastAPI 与 zenoh；浏览器端为 React 19、TypeScript 与 three.js；界面组件 shadcn/ui、图表 lieflat、
图标 morphicons；Potree 风格的八叉树容器；文档采用 Sphinx 与 Read the Docs 布局。服务端无需 GPU。第三方组件及其许可见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

</details>

<details>
<summary><b>与 ANet 的关系</b></summary>

<br/>

ANet-Drone4D 属于 Agent Network Research 的 ANet 系列，与 [ANet](https://github.com/ANetResearch/ANet)、
[ANetHub](https://github.com/ANetResearch/ANetHub) 和 [ANetCore](https://github.com/ANetResearch/ANetCore) 并列。
无人机是第一类物理智能体：它发布自身能力，其他智能体发现它并委派任务，结果以回执校验。ANet 是协作面而非控制面：
委派的任务转换为普通指令，与任何操作员的指令一样经过准入与安全检查。

</details>

<details>
<summary><b>数据与引用</b></summary>

<br/>

六座城市世界由 UrbanScene3D 构建，在公开演示站经数据集权利方授权使用。本仓库不包含该数据集，详见 [NOTICE](NOTICE)。
使用这些世界时请引用 UrbanScene3D（ECCV 2022）：

```bibtex
@inproceedings{UrbanScene3D,
  title     = {Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset},
  author    = {Liqiang Lin and Yilin Liu and Yue Hu and Xingguang Yan and Ke Xie and Hui Huang},
  booktitle = {ECCV},
  year      = {2022}
}
```

引用 ANet-Drone4D 可使用 GitHub 的 "Cite this repository"（[CITATION.cff](CITATION.cff)），或：

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

## 参与贡献

欢迎以中文或英文提交 Issue 与 Pull Request，参见 [CONTRIBUTING.md](CONTRIBUTING.md)。涉及契约的变更（文件格式、`awr.rt.v1`、
REST 接口、原因码）先以 Issue 讨论，`make ci` 为合并门槛。安全问题请发送至 hi@anet0.com（[SECURITY.md](SECURITY.md)）。

## 许可证

以 **ANet Open Source License（ANet-Drone4D）** 发布，这是在 Apache License 2.0 基础上修改的许可：允许商用，并增加两项条件——
面向无关的组织或个人运营多租户托管仿真或数字孪生服务需书面授权；Web 沙盒与 CLI 中的 ANet 标识与版权声明须保留。详见 [LICENSE](LICENSE)。

<div align="center">
<br/>
<img src="docs/media/anet-avatar.png" alt="Agent Network Research" width="48" />
<br/>
<sub>Agent Network Research</sub>
</div>
