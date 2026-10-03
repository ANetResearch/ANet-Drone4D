# n04 新项目发现：2025–2026 Web 天气 / 体积云 / 风场可视化、城市风场代理模型、数字孪生框架与实时遥测协议

> 研究单元：n04（Discovery）｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §12–13（WebGPU 定位）、§17–25（Environment Engine、E 场、风、雨、雾、云）、§21（风场 Web 可视化）、§28/§36–37（DroneState、WebSocket、频率）、§39（Timeline）、§46–47（V0.3/V0.4）、§50（V1.0）
>
> 方法：WebSearch 多轮中英文检索 → 对每个候选用 `curl` 抓 GitHub 页面取 star、用 `commits.atom` 取默认分支最近提交日期（脚本 `/data/projs/anet-drone/.cache/research/n04/meta.sh`，拿不到写“未知”）→ 挑最有价值的 8 个仓库 shallow clone 到 `refs/discovery/` 精读源码（takram 另 clone 了 `webgpu/clouds` 分支，共 9 个目录） → 另有 2 个小仓库 clone 到缓存目录只读 → 对两个关键结论在本机 headless Chromium（SwiftShader）上做了可行性实测（脚本在 `/data/projs/anet-drone/.cache/research/n04/bench/`）。
>
> 本单元与已有笔记的分工：r16 已把雨/雪/雾/沙尘/闪电/Eanpa-Sky/natural-disasters 讲透；r17 已把 WindNinja 质量守恒 Level 2 求解器、OpenFOAM 离线库、VDB 讲透；r27 已定下自研 `anet.rt.v1` WebSocket 主协议；r15 已把 Foxglove/Lichtblick 的回放算法讲透。本文**只补这些笔记没覆盖的新项目**：takram 大气/体积云、3D 风场粒子/流线/箭头的四种实现、浏览器 LBM 与 2026 年城市风场 ML 代理模型、MoQ/WebTransport/Rerun 等 2026 年遥测协议、Eclipse Ditto 等数字孪生框架，并与已有仓库对比给出替换/补充建议。
>
> 测试机负载很高（load average 13–20，8 核，同时有多个研究单元在跑浏览器基准），所有毫秒数**只可用于同页相对比较**。

---

## 0. 结论速览

| 仓库（stars / 最近提交，实测） | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **takram-design-engineering/three-geospatial**（1,697 stars / 2026-05-27；`webgpu/clouds` 分支 2026-04-10） | Three.js 地理空间渲染库。`@takram/three-atmosphere` 实现 Bruneton 预计算大气散射 + Hillaire 多重散射 LUT，**已有 TSL/WebGPU 入口**（代码里也有 WebGL2 后端分支，但本机实测 WebGL2 后端有着色器编译错误）；`@takram/three-clouds` 是 Nubis/Frostbite 系体积云（4 层天气通道、BSM 云影、1/16 时间上采样），**仍只有 GLSL + postprocessing 版本** | **adopt**：`three-atmosphere/webgpu` 作为 High 档天空、日照、空气透视（需锁 three r184，或等上游/自行 fork 修复 r186 兼容，简单补丁不够，见 §6.1）。**port**：`three-clouds` 的密度模型、步进策略、BSM、时间上采样整体改写为 TSL | V0.3 起 Low/Med 用自研或 SkyMesh；V0.5 High 档 adopt 大气；V0.5–V0.6 port 云 | 4/5 |
| **mapbox/webgl-wind**（1,108 stars / 2026-06-18） | 经典 GPGPU 风粒子：粒子位置编码进 RGBA8 纹理、drop rate、速度色带、**屏幕空间拖尾（上一帧×0.996 衰减）** | **port**：状态纹理编码、drop rate 公式、手工双线性采样。**skip**：屏幕空间拖尾（3D 相机一动就糊） | V0.3 | 3/5 |
| **NOC-OI/cesium-wind-layer**（0 stars / 2026-09-24；原仓 hongfaqiu/cesium-wind-layer 125 stars / 2026-04-26） | Cesium 风粒子层，fork 增加了**三维 velocity cube**（多层图集）、按层播种；拖尾用 **previous/current/next 三帧位置 + 每粒子 4 顶点展开的屏幕空间线段四边形** | **port**：三帧环形位置纹理、扁平四边形展开、按速度调节长度/宽度、RK2 | V0.3 | 4/5 |
| **Rawcloud/cesium-wind-arrows-3d**（0 stars / 2026-09-16，clone 在缓存目录） | WebGL2 3D 风箭头：`sampler3D` 存 (u,v,w)、RK2 平流锚点、**箭头俯仰角来自 w**、屏幕恒定像素尺寸、“视口∩数据”播种、视角突变强制重生 | **port**：箭头字形与播种策略 | V0.3 | 3/5 |
| **weatherlayers/weatherlayers-gl**（161 stars / 2026-09-28，clone 在缓存目录） | deck.gl 气象图层（粒子、栅格、等值线、风羽格网、高低压、锋面）+ 图例/时间轴控件；**粒子拖尾用“按年龄分块的环形缓冲”** | **reference**：年龄环形缓冲拖尾（→ WebGPU compute 环形索引）、图例/时间轴交互。**skip**：deck.gl 依赖 | V0.3 | 3/5 |
| **arch85-km/urban-wind-lab**（1 star / 2026-09-26） | 单 HTML 文件：Web Worker 里跑 **D3Q19 LBM + Smagorinsky LES + ABL 对数律入口**，WebGL2 transform feedback 流线示踪粒子 | **port**：LBM 求解器移植到服务端（numba），作为 Level 2.5“瞬态阵风/尾流涡脱落”；示踪粒子的 RK2、实体内杀死、底层加权播种 | V0.4–V0.6 | 3/5 |
| **rbischof/windinet**（12 stars / 2026-04-15；TUM-CFD-Video 镜像 2026-09-26） | 2026 年城市风场代理模型：LTX-Video 视频扩散 DiT 微调，输入建筑平面图 256² + 入口风速，**<1 s 输出 112 帧 2D 瞬态速度场**；物理约束 VAE 解码器 | **reference**：输入输出契约、物理损失（散度、壁面不穿透、距离加权 MSE）可直接作为我们任何风场的**验收指标**。**skip**：模型本身（需 CUDA，2D 行人层） | V1.0（可选） | 2/5 |
| **NVIDIA/physicsnemo**（3,297 stars / 2026-09-22）、**neuraloperator/neuraloperator**（3,918 stars / 2026-08-06）、**thuml/Transolver**（415 stars / 2026-02-26） | Physics-ML 训练框架 / 神经算子 / 任意几何 Transformer 求解器 | **reference**：V1.0 用我们自己的 WindNinja/OpenFOAM 风场库训练 3D 城市风场代理 | V1.0 | 2/5 |
| **rerun-io/rerun**（11,503 stars / 2026-09-27，0.39-dev） | 多模态机器人数据记录与可视化：Arrow 列式 chunk、实体路径 + 多时间轴、latest-at / range 查询、gRPC 流、Wasm 网页查看器 | **adopt（仅开发调试）**：`rerun-sdk` 做仿真服务的记录/调试旁路。**port**：实体路径命名、多时间轴、latest-at 语义、迟到客户端缓冲（内存上限、静态数据永不丢、newest_first）。**skip**：嵌进产品 UI（egui，违反 shadcn 约束） | V0.2 | 4/5 |
| **moq-dev/moq**（原 kixelated/moq，1,546 stars / 2026-09-28） | Media over QUIC：moq-lite 协议、Rust relay、TS/Python 客户端；**WebTransport 与 WebSocket 竞速连接**、每轨道 priority/order/max-age、JSON Snapshot（快照 + RFC 7396 合并补丁） | **port（V0.2）**：竞速连接、轨道 QoS 三旋钮、Snapshot/Stream 两种 JSON 轨道语义进 `anet.rt.v1`。**adopt（V1.0 可选）**：`moq-relay` 做 FPV 视频（WebCodecs）与多观众扇出 | V0.2 port；V1.0 adopt | 4/5 |
| **wtransport/pywebtransport**（42 stars / 2026-08-23） | 2026 年新的 Python WebTransport 栈（Rust 状态机 + asyncio） | **skip**：官方 KNOWN_ISSUES KI-003 写明**与当前 Chrome/Edge/Firefox 握手失败**（状态 Blocked） | — | 1/5 |
| **aiortc/aioquic**（2,006 stars / 2025-10-11）、**BiagioFesta/wtransport**（709 stars / 2026-09-22） | Python QUIC/HTTP3；Rust WebTransport | **reference**：若 V1.0 真要 WebTransport，走 Rust（wtransport 或 moq-relay）而不是 Python | V1.0 | 2/5 |
| **eclipse-ditto/ditto**（928 stars / 2026-09-25） | Eclipse IoT 数字孪生框架：Thing / Feature / Policy，`properties` 与 `desiredProperties` | **reference**：孪生状态模型（reported vs desired、definition 作能力类型）。**skip**：部署（Java + MongoDB + Pekko，过重） | V0.2 模型；V1.0 ANet 能力 | 3/5 |
| ertis-research/opentwins（276 stars）、iTwin/itwinjs-core（732 stars）、paramountric/digitaltwincityviewer（18 stars / 2025-01-31） | 组合式孪生平台（K8s）/ BIM 孪生 SDK / 城市孪生查看器 | **skip** | — | 1/5 |

**实现者先读这 10 条（每条都有源码或实测依据）：**

1. **takram 大气能挂到我们的 WebGPURenderer 上，但只适合真 GPU 的 WebGPU 档**：`packages/atmosphere/src/webgpu/AtmosphereLUTNode.ts` 的 `setup()` 用 `isWebGPU(builder)` 选择 `AtmosphereLUTTexturesWebGPU`（storage texture）或 `AtmosphereLUTTexturesWebGL`（render target 逐层渲染）。**但 npm 版 0.19.1 与 three r186 不兼容**：本机实测 r186 下模块求值即崩溃（`struct(...).layout` 为 undefined，TSL `struct()` 在 r186 改成了 Proxy），换 three r184（takram 仓库 `package.json` 锁定的版本）后 WebGPU 后端可以正常出图。见 §6.1 的三种处置方案。本机 SwiftShader 实测：WebGPU 后端 LUT 生成 12–29 s，`skyBackground` 使每帧从 268 ms 升到 1,111 ms、`aerialPerspective` 升到 1,424 ms；WebGL2 后端出现 `AtmosphereParameters` 着色器编译错误且 LUT 反复重建（P90 21 s）。所以它**只进 High 档（真 GPU + WebGPU）**，Potato/Low 档用预烘焙天空全景或 `SkyMesh`（§3.1.2）。
2. **three-clouds 没有 WebGPU 版可用**：`main` 上 clouds 只有 GLSL（`packages/clouds/src/shaders/clouds.frag`，1003 行）并依赖 `postprocessing` 的 EffectComposer；`webgpu/clouds` 分支到 2026-04-10 只完成了噪声纹理节点（`packages/clouds/src/webgpu/{CloudShapeNode,LocalWeatherNode,TurbulenceNode}.ts`），**没有 raymarcher**。所以云必须自己用 TSL 写，算法按 §3.2 移植（它是目前 Web 上最完整的 Nubis/Frostbite 实现）。
3. **3D 风场可视化不能用 webgl-wind 的“屏幕空间拖尾”**：`webgl-wind/src/index.js` 的 `drawScreen()` 每帧把上一帧画面 ×`fadeOpacity=0.996` 再叠新粒子，这只在相机静止的 2D 地图上成立；3D 沙盘里相机一动，拖尾就变成一团糊。3D 必须用**几何拖尾**：cesium-wind-layer 的“三帧位置 → 屏幕空间线段四边形”，或 WeatherLayers 的“按年龄分块的环形缓冲”。
4. **软件渲染 / WebGL2 档首选“无状态流线虚线”**：服务端沿风场积分流线（r17 已有 RK4），每个顶点带**飞行时间 τ**；前端只画静态四边形带，片元里 `phase = fract((τ − t)/T)` 生成沿流线以当地风速移动的“彗星”。无 compute、无状态、Timeline 可任意 seek。本机 SwiftShader 实测：500 条×32 段（1.6 万段）只增加约 32 ms/帧，1000×64 段增加约 110 ms/帧，服务端生成 1000 条流线约 0.14 s。
5. **有状态粒子（真正随风场积分）不进软件档**：Med 档用两张 float RenderTarget 乒乓（WebGL2/WebGPU 两后端通用，不依赖 compute），High 档用 WebGPU compute 更新 (pos, age, prevPos)；RK2 中点法、`dropRate + speedNorm·dropRateBump` 概率重生、进入建筑体素即杀死、底层加权播种 `z = pow(r, 1.9)·H`（urban-wind-lab）；拖尾用 K=8 的历史环形索引（WeatherLayers 思路，用环形索引替代整块 copy）。
6. **城市风场 ML 代理在 2026 年仍以 2D 行人高度层为主，且都要 CUDA**（WinDiNet 推理要 GPU，训练建议 48 GB 显存；UrbanTALES 系列模型同样是行人层）。对无人机 0–150 m 的 3D 飞行空间，**主路线仍是 r17 的“离线风场库 + 运行时插值”**；ML 代理放到 V1.0，用我们自己的库训练 3D 算子（FNO/Transolver）。WinDiNet 的**散度损失与壁面不穿透损失**应立即拿来当 Level 1/2 风场的自动验收指标（§3.5）。
7. **想要“瞬态阵风、建筑尾流涡脱落”，质量守恒法给不了**（它只有稳态散度自由场）。urban-wind-lab 证明 D3Q19 LBM + Smagorinsky 在 88×88×33 网格上用纯 CPU（浏览器 Web Worker）就能连续求解；本机 numba 移植实测 3.8–5.8 MLUPS（8 线程、有其他负载）；建议移植到服务端（numba/NumPy），作为 Level 2.5：离线跑出 60–120 s 的瞬态序列，用于湍流强度统计和 Timeline 回放（§3.4）。
8. **WebTransport 在 2026 年浏览器侧已普及，但 Python 服务端没有可用实现**：pywebtransport 自己在 `KNOWN_ISSUES.md` 标注 KI-003“与浏览器握手失败（Blocked）”，aioquic 最近提交停在 2025-10-11。MVP 继续用 r27 定下的 WebSocket `anet.rt.v1`；但**传输层接口按 moq 的“WebTransport/WebSocket 竞速”设计**，V1.0 可以在前面挂 Rust 的 moq-relay 或 wtransport，而不改业务协议。
9. **moq-lite 的三个订阅旋钮正好是我们缺的遥测 QoS 语义**：`priority`（0–255）、`order`（新到旧 / 旧到新）、`maxAge`（非最新 group 超过多久就跳过）；再加 `@moq/json` 的 Snapshot 规则（group 首帧是完整快照、后续是合并补丁，补丁累计超过 `deltaRatio×快照大小` 就开新 group，默认 8）——迟到的订阅者只读最新 group 就能重建状态。建议直接写进 `anet.rt.v1` 的 channel 定义（§3.6）。
10. **Rerun 适合当“研发记录仪”，不适合当产品 UI**：Python 端 `rr.set_time("sim_time", timestamp=…)` + `rr.log("world/uav/01", …)`，`rr.serve_grpc(server_memory_limit="1GiB", newest_first=…)` 让迟到的查看器补齐历史（超过上限丢最旧的，静态数据永不丢）。这正是我们 Gateway 迟到客户端缓冲该有的语义；Rerun 查看器本身是 egui/wgpu，只放在开发者工具里。

---

## 1. 仓库概览

### 1.1 精读仓库（已 clone）

| 仓库 | 快照 | stars / 最近提交（实测） | 许可 | 语言 / 规模 | 本地路径 |
|---|---|---|---|---|---|
| takram-design-engineering/three-geospatial（main） | `b012ad0` 2026-05-27 | 1,697 / 2026-05-27 | MIT | TS + GLSL/TSL，nx monorepo，4 个包：`atmosphere` 0.19.1、`clouds` 0.7.6、`core`（`@takram/three-geospatial` 0.9.1）、`effects` 0.6.4 | `refs/discovery/three-geospatial` |
| 同上（`webgpu/clouds` 分支） | `88f7d0e` 2026-04-10 | — | MIT | 只多了 `packages/clouds/src/webgpu/` 下 9 个噪声/天气纹理节点 | `refs/discovery/three-geospatial-webgpu-clouds` |
| mapbox/webgl-wind | `b1f6468` 2026-06-18 | 1,108 / 2026-06-18（仅加 CODEOWNERS，算法 2017 年定型） | ISC | JS + GLSL，410 行 | `refs/discovery/webgl-wind` |
| NOC-OI/cesium-wind-layer（hongfaqiu 原仓的 fork） | `c219802` 2026-09-24 | 0 / 2026-09-24；原仓 125 / 2026-04-26 | MIT | TS + GLSL，核心 2,311 行 | `refs/discovery/cesium-wind-layer` |
| arch85-km/urban-wind-lab | `cad0241` 2026-09-26（v1.0.0，2026-09-15 发布） | 1 / 2026-09-26 | MIT | 单文件 HTML 5,067 行（WebGL2 + Web Worker） | `refs/discovery/urban-wind-lab` |
| rbischof/windinet | `cdc60ac` 2026-04-15 | 12 / 2026-04-15 | Apache-2.0 | Python/PyTorch，基于 LTX-Video 2B | `refs/discovery/windinet` |
| moq-dev/moq | `2b689c2` 2026-09-28 | 1,546 / 2026-09-28 | MIT/Apache-2.0 | Rust（`rs/` 40+ crate）+ TS（`js/`）+ Python（`py/`）+ Go/Swift/Kotlin/C++ 绑定 | `refs/discovery/moq` |
| rerun-io/rerun | `8f6c4ed` 2026-09-27（0.39.0-alpha） | 11,503 / 2026-09-27 | MIT/Apache-2.0 | Rust + Python + JS；`--filter=blob:limit=2m` 浅克隆 85 MB | `refs/discovery/rerun` |
| wtransport/pywebtransport | `86663e9` 2026-08-23 | 42 / 2026-08-23 | Apache-2.0 | Python + Rust（maturin），首发 2026-02-07 | `refs/discovery/pywebtransport` |
| weatherlayers/weatherlayers-gl | `785bf77` 2026-09-28 | 161 / 2026-09-28 | MPL-2.0 或商业条款（双许可） | TS + GLSL（deck.gl/luma.gl） | `.cache/research/n04/wl`（只读参考） |
| Rawcloud/cesium-wind-arrows-3d | `ee94829` 2026-09-16 | 0 / 2026-09-16 | MIT | TS + GLSL，2,352 行 | `.cache/research/n04/arrows`（只读参考） |

### 1.2 只做了元数据评估的候选（未 clone）

| 方向 | 仓库 | stars / 最近提交（实测） | 评估结论 |
|---|---|---|---|
| 体积云 | FarazzShaikh/three-volumetric-clouds | 124 / 2024-09-05 | 旧版 WebGL Nubis 复现，作者 2025 年另起 `Faraz-Portfolio/demo-2025-raymarch-clouds`（7 / 2026-06-28），都是 demo 级。takram 更完整，skip |
| 体积云 | CK42BB/procedural-clouds-threejs | 47 / 2026-02-12 | 和 r16 的 `procedural-weather-threejs` 同作者，同样是 Claude skill 文档形式，skip |
| 体积云 | joshbrew/webgpu_realtime_clouds、nff747/volumetric-clouds-wgsl | 3 / 2026-09-05；1 / 2026-09-21 | 原生 WebGPU compute raymarch，2026 新项目但 star 极少、不基于 three，reference |
| 雨/场景 | ektogamat/threejs-conference | 100 / 2026-09-23 | three WebGPU + TSL 的赛博雨夜巷：“尊重屋顶的 GPU 雨”、湿地面、TSL 后处理。与 r16 的 Eanpa-Sky 雨向深度场同思路，reference |
| 风粒子 | RaymanNg/3D-Wind-Field | 498 / 2025-06-18 | Cesium 官方博客那套 3D 风粒子（NetCDF lon/lat/lev），算法被 cesium-wind-layer 覆盖，skip |
| 风粒子 | sakitam-fdd/wind-layer | 709 / 2026-03-28 | OpenLayers/maptalks/高德/百度 2D 风场插件，国内常用，2D，skip |
| 风粒子 | onaci/leaflet-velocity、cambecc/earth | 665 / 2023-03-15；6,608 / 2016-09-03 | 2D 经典，已停更，skip |
| 风粒子 | weatherlayers/deck.gl-particle | 96 / 2025-03-06 | 已并入 weatherlayers-gl，skip |
| 风粒子 | Deltares/webgl-streamline-visualizer | 2 / 2026-09-15 | 2D 流线粒子，MapLibre，skip |
| 风粒子 | anvaka/wind-lines | 65 / 2026-04-18 | 保留完整轨迹的 2D 流线动画，思路与 §3.3.1“无状态流线虚线”相近，reference |
| 浏览器流体 | matsuoka-601/WebGPU-Ocean、dgreenheck/threejs-particle-fluids | 557 / 2025-06-09；7 / 2026-09-28 | 粒子流体（SPH/PBF/MLS-MPM）效果演示，与风场无关，skip |
| 城市风 ML | UMBE-LAB/FLUME、Zyzrepositories/Geo_MSO_UrbanTALES_Buildings | 0 / 2026-09-16；0 / 2026-09-15 | 2026 年论文代码，行人层 2D，reference（数据集 UrbanTALES：538 个城市布局的 PALM LES） |
| 城市风 ML | ooichinchun/FastFlow、nasa/wind-generative-modeling、ml-jku/wind | 0 / 2022-09-09；9 / 2025-03-11；18 / 2026-07-31 | skip |
| 城市风 ML | specklesystems/speckle_automate-urban-wind-simulation | 16 / 2024-12-16 | Speckle 自动化里跑城市风 CFD，skip |
| Physics-ML | NVIDIA/physicsnemo、NVIDIA/physicsnemo-cfd、neuraloperator/neuraloperator、thuml/Transolver | 3,297 / 2026-09-22；153 / 2026-08-18；3,918 / 2026-08-06；415 / 2026-02-26 | V1.0 训练框架候选，reference |
| 遥测 | foxglove/foxglove-sdk、Lichtblick-Suite/lichtblick | 311 / 2026-09-24；1,141 / 2026-09-25 | r27/r15 已覆盖，本文不重复 |
| 遥测 | aiortc/aioquic、BiagioFesta/wtransport | 2,006 / 2025-10-11；709 / 2026-09-22 | 见 §2.8 |
| 孪生框架 | eclipse-ditto/ditto、ertis-research/opentwins、iTwin/itwinjs-core、paramountric/digitaltwincityviewer | 928 / 2026-09-25；276 / 2026-09-10；732 / 2026-09-26；18 / 2025-01-31 | 见 §2.9 |

> 已有同类仓库（`refs/weather/*`、`refs/web3d/three.js`、`refs/backend/ws-protocol`、`refs/backend/zenoh`、`refs/weather/windninja` 等）不在本表重复，对比见 §5。

---

## 2. 源码结构与关键模块

### 2.1 takram three-geospatial（大气 + 体积云）

**包结构**（`packages/`，nx monorepo，pnpm）：

| 包 | 关键文件 | 作用 |
|---|---|---|
| `core`（`@takram/three-geospatial`） | `src/Ellipsoid.ts`（`getNorthUpEastFrame`、`getEastNorthUpFrame`）、`src/Geodetic.ts`（`toECEF`）、`src/webgpu/{TemporalAntialiasNode,HighpVelocityNode,DualMipmapFilterNode,LensFlareNode,CascadedShadowMapsNode,StorageTexture3DNode}.ts` | WGS84 数学；TSL 通用节点：TAA、高精度速度缓冲、镜头光晕、CSM、3D storage texture 输出 |
| `atmosphere` | WebGL：`src/{SkyMaterial,AerialPerspectiveEffect,PrecomputedTexturesGenerator,SunDirectionalLight,SkyLightProbe}.ts` + `src/shaders/bruneton/`；**WebGPU/TSL**：`src/webgpu/{AtmosphereContext,AtmosphereLUTNode,AtmosphereLUTTexturesWebGPU,AtmosphereLUTTexturesWebGL,SkyNode,AerialPerspectiveNode,AtmosphereLight,AtmosphereLightNode,ShadowLengthNode,precompute,multiscattering,runtime}.ts`（合计 6,739 行） | Bruneton 4D 散射 LUT + Hillaire 多重散射 LUT；天空、日月星、空气透视后处理、物理日照/天空光、光柱（epipolar 采样 + CSM） |
| `clouds` | `src/{CloudsEffect,CloudsPass,CloudsMaterial,CloudLayers,CloudLayer,DensityProfile,ShadowPass,qualityPresets,bayer}.ts`，`src/shaders/{clouds.frag,clouds.glsl,shadow.frag,cloudsResolve.frag,varianceClipping.glsl,catmullRomSampling.glsl,structuredSampling.glsl}` | Nubis/Frostbite 体积云（**仅 GLSL + postprocessing**） |
| `effects` | `src/{LensFlareEffect,DitheringEffect,GeometryEffect}.ts` | WebGL 后处理小件 |

**大气 WebGPU 入口的关键机制**：

- `AtmosphereContext`（`src/webgpu/AtmosphereContext.ts`）：一组 uniform——`matrixWorldToECEF`、`matrixECIToECEF`、`sunDirectionECEF`、`moonDirectionECEF`、`scatteringSampleCount=(4,14)`，以及开关 `correctAltitude / constrainCamera / showGround / accurateShadowScattering / raymarchScattering`（默认全 true）。通过 `renderer.contextNode = context({...,getAtmosphere: () => ctx})` 注入，所有大气节点从上下文取参数。
- `AtmosphereLUTNode.setup()`（L191–223）：按后端选择 LUT 生成器；`isFloatLinearSupported(renderer)` 为假时自动降为 `HalfFloatType`。LUT 生成是异步的（`updateTextures(renderer)`，注释里自己写着 “TODO: Race condition”）。
- `AerialPerspectiveNode`：输入 color + depth，输出含空气透视的颜色，depth==1 的像素直接画天空。字段 `correctGeometricError / lighting / transmittance / inscattering / moonScattering`。
- `AtmosphereLight` + `AtmosphereLightNode`：替代 `DirectionalLight + SkyLightProbe`，让 three 内置 PBR 材质拿到物理正确的太阳直射与天空漫射。必须 `renderer.library.addLight(AtmosphereLightNode, AtmosphereLight)` 注册。
- 非地理场景用法（`storybook-webgpu/src/atmosphere/NonGeospatial-Story.tsx`）：用 `Ellipsoid.WGS84.getNorthUpEastFrame(positionECEF, ctx.matrixWorldToECEF.value)` 把局部场景“钉”到地球某点，场景坐标系约定为 **x: north, y: up, z: east**。

**体积云 GLSL 主循环**（`packages/clouds/src/shaders/clouds.frag`）：`main()` → `getIntersections/getRayNearFar`（与云层壳求交）→ `marchClouds()`（L472–620，主步进）→ 内部调 `sampleWeather()`、`sampleMedia()`（`clouds.glsl`）、`marchOpticalDepth()`（向太阳次级步进）、`sampleShadowOpticalDepth()`（读 BSM）、`approximateMultipleScattering()`（多八度近似）→ 输出 (radiance, transmittance) 与 `frontDepth`（供空气透视）。`CloudsPass.setSize()`（L203–223）在 `temporalUpscale` 时以 1/4 分辨率渲染，`cloudsResolve.frag` 用 4×4 Bayer 序列 + 方差裁剪做时域重建。

### 2.2 mapbox/webgl-wind

- `src/index.js` 的 `WindGL`：`numParticles` setter 建 `ceil(sqrt(N))²` 的 RGBA8 状态纹理两张（乒乓）；`draw()` = `drawScreen()`（屏幕纹理乒乓 + 淡出）+ `updateParticles()`（全屏四边形跑 `update.frag.glsl`）。
- `src/shaders/update.frag.glsl`：位置用 **RG=低 8 位、BA=高 8 位** 编码成 16 位定点 `pos = rg/255 + ba`；`lookup_wind()` 手工 4 点双线性（比硬件过滤平滑）；`drop_rate = u_drop_rate + speed_t·u_drop_rate_bump`；随机重生。
- `src/shaders/draw.vert.glsl` 用 `a_index` 从状态纹理取位置画 1px 点；`draw.frag.glsl` 按速度查 16×16 色带纹理。
- 默认参数：`fadeOpacity=0.996`、`speedFactor=0.25`、`dropRate=0.003`、`dropRateBump=0.01`，风场纹理是 u/v 归一化到 [min,max] 的 RGBA8 PNG。

### 2.3 NOC-OI/cesium-wind-layer（3D velocity cube）

- `src/windParticlesComputing.ts`：三张位置纹理轮换 `previousParticlesPosition → currentParticlesPosition → postProcessingPosition`（L206–209），外加一张速度纹理；每帧三个 compute 通道：`calculateSpeed`（RK2）、`updatePosition`、`postProcessingPosition`（重生/越界）。
- `src/shaders/calculateSpeed.ts`：多层风场打包成**2D 图集**（`atlasGrid`，每层一块），`mapPositionToAtlasUV()` 取最近层；`calculateSpeedByRungeKutta2()` 用 h=0.5 的中点法；`lengthOfLonLat()` 是 WGS84 经纬度每度米数的级数展开。
- `src/shaders/postProcessingPosition.ts`：`generateRandomParticle()` **按粒子序号确定性分配到各高度层**（`mod(particleOrdinal, availableLevels)`），保证每层都有粒子；越界或 `rand < dropRate + dropRateBump·speedNorm` 时重生，alpha 通道标记“本帧刚重生”。
- `src/shaders/segmentDraw.ts` + `windParticlesRendering.ts#createSegmentsGeometry()`：**每粒子 4 个顶点（非实例化）**，`normal=(pointToUse, offsetSign)`；顶点着色器把 previous/current/next 三个位置投影到裁剪空间，在屏幕空间求法线，按速度在 `lineWidth.{min,max}` 与 `lineLength.{min,max}`（像素）之间插值；片元按 `v_segmentPosition` 做 `smoothstep → pow 1.5` 的头尾渐隐，并与地球深度比较做遮挡。任一帧刚重生的粒子退化成零长度，避免“跨屏拉线”。
- 默认值（`src/index.ts`）：`particlesTextureSize=100`（1 万粒子）、`dropRate=0.003`、`dropRateBump=0.01`、`lineWidth 1–2 px`、`lineLength 20–100 px`；多层时有效纹理尺寸 `ceil(base·sqrt(activeLevels))`。

### 2.4 Rawcloud/cesium-wind-arrows-3d（缓存目录只读）

- `src/shaders/computing.ts` 的 `anchorComputeShader`：状态纹理每 texel 一个箭头 `(lon, lat, zNorm, life)`；`u_wind` 是 **`sampler3D`，RGB=(u,v,w)**，硬件三线性；RK2 中点法把 m/s 换成 (deg/s, deg/s, zNorm/s)；`expired || outside || reseed` 时在**播种范围 = 视口 ∩ 数据**内重生；`u_reseed` 在视角突变时让全体一帧内重生（消除缩放后的密集带）；`u_zLock` 锁定某一高度层。
- `src/shaders/skinning.ts`：顶点着色器按锚点风向组装箭头网格，**w 分量给出俯仰角**，尺寸按相机距离换算成屏幕恒定像素。

### 2.5 weatherlayers-gl 粒子层（缓存目录只读）

- `src/deck/layers/particle-layer/particle-line-layer.ts#_setupTransformFeedback()`：位置缓冲按“年龄块”排布 `|age0 的 N 个位置|age1|…|age(maxAge−1)|`；每步先把 age0..age(maxAge−2) **整体拷贝后移一块**，再用 transform feedback 只更新 age0；绘制时 age k 与 age k+1 连成线段，透明度 `1 − k/maxAge`。默认 `numParticles=5000`、`maxAge=10`、`speedFactor=1`。
- `particle-line-layer-update.vs.glsl`：`randPointToPosition()` 在视口内均匀播种（地球视图下按 `sqrt` 半径均匀分布）；越界设 `DROP_POSITION_Z=-1` 隐藏。
- `src/deck/controls/{legend-control,timeline-control,tooltip-control}`：图例、时间轴、悬停读数三件套，是天气 UI 的完整参考（视觉上我们用 lieflat/shadcn 重做）。

### 2.6 urban-wind-lab（浏览器 LBM）

单文件 `urban-wind-lab.html`，注释分节：`1` 数学/相机、`2` WebGL 渲染（`2b` GPU 流线示踪粒子，L1698 起）、`3` 几何与体素化（L1895 起）、`4` **D3Q19 LBM 求解器（Web Worker，主线程回退）**、`5` 分析（Lawson 舒适度、探针、统计）。

- 求解器常量（L2260–2275）：D3Q19 的 `EX/EY/EZ/OPP/W`；`UREF_LAT=0.045`（10 m 参考高度的格子速度）、`TAU0=0.51`、`CS2=0.0196`（Smagorinsky C=0.14）、`ZREF=10`。
- 入口：`profile(k) = ln((z+z0)/z0) / ln((ZREF+z0)/z0)`，四档地表粗糙度 z0；来风面自动识别（“inlet 施加在所有迎风面上”）。
- 碰撞（L2380–2395）：BGK + 由非平衡矩 `Π` 求局部涡黏的 Smagorinsky（Hou et al.）`τ = ½(τ0 + sqrt(τ0² + 18√2·C²·|Π|/ρ))`（代码中 `25.4558441 = 18√2`），并 `τ ≤ 1.6` 截断；流动：**半程反弹**（half-way bounce-back）处理建筑与地面。
- 网格预设（L1901）：`fast 64×64×24`、`balanced 88×88×33`、`detailed 112×112×42`，场地 240 m × 240 m × 90 m（balanced 时 dx≈2.7 m）。
- 示踪粒子（L1704–1760）：每粒子 8 float `(pos.xyz, age, prev.xyz, speed)`，transform feedback 更新；RK2 中点；出界、进实体、或 `speed < 0.02·Uref` 且活过 1/4 寿命即重生；播种 `z = pow(r, 1.9)·0.86·H`（偏向低层，建筑所在处）。
- 作者在 README 里明确“未经验证、只作教学”，本项目只借算法，不借结论。

### 2.7 WinDiNet（2026 城市风场视频扩散代理）

- 入口 `scripts/inference.py` + `configs/inference.yaml`：`model_source: LTXV_2B_0.9.6_DEV`、`num_inference_steps: 2`（蒸馏）、`num_frames: 113`（1 帧条件 + 112 帧输出）、速度编码 `rgb = (v/mag_cap + 1)/2`，`mag_cap_mps=30`；标量条件 `inlet_speed_mps ∈ [0.1, 20]`、`field_size_m ∈ [900, 1400]`。
- 输入：建筑平面图 PNG（黑=建筑）+ JSON；输出 `.npz`：`u_fields, v_fields [T,H,W] float16`、`bldg_mask`。**只有 2D 水平切片**。
- `windinet/training/losses.py`：`distance_weighted_mse`（离建筑越近权重越大，`alpha=2, sigma=20 px`）、`divergence_loss`（∂u/∂x+∂v/∂y）、`wall_no_penetration_loss`（壁面带内法向速度为零），物理项只在 `warmup_frames=56` 之后施加。
- `inverse/`：可微代理做建筑布局逆设计（与本项目无关）。

### 2.8 实时遥测：moq、pywebtransport、Rerun

**moq-dev/moq**：

- `js/net/src/connection/connect.ts#connectInner()`：WebTransport 与 WebSocket **竞速**——WebTransport 先跑，WebSocket 延迟 `DEFAULT_WEBSOCKET_DELAY_MS=500` 再起跑（“QUIC 少半个 RTT，应该更快”）；`Promise.any` 取先成功者；若 WebSocket 赢过一次，把 URL 记进 `websocketWon`，下次不再给 QUIC 让步（headstart=0）。`serverCertificateHashes` 支持自签证书（本地开发用）。
- `doc/concept/moq-lite.md`：Session / Origin / Broadcast / Track / Group / Frame / Datagram 七个概念；订阅三旋钮 **Priority（0–255）/ Order（新→旧或旧→新）/ Max age（非最新 group 允许的最大年龄，按媒体时间轴计）**；group 必须从“可独立解码点”（关键帧或完整 JSON 快照）开始；Datagram 单帧 <≈1200 B、不重传，适合传感器数据。
- `js/json/README.md`（Rust 对应 `rs/moq-json`）：`Snapshot`（有损，只保最新值；group 首帧完整快照 + RFC 7396 merge-patch 增量；增量累计超过 `deltaRatio×快照大小` 开新 group，默认 8，设 0 则每次都发完整快照）与 `Stream`（无损，有序追加日志）。可选 group 级 DEFLATE。
- `py/moq-rs`：Python 客户端/服务端（UniFFI 包 Rust），Alpha；`rs/moq-relay`：Rust 中继。

**wtransport/pywebtransport**：Python API 很干净（`ServerApp`、`@app.route`、`session.send_datagram`、双向/单向流、`memoryview` 零拷贝），但 `KNOWN_ISSUES.md` 的 **KI-003（2025-12-15 发现，状态 Blocked）**：与当前稳定版 Chrome/Edge/Firefox 握手后立即断开（`ERR_QUIC_PROTOCOL_ERROR`，库跟的是最新 IETF 草案、浏览器还停在旧草案）；KI-004：底层 quinn-proto 不支持 `RESET_STREAM_AT`。**结论：2026-09 时点 Python 侧没有能和浏览器互通的 WebTransport 服务器**。

**rerun-io/rerun**（`ARCHITECTURE.md`、`crates/store/re_chunk*`、`rerun_py/rerun_sdk/rerun/`）：

- 数据模型：日志 = Apache Arrow 列式 `Chunk`；`re_chunk_store` 是按 **实体路径 × 组件 × 时间轴 × 时间** 索引的内存时序库，支持乱序插入，`O(log N)` 查询。
- 查询：`LatestAtQuery { timeline, at }`（`re_chunk/src/latest_at.rs`）与 `RangeQuery { timeline, range, options }`（`range.rs`）。一个实体可同时挂多个时间轴（如 `sim_time`、`frame`、`wall_time`）。
- Python：`rr.set_time(timeline, sequence=|duration=|timestamp=)`、`rr.log(path, archetype)`、`rr.send_columns()`（列式批量）、`rr.save()`（.rrd）、`rr.serve_grpc(grpc_port, server_memory_limit="1GiB", newest_first=False, cors_allow_origin)`（`sinks.py` L331）、`rr.serve_web_viewer()`（`web.py`）。
- 网页查看器 `rerun_js/web-viewer`：`new WebViewer().start(rrdOrGrpcUrl, parent, {width,height})`，Wasm + wgpu（WebGPU 优先、WebGL 回退）+ egui。版本号必须与 SDK 一致（最新 npm 0.35.x；只兼容上一个 minor 的 .rrd）。

### 2.9 数字孪生框架

- **Eclipse Ditto**：Thing（孪生）→ Features（功能面）→ 每个 Feature 有 `properties`（当前状态，即 reported）、`desiredProperties`（目标状态）、`definition`（`namespace:name:version` 或 WoT 模型 URL，作类型注解，Ditto 不做校验）；Policy 管权限。部署是 Java 微服务 + MongoDB，过重，只借模型。
- **OpenTwins**：以 Ditto 为核心、叠加 Kafka/时序库/Grafana/3D 与 FMI/ML 的“组合式孪生”，Helm/K8s 部署，README 自称“开发中、不建议生产”。skip。
- **iTwin.js**：Bentley 的 BIM/基础设施孪生 SDK（iModel），体系庞大且面向 BIM。skip。

---

## 3. 可复用算法与实现（含伪代码 / 参数）

### 3.1 天空与大气：分档方案与 takram 集成配方

#### 3.1.1 本机实测（`bench/src/atmo.js`，three r184 + `@takram/three-atmosphere@0.19.1`）

场景：640×360，10 万点（1px）+ 地面 + 40 个盒子建筑，SwiftShader，测 40 帧 rAF 间隔。

| 后端 | base（普通背景 + 平行光） | sky（`skyBackground()` + `AtmosphereLight`） | ap（`aerialPerspective(color, depth)` 后处理，含天空） | LUT 就绪时间 |
|---|---|---|---|---|
| WebGL2（ANGLE/SwiftShader） | 中位 201 ms / P90 388 ms | 中位 839 ms / **P90 21,254 ms**；控制台 `Shader Error … ERROR: 0:76: 'AtmosphereParameters' : syntax error`，LUT 的 `update` 事件在 13 s、41 s、100 s 反复触发，期间整帧卡死 | 未测（sky 已不稳定） | 13.2 s（首次） |
| WebGPU（SwiftShader fallback adapter） | 中位 268 ms / P90 650 ms | 中位 1,111 ms / P90 2,303 ms，无报错 | 中位 1,424 ms / P90 2,140 ms，无报错 | 29.2 s（sky 用例）/ 12.4 s（ap 用例） |

另外两个事实：

- **three r186 下直接崩溃**：`@takram/three-atmosphere@0.19.1` 的 `webgpu.js` 在模块顶层执行 `struct({...}).layout.name`，r186 的 TSL `struct()` 返回 `nodeProxyConstructor` 代理，`layout` 不再存在 → `TypeError: Cannot read properties of undefined (reading 'name')`。takram 仓库自身锁 `three@0.184.0`（根 `package.json` L98）。
- 软件渲染下 sky 比 base 贵约 4 倍，ap 约 5 倍；LUT 生成要 12–30 s。**结论：takram 大气只能放在“真 GPU + WebGPU”的 High 档**；软件档/WebGL2 档必须用别的天空方案。

#### 3.1.2 天空分档（与 r11/r16 的 QualityController 档位对齐）

| 档 | 天空 | 日照 | 空气透视 / 雾 | 云 | 成本（估算） |
|---|---|---|---|---|---|
| Potato（软件渲染） | **预烘焙天空全景**：离线用 takram 按 {太阳高度角 × 能见度} 渲 12–24 张 256×128 RGBE 等距柱面图，运行时两张插值当 `scene.backgroundNode` | 平行光颜色/强度查同一张表（1D LUT） | 指数高度雾（three `fog` 节点，r16 已定） | 2D 云层贴图（r16） | ≈ 一次全屏纹理采样 |
| Low（WebGL2 集显） | three `SkyMesh`（Preetham，TSL 版，`examples/jsm/objects/SkyMesh.js`） | 同上 | 高度雾 + 距离雾 | 2D 云 / 体积云 Low（§3.2 的 1/4 分辨率、32 步） | 0.3–0.5 ms（估算） |
| Med（WebGPU 或强 WebGL2） | SkyMesh 或 takram `skyEnvironment()` 只在太阳角变化 > 0.5° 时渲一次立方体贴图 | `AtmosphereLight` | 高度雾 | 体积云 Med（§3.2） | 立方体贴图更新时一次性开销 |
| High（真 GPU WebGPU） | takram `skyBackground()` / `aerialPerspective()` | `AtmosphereLight` | takram 空气透视（`raymarchScattering=true`） | 体积云 High（BSM + 光柱） | 1–3 ms（takram 自述在 WebGPU 上“性能好很多”，本机无法测） |

#### 3.1.3 takram 集成配方（适配本项目坐标约定）

r15 定下 **渲染坐标 = World ENU，Z-up，不做轴交换**。takram 示例是 Y-up 的“北-上-东”帧，我们要换成东-北-天帧：

```ts
// 依赖：three@0.184.x（r186 需等 takram 上游或 fork 修复，简单补丁不够，见 §6.1）、@takram/three-atmosphere/webgpu、@takram/three-geospatial
const ctx = new AtmosphereContext()
ctx.camera = camera
renderer.contextNode = context({ ...renderer.contextNode.value, getAtmosphere: () => ctx })

// 1) 把 World ENU 钉到地球：锚点来自 World Package 的 coordinate.json（WGS84 经纬高）
const anchorECEF = new Geodetic(radians(lon), radians(lat), h).toECEF()
Ellipsoid.WGS84.getEastNorthUpFrame(anchorECEF, ctx.matrixWorldToECEF.value)   // x=E, y=N, z=U（与 r15 一致）

// 2) 太阳/月亮随仿真时间走（Timeline seek 时同步调用）
function onSimTime(date: Date) {
  const m = getECIToECEFRotationMatrix(date, ctx.matrixECIToECEF.value)
  getSunDirectionECI(date, ctx.sunDirectionECEF.value).applyMatrix4(m)
  getMoonDirectionECI(date, ctx.moonDirectionECEF.value).applyMatrix4(m)
}

// 3) 光照：注册一次，替换 DirectionalLight + HemisphereLight
renderer.library.addLight(AtmosphereLightNode, AtmosphereLight)
scene.add(new AtmosphereLight())

// 4) 后处理：在现有 RenderPipeline（EDL/描边/TRAA，r11）之前插入空气透视
const p = pass(scene, camera, { samples: 0 })
pipeline.outputNode = aerialPerspective(p.getTextureNode('output'), p.getTextureNode('depth'))
```

注意点：

1. **能见度/雾联动**：E 场的 `visibility`（km）应映射到 takram `AtmosphereParameters` 的 Mie 散射系数（`mieScattering`、`mieExtinction`，Koschmieder：`β_ext ≈ 3.912 / V`），但**改参数会触发 LUT 重算**（上表 12–30 s，真 GPU 估算 < 1 s），所以能见度变化要**量化成档位 + 防抖**，不能每帧改；局地浓雾仍用 r16 的高度雾叠加，不走 LUT。
2. **点云材质**：r11 定的点云是 `PointsNodeMaterial` / 自定义 TSL 无光照材质，`AtmosphereLight` 不影响它；点云的“被大气染色”完全来自空气透视后处理（depth 必须正确写入，EDL 之后再做空气透视）。
3. **LUT 异步**：`AtmosphereLUTNode.updateTextures()` 是异步的，首帧前 LUT 为空；UI 上要有“大气初始化中”的占位（天空先用 SkyMesh 顶上，LUT 就绪事件 `lutNode.addEventListener('update', …)` 后再切换，transitions.dev 的淡入动效）。

### 3.2 体积云：把 three-clouds 的算法移植为 TSL（Med / High 档）

three-clouds 是面向“地球尺度、相机可在太空”的实现（步长 50–1000 m、射线 200 km、`maxIterationCount=500`）。本项目是 1–3 km 城区、无人机 0–150 m、相机基本在云下，参数要重定。下面是移植时保留的算法骨架与改写点。

#### 3.2.1 云层与天气模型（`clouds.glsl` 的 `sampleWeather` / `sampleMedia`）

每个像素最多 4 个云层，分别对应天气纹理 RGBA 四个通道（`CloudLayers.DEFAULT`：R=低云 750 m 起厚 650 m、G=中云 1000 m 起厚 1200 m、B=卷云 7500 m 厚 500 m、A 空）。对位置 `p`（我们用 ENU，高度 `h = p.z`，不再用 `length(p) − bottomRadius`）：

```
hf      = clamp((h − layerMin) / (layerMax − layerMin), 0, 1)         // 各层归一化高度（vec4）
lw      = pow(texture(localWeather, uv·repeat + offset), weatherExponent)
shapeH  = 1 − (clamp(pow(hf, shapeAlteringBias)·2 − 1, −1, 1))²      // 半圆形轮廓：底平顶圆，bias=0.35
factor  = 1 − coverage · shapeH                                        // coverage 直接接 UI 的“云量 %”
density = remapClamped(mix(lw, 1, filterWidth), factor, factor + filterWidth)   // filterWidth=0.6
shape   = texture3D(shapeNoise, (p + evolution + turbulence)·shapeRepeat).r    // shapeRepeat=0.0003
density = remapClamped(density, (1 − shape)·shapeAmount, 1)
if (mip 低 && 细节开): detail = texture3D(detailNoise, p·0.006).r
    modifier = mix(detail⁶, 1 − detail, remapClamped(hf, 0.2, 0.4))    // 顶部蓬松、底部卷须
    density  = remapClamped(density·2, modifier·0.5·detailAmount, 1)
density *= densityScale · (expTerm·e^{exponent·hf} + linearTerm·hf + constantTerm)   // DensityProfile，默认 (0,0,0.75,0.25)
σs = Σdensity · scatteringCoefficient(=1)；σt = Σdensity · absorptionCoefficient(=0) + σs
```

`coverage` 默认 0.3，`turbulenceDisplacement=350`，`localWeatherRepeat=100`（地球尺度；我们按城区尺度改为 “1 个天气纹理周期 ≈ 20 km”）。

#### 3.2.2 主步进（`marchClouds`）与次级步进

```
stepSize = minStep + (perspectiveStepScale − 1)·rayNear         // 越远步越长，perspectiveStepScale=1.01
t = stepSize · jitter · 2                                         // STBN/Bayer 抖动
for i in 0..maxIter:
  if t > rayFar − rayNear: break
  p = o + t·d;  mip = log2(max(1, texelsPerPixel + t·1e-5))
  if 不在任何云层高度区间:  stepSize *= k; t += mix(stepSize, maxStep, min(1, mip)); continue
  W = sampleWeather(p)
  if all(W.density ≤ minDensity):  同上跳空；continue            // 空域加速
  M = sampleMedia(W, p)
  if M.σt > minExtinction:
    τsun = marchOpticalDepth(p, sunDir, N=maxIterToSun, step0=minSecondaryStep, ×secondaryStepScale)
         + (High 档) BSM(p)                                        // Beer Shadow Map 补远处遮挡
    L  = E_sun · Σ_{k<oct} a^k · e^{−τsun·b^k} · phase(cosθ, c^k)  // a=b=c=0.5，oct=8（Low 可 4）
       + E_sky · RECIPROCAL_PI4 · skyGradient · skyLightScale
    L *= σs · (1 − powderScale·e^{−σt·powderExponent})              // powder: 0.8, 150
    T  = e^{−σt·stepSize}
    Lint += Tint · (L − L·T)/max(σt, 1e−7)                          // Frostbite 能量守恒解析积分
    Tint *= T
    frontDepth 加权累计（供空气透视）
  if Tint ≤ minTransmittance: break                                 // 早停
  stepSize *= k; t += stepSize
```

相函数：双瓣 HG，`g1=0.7, g2=−0.2, mix=0.5`；衰减（多八度）时把 g 乘 `c^k`（“Frostbite 的相位衰减”）。精确模式可换 `draine` 混合（`ACCURATE_PHASE_FUNCTION`），移动端不开。

#### 3.2.3 时域上采样与重建（`CloudsPass` + `cloudsResolve.frag`）

- 以 **1/4×1/4 分辨率**渲染（每帧只算 1/16 像素），像素偏移按 4×4 Bayer 序列 `[0,8,2,10,12,4,14,6,3,11,1,9,15,7,13,5]` 轮换，16 帧覆盖全屏。
- 重建：用 depth+velocity 缓冲找 3×3 邻域最近片元做重投影，历史颜色做 **方差裁剪**（`varianceGamma=2`），`temporalAlpha=0.1` 混合，Catmull-Rom 采历史。
- 这与 r16 从 natural-disasters 学到的“1/16 Bayer 摊销 + 重投影”是同一族算法，**两者择一实现即可**：WebGL2 档用 natural-disasters 的 GLSL→TSL 版本，WebGPU 档用 takram 的更完整版本（BSM、光柱、地面反弹）。

#### 3.2.4 本项目参数重定（建议值，需要在真机上调）

| 参数 | takram 默认（地球尺度） | 本项目 Low | Med | High |
|---|---|---|---|---|
| 渲染分辨率 | 1/4 + 时域重建 | 1/4 | 1/4 | 1/4 |
| `maxIterationCount` | 500（low 预设 200） | 32 | 64 | 128 |
| `minStepSize` / `maxStepSize` | 50 / 1000 m | 60 / 400 m | 40 / 300 m | 25 / 250 m |
| `maxRayDistance` | 200 km | 15 km | 25 km | 40 km |
| 到太阳次级步数 | 2（low 1） | 1 | 2 | 2 + BSM |
| 多重散射八度 | 8 | 4 | 6 | 8 |
| 形状细节噪声 / 湍流 | 开 | 关 | 细节开 | 全开 |
| 云影（BSM 级联） | 3 级 512² | 无（用天气纹理直接投 2D 云影） | 1 级 256² | 2 级 512² |

云层随风移动：`localWeatherOffset += windAtCloudHeight · dt / weatherTileSize`，**用 CPU float64 积分相位**（r16 已证明直接写 `time·speed` 会在变风速时倒退）；风来自 E 场在云底高度的值。

---

### 3.3 3D 风场可视化：四种实现与分档

#### 3.3.0 数据契约（沿用 r17，补充可视化字段）

服务端每次风况变化（风向/风速/阵风档位变化或 Timeline seek）下发一份**规则网格 3D 纹理**：`RGBA16F`，`R,G,B = (u, v, w)`（m/s，ENU），`A = |u|` 或实体掩码（实体 = NaN 或 −1），尺寸如 `128×128×32`（≈ 1 MB），附 `origin(ENU)`、`size(m)`、`dims`、`uMax`。前端用硬件三线性采样（WebGL2 下 RGBA16F 可线性过滤，r17 已确认）。可视化还需要：

- `streamlines.bin`（§3.3.1 用）：服务端积分好的流线，每条 `K` 个点 `(x, y, z, τ, |u|)`，Float32；
- 实体占据（`R8`，与风场同网格或更细），用于粒子“撞楼即死”。

#### 3.3.1 A 方案：无状态流线虚线（Potato / Low 档，所有后端）

**服务端**（Python，RK4 或 RK2，r17 的 `VelocityIntegrator` 已有）：

```python
def build_streamlines(field, solid, n_lines, K=64, dt_scale=0.5):
    seeds = sample_seeds(field.aabb, n_lines,
                         z = zmin + (zmax - zmin) * rng.random()**1.9,   # 偏向低层（urban-wind-lab）
                         reject = solid)                                   # 不在实体里播种
    lines = []
    for p in seeds:
        pts, tau = [p], [rng.random() * T_DASH]      # τ 随机初相，避免所有虚线同步
        speeds = [norm(trilinear(field, p))]
        for k in range(K - 1):
            u = trilinear(field, p)
            if norm(u) < 0.2 or out(p) or solid_at(p): break
            dt = dt_scale * field.dx / max(norm(u), 0.5)          # CFL 式自适应步长
            p  = rk4(field, p, dt)
            pts.append(p); tau.append(tau[-1] + dt)                 # τ = 沿流线累计飞行时间
            speeds.append(norm(trilinear(field, p)))
        if len(pts) >= 8: lines.append((pts, tau, speeds))
    shuffle(lines)            # 打乱，前缀即均匀子采样 → 前端用 drawRange 调密度（r11 同一招）
    return pack_float32(lines)
```

**前端**（TSL，扁平四边形展开，不用实例化——r16 已实测 SwiftShader 上实例化慢 15–25 倍）：每个线段 6 个顶点，属性 `aP0, aP1 (vec3)`、`aTau (vec2)`、`aSpd`、`aCorner=(t∈{0,1}, side∈{−1,1})`。

```
vertex:
  c0 = P·V·(aP0,1);  c1 = P·V·(aP1,1)
  d  = normalize((c1.xy/c1.w − c0.xy/c0.w) · (aspect, 1))        // 屏幕方向
  n  = (−d.y/aspect, d.x)                                          // 屏幕法线
  c  = mix(c0, c1, aCorner.x)
  gl_Position = (c.xy + n · widthPx/screenH · c.w · aCorner.y, c.z, c.w)
  vTau = mix(aTau.x, aTau.y, aCorner.x);  vSpd = aSpd
fragment:
  phase = fract((vTau − simTime) / T_DASH)                         // 虚线以当地风速沿流线前进
  a     = smoothstep(0, 0.04, phase) · (1 − phase)^3               // 亮头 + 渐隐尾（彗星）
  color = ramp(vSpd / vMax)                                        // 科技灰 → 白 → 红（ANet #E93024）
```

性质：零 compute、零状态、`simTime` 任意 seek 可精确复现；风场更新时只换一个 VBO。代价：它画的是**流线**（瞬时场的切线），不是**迹线**；对阵风/湍流的时间变化只能靠服务端按 1–2 s 重算流线表达。

本机实测（`bench/src/streak.js`，640×360，10 万点背景，SwiftShader）：

| 配置 | 顶点数 | WebGL2 中位 / P90（ms） | 相对 base 增量 | WebGPU(SwiftShader) 中位 / P90 |
|---|---|---|---|---|
| base（仅 10 万点背景） | — | 59.5 / 117（第二轮）；61.2 / 110（第一轮） | — | 75.8 / 158 |
| 虚线 500 条 × 32 段（1.6 万段） | 9.6 万 | 91.5 / 171 | **+32 ms** | 未测 |
| 1px `LineSegments` 500 × 32 | 3.2 万 | 82.4 / 125 | +23 ms | 未测 |
| 虚线 1000 × 64（6.4 万段） | 38.4 万 | 166.8 / 301；173.7 / 231 | +106–114 ms | 331.6 / 708 |
| 1px `LineSegments` 1000 × 64 | 12.8 万 | 203.2 / 592 | +144 ms（噪声大，1px 线在 SwiftShader 上并不更省） | 未测 |
| 虚线 2000 × 64（12.8 万段） | 76.8 万 | 645.8 / 1,192 | +585 ms | 未测 |

服务端（这里用 JS 模拟）生成 1000 条 × 64 段流线 143 ms。截图 `bench/shot_s2_mode_dash_lines_1000_segs_64.png` 确认 WebGL2 后端正确出图。**结论：软件渲染下虚线预算约 1–1.6 万段（+20–30 ms），真 GPU 上 6.4 万段以上可忽略；密度靠“打乱后前缀 + drawRange”零成本调节，接进 r11 的 QualityController。**

#### 3.3.2 B 方案：GPGPU 状态纹理粒子 + 三帧线段（Med 档，WebGL2/WebGPU 通用）

在 three 的 WebGPURenderer 里，**不用 compute**，而用两张 `RGBA32F` RenderTarget 乒乓 + 全屏 `QuadMesh`（NodeMaterial）写状态——两个后端行为一致，也绕开 WebGL2 回退里 compute→transform feedback 的限制（r16）。

```
state0: (x, y, z, age)       state1: (xPrev, yPrev, zPrev, flags)
update (fragment, 每 texel 一个粒子):
  p  = state0.xyz;  v0 = W(p)
  pm = p + 0.5·dt·v0;  v1 = W(pm);  p' = p + dt·v1                   // RK2 中点（cesium-wind-layer/arrows/urban-wind-lab 一致）
  s  = |v1| / vMax
  dead = age > life || out(p') || solid(p')
       || rand(seed) < dropRate + s·dropRateBump                      // 0.003 + s·0.01（webgl-wind/cesium-wind-layer）
       || (|v1| < 0.02·Uref && age > life/4)                          // 滞止区回收（urban-wind-lab）
       || reseedFlag                                                  // 视角突变时整体重生（arrows 的 u_reseed）
  if dead: p' = spawn(id, seed) within (视锥 ∩ 数据域)，z 按 pow(r,1.9) 偏低层；prev = p'；flags.justSpawned = 1
  else:    prev = p
render（每粒子 4 顶点，扁平四边形）:
  头 = proj(p')，尾 = proj(p') − dir·lengthPx(s)，宽 = mix(wMin, wMax, s)       // lineLength 20–100 px、lineWidth 1–2 px
  justSpawned → 退化为零长度（避免跨屏拉线）
  片元 alpha = pow(smoothstep(0,1,segPos), 1.5) · mix(0.3, 1, s)
```

- 状态纹理尺寸 `ceil(sqrt(N))`；若是多高度层，按粒子序号 `mod(id, levels)` 确定性分层（cesium-wind-layer），保证每层都有粒子。
- `dt` 用仿真时间步 × `speedFactor`，和帧率解耦（arrows 的 `u_dtScale` 含“时间加速倍率与帧率补偿”，对应我们 Timeline 的 ×1/×2/×5/×10）。
- **不要**用 webgl-wind 的屏幕淡出拖尾；也不必像 webgl-wind 用 RGBA8 编码位置（那是 2016 年兼容性手段），直接用 float RT。

#### 3.3.3 C 方案：WebGPU compute + 历史环形缓冲（High 档）

WeatherLayers 的“按年龄分块、每步整体后移一块”在 WebGPU 里改成**环形索引**，省掉整块拷贝：

```wgsl
struct P { pos: vec3f, age: f32 };
@group(0) @binding(0) var<storage, read_write> hist : array<vec4f>;   // K × N，环形
@group(0) @binding(1) var<storage, read_write> part : array<P>;
@group(0) @binding(2) var wind : texture_3d<f32>;  @group(0) @binding(3) var samp : sampler;
@group(0) @binding(4) var<uniform> U : Params;      // head (当前环位置), K, N, dt, life, dropRate, bump, seed, aabb...

@compute @workgroup_size(256)
fn step(@builtin(global_invocation_id) g: vec3u) {
  let i = g.x; if (i >= U.N) { return; }
  var p = part[i];
  let v0 = sampleWind(p.pos); let v1 = sampleWind(p.pos + 0.5*U.dt*v0);
  var np = p.pos + U.dt*v1;
  if (isDead(np, p.age, length(v1), i)) { np = spawn(i); resetHistory(i, np); p.age = 0.0; }
  part[i] = P(np, p.age + U.dt);
  hist[U.head * U.N + i] = vec4f(np, length(v1));     // 写入当前环位置
}
// 绘制：顶点 k∈[0,K−1) 连 hist[(head−k) mod K] 与 hist[(head−k−1) mod K]，alpha = 1 − k/K
```

建议 `N = 100k–300k`、`K = 8`；每帧 1 次 dispatch，环头 `head = (head+1) mod K` 由 CPU 更新 uniform。与 r11 的“粒子 compute”配方共用 `instancedArray`/storage 基础设施。

#### 3.3.4 D 方案：3D 风箭头（所有档，“查看风矢量”模式）

来自 cesium-wind-arrows-3d：锚点也做 RK2 平流（“游动的箭头”比固定网格箭头更易读），寿命到期或出播种范围即重生。

```
yaw   = atan2(v, u)；pitch = atan2(w, sqrt(u²+v²))          // w 给出俯仰，上升/下沉气流一眼可见
Lworld = arrowPx · 2·dist·tan(fov/2) / screenH              // 屏幕恒定像素长度
颜色 = ramp(|u|/vMax)；可选 u_zLock 锁定某高度层（UI 上的高度滑杆）
```

建议 Low 档 2k 个、High 档 16k 个锚点；箭头网格 ≤ 12 个三角形，按扁平展开绘制。

#### 3.3.5 可视化参数总表

| 参数 | 值 | 出处 |
|---|---|---|
| dropRate / dropRateBump | 0.003 / 0.01 | webgl-wind、cesium-wind-layer |
| 粒子寿命 | 2–4 s（仿真时间） | r11 建议；urban-wind-lab `uLife` 同量级 |
| 播种高度分布 | `z = zmin + (zmax−zmin)·r^1.9` | urban-wind-lab `spawn()` |
| 滞止回收阈值 | `|u| < 0.02·Uref` 且 `age > life/4` | urban-wind-lab |
| 线宽 / 线长 | 1–2 px / 20–100 px，随速度插值 | cesium-wind-layer 默认值 |
| 流线虚线周期 | `T_DASH = 6 s`，头部 0.04，尾部 `(1−φ)^3` | 本文实测参数 |
| 色带 | 0 → vMax：`#8A8F98`（科技灰）→ `#F5F5F5` → `#E93024`（ANet 红） | 产品色约束 |
| 粒子数 | Potato 流线 ≤ 500×32 段；Low 8k；Med 32k–65k；High 100k–300k | 本文实测 + r16 |

---

### 3.4 Level 2.5 瞬态风：把 urban-wind-lab 的 D3Q19 LBM 移到服务端

**为什么需要**：r17 的 Level 2（WindNinja 质量守恒变分法）给的是**稳态、无散**的平均风场，速度是对的，但没有**阵风、建筑尾流涡脱落、街谷间歇性**——这些恰恰是无人机在城区贴楼飞行时最危险的扰动，也是 §20 Level 1 “random turbulence” 该有的物理来源。LBM 天然是瞬态的、对复杂几何只需体素掩码（UrbanScene3D 点云体素化直接可用），实现只有 ~200 行。

**算法（逐项对应 `urban-wind-lab.html` L2260–2420）**：

```
格子：D3Q19，e_q、w_q = {1/3, 1/18×6, 1/36×12}，c_s² = 1/3
单位换算：U10_lat = 0.045（格子单位下 10 m 高参考风速，Ma≈0.08）
          dt = U10_lat · dx / U10_phys                      // 例：dx=4 m、U10=8 m/s → dt=22.5 ms
          ν_lat = (τ0 − 0.5)/3，τ0 = 0.51（分子黏性极小，湍流靠 SGS）
每步（融合 collide+stream，push 格式）：
  ρ = Σ f_q；u = Σ f_q e_q / ρ
  f_eq = w_q ρ (1 + 3 e·u + 4.5 (e·u)² − 1.5 u²)
  Π_ab = Σ e_qa e_qb (f_q − f_eq)                             // 非平衡二阶矩
  τ = ½ (τ0 + sqrt(τ0² + 18√2 · C_s² · |Π| / ρ))，C_s = 0.14，τ ≤ 1.6   // Smagorinsky（Hou 1996）
  f_q* = f_q − (f_q − f_eq)/τ
  若邻居是实体或地面：f_opp(q)(x) = f_q*        // 半程反弹，无滑移
  否则：f_q(x + e_q) = f_q*
入口（所有迎风面）：f = f_eq(ρ=1, u = U10_lat · ln((z+z0)/z0)/ln((10+z0)/z0) · 风向)
出口：零梯度（复制内层）；顶面：反弹或自由滑移
输出：每 N 步把 u 写成 RGBA16F 快照（§3.3.0 同契约），同时累计 ū 与 u'u'（湍流强度 I = sqrt(⅓ tr(u'u'))/|ū|）
```

**本机实测**（`.cache/research/n04/lbm/lbm_bench.py`，numba 0.67，float32，8 线程，机器同时有其他负载，load 15–20，属保守下限）：

| 网格 | 单元数 | 内存（双缓冲） | 吞吐 | 墙钟 / 仿真秒 | 稳定性 |
|---|---|---|---|---|---|
| 88×88×33（urban-wind-lab balanced，dx=2.7 m） | 25.6 万 | 39 MB | 3.8 MLUPS | 4.4 s | 300 步无 NaN |
| 200×200×60（dx=4 m，800 m×800 m×240 m） | 240 万 | 365 MB | 5.8 MLUPS | 18.5 s | 60 步无 NaN |

据此推算：UrbanScene3D 一个城区（约 1.5 km×1.5 km×300 m）取 dx=8 m 为 188×188×38≈134 万格，60 s 瞬态约 5 分钟；dx=4 m 为 1,050 万格，约 80 分钟/60 s——**只能离线**，放进 r17 的风场库流水线（每个风向×风速档额外存一段 60 s 瞬态），或 V1.0 上 GPU（CUDA LBM 通常 ≥1,000 MLUPS，估算）。运行时仍然只做插值：`W(x,t) = W_L2(x; dir, speed) + α·(W_LBM(x, t mod T) − W̄_LBM(x))`，即稳态场 + 瞬态脉动（周期回放），α 由 UI 的“阵风强度”控制。

### 3.5 城市风场 ML 代理（V1.0）与**立即可用的风场验收指标**

**V1.0 路线**：用 r17 离线库（WindNinja L2 + OpenFOAM L3 + 本文 L2.5 LBM）产出的 `{DSM/体素, 风向, 风速} → 3D 风场` 对，训练 3D 神经算子（`neuraloperator` 的 FNO/TFNO，或 `Transolver`/PhysicsNeMo 的几何感知 Transformer），目标是“新场景、新风向 1 s 内给出 3D 风场”。WinDiNet 证明了两件事：(1) 大模型先验 + 1 万条 CFD 就能在 2D 上做到 <1 s 的 112 帧瞬态；(2) 只需 2 个去噪步。但它是 2D 行人层、需要 CUDA，**不能直接用**。

**立即可用**：把 WinDiNet 的三个物理损失改成 3D，作为**任何风场（L1 解析、L2 质量守恒、L2.5 LBM、L3 CFD、未来 ML）入库前的自动验收**，写进 World Package 的 `environment/wind/manifest.json`：

```
流体掩码 f（1=流体），距实体距离 d（EDT，米）
散度残差     D  = mean_{2×2×2 全流体模板}( (∂u/∂x + ∂v/∂y + ∂w/∂z)² ) · dx² / U10²       // 无量纲；阈值需标定：L2 质量守恒法 ≪ 1e−6（r17 实测散度 ≤ 3e−6），LBM（弱可压）建议 < 1e−4（估算）
壁面穿透     P  = mean_{壁面带 band=2 格}( (u·n)² ) / U10²，n = ∇(1−f)/|∇(1−f)|           // 建议 < 1e−3（估算，需用 OpenFOAM 结果标定）
近壁加权误差 E  = Σ w(x)|u−u_ref|² / Σ w(x)，w = f·(1 + α·exp(−d²/(2σ²)))，α=2，σ=20 m    // 与参考 CFD 比较时用
入口廓线误差 Pz = max_z |U(z) − U10·ln((z+z0)/z0)/ln((10+z0)/z0)| / U10（上风 10% 区域）
```

### 3.6 实时遥测：从 moq 借来的 QoS 语义与传输层抽象

r27 已定下 `anet.rt.v1`（WebSocket、16 B 对齐帧头、每 tick 一个 BATCH、credit 窗口）。本文补三块。

#### 3.6.1 传输层抽象：WebTransport / WebSocket 竞速（移植 `moq/js/net/src/connection/connect.ts`）

```ts
interface RtTransport {                    // anet.rt.v1 只依赖这个接口
  send(frame: ArrayBufferView, opts?: { unreliable?: boolean }): void
  onFrame(cb: (buf: ArrayBuffer) => void): void
  close(code?: number): void
  readonly kind: 'webtransport' | 'websocket'
}
const wsWon = new Set<string>()            // 进程级记忆：WebSocket 赢过的 URL
async function connectRt(url: URL, o: { wtDelayMs?: number; certHashes?: Uint8Array[] } = {}) {
  const abort = new AbortController()
  const wt = ('WebTransport' in globalThis) && !wsWon.has(url.href)
    ? openWebTransport(url, o.certHashes, abort.signal) : undefined     // HTTP/3, 可发 datagram
  const head = wt ? (o.wtDelayMs ?? 500) : 0                            // 给 QUIC 500 ms 先跑
  const ws = delay(head, abort.signal).then(() => openWebSocket(toWs(url), abort.signal))
  const t = await Promise.any(wt ? [wt, ws] : [ws]); abort.abort()      // 先成功者胜，其余取消
  if (t.kind === 'websocket' && wt) wsWon.add(url.href)
  return t
}
```

MVP 阶段服务端只有 WebSocket（FastAPI），`WebTransport` 分支会失败并被 `Promise.any` 忽略；V1.0 在 Gateway 前挂 Rust 的 `moq-relay` 或 `wtransport` 服务后自动启用，业务协议不变。**datagram 通道**只用于可丢的高频小包（如 50 Hz 位姿，<1200 B），关键控制/事件永远走可靠流。

#### 3.6.2 通道 QoS 三旋钮（写进 `advertise` 的 channel 元数据）

| 字段 | 取值 | 语义（moq-lite） | 本项目示例 |
|---|---|---|---|
| `priority` | 0–255 | 拥塞时高优先级先占带宽 | `cmd.ack`=250，`uav/+/state`=200，`env/wind/meta`=150，`sensor/+/lidar`=60，`env/wind/volume`=40 |
| `order` | `newest` / `oldest` | 多个 group 待发时先发哪个 | 状态类 newest；事件日志、回放补数 oldest |
| `maxAgeMs` | ≥0 | 非最新 group 超过此年龄就跳过（按仿真时间轴，不按墙钟）；0 = 只要最新 | 位姿 200；风场体 0；事件日志 ∞ |

发布端与订阅端**都**执行 maxAge 过滤（发布端只知道所有订阅者里最宽松的那个）。这与 r27 的 credit 窗口正交：credit 管“发多少”，三旋钮管“发哪个”。

#### 3.6.3 两类 JSON/二进制轨道语义（移植 `@moq/json`）

- **Snapshot 轨道（有损，只关心“现在的值”）**：每个 group 以**完整快照**开头，后续帧是增量（JSON 用 RFC 7396 merge-patch；二进制 DroneState 用“字段掩码 + 变更字段”）；当本 group 已写增量字节 `> deltaRatio × 快照字节`（默认 8）时开新 group。迟到的订阅者只取最新 group：读快照、依次应用增量即得当前状态。适用：`mission/+/plan`、`uav/+/health`、`env/params`、`world/layers`。
- **Stream 轨道（无损，有序追加）**：每帧是自足记录，全部按序送达。适用：`events`、`mission/+/log`、`anet/+/messages`。
- 高频数值状态（`uav/+/state` 50 Hz）仍用 r27 的定长二进制记录，但**每 1 s 强制一个关键帧**（全量），中间帧可以是差分——同一套 group 规则。

#### 3.6.4 迟到客户端缓冲（移植 Rerun `serve_grpc` 语义）

```
Gateway 每个 channel 一个环形缓冲：
  static（场景元数据、风场 manifest、World 图层表）→ 永不丢弃
  temporal → 总内存上限（默认 256 MB，Rerun 默认 1 GiB），超限丢最旧
新客户端连上：先推全部 static，再按 newest_first（实时观看）或 oldest_first（打开回放）补 temporal
```

### 3.7 Rerun 旁路：研发记录与离线复盘（V0.2 起）

```python
import rerun as rr
rr.init("anet_world_runtime", recording_id=session_id)
rr.save(f"recordings/{session_id}.rrd")               # 或 rr.serve_grpc(server_memory_limit="1GiB")
def on_tick(sim_t, uavs, wind_slice):
    rr.set_time("sim_time", duration=sim_t)             # 多时间轴：再加 rr.set_time("tick", sequence=k)
    for u in uavs:
        rr.log(f"world/uav/{u.id}", rr.Transform3D(translation=u.p_enu, quaternion=u.q_xyzw))
        rr.log(f"world/uav/{u.id}/vel", rr.Arrows3D(origins=[u.p_enu], vectors=[u.v_enu]))
        rr.log(f"plots/uav/{u.id}/alt", rr.Scalars(u.p_enu[2]))
    rr.log("world/env/wind_slice", rr.Arrows3D(origins=wind_slice.o, vectors=wind_slice.v))
rr.log("world/pointcloud", rr.Points3D(xyz, colors=rgb), static=True)   # 静态数据只发一次
```

- 实体路径约定与 `anet.rt.v1` 的 channel 名一一对应（`world/uav/{id}` <-> `uav/{id}/state`），两边可以互相翻译。
- **只进开发者工具链**：Rerun 查看器是 egui/wgpu，不符合 shadcn 约束；产品 UI 的回放仍是我们自己的 Timeline（r15 的 IterablePlayer 算法），只借 Rerun 的 latest-at / range 查询语义：`latestAt(entity, component, timeline, t)` = 该时间轴上 ≤ t 的最后一条；`range(entity, timeline, [t0, t1])` 返回区间内全部记录。
- 大批量点云/轨迹用 `rr.send_columns()` 列式发送，避免逐条 `log` 的开销。

### 3.8 孪生状态模型（借 Eclipse Ditto 的 Thing/Feature 结构）

```json
{
  "thingId": "hefei-campus:uav/P600-01",
  "definition": "anet:uav.p600:1.0.0",
  "attributes": { "model": "P600", "mass_kg": 3.2, "sensors": ["rgb.zoom", "lidar.mid360"] },
  "features": {
    "pose":     { "properties": { "p_enu": [12.3, -4.1, 82.3], "q": [0,0,0.38,0.92], "t": 1727501520.12 } },
    "flight":   { "properties": { "mode": "MISSION", "armed": true },
                  "desiredProperties": { "mode": "RTL" } },
    "battery":  { "properties": { "pct": 78 } },
    "mission":  { "properties": { "wp_index": 5 }, "desiredProperties": { "plan_id": "m-042" } },
    "capability.thermal": { "definition": "anet:cap.thermal.imaging:1.0.0", "properties": { "available": false } }
  }
}
```

要点：`properties` = 实际上报（reported），`desiredProperties` = 指令意图；二者不一致即“执行中”，UI 用 transitions.dev 的过渡态展示；`definition` 字段直接承载 ANet 的 capability 类型（§31），V1.0 的能力发现就是按 `definition` 查询 Feature。Ditto 本身不部署，这只是 World Runtime 内部与 REST 快照 API 的数据形状。

---

## 4. 在本项目中的落点与复用方式

| 能力 | 来源（文件 / 函数） | 本项目模块 | 版本 | 复用方式 | 理由 |
|---|---|---|---|---|---|
| 物理大气（天空、日照、空气透视、光柱） | takram `atmosphere/src/webgpu/*`（`AtmosphereContext`、`AtmosphereLight`、`aerialPerspective`、`skyBackground`、`skyEnvironment`） | `apps/web` EnvironmentLayer / PostFX | V0.5（High 档） | adopt（npm，锁 three r184 或补丁） | Web 上唯一成熟的 TSL 版 Bruneton+Hillaire；自研成本高 |
| 预烘焙天空全景 | 同上，离线脚本用 `skyEnvironment()` 渲等距柱面图 | `environment/atmosphere/` 资产 + EnvironmentLayer | V0.3 | adopt（离线工具）| 软件档唯一可负担的“物理正确”天空 |
| Preetham 天空 | three `examples/jsm/objects/SkyMesh.js` | EnvironmentLayer | V0.3 | adopt | Low/Med 档默认 |
| 体积云（天气通道、形状改变函数、密度廓线、空域跳跃、多八度散射、能量守恒积分、BSM、1/4 时域上采样） | takram `clouds/src/shaders/{clouds.frag,clouds.glsl,cloudsResolve.frag}`、`CloudLayers.ts`、`qualityPresets.ts`、`bayer.ts` | EnvironmentLayer/Cloud | V0.5–V0.6 | port（GLSL→TSL） | three-clouds 无 WebGPU 版；算法最完整 |
| 无状态流线虚线 | 本文 §3.3.1（思路近 anvaka/wind-lines；实测脚本 `bench/src/streak.js`） | 服务端 `environment/wind/streamlines.py` + 前端 `WindLayer` | V0.3 | 自研（本文给出完整实现） | 软件档可用、可 seek、零状态 |
| GPGPU 粒子 + 三帧线段 | cesium-wind-layer `shaders/{calculateSpeed,postProcessingPosition,segmentDraw}.ts`、webgl-wind `update.frag.glsl` | `WindLayer`（Med） | V0.3 | port | 两后端通用，不依赖 compute |
| compute 粒子 + 历史环 | weatherlayers-gl `particle-line-layer.ts#_setupTransformFeedback`（年龄块思路） | `WindLayer`（High） | V0.5 | port（改环形索引） | 真正的迹线、百万级粒子 |
| 3D 风箭头 | cesium-wind-arrows-3d `shaders/{computing,skinning}.ts` | `WindLayer`/DebugLayer（Wind Vector） | V0.3 | port | §16 DebugLayer 的 Wind Vector 与 §21 Arrow |
| 瞬态风（LBM） | urban-wind-lab 求解器段（L2260–2420） | 服务端 `environment/wind/lbm.py`（numba） | V0.4–V0.6 | port | 阵风/尾流，补 L1 湍流的物理来源 |
| 风场验收指标 | windinet `training/losses.py` 三个损失 | `environment/wind/validate.py` | V0.3 | port（改 3D） | 所有风场入库前自动检查 |
| 3D 风场神经代理 | physicsnemo / neuraloperator / Transolver | `environment/wind/surrogate/`（GPU 服务器） | V1.0 | reference → adopt | 需 CUDA，放最后 |
| 传输竞速 + 通道 QoS + Snapshot/Stream | moq `js/net/src/connection/connect.ts`、`doc/concept/moq-lite.md`、`js/json` | `apps/web/rt/transport.ts`、Gateway | V0.2 | port | 补齐 r27 协议缺的“发哪个”语义 |
| MoQ 中继（FPV 视频、多观众） | moq `rs/moq-relay` + `@moq/net`/`hang` | Video Gateway | V1.0 | adopt（可选） | WebCodecs + QUIC 低延迟视频，替代 WebRTC 信令复杂度 |
| 迟到客户端缓冲 | rerun `rerun_py/rerun_sdk/rerun/sinks.py#serve_grpc` 语义 | Gateway | V0.2 | port | static 永不丢 + 内存上限 + newest_first |
| 研发记录/复盘 | `rerun-sdk`（Python）+ Rerun Viewer | `tools/rerun_sidecar.py` | V0.2 | adopt（开发用） | 零成本拿到可视化调试与 .rrd 记录 |
| 孪生状态模型 | Eclipse Ditto Thing/Feature/`desiredProperties`/`definition` | World Runtime 数据模型、REST 快照 | V0.2（模型）/ V1.0（能力） | reference | reported/desired 分离、能力类型化 |
| WebTransport Python 服务 | pywebtransport | — | — | skip | KI-003：与浏览器互通失败 |

---

## 5. 对比与推荐

### 5.1 天空 / 体积云（与 `refs/weather/*`、`refs/web3d/three.js` 对比）

| 排名 | 仓库 | stars / 2026 活跃 | 契合度 | 结论 |
|---|---|---|---|---|
| 1 | takram three-geospatial | 1,697 / 是（2026-05，WebGPU 分支 2026-04） | 大气可直接用（High 档）；云算法最完整但只有 GLSL | **补充**：High 档大气 adopt、云 port；不替代 r16 的任何仓库 |
| 2 | natural-disasters（r16） | 273 / 是 | WebGL2 + 质量自适应 + potato 档 | 继续作为 WebGL2/软件档云与雨的主算法源 |
| 3 | Eanpa-Sky（r16） | 56 / 是 | WebGPU/TSL 天气引擎 | 继续作为雨/闪电/湿地面算法源 |
| 4 | three.js `webgpu_volume_cloud` / `SkyMesh` | 官方 | 最小实现 | Low 档默认 |
| — | procedural-clouds（r16）、CK42BB/procedural-clouds-threejs、FarazzShaikh/three-volumetric-clouds、joshbrew/nff747 WebGPU 云 | 6–124 | demo 级 | reference / skip |

### 5.2 3D 风场可视化

| 排名 | 仓库 | stars / 最近提交 | 适用档 | 优点 | 缺点 |
|---|---|---|---|---|---|
| 1 | NOC-OI/cesium-wind-layer（← hongfaqiu 125 stars） | 0 / 2026-09-24 | Med | 三帧几何拖尾、多层 cube、确定性分层播种，2026 仍在迭代 | Cesium 专用，只做水平平流（层内 z 恒定） |
| 2 | Rawcloud/cesium-wind-arrows-3d | 0 / 2026-09-16 | 全档 | `sampler3D`+(u,v,w)、俯仰箭头、视口播种、突变重生 | 新、无 star，需要自己验证 |
| 3 | weatherlayers-gl | 161 / 2026-09-28 | High（思路） | 年龄环形拖尾、完整气象控件 | deck.gl 生态、双许可 |
| 4 | mapbox/webgl-wind | 1,108 / 2026-06（算法 2017） | — | 最经典、最短 | 屏幕空间拖尾不适用 3D；RGBA8 编码过时 |
| 5 | urban-wind-lab 示踪粒子 | 1 / 2026-09-26 | Med/High | 实体内杀死、底层加权播种、滞止回收 | transform feedback 实现，不能直接搬到 three |
| — | RaymanNg/3D-Wind-Field、sakitam wind-layer、leaflet-velocity、earth | 498–6,608 | — | 历史参考 | 2D 或已停更 |

与已有 `refs/web3d/deck.gl`（r15）对比：deck.gl 的 `TripsLayer` 解决“轨迹随时间显隐”，不解决“风场平流”；风场可视化以本节方案为准。

### 5.3 风场物理与代理（与 `refs/weather/{windninja,OpenFOAM-dev,FastEddy-model,openvdb}` 对比）

| 层级 | 首选 | 补充（本文） | 说明 |
|---|---|---|---|
| L0/L1 常风 + 阵风 + 湍流 | 解析模型（r17） | LBM 瞬态脉动统计（湍流强度 I、功率谱）用于标定 Dryden/von Kármán 参数 | 让 L1 的“random turbulence”有物理来源 |
| L2 地形/建筑稳态 | WindNinja 质量守恒移植（r17） | — | 不替换 |
| **L2.5 瞬态城区** | — | **urban-wind-lab LBM 服务端移植** | 新增层级 |
| L3 CFD | OpenFOAM 离线库（r17） | — | 不替换 |
| L4 LES | FastEddy（r17，reference） | — | 不替换 |
| ML 代理 | — | physicsnemo / neuraloperator / Transolver（V1.0）；WinDiNet 仅借物理损失 | 需 GPU |

### 5.4 实时遥测（与 `refs/backend/{ws-protocol,rosbridge_suite,zenoh,mavlink}` 对比）

| 排名 | 方案 | stars / 活跃 | 角色 | 结论 |
|---|---|---|---|---|
| 1 | 自研 `anet.rt.v1`（r27）+ 本文 §3.6 语义 | — | 浏览器主协议 | 保持；补 QoS 三旋钮、Snapshot/Stream、传输竞速 |
| 2 | moq-dev/moq | 1,546 / 2026-09-28 | 语义来源；V1.0 视频与扇出 | **补充**（不替换 r27） |
| 3 | rerun-io/rerun | 11,503 / 2026-09-27 | 研发记录与复盘 | **补充**（与 r15/r27 定的 foxglove-sdk 并存：foxglove 连 ROS 生态，Rerun 连 Python/ML 研发） |
| 4 | zenoh（r27） | 3,216 / 2026-09 | 服务端内部总线 | 不变 |
| — | foxglove ws-protocol（已归档）→ foxglove-sdk | 150 / 311 | 调试旁路 | 不变（r27） |
| — | pywebtransport / aioquic / wtransport | 42 / 2,006 / 709 | WebTransport 服务端 | Python 两者 skip；Rust wtransport 作 V1.0 备选 |

### 5.5 数字孪生框架

没有一个开源孪生框架适合直接当本项目的 World Runtime：Ditto 偏 IoT 设备影子、OpenTwins 偏 K8s 组合平台、iTwin.js 偏 BIM。**结论：不引入框架，只借 Ditto 的数据形状（§3.8）**；World Runtime 仍是我们自己的核心资产（与 01-design §51 的结论一致）。

---

## 6. 风险与注意事项

### 6.1 takram 与 three 版本锁（已实测，最高优先级）

- `@takram/three-atmosphere@0.19.1`（2026-05-06 发布）+ three r186：模块求值即抛 `TypeError: Cannot read properties of undefined (reading 'name')`。把构建产物里 5 处 `X.layout.name` 改成 `X.name` 之后，又抛 `Error: Unsupported layout type: [object Function]`（`Fn` 布局里把 struct 当类型用，r186 的 struct 代理不再被识别）。**不是一两行补丁能解决的**。
- 处置建议（按优先级）：
  1. **主应用保持 r11 定的 three r186**，takram 大气推迟到 V0.5 的 High 档再接入；届时先看 takram 是否已跟进（它 2026-03→05 两个月发了 0.17→0.19 三个版本，跟进速度快），否则 fork 修复（TSL `struct` 用法集中在 `AtmosphereContextBase.ts` 和 core 的 `webgpu/` 下）。
  2. 若 V0.3 就必须要物理天空：离线（另一个锁 three r184 的小工具工程）用 takram 烘焙天空全景（§3.1.2 Potato 档），主应用只加载图片——**版本锁只影响离线工具，不影响主应用**。
  3. 不建议为了 takram 把整个应用降到 r184：r11 的点云 `glpoint` monkey-patch、`BundleGroup` 等结论都是在 r186 上实测的。
- 另：takram 的 WebGL2 后端分支在本机出现 `'AtmosphereParameters' : syntax error` 与 LUT 反复重建；即使将来修好，软件档的 LUT 生成（12–30 s）和每帧 +0.8–1.2 s 也不可接受。

### 6.2 体积云移植成本

- `clouds.frag` 1,003 行 + `shadow.frag` 193 行 + `cloudsResolve.frag` 177 行 + 噪声生成，含 `#pragma unroll_loop` 展开循环、`#ifdef` 分支组合、`sampler2DArray` 级联阴影。改写为 TSL 估计 2–3 人周（估算），且 WebGL2 后端对循环次数、uniform 数量更敏感，需要按 §3.2.4 的档位裁剪。
- 地球曲率相关代码（`getGlobeUv` 立方球映射、`length(p) − bottomRadius`）在我们的平面 ENU 场景里要换成平面映射，否则 1–3 km 范围内会引入不必要的计算。

### 6.3 风场可视化

- **流线 ≠ 迹线**：方案 A 画的是瞬时场切线。阵风/湍流场里，它会“看起来比实际平稳”；必须由服务端按 1–2 s 重算流线，并在 UI 说明“显示模式：流线 / 粒子迹线”。
- **SwiftShader 是顶点受限的**：虚线 2000×64 段已达 +585 ms/帧（§3.3.1 表）。所有风可视化必须接 QualityController 的顶点预算，Potato 档 ≤ 1.6 万段。
- **float 渲染目标**：方案 B 需要 WebGL2 的 `EXT_color_buffer_float`（RGBA32F 可渲染）；不支持时降到 RGBA16F（位置用相对域原点的归一化坐标，精度约 1/2048 域长，1.5 km 域 ≈ 0.7 m，可接受）或只用方案 A。
- **seek 一致性**：方案 B/C 是有状态的，Timeline 拖动后粒子分布不能复现（只能整体重播种）；需要“可复现”的场景（录屏、对比实验）用方案 A。
- 相机远离时粒子密度会变化：必须按“视锥 ∩ 数据域”播种并在视角突变时整体重生（arrows 的 `u_reseed`），否则缩放后出现密集带。

### 6.4 LBM（Level 2.5）

- 稳定性：`τ` 接近 0.5 时 BGK 易发散，urban-wind-lab 用 `τ0=0.51` 加 Smagorinsky 抬高有效 τ 并截断 `τ≤1.6`；格子马赫数由 `U10_lat=0.045` 控制，**高层风速放大（对数律）后要确保 `|u|_lat < 0.15`**，高楼顶部加速区尤其要检查。
- 分辨率：建筑至少 5–10 格才能出现合理的分离与尾流；dx=8 m 只适合街区尺度的统计，不适合“贴墙飞”的局部阵风。
- 点云体素化：树木、广告牌、镂空结构被当成实体会高估阻塞；需要语义层（§7 Semantic）把植被标成“多孔介质”（LBM 可加 Brinkman 阻力项，urban-wind-lab 未实现）。
- 吞吐：本机 numba 实测 3.8–5.8 MLUPS（有其他负载，保守下限）；城区级 4 m 分辨率只能离线跑，GPU 化要到 V1.0。
- urban-wind-lab 作者明确“未验证、只作教学”；我们引入时必须用 r17 的 OpenFOAM 结果做对照，并用 §3.5 的指标自动检查。

### 6.5 ML 代理

- WinDiNet：推理需要 CUDA GPU（README 训练建议 48 GB 显存），输入限定 `field_size_m ∈ [900,1400]`、`inlet_speed ∈ [0.1,20] m/s`、256² 平面图，输出只有 2D。本机无 GPU，**无法实测**。
- PhysicsNeMo / neuraloperator / Transolver 都需要 GPU 训练；数据集要靠我们自己的 CFD 库积累（每个城区 × 16 风向 × 若干风速），这是 V1.0 的前置条件。

### 6.6 遥测 / 传输

- **Python 没有可用的浏览器 WebTransport 服务端**（pywebtransport KI-003 Blocked；aioquic 最近提交 2025-10-11）。任何 WebTransport 计划都意味着引入 Rust 组件（moq-relay 或 wtransport）。
- 本地开发用自签证书时，浏览器 `serverCertificateHashes` 对证书有额外要求（通常是 ECDSA 且有效期很短，Chrome 要求 ≤ 14 天——以浏览器当期文档为准），需要开发脚本自动轮换；企业/校园网常封 UDP/QUIC，**WebSocket 回退必须永远存在**（moq 的竞速逻辑正是为此）。
- moq-lite 仍在演进（moq-lite 07 为 `-wip`，moq-transport 草案到 22），**只借语义、不绑线格式**；V1.0 若 adopt moq-relay，要锁定版本。
- Rerun：SDK 与 Viewer 版本必须一致，`.rrd` 只保证读上一个 minor 版本；研发记录要连同 Rerun 版本一起归档。`serve_grpc` 默认缓冲 1 GiB 内存，长时间仿真要设 `server_memory_limit`。

### 6.7 测量可信度

本文所有浏览器数字来自 SwiftShader（软件渲染）且测量时 load average 6–20；只能用于**同页相对比较和“能不能跑”的判断**，不能当真 GPU 的性能预期。真 GPU 上的数据要在 V0.3 的 CI（带 GPU 的 runner）或开发者机器上补测。

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§12 “300,000 particles → GPU Compute Shader” 需要分档改写**：风可视化按 §3.3 分成 A（无状态流线虚线，所有后端，可 seek）/ B（float RT 乒乓粒子，WebGL2 通用）/ C（WebGPU compute + 历史环）/ D（3D 箭头）四种；compute 只在 WebGPU 档启用。并明确“风可视化读的是服务端下发的同一份风场体”，浏览器不做任何风场求解（与 §13 一致）。
2. **§19–20 风场分级补一个 Level 2.5（瞬态城区风，LBM）**，并说明它的用途：给 Level 1 的 “random turbulence” 提供物理标定（湍流强度、谱），给 Timeline 提供可回放的阵风序列；Level 3 之后再加 “Level 3′：ML 代理（V1.0，需 GPU）”。
3. **§20 Level 3 的 `.vdb` 文件结构旁边补“风场 manifest”**：记录生成层级（L0–L3/ML）、求解参数、以及 §3.5 的验收指标（散度残差、壁面穿透、入口廓线误差），入库前自动检查不达标即拒绝。
4. **§21 风场 Web 可视化补数据契约与视觉规范**：3D 纹理 `RGBA16F(u,v,w,|u|)` + `streamlines.bin`（含飞行时间 τ）+ 实体掩码；四种显示模式（流线、粒子、箭头、切片热力图）；色带用产品色（科技灰 → 白 → ANet 红 #E93024），图例/读数面板走 lieflat-charts 视觉语言；**3D 必须用几何拖尾，禁止屏幕空间淡出拖尾**。
5. **§16 EnvironmentLayer 的 “Sky” 需要单独成节**：天空/太阳位置由“World Package 锚点经纬度 + 仿真时间”决定（Timeline seek 时同步更新），分 Potato（预烘焙全景）/ Low（SkyMesh）/ High（takram 物理大气 + 空气透视）三档；**§41 的 `coordinate.json` 必须包含 WGS84 锚点**（否则算不出太阳方位），`environment/atmosphere/` 目录放预烘焙天空全景。
6. **§25 云的分档与首期建议需要修正**：原文“第一阶段推荐 Level 2（3D Noise Volume）”在软件渲染/集显上仍然太贵。建议 V0.3：Level 1（2D 云层 + 云影贴图）作为默认，Level 2 只在 Med 档；V0.5–V0.6：Level 3（ray-march，takram 算法 TSL 移植，1/4 分辨率时域重建，BSM 云影投到点云上）。另外补一条原文缺失的：**低云/雾层（云底 < 300 m，山区常见）必须是“可进入的体积”**，与 §23 Fog 共用一个体积密度场，这样无人机“入云”时视觉、RGB/LiDAR 退化（§22–23）来自同一数据。
7. **§17 “Environment Visualization vs Environment Physics” 再加一条 “Environment Provenance/Validation”**：每个环境场标注来源层级与验收指标，UI 在 Weather 面板显示“当前风场：L2 质量守恒 · 散度 3e−6 · 生成于 …”，避免把可视化当成物理结论。
8. **§3 总体架构图里的 WebRTC 与 §36 通信章节不一致**：§36 只有 REST + WebSocket。建议写明：遥测 = WebSocket 上的 `anet.rt.v1`（r27），传输层抽象允许 V1.0 通过 Rust 中继启用 WebTransport（WebSocket 永远保留为回退）；**视频（FPV/吊舱）**单独一条路：V0.x 用 MJPEG/HTTP 或 WebRTC，V1.0 评估 MoQ + WebCodecs（moq-relay）。
9. **§37 更新频率补 QoS 语义**：每个 channel 声明 `priority / order / maxAgeMs`（§3.6.2），高频状态每 1 s 一个关键帧，JSON 状态用 Snapshot（快照 + merge-patch）、事件用 Stream；Gateway 为迟到客户端保留 static 数据与有上限的 temporal 缓冲（§3.6.4）。
10. **§28 DroneState 拆成 reported / desired 两半**（借 Ditto）：`properties` 是飞控上报，`desiredProperties` 是 UI/Agent 指令意图；二者差异驱动 UI 的“执行中”状态与 transitions.dev 过渡；`definition` 字段承载 §31 的 capability 类型，V1.0 的能力发现直接按它查询。
11. **§39 Timeline 补“多时间轴 + latest-at 语义”**：至少 `sim_time`、`tick`、`wall_time` 三条轴；任意组件在时间 t 的值 = 该轴上 ≤ t 的最后一条（Rerun 语义）；研发阶段同步写一份 `.rrd` 供 Rerun 复盘（不进产品 UI）。
12. **§33/§34 技术栈表补四行**：`Atmosphere（High 档）: @takram/three-atmosphere/webgpu（受 three 版本约束）`、`Wind transient: D3Q19 LBM（numba，离线）`、`Dev recording: rerun-sdk`、`Video/Relay（V1.0 可选）: moq-relay + WebCodecs`；并在 §34 前端栈加一句“three 版本由点云与环境两条线共同决定，升级前跑 WebGL2/WebGPU 双后端回归”。
13. **§46 V0.3 “首先以视觉表现为主”补验收标准**：在软件渲染（CI 的 SwiftShader）上，环境层整体增量 ≤ 30 ms/帧（640×360），在真 GPU 上 ≤ 2 ms/帧；风可视化支持 Timeline 精确 seek（方案 A）。
