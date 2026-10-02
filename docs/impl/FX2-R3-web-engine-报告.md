# FX2-R3-web-engine 前端引擎与视口：验收与加固报告（第 3 轮修复）

| 项 | 内容 |
|---|---|
| 工作包 | FX2-R3-web-engine（修复区域 web-engine：`apps/web/src/{engine,viewport,net}` 与性能驱动） |
| 日期 | 2026-10-02（05:38–10:40） |
| 依据 | `docs/impl/D1-验收报告-第2轮.md`（§3、§4.2、§4.5、§5、§7）；INT-1 报告 §3、§7；AWR-03 §8.4、ADR-033、ADR-041、ADR-044、ADR-046、ADR-064；AWR-17 §6.9；AWR-18 §2.3、§2.4、§4.7、§5.2、§7.3、§8.5；M06、M07、M11、M12、M16 PRD；FX2-R2-web-engine 报告 |
| 本区域待修项 | D1-AC-09a（P0）、D1-AC-14（P1 子项 Tier A）、D1-AC-19（P0）、D1-AC-26（P0） |
| 环境 | 8 核 Xeon E5-2603 v4、无 GPU；Chrome for Testing 151（SwiftShader，C1；Tier A 用 C2）；Node 22.12；`.venv`。本阶段 gateway、sim、web-ui、other 修复包同时在本机运行，排他锁排队常见 5–20 min，持锁期间运行内 load 多在 5–10（其他包的 pytest、sim-core 不持锁） |
| 约束执行 | 没有安装依赖；没有使用 git；没有跑验收用的性能基准与 perf 标记用例。诊断与自测全部在排他锁 `runs/.perf.lock` 下（`.cache/fx2r3web/diag/runlock.sh`：取锁后等 1 分钟 load ≤ 4 最多 240 s，再 `taskset -c 2-6`，单次持锁上限 540 s）；构建与功能测试持共享锁；颜色只用 token（新代码中的颜色只有测试夹具与既有 SCENE token），无 emoji 与禁用字形，未引入 lucide-react 与 backdrop-filter，没有新增 UI 组件 |
| 结论 | 四项的根因都已定位。D1-AC-26 的"选中后帧率减半"来自实例化宽线在 SwiftShader 上按实例计费（选中轨迹与光晕 2 × 1024 个实例，每帧约 180 ms GPU 进程 CPU），t_sim 到像素钳在 300 ms 来自三处缺陷（焦点通道按帧时刻统计到达、跟随锁定从不启用焦点例外、ack 晚一个帧间隔触发 credit 窗口）；D1-AC-09a 的主因是点云下限释放前调度器在一串无效子步骤上逐个等 2 s；D1-AC-19 的长帧来自过渡中的调度器步骤与首个 Toast 的合成器编译，另有降水开始 2–3 帧与约 5–8 s 一帧未能消除；D1-AC-14 的 Tier A 功能矩阵已实现并通过。规格变更以 ADR-067 记录，没有放宽或冻结任何阈值 |

## 1 结论摘要

- **D1-AC-26（P0）**：
  1. 实例化宽线：轨迹三批（`TrailBatch`）与粗线（计划路径、zones 顶部轮廓）原用 `LineSegments2` + `Line2NodeMaterial`，`instanceCount` 恒为全部分配段数。块交替 A/B（FakeSource S1、`fixedB=20000`、调度器关闭）：选中一架机使平均帧间隔 47–56 → 98–157 ms、GPU 进程 CPU 每帧 +187 ms，`chrome=0` 下相同；隐藏轨迹根节点 −179 ms/帧，只把两批 `instanceCount` 改为 16 −177 ms/帧，把空段移到远处无变化。改为每段一个显式四边形的非实例化宽线（`engine/lines/quadLines.ts`）后，选中不再增加帧间隔。
  2. 焦点通道到达统计：rt.worker 每帧只交付每个 channel 的最新一条记录，M12 用主线程摄入时刻统计 60 Hz 通道，测到的是帧率（10 fps 时 D_focus 钳到 300 ms）。改为 Worker 侧统计（槽头新增 `selJitterMs`），D_focus 落到 60 ms 下限。
  3. 跟随锁定（L）从不调用 M12 `setFocus`，ADR-046 的焦点例外实际未生效，latency 用例的"跟随"测的是全局 D（约 205–245 ms）。已修正。
  4. ack 在主线程归还槽后才发出，多一个帧间隔，15–30 fps 时 60 Hz 选中机通道约一半 tick 被 credit 窗口跳过；改为槽交付时确认、合并间隔 34 ms。credit_skips 由 52–84% 降到 3.2–6.5%。
  5. 用例补齐命令到可见、关注集切换、×10 HOLD、选中机通道与 credit_skips；命令到可见的判据改为效果首次上屏（飞行状态字节含子状态，或控制 owner），与 AWR-18 §7.3 的预算一致。
  - 自测（live S1，生产构建，harness 单次）：t_sim 到像素 p95 133.6 ms（最后一次，阈值 150）、此前两次 194、224 ms；关注集切换不连续最大 0.009 m（20 次）；×10 HOLD 0%；选中机通道 60 Hz、为 rAF 的 2.7 倍；credit_skips 3.2–6.5%（阈值 1%，未达）；命令到可见 p95 135 ms（一次 PASS），另一次 851 ms（S1 剧本立即收回 hover，见第 7 节）。
- **D1-AC-09a（P0）**：live ladder n200 的飞行中块交替：B 20k → 10k 平均帧间隔 −7.9 ms、> 50 ms −9.5 个百分点、GPU −44 ms/帧；①–⑥ 全部可选图层降到底只有 GPU −6.5 ms/帧、帧间隔不变。整段对照：默认 16.4%/1.19%、关闭调度器 15.5%/1.31%、开局即降级终态 5.1%/0.07%、去掉 Toast 15.5%/1.19%。修复：变化不在屏上的子步骤与下一子步骤在同一次评估中串联执行（不再每步等 2 s），⑤ 环境与 ⑥ motion 补全可见性判断；加上非实例化宽线（关注集轨迹原为 4096 个实例）。自测（单次 flight60，生产构建）：4.81%/0.27%（p95 50、p99 83.3）、6.61%/0.20%、6.69%/0.55%，第 2 轮为 14.05%/1.08%；另两次在运行内 load 7.8 与 10.3 时为 11.26% 与 15.18%。最大间隔 300–667 ms 全部在 t = 1–3 s（UI 首次光栅，web-ui ADR-069 处理）。S1 整景同时受益：3.75%/0.25%、p95 50（第 2 轮 10.57%）。
- **D1-AC-19（P0）**：trace 显示 30 s 过渡内调度器走完 11 步（第 ⑤ 步把环境降到 Off），首个 Toast 帧有 18 次 `Compile/Link`、6 次 `warmUpGraphicsPipelineCache`（436 ms）；子步骤串联后可见步骤与 Toast 次数减少，首次 Toast 编译由 web-ui 的遮罩下预热（ADR-069）处理。关闭调度器后仍有：降水开始的 2–3 帧约 175 ms（关闭降水子层或提前显示过雷暴后消失，在着色器预热里画真实雨滴、填入典型雨参数都不能消除，原因未定位，相关试验性改动已撤回）、约 5–8 s 处一帧 300–470 ms（与降水无关）。预计本项仍超阈值，移交（第 7 节）。
- **D1-AC-14（P1 子项）**：回归页 `viewport/dev/featMatrixWgpu.ts` 在 C2 下自建 WebGPURenderer 跑 28 项 + PointPool，29 项与 g01 `F_feat_wgpu.json`、`P_pool.jsonl` 逐项相等；产品页 `?tier=A` 回退经典路径的冒烟（无 pageerror、`forced.tier = "A"`、calls 等于 pass 计划）通过。

## 2 定位过程与证据

诊断脚本与原始结果在 `.cache/fx2r3web/diag/`（不进仓库）：`f60.mjs`（单次 flight60 摘要与逐秒分布）、`ab.mjs`（同页面块交替 A/B，含 GPU 与渲染进程 CPU 时间/帧，`--flight` 为飞行中交替）、`live.mjs`（起 live 后端、等剧本标记后跑任务）、`env.mjs`（天气过渡时间线，可选 trace、预热、关闭降水）、`tr*.mjs`（trace 摘要）、`cpu.mjs`、`runlock.sh`；`g1`–`g3`、`s1`–`s14`、`e1`–`e11`、`v1`–`v9` 为各批结果。试验性开关（`?govx=off|fix:<ids>`、可变 `lockB`、`__vp.gx`）只存在于 `.cache` 下的临时副本，已删除，仓库源码中没有。以下数据除第 6 节外都是单次诊断运行，非判定。

### 2.1 选中态与宽线（D1-AC-26，FakeSource S1，`fixedB=20000`，调度器关闭，5 s 一块）

| 对照 | A | B | 配对差中位 | GPU 进程 CPU/帧 |
|---|---|---|---|---|
| 未选中 / 选中，UI 壳开 | 47–56 ms | 103–157 ms | +54.2 ms，> 50 ms +68 pp | 241–260 → 422–478 ms（+186.5） |
| 未选中 / 选中，`chrome=0` | 46–51 ms | 99–120 ms | +57.6 ms | +187.5 ms |
| 选中时视锥显示 / 隐藏 | — | — | −0.4 ms | +2 ms |
| 选中时轨迹显示 / 隐藏 | 101–107 ms | 47–50 ms | −54.2 ms | −178.6 ms |
| 选中时 hero 显示 / 隐藏 | — | — | −0.3 ms | 噪声内 |
| 选中轨迹两批 `instanceCount` 1024 / 16 | 103–109 ms | 47–50 ms | −58.5 ms | −176.8 ms |
| 空段坐标 0 / 1e9（实例数不变） | — | — | −8 ms（噪声） | −1.5 ms |

选中轨迹两批各 4 槽 × 256 段，只有 8 段有效（`instance` 检查脚本）。修复后同条件下"选中"不再增加帧间隔（−6.7 ms、GPU −4.5 ms，噪声内）；跟随（Third）与 Orbit 无差别（33–36 ms），隐藏点云时 23.6 ms。

### 2.2 n200（D1-AC-09a，live ladder-shenzhen n200，生产或测试构建）

| 对照（整段 flight60） | > 50 ms | > 100 ms | p95 | 说明 |
|---|---|---|---|---|
| 默认 | 16.38% | 1.19% | 66.7 | 调度器 17.6–38.1 s 走完 11 步 |
| 调度器关闭（B = 2 万） | 15.50% | 1.31% | 83.3 | — |
| 开局即降级终态（B = 1 万，可选图层全降） | 5.11% | 0.07% | 50 | — |
| 去掉降级事件（无 Toast） | 15.51% | 1.19% | 66.7 | 步骤更分散，结果相同 |

| 飞行中块交替（2.5 s 一块，配对差中位） | 平均帧间隔 | > 50 ms | GPU/帧 |
|---|---|---|---|
| B 2 万 → 1 万 | −7.9 ms | −9.5 pp | −44 ms |
| B 2 万 → 1.5 万 | −1.6 ms | −2.3 pp | −15.7 ms |
| ①–⑥ 可选图层全降（B 2 万） | 0 | −4 pp（噪声大） | −6.5 ms |

层配对（ladder n200，`fixedB=25000`）：drones +0.17、trails +3.13、environment −0.40、groundSky +5.33 ms，基底 21 ms。这一路径上多数降级子步骤对画面与成本都没有作用（无可见轨迹时的三级轨迹、未选中时的视锥、少于上限的标签与低模、晴天的环境视觉），而调度器对每个子步骤都等 2 s，点云下限要 33–47 s 才释放。串联后实测（`v1`–`v3`）的历史条目形如 `21.1:trails:1:hidden … env:1、motion:1、pc.floor:1`，点云下限在 19–32 s 释放，之后各秒 > 50 ms 帧多为 0–2 帧。

### 2.3 焦点时延与 credit（D1-AC-26，live S1，latency 用例）

| 阶段 | rAF | 选中机通道 | credit_skips/tick | D_focus | t_sim 到像素 p50 / p95 |
|---|---|---|---|---|---|
| 修复前（`v1`） | 8.8 Hz | 17.7 Hz | 84% | = D_global | — |
| 宽线修复后（`v2`、`v3`） | 18–20 Hz | 42–43 Hz | 58–62% | = D_global | —（用例在命令段失败） |
| + 判据与待检查机体（`v5`） | 22.7 Hz | 47.8 Hz | 51% | = D_global 244 ms | 249 / 300 ms |
| + 跟随锁定启用焦点例外（`v6`） | 21.8 Hz | 47.5 Hz | 52% | 76 ms | 79 / 223 ms |
| + ack 槽交付时发出（`v7`–`v9`） | 21.8–22.5 Hz | 60.0–60.5 Hz | 3.2–6.5% | 60 ms | 60 / 194、224、134 ms |

`v5` 的快照里 `focusLowLatency = false`、`dFocusMs = dGlobalMs`，由此查到跟随锁定不启用焦点例外（`CameraRig.setFollowLock` 不调用 `deps.setFocus`）。rt.worker 单测（FakeSource，60 Hz 订阅）：30 fps 拉取时 Worker 侧 selHz > 55、抖动 < 16.7 ms、跳过 0；20 fps 在新 ack 时机下跳过 ≤ 2 次/3 s；10 fps 时 credit 停顿表现为抖动（约 58 ms）。

### 2.4 命令到可见（D1-AC-26）

- sim 的 hover 是免租约的安全类命令，飞行状态保持 FLYING，只把子状态改为 HOVER（状态字节第 5–7 位），控制 owner 不变；S1 剧本随即取消该调用、收回控制（测试构建 live 调试：`accepted → running → canceled`，1 s 内）。原运动学判据（速度模 ≤ 0.3 m/s）在 6 m/s 巡航时只能测到减速时间或测不到。
- 判据改为状态字节或 owner 的切换在渲染时刻上屏；效果样本时刻一经观察即保留；同一机体的新命令使无效果的旧命令作废（否则新命令的效果记到旧命令上，`v7` 曾测得 8.2 s）。三次 hover：`v6` 138/123/93 ms，`v8` 88/135/119 ms（p95 − D_global < 0），`v9` 689/104/851 ms。

### 2.5 天气过渡（D1-AC-19，FakeSource S1，默认相机，`tier=S`，30 s）

| 条件 | > 100 ms | 长帧（s:ms） |
|---|---|---|
| 测试构建，调度器开（第 2 轮修复后） | 1.79–3.62% | 1.15:644、4.53–6.29 若干、8.33:374（调度器步骤 4.2–8.3 s：串联后 env:1、motion:1、pc.floor:1） |
| 调度器关闭，`chrome=0` | 0.96–1.56% | 0.2–1.6 s 一帧、4.6–8 s 一帧 300–470、13.6–16.5 s 两到三帧约 175–195（降水开始） |
| 同上 + 先显示 3 s 雷暴 | 0.76%（另一次被其他包负载淹没） | 降水开始处无长帧 |
| 同上 + 关闭降水子层 | 0.14–0.87% | 只剩约 8 s 一帧 323–468 |
| 同上 + 着色器预热画 512 / 4000 个真实雨滴、填入典型雨参数 | 1.09–2.40% | 降水开始处长帧仍在（试验已撤回） |
| 同上 + 过渡前闪现两帧雷暴 | 0.60% | 降水开始处长帧仍在 |

trace（含 `gpu.angle`）：首个降级 Toast 所在窗口有 `Compile/Link` × 18、`ShaderTranslateTask` × 10、`LinkTaskVk` × 6、`warmUpGraphicsPipelineCache` × 6（合成器首次光栅新 DOM）；降水开始与约 8 s 的长帧窗口里只有 GPU 进程等待 SwiftShader（`ContextVk::finishImpl`），没有编译或上传事件。DTM 纹理（185 × 200 R32F）在过渡前已上传（点云材质使用同一纹理）。

## 3 改动清单

所有者按 AWR-03 §4.3，全部在本区域（web-engine）内。

| 文件 | 所有者 | 改动 |
|---|---|---|
| `src/engine/lines/quadLines.ts`（新增） | M06 | 非实例化屏幕空间宽线：交错缓冲（9 float/顶点）与静态角点、顶点阶段投影与近平面裁剪、方头、无效段折叠、每帧线宽/绘制缓冲/近平面 uniform、按段区间增量上传 |
| `src/engine/drones/trails/TrailBatch.ts` | M06 | 改用 quadLines；槽步长 = 当前段数，绘制范围止于最后占用槽；脏区间无分配合并；`setView`、`warmBefore` |
| `src/engine/drones/DroneLayer.ts` | M06 | 每帧向三批轨迹传绘制缓冲尺寸与近平面 |
| `src/engine/mission/lineBatch.ts`、`MissionOverlay.ts`、`zones.ts` | M06 | `WideLineBatch` 改用 quadLines（虚线 3/6 m）；计划路径与 zones 顶部轮廓每帧 `setView` |
| `src/viewport/layers/zones.tsx`、`trails.tsx`、`mission.tsx` | M06 | zones 每帧视图更新任务；预热改 `warmBefore()` |
| `src/engine/perf/governor.ts` | M06 | 不可见子步骤串联（`maxChain`）、`lastVisible`、历史原因 `:hidden` |
| `src/engine/environment/EnvRuntime.ts`、`quality/EnvQuality.ts` | M07 | ⑤ 旋钮 `visible`：`lowVisualsPresent()` |
| `src/viewport/hostRuntime.ts` | M06 | ⑥ motion 旋钮只在 UI 有运行中的动画时可见 |
| `src/engine/camera/CameraRig.ts` | M06 | 跟随锁定的开、关、平移解除、模式切换与焦点丢失同步 `setFocus` |
| `src/net/rt/decode.ts`、`frame.ts`、`types.ts`、`session.ts` | M11 | Worker 侧 60 Hz 通道到达抖动 `selJitterMs`（槽头 228）；ack 在槽交付时发出，`ACK_MAX_GAP_MS` 50 → 34 |
| `src/engine/time/delay.ts`、`register.ts` | M12 | 焦点通道 hz 与抖动取槽头 Worker 统计（`ArrivalStats.setExternal`） |
| `src/engine/time/interpRing.ts` | M12 | 控制字节 `ctrl`、状态或控制切换的样本时刻 `stateSince`，`stateSinceMs`、`ctrlOf` |
| `src/engine/perf/latency.ts`、`src/engine/drones/index.ts` | M06 | 命令到可见的效果判据、效果时刻保留、同机新命令作废旧命令、待闭合命令的机体每帧检查 |
| `src/viewport/dev/featMatrix.ts`、`featMatrixWgpu.ts`（新增） | M06 | Tier A 分派与 WebGPURenderer 版 28 项 + PointPool |
| `perf/latency.spec.ts` | M16 用例（D1-AC-26，本区域负责项） | 四段：跟随 30 s、hover × 3、关注集切换 × 5（Third + 清除选择 + Home）、×10 20 s；写 metrics.json |
| `perf/governor.spec.ts`、`perf/layers.spec.ts` | M16 用例 | governor：历史时间单位改为秒、可见步骤判间隔；layers：URL 带 `n` |
| `perf/harness/cases/flight60.mjs`、`ui.mjs`、`perf/thresholds.json` | M16 用例 | layers 改 ladder n200；latency 增四个 script 指标；阈值键 `focus_jump_m`、`sel_hz_over_raf`、`credit_skips_sel_pct`（数值即 D1-AC-26 原文） |
| `perf/m06/feat-matrix.spec.ts`、`featMatrix.expected.ts`、`common.ts` | M06 | Tier A 用例（自起 C2 浏览器）与 `WGPU` 预期 |
| `tests/m06/trailBatch.browser.test.ts`（新增）、`trails.test.ts`、`zones.test.ts`、`latency.test.ts`、`camera-modes.test.ts`、`tests/net/codec.test.ts`、`tests/time/delay.test.ts`、`tests/time/helpers.ts`、`tests/perf/governor.unit.test.ts` | M06、M11、M12 | 新布局与新行为的断言 |

文档：`docs/03`（ADR-067、§7.0 索引、§3 模块表"轨迹"）、`docs/17`（§0 第 3 条、§6.9 L1、时序图、计时器表）、`docs/18`（§4.7 施压、恢复两行）、`docs/15`（轨迹实现说明）、`docs/modules/M06`（FR-042、FR-049、FR-076、§6.10 GPU 批次/线宽与材质/任务叠加/zones、§6.16 判据、M06-AC-011、依赖表）、`M07`（FR-043）、`M11`（§6.3.8 槽头、FR-091、AC-014、计时器与时序）、`M12`（FR-005、§6.3 焦点）、`M16`（latency、layers 用例行）。

## 4 本区域验收项逐条结果

"自测"为排他锁下的单次运行（同机其他包运行中）；最终判定以验收阶段 3 次中位为准。

| 编号 | 第 2 轮 | 根因 | 处理 | 自测 | 预期状态 |
|---|---|---|---|---|---|
| D1-AC-09a | 14.05% / 1.08% / 最大 617 ms | 点云下限释放前的无效子步骤逐个等 2 s；关注集轨迹 4096 个实例 | 不可见子步骤串联；⑤⑥ 可见性；非实例化宽线 | 4.81%/0.27%、6.61%/0.20%、6.69%/0.55%（高负载两次 11.26%、15.18%）；p95 50–66.7、p99 83.3；最大 300–667 ms（t = 1–3 s） | > 50 ms 与 > 100 ms 预计通过；最大间隔取决于 web-ui 的揭开前预光栅（ADR-069） |
| D1-AC-14 | Tier A 用例不存在 | 未实现 | WebGPU 版功能矩阵与回退冒烟 | 29 项相等，用例通过 | 通过（P1） |
| D1-AC-19 | > 100 ms 1.55% | 过渡中的调度器步骤与首个 Toast 编译；降水开始与约 8 s 的单帧首次成本 | 串联减少可见步骤；Toast 预热归 web-ui | 调度器开 1.79–3.62%（测试构建、FakeSource、同机高负载） | 预计仍不通过（第 7 节第 1 条） |
| D1-AC-26 | t_sim p95 300 ms；credit_skips 与发送数同量级；三子项未测 | 实例化宽线；焦点到达统计时基；跟随锁定无焦点例外；ack 晚一帧 | 第 1 节 1–5 | t_sim p95 134/194/224 ms；关注集 0.009 m；×10 HOLD 0；选中通道 2.7 × rAF；credit_skips 3.2–6.5%；命令到可见 p95 135 ms（另一次 851 ms） | t_sim、关注集、×10、通道频率预计通过；credit_skips 未达 1%；命令到可见受 S1 剧本收回控制影响（第 7 节第 2、3 条） |

S1 整景（D1-AC-03b）同时受益：3.75%/0.25%、p95 50、p99 66.7、最大 333 ms（t = 2.x s）。

## 5 规格变更与阈值

- **ADR-067**（`docs/03` 附录 E）：非实例化宽线；不可见子步骤串联与 ⑤⑥ 可见性；D_focus 用 Worker 侧到达统计；跟随锁定启用焦点例外；ack 在槽交付时发出、合并间隔 34 ms（AWR-17 §6.9 L1，服务端 W 公式不变）；命令到可见判据；layers 与 latency 用例落实；Tier A 功能矩阵的 D1 验收形式。降级顺序、间隔、点云下限、阈值、点径与预算带都不变。
- **阈值**：没有放宽，也没有冻结。新登记的三个阈值键数值即 D1-AC-26 原文（≤ 0.5 m、≥ rAF、≤ 1%）。本包的测量都在其他修复包同时运行的机器上（运行内 load 5–10），不适合作为冻结依据；建议验收阶段按 AWR-18 §5.3：若 D1-AC-09a 在独占条件下 3 次中位满足暂定值即按暂定值冻结；D1-AC-26 的 credit_skips ≤ 1% 只在跟随视角帧率 ≥ 约 30 fps 时可达（第 7 节第 2 条），届时若仍不可达，需以 ADR 把口径改为"按客户端拉取节奏计"或调整阈值，并附数据。

## 6 测试

| 范围 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `npx tsc -p tsconfig.json --noEmit` | 通过 |
| oxlint | 改动路径 `npx oxlint --type-aware …`；`make lint` 中前端规则 | 通过 |
| vitest unit | `vitest run --project unit tests/m06 tests/perf tests/pointcloud tests/environment tests/mission tests/net tests/sensors tests/time tests/geo tests/m11 tests/m16 tests/contracts tests/fixtures tests/m15` | 930 通过、1 跳过（新增：Worker 侧到达统计 30/20/10 fps、D_focus 用 Worker 统计、命令效果判据四种情形、跟随锁定焦点例外、宽线新布局、串联子步骤） |
| vitest browser | `vitest run --project browser tests/m06 tests/pointcloud tests/environment tests/fixtures/env.browser.test.ts` | 26 通过（新增 `trailBatch.browser.test.ts`：线宽、绘制范围、空槽、近平面裁剪与相机后段） |
| Playwright M06 | `playwright test --config perf/m06/playwright.m06.config.ts`（测试构建，共享锁） | 17 通过、1 跳过（`prod.spec` 需要生产构建，按设计跳过）；含 Tier S/B/A 功能矩阵与 pass 计划、warmup（揭开后程序数不增）、layout、smoke、drone-tiers、frustum-live |
| Playwright M07 | `playwright test --project=perf perf/m07`（测试构建） | 5 通过（env-switch 功能、env-visual、env-fixed-time、env-gpu S/B） |
| harness 注册表 | `node perf/harness/run.mjs --check` | 106 个用例通过校验 |
| make lint | `make lint` | 前端与通用规则（oxlint type-aware、no-emoji、no-hex、lint-lf、motion、icons、no-raw-controls、brand、deps、units、perf flags、thresholds）全部通过；失败项只有 `check_py_imports` 的 4 条，均在 sim 区域同期修改中的 `python/awr/sim/runtime/main.py`（M08 直接 import `awr.sim.mission`），本包没有修改任何 Python 文件 |
| 性能自测 | harness `--case latency --runs 1`（`runs/perf/fx2r3-web-v4`…`v9`）；`live.mjs` 单次 flight60 n200 与 S1 | 见第 1、2、4 节；未跑验收用的 3 次中位 |

## 7 遗留问题与建议

| 编号 | 问题 | 区域 | 建议 |
|---|---|---|---|
| 1 | D1-AC-19：降水开始的 2–3 帧（约 175 ms）与约 5–8 s 处的一帧（300–470 ms）仍在；关闭降水子层或先显示过一次雷暴后前者消失，着色器预热里画真实雨滴、填入典型参数、闪现两帧雷暴都不能消除，trace 中只有 SwiftShader 执行等待 | web-engine（M07） | 下一步用 SwiftShader 的 `SWIFTSHADER_*` 诊断或逐项关闭雨滴着色器中的项（DTM 采样、透射率、opticalDepth）二分；若属 SwiftShader 首次执行开销，可在揭开前用真实环境参数离屏画几帧降水与云（需要 M07 给出"按预设求一组可视参数"的入口）。约 8 s 的一帧需按路线分段（7.5 s 一段）检查云覆盖越过阈值时启用的分支 |
| 2 | credit_skips 在跟随视角约 22 fps 时为 3–7%（阈值 1%）：每帧一次拉取、W = 6，帧间隔超过约 80 ms 时 60 Hz 通道必然跳过 | web-engine、gateway | 先解决跟随视角帧率（S1 塔近景点云近处点大；跟随锁定段 rAF 约 22 Hz，Orbit 默认视角 30 fps）；或由 gateway 把 W 的 `ack_interval` 取实测 ack 间隔（AWR-17 §6.9 允许每 1 s 重算），属 M11 服务端，未改 |
| 3 | 命令到可见：S1 剧本在 1 s 内取消操作员 hover 并收回控制，有时子状态切换不上屏，只能等后续状态变化（689、851 ms） | M16、sim | latency 用例的命令段改在一个不被剧本收回控制的机体上（例如 S1 结束后或 free 场景中悬停机的 goto），或由 sim 保证被取消的调用至少发布一帧效果 |
| 4 | 整景揭开后 t = 1–3 s 的 300–667 ms 尖峰（D1-AC-03b、09a 的最大间隔） | web-ui | ADR-069 的遮罩下预光栅阶段已在实施，验收时复测 |
| 5 | 产品 Tier A 后端（M06-FR-012，WebGPURenderer + quad 点径）仍未实现，`?tier=A` 回退经典路径 | web-engine | D1-MS6；功能矩阵已可作为其回归基线 |
| 6 | `tools/bench/ipc/rt_client.mjs` 的 ack 合并仍为 50 ms（工具客户端） | gateway | 如需与浏览器一致，同步改为槽交付语义与 34 ms |
| 7 | WebGPU 回归页 `direct_contextNode_encoding` 的两次画布读取在 headless SwiftShader WebGPU 上为透明黑（g01 记录相同），只能断言该现象 | web-engine | 有真 GPU runner 时改为 `readRenderTargetPixelsAsync` 读帧缓冲目标 |
