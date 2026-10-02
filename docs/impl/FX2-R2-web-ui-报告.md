# FX2-R2-web-ui 报告（验收与加固第 2 轮，修复区域 web-ui）

| 项 | 内容 |
|---|---|
| 工作包 | FX2-R2-web-ui：D1-AC-23（UI 开销，P1）根因定位与修复 |
| 区域 | web-ui：`apps/web/src/{ui,app,lib,styles,stores}`；本包另改了 M15 自己的 Playwright 用例 `apps/web/perf/m15/fxweb2.spec.ts` 与 M15 单元、浏览器测试 |
| 日期 | 2026-10-01 |
| 依据 | `docs/impl/D1-验收报告-第1轮.md` §3（D1-AC-23 行）、§4.2；`docs/impl/INT-1-集成报告.md` §3、§7；AWR-03 §1.3、§8.4、ADR-029、ADR-033；AWR-18 §3、§6、§8.6、§9.5；M15 PRD FR-024、FR-034、FR-072、NFR-001 |
| 环境 | 8 核 Xeon E5-2603 v4、无 GPU；Chrome for Testing 151（SwiftShader，C1 标志）；Node 22.12；`.venv` Python 3.12；六城 `worlds/` |
| 约束执行 | 没有安装依赖；没有使用 git；所有性能测量都在排他锁 `runs/.perf.lock` 下、开跑前 load ≤ 4、`taskset -c 2-6`，单次持锁 < 10 min；功能测试在共享锁下运行，不与他人的排他测量重叠；规格变更以 ADR-066 记录（ADR-064 由 web-engine 使用，ADR-065 已由 gateway 的代码引用，故取 066） |

## 1 结论摘要

- **根因**（第 2 节）：UI 壳的代价不在我方 JS（主线程每帧中位 1.6 ms），而在 GPU 进程。Tier S 上 GPU 进程主线程 100% 忙，几乎全部在 `SwapBuffers`
  （等待 SwiftShader 执行 WebGL、GPU 栅格与合成）。UI 壳在 GPU 进程上的成本来自四处：(1) 4 Hz 的文本与画布改动落在 1280 px 宽的 header、timeline 条与
  被挤压的整屏浮层里，每次整条重栅格，带栅格的帧比不带的多 20–50 ms；(2) 五个周期发布者各有 4 Hz 相位，约一半的帧带改动；(3) 浮层被挤压成整屏层、
  DroneRail 的 mask 每帧多一个离屏 render pass；(4) 收起的栏仍在重绘根层。
- **修复**（第 3 节）：栅格岛、表面分层、共享 UI 节拍、无 mask 渐隐、收起栏不绘制、DroneRail 高度受限（虚拟列表生效），另有三处小的 tick 帧减负。
  规格层面以 ADR-066 记录约束，阈值不变。
- **效果**：
  - 同一引擎快照、FakeSource、20 s 预热后 12 s 窗口、交替 2 轮：修复前 UI 壳开比关 均值间隔 +14–19 ms、> 50 ms +19–25 个百分点、GPU 进程 CPU/帧 +65–85 ms；
    修复后 均值 +0.8–3.4 ms、> 50 ms −2.0 至 +2.7 个百分点、p50 相同（33.3 ms），GPU 进程 CPU/帧 +7–18 ms。
  - 门禁用例自测（live S1，`perf/harness/run.mjs --case ui-overhead`，60 s × 交替 3 次）：第 1 次（第 3.1–3.6 节改动）p50 差 0 ms（通过），
    > 50 ms 差 +4.4 个百分点（关 7.2%、开 11.7%；第 1 轮为 +10.3，开模式绝对值由 77–87% 降到 9–12%）。第 2 次（加第 3.7 节）运行内 load 最大 13.4、
    均值 9.8（锁外负载），harness 判 WARN，数值（p50 −16.7 ms、−1.8 个百分点）不作判定，见 6.2。
  - **D1-AC-23 的 p50 子项已满足；> 50 ms 增量 ≤ 1 个百分点仍未达到**（剩余约 3–4 个百分点，构成与建议见第 7 节）。本包没有放宽阈值。
- 连带修复：DroneRail 虚拟列表此前没有生效（列表视口随行数增高，第 1 轮诊断 N = 1000 多渲染 989 行，D1-AC-27 子项）；reduced 档 Progress 的过渡
  （D1-AC-20"reduced 档 Base UI 部件无动画"）；`perf/m15/fxweb2.spec.ts` 时间轴悬停用例在负载下偶发失败。

## 2 根因定位

### 2.1 方法

全部诊断脚本在 scratchpad（不入库）：生产构建输出到临时目录（不动共享 `apps/web/dist`），静态服务 + 浏览器内 FakeSource 或 harness 的 live 后端；
Chrome trace（`devtools.timeline`、`gpu`、`viz`、`cc`、`invalidationTracking`）；CDP LayerTree 层清单（尺寸、paintCount、合成原因）；
注入 CSS/JS 的消融（隐藏、冻结或提升某一部分）；`/proc` 统计 GPU 进程与渲染进程的 CPU 时间并除以帧数（比帧间隔更稳定，帧间隔量化到 16.7 ms 档）；
未压缩构建 + 最小 React devtools hook 统计每次提交的渲染根；MutationObserver 统计 DOM 写入。为在同一引擎快照上比较，另写了一个把本包改动
逆向还原的副本（current base）。

### 2.2 发现（修复前，`scene=full`，FakeSource 两架，深圳 flight60）

| # | 现象 | 证据 | 结论 |
|---|---|---|---|
| 1 | GPU 进程是瓶颈 | 8 s trace：CrGpuMain 忙 977 ms/s，其中 `SwapBuffers` 909 ms/s；关壳时 `SwapBuffers` 中位 61 ms、开壳 80 ms；渲染进程主线程 294 ms/s | 帧时间由 SwiftShader 的 GPU 工作量决定，主线程不是瓶颈 |
| 2 | 静态壳很便宜，活动壳很贵 | 把整个壳克隆成静态 DOM（几何与层结构相同、无更新）：均值间隔 73.5 ms，关壳 67.4，活动壳 96；同样尺寸的静态覆盖块 72.1 | 主要代价来自"更新"而不是"存在" |
| 3 | 更新的代价在 GPU 栅格 | 前一帧有栅格任务的 `SwapBuffers` 平均 108.7 ms，无栅格 85.5 ms；按层：header（1280 × 44）+20–37 ms，timeline 条（1280 × 48）+29–50 ms，挤压浮层 +6–11 ms；8 s 内栅格 timeline 41 次、header 27 次、浮层 34 次 | 4 Hz 文本与播放头画布每次重栅格整条宽层 |
| 4 | 相位分散 | fleet、timeline、perf、C 类文本、LfScheduler 各自 `fps: 4`（各自的 `last`）；HUD KPI 经 rAF 延后一帧写；时间轴画布在 React 提交后的下一帧才画 | 约一半的帧带栅格 |
| 5 | 提升动态区域即可消除大部分 | 只用 CSS 把时间轴区、读数、header 数字、HUD 数字、DroneRail 列表提升为层：均值 70.8 ms（≈ 静态克隆 71.8） | 栅格岛方向正确 |
| 6 | 整屏挤压层 | 层清单中 `app-layer-labels` 为 1280 × 720 且绘制内容（header、timeline、浮层被挤压进来）；GPU 栅格瓦片整宽 | 每帧混合整宽瓦片 |
| 7 | mask + 合成子层 = 每帧 render pass | DroneRail 行成岛后每帧 render pass 由 2 变 3；去掉 `scroll-fade-y` 的 mask：GPU/帧 222/212 → 200/203 ms（= 关壳水平） | 含合成子层的容器不能用 mask |
| 8 | 收起的栏仍在绘制 | 收起的左栏、Dock 只是 `opacity: 0`：其中事件表、时间轴面板 Slider、进度条宽度过渡在 8 s 内使根层重绘 118 次 | 不可见也要不绘制 |
| 9 | DroneRail 虚拟化没有生效 | 列表视口 `clientHeight == scrollHeight`，由外层 SidebarContent 滚动；N = 30 渲染 30 行，N = 200 渲染 200 行 | 行岛数会随 N 增长，必须先修 |

## 3 修复

| # | 修复 | 文件 | 单项效果（同快照诊断） |
|---|---|---|---|
| 3.1 | **栅格岛**：`[data-island] { will-change: transform }`，`span[data-island]` 改 inline-block；`BoundText`、`MotionNumber` 默认成岛（`island={false}` 由外层成岛）；PerfHud、时间轴读数、时间轴轨道区、DroneRail 每行遥测行；`.lf-canvas`、`.lf-svg` | `styles/layout.css`、`styles/lf.css`、`ui/motion/BoundText.tsx`、`ui/motion/MotionNumber.tsx`、`ui/hud/PerfHud.tsx`、`ui/layout/TimelineBar.tsx`、`ui/layout/TimelineTrackArea.tsx`、`ui/panels/drones/DronesPanel.tsx` | 带栅格的帧 `SwapBuffers` 不再高于无栅格的帧（中位 37–40 对 44–46 ms） |
| 3.2 | **共享 UI 节拍** `stores/uiTick.ts`：overlay 首个任务判定节拍帧，周期 = `--telemetry-text-interval`（S 250 ms、B/A 100 ms）；fleet、timeline、perf、sensors 摘要、C 类文本、Tier S 的 LfScheduler 都以 `uiTickDue(ctx)` 代替自带 `fps`（注册 id、档位与 perf 图层不变）；HUD KPI 在 store 写入时同步写 | `stores/uiTick.ts`（新）、`stores/fleet.ts`、`stores/timeline.ts`、`stores/perf.ts`、`stores/sensors.ts`、`ui/motion/bindText.ts`、`ui/lf/scheduler.ts`、`ui/hud/PerfHud.tsx` | 改动集中到 4 Hz 的节拍帧，其余帧只有 WebGL 与合成 |
| 3.3 | **表面分层**：`.app-layer-header`、`.timeline-bar`、`[data-anchor]`、打开栏的 `sidebar-inner` 各自成层 | `styles/layout.css` | 已绘制层面积 4060 → 3193 千像素（整屏挤压层不再绘制），GPU/帧 193–199 → 177 ms |
| 3.4 | **无 mask 渐隐** `.fade-scroll-y`：两个覆盖渐变伪元素（栏底色到透明），命名滚动时间线驱动，只在有内容被卷走的一侧出现；DroneRail 列表改用它 | `styles/layout.css`、`ui/panels/drones/DronesPanel.tsx` | 每帧 render pass 3 → 2，GPU/帧回到关壳水平 |
| 3.5 | **收起栏不绘制**：`[data-slot=rail-host][data-inert] { visibility: hidden }`（inert 只在关闭过渡结束后出现）；reduced/off 档 `progress-indicator` 过渡为 0 | `styles/layout.css`、`styles/motion/tiers.css` | 根层重绘 118 → 36 次/8 s |
| 3.6 | **DroneRail 高度受限**：页栈单元格 `grid-rows-[minmax(0,1fr)]`，列表 `PanelBody fill` | `ui/layout/DroneRail.tsx`、`ui/panels/PanelHost.tsx` | N = 200：列表视口 512 px、渲染 25 行（原 200 行）；截图核对渐隐与滚动正常 |
| 3.7 | **节拍帧减负**：`MotionNumber` 复用每位数字的 span，只改变化的字符（原来每次清空重建）；`SwapText` 进入动画结束后取消 fill（不再常驻合成层）；直播模式下时间轴 Slider（禁用，只承担键盘与读屏）的 value/min/max 取整秒，拇指位置在节拍之间不变，不再每拍强制布局与二次提交 | `ui/motion/MotionNumber.tsx`、`ui/motion/SwapText.tsx`、`ui/layout/TimelineTrackArea.tsx` | 见 6.2 |

视觉核对：修复前后同一机位截图（FakeSource 30 架与 200 架、列表滚动前后）除数值外一致；岛不改变版式（Badge、行与 HUD 尺寸不变）。

## 4 改动文件

- 新增：`apps/web/src/stores/uiTick.ts`、`apps/web/tests/m15/uiTick.test.ts`、本报告。
- 修改（web-ui 区域）：`apps/web/src/styles/layout.css`、`styles/lf.css`、`styles/motion/tiers.css`；`stores/fleet.ts`、`stores/timeline.ts`、`stores/perf.ts`、
  `stores/sensors.ts`；`ui/motion/BoundText.tsx`、`ui/motion/MotionNumber.tsx`、`ui/motion/SwapText.tsx`、`ui/motion/bindText.ts`；`ui/lf/scheduler.ts`；
  `ui/hud/PerfHud.tsx`；`ui/layout/TimelineBar.tsx`、`ui/layout/TimelineTrackArea.tsx`、`ui/layout/DroneRail.tsx`；`ui/panels/drones/DronesPanel.tsx`、
  `ui/panels/PanelHost.tsx`。
- 测试：`apps/web/tests/m15/motion-dom.browser.test.tsx`（岛属性与样式）、`apps/web/tests/m15/uiTick.test.ts`（S 4 Hz、B 10 Hz、同帧、时钟回跳）、
  `apps/web/perf/m15/fxweb2.spec.ts`（悬停按标记当前位置重试，消除直播视图漂移造成的偶发失败）。
- 文档：`docs/03-设计基线与决策记录.md`（ADR 索引表与附录 E ADR-066）、`docs/18-性能与测试方案.md`（§6.1 第 5、6 条）、
  `docs/modules/M15-前端UI壳与设计体系组件PRD.md`（FR-024、FR-034、FR-072、NFR-001）。

## 5 规格变更

- **ADR-066**（03 附录 E）：Tier S UI 壳的合成与栅格约束——栅格岛、表面分层、共享 UI 节拍、无 mask 渐隐、收起栏不绘制、DroneRail 高度受限。
  是 ADR-029（Tier S 文本 ≤ 4 Hz、只写 transform）在 SwiftShader 合成模型下的实现约束；**不改变任何阈值**（D1-AC-23 仍为 p50 不变、> 50 ms 增量 ≤ 1 个百分点）。
- AWR-18 §6.1 新增第 5 条（GPU 进程计价、栅格岛、表面分层、禁止 mask 覆盖合成子层、不可见即不绘制）与第 6 条（共享 UI 节拍，新增周期性 UI 写入必须挂节拍）。
- M15 PRD：FR-024（列表高度受限、每行一个岛、覆盖渐变）、FR-034（节拍与岛）、FR-072（Tier S 只在节拍帧重画、图表都是岛）、NFR-001（实现约束引用）。

## 6 测试与自测

### 6.1 功能

| 项 | 结果 |
|---|---|
| Vitest unit（全量） | 922 通过、1 跳过（含新 `uiTick.test.ts`；最后一次在全部改动之后）|
| Vitest browser（全量） | 11 文件、37 通过（含新岛用例）|
| Playwright M15（`perf/m15/` smoke、interaction、layout-probe、responsive、fxweb2、report；测试构建） | 25 通过（全部改动之后复跑）|
| Playwright e2e（motion、a11y、brand；生产构建、FakeSource） | 4 通过，sanitize 跳过（需后端）|
| `tsc --noEmit` | 通过 |
| `make lint` | Web 侧全部通过（oxlint type-aware、no-emoji、no-hex、lint-lf、motion-lint、check-icons、no-raw-controls、check-brand、check-deps、check-units、check-perf-flags、check-thresholds、m06-lint）；Python 侧失败 4 + 3 处均在 sim 区域（`python/awr/sim/fleet/kernels_l1.py`、`kernels_watch.py`、`stages/tap.py` 的 ruff，`python/awr/sim/runtime/warm.py` 的包边界），是 sim 工作包进行中的改动，不在本区域 |

`fxweb2.spec.ts` 的时间轴悬停用例在一次满载批量中失败过一次：直播视图随时钟以约 25 px/s 漂移，指针到达时标记已偏出 6 px 命中容差。修复前后的构建
单独与整文件运行都通过，判定为既有的时序脆弱；已改为按标记的当前位置重试悬停。

### 6.2 性能自测（排他锁，诊断，非判定）

| 运行 | 条件 | p50 关/开（ms） | > 50 ms 关/开（%） | 差 |
|---|---|---|---|---|
| 第 1 轮验收（参考） | live S1，60 s × 3 | 66.7 / 83.3 | 73.5 / 83.5（中位） | p50 +16.7、+10.3 个百分点 |
| 同快照对照（未改 UI） | FakeSource，12 s × 2 | 33.3–50 / 50 | 12.5、12.9 / 37.4、31.9 | 均值 +14–19 ms |
| 本包（3.1–3.6） | FakeSource，12 s × 2 | 33.3 / 33.3 | 8.5、5.3 / 6.5、8.0 | 均值 +0.8–3.4 ms |
| 门禁自测 1（3.1–3.6） | live S1，60 s × 3（harness） | 33.3 / 33.3 | 7.2、6.0、10.3 / 12.2、9.3、11.7 | p50 0（通过），+4.4 个百分点（不通过）|
| 门禁自测 2（3.1–3.7） | live S1，60 s × 3（harness）；运行内 load 最大 13.4、均值 9.8（其他工作包在锁外运行），harness 判 WARN | 50、50、50 / 50、33.3、33.3 | 13.9、13.6、10.6 / 34.8、11.6、11.9 | p50 −16.7，−1.8 个百分点；负载超出协议（> 12.8），不作判定 |

门禁自测数据：`runs/perf/p20261001-210000-fx2r2webui/ui-overhead/`（第 1 次）、`runs/perf/p20261001-220000-fx2r2webui/ui-overhead/`（第 2 次）。

## 7 遗留问题与建议

1. **D1-AC-23 剩余约 3–4 个百分点**（门禁自测 1）。剩余构成（live 与 FakeSource 消融）：
   - GPU 进程 CPU/帧仍高约 7%（+10–13 ms CPU）：开壳 26–27 个合成层对关壳 4 个，每帧多出的小四边形与节拍帧的岛栅格与画布上传；
   - 渲染进程主线程每帧多约 5 ms：其中 **ViewCube 每帧写 3D 变换**（`viewport/overlay/ViewCube.tsx`，M06，web-engine 区域）在每帧触发样式重算、
     预绘制与分层；隐藏 ViewCube 时渲染进程 −1.2 至 −2 ms/帧、GPU −2 至 −4 ms CPU/帧。web-engine 已让 `?chrome=0` 不挂载 ViewCube，
     因此它现在全部计入 UI 开销。建议 M06：Tier S 下 ViewCube 只在相机朝向变化超过可见阈值时写，或挂到共享 UI 节拍（`stores/uiTick.ts`；
     engine 不得 import stores，可由 `viewport/` 侧读取）；
   - 节拍帧的 `:has()` 失效：shadcn Badge（`has-data-[icon=...]`）与 sidebar wrapper（`has-data-[variant=inset]`）在内部文本变化时重算祖先样式；
   - 门禁窗口从揭开遮罩后 1 s 开始，包含 PerfGovernor 的收敛期（开壳时 Governor 步进与恢复更频繁，每步一条 Toast），开壳前 20 s 的长帧占比
     12–21%，关壳 4–13%。
2. **测量噪声**：同一构建关壳三次的 > 50 ms 为 6.0–10.3%（极差 4.3 个百分点），1 个百分点的阈值小于"中位数之差"的噪声。建议 M16 在验收前
   做一次 A/A（关对关）测得噪声底，并考虑 18 §5.2 第 2 条那样的同页块交替（同一页面内用开关切换壳，避免每块重新加载、Governor 从零收敛）。
   本包没有修改阈值或用例口径。
3. `apps/web/perf/thresholds.json` 的 `over50_delta_pct` 仍是 2 个百分点（第 1 轮验收已指出 D1-AC-23 应为 1 个百分点，用例断言本身用 1）；
   `p50_delta_ms` 0.5 与"同一呈现档"等价。属 M16。
4. D1-AC-27 渲染行数：虚拟列表现在生效，但 overscan 10 是两侧各 10 行，列表滚到中部时渲染行 = 可见 + 20（列表顶部为可见 + 10）。
   若该条按任意滚动位置判定，需把 `LAYOUT.railOverscan` 改为 5 或明确口径（M15/M16）。
5. `__perf.ui.charts.svgHzMax` 目前把非流式的画布任务（时间轴轨道，4 Hz）也计为"SVG 图"，开壳时恒为 4.0；而 React 渲染的 SVG 图（HUD 刻度表）
   不经 LfScheduler，没有被统计。PERF-AC-022 的"SVG ≤ 2 Hz"口径需要修正（只统计 SVG 元素，并给 React 渲染的 SVG 图加计数），本包没有改动该指标。
6. 收起的栏现在不绘制，但 React 仍为其中面板（事件表、时间轴面板等）在节拍时提交，样式与布局仍在进行；若需要进一步减负，可在关闭过渡结束后
   用 React `Activity` 的 hidden 模式挂起这些面板（会卸载其 effect，需要评估各面板的订阅与恢复）。
