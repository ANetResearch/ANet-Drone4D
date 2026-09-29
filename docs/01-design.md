# 真实世界无人机数字孪生与多智能体仿真平台  
## 架构设计文档

> **定位**：面向真实街区、山区、园区等物理空间，将无人机实际采集的视频、激光雷达、RTK/IMU 等数据重建为可计算的三维世界，并在该世界中叠加风、雨、雾、云、沙尘等动态自然环境，进一步支持单架及多架无人机的真实动力学仿真、飞行控制、任务规划和多智能体协同。
>
> 核心理念：
>
> **Reality → World → Environment → Simulation → Agent**

---

# 1. 项目目标

本项目不定位为传统意义上的“无人机模拟器”，而是建立一个：

> **Real-World Grounded Physical World Simulator for Autonomous Agents**

即以真实世界为基础的自主智能体物理世界仿真平台。

平台首先通过真实无人机采集视频、LiDAR、RTK、IMU 等数据构建某个真实区域的三维数字空间，再将该空间转换为可渲染、可碰撞、可查询、可仿真的统一 World Model。

在此基础之上进一步增加：

```text
真实世界
    ↓
无人机采集
    ↓
三维重建
    ↓
可计算世界模型
    ↓
动态自然环境
    ↓
无人机动力学
    ↓
单机 / 多机控制
    ↓
多智能体自主协作
```

长期来看，World 中的主体不仅局限于无人机，还可以扩展为：

```text
Drone
UGV
Robot Dog
Autonomous Car
Camera
IoT Sensor
Human Agent
```

因此，整个系统的核心应当是 **World**，而不是 Drone。

---

# 2. 首期硬件平台

首期真实无人机平台采用阿木实验室 **Prometheus 600 / P600**。

该平台本身已经具备较好的真实世界采集、定位和自主飞行能力。

P600 配套方案中包含 MID-360S 三维激光雷达，并已经具备快速构建三维环境地图、基于点云进行 EGO-Swarm 路径规划等能力。:chatgpt-content-reference{index="0"}

机载计算采用 NVIDIA Jetson Orin NX 平台，产品资料中标称算力为 100 TOPS，可承担图像处理、数据预处理、知和部分在线推理任务。:chatgpt-content-reference{index="1"}

P600 软件体系采用 Prometheus V2，并基于 ROS；配套地面站采用 TCP/UDP 与无人机通信，这使其比较容易进一步接入仿真平台和 Web 控制系统。:chatgpt-content-reference{index="2"}

首期建议统一采集以下数据：

```text
P600
│
├── RGB / 吊舱视频
├── MID-360 LiDAR Point Cloud
├── RTK
├── IMU
├── UAV Pose
├── Velocity
├── Attitude
├── Battery
└── Flight Log
```

---

# 3. 总体架构

整体采用**前后端分离 + GPU Server + Simulation Server**的架构。

核心原则：

> **浏览器负责看世界、操作世界；服务器负责计算世界。**

```text
                            ┌─────────────────────────┐
                            │        Browser          │
                            │                         │
                            │ React + Three.js        │
                            │ WebGPU / WebGL2         │
                            │                         │
                            │ 3D World                │
                            │ Drone UI                │
                            │ Weather UI              │
                            │ Mission UI              │
                            │ Timeline                │
                            └────────────┬────────────┘
                                         │
                            HTTP / WebSocket / WebRTC
                                         │
            ┌────────────────────────────┴────────────────────────────┐
            │                                                         │
            │                     Backend                             │
            │                                                         │
            │  ┌───────────────────┐      ┌───────────────────────┐   │
            │  │ World Service     │      │ Simulation Service    │   │
            │  │                   │      │                       │   │
            │  │ Scene             │      │ Drone Dynamics        │   │
            │  │ Point Cloud       │      │ PX4 SITL              │   │
            │  │ Mesh              │      │ Multi-UAV             │   │
            │  │ Semantic          │      │ Mission               │   │
            │  └─────────┬─────────┘      └──────────┬────────────┘   │
            │            │                           │                │
            │  ┌─────────▼─────────┐      ┌──────────▼────────────┐   │
            │  │ Environment       │      │ Agent Runtime         │   │
            │  │                   │      │                       │   │
            │  │ Wind              │      │ ANet                  │   │
            │  │ Rain              │      │ Task Planner          │   │
            │  │ Fog               │      │ Collaboration         │   │
            │  │ Sand              │      │ Capability Discovery  │   │
            │  │ Cloud             │      │                       │   │
            │  └───────────────────┘      └───────────────────────┘   │
            │                                                         │
            └───────────────────────────┬────────────────────────────┘
                                         │
                             Reconstruction Pipeline
                                         │
                 ┌───────────────────────┼────────────────────────┐
                 ↓                       ↓                        ↓
          LingBot-Map                MID-360                  RTK / IMU
          RGB → Geometry             LiDAR                    Pose
                 │                       │                        │
                 └───────────────────────┼────────────────────────┘
                                         ↓
                               Global World Model
```

---

# 4. 核心架构原则

## 4.1 浏览器不是仿真核心

浏览器负责：

```text
Rendering
Interaction
Visualization
Mission Editing
World Editing
Weather Visualization
Telemetry Display
Playback
```

但不负责：

```text
LingBot-Map inference
LiDAR registration
CFD
PX4 flight dynamics
Large scale physics
Multi-UAV dynamics
Heavy AI inference
```

即：

```text
Browser = Visualization Runtime

Server = Physical Runtime
```

这样未来无论底层仿真从 Gazebo 切换至 Isaac Sim、自研动力学引擎或者直接连接真机，Web UI 都不需要推倒重做。

---

# 5. Reality Reconstruction

## 5.1 LingBot-Map

三维重建第一阶段采用：

**Robbyant / LingBot-Map**

作为主要视觉三维重建引擎。

LingBot-Map 属于 streaming 3D reconstruction 模型，可以从长视频序列持续估计：

```text
Camera Pose
Depth
Geometry
Point Cloud
Trajectory
```

特别适合无人机：

```text
UAV Flight Video
        ↓
LingBot-Map
        ↓
Camera Trajectory
        +
Dense Geometry
```

其价值在于不需要首先建设传统的完整 SfM/MVS 流程，就可以较快速地从飞行视频构建连续三维空间。

### 技术定位

LingBot-Map 不作为整个 World Engine，而作为：

> **Visual Reconstruction Engine**

---

# 6. LiDAR 与视觉融合

仅依赖视频重建存在三个问题：

```text
尺度可能存在误差
部分弱纹理区域几何质量较低
真实距离精度不足
```

因此建议：

```text
LingBot-Map
Visual Geometry
      +
MID-360
LiDAR Geometry
      +
RTK
Global Coordinate
      ↓
World Fusion
```

形成：

```text
Video Geometry
          \
           \
LiDAR ------→ Registration → World Geometry
           /
RTK / IMU /
```

建议后端使用：

### Open3D

承担：

```text
Point Cloud Processing
ICP
Registration
Voxelization
Noise Filtering
Normal Estimation
Mesh Reconstruction
Coordinate Transform
```

后续如果点云计算规模进一步提升，可逐步引入：

```text
PCL
CUDA Point Cloud Kernels
cuVSLAM
```

---

# 7. World Model

这是整个系统最重要的一层。

不要把 World 设计成：

```text
world.ply
```

而应该设计成一个完整的世界对象。

建议：

```text
World
│
├── Geometry
│   ├── Point Cloud
│   ├── Mesh
│   ├── Collision Mesh
│   ├── Voxel
│   └── SDF
│
├── Geographic
│   ├── WGS84
│   ├── UTM
│   ├── ENU
│   └── RTK Origin
│
├── Semantic
│   ├── Road
│   ├── Building
│   ├── Tree
│   ├── Mountain
│   ├── Powerline
│   └── Restricted Area
│
├── Environment
│   ├── Wind
│   ├── Rain
│   ├── Fog
│   ├── Cloud
│   └── Sand
│
└── Dynamic Objects
    ├── Drone
    ├── Vehicle
    ├── Human
    └── Robot
```

---

# 8. 世界的两种表达

建议同时维护：

## Geometry World

负责：

```text
Collision
Distance
Navigation
Path Planning
Sensor Simulation
Physics
```

数据类型：

```text
Point Cloud
Mesh
Voxel
SDF
Occupancy Grid
```

---

## Visual World

负责：

```text
Rendering
Presentation
FPV
Visualization
Digital Twin
```

第一阶段可以直接使用：

```text
Point Cloud
```

未来逐渐增加：

```text
Texture Mesh
3D Gaussian Splatting
```

因此最终：

```text
                 WORLD
                  │
          ┌───────┴────────┐
          │                │
    Geometry World     Visual World
          │                │
       Physics           Render
       Collision         PointCloud
       Planning          Mesh
       Sensor            3DGS
```

这是整个架构中非常重要的设计。

---

# 9. Web 3D 技术选型

## 9.1 推荐结论

主技术栈建议：

```text
React
+
Three.js
+
WebGPURenderer
+
WGSL
```

而不是把 Unreal / Unity 作为第一代主 UI。

---

# 10. 为什么选择 Web

Web 场景非常适合项目未来产品形态。

用户只需要访问：

```text
https://simulation.xxx.com/world/hefei-01
```

即可进入三维空间。

能够直接进行：

```text
旋转
缩放
漫游
飞行
选无人机
规划航线
设置风速
设置天气
播放任务
观察 Sensor
查看 Point Cloud
回放真实飞行
```

天然适合：

```text
多人访问
远程演示
教学
科研
运维
在线实验
```

---

# 11. Three.js vs Cesium vs Unreal

| 技术 | 定位 | 本项目建议 |
|---|---|---|
| Three.js | Web 3D Engine | **核心引擎** |
| WebGPU | GPU Rendering / Compute | **核心 GPU Backend** |
| CesiumJS | GIS / 地球 / 超大空间 | 后续增加 |
| Potree | 大规模点云 | 点云能力参考/集成 |
| Unreal Engine | 高保真仿真 | 后续可作为专业仿真 Backend |
| Unity | 通用 3D Engine | 暂不优先 |
| Isaac Sim | 机器人高保真仿真 | 第二阶段 Backend |
| Gazebo | ROS/PX4仿真 | 飞控 Backend |

第一阶段建议：

> **Three.js + WebGPU**

---

# 12. WebGPU 的定位

浏览器能够直接访 GPU。

因此 WebGPU 可以承担：

```text
Point Rendering
Particle Simulation
Wind Visualization
Rain
Snow
Dust
Cloud
Fog
Vector Field
Heatmap
Trajectory
Large Drone Swarm Rendering
```

例如：

```text
300,000 particles
       ↓
GPU Compute Shader
       ↓
Wind Field
       ↓
GPU Rendering
```

这些计算无需 CPU 每一帧处理几十万个粒子。

---

# 13. WebGPU 不做什么

WebGPU 并不等同于 CUDA。

不应该试图让浏览器运行：

```text
LingBot-Map
OpenFOAM
大型 CFD
PX4
复杂动力学
千万规模 Agent AI
```

这些应该在服务器运行。

因此：

```text
                      GPU

         Browser                  Server
            │                        │
         WebGPU                    CUDA
            │                        │
  Visualization / FX       AI / Physics / CFD
```

---

# 14. 点云 Web 渲染架构

这是整个 Web 系统最容易遇到性能问题的地方。

不能：

```text
500 million points
        ↓
Browser
        ↓
Three.js
```

全部一次加载。

必须建立空间分块。

推荐：

```text
World Point Cloud
        ↓
Spatial Partition
        ↓
Octree
        ↓
LOD
        ↓
Streaming
```

浏览器根据：

```text
Camera Position
Camera FOV
Distance
Screen Size
```

自动确定需要加载哪些节点。

例如：

```text
                  Root
                   │
        ┌──────────┼──────────┐
      Level 1   Level 1    Level 1
        │
     Level 2
        │
     Level 3
```

远处：

```text
10K points
```

近处：

```text
1M points
```

只有相机附近才加载高精度点云。

---

# 15. 点云数据格式

第一阶段：

```text
PLY
PCD
LAZ
```

内部处理。

Web 端则转换为：

```text
Binary Tile
+
Octree Index
```

后期建议兼容：

```text
3D Tiles
```

这样未来可以与：

```text
Cesium
GIS
City Model
Satellite Data
DEM
```

结合。

---

# 16. Three.js 场景结构

建议：

```text
Scene
│
├── WorldLayer
│   ├── PointCloud
│   ├── Mesh
│   ├── Terrain
│   └── Semantic
│
├── EnvironmentLayer
│   ├── Sky
│   ├── Cloud
│   ├── Rain
│   ├── Fog
│   ├── Sand
│   └── Wind
│
├── DroneLayer
│   ├── UAV01
│   ├── UAV02
│   └── UAV03
│
├── SensorLayer
│   ├── Camera FOV
│   ├── LiDAR FOV
│   └── Radar FOV
│
├── MissionLayer
│   ├── Waypoints
│   ├── Path
│   ├── Target
│   └── Region
│
└── DebugLayer
    ├── Coordinate
    ├── Velocity
    ├── Force
    └── Wind Vector
```

---

# 17. Environment Engine

环境不应该只是一组 Shader。

需要明确区分：

```text
Environment Visualization

和

Environment Physics
```

---

# 18. 环境场模型

整个自然环境建议统一表示为：

\[
E(x,y,z,t)
\]

即一个：

> **4D Physical Field**

例如：

```text
Environment(x,y,z,t)
│
├── Wind
├── Temperature
├── Humidity
├── Visibility
├── Pressure
├── Rain
├── Snow
├── Dust
└── Cloud
```

任何无人机在任何时刻都可以查询：

```text
environment.query(
    x,
    y,
    z,
    timestamp
)
```

---

# 19. 风场系统

建议定义：

\[
W(x,y,z,t)
=
(u,v,w)
\]

即每个空间位置都有三维风速。

第一阶段：

```text
Constant Wind
+
Gust
+
Turbulence
```

第二阶段：

```text
Terrain-aware Wind
```

第三阶段：

```text
CFD Wind
```

---

# 20. 风场分级实现

### Level 0

```text
wind_speed
wind_direction
```

例如：

```text
8 m/s
270°
```

---

### Level 1

加入：

```text
gust
random turbulence
vertical wind
```

---

### Level 2

加入：

```text
Building Effect
Mountain Effect
Valley Wind
Updraft
Downdraft
Wake
```

---

### Level 3

采用：

```text
OpenFOAM
```

离线计算 CFD。

例如：

```text
wind/
├── 000_05.vdb
├── 000_10.vdb
├── 000_15.vdb
├── 045_05.vdb
├── 045_10.vdb
├── 090_05.vdb
└── ...
```

运行时插值：

```text
windDirection = 32°
windSpeed = 8.5 m/s

↓ interpolation

3D Wind Field
```

这样无需实时运行 CFD。

---

# 21. 风场的 Web 可视化

WebGPU 可以直接绘制：

```text
Wind Particle
Streamline
Arrow
Heatmap
```

例如：

```text
        → → → →
    ↗
 → → →   █████
 → →     █████ Building
    ↘    █████
        ↓↓↓↓↓
```

这一部分完全可以由 WebGPU 完成。

---

# 22. Rain Engine

雨包含两个层面。

### Visual

```text
GPU Rain Particles
Wet Surface
Cloud
Atmospheric Scattering
```

### Physics

```text
Visibility
Camera Noise
LiDAR Noise
Flight Drag
Sensor Reliability
```

---

# 23. Fog Engine

Fog 不应只是：

```text
scene.fog = xxx
```

还应该成为传感器环境参数。

例如：

```text
Fog
│
├── Visual
│
├── RGB
│   └── contrast ↓
│
├── LiDAR
│   ├── range ↓
│   ├── dropout ↑
│   └── noise ↑
│
├── Thermal
│
└── Radar
```

---

# 24. 沙尘

同样分成：

```text
Particle Simulation
+
Optical Attenuation
+
LiDAR Degradation
+
Flight Effect
```

WebGPU 负责：

```text
sand particle
```

Server 负责：

```text
sensor / dynamics
```

---

# 25. 云

云分为三档。

### Level 1

Billboard / Texture Cloud。

### Level 2

3D Noise Volume。

### Level 3

Ray-marched Volumetric Cloud。

第一阶段推荐 Level 2。

---

# 26. Drone Simulation

无人机物理层不建议自行从零开发。

建议采用：

> **PX4 SITL**

整体：

```text
Drone Model
      ↓
Flight Dynamics
      ↓
PX4 SITL
      ↓
MAVLink
      ↓
ROS / MAVSDK
      ↓
Simulation Service
```

---

# 27. P600 Digital Twin

为 P600 建立：

```text
P600 Digital Twin
│
├── Geometry
├── Mass
├── Inertia
├── Motor
├── Propeller
├── Battery
├── Flight Controller
├── Camera
├── LiDAR
├── RTK
└── Payload
```

然后：

```text
真实 P600
```

和

```text
Virtual P600
```

尽可能保持一致。

---

# 28. 无人机状态模型

统一定义：

```text
DroneState
{
    id

    position
    orientation

    velocity
    acceleration

    angular_velocity

    battery

    flight_mode

    gps

    health

    mission

    sensors
}
```

通过 WebSocket 推送：

```text
Simulation
    ↓
10 ~ 50 Hz
    ↓
Web UI
```

---

# 29. 多无人机架构

每架无人机独立维护：

```text
PX4 SITL #1
PX4 SITL #2
PX4 SITL #3
...
PX4 SITL #N
```

世界里：

```text
Drone01
Drone02
Drone03
...
```

每一架都有独立：

```text
State
Sensor
Mission
Controller
Agent
```

---

# 30. 控制模式

第一阶段：

```text
Takeoff
Land
GoTo
FollowPath
Orbit
Hover
ReturnHome
```

第二阶段：

```text
Area Coverage
Search
Tracking
Formation
Collision Avoidance
Swarm
```

---

# 31. Agent Network 接入

多机仿真成熟之再引入 Agent Network。

每一架无人机成为：

> **Physical Agent**

例如：

```text
Drone A
RGB + Zoom

Drone B
Thermal

Drone C
LiDAR

Drone D
Relay
```

每架 Drone 对外描述自己的 capability。

```text
capability:
    thermal.imaging
    rgb.zoom
    lidar.mapping
    relay.communication
```

---

# 32. Multi-Agent Workflow

例如山区搜救：

```text
Drone A
发现疑似目标
      ↓
confidence = 0.42
      ↓
发布任务
"Need thermal verification"
      ↓
ANet
      ↓
发现 Drone B
      ↓
Drone B 接受
      ↓
调整路线
      ↓
Thermal Observation
      ↓
结果回传
```

这时 Agent Network 不再只是“无人机之间通信”。

而成为：

> **Capability-based Physical Agent Network**

---

# 33. Backend 技术栈

建议：

| 模块 | 技术 |
|---|---|
| Reconstruction | LingBot-Map |
| Point Processing | Open3D |
| LiDAR | Livox MID-360 |
| Coordinate | RTK / ENU / WGS84 |
| API | FastAPI |
| Real-time | WebSocket |
| Message | ROS2 / DDS |
| UAV | PX4 |
| UAV API | MAVSDK |
| Simulation | Gazebo |
| CFD | OpenFOAM |
| GPU | CUDA |
| Agent | ANet |

---

# 34. Frontend 技术栈

建议：

| 模块 | 技术 |
|---|---|
| Web Framework | React |
| Language | TypeScript |
| 3D Engine | Three.js |
| Renderer | WebGPURenderer |
| GPU Shader | WGSL |
| Fallback | WebGL2 |
| UI | shadcn/ui |
| State | Zustand |
| API | REST |
| Realtime | WebSocket |
| Point Cloud | Custom Octree / Potree concepts |
| GIS | Cesium later |

---

# 35. Simulation Backend

第一阶段：

```text
PX4
+
Gazebo
```

因为重点是：

```text
Flight Control
Planning
Multi Drone
ROS
```

第二阶段增加：

```text
Isaac Sim
```

主要用于：

```text
High Fidelity Rendering
Camera Simulation
LiDAR Simulation
Radar Simulation
Sensor Physics
```

最终允许：

```text
Simulation Backend

├── Gazebo
│
└── Isaac Sim
```

共同使用同一个：

```text
World Model
```

---

# 36. 前后端实时通信

建议：

```text
REST
```

负责：

```text
Scene
Mission
Configuration
File
```

WebSocket 负责：

```text
Drone State
Weather
Simulation Time
Sensor State
Event
```

数据流：

```text
PX4
 ↓
ROS2
 ↓
Simulation Gateway
 ↓
WebSocket
 ↓
Browser
```

---

# 37. 世界状态更新频率

建议：

```text
Physics

100~1000 Hz
```

飞控：

```text
100~400 Hz
```

Backend State：

```text
50~100 Hz
```

WebSocket：

```text
10~50 Hz
```

Web Rendering：

```text
60 FPS
```

不需要每一次物理更新都推给浏览器。

---

# 38. UI 设计

整体采用数字沙盘。

左侧：

```text
WORLD

Scene
 └ Hefei Campus

Layers
 ☑ Point Cloud
 ☑ Terrain
 ☑ Building
 ☑ Semantic
 ☑ LiDAR

ENVIRONMENT

Wind
8.2 m/s

Direction
NW

Rain
22 mm/h

Fog
0.21

Sand
0

Cloud
35%
```

右侧：

```text
DRONES

P600-01
Flying

Altitude
82.3 m

Speed
7.2 m/s

Battery
78%

────────────

P600-02
Ready

────────────

P600-03
Mission
```

---

# 39. Timeline

底部增加：

```text
14:32:00 ───────────────────────── 14:45:00

▶

×1
×2
×5
×10
```

支持：

```text
Pause
Play
Replay
Fast Forward
Seek
```

---

# 40. Drone Interaction

选中无人机后提供：

```text
Follow
FPV
Thermal
LiDAR
Trajectory
Camera FOV
Wind Force
Velocity
Mission
```

可以切换：

```text
Third Person
FPV
Bird Eye
Free Camera
```

---

# 41. World 文件结构

每个真实世界场景建议形成独立 World Package：

```text
worlds/
└── hefei-campus/
    │
    ├── metadata.json
    │
    ├── coordinate.json
    │
    ├── geometry/
    │   ├── pointcloud/
    │   ├── mesh/
    │   ├── collision/
    │   └── voxel/
    │
    ├── semantic/
    │
    ├── environment/
    │   ├── wind/
    │   ├── weather/
    │   └── atmosphere/
    │
    ├── reconstruction/
    │   ├── cameras/
    │   └── trajectory/
    │
    ── visual/
        ├── texture/
        └── gaussian/
```

---

# 42. GitHub Repo 架构

建议项目名暂时：

```text
drone-world
```

Repo：

```text
drone-world/
│
├── apps/
│   ├── web/
│   ├── api/
│   └── simulator/
│
├── reconstruction/
│   ├── lingbot/
│   ├── lidar/
│   ├── registration/
│   └── fusion/
│
├── world/
│   ├── geometry/
│   ├── pointcloud/
│   ├── voxel/
│   ├── sdf/
│   ├── semantic/
│   └── georef/
│
├── environment/
│   ├── wind/
│   ├── rain/
│   ├── fog/
│   ├── cloud/
│   ├── sand/
│   └── weather/
│
├── vehicles/
│   ├── p600/
│   └── common/
│
├── simulation/
│   ├── px4/
│   ├── gazebo/
│   └── isaac/
│
├── sensors/
│   ├── rgb/
│   ├── thermal/
│   ├── lidar/
│   ├── radar/
│   ├── imu/
│   └── gnss/
│
├── swarm/
│   ├── mission/
│   ├── planning/
│   └── avoidance/
│
└── agent/
    └── anet/
```

---

# 43. MVP 开发范围

第一阶段千万不要同时做：

```text
CFD
3DGS
Multi-Agent
Radar
完整天气
100架无人机
```

首个版本应该只实现一条完整链路。

建议：

```text
P600 Video
     ↓
LingBot-Map
     ↓
Point Cloud
     ↓
Octree
     ↓
Three.js/WebGPU
     ↓
Interactive World
     ↓
P600 3D Model
     ↓
WebSocket
     ↓
Drone Movement
```

最终 Demo：

> 上传/导入一次真实飞行采集的视频 → 自动重建真实 3D 场景 → 浏览器进入该场景 → 添加虚拟 P600 → 控制无人机飞行。

---

# 44. V0.1

目标：

> **Reality → Web World**

功能：

```text
LingBot-Map reconstruction
Point Cloud
Web Viewer
Camera
World Navigation
P600 Model
```

---

# 45. V0.2

目标：

> **Drone Simulation**

增加：

```text
PX4 SITL
Drone State
Takeoff
Land
GoTo
Trajectory
WebSocket
```

---

# 46. V0.3

目标：

> **Environment**

增加：

```text
Wind
Rain
Fog
Cloud
Sand
```

首先以视觉表现为主。

---

# 47. V0.4

目标：

> **Physical Environment**

增加：

```text
Wind → Drone Force

Fog → Sensor

Rain → Sensor

Environment →
Flight Dynamics
```

---

# 48. V0.5

目标：

> **Real World Fusion**

增加：

```text
MID-360
RTK
IMU
```

形成真正 metric 级 World。

---

# 49. V0.6

目标：

> **Multi-UAV**

增加：

```text
Drone01
Drone02
Drone03
...
```

支持：

```text
Swarm
Formation
Planning
Avoidance
```

---

# 50. V1.0

目标：

> **Physical Multi-Agent World**

增加：

```text
ANet

Capability Discovery

Task Assignment

Agent Collaboration

Heterogeneous UAV

Sensor Collaboration
```

最终形成：

```text
Real World
     ↓
World Model
     ↓
Physical Environment
     ↓
Digital Twin
     ↓
Autonomous Agents
     ↓
Agent Network
```

---

# 51. 最终技术架构结论

整个项最核心的技术路线建议确定为：

```text
REALITY
│
├── P600
├── Video
├── MID-360
├── RTK
└── IMU
      │
      ▼
RECONSTRUCTION
│
├── LingBot-Map
├── Open3D
├── LiDAR Fusion
└── Georeference
      │
      ▼
WORLD MODEL
│
├── Point Cloud
├── Mesh
├── Voxel
├── SDF
├── Semantic
└── 3D Tiles
      │
      ▼
WORLD RUNTIME
│
├── Geometry
├── Environment
├── Sensor
└── Dynamic Object
      │
      ├─────────────────┐
      ▼                 ▼
WEB RUNTIME        SIMULATION
│                  │
├ Three.js         ├ PX4
├ WebGPU           ├ Gazebo
├ WGSL             ├ ROS2
├ React            └ Isaac Sim
│
│
└─────────────┬───────────────
              ▼
        DRONE / AGENT
              │
        ┌─────┼─────┐
        ↓     ↓     ↓
      UAV01 UAV02 UAV03
        │     │     │
        └─────┼─────┘
              ↓
             ANet
```

其中各技术的职责应该严格分开：

> **LingBot-Map 构建世界；**
>
> **Open3D 整理和融合世界；**
>
> **Three.js + WebGPU 展示和交互世界；**
>
> **Environment Engine 描述世界中的自然环境；**
>
> **PX4 / Gazebo 模拟无人机在世界中的真实运动；**
>
> **Isaac Sim 提供后续高保真 Sensor Simulation；**
>
> **ANet 负责多个 Physical Agent 在这个世界中的自主发现、协同和任务执行。**

最终整个系统真正应该沉淀下来的核心资产不是某一个无人机模型，也不是某一个仿真器，而是：

# **World Runtime**

它向下连接真实世界的数据和物理规律，向上连接无人机、机器人及智能体。

从这个角度，这个项目可以最终定义为：

> **A Real-World Grounded 4D Physical World Runtime for Autonomous Agents**

而无人机是第一个最适合验证这套 World Runtime 的具身智能载体。
