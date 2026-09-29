对照你附件里的链路（Reality → Reconstruction → World → Environment → Simulation → Agent），下面是一份**值得 clone 到本地读代码**的仓库清单。

优先度：

- **P0**：V0.1–V0.3 马上用
- **P1**：V0.4–V0.6 再读
- **P2**：V1.0 / 高保真 / 4D 外观

---

## 0. 建议本地目录

```bash
drone-world-refs/{recon,lidar,world,web3d,weather,sim,swarm,gis}
```

后面按这个目录分组。

---

## 1. Reality / Reconstruction（视频 → 几何）

对应文档 §5、V0.1。

| Pri | Repo | 为什么必须看 |
|---|---|---|
| P0 | https://github.com/Robbyant/lingbot-map | 你选定的视觉重建引擎。重点看 `demo.py`、viser 输出、pose/depth/point 数据结构、`--mask_sky`、windowed 长视频。 |
| P0 | https://github.com/nerfstudio-project/viser | LingBot-Map 官方 viewer。直接抄它的点云流式推送、相机轨迹、Web 交互协议。 |
| P1 | https://github.com/facebookresearch/vggt | 同代前馈多视几何，对照 LingBot-Map 的 pose/depth 接口该怎么规范化。 |
| P1 | https://github.com/colmap/colmap | 尺度、外参、稀疏重建的工业基准。雷达/RTK 配不准时拿它做对照。 |
| P1 | https://github.com/cvg/glomap | COLMAP 的更快全局 SfM，航拍街区级离线兜底。 |
| P2 | https://github.com/nerfstudio-project/nerfstudio | 以后 Visual World 走 NeRF/3DGS 时的训练/导出管线。 |
| P2 | https://github.com/graphdeco-inria/gaussian-splatting | 3DGS 原版，点云之后的 Visual World 升级路径。 |
| P2 | https://github.com/nerfstudio-project/gsplat | 更快的 3DGS rasterizer，和网页 splat 导出相关。 |

---

## 2. LiDAR / 配准 / 地理对齐（MID-360 + RTK）

对应文档 §6、V0.5。P600 带 MID-360S，这一层比再找一个建图论文更重要。

| Pri | Repo | 为什么必须看 |
|---|---|---|
| P0 | https://github.com/Livox-SDK/Livox-SDK2 | MID-360/360S 驱动底层。 |
| P0 | https://github.com/Livox-SDK/livox_ros_driver2 | ROS2 点云/`CustomMsg`，P600 实采第一步。 |
| P0 | https://github.com/hku-mars/FAST_LIO | MID-360 建图事实标准。看 ikd-Tree、scan-to-map、地图保存。 |
| P0 | https://github.com/isl-org/Open3D | 你文档里定的融合库：ICP、体素、网格、坐标变换、滤波。v0.20 已能画 Gaussian。 |
| P0 | https://github.com/koide3/fast_gicp | 视觉点云 ↔ LiDAR 地图配准。CPU/CUDA GICP，World Fusion 的核心算子。 |
| P1 | https://github.com/hku-mars/FAST-LIVO2 | RGB + LiDAR + IMU 紧耦合。比“先 LingBot 再 ICP”更强，但是重，作对照。 |
| P1 | https://github.com/hku-mars/LiDAR_IMU_Init | 雷达-IMU 外参/时延标定，P600 真机必做。 |
| P1 | https://github.com/koide3/hdl_graph_slam | 图优化 + GPS 约束，学怎么把 RTK 钉进点云图。后续作者推荐 https://github.com/koide3/glim 。 |
| P1 | https://github.com/koide3/small_gicp | fast_gicp 后继，依赖更少，适合嵌进 World Service。 |
| P1 | https://github.com/Liansheng-Wang/faster_lio_localization | **无人机 + Mid-360** 建图/重定位，场景和你最像。 |
| P1 | https://github.com/liangheming/FASTLIO2_ROS2 | FAST-LIO2 的 ROS2 + 回环 + 重定位，贴近 ProSim/ROS2。 |
| P2 | https://github.com/hku-mars/r3live | 带颜色的 LiDAR-视觉地图，Visual World 上色参考。 |

---

## 3. World Model / 点云切片 / 地形

对应文档 §7–8、§14–15、§41。

| Pri | Repo | 为什么必须看 |
|---|---|---|
| P0 | https://github.com/potree/PotreeConverter | 把 LAS/PLY 切成可流式八叉树。Web 点云的预处理标准件。 |
| P0 | https://github.com/PDAL/PDAL | 点云管道：滤波、重投影、WGS84/UTM、与 DEM 叠加。World Package 离线工具。 |
| P1 | https://github.com/CesiumGS/3d-tiles-tools | 点云/网格 → 3D Tiles，以后接 Cesium/GIS。 |
| P1 | https://github.com/CesiumGS/3d-tiles | 规范本身，设计 `worlds/hefei-campus/` 瓦片格式时读。 |
| P1 | https://github.com/LAStools/LAStools | LAZ 读写、抽稀、分类。注意部分工具许。 |
| P2 | https://github.com/CesiumGS/cdb-to-3dtiles | 大规模地形/影像/建筑切片思路，山区+街区参考。 |
| P2 | https://github.com/yeyan00/potree23dtiles | Potree 八叉树转 3D Tiles 的小工具。 |

---

## 4. Web 3D / 渐进式点云 / UI

对应文档 §9–16、§34、§38–40。你选了 React + Three.js + WebGPU，下面按这个选。

| Pri | Repo | 为什么必须看 |
|---|---|---|
| P0 | https://github.com/mrdoob/three.js | 引擎本体。必看 `examples/webgpu_volume_cloud.html` 和 WebGPURenderer。 |
| P0 | https://github.com/potree/potree | 十亿级点云 LOD 的完整实现。学 octree、point budget、EDL、拾取。商用需注意许可。 |
| P0 | https://github.com/tentone/potree-core | **能直接嵌进 Three.js/React 的 Potree 内核**，比整站 Potree 更适合你的 WorldLayer。 |
| P0 | https://github.com/pnext/three-loader | TypeScript 版 Potree loader，和你的 TS 前端更贴。 |
| P0 | https://github.com/pmndrs/react-three-fiber | React 挂 Three 场景的标准写法。 |
| P0 | https://github.com/pmndrs/drei | 相机、Gizmo、环境、控件，少写样板。 |
| P1 | https://github.com/CesiumGS/cesium | 地球级 GIS、地形、3D Tiles。文档写“后续加 Cesium”，现在先读数据模型。 |
| P1 | https://github.com/m-schuetz/Potree-Next | WebGPU 重写的 Potree，看下一代点云 raster。 |
| P1 | https://github.com/sparkjsdev/spark | Three.js 里画 3DGS，Visual World 从点云升高斯时用。 |
| P1 | https://github.com/visgl/deck.gl | 大规模地理图层 + 点云/轨迹，和 React 很熟。 |
| P2 | https://github.com/playcanvas/supersplat-viewer | 高质量网页 splat viewer。 |
| P2 | https://github.com/antimatter15/splat | 最早也最薄的 WebGL splat，理解 GPU sort。 |

Foxglove 适合当调试面板，不是主沙盘：https://github.com/foxglove/studio

---

## 5. 风 / 雨 / 雾 / 云 / 沙（视觉 + 物理场）

对应文档 §17–25、V0.3–V0.4。务必分开两套仓库：**网页外观** 和 **服务端风场**。

### 5.1 网页外观（WebGPU / Three.js）

| Pri | Repo | 看什么 |
|---|---|---|
| P0 | https://github.com/SkyeShark/Eanpa-Sky | Three.js WebGPU 体积云 + 天气状态机 + 雨打湿地面。和你的 EnvironmentLayer 最像。 |
| P0 | https://github.com/CK42BB/procedural-weather-threejs | 雨/雪/雾/沙尘/闪电的 GPU 粒子，WebGPU compute + WebGL2 fallback。 |
| P0 | https://github.com/jeantimex/procedural-clouds | WebGPU 体积云：compute 填 3D density + ray march。 |
| P1 | https://github.com/Token-Gremlin/natural-disasters | 纯 GPU 程序化风雨云雷，学天气图驱动体积云，不要抄成仿真核心。 |
| P1 | https://github.com/mrdoob/three.js/blob/dev/examples/webgpu_volume_cloud.html | 官方体积云最小实现。 |

### 5.2 物理风场 / 地形风（服务器，对应你的 Level 2–3）

| Pri | Repo | 看什么 |
|---|---|---|
| P0 | https://github.com/firelab/windninja | **复杂地形诊断风场**，输入 DEM + 来向风速，输出栅格风矢量。山区沙盘 Level 2 首选，不必一上来 OpenFOAM。 |
| P1 | https://github.com/NCAR/FastEddy-model | GPU LES 边界层，比 WindNinja 更物理，也更重。 |
| P1 | https://github.com/OpenFOAM/OpenFOAM-dev | 你文档 Level 3。只学怎么离线出 VDB/体素风场再插值，不要实时跑。 |
| P2 | https://github.com/AcademySoftwareFoundation/openvdb | 风场/云密度体数据格式，和文档里 `wind/000_05.vdb` 对得上。 |

---

## 6. 4D 动态世界（可选，不要进 MVP）

对应文档里 Environment 的 \(E(x,y,z,t)\) 和未来 Visual World。

| Pri | Repo | 看什么 |
|---|---|---|
| P1 | https://github.com/fudan-zvg/4d-gaussian-splatting | 原生 4D 高斯，动态外观。 |
| P1 | https://github.com/JonathonLuiten/Dynamic3DGaussians | 高斯随时间运动，适合“回放真实飞行 + 动态场景”。 |
| P2 | https://github.com/hustvl/4DGaussians | 另一条 4D-GS（变形场）实现，对比即可。 |

MVP 不要训练 4DGS。先用时间回放位姿 + 静态 World。

---

## 7. Drone / PX4 / 多机仿真

对应文档 §26–30、§35、V0.2 / V0.6。和阿木 P600 对齐的优先级最高。

| Pri | Repo | 为什么必须看 |
|---|---|---|
| P0 | https://github.com/amov-lab/Prometheus | P600 软件栈。看控制话题、规划、编队、地面站通信。注意开源主体仍偏 ROS1，真机完整代码部分绑定整机。 |
| P0 | https://github.com/PX4/PX4-Autopilot | SITL、多机、风扰、Gazebo/gz 插件。所有虚拟 P600 的飞控内核。 |
| P0 | https://github.com/mavlink/MAVSDK | 文档里的 UAV API。Python 控起飞/GoTo，比直接啃 MAVLink 快。 |
| P0 | https://github.com/TannerGilbert/PX4-Multiagent-Simulation | ROS2 + Gazebo 多机生成、雷达/相机、Offboard。V0.2 脚手架。 |
| P1 | https://github.com/robin-shaun/XTDrone | 国内用得最多的 PX4+ROS+Gazebo 教学/科研平台。 |
| P1 | https://github.com/andy-zhuo-02/XTDrone2 | ROS2 + Gazebo Harmonic + PX4 1.15，更接近 ProSim 新栈。 |
| P1 | https://github.com/microsoft/AirSim | UE 高保真、天气、多机。ProSim 早期也吃过这套。 |
| P1 | https://github.com/gazebosim/gz-sim | Gazebo Harmonic 本体，自定义 World/传感器。 |
| P1 | https://github.com/mavlink/mavros | 若仍走 ROS1/MAVROS；ROS2 优先 XRCE-DDS。 |
| P2 | https://github.com/AntoniSHBK/px4_multi_drone_sim | Docker 一键多机，快速搭环境。正确地址：https://github.com/AntonSHBK/px4_multi_drone_sim |
| P2 | https://github.com/ctu-mrs/mrs_uav_system | 成熟多机科研栈，看状态机和安全层。 |

ProSim / PrometheusSim 本身不是完整公开 Git 单体，安装包走阿木 Wiki。仿真接口仍以 Prometheus + PX4 为准。

---

## 8. 规划 / 集群 / 避障

对应文档 §30、V0.6。P600 资料里已有 EGO-Swarm。

| Pri | Repo | 为什么必须看 |
|---|---|---|
| P0 | https://github.com/ZJU-FAST-Lab/ego-planner-swarm | 单机/多机轨迹规划，阿木方案同源。有 `ros2_version` 分支。 |
| P1 | https://github.com/HKUST-Aerial-Robotics/Fast-Planner | 老牌无人机规划。 |
| P1 | https://github.com/spirit0609/Fast-LIO2_Ego-Planner | **Mid-360 + FAST-LIO2 + Ego-Planner** 一条链，最接近 P600 实机规划。 |
| P1 | https://github.com/artastier/PX4_Swarm_Controller | PX4 + ROS2 领航-跟随。 |
| P2 | https://github.com/Apoorv-1009/PX4-Aerial-Swarm-Reconstruction | 多机扫城市场景 + 点云可视化。 |

---

## 9. 后端 / 实时通信 / 数字孪生壳

对应文档 §33、§36–37。

| Pri | Repo | 为什么必须看 |
|---|---|---|
| P0 | https://github.com/RobotWebTools/rosbridge_suite | ROS ↔ WebSocket，网页控仿真的最快桥。 |
| P1 | https://github.com/foxglove/ws-protocol | 比 rosbridge 更现代的机器人可视化协议。 |
| P1 | https://github.com/mavlink/mavlink | 消息定义，自研 Gateway 时查。 |
| P2 | https://github.com/eclipse-zenoh/zenoh | 以后真机/仿真多节点传输可选项。 |

API 框架用 FastAPI 即可，不必为它单独 clone 教学仓。

由于我们是科研用，不是商用，所以可以忽略所有license说明。
