# R18 研究笔记：4D Gaussian Splatting 三件套（动态世界，可选）

> 研究单元：r18 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §7（World Model / Dynamic Objects）、§8（世界的两种表达）、§18（E(x,y,z,t)）、§39（Timeline）、§41（World Package `visual/gaussian`）、§43（MVP 不做 3DGS）、§50–51
> 仓库快照（shallow clone，只有 1 个 commit）：
>
> | 本地路径 | commit | 日期 | stars |
> |---|---|---|---|
> | `refs/dynamic4d/4d-gaussian-splatting`（fudan-zvg，下称 **Fudan-4DGS**） | `63725f2` "Support prefilter in time dimension." | 2026-01-12 | 1038 |
> | `refs/dynamic4d/Dynamic3DGaussians`（JonathonLuiten，下称 **D3DGS**） | `7dbbd4d` | 2023-12-22 | 2299 |
> | `refs/dynamic4d/4DGaussians`（hustvl，下称 **HexPlane-4DGS**） | `843d5ac` | 2024-10-27 | 3950 |
>
> 交叉核对（只读）：`refs/recon/gsplat` @ `512d366`（2026-09-19）里的 `gsplat/contrib/dynamic/`；`refs/web3d/three.js` @ `110fbbe`（r186）里的 `examples/jsm/objects/GaussianSplat.js`；`refs/web3d/spark` @ `9672638`。
> 本机验证脚本：`/data/projs/anet-drone/.cache/research/r18_slice4d.py`（纯 numpy，无需 GPU）。它做了四件事：比对 4D 旋转在 Python 与 CUDA 两处实现是否一致，用 Monte-Carlo 检验条件分布，计算时间截断阈值，估算各表示的字节预算。凡是估算都标注"估算"，所有路径都相对各仓库根目录。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **Fudan-4DGS** | 原生 4D 高斯（xyzt 联合协方差），时间连续，切片可以写成闭式解。2026 年仍在更新（时间维 prefilter） | **port**：把 4D→3D 切片核移植成 TSL/WGSL 计算前处理，并移植"时间窗不透明度 + 时间预滤波"。训练代码在 GPU Worker 里原样隔离运行 | MVP 只移植时间窗算法（V0.2 回放）；训练与渲染放 V1.0+ | 4/5 |
| **HexPlane-4DGS** | 规范空间 3DGS + HexPlane(6 平面) + MLP 形变场。star 最多，但 2024-10 起冻结 | **reference**：训练改用 gsplat 2026 的 `gsplat.contrib.dynamic`（同一架构，Apache-2.0，现代 torch）。参考它的逐帧导出脚本，以及用 HexPlane 因子化压缩 E(x,y,z,t) 的思路 | V1.0+（训练）；V0.4+（场压缩，可选） | 3/5 |
| **D3DGS** | 固定数量高斯，逐时间步在线优化，带物理先验（局部刚体、等距），同时输出稠密 6-DoF 轨迹 | **reference**：参考固定槽位加逐帧属性数组的回放结构、恒速外推初始化、逐相机曝光补偿、刚体正则。**不跑训练** | 回放结构用于 MVP V0.2；正则项用于 V1.0+ | 2/5 |

**关键结论：**

1. **MVP（V0.1–V0.3）不训练、不渲染 4DGS。** 理由有五条，详见 §4.2：
   - 没有输入数据：UrbanScene3D 是静态 `xyz+normal` 点云，没有图像，没有时间。
   - 本机没有 CUDA。
   - 采集形态不匹配：三个仓库都依赖同步多机位，或者物体尺度的短单目片段。
   - 尺度不匹配：参数写死在物体或房间尺度。
   - 价值不匹配：回放真实飞行只需要"位姿时间线 + 静态世界 + 视频"。
2. **本单元对 MVP 有直接价值的是四个小算法，都不依赖 GPU 训练：**
   - 时间窗不透明度与时间预滤波（§3.2）：用于 LiDAR 扫描和轨迹的时间回放。
   - 固定槽位回放缓冲与恒速外推（§3.3）：用于多机回放和遥测抖动隐藏。
   - OpenCV 内参转投影矩阵（§3.4）：用于 FPV 和相机 FOV 视锥。
   - 逐相机曝光补偿（§3.5）：用于多相机上色。
3. **本单元最重要的发现（已用数值验证）：** Fudan 原生 4D 高斯按时间 t 切片后，**3D 协方差与 t 无关**，中心随 t **线性**移动，不透明度乘以一个时间高斯包络（`r18_slice4d.py` 第 [7] 项，误差 0.0）。
   - 每个 4D 高斯因此等价于"匀速运动、限时出现的 3D 高斯"。
   - Web 端每帧只需更新 center（16 B）和 alpha（4 B），协方差缓冲不变。
   - 这让 Fudan-4DGS 成为三者里**最适合 Web 播放**的表示。它比 star 更多的 HexPlane-4DGS 更适合 Web，因为后者每个高斯每帧要跑约 28–96 K MAC 的 MLP。
4. **"回放真实飞行 + 动态场景"应当分三层递进，4DGS 只是最后一层：**
   - L1（V0.2）：位姿时间线加静态世界。
   - L2（V0.5–V0.6）：检测与跟踪得到的动态目标轨迹，渲染成代理几何，同时写入时变占据。
   - L3（V1.0+）：先做"静态 3DGS + 刚体目标高斯"，最后再上 4DGS 片段。
   - **物理权威始终是 Geometry World(t)**，4DGS 只属于 Visual World。
5. **2026 年要关注的补充（未 clone，只用 GitHub API 查了 star 和 pushed_at）：**
   - `zju3dv/street_gaussians`（1404 stars，2026-09-15 仍有推送）：静态背景加刚体车辆分解，正好对应 L3 前半段。
   - `ziyc/drivestudio`（OmniRe，1263 stars）。
   - `adamraudonis/splats4D`（58 stars，2026-07，MIT）：一种可流式的 4D splat 格式，由静态段和 GOP 关键帧/差分组成，支持 HTTP Range，带 WebGPU/WebGL2 查看器。
   - `yangzf-1023/4C4D`（CVPR 2026，159 stars）：只用 4 台相机的稀疏 4DGS，可对应多机同步采集。
   - 建议在 `02-refs.md` 的 §6 补上这几项。

---

## 1. 仓库概览

| 项 | Fudan-4DGS | D3DGS | HexPlane-4DGS |
|---|---|---|---|
| 论文 | ICLR 2024（另有 arXiv 2412.20720 扩展版） | 3DV 2024 | CVPR 2024 |
| 动态表示 | 4D 椭球：μ∈R⁴，scale∈R⁴，左右两个四元数 `q_l,q_r`（4D 旋转），外加 4D SH（空间 SH × 时间 Fourier） | N 个 3D 高斯，逐帧存 `means3D/rgb/quat`；opacity、scale 全程不变 | 规范空间 3DGS，加 HexPlane 特征（xy,xz,yz,xt,yt,zt 六个平面，多分辨率），再经 MLP 输出 Δxyz/Δscale/Δquat（可选 Δopacity/ΔSH） |
| 时间 | **连续 t**（每张图有自己的 float `timestamp`） | **离散时间步**，所有相机必须同步 | 连续 t，归一化到 [0,1]，按平面时间分辨率（25/150）双线性插值 |
| 训练输入 | 多视图视频（N3V：18–21 机位，经 `scripts/n3v2blender.py`）或单目（D-NeRF 合成数据）。初始点云支持逐点 `time` 属性（`scene/dataset_readers.py:fetchPly`） | 同步多机位（CMU Panoptic，27 个训练机位）、`train_meta.json`（逐时间步、逐相机的 K 和 w2c）、前景/背景分割图、`init_pt_cld.npz`（xyz,rgb,seg） | D-NeRF、HyperNeRF（单目）、N3V、自定义多视图（`multipleviewprogress.sh` + COLMAP） |
| 渲染 | 改过的 CUDA 光栅化器（放在仓库内，**import 时 JIT 编译**），另有 flow/depth/alpha 输出 | `diff-gaussian-rasterization-w-depth`（需另外 clone）；README 称可达 800 FPS | 3DGS 光栅化器，每帧先跑形变 MLP；README 称 82 FPS@800² on RTX 3090（D-NeRF） |
| 产物格式 | 只有 `chkpnt*.pth`（`GaussianModel.capture()` 的 tuple），**没有 PLY 导出，也没有 render/eval 脚本** | `output/<exp>/<seq>/params.npz`：`means3D[T,N,3]`、`rgb_colors[T,N,3]`、`unnorm_rotations[T,N,4]` 逐帧存，其余静态 | `point_cloud.ply`（Inria 3DGS 字段）、`deformation.pth`、`deformation_table.pth`；`export_perframe_3DGS.py` 可导出**逐帧标准 3DGS PLY** |
| 环境 | py3.7 / torch 1.12.1 / CUDA 11.6，另需 `pointops2`（CUDA 扩展） | py3.7 / torch 1.12.1 / CUDA 11.6 / open3d 0.16 | py3.7 / torch 1.13.1+cu116 / mmcv 1.6；子模块**未 clone**（目录是空的） |
| 训练成本 | 30k iter，batch 2–4（源码没有给时长，需要实测） | 10000 + 2000×(T−1) iter。T=150 时为 30.8 万 iter，按 README 自述约 50 it/s，**估算约 1.7 h** | README：D-NeRF 约 8 min，HyperNeRF 约 30 min |
| 许可 | 含 Inria 部分（研究用途，本项目忽略） | MIT + Inria | Apache-2.0 + Inria |

---

## 2. 源码结构与关键模块

### 2.1 Fudan-4DGS（`train.py` 407 行，`scene/gaussian_model.py` 588 行）

- **参数**（`GaussianModel.__init__`）：
  - 空间与时间中心：`_xyz`、`_t`
  - 缩放：`_scaling(3)`、`_scaling_t(1)`
  - 旋转：`_rotation`（左四元数）、`_rotation_r`（右四元数，`rot_4d=True` 时启用）
  - 颜色与不透明度：`_features_dc/_rest`、`_opacity`
  - SH 通道数由 `get_max_sh_channels` 决定：`sh_degree_t>0` 时为 `(deg+1)²·(deg_t+1)`，deg=3、deg_t=2 时为 48；`sh_channels_4d=[1,6,16,33]` 是另一种紧凑方案。
- **4D 旋转** `utils/general_utils.py:build_rotation_4d(l, r)`：
  - `M_l(q_l) @ M_r(q_r)` 之后再 `.flip(1,2)`。
  - CUDA 版 `forward.cu:computeCov3D_conditional` 用 glm 列主序重写，得到 `R = M_r*M_l`、`Σ = (S R)ᵀ(S R)`。
  - **本机验证两者得到同一个 Σ**，1000 组随机样本最大误差 1.1e-13。
- **切片**（CUDA `computeCov3D_conditional`，Python 对应 `get_current_covariance_and_mean_offset`）：
  ```
  Σ = 4x4;  cov11 = Σ[:3,:3]; cov12 = Σ[:3,3]; cov_t = Σ[3,3]
  marginal = exp(-0.5·dt²/(cov_t + prefilter_var))   // dt = timestamp − t
  if marginal ≤ 0.05: cull
  opacity *= marginal
  cov3 = cov11 − cov12·cov12ᵀ / cov_t               // 与 dt 无关
  μ3   = μ_xyz + cov12/cov_t · dt                    // 线性
  ```
  - 时间剔除在视锥剔除**之前**执行（`preprocessCUDA`），这是长序列提速的关键。
  - 2026-01 新增的 `prefilter_var` 相当于时间维的抗锯齿，做法是给时间方差加一个常数。
- **4D 颜色** `computeColorFromSH_4D`：`c = Σ SH_l(dir)·[f₀ + cos(2π·dt/T)·f₁ + cos(4π·dt/T)·f₂]`，其中 T 是 `time_duration`。
- **初始化** `create_from_pcd`：
  - 无时间点云时，t ~ U(−0.1, 1.1)·duration。
  - 时间方差初值为 duration/5，opacity 初值 0.1，空间尺度来自 `distCUDA2` 的 kNN 距离。
- **训练** `train.py:training`：
  - 损失：L1 + SSIM（λ=0.2），可选 `lambda_rigid`（kNN k=20 的速度一致性，D-NeRF 配置里为 1.0）、`lambda_motion`、`lambda_opa_mask`。
  - 背景：`env_map`（可学习的等距柱状图，球半径 **R=60 写死**）。
  - 致密化：沿用 3DGS 的 clone/split，并同时在 4D 空间里采样（`densify_and_split` 在 rot_4d 分支用 `build_rotation_4d`）。
- **配置** `configs/dynerf/*.yaml`：`time_duration: [0,10]`、`num_pts: 300000`、`batch_size: 4`、`env_map_res: 500`、30k iter，致密化在 500–15000 iter 之间。
- **缺陷**：
  - `scene/__init__.py` 在 `load_iteration` 分支调用了 `gaussians.load_ply`，但 `GaussianModel` **没有这个方法**。
  - `densify_grad_t_threshold` 被传入，却没有被使用。
  - `utils/general_utils.knn/fps` 依赖 `pointops2` 的 CUDA 扩展。

### 2.2 D3DGS（总计约 1038 行，全部集中在 `train.py`/`helpers.py`/`external.py`/`visualize.py`）

- **数据结构**：
  - `params`（可学习）：`means3D, rgb_colors, seg_colors, unnorm_rotations, logit_opacities, log_scales, cam_m[50,3], cam_c[50,3]`
  - `variables`（不可学习）：梯度累计、邻居索引等
- **时间推进** `train()`：
  - t=0 时跑 10000 iter，并且**只在这一帧致密化**（`densify` 只在 `is_initial_timestep` 时调用）。
  - 之后每帧跑 2000 iter，用 `initialize_per_timestep` 做**恒速外推初始化**：`new_pts = pts + (pts − prev_pts)`，四元数同理后再归一化。
- **物理先验** `get_loss`，权重为 `rigid 4, rot 4, iso 2, floor 2, bg 20, seg 3, soft_col_cons 0.01`：
  - `rigid`：邻居偏移在上一帧局部坐标系下保持不变。
  - `rot`：邻居的相对旋转一致。
  - `iso`：邻居距离保持不变。
  - `floor`：`fg_pts[:,1]` 不低于 0，写死了 y 轴向上且地面为 0。
  - `bg`：背景点固定不动。
  - 邻居集合来自 t=0 前景点的 `o3d_knn`（k=20），权重 `exp(−2000·d²)`。
- **曝光补偿**：`im = exp(cam_m[c])·im + cam_c[c]`，每台相机一个仿射颜色变换。
- **产物** `save_params`：逐帧键（`means3D/rgb_colors/unnorm_rotations`）用 `np.stack` 堆成 `[T,N,·]`，其余只存一份。
- **播放** `visualize.py`：
  - 帧号 `t = int(elapsed·fps % T)`。
  - 轨迹：每 25 个前景高斯取 1 个，显示最近 `traj_length=15` 帧的折线。
  - `setup_camera` 给出了 OpenCV K 到投影矩阵的写法。

### 2.3 HexPlane-4DGS（约 4.9 K 行）

- **`scene/hexplane.py:HexPlaneField`**：
  - 4D 坐标的 C(4,2)=6 个平面，每个是 `[1, out_dim, reso_a, reso_b]` 参数。
  - 采样时各平面 `grid_sample` 结果**逐元素相乘**，多分辨率之间**拼接**。
  - 时间平面初始化为 1，空间平面初始化为 U(0.1,0.5)。
  - 坐标用 AABB 归一化，AABB 由初始点云的 min/max 设定（`Scene.__init__ → set_aabb`）。
- **`scene/deformation.py:Deformation`**：
  - `feature_out = Linear(feat, W)`，之后是 `(D−1)` 层 `Linear(W,W)`。
  - 5 个头都是 `ReLU→Linear(W,W)→ReLU→Linear(W,k)`，k 分别为 3/3/4/1/48。
  - 形变作用在**激活前**的量上：`pts = xyz + dx`，`scales = s + ds`，`rot = q + dq`（加法，不是四元数乘法）。
  - 注意第 5 行有 `from tkinter import W`（多余的 import，在没有 tk 的镜像里会直接 ImportError）。
- **两阶段训练** `train.py:scene_reconstruction`：
  - coarse 阶段 3000 iter，只训静态 3DGS。
  - fine 阶段 14–20k iter，加上形变网络。
  - 正则项（`scene/regulation.py`）：平面 TV、时间平滑（二阶差分）、时间平面 L1。
- **导出** `export_perframe_3DGS.py`：
  - 调用 `utils/render_utils.get_state_at_time`，写出 Inria 字段的逐帧 PLY。
  - **坑 1**：它遍历的是测试相机，而不是唯一的时间戳。
  - **坑 2**：返回的是未形变的 `pc._opacity`，在 `no_do=False` 的配置（dynerf 和 multipleview 默认都是）下 opacity 会错。
- **组合** `merge_many_4dgs.py`：
  - 把多个 4DGS 拼接成一次光栅化，这是正确的做法，因为可以统一深度排序。
  - 但 `rotate_point_cloud` **只旋转中心**，没有旋转四元数和 SH。
- **2026 延续**：`gsplat/contrib/dynamic/`（NVIDIA，2026，从 Holoscan G-SHARP 手术重建移植而来）提供四个组件：
  - `HexPlaneField`
  - `DeformNetwork`：三个头，零初始化，训练开始时即为恒等映射
  - `DeformationTable`
  - `DynamicStrategy`：用 `state["dynamic_mask"]` 标记哪些高斯是动态的，**默认静态、按需翻转**
  - 示例在 `examples/dynamic_surgical_trainer.py`。
  - 这正是"城市静态、局部动态"需要的机制。

### 2.4 三种 4D 表示对照（本单元核心）

| 维度 | Fudan 原生 4D | D3DGS 逐帧轨迹 | HexPlane 形变场 |
|---|---|---|---|
| 时间建模 | 每个高斯自带时间中心与时间宽度，运动为线性，靠大量短寿命高斯拼出复杂运动 | 每帧独立优化位置和旋转，N 固定 | 连续形变函数 f(x,t) |
| 新物体入场 | 支持（加新的 t 段高斯） | **不支持**（t>0 不致密化） | 部分支持（规范空间里必须已有） |
| 单机航拍（单目、移动、城市尺度） | 理论可行，实际欠约束 | 不可行（需要固定同步机位） | 弱（HyperNeRF 级别，物体尺度） |
| 存储（估算，float32） | 644 B/高斯（SH3+t2）；数量随时长增长 | 静态 28 B + **40 B/高斯/帧** | 3DGS 248 B/高斯 + HexPlane 9–28 MiB + MLP 约 0.1 M 参数 |
| 每帧渲染开销 | 闭式切片，O(1)/高斯，约 30 FLOP | 查表加插值 | 12–18 次双线性采样 + **28–96 K MAC**/高斯 |
| Web 可行性 | **高**：协方差静态，每帧只写 center+alpha | 中：逐帧纹理，带宽随 T 线性增长 | 低：要么烘焙成逐帧，要么在 WGSL 里跑 MLP |
| 副产品 | 逐高斯速度 `cov12/cov_t`，可得光流和动态掩码 | **稠密 6-DoF 轨迹**，可用于跟踪 | 形变幅度 `_deformation_accum`，可得动态掩码 |

---

## 3. 可复用算法与实现（含伪代码和参数）

### 3.1 【V1.0+】原生 4D 切片 → three.js GaussianSplat 计算前处理

先离线导出，每个高斯只算一次：由 `(scale4, q_l, q_r)` 得到 Σ，再得到下列量：

```
cov3    = cov11 − cov12·cov12ᵀ/cov_t         // 6 float，three.js GaussianSplat 直接接受 covariance(6)
vel     = cov12 / cov_t                       // 3 float，m/s（时间单位为秒时）
sigmaT2 = cov_t                               // 1 float
t0, mu3, rgba(DC)                             // 若用 force_sh_3d / sh_degree_t=0，就不需要时间 SH
```

压缩布局（估算 32 B/高斯）：

| 字段 | 编码 | 大小 |
|---|---|---|
| center | u16×3，瓦片内量化 | 6 B |
| t0 | fp16，相对段起点 | 2 B |
| σt | fp16 | 2 B |
| vel | fp16×3 | 6 B |
| cov | fp16×6 | 12 B |
| rgba | RGBA8 | 4 B |

每帧计算前处理（TSL/WGSL 伪代码）：

```wgsl
// static: g4[i] = {mu3, t0, vel, invVarT, rgba}; covariance buffers untouched per frame
@compute @workgroup_size(256)
fn slice4d(@builtin(global_invocation_id) id: vec3u) {
  let i = id.x; if (i >= N) { return; }
  let g = g4[i];
  let dt = uTime - g.t0;
  let m  = exp(-0.5 * dt * dt * g.invVarT);   // invVarT = 1/(σt² + prefilterVar)
  var c  = unpack4x8unorm(g.rgba);
  if (m < 0.05) { c.a = 0.0; }                 // |dt| > 2.4477·σt
  else { c.a *= m; center[i] = vec4f(g.mu3 + g.vel * dt, 1.0); }
  color[i] = pack4x8unorm(c);
}
```

移植到 three.js（r186）的要点：

- `GaussianSplat.js` 内部 storage 是 `.toReadOnly()`（第 671–674 行），需要 **fork 这个约 1000 行的类**，开放 center/color 的写节点。
- 它的排序只在视线方向变化时触发：`_needsSort` 条件为 `dot < 0.9995`，所以高斯中心移动后**必须强制重排**，可以每 2–4 帧一次。
- WebGL 后端走 `_sortCPU()`，因此动态 splat 在 WebGL2 回退下只适合 ≤10 万级规模。
- alpha=0 的高斯仍然参与排序和绘制，所以要配合 §3.6 的时间桶，把活跃集控制在预算内。

### 3.2 【MVP V0.2】时间窗不透明度与时间预滤波：带时间戳点的回放

Fudan 的 `marginal_t` 和 `prefilter_var` 可以直接迁移到回放带时间戳的 LiDAR 扫描点、事件点和轨迹点，好处是**不必重新上传缓冲**：

```ts
// TSL 片元/顶点里，每个点带 aTime（相对 session 起点，float32 秒）
sigmaEff2 = sigma*sigma + pow(0.5 * playbackSpeed / displayFps, 2)   // 时间预滤波：×10 回放时避免闪烁
dt = uTime - aTime
mode "window":      a = exp(-0.5*dt*dt/sigmaEff2); discard if a < 0.05   // 即 |dt| > 2.4477·σ
mode "persistence": discard if dt < 0; a = exp(-dt/tau)                 // 只显示"过去"，形成拖尾
```

参数建议：
- `sigma` 取 0.1 s，大约是 MID-360 一帧 10 Hz 的周期。
- `tau` 取 2–5 s。
- `aTime` 用 float32，以 session 起点为零点，在 2^14 s（约 4.5 h）内 ULP ≤ 0.98 ms。

### 3.3 【MVP V0.2】固定槽位回放缓冲与恒速外推（D3DGS 的结构）

```ts
class ReplayBuffer {        // SoA，N 个槽位固定（槽位 = 无人机 / 跟踪目标）
  t: Float64Array;          // [T]
  pos: Float32Array;        // [T*N*3]  ENU（瓦片局部）
  quat: Float32Array;       // [T*N*4]
  sample(time, out) {       // 二分查找 k，使 t[k] ≤ time < t[k+1]
    a = (time - t[k]) / (t[k+1] - t[k]);
    pos = lerp(p_k, p_k1, a);
    q1 = dot(q_k, q_k1) < 0 ? -q_k1 : q_k1;   // 四元数符号修正，D3DGS 没做但必须做
    quat = normalize(lerp(q_k, q1, a));        // nlerp
  }
}
// 实时 WebSocket 丢包/迟到时用恒速外推（即 D3DGS 的 initialize_per_timestep）：
//   p̂(t) = p_last + v_last·min(t − t_last, 0.2 s)；超过 0.2 s 冻结并标记 stale
// 尾迹：每槽位一个长度 L（15–120）的环形缓冲 → 单个 LineSegments（DynamicDrawUsage），颜色按槽位
```

### 3.4 【MVP V0.2】OpenCV 内参 → three.js 投影（FPV 与相机 FOV 视锥）

D3DGS 的 `helpers.setup_camera` 是 OpenCV 坐标系、z 映射到 [0,1] 的写法。在 three.js 里**不要手写矩阵**，改用离轴视锥，这样 WebGL 和 WebGPU 两种深度范围都能正确处理：

```ts
const n = near;
cam.projectionMatrix.makePerspective(
  -cx*n/fx, (w-cx)*n/fx,  cy*n/fy, -(h-cy)*n/fy,  n, far, renderer.coordinateSystem);
cam.projectionMatrixInverse.copy(cam.projectionMatrix).invert();
// 位姿：T_world_cam(GL) = T_world_cam(CV) · diag(1,−1,−1,1)
```

### 3.5 【V0.5】逐相机曝光补偿

`c' = exp(m_c) ⊙ c + b_c`，每台相机 6 个参数，在上色或融合时一起优化，或者用直方图配准估计。适用场景：多架 P600 相机对同一片点云上色时，曝光和白平衡不一致。

### 3.6 【V1.0+】时空流式：时间桶 + 空间瓦片

这个做法把点云八叉树的流式思想推广到时间轴，参照 Layered 4D-Rotor GS（CVPR 2026）和 splats4D 的 GOP 布局。

```
离线：
  life_i = [t0_i − 2.45σt_i, t0_i + 2.45σt_i]
  layer_i = clamp(ceil(log2(len(life_i)/D0)), 0, Lmax)      // D0 = 1 s
  life_i 横跨整个 session 的高斯 → static 段（只存一份，即 splats4D 的 STATIC section）
  其余高斯放进 layer 对应的桶：bucket = floor(t0/(D0·2^layer))
  产物：tile × layer × bucket 的二进制块，加一个 manifest 索引
运行时（与点云 LOD 调度器共用一个优先级队列）：
  需要 = 覆盖 [t − D0, t + D0·(1 + 2·playbackSpeed)] 的桶
  优先级 = w_t·|Δt| + w_s·SSE_tile
  活跃高斯数超过预算时，先砍远处瓦片的高 layer 桶
```

D3DGS 或 HexPlane 的逐帧产物走 splats4D 风格：先一个静态段，之后每 30 帧一个 GOP，GOP 里是关键帧加整数差分。误差用上界来约束，比如位置 ±2 mm、颜色 ±4/255。

### 3.7 【V0.4+ 可选】HexPlane 因子化压缩环境场 E(x,y,z,t)

CFD 或风场时间序列很平滑，可以用 6 个平面加多分辨率来因子化存储。

- 查询：`f(x,y,z,t) = ⊕_scales Π_{6 planes} bilinear(P_ab, (a,b))`，输出 16–32 维特征，再接一个线性解码得到 `(u,v,w)`。物理量需要可解释，所以不要用 MLP 解码。
- 参数量：按 dynerf 配置 `[64,64,64,150]×16ch×[1,2]` 估算约 237 万个参数，合 9 MiB（估算）。
- 浏览器端只需要 2D 纹理，每次查询 12 次采样。
- 注意：这只是一个压缩选项。V0.4 阶段首选仍然是"3D 纹理 × 时间关键帧，三线性插值 + 时间线性插值"，因为简单、可解释，服务端和客户端也容易保持一致。

---

## 4. 在本项目中的落点与复用方式

### 4.1 复用清单

| 项 | 来源 | 目标模块 | 版本 | 方式 |
|---|---|---|---|---|
| 时间窗不透明度与时间预滤波 | Fudan `computeCov3D_conditional` / `prefilter_var` | `apps/web/src/world/materials/TemporalWindowNode`（TSL） | V0.2（MVP） | port |
| 固定槽位回放缓冲、恒速外推、尾迹 | D3DGS `save_params` / `initialize_per_timestep` / `visualize.calculate_trajectories` | `apps/web/src/replay/ReplayBuffer.ts`、`DroneTrail.ts` | V0.2（MVP） | port |
| 内参 → 投影 | D3DGS `helpers.setup_camera` | `apps/web/src/world/camera/intrinsics.ts` | V0.2（MVP） | port |
| 逐相机曝光补偿 | D3DGS `cam_m/cam_c` | `reconstruction/fusion/colorize` | V0.5 | port |
| 4D 切片计算前处理 | Fudan `forward.cu` | `apps/web/src/world/visual/dynamic/Gaussian4DLayer`（fork three.js GaussianSplat） | V1.0+ | port |
| 4DGS 训练 | Fudan `train.py`（容器隔离），或 `gsplat.contrib.dynamic` | `reconstruction/gs4d/` GPU Worker | V1.0+ | adopt（gsplat）/ 原样运行（Fudan） |
| 逐帧导出与 PLY 契约 | HexPlane `export_perframe_3DGS.py` | `reconstruction/gs4d/export` | V1.0+ | reference（需修正 opacity 和时间戳两个 bug） |
| 刚体、等距、旋转正则 | D3DGS `get_loss` | `reconstruction/gs4d/losses` | V1.0+ | reference |
| HexPlane 场压缩 | HexPlane `hexplane.py` / gsplat `HexPlaneField` | `environment/field/codec` | V0.4+ 可选 | reference |
| 时空流式格式 | splats4D、Layered 4D-Rotor（不在 refs 中） | `world/package/visual/dynamic` | V1.0+ | reference |

### 4.2 MVP 不做的理由（明确写入 PRD 的非目标）

1. **没有数据。** UrbanScene3D 的 6 个城市都是 `x y z nx ny nz` 静态点云（见 r03 的实测），没有图像、没有位姿、也没有时间。
2. **没有算力。** 三个仓库都只能在 CUDA 上跑：
   - Fudan 在 import 时就 JIT 编译光栅化器（`gaussian_renderer/diff_gaussian_rasterization.py` 里的 `load(...)`，带 `-g`），还需要 `pointops2`。
   - 本机没有 GPU，headless Chromium 也没有 WebGPU。three.js `GaussianSplat` 在 `forceWebGL` 下走 CPU 排序，连静态 3DGS 的 CI 性能测试都不具代表性。
3. **采集形态不匹配。** 旗舰结果都来自固定同步机位（N3V 18–21 台、Panoptic 27 台），或者小于 10 s 的物体尺度单目片段。单架无人机飞越城市是单目、相机在动、场景是城市尺度，对动态内容几乎无约束。D3DGS 在 t>0 不致密化，而无人机持续看到新区域，与这个假设直接冲突。
4. **尺度参数写死。** 以下都是物体或房间尺度：
   - Fudan 的 `env_map` 球半径 R=60，并且 `assert(delta>0)`。
   - HexPlane 的 `bounds=1.6` 和 64³ 网格。瓦片 512 m 时，multires [1,2] 的最细网格是 4 m/格，无法分辨行人。
   - D3DGS 的 `near=1,far=100`，以及写死 y 轴向上的 floor loss。
5. **价值不匹配。** 回放真实飞行的核心价值是：
   - 位姿与时间线
   - 事件
   - 视频投影
   - 在静态世界里复盘
   动态外观（车、人的真实像素）对仿真和规划**没有物理价值**。物理需要的是时变占据，用检测和跟踪来做更便宜，也更可靠。
6. **工具链冲突。** py3.7、torch 1.12/1.13、cu116、mmcv 1.6，与项目的 py3.12 venv 以及 gsplat（torch ≥2.7）互不兼容。

### 4.3 V1.0+ 接入点（现在就要在接口里预留）

- **时间基准**：所有数据带同一个 `worldTime`（GPS/UTC，单位 ns，存为 int64），另有 `sessionTime`（float64 秒）和 `playbackTime`。相机曝光时间戳和 LiDAR 逐点时间都要求可追溯。
- **World Package** 在 §41 基础上增加以下目录：
  ```
  sessions/<flight-id>/  telemetry.mcap | video/ | lidar/ (逐点 t) | events.json | sync.json (时钟偏移、曝光延迟)
  dynamic/tracks/<flight-id>.parquet     // L2：id, class, t, pose/bbox, source, conf
  visual/dynamic/<flight-id>/manifest.json + static.spz + <tile>/<layer>/<bucket>.bin   // L3
  ```
- **重建流水线**：在静态 3DGS 瓦片（r03）之后加一个 `gs4d` 作业。
  - 输入：图像，每张带连续时间戳和瓦片局部 ENU 位姿；带时间的 LiDAR 点（Fudan 的 `fetchPly` 原生支持 `time` 顶点属性）；动态掩码。
  - 默认用 `gsplat.contrib.dynamic` 加 `dynamic_mask`，只让掩码内的高斯动态化。
  - 需要高保真短片段时，改用 Fudan 原生 4D。
- **Web 端**：在 `WorldLayer/Visual` 下新建 `DynamicVisualLayer`，由同一个 `Timeline` 时钟驱动，按以下顺序降级：
  - L3 4D 切片（仅 WebGPU）
  - L3 刚体目标 splat（按目标变换）
  - L2 代理几何（InstancedMesh，两种后端都能跑）
- **Geometry World(t)**：L2 轨迹写成时变占据，供避障和规划查询 `occupancy(x,y,z,t)`。4DGS **永远不写入** Geometry World。

---

## 5. 对比与推荐

| 排名 | 仓库 | 综合理由 |
|---|---|---|
| 1 | **Fudan-4DGS** | 唯一一个 2026 年仍在更新的仓库（1-12 提交了时间 prefilter）。时间连续，所以能接受不同步、多架无人机的图像。切片是闭式解，Web 友好，还自带速度场。短板：存储大（644 B/高斯），没有导出和渲染脚本，环境老旧 |
| 2 | **HexPlane-4DGS** | star 最高，存储紧凑，训练快（8–30 min），有逐帧 3DGS 导出。但仓库已冻结，同一架构在 **gsplat.contrib.dynamic（2026）** 里延续，所以训练应当走 gsplat。它的 MLP 形变不适合在 Web 端实时运行 |
| 3 | **D3DGS** | 代码最干净，物理先验和轨迹输出最有启发性，回放结构可以直接借鉴。但必须有同步多机位和分割图，t>0 不能新增高斯，自 2023-12 以来没有更新 |

组合建议：
- **训练**用 gsplat 的动态模块（与 r03 选定的 gsplat 同栈）。
- **原生 4D 的高保真片段**用 Fudan。
- **Web 播放格式**统一为"静态段 + 时间桶或 GOP"，4D 切片与逐帧烘焙两种产物都能装进去。

---

## 6. 风险与注意事项

1. **GPU 与构建**：
   - 三者都要 CUDA。Fudan 首次 import 要 JIT 编译（带 `-g`，很慢），另需 `pointops2`。
   - HexPlane 的子模块在本地 clone 里是空的，还要从 gitlab.inria.fr 拉取。
   - D3DGS 的光栅化器在另一个仓库。
   - 统一用容器隔离，每个作业一个镜像。
2. **旧环境**：py3.7、torch 1.12/1.13、cu116 在新显卡（sm_90 及以上）上可能根本编不过。优先迁移到 gsplat。
3. **已知 bug**：
   - Fudan：`Scene` 的 `load_ply` 缺失。
   - HexPlane：`from tkinter import W`；`export_perframe_3DGS` 用的是未形变的 opacity，且按相机而不是按时间戳遍历；`merge_many_4dgs` 不旋转四元数和 SH。
   - D3DGS：floor loss 写死了坐标轴。
4. **坐标与尺度**：
   - 必须用瓦片局部 ENU，瓦片 ≤512 m（同 r03）。
   - 时间也要做局部化：每个段的起点作为零点，fp16 的 t0 才够用；段长 ≤60 s 时，fp16 精度约 30 ms，**段长要与编码精度联动**。
5. **时间同步**：多机或多相机的曝光时间戳误差应小于 1/(2·fps)。P600 的吊舱视频需要标定时延，快速运动下还要处理卷帘快门和运动模糊。
6. **带宽与存储（估算）**：
   - D3DGS 结构下 N=20 万、T=150 时，原始数据 1.1 GiB，量化后 286 MiB。T=3000（100 s@30fps）时量化后 5.6 GiB。必须同时做仅前景、GOP 差分和误差上界量化。
   - Fudan 的高斯数随时长线性增长，必须按时间分桶。
7. **Web 排序**：
   - three.js `GaussianSplat` 按对象独立排序，只在视线变化时重排，WebGL 后端在 CPU 上排序。源码里没看到跨对象合并排序的实现，所以静态世界和多个动态对象重叠时透明顺序会出错。
   - Spark 的 `SplatAccumulator` 能全局排序，它还有 `objectModifier`、`time` dyno 和 `SplatSkinning`，天然适合动态高斯。但它只支持 `WebGLRenderer`，与主栈冲突，见 r03。
   - 这是 V1.0+ 选型时要重新评估的点。
8. **物理边界**：4DGS 的浮点噪点和时间伪影不能进入碰撞和规划；它的速度场只能作为"动态掩码候选"，需要检测和跟踪来确认。

---

## 7. 对设计文档的优化建议

1. **§7 Dynamic Objects** 要把两类东西分开：
   - **受控智能体**（Drone/UGV）
   - **观测到的动态**（真实数据里的车和人）
   第二类新增 `Track` schema（`id, class, t[], pose/bbox[], source, confidence`），并以时变占据的形式进入 Geometry World(t)。
2. **§8 两种表达要加上时间轴。** 改为 `Geometry World(t)`（权威）和 `Visual World(t)`。Visual 再分静态层（点云、Mesh、3DGS）和动态层（L2 代理几何、L3 刚体 splat、L3 4DGS 片段），并写明 "4DGS 只是外观，不是物理"。
3. **§41 World Package 缺少"会话/录制"概念。** 回放真实飞行需要 `sessions/<flight-id>/`（MCAP 遥测、视频、带逐点时间的 LiDAR、事件、`sync.json`）。`visual/gaussian/` 拆成 `static/`（瓦片）和 `dynamic/`（manifest + 时间桶）。
4. **新增"统一时钟"一节。** 区分 worldTime、simTime、sessionTime、playbackTime，规定每个数据源的时间戳语义、偏移标定和插值规则。E(x,y,z,t)、回放、4DGS 都依赖这一节。
5. **§39 Timeline 补充播放语义**：
   - 时间连续，播放时对样本插值
   - 倍速时加宽时间窗（时间预滤波，§3.2）
   - 段预取策略与空间 LOD 共用一个调度器
   - 带时间戳的点用时间窗渲染
6. **§43/§44 MVP 范围**：
   - 在非目标里明确写上"4DGS 或任何动态外观重建"。
   - 把"真实飞行回放 lite"（位姿 + 视频投影 + 事件 + 尾迹）提前到 V0.2，它只依赖 §3.2–3.4。
7. **§49 V0.6 多机**：增加"多机同步采集"这一能力，包括 GNSS PPS 或 PTP 授时和曝光时间戳记录。它为 V1.x 的稀疏多视角动态重建（可参考 4C4D 这类方法）准备数据。
8. **路线图新增 V1.x "Dynamic Visual World"**：
   - V1.1：L2 轨迹 → 代理几何与时变占据
   - V1.2：静态 3DGS + 刚体目标高斯（参考 street_gaussians / OmniRe）
   - V1.3：4DGS 片段与时空流式
9. **§18 与 §51 的措辞**："4D Physical Field" 和 "4D World Runtime" 里的 4D 指时变世界状态，与 4DGS 无关，建议加注释避免混淆。
10. **§18 环境场存储**：写明三档：解析式（Level 0–1）、3D 网格 × 时间关键帧（Level 2–3，首选）、因子化平面（HexPlane/K-Planes，长时序压缩选项）。同时规定服务端物理与前端可视化**从同一份数据采样**，保证一致。
