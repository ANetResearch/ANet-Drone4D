# FX2-R3-web-ui 报告（验收与加固第 3 轮，修复区域 web-ui）

| 项 | 内容 |
|---|---|
| 工作包 | FX2-R3-web-ui：第 2 轮验收归到 web-ui 的 D1-AC-03b、04、23、25、27 的根因定位与修复 |
| 区域 | web-ui：`apps/web/src/{ui,app,lib,styles,stores}`；另改了本区域的单元与浏览器测试（`apps/web/tests/m15/`） |
| 日期 | 2026-10-02 |
| 依据 | `docs/impl/D1-验收报告-第2轮.md` §3、§4.3、§5；`docs/impl/INT-1-集成报告.md` §3、§7；`docs/impl/FX2-R2-web-ui-报告.md` 第 7 节；`docs/impl/FX2-R2-web-engine-报告.md` 2.4；AWR-03 §1.3、§4.3、§8.4、ADR-033、ADR-066；AWR-18 §3、§6；AWR-14 §11.4；M15 PRD FR-006、FR-024、FR-036、FR-089、NFR-005 |
| 环境 | 8 核 Xeon E5-2603 v4、无 GPU；Chrome for Testing 151（SwiftShader，C1）；Node 22.12；`.venv` Python 3.12；六城 `worlds/` |
| 约束执行 | 没有安装依赖；没有使用 git；没有跑验收基准与 perf 标记用例。诊断与自测都在排他锁 `runs/.perf.lock` 下（脚本在锁内等 1 分钟 load ≤ 4 最多 4 分钟，再 `taskset -c 2-6`，单次持锁 ≤ 9 分钟）；功能测试持共享锁。本阶段其他工作包同时在本机运行（运行内 load 4–12），web-engine 区域的源码在本包期间持续变化，因此 A/B 对照都从同一份冻结快照构建，A 只比 B 少本包的改动（第 2.1 节）。规格变更以 ADR-069 记录（ADR-067 由 web-engine、ADR-068 由 other 登记） |

## 1 结论摘要

- **根因**（第 2 节）。归到 web-ui 的五项里，UI 自身有两类成本，另有一类不属于 UI：
  1. **首次绘制落在揭开之后**：SwiftShader 下每一类 DOM 内容第一次被栅格或合成时，GPU 进程要编译 Skia 程序并即时编译绘制例程（0.1–0.5 s 的帧）。不透明的启动遮罩让合成器裁掉其下全部内容，UI 壳因此在揭开后才第一次被栅格与合成（揭开后 0.3–1.7 s 编译 5 个程序、472/527 ms 的帧）；遮罩淡出时首次成层、重新栅格 480 px 徽章（470 ms）；命令面板、Toast、ViewCube 新朝向的面在第一次出现时各自再编译。这是 D1-AC-04 进入目标带、D1-AC-25 首次操作与整景早期长帧中 UI 的部分。
  2. **周期任务里的我方脚本**：风暴中事件桥每 250 ms 的刷入耗时 20–96 ms，约 45% 是 Base UI Toast 的高度重测（MutationObserver 触发 `offsetHeight` + `flushSync`）强制布局；折叠的事件表随刷入重渲染；环境面板为四个读数整面板重渲染；告警主体去重是 O(n)；全选按键里顶栏、工具条、批量操作条同步重渲染；URL 回写每次让全部路由订阅者重渲染。这是 D1-AC-27 我方 LoAF 的主体。
  3. **不属于 UI**：揭开 2 s 之后的整景尖峰（300–650 ms）在 `?chrome=0` 下同样出现，帧内没有 UI 栅格与程序编译，属 WebGL 侧（web-engine）。另发现 LoAF 探针按 16 帧时间环识别主循环，风暴中每次运行有 3–5 个条目把主循环计为我方脚本（web-engine 区域的测量口径）。
- **修复**（第 3 节，ADR-069）：遮罩下的有界预光栅阶段与预热舞台；Toast 帧后空闲期投递、同一条 ≤ 1 Hz、每次至多写一条；折叠面板冻结订阅；选择变化的非紧急子树延迟渲染；顶栏面包屑独立订阅；告警主体集合去重；URL 静默回写。阈值没有放宽、没有冻结。
- **效果**（第 6 节，同快照 A/B，诊断运行，非判定）：
  - D1-AC-27（live n1000 全机 RTL）：`loaf.oursOver50` A 12、9 → B 最终版 4、4、3；按脚本源位置排除主循环后 A 8、6 → B 0、0、0（每个 LoAF 中我方脚本最大 32–46 ms）。刷入单次最大 52–145 ms → ≤ 9 ms；全选按键 39–55 ms → 13–17 ms；15 s 内 ToastViewport 重渲染 175 → 66–95 次，折叠的事件表 67 → 1 次。探针剩下的 3–4 次都是把引擎主循环计为我方脚本的条目。
  - 首次绘制（FakeSource 整景 trace）：揭开后编译的 Skia 程序 5 → 0（约 16 个移到预光栅阶段）。
  - 整景帧节奏（live S1，60 s，各 2 次）：在当前引擎快照下 A 与 B 的 > 50 ms 都在 4.6–4.9%，B 一次运行窗口内最大间隔 100 ms、进入目标带 2.79 s，另一次出现 467 ms 的引擎侧尖峰；差异在噪声内，引擎侧尖峰仍决定最大间隔与进入目标带。
  - 代价：整景冷启动可交互（记录项）增加 1.5–3 s；`?chrome=0` 不受影响。
- **预期状态**：D1-AC-27 的 Toast、渲染行、FIFO 子项保持满足，我方脚本按脚本源位置计为 0，探针口径修正（web-engine）后的验收计数应为 0；单步最大属 sim。D1-AC-03b 的 > 50 ms 在当前引擎快照下已远低于 10%，最大间隔与 D1-AC-04 的进入目标带仍受引擎侧尖峰限制。D1-AC-25 的编译项保持通过，150 ms 间隔项在本机高负载下没有可判定的改善（6.4 节）。D1-AC-23 没有得出 1 个百分点以内的自测结论（第 7 节第 3 条）。

## 2 根因定位

### 2.1 方法

全部诊断脚本在 scratchpad 的 `r3ui/`（不入库）：生产构建与非压缩构建输出到 scratchpad（不动共享 `apps/web/dist`）；harness 的 `startBackend` 起 live S1、`ladder-shenzhen` n1000 或 FakeSource；Chrome trace（`devtools.timeline`、`cc`、`gpu`、`viz`、`gpu.angle`、`disabled-by-default-skia.gpu`、`invalidationTracking`）按长 `SwapBuffers` 帧归因（窗口内 Skia 操作类型、`Compile/Link` 的父事件、主线程 Paint 节点）；页面内 LoAF 观察器记录每个条目全部脚本的 invoker、函数名、源位置、时长与强制布局；CDP CPU profile（200 µs）按根函数与 React 组件归因；最小 React devtools hook 统计每次提交的渲染根。

web-engine 区域的源码在本包期间持续变化（同一时刻构建的结果会混入对方未完成的改动），因此对照实验从一份冻结快照构建两份产物：B 为快照本身（含本包全部改动），A 为同一快照把本包的 web-ui 文件还原为第 2 轮验收时的内容，两者只差本包改动，交替运行。

### 2.2 首次绘制（D1-AC-03b、04、25）

| # | 现象 | 证据 | 结论 |
|---|---|---|---|
| 1 | 揭开后 1–2 s 内的长帧 | FakeSource 整景 trace：揭开后 0.3–1.7 s 有 15 次 `Compile/Link`（5 个程序），对应 472、527 ms 的 `SwapBuffers`；`Compile/Link` 的父事件为 `FinishPaintCurrentFrame` 下的 `TextureOp`（合成）与 `DoEndRaster`（栅格） | 揭开前遮罩不透明，合成器裁掉其下的壳，壳在揭开后才第一次栅格与合成 |
| 2 | 揭开时重新栅格徽章 | 揭开帧的栅格含 30 个任务（路径与文字操作），主线程 Paint 节点为 `boot-mask`；遮罩只有 `transition: opacity`，淡出开始时才成为合成层 | 遮罩应从一开始就是合成层 |
| 3 | 遮罩全程半透明的对照 | 同一快照、遮罩 0.996：31 个程序在启动期间编译（揭开后仍有 4 个，来自遮罩淡出与 ViewCube），但冷启动 9.0–9.8 s → 15.8–16.6 s | 方向正确，代价不可接受：改为引擎就绪后的有界阶段 |
| 4 | ViewCube 的面 | 揭开后每帧 render pass quad 由 4 增到 6、9（相机开始转动后新面可见），同时编译 2–4 个合成程序 | 六个圆角裁剪的 3D 面各是一个 render pass，朝向与透视状态在预光栅阶段走一遍 |
| 5 | 第一次命令面板 | trace：按键后 97 ms 的渲染任务（cmdk 首次挂载约 100 个条目、样式重算 12 ms、强制布局 10 ms），GPU 侧编译 2 个程序（对话框与遮罩底色）；第二次打开 50–150 ms | 预热舞台中渲染一次列表与弹层样本 |
| 6 | 第一个 Toast | trace：Toast 出现的帧编译 3 个程序 | 预热舞台中放四级 Toast |
| 7 | 揭开 2 s 后的尖峰 | `?chrome=0` 的 FakeSource 整景 trace：394、338 ms 的 `SwapBuffers`，窗口内只有画布与标签的 11 个绘制操作，无 Paint、无编译；live 整景 422 ms 尖峰同样无 UI 栅格与编译；隐藏 ViewCube 的对照仍有 383–617 ms 尖峰 | WebGL 侧新绘制状态的即时编译或上传，属 web-engine（第 7 节） |

### 2.3 风暴中的我方脚本（D1-AC-27）

全机 RTL（live `ladder-shenzhen` n1000，操作同 `perf/storm.spec.ts`，20 s 观察）：

| # | 现象 | 证据（修复前，当前树） | 结论 |
|---|---|---|---|
| 1 | 刷入是 LoAF 主体 | `flushEvents`（setInterval）69 个脚本条目 1951 ms，单次最大 96 ms，其中强制布局 900 ms；同期 `loaf.oursOver50` 27、17、20 | 刷入任务内的同步工作过多 |
| 2 | 强制布局来自 Toast | CPU profile：根级 `trampoline > recalculateHeight` 自身 573 ms/10 s（Base UI `ToastContent` 的 MutationObserver 回调：`style.height = auto` 后读 `offsetHeight`，再 `flushSync`）；渲染根统计 ToastViewport 175 次/15 s | 合并 Toast 每次刷入都改写计数，重测强制布局刚提交的整页改动 |
| 3 | 同步渲染的范围 | `processRootScheduleInMicrotask` 886 ms/10 s；渲染根：EventsPanel 67（Dock 折叠，不可见）、EnvPanel 45（只为本机读数）、DronesPanel 46、Timeline 47 次/15 s | 隐藏面板不应随刷入重渲染；读数应独立成组件 |
| 4 | 告警合并 | `consumeAlarm` 自身 50 ms/10 s（`subjects.includes` 逐条扫描 1000 个主体） | 改集合 |
| 5 | 全选按键 | 按键脚本 47–55 ms（含强制布局 12 ms）：批量操作条挂载、顶栏（菜单栏）、视口工具条与 HUD 在按键任务内同步重渲染 | 非紧急子树延迟渲染，面包屑独立订阅 |
| 6 | URL 回写 | 选择后 1 s 的计时器任务中 41.7 ms 的 React 渲染：`updateSearch` 换新路由对象，App、RouterOutlet（整个壳）、WorldCanvas、各面板都订阅路由 | `sel`、`cam` 只镜像应用状态，静默回写 |
| 7 | 探针口径 | 修复后的运行中，探针计入的条目里有引擎主循环回调（例：按键条目 `oursMs` 72 = 8.5 + 13.8 + 42.3（主循环）+ 7.6）；按主循环的脚本源位置排除后，计数比探针少 3–5 | `engine/perf/loaf.ts` 的 16 帧时间环在长帧下漏识别主循环（web-engine，第 7 节） |

## 3 修复

| # | 修复 | 文件 | 单项效果（诊断） |
|---|---|---|---|
| 3.1 | **预光栅阶段**：UI 壳挂载时登记闸门 `uiWarm`；其余闸门满足后遮罩图层以不透明度 0.996 绘制并显示预热舞台，连续两帧间隔 ≤ 100 ms（至少 6 帧、400 ms）或满 2 s 后揭开；遮罩从一开始 `will-change: opacity`；错误态回到不透明。`BootController.whenReadyExcept` | `app/boot/BootMask.tsx`、`app/boot/BootController.ts`、`app/GlobalLayers.tsx`、`styles/boot.css` | 揭开后编译的程序 5 → 0；阶段内编译约 16 个 |
| 3.2 | **预热舞台**：四级 Toast（真实部件、独立 manager）、对话框与警示对话框、命令面板列表（真实 `PaletteList`）、弹出层与菜单、Tooltip、遮罩底色、按钮与徽标各变体、表单控件、与 ViewCube 同构并逐帧转动的立方体；`inert`、`aria-hidden`，阶段外 `display: none`，揭开的同一任务内隐藏后卸载 | `app/boot/WarmStage.tsx`（新）、`ui/views/CommandPalette.tsx`（拆出 `PaletteList`） | 第一次命令面板、第一个 Toast、ViewCube 新面不再在揭开后编译 |
| 3.3 | **Toast 帧后投递与限频**：`notify()` 只记录最新内容；下一呈现帧之后的空闲期投递（上限 300 ms），每次至多写一条；同一条 ≤ 1 Hz、级别变化立即、内容相同不写 | `app/providers/ToastProvider.tsx` | 刷入单次 ≤ 16 ms；ToastViewport 渲染 175 → 66–95 次/15 s |
| 3.4 | **折叠面板冻结订阅**：`PanelBody` 提供可见性上下文与 `useVisibleState`；事件表的日志与选择、图表的选择在不可见时冻结 | `ui/panels/PanelHost.tsx`、`ui/panels/events/EventsPanel.tsx`、`ui/panels/charts/ChartsPanel.tsx` | 事件表 67 → 1 次/15 s |
| 3.5 | **环境面板读数独立** | `ui/panels/env/EnvPanel.tsx` | 风暴中整面板重渲染 45 次 → 只重渲染四个读数 |
| 3.6 | **选择的延迟渲染**：DroneRail 行高亮与批量操作条、视口浮层（工具条、徽标、HUD）以 `useDeferredValue` 更新；顶栏只有聚焦面包屑订阅选择 | `ui/panels/drones/DronesPanel.tsx`、`ui/layout/ViewportOverlay.tsx`、`ui/layout/AppHeader.tsx` | 全选按键 39–55 ms → 14 ms |
| 3.7 | **告警主体集合去重** | `ui/notify/alarms.ts` | `consumeAlarm` 不再随主体数线性增长 |
| 3.8 | **URL 静默回写**：`updateSearch(…, { silent: true })` 不换路由对象、不通知订阅者；urlSync 的 `sel`、`cam` 用它 | `app/router/router.ts`、`ui/shell/urlSync.ts` | 选择后 1 s 不再整树重渲染（原 41.7 ms 的任务） |

视觉核对：预光栅阶段遮罩只透出不超过一个 8 bit 色阶；预热舞台在揭开的同一任务内 `display: none`，淡出过程中不可见（`tests/m15/fxr3.browser.test.tsx` 断言隐藏，Playwright M15 冒烟与 e2e brand 截图通过）。

## 4 改动文件

- 新增：`apps/web/src/app/boot/WarmStage.tsx`；测试 `apps/web/tests/m15/toastDelivery.test.ts`、`apps/web/tests/m15/bootWarm.test.ts`、`apps/web/tests/m15/fxr3.browser.test.tsx`；本报告。
- 修改（web-ui 区域）：`apps/web/src/app/boot/BootMask.tsx`、`app/boot/BootController.ts`、`app/GlobalLayers.tsx`、`app/providers/ToastProvider.tsx`、`app/router/router.ts`；`styles/boot.css`；`ui/notify/alarms.ts`；`ui/panels/PanelHost.tsx`、`ui/panels/events/EventsPanel.tsx`、`ui/panels/env/EnvPanel.tsx`、`ui/panels/drones/DronesPanel.tsx`、`ui/panels/charts/ChartsPanel.tsx`；`ui/views/CommandPalette.tsx`；`ui/layout/ViewportOverlay.tsx`、`ui/layout/AppHeader.tsx`；`ui/shell/urlSync.ts`。
- 文档：`docs/03-设计基线与决策记录.md`（§7.0 索引与附录 E ADR-069）、`docs/modules/M15-前端UI壳与设计体系组件PRD.md`（FR-006、FR-024、FR-036、FR-089、NFR-005、§6.3.3 状态表）、`docs/14-UI交互设计PRD.md`（§11.4 第 2 条）、`docs/18-性能与测试方案.md`（§6.1 第 7、8 条）。

## 5 规格变更

- **ADR-069**（03 附录 E）：Tier S UI 的首次绘制前移与事件风暴调度。决策七条（预光栅阶段、预热舞台、Toast 帧后投递与限频、折叠面板冻结订阅、选择的延迟渲染与集合去重、URL 静默回写、阈值不变）。**不改变任何验收阈值**；整景冷启动可交互（记录项，不在 D1-AC-02 口径内）因预光栅阶段增加 1.5–3 s。
- M15 PRD：FR-006（预光栅阶段与 `uiWarm` 闸门）、FR-024（批量操作条延迟挂载）、FR-036（刷入不直接写 Toast，折叠面板冻结订阅）、FR-089（投递与 ≤ 1 Hz 改写）、NFR-005（实现约束）、§6.3.3 状态表新增预光栅行。
- AWR-14 §11.4 第 2 条：同一条 Toast 的改写由"≤ 4 Hz"收紧为"≤ 1 Hz、下一呈现帧之后写入、级别变化立即"（下游收紧，按 03 §1.3 第 3 条不需放宽流程）。
- AWR-18 §6.1：新增第 7 条（首次绘制前移：新增一类揭开后才出现的 DOM 内容必须在预热舞台加样本）、第 8 条（周期任务内只写数据，不触发布局测量或整树重渲染）。
- 暂定阈值：没有冻结。D1-AC-03b 的最大间隔与 > 50 ms 的冻结建议见第 2 轮验收报告第 5 节，本包的数据（第 6 节）不支持在本轮改变它们：最大间隔的超限来自引擎侧尖峰。

## 6 测试与自测

### 6.1 功能

| 项 | 结果 |
|---|---|
| Vitest unit + browser（全量，最后一次改动之后） | 131 个文件：130 通过、1 跳过；970 例通过、1 跳过（含新增 `toastDelivery` 3 例、`bootWarm`、`fxr3.browser` 2 例） |
| Playwright M15（smoke、interaction、layout-probe、responsive、fxweb2、report；测试构建，私有 `M15_DIST`，端口偏移 23） | 25 通过 |
| Playwright e2e（motion、a11y、brand；当前树生产构建、FakeSource，私有静态服务） | 4 通过 |
| `tsc --noEmit` | 通过 |
| `make lint`（最后一次改动之后） | Web 侧全部通过（oxlint type-aware、no-emoji、no-hex、lint-lf、motion-lint、check-icons、no-raw-controls、check-brand、check-deps、check-units、check-perf-flags、check-thresholds、m06-lint、生成物与 codemod 检查）；失败只在 sim 区域进行中的 Python 改动：ruff `tests/sim/test_checkpoint.py:148`（RUF059）与导入边界 `python/awr/sim/runtime/main.py:1666–1667`（4 处），不在本区域。本包第一次运行时 lint-lf 报预热舞台的 `rounded-[2px]`，已改为 `rounded-xs` 并复核通过 |

### 6.2 事件风暴（D1-AC-27 形态，诊断）

live `ladder-shenzhen` n1000，`ladder.steady` 之后打开 `/world/shenzhen`，揭开后 `__perf.reset('frame')`，`Ctrl+A`、`Shift+R`、确认，观察 20 s；同一快照的 A 与 B 交替，每次新后端与新浏览器。

| 构建 | `loaf.oursOver50`（探针） | 排除主循环后 > 50 ms 的条目 | 单个 LoAF 中我方脚本合计最大（ms，排除主循环） | 刷入单次最大（ms） | 按键单次最大（ms） |
|---|---|---|---|---|---|
| 第 2 轮验收（参考） | 18（10、20、18） | — | 78–141 | — | — |
| 修复前当前树（3 次） | 27、17、20 | 12、—、8 | 105–125 | 86–96 | 47–55 |
| A（同快照，2 次） | 12、9 | 8、6 | 145、81 | 145、52 | 39、41 |
| B 中间版（Toast 帧后投递、面板冻结、告警集合、批量条与视口浮层延迟、URL 静默，2 次） | 3、5 | 1、0 | 58、49 | 6、16 | 31、35 |
| B 前一版（加 DroneRail 行与顶栏的延迟渲染、Toast 空闲期投递，2 次） | 5、4 | 0、1 | 45、58 | 8、14 | 14、14 |
| **B 最终版**（每个空闲期至多写一条 Toast，3 次） | **4、4、3** | **0、0、0** | **32、46、40** | 7、0、9 | 13、17、17 |

前一版那一次 58 ms 是一次投递写了两条 Toast 加 ResizeObserver 回调，最终版把投递限为每个空闲期一条后，三次运行中单个 LoAF 的我方脚本合计最大 32–46 ms，Toast 投递单次 ≤ 37 ms。探针仍报 3–4 次，全部是把引擎主循环回调计为我方脚本的条目（2.3 节第 7 条、第 7 节第 2 条）。Toast 合并后的同屏条数与 DroneRail 渲染行数没有变化（≤ 3、多渲染 ≤ 10 行，验收第 2 轮已满足）。

### 6.3 首次绘制与整景帧节奏（诊断）

- FakeSource 整景 trace（A 与 B 各 1 次）：揭开后编译的 Skia 程序 A 5 个、B 0 个；B 的预光栅阶段内编译约 16 个，阶段持续到 2 s 上限（同机 load 5 时帧不满足 ≤ 100 ms 的稳定条件）。
- live S1 整景 flight60（60 s，同快照交替，各 2 次；运行内 load 最高 8）：

| 构建 | > 50 ms（%） | > 100 ms（%） | 窗口内最大（ms） | 进入目标带（ms） | 头 10 s 的 > 50 ms（%） | 冷启动可交互（s） |
|---|---|---|---|---|---|---|
| A | 4.57、4.89 | 0.52、0.58 | 367、367 | 3620、4911 | 10.7、18.4 | 9.54、8.45 |
| B | 4.82、4.78 | 0.44、0 | 467、100 | 3493、2791 | 16.4、10.9 | 11.81、12.58 |

  两组 > 50 ms 都远低于第 2 轮的 10.57%（当前引擎快照的帧节奏改进），A 与 B 的差异在噪声内；B 的 467 ms 与 A 的 367 ms 都是揭开 3–7 s 后、无 UI 栅格与编译的引擎侧尖峰。B 第 2 次运行窗口内最大 100 ms、进入目标带 2.79 s。
- FakeSource 整景（20 s 窗口，A/B 交替 3 次，load 6–7）：头 10 s 的 > 50 ms A 23.5、18.6、12.0% 对 B（全程半透明版）11.2、10.8、9.5%；进入目标带 A 3.7–4.0 s、B 2.9–3.6 s。

### 6.4 首次操作（D1-AC-25 形态，诊断）

`perf/warmup.spec.ts` 同样的操作（命令面板切换天气、选中下一架、L、F、点击拾取），每个操作做两遍，记录操作开始后 1 s 内的最大帧间隔。

live S1（同快照交替，各 2 次，运行内 load 6–7.8）：

| 构建 | 命令面板（第一次 / 第二次） | 选中（第一次 / 第二次） | L | F | 拾取 | programs 增量 |
|---|---|---|---|---|---|---|
| A | 333、200 / 117、100 | 400、533 / 450、250 | 133、183 | 50、268 | 117、117 | 0、0 |
| B | 431、283 / 67、100 | 567、300 / 150、450 | 77、183 | 50、57 | 83、117 | 0、0 |

- 这组数据在同机高负载下离散很大，A 与 B 没有可判定的差异。"选中"第二次与第一次同样慢，说明它不是首次绘制问题：选中后右栏切到详情页（页栈过渡与详情面板挂载）、订阅选中机通道、引擎高亮与跟随相关的状态，trace 中是 100–170 ms 的常规帧连续出现，没有单个长任务；默认视角下的常规帧本身在这样的负载下就接近 150 ms。
- 第一次命令面板（FakeSource trace，B 最终版）：按键渲染任务由 97 ms 降到 69 ms（列表已在预热舞台渲染过一次），GPU 侧由 2 个新程序降到 1 个（对话框边框的描边路径），1 s 内最大间隔 167 ms（A 的同类 trace 233 ms）。
- 本包没有让 D1-AC-25 在本机高负载下可判定地满足 150 ms；编译项（programs 增量 0）保持通过。

### 6.5 UI 开销（D1-AC-23 形态，诊断）

live S1，同一浏览器内 UI 壳开、关各一块（60 s），A 与 B 各一个浏览器（运行内 load 最高 8.3）：

| 构建 | p50 开 / 关（ms） | > 50 ms 开 / 关（%） | 差 |
|---|---|---|---|
| A | 33.3 / 33.3 | 4.74 / 2.76 | p50 0，+1.98 个百分点 |
| B | 33.3 / 33.3 | 5.71 / 2.78 | p50 0，+2.93 个百分点 |

只有一块，块间噪声（第 2 轮关壳三块极差 1.6 个百分点）大于 A 与 B 之差，不作结论；两组的增量都小于第 2 轮的 +4.22（当前引擎快照整体帧节奏的改进）。

## 7 遗留问题与建议

1. **WebGL 侧的尖峰（web-engine）**：揭开 2 s 之后的整景尖峰（300–650 ms）在 `?chrome=0` 下同样出现，帧内只有画布与标签的绘制，没有 UI 栅格与程序编译。它决定了 D1-AC-03b 的最大间隔与 D1-AC-04 的进入目标带，也是 D1-AC-25 操作窗口里偶发的 300–600 ms。建议 web-engine 按本包同样的方法（trace 中长 `SwapBuffers` 帧前后的 WebGL 绘制、首次出现的材质与状态、纹理分配）定位，并让 shader zoo 以真实绘制状态（顶点布局、混合、深度、渲染目标格式、实例化）各画一次。
2. **LoAF 主循环识别（web-engine）**：`engine/perf/loaf.ts` 以 `loop.cbStarts`（16 帧时间环，±0.5 ms）识别主循环回调；风暴中 LoAF 条目到达晚、帧长 150–600 ms，每次运行有 3–5 个条目把主循环（20–90 ms）计为我方脚本。建议改用脚本源位置（第一次时间匹配成功后记下主循环的 `sourceURL` 与 `sourceCharPosition`）或把时间环扩到 256。在此之前 D1-AC-27 的探针计数偏高。
3. **D1-AC-23**：本包的改动不针对稳态 UI 开销（预光栅只影响揭开前后，ui-overhead 用例同一浏览器内的后续块不受首次编译影响），没有得出 1 个百分点以内的自测结论；第 2 轮验收的关壳三块极差 1.6 个百分点、FX2-R2 测得 4.3 个百分点，仍大于阈值。剩余构成见 FX2-R2-web-ui 报告第 7 节（ViewCube 每帧写 3D 变换属 M06）。建议 M16 先做 A/A 噪声底与同页块交替。
4. **整景冷启动**：预光栅阶段使整景冷启动可交互增加 1.5–3 s（记录项）。若之后要为整景冷启动设阈值，可以把阶段上限从 2 s 下调，代价是揭开后少量编译回到可见帧里。
5. **D1-AC-04 的"页面隐藏期间不评估"子项**：用例仍缺（`flight60` 驱动，属 web-engine 区域的 perf 驱动与 M16）；引擎侧 `PointCloudEngine` 已在 `document.visibilityState === 'hidden'` 时冻结。
6. **预热舞台的维护**：新增一类揭开后才出现的 DOM 内容（新的弹层、对话框、特殊的 3D 变换或圆角裁剪）时，需要在 `WarmStage.tsx` 加样本（AWR-18 §6.1 第 7 条）。可以考虑在 M16 的 warmup 用例中统计揭开后 `Compile/Link` 的次数作为回归项（需要 trace，属 M16）。
