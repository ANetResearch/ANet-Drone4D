# G2 补充深挖：PointCloudEngine 的实现形态与参数表——选择器、GPU 组织、点径、控制器、软件档预算的本机实测定案，以及 60 s 飞行基线与分档验收阈值

> 研究单元：G2（00-index §9 缺口 G2 的补充深挖）｜日期：2026-09-28｜关联单元：n01、r12、r11、r13、n05、x01
>
> 本文回答 00-index §8 冲突 C2、C14、C18 与 §9 G2 留下的五个问题，全部以本机实测或基于实测的闭环仿真为依据：
>
> 1. **选择器**：voxelkloud 两级预算 best-first（n01）还是 Potree 式“目标集 + 分段前缀”（r12）？
> 2. **GPU 组织**：逐节点 `Points`（12 B/点）、单缓冲 24 B/点 + liveness 纹理（voxelkloud）、还是页池 + DrawTable（r13）？
> 3. **点径**：octree cut 纹理（每顶点最多 20 次 `texelFetch`）还是 Lite 掩码，在 SwiftShader 顶点瓶颈下各花多少？
> 4. **控制器**：画质阶梯（voxelkloud `QualityController`）与 n05 `AdaptiveBudget` 叠加后会不会振荡？
> 5. **软件档预算**：40k（r12）、60k（r11）、150k（n01）、250k（r09）到底取多少？
>
> 另外给出一个**固定的 60 s 飞行脚本**作为所有性能测试的基线，并给出**本机（Tier S）与真 GPU（Tier B/A）分开的验收阈值**。

---

## 0. 结论速览

| 问题 | 候选 | 定案 | 关键证据（本机实测 / 闭环仿真） |
|---|---|---|---|
| 选择器 | A：voxelkloud 两级预算；AP：A + 前缀；APH：AP + ±10% 迟滞；B：r12 目标集 + 分段前缀 | **APH**：voxelkloud `selectVisible` 原样移植，另加两处修改：①**第一个被预算拒绝的 required 节点画前缀**（`cnt = B − pts`，下限 512 点）；②对“上一帧已绘制”的节点把投影误差放大 1.1 倍，新节点缩小 0.9 倍（迟滞）。屏幕中心加权与焦点加权**只用于下载排序**，不进入选择键 | 3 城 × 6 档预算 × 60 s 飞行（30 Hz，含流式模型）：预算受限区 APH 的填充率 0.99–1.00，A 为 0.93–0.99，B 在流式时只有 0.71–0.98；τ 受限区 APH 与 A 的空洞率 0.9–1.2%，B 为 1.3–3.4%（B 少画 30–40% 的点）；帧间节点变化率 APH 比 AP 低 5–20%，下载量少 3–11%；选择耗时 p95 ≤ 0.17 ms（3M 预算） |
| GPU 组织 | O1 逐节点 VBO + draw；O2 单缓冲 24 B/点 + liveness；O3m 单 Q16 缓冲 + `WEBGL_multi_draw`；O3d 页池纹理 + DrawTable 二分（r13） | **O3d：PointPool（RGBA32UI 纹理，16 B/点）+ DrawTable 二分 + 无属性 `THREE.Points`**，作为 Tier B/S 的唯一路径，着色器只写一份 TSL（G1 已验证三种后端等速且逐像素一致）；Tier A 用同一布局的 storage buffer，可压到 12 B/点（与 voxelkloud compute 的 compact 分派同构）。O3m 作为可选优化保留（需要借用 three 的 `isBatchedMesh` 私有路径）。O1 只做 dev 对照页。**O2 不采用** | 逐帧交错配对测量（每帧每方案绘制 5 次取最小值，16 帧 × 6 档预算）：O1、O3m、O3d 的平均耗时比为 1.02 / 1.00 / 1.04，差异在噪声内（每点 0.5–1.3 µs）；O2 在常驻上限为 1×、1.5×、3× 预算时分别慢 18%、22%、50%，原因是它的顶点数等于高水位（为绘制点数的 1.3–2.4 倍），被剔除的顶点仍要花约 0.4–0.6 µs |
| 点径 | N：节点层级；L：Lite 子节点掩码；C：cut 纹理遍历（带早退）；CF：cut 遍历（voxelkloud 原版，无早退） | **L（Lite）用于全部档位**。在“缩小最多 1 级”（voxelkloud 实测的最优值）这一前提下，cut 遍历与 Lite 在**数学上等价**，实测三项画质指标在所有配置下完全一致（4 位小数），cut 纹理没有必要。另加**自适应 `maxPx`**：受预算约束时取 16（设备像素），受 τ 约束时取 8 | 配对测量耗时比：L 0.99、C 1.08、CF 1.03（相对 N）。本数据树深只有 5 层，早退版每顶点最多 7 次 `texelFetch`，达不到“20 次”。画质：12 个采样帧对照全量参考渲染，L 与 C 的空洞率、溢出率、亮度误差完全相同；在 τ 受限区（750k–3M），L 把亮度误差从 N 的约 1.4 降到 0.03–0.75，溢出率从 0.22% 降到 ≤ 0.04%，且没有额外开销。`maxPx` 从 8 调到 16，1280×720 下空洞率 40k 时 64%→37%，100k 时 20%→4.5%，150k 时 7.5%→0.4%，代价是耗时 +4–38%（平均 +21%） |
| 控制器 | QL：voxelkloud 画质阶梯；AB：n05 AIMD；QL+AB：两者直接叠加；CAS：级联（本文提出） | **CAS：内环在当前档位的预算带 `[lo, hi]` 内做对数域 AIMD（250 ms 评估一次）；外环只在内环饱和时换档（下限饱和且超载 1 s 才降档；上限饱和且有余量 3–5 s 才升档，升档后 10 s 内又降档则等待时间翻倍，最长 120 s）；换档时 B 无扰切换**。Tier S 的目标帧间隔固定为 33.3 ms，不用刷新率估计 | 离线闭环仿真（真实选择结果 + 四类设备的帧耗时模型 + vsync 量化 + 噪声与尖峰 + 第 30–40 s 负载翻倍扰动；2 城 × 3 个种子）：QL+AB 每分钟 B 反向 23–46 次、在 iGPU 上 60 s 内换档 5–6 次；QL 单独用时在 iGPU 上换档 6–8 次，其中 1–2 次是 10 s 内来回；CAS 换档 ≤ 2 次，10 s 内来回为 0（只有“带 GPU 计时”的变体在 iGPU 深圳场景平均出现 1 次），B 反向 1–8 次/分钟，超过 1.5T 的帧比例不高于 QL。在 SwiftShader 上 QL 会被刷新率估计卡死在 0 档不动（20 fps，p95 94 ms），CAS 为 29.7 fps、p95 38.9 ms。**本机实时验证**（真实 rAF，负载 3.4–12.8，深圳与纽约）：CAS 的 p95 都是 50 ms，超过 50 ms 的帧占 2.3–3.9%，没有换档，B 每分钟反向 6–13 次；QL 为 p95 83 ms、16% 帧超过 50 ms；AB 与 QL+AB 每分钟反向 29–36 次 |
| 软件档预算 | 40k / 60k / 150k / 250k | **Tier S 固定用 0.5 渲染比例；soft-min 档带宽 [10k, 40k]，起步 25k；soft 档 [40k, 150k]，渲染比例 0.6，只有安静主机才能升到**。250k 否决 | 本机每点 0.5–1.3 µs（随其他进程负载波动 2 倍），30 fps 能承受 25k–60k 点。0.5 渲染比例下的画质：25k、`maxPx` 16 时空洞率 20%、约 32 ms；40k、`maxPx` 8 时空洞率 38%、约 46 ms，所以**少画点、画大点**更好。250k 约 3–4 fps |
| 基线脚本 | — | `flight60`：6 段、60 s、60 Hz 采样，深圳、纽约、上海三城各一份（§2） | `.cache/research/g02/prep.py::flight()` 生成，文件为 `data/<city>/flight.{bin,json}` |
| 验收阈值 | — | Tier S（本机）与 Tier B/A（真 GPU）分开，见 §8 | Tier S 按实时闭环实测给出；Tier B/A 按仿真和上游公布的数据给出设计阈值，要求在真 GPU runner 上复测 |

**对 00-index 的直接修订**：
- C14（软件档预算）按上表关闭。
- C18（单缓冲还是逐节点）改判为页池加 DrawTable，不再“V0.1 先逐节点”。
- §3.5 的点径条目“默认 cut、软件档 Lite”改为“全部 Lite”。
- §3.5 的“档内由 AdaptiveBudget 连续微调”改为 CAS。

---

## 1. 方法与环境

### 1.1 本机与负载

| 项 | 值 |
|---|---|
| CPU | Intel Xeon E5-2603 v4 @ 1.70 GHz，8 个 vCPU（8 socket × 1 core），无 GPU |
| 浏览器 | Chrome for Testing 151.0.7922.34（Playwright rev 1234），`--use-angle=swiftshader --enable-unsafe-swiftshader --ignore-gpu-blocklist` |
| WebGL2 | `ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)))`；`WEBGL_multi_draw` 可用；`MAX_TEXTURE_SIZE` 8192；页面开启 COOP/COEP（`crossOriginIsolated=true`，计时精度 5 µs） |
| 负载 | **测试期间其他研究单元在同一台机器上并发运行 SwiftShader 基准**，1 分钟 load average 在 3–17 之间（配对与画质测量时为 10–17，实时闭环时为 3.4–12.8）。同一配置在不同时刻的绝对耗时相差可达 2 倍（例如 25k 点：13 ms 到 38 ms） |

**应对方法**：
- 方案之间的比较**全部使用逐帧交错的配对测量**：同一帧内依次绘制各方案，每个方案 5 次，取最小值（干扰只会增加耗时，最小值最接近真实开销），奇偶帧反转顺序，最后取各帧比值的中位数。
- 绝对耗时只以区间形式给出，并记录当时的负载。

### 1.2 数据与八叉树

- 深圳、纽约、上海三城，使用 x01 规范化后的 ENU 点云（`.cache/research/x01/enu_<city>.npy`、`nrm_<city>.npy`、`hag_<city>.npy`），每城约 500 万点。
- 用 r09 的 `build_fast` 建树，参数 G=64、LEAF=20000（与 00-index C16 一致），节点内用固定种子打散点序。深圳 696 个节点、深度 5，纽约 852 个、深度 5，上海 676 个、深度 5。
- 点记录为 **12 B/点**，与 ANET_Q16 同构：u16×3 为节点局部 unorm 坐标，u16 为 oct 法线，u8×4 为 HAG、HAG、HAG、class。
- 输出 `.cache/research/g02/data/<city>/{nodes.json, points.q16}`。

### 1.3 工具（全部在 `/data/projs/anet-drone/.cache/research/g02/`）

| 文件 | 作用 |
|---|---|
| `prep.py` | 建树、Q16 编码、生成 60 s 飞行脚本 |
| `lod.mjs` | 被测选择器 `selA`（A/AP/APH，参数 `prefix`、`hyst`）、`selB`（r12 §4.4）、`buildCut`（voxelkloud `cut.ts` 同构）、相机与视锥工具。Node 与浏览器共用 |
| `sim.mjs` | 离线仿真：选择器加流式模型（首节点并发宽度 1、之后 4；延迟 20 ms + 30 MB/s；离开视锥 2 帧取消；每帧上传 20k 或 500k 点；驱逐阈值 1.5B→1.15B）。每 0.5 s 用 4 px 瓦片覆盖率对比全量点云计算空洞率 |
| `bench.html` / `bench.mjs` | 原生 WebGL2 基准：四种 GPU 组织（O1/O2/O3m/O3d）× 四种点径（N/L/C/CF）。带全帧回读的画质指标：以全部节点 + cut 点径 + `minPx` 1 的参考渲染为基准，统计 `hole_px`（参考有、测试无）、`bleed_px`（参考无、测试有）、`lumErr`（共同覆盖像素的平均亮度差） |
| `pair.html` / `pair.mjs` / `run_pair.cjs` | 逐帧交错的配对测量 |
| `ctrl.mjs` / `ctrlsim.mjs` | 控制器实现（`QualityController` 为 voxelkloud 逻辑原样移植，`AdaptiveBudget` 为 n05 原样，`CascadeController` 为本文方案）与闭环仿真 |
| `live.html` / `live.mjs` / `run_live.cjs` | 本机实时闭环：按真实 rAF 呈现间隔沿 60 s 飞行 |
| `out/*.json` / `out/*.log` | 全部原始结果 |

---

## 2. 固定的 60 s 飞行脚本（基线）

### 2.1 设计

一段脚本需要依次覆盖 LOD 系统的所有工况：远景大覆盖、快速下降（大量新节点）、街道巡航、原地急转（请求取消与节点变化）、贴近立面（深层级、near 平面变小）、跟随无人机、拉升回全景（大量驱逐）。

| 段 | 时间 | 内容 | 主要考验 |
|---|---|---|---|
| overview-descent | 0–6 s | 从城市上空 500–800 m 高、俯角 38° 的全景，下降到巡航起点（离地约 90–190 m） | 首屏、粗到细、大量新请求 |
| transit | 6–20 s | 朝最高楼方向直线巡航 290 m（约 21 m/s），俯角 18°→12° | 稳态流式、前向预取 |
| fast-yaw | 20–24 s | 原地偏航 180°（90°/s），然后转回 | 取消、节点变化率、迟滞 |
| tower-orbit | 24–40 s | 以最高楼为圆心、半径 75 m、高度为 0.55 倍楼高，环绕半圈，视线对准楼体轴线 | 深层级、FPV 近景、near/far、cut/Lite 点径 |
| follow | 40–50 s | 第三人称跟随，离地约 120 m、15 m/s 直线飞离，俯角 25° | 中距离、持续平移 |
| climb-out | 50–60 s | 拉升到 600–900 m，俯角 35°，看向城市中心 | 大覆盖、驱逐、预算重分配 |

- **生成规则**（`prep.py::flight`）：
  - 关键帧依次为时间、eye（ENU，米）、yaw、pitch。
  - 位置和角度先线性插值，再做高斯平滑：位置 σ=12 帧，角度 σ=9 帧，均按 60 Hz 计。
  - 视线目标为 `eye + 100·fwd`。
  - 最高楼取 x01 `analysis.json` 中的 `buildings.peaks[0]`。
  - 巡航高度取 `max(沿途 hmap 最大值 + 25 m, 90 m)`。
- **文件格式**：`flight.bin` 为 f32 [3601 × 6]，每行是 eye xyz 和 target xyz，60 Hz；`flight.json` 记录分段、关键帧和统计量。
- **三城统计**：

  | 城市 | 最高楼 | 最大速度 | 速度中位数 | 最大偏航角速度 | 高度范围 |
  |---|---|---|---|---|---|
  | 深圳 | (−162, 98.5)，h = 381 m | 97 m/s（仅出现在相机转场段） | 20.7 m/s | 90°/s | 90–695 m |
  | 纽约 | (−434, −741)，h = 287 m | 130 m/s | 20.7 m/s | 90°/s | 141–894 m |
  | 上海 | (−2923, 810)，h = 637 m | 143 m/s | 20.7 m/s | 90°/s | 90–894 m |

- 相机参数：`fovY` 60°，画布 1280×720（CSS 像素），near 1 m，far 20 km。

### 2.2 落地

- 工程里把 `prep.py::flight()` 移植为 `tools/bench/flight60.py`，产物放在 `apps/web/public/bench/flight60/<city>.bin`。
- 前端提供 `?bench=flight60&city=shenzhen&tier=auto`：按墙钟时间 t 取样（`k = round(t·60)`，两帧之间线性插值），60 s 后把 `window.__perf` 写出。
- 帧间隔、B、档位、绘制点数、`limitedBy`、`achievedScreenError`、在途/排队/失败/取消数、驻留点数，都写入环形缓冲。
- 离线仿真（Node/vitest）和浏览器使用同一份 `.bin`，保证选择器单测与 e2e 的相机轨迹逐帧一致。
- **默认场景是深圳**（默认演示城市），纽约与上海作为回归用例。6 城并排的压力场景（n01 §3.12）作为扩展用例，沿用同一套关键帧规则。

---

## 3. 选择器：两级预算还是目标集 + 分段

### 3.1 被测方案

| 代号 | 定义 | 来源 |
|---|---|---|
| A | 两级预算 best-first：入堆时裁剪；放不下时跳过而不是 break（最多 32 次）；τ 以下为奖励层，最多花到 B·(1−0.15)；τ/4 为地板 | `refs/discovery/voxelkloud-view/src/lod/select.ts::selectVisible`（按原文逐行移植到 `g02/lod.mjs::selA`） |
| AP | A 加一条：**第一个**因预算被拒的 required 节点（key ≥ τ）画一个前缀，`cnt = B − pts`（≥ 512 点），节点内点序已打散，所以前缀就是均匀子采样 | r12 §4.4 与 r13 §3.1 的 fractional 思想 |
| APH | AP 加迟滞：上一帧已绘制的子节点 `key × 1.1`，其余 `key × 0.9`，再与 τ、τ/4 比较 | r12 §3.2 的 ±10% 迟滞 |
| B | r12 §4.4：Potree 式 best-first，**第一个放不下就 break**，最后一个节点画前缀；L≤1 不做视锥裁剪；阈值 0.9τ/1.1τ 迟滞；键乘屏幕中心权重；未驻留节点也占预算（目标集语义）；只画“父节点已绘制”的节点 | `refs/web3d/potree-core/source/potree.ts:192–194`（break）、`:342`（`minNodePixelSize`）；r12 §3.2 |

四种方案的几何量完全一致：
- `key = spacing_L · 0.5·H_dev / (tan(fovY/2) · d)`；
- `d = max(eye 到 tight AABB 的最近距离, near)`；
- 统一使用设备像素。

### 3.2 结果（流式模式，30 Hz，60 s；完整表见 `out/sim_<city>_{stream,ideal}_30.json`）

| 城市 | B（τ） | 方案 | 填充率均值（p10） | 4 px 空洞率 | 帧间节点变化率 | 下载量 | 选择耗时 p50/p95 | limitedBy |
|---|---|---|---|---|---|---|---|---|
| 深圳 | 40k（4） | A | 0.974（0.932） | 81.5% | 0.54% | 1.9 MB | 4.6 / 54 µs | budget |
| | | APH | **0.998（1.000）** | 82.8% | 0.59% | 2.8 MB | 3.9 / 24 µs | budget |
| | | B | 0.860（**0.642**） | 83.5% | 0.47% | 1.8 MB | 4.6 / 33 µs | budget |
| 深圳 | 150k（3） | A | 0.984（0.979） | 33.4% | 1.82% | 16.1 MB | 15 / 61 µs | budget |
| | | APH | **0.994（0.998）** | **32.9%** | **1.15%** | 16.4 MB | 15 / 26 µs | budget |
| | | B | 0.977（0.935） | 34.1% | 1.16% | 14.6 MB | 20 / 36 µs | budget |
| 深圳 | 750k（2.7） | A | 0.819 | **1.2%** | 1.94% | 85 MB | 61 / 120 µs | headroom 75%、complete 19% |
| | | APH | 0.807 | **1.2%** | 1.65% | 77 MB | 56 / 86 µs | headroom 81%、complete 19% |
| | | B | 0.581 | 3.4% | 1.13% | 35 MB | 51 / 86 µs | **error 100%** |
| 纽约 | 40k（4） | APH / B | 0.998 / **0.800（p10 0.607）** | 77.9% / 80.5% | 0.36% / 0.25% | 2.3 / 0.8 MB | — | budget |
| 纽约 | 750k（2.7） | APH / B | 0.778 / 0.682 | **1.1% / 2.1%** | 1.68% / 1.11% | 68 / 33 MB | 48 / 74 µs、58 / 88 µs | — |
| 上海 | 150k（3） | APH / B | 0.991 / 0.983 | 12.0% / 12.5% | 1.09% / 1.23% | 13.5 / 11.6 MB | — | — |
| 所有城市 | 3M（1.35） | APH / B | 0.21–0.46 / 0.14–0.30 | 0.8–0.9% / 0.9–1.0% | 1.4–1.6% / 1.1–1.2% | — | p95 ≤ 169 µs | complete 为主 / error 为主 |

**解读**：

1. **预算受限区（Tier S 与集显的常态）**：
   - B 的“第一个放不下就 break”加上“未驻留节点也占预算”，在流式期间把预算锁给了还没到的节点。结果是实际绘制的点数明显不足：深圳 40k 时平均只画到 86%，最差的 10% 帧只有 64%；纽约 60k 时平均 0.71。
   - A 靠跳过找到小节点补位，填充率 0.93–0.99。
   - AP/APH 用前缀补齐最后一段，填充率 ≥ 0.99，**并且点数随 B 线性变化**，这正是控制器需要的执行器特性（单调、近似线性）。
2. **τ 受限区（独显常态）**：B 没有奖励层，停在 τ（加迟滞后实际是 1.1τ），空洞率比 A/APH 高 1.5–3 倍。它少画的 30–40% 点数，本来就在预算之内，GPU 放得下。A/APH 在 τ 以下还能多细化最多 2 级，直到 0.85B 为止，这也是 voxelkloud 的原设计意图。
3. **稳定性**：
   - AP 的节点变化率比 A 低（前缀替代了“跳过后换一批小节点”）。
   - APH 在 AP 基础上再降 5–20%，下载量少 3–11%，空洞率和填充率不变。
   - B 在 τ 受限区变化率最低，但那是因为它细化得少，不是因为更稳。
4. **CPU**：3M 预算、深度 5 的树，每帧选择耗时 p95 ≤ 0.17 ms（本机 1.7 GHz、负载 10+）。**每帧都跑选择，不需要降频到 10 Hz**。

### 3.3 定案：APH（`engine/pointcloud/core/Selector.ts`）

```ts
export interface SelectOptions {
  tau: number;            // 目标屏幕误差（设备 px），由档位给出
  tauMin?: number;        // 默认 tau/4（BONUS_LEVELS=2）
  B: number;              // 本帧点预算（CAS 给出）
  headroom?: number;      // 0.15
  maxNodes?: number;      // 4096
  maxSkips?: number;      // 32
  minPrefix?: number;     // 512：前缀小于此值时不画
  hysteresis?: number;    // 0.1
}
export interface Selection {
  idx: Int32Array; cnt: Int32Array; n: number;       // pop 顺序 = 下载优先级；cnt < numPoints 表示前缀
  points: number; limitedBy: 'budget'|'nodes'|'headroom'|'error'|'complete';
  achievedScreenError: number; minPointSpacingWorld: number; levelCounts: Int32Array;
}
export function selectVisible(tree: LodTreeView, cam: LodCameraState, o: SelectOptions, s: LodScratch, out: Selection): Selection;
```

相对 voxelkloud `select.ts` 只有三处改动（其余包括 `LodTreeView`、`LodScratch`、零分配、`needsExpand`、`limitedBy`、`achievedScreenError`，全部原样保留）：

```ts
// (1) 迟滞：计算子节点 key 之后
let ck = spacing(child.level) * pf(max(distToTightAabb(eye, child), near));
ck *= s.drawnFrame[child.index] === frame - 1 ? 1 + h : 1 - h;          // h = 0.1

// (2) 预算拒绝分支：跳过之前，先检查是否画前缀
if (i !== root && pts + own > cap) {
  if (required) hitBudget = true; else hitHeadroom = true;
  worst = max(worst, key);
  if (required && !prefixed && B - pts >= o.minPrefix) {                // 只给第一个被拒的 required 节点
    prefixed = true; out.idx[n] = i; out.cnt[n++] = B - pts; pts = B;   // 它的子节点不展开
    continue;
  }
  if (++skips > o.maxSkips) break; continue;
}

// (3) 选择结束后：记录本帧已绘制（选中且已驻留）的节点，供下一帧迟滞使用
for (k < out.n) if (resident(out.idx[k])) s.drawnFrame[out.idx[k]] = frame;
```

**下载排序**（不改变选择集）：
- 默认 `prio = popOrder`。
- FPV 或跟随模式下，对同一 pop 批次内的节点按 `key · w_center · w_focus` 重排：
  - `w_center = clamp(1 − |ndc|, 0, 1) + 0.5`；
  - `w_focus = 1 + 1.5·exp(−|c − p_focus|²/(2·150²))`。
- 中心或焦点权重**不能**乘进选择键：它会破坏“子节点 key ≤ 父节点 key”的单调性，而 best-first 的正确性依赖这一点（voxelkloud `LodTreeView.nodeGeometricError` 注释）。

---

## 4. GPU 组织：逐节点、单缓冲，还是页池 + DrawTable

### 4.1 被测方案（`g02/bench.mjs`，原生 WebGL2）

| 代号 | 缓冲 | 绘制 | 每顶点额外工作 | 前缀支持 |
|---|---|---|---|---|
| O1 | 每节点一个 VBO + VAO，12 B/点（u16×3 规格化 + u8×2 oct + u8×4） | 每个选中节点一次 `drawArrays`，节点参数用 uniform | 无 | 有（count） |
| O2 | 单个 arena VBO，24 B/点（f32×3、u8×4、f32 pitch、u32 meta），首次适配分配，尾部回缩；R8UI liveness 纹理 | 一次 `drawArrays(0, 高水位)` | 1 次 liveness `texelFetch`，未选中的点移出裁剪空间 | 无（按节点整体开关） |
| O3m | 单个 Q16 VBO，按节点连续存放 | 一次 `multiDrawArraysWEBGL(firsts, counts)`，节点参数用 `gl_DrawID` 从 RGBA32F 表中读 2 次 | 2 次 `texelFetch` | 有 |
| O3d | RGBA32UI 池纹理（宽 4096，1 点 1 texel）；DrawTable（RGBA32UI：prefix、poolBase、count）加节点表 | 无属性，一次 `drawArrays(POINTS, 0, Σcnt)` | 按 `gl_VertexID` 在 DrawTable 上二分（≤ log₂n 次），再读 1 次池、2 次节点表 | 有 |

O2 的常驻上限按 OLV 规则：超过 `ratio·B` 触发驱逐，驱逐到 `min(ratio, 1.15)·B`，永远不驱逐当前选择集。测试 `ratio ∈ {1, 1.5, 3}`。voxelkloud 默认的 512 MiB 常驻上限相当于 2200 万点，对本数据等于“整城常驻”。

### 4.2 结果（配对测量，深圳 60 s 脚本中 16 帧，负载 12–16）

每格为：耗时中位数（ms） | 相对 O3m 的逐帧比值中位数 | 平均顶点数。

| 方案 | 25k | 40k | 60k | 100k | 150k | 250k | **平均比值** |
|---|---|---|---|---|---|---|---|
| O3m-N | 26.4 \| 1.00 \| 23k | 39.8 \| 1.00 \| 39k | 54.1 \| 1.00 \| 58k | 83.3 \| 1.00 \| 99k | 141.7 \| 1.00 \| 149k | 272.2 \| 1.00 \| 249k | 1.000 |
| O1-N | 26.0 \| 1.15 | 38.8 \| 0.97 | 46.8 \| 0.89 | 78.9 \| 0.95 | 160.3 \| 1.15 | 269.8 \| 1.04 | 1.024 |
| O3d-N | 26.9 \| 1.17 | 41.1 \| 1.18 | 49.9 \| 0.85 | 78.2 \| 0.93 | 141.8 \| 1.05 | 326.2 \| 1.07 | 1.042 |
| O2-N（常驻 1×） | 27.9 \| 1.23 \| 32k | 55.1 \| 1.48 \| 63k | 64.1 \| 1.07 \| 85k | 86.9 \| 1.14 \| 127k | 149.5 \| 1.05 \| 191k | 330.8 \| 1.11 \| 310k | **1.178** |
| O2-N（常驻 1.5×） | 29.4 \| 1.25 \| 35k | 48.9 \| 1.34 \| 57k | 64.2 \| 1.19 \| 104k | 103 \| 1.27 \| 165k | 171 \| 1.19 \| 238k | 298.8 \| 1.10 \| 402k | **1.224** |
| O2-N（常驻 3×） | 32.3 \| 1.40 \| 52k | 50.6 \| 1.47 \| 80k | 72.4 \| 1.34 \| 147k | 142.5 \| 1.65 \| 237k | 219.4 \| 1.50 \| 348k | 430 \| 1.66 \| 609k | **1.501** |

补充数据：
- 非配对的单轮扫描（`out/bench_shenzhen_main.json`，负载 9–17）得到同样的排序。
- 线性拟合每个**绘制点**的开销：O1/O3m/O3d 为 1.15–1.4 µs，O2 为 1.4–2.0 µs。
- 按**顶点**算，O2 被剔除的顶点每个约 0.4–0.6 µs，不是零成本。
- r13 用 `gl_VertexID` 加 `texelFetch` 取点的实测（与属性取点持平）在这里再次得到确认。

### 4.3 解读

1. **在 SwiftShader 上，“一次 draw”对 GPU 耗时没有收益**。O1 在每帧 3–30 次 draw（25k 时平均 3.4 个节点，250k 时 29.4 个）、每次 draw 约 40–60 µs（r12）的规模下，与单次 draw 的差异淹没在噪声里。真正决定成本的是**顶点数**。
2. **O2 的成本与“常驻/绘制”比例成正比**。它的优点（单次 draw、分配器简单）在真 GPU 上确实成立，因为 500 万个被剔除的顶点大约只要 1–2 ms。但在 Tier S 上，它把驱逐策略直接变成了帧耗时。此外 liveness 以节点为粒度，无法画前缀，使 §3 的线性执行器失效。24 B/点也比 PointPool 的 16 B（storage 为 12 B）更大。**不采用。**
3. O1、O3m、O3d 三者的 GPU 耗时相当，**应按 CPU 开销与架构一致性选择**：
   - O1 在 three 里是每节点一个 `Points` 对象。经典 `WebGLRenderer` 每对象约 14 µs（r11 实测），256–2048 个可见节点就是 3.6–29 ms 的 CPU 时间，在 Tier A/B 上无法接受。另外节点进出伴随缓冲的创建和销毁。只保留为 dev 对照页。
   - O3m 需要 `WEBGL_multi_draw`（本机 SwiftShader 可用，主流桌面 Chrome 都有），而且 three r186 只对 `isBatchedMesh` 走 `renderMultiDraw`（`src/renderers/WebGLRenderer.js` L1319–1337）。要在 `Points` 上用它，必须冒充 `isBatchedMesh` 并提供 `_multiDrawStarts/_multiDrawCounts/_multiDrawCount/_matricesTexture/_indirectTexture/_colorsTexture` 这些私有字段。这属于私有 API 依赖。
   - O3d 在 three 中**完全用公开 API 实现**：几何体不设 `position` 属性时，`drawEnd` 只受 `drawRange.count` 限制（`WebGLRenderer.js` L1230–1252），因此 `new Points(new BufferGeometry().setDrawRange(0, total), pointMaterial)` 加上 `frustumCulled = false` 即可（`pointMaterial` 为 TSL 材质，经典路径用 G1 的 `GLPointsNodeMaterial`；本基准的原生 GLSL 与之等速，见 G1 §4.5）。它的数据布局与 Tier A 的 storage buffer 以及 voxelkloud compute 路径的 compact 分派（`buildVisibleBlocks` 加线程内二分）**完全同构**，两个后端共享一套分配器和 DrawTable。

### 4.4 定案：PointPool + DrawTable（`engine/pointcloud/gpu/PointPool.ts`、`DrawTable.ts`）

```text
PointPool（Tier B/S：纹理；Tier A：纹理或 storage buffer，word 布局相同）
  texel      = 1 点 = RGBA32UI（16 B）：w0 = x | y<<16，w1 = z | octX<<16 | octY<<24，w2 = rgba8（r,g,b,class），
               w3 = 保留（intensity、gpsTime 低位或 returnNumber）
               Tier A 用 storage 时每点 3 个 u32（12 B），w3 不存
  width      = 4096 texel（1 行 = 1 页 = 4096 点）
  capacity   = ceil(1.6 · B_hi(autoCeiling) / 4096) 行
               Tier S：1.6×150k → 59 行，约 3.9 MB；Tier B：1.6×3M → 1172 行，约 77 MB；Tier A：1.6×6M → 2344 行，约 115 MB（storage 12 B）
  alloc      = 按页首次适配，要求连续页（节点 ≤ 31,745 点，即 ≤ 8 页）；释放时合并；尾部空闲时下调高水位
               （voxelkloud BlockAllocator 语义，粒度改为页）
  attach     = texSubImage2D(rows)，每帧上传上限：Tier S 20k 点（约 5 页），Tier B/A 8 MiB
  evict      = OLV 规则：驻留 > 1.5·B 时触发，释放到 1.15·B；不碰本帧选择集和根节点；可见节点驻留 1 s 内受保护（被更细层取代的除外）

DrawTable（每帧重写，≤ maxNodes 条；RGBA32UI 纹理，宽 1024）
  entry k    = [prefixStart_k, poolBase_k, cnt_k, nodeIdx_k]      // prefixStart 为 cnt 的前缀和
NodeTable（同一顺序，RGBA32F × 2）
  [min.xyz, cubeSize] [spacing_L, level, childDrawnMask, classMask]
绘制：Points(无属性 geometry, TSL pointMaterial).setDrawRange(0, Σcnt)，frustumCulled=false，renderOrder=-1，
      raycast 置空（拾取走 ID 附件；点云祖先上不挂指针事件，见 00-index §3.6）
```

> 注：
> - 纹理格式统一用 RGBA32UI。WebGL2 的 RGB32UI（12 B/点）在本机 SwiftShader 上可用：4096×2 上传无 GL 错误，`texelFetch` 读回正确（`g02/rgb32ui.html`）。但 WebGPU 没有三通道 32 位格式（G1 §4.5），为了让一份 TSL 覆盖三个后端，放弃 RGB32UI。
> - 按 G1 的交叉验证：
>   - 着色器写成 TSL：`vertexIndex`、`textureLoad`，二分查找用 `Loop`。经典路径的点径用 `GLPointsNodeMaterial`，TSL 与 GLSL 的速度比为 1.05。
>   - DrawTable 的计数等参数用 **float uniform**，因为 handler 下 int uniform 会失效。
>   - 任何后端都**不设 `texture.internalFormat`**。
> - 下面的 GLSL3 只作为语义参考与 dev 对照页实现。

顶点着色器核心（GLSL3 参考写法；产品实现为 TSL，用 `vertexIndex`、`textureLoad`（或 storage）和 `Loop`，语义相同）：

```glsl
int vid = gl_VertexID, lo = 0, hi = uNumDraws;
while (hi - lo > 1) { int mid = (lo + hi) >> 1; if (int(texelFetch(uDraw, ivec2(mid & 1023, mid >> 10), 0).x) <= vid) lo = mid; else hi = mid; }
uvec4 d = texelFetch(uDraw, ivec2(lo & 1023, lo >> 10), 0);
int gi  = int(d.y) + (vid - int(d.x));
uvec3 w = texelFetch(uPool, ivec2(gi & 4095, gi >> 12), 0).xyz;
int e = 2*lo; vec4 n0 = texelFetch(uNode, ivec2(e & 1023, e >> 10), 0), n1 = texelFetch(uNode, ivec2((e+1) & 1023, (e+1) >> 10), 0);
vec3 q  = vec3(float(w.x & 0xffffu), float(w.x >> 16u), float(w.y & 0xffffu)) / 65535.0;
vec3 p  = n0.xyz + q * n0.w;                                   // 云局部 float32（原点 = cube min），精度见 n01 §3.3
```

补充说明：
- **拾取**：点 ID 为 `gl_VertexID + 1`，写入 R32UI 或 RGBA8 附件。CPU 端在 DrawTable 的前缀和上二分，得到节点和节点内下标（与 voxelkloud `pick.ts` 以及 r13 §3.6 语义一致）。
- **可选优化 O3m**：`gpuOrg: 'pool-multidraw'`。在真 GPU 上，如果 profiling 显示二分成为热点（可见节点 > 2048 时每顶点 11 次以上读取），就改用 `WEBGL_multi_draw`，由 `gl_DrawID` 直接给出 entry，省去二分。three 的集成用上面提到的 `isBatchedMesh` 鸭子类型，并加版本锁与单测。
- **dev 对照**：O1，即 `apps/web/dev/oracles/per-node.tsx`。

---

## 5. 点径：cut 纹理、Lite 掩码，还是按节点

### 5.1 开销（配对测量，O3m，相对 N 的逐帧比值中位数）

| 模式 | 25k | 40k | 60k | 100k | 150k | 250k | 平均 |
|---|---|---|---|---|---|---|---|
| L（Lite：按点的八分体查节点的 8 位子掩码） | 1.01 | 0.95 | 0.97 | 0.97 | 1.03 | 1.00 | **0.99** |
| C（cut 遍历，`depth > level` 时早退） | 1.04 | 1.08 | 1.29 | 0.92 | 1.05 | 1.10 | **1.08** |
| CF（voxelkloud 原版遍历，无早退） | 1.10 | 1.08 | 0.89 | 0.93 | 1.15 | 1.03 | **1.03** |
| O3d-C | 1.15 | 1.10 | 1.12 | 1.12 | 1.09 | 1.12 | 1.12 |
| O2-C（常驻 1.5×） | 1.23 | 1.30 | 1.61 | 1.17 | 1.22 | 1.41 | 1.32 |

**“每顶点最多 20 次 texelFetch”在本项目数据上不会出现**：
- `MAX_CUT_DEPTH=20` 只是 float32 精度的上限（voxelkloud `cut.ts`）。
- 实际循环次数等于“该点位置上 cut 的最深层级 + 1”，UrbanScene3D 按 G=64 建树只有 5 层，最多 6–7 次。
- 带早退时最多 `level + 2` 次。
- SwiftShader 以 4 宽 SIMD 批处理顶点，循环按批内最大次数执行，实测整体开销只增加 3–8%。

### 5.2 画质（12 个采样帧，与全量参考渲染逐像素比较；`out/bench_shenzhen_quality{,05,1b}.json`）

| 渲染尺寸 | B（τ） | minPx/maxPx | 模式 | 空洞率 | 溢出率 | 亮度误差 |
|---|---|---|---|---|---|---|
| 1280×720 | 40k（4） | 1.5/8 | N = L = C | 63.7% | 0.01% | 9.76 |
| 1280×720 | 40k（4） | 2/16 | N / L = C | 37.0% | 1.53% / 1.49% | 8.91 / 8.88 |
| 1280×720 | 100k（3） | 1.5/8 | N = L = C | 20.2% | 0.07% | 4.23 |
| 1280×720 | 100k（3） | 2/16 | N / L = C | 4.5% | 4.11% / 4.01% | 5.85 / 5.68 |
| 1280×720 | 150k（3） | 1.5/8 | N / L = C | 7.5% | 0.12% / 0.11% | 2.96 / 2.92 |
| 1280×720 | 150k（3） | 2/16 | N / L = C | 0.4% | 4.27% / 4.07% | 5.43 / 5.10 |
| 1280×720 | 250k（3） | 1.5/8 | N / L = C | 1.1% | 0.20% / 0.16% | 1.99 / 1.92 |
| 1280×720 | 250k（3） | 2/16 | N / L = C | 0.0% | 4.29% / 3.93% | 5.30 / 4.65 |
| 1280×720 | 400k（2.7） | 1.5/8 | N / L | 0.19% | 0.21% / 0.14% | 1.61 / 1.36 |
| 640×360 | 25k（4） | 1.5/8 | N = C | 44.7% | 0.22% | 9.98 / 9.95 |
| 640×360 | 25k（4） | 2/16 | N / C | 20.4% | 1.88% / 1.60% | 8.53 / 8.29 |
| 640×360 | 40k（4） | 2/16 | N / C | 17.0% | 1.99% / 1.62% | 7.36 / 6.96 |
| 640×360 | 60k（4） | 2/16 | N / C | 8.3% | 2.22% / 1.50% | 6.52 / 5.98 |
| 640×360 | 100k（3） | 2/16 | N / C | 0.5% | 2.55% / 1.52% | 5.76 / 4.73 |

τ 受限区（1280×720；参考渲染为全部节点）：

| B（τ） | 实际绘制 | minPx/maxPx | N | L = C |
|---|---|---|---|---|
| 750k（2.7） | 622k | 1.5/8 | 空洞 0，溢出 0.22%，亮度误差 1.45 | 空洞 0，溢出 **0.04%**，亮度误差 **0.75** |
| 1.5M（2.0） | 1.04M | 1.5/8 | 0 / 0.22% / 1.42 | 0 / **0.01%** / **0.29** |
| 3M（1.35） | 1.54M | 1.5/8 | 0 / 0.22% / 1.41 | 0 / **0.00%** / **0.03** |
| 3M（1.35） | 1.54M | 2/16 | 0 / 4.29% / 5.16 | 0 / 3.78% / 3.26 |

在 τ 受限区：
- N 让父层点按父层间距画，糊掉了已驻留的细层，亮度误差恒定在约 1.4。
- L（等价于 C）把误差降到 0.03–0.75，几乎与参考渲染一致。
- 这时如果把 `maxPx` 放到 16，反而会引入 3.8% 的溢出和 3.3 的亮度误差，所以 **`maxPx` 必须按 `limitedBy` 自适应**。

**解读**：

1. **L 与 C 的画质指标完全一致**（上表所有配对 L=C，空洞率、溢出率、亮度误差到 4 位小数都相同）。原因是 voxelkloud 实测的最优设置是**缩小最多 1 级**（`shrink = clamp(depth − level, 0, 1)`；放开到全级时覆盖率随加载反而下降 5.4%，见 n01 §3.3）。在这个上限下，cut 遍历真正回答的只是“本点所在的子八分体在本帧是否已绘制”，而这正是 Lite 的 `childDrawnMask` 查询。唯一的差别是“子节点已驻留但父节点未驻留”的瞬态，这时 cut 给出 0、Lite 给出 1，影响可以忽略。**所以 cut 纹理没有存在的必要**：Lite 是 O(1)，不需要每帧构建和上传 cut 纹理，TSL 与 WGSL 实现也更简单。
2. **预算受限时（Tier S 常态）三种模式没有区别**：前沿节点的投影间距远大于 τ，点径被 `maxPx` 截断。这时起作用的是 `maxPx` 和渲染比例：
   - `maxPx` 从 8 调到 16，空洞率大约减半，代价是 +4–38% 的耗时（平均 +21%；24 px 时 +37%，32 px 时 +40%）。
   - **0.5 渲染比例下画 25k 点、`maxPx` 16，比全分辨率画 60k 点、`maxPx` 8 的画面更完整**（空洞率 20% 对 47%，各自与同分辨率的参考渲染比较），点数不到一半，耗时也约为一半（SwiftShader 受顶点限制，渲染比例本身几乎不改变每点开销，见 §6.4 第 5 条）。
   - 这就是 Tier S 采用 0.5 渲染比例、较少点数、较大点径的原因。代价是画面放大后偏软。
3. **τ 受限时（Tier B/A 常态）**，L 相对 N：
   - 在 400k 时亮度误差为 1.36 对 1.61；
   - 在 750k–3M 时亮度误差为 0.03–0.75 对约 1.4，溢出率为 ≤ 0.04% 对 0.22%。

   这正是“父层点过大糊掉细节”的问题，Lite 已经解决了，而且在 SwiftShader 上零成本。

### 5.3 定案（`engine/pointcloud/render/pointSize.ts`，TSL 为准；下面的 GLSL 是等价的参考写法）

```glsl
// n0 = [min.xyz, size]，n1 = [spacing_L, level, childDrawnMask, classMask]，q ∈ [0,1]³ 为节点局部坐标
int oc = (q.x >= 0.5 ? 4 : 0) | (q.y >= 0.5 ? 2 : 0) | (q.z >= 0.5 ? 1 : 0);   // Potree 位序 x<<2|y<<1|z
float pitch = ((int(n1.z) >> oc) & 1) == 1 ? 0.5 * n1.x : n1.x;             // Lite：缩小最多 1 级
float px = uSizeK * pitch * uProjScale / max(-view.z, 1e-6);                 // uProjScale = 0.5·H_dev·P[1][1]
gl_PointSize = clamp(px, uMinPx, uMaxPxEff);
```

- `childDrawnMask`：CPU 在写 DrawTable 时计算，即本帧“已绘制”（选中且驻留）的子节点位掩码。前缀节点的子节点不会被绘制，所以掩码为 0。
- `uSizeK = 1.7`（Potree 与 3DTilesRendererJS 的覆盖系数）。
- `uMaxPxEff` 是自适应最大点径：
  ```ts
  target = (sel.limitedBy === 'budget' || sel.limitedBy === 'nodes') ? rung.maxPxSparse : rung.maxPx;   // 16 : 8
  maxPxEff += (target - maxPxEff) * (1 - Math.exp(-dtMs / 300));                                        // 300 ms 平滑，避免逐帧跳变
  ```
  Tier S 的 `minPx` 取 2（0.5 渲染比例下的设备像素），Tier B/A 取 1。

---

## 6. 控制器：画质阶梯 + AdaptiveBudget 会不会振荡

### 6.1 被测控制器（`g02/ctrl.mjs`）

| 代号 | 规则 |
|---|---|
| QL | voxelkloud `quality.ts::QualityController` 原样移植：90 帧窗口；`p50 > 1.35·T_refresh` 降档，`p95 < 1.1·T_refresh` 且距上次换档 > upDelay 升档；升档后又降档则 upDelay 翻倍（5 s→120 s）；`T_refresh` 为“最快帧”EMA，范围限制在 6–34 ms。B 取档位预算 |
| AB | n05 `adaptiveBudget.ts` 原样：EMA 0.2，每 250 ms 评估；`r > 1.2` 时按 `max(0.6, 1/r)` 乘性下降；`r < 1.05` 且距上次下降 > 1 s 时乘 1.1 |
| QL+AB | 00-index §3.5 的写法：QL 定档，AB 在档内 `[lo, hi]` 微调，换档时把 B 重置为 hi |
| **CAS** | 本文方案，见 §6.3 |

**闭环仿真**（`g02/ctrlsim.mjs`）：
- 每帧用真实的 APH 选择结果（给定 B、τ、渲染比例）得到绘制点数 N。
- 帧耗时模型：`work = (a + b·N·dist + e·W·H)·exp(0.1·𝒩)`，1% 的帧额外加 20–60 ms 尖峰；第 30–40 s 施加 `dist = 2` 的扰动；呈现间隔为 `ceil(work/T_vsync)·T_vsync`。

| 设备 | 分辨率 | a | b | e | vsync | 目标 T | 起始档 |
|---|---|---|---|---|---|---|---|
| swiftshader | 1280×720 | 1.5 ms | 1.0 µs/点（本机实测） | 1e-6 ms/px | 60 Hz | 33.3 ms | 0 |
| igpu | 1080p | 3 ms | 5 ns/点 | 2e-6 ms/px | 60 Hz | 16.7 ms | 3 |
| dgpu | 1080p | 2 ms | 1 ns/点 | 5e-7 ms/px | 60 Hz | 16.7 ms | 4 |
| dgpu144 | 1440p | 1.5 ms | 1 ns/点 | 5e-7 ms/px | 144 Hz | 6.9 ms | 4 |

igpu、dgpu 的参数为估计值，量级参照 n01 §3.8 引用的 Bauer 2025 与 voxelkloud 自报数据。

### 6.2 仿真结果（深圳与纽约 × 3 个种子的均值；`out/ctrlsim_{shenzhen,newyork}.json`）

| 设备 | 控制器 | fps | p50 / p95（ms） | >1.5T 帧占比 | 60 s 换档次数 | 10 s 内来回 | B 反向次数/分钟 | B 变异系数（45–50 s） | 平均点数 |
|---|---|---|---|---|---|---|---|---|---|
| swiftshader | QL | 18.2 | 50 / 94 | **15.4%** | 0 | 0 | 0 | 0 | 40k（卡在 0 档） |
| | AB | 28.7 | 33 / 50 | 1.2% | — | — | **46** | 0.11–0.13 | 22.6k |
| | QL+AB | 28.3 | 33 / 50 | 1.5% | 0.7 | 0.3 | **45–46** | 0.15 | 23k |
| | **CAS** | **29.7** | 33 / **39** | 1.3% | 0 | 0 | **5** | **0.02** | 20.4k |
| igpu | QL | 52.4–53.2 | 17 / 33 | 10.7–13.0% | **6–8** | **1–2** | 3–4 | 0–0.31 | 0.9–1.0M |
| | AB | 57.0 | 17 / 17 | 3.2–3.4% | — | — | 31–35 | 0.30–0.33 | 0.73–0.79M |
| | QL+AB | 53.0–53.4 | 17 / 33 | 10.9–11.8% | **5.3–6** | 0.3–1 | 28–30 | 0.25–0.34 | 0.89–1.06M |
| | **CAS** | 56.2–57.0 | 17 / 17–22 | 3.6–4.9% | 2 | **0** | 6–8 | 0.02 | 0.81–0.95M |
| | CAS（有 GPU 计时） | 57.0–57.4 | 17 / 17 | 2.9–3.5% | 2 | 0–1 | 3–6 | 0.02 | 0.81–0.93M |
| dgpu | QL / QL+AB | 58.6 | 17 / 17 | 1.1% | 1 | 0 | 0 / 23–24 | 0 / 0.10 | 1.2–1.5M |
| | **CAS** | 58.6 | 17 / 17 | 1.1% | 1 | 0 | 1–2 | 0.02 | 1.2–1.5M |
| dgpu144 | QL | 130.5–131.1 | 6.9 / 6.9 | 4.5% | **6** | 1 | 3 | 0.13–0.22 | 1.03–1.39M |
| | AB | 134.9–135.2 | 6.9 / 6.9 | 1.1–1.2% | — | — | 36–37 | 0.34–0.47 | **0.35–0.37M** |
| | QL+AB | 130.7–131.6 | 6.9 / 6.9 | 4.1–4.6% | 3 | 0.7–1 | 30 | 0.23–0.27 | 1.0–1.3M |
| | **CAS** | 132.1–132.8 | 6.9 / 6.9 | 3.1–3.6% | 0–1.3 | 0 | 4–6 | 0.02–0.03 | 0.99–1.30M |

**结论：会振荡，而且振荡有两种形态**：

1. **B 的锯齿**：AB 是 TCP 式 AIMD，在 vsync 钉住时靠“超载再退”来探测余量。以 `lo = 1.05` 往上加、`hi = 1.2` 往下砍，每分钟反向 23–46 次，B 的变异系数 0.1–0.47。点数周期性涨落，画面表现为密度“呼吸”。
2. **档位翻转**：QL 的判据是 `p50` 相对 `T_refresh`。在 iGPU 上，一个档位的负载可能正好落在 1.0–1.35T 之间，QL 会反复“升档、超载、降档”，60 s 内换档 6–8 次，其中 1–2 次在 10 s 内来回。叠加 AB 后，AB 把 B 推到 r≈1.05–1.2，QL 几乎看不到 `p95 < 1.1T` 的升档条件。一旦降档，τ 和渲染比例同时改变，负载骤降，AB 又把 B 推高，于是出现“降档、推满、再升档”的循环（5–6 次）。
3. **SwiftShader 上 QL 失效**：它的刷新率估计取最快帧的 EMA（下降方向增益 0.25，上升方向 0.001），在本机停在约 16.7 ms，所以 `p50 > 1.35·16.7 ms` 永远成立，只会一路降到 0 档，档内没有任何调节（20 fps，p95 94 ms，15% 的帧超过 50 ms）。

### 6.3 定案：CAS 级联控制器（`engine/pointcloud/core/CascadeController.ts`）

```ts
export interface Rung { name: string; rs: number; lo: number; hi: number; tau: number; minPx: number; maxPx: number; maxPxSparse: number; edl: boolean }
export class CascadeController {
  constructor(o: { ladder: Rung[]; start: number; targetMs: number; initialB?: number /*Tier S 25k；缺省为 rung.hi*/;
                   tailK?: number /*Tier S 2.0，硬件 1.6*/; autoCeiling?: number /*5*/; floor?: number /*0*/ });
  /** interval：本帧呈现间隔（rAF delta）；workMs：可选的 GPU 计时（timer query 或 timestamp-query）；pending：有解码或上传积压 */
  sample(intervalMs: number, nowMs: number, pending: boolean, workMs?: number): -1 | 0 | 1;
  readonly index: number; readonly B: number; readonly rung: Rung;
  setManual(index: number | null): void;   // 用户手动锁档：只停外环，内环仍在档内工作
}
```

```text
每帧 push(interval, workMs)；窗口保留最近 24 帧（至少 6 帧）；每 250 ms 评估一次：
  r    = p50(interval) / T*                        // 内环始终看“用户实际看到的”呈现间隔
  tail = p90(interval) > tailK·T*                  // 超过 10% 的帧明显超时（tailK：Tier S 2.0，硬件 1.6）
  if      r > 1.10 : B *= clamp((1/r)^0.8, 0.5, 0.92)      // 按超载比例下降
  else if tail     : B *= 0.90
  else if r < 0.85 : 连续 2 次评估且没有积压 → B *= 1.08
  else             : 在目标带内（0.85–1.10）：每 8 次评估（约 2 s）且没有积压 → B *= 1.03   // vsync 钉住时的慢探测
  B = clamp(B, rung.lo, rung.hi)
  // 外环：只在内环饱和时换档
  下限饱和：B == lo 且 r > 1.2 持续 ≥ 1 s，并且 index > floor
     → index--；B = ladder[index].hi（无扰：从新档位的上沿开始）；如果距上次升档 < 10 s，upDelay = min(2·upDelay, 120 s)
  上限饱和：B == hi 且有余量证据，并且距上次换档 > upDelay（初值 5 s）
     余量证据：有 workMs 时要求 median(workMs) < 0.7·T*，持续 3 s；
              否则要求 r < 0.7 持续 3 s，或 p95(interval) ≤ 1.1·T* 持续 5 s（vsync 钉住时的探测，失败则退避）
     → index++；B = ladder[index].lo（无扰：从新档位的下沿开始）
  T* = Tier S：33.3 ms（固定 30 fps 目标，不用刷新率估计）；Tier A/B：refreshMs（voxelkloud 的“最快帧 EMA”估计）
```

**与原两者的对应关系**：
- 内环替代 n05 的 `AdaptiveBudget`，加入了死区、慢探测和“积压时不加”。
- 外环替代 voxelkloud 的 `QualityController`，保留升档退避翻倍，但把“是否换档”的判据改为“内环饱和”。

两层时间尺度分离：内环 250 ms，外环 ≥ 1 s 或 3–5 s。又因为换档是无扰的（B 在档位边界上连续），所以不会出现 QL+AB 的相互追逐。

**运动时的降载**不走 CAS，按 n01 的结论作用在 DPR 或渲染比例上（OLV `adaptiveDpr`：角速度 1.2 rad/s 时降到 `0.85·maxDpr` 至下限 1.0，按 0.25 量化，降档限速 250 ms）。Tier S 已经是 0.5 渲染比例，不再叠加这一项。

### 6.4 本机实时验证（SwiftShader，真实 rAF 呈现间隔，深圳 60 s 脚本）

用 `run_live.cjs` 实测：O3m 组织、N 点径、`maxPx` 8、1280×720 CSS 画布、真实 `requestAnimationFrame` 节奏，全部节点预先驻留，以便把控制器动态与流式分开。

| 批次（负载） | 城市 | 控制器 | 渲染比例 | fps | p50 / p95 / p99（ms） | >50 ms | >100 ms | 平均点数 | B 反向/分钟 | 换档 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1（7.5–12.8） | 深圳 | 固定 40k | 1.0 | 31.7 | 33.3 / 66.7 / 100 | 6.6% | 0.4% | 40.0k | 0 | 0 |
| | | 固定 25k | 1.0 | 31.4 | 33.3 / 66.7 / 83.3 | 8.2% | 0.7% | 25.0k | 0 | 0 |
| | | QL | 0.5 | **25.3** | 33.3 / **83.3 / 116.7** | **16.1%** | 1.8% | 40.0k（0 档） | 0 | 0 |
| | | AB（T=33.3） | 1.0 | 32.6 | 33.3 / 66.7 / 83.3 | 5.7% | 0.2% | 27.9k | **29** | — |
| | | QL+AB | 0.5 | 29.7 | 33.3 / 66.7 / 83.3 | 6.6% | 0.1% | 12.1k | **36** | 0 |
| | | **CAS** | 0.5 | 32.6 | 33.3 / **50 / 66.7** | **3.4%** | 0.2% | 11.3k | 12 | 0 |
| 2（3.4–6.5） | 深圳 | 固定 40k | 1.0 | 30.9 | 33.3 / 50 / 66.7 | 4.5% | 0.1% | 40.0k | 0 | 0 |
| | | 固定 40k | 0.5 | 28.6 | 33.3 / 66.7 / 100 | 8.4% | 0.4% | 40.0k | 0 | 0 |
| | | **CAS**（tailK 1.6） | 0.5 | **35.2** | 33.3 / **50** / 83.3 | 3.4% | 0.1% | **26.5k** | 13 | 0 |
| | | CAS（tailK 2.0） | 0.5 | 33.4 | 33.3 / 50 / 83.3 | 3.9% | 0.2% | **30.2k** | 13 | 0 |
| 3（7.1–12.6） | 纽约 | 固定 40k | 1.0 | 23.0 | 33.3 / 83.3 / 116.7 | 19.4% | 1.8% | 40.0k | 0 | 0 |
| | | AB | 1.0 | 31.2 | 33.3 / 50 / 83.3 | 4.7% | 0.1% | 27.6k | **31** | — |
| | | **CAS** | 0.5 | **35.0** | 33.3 / **50 / 66.7** | **2.3%** | **0** | 22.5k | **6** | 0 |

原始逐帧记录见 `out/live_shenzhen.json`、`out/live_shenzhen_b.json`、`out/live_newyork_ny.json`，每帧字段为 t、dt、档位、B、点数、CPU ms。

**实测结论**：

1. **本机 headless 管线的呈现间隔中位数停在 33.3 ms**：固定 25k 点时 p50 仍是 33.3 ms，与点数无关，只是偶尔出现 16.7 ms 的帧（fps 31–35）。瓶颈在 SwiftShader 的合成与呈现。因此 Tier S 的 `T* = 33.3 ms` 就是本机能达到的上限，而不是保守取值。
2. **QL 在真实管线上与仿真一致地失效**：卡在 0 档、固定 40k，p95 83 ms，16% 的帧超过 50 ms。
3. **AB 与 QL+AB 的锯齿在实测中同样存在**，B 每分钟反向 29–36 次。
4. **CAS 在三批、两座城市、负载 3.4–12.8 的条件下，p95 都保持在 50 ms，没有换档**，B 每分钟反向 6–13 次。它的平均点数随主机负载自动变化：负载约 12 时为 11k，约 6 时为 26–30k。这正是期望的行为：保节奏，用点数来吸收负载。
   - 负载较低时，把 `tailK` 从 1.6 放宽到 2.0 可以多画 14% 的点，节奏不变。**Tier S 取 `tailK = 2.0`，硬件档取 1.6**，因为 60 Hz 下 2T 就是一次明显的卡顿。
5. **0.5 渲染比例在本机不会让帧更快**：固定 40k 时为 28.6 fps，1.0 比例为 30.9 fps，差异在噪声内。原因是 SwiftShader 受顶点限制。0.5 比例的收益在于：
   - 每个点覆盖的画面比例更大，同样点数下空洞率大约减半（§5.2）；
   - 任何全屏 pass（EDL、雾、天气合成）的成本降为约 1/3（r12：8 tap EDL 在 720p 为 107 ms，在 360p 为 36 ms）。

   所以 soft-min 档保留 0.5 比例。

---

## 7. 软件档预算与完整参数表

### 7.1 为什么是这些数

本机每个绘制点的开销是 0.5–1.3 µs：
- 配对与单轮测量中，同一配置的中位数随其他进程的 SwiftShader 负载变化 2 倍；
- r12 在负载 12–18 时测得 1–3 µs；
- n05 在负载约 5 时，10–15 万点加 200 架无人机为 5.5–6.6 fps。

据此估算 30 fps 的点预算：`(33.3 − 2) / b`。b = 0.5 µs 时约 62k，b = 1.0 µs 时约 31k，b = 1.3 µs 时约 24k。20 fps（50 ms）时为 36k–96k。

| 候选 | 出处 | 本机帧耗时 | 判定 |
|---|---|---|---|
| 250k | r09 | 230–330 ms（3–4 fps） | **否决** |
| 150k | n01 soft 档上限 | 100–240 ms（4–10 fps） | 只作为 soft 档（idx 1）的上沿。安静主机可以用在截图或画质评测，自动模式很少会升到 |
| 60k | r11 | 47–85 ms | 只在安静主机上可达。0.5 渲染比例、`maxPx` 16 时空洞率 8%，是 Tier S 画质与速度的甜点，但不作为起步值 |
| 40k | r12 | 20–60 ms | **soft-min 档上沿**（B 的 hi） |
| 25k | 本文 | 13–38 ms | **Tier S 起步值 B0**：首帧快，TTFP 优先，CAS 在 1–2 s 内找到平衡点 |

### 7.2 最终画质阶梯（替换 00-index §3.5 的阶梯表）

| idx | 名称 | 渲染比例 | 预算带 [lo, hi] | τ（设备 px） | minPx | maxPx（受 τ 约束 / 受预算约束） | EDL | 适用 |
|---|---|---|---|---|---|---|---|---|
| 0 | soft-min | 0.5 | [10k, 40k] | 4.0 | 2 | 8 / 16 | 关 | Tier S 起步（SwiftShader、llvmpipe、CI），B0 = 25k |
| 1 | soft | 0.6 | [40k, 150k] | 3.0 | 2 | 8 / 16 | 关 | Tier S 上限（安静主机） |
| 2 | minimum | 0.6 | [150k, 750k] | 2.7 | 1.5 | 8 / 12 | 开（4 tap，半分辨率） | 集显低档 |
| 3 | low | 0.75 | [750k, 1.5M] | 2.0 | 1.5 | 8 / 12 | 开（4 tap） | 集显（Tier B 起步） |
| 4 | medium | 1.0 | [1.5M, 3M] | 1.35 | 1 | 8 / 8 | 开（8 tap） | 默认（Tier A 起步） |
| 5 | high | 1.0 | [3M, 6M] | 1.0 | 1 | 8 / 8 | 开 | 自动模式上限 |
| 6 | ultra | 1.0 | [6M, 12M] | 0.7 | 1 | 8 / 8 | 开 | 只能手动选择 |

**初始档**：
- Tier S（`architecture` 或 renderer 字符串匹配 `swiftshader|llvmpipe|software`）固定为 0。
- 其余按 voxelkloud `initialQualityIndex`：集显为 3，独显为 4，核数 ≥ 8 且像素 < 1.5M 时 +1，但不超过 5。
- 用户显式给出的 B 或 τ 作为上限（`ceilingForOptions`）。

**其余参数**：

| 参数 | Tier S | Tier B | Tier A |
|---|---|---|---|
| 选择器 | APH，τ 与 B 取自档位，h=0.15，maxNodes 4096，maxSkips 32，minPrefix 512，hysteresis 0.1 | 同左 | 同左 |
| 在途请求 | 首节点落地前 1，之后 4 | 1 → 8 | 1 → 12 |
| 每帧上传 | 20k 点 | 8 MiB | 8 MiB |
| 驱逐 | 1.5B → 1.15B，1 s 驻留保护 | 同左 | 同左 |
| PointPool 容量 | 59 行，约 3.9 MB | 1172 行，约 77 MB | storage，约 115 MB（12 B/点） |
| 点径 | Lite，sizeK 1.7 | 同左 | 同左 |
| 控制器 | CAS，T* = 33.3 ms，tailK 2.0 | CAS，T* = 刷新周期，tailK 1.6 | CAS，T* = 刷新周期，tailK 1.6；有 timestamp-query 时启用 workMs |
| 运动 DPR | 不用 | OLV `adaptiveDpr` | OLV `adaptiveDpr` |

---

## 8. 验收阈值（本机 Tier S 与真 GPU 分开）

**统一测法**：
- 场景为 `?bench=flight60&city=<c>`，默认深圳，回归用纽约和上海。
- 预热：metadata、`hierarchy.bin` 和首屏 Range 请求返回后，再等 1 s。
- 采集 60 s 的 `window.__perf`，同时记录 `os.loadavg()`。

### 8.1 Tier S（本机：SwiftShader、headless Chromium、1280×720 CSS、0.5 渲染比例）

| 类别 | 指标 | 阈值 | 依据 |
|---|---|---|---|
| 帧节奏 | 呈现间隔 p50 / p95 / p99 | ≤ 33.4 / ≤ 50 / ≤ 100 ms | §6.4：CAS 在负载 3.4–12.8 时实测为 33.3 / 50 / 66.7–83.3 |
| | 超过 50 ms 的帧占比；超过 100 ms 的帧占比 | ≤ 5%；≤ 0.5% | §6.4 实测 2.3–3.9%；0–0.2% |
| | 平均绘制点数（仅在 load < 6 时检查） | ≥ 20k | §6.4 实测 26.5–30.2k（负载约 6） |
| | t > 2 s 后的最大帧间隔 | ≤ 250 ms（排除 shader 首编译） | r12 §4.11；CAS 实测 100–133 ms，固定预算时出现过 333 ms |
| | 进入目标带的时间（1 s 滑窗 p50 ≤ 1.1·T*） | ≤ 2 s | CAS 实测 1.03–1.25 s |
| 控制器 | 60 s 内换档次数；10 s 内来回 | ≤ 2；0 | §6.2 |
| | B 反向次数 | ≤ 15 次/分钟 | §6.2 仿真 5 次/分钟；§6.4 实测 6–13 次/分钟（慢探测 +3% 与 tail 回退在主机噪声下交替）；AB 为 29–36 次/分钟 |
| 画质 | 12 个采样帧的平均空洞率（与全量参考渲染对比，同为 0.5 比例；仅在 load < 6 时检查） | ≤ 25% | §5.2：25k、`maxPx` 16 时 20.4%，15k 时 25.7% |
| 流式 | TTFP（metadata 返回到首帧有点） | ≤ 1.0 s | 00-index §3.5 |
| | 失败节点数 | 0 | — |
| | 驻留点数 | ≤ 1.5·B_hi + 根节点 | 驱逐规则 |
| CPU | 选择耗时 p95 | ≤ 0.5 ms | §3.2 实测 ≤ 0.17 ms |
| | 我方脚本的长任务（> 50 ms） | 0 | n05 CI 门禁 |
| 有效性 | 1 分钟 load average | 帧节奏阈值在任何负载下都执行，因为 CAS 用点数吸收负载，实测负载 12.8 时仍然达标；画质与点数阈值只在 load < 6 时执行 | §6.4；本机负载会使同一点数的耗时相差 2 倍 |

### 8.2 真 GPU（设计阈值；本机无法实测，必须在带 GPU 的 runner 上复测后固化）

| 类别 | 指标 | Tier B（集显，1080p，60 Hz，起步档 3） | Tier A（独显，1440p，60 或 144 Hz，起步档 4） |
|---|---|---|---|
| 帧节奏 | p50 | = 刷新周期（16.7 ms） | = 刷新周期 |
| | 超过 1.5T 的帧（掉帧）占比 | ≤ 5% | ≤ 2%（60 Hz）/ ≤ 4%（144 Hz） |
| | p99 | ≤ 2T | ≤ 2T |
| 控制器 | 60 s 内换档次数；10 s 内来回 | ≤ 2；0 | ≤ 1；0 |
| | B 反向次数 | ≤ 8 次/分钟 | ≤ 6 次/分钟 |
| 画质 | 静止帧空洞率（全分辨率） | ≤ 3% | ≤ 1% |
| | 静止 2 s 后的 `limitedBy` | 不为 budget 的帧 ≥ 70% | ∈ {error, headroom, complete} 的帧 ≥ 90% |
| | `achievedScreenError`（静止） | ≤ 档位 τ | ≤ 档位 τ（medium 为 1.35 px） |
| 流式 | TTFP | ≤ 700 ms | ≤ 500 ms |
| | 相机停止后收敛时间（没有待取节点且 `limitedBy ≠ budget`） | ≤ 4 s | ≤ 3 s |
| 资源 | GPU 内存（点池加纹理） | ≤ 256 MB | ≤ 512 MB |
| | 每帧 JS 耗时（选择、DrawTable、上传） | ≤ 2 ms | ≤ 2 ms |

仿真依据：§6.2 中 igpu 和 dgpu 的 CAS 行分别为掉帧 3.6–4.9% 与 1.1%，换档 ≤ 2 次、0 次来回，B 反向 1–8 次/分钟；§3.2 中 τ 受限区的空洞率为 0.8–1.2%。

### 8.3 CI 门禁（headless，不看绝对 fps）

1. 深圳 flight60 跑完无 `pageerror`，GL 错误为 0。
2. CAS 行为：
   - 换档 ≤ 2 次；
   - 没有 10 s 内来回；
   - B 反向 ≤ 15 次/分钟；
   - 最终档位为 0 或 1；
   - 2 s 内进入目标带（1 s 滑窗 p50 ≤ 1.1·T*，或 B = lo）。
3. 选择器单测（vitest，Node，使用同一份 `flight.bin`）：
   - 填充率 p10 ≥ 0.98（B ≤ 250k 时）；
   - τ 受限时 `limitedBy ≠ budget`；
   - 与 `g02/lod.mjs::selA(prefix=true, hyst=0.1)` 的输出逐帧一致（差分测试）。
4. 画质回读：12 个采样帧空洞率 ≤ 25%，与基线 PNG 的像素差不超过 2%。

---

## 9. 对 01-design 与 00-index 的修订建议

1. **00-index §3.5 PointCloudEngine**：
   - 选择器写明“APH（两级预算 + 首个被拒 required 节点前缀 + ±10% 迟滞）”。
   - RenderBackend 写明“PointPool + DrawTable（Tier B/S 用纹理，Tier A 用 storage），无属性 Points 一次 draw”。
   - 点径改为“全部 Lite + 自适应 maxPx”。
   - 控制器改为 CAS。
   - 阶梯表替换为 §7.2。
2. **§8 冲突表**：C14 按 §7.1 关闭；C18 改判为 O3d（理由见 §4.3）；C2 补充“τ 按档位取值，Tier S 为 4 或 3，并固定 0.5/0.6 渲染比例”。
3. **§9 G2 标记为已关闭**，产物是本文、`g02/` 下的基准代码，以及 flight60 脚本。
4. **01-design §14**（“远处 10K / 近处 1M”）改写为“APH 选择 + CAS 闭环 + 七级阶梯”；§37（刷新率）写入 §8 的分档阈值；§43/§44 的 MVP 验收引用 §8.1 与 §8.3。
5. **01-design §38 性能面板**加三项：`limitedBy`、`achievedScreenError`、CAS 当前档位与 B。换档时用 toast 说明原因，例如“帧间隔超过目标 20%，已降到 soft-min”。

---

## 10. 复现

```bash
cd /data/projs/anet-drone/.cache/research/g02
/data/projs/anet-drone/.venv/bin/python prep.py shenzhen newyork shanghai     # 建树、Q16 编码、flight60（每城约 11 s）
node sim.mjs shenzhen stream 30                                            # 选择器与流式仿真（输出 out/sim_*.json）
node ctrlsim.mjs shenzhen                                                  # 控制器闭环仿真（输出 out/ctrlsim_*.json）
FR=16 node run_pair.cjs shenzhen org|size|px                               # 配对 GPU 基准（SwiftShader）
node run_bench.cjs shenzhen quality|quality05|quality1b                    # 画质回读
ORG=O3m SIZE=N node run_live.cjs shenzhen                                  # 实时闭环（真实 rAF）
```

**局限**：
- 本机测试期间与其他单元共享 CPU，负载 3–17，绝对耗时只能看区间。
- Tier B/A 的阈值来自仿真，必须在真 GPU 上复测。
- 画质指标以“全量点云 + cut 点径（与 Lite 等价）、minPx 1”的参考渲染为真值，不能衡量感知上的锐度。
- §6.4 的实时闭环用的是 N 点径和 `maxPx` 8。按 §7.2 改用 Lite 和自适应 `maxPx` 16 后，每点开销约增加 21%（§5.2），CAS 会以约少 17% 的点数维持同样的节奏，这一推论没有单独实测。
- 流式模型没有模拟 HTTP/2 队头阻塞与 CDN。
- 测试数据的八叉树只有 5 层；更深的 COPC 树会让 cut 遍历更贵，但本文结论是改用 Lite，这一差异因此不影响结论。
