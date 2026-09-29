# R02 研究笔记：VGGT 前馈多视几何 + COLMAP/GLOMAP SfM 基准，以及 Reconstruction 统一中间表示（Recon IR）

> 研究单元：r02 ｜ 日期：2026-09-28 ｜ 关联笔记：[r01 LingBot-Map + viser](./r01-lingbot-map-viser.md)（本文与 r01 的坐标约定、产物目录保持一致，只补充 r01 未覆盖的部分）
>
> 本地仓库：`refs/recon/vggt`（★14435，最后提交 2026-05-18）、`refs/recon/colmap`（★12828，最后提交 2026-09-27，main = 4.3.0.dev0，最新发布 4.2.0 / 2026-08-31）。`cvg/glomap` 已并入 COLMAP，全局 SfM 代码在 `src/colmap/sfm/global_mapper.*`、`src/colmap/estimators/{rotation_averaging,global_positioning}.*`。
>
> 本机验证：在隔离 venv（`.cache/research/r02-venv`，pycolmap 4.2.0 CPU wheel，8 核、无 GPU）跑了 4 个脚本：`r02_validate.py`、`r02_glomap_cpu.py`、`r02_prior.py`、`r02_collinear.py`。结果见各节和附录。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **colmap/colmap** 4.2.0（GLOMAP 已内置） | SfM/MVS 的工业基准。本项目用它的 Rig/Frame 多传感器数据模型、全局 SfM（`global_mapper`）、带位置先验的增量 SfM（`pose_prior_mapper`）、Sim3/GPS 地理配准和模型比对 | **adopt**：pycolmap 4.2.0 CPU wheel 作为离线依赖，`pip` 即可装。**port**：GPS/ENU 公式、Umeyama、官方 TS 二进制解析器（`doc/viewer_src`） | V0.1：数据规范、COLMAP 模型导入。V0.5：度量基准、地理配准、重建 QA | ★★★★★ |
| **facebookresearch/vggt** | 前馈多视几何。一次前向同时输出相机、深度、点图和轨迹。LingBot-Map 的 heads 和 `pose_enc` 都从它继承 | **port**：`pose_enc`/几何工具、518 预处理坐标映射、COLMAP 导出、sky mask。**模型本身**在 V0.5 作为第二引擎可选 adopt（关键帧批量重建、交叉校验、给 3DGS 做初始化） | V0.1：接口规范。V0.5：批量引擎与 QA | ★★★★ |

**关键结论**

1. **VGGT 与 LingBot-Map 的位姿方向相反，这是最大的静默 bug 源。** 两者 `pose_enc` 都是 9 维 `absT_quaR_FoV`，函数名和 docstring 也一样。区别在于：VGGT 的 `pose_encoding_to_extri_intri` 解码得到 **W2C**（`cam_from_world`，见 `vggt/utils/pose_enc.py` 和 `demo_colmap.py` 的直接使用）；LingBot-Map 解码得到 **C2W**（README 写明 "(c2w)"，`benchmark/methods/lingbot_map.py` 注释为 "output is C2W directly"，`gct_stream_window_v2._pairwise_alignment` 按 C2W 做拼接）。统一 IR 只存 `T_world_cam`（C2W）加 `q_xyzw`，与 r01 一致。每个引擎适配器都必须带方向自检（§3.3）。
2. **前馈模型输出的是"引擎规范系"（engine gauge），本身不带度量尺度。** VGGT 的世界系就是第 0 帧相机系（`Aggregator` 给首帧单独一组 camera/register token），训练时把尺度归一化为"有效点到首相机的平均距离 = 1"（`training/train_utils/normalization.py`），也不与重力对齐。01-design §6 说"尺度可能存在误差"不准确，应改为"**尺度本来就不存在**"，任何 World 都必须先经过 Sim3 锚定。
3. **COLMAP 4.x 的 `Rig / Camera / Frame / Image / Point3D / Track / PosePrior` 是目前最成熟的多传感器重建数据模型。** 建议 Recon IR 以它为骨架，再补上时间戳、深度、置信度和地理元数据（§3.2）。交换格式用 COLMAP 4 sparse 二进制（`rigs/cameras/frames/images/points3D.bin`），因为 BA、gsplat 3DGS 训练和 QA 都直接读它。
4. **GLOMAP 已并入 COLMAP：** CLI 是 `colmap global_mapper`，Python 是 `pycolmap.global_mapping`（docstring 写明 "(GLOMAP)"）。流程是旋转平均 → 轨迹建立 → BATA 全局定位 → 迭代 BA → 重三角化。4.2 起默认按连通分量返回**多个模型**。global mapper 只把 PosePrior 当作重力方向使用，最后还会调用 `Reconstruction::Normalize()`，所以输出**没有尺度**，必须再跑 model_aligner。`pose_prior_mapper`（增量 SfM 加位置先验 BA）则直接输出度量尺度。本机实测（40 帧合成数据）：pose-prior 用时 12.6 s，对 GT 的 Sim3 scale = **1.000000**；global 用时 17.2 s，scale = 1.368。
5. **地理配准的核心算子是 Umeyama Sim3 加 LO-RANSAC**（COLMAP `EstimateSim3dRobust`）。本机实测：60 帧、240×160 m 的环绕航线，RTK（σ=2 cm）下相机中心 RMSE **0.011 m**，尺度误差 0.002%；普通 GPS（σ=2 m/3 m）下 RMSE **0.56 m**，尺度误差 0.23%。numpy 实现与 pycolmap 的差值 |Δs| = 1e-10。
6. **直线航带会导致退化（01-design 没有提到，航测必然遇到）。** 相机中心共线时，绕航线轴的旋转不可观测。实测纯 Umeyama 的旋转误差达 **60°–117°**；加入 IMU 重力"虚拟点"的两遍法后，误差降到 **0.00°/0.19°**（§3.5）。
7. **model_aligner 有三个坑：**
   - `ref_is_gps=1`（默认值）会把模型变换到 **ECEF**，坐标量级约 6e6 m，float32 精度只有约 0.5 m。
   - `enu` 模式以**第一条 GPS 记录**为原点，同一 World 多次采集会得到不同原点。
   - `enu-plane` 模式下 `--transform_path` 写出的只是第二步变换（`AlignToENUPlane` 覆盖了 `tform`，没有做复合）。
   
   对策：自己用固定的 World 原点把 WGS84 转成 ENU，再以 Cartesian 方式（`ref_is_gps=0`）对齐。
8. **VGGT 的 518 预处理是非等比缩放。** 例如 1920×1080 会变成 518×294（高度按 14 的倍数取整），x/y 两个方向的缩放差 0.9%。还原到原图内参时必须分轴处理，还要统一像素中心约定（COLMAP 取 +0.5）。
9. **VGGT 的 conf 激活是 `1+exp(x)`，所以 `1 − 1/conf = sigmoid(x)` 严格成立。** 置信度因此可以无损映射到 [0,1] 并量化为 u8，VGGT 和 LingBot 的阈值语义也能统一：`conf≥5` ⇔ 0.8，`conf≥1.5` ⇔ 0.333。
10. **对 MVP 直接有用的部分：**
    - ① 坐标与投影约定：K → three.js 投影矩阵、OpenCV → three 相机、ENU → Y-up。
    - ② COLMAP 官方 TS 查看器（three 0.185）：Worker 加 Transferable 的二进制解析、1%–99% 鲁棒包围盒、视锥几何。
    - ③ WGS84 ↔ ENU 精确公式，用于遥测经纬度显示和 Mock 地理原点。
    - ④ 置信度 u8、直方图百分位阈值，以及节点内预洗牌的前缀子采样（调疏密时不用重传数据）。

---

## 1. 仓库概览

| 项 | VGGT | COLMAP |
|---|---|---|
| 版本 / 活跃度 | main（2026-05-18 最后提交）。2026-05-15 修复了中间张量冗余，同显存下可跑 2–3 倍帧数。2026-05-18 发布后继者 **VGGT-Omega**（独立项目，未 clone） | 4.1.0（06-26）：Caspar GPU BA、球面相机、EUCM、EXIF 重力先验。4.2.0（08-31）：global mapper 多连通分量、LoMa ONNX 特征、ROCm PatchMatch、**浏览器查看器**、6 点共享焦距求解器、DEGENSAC、纳秒时间戳工具、优雅中断。main 已是 4.3.0.dev0 |
| 语言 / 规模 | Python/PyTorch，约 1.5 万行（含 training） | C++17 + pybind11，外加 TS 查看器（`doc/viewer_src`，约 2.5k 行） |
| 许可 | 代码 VGGT License（2025-07 起允许商用，军事用途除外）。原始 ckpt 仅限非商用，`VGGT-1B-Commercial` 需申请。本项目为科研用途，可忽略 | BSD-3 |
| 依赖 | torch 2.3.1、torchvision 0.18.1、numpy<2、huggingface_hub、einops、safetensors。demo 另需 gradio 5.17.1、viser 0.2.23、onnxruntime、trimesh、pycolmap | Eigen、Ceres、Boost、SQLite、glog。可选：CUDA/HIP、Qt、faiss、ONNX Runtime、PoseLib（FetchContent） |
| 模型 / 算力 | VGGT-1B（约 1.2B 参数，fp32 权重约 5 GB），bf16 需要 Ampere 及以上。旧版 README 数据（H100）：100 帧约 3 s / 21 GB，200 帧约 9 s / 41 GB（修复后同显存可跑 2–3 倍帧数） | 纯 CPU 可用。Ceres CUDA BA 和 Caspar（CUDA ≥7.0）可选；4.2 在无 GPU 时自动回退到 CPU Ceres |
| 本机可运行性 | 代码可以 import，但推理在 CPU 上不实用。`demo_*.py` 都会调用 `torch.cuda.get_device_capability()`，无 GPU 时直接抛异常 | **pycolmap 4.2.0 CPU wheel 已实测可用**（`has_cuda=False`）。源码构建较重，不推荐 |

---

## 2. 源码结构与关键模块

### 2.1 VGGT 目录与数据流

| 路径 | 职责 | 关键符号 / 参数 |
|---|---|---|
| `vggt/models/vggt.py` | 顶层模型 | `VGGT(img_size=518, patch_size=14, embed_dim=1024, enable_*)`；`forward(images[S,3,H,W] 或 [B,S,3,H,W], query_points=None) → dict` |
| `vggt/models/aggregator.py` | 交替注意力主干 | `Aggregator`：patch embed 用 DINOv2 ViT-L/14-reg；24 层 frame block + 24 层 global block 交替（`aa_order=["frame","global"]`）；每帧 1 个 camera token 加 4 个 register token。**首帧与其余帧用不同 token**（`slice_expand_and_flatten`），这就是"首帧即世界系"的来源。缓存第 4、11、17、23 层，输出拼接为 2C = 2048 维；RoPE 频率 100 |
| `vggt/heads/camera_head.py` | 相机头 | `CameraHead`：4 层 trunk，AdaLN 调制（shift/scale/gate），迭代精化 4 次，逐次累加 Δpose |
| `vggt/heads/dpt_head.py` | 稠密头 | `DPTHead`：DPT 多尺度融合，`frames_chunk_size=8` 分块推理以省显存。深度：`output_dim=2, activation="exp"`；点图：`output_dim=4, activation="inv_log"`；conf 统一用 `expp1` |
| `vggt/heads/head_act.py` | 激活 | `activate_head`：`expp1` → `conf = 1 + exp(x)`；`inv_log` → `sign(y)·(exp|y|−1)` |
| `vggt/heads/track_head.py` | 跟踪头 | `TrackHead`：query 点位于第 0 帧（要查询其他帧就得重跑主干，作者在 `demo_colmap.py` 注释中说明，所以 BA 改用 VGGSfM tracker） |
| `vggt/utils/pose_enc.py` | 位姿编解码 | `extri_intri_to_pose_encoding` / `pose_encoding_to_extri_intri`：`[T(3), quat_xyzw(4), fov_h, fov_w]` |
| `vggt/utils/rotation.py` | 四元数 | `quat_to_mat` / `mat_to_quat`：**XYZW 标量在后**，`standardize_quaternion` 保证 w≥0 |
| `vggt/utils/geometry.py` | 几何 | `unproject_depth_map_to_point_map`、`depth_to_world_coords_points`、`closed_form_inverse_se3`、`project_world_points_to_cam`、`img_from_cam`、`cam_from_img`（含迭代去畸变） |
| `vggt/utils/load_fn.py` | 预处理 | `load_and_preprocess_images(mode="crop"/"pad")`；`load_and_preprocess_images_square(target=1024)` 返回 `original_coords=[x1,y1,x2,y2,W,H]` |
| `vggt/dependency/np_to_pycolmap.py` | 导出 | `batch_np_matrix_to_pycolmap`（带 tracks，可做 BA）；`batch_np_matrix_to_pycolmap_wo_track`（**只能用作 3DGS 初始化，不能 BA**）；`pycolmap_to_batch_np_matrix` |
| `vggt/dependency/track_predict.py` | BA 用的跟踪 | `predict_tracks`：VGGSfM tracker，ALIKED+SuperPoint 关键点，用 DINO 排序选 query 帧（`generate_rank_by_dino`） |
| `demo_colmap.py` | VGGT → COLMAP（可选 BA） | `run_VGGT`、`rename_colmap_recons_and_rescale_camera` |
| `demo_viser.py` / `visual_util.py` | 可视化 | `viser_wrapper`（conf 百分位滑条，默认过滤 25%）；`segment_sky`（`skyseg.onnx`，320×320，阈值 32）；`get_opengl_conversion_matrix`（diag(1,−1,−1)） |
| `training/train_utils/normalization.py` | 训练归一化 | `normalize_camera_extrinsics_and_points_batch`：先把坐标变到首相机系，再除以有效点平均距离 |

```text
images[S,3,H,W]∈[0,1] ─(ResNet mean/std)→ DINOv2 patch tokens (S×1369, 518×518 时)
   + [camera tok, 4 register tok]（首帧用第 0 组，其余帧用第 1 组）
   └→ 24×(frame-attn(每帧内) → global-attn(跨全部 S·P token))
        ├→ CameraHead(最后一层 camera token, 4 次迭代) → pose_enc[B,S,9]
        ├→ DPTHead(depth)  → depth[B,S,H,W,1] (exp),     depth_conf (1+exp)
        ├→ DPTHead(point)  → world_points[B,S,H,W,3] (inv_log), world_points_conf
        └→ TrackHead(query@frame0) → track[B,S,N,2], vis, conf
```

### 2.2 VGGT 输出数据契约

| key | 形状 | 语义 |
|---|---|---|
| `pose_enc` | [B,S,9] | `[tx,ty,tz, qx,qy,qz,qw, fov_h, fov_w]`。解码后是 **W2C**（OpenCV 相机系），主点固定为 (W/2, H/2)，内参按帧给出 |
| `pose_enc_list` | 4×[B,S,9] | 4 次迭代的中间结果（可用于收敛诊断） |
| `depth` | [B,S,H,W,1] | **z-depth**（沿相机 z 轴，不是射线长度），单位为引擎尺度 |
| `depth_conf` | [B,S,H,W] | `1+exp(x)`，取值 (1, ∞) |
| `world_points` | [B,S,H,W,3] | 点图分支，位于首帧相机系。README 建议优先用"深度 + 相机反投影"，精度更高 |
| `world_points_conf` | [B,S,H,W] | 同上 |
| `track/vis/conf` | [B,S,N,2] / [B,S,N] | 仅在传入 `query_points` 时输出 |
| `images` | [B,S,3,H,W] | eval 模式下回传输入图像 |

### 2.3 VGGT 的隐含约定

- **世界系**是第 0 帧相机系（OpenCV：x 右、y 下、z 前），`E_0 ≈ [I|0]`。
- **尺度**是训练时归一化的：有效点到首相机的平均距离为 1，推理输出只能确定到相似变换。第 0 帧的选择会影响尺度和坐标系，建议选一个视野覆盖大、清晰的关键帧放在首位。
- **内参**由 FoV 反推：`fy=(H/2)/tan(fov_h/2)`，`fx=(W/2)/tan(fov_w/2)`，主点固定在中心，**不建模畸变**。无人机吊舱画面必须先去畸变。
- **像素坐标**：`depth_to_cam_coords_points` 用 `np.meshgrid(np.arange(W))`，也就是以整数索引作为像素中心；COLMAP 的约定是左上角像素中心为 (0.5, 0.5)（`sensor/models.h`）。两者相差半个像素，在 1080p 下影响不到 2 px，但全链路必须统一。
- **预处理**（`load_and_preprocess_images`，crop 模式）：宽度缩放到 518，高度取 `round(H·518/W/14)·14`，超过 518 时居中裁剪。**这是非等比缩放**：1920×1080 → 518×294，`sx=0.26979`，`sy=0.27222`。pad 模式先把长边缩放到 518 再用**白色**补成正方形；`load_and_preprocess_images_square` 用**黑色**补成正方形后缩放到 1024。
- **置信度阈值**：`demo_colmap` 用绝对阈值 `depth_conf ≥ 5.0`；`demo_viser` 用百分位（默认过滤最低 25%），同时要求 `> 0.1`；LingBot demo 的默认值是 `conf_threshold=1.5`。

### 2.4 VGGT → COLMAP / BA 路径（`demo_colmap.py`）

```text
前馈（不做 BA）：square 1024 → 插值到 518×518 → aggregator/camera/depth 头
  → 反投影 → conf≥5.0 → randomly_limit_trues(≤100k) → PINHOLE、每帧一个相机
  → batch_np_matrix_to_pycolmap_wo_track（只用于 3DGS 初始化，不能 BA）
带 BA（--use_ba）：
  → predict_tracks(max_query_pts=4096, query_frame_num=8, "aliked+sp", fine_tracking)
  → intrinsic[:, :2] *= 1024/518
  → batch_np_matrix_to_pycolmap(max_reproj_error=8px, vis_thresh=0.2,
       min_inlier_per_frame=64, SIMPLE_PINHOLE, 可选 shared_camera)
  → pycolmap.bundle_adjustment(默认选项)
最后：rename_colmap_recons_and_rescale_camera（参数 × max(W,H)/img_size，主点 = 原图中心，
     points2D 减去 pad 偏移后再乘比例）→ sparse/{cameras,images,points3D}.bin + points.ply
  → gsplat examples/simple_trainer.py（README 推荐 gsplat==1.3.0）
```

代码中的 TODO 包括：mask、迭代 BA、径向畸变（`SIMPLE_RADIAL` 会直接 `raise NotImplementedError`）。

### 2.5 VGGT 与 LingBot-Map 接口对照（统一 IR 的依据）

| 项 | VGGT | LingBot-Map（详见 r01） | 统一 IR |
|---|---|---|---|
| 调用方式 | 批量：`VGGT.forward`，全部帧做全局注意力 | 流式：`inference_streaming`（KV cache）；窗口：`inference_windowed` | `ReconEngine.run()` 逐帧产出 `EngineFrame` |
| `pose_enc` 解码方向 | **W2C** | **C2W** | `T_world_cam`（C2W），f64 |
| 四元数顺序 | xyzw（w≥0） | xyzw | xyzw（与 three.js、scipy、Eigen coeffs 一致） |
| 世界系 | 第 0 帧相机 | 前 `num_scale_frames`（默认 8）批的相机系 | 经 Sim3 进入 ENU；之前标记 `gauge="engine"` |
| 尺度 | 平均点距归一化 | 相对尺度；windowed 模式有 `chunk_scales` | `scale_status: relative/gnss/rtk/lidar` |
| 内参 | 每帧 FoV，主点在中心 | 同左 | `Camera`：标定值优先，预测值只作 QA |
| 深度 / 置信度 | z-depth, `1+exp` | 同左 | `DepthMap` f16 + `conf_u8 = round(255·(1−1/conf))` |
| 点图 | `world_points` | 同左（可选） | `PointMap` 仅调试时保存，默认由深度和位姿推导 |
| 额外信息 | `pose_enc_list`、tracks | `frame_type`（0 = scale，1 = key，2 = non-key）、`is_keyframe`、`chunk_transforms`、`alignment_mode` | `Frame.frame_type`；`Session.chunks[]` |
| 输入分辨率 | 518 宽（crop）或 518 方形（pad） | `image_size=518` | `Camera` 的 K 一律换算到**原图像素** |

### 2.6 COLMAP 4.2 模块地图（本单元关注的部分）

| 目录 | 关键类 / 函数 | 本项目用途 |
|---|---|---|
| `scene/` | `Reconstruction`、`Rig`、`Camera`、`Frame`、`Image`、`Point3D`、`Track`、`Database`（SQLite）、`reconstruction_io_{binary,text}.cc` | IR 骨架、交换格式 |
| `geometry/` | `Rigid3d`、`Sim3d`（`x_b = s·R·x_a + t`，`ToFile`/`FromFile` 文本格式为 `s qw qx qy qz tx ty tz`）、`GPSTransform`、`PosePrior`、`TransformCameraWorld` | 地理配准、坐标变换 |
| `estimators/` | `EstimateSim3dRobust`（LO-RANSAC + Umeyama）、`AlignReconstructionToLocations`/`ToPosePriors`、`AlignToPrincipalPlane`/`AlignToENUPlane`、`RotationEstimator`、`GlobalPositioner`（BATA）、`CreatePosePriorBundleAdjuster`、Caspar | 配准、全局 SfM、先验 BA |
| `sfm/` | `GlobalMapper::Solve`、`IncrementalMapper`、`IncrementalTriangulator` | 度量基准 |
| `controllers/` | `GlobalPipeline`、`IncrementalPipeline`、`HierarchicalPipeline`、`pairing.h`（Sequential/Spatial/Transitive/VocabTree） | 配对与流水线 |
| `exe/` | `model_aligner`、`model_comparer`、`model_analyzer`、`pose_prior_mapper`、`global_mapper`、`spatial_matcher`、`sequential_matcher` 等（`colmap.cc` 共注册 50 余个子命令） | CLI 流水线 |
| `doc/viewer_src/` | `parser.ts`、`viewer.ts`、`math.ts`、`camera_models.ts`（three 0.185.1，Vite） | Web 端移植 |

### 2.7 COLMAP 数据模型与二进制布局（小端，`reconstruction_io_binary.cc`）

实体关系：`Rig`（一个参考传感器 + 若干 `sensor_from_rig`）→ `Frame`（某一时刻的 `rig_from_world` + 一组 `data_id`）→ `Image`（`camera_id`、`frame_id`、`name`、`points2D`）；`Camera` 存内参；`Point3D` 存 xyz、rgb、error 和 `Track[(image_id, point2D_idx)]`；`PosePrior` 存 `position`（WGS84 或 CARTESIAN）、`position_covariance` 和 `gravity`（传感器系下的向下方向）。ID 类型：`rig_t/camera_t/image_t/frame_t/point2D_t` 为 u32，`point3D_t` 为 u64；`SensorType` 取值为 `INVALID=-1, CAMERA=0, IMU=1`。

| 文件 | 布局 |
|---|---|
| `cameras.bin` | `u64 n`；每项：`u32 camera_id, i32 model_id, u64 width, u64 height, f64 params[k]` |
| `rigs.bin` | `u64 n`；每项：`u32 rig_id, u32 num_sensors`，参考传感器 `[i32 type, u32 id]`，其余传感器 `[i32 type, u32 id, u8 has_pose, (f64 qw qx qy qz tx ty tz)?]`（即 `sensor_from_rig`） |
| `frames.bin` | `u64 n`；每项：`u32 frame_id, u32 rig_id, f64 qw qx qy qz tx ty tz`（`rig_from_world`），`u32 num_data`，每条 `[i32 sensor_type, u32 sensor_id, u64 data_id]` |
| `images.bin` | `u64 n_reg`；每项：`u32 image_id, f64 qw qx qy qz tx ty tz`（`cam_from_world`，已由 rig 和 frame 合成），`u32 camera_id, char name[]\0, u64 n_pts`，每点 `f64 x, f64 y, u64 point3D_id`（无效值为 `0xFFFF…`） |
| `points3D.bin` | `u64 n`；每项：`u64 id, f64 x y z, u8 r g b, f64 error, u64 track_len`，每条 track `(u32 image_id, u32 point2D_idx)` |

相机模型（`camera_models.ts` 与 C++ 端一致，`id: name(参数个数)`）：`0 SIMPLE_PINHOLE(3)`、`1 PINHOLE(4)`、`2 SIMPLE_RADIAL(4)`、`3 RADIAL(5)`、`4 OPENCV(8)`、`5 OPENCV_FISHEYE(8)`、`6 FULL_OPENCV(12)`、`7 FOV(5)`、`8 SIMPLE_RADIAL_FISHEYE(4)`、`9 RADIAL_FISHEYE(5)`、`10 THIN_PRISM_FISHEYE(12)`、`11 RAD_TAN_THIN_PRISM_FISHEYE(16)`、`12 SIMPLE_DIVISION(4)`、`13 DIVISION(5)`、`14 SIMPLE_FISHEYE(3)`、`15 FISHEYE(4)`、`16 EUCM(6)`、`17 EQUIRECTANGULAR(2)`。

**注意：** 4.x 的二进制文件**没有时间戳**。`timestamp_t` 只存在于 `util/timestamp.h` 工具中，所以 IR 必须用 sidecar 文件（`frames.jsonl`）保存 `t_ns`。另外，文件中的四元数顺序是 **wxyz**，而 pycolmap 的 `Rotation3d.quat` 返回 **xyzw**（Eigen coeffs，已实测）。读旧格式模型（没有 rigs/frames）时，COLMAP 会自动"每相机一个 rig、每图像一个 frame"（`CreateOneRigPerCamera`）。

### 2.8 全局 SfM（原 GLOMAP）：`GlobalMapper::Solve`

```text
RotationAveraging（两遍：先全量求解，按 max_rotation_error_deg=10 剔除外点对，再只对已注册帧重解）
  L1 最多 5 次 + IRLS 最多 100 次，Geman-McClure 权重，σ=5°，ridge=1e-9
  use_gravity=false（打开后读取 PosePrior.gravity，分层求解：先 1-DoF 再 3-DoF）
  4.2 新增：reweighting=INLIER_MATCH_COUNT
→ EstablishTracks：每条 track 至少 3 视图、最多 100；图内一致性阈值 10 px；keep_max_num_tracks
→ GlobalPositioning（BATA：t_ij − s·(p_j − p_i)，相机-点约束，Huber 0.1）
  随机初始化；未标定焦距的观测权重 0.5；max_angular_reproj_error_deg=1
→ IterativeBundleAdjustment：3 轮，每轮先固定旋转、再联合优化；Huber；SPARSE_SCHUR；最多 200 次迭代
→ IterativeRetriangulateAndRefine：complete/merge 重投影阈值 15 px，最小三角化角 1°
→ reconstruction->Normalize()（输出为任意规范系）→ UpdatePoint3DErrors
```

外围流水线（`GlobalPipelineOptions`）：`min_num_matches=15`、`decompose_relative_pose=true`、`multiple_models=true`（**4.2 的破坏性变更**：每个连通分量各出一个模型）、`min_model_size=3`。焦距先验不足时会提示先跑 `view_graph_calibrator`（`pycolmap.calibrate_view_graph`）。

**本机 CPU 实测**（pycolmap 4.2.0，8 核，合成数据，全连通、无噪声，所以偏悲观）：

| 数据 | global（GLOMAP） | incremental | incremental + pose prior |
|---|---|---|---|
| 100 帧 × 3000 点 | 121.9 s，100/100 注册，Sim3→GT scale 1.362 | 134.8 s，scale 1.419 | — |
| 40 帧 × 1500 点 | 17.2 s，scale **1.368**（需要对齐） | — | 12.6 s，scale **1.000000**（直接得到度量尺度） |

结论：无 GPU 的机器也能跑中小规模的基准；global 的优势要在大规模、稀疏连接的航测图上才明显；无论哪个 mapper，**尺度锚定都要单独做**。

### 2.9 地理配准相关源码

- `geometry/gps.cc::GPSTransform`：支持 GRS80 和 WGS84 椭球，提供 `EllipsoidToECEF`、`ECEFToEllipsoid`（迭代最多 100 次，eps 1e-12）、`ECEFToENU`、`EllipsoidToENU`、`ENUToEllipsoid`、`EllipsoidToUTM`（4 阶级数展开，k0=0.9996）。公式见 §3.6。
- `controllers/image_reader.cc`：从 EXIF 读 lat/lon/alt，写成 `PosePrior(WGS84)`；从 EXIF orientation 推重力方向。**视频抽帧没有 EXIF**，需要用 `Database.write_pose_prior` 自行写入 RTK 数据。
- `scene/database_cache.cc::ConvertPosePriorsToENU`：**以第一条先验为 ENU 原点**，并且要求所有先验使用同一个坐标系。
- `controllers/pairing.h::SpatialPairingOptions`：`ignore_z=true`、`max_num_neighbors=50`、`max_distance=100 m`，适合航测。`SequentialPairingOptions`：`overlap=10`、`quadratic_overlap`、词汇树回环检测、`loop_detection_min_index_distance`（4.2），适合视频。
- `exe/sfm.cc::RunPosePriorMapper`：设置 `use_prior_position=true`，可以用 `prior_position_std_{x,y,z}` 覆盖协方差，可选鲁棒损失（`prior_position_loss_scale=7.815`，即 χ²(3) 的 95% 分位）。
- `estimators/alignment.cc`：
  - `AlignReconstructionToLocations`：按图像名配对相机中心 → `EstimateSim3dRobust`，要求 `min_common_images≥3`。
  - `AlignReconstructionToPosePriors`：阈值 `max_error = sqrt(7.8147·median(trace(Σ)/3))`，没有协方差时使用 fallback σ。
- `estimators/cost_functions/pose_prior.h`：`AbsolutePosePositionPriorCostFunctor`，3-DoF 位置残差，按协方差加权。
- `exe/model.cc::RunModelAligner`：`alignment_type ∈ {plane, ecef, enu, enu-plane, enu-plane-unscaled, custom}`，参考文件每行 `name x y z`，`ref_is_gps` 默认为 true，`alignment_max_error` 必须大于 0，`merge_image_and_ref_origins` 可选。

### 2.10 COLMAP 浏览器查看器（`doc/viewer_src`，4.2 新增）

- `parser.ts`：`BinaryReader` 基于 DataView 小端读取，做边界和有限值校验，`count()` 会防止伪造的超大计数。它能解析 5 个 bin 文件（兼容旧格式），对 points3D 两遍扫描后排序。输出是 SoA TypedArray（`xyz: Float64Array`、`colors: Uint8Array`、`trackOffsets/trackImageIds/trackPoint2DIdxs: Uint32Array`），通过 `reconstructionTransferables()` 零拷贝转交给主线程。多相机 rig 用 `composeRigid(sensorFromRig, frame.rigFromWorld)` 合成 `camFromWorld`。
- `viewer.ts::ReconstructionViewer`：
  - 渲染：`WebGLRenderer` + `OrbitControls` + `ShaderMaterial`（GLSL）。拾取靠颜色 ID 渲染到 RenderTarget。
  - 过滤：默认 `minTrackLength=3`、`maxError=2px`。
  - `computeViewBounds`：最多抽 10 万个点，加上相机中心，**按轴取 1%/99% 分位**，得到视点中心和半径。
  - `cameraGeometry`：视锥四角为 `(±aspectW/2, ±aspectH/2, max(f/maxDim, 0.25))`，经 `toThreeQuaternion(camFromWorld.q).invert()` 旋转后加上投影中心。
- 局限：所有点放在一个 `THREE.Points` 里，没有 LOD；GLSL 在 WebGPURenderer 下不可用。**只移植解析器、数学库和视锥公式，不移植渲染器。**

---

## 3. 可复用算法与实现

### 3.1 统一坐标与位姿约定（规范性，与 r01 §3.1 一致并补全）

| 坐标系 | 定义 | 备注 |
|---|---|---|
| World | 局部 **ENU**（x 东、y 北、z 上），单位米，原点为 World 固定原点（WGS84 椭球高） | 同一 World 的所有采集共享原点，**禁止**用"第一条 GPS"当原点 |
| Engine gauge | 引擎自身的世界系，相对尺度 | VGGT：第 0 帧相机；LingBot：scale 帧；COLMAP：Normalize 后的系 |
| Camera | OpenCV：x 右、y 下、z 前 | 像素中心 +0.5（COLMAP 约定） |
| Body（无人机） | **FLU**（x 前、y 左、z 上，REP-103） | PX4 用 NED/FRD，在边界处转换 |
| three.js | Y-up，相机看向 −Z | 根节点 `rotation.x = −π/2`：(e,n,u) → (e,u,−n) |

命名规则：`T_a_b` 把 b 系坐标变到 a 系，等价于 COLMAP 的 `a_from_b`。位姿统一存 `T_world_cam`；紧凑形式为 `t[3] + q_xyzw[4]`；文件中用 f64，Web 端用 f32（位置相对 ENU 原点）。

```python
# 引擎/格式 → IR（T_world_cam, 4×4）
T_from_vggt    = inv(to4x4(E_vggt))            # VGGT 解码 = W2C
T_from_lingbot = to4x4(E_lingbot)              # LingBot 解码 = C2W（勿再求逆）
T_from_colmap  = inv(to4x4(R(qw,qx,qy,qz), t)) # COLMAP 文件 = cam_from_world，wxyz
q_xyzw = [qx,qy,qz,qw]; q_wxyz = [qw,qx,qy,qz]
# PX4 边界：p_enu = (p_ned.y, p_ned.x, -p_ned.z)
R_ENU_NED = [[0,1,0],[1,0,0],[0,0,-1]]; R_FRD_FLU = diag(1,-1,-1)
R_enu_flu = R_ENU_NED @ R_ned_frd @ R_FRD_FLU
# OpenCV 相机 → three 相机（FPV/视锥朝向）：R_three_cam = R_world_cam @ diag(1,-1,-1)
```

**float32 精度：** GPU 缓冲只能存相对 ENU 原点或节点原点的坐标。量级对比：相对 ENU 原点 8 km 时 float32 ULP 约 1 mm；UTM 北向坐标约 3.5e6 m 时 ULP 约 0.25 m；ECEF 约 6.4e6 m 时 ULP 约 0.5 m。model_aligner 默认的 ECEF 输出**绝不能**直接进前端。

### 3.2 Recon IR v1（Reconstruction 统一中间表示）

设计原则：
1. 以 COLMAP 4.x 的 Rig/Frame/Image/Camera 为骨架，交换格式直接用 COLMAP sparse。
2. 补上 COLMAP 缺的时间戳、深度、置信度、地理元数据和来源追溯。
3. **原始层（引擎规范系）与配准层（ENU）分开**：配准只改 `alignment.json` 中的 `T_enu_engine`，原始数据不回写，可以重复配准。
4. 所有引擎（LingBot / VGGT / COLMAP / Mock）产出同一套 IR。

| 实体 | 字段（类型） | 说明 |
|---|---|---|
| `ReconSession` | `id`、`world_id`、`engine{name,version,commit,ckpt_sha256,params}`、`input{kind: video/images/rosbag, uri, fps, stride}`、`gauge: "engine"/"enu"`、`scale_status: relative/gnss/rtk/lidar`、`time_base: "unix_ns"`、`created_at`、`ir_version: "1.0"` | 一次重建作业 |
| `Rig` / `Sensor` | `rig_id`、`ref_sensor`；`sensors[]{sensor_id, type: CAMERA/IMU/LIDAR/GNSS, T_body_sensor(Pose)?, time_offset_ns}` | COLMAP 只有 CAMERA/IMU 两种类型，IR 扩展了 LIDAR/GNSS。**RTK 天线杆臂**存在 `T_body_gnss` |
| `Camera` | `camera_id`、`model`（COLMAP 名称）、`width`、`height`、`params[]`（COLMAP 顺序）、`source: calibrated/predicted`、`pixel_convention: "colmap"` | 标定内参优先；引擎预测的焦距只进 QA |
| `Frame` | `frame_id`、`t_ns: int64`、`rig_id`、`T_world_body` 或 `T_world_cam`（Pose）、`pose_source: engine/rtk/gps/px4/fused`、`pose_cov[6×6]?`、`frame_type: 0/1/2`、`image{camera_id, uri, name}`、`gps{lat,lon,h_ell,cov}?`、`gravity_cam[3]?`、`quality{conf_mean_u8, valid_ratio}` | 一帧 = 一个时刻的 rig 状态 |
| `DepthMap` | `frame_id`、`w,h`、`K_depth`（深度图分辨率下的 K）、`dtype: f16`、`units: engine/m`、`kind: "z"`、`uri`（EXR half，与 r01 的 BSS 格式一致）、`conf_uri`（u8 PNG） | 默认只存关键帧 |
| `PointMap` | 与 DepthMap 相同，外加 `frame: "engine"` | **默认不落盘**，由深度和位姿推导 |
| `Trajectory` | 由 `Frame` 派生的视图：`t_ns[]`、`pos[]`、`q_xyzw[]`、`source`；插值：位置线性，姿态 slerp | 与仿真 `DroneState` 回放共用同一格式 |
| `Alignment` | `T_enu_engine: Sim3{s, q_xyzw, t}`、`method: rtk-sim3/gps-sim3/lidar-icp/pose-prior-ba/manual`、`inliers`、`rmse_m`、`rot_err_deg?`、`time_offset_s`、`lever_arm_m[3]`、`chunks[]{frame_range, s, q, t}` | 可重复计算，可审计 |

**产物目录**（对 r01 §4.2 的 `reconstruction/<job_id>/` 的补充；带 † 的是 r02 新增）：

```text
reconstruction/<session_id>/
  session.json  rig.json†  cameras.json†(IR 版)  cameras.txt(BSS)  traj_c2w.txt(BSS)
  frames.jsonl†          # 每行一个 Frame（含 t_ns、frame_type、gps、quality）
  alignment.json†        # Alignment（Sim3 + 报告），与 coordinate.json 的 T_enu_world 联动
  trajectory.bin         # Web：见下
  depth/*.exr  conf/*.png
  sparse/0/{rigs,cameras,frames,images,points3D}.bin†   # COLMAP 4 交换格式（ENU、米）
  qa.json†               # §3.10 的指标与门禁结果
```

**Web 端 `trajectory.bin`**（与 r01 一致，扩展为带头的 SoA 布局，全部小端）：

```text
header 32 B: magic "ANTR" | u16 version=1 | u16 flags | u32 N | f64 t0_unix_s | f32 origin_offset[3]
t_rel    f32[N]      # 相对 t0 的秒数（1 小时内精度约 0.24 ms）
pos      f32[N*3]    # ENU 米，相对 World 原点
quat     f32[N*4]    # xyzw，T_world_cam（或 T_world_body，由 flags 位 0 标识）
ftype    u8[N]       # 0 scale / 1 key / 2 non-key / 255 sim
conf     u8[N]       # 帧平均置信度（1−1/conf 量化）
camera   u16[N]
```

TS 类型（`apps/web/src/types/recon.ts`）：

```ts
export type Vec3 = [number, number, number];
export type QuatXYZW = [number, number, number, number];
export interface Pose { t: Vec3; q: QuatXYZW }           // 语义固定为 T_world_x
export interface Sim3 { s: number; q: QuatXYZW; t: Vec3 } // x_a = s·R·x_b + t
export interface CameraIntrinsics { id: number; model: string; width: number; height: number; params: number[]; source: 'calibrated' | 'predicted' }
export interface ReconFrame { id: number; tNs: string /* int64 用字符串 */; cameraId: number; T_world_cam: Pose; frameType: 0 | 1 | 2; poseSource: 'engine' | 'rtk' | 'gps' | 'px4' | 'fused'; confMean?: number; thumb?: string }
export interface ReconSession { id: string; worldId: string; engine: { name: 'lingbot-map' | 'vggt' | 'colmap-global' | 'colmap-pose-prior' | 'mock'; version: string };
  gauge: 'engine' | 'enu'; scaleStatus: 'relative' | 'gnss' | 'rtk' | 'lidar'; alignment?: { T_enu_engine: Sim3; method: string; rmseM: number; inliers: number } }
```

### 3.3 引擎适配器（Engine Adapter）

```python
class ReconEngine(Protocol):
    name: str
    caps: set[str]      # {"streaming","batch","depth","tracks","metric","ba","intrinsics"}
    def run(self, frames: Iterator[FrameInput], p: EngineParams) -> Iterator[EngineFrame]: ...
    def finalize(self) -> EngineSessionExtras: ...   # chunks、tracks、sparse model 等

@dataclass
class EngineFrame:            # 已经规范化，下游不再关心引擎差异
    idx: int; t_ns: int
    T_world_cam: np.ndarray   # 4x4 f64，C2W，处于 engine gauge
    K_orig: np.ndarray        # 原图像素，COLMAP 像素约定
    depth: np.ndarray | None; K_depth: np.ndarray | None   # z-depth，引擎尺度
    conf_u8: np.ndarray | None; frame_type: int

def vggt_crop_geom(W, H, target=518, patch=14):          # 复刻 load_fn.py 的 crop 模式
    new_w = target; new_h = round(H * new_w / W / patch) * patch
    sx, sy = new_w / W, new_h / H                          # 非等比缩放！
    crop_y = (new_h - target) // 2 if new_h > target else 0
    return sx, sy, crop_y

def K_model_to_orig(K, sx, sy, crop_y):
    fx, fy, cx, cy = K[0,0], K[1,1], K[0,2], K[1,2]
    cx_c, cy_c = cx + 0.5, cy + 0.5 + crop_y               # 整数索引 → COLMAP 连续坐标，并撤销裁剪
    return np.array([[fx/sx, 0, cx_c/sx], [0, fy/sy, cy_c/sy], [0, 0, 1]])

class VGGTEngine:   # batch：按块处理（§3.7），每块最多 S_max 帧
    def run(self, frames, p):
        for chunk in plan_chunks(frames, p.chunk, p.overlap):
            pred = model(images(chunk))                              # bf16, GPU
            E, K = pose_encoding_to_extri_intri(pred["pose_enc"], hw) # W2C
            for i, f in enumerate(chunk):
                T = inv4(E[i])                                        # → C2W
                yield EngineFrame(f.idx, f.t_ns, T, K_model_to_orig(K[i], *geom),
                                  pred["depth"][i, ..., 0], K[i],
                                  conf_to_u8(pred["depth_conf"][i]), 1)

def detect_pose_convention(E, K, depth, conf, i, j):
    """防呆测试：两帧 i、j 深度反投影后，在两种假设下各算一次对称 Chamfer 中位数，取小的那个。"""
    errs = {}
    for hyp in ("c2w", "w2c"):
        Ti, Tj = [(to4(E[k]) if hyp == "c2w" else inv4(to4(E[k]))) for k in (i, j)]
        Pi, Pj = unproject(depth[i], K[i], Ti, conf[i]), unproject(depth[j], K[j], Tj, conf[j])
        errs[hyp] = median_nn(Pi, Pj) + median_nn(Pj, Pi)
    return min(errs, key=errs.get)   # 在 CI 中对 VGGT 和 LingBot 各断言一次
```

`ColmapEngine` 读取 `sparse/*`，用 `pycolmap.Reconstruction` 按 `image.frame` 取出 `T_world_cam = inv(cam_from_world)`，时间戳从 `frames.jsonl` 按名字回填。`MockEngine` 沿用 r01 §4.3。

### 3.4 置信度规范化、百分位阈值、前缀子采样（直接服务于 MVP 的"疏密自动调节"）

```text
conf_n  = 1 − 1/conf = sigmoid(x) ∈ (0,1)        # VGGT 和 LingBot 都满足（conf = 1+exp(x)）
conf_u8 = round(255·conf_n)                      # 绝对阈值换算：conf≥5 → 204；conf≥1.5 → 85
构建期：每个 session/World 统计一个 hist[256]（u32），写入 metadata.json
UI 滑条 "过滤最低 p%"：thr = min{k | cumsum(hist)[k] ≥ p·N}   # O(256)，客户端完成，不扫描点
shader：if (aConf < uConfMin) discard;（WebGL2）/ TSL 中把点移到裁剪体外
```

**节点内预洗牌（Fisher–Yates，固定种子）：** 构建八叉树节点时，先把节点内的点随机打乱再写盘。这样任意前缀 `[0, k)` 都是均匀随机子样本。运行时"疏密调节"只需要 `geometry.setDrawRange(0, ⌊n·density⌋)`（WebGPU 用 `drawCount`），不需要重新上传或重新请求数据。它与 VGGT 的 `randomly_limit_trues` 等价，但可以增量调整。LOD 负责选节点（r01 §3.6 / web3d 单元），density 由 FPS 反馈控制，只调节点内前缀。UrbanScene3D 数据没有 conf，默认填 255。

### 3.5 Sim3 地理配准（Umeyama + LO-RANSAC + 重力增强 + 杆臂 + 时间偏移）

```python
def umeyama(src, dst, w=None):            # dst ≈ s·R·src + t（Umeyama 1991；COLMAP SimilarityTransformEstimator）
    w = ones(n) if w is None else w; w = w / w.sum()
    ms, md = w @ src, w @ dst; xs, xd = src - ms, dst - md
    U, D, Vt = svd((w[:, None] * xd).T @ xs)
    S = diag([1, 1, sign(det(U) * det(Vt))]); R = U @ S @ Vt
    s = (D * diag(S)).sum() / (w @ (xs**2).sum(1)); t = md - s * R @ ms
    return s, R, t

def georef(C_eng, frames, rtk, imu, origin, p):
    # 1) 同步与杆臂：相机中心 = 天线位置 + R_enu_body·(p_cam_b − p_ant_b)
    P = [enu(rtk.interp(f.t + dt), origin) + R_enu_body(imu, f.t) @ (p.lever_cam - p.lever_ant) for f in frames]
    # 2) 鲁棒估计：阈值由协方差决定（COLMAP AlignReconstructionToPosePriors 的做法）
    thr = sqrt(7.8147 * median(trace(cov) / 3))   # RTK σ=2cm → 5.6cm；GPS σh=2,σv=3 → 6.65m
    s, R, t, inl = lo_ransac(umeyama, C_eng, P, thr, min_sample=3, iters=10_000)
    # 3) 退化检测与重力增强：航带共线时必须做
    sv = svd(P[inl] - P[inl].mean(0), compute_uv=False)
    if sv[1] / sv[0] < 0.05 or p.always_gravity:
        L = 0.25 * sv[0] / sqrt(len(inl))          # 虚拟点杆长（米）
        g_eng = [R_eng_cam(f) @ g_cam(f) for f in frames[inl]]   # IMU 给出的"向下"方向，转到 engine 系
        src = vstack([C_eng[inl], C_eng[inl] + (L / s) * g_eng])  # 用第 1 遍的 s 把 L 换算为引擎单位
        dst = vstack([P[inl], P[inl] + L * [0, 0, -1]])
        s, R, t = umeyama(src, dst, w=r_[ones(n), p.w_gravity * ones(n)])
    # 4) 时间偏移：在 ±0.5 s 内按 5 ms 步长一维搜索 dt，使内点 RMSE 最小（与 r01 的互相关法互为校验）
    return Alignment(Sim3(s, R, t), rmse=..., inliers=len(inl), time_offset=dt)
```

| 本机实测（`r02_validate.py` / `r02_collinear.py`） | 结果 |
|---|---|
| 环绕航线 60 帧，RTK σ=2 cm | scale 相对误差 0.002%，相机中心 RMSE 0.011 m，最大 0.016 m |
| 环绕航线 60 帧，GPS σ=2 m/3 m | scale 相对误差 0.225%，RMSE 0.560 m，最大 0.902 m |
| numpy Umeyama 与 `pycolmap.align_reconstruction_to_locations` 对比 | Δs = 1.2e-10，Δt = 8.5e-11 |
| 直线航带 50 帧（400 m），纯 Umeyama | 旋转误差 **59.9°**（RTK）/ **117.1°**（GPS），属于退化 |
| 直线航带，重力增强两遍法 | 旋转误差 **0.00°**（RTK）/ **0.19°**（GPS），scale 误差 0.034% |
| WGS84 → ENU → WGS84 往返（pycolmap GPSTransform） | 最大误差 1.6e-9 m |

分块策略（与 r01 §3.7 一致）：VGGT 的每个块、LingBot 的每个窗口单独估计 Sim3，再对 `s_k` 做平滑，并检查相邻块在重叠帧上的位姿一致性。

### 3.6 WGS84 ↔ ECEF ↔ ENU（移植 `geometry/gps.cc`，Python 和 TS 共用同一套公式）

```ts
// apps/web/src/core/geo.ts（Python 版在 world/georef/geo.py 中逐行对应）
const A = 6378137.0, F = 1 / 298.257223563, E2 = F * (2 - F);   // WGS84
const d2r = Math.PI / 180;
export function llaToEcef(lat: number, lon: number, h: number): Vec3 {
  const sl = Math.sin(lat * d2r), cl = Math.cos(lat * d2r), so = Math.sin(lon * d2r), co = Math.cos(lon * d2r);
  const N = A / Math.sqrt(1 - E2 * sl * sl);
  return [(N + h) * cl * co, (N + h) * cl * so, (N * (1 - E2) + h) * sl];
}
export function ecefToLla([x, y, z]: Vec3): Vec3 {         // COLMAP 迭代法：最多 100 次，eps 1e-12
  const p = Math.hypot(x, y); let lat = Math.atan2(z, p), h = 0;
  for (let i = 0; i < 100; i++) {
    const s = Math.sin(lat), N = A / Math.sqrt(1 - E2 * s * s);
    const h1 = p / Math.cos(lat) - N, lat1 = Math.atan((z / p) / (1 - E2 * N / (N + h1)));
    const done = Math.abs(lat1 - lat) < 1e-12 && Math.abs(h1 - h) < 1e-12; lat = lat1; h = h1; if (done) break;
  }
  return [lat / d2r, Math.atan2(y, x) / d2r, h];
}
// R_enu_ecef：行向量分别为 e = [-sinλ, cosλ, 0]，n = [-sinφcosλ, -sinφsinλ, cosφ]，u = [cosφcosλ, cosφsinλ, sinφ]
export const enuFromLla = (lla: Vec3, o: Vec3): Vec3 => rotEnu(o, sub(llaToEcef(...lla), llaToEcef(...o)));
export const llaFromEnu = (enu: Vec3, o: Vec3): Vec3 => ecefToLla(add(llaToEcef(...o), rotEnuT(o, enu)));
```

高度基准：RTK 和 GNSS 给的是**椭球高**；MAVLink `GLOBAL_POSITION_INT.alt` 是 **AMSL**。IR 同时保存两者，`coordinate.json` 记录 `vertical_datum` 和 `geoid_undulation_m`。

### 3.7 VGGT 分块拼接（移植 LingBot `gct_stream_window_v2`，泛化到任意 C2W 引擎）

```text
规划：关键帧序列按 chunk=K（例如 64–128 帧，由显存决定）、overlap=O（8–16 帧）切块；每块的首帧选覆盖最好的关键帧
配对：prev 块尾部与 cur 块头部的重叠帧中，取两边都是关键帧的最后一帧作锚点 (a, b)
  R_ab = R_a · R_bᵀ               # 两者都是 C2W 旋转
  s_ab = median(depth_a / depth_b)  # 对所有配对重叠帧的有效像素逐像素求比值后取中位数，clamp 到 [1e-3, 1e3]
  t_ab = c_a − s_ab · R_ab · c_b
变换：cur 块的 pose：R ← R_ab·R，c ← s_ab·R_ab·c + t_ab；depth ← s_ab·depth；点图同样变换
拼接：每块贡献 [0, len−O) 帧，最后一块贡献全部；保存 chunk_transforms 和 chunk_scales 供审计
```

VGGT 与 LingBot 的唯一区别是先把 W2C 转成 C2W。块间的累积漂移由 §3.5 的逐块 RTK Sim3 纠正。

### 3.8 COLMAP 度量基准流水线（离线 Job，同时作为 QA 的"真值"来源）

```bash
colmap feature_extractor --database_path db.db --image_path frames \
  --ImageReader.single_camera 1 --ImageReader.camera_model OPENCV \
  --ImageReader.camera_params "fx,fy,cx,cy,k1,k2,p1,p2"         # 用 P600 吊舱的标定值
python write_priors.py db.db rtk.csv origin.json   # pycolmap: Database.write_pose_prior(PosePrior(CARTESIAN ENU, cov))
colmap sequential_matcher --database_path db.db --SequentialMatching.overlap 10 --SequentialMatching.loop_detection 1
# 或者（航点拍照）：colmap spatial_matcher --SpatialMatching.max_distance 100 --SpatialMatching.ignore_z 1
# A：直接得到度量结果（推荐作为基准）
colmap pose_prior_mapper --database_path db.db --image_path frames --output_path sparse_pp \
  --overwrite_priors_covariance 1 --prior_position_std_x 0.02 --prior_position_std_y 0.02 --prior_position_std_z 0.05
# B：大规模场景更快；输出无尺度，需要再对齐
colmap global_mapper --database_path db.db --image_path frames --output_path sparse_g
colmap model_aligner --input_path sparse_g/0 --output_path sparse_g_enu --ref_images_path enu.txt \
  --ref_is_gps 0 --alignment_type custom --alignment_max_error 0.1 --transform_path sim3.txt
colmap model_comparer --input_path1 sparse_pp/0 --input_path2 recon_ir_as_colmap --alignment_error proj_center
```

数千张图以上用 `hierarchical_mapper` 分簇处理；有 GPU 时 BA 切换到 Caspar。

### 3.9 Web 端可直接移植的几何（MVP）

**K → three.js 投影矩阵**（FPV 画面和 Camera FOV 视锥要与真实吊舱一致）。

推导：OpenCV 相机系到 GL 相机系 `(Xg,Yg,Zg) = (X,−Y,−Z)`，NDC 满足 `x=2u/W−1`、`y=1−2v/H`。

```ts
function projectionFromK(fx: number, fy: number, cx: number, cy: number, W: number, H: number, n: number, f: number) {
  return new THREE.Matrix4().set(
    2 * fx / W, 0,          1 - 2 * cx / W,       0,
    0,          2 * fy / H, 2 * cy / H - 1,       0,
    0,          0,          -(f + n) / (f - n),   -2 * f * n / (f - n),
    0,          0,          -1,                   0);   // camera.projectionMatrix.copy(P); projectionMatrixInverse.copy(P).invert()
}
// cx=W/2, cy=H/2 时退化为 PerspectiveCamera(fovY = 2·atan(H/(2fy)))
```

**视锥几何**（取自 COLMAP `viewer.ts::cameraGeometry`）：`corners = R_world_cam · (±w/2, ±h/2, d) + C`，其中 `w,h = W/max(W,H), H/max(W,H)`，`d = max(fx/max(W,H), 0.25)`，再乘以 UI 的 cameraSize。帧数超过 500 时按距离抽稀（r01）。

**鲁棒包围盒**（取自 `computeViewBounds`）：抽样最多 10 万点，加上相机中心，按轴取 1%/99% 分位，得到中心和半径 = ‖p99 − p1‖/2，用于初始视角和"适配视图"。UrbanScene3D 很需要这一步：Shenzhen 的 z 全范围 391 m，而 p1–p99 只有 132 m；Shanghai 分别是 645 m 和 116 m。**高度色带也要用 p1–p99**，否则因为没有颜色，渲染出来会一片灰。

**COLMAP 模型导入**（V0.1 可选功能"导入 COLMAP 工程"）：把 `parser.ts` 放进 `workers/colmap.worker.ts`，输出 SoA 数组并以 Transferable 交给主线程。点数 ≤ 20 万时直接做成一个节点；更多时交给后端 tiler。

### 3.10 重建 QA 指标与门禁

| 指标 | 定义 / 工具 | 建议门禁（World 可发布） |
|---|---|---|
| 相机中心误差（ATE） | Sim3 对齐后的中心 RMSE / 中位数（`model_comparer --alignment_error proj_center`，`pycolmap.compare_reconstructions`） | RTK ≤ 0.3 m；GPS ≤ 3 m |
| 旋转误差 | `angle(R_estᵀ R_ref)`，`ImageAlignmentError.rotation_error_deg` | 中位数 ≤ 1°，P90 ≤ 3° |
| 重投影误差 | `model_analyzer` 的 mean reprojection error | ≤ 1.5 px |
| 注册率 / 轨迹长度 | 注册帧数 / 总帧数；平均 track 长度 | ≥ 95%；≥ 3 |
| 配准质量 | Sim3 内点率、RMSE、scale 的逐块方差 | 内点率 ≥ 80%；逐块 s 的变异系数 ≤ 2% |
| 位姿约定自检 | §3.3 的 `detect_pose_convention` | CI 必须通过 |
| AUC@30 | VGGT 论文使用的相对位姿指标（Co3D，1B-Commercial 为 90.37） | 仅用于引擎选型，不作门禁 |

---

## 4. 在本项目中的落点与复用方式

| 条目 | 来源 | 落点模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|
| Recon IR v1（实体、目录、TS 类型、`trajectory.bin`） | COLMAP `scene/*`，VGGT/LingBot 输出契约 | `reconstruction/ir/`、`apps/web/src/types/recon.ts` | **V0.1** | port | 所有引擎、Mock 和 UI 共用的契约 |
| 位姿与坐标约定、K→投影、视锥、ENU→three | VGGT `visual_util`，COLMAP `viewer.ts`/`math.ts` | `apps/web/src/core/coords.ts` | **V0.1** | port | FPV、SensorLayer、ReconstructionLayer |
| WGS84/ECEF/ENU 公式 | COLMAP `geometry/gps.cc` | `world/georef/geo.py`、`apps/web/src/core/geo.ts` | **V0.1** | port | 遥测经纬度显示、Mock 地理原点、coordinate.json |
| 置信度 u8、直方图阈值、节点内预洗牌 | VGGT `head_act`、`demo_viser`、`helper.randomly_limit_trues` | `world/tiler/`、`apps/web/src/layers/pointcloud/` | **V0.1** | port | 疏密自动调节时不重传数据 |
| COLMAP 二进制解析器（Worker） | COLMAP `doc/viewer_src/parser.ts` | `apps/web/src/workers/colmap.worker.ts` | V0.1（可选） | port | 导入 COLMAP 工程，零后端依赖 |
| Sim3 地理配准（加重力增强、杆臂、时间偏移） | COLMAP `EstimateSim3dRobust`/`alignment.cc` | `reconstruction/georef/` | V0.1（GNSS）→ V0.5（RTK） | adopt pycolmap + numpy port | r01 建议 V0.1 就做 GNSS 配准，本单元补上退化处理 |
| 引擎适配器、方向自检 | VGGT `pose_enc`/`geometry`，LingBot benchmark | `reconstruction/engines/{vggt,lingbot,colmap,mock}` | V0.1 接口，V0.5 VGGT 实装 | port | 引擎可以替换 |
| VGGT 模型（批量关键帧引擎） | `vggt/models` | `reconstruction/engines/vggt` | V0.5 | adopt（可选） | 短序列或无序照片、交叉校验、3DGS 初始化 |
| 分块拼接 | LingBot `_pairwise_alignment`/`_warp_predictions` | `reconstruction/stitch/` | V0.5 | port | VGGT 长序列 |
| VGGT → COLMAP 导出 + BA | `np_to_pycolmap.py`、`demo_colmap.py` | `reconstruction/export/` | V0.5 | port | 为 gsplat/3DGS 做准备（Visual World 升级） |
| Sky mask | `visual_util.segment_sky` | `reconstruction/preprocess/` | V0.1 | port | 航拍必须用（r01 已采纳 LingBot 版） |
| 度量基准（pose_prior_mapper / global_mapper / model_aligner） | COLMAP CLI / pycolmap | `reconstruction/baseline/`（离线 Job） | V0.5 | adopt | 验收真值、LiDAR 缺失时的兜底 |
| QA（model_comparer、门禁） | COLMAP `exe/model.cc`、`estimators/alignment` | `reconstruction/qa/` | V0.5 | adopt | World 发布门禁 |

```text
reconstruction/
  ir/ (schema, io: jsonl/bin/colmap)   engines/{lingbot,vggt,colmap,mock}   stitch/
  georef/ (sim3, gravity, lever-arm, time-offset)   preprocess/ (undistort, skymask)
  export/ (colmap, bss, tum, ply)   baseline/ (pycolmap jobs)   qa/ (metrics, gates)
world/georef/geo.py ── 与 apps/web/src/core/geo.ts 共用测试向量（JSON）
```

---

## 5. 对比与推荐

| 维度 | LingBot-Map（r01） | VGGT | COLMAP global（GLOMAP） | COLMAP pose-prior（增量） |
|---|---|---|---|---|
| Star / 活跃度 | ★17k / 2026-09 | ★14.4k / 2026-05（后继 Omega） | ★12.8k / 2026-09 | 同左 |
| 输入 | 长视频，流式 | 1 到数百帧，批量 | 无序图像或视频帧 | 图像 + 位置先验 |
| 输出 | 位姿、深度、点（稠密） | 位姿、深度、点图、tracks（稠密） | 稀疏点、位姿 | 稀疏点、位姿 |
| 尺度 / 地理 | 相对尺度 | 相对尺度 | 任意规范系，需要 aligner | **直接得到度量 ENU**（原点为首条先验） |
| 速度 | 实时级（GPU） | 秒级（GPU） | 分钟级（CPU 可跑） | 分钟到小时级（CPU） |
| 精度来源 | 学习先验 | 学习先验，可选 BA | 几何 + BA | 几何 + BA + 先验 |
| 本项目角色 | 主引擎 | 第二引擎、交叉校验、3DGS 初始化 | 大场景离线基准 | **验收真值和兜底** |

**推荐排序：**
1. **COLMAP（adopt pycolmap 4.2.0）。** 数据模型、交换格式、地理配准和 QA 都离不开它，CPU 上能跑，pip 即可安装。V0.1 就把 IR 和 geo 工具建立在它的约定之上。
2. **VGGT（port 工具链，模型 V0.5 可选）。** 它是 LingBot 接口的"源头规范"，对照它才能看清 LingBot 的方向差异。它的 COLMAP 导出和 BA 路径是通往 3DGS 的最短链路。

---

## 6. 风险与注意事项

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| 1 | VGGT（W2C）与 LingBot（C2W）同名同 docstring，方向却相反 | 视锥反向、点云错位，但不会报错 | 适配器内完成转换；`detect_pose_convention` 进 CI |
| 2 | 前馈输出没有尺度，也不与重力对齐 | 物理仿真失真 | Sim3 是必经步骤；`scale_status` 在 UI 上显式标注 |
| 3 | 直线航带下 Umeyama 退化（实测 60–117°） | World 绕航线轴翻转 | 退化检测加重力增强（§3.5） |
| 4 | model_aligner 的 ECEF 默认输出、ENU 原点取首条 GPS、`enu-plane` 的 transform 被覆盖 | float32 抖动、多次采集原点不一致、Sim3 不可追溯 | 自己转 ENU，`ref_is_gps=0`，Sim3 自行复合保存 |
| 5 | global mapper 不使用位置先验，并在 Normalize 后输出多模型（4.2） | 流水线只取 `[0]` 会丢掉分量；没有尺度 | 遍历模型字典，按注册数选主模型；先验 BA 或 aligner |
| 6 | VGGT 非等比缩放和半像素差异 | 内参误差约 0.9%，在 1080p 下偏 ≤2 px | 按 §3.3 分轴换算 |
| 7 | VGGT/LingBot 不建模畸变，主点固定在中心 | 广角吊舱画面边缘几何误差 | 输入前用 COLMAP OPENCV 标定参数去畸变；标定内参优先 |
| 8 | VGGT 显存随帧数增长（全局注意力） | OOM | 分块加拼接（§3.7）；bf16；`frames_chunk_size`；修复后同显存可跑 2–3 倍帧 |
| 9 | VGGT demo 在 CPU 上直接崩溃（`get_device_capability`），BA 路径还要下载 ALIKED/SP/VGGSfM 权重 | 离线或本机环境跑不起来 | 引擎只在 GPU Worker 中运行，镜像预置权重；本机用 Mock |
| 10 | RTK 天线与相机的杆臂（10–30 cm）以及时间偏移 | 米级以下的系统偏差 | IR 的 `Rig` 记录 `T_body_gnss` 和 `time_offset`；§3.5 的一维时间搜索 |
| 11 | 椭球高与 AMSL 混用 | 高度偏差可达数十米 | 双字段保存，并记录 `vertical_datum` |
| 12 | 本地 UrbanScene3D 只有采样 PLY：没有影像、没有相机、没有颜色，单位也不统一（Chicago 的高程 ×1000 后，最高点离地约 530–564 m，接近 Willis Tower 含天线的 527 m，推断单位是 km） | 重建链路无法端到端验证 | 重建模块用 Mock 会话闭环测试；入库时归一化单位，把 `units_to_m` 写入 coordinate.json；V0.5 再下载 UrbanScene3D 真实场景影像做基准 |
| 13 | COLMAP 源码构建依赖重（Ceres、CUDA、Qt、faiss、ORT） | 构建耗时长 | 用 pycolmap wheel（CPU）；需要 GPU 时用官方 Docker 镜像 |
| 14 | COLMAP 查看器基于 GLSL `ShaderMaterial` 和单个 `Points` | 不兼容 WebGPURenderer，也不支持大点云 | 只移植解析器和数学库 |
| 15 | 许可：VGGT 原始 ckpt 仅限非商用 | 将来商用时有风险 | 本项目为科研用途可以忽略；需要时换用 `VGGT-1B-Commercial` |

---

## 7. 对设计文档 01-design.md 的优化建议

以下建议与 r01 §7 互补，重复的内容不再展开。

1. **§5 从"单一引擎"改为"引擎适配层"。** 把 "LingBot-Map 作为 Visual Reconstruction Engine" 改为：`Reconstruction Engine Adapter` 下挂 LingBot-Map（主引擎，流式）、VGGT（批量关键帧和交叉校验，V0.5）、COLMAP pose-prior/global（度量基准和兜底）、Mock（本机和 CI）。所有引擎输出同一套 Recon IR（§3.2）。原文"不需要首先建设传统的完整 SfM/MVS 流程"应改为"不以 SfM 作为主链路，但**保留 SfM 作为验收基准**"。
2. **§6 措辞纠正并补全链路。** "尺度可能存在误差"改为"**前馈重建天然无尺度、不与重力对齐**"。融合链路改为：`引擎（gauge）→ 去畸变 / sky mask → Sim3（RTK/GNSS，含杆臂、时间偏移、退化时重力增强）→ LiDAR 深度比校验尺度 → GICP 精配准 → 融合`。同时写明 Umeyama 在直线航带上的退化问题及对策。
3. **§7 Geographic 要写清定义。**
   - ENU 原点必须是 World 级别的固定原点（写在 coordinate.json 中），不能随采集变化。COLMAP 的 `ConvertPosePriorsToENU` 和 `model_aligner --alignment_type enu` 都默认"首条 GPS"，接入时必须绕开。
   - 同时保存椭球高和 AMSL。
   - 明确"GPU 缓冲只存相对坐标"，给出 float32 精度对照：ENU 8 km ≈ 1 mm，UTM ≈ 0.25 m，ECEF ≈ 0.5 m。
4. **§7/§41 增加 Recon IR 与 COLMAP 4 交换格式。** 在 `worlds/<id>/reconstruction/<session>/` 下定义 `session.json`、`rig.json`、`cameras.json`、`frames.jsonl`、`alignment.json`、`sparse/0/*.bin`、`qa.json`，与 r01 的 BSS 文本和 `trajectory.bin` 并存。`coordinate.json` 在 r01 字段基础上增加 `units_to_m`、`source_crs`、`vertical_datum`、`geoid_undulation_m`、`alignment_ref`。
5. **§8 双表达增加"原始层 / 配准层"分离。** 引擎原始输出（gauge 系）永不回写，配准只是可重算的 `T_enu_engine`。这样换引擎、重配准、比较版本时都不会破坏数据。
6. **§15 数据格式补充**：COLMAP sparse（BA、3DGS 训练和 QA 的通用交换格式）、TUM/BSS 轨迹、EXR 深度、u8 置信度。说明 VGGT → COLMAP → gsplat 是现成可用的 Visual World 升级路径（对应 §8 的 3DGS）。
7. **§28 DroneState 补齐坐标语义**：`pose.T_world_body`（ENU/FLU，`q_xyzw`）、`t_ns` 时间基准、`gps{lat, lon, h_ell, alt_amsl}`，以及 PX4 NED/FRD 边界转换规则。FPV 相机用 `T_body_cam` 和标定 K（§3.9 投影矩阵），做到"虚拟吊舱 = 真实吊舱"。
8. **§33 技术栈表**增加 `pycolmap 4.2（CPU wheel，离线依赖）`，GPU 可选 Caspar BA。在 02-refs.md 中把 `cvg/glomap` 标注为"已并入 COLMAP 4.x，使用 `colmap global_mapper`"。
9. **新增"重建质量与验收"章节**：给出 §3.10 的指标和门禁。World 发布前必须在 UI 上展示 `scale_status`、配准 RMSE 和 QA 结论。
10. **§43 MVP 补充两个低成本交付物**：①Recon IR 加 Mock 会话生成器，让 ReconstructionLayer（轨迹、视锥、回放）在无 GPU 的环境中也能开发和测试；②"导入 COLMAP 工程"（移植官方解析器）。另外，UrbanScene3D 入库时必须先做单位和上方向归一化（Chicago 疑似以 km 为单位）。
11. **§37 频率表**补充时间基准：所有来源（视频帧、RTK、IMU、PX4、仿真）统一用 `t_ns`（int64，UTC 或会话单调时钟二选一，并写进 session），这是配准与回放对齐的前提。

---

## 附录：本机验证记录（`.cache/research/`）

```text
r02_validate.py
[vggt-sim] cam0 center=[0. 0. 0.]  avg_scale=165.481 m/unit
[umeyama-RTK] scale=165.4776 (true 165.4808) rel_err=0.002%  center RMSE=0.011 m  max=0.016 m
[umeyama-GPS] scale=165.1089 (true 165.4808) rel_err=0.225%  center RMSE=0.560 m  max=0.902 m
[gps] ENU->LLA->ENU roundtrip max err=1.63e-09 m
[numpy vs pycolmap] |ds|=1.23e-10  |dt|=8.46e-11
[io] files: ['cameras.bin', 'frames.bin', 'images.bin', 'points3D.bin', 'rigs.bin']
[io] pycolmap Rotation3d.quat order is xyzw
r02_glomap_cpu.py (100 frames, 3000 pts):  global 121.93s | incremental 134.81s (reg 100/100)
r02_prior.py (40 frames): incremental+pose_prior 12.6s scale=1.000000 | global 17.2s scale=1.368365
r02_collinear.py (400 m straight leg):
  sigma=0.02m plain rot err=59.92 deg | gravity-augmented 0.00 deg
  sigma=2.0m  plain rot err=117.12 deg | gravity-augmented 0.19 deg, scale err 0.034%
UrbanScene3D Chicago z (x1000): tallest above p5 ground ≈ 564 m, p99.99 ≈ 530 m → units likely km
```
