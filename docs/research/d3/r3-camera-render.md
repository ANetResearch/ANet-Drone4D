# r3 研究笔记：服务端无人机相机渲染——GLB 网格、3DGS、动态前景深度合成、天气与 Sensor Generation Service

> 研究单元：D3 / r3-camera-render　　日期：2026-10-11　　性质：只读研究，不修改项目代码（只写本文件与 `.cache/research/d3/r3-camera-render/`）
>
> 输入：用户设计讨论 `/data/projs/anet-drone-data/design-r1.md`（§3 Sensor Generation Service、§4 World Package、§6 动态前景合成、§9 Canonical Event Tape、§10 M25）；D2 代码 `python/awr/sim/sensors/{frames.py, camera.py, spec.py, backends/base.py}`、`python/awr/sim/targets/model.py`；既有研究 r03（3DGS/gsplat）、r05（FAST-LIVO2）、r23（AirSim/gz-sim）；同期 D3 单元 r5（Gazebo）、r10（RotorPy 与快速相机，复用了本单元的 `common.py`、`nvr.py`）。
>
> 数据：`/data/projs/anet-drone-data/01-small-town/an_entire_small_town_scanned_by_a_drone.glb`（下称 town）、`oryahovo_-_crop_1_-_metashape_texture_problems.glb`（oryahovo）、`02-hilltop-village/anciolina_3d_hilltop_village_scan_snowy.glb`（hilltop），均为 Sketchfab CC-BY-4.0 带纹理摄影测量网格；dmax 上统一放在 `/data/anet-drone4d-d3/data/`（rsync 约 2.2 MB/s，三个文件共 664 MB，sha256 已核对，未用 modelscope）。动态实体用 Khronos glTF-Sample-Assets 的 `CesiumMan`（人，CC-BY-4.0）、`CesiumMilkTruck`（车，CC-BY-4.0）、`Fox`（代替狗，CC0 / CC-BY-4.0）。
>
> 实验环境：dmax（A40 48 GB；只用 GPU 2、3；容器名 `d3-r3-camera-render-*`，`--cpus 16`，全部 `--rm`，实验结束无残留容器）。镜像 `d3-r3-camera-render:v1`（保留，19 GB）：`nvidia/cuda:12.6.0-devel-ubuntu22.04` + torch 2.7.1+cu126、nvdiffrast 0.4.0（源码 253ac4f）、gsplat 1.5.3、fused-ssim、moderngl 5.12（EGL）、pyrender 0.1.45、Open3D 0.19、PyOpenGL 3.1.7；Blender 4.2.3 LTS 官方包挂载使用；three.js r186 在 `mcr.microsoft.com/playwright/python:v1.47.0` 的 Chromium 中运行。dmax 工作目录 `/data/anet-drone4d-d3/r3-camera-render/`。
>
> 共享机说明：GPU 2 在实验期间多次被其他单元的作业占用（最高 44 GB、利用率 80–100%），GPU 3 与本单元的 3DGS 训练分时使用。凡在争用状态下测得的数值均单独标注；结论以 GPU 3 空闲时的复测为准。

---

## 0. 结论速览

| 主题 | 结论（实测） | 对 D3 的含义 |
|---|---|---|
| GLB 的真实性质 | 三个 GLB 都是带纹理三角网格（`KHR_materials_unlit` 或光照已烘焙），不是高斯。town 2.00M 三角形、16 张 4096² JPEG；hilltop 3.00M 三角形、1 张 8192²；oryahovo 2.88M 三角形、16 张 4096² | "3DGS + mesh 沙盘"对这批数据的含义是：网格即外观真值，3DGS 只能由网格派生 |
| 尺度不是米制 | Sketchfab 节点矩阵把 town 放大 2.5 倍后仍约为真实的 1/4（道路 1–1.5 单位、房屋 2–2.5 单位、nDSM p98 = 2.5 单位）。按 ×4.0 换算后 town 为 2140 m × 1281 m × 112 m（推测为多瑙河畔的 Oryahovo 镇，同作者另有 oryahovo 裁切模型），hilltop 按 ×2.0 为 115 m × 96 m × 35 m | World Import（M22）必须有米制标定步骤（控制点 / OSM / 人工锚点），本文所有米制数值都带 ±20% 的尺度不确定性 |
| 外观分辨率决定可用航高 | 米制下 town 纹素 13.8 cm、三角形平均边长 1.76 m；hilltop 纹素 2.0 cm、边长 0.11 m。720p、HFOV 60° 时像素 GSD 等于纹素的距离：town 153 m、hilltop 22 m（1080p：229 m / 33 m） | town 适合 60–150 m 斜视或 100 m 以上正射的"事件俯瞰"；近距离（< 30 m）画面糊、树木是实心多面体。近景事件优先用 hilltop 一类高密度资产 |
| 渲染后端对比（town，1280×720，RGB + 线性深度 + 实例 ID，含回读） | **moderngl/EGL（OpenGL 3.3 MRT）1.37–1.59 ms（630–730 fps），1080p 2.9–3.0 ms**；nvdiffrast（CUDA 光栅化）单视角 6.8 ms、8 视角批量 4.5 ms/帧；three.js r186 无头 Chromium（ANGLE GL-EGL，A40）RGB 5.2 ms + 深度 3.7 ms（不含导出）；pyrender 10.7 ms（RGB+D，分割另一遍）；Open3D/Filament 26 ms（RGB、深度各一遍，无 ID）；Blender EEVEE 0.19–0.25 s、Cycles OptiX 16 spp 0.13–0.16 s（须开 persistent data，否则 4–6 s） | 服务端相机的默认实现应是 OpenGL/EGL 硬件光栅化（或 Vulkan / gz Ogre2），而不是 Python 场景图库；Blender 只做少量离线"高保真金标准" |
| 帧契约一致性 | 同一位姿下 moderngl、Open3D、three.js（ANGLE）与 nvdiffrast 的平面深度差中位数 0.008–0.32 mm、整体像素偏移 < 0.06 px（相位相关噪声底）；Blender EEVEE / Cycles RGB 与 nvdiffrast 41.5–45.3 dB、偏移 < 0.01 px；pyrender 深度差 2.7–20 mm（24 位非线性深度缓冲）且有约 0.3 px 纵向偏移 | D2 `frames.py`/`camera.py` 的 FLU、RDF、COLMAP 像素中心 0.5 约定可以无损落到 5 种渲染器上；本文 `common.Cam` 给出了每种后端的投影矩阵写法 |
| 实例 / 语义分割 | 一次光栅化输出三角形 ID，经"三角形 → 实例 / 语义"查表得到逐像素实例与语义，零额外渲染；静态网格语义按 DTM 高差 + 法向启发式分为地面 / 立面 / 高起物（town：41% / 30% / 28% 的三角形） | 分割与 RGB、深度严格同一几何、同一时刻；静态语义的精细类别（树、屋顶、车）需要 M22 导入时标注 |
| 动态前景合成正确性（60 个视角 × 5 个实体，720p） | 网格背景与动态网格分两遍再按深度合成（C1），与"同一 z-buffer 联合渲染"真值相比：实例掩膜 IoU 均值 0.9997、最小 0.984，89.7% 的实体逐像素一致（差异来自脚底与地面共面的深度并列）。3DGS 背景 + 3DGS 期望深度 ED 合成（C2）：IoU 均值 0.892、p10 0.814、最小 0；误显示像素占真值的 14.3%。3DGS 颜色 + 网格深度（C3）：掩膜与 C1 完全相同 | 遮挡判定与标签必须以网格深度为准；3DGS 只提供颜色。推荐 C3 |
| 可见度标签 | 以各实体为焦点、20–70 m 距离、12–55° 仰角的视角中：完全可见 1.7%、部分遮挡 70%、完全遮挡 28%，可见比中位数 0.89；被遮挡像素 51% 来自高起物（屋顶 / 树冠）、47% 来自立面、2% 来自地面（脚底穿地） | "visible_ratio + 遮挡物类别"可直接作为 `visibility.json` 的真值，替代 M19 的 Johnson/TTPF 估计用于 Benchmark 评价；摄影测量树冠是实心的，遮挡率偏高 |
| 3DGS 路径（town 160 m × 160 m 瓦片，360 张 720p 训练图、30–90 m 航高） | 不训练、只用网格表面采样 1.5M 个面元：23.1 dB。MCMC（上限 3M）30k 步 11–12 分钟：同分布 33.3–34.2 dB / SSIM 0.95–0.96，12–25 m 低空 29.5–31.6 dB，5–10 m 26.9–28.3 dB（近景拖影、空洞）。位置学习率按瓦片尺度（scene_scale = 10）时 2k 步 33 秒即 31.8 dB。渲染 3M 高斯：720p 2.4–3.0 ms（B=8 时 1.6–2.1 ms/帧），1080p 2.9–3.8 ms；RGB+ED 多约 0.5 ms | 从网格训练 3DGS 只会逼近网格、不会更真实，并带来视角外失真与瓦片边界空洞；对这批 GLB，P0–P2 走网格路径，3DGS 留给真实航拍重建的资产（P3） |
| 天气与成像效果 | 深度驱动的后处理（GPU，720p）：雾（Koschmieder）0.3 ms；雨（雨致消光 + 深度测试雨线 + 湿润变暗）1.7–2.4 ms；动态实体投影阴影（2048² 阴影图）4.0–4.7 ms；传感器噪声 < 0.5 ms | 天气按 Event Tape 的环境时间线逐帧生效，固定种子可复现；光照烘焙在纹理里，太阳方位必须与贴图阴影一致 |
| 编码与落盘（CPU 单线程） | 720p：JPEG q90 3.0 ms、PNG 65 ms、深度 PNG16（cm）23 ms、实例 PNG16 16 ms；1080p：JPEG 6.4 ms、PNG 150 ms、深度 PNG16 41 ms、实例 PNG16 35 ms | 1080p 多机批量时 CPU 编码先于 GPU 成为瓶颈；RGB 用 JPEG（或 nvJPEG，见 r10），深度与标签降低压缩级别或改 f16 + zstd |
| Sensor Generation Service | 见 §8：请求 = (run, uav, sensor, 曝光中点时间, 位姿, 内参, 环境快照, seed)；按 (world, backend, 分辨率, 时间桶) 合批；在线闭环与离线重渲染两种模式；Agent 只拿观测，真值标签进评价库 | 落在 D2 `SensorBackend` 协议上（`open / request / poll`），作为 M25 的核心 |

---

## 1. 问题界定

D3 的目标是"固定事件、可变观察者"：同一沙盘、同一事件磁带（Canonical Event Tape），不同数量、策略、协同方式的无人机反复观测。相机渲染在这里的职责是一个纯函数：

```
O_i(t) = Render( World, Tape(t), Pose_i(t), Rig_i, Env(t), seed )
```

它必须满足四条约束：

1. **严格私有视野**：只按该机该传感器的实际位姿与内参成像，不读取全局真值，输出不含 Oracle 信息（真值标签另存）。
2. **时空一致**：RGB、深度、实例、语义、LiDAR（r4）对应同一世界时间和同一位姿；动态实体与静态世界之间的遮挡正确。
3. **可复现**：同一 (World, Tape, Pose, Rig, Env, seed) 在同一后端上结果一致，跨后端在给定容差内一致（design-r1 §9）。
4. **吞吐可预算**：给出每秒帧数、显存与 CPU 预算，决定 GPU Worker 池的规模（design-r1 §10 末段）。

本单元回答：用什么渲染（§3）、怎么对齐坐标与像素约定（§4）、动态前景与分割怎么做（§5）、天气怎么加（§6）、3DGS 值不值得（§7）、服务怎么设计（§8）。

---

## 2. 输入资产实测

### 2.1 GLB 结构

| 资产 | 三角形 / 顶点 | 网格 / 材质 | 纹理 | 扩展 | 节点变换 | 加载（自写 GLB 解析 + JPEG 解码） |
|---|---|---|---|---|---|---|
| town | 1,996,759 / 3,087,397 | 49 / 16 | 16 × 4096² JPEG，107.8 MB | `KHR_materials_unlit` | 根节点 2.5 倍缩放 + 旋转 | 5.9–6.9 s |
| oryahovo | 2,879,110 / 4,432,293 | 80 / 16 | 16 × 4096² JPEG，113.3 MB | `KHR_materials_unlit` | 根节点旋转，1 倍 | 12.1 s |
| hilltop | 2,999,995 / 2,444,257 | 38 / 1 | 1 × 8192² JPEG，29.6 MB | `KHR_materials_specular`（specular 0） | 根节点 Y/Z 交换 | 2.2–3.9 s |

- 全部顶点属性为 `POSITION / NORMAL / TEXCOORD_0`，没有顶点色、没有语义。
- 面积加权法向的 +U 分量占比为 0.48 / 0.36 / 0.36（正值，场景图变换后 Y-up → ENU 的映射 `E = x, N = −z, U = y` 正确）。
- 纹理里已烘焙了拍摄时刻的光照与阴影，应按 unlit 渲染（moderngl / nvdiffrast 直接取贴图；pyrender 用 `RenderFlags.FLAT`；Blender 把 Principled BSDF 换成 Emission；Open3D 用 `defaultUnlit`）。

### 2.2 米制尺度与分辨率

正射渲染（nvdiffrast 正交投影，0.5 m 格）得到 DSM 与正射影像，DTM 由 DSM 做 25 m 形态学开运算再平滑。以道路宽度、房屋尺寸、nDSM 分位数估计米制缩放：

| 资产 | 估计缩放 | 米制范围 | 表面积 | 三角形密度 / 平均边长 | 纹素尺寸（按实际 UV 占用） | UV 利用率 |
|---|---|---|---|---|---|---|
| town | ×4.0 | 2140 × 1281 × 112 m | 2.67 km² | 0.75 /m²，1.76 m | **13.8 cm** | 52% |
| hilltop | ×2.0 | 115 × 96 × 35 m | 1.63 万 m² | 184 /m²，0.11 m | **2.0 cm** | 60% |
| oryahovo | 未定（本文按 1.0 处理） | 119 × 76 × 10 单位 | — | 243 /单位²，0.10 单位 | 0.9 cm/单位 | 53% |

依据：town 正射图上道路 1–1.5 单位、独栋房屋 2–2.5 单位、停车场车辆约 1 单位；nDSM p98 = 2.5 单位、p99.5 = 3.3 单位（对应 10–13 m 的屋脊与树冠）；×4 后高差 112 m 与多瑙河岸到台地的地形相符。oryahovo 是同一城镇中心的另一次重建，但原始单位与 town 不一致，未做标定，不进入米制结论。

"像素 GSD = 纹素"的临界距离（HFOV 60°）：

| 分辨率 | fx | town（13.8 cm） | hilltop（2.0 cm） |
|---|---|---|---|
| 1280×720 | 1108.5 | 153 m | 22 m |
| 1920×1080 | 1662.8 | 229 m | 33 m |

距离小于临界值时画面是纹理放大（变糊），几何细节受三角形边长限制。town 在 8 m 近距（`results/check/actors_town.jpg`）里树木是多面体、车辆是三角面团；在 60 m 高、约 85 m 斜距（`results/check/overview_0.jpg`）时已接近航拍照片。

### 2.3 动态实体资产

| 类别 | 来源 | 三角形 | 归一化尺寸（l × w × h） | 处理 |
|---|---|---|---|---|
| person | CesiumMan（带骨骼，取绑定姿态） | 4,672 | 1.32 × 0.36 × 1.75 m（T 字姿态） | 1024² 纹理 |
| vehicle | CesiumMilkTruck | 3,624 | 2.81 × 4.90 × 2.60 m | 2 张纹理拼图集 |
| dog | Fox（骨骼，绑定姿态） | 576 | 0.19 × 1.17 × 0.60 m | 1024² 纹理 |

高度按 D2 `targets` 类别尺寸缩放（人 1.75 m、车 2.6 m、狗 0.6 m），原点在底面中心，落地高度取 DSM 半径 1 m 内最小值。骨骼动画（LBS）本次未实现，见 §5.4。

---

## 3. 渲染后端对比

### 3.1 测试方法

- 同一场景（town，米制 ×4）、同一组相机（`exp2` 用 seed 生成的 4 个参考位姿 + 每种分辨率 24 个随机航拍位姿，航高 30–100 m、俯仰 −30° 至 −90°、HFOV 60°），输出 RGB、平面深度（光学轴 z，m）和实例 ID。
- 计时取第 2 帧之后的中位数（p50），包含把结果读回到 CPU 内存（除非特别注明）。首帧单列（包含着色器编译与纹理上传）。
- 一致性以 nvdiffrast 为参照：深度绝对差的中位数 / p90；RGB PSNR；`cv2.phaseCorrelate` 估计的整体像素偏移（噪声底约 0.06 px）。

### 3.2 结果

| 后端 | 一次调用的输出 | 720p ms/帧（p50，含读回） | 1080p ms/帧 | 加载 / 首帧 | 深度差 p50 / p90（mm） | 整体像素偏移 | RGB PSNR（对 nvdiffrast） | 备注 |
|---|---|---|---|---|---|---|---|---|
| **moderngl 5.12 / EGL**（OpenGL 3.3 core，驱动 580.95） | RGB8 + R32F 线性 z + R16UI 实例（MRT，一遍） | **1.37–1.59**（其中 draw 0.34–0.38） | **2.86–3.04**（draw 0.40–0.48） | 构建 2.8–3.3 s，首帧 5–9 ms | 0.008–0.046 / 0.03–0.17 | ≤ 0.06 px | 45.6–48.8 dB | 16 个材质 16 次 draw，各向异性 16×；hilltop（3.0M 三角形）1.89 / 3.59 ms |
| **nvdiffrast 0.4.0**（CUDA 光栅化，torch 张量） | RGB + 平面 z + 实例 + 语义（一次 rasterize） | B=1：6.83（纯 GPU 5.66）；B=8：4.53（3.67）；B=16：8.72（3.51） | B=1：10.9（6.66）；B=8：13.9（4.33）；B=16：18.5（4.18） | 初始化 1.1–1.5 s | 参照 | 参照 | 参照 | float32 图集 16384² 常驻 4.2 GB；降到 8192² 为 1.2 GB 且速度不变；hilltop B=8 含读回 6.1 ms/帧 |
| three.js r186 + 无头 Chromium（`--use-angle=gl-egl`，A40） | RGB 一遍 + 线性 z 一遍（ShaderMaterial 写 float RT） | 5.2 + 3.7（页内 render + readPixels） | — | GLB 加载 1.6–1.9 s | < 0.05（9 点抽样） | ≤ 0.004 px | 37.0 dB | 一帧经 CDP 导出 240–460 ms；ANGLE Vulkan 4.1 + 3.1 ms；默认参数与 `--use-gl=egl` 退回 SwiftShader（CPU）57–187 ms |
| pyrender 0.1.45（EGL） | RGB + 深度（分割需第二遍 `RenderFlags.SEG`） | 10.7 | 19.9 | 构建 1.4 s，首帧 2.4–2.5 s | 2.7–20.2 / 9.3–69.4 | ≈ (+0.1, −0.3) px | 43.6–48.3 dB | 24 位非线性深度缓冲；需 `FLAT` 与 UV 纵向翻转 |
| Open3D 0.19 `OffscreenRenderer`（Filament，EGL） | RGB 一遍 + 深度一遍，无实例通道 | 26.3（RGB 11.2 + 深度 15.2） | 39–44 | 构建 2.3–2.9 s，首帧 1.3–1.7 s | 0.05–0.32 / 0.10–0.83 | ≤ 0.06 px | 15.3–15.5 dB（默认色调映射）/ 8.3–8.6 dB（LINEAR） | 色彩管线未能对齐 |
| Blender 4.2.3 EEVEE Next（`-b`，EGL） | RGBA + Z（多层 EXR 或 PNG） | 190–250（含写文件） | — | 导入 3.9–4.9 s，首帧 4.9–25 s | — | < 0.01 px | 41.5–44.7 dB | 材质改 Emission、Standard 视图变换 |
| Blender 4.2.3 Cycles（OptiX，0 次反弹，无降噪） | RGBA + Z + IndexOB | 16 spp：130–160；128 spp：310–340（`use_persistent_data`）；不开 persistent data 4200–6000 | — | 首帧 6.8–8.0 s | — | < 0.01 px | 41.7–45.3 dB | 适合少量金标准帧 |
| gz-sim Ogre2（r5 单元） | RGB、深度、分割等传感器 | r5 按 2 Hz 配置，未压测上限 | — | town 首张图 11.2 s、hilltop 5.1 s | 顶视深度对 DSM 中位 0.6 mm（r5） | — | — | 无头 `--headless-rendering` 可直接加载三个 GLB |

注：nvdiffrast 数值为 GPU 3 复测（同卡另有约 5.8 GB 其他进程，利用率波动）；GPU 2 争用期间的首轮数值为 B=1 5.9–42 ms、B=8 3.9 ms（纯 GPU），见 `nv/town_full_bench_gpu2_shared.json`。pyrender、Open3D、Blender、three.js 在 GPU 2 上测得。批量大时"含读回"明显变慢（1080p B=16：4.2 → 18.5 ms/帧），原因是同步拷贝到可分页内存（每帧约 22 MB），见 §8.4。

### 3.3 解读

1. **硬件光栅化（OpenGL/EGL）比 CUDA 软件光栅化快 4–10 倍。** 200 万三角形的 draw 只需 0.4–0.5 ms；720p 一帧含三个附件读回 1.4 ms。nvdiffrast 的成本主要在光栅化本身（720p、B=8 分项：只出深度 26.3 ms，全部输出 29.4 ms，即 89%），批量能摊薄但不改变量级。
2. **nvdiffrast 的优点是"张量进张量出"**：位姿批量、可微、与 gsplat / torch 后处理零拷贝，最适合做研究原型与 3DGS 合成实验。但 r10 已核实其许可为 NVIDIA Source Code License（第 3.3 条限研究与评估），产品化必须用 OpenGL/EGL、Vulkan 或 gz Ogre2。
3. **pyrender 与 Open3D 不推荐。** pyrender 慢 8 倍、深度用 24 位非线性缓冲（远处有厘米级误差）、主点约定有约 0.3 px 偏差、分割需第二遍；Open3D/Filament 慢 20 倍、深度与 RGB 分两遍、没有实例 ID 通道，且色彩管线（sRGB 解码、色调映射、曝光）与摄影测量贴图不直通，本次两种设置下亮度都对不上（15.3 dB / 8.3 dB），需要额外标定。
4. **three.js 无头渲染可行但不适合做服务端数据生产。** 默认参数和 `--use-gl=egl` 都退回 SwiftShader（CPU，57–187 ms/帧）；必须 `--use-gl=angle --use-angle=gl-egl`（或 `vulkan`）才用上 A40。页内渲染 + `readPixels` 只要 4–5 ms，但把一帧经 CDP（base64）交给宿主进程要 240–460 ms，是瓶颈。它的价值是"与前端同一渲染器"：深度与 nvdiffrast 一致到 0.1 mm 以内，RGB 37 dB（三者贴图过滤不同）。
5. **Blender 适合离线金标准，不适合批量。** EEVEE 首帧 4.9–25 s（着色器编译），稳态约 0.2 s/帧；Cycles 必须 `render.use_persistent_data = True`，否则每帧重建场景 4.2–6.0 s，开启后 16 spp 0.13–0.16 s、128 spp 0.31–0.34 s。它能给出真实光照、软阴影、景深、运动模糊，适合做少量"高保真对照集"来度量其他后端的域差。
6. **Gazebo ogre2（r5 单元实测）**：`gz sim -s --headless-rendering` 可直接加载这三个 GLB，town 首张图 11.2 s、hilltop 5.1 s，r5 的相机按 2 Hz 配置；其顶视深度与 DSM 差值中位数 0.6 mm。它与本文的 GL 路径同属硬件光栅化，适合 Realistic 档位（传感器在 gz 闭环内）；吞吐上限以 r5 报告为准。

---

## 4. 帧契约：坐标、像素与深度约定

### 4.1 统一约定（沿用 D2 M13）

- 世界 ENU（m），机体与传感器帧 FLU，`R_ws = WORLD←FLU`，四元数 `[x, y, z, w]`；
- 光学帧 RDF：`x_opt = −y_s, y_opt = −z_s, z_opt = x_s`；
- 像素 COLMAP 约定：`u = fx·x/z + cx`，像素 i 的中心在 `u = i + 0.5`；
- 深度图 = 光学轴 z（平面深度，m），不是斜距；LiDAR 用斜距（r4）。

### 4.2 各后端的投影写法（`common.Cam`）

| 后端 | 视图矩阵 | 投影矩阵 | 读回方向 |
|---|---|---|---|
| nvdiffrast / moderngl（推荐写法） | `world_to_opt()` = `[R_woᵀ, −R_woᵀ·pos]`，`R_wo = R_ws·R_FLU_OPT` | `proj_ndc_down()`：`P[0,0]=2fx/W, P[0,2]=2cx/W−1, P[1,1]=2fy/H, P[1,2]=2cy/H−1, P[3,2]=1`（光学帧直接进裁剪空间，`y_ndc = 2v/H − 1`） | 行 0 即图像顶部，无需翻转；正反面绕序反转，需关闭背面剔除（摄影测量网格本来就是 doubleSided） |
| 标准 OpenGL / three.js / pyrender | `diag(1,−1,−1)·world_to_opt()`（RUB） | `proj_gl()`：`P[0,2]=1−2cx/W, P[1,2]=2cy/H−1, P[2,2]=−(f+n)/(f−n), P[3,2]=−1` | GL 读回行 0 为底部，需上下翻转；pyrender 内部已翻转 |
| Open3D | `setup_camera(intrinsic, world_to_opt)`，再 `set_projection(K, near, far, W, H)` | 由 K 生成 | 已为图像方向 |
| Blender | `matrix_world = [R_ws·R_FLU_GL, pos]`，`R_FLU_GL` 列为 (−y, z, −x) | `lens = fx·36/W`（传感器宽 36 mm、水平适配），`shift_x = −(cx−W/2)/W`，`shift_y = (cy−H/2)/W` | 图像方向 |
| gsplat | `viewmats = world_to_opt()`（OpenCV） | `K` | 图像方向 |

纹理坐标陷阱：glTF 的 UV 原点在左上，pyrender 与 Open3D 按 OpenGL 约定原点在左下，直接喂原始 UV 会出现贴图错位（本次第一轮 pyrender 11.96 dB、Open3D 9.3 dB），需 `v' = 1 − v`；moderngl 按行优先上传图像时与 glTF 一致，无需翻转。

### 4.3 容差建议（用于"跨后端可复现"的判定）

| 量 | 同后端同版本 | 跨后端（GL 族、nvdiffrast、Blender、gz） |
|---|---|---|
| 平面深度 | 逐位一致 | p50 ≤ 1 mm，p90 ≤ 5 mm（实测 GL 族 ≤ 0.8 mm） |
| 实例 / 语义掩膜 | 逐位一致 | 每实体 IoU ≥ 0.98（差异只在深度并列像素） |
| RGB | 逐位一致 | PSNR ≥ 40 dB（实测 41.5–48.8 dB；three.js 因 sRGB / 各向异性过滤差异为 37 dB，需单独约定） |
| 主点 / 像素偏移 | 0 | ≤ 0.1 px |

---

## 5. 动态前景、实例与语义分割、可见度

### 5.1 实现

`nvr.NvScene.render(cams, dyn_poses, static=True|False)`：静态网格（图集纹理）与动态网格（逐帧位姿，独立小图集）拼接为一次 rasterize，输出 `rast[..., 3] = tri_id + 1`。随后：

- RGB：静态三角形取图集纹理（mipmap 三线性），动态三角形取动态图集；
- 深度：插值光学帧 z；
- 实例：`lut_inst[tri_id]`（静态 0，实体 1..N）；语义：`lut_sem[tri_id]`（0 无、1 地面、2 立面、3 高起物、10 人、11 车、12 狗）；
- `static=False` 只渲染动态网格（用于与 3DGS 背景合成、计算"无遮挡像素数"）。

GL 路径（`exp3_gl_bench.py` 的 moderngl 着色器）用 MRT 一次写出 `RGBA8 + R32F(线性 z) + R16UI(实例)`，每个 draw 设置 `uniform uint inst`；语义可用第二个 R8UI 附件或同一 R16UI 的高位。

### 5.2 合成正确性

实验 `exp6_composite.py`：5 个实体（3 人、1 车、1 狗，town 中心广场与街巷，`actors_town.json`），每个实体 12 个视角（距离 20–70 m、仰角 12–55°、HFOV 60°、720p），共 60 个视角、185–189 个"实体 × 视角"样本。真值 GT = 静态 + 动态同一 rasterize。

| 方案 | 背景颜色 | 遮挡用深度 | 实体掩膜 IoU 均值 / p10 / 最小 | 误显示（应被挡却画出） | 误隐藏 | 逐像素一致的样本 | 全图 PSNR（对 GT） |
|---|---|---|---|---|---|---|---|
| C1 | 网格 | 网格 | 0.9997 / 0.99998 / 0.984 | 0.009% | 0.004% | 89.7% | —（与 GT 同源） |
| C2 | 3DGS | 3DGS 期望深度 ED（`render_mode="RGB+ED"`，alpha < 0.5 视为空） | 0.892 / 0.814 / 0.0 | **14.3%** | 0.2% | 3.7% | 28.7 dB |
| C3 | 3DGS | 网格 | 0.9997 / 0.99998 / 0.984 | 0.009% | 0.004% | 89.7% | 29.4 dB |

结论：

- 分两遍渲染再按深度合成与联合 z-buffer 等价，残差只在脚底与地面"深度并列"的像素（可在合成时给动态深度减 1 cm 的偏置消除）。
- 3DGS 的期望深度在物体边缘是前后景的加权平均，且半透明区域 alpha 不足，导致实体"穿墙"被画出（C2 中存在 IoU = 0 的样本：真值完全被遮挡，合成后却完整显示）。**Benchmark 的遮挡与可见度标签不能来自 3DGS 深度**。
- C3 把颜色交给 3DGS、几何交给网格，标签与 C1 完全相同，额外代价只是一次网格深度渲染。

### 5.3 可见度标签

每个实体在每帧计算 `px_unoccluded`（只渲染动态网格时的像素数）、`px_visible`（GT 中实例像素数）、`visible_ratio`，以及被挡像素处 GT 语义的直方图（遮挡物类别）。60 个焦点样本：

| 完全可见（> 0.99） | 部分遮挡 | 完全遮挡（≤ 0.01） | 可见比中位数 | 遮挡物：高起物 / 立面 / 地面 |
|---|---|---|---|---|
| 1.7% | 70.0% | 28.3% | 0.89 | 51% / 47% / 2% |

- 街巷里的 person_C 在 12 个视角中 7 个完全被挡；车辆停在树冠下，可见比 0.68–0.82。
- 摄影测量网格把树冠当实心体，树下的遮挡率高于真实（真实树冠部分透光）。建议 M22 把"植被"作为独立语义，评价时把植被遮挡单独统计，或对植被三角形做随机镂空（alpha mask）。
- 这些量就是 design-r1 §8 `annotations/visibility.json` 与 §8 "Event Capture" 评价所需的像素级真值；M19 的 Johnson/TTPF 可以用它们标定。

### 5.4 未完成与建议

- **骨骼动画**：CesiumMan、Fox 自带骨骼与动画，本次用绑定姿态。生产实现应由 Event Runtime 输出每个实体每个时刻的关节变换（或动画片段 + 相位），渲染端做 LBS（人约 5k 顶点 × 20 关节，GPU 上微秒级），不影响吞吐。
- **外观域差**：CG 实体清晰、背景是放大的摄影测量纹理，实体"过于锐利"。建议对实体按与背景相同的有效 GSD 做 mip 偏置或轻微模糊，并统一传感器噪声 / JPEG。
- **光照一致**：实体用"太阳方向 + 环境光"的 Lambert 着色，太阳方位应从贴图阴影方向估计并写入 World Package（photogrammetry 光照烘焙，无法随时间改变）。
- **接地**：实体落在 NavMesh / DTM 上，脚底与地面深度并列；渲染时动态深度偏置 1–2 cm，可见度统计忽略离地 0.1 m 以下的像素。

---

## 6. 天气与成像效果

实现 `fx.py`（torch，GPU），全部以同一帧的深度与位姿为输入，参数来自 Event Tape 的 `environment` 时间线，随机性由显式 seed 决定：

| 效果 | 模型 | 720p 耗时（p50） |
|---|---|---|
| 雾 | `t = exp(−β d)`，`β = 3.912 / V`（V 为能见度），`I = I·t + A·(1−t)`；天空按 3 km 处理 | 0.30 ms |
| 雨致消光 | `σ ≈ 1.076·R^0.67 km⁻¹`（R：mm/h；可见光经验式），4 mm/h 约 1.4 km 能见度，25 mm/h 约 0.4 km；与雾的消光相加 | 含在雨中 |
| 雨线 | 相机前 0.5–30 m 视锥内按体积均匀采样雨滴（数量 ∝ R，视觉经验值），速度 = 风 + 终速 6.5±1 m/s，曝光 1/50 s 内的位移投影成线段，16 个采样点散射写入，**逐点与场景深度比较**（雨滴在表面之前才可见），近处更亮更粗 | 1.7–2.4 ms |
| 湿润 | 按雨强最多变暗 18%、去饱和 15% | < 0.1 ms |
| 动态实体投影阴影 | 以太阳方向做 2048² 正交阴影图（只含动态网格），背景像素反投影到世界后查表，阴影内乘 0.55 | 4.0–4.7 ms |
| 传感器噪声 | 读出噪声 + 散粒噪声（与亮度相关） | < 0.5 ms |

样张：`results/comp/town_v0{0..3}_fx.jpg`（每张 3 × 2：合成原图 / 加阴影 / 8 mm/h 雨 + 5 m/s 风 / 250 m 雾 / 25 mm/h 暴雨 + 7 m/s 风 / 加噪声），雨线局部 `town_v00_fx_heavy_crop.jpg`。

注意：

- 雨线的数量与亮度是视觉经验参数，不是物理标定；Benchmark 中应把"雨强 → 雨线密度"的映射版本化，并同步给 LiDAR（r4）用同一消光系数。
- 摄影测量贴图自带晴天光照，"下雨"只能做全局变暗与去饱和，不能消除贴图里的硬阴影；时间变化（日落、夜间）同理受限。
- 镜头畸变（`dist` 非空时）、卷帘快门、运动模糊（K 个子位姿渲染后平均，r10 测得 4 子帧约为单帧 1/4 吞吐）应在同一后处理链中按传感器配置开关。

---

## 7. 3DGS 路径

### 7.1 方法

`exp5_gs_train.py`：在 town 中以实体群为中心取 160 m × 160 m 瓦片；用 nvdiffrast 渲染 360 张训练图（720p、HFOV 60°、航高 30–90 m、仰角 35–90°，注视点在瓦片内）与 40 张同分布测试图、30 张低空测试图（12–25 m、仰角 20–45°），另在 `exp5b` 中评估 5–10 m 极低空。位姿精确已知，无需 COLMAP；损失与评价只算表面点落在瓦片内的像素。

- 初始化：按面积在瓦片 +10 m 内的网格表面采样 1.5M 点，颜色取纹理，形状为沿法向压扁的面元（切向 0.7 × 间距，法向 0.07 × 间距；间距 0.21 m），不透明度 0.9；
- 训练：gsplat 1.5.3 `rasterization` + `MCMCStrategy(cap_max = 3M)`，SH 0 阶（贴图无视角相关性），损失 0.8·L1 + 0.2·(1 − SSIM) + 0.01·不透明度正则 + 0.01·尺度正则；
- v1：位置学习率按 gsplat 默认的"相机分布半径 × 1.1"（本例 192 m）缩放；v2：固定 scene_scale = 10。

### 7.2 结果

| 方案 | 训练步数 / 耗时 | 高斯数 | 训练峰值显存 | 同分布 30–90 m：PSNR / SSIM | 低空 12–25 m | 极低空 5–10 m |
|---|---|---|---|---|---|---|
| 只初始化（1.5M 面元，不训练） | 0 | 1.5M | — | 23.1 / 0.64 | 23.0 / 0.72 | — |
| v1（位置学习率按相机分布半径缩放） | 7k / 153 s | 3M | 6.6 GB | 22.7 / 0.65 | 21.6 / 0.71 | — |
| v1 | 30k / 659 s | 3M | 7.6 GB | 33.3 / 0.95（复评 33.6） | 29.6 / 0.92（复评 29.5） | 26.9 / 0.89 |
| v2（scene_scale = 10） | 2k / 33 s | 3M | 6.6 GB | 31.8 / 0.94 | 30.0 / 0.91 | — |
| v2 | 7k / 152 s；15k / 346 s | 3M | 7.8 GB | 31.7 / 0.94；31.6 / 0.94 | 29.9 / 0.92；29.8 / 0.92 | — |
| v2 | 30k / 710 s | 3M | 7.8 GB | **34.0 / 0.96**（复评 34.2） | **31.4 / 0.94**（复评 31.6） | **28.3 / 0.90** |
| v2，1080p 评估 | 同上 | 3M | — | 34.4 / 0.96 | 31.3 / 0.94 | 28.1 / 0.92 |

指标只在"表面点落在瓦片内"的像素上计算（瓦片外没有高斯）；LPIPS 未测（容器无外网，权重下载失败）。GT 渲染 430 张 720p 图用时 4.0–4.7 s。

渲染速度（3M 高斯，SH0，A40，ms/帧，v1–v2 两个模型的范围）：

| 分辨率 | B=1 RGB | B=1 RGB+ED | B=8 RGB | B=8 RGB+ED |
|---|---|---|---|---|
| 1280×720 | 2.41–3.02 | 2.88–3.37 | 1.59–2.13 | 1.94–2.50 |
| 1920×1080 | 2.93–3.81 | 3.51–4.48 | 2.14–3.01 | 2.71–3.59 |

存储：3M × 14 个 float32 = 160 MB / 瓦片（`.pt` 168 MB）。

样张：`results/gs/town_in0_gt_vs_gs.jpg`（左网格、右 3DGS；瓦片外为空，只有远处的大尺度浮点）、`town_vlow0_gt_vs_gs.jpg`（5–10 m：近景拖影与空洞），v2 模型同名加 `_v2`。v1 的训练 JSON 因渲染计时阶段的参数错误（`backgrounds` 通道数）未写出，数值取自 `results/logs/exp5_town.log`，模型已保存并由 `exp5b` 复评；§5.2 的 C2、C3 使用 v1 模型。

### 7.3 解读

1. **上限是网格。** 训练数据就是网格渲染，3DGS 只能逼近；同分布 33–34 dB 意味着肉眼接近但不等同，近景与视角外明显劣化。对 GLB 资产，3DGS 不提供额外真实感。
2. **视角覆盖决定可用性。** 30–90 m 训练、5–10 m 使用时 PSNR 掉 6–7 dB，且出现结构性伪影（拖影、空洞），会被 Agent 的检测器当作真实信号。训练视角必须覆盖 Agent 的飞行包络（航高、斜视角、距离）。
3. **瓦片化代价。** 一个 160 m 瓦片 3M 高斯（参数 160 MB）、训练 2.5–11 分钟；town 全镇（约 2.7 km²）需 100 个以上瓦片、约 16 GB 参数、数十 GPU 小时，渲染时还要同时驻留相邻瓦片（60 m 航高、60° 斜视的视野跨度数百米），否则出现瓦片外空洞（样张左侧）。
4. **学习率要按瓦片尺度设置。** 航拍场景的相机分布尺度（本例 192 m）远大于高斯尺度，按 gsplat 默认缩放位置学习率会让 MCMC 噪声过大（v1 7k 步 22.7 dB，低于不训练的 23.1 dB，直到学习率衰减后才收敛到 33.3 dB）；按瓦片尺度（v2）2k 步即达 31.8 dB，30k 步 34.0 dB，低空也高出 1.8 dB。与 r03 记录的"航拍需收紧 grow_scale3d"一致。
5. **渲染很快。** 3M 高斯 720p 2.4–3.0 ms、1080p 2.9–3.8 ms，批量 8 视角 1.6–2.1 ms/帧，与网格 GL 路径同一量级。"3DGS 背景 + 网格深度 + 动态网格 + 合成"（C3）的总代价：GL 路径约 5 ms/帧，nvdiffrast 路径约 9.5 ms/帧（§5.2、§8.5 计时）。

建议：3DGS 作为 World Package 的可选外观层 `appearance.3dgs`，只用于**真实航拍图像重建**的资产（那时 3DGS 才比网格更真实）；对 GLB 资产，P0–P2 用网格渲染，3DGS 仅用于前端 Web 流式展示（SPZ，r03）或研究对照。一旦采用 3DGS 背景，遮挡与标签一律用网格深度（C3）。

---

## 8. Sensor Generation Service 设计（M25 核心）

### 8.1 定位

```
                Canonical Event Tape (实体位姿/骨骼、环境时间线, 版本哈希)
World Package ─────────────┐        │
 appearance.glb / tiles    │        ▼
 tri_semantic / anchors    │   ┌─────────────────────────────────────────────┐
 appearance.3dgs (可选)     └──►│ Sensor Generation Service（每 GPU 一个 Worker）│
                               │  1 位姿解算  2 实体状态  3 光栅化/合成          │
Flight/Physics Authority ─────►│  4 后处理（天气/阴影/畸变/ISP）  5 标签          │
 (FleetSim / gz / PX4, 位姿流)  │  6 编码落盘  7 发布                            │
                               └───────┬───────────────────────┬─────────────┘
                                       │ agent obs（私有）       │ GT labels（隐藏）
                                       ▼                       ▼
                             M26 Agent Observation SDK    M27 Benchmark / 评价库
```

- 是纯函数式服务：不改变世界、不读 Agent 状态，只按请求成像。
- 只依赖三类输入：静态世界资产、事件磁带、机体位姿流；因此**任何一次运行都可以离线重渲染**。

### 8.2 两种运行模式

| 模式 | 用途 | 时间语义 | 背压 |
|---|---|---|---|
| 在线闭环 | Agent 依赖图像决策（主动观测、跟踪） | 仿真内核在触发时刻 `t_k` 发出请求；观测在 `t_k + latency`（传感器延迟模型，仿真时间）交付给 Agent | 在下一次需要该观测的仿真步前未完成，则内核在屏障处等待（lockstep），仿真可快于或慢于实时 |
| 离线重渲染 | 数据集导出、更高分辨率、换后端（3DGS / Blender 金标准）、补算标签 | 从运行记录（位姿流 + 磁带 + rig + seed）逐帧重放 | 无，按 GPU 能力批量 |

在线模式只渲染 Agent 需要的通道与分辨率；数据集所需的高分辨率、多通道、金标准对照在离线模式补齐。两者对同一 `(run, uav, sensor, seq)` 使用同一位姿与时间，因此可以对齐；但"Agent 实际看到的帧"必须原样存档，不能用重渲染替代。

### 8.3 请求与帧契约（`drone4d.frame.v1`）

请求（内核 → 服务）：

```json
{"schema": "drone4d.render_req.v1", "run_id": "r-0007", "world": {"id": "smalltown-01", "hash": "sha256:..."},
 "tape": {"id": "abc-crossing-001", "hash": "sha256:..."}, "uav": "uav-02", "sensor": "camera", "seq": 1532,
 "t_trigger_ns": 153200000000, "exposure_ns": 5000000, "readout_ns": 0,
 "pose_samples": [{"t_ns": 153202500000, "pos_enu_m": [81.2, -301.4, 75.0], "q_ws_xyzw": [0.12, 0.33, -0.04, 0.93]}],
 "rig": {"w": 1280, "h": 720, "fx": 1108.5, "fy": 1108.5, "cx": 640.0, "cy": 360.0, "dist": {"model": "none", "k": []},
         "pixel_convention": "colmap_center_0.5", "near_m": 0.5, "far_m": 3000.0},
 "outputs": ["rgb", "depth", "instance", "semantic", "visibility"], "quality": "online",
 "env": {"rain_mm_h": 4.0, "fog_vis_m": null, "wind_enu_mps": [5.0, 2.0], "sun_az_el_deg": [135.0, 50.0]},
 "seed": [20261011, 2, 0, 1532]}
```

帧记录（服务 → 记录器；JSON 旁注 + 二进制块，或 MCAP / Rerun 的一条消息）：

```json
{"schema": "drone4d.frame.v1", "frame_id": "r-0007/uav-02/camera/1532", "t_exposure_mid_ns": 153202500000,
 "T_world_sensor": {"pos_enu_m": [81.2, -301.4, 75.0], "q_ws_xyzw": [0.12, 0.33, -0.04, 0.93], "frame": "ENU<-FLU"},
 "intrinsics": {"w": 1280, "h": 720, "fx": 1108.5, "fy": 1108.5, "cx": 640.0, "cy": 360.0, "dist": {"model": "none"}},
 "renderer": {"backend": "gl-egl", "version": "m25-0.1.0+<git>", "world_hash": "...", "tape_hash": "...", "fx_version": "fx-1"},
 "agent_view": [{"kind": "rgb", "uri": "rgb/001532.jpg", "enc": "jpeg-q92"},
                {"kind": "depth", "uri": "depth/001532.png", "enc": "png16", "unit": "cm", "type": "planar_z"}],
 "ground_truth": {"instance": {"uri": "gt/inst/001532.png", "enc": "png16"}, "semantic": {"uri": "gt/sem/001532.png", "enc": "png8"},
   "objects": [{"inst": 1, "actor": "person_A", "class": "person", "px_visible": 182, "px_unoccluded": 203,
                "visible_ratio": 0.897, "bbox_xywh": [611, 402, 9, 21], "occluder_sem_px": {"3": 21}}]}}
```

- `agent_view` 进入 Agent 的观测通道；`ground_truth` 只写评价库（design-r1 §7 信息权限、§8 `event_truth.json` 不对参赛 Agent 开放）。
- 时间戳取曝光中点；位姿由物理状态在曝光中点做 SE(3) 插值（位置线性、姿态 slerp），云台按 M13 `T_world_base · T_base_mount · R_gimbal`；卷帘 / 运动模糊时 `pose_samples` 给出多个子位姿。
- 与 FAST-LIVO2（r05、r1 单元）对接：仿真可提供"理想硬同步"（相机曝光时刻与 LiDAR 扫描切分一致），也可按配置注入时间偏移、曝光时间变化（FAST-LIVO2 会估计 `inv_expo_time`）与灰度输出；内参导出为其 vikit 相机 yaml 即可。

### 8.4 调度与合批

1. **Worker**：每块 GPU 一个进程，常驻一个或多个 World（town：网格与纹理加载 6–7 s、上传 1.3 s；3DGS 瓦片按需换入）。
2. **合批键**：`(world, backend, 分辨率, 输出集合, 事件时间桶)`。同一时间桶内多架机共享一次实体状态更新（骨骼蒙皮、动态顶点上传）；nvdiffrast 路径把 B = 8–16 个视角合成一次调用，GL 路径在同一上下文内顺序 draw（单帧 draw 只有 0.4–0.5 ms，无需复杂批处理）。
3. **队列**：三个优先级——交互预览（沙盒界面、低分辨率）> 在线闭环 > 离线重渲染；每个 Run 有在途上限，超出时向内核施加背压。
4. **读回与编码流水线**：GPU 渲染 → 固定页内存异步拷贝（PBO / pinned）→ 编码线程池（或 nvJPEG）→ 落盘 / 发布。实测 nvdiffrast 批量 16 视角 1080p 时，同步读回把每帧从 4.9 ms 拉长到 18.7 ms，说明拷贝与编码必须与渲染重叠。
5. **扩展**：按 Run 或按无人机分片到多 GPU；World 常驻内存可在同机多 Worker 间只读共享。

### 8.5 吞吐与预算

单帧成本（A40，720p / 1080p，p50；"全部"= RGB + 深度 + 实例 + 语义）：

| 环节 | 720p | 1080p | 来源 |
|---|---|---|---|
| 网格光栅化，GL 路径（RGB + z + id，含读回） | 1.4–1.6 ms（town）/ 1.9 ms（hilltop） | 2.9–3.0 / 3.6 ms | exp3 |
| 网格光栅化，nvdiffrast（全部输出，B=8，含读回） | 4.5 ms（town）/ 6.1 ms（hilltop） | 13.9 / 14.4 ms | exp2（GPU 3） |
| 动态实体单独一遍（可见度分母，nvdiffrast） | 1.8–2.1 ms | — | exp6 |
| 3DGS 背景 3M（RGB+ED，B=1 / B=8） | 2.9–3.4 / 1.9–2.5 ms | 3.5–4.5 / 2.7–3.6 ms | exp5b |
| 深度合成 | 0.2 ms | — | exp6 |
| 动态实体阴影 | 4.0–4.7 ms | — | exp6 |
| 雨 / 雾 | 1.7–2.4 / 0.3 ms | — | exp6 |
| 编码（CPU 单线程）：JPEG q90 + 深度 PNG16 + 实例 PNG16 | 3.0 + 23 + 16 ms | 6.4 + 41 + 35 ms | exp7 |

CPU 编码（单线程，§0 表）：720p 每帧 JPEG + 深度 PNG16 + 实例 PNG16 ≈ 42 ms；1080p ≈ 82 ms。

算例：

| 场景 | 帧率需求 | GPU（GL 路径） | GPU（nvdiffrast 研究路径） | CPU 编码 |
|---|---|---|---|---|
| P0：3 架 × 10 Hz × 720p，全部输出 + 可见度 | 30 fps | 约 0.1 GPU（含可见度所需的动态单独一遍） | 约 0.25 GPU | 约 1.3 核 |
| Benchmark：10 架 × 15 Hz × 1080p | 150 fps | 约 0.5 GPU | 约 2 GPU（含读回） | 约 12 核：CPU 先成瓶颈，需 nvJPEG 与更轻的深度编码 |
| 离线数据集：1 h 事件、10 架、10 Hz、720p | 36 万帧 | 约 10–15 分钟 | 约 40 分钟 | 4.2 核·小时 |

显存（单 World 常驻）：

| 项 | 实测 / 估算 |
|---|---|
| town 网格 + 16384² 浮点图集（nvdiffrast 只支持 float32 纹理） | 4.2 GB（其中纹理 3.0 GB + mip 1.0 GB） |
| town 网格 + 8192² 图集（降采样） | 1.2 GB（实测；720p B=8 含读回 5.3 ms/帧，与 16384² 相当，远景无可见差异，近景纹理减半） |
| town GL 路径（16 张 4096² RGB8 + mip，顶点 3.1M） | 估算约 1.5 GB |
| hilltop 网格 + 8192² | 1.1 GB |
| 3DGS 瓦片 3M 高斯（SH0） | 参数 160 MB；训练峰值 6.6–7.8 GB；渲染工作区约 1 GB |
| 每批工作区（nvdiffrast） | 720p B=8：+1.8 GB；1080p B=16：+6.0 GB（峰值 10.2 GB） |

建议预算：一块 48 GB GPU 常驻 2–3 个 World（GL 路径）或 1 个 World + 3DGS 瓦片缓存；保留 50% 显存给批量工作区与其他单元。

### 8.6 与现有代码的衔接

- **M13 `SensorBackend`**（`backends/base.py`）：新增 `RenderBackend` 实现 `open(world, rigs)`、`request(slots, poses_enu, t_sim_ns)`（非阻塞，入队）、`poll()`（步顶锁存，返回已完成帧）。`SensorFrame.payload` 放帧记录（URI，不放像素数组），`caps = SensorCaps(pose=True, noise=True, detect=False, raycast="mesh")`。`FakeBackend` 的位姿公式与本文 `common.Cam` 一致，可直接作单元测试对拍。
- **M13 相机几何**：`camera.project` / `CameraGeom.pixel_of` 与渲染投影同一公式（COLMAP 0.5 约定），可用渲染出的实例质心与投影点对拍（本文实测像素偏移 < 0.06 px）。
- **M19 感知**：Fast 档位继续用 Johnson/TTPF；Benchmark 档位以渲染可见度（`px_visible`、`visible_ratio`）为真值，并用它标定 M19 的像素阈值。
- **M12 记录 / 回放**：帧记录作为时间轴上的引用，原始帧进 MCAP / Rerun（r10 已验证 `world/uav{id}/cam/...` 布局）；回放时可选择"原帧"或"离线重渲染帧"。
- **M17 沙盒**：公开沙盒只拿低分辨率预览流；渲染 Worker 是独立的 GPU 资源池，不进入轻量沙盒进程池（design-r1 §10 末段）。
- **M22 World Import**：输出 `appearance.glb`（原样）、`tri_semantic.npy`（逐三角形语义）、`metric_scale` 与原点、`texel_size_m` 与"推荐最小斜距"、太阳方位、DSM / DTM（本文 `exp1_probe.py` 已给出原型）。

### 8.7 可复现性

- 每帧记录 world / tape / renderer / fx 版本哈希与 seed；随机量（雨滴、噪声）用计数器式 RNG（D2 `sim/sensors/cbrng.py`），键为 `(run seed, uav, sensor, seq)`，与渲染顺序、合批方式无关。
- 跨 GPU / 驱动的容差按 §4.3；同一后端同一版本要求逐位一致（本次 C1 与 GT 的对比即在同一后端内）。

---

## 9. 对 D3 架构的建议

1. **M25 默认后端选 OpenGL/EGL 硬件光栅化**（moderngl 原型 → 产品化可用 C++/Vulkan，或在 Realistic 档位直接用 gz Ogre2 传感器）。一次 MRT 输出 RGB、平面深度、实例、语义；单 A40 720p 约 700 fps 量级，足以支撑多机在线闭环。nvdiffrast 只作研究原型与 3DGS 合成实验（许可限制，见 r10）。
2. **P0 用网格，不训练 3DGS。** 对现有 GLB，网格就是外观真值；3DGS 放到 P3，且只用于真实航拍重建的资产。若用 3DGS，遮挡与标签走网格深度（C3）。
3. **World Import 增加四项必做步骤**：米制尺度标定、逐三角形语义（至少地面 / 建筑 / 植被 / 水体 / 车辆）、分辨率体检（纹素尺寸、三角形边长、推荐最小斜距）、太阳方位估计。
4. **可见度与遮挡标签由渲染产生**：每帧每实体 `px_visible / px_unoccluded / visible_ratio / bbox / 遮挡物类别`，作为 Event Capture 评价的像素级依据。
5. **天气由 Event Tape 驱动**：雨强、能见度、风速按时间线插值，相机（本文 `fx.py`）与 LiDAR（r4）共用同一消光参数；版本化"雨强 → 雨线"映射。
6. **在线闭环与离线重渲染分离**：在线只渲染 Agent 需要的通道；数据集的高分辨率、金标准与补充标签离线生成，降低闭环实验对 GPU 的要求。
7. **前端保持 three.js**：同一 GLB、同一相机约定在 three.js 中与服务端深度一致到 0.1 mm，可用于"浏览器看到的就是无人机看到的"的视觉校验；但不要用无头浏览器做服务端数据生产（帧导出慢、默认退回软件渲染）。
8. **场景选择**：town 用于中高空（≥ 60 m 斜距）的大范围事件与多机协同；hilltop 用于近距离（10–30 m）观测与 Re-ID；两者分工与 design-r1 §11 的"两个演示场景"一致。

---

## 10. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| GLB 尺度非米制（town 估计 ×4.0，±20%） | 航高、实体尺寸、风速、LiDAR 量程全部失真 | M22 标定步骤；控制点或 OSM 道路宽度回归；尺度写入 World manifest |
| 外观分辨率有限（town 纹素 13.8 cm、三角形 1.76 m） | 低空画面糊、树木与车辆是多面体，检测 / Re-ID 学到伪特征 | 发布"推荐最小斜距"；近景任务换高密度资产；数据集标注资产分辨率 |
| 摄影测量树冠为实心体 | 遮挡率偏高（被挡像素 51% 来自高起物） | 植被语义 + 统计分开；可选随机镂空 |
| 光照烘焙在贴图中 | 无法做时间变化与真实雨天光照；实体着色与背景不一致 | 估计太阳方位；实体按同一太阳着色；雨天只做全局变暗 |
| nvdiffrast 许可（研究与评估） | 不能进入产品 | 产品化走 GL / Vulkan / gz Ogre2 |
| 3DGS 视角外失真与瓦片边界空洞 | Agent 看到伪影；5–10 m 低空比同分布低 6–7 dB | 训练视角覆盖飞行包络；相邻瓦片同时驻留；标签走网格深度 |
| 3DGS 期望深度做遮挡 | 实体穿墙（误显示 14.3%） | 禁止用于遮挡与标签（C3） |
| CPU 编码瓶颈（1080p 每帧约 82 ms 单线程） | 多机批量时 GPU 空等 | nvJPEG、深度 f16 + zstd、标签降低压缩级别、编码与渲染流水线化 |
| 无头 Chromium 默认 SwiftShader、CDP 导出 240–460 ms/帧 | 若误用作服务端渲染，吞吐下降两个数量级 | 只用于前端一致性校验；必须显式 `--use-angle=gl-egl` |
| pyrender 主点偏移约 0.3 px、深度厘米级误差；Open3D 色彩管线不直通 | 跨后端对拍失败 | 不选用；若用需单独标定 |
| Blender 首帧编译 5–25 s，Cycles 不开 persistent data 每帧 4–6 s | 批量时极慢 | 常驻进程 + persistent data；只做金标准小集 |
| 共享 GPU 争用 | 计时波动（本次 GPU 2 上 nvdiffrast 720p 单帧首轮 42 ms、复测 5.7–5.9 ms） | 生产 Worker 独占 GPU；基准测试记录同卡其他进程的显存与利用率 |
| 骨骼动画未实现 | 人与狗是静态姿态，动作类事件不可识别 | Event Runtime 输出关节变换，渲染端 LBS |

---

## 11. 可复用实现与原始结果

脚本目录 `/data/projs/anet-drone/.cache/research/d3/r3-camera-render/`（dmax 镜像目录 `/data/anet-drone4d-d3/r3-camera-render/work/`）：

| 文件 | 内容 | 复用建议 |
|---|---|---|
| `common.py` | 无依赖 GLB 解析器（场景图变换、多材质、纹理解码）、Y-up → ENU、米制缩放、多纹理图集、`Cam`（FLU / RDF / COLMAP，nvdiffrast / GL / Blender 投影矩阵，四元数） | M22 导入器与 M25 的相机约定可直接移植 |
| `nvr.py` | nvdiffrast 静态 + 动态网格渲染（RGB、平面深度、实例、语义，批量视角）、正射 DSM / 正射影像 | 研究原型、3DGS 合成实验、DSM 派生 |
| `exp3_gl_bench.py` | moderngl/EGL MRT 渲染器（RGB8 + R32F z + R16UI id，一次 draw），pyrender / Open3D 对照 | M25 GL 后端原型 |
| `fx.py` | 雾、雨（消光 + 雨线 + 湿润）、动态实体阴影图、传感器噪声 | M25 后处理链 |
| `actors.py` | glTF 动态实体归一化（z-up、底面中心、按类别尺寸缩放、多材质图集） | Event Runtime 资产规范 |
| `exp1_probe.py`、`exp8_asset_stats.py` | DSM / DTM / 逐三角形语义启发式；米制分辨率体检 | M22 体检步骤 |
| `poses.py`、`check_actors.py`、`actors_town.json` | 由 DSM / DTM 生成航拍位姿、实体落地高度；实体近景目检；town 中心 5 个实体的摆放 | Event Tape 锚点与采样工具 |
| `exp5_gs_train.py`、`exp5b_gs_eval.py` | 网格 → 3DGS（面元初始化、MCMC、掩膜损失、多高度评估、渲染计时） | P3 3DGS 流水线起点 |
| `exp6_composite.py` | 三种合成方案的正确性评测、可见度标签、遮挡物类别 | M27 可见度真值生成 |
| `exp4_blender.py` | Blender 4.2 无头 GLB 渲染（unlit 改 Emission、内参 / 位姿映射、EEVEE / Cycles、persistent data） | 金标准离线渲染 |
| `threejs/bench.html`、`run_bench.py` | three.js r186 无头渲染 + 深度对拍 | 前端一致性回归 |
| `exp7_encode.py` | 编码代价 | 预算 |
| `run.sh`、`docker/Dockerfile` | 容器启动与镜像 | 复现实验 |

原始结果：`results/`（JSON 与样张；EXR、npy 与大图保留在 dmax `work/out/`）。主要文件：`probe/*.json`、`asset_stats.json`、`nv/*_bench*.json`、`gl/*.json`、`blender*/*.json`（dmax）、`three_bench.json`、`gs/*_train.json`、`gs/*_eval.json`、`comp/town_composite*.json`、`comp/town_visibility_rows.json`、`encode_bench.json`。

复现命令（dmax）：

```bash
cd /data/anet-drone4d-d3/r3-camera-render/work
./run.sh 3 probe python3 exp1_probe.py town hilltop          # 尺度、DSM/DTM、三角形语义
./run.sh 3 nv    python3 exp2_nv_bench.py town [8192]        # nvdiffrast 吞吐与样张
./run.sh 3 gl    python3 exp3_gl_bench.py town moderngl      # 或 pyrender / open3d
BL_PERSIST=1 CYCLES_SPP=16 ./run.sh 3 blender /opt/blender/blender -b --factory-startup -P exp4_blender.py -- \
    /data/an_entire_small_town_scanned_by_a_drone.glb CYCLES 1280 720 4 /work/out/blender /work/out/nv/town_cams.json "$OFF" 4.0
./run.sh 3 gs    python3 exp5_gs_train.py town 80 1500000 3000000 30000 10 _v2
./run.sh 3 gs    python3 exp5b_gs_eval.py town _v2
./run.sh 3 comp  python3 exp6_composite.py town
```

其中 `OFF` 为 `out/probe/town.json` 的 `offset_enu`。three.js 测试需 `--network host` 外加宿主下载的 playwright wheel（容器内 DNS 不可用），命令见 `threejs/run_bench.py` 头部注释与本节日志 `logs/three.log`。

镜像保留：`d3-r3-camera-render:v1`（dmax）；其余数据：`/data/anet-drone4d-d3/r3-camera-render/{blender-4.2.3-linux-x64, wheels, assets}`。
