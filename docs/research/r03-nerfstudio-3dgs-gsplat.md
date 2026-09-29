# R03 研究笔记：nerfstudio / 3DGS / gsplat —— Visual World 升级路径

> 研究单元：r03 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §8（世界的两种表达）、§14–16（点云 Web 渲染）、§35（Simulation Backend）、§41（World Package）、§43–50（路线图）
> 仓库快照（shallow clone，仅 1 个 commit，无法看历史）：
> - `refs/recon/gsplat` @ `512d366`（2026-09-19，★5736，Apache-2.0，`gsplat/version.py: __version__ = "1.6.0"`，main 未发 PyPI）
> - `refs/recon/nerfstudio` @ `50e0e3c`（2025-07-28，★12032，Apache-2.0，`pyproject.toml: version = "1.1.5"`，pin `gsplat==1.4.0`、`viser==1.0.0`）
> - `refs/recon/gaussian-splatting` @ `54c035f`（2024-10-30，★24003，Inria 非商用许可，本项目忽略许可）。子模块 `diff-gaussian-rasterization`、`simple-knn`、`fused-ssim`、`SIBR_viewers` **没有被 clone**，目录为空。
>
> 跨单元对照（只用于 Web 格式选择，不在本单元深挖）：`refs/web3d/three.js` @ `110fbbe`（r186，2026-09-28）、`refs/web3d/spark` @ `9672638`（v2.2.0，2026-09-25）、`refs/web3d/splat` @ `ba182b5`、`refs/web3d/cesium` @ `b3155a8`、`refs/world/3d-tiles`。
>
> 本文路径均相对各自仓库根目录。本机实测数据（UrbanScene3D 统计）的脚本在 `/data/projs/anet-drone/.cache/research/r03_knn.py`、`r03_bbox.py`。凡是估算都会标注"估算"。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **gsplat** | CUDA 3DGS 光栅化与训练库，Python API。2026 年仍高频迭代：3DGUT、LiDAR 渲染、MCMC、稀疏光栅化、HiGS 推理打包、Gaussian ID 光栅化 | **adopt**：离线 GPU Worker 的 3DGS 训练与导出引擎（`rasterization`、`MCMCStrategy`、`export_splats`、`PngCompression`、`init_utils`）。**port**：分块量化打包（compressed PLY）、Morton 排序、opacity-aware 足迹公式，移植到我们的点云瓦片编码和 Web shader | V0.3（移植算法进 MVP）；V0.6–V0.8（3DGS 训练导出）；V1.0（服务端神经传感器仿真） | ★★★★★ |
| **nerfstudio** | NeRF/3DGS 全家桶：数据处理 CLI、训练框架 splatfacto、viser Web viewer、导出器 | **reference**：`transforms.json` 数据契约、`dataparser_transforms` 坐标反变换、**Render State Machine（按吞吐自适应分辨率）**、OBB 裁剪导出。**port**：state machine → 点云自适应密度控制器（MVP 直接用）。**skip**：不作为训练主引擎，它 pin 的是旧版 gsplat 1.4.0 | V0.1（控制器移植）；V0.6（数据契约） | ★★★☆ |
| **gaussian-splatting (Inria)** | 3DGS 原论文官方实现，2024-10 后冻结 | **reference**：标准 3DGS PLY 字段定义（事实标准）、densify/prune 语义、单目深度 scale/offset 对齐（`make_depth_scale.py`）、SH 常数。**skip**：训练与 SIBR viewer 都不用 | V0.6（深度对齐算法）；全程（PLY 契约） | ★★☆ |

**关键结论（实现者先读这几条）：**

1. **MVP（V0.1–V0.3）不训练 3DGS。** 本机没有 GPU，下载的 UrbanScene3D 也只有 `xyz + normal` 点云，**没有颜色，也没有带位姿的图像**（实测见 §3.6），不具备 3DGS 训练条件。本单元对 MVP 的价值在于**四个可以直接移植的算法**：
   - 自适应密度控制器（nerfstudio render state machine，§3.5）
   - 分块量化编码（gsplat compressed PLY，§3.3）
   - 渐进排序（Morton 排序加重要性排序，§3.4）
   - 法线定向 surfel 点精灵（EWA 投影思想的不透明版本，§3.6）
2. **Web 端 3DGS 渲染不要自己写，也不要选 Spark 作主渲染器。** three.js r186 已经内置 `examples/jsm/objects/GaussianSplat.js`（TSL 实现，**只支持 WebGPURenderer**，也支持其 `forceWebGL` 回退），并带 SPZ/PLY/SPLAT/KSPLAT/glTF `KHR_gaussian_splatting` 加载器，和我们的 WebGPURenderer 主栈一致。Spark 2.x 的 LoD/分页流式最成熟，但**只接受 `THREE.WebGLRenderer`**（`src/SparkRenderer.ts:30`），与主栈冲突，所以只作参考或备选演示模式。
3. **格式分层：**
   - 训练母版用 **3DGS PLY（float32，Inria 字段）**
   - Web 分发用 **SPZ**（three.js 原生支持 v1–v4）
   - 标准化与 GIS 用 **3D Tiles + glTF `KHR_gaussian_splatting` + `KHR_gaussian_splatting_compression_spz_2`**。Cesium 本地源码已实现 `GaussianSplat3DTileContent.js`，3D Tiles "next 2.0" 路线图已写入
   - `.splat` 只当预览，Spark RAD 只当备选
4. **坐标精度是硬约束。** 两个上限：
   - SPZ 位置是 24-bit 定点（带 `fractional_bits`，常用 12 → 范围约 ±2048 m）
   - Spark `PackedSplats` 中心点是 float16
   UrbanScene3D Shanghai 的范围是 7.7 km × 6.2 km，两者都会溢出或出现条带。结论：**`visual/gaussian/` 必须按瓦片存储，采用瓦片局部 ENU 坐标，瓦片半径 ≤ 1 km，推荐 256–512 m 一块**，瓦片原点写进 manifest。
5. **训练坐标系陷阱：**
   - gsplat `simple_trainer` 默认 `normalize_world_space=True`（带旋转，`datasets/colmap.py:277–303`）
   - nerfstudio dataparser 默认 `orientation_method="up"`、`auto_scale_poses=True`
   两者导出的高斯都**不在原世界系**。旋转过的高斯要回到 ENU，必须旋转 SH 系数（需要 Wigner-D 矩阵，工程上容易错）。**规定：先把数据变换到瓦片局部 ENU，再用 `normalize_world_space=False` 训练，训练后只允许平移。**
6. **3DGS 永远只属于 Visual World，不是物理权威。** 碰撞、规划、传感器几何以 Geometry World（点云/Mesh/Voxel/SDF）为准。3DGS 可以向下反哺三件事：
   - RGB/深度相机仿真（gsplat 服务端渲染）
   - LiDAR 仿真（gsplat 2026-03 起支持 LiDAR 光栅化）
   - Gaussian ID 拾取与语义
7. **推荐排序：** gsplat（adopt）> nerfstudio（reference + 移植 state machine）> Inria 3DGS（reference 字段契约）。

---

## 1. 仓库概览

| 项 | gsplat | nerfstudio | gaussian-splatting |
|---|---|---|---|
| Stars / 最后提交 | ★5736 / 2026-09-19（2026 年高活跃） | ★12032 / 2025-07-28（维护期） | ★24003 / 2024-10-30（冻结） |
| 语言 | Python + CUDA（`gsplat/cuda/csrc/` 共 77 个文件，其中 `.cu` 36 个） | Python（依赖 gsplat、viser、tyro） | Python + CUDA 子模块 |
| 核心依赖 | `torch>=2.7`、ninja、jaxtyping；examples 用 `torch==2.9.1`、pycolmap、fused-ssim、ppisp | `torch==2.7.1`（pixi）、`gsplat==1.4.0`、`viser==1.0.0`、open3d | PyTorch 1.12 时代的 environment.yml、SIBR（C++/CMake） |
| 构建 | 首次运行 JIT 编译 CUDA，或按 pt/cu 版本装预编译 wheel | pip 或 pixi；需要 CUDA | `--recursive` clone 子模块（gitlab.inria.fr）后 `pip install submodules/*` |
| GPU 需求 | 必须有 CUDA。Mip-NeRF360 garden 7k 步约 4–7.5 GB；城市级分块另算 | 同 gsplat | 显存比 gsplat 多约 4 倍（gsplat README 自述） |
| 许可 | Apache-2.0 | Apache-2.0 | Inria 非商用（本项目忽略） |
| 本项目定位 | 3DGS 训练、压缩、导出、服务端渲染与传感器仿真的**引擎** | 数据契约、viewer 交互范式的**参考** | 字段标准与算法语义的**参考** |

gsplat 2026 年的更新（README "News"）中，和本项目有关的有：
- 2026-01：PPISP（曝光/ISP 补偿，适合无人机自动曝光视频）
- 2026-03：LiDAR 光栅化，支持旋转式 LiDAR 模型、`eval3d`、depth/hit-distance 模式
- 2026-03：3DGUT 支持外部畸变、per-ray 输入（`rays=[C,H,W,6]`）
- 2026-04：AccuTile/SNUGBOX 紧致椭圆–tile 求交；NCore v4 点云加载
- 2026-05：HiGS 推理渲染（fp16 打包）
- 2026-06：Gaussian ID 光栅化；新的 sensors 库（pinhole/FTheta/fisheye/LiDAR）
- 2026-07：稀疏光栅化、多 GPU dense 路径

---

## 2. 源码结构与关键模块

### 2.1 gsplat

```text
gsplat/
├── rendering.py          # rasterization() 主入口 L234；rasterization_2dgs() L1358；inria wrapper L1199
├── exporter.py           # export_splats(): ply / splat / ply_compressed；load_ply_to_splats()
├── compression/          # PngCompression（PLAS 排序 + PNG 量化 + SH k-means）；sort.py
├── strategy/             # default.py: DefaultStrategy；mcmc.py: MCMCStrategy；ops.py
├── init_utils.py         # multi_frame_depth_unprojection()、knn_scale_init()（2026 自 G-SHARP 移植）
├── cuda/
│   ├── _torch_impl.py    # 纯 PyTorch 参考实现：_persp_proj L53、_fully_fused_projection L262、
│   │                     # _isect_tiles L356、_rasterize_to_pixels L814、_spherical_harmonics L1052
│   ├── _constants.py     # ALPHA_THRESHOLD=1/255, MAX_ALPHA=0.99, TRANSMITTANCE_THRESHOLD=1e-4
│   ├── _lidar.py         # SpinningLidarModelParameters 等 LiDAR 模型
│   └── csrc/             # ProjectionEWA3DGSFused.cu、IntersectTile.cu（AccuTile/SNUGBOX）、
│                         # RasterizeToPixels3DGS*.cu、MCMCPerturbCUDA.cu、ProjectionUT3DGSFused.cu …
├── scene/                # GaussianScene / GaussianInferenceScene（HiGS 打包：means fp32 planar + qso fp16[N,8]）
├── stage/                # Stage：多 scene 组合渲染（examples 已改用 self.stage.render(scene_id, …)）
├── sensors/              # cameras/lidars 模型与投影 kernel（2026-06）
└── experimental/         # render_scene / GaussianInferenceScene（HiGS）
examples/
├── simple_trainer.py     # Config L79；create_splats_with_optimizers L292；Runner；PLY 导出 L1085–1115；压缩 L1422
├── av_trainer.py         # 多相机 + LiDAR 监督 + 天空 mask 的自动驾驶训练器（与无人机+MID-360 同构）
├── datasets/colmap.py    # Parser L121：pycolmap 读取、normalize（similarity + PCA + 翻转）L277–303
├── simple_viewer.py      # viser/nerfview viewer；--scene_grid 大场景压测；radius_clip
└── benchmarks/compression/mcmc.sh  # cap_max 0.36M/0.49M/1M/4M 压缩基准
```

**光栅化管线**（`rendering.py::rasterization` → `_rasterization`，参考实现在 `cuda/_torch_impl.py`）：

1. `quat_scale_to_covar_preci`：Σ = R S Sᵀ Rᵀ。四元数约定 **wxyz**（`cuda/_math.py::_quat_to_rotmat` 先 `F.normalize`）。
2. `fully_fused_projection`：做世界到相机变换，再做 EWA 投影 Σ₂ = J W Σ Wᵀ Jᵀ。其中 J 是透视雅可比，tx/ty 先按视锥 ±1.3·tan(fov/2) 截断（`_persp_proj` L91–96）。之后：
   - 加低通 `eps2d=0.3`
   - 取 conic = Σ₂⁻¹
   - 半径 `radius = ceil(3.33·sqrt(diag(Σ₂)))`（`GAUSSIAN_EXTEND=3.33f`，`cuda/include/Common.h:99`）
   - 近远平面与屏幕外剔除（L336–352）
3. `isect_tiles`：生成 16×16 tile 与 (tile_id, depth) 的 64-bit key，再做 radix sort。2026-04 起用 **SNUGBOX + AccuTile**（`csrc/IntersectTile.cu` L80–340），用不透明度决定等值线：t = min(3.33², 2·ln(o / (1/255)))，据此求椭圆的紧 AABB。
4. `rasterize_to_pixels`：每个 tile 从前到后做 α 混合：α = min(0.99, o·exp(−½dᵀΣ₂⁻¹d))；α < 1/255 跳过；T < 1e-4 提前终止。
5. `radius_clip`：投影半径小于 N 像素的高斯直接丢弃。官方大场景示例（`docs/source/examples/large_scale.rst`）靠这一招把 30M 高斯（9×9 复制 garden）做到实时。它本质上是**屏幕空间 LOD 阈值**。

**致密化策略参数**（对 web 预算很关键）：

| 策略 | 关键参数（默认） | 语义 |
|---|---|---|
| `DefaultStrategy`（`strategy/default.py:32`） | `prune_opa=0.005, grow_grad2d=2e-4, grow_scale3d=0.01, prune_scale3d=0.1, refine_start/stop=500/15000, reset_every=3000, refine_every=100, absgrad=False` | 原论文的 clone/split/prune/opacity reset。scale 阈值按 `scene_scale` 归一化 |
| `MCMCStrategy`（`strategy/mcmc.py:40`） | `cap_max=1_000_000, noise_lr=5e5, refine_start/stop=500/25000, min_opacity=0.005` | 把低不透明度高斯"传送"到高不透明度处，总数**硬上限 cap_max**，最适合按 Web 预算控制数量 |

`EXPLORATION.md` 实测过**航拍大学场景 U1/U4（LocalRF 数据）**：30k 步默认参数下 U1 有 4.18M 高斯，PSNR 24.67；`--absgrad --grow_grad2d 8e-4` 降到 2.37M，PSNR 24.65。**absgrad 能把高斯数砍半而画质不变**，这是航拍场景的首选。U1 配方用了 `--grow_scale3d 0.001`，因为大场景 `scene_scale` 大，必须收紧。

**导出**（`exporter.py`）：

| 函数 | 格式 | 每个 splat 的字节布局 |
|---|---|---|
| `splat2ply_bytes` | 标准 3DGS PLY | `x y z f_dc_0..2 f_rest_0..44 opacity scale_0..2 rot_0..3`，全 float32，SH3 时 59×4 = **236 B**（无 nx/ny/nz）。opacity 是 logit，scale 是 log，rot 是 wxyz 未归一化 |
| `splat2splat_bytes` | antimatter15 `.splat` | 位置 3×f32 + exp(scale) 3×f32 + RGBA u8×4（SH0 + sigmoid(opa)）+ 四元数 u8×4（q·128+128）= **32 B**。按 Morton 排序 |
| `splat2ply_bytes_compressed` | PlayCanvas/SuperSplat compressed PLY | 先按 Morton 排序，**每 256 个一块**，每块 18 个 float（位置/scale/颜色的 min、max）。每个 splat 4×u32：位置 11/10/11、四元数 2+10+10+10（smallest-three）、scale 11/10/11、RGBA 8888；SH 每系数 uint8（`(sh/8+0.5)·256`）。约 **16.3 B + SH 45 B** |

`load_ply_to_splats()` 是 `splat2ply_bytes` 的逆过程（`f_rest` 按 **channel-major** 解释：先 R 的 15 个，再 G，再 B）。

**压缩**（`compression/png_compression.py`）是 SOGS（Self-Organizing Gaussians）思路：
- 数量裁成完全平方数
- PLAS 做 2D 网格自组织排序
- `means` 先做 log 变换 `sign(x)·log1p(|x|)`，再存 16-bit（拆成 `_l.png`、`_u.png` 两张）
- scales/quats/opacities/sh0 存 8-bit PNG
- shN 用 k-means 聚成 65536 个中心（6-bit 量化），labels 存 uint16
- 官方数据：1M 高斯从 236 MB 压到 16.5 MB，PSNR 29.18 → 28.65
- 依赖 `plas`、`torchpq`、`cupy`，只能在 GPU 上跑

**初始化**（`init_utils.py`，2026 新增）：
- `multi_frame_depth_unprojection(images, depths, masks, poses_c2w, Ks, max_points)`：多帧深度反投影成世界点云加 RGB。**和 LingBot-Map 的 depth/conf/pose/K 输出正好对上**。
- `knn_scale_init(xyz, k=3)`：初始 log-scale = log(rms(kNN 距离))。纯 torch 实现，O(N²) 分块，**只适合 ≤10⁵ 点**。更大的规模用 `examples/utils.py::knn`（sklearn）或 KD-tree。

`simple_trainer.create_splats_with_optimizers`（L292）：
- 支持三种 `init_type`：`sfm`、`lidar`、`random`
- scale = log(√mean(d²₁..₃))；quats 随机；opacity = logit(0.1)（MCMC 配置用 0.5）
- sh0 = (rgb − 0.5)/C0，shN 置零
- 各参数学习率：means 1.6e-4·scene_scale、scales 5e-3、opacities 5e-2、quats 1e-3、sh0 2.5e-3、shN 2.5e-3/20
- batch 时按 √BS 缩放
- `scene_scale = max‖cam − center‖ × 1.1 × global_scale`（L464，`datasets/colmap.py:441`）

**av_trainer.py**：
- NPZ 场景契约 `load_scene_npz`：`images[F,Ncam,H,W,3]`、`cam_intrinsics[Ncam,4]`、`cam_to_worlds`、`lidar_points`、`lidar_frame_indices`、`is_test`、`camera_names`
- LiDAR 渲染监督（`camera_model="lidar"` + `lidar_distance_loss`）
- `estimate_sky_mask()`（L340，按亮度和高度比例估天空）加天空惩罚
- NCore 基准：MCMC +1.5 dB，SH3 +1.4 dB，LiDAR +0.2 dB
- 这是 **"P600 视频 + MID-360 → 3DGS"最接近的现成模板**

### 2.2 nerfstudio

- **`models/splatfacto.py`**：
  - `SplatfactoModelConfig`（L86）要点：`cull_alpha_thresh=0.1`、`densify_grad_thresh=8e-4`、`use_absgrad=True`、`resolution_schedule=3000`（从 1/4 分辨率起步，逐级翻倍）、`strategy: default|mcmc`、`max_gs_num=1M`
  - `populate_modules`（L189）用 `seed_points` 初始化：kNN(3) 平均距离取 log 作 scale；`RGB2SH`
- **`data/dataparsers/nerfstudio_dataparser.py`**：
  - 读 `transforms.json`：相机内参 `fl_x/fl_y/cx/cy/w/h/k1..`；`frames[]` 里有 `file_path`、`transform_matrix`（**c2w，OpenGL 约定**：colmap 转换时 `c2w[0:3,1:3] *= -1`，见 `process_data/colmap_utils.py` L446）、`depth_file_path`、`mask_path`；另有 `ply_file_path`（种子点）、`applied_transform`
  - 默认 `orientation_method="up"`、`center_method="poses"`、`auto_scale_poses=True`（缩到 ±1）
  - `load_3D_points`（L352）从 `ply_file_path` 读种子点（open3d），做 transform×scale
- **`scripts/exporter.py::ExportGaussianSplat`**（L484）：
  - 导出 Inria 字段 PLY，写入 `comment Vertical Axis: z`
  - 过滤 NaN/Inf，以及 `opacity < logit(1/255) = −5.5373` 的高斯
  - 支持 OBB 裁剪（`obb_center/rotation/scale`）
  - **注意：导出的是 dataparser 归一化后的坐标，不会自动反变换**
  - 逐顶点 Python 循环写文件，百万级很慢
- **`viewer/render_state_machine.py`**（L55）：**本单元对 MVP 最有价值的一段代码**。
  - 三个状态 `low_move → low_static → high`，动作 `move/static/step/rerender`
  - 空闲 0.2 s 自动触发 `static`
  - 低分辨率状态按吞吐计算：`num_vis_rays = vis_rays_per_sec / target_fps(30)`，`H = sqrt(num_vis_rays / aspect)`，取整到 10，clamp 到 [30, max_res]
  - 高分辨率状态直接用 `max_res`
  - 相机一动就中断正在进行的高分辨率渲染（`check_interrupt`）
- **`process_data/`**：`video_to_nerfstudio_dataset.py`（ffmpeg 抽帧）、colmap/hloc（`matching_method="vocab_tree"`、`refine_intrinsics=True`）、odm/metashape/realitycapture/polycam 转换器

### 2.3 graphdeco-inria/gaussian-splatting

- **`scene/gaussian_model.py`**：
  - `create_from_pcd`（L149）：scale = log(√distCUDA2)，rot = (1,0,0,0)，opacity = logit(0.1)，SH DC = RGB2SH
  - `save_ply`（L239，字段见 `construct_list_of_attributes`：`x y z nx ny nz f_dc_* f_rest_* opacity scale_* rot_*`，这就是**事实标准 PLY 字段**）
  - `densify_and_split`（L409，N=2，新 scale = s/(0.8N)，按高斯采样新中心）、`densify_and_clone`、`densify_and_prune`（剔除屏幕半径大于 max_screen_size，或 scale 大于 0.1·extent 的高斯）
- **`arguments/__init__.py::OptimizationParams`**：`iterations=30000`、`position_lr 1.6e-4→1.6e-6`、`densify 500–15000 / 100`、`opacity_reset_interval=3000`、`densify_grad_threshold=2e-4`、`depth_l1_weight 1.0→0.01`
- **`utils/make_depth_scale.py::get_scales`**：单目逆深度与 COLMAP 稀疏点逆深度做稳健对齐：`t = median`，`s = mean|x − t|`（MAD），`scale = s_colmap/s_mono`，`offset = t_colmap − t_mono·scale`。**可以直接用来把 LingBot 深度对齐到 LiDAR 或 RTK 尺度。**
- **`utils/sh_utils.py`**：C0 = 0.28209479…，C1 = 0.48860251…，C2[5]、C3[7]、C4[9] 常数与 `eval_sh`（deg ≤ 4）。Web 着色器移植时照抄。
- **不做世界归一化**（只用 `getNerfppNorm` 的半径缩放学习率），导出坐标就是 COLMAP 世界系。这一点比 gsplat/nerfstudio 的默认行为更适合做数字孪生。

### 2.4 Web 端对照（跨单元，仅与格式选择相关）

| 渲染器 | 渲染后端 | 排序 | LoD/流式 | 格式 | 结论 |
|---|---|---|---|---|---|
| three.js r186 `GaussianSplat`（`examples/jsm/objects/GaussianSplat.js`） | **WebGPURenderer**（支持 `forceWebGL` 回退；不支持 WebGLRenderer） | GPU counting sort（`gpgpu/CountingSort.js`，4096 bins）；视线方向变化 dot < 0.9995 才重排；2σ cutoff；`KERNEL_2D_SIZE=0.3`；最大屏幕尺寸 1024 px；支持 raycast（不透明度 ≥ 0.2） | 无（单对象） | `SPZLoader`（v1–3 gzip，v4 zstd）、`GaussianSplatPLYLoader`、`SPLATLoader`、`KSPLATLoader`、`GLTFGaussianSplatLoaderExtension`（`KHR_gaussian_splatting`） | **主选**：与 WebGPURenderer 主栈一致。LoD 由我们的瓦片层实现 |
| Spark 2.2（`refs/web3d/spark`） | **仅 WebGLRenderer**（`SparkRenderer.ts:30`） | Rust/WASM worker 排序 | **最成熟**：RAD 分块 LoD（`npm run build-lod -- --quality --rad-chunked`）、`paged:true` 按需拉取、`lodSplatCount`（桌面 2.5M、Android 1M、iOS 1.5M）、注视点（foveation）参数 | PLY/SPZ/SPLAT/KSPLAT/SOGS/RAD | **参考或备选**（单独的"高保真预览"页）。`build-lod` 可离线生成 RAD |
| antimatter15/splat（`main.js`） | WebGL2 | Worker 里做 16-bit 单遍 counting sort；视线 dot 在 0.99 以内跳过 | 无；但**按 size×opacity 降序排列文件**，任意前缀都能渲染 | `.splat` | 最小实现参考（着色器、重要性排序） |
| Cesium（`GaussianSplat3DTileContent.js`、`GltfSpzLoader.js`） | WebGL2 | `GaussianSplatSorter.js` | **3D Tiles 层级 LoD** | glTF `KHR_gaussian_splatting` + `KHR_gaussian_splatting_compression_spz_2` | GIS 标准路线（V1.0+） |

---

## 3. 可复用算法与实现（含伪代码/参数）

### 3.1 统一数据契约：3DGS 参数化与各家约定

| 字段 | 存储值（PLY/训练参数） | 渲染时激活 | 约定陷阱 |
|---|---|---|---|
| `means` | 世界坐标 xyz | — | **必须是瓦片局部 ENU**（§3.9） |
| `scales` | log σ（3 轴） | exp | `.splat` 存的是 exp 后的值 |
| `quats` | wxyz，未归一化 | normalize | three.js `Quaternion(x,y,z,w)`；LingBot-Map 是 XYZW（见 r01）；`.splat` 存 u8 (q·128+128) |
| `opacities` | logit | sigmoid | 导出时丢弃 logit < −5.5373 的高斯 |
| `sh0` / `f_dc_0..2` | SH DC | rgb = 0.5 + C0·sh0 | C0 = 0.28209479177387814 |
| `shN` / `f_rest_*` | deg 3 时 45 个 | eval_sh(dir) | **PLY 是 channel-major**（R15, G15, B15）；gsplat 张量是 `[N,K,3]` basis-major |
| 相机 | gsplat 用 c2w/viewmat（OpenCV）；nerfstudio `transform_matrix` 用 c2w（OpenGL，y/z 取反） | — | nerfstudio `applied_transform` 默认交换 y/z |

### 3.2 投影与足迹公式（Web 着色器移植用）

```text
输入：μ(world), q(wxyz), s=exp(log_s), o=sigmoid(logit), V(view 4x4), fx, fy, W, H
R = quat_to_rotmat(normalize(q))
Σ = R·diag(s²)·Rᵀ
t = V·[μ,1]                                   # 相机系，OpenCV 约定 z>0 朝前（three.js 需 z 取反）
tx' = clamp(tx/tz, -1.3·tanfx, 1.3·tanfx)·tz  # 同理 ty'；tanfx = W/(2fx)
J = [[fx/tz, 0, -fx·tx'/tz²],
     [0, fy/tz, -fy·ty'/tz²]]
Σ₂ = J·V₃ₓ₃·Σ·V₃ₓ₃ᵀ·Jᵀ + 0.3·I                # 0.3 = eps2d/KERNEL_2D_SIZE 低通
conic = Σ₂⁻¹ = [c/det, -b/det, a/det]         # Σ₂ = [[a,b],[b,c]]
# 紧致足迹（gsplat SNUGBOX，opacity-aware）：
T  = min(3.33², 2·ln(o·255))                  # o<1/255 直接剔除
ex = sqrt(T·Σ₂[0][0]);  ey = sqrt(T·Σ₂[1][1])  # 即 gsplat 的 sqrt(-T/disc·C)，disc = B²-AC = -1/det(Σ₂)
# antimatter15/three.js 的特征分解版（instanced quad 顶点着色器用）：
mid = (a+c)/2; r = length(((a-c)/2, b)); λ1 = mid+r; λ2 = mid-r
axis1 = normalize((b, λ1-a))·min(k·sqrt(λ1), 1024);  axis2 = perp(axis1)·min(k·sqrt(λ2), 1024)
# k: three.js=2（2σ），antimatter15=√2·2；gsplat=3.33（训练用，更保守）
片元：q = dᵀ·conic·d;  α = min(0.99, o·exp(-½q));  if α<1/255 discard;  输出预乘 (α·rgb, α)
LOD 剔除（gsplat radius_clip）：max(ex,ey) < radius_clip_px → 不渲染（Web 建议 0.5–1 px）
```

### 3.3 MVP 可直接用 A：点云瓦片的分块量化编码（移植自 compressed PLY）

gsplat `splat2ply_bytes_compressed` 的核心是"**Morton 排序 → 每 256 个一块 → 块内按 AABB 做归一化定点**"。这套方法原样适用于点云瓦片。

**MVP 默认方案**（简单、稳健）：节点局部 `uint16×3`，精度 = 节点尺寸 / 65535。7.7 km 根节点约 0.12 m；第 4 级约 7 mm。

**V0.2+ 可选紧凑方案**（位置 4 B/点）：

```python
# encoder（Python/numpy，World Pipeline 离线执行）
def encode_node(points_xyz, normals, rgb=None, chunk=256, max_chunk_extent=32.0):
    order = morton_argsort(points_xyz, bits=10)          # gsplat exporter.sort_centers 同算法
    P, N = points_xyz[order], normals[order]
    chunks, words = [], []
    for c0 in range(0, len(P), chunk):
        p = P[c0:c0+chunk]; lo, hi = p.min(0), p.max(0)
        u = (p - lo) / np.maximum(hi - lo, 1e-9)
        if (hi-lo).max() > max_chunk_extent:            # Morton 跳变保护：该块改存 uint16×3（mode=1）
            mode, w = 1, (np.round(u*65535).astype('<u2'))
        else:                                           # mode=0：x11 | y11 | z10（z=up 给 10 bit）
            mode, w = 0, (q(u[:,0],11) << 21) | (q(u[:,1],11) << 10) | q(u[:,2],10)
        chunks.append((lo, hi, mode)); words.append(w)
    normal_oct = oct_encode_u8x2(N)                      # 2 B/点
    color = pack_8888(rgb) if rgb is not None else None  # 4 B/点（UrbanScene3D 无色，省略）
    return header, chunks(float32×6 each), words, normal_oct, color
def q(v, bits): return np.clip(np.floor(v*((1<<bits)-1)+0.5), 0, (1<<bits)-1).astype(np.uint32)
```

```glsl
// decoder（顶点着色器；chunk 边界用 uniform buffer 或 texelFetch 取 chunkId = gl_VertexID >> 8）
uint w = aPacked;
vec3 u = vec3(float(w >> 21u) / 2047.0, float((w >> 10u) & 2047u) / 2047.0, float(w & 1023u) / 1023.0);
vec3 pos = mix(chunkMin, chunkMax, u);
```

布局提示：mode=1 的块每点 6 B，会破坏固定步长，不利于 GPU 按 `gl_VertexID` 直接寻址。**实现上以节点为单位选方案**：节点内只要有一个块超出 `max_chunk_extent`，整个节点就退回 uint16×3。这样每个节点的 buffer 步长固定，节点头里用 1 byte 标出编码方式。

体积估算（UrbanScene3D 5M 点）：原始 24 B/点 = 120 MB。
- 紧凑方案：4 B 位置 + 2 B 法线 + 块头 24 B/256 → 约 **6.1 B/点，约 31 MB**
- uint16 方案：6 + 2 → 约 8 B/点，约 40 MB

两者都还能再叠加 HTTP brotli/gzip。精度：SF 点距 0.29 m，256 点的块约 4.7 m，11-bit 量化误差约 2.3 mm；NY 点距 1.6 m，块约 26 m，误差约 13 mm（估算）。

### 3.4 MVP 可直接用 B：渐进加载的节点内排序

- **节点之间**：按 LOD 八叉树逐级加载（由 Potree 单元负责）。
- **节点内部**：借鉴 antimatter15 把 splat 按 `size×opacity` 降序存储的做法，让**任意字节前缀都是均匀子采样**。这样可以用一个 HTTP 请求（或 Range 请求）流式到达、边到边画，也可以按预算只取前 k 个点（"节点内部连续 LOD"）。

```python
def order_for_progressive(P, spacing):
    # 1) Morton 排序得到空间局部性（量化块需要）
    # 2) 块级打乱：以 256 点块为单位做 "bit-reversal / 随机" 置换，块内保持 Morton
    #    → 前缀 = 若干完整块的随机子集，空间上近似均匀，同时每块仍可独立反量化
    blocks = np.arange(n_blocks); rng.shuffle(blocks)    # 种子固定，保证可复现
    return concat(block[i] for i in blocks)
# 客户端：renderCount = min(loaded, floor(nodeBudget))；drawRange = [0, renderCount)
```

对 3DGS 瓦片同样适用：导出前按 `importance = sigmoid(o)·exp(s0+s1+s2)` 降序排列，Web 端只画前 k 个作为粗 LOD。

### 3.5 MVP 可直接用 C：自适应点密度控制器（移植 nerfstudio Render State Machine）

nerfstudio 的思路是"**测吞吐 → 按目标帧率反推工作量 → 运动时降、静止时升 → 一动就打断**"。把"光线数"换成"点数"，就是我们要的点云疏密自动调节。

```ts
type RS = 'MOVING' | 'SETTLING' | 'IDLE';
const P = { targetFps: 60, targetFpsSoftware: 30, idleMs: 250, emaAlpha: 0.15,
            bMin: 150_000, bMax: 8_000_000, bMaxSoftware: 1_200_000, bStart: 1_000_000,
            moveFactor: 0.5, upStep: 1.2, downHysteresis: 1.15, upIntervalMs: 400,
            dprMove: 0.75, dprIdle: Math.min(devicePixelRatio, 2) };
class AdaptiveDensityController {
  state: RS = 'IDLE'; budget = P.bStart; ema = 1000 / 60; lastMove = 0; lastUp = 0;
  onCameraChange(now: number) { this.state = 'MOVING'; this.lastMove = now; }  // OrbitControls 'change'
  onFrame(now: number, frameMs: number, renderedPoints: number) {
    this.ema = this.ema * (1 - P.emaAlpha) + frameMs * P.emaAlpha;
    if (this.state !== 'IDLE' && now - this.lastMove > P.idleMs) this.state = 'IDLE';
    const tgtMs = 1000 / (isSoftwareGL ? P.targetFpsSoftware : P.targetFps);
    const ptsPerMs = renderedPoints / Math.max(this.ema, 1);          // 吞吐（nerfstudio: vis_rays_per_sec）
    const sustainable = ptsPerMs * tgtMs * 0.9;                        // num_vis_rays = rays/s ÷ fps
    const cap = isSoftwareGL ? P.bMaxSoftware : P.bMax;
    if (this.ema > tgtMs * P.downHysteresis) {                          // 超时：立即下调（快降）
      this.budget = clamp(sustainable, P.bMin, cap);
    } else if (this.state === 'IDLE' && now - this.lastUp > P.upIntervalMs) {
      this.budget = clamp(Math.min(this.budget * P.upStep, sustainable * 1.1), P.bMin, cap); // 慢升
      this.lastUp = now;
    }
    const effective = this.state === 'MOVING' ? this.budget * P.moveFactor : this.budget;
    renderer.setPixelRatio(this.state === 'MOVING' ? P.dprMove : P.dprIdle);
    return { pointBudget: Math.floor(effective), edl: this.state !== 'MOVING' || this.ema < tgtMs * 0.6 };
  }
}
```

配套规则：
1. **LOD 可见集重算节流**。借鉴 splat 排序的节流：相机平移 < 0.5% 目标距离、且视线 dot > 0.99996（约 0.5°）时复用上一帧的节点集；否则最多每 100 ms 重算一次。
2. **降级顺序**：DPR → 点预算 → EDL → 环境粒子数 → 点尺寸。
3. **中断**：相机一动，就取消"高密度补全"队列里还没发出的请求（对应 nerfstudio 的 `interrupt_render_flag`）。
4. **HUD 显示**：`budget / rendered / fps / state`，用 lieflat-charts 风格的 sparkline。

### 3.6 MVP 可直接用 D：法线定向 surfel 点精灵（不透明、免排序）

**本机实测**（`r03_knn.py`、`r03_bbox.py`；每城 300 个随机查询点，kNN3 RMS 距离）：

| 文件 | 点数 | 范围（x, y, z） | 推测 up 轴 / 单位 | kNN3 RMS p10 / p50 / p90 |
|---|---|---|---|---|
| Chicago | 5.00M | 4.2 × 8.0 × 0.6 | z / **疑似 km**（点距 0.002） | 0.002 / 0.002 / 0.003 |
| New York | 5.00M | 2928 × 3167 × 293 | z / m | 1.01 / 1.63 / 2.25 |
| San Francisco | 5.00M | 740 × 717 × 55 | z / m | 0.20 / 0.29 / 0.45 |
| Shenzhen | 5.00M | 1848 × 1999 × 391 | z / m | 0.48 / 0.79 / 1.34 |
| Suzhou | 5.00M | 4407 × 156 × 687 | **y** / m | 0.33 / 0.55 / 0.77 |
| Shanghai | 5.00M | 7736 × 6211 × 646 | z / m | 1.88 / 2.90 / 4.30 |

所有文件的字段都只有 `x y z nx ny nz`（float32，CloudCompare 导出），**没有颜色**，法线已归一化。

结论：
- **up 轴和单位不统一**（Suzhou 是 Y-up，Chicago 疑似 km）。World Package 的 `coordinate.json` 必须记录 `up_axis` 和 `unit_scale`，导入时统一到 ENU z-up、单位米。
- 点距差异达 10 倍，所以**点尺寸必须按节点 spacing 自适应**，不能用全局常数。

有法线，就可以把每个点画成**朝向法线的小圆盘**，它是扁平高斯（s_z → 0）的不透明近似：不需要排序、不需要 α 混合、能正常写深度，孔洞明显减少。WebGPU 原生点只有 1 px（three.js `PointsNodeMaterial` 注释 L57），所以要用 **instanced quad（Sprite + PointsNodeMaterial）**。下面是按 quad 写的伪代码：

```glsl
// vertex（每实例：p, n, nodeSpacing；每顶点：corner ∈ {(-1,-1),(1,-1),(1,1),(-1,1)}）
vec4 pv = modelView * vec4(p, 1.0);
vec3 nv = normalize(normalMatrix * n);
vec3 d  = normalize(-pv.xyz);                    // 指向相机
float c = max(abs(dot(nv, d)), 0.15);            // 倾角余弦（法线方向不一致时取绝对值）
float r = kRadius * nodeSpacing;                 // kRadius ≈ 0.7–1.0；粗 LOD 节点 spacing 大 → 盘大
float px = r * K / -pv.z;                         // K = H_px / (2·tan(fovy/2))
px = clamp(px, minPx(1.0), maxPx(48.0));
vec2 n2 = length(nv.xy) > 1e-4 ? normalize(nv.xy) : vec2(1, 0);   // 法线在屏幕上的方向 = 椭圆短轴
vOffset = corner; vN2 = n2; vCos = c; vNv = nv; vWorldZ = p.z;
gl_Position = proj * pv;  gl_Position.xy += corner * px / viewport * gl_Position.w;   // px 是直径，NDC 半宽 = px/viewport
// fragment
float a = dot(vOffset, vN2), b = dot(vOffset, vec2(-vN2.y, vN2.x));
if (a*a / (vCos*vCos) + b*b > 1.0) discard;       // 圆盘透视成椭圆：短轴 = cosθ
float shade = 0.55 + 0.45 * max(dot(vNv, lightDir), 0.0);   // 无色点云：高度色带 × 法线光照
out = vec4(heightRamp(vWorldZ) * shade, 1.0);
```

参数：
- `kRadius` 默认 0.8（取 kNN3 RMS 的 0.8 倍）；UI 提供 "Point | Surfel" 切换
- EDL 仍然可以叠加
- 软件 WebGL 下 surfel 比方点多约 30% 的片元开销（估算，因为 quad 面积更大），所以放进自适应控制器的降级链：MOVING 时退回方点

### 3.7 MVP 可选 E：遥测四元数 32-bit 打包（移植 `pack_rotation`）

无人机姿态走二进制 WebSocket 时，可以用 smallest-three 编码把 16 B（4×f32）压到 **4 B**。最大分量的索引占 2 bit，其余 3 个分量各 10 bit，范围 [−1/√2, 1/√2]，角误差约 0.1°（估算）。

```python
def pack_quat(q):                     # q: (x,y,z,w) 或 (w,x,y,z)，协议固定一种
    q = q/np.linalg.norm(q); i = np.argmax(abs(q)); q = -q if q[i] < 0 else q
    rest = np.delete(q, i); u = rest*(np.sqrt(2)/2) + 0.5          # 与 gsplat 一致：×√2/2 再 +0.5
    v = np.clip(np.floor(u*1023+0.5), 0, 1023).astype(np.uint32)
    return (i << 30) | (v[0] << 20) | (v[1] << 10) | v[2]
# decode: rest=(v/1023-0.5)/(√2/2); q[i]=sqrt(max(0,1-Σrest²))
```

核对：非最大分量的取值范围是 [−1/√2, 1/√2]。乘以 √2/2 之后正好落在 [−0.5, 0.5]，加 0.5 后映射到 [0, 1]，10 bit 全部用上，没有浪费。量化步长 = √2/1023 ≈ 0.0014。协议里要写死分量顺序（建议统一用 three.js 的 xyzw），解码端按同一顺序还原。

### 3.8 PointCloud → 3DGS：伪高斯（PointSplat）与训练初始化

**用途：**
- (a) V0.3 无训练的视觉升级，同时把 Gaussian 管线（manifest、loader、tile LoD）端到端跑通
- (b) V0.6 训练时作为比随机初始化更好的"扁平定向"种子（城市立面和屋顶多为平面）

```python
def pointcloud_to_pseudo_gaussians(xyz, normals, rgb=None, k=6, alpha=0.8, flat=0.1,
                                   s_min=0.02, s_max=5.0, opa=0.85):
    d = kdtree_knn(xyz, k+1)[:, 1:]                       # scipy cKDTree（venv 需安装 scipy；open3d 在本机缺 libEGL）
    s = np.clip(alpha*np.sqrt((d**2).mean(1)), s_min, s_max)
    log_scale = np.log(np.stack([s, s, s*flat], 1))       # 盘面在局部 xy，法线为局部 z
    q = quat_z_to(normals)                                # wxyz
    if rgb is None: rgb = height_ramp(xyz[:,2]) * lambert(normals)   # 无色点云的合成色
    f_dc = (rgb - 0.5) / 0.28209479177387814
    logit_o = np.log(opa/(1-opa))
    return export_ply_3dgs(xyz_local, f_dc, f_rest=None, logit_o, log_scale, q)   # 等价 gsplat splat2ply_bytes（SH0）
def quat_z_to(n):                                         # 把 (0,0,1) 旋到 n：q = (1+n·z, z×n) 归一化
    w = 1 + n[:,2]; x = -n[:,1]; y = n[:,0]; z = np.zeros_like(w)
    q = np.stack([w,x,y,z],1); bad = w < 1e-6; q[bad] = [0,1,0,0]
    return q/np.linalg.norm(q,axis=1,keepdims=True)
```

训练初始化时的差异：opacity 取 0.1（Default）或 0.5（MCMC），`flat=1`（各向同性，gsplat 默认），种子点按 0.1–0.2 m 体素下采样，上限 1–3M/瓦片。

### 3.9 Reconstruction → gsplat 训练数据契约与配方（V0.6–V0.8，GPU Worker）

**数据流**（与 r01、r04、r05 对齐）：

```text
P600 视频帧 ──► LingBot-Map（pose/intrinsic/depth/conf/world_points，相对尺度，不对齐重力）
MID-360 ─────► FAST-LIO 地图 + 外参（米制）
RTK ─────────► Sim(3) 地理配准（r01 结论：V0.1 起就要做）
                  │
                  ▼  统一到【瓦片局部 ENU，z-up，米】（只允许平移 + 缩放；旋转在训练前做完）
        gsplat NPZ（av_trainer 契约）或 nerfstudio transforms.json（带 ply_file_path）
                  │
                  ▼  gsplat simple_trainer / av_trainer（MCMC + LiDAR 监督 + 天空 mask + PPISP）
        master/t_{key}.ply（float32，Inria 字段，瓦片局部坐标）
```

**NPZ 字段**（沿用 `av_trainer.load_scene_npz`，另加我们的扩展）：
- `images[F,1,H,W,3] u8`：原始视频帧，不用 LingBot 的 518 px 输入
- `cam_intrinsics[1,4] = fx,fy,cx,cy`：从 LingBot 分辨率按 `sx = W/W_lb`、`sy = H/H_lb` 缩放
- `cam_to_worlds[F,1,4,4]`：OpenCV 约定。**注意 r01 发现 `demo.py` 的 `predictions["extrinsic"]` 实际是 W2C**，要求逆，并用重投影做自检
- `lidar_points[M,3]`、`lidar_frame_indices[M]`：MID-360 去畸变后的点，用于稀疏深度监督
- `is_test[F]`
- 扩展字段：`tile_origin_enu[3]`、`T_enu_from_recon[4,4]`、`sky_masks[F,H,W]`（LingBot `--mask_sky`）

**深度对齐**（移植 Inria `make_depth_scale.get_scales`，逐帧计算）：

```python
inv_ref = 1/z_lidar_at_px; inv_mono = 1/depth_lingbot[px]           # 只取 LiDAR 投影有效像素
t_r, t_m = median(inv_ref), median(inv_mono)
s_r, s_m = mean(|inv_ref-t_r|), mean(|inv_mono-t_m|)
scale = s_r/s_m; offset = t_r - t_m*scale                          # 有效点 ≤ 10 或动态范围 < 1e-3 → 丢弃该帧
```

**航拍配方**（每块 256–512 m；参数全部来自源码默认值加 EXPLORATION 的航拍经验，数值是起点，不是结论）：

| 参数 | 取值 | 依据 |
|---|---|---|
| strategy | `mcmc`，`cap_max` = 1.5M–3M/瓦片 | 数量硬上限，便于 Web 预算；NCore 实测 MCMC +1.5 dB |
| 或 default | `absgrad=True, grow_grad2d=8e-4, grow_scale3d=0.001` | U1 航拍：高斯数减半，PSNR 持平 |
| `normalize_world_space` | **False** | 避免旋转，导出即局部 ENU（§0 第 5 条） |
| `global_scale` | 1.0；scene_scale 过大时调小，让 `prune_scale3d·scene_scale` ≈ 5–10 m | 防止巨型高斯 |
| `init_type` | `lidar`（种子点 = LiDAR 地图 ∪ LingBot conf>p50 的点，0.15 m 体素） | 比 sfm/random 稳 |
| `sh_degree` | 3（母版）；Web 导出截到 1 或 0 | Web 体积与性能 |
| `pose_opt` | True（lr 1e-5） | LingBot 位姿是近似值 |
| `post_processing` | `ppisp`（2026）或 `bilateral_grid` | 无人机自动曝光 |
| `depth_loss` | True，`depth_lambda=1e-2`（LiDAR 稀疏深度，视差空间 L1） | 几何约束，防止漂浮物 |
| `antialiased` | True | 仿真机位与采集机位不同（OOD 视角），mip 低通更稳 |
| `with_ut/with_eval3d` | 卷帘快门或大畸变时开（仅 MCMC 可用） | 3DGUT：`rolling_shutter` + `viewmats_rs` |
| 天空 | `sky_masks` 或 `random_bkgd=True` | 避免天空漂浮物 |
| `max_steps` | 30k（先跑 7k 做 QA） | 默认值 |

### 3.10 分块训练、裁剪、LoD 生成与精度约束

- **瓦片划分**：按与点云瓦片相同的四叉树或八叉树 key 划分 256–512 m 的块。训练时相机集合外扩 15–20%（VastGaussian/CityGS 做法）。导出时**按高斯中心裁回瓦片 AABB**（相当于 nerfstudio OBB 裁剪的轴对齐版），跨界的大高斯归中心所在块。
- **精度上限**：
  - float16 的 ULP 在 1024–2048 m 为 1 m（r01 实测 NY 1607 m → ULP 1 m）
  - SPZ 24-bit 定点，12 fractional bits → ±2048 m、0.24 mm
  - 因此瓦片局部坐标 |x| ≤ 1024 m 时，SPZ 与 float32 都安全
  - 如果要用 Spark 的 PackedSplats，再加 `pagedExtSplats/extSplats`
- **合并式 LoD**（离线，每级体素 v_L = v₀·2^L。质量对标 Spark `bhatt-lod`，这里给最简的矩匹配版本）：

```python
for voxel in group_by(floor(mu / v_L)):
    w  = o_i * prod(s_i)**(2/3)                        # 不透明度 × 投影面积近似
    mu = Σ w·μ_i / Σw
    Σ  = Σ w·(Σ_i + (μ_i-μ)(μ_i-μ)ᵀ) / Σw              # 二阶矩匹配
    R, s² = eigh(Σ);  q = rotmat_to_quat_wxyz(R)       # 保证 det(R)=+1
    o  = clamp(Σ o_i·area_i / area(s), 0, 0.99)        # area = 最大两轴乘积
    sh0 = Σ w·sh0_i / Σw;  shN = 0                      # 粗级只保留 SH0
    保留 LoD 级别：L0（原始）、L1、L2 …，直到 ≤ 64k 高斯/瓦片
```

Web 端的瓦片选择与点云共用 SSE 公式：`px = node_extent·K / distance`，px > τ（例如 64 px）时细化；预算用 splat 数（桌面 1–2.5M，软件 GL ≤ 200k，估算）。

### 3.11 Web 格式选择矩阵

| 格式 | B/splat（SH0 / SH3） | 位置精度 | Web 加载器 | LoD/流式 | 本项目用途 |
|---|---|---|---|---|---|
| 3DGS PLY（Inria 字段） | 56 / 236（无法线） | f32 | three.js、Spark、所有工具 | 无 | **训练母版、归档**（`master/`） |
| compressed PLY（PlayCanvas/gsplat） | 约 16.3 / 约 61 | 块内 11/10/11 | Spark、SuperSplat | 无 | 兼容导出 |
| `.splat` | 32 / 不支持 | f32 | three.js、Spark | 无（可按前缀渐进） | **缩略预览**（≤300k） |
| `.ksplat` | 分级压缩 | f32/f16 | three.js、Spark | 无 | 不用 |
| **SPZ v3（gzip）/ v4（zstd）** | 约为 PLY 的 1/10（Niantic 数据） | 24-bit 定点 | **three.js `SPZLoader`**、Spark、Cesium | 无（按瓦片、按级拆文件） | **Web 主分发格式**（`web/spz/{lod}/{key}.spz`） |
| SOG/SOGS（WebP/PNG 纹理） | 1M → 16.5 MB（gsplat PNG 基准） | 16-bit log | Spark（pcsogs）、PlayCanvas | 无 | 可选归档（gsplat PngCompression） |
| Spark RAD（chunked） | 可配置 | 可配置 | 仅 Spark（WebGLRenderer） | **有**（分页 LoD） | 备选高保真演示 |
| **glTF `KHR_gaussian_splatting` (+ `_compression_spz_2`) + 3D Tiles** | 同 SPZ | 同 SPZ + tile transform | three.js GLTF 插件、Cesium | **有**（3D Tiles 层级） | **V1.0 标准化出口**（接 Cesium/GIS） |

### 3.12 Web 端 3DGS 渲染要点（V0.8，three.js `GaussianSplat`）

- 每个瓦片、每个 LoD 级别对应一个 `GaussianSplat` 对象。瓦片切换时用 transitions.dev 的交叉淡入（在 opacity uniform 上做 200–300 ms 插值）。
- 3DGS 在不透明层（点云、无人机、Mesh）之后绘制：开深度测试、关深度写入。点云与 3DGS 同时开启时，默认点云只画几何调试色，或者直接隐藏。
- 雾和雨的视觉：GaussianSplat 自建 NodeMaterial，`scene.fog` 不一定生效（未验证）。需要 fork 材质，在 fragmentNode 里按 `exp(−ρ·dist)` 混合雾色，或者后处理做深度雾（3DGS 不写深度，需要单独的期望深度 pass，成本高）。V0.8 先用后处理屏幕空间雾，统一作用于所有图层。
- `autoSort=true`，排序阈值 0.9995；多瓦片时每个对象独立排序，瓦片之间按中心深度做对象级排序（three.js 透明对象默认行为）。瓦片交界处可能出现排序瑕疵，瓦片够大时可以接受。
- 拾取：`GaussianSplat.raycast` 的不透明度阈值是 0.2。设置航点时优先对 Geometry World（点云/Mesh）拾取。

---

## 4. 在本项目中的落点与复用方式

| # | 算法/模块 | 来源 | 落点模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|---|
| 1 | 自适应密度控制器（state machine + 吞吐预算） | nerfstudio `viewer/render_state_machine.py` | `apps/web/src/world/pointcloud/AdaptiveDensityController.ts` | V0.1 | port | 点云疏密自动调节的核心逻辑，直接对应"保证流畅"的需求 |
| 2 | 节点内渐进排序（Morton + 块级打乱 / 重要性） | gsplat `exporter.sort_centers`；splat `main.js` | `world/pipeline/tiler`（Python）+ Web 解码 | V0.1 | port | 单请求流式、前缀可渲染 |
| 3 | 分块量化编码（uint16 默认，11/10/11 可选） | gsplat `splat2ply_bytes_compressed` | `world/pipeline/tiler` + `pointcloud/decoder.worker.ts` | V0.1 / V0.2 | port | 体积降到约 1/4，解码简单 |
| 4 | 法线定向 surfel 点精灵 | gsplat EWA 投影 + 扁平高斯思想 | `pointcloud/materials/SurfelMaterial`（TSL） | V0.2–V0.3 | port | UrbanScene3D 有法线没颜色，surfel 明显减少孔洞 |
| 5 | smallest-three 四元数打包 | gsplat `pack_rotation` | `apps/api/ws/codec` + Web `telemetryDecoder` | V0.2 | port | 遥测带宽 |
| 6 | 3DGS 字段契约与 PLY 读写 | Inria `save_ply`；gsplat `export_splats`、`load_ply_to_splats` | `world/visual/gaussian/io.py` | V0.3 | adopt | 事实标准 |
| 7 | PointSplat 伪高斯转换 | gsplat `create_splats_with_optimizers` 初始化语义 | `world/pipeline/pointsplat.py` | V0.3 | port | 不需要 GPU 也能跑通 Gaussian 管线 |
| 8 | 3DGS 训练 | gsplat `simple_trainer` / `av_trainer`、`MCMCStrategy` | `reconstruction/gaussian/train.py`（GPU Worker Job） | V0.6–V0.8 | adopt | 2026 最活跃、最省显存 |
| 9 | 深度初始化与 kNN scale | gsplat `init_utils` | `reconstruction/gaussian/init.py` | V0.6 | adopt | 直接吃 LingBot 的 depth/pose/K |
| 10 | 单目深度 scale/offset 对齐 | Inria `utils/make_depth_scale.py` | `reconstruction/fusion/depth_align.py` | V0.5 | port | LingBot 相对尺度 → 米制 |
| 11 | 训练数据契约（NPZ / transforms.json） | gsplat `av_trainer`；nerfstudio dataparser | `worlds/<id>/reconstruction/` | V0.5–V0.6 | adopt（格式） | 与第三方训练器兼容 |
| 12 | 导出、裁剪、LoD 合并、SPZ 转换 | gsplat exporter；nerfstudio OBB 裁剪；Spark `build-lod`（参考） | `world/pipeline/gaussian_tiles.py` | V0.8 | port | 瓦片局部 ENU 加分级 |
| 13 | PNG/SOGS 压缩 | gsplat `PngCompression` | 归档 `visual/gaussian/archive/` | V0.8 | adopt | 约 14 倍压缩 |
| 14 | Web 3DGS 渲染 | three.js r186 `GaussianSplat`、`SPZLoader` | `apps/web/src/world/visual/GaussianLayer.ts` | V0.8 | adopt | 与 WebGPURenderer 一致 |
| 15 | 服务端 RGB/深度/LiDAR 仿真 | gsplat `rasterization(render_mode="RGB+ED")`、LiDAR 渲染、HiGS 推理 | `simulation/sensors/neural/` | V1.0 | adopt | 真实外观的相机与 LiDAR 仿真，比 Isaac 轻 |
| 16 | Gaussian ID 拾取与语义 | gsplat `rasterize_contributing_gaussian_ids` | `world/semantic/` | V1.0+ | reference | 语义标注回传 |
| 17 | nerfstudio 训练框架 / Inria SIBR | — | — | — | skip | 依赖旧、与主栈重复 |
| 18 | 4DGS | 见 dynamic4d 单元 | — | V1.0 之后 | skip（MVP） | 与设计文档 §43 一致 |

### 4.1 World Package：`visual/gaussian/` 目录设计

```text
worlds/<world-id>/visual/gaussian/
├── manifest.json                  # 见下
├── alignment.json                 # T_enu_from_train、与 Geometry 点云的一致性残差（median/p95）
├── source/
│   ├── scene.npz → ../../reconstruction/…   # 训练数据契约（软链或引用）
│   ├── train_config.yaml          # gsplat Config 全量 dump（tyro）
│   └── metrics.json               # PSNR/SSIM/LPIPS（含 cc_* 色彩校正指标）、#GS、耗时、GPU
├── master/                        # float32 Inria PLY，SH3，瓦片局部 ENU（只做归档与再训练）
│   └── {tileKey}.ply
├── web/
│   ├── spz/L0/{tileKey}.spz       # 原始级（SH1），v3 gzip（three.js/Spark/Cesium 都能读）
│   ├── spz/L1/{tileKey}.spz       # 合并级（SH0）
│   ├── spz/L2/{tileKey}.spz
│   └── preview.splat              # 全场景 ≤300k 的缩略预览（importance 前缀）
├── tiles3d/                       # V1.0：tileset.json + *.glb（KHR_gaussian_splatting + _compression_spz_2）
└── archive/                       # 可选：gsplat PngCompression 输出（meta.json + *.png + shN.npz）
```

`manifest.json`（前端 `GaussianLayer` 唯一入口）：

```json
{
  "schema": "anet.visual.gaussian/1.0",
  "worldId": "hefei-campus",
  "crs": { "frame": "ENU", "originWgs84": [117.2, 31.8, 30.0], "upAxis": "z", "unit": "m" },
  "shDegreeMaster": 3,
  "encoding": { "web": "spz-v3", "positionsLocal": true, "maxLocalExtentM": 1024 },
  "lod": { "levels": 3, "voxelBaseM": 0.5, "sseThresholdPx": 64 },
  "budget": { "desktop": 2000000, "integrated": 800000, "software": 150000 },
  "tiles": [{
    "key": "q/2/1/3", "originEnu": [512.0, 768.0, 0.0],
    "aabbLocal": [[-256,-256,-20],[256,256,180]],
    "counts": [1800000, 420000, 64000],
    "files": { "L0": "web/spz/L0/q_2_1_3.spz", "L1": "web/spz/L1/q_2_1_3.spz", "L2": "web/spz/L2/q_2_1_3.spz",
               "master": "master/q_2_1_3.ply" },
    "sha256": { "L0": "…" }
  }],
  "quality": { "psnr": 25.1, "ssim": 0.78, "lpips": 0.29, "geomResidualP95M": 0.35 },
  "provenance": { "trainer": "gsplat@1.6.0", "strategy": "mcmc", "capMax": 2000000, "createdAt": "2026-…" }
}
```

规则：
- `tileKey` 与点云瓦片 key 同构，这样 LOD 选择器可以复用。
- 瓦片局部坐标加 `originEnu`，前端用 `object.position` 平移（相机相对渲染，RTC）。
- **质量闸门**：`geomResidualP95M` = 高斯中心（o > 0.5）到 Geometry 点云最近点距离的 p95，超过阈值（例如 0.5 m）不允许发布。

### 4.2 Visual World 分阶段升级路径（修订版）

```text
V0.1  PointCloud（八叉树 + 自适应预算 + EDL）           ← 本单元：控制器、编码、渐进排序
V0.2  + Surfel 模式（法线定向盘，不透明、免排序）         ← 本单元：SurfelMaterial
V0.3  + PointSplat 伪高斯（无训练）+ GaussianLayer 骨架   ← 端到端跑通 manifest/SPZ/three.js GaussianSplat
V0.5  + 深度对齐 / 训练数据契约随融合一起落盘             ← reconstruction/ 保存 NPZ + transforms.json
V0.6  + GPU Worker：gsplat 训练单瓦片（MCMC + LiDAR + PPISP）
V0.8  + 多瓦片、LoD 合并、SPZ 分发、质量闸门、Web 3DGS 图层正式开放
V1.0  + 3D Tiles/glTF KHR_gaussian_splatting 出口；gsplat 服务端神经相机/LiDAR 仿真
>1.0  4DGS / 动态外观（dynamic4d 单元）
```

---

## 5. 对比与推荐

| 维度 | gsplat | nerfstudio | Inria 3DGS |
|---|---|---|---|
| 2026 活跃度 | 高（v1.6.0 main，2026-09） | 低（2025-07 起停更，pin gsplat 1.4.0） | 冻结（2024-10） |
| 训练效率 | 显存约为 Inria 的 1/4，时间少约 15% | 用的是 gsplat 1.4 | 基准 |
| 与本项目契合 | LiDAR 监督、3DGUT 卷帘快门、MCMC 数量上限、导出、压缩、服务端传感器仿真 | 数据契约、viewer 交互范式 | 字段标准、深度对齐 |
| 部署难度 | 中（CUDA JIT；torch ≥ 2.7） | 中高（依赖多，版本锁旧） | 高（gitlab 子模块，SIBR C++） |
| 可移植到 MVP 的算法 | 量化编码、Morton、足迹公式、四元数打包 | **自适应 state machine** | SH 常数、深度对齐 |
| 结论 | **adopt**（训练/导出/仿真引擎） | **reference + port** | **reference** |

**推荐排序**：
1. **gsplat**（★5.7k，2026 最活跃，功能最全，是唯一需要作为依赖引入的）
2. **nerfstudio**（★12k，只移植 state machine，并参考 transforms.json 契约）
3. **gaussian-splatting**（★24k，只当规范来源）

Web 端的配套选择是 **three.js r186 `GaussianSplat` + SPZ**（adopt），Spark RAD 作备选，3D Tiles `KHR_gaussian_splatting` 作 V1.0 标准出口。

---

## 6. 风险与注意事项

1. **GPU**：gsplat/nerfstudio/Inria 训练与渲染都**必须有 CUDA**。gsplat 的 PyTorch 参考实现 `_rasterize_to_pixels` 也依赖 `nerfacc`（CUDA），CPU 上没有实际可用路径。本机无 GPU，V0.6 前必须规划 GPU Worker（≥24 GB 显存，估算：城市瓦片 cap 2–3M、SH3、1080p）。MVP 阶段完全不依赖它们。
2. **数据前提**：3DGS 需要带位姿的图像。下载的 UrbanScene3D 只有无色点云，无法训练（完整数据集的图像部分需要另外下载，体量很大）。PointSplat 只是视觉近似，不是真 3DGS，不能宣传为重建结果。
3. **坐标系与归一化**：gsplat `normalize_world_space` 默认开且带旋转；nerfstudio 默认 `auto_scale_poses`、`orientation="up"`、`applied_transform`（交换 y/z），导出不做反变换；LingBot `extrinsic` 实为 W2C（r01）；四元数有 wxyz/xyzw 两种顺序；PLY 的 SH 是 channel-major。任何一处弄错都会导致"整体偏转或镜像"。**必须写单元测试**：用已知高斯往返导出与导入，再与点云做重投影一致性检查。
4. **精度**：float16（Spark PackedSplats、viser 默认）与 SPZ 24-bit 定点在城市尺度会溢出或出现条带。强制瓦片局部坐标，|x| ≤ 1024 m。
5. **渲染器兼容**：Spark 只支持 WebGLRenderer；three.js `GaussianSplat` 只支持 WebGPURenderer（含 forceWebGL 回退），不支持 WebGLRenderer。**两者不能共用一个渲染上下文**。主栈既然定为 WebGPURenderer，就用 three.js 自带实现，Spark 只能放在独立页面。
6. **性能**：3DGS 需要逐帧排序和 α 混合，在无头 Chromium 的软件 WebGL2（SwiftShader）下，100k 以上 splat 预计只有个位数 FPS（估算，未实测）。3DGS 图层必须**默认关闭**，受自适应控制器约束（软件 GL 上限约 150k），CI 流畅性测试只测点云与 surfel。
7. **航拍特有问题**：天空与远景会产生漂浮物（用 sky mask、`random_bkgd`）；动态物体会产生拖影（用 mask）；自动曝光（用 PPISP/bilateral grid）；卷帘快门（用 3DGUT）；仿真机位偏离采集轨迹时画质下降，特别是低空 FPV 视角（用 `antialiased`，多高度、多角度采集）。
8. **Livox MID-360 是非重复扫描**，不是 gsplat LiDAR 模型支持的"旋转式结构化 LiDAR"。LiDAR 渲染监督要么退化为"投影稀疏深度"（`depth_loss`），要么用 3DGUT 的 per-ray `rays` 输入自定义射线（V1.0 研究项）。
9. **依赖冲突**：nerfstudio pin `gsplat==1.4.0` 和 `torch==2.7.1`，gsplat main 要求 torch ≥ 2.7，examples 用 2.9.1。**不要把 nerfstudio 和 gsplat main 装进同一个环境。**
10. **本机工具链**：项目 venv 里的 open3d 导入失败（缺 `libEGL.so.1`），也没有 scipy/torch。kNN 工具优先用 scipy cKDTree（装进 venv），或者安装系统库 `libegl1`。
11. **Inria 仓库**：子模块在 gitlab.inria.fr，没有被 clone，编译需要匹配的 CUDA toolkit。只读字段定义，不要尝试构建。
12. **规模**：城市级场景高斯数可达数千万。单瓦片必须用 MCMC `cap_max` 控制数量，否则 Web 预算与训练显存都会失控。

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§8 Visual World**：补一条硬规则："Visual World 不是物理权威"，碰撞、规划、传感器几何只读 Geometry World。再加"Visual ↔ Geometry 一致性闸门"（高斯中心到几何点云残差 p95），作为发布条件。
2. **§8 升级路径过于跳跃**（PointCloud → Mesh → 3DGS）。建议改成 §4.2 的五级路径：PointCloud → Surfel → PointSplat（无训练）→ 训练 3DGS（单瓦片）→ 多瓦片 LoD + 3D Tiles。每一步都能独立交付。
3. **§14–15 点云格式**：补充**节点局部量化**（uint16 或 11/10/11 分块），明确"禁止 float16 世界坐标"。补充**节点内渐进排序**，以及**自适应密度控制器**（运动降级、静止补全、吞吐反推预算、可中断），并把控制器参数（目标 FPS、预算上下限）写进性能 SLO。
4. **§15/§41 格式**：Visual World 的 3DGS 格式要写明分层：`master` 用 PLY，`web` 用 SPZ，`tiles3d` 用 glTF `KHR_gaussian_splatting` + `_compression_spz_2`。原文"后期兼容 3D Tiles"可以落实为"3DGS 与点云都走 3D Tiles 出口，与 Cesium 路线对齐"（Cesium 源码已支持 Gaussian 3D Tiles 内容）。
5. **§41 World Package**：
   - 目前 `geometry/pointcloud/` 同时承担"物理权威"和"Web 渲染瓦片"两个角色。建议拆开：`geometry/pointcloud/` 存全精度 LAS/LAZ/PLY，`visual/pointcloud-tiles/` 存 Web 八叉树
   - `visual/gaussian/` 采用 §4.1 的结构与 manifest
   - `coordinate.json` 必须包含 `upAxis`、`unitScale`（UrbanScene3D 实测有 Y-up 与 km 的情况）以及每个瓦片的 `originEnu`
6. **§5 LingBot-Map / §41 reconstruction/**：把 LingBot 的 depth、conf、pose、K 以及抽帧原图作为**持久化资产**（NPZ + transforms.json）保存下来。它们是 3DGS 训练与重训的输入，不应只当中间产物丢弃。
7. **§9/§34 渲染器**：明确"WebGPURenderer 主栈，`forceWebGL` 回退"，**不要**混用 WebGLRenderer 生态（Spark、部分 Potree 衍生库）。这决定了 3DGS 选 three.js 内置 `GaussianSplat`。另外注明 WebGPU 点只有 1 px，大点、surfel、splat 都走 instanced quad。
8. **§35 Simulation Backend**：在 Gazebo 和 Isaac Sim 之外，增加 **"Neural Sensor Backend（gsplat）"**。它在真实外观的世界中渲染 RGB/深度/LiDAR（`render_mode="RGB+ED"`、LiDAR 光栅化、HiGS 推理打包），这是"Real-World Grounded"定位最有说服力的一环（V1.0）。
9. **§43 MVP 范围**：同意 MVP 不做 3DGS，但建议在 V0.3 交付 `GaussianLayer` 骨架（manifest、SPZ 加载、默认关闭），以及 PointSplat 转换器。这样 V0.8 引入真 3DGS 时不需要改架构。
10. **§37 更新频率/性能**：补充渲染侧 SLO，例如：桌面 GPU 60 FPS，点预算 ≥ 2M；软件 GL 30 FPS，≥ 300k；3DGS 默认关闭。补充降级链（DPR → 预算 → EDL → 粒子 → surfel → 3DGS）。
11. **§33 技术栈表**：Reconstruction 行加 "gsplat（3DGS 训练/导出，GPU Worker）"；Point Processing 行注明 "Open3D 在无头服务器需要 libEGL，或用 scipy/PDAL 替代部分功能"。
12. **§28 DroneState / §36 WebSocket**：姿态建议用 smallest-three 32-bit 四元数加二进制帧，单机单帧可以节省 12 B，多机 50 Hz 时收益明显。
