# R01 研究笔记：LingBot-Map 视觉流式重建 + viser Web 可视化

> 研究单元：r01 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §5、§14–16、§36–37、§41、§43–44
> 仓库快照：
> - `refs/recon/lingbot-map` @ `849e690`（2026-09-08，17140 stars，Apache-2.0）
> - `refs/recon/viser` @ `56712d3`（2026-09-24，2794 stars，MIT，`__version__ = 1.1.1`）
>
> 本文所有路径均相对各自仓库根目录。结论全部来自源码精读，另有两处本机实测（UrbanScene3D 坐标范围、法线主轴）。凡是估算都会明确标注"估算"。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **lingbot-map** | 前馈式流式三维重建基础模型。GCT（Geometric Context Transformer）配合 FlashInfer paged KV cache，从单目视频逐帧输出相机位姿、深度和置信度 | **adopt**：在 GPU Worker 里把 `GCTStream` 当作库调用，不走 CLI。**port**：voxel-morton 去重、centroid octree 与 LOD 选择、窗口 Sim(3) 拼接、follow camera、RANSAC-Umeyama 地理配准 | V0.1 离线重建 Job；V0.5 RTK/LiDAR 融合；V1.0 在线流式建图 | 5/5（Reconstruction Engine 首选） |
| **viser** | Python <-> Web 的 3D 可视化框架。服务端是 msgpack、zstd 与二进制 WebSocket，客户端是 React-Three-Fiber。LingBot-Map 的官方 viewer 就是它 | **port**：混合二进制线协议、消息合并窗口、晚加入状态回放、Worker 解码与节拍平滑、点云 shader、GPU buffer 复用、pose 瞬时更新、录制格式、headless 基准 harness。**adopt**：`viser.transforms`（SO3/SE3），另可做内部 Recon QA viewer。**reference**：整体 UI（Mantine，与 shadcn 冲突，不进产品） | V0.1–V0.2（协议与渲染）；V0.1 内部 QA 工具 | 4/5（协议与工程范式的最佳参考，但不能当产品前端） |

**关键结论（实现者先读这几条）：**

1. **LingBot-Map 输出的坐标只有相对尺度，也没有和重力对齐。** 世界系是 scale 帧附近的 OpenCV 相机系（x 右、y 下、z 前），单位没有物理意义。户外大场景漂移很明显：官方 benchmark 的 ATE 在 KITTI 是 24.0，VBR 31.2，Oxford Spires 5.37。所以 **GNSS/RTK 的 Sim(3) 配准必须从 V0.5 提前到 V0.1**，否则无人机仿真里的 m/s、风速、碰撞都没有意义。
2. **位姿约定容易踩坑。** `pose_enc` 解码出来是 **C2W**（`benchmark/methods/lingbot_map.py::_process_outputs` 有注释 "output is C2W directly"）。`demo.py::postprocess` 求逆后存进 `predictions["extrinsic"]` 的是 **W2C**，虽然代码注释写的是 "Convert w2c to c2w"。四元数是 **XYZW**（`utils/rotation.py`），viser 用 **wxyz**，three.js 的 `Quaternion` 构造参数是 (x,y,z,w)。
3. **显存。** KV 池在第一次 forward 时整块预分配。按代码推导，518×378 下 bf16 KV 池约 **10.9 GB**，加上权重和激活，**推荐 ≥24 GB 显卡**（估算，见 §2.4）。本机没有 GPU，所以必须有一个 **Mock Reconstruction Engine**（§4.3），让前后端链路在 CPU 上也能跑通。
4. **viser 没有点云 LOD。** 它每个 scene node 一次性下发完整数组，默认把坐标压成 float16。**本机实测 UrbanScene3D**：New York 最大坐标 1607 m，float16 在这个量级的 ULP = **1.0 m**；Shanghai 7052 m，ULP = **4.0 m**。所以世界坐标绝不能用 float16 传输，必须做**节点局部量化**（uint16 normalized，§3.12）。viser 可以借鉴协议，大点云流式要走 Potree 式八叉树加 HTTP 静态分块。
5. **"视频 → 点云 + 轨迹 → World Package"要做成异步 Job。** 分阶段幂等（带 `.complete` 标记）、逐帧推送进度与预览、支持取消和按阶段重试。详见 §4.2。
6. **推荐顺序**：lingbot-map（核心引擎，必须）> viser（协议与工程参考，按模块移植）。两者分工不同，不是互相替代。

---

## 1. 仓库概览

| 项 | lingbot-map | viser |
|---|---|---|
| 最后提交 | 2026-09-08（仍很活跃：6 月修 SDPA KV bug，4 月加速、修 keyframe bug，5 月发 benchmark） | 2026-09-24（RSS 2026 引用更新，维护频繁） |
| 语言与规模 | Python + 少量 CUDA；核心包约 12k 行（`lingbot_map/`），另有 `demo_render/` 离线渲染和 `benchmark/` | Python 约 22k 行（`src/viser`）+ TypeScript 客户端约 60+ 文件（`src/viser/client/src`） |
| 核心依赖 | torch 2.8 + cu128（推荐）、flashinfer-python（可选，回退 SDPA）、onnxruntime（sky mask）、viser、trimesh；离线渲染额外要 open3d、kaolin 和自编 CUDA 扩展 | websockets、msgspec、zstandard、numpy、trimesh；客户端：three 0.186、@react-three/fiber 9、drei 10、Mantine 9、@msgpack/msgpack、zstddec（WASM） |
| 模型 | `robbyant/lingbot-map`（HF/ModelScope）；`lingbot-map-stage1` 可加载进 VGGT | — |
| 构建 | `pip install -e .`；`[vis]` 装 viser 等 | `pip install viser`（wheel 自带编好的客户端）。客户端源码构建要求 **Node ≥ 24**（`package.json engines`），本机是 Node 22 |
| 许可 | Apache-2.0（科研用途，按要求忽略） | MIT |

**LingBot-Map** 在 VGGT 的交替注意力（frame/global）主干上做了流式化，模型思路和工程实现都值得看：
- **Anchor context**：前 `num_scale_frames=8` 帧作为 scale 帧，一起做双向注意力，确定坐标和尺度基准，这些帧永不从 KV 中淘汰。
- **Pose-reference window**：最近 `kv_cache_sliding_window=64` 帧的 patch token 留在 KV 里。
- **Trajectory memory**：被淘汰帧的 6 个 special token（camera、4 个 register、scale）永久保留，用来抑制长程漂移。
- 训练时的 video RoPE 覆盖 320 个视角。超过 320 个关键帧质量会下降，所以需要 keyframe 间隔或 windowed 模式。

**viser** 本身是通用可视化框架。和本项目相关的是三块：**(a)** 一套非常讲究的"Python 服务端 → 浏览器"二进制消息协议和缓冲语义；**(b)** 一套在 R3F 上做高频更新和大 buffer 更新的性能工程经验（大量注释记录了踩过的坑）；**(c)** 点云、相机视锥、轨迹、Gaussian Splat 的渲染组件。

---

## 2. 源码结构与关键模块

### 2.1 LingBot-Map 目录职责

```text
demo.py                         交互 demo 入口：load_images → load_model → [compile] → inference → postprocess → PointCloudViewer
lingbot_map/
  models/gct_base.py            GCTBase：heads 装配 + forward()；DPTHead(depth: exp / conf: expp1)
  models/gct_stream.py          GCTStream：streaming 推理（scale 阶段 + 逐帧 KV）；_set_skip_append；clean_kv_cache
  models/gct_stream_window.py   windowed 版：flow 关键帧、_pairwise_alignment、_align_and_stitch_windows、inference_windowed
  models/gct_stream_window_v2.py 同上变体（含 KV debug）
  aggregator/{base,stream}.py   24 层 frame + 24 层 global 交替注意力（ViT-L，16 head，4 register token）
  layers/flashinfer_cache.py    FlashInferKVCacheManager：two-stream paged KV cache
  heads/camera_head.py          CameraCausalHead：因果 + KV 的相机头，num_iterations=4 迭代细化
  heads/dpt_head.py             DPTHead（深度/点图）
  utils/pose_enc.py             absT_quaR_FoV 9 维编码 <-> extrinsic/intrinsic（主点固定在图像中心）
  utils/geometry.py             unproject_depth_map_to_point_map、closed_form_inverse_se3、umeyama
  utils/load_fn.py              load_and_preprocess_images（crop 模式）
  vis/point_cloud_viewer.py     PointCloudViewer（viser）：逐帧节点、置信度过滤、视锥、播放、GLB/视频导出
  vis/viser_wrapper.py          轻量 viewer：随机采样 ≤6M 点，按置信度百分位过滤
  vis/sky_segmentation.py       ONNX 天空分割 + 磁盘缓存 + conf 置零
  vis/glb_export.py             predictions_to_glb（trimesh）
demo_render/                    长序列离线渲染（Open3D/Kaolin/CUDA 扩展），保存 per-frame NPZ
  rgbd_render/geometry/{voxel,octree,unproject}.py   *可移植：morton 体素去重、centroid 八叉树 LOD、带 jitter 的反投影
  rgbd_render/camera.py         *可移植：compute_global_up、follow/birdeye 相机路径
  interactive_viewer/server.py  服务端渲染 + WebSocket 推 JPEG（瘦客户端方案）+ EDL
benchmark/                      9 个数据集评测（ATE/RPE/AUC/点云），BSS 存储格式，umeyama RANSAC，viser 结果 viewer
```

### 2.2 模型输入输出数据契约（落盘和服务化的依据）

**输入**。`demo.py::load_images` → `utils/load_fn.py::load_and_preprocess_images(mode="crop", image_size=518, patch_size=14)`：
- 宽度固定缩放到 518；高度 `round(h·518/w / 14)·14`；高度超过 518 时中心裁剪。
- **P600 吊舱 1920×1080 视频 → 518×294**（37×21 = 777 patch/帧）；4:3 → 518×378（999 patch/帧）。
- 张量为 `[S,3,H,W] float32 ∈[0,1]`。**demo 会把所有帧一次性读进 CPU 内存**：518×378 每帧约 2.35 MB，1 万帧约 23.5 GB。服务化时必须改成流式读取（§3.3）。
- 视频抽帧：`interval = round(src_fps / fps)`，默认 `--fps 10`，帧先落盘为 `<video>_frames/*.jpg` 再读回。

**输出**。`GCTStream.inference_streaming` 返回 dict（B=1）：

| key | shape | dtype | 含义与约定 |
|---|---|---|---|
| `pose_enc` | [B,S,9] | f32 | `[tx,ty,tz, qx,qy,qz,qw, fov_h, fov_w]`。解码后是 **C2W**，OpenCV 相机系，四元数 **XYZW** |
| `depth` | [B,S,H,W,1] | f32 | 深度，相对尺度（与 scale 帧一致），activation=`exp` |
| `depth_conf` | [B,S,H,W] | f32 | `1+exp(x)`，因此 **≥1**；demo 阈值 1.5，离线 preset 1.3（户外）/ 2.5（室内）/ 4.0（默认） |
| `world_points(_conf)` | [B,S,H,W,3] | f32 | **checkpoint 默认 `enable_point=False`，不产出**。点云一律由 depth 反投影得到（benchmark README 有明确说明） |
| `frame_type` | [B,S] | u8 | 0=scale，1=keyframe，2=non-keyframe |
| `is_keyframe` | [B,S] | bool | `frame_type != 2` |
| `images` | [B,S,3,H,W] | f32 | 回传原图，用于着色 |
| `chunk_scales` / `chunk_transforms` | [B,W] / [B,W,4,4] | f32 | 仅 windowed 模式有：每个窗口相对第一个窗口的 Sim(3) |

**内参**：`pose_encoding_to_extri_intri` 的公式是 `fy = (H/2)/tan(fov_h/2)`、`fx = (W/2)/tan(fov_w/2)`、`cx=W/2`、`cy=H/2`，**主点固定在中心，没有畸变参数**。P600 已知内参时，只能在输入端先做去畸变。

**`demo.py::postprocess`** 之后，`predictions["extrinsic"]` 是 W2C `[S,3,4]`（求逆的结果），`intrinsic` 是 `[S,3,3]`。viewer（`_process_pred_dict`）把它当 W2C 用，先反投影，再 `closed_form_inverse_se3` 得到 C2W 画视锥。`demo_render/rgbd_render/data/loader.py` 也要求 `"NPZ must contain 'extrinsic' (W2C poses)"`。结论：**Job 落盘统一存 C2W，并在字段名上写明 `T_world_cam`**，避免继承这套歧义。

### 2.3 推理模式

| 模式 | 入口 | 机制 | 适用 |
|---|---|---|---|
| streaming | `gct_stream.py::GCTStream.inference_streaming` | Phase 1：前 8 帧组成一个 block 做双向注意力（`num_frame_per_block=scale_frames`）；Phase 2：逐帧 `forward(num_frame_per_block=1, causal_inference=True)` | ≤320 帧，kf=1 |
| streaming + keyframe | 同上，`keyframe_interval=k` | 非关键帧 `_set_skip_append(True)`：会 attend 到 cache，但不写入 cache。demo 自动取 `k = ceil(S/320)`（`demo.py main`） | 320 到约 3000 帧 |
| flow keyframe | `gct_stream_window.py::inference_streaming(flow_threshold>0)` | 先前向（defer eviction），用 `_compute_flow_magnitude`（用当前深度和两帧位姿计算诱导光流，stride 8 采样平均像素位移）决定是否成为关键帧，否则 `_rollback_last_frame` 回滚；`max_non_keyframe_gap=30` 强制插入关键帧 | 无人机悬停与高速交替的视频（推荐） |
| windowed | `gct_stream_window.py::inference_windowed` | 每个窗口 `clean_kv_cache()` 从头开始。窗口覆盖实际帧数 = `scale + (window_size − scale)·k`。重叠帧数 = `max(scale, overlap_keyframes·k)`。窗口之间用 `_align_and_stitch_windows` 做 Sim(3) 拼接 | >3000 帧、出现 pose collapse 时 |

README 的推荐用法：`--mode windowed --window_size 128 --overlap_keyframes 8 --keyframe_interval 2`（长视频）。**注意 `window_size` 计的是 KV 槽位（关键帧数），不是实际帧数。**

### 2.4 KV Cache 与显存（服务化资源规划的依据）

`layers/flashinfer_cache.py::FlashInferKVCacheManager`，双流分页：
- **Patch 流**（可回收）：`page_size = patches_per_frame`（999 或 777），每帧一页。scale 页永不淘汰，窗口页超过 `sliding_window` 就淘汰。池大小 `max_patch_pages = scale + window + 16 = 88`。
- **Special 流**（只追加）：每帧 6 个 token 连续打包，页数 = `ceil((max_frame_num+100)·6 / page_size) + 16`。**池耗尽时直接 `assert` 崩溃**（"special page pool exhausted … Increase max_total_frames"）。
- 每个 block 的物理布局是 `[max_num_pages, 2, page_size, 16 heads, 64]`，共 24 个 global block（`aggregator/stream.py::_get_flashinfer_manager`，`num_blocks=self.depth`）。

**显存估算公式（bf16，按代码推导，未实测）：**

```text
bytes_per_page_all_layers = 24(L) × 2(K,V) × P × 16 × 64 × 2B = 98,304·P B
  P=999 (518×378) → 98.2 MB/页；P=777 (518×294) → 76.4 MB/页
KV_pool = (scale + window + 16 + ceil((max_frame_num+100)·6/P) + 16) × bytes_per_page
  默认 (8+64+16) + (7+16) = 111 页 → 518×378: ≈10.9 GB；518×294: ≈8.5 GB
权重：aggregator ≈0.9B 参数 bf16（demo 会把 aggregator cast 成 bf16，省 2–3 GB）+ heads fp32 ≈1 GB
峰值 ≈ KV_pool + ~3 GB 权重 + 激活（DPT head 以 fp32 运行）
```

**资源档位**（估算）：24 GB 以上直接用默认值；16 GB 需要 `--kv_cache_sliding_window 32`（patch 池 88→56 页）、`--num_scale_frames 2`、`--camera_num_iterations 1`；8 GB 需参考 README 提到的社区 fork（rtx4060-8g）。速度：README 标称 518×378 约 20 FPS，`--compile` 再快约 5 FPS（warmup 30–60 s，仅 streaming 模式）。

### 2.5 `--mask_sky`

`vis/sky_segmentation.py`：
- `run_skyseg` 把图像缩放到 320×320，做 ImageNet mean/std 归一化，ONNX 输出 min-max 归一化到 0–255，结果是"天空处高"。
- `_result_map_to_non_sky_conf` 取反得到非天空置信度。`apply_sky_segmentation` 用 `>0.1` 二值化后乘到 `conf` 上，于是天空像素 conf=0，被阈值过滤掉。
- mask 缓存在 `<image_folder>_sky_masks/`，带版本戳 `.skyseg_cache_version = imagenet_norm_softmap_inverted_v3`。
- 模型 `skyseg.onnx` 在 CWD 缺失时会从 HF 自动下载。离线批渲染用的是动态 batch 版 `skyseg_batch.onnx`。

**对无人机航拍很关键**：天空像素的深度发散，不做 mask 会在远处形成"天空壳"噪声。代码里还有一个小 bug：mask 数量少于帧数时，日志写的是 "leaving the remaining frames unmasked"，实际却用 **0 填充**，这些帧的点会被**全部过滤**。

### 2.6 输出如何落盘（点云 + 相机轨迹）

LingBot-Map 本身**没有**标准的"重建产物"格式，只有三种形态：
1. **交互 viewer**（`vis/point_cloud_viewer.py`）：逐帧 scene node `/frames/{step}/pred_pts` 和 `/frames/{step}/camera`，视锥颜色按 viridis 时间渐变。`parse_pc_data` 的处理顺序是：去 NaN → `conf > vis_threshold` → 步长 `downsample_factor=10` 下采样。Export GLB 用 trimesh 写点、视锥和轨迹管线（`_build_trajectory_tube`）。
2. **per-frame NPZ**（`demo_render/batch_demo.py::save_predictions_npz`，`--save_predictions`）：每帧一个 `frame_%06d.npz`（所有带帧维度的 key 的切片），非序列量写入 `meta.npz`，并行写盘。**`world_points` 很占空间**，loader 注释举例 25k 帧约 46 GB，所以 `_NEEDED_KEYS` 故意不读它。
3. **benchmark BSS**（`benchmark/README.md`）：`traj.txt` 每行 13 个数（timestamp + 3×4 C2W 行主序），`intrinsics.txt` 每行 7 列（ts fx fy cx cy w h），`depth/*.exr`，`confidence/*.exr`，`points.ply`，`.complete.json` 完成标记。**这是最接近我们需求的规范格式，World Package 的 `reconstruction/` 直接沿用它的轨迹和内参文本格式。**

### 2.7 `demo_render` 中值得移植的几何模块

- `geometry/unproject.py::unproject_depth_batch_gpu`：像素步长 `downsample` 下采样，并加 **±0.4·downsample 的子像素 jitter** 抗网格摩尔纹，同时做 `0 < d < max_depth` 截断。
- `geometry/voxel.py::VoxelGridCUDA` + `render_cuda_ext/voxel_morton/voxel_morton.cu`：`floor(p/voxel)` 加偏移 `2^20` 后编码为 **21-bit/轴的 63-bit Morton**，然后 `sort` 加相邻比较去重，颜色保留**首次出现**（`color_update='first'`）。每个点带 `frame` 编号，`finalize` 时按 frame 稳定排序，`compute_ptrs(num_frames)` 用 `searchsorted` 得到**每帧可见点的前缀指针**。这就是"重建过程回放"的数据结构。
- `geometry/octree.py::OctreeSPC`：**centroid 八叉树**，最细层 cell 的质心和均值色，自底向上加权聚合到根。每层记录 `min_frame`。`lod_select` 按**投影像素尺寸**（`proj_px = cell_size·f/z`，目标 1.5 px）决定输出或细分，同时做保守视锥裁剪（加 half_diag 边距）和"未揭示帧"过滤。
- `camera.py`：`compute_global_up`（取相机 −Y 轴的中位数作为世界上方向）；`_follow_camera_at`（平滑窗口、水平前向投影；相机接近垂直时退化为用速度方向）；`make_birdeye_path`。
- `interactive_viewer/server.py`：`_edl_shade`（Eye-Dome Lighting，对 log2 深度取 8 邻域正差均值，`shade = exp(−300·strength·resp)`），以及 aiohttp WebSocket 推 JPEG 的服务端渲染模式。
- 预设（`demo_render/config/outdoor_drive.yaml`）：`voxel_size 0.003`（模型尺度单位）、`octree_level 16`、`max_depth 250`、`downsample 2`、`jitter true`、`mask_sky true`、`vis_threshold 1.3`。

### 2.8 viser 服务端

- **消息基类** `infra/_messages.py::Message`：dataclass 定义，`as_serializable_dict(binary_buffers)` 把 numpy 数组抽出来，替换成占位符 `{"__binary_index": i, "dtype": "<f4"}`，这样 msgpack 不需要遍历大数组。消息类型写在 `type` 字段（类名）里，TS 类型由 `_typescript_interface_gen.py` 自动生成（`sync_client_server.py --sync-messages`）。
- **线格式** `infra/_infra.py::_message_producer`（直接抄）：

```text
[8B] msgpack 解压后长度 (LE u64)
[8B] zstd 压缩后长度 (LE u64)
[N B] zstd(level=1) 压缩的 msgpack：{"messages":[...], "timestampSec": perf_counter, "binaryBufferLengths":[...]}
[P B] 补齐到 8 字节对齐
[M B] 原始二进制 buffer 依次拼接，每个都 8 字节对齐（不压缩：浮点数组压缩率低，还费 CPU）
```

- **缓冲语义** `infra/_async_message_buffer.py::AsyncMessageBuffer`：
  - `redundancy_key` **latest-wins 合并**：同一实体的同类 update 只保留最新一条，create 和 remove 共用一个槽位互相覆盖（`_messages.py::Message.redundancy_key`）。
  - `window_generator`：每 1/60 s 或每 128 条消息组成一个窗口一起发，`atomic()` 块内暂停发送。
  - `persistent_messages=True` 的广播缓冲：新连接的客户端**先回放全部持久消息**，再收到 `ReplayDoneMessage` 标记。
  - 每个连接有自己的 GC cursor。
  - 每个客户端两个 producer（per-client buffer + broadcast buffer）和一个 consumer。
- **握手**：WebSocket 子协议 `viser-v{version}`，版本不一致时以 1002 关闭。`serve(max_size=50MB, compression=None)`，关闭 permessage-deflate 是因为太慢。
- **录制** `StateSerializer.serialize`：`(t, message)` 列表，二进制 buffer 按 **sha256 去重**，整体 zstd level 12 压缩，扩展名 `.viser`。
- **点云 API** `_scene_api.py::add_point_cloud`：`precision="float16"` 是默认值，颜色 uint8。`set_up_direction` 默认 **+Z up**（ROS/Blender 习惯），客户端通过根节点旋转换算到 three.js 的 Y-up。
- **相机**：`ViewerCameraMessage` 是客户端到服务端，T_world_camera（OpenCV，+Z 前），包含 fov/near/far/宽高/look_at/up；客户端节流 20 ms（`CameraControls.tsx`）。
- `viser.transforms`：纯 numpy 的 SO2/SE2/SO3/SE3，支持 exp/log、rpy、四元数互转和批量运算（内部 wxyz）。

### 2.9 viser 客户端（three.js 实现）

| 文件 | 做了什么 | 对我们的价值 |
|---|---|---|
| `WebsocketClientWorker.ts` | 在 **Web Worker** 里解码。`binaryType="arraybuffer"`，zstd 用 WASM 解压 msgpack，二进制部分直接构造 **TypedArray 视图（零拷贝）**，整个 ArrayBuffer 以 **transferable** 方式交给主线程。`orderLock` 保证顺序。**节拍平滑**：用服务端 `timestampSec` 估计时钟差，早到的消息 `setTimeout(Δ·0.95)` 延后发出，落后超过 100 ms 立即 flush。`generation` 计数丢弃旧连接的消息 | 遥测和点云增量解码不阻塞主线程 |
| `BinaryMessageDecode.ts` | `replaceBinaryPlaceholders`，dtype→TypedArray 映射（`<f2` 映射为 Uint16Array） | 协议对端实现 |
| `messageQueue.ts` | 消息入队后在渲染循环里排空；标签页隐藏时直接排空；纯 GUI 消息不唤醒 3D 渲染 | 避免 rAF 暂停时积压 |
| `batchedSceneUpdates.ts` | 一个 batch 内对同一 node 的属性更新 park 起来合并，一次 store 写入 | 60 Hz 流量下 store 写入是 O(节点数) 而不是 O(消息数) |
| `MessageHandler.tsx`（`SetPositionMessage`） | 位姿写进 `viewerMutable.nodePoseData`（**可变 ref，不触发 React 重渲染**），并置 `poseUpdateState="needsUpdate"`，由渲染循环应用 | 多机 20–50 Hz 位姿的正确写法 |
| `ThreeAssets.tsx::PointCloudMaterial` | GLSL 点着色器：`gl_PointSize = scale / −z_view`；形状用 Lp 范数（square=∞，circle=2，diamond=1，rounded=3，sparkle=0.6）；gradient 着色；支持 fog | 点云 shader 模板 |
| `utils/bufferGeometrySync.ts` | **GPU buffer 复用规则**：长度和类型不变时换数组并置 `needsUpdate`（走 bufferSubData）；变了就 `geometry.dispose()` 后整体重建。否则会泄漏 GL buffer 或触发 "Resizing buffer attributes is not supported" | 点云节点加载和卸载时必须遵守 |
| `CameraFrustumVariants.tsx` | 视锥：`y=tan(fov/2)`、`x=y·aspect`、`z=1`，再按 `cbrt(xyz/3)` 归一化，使视觉体积恒定；可贴 JPEG 缩略图；OpenCV +Z 前 | 轨迹关键帧视锥 |
| `WorldTransformUtils.ts` | 用根节点旋转实现"Python 世界系（Z-up）<-> three 世界系（Y-up）"的转换 | ENU 的做法同理 |
| `FilePlayback.tsx` / `PlaybackDecode.ts` | `.viser` 回放；**向后 seek = 重置场景后从头重放**，复杂度 O(N) | 需要改进：周期性快照（§3.14） |
| `Splatting/` | WASM + SIMD 排序 Worker，按 group 变换排序 | V1.0 3DGS 参考 |
| `CLAUDE.md` + `benchmarks/run_bench.py` | 按需渲染（`frameloop="demand"`，命令式修改后 `requestRender()`）；**Playwright headless Chromium 基准**：rAF 间隔 p50/p95/p99、longtask、CDP `Performance.getMetrics`、`renderer.info`、V8 CPU profile | 流畅性测试 harness 直接移植 |

---

## 3. 可复用算法与实现（伪代码 + 参数）

### 3.1 坐标约定与转换（全链路统一）

| 坐标系 | 轴 | 旋转表示 | 来源 |
|---|---|---|---|
| LingBot 相机 | OpenCV：x 右、y 下、z 前 | XYZW | `utils/pose_enc.py` |
| LingBot 世界 | 大致是首批 scale 帧的相机系，相对尺度，不与重力对齐 | — | 模型 |
| viser 世界 | 默认 Z-up，相机 OpenCV | wxyz | `_scene_api.py` |
| **本项目 World（ENU）** | X 东、Y 北、Z 上，米 | xyzw（与 three 一致） | 设计 |
| three.js | Y-up，相机看向 −Z | Quaternion(x,y,z,w) | three |

```text
# 1) 重建世界 → ENU：Sim(3) (s, R, t)，由 §3.7 地理配准或 §3.8 重力对齐得到
p_enu = s · R · p_recon + t
T_enu_cam = [[s·R·R_c2w, s·R·t_c2w + t]]      # 旋转部分不乘 s（姿态），位置乘 s
# 2) OpenCV 相机 → three.js 相机（用于 FPV / 视锥朝向）
R_three_cam = R_enu_cam · diag(1, −1, −1)
# 3) ENU → three 世界：在根 Group 上设置 rotation.x = −π/2
(x_three, y_three, z_three) = (e, u, −n)
```

### 3.2 推理模式自动选择（Job 参数解析）

```python
def resolve_inference(S, fps, speed_mps=None, gpu_mem_gb=24):
    cfg = dict(num_scale_frames=8, camera_num_iterations=4,
               kv_cache_sliding_window=64 if gpu_mem_gb >= 20 else 32)
    if S <= 320:
        cfg.update(mode="streaming", keyframe_interval=1)
    elif S <= 3000:
        # 与 demo.py 一致：ceil(S/320)，但设上限避免关键帧过稀
        cfg.update(mode="streaming", keyframe_interval=min(ceil(S / 320), 10))
    else:
        # 航拍推荐 flow 关键帧：阈值约 8–16 px（以 518 宽计），max_gap≈fps·3
        cfg.update(mode="windowed", window_size=128, overlap_keyframes=8,
                   keyframe_interval=2, flow_threshold=12.0, max_non_keyframe_gap=int(fps * 3))
    # special 池约束：单个 streaming 会话内关键帧数必须 ≤ max_frame_num + 100
    n_kf = cfg["num_scale_frames"] + (S - 8) // cfg["keyframe_interval"]
    cfg["max_frame_num"] = max(1024, n_kf + 64) if cfg["mode"] == "streaming" else 1024
    return cfg
```

### 3.3 流式会话封装（逐帧产出，实时推进度和预览）

`inference_streaming` 要等全部结束才返回，也不能接直播流。下面基于它内部同样的调用序列，封装成 push 式会话。这依赖私有 API（`clean_kv_cache`、`_set_skip_append`），**必须锁定 commit `849e690`**。

```python
class LingbotSession:
    """push_frame(uint8 HxWx3) → on_frame(FrameResult)；自动开窗、Sim(3) 拼接。"""
    def __init__(self, model, dtype, scale=8, kf=1, window_kf=128, overlap_kf=8, on_frame=None):
        self.buf, self.phase, self.kf_count = [], 1, 0
        self.T_global = Sim3.identity()          # 当前窗口 → 第一个窗口
        self.tail = deque(maxlen=max(scale, overlap_kf * kf))  # 最近若干帧，供下一个窗口做 scale 阶段

    @torch.no_grad()
    def push_frame(self, idx, rgb_u8):
        x = preprocess_crop518(rgb_u8)           # 与 load_fn 一致；CPU 上只保留 uint8 原帧
        self.tail.append((idx, x, rgb_u8))
        if self.phase == 1:
            self.buf.append((idx, x))
            if len(self.buf) == self.scale:
                imgs = stack([b[1] for b in self.buf])[None].cuda()
                out = self.model.forward(imgs, num_frame_for_scale=self.scale,
                                         num_frame_per_block=self.scale, causal_inference=True)
                for j, (fi, _) in enumerate(self.buf):
                    self._emit(fi, slice_frame(out, j), frame_type=0)
                self.phase, self.buf = 2, []
            return
        is_kf = self.kf <= 1 or ((idx - self.first_stream_idx) % self.kf == 0)
        if not is_kf: self.model._set_skip_append(True)
        out = self.model.forward(x[None, None].cuda(), num_frame_for_scale=self.scale,
                                 num_frame_per_block=1, causal_inference=True)
        if not is_kf: self.model._set_skip_append(False)
        self._emit(idx, out, frame_type=1 if is_kf else 2)
        self.kf_count += is_kf
        if self.kf_count >= self.window_kf - self.scale:
            self._roll_window()                  # clean_kv_cache + 用 tail 重跑 scale 阶段 + §3.4 对齐

    def _emit(self, idx, out, frame_type):
        ext, K = pose_encoding_to_extri_intri(out["pose_enc"], (H, W))
        T_c2w = self.T_global @ se3(ext[0, 0])   # LingBot 解码结果就是 C2W
        depth = out["depth"][0, 0, ..., 0].half().cpu()
        conf  = out["depth_conf"][0, 0].cpu()
        self.on_frame(FrameResult(idx, T_c2w, K[0, 0], depth, conf, frame_type))
        # 下游：写 shard（depth f16 + conf u8 量化）→ §3.5 过滤与体素化 → 推预览增量
```

- 显存保持在 O(1) 帧加 KV 池；CPU 只保留 uint8 帧和 tail。
- 结果逐帧落盘，**不在内存里累积 `all_depth`**，这一点和原实现相反。
- 预览推送节流：每 N 帧（或每 250 ms）推一次合并后的点增量，每次最多 20k 点。

### 3.4 窗口间 Sim(3) 对齐（移植 `_pairwise_alignment` / `_warp_predictions`）

```text
输入：prev 窗口尾部 overlap 帧、curr 窗口头部 overlap 帧（同一批物理帧）
1. 锚点 = 重叠区内"两个窗口里都是关键帧"的最后一帧；没有这样的帧就退回重叠区第一帧
2. R_ab = R_a · R_bᵀ                        # 由锚点两份 C2W 旋转求得
3. s_ab = median( depth_a / depth_b )  对所有成对关键帧像素，只用有限值，clamp 到 [1e-3, 1e3]
4. t_ab = c_a − s_ab · R_ab · c_b            # c 为相机中心
5. 对 curr 窗口所有帧：R ← R_ab·R；c ← s_ab·R_ab·c + t_ab；depth ← s_ab·depth
6. 拼接：非末窗口丢弃尾部 overlap 帧（_stitch_windows）
```

这一步只用了一个锚点位姿，存在累积误差。**改进（V0.5）**：对重叠区全部成对帧的相机中心跑 Umeyama（§3.7），或者对两份重叠点云做 small_gicp 精配准。

### 3.5 点过滤、体素去重与"首见帧"（重建回放）

```python
def frame_to_points(depth, conf, rgb, K, T_c2w, sky_nonsky=None, stride=2,
                    conf_min=1.5, conf_pct=30, max_depth_pct=98, jitter=True):
    H, W = depth.shape
    v, u = mgrid[0:H:stride, 0:W:stride]
    if jitter: u = u + (rand_like(u) - .5) * stride * .8; v = v + (rand_like(v) - .5) * stride * .8
    d = bilinear(depth, u, v); c = conf[v.astype(int), u.astype(int)]
    m = (d > 0) & (c > max(conf_min, percentile(c, conf_pct))) & (d < percentile(d, max_depth_pct))
    if sky_nonsky is not None: m &= sky_nonsky[v.astype(int), u.astype(int)] > 0.1
    Xc = stack([(u - K[0,2]) * d / K[0,0], (v - K[1,2]) * d / K[1,1], d], -1)[m]
    return Xc @ T_c2w[:3,:3].T + T_c2w[:3,3], rgb_at(u, v)[m], conf_to_u8(c[m])

# 体素去重（CPU 版 VoxelGridCUDA）：保留"首见"点，记录 first_seen 帧号
def morton21(ix, iy, iz):                    # 21 bit/轴，与 voxel_morton.cu 相同的 magic numbers
    def spread(v):
        v = (v | v << 32) & 0x1f00000000ffff; v = (v | v << 16) & 0x1f0000ff0000ff
        v = (v | v << 8) & 0x100f00f00f00f00f; v = (v | v << 4) & 0x10c30c30c30c30c3
        return (v | v << 2) & 0x1249249249249249
    return spread(ix) | spread(iy) << 1 | spread(iz) << 2
key = morton21(floor(p/vox) + 2**20)         # uint64
order = np.lexsort((frame, key))              # 同一体素内按帧号升序，保留首见
uniq = np.r_[True, key[order][1:] != key[order][:-1]]
keep = order[uniq]; keep = keep[np.argsort(frame[keep], kind="stable")]
ptrs = np.searchsorted(frame[keep], np.arange(S), side="right")  # 第 t 帧可见点 = [0, ptrs[t])
```

**参数建议**：`vox = 0.002–0.003 × 场景对角线`（与离线 preset 同量级；ENU 米制时航拍取 0.05–0.10 m）；`stride=2`；只用关键帧出点（`--keyframes_only_points`）。

### 3.6 Centroid 八叉树与 LOD 选择 → 我们的 Web LOD

**离线构建**（`OctreeSPC._compute_centroids` 的 numpy 移植，CPU 就能跑）：

```python
G = 2**Lmax; q = clip(((p - center)/half + 1) * .5 * G, 0, G-1).astype(int64)
cid = (q[:,0]*G + q[:,1])*G + q[:,2]
cells, inv = np.unique(cid, return_inverse=True)            # 5M 点约 0.5 s
cnt = np.bincount(inv); xyz = stack([np.bincount(inv, p[:,k]) for k in 0..2], 1) / cnt[:,None]
first = np.full(len(cells), 2**30); np.minimum.at(first, inv, frame)
for L in range(Lmax-1, -1, -1):                               # 自底向上加权质心
    coords = coords >> 1; pid = (coords[:,0]*2**L + coords[:,1])*2**L + coords[:,2]
    par, inv = np.unique(pid, return_inverse=True)
    w = cnt; cnt = np.bincount(inv, w); xyz = Σ(xyz·w)/cnt; first = min-reduce(first)
```

**两种 LOD 形态对比：**

| | centroid / replacement（LingBot） | additive / sampling（Potree） |
|---|---|---|
| 内节点内容 | 子 cell 的**加权质心**（均值色，更平滑） | 真实点的子集，子节点只存剩余点 |
| 下载冗余 | 表面类数据约多 33%（各级 4 倍关系的几何级数） | 0 |
| 适合 | 重建回放层（每个 cell 带 `min_frame`，可做时间揭示）；服务端预览抽稀 | **World 点云主渲染**（点预算效率高，与 potree-core/three-loader 兼容） |

**LOD 选择**（`lod_select` 的核心，改写成节点级 SSE 加预算）：

```text
节点 n：包围球半径 r，相机空间深度 z，垂直 FOV θ，视口高 H_px
  projPx(n) = r / (max(z, near) · tan(θ/2)) · H_px/2
  可见 <=> 包围球与视锥相交（z 方向加 r 余量）且 n.minFrame ≤ uRevealFrame（回放模式）
  细分 <=> projPx > minNodePx（推荐 100–150 px；LingBot 按 cell 取 1.5 px）
  priority = projPx · (1 + 0.5·max(0, dot(dir_to_node, view_dir)))    # 视线中心加权
遍历：max-heap(priority)，累加 n.pointCount，超过 pointBudget 就停；
      缺失节点入加载队列（并发 4–6，按 priority 排序），未加载前画父节点（additive 下天然渐进）
```

**FPS 反馈的点预算**（自适应疏密）：

```text
frameMs_ema = 0.9·frameMs_ema + 0.1·frameMs
err = (targetMs − frameMs_ema)/targetMs          # targetMs = 16.7（桌面）/ 33（软件渲染 CI）
if |err| > 0.1 (滞回): budget = clamp(budget·(1 + 0.5·err), 2e5, 8e6)  （每 500 ms 最多调整一次）
pointSizeWorld = n.spacing · sizeFactor         # 预算下降时 sizeFactor ↑ 补洞：sizeFactor = clamp(√(budget0/budget), 1, 2)
```

### 3.7 GNSS/RTK Sim(3) 地理配准（移植 `umeyama` + `umeyama_registration_ransac`）

```python
# 输入：重建相机中心 C_recon[i]（C2W 平移），同步后的 GNSS/RTK ENU 坐标 P_enu[i]
#      （先把 WGS84 转到以 origin 为原点的 ENU）
T, inl = ransac_umeyama(C_recon, P_enu, inlier_threshold=2.0 if gnss else 0.1,  # 米
                        ransac_n=3, iters=1e4)
s, R, t = umeyama(C_recon[inl].T, P_enu[inl].T)      # 用内点再精估一次（utils/geometry.py::umeyama）
quality = {"inlier_ratio": inl.mean(), "rmse_m": rmse(s*R@C+t - P), "scale": s}
# 窗口级：每个 chunk 单独估 (s_k, R_k, t_k) 再做平滑，可吸收 windowed 的尺度漂移（chunk_scales）
```

时间同步：视频帧时间戳要对齐到飞控日志（PX4 ulog 或 P600 日志）。可以用起飞加速度峰值和视觉运动的互相关估计偏移 Δt。

### 3.8 重力方向估计

- `compute_global_up`：用相机 −Y 轴的中位数。**适合前视或斜视；吊舱正下视（pitch≈−90°）时相机 −Y 是水平的，会失效。**
- 航拍的替代方案：①用飞控 IMU/吊舱姿态（首选）；②地面 RANSAC 平面拟合，取 z 方向最低 20% 点的主平面法向；③已有 GNSS 时直接由 §3.7 的 Sim(3) 得到。

### 3.9 本项目 WebSocket 二进制协议（在 viser 混合格式上的定制）

控制和事件消息用 JSON 或 msgpack。高频遥测和点增量用 viser 式混合帧。**遥测帧用固定布局（SoA），不走 msgpack**：

```text
Frame := Header(32B) | Meta(zstd msgpack, 可选) | pad8 | Buffers(8B 对齐)
Header: magic "ANW1"(4) | ver u16 | type u16 | seq u32 | flags u32 | t_sim f64 | meta_raw_len u32 | meta_zlen u32
type=0x10 TELEMETRY（20–50 Hz）：meta = {"ids":[...], "stride":64} （ids 变化时才带）
  Buffers[0] = N×64B：pos f32×3 | quat f32×4 (xyzw) | vel f32×3 | batt u8 | mode u8 | health u8 | pad | ...
type=0x20 RECON_PREVIEW：meta={job_id, frame_range, T_enu_recon}；Buffers = pos(u16×4 量化) | rgba u8×4
type=0x30 ENV_FIELD（低频）：风场网格切片 f16
```

握手用子协议 `anet-v{semver}` 做版本校验。连接后先发**快照**（世界、无人机、任务），再发 `snapshot_done`（对应 viser 的 ReplayDone）。

### 3.10 服务端发送缓冲（`AsyncMessageBuffer` 语义精简版）

```python
class ClientBuffer:
    def push(msg):                       # 同 key 覆盖：drone:{id}:telemetry、node:{id}:pose、env:wind
        with lock: slots[msg.key] = msg; order.move_to_end(msg.key); event.set()
    async def windows():
        while True:
            await event.wait(); await asyncio.sleep(1/60)          # 合批窗口
            batch = [slots.pop(k) for k in list(order)[:128]]      # 每窗最多 128 条
            yield batch
# 慢客户端：latest-wins 自动丢弃过期遥测，不会积压；事件类消息（任务状态、告警）用独立的不可合并队列
```

### 3.11 客户端接收与节拍平滑

- **Worker 解码**：沿用 viser 的 `decodeHybridMessage` 思路，TypedArray 视图零拷贝，`postMessage(batch, [buffer])` 转移所有权。
- **插值缓冲**（比 viser 的"延迟发送"更适合无人机）：保存最近 K=4 个 `(t_sim, pose)` 快照，以 `t_render = t_server_now − 100 ms` 做线性插值和 slerp；落后超过 250 ms 就直接跳到最新（对应 viser 的 >100 ms flush）。
- **瞬时更新**：位姿写进 `mutable.dronePose[id]`（Float32Array 池），`useFrame` 里批量写 `instanceMatrix`。**不进 React/Zustand state**；UI 数字面板用 `throttle(100 ms)` 从 ref 读取。
- **按需渲染**：仿真暂停且相机静止时 `frameloop="demand"`，命令式修改后调用 `invalidate()`，headless CI 上可以显著降低 CPU。

### 3.12 点云 shader 与量化布局

viser 的点尺寸公式（和 `PointsMaterial` 的 sizeAttenuation 等价）：

```glsl
// uniform: uScale = (pointSizeWorld / tan(fovY/2)) * viewportHeightPx * pixelRatio
vec4 mv = modelViewMatrix * vec4(position, 1.0);
gl_Position = projectionMatrix * mv;
gl_PointSize = clamp(uScale / -mv.z, uMinPx, uMaxPx);
// frag：Lp 形状  r = pow(pow(|dx|,p)+pow(|dy|,p), 1/p); if (r>0.5) discard;   p=2 圆
// 回放：if (aFirstSeen > uRevealFrame) discard;（放在 vertex 中把 gl_Position 置到裁剪体外）
```

**节点局部量化布局**（替代 viser 的 float16 世界坐标，同时兼容 WebGPU 顶点格式）：

```text
每点 12 B，交错存储：
  [u16 x, u16 y, u16 z, u16 extra]   → WebGPU "unorm16x4" / WebGL2 normalized Uint16 (itemSize 4 或 3+1)
  [u8 r, u8 g, u8 b, u8 cls|conf]    → "unorm8x4"
解码：pos = nodeMin + q/65535 · nodeSize；直接用 Points.position = nodeMin、Points.scale = nodeSize 实现，不需要自定义反量化 shader
精度：节点边长 / 65535。根节点 8 km 时约 12 cm，深层节点毫米级（远好于 float16 世界坐标的 1–4 m）
extra：first_seen 帧号（回放）或强度；cls/conf：语义类别或置信度（u8）
```

WebGPU **没有** `unorm16x3` 和 `unorm8x3` 格式，所以必须补齐到 4 分量。WebGPU 的 point-list 恒为 1 px，**在 WebGPURenderer 下大点要用实例化 quad**（PointsNodeMaterial/sizeNode，具体 API 以 three.js 研究单元结论为准）。WebGL2 回退路径可以直接用上面的 GLSL。

EDL（`_edl_shade` 的屏幕空间版本）：对 log2 深度取 8 邻域 `max(0, logd − logd_n)` 的均值 R，`shade = exp(−300·strength·R)`，推荐 strength 0.2–0.5、半径 2–3 px。**UrbanScene3D 样本只有 xyz + 法线、没有颜色（本机 PLY 头实测）**，所以 EDL 加上构建期烘焙的高度与法线着色是保证可读性的关键。

### 3.13 GPU buffer 生命周期（移植 `syncBufferGeometry` 规则）

1. 节点首次加载：`new BufferGeometry` + `InterleavedBufferAttribute`，一次上传。
2. 节点卸载：`geometry.dispose()`，并从 LRU 删除。**不要**用 `setAttribute` 替换旧属性（会泄漏 GL buffer）。
3. 流式增量（重建预览）：预分配容量为 2 的幂的 buffer，用 `setDrawRange` 和 `addUpdateRange` 局部上传。容量不够时整体翻倍重建。
4. LRU 显存预算：`Σ nodeBytes ≤ gpuBudget`（默认 512 MB，软件渲染 128 MB）。淘汰顺序为不可见优先，其次最久未用。

### 3.14 录制与回放（改进 `.viser`）

```text
.anetrec := zstd( header{version, duration, world_id, dt_keyframe=5s}
                  | messages[(t, msg)] | keyframes[(t, full_snapshot_msg_index)] | dedup_buffers )
seek(t)：二分找 ≤t 的最近快照 → 应用快照 → 重放 (t_kf, t] 的增量消息
（viser 的做法是"回退 = 从头重放"，O(N)；加快照后变为 O(log N + Δ)）
```

### 3.15 无人机跟随相机（移植 `_follow_camera_at`）

```text
pos̄ = mean(C[i−w/2 : i+w/2]); fwd = normalize(mean(R[:,2]))
fwd_h = fwd − (fwd·up)·up
if |fwd_h| < 0.1:  fwd_h = 速度方向的水平分量（正下视退化时）
eye = pos̄ − fwd_h·scale·back(0.2–0.3) + up·scale·upOff(0.05–0.1)
center = pos̄ + fwd·scale·look(0.4–0.5)
平滑：smooth_window = 30–60 帧；段切换时 smoothstep 过渡 transition = 30–40 帧
```

### 3.16 流畅性测试 harness（移植 `benchmarks/run_bench.py`）

用 Playwright 启动 headless Chromium（本机走 SwiftShader 的 WebGL2），注入 INIT_SCRIPT 记录 rAF 时间戳和 `PerformanceObserver('longtask')`，再用 CDP `Performance.getMetrics` 取 TaskDuration 和 JSHeap，从 `window.__anetTestpoints.rendererInfo` 取 draw calls 和点数。
- **场景脚本**：加载 Shenzhen 5M 点 → 相机沿预设路径飞行 30 s（远 → 近 → 贴地）→ 10 架 mock 无人机、20 Hz 遥测 → 切换天气。
- **判定**：p95 帧间隔 ≤ 50 ms（软件渲染）或 ≤ 20 ms（GPU）；longtask 总时长 < 5%；首帧可交互 < 3 s；点预算收敛时间 < 2 s；GPU 内存平稳（无泄漏）。

---

## 4. 在本项目中的落点与复用方式

### 4.1 复用清单

| 算法或模块 | 来源 | 用途 | 落点模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|---|
| GCTStream 推理 | `lingbot_map/models/*` | 视频 → 位姿、深度、置信度 | `reconstruction/lingbot/worker` | V0.1 | adopt | 核心能力，不值得重写 |
| 流式会话封装 | `gct_stream.py::inference_streaming` | 逐帧产出、在线建图 | `reconstruction/lingbot/session.py` | V0.1（离线）/ V1.0（在线） | port | 原 API 返回前阻塞且全量驻留内存 |
| 窗口 Sim(3) 拼接 | `gct_stream_window.py::_pairwise_alignment` | 长视频 | 同上 | V0.1 | port | 需要接入我们的会话 |
| 天空分割 | `vis/sky_segmentation.py` | 去天空噪声 | `reconstruction/lingbot/filters.py` | V0.1 | adopt（修补 0 填充 bug） | 模型现成 |
| 体素 Morton 去重 + first_seen | `demo_render/.../voxel.py` + `.cu` | 融合、去重、回放指针 | `world/pointcloud/fuse.py`（numpy） | V0.1 | port | CUDA 扩展不可用，改写成 numpy 很简单 |
| Centroid 八叉树 + LOD | `demo_render/.../octree.py` | 回放层和服务端抽稀 | `world/pointcloud/octree_centroid.py` | V0.1–V0.3 | port | 公式可直接照搬 |
| 带 jitter 的反投影 | `unproject.py` | 抗摩尔纹 | `world/pointcloud/unproject.py` | V0.1 | port | 几行代码 |
| Umeyama + RANSAC | `utils/geometry.py::umeyama`、`benchmark/.../registration.py` | GNSS/RTK 配准 | `world/georef/sim3.py` | **V0.1**（GNSS）/ V0.5（RTK） | port | 米制世界的前提 |
| ATE/RPE 评测（evo） | `benchmark/.../trajectory.py` | 重建质量报告 | `reconstruction/qa/` | V0.5 | reference | 质量门禁 |
| follow/birdeye 相机 | `demo_render/rgbd_render/camera.py` | 无人机跟随、俯瞰 | `apps/web/src/camera/` | V0.2 | port（转 TS） | 正下视退化已处理 |
| EDL | `interactive_viewer/server.py::_edl_shade` | 点云深度感 | Web 后处理 pass | V0.1 | port（转 shader） | 无色点云必需 |
| JPEG-over-WS 服务端渲染 | `interactive_viewer/server.py` | 瘦客户端或大屏兜底 | `apps/api`（可选） | V1.0 | reference | 非 MVP |
| 混合二进制帧 | `infra/_infra.py::_message_producer` + 客户端 Worker | 遥测、点增量 | `apps/api/ws` + `apps/web/src/net/wsWorker.ts` | V0.1–V0.2 | port | 零拷贝、主线程不阻塞 |
| latest-wins 合批缓冲 | `_async_message_buffer.py` | 慢客户端不积压 | `apps/api/ws/buffer.py` | V0.2 | port | 语义成熟 |
| 快照 + snapshot_done | ReplayDoneMessage | 晚加入与重连 | 同上 | V0.2 | port | |
| 节拍平滑 / 插值 | `WebsocketClientWorker.ts` | 平滑多机运动 | `apps/web/src/net/` | V0.2 | port（改为插值缓冲） | |
| 可变 ref 位姿 | `MessageHandler.tsx`（nodePoseData） | 高频位姿不触发重渲染 | `apps/web/src/stores/transient.ts` | V0.2 | port | 保证 60 FPS 的关键 |
| 点 shader / 视锥几何 | `ThreeAssets.tsx`、`CameraFrustumVariants.tsx` | 点云、轨迹视锥 | `apps/web/src/layers/*` | V0.1 | port | |
| buffer 复用规则 | `bufferGeometrySync.ts` | 防泄漏 | `apps/web/src/gpu/` | V0.1 | port | |
| 录制格式 | `StateSerializer` | 飞行回放 | `simulation/recording/` | V0.2 | port（加快照） | |
| 基准 harness | `benchmarks/run_bench.py` | 流畅性测试 | `tests/perf/` | V0.1 | port | 用户明确要求 |
| `viser.transforms` | `src/viser/transforms` | 后端 SO3/SE3 | `simulation/common/` | V0.2 | adopt（pip 依赖） | 纯 numpy，API 干净 |
| Recon QA viewer | `benchmark/viewer.py` + viser | 算法同学核对 GT 和预测轨迹 | `tools/recon_qa/` | V0.1 | adopt（内部工具） | 零前端成本；不进产品 UI |
| viser 客户端 UI | Mantine | — | — | — | skip | 与 shadcn 规范冲突 |

### 4.2 Reconstruction Service：视频 → 点云 + 轨迹 → World Package

**API（FastAPI）**

```text
POST   /api/v1/uploads                       分片上传（视频、飞控日志），返回 upload_id
POST   /api/v1/recon/jobs                    提交 JobSpec → {job_id, state:"QUEUED"}
GET    /api/v1/recon/jobs?world_id=&state=   列表
GET    /api/v1/recon/jobs/{id}               状态、阶段、进度、指标
POST   /api/v1/recon/jobs/{id}:cancel        取消（在帧边界协作式停止）
POST   /api/v1/recon/jobs/{id}:retry         {from_stage} 按阶段断点重试
GET    /api/v1/recon/jobs/{id}/artifacts     产物清单（带 URL 和 sha256）
WS     /ws  subscribe "recon/{id}"           事件：state / progress / preview(binary) / log / done
```

**JobSpec**

```json
{
  "world_id": "hefei-campus",
  "input": {"video": "upl_01", "flight_log": "upl_02", "time_offset_s": "auto",
            "camera": {"undistort": {"fx": 1380, "fy": 1380, "cx": 960, "cy": 540, "k1": -0.12}}},
  "sampling": {"fps": 10, "stride": 1, "max_frames": 20000, "rotate_cw90": false},
  "model": {"name": "lingbot-map", "commit": "849e690", "ckpt": "lingbot-map.pt",
            "backend": "flashinfer", "dtype": "bf16", "compile": false},
  "inference": {"mode": "auto", "num_scale_frames": 8, "keyframe_interval": "auto",
                "window_size": 128, "overlap_keyframes": 8, "camera_num_iterations": 4, "flow_threshold": 0},
  "filter": {"mask_sky": true, "conf_min": 1.5, "conf_pct": 30, "pixel_stride": 2,
             "max_depth_pct": 98, "keyframes_only": true, "jitter": true},
  "fusion": {"voxel_m": "auto", "color": "first"},
  "georef": {"mode": "gnss", "ransac_inlier_m": 2.0, "gravity": "imu|plane|none"},
  "tiling": {"format": "anet-octree-v1", "max_points_per_node": 20000, "quant": "u16x4+u8x4"},
  "retain": {"depth_shards": false, "keyframe_thumbs": true}
}
```

**状态机**（每个阶段的输出都幂等，完成后写 `stage/.complete.json`，retry 时跳过已完成阶段）：

```text
QUEUED → PREPARING(探测、抽帧、去畸变) → SEGMENTING(sky mask 批量) → INFERRING(逐帧；窗口 i/n)
→ FUSING(过滤、体素、first_seen) → GEOREFERENCING(Sim3、重力) → TILING(八叉树、量化)
→ PACKAGING(写 World Package、校验) → SUCCEEDED
任一阶段 → FAILED{stage, code: OOM|DECODE|NO_GNSS|POSE_COLLAPSE|...} ；任意时刻 → CANCELLED
```

**Worker 架构**：API 进程只负责入队（MVP 用 SQLite 表队列，生产换 Redis Streams）。`recon-worker` 独立进程，**每块 GPU 一个 worker**，模型常驻（冷启动约 30 s）。心跳每 5 s 一次，超时回收。进度事件 `{stage, frames_done, frames_total, fps, eta_s, gpu_mem_gb, window: [i, n]}` 每 250 ms 节流推送一次。`POSE_COLLAPSE` 检测：相邻帧平移大于中位数的 20 倍，或尺度跳变大于 3 倍，触发自动降级到 windowed 并重跑。ETA 估算：`frames/fps_ema + 模型加载 + compile 预热`。

**产物（扩展 01-design §41）**

```text
worlds/<world_id>/
  coordinate.json      {crs:"EPSG:4326", enu_origin:{lat,lon,h}, T_enu_world:{s,R,t},
                        up_axis:"+Z", units:"m", scale_status:"relative|gnss|rtk|lidar"}
  reconstruction/<job_id>/
    job.json           JobSpec、解析后参数、模型 commit/ckpt 的 sha256、耗时
    cameras.txt        # timestamp fx fy cx cy width height（沿用 BSS intrinsics.txt）
    traj_c2w.txt       # timestamp r00 r01 r02 tx … r22 tz（BSS 格式，ENU 米，已配准）
    trajectory.bin     # Web 用：Float32 [N×(3 pos + 4 quat_xyzw)] + Uint8 frame_type
    chunks.json        窗口 Sim(3) 列表（frame 范围、s、R、t）
    quality.json       conf 直方图、保留率、GNSS 内点率与 RMSE、漂移估计、推理 FPS
    keyframes/*.jpg    256 px 缩略图（视锥贴图）
    shards/            可选：depth f16 PNG/EXR + conf u8
  geometry/pointcloud/
    source/fused.ply   xyz(f32, ENU 相对 origin) + rgb + first_seen(u32) + conf(u8)
    octree/            metadata.json + hierarchy.bin + octree.bin（节点二进制见 §3.12）
```

### 4.3 Mock Reconstruction Engine（本机无 GPU 时的 MVP 替身）

它的输出契约与真实 Worker **完全一致**：同样的事件、同样的产物，只替换 INFERRING 阶段。

```text
输入：UrbanScene3D 点云（ENU 归一化后）+ 程序化航线（lawnmower 或环绕，高度 80–150 m，10 fps）
逐帧：
  1. 由航线插值得到 T_c2w（前视或 −45° 斜视），内参取 518×294 / fov 60°
  2. 视锥裁剪后，按 128×72 低分辨率做 z-buffer splat，得到深度图和可见点索引
  3. 可选：注入漂移，每个窗口加 s∈[0.97, 1.03]、yaw ±0.5°，用来测试 §3.4 和 §3.7 的纠偏链路
  4. 发出 FrameResult → 与真实链路共用 FUSING / TILING / 预览推送
```

用途：前端的"重建进行中"渐进揭示、Job UI、配准 QA、流畅性测试，全部可以在 CPU 上闭环。

---

## 5. 对比与推荐

| 维度 | lingbot-map | viser |
|---|---|---|
| Star / 活跃度 | 17140 stars / 2026-09 活跃 | 2794 stars / 2026-09 活跃 |
| 与本项目契合度 | 极高（设计文档指定的 Visual Reconstruction Engine） | 中高（协议与渲染工程可以直接借鉴；UI 栈不兼容） |
| 本机可运行性 | 否（需要 CUDA GPU；推理和 flashinfer 都不能在 CPU 上跑） | 是（纯 Python + 浏览器） |
| 城市级大场景 | 需要 windowed 模式加 GNSS 配准，户外漂移明显 | 不支持 LOD，float16 世界坐标精度不够 |
| 可移植资产 | 几何算法（体素、八叉树、LOD、Sim3、相机路径） | 线协议、缓冲语义、客户端性能范式、基准 harness |
| 风险 | GPU、显存、私有 API、位姿约定 | 版本锁、Node ≥24、Mantine |

**推荐排序**：
1. **lingbot-map**：必选，作为 Reconstruction Service 的引擎。V0.1 以离线 Job 形式接入，配 Mock 引擎；V1.0 升级为在线会话。
2. **viser**：按模块移植协议和前端工程范式，外加一个内部 QA viewer。**不要**把 viser 前端嵌进产品。

---

## 6. 风险与注意事项

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| 1 | 本机无 GPU；LingBot 需要 CUDA（torch 2.8 + cu128），flashinfer 首次使用要 JIT 编译（需要 CUDA toolkit） | 本地跑不了真实重建 | Mock 引擎（§4.3）；GPU 服务器上的 Worker 用 Docker 固化；无 flashinfer 时用 `--use_sdpa`（2026-06 已修复长序列 bug） |
| 2 | 显存约 16–20 GB（估算） | 小卡 OOM | 按 §2.4 分档；OOM 时自动降 window、改 SDPA、`camera_num_iterations=1` |
| 3 | **相对尺度 + 户外漂移**（KITTI ATE 24.0，VBR 31.2） | 仿真物理失真 | V0.1 就做 GNSS Sim(3)；按窗口分段配准；V0.5 加 RTK + LiDAR ICP |
| 4 | special 页池 `assert` 崩溃（streaming 模式关键帧数 > max_frame_num+100） | 长视频直接失败 | §3.2 自动设 `max_frame_num` 或切 windowed |
| 5 | 训练范围 320 视角；超出后 pose collapse | 轨迹崩坏 | keyframe/flow 策略、windowed、`POSE_COLLAPSE` 检测后自动重跑 |
| 6 | 位姿约定歧义（C2W/W2C、XYZW/wxyz） | 视锥朝向和点云错位 | 产物只存 C2W + xyzw，字段名写明 `T_world_cam`；加单元测试：反投影后与视锥方向一致 |
| 7 | demo 把全部帧以 float32 读进内存；`--offload_to_cpu` 的帮助文本写"默认开"，实际默认是 False | 长视频内存爆掉 | 流式读帧（§3.3）；显式 `output_device=cpu` |
| 8 | sky mask 数量不足时用 0 填充（与日志描述相反）；模型路径相对 CWD，且需要联网从 HF 下载 | 帧点云全被过滤；离线环境失败 | 修补为 1 填充；把 `skyseg.onnx` 固化进镜像 |
| 9 | 主点固定为中心、无畸变；crop 模式会裁掉竖屏视频 | 广角或鱼眼误差 | 输入端先去畸变；竖屏用 `rotate_clockwise_90` 或 pad |
| 10 | 正下视时 `compute_global_up` 失效 | 世界上下颠倒或倾斜 | IMU/吊舱姿态 → 平面拟合 → GNSS Sim3（§3.8） |
| 11 | 动态物体（车、人）产生鬼影点 | 视觉噪声 | 高 conf 百分位；多帧一致性过滤（V0.5） |
| 12 | 私有 API（`_set_skip_append` 等）会随上游变化 | 升级后崩溃 | 锁定 commit；为会话封装写回归测试 |
| 13 | viser 客户端/服务端版本强绑定（子协议）；源码构建要 Node ≥24（本机 Node 22） | 不能直接改它的前端 | 只借鉴协议；QA viewer 用 pip wheel |
| 14 | viser 点云是整包下发 + float16 世界坐标（实测 NYC ULP 1 m、Shanghai 4 m） | 大场景卡顿、抖动 | 八叉树 + HTTP Range 静态分块 + u16 节点局部量化 |
| 15 | WebGPU 不支持 point size；`ShaderMaterial` (GLSL) 在 WebGPURenderer 下不可用 | 点云着色器要写两套 | 用 TSL 节点材质统一；WebGL2 回退走 GLSL；本机 headless 以 WebGL2 为主 |
| 16 | UrbanScene3D 各城市单位和偏移不一致（Chicago 范围约 4×8×0.6 单位，显然不是米；Suzhou 的 z∈[−1032,−346]）；实测六个场景法线主轴都是 Z | 地面高度、比例尺错误 | 入库时做单位与上方向归一化，写进 `coordinate.json`（`units`、`T_enu_world`） |
| 17 | 软件渲染（SwiftShader）性能很低 | 流畅性测试误判 | CI 目标按 33 ms/帧设定，点预算下限 2e5，另建 GPU 基线 |

---

## 7. 对设计文档 01-design.md 的优化建议

1. **§5 LingBot-Map：补一段"输出契约与局限"。** 输出只有相对尺度、没有和重力对齐；户外公里级会漂移（引用 benchmark ATE）；超过 320 帧需要 keyframe 或 windowed；航拍必须 `--mask_sky`；显存按 §2.4 分档。明确它是**离线或准实时 Job 引擎**，在线建图放到 V1.0。
2. **§44 V0.1 范围调整：** 把"GNSS（飞控日志）Sim(3) 配准"从 V0.5 **提前到 V0.1**。P600 本来就有 GPS/RTK 日志，成本很低；不做的话，V0.2 的 GoTo、速度、风速都没有物理意义。V0.5 只保留 RTK 精化和 LiDAR ICP。
3. **§6 融合链路细化为：** `LingBot（相对）→ GNSS/RTK RANSAC-Umeyama Sim(3)（按窗口）→ small_gicp/fast_gicp 视觉<->LiDAR 精配准 → Open3D 体素融合`，并定义 `scale_status` 枚举，方便 UI 标注世界可信度。
4. **§7 World Model：** 新增 `Reconstruction` 子对象（frames、poses、intrinsics、keyframe flags、chunk transforms、quality 指标、模型版本等 provenance）。点属性加 `first_seen_frame`、`confidence`、`class`，用于回放和语义过滤。
5. **§14–15 点云 Web 格式：** "Binary Tile + Octree Index"要具体化。推荐 Potree 2.0 风格（hierarchy.bin + octree.bin，HTTP Range），每点 12 B 节点局部量化（u16x4 + u8x4，兼容 WebGPU 顶点格式），明确写"禁止 float16 世界坐标"（附 UrbanScene3D 实测数据）。3D Tiles 作为导出格式而不是运行时格式。LOD 公式、点预算、FPS 反馈要写进规范（§3.6）。
6. **§12 WebGPU 定位修正：** WebGPU 没有 point size，GLSL ShaderMaterial 不能在 WebGPURenderer 上运行。点云需要实例化 quad 或 TSL；WebGL2 回退是本地和 CI 的主测试路径。
7. **§16 场景结构：** 新增 `ReconstructionLayer`，包含轨迹线（时间渐变）、关键帧视锥（≤500，按距离抽稀，贴缩略图）、`uRevealFrame` 回放滑块、配准残差热力。`DebugLayer` 加 LOD 节点包围盒和预算 HUD。
8. **§36–37 实时通信：** 补充协议规范，包括二进制遥测帧布局（§3.9）、latest-wins 合批（1/60 s、≤128 条）、连接快照加 `snapshot_done`、子协议版本握手、客户端 100 ms 插值缓冲、位姿走可变 ref（不进 React state）、按需渲染。"WebSocket 10–50 Hz"的前提是渲染端插值，否则 10 Hz 会明显顿挫。
9. **§39 Timeline 与回放：** 定义录制格式（仿 `.viser`，每 5 s 一个快照，seek 为 O(log N)），与仿真时钟 `t_sim` 统一。
10. **缺失章节：Reconstruction Service**（本文 §4.2）：API、JobSpec、状态机、阶段幂等与断点重试、GPU Worker 池、进度和预览事件、产物目录、质量门禁（内点率 < 60% 或 RMSE > 5 m 时标红，需要人工确认）。
11. **缺失章节：Mock 与测试策略**：Mock Reconstruction Engine（§4.3）和 headless 流畅性基准（§3.16）要作为 V0.1 的交付物，让前端开发不依赖 GPU 服务器。
12. **§41 World Package：** `coordinate.json` 增加 `T_enu_world`（Sim3）、`units`、`up_axis`、`scale_status`；`reconstruction/` 沿用 BSS 的 `traj_c2w.txt` / `cameras.txt` 文本格式并附 Web 用的二进制版本；每个阶段目录加 `.complete.json`。
13. **§4.1 补充：** 增加"内部算法 QA viewer（viser）"与"产品前端（shadcn + Three.js）"分离的原则，避免把科研调试需求塞进产品 UI。
14. **小修正：** §33 表中 "Reconstruction | LingBot-Map" 应注明"GPU ≥24 GB、CUDA 12.8、FlashInfer（可选）"；§43 MVP 链路里的 "Octree" 前面应插入 "Georeference（GNSS Sim3）"。

---

*附：本机实测数据（`.venv` numpy）。UrbanScene3D 六个城市，每个约 5M 点，PLY 为 xyz+normal 共 24 B/点、无颜色。坐标绝对值最大 / float16 ULP：Chicago 11.1 / 0.0078；San Francisco 370 / 0.25；Shenzhen 1250 / 1.0；New York 1608 / 1.0；Suzhou 2519 / 2.0；Shanghai 7052 / 4.0。法线 |n|>0.9 的主轴比例均以 Z 最高（0.46–0.78）。*
