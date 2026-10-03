# P4-UI 报告（第 4 轮修复，前端 UI 壳：稳态开销、任务编辑、单操作提交、许可卫生）

| 项 | 内容 |
|---|---|
| 工作包 | P4-UI：D1-AC-23（UI 壳稳态开销，P1）、D1-AC-17 的任务编辑子项（P1）、D1-AC-25 中 UI 侧的单操作提交（P0，与 P4-WEB 配合）、`ui/lf/rnd.ts` 与 LF-CHART-01 的许可卫生 |
| 区域 | web-ui：`apps/web/src/{ui,app,stores}`、`apps/web/src/main.tsx`；为本包需要跨模块改了 `viewport/overlay/ViewCube.tsx`、`viewport/facade.ts`、`viewport/interaction.ts`、`viewport/layers/pointcloud.tsx`、`engine/mission/MissionOverlay.ts`（门面只增不改，引擎只加草稿绘制）、`tools/lint/`、`apps/web/perf/`（新用例与判定键） |
| 日期 | 2026-10-02（17:28–21:30） |
| 依据 | `docs/impl/D1-验收报告-第3轮.md` §3、§4.3、§4.5、§5；FX2-R2-web-ui、FX2-R3-web-ui 第 7 节；AWR-03 §1.3、§8.4、ADR-016、ADR-028 至 ADR-033、ADR-066、ADR-069；AWR-14 §3.5、§5.3、§6.8、§6.10；AWR-15 §12；AWR-18 §6.1、§8.5、§13.1；M06、M10、M15、M16 PRD；研究笔记 d02 §4.7 |
| 环境 | 8 vCPU Xeon E5-2603 v4、无 GPU；Chrome for Testing 151（SwiftShader，C1）；Node 22.12；`.venv`；六城 `worlds/`。P4-SIM、P4-WEB 同时在本机运行：排他锁排队常见 5–20 min，持锁期间运行内 load 多在 5–8（其他包的 pytest 与构建只持共享锁或不持锁） |
| 约束执行 | 没有安装依赖；没有执行 git 写操作（只用 `git status`、`git diff`、`git show` 只读核对）；诊断与自测在排他锁 `runs/.perf.lock` 下（取锁后等 1 分钟 load ≤ 4 最多 240 s，再 `taskset -c 2-6`；单次持锁 ≤ 10 min），构建与功能测试持共享锁；颜色只用 token；没有 emoji 与禁用字形；没有 lucide-react、backdrop-filter；新 UI 全部用 shadcn 组件（Button、ButtonGroup、ToggleGroup、Badge、Table 上的 LfTable、InputGroup、NativeSelect、Select、Combobox、DropdownMenu、Tooltip、Alert、Kbd、Spinner），图标走 `ui/icons` 注册表，动效只用 transitions.dev token（WAAPI 时长与缓动取 `MOTION`、`EASE_CSS`）；规格变更以 ADR-072 记录（§7.0 索引已登记） |

## 1 结论摘要

- **D1-AC-23（UI 壳稳态开销）**：第 3 轮验收 +3.00 个百分点（开壳 > 50 ms 帧中位 5.43%、关壳 2.43%）。本包定位到两类成本并修复（第 2、3 节）：
  1. **ViewCube 的 CSS 3D 面**：每个可见的 `preserve-3d` 面每帧都是一个 render pass。同页交替 A/B 中隐藏 ViewCube 使 GPU 进程每帧少 11.2 ms CPU、> 50 ms 帧 4.17% → 2.23%，约占增量的 2 个百分点。改为 JS 计算的正交 2D 面（合成层、只显示朝向观察者的面、亚像素变化不写、Tier S 挂共享节拍）后，同一 A/B 中显示与隐藏 ViewCube 的 GPU 成本不可区分。
  2. **订阅粒度**：开壳每秒 6.3 次 React 提交、124 条 DOM 变更，86% 的帧带 DOM 写入。时间轴读数与轨道区、DronesPanel、LayersPanel、WorldPanel、PerfHud 为几个变化的数整块重渲染；Base UI 的 Switch、Slider、搜索框每次渲染都改写隐藏 input 的属性（55 条/s）；`<html>` 的 `data-tier`、`data-motion` 随每次性能摘要重写（约 1.2 次/s，全文档属性选择器失效）；点云统计在自己的 4 Hz 相位写 store。改为读数绑定文本、画布在调度时读取输入、行签名与栏键、逐部件订阅、根属性只在变化时写、统计复制挂节拍后，提交 6.3 → 4.0–4.8 次/s（栏键之后的构建中 FleetList 不再每拍提交）、DOM 变更 124 → 67–72 条/s、带写入的帧 86% → 55%、渲染进程每帧 8.1–8.4 → 7.0–7.1 ms。
  - 结果：harness `ui-overhead`（同一浏览器、开关壳交替各 3 块，live S1，运行内 load 均值 6.7–7.3）三次为 **+1.28**、**+1.41**、**+2.24** 个百分点（p50 差 0.005 ms、0、0.005 ms；第三次开壳第 1 块 5.20 偏高，其余两块 3.50、4.38），详见 6.2 节；同页交替 A/B 中整个壳（CSS 隐藏）的增量为 +0.94 个百分点。**由 +3.00 降到 +1.3 至 +2.2（块间噪声与增量同一量级），尚未落入 ≤ 1**；剩余构成与建议见第 9 节（主要是合成整块壳面的"存在成本"与更大的 JS 堆带来的 GC：开壳 12 s 内 GC 193 ms 对 53 ms）。阈值没有放宽；harness 的判定键改为与 AWR-03 一致的 1 个百分点（原来误用弱网的 2 个百分点）。
- **D1-AC-17（任务编辑子项）**：右栏第 3 页 `mission-edit` 已实现并随启动注册：航点增（视口单击地面、行菜单插入、航段中点"+"）、删（Delete、行菜单）、改（E、N、高度、AGL 或 world z、航线速度）、拖（视口拖动、Shift 改高）、重排（Alt+↑↓、行菜单）、撤销重做（≤ 50 步）、粗校验（300 ms 静默后 M04 `path_coarse_check`，违规航段 r500 虚线与警示三角）、ADR-016 上限、提交 `follow_path`（AGL 经 `ground_dtm` 换算）；视口框选区域（逐点或 Shift+拖矩形）→ 覆盖（lawnmower）或搜索（expanding_square）→ 预览（R26，能量预检）→ 生成任务（R25 + `mission/{mid}/start`）。验收用例 `apps/web/perf/mission-edit.spec.ts`（harness `e2e.mission-edit`）**通过**：独立运行 3.0 min，harness 判 PASS（第 4、6 节）。
- **D1-AC-25（UI 侧单操作提交）**：选中（Period）每次提交的组件渲染 340–575 → 181–197 个；行点击切到详情页时整个壳不再重渲染（1100–1387 → 697–881 个，点击任务 50–89 → 40–58 ms）；Esc 清除选择 768–810 → 190–201 个，按键任务 44–51 → 26–28 ms。三类操作 1 s 内最大帧间隔均 ≤ 150 ms（首个"选中"仍落在揭开后的 WebGL 尖峰时段，见第 5 节）。
- **许可卫生**：`ui/lf/rnd.ts` 改为独立实现（黄金比 Weyl 步进 + MurmurHash3 32 位终混函数，公有领域）；LF-CHART-01 改为 `tools/lint/lf-chart.mjs` 的 oxc AST 规则（取随机源、整体重建子树、常量元素 id），不再沿用 lieflat 的 `validate.mjs`；`THIRD_PARTY_NOTICES.md` 改为"只取视觉语言，不含 lieflat 代码"。
- 功能测试：Vitest unit 945 通过（1 跳过）、browser 41 通过；Playwright M15 25 通过、e2e motion、a11y、brand 4 通过、mission-edit 通过；`make lint` 通过（含 lint selftest 的 LF-CHART-01 新用例）。

## 2 根因定位（D1-AC-23）

### 2.1 方法

诊断脚本在 scratchpad 的 `p4ui/`（不入库）：生产与非压缩构建输出到 scratchpad；harness 的 `startBackend` 起 live S1（与 `ui-overhead` 用例同一剧本）；同一浏览器内开、关壳交替。

| 工具 | 内容 |
|---|---|
| `uidiag.mjs` | 每块 flight60 `scene=full`：稳态帧间隔（t ∈ (2, 60] s），CDP `Performance.getMetrics` 的脚本、样式、布局时长与次数，`/proc` 的 GPU 进程与渲染进程 CPU（每帧），MutationObserver（每秒变更数、带写入的帧的比例），最小 React devtools 钩子（每秒提交、按"本次提交中自己开始渲染的组件"计的工作根，只计本次提交中新克隆的 fiber，避开旧 fiber 残留的 `PerformedWork` 标志） |
| `uiab.mjs` | 同一页面每 4 s 切换一个 CSS 变体（隐藏或改写某一部分），每轮把切换顺序旋转一位，使每个变体遇到飞行的每个阶段；比较每帧 GPU 进程与渲染进程 CPU、平均帧间隔与 > 50 ms 占比。本机负载漂移大，这种交替比"分块开关"稳得多 |
| `prof.mjs`、`heap.mjs` | 渲染进程主线程 CPU profile（200 µs）与分配采样（开壳对关壳各 12 s） |
| `opdiag.mjs` | D1-AC-25 单操作：1 s 内最大 rAF 间隔、LoAF（≥ 50 ms，逐脚本与强制布局）、React 提交与工作根 |

### 2.2 计数（修复前，生产构建，live S1，2 块开、2 块关）

| 项 | 开壳 | 关壳 |
|---|---|---|
| > 50 ms 帧 | 4.08%、2.52% | 2.81%、2.16% |
| GPU 进程 CPU / 帧 | 156.6、153.0 ms | 149.7、143.7 ms |
| 渲染进程 CPU / 帧 | 8.4、8.1 ms | 4.4、4.3 ms |
| 脚本 / 帧 | 3.58、3.30 ms | 2.07、2.06 ms |
| 样式重算 | 25.5 次/s | 0.1 次/s |
| React 提交 | 6.3、6.0 次/s | 0.1 次/s |
| DOM 变更 | 125、124 条/s | 2 条/s |
| 带 DOM 写入的帧 | 86% | 3% |

非压缩构建的归因（58 s）：

| 工作根（自己开始渲染的组件） | 次数 | 说明 |
|---|---|---|
| TimelineBar › Readout、TimelineTrackArea | 218、218 | 每个节拍一次；轨道区连带 ContextMenu、HoverCard、Slider 与两条画布的 props |
| DronesPanel | 217 | 每个节拍一次（`fleetStore.version`）；搜索框、筛选菜单、批量条宿主随之重渲染；行的 `onClick` 每次是新函数，行 memo 失效 |
| LayersPanel | 107 | 点云统计在自己的 `fps: 4` 相位写 `stores/world`，与节拍错开；整组 9 个 Switch 重渲染 |
| PerfHud | 53 | 订阅 p95 数值本身 |
| Slider（时间轴） | 42 | 值与范围取整秒，每秒一次 |
| WorldPanel | 28 | 点数 |

| DOM 变更来源 | 条/s | 说明 |
|---|---|---|
| Layers 组的 Switch 隐藏 input（`name`、`type`） | 55 | Base UI 每次渲染都写属性 |
| ViewCube 立方体 `style` | 23 | 相机转动时每帧写 `matrix3d` |
| 时间轴 Slider 隐藏 input | 14 | |
| DroneRail 行文本、搜索框 `name` | 8、8 | |
| `<html>` 的 `data-tier`、`data-motion` | 1.2、1.2 | `main.tsx` 的 `applyPerf` 随每次性能摘要写入（值不变） |

### 2.3 同页交替 A/B（ViewCube 与壳的各部分）

修复前构建，3 轮、每段 4 s（运行内 load 6.5–7）：

| 变体 | 平均帧间隔 | > 50 ms | GPU 进程 / 帧 | 渲染进程 / 帧 |
|---|---|---|---|---|
| 原样 | 37.2 ms | 4.17% | 160.0 ms | 7.91 ms |
| 隐藏 ViewCube | 34.0 | 2.23% | 148.8 | 7.22 |
| 面不裁圆角、不裁溢出 | 36.0 | 3.76% | 155.5 | 8.14 |
| 固定朝向（三面可见） | 40.5 | 10.31% | 181.4 | 7.87 |
| 背面也绘制 | 37.3 | 6.31% | 164.5 | 7.94 |

成本随绘制的面数增减：每个可见的 3D 面是一个 render pass，SwiftShader 下每个 render pass 都是一次离屏绘制与合成。正交 ViewCube 之后（3 轮，load 8）：显示与隐藏 ViewCube 的 GPU 进程每帧 158.8 对 160.8 ms（噪声内）。

最终构建（栏键之前），6 轮旋转：

| 变体 | > 50 ms | GPU 进程 / 帧 | 渲染进程 / 帧 |
|---|---|---|---|
| 原样 | 3.44% | 151.8 ms | 6.15 ms |
| 整个壳 CSS 隐藏 | 2.50% | 143.1 | 4.76 |
| 隐藏全部画布（时间轴、HUD 火花线） | 3.32% | 153.9 | 6.46 |
| 去掉 DroneRail 行的栅格岛 | 8.74% | 173.2 | 6.18 |
| 去掉全部栅格岛与表面分层 | 9.28% | 181.0 | 6.73 |

同页中整个壳的剩余增量为 +0.94 个百分点、GPU 每帧 +8.7 ms、渲染进程 +1.4 ms；单独隐藏右栏、左栏、HUD、时间轴、顶栏各自只差 ±1–6 ms（同一组 6 轮旋转，噪声量级），没有再占大头的单一部分；ADR-066 的栅格岛仍然必要（去掉后差 5 个百分点）。

### 2.4 剩余（最终构建，CPU profile 与分配采样，12 s）

- 渲染进程原生工作（`(program)`：样式、布局、绘制记录、合成提交）开壳 1243 ms 对关壳 711 ms；GC 193 对 53 ms。我方 JS 中最大的是时间轴明细画布的重画（`drawDetail` 3 ms/s）。
- 分配：开壳 12.5 MB 对关壳 10.9 MB（UI 侧主要是时间轴画布绘制中的闭包与数值装箱、React 渲染）。GC 差主要来自更大的常驻堆（DOM、fiber 树、Base UI），不是某个热点分配。

## 3 修复

| # | 修复 | 文件 | 效果（诊断） |
|---|---|---|---|
| 3.1 | **正交 ViewCube**：立方体矩阵在 JS 中作用于六个面，每面写 2D `matrix()`，只显示法线朝向观察者的面（`visibility`），面是 `will-change: transform` 的合成层；矩阵元素变化 < 0.006（角点约 0.3 px）不写；Tier S 只在共享 UI 节拍的帧写。预热舞台的样本立方体同构 | `viewport/overlay/ViewCube.tsx`、`app/boot/WarmStage.tsx` | GPU 进程每帧约 −11 ms；ViewCube 显示与隐藏不可区分 |
| 3.2 | **时间轴**：SIM 读数改为绑定文本；两条轨道画布的视窗、播放头、总览范围由 LfScheduler 重画前读取（`LfTimelineTrack` 的 `read`，按值比较才重画）；轨道区不订阅它们，事件处理在事件时读 store；Slider 拆成独立组件，直播时以 10 s 为步长；预览标签与 BUFFERING 只在出现时订阅播放头 | `ui/layout/TimelineBar.tsx`、`ui/layout/TimelineTrackArea.tsx`、`ui/lf/LfTimelineTrack.tsx`、`ui/motion/BoundText.tsx`（透传 `data-*`） | 时间轴每拍 2 个工作根 → 0；Slider 隐藏 input 改写 14 → ≤ 0.5 条/s |
| 3.3 | **DroneRail**：外壳（搜索、筛选）不订阅机群摘要；FleetList 订阅"栏键"（行数与行签名的散列）；行以行签名 memo，点击回调稳定 | `ui/panels/drones/DronesPanel.tsx` | 搜索框改写 8 → 0 条/s；稳态中只有行内绑定文本按节拍写 |
| 3.4 | **Layers 组**：逐层开关、配色、类别、画质、预算各自订阅；**World 组**点数、**HUD** 明细行与红色判定（布尔选择器）独立 | `ui/panels/layers/LayersPanel.tsx`、`ui/panels/world/WorldPanel.tsx`、`ui/hud/PerfHud.tsx` | Switch 隐藏 input 改写 55 → 0 条/s |
| 3.5 | **统计复制挂节拍**：`stores/world` 的复制在共享 UI 节拍内（Tier B/A 仍 ≤ 4 Hz） | `viewport/layers/pointcloud.tsx` | 节拍之外的帧不再有 Layers、World 的提交 |
| 3.6 | **根属性只在变化时写**：`data-tier`（连同 `loop.setTier`）与 `data-motion` | `main.tsx`、`ui/motion/tier.ts` | `<html>` 变更 2.4 → 0 条/s |
| 3.7 | **壳布局订阅**：`useShellLayout`、BottomDock、AppHeader 只订阅用到的字段；`prefs.setLayout` 保持未变化切片的对象身份 | `ui/layout/layoutState.ts`、`ui/layout/BottomDock.tsx`、`ui/layout/AppHeader.tsx`、`stores/prefs.ts` | 右栏换页、HUD 开关不再重渲染整个壳（D1-AC-25，第 5 节） |

视觉核对：Playwright M15 冒烟与 e2e brand 截图通过；ViewCube 由透视 400 px 变为正交投影（72 px 的立方体上差别很小），区域、方向、点击飞行与键盘操作不变（`[data-viewcube] [role=button]` 仍为 54 个）。

## 4 任务编辑（D1-AC-17）

### 4.1 功能（AWR-14 §5.3、§6.8）

- 入口：单机详情"编辑航线"（`[data-edit-route]`，写入受限时禁用并说明原因）、任务标签"编辑航线""绘制区域"（对焦点机）。打开时右栏切到第 3 页并加宽到 400 px（AWR-14 §3.5 的编辑布局），关闭时恢复。
- 页面（`ui/panels/mission-edit/MissionEditPanel.tsx`）：返回（DIRTY 时 AlertDialog"放弃修改？"）、标题与机体 Badge；工具 ToggleGroup（选择、添加航点、绘制区域）与撤销、重做；计数行"航点 k / 1000 · L / 20 km"与粗校验状态；航线速度（空为巡航速度）与新航点高度基准；航点表（LfTable，> 200 行虚拟化；E、N、高度为行内 InputGroup，基准为 NativeSelect，违规行尾 TriangleAlert 与原因 Tooltip，行菜单插入、上移、下移、删除）；放弃与提交（被挡时 Tooltip 说明：不足 2 点、超过 1000 点或 20 km、存在违规、粗校验未完成、机体未起飞）。区域表单：生成器（覆盖、搜索）、AGL、间距或首腿长度、速度、分配机体（Combobox 多选，能量预检不可行的机体描边并列出 119 ENERGY_INFEASIBLE）、预览、清除、闭合、生成任务；生成后显示任务 id 与实时状态（`mission/*/status`）。
- 状态（`editStore.ts`、`editModel.ts`）：CLEAN、DIRTY、SUBMITTING；撤销重做 ≤ 50 步；每次修改 300 ms 静默后粗校验（`input.validateIdleMs`）；提交时 AGL 航点经 `ground_dtm`（≤ 64 点一批）换算 world z，再发 `uav/{id}/cmd/follow_path {waypoints, speed_mps}`；accepted 显示"细校验中"，running 关闭编辑页回到详情页（详情页显示航线结果），rejected 或 failed 回到 DIRTY 并标出违规航段。生成任务前，分配机体中由操作员持有租约的先 `release {return_to: none}`（否则任务引擎的 MISSION 调用被拒、无限退避，任务一直 RUNNING，见 4.3 第 5 条）。
- 视口（`EditViewportLayer.tsx`，只在编辑页打开时挂载）：window 捕获阶段只拿走"按在航点 10 px 内的拖动"与"航段中点 + 的按下"，拖动在航点高度的水平面内（Shift 沿竖直线改高度），拖动中的位置只进绘制与标记（`dragging`），松开时一次写入航点并压一步历史；普通单击与双击经门面新增的 `pick.setClickConsumer` 交给编辑工具（相机控制照常收到完整指针序列，视口的选择、GoTo 预览标记与双击移目标不发生）；所选航点与插入手柄是两个只写 transform 的 DOM 标记（新选中时环以 `--duration-very-slow` + `--ease-bounce` 弹出，reduced 档不动）；航线、违规航段、区域与预览路径由 MissionLayer 经 `mission.setDraft` 绘制。编辑键（编辑作用域，守卫不满足时让给全局绑定）：Delete、Backspace、方向键（Shift ×10 m）、Alt+↑↓、Mod+Z、Mod+Shift+Z、Enter（提交或闭合区域）、Esc（逐级退出；打开的弹层自己处理 Enter 与 Esc）。
- 门面（M06 §7.1，只增）：`viewport.screenRay`、`viewport.groundZ`、`pick.setClickConsumer`、`mission.setDraft`；引擎 `MissionOverlay` 增加 `MissionDraft`（草稿变化在下一帧重建，不受 4 Hz 重建上限约束）。

### 4.2 验收用例 `apps/web/perf/mission-edit.spec.ts`

生产构建、free-shenzhen（harness 注册为 `e2e.mission-edit`，P1、ext、G2d/G3/G4；独立运行时自起 ci profile 后端）。只经 UI，不用测试钩子：

1. 选中 p600-01，起飞（`[data-cmd=takeoff]` 到 succeeded），聚焦，打开编辑页；
2. "添加航点"工具在视口点击地面 3 次（计数 1、2、3）；
3. 在表格中改 3 个航点的 E、N、高度（停机坪上方 25 m AGL 的方形）；
4. 行菜单在航点 1 之后插入（计数 4），Delete 删除新航点（计数 3）；
5. 在表格中选中航点 2，拖动其视口标记 40 px，表格的 E/N 随之变化；Mod+Z 撤销、Mod+Shift+Z 重做；
6. 粗校验通过后提交：编辑页关闭，详情页的航线状态到 **succeeded**；
7. 重开编辑页，"绘制区域"，Shift+拖出矩形（4 个顶点、闭合），覆盖生成器、间距 10 m，Combobox 改派 p600-02（仍在地面，由任务起飞），预览 ok 且无能量不可行，生成任务：已开始，任务状态到 **DONE**，R24 的生成器为 lawnmower；
8. 无 pageerror，页面请求无 ≥ 500 响应。

结果：独立运行 1 passed（3.0 min）；harness `e2e.mission-edit` PASS（`runs/perf/p20261002-p4ui-final/e2e.mission-edit/`）。

### 4.3 开发中发现并修正的问题

1. 数字输入框不能显示 `fmt.num` 的排版负号（U+2212），负坐标显示为空：单元格改用 ASCII 的 `toFixed(1)`。
2. 视口的指针目标是视口宿主 div 而不是 canvas：编辑层按"宿主或其子元素、不在 ViewCube 内"判定。
3. 所选标记的弹出动画写在定位元素上时，`scale` 属性连带缩放了定位用的平移，测量到的标记位置偏移：动画移到内层环。
4. React 合成事件会从 portal 冒泡回宿主组件：行菜单中"插入"之后行点击再次选中原航点，Delete 删错航点：菜单内容阻止冒泡。
5. 操作员刚飞完航线的机体仍持有 OPERATOR 租约，为它生成的任务一直 RUNNING（MISSION 调用被拒后以 0.5·2^k s 退避重试）：生成任务前先交还控制（AWR-14 §6.8 已补充）。
6. 编辑页的 Esc 在区域工具下会退回选择工具，打开 Combobox 时的 Esc 被它抢先：编辑层的 Enter、Esc 在弹层（菜单、Combobox、Select、对话框）打开时不处理。

## 5 单操作提交（D1-AC-25 的 UI 侧）

`opdiag.mjs`，live S1，揭开 20 s 后每种操作 3 次，非压缩构建（修复前 / 最终）：

| 操作 | 1 s 内最大帧间隔（ms） | 提交数 | 渲染的组件数 | 我方最长脚本（ms，强制布局） |
|---|---|---|---|---|
| 选中下一架（Period） | 450、100、117 / 317、83、67 | 8–11 / 8–9 | 340–575 / 181–197 | ≤ 19 / ≤ 17 |
| 行点击（切到详情页） | 117、83、83 / 117、83、83 | 11–14 / 13–15 | 1100–1387 / 697–881 | 点击任务 50–89（10–19）/ 40–58（11–19） |
| Esc（清除选择，回列表） | 83、100、67 / 67、83、67 | 13–14 / 13–14 | 768–810 / 190–201 | 按键任务 44–51（6–8）/ 26–28（6–7） |

- 行点击与 Esc 之前会让 `RouterOutlet` 整个壳（顶栏六个菜单、左栏三组、Dock 中全部面板）重渲染：`useShellLayout`、BottomDock、AppHeader 订阅整个 `layout`，`prefs.setLayout` 又为每个切片新建对象。修复后只有右栏页栈、详情页与相关读数重渲染。
- 三类操作 1 s 内最大帧间隔均 ≤ 150 ms，首次"选中"的 317 ms（修复前 450 ms）落在揭开后 20–25 s 的 WebGL 侧尖峰时段，与第 3 轮验收 4.2 的结论一致（属 web-engine）。
- 配色切换（Layers 组）现在只重渲染配色 ToggleGroup（3.4）；切换世界属于路由变化，仍会重渲染整个壳一次（验收中 83 ms，未超阈值）。

## 6 测试与自测

### 6.1 功能

| 项 | 结果 |
|---|---|
| Vitest unit（全量，最后一次改动之后） | 120 个文件：119 通过、1 跳过；945 例通过、1 跳过。新增 `tests/m15/missionEdit.test.ts`（10 例：编辑、AGL 换算、中点插入、50 步历史、ADR-016 上限、矩形、自相交、生成器参数、命中与射线几何）、`tests/m15/subscriptions.test.ts`（3 例：画布输入按值比较、行签名、栏键）、`tests/m06/viewcube.test.ts` 新增正交面 1 例；`lf-render` 三个图表快照随新的抖动更新 |
| Vitest browser（全量） | 13 个文件、41 例通过（`fxr3.browser` 的预热舞台立方体断言改为正交面） |
| Playwright M15（smoke、interaction、layout-probe、report、responsive、fxweb2；私有测试构建） | 25 通过 |
| Playwright e2e（motion、a11y、brand；私有测试构建、静态服务） | 4 通过 |
| `apps/web/perf/mission-edit.spec.ts` | 独立运行通过（3.0 min）；harness `e2e.mission-edit` 两次 PASS（`p20261002-p4ui-final`、`p20261002-p4ui-final3`） |
| `tsc --noEmit`、`make lint` | 通过（oxlint type-aware、no-emoji、no-hex、lint-lf 含新 LF-CHART-01、motion-lint、check-icons、no-raw-controls、check-brand、check-deps、check-units、check-perf-flags、check-thresholds、m06-lint、M15 生成物与 codemod 检查、harness 注册表 107 个用例、lint selftest） |

### 6.2 性能（诊断与 harness 自测，非验收判定）

| 运行 | 条件 | 开壳 > 50 ms（%） | 关壳 > 50 ms（%） | 差 |
|---|---|---|---|---|
| 第 3 轮验收（参考） | live S1，3 块交替，load 均值 7.0 | 5.43、4.43、5.86 | 2.43、2.08、2.56 | +3.00（p50 差 0.005 ms） |
| 本包最终构建 1 | harness `ui-overhead`，load 均值 6.7、最大 7.9 | 4.36、3.74、3.77 | 2.49、2.50、2.21 | **+1.28**（p50 差 0.005 ms） |
| 本包最终构建 2（ViewCube 挂节拍、根属性、Slider 10 s） | 同上，load 均值 7.3、最大 11.5（关壳第 3 块受外部负载影响） | 4.65、3.57、2.78 | 2.16、1.70、19.32 | **+1.41**（p50 差 0） |
| 本包最终构建 3（加栏键） | 同上，load 均值 6.9、最大 9.0 | 5.20、3.50、4.38 | 2.21、2.14、1.95 | **+2.24**（p50 差 0.005 ms） |
| 同页交替（整个壳 CSS 隐藏） | 6 轮旋转，load 7.1 | 3.44 | 2.50 | +0.94 |

运行证据：`runs/perf/p20261002-p4ui-final/`、`p20261002-p4ui-final2/`、`p20261002-p4ui-final3/`（`ui-overhead/run-1/snapshot-{on,off}-*.json`、`e2e.mission-edit/`）。

## 7 许可卫生

- `apps/web/src/ui/lf/rnd.ts`：`rnd(i, k)` 改为黄金比 Weyl 步进折叠两个整数，再经 MurmurHash3 的 32 位终混函数（公有领域），乘 2^-32 落到 [0, 1)。4000 个 (i, k) 的均值 0.4994，十个区间计数 373–415。调用方（LfBarRank、LfTickGauge、DesignSample）不变；抖动的具体取值改变，`lf-render` 快照随之更新。
- `tools/lint/lf-chart.mjs`（新）：LF-CHART-01 由 AWR-15 §12 与 AWR-18 §13.1 的规则文本重写为 oxc AST 检查，作用于 `apps/web/src/ui/lf/**`：取随机源（`Math.random()`、`crypto.getRandomValues()`、`crypto.randomUUID()`）、整体重建子树（对 `innerHTML`、`outerHTML` 赋值，`replaceChildren()`）、常量元素 id（JSX `id="…"`、`id={'…'}`、`setAttribute('id', '…')`、`.id = '…'`）。注释中的文字不再误报；常量 id 一律拒绝（图表常同时出现多份）。`lint-lf.mjs` 改为调用它，selftest 加了 3 组用例。
- `THIRD_PARTY_NOTICES.md`：lieflat 一行改为"只取视觉语言，不含代码"，说明段落改写；"Published methods"以 MurmurHash3 终混函数替换原先为抖动表达式引用的 Teschner 素数（仓库代码中已不再使用）。AWR-15 §12、AWR-18 §13.1 的 LF-CHART-01 条目同步。

## 8 改动文件与规格变更

- 新增：`apps/web/src/ui/panels/mission-edit/{MissionEditPanel.tsx,EditViewportLayer.tsx,editStore.ts,editModel.ts}`、`apps/web/perf/mission-edit.spec.ts`、`tools/lint/lf-chart.mjs`、`apps/web/tests/m15/{missionEdit,subscriptions}.test.ts`、本报告。
- 修改（UI 区域）：`apps/web/src/main.tsx`；`app/boot/WarmStage.tsx`；`app/i18n/{zh-CN,en}.json`；`stores/prefs.ts`；`ui/hud/PerfHud.tsx`；`ui/layout/{AppHeader,BottomDock,DroneRail,TimelineBar,TimelineTrackArea,ViewportOverlay,layoutState}.tsx/ts`；`ui/lf/{LfTimelineTrack.tsx,rnd.ts}`；`ui/motion/{BoundText.tsx,tier.ts}`；`ui/panels/{builtin.ts,drone-detail/DroneDetailPanel.tsx,drones/DronesPanel.tsx,layers/LayersPanel.tsx,mission/MissionPanel.tsx,world/WorldPanel.tsx}`。
- 修改（跨模块）：`viewport/overlay/ViewCube.tsx`、`viewport/facade.ts`、`viewport/interaction.ts`、`viewport/layers/pointcloud.tsx`、`engine/mission/{MissionOverlay.ts,index.ts}`；`tools/lint/{lint-lf,selftest}.mjs`；`apps/web/perf/harness/cases/ui.mjs`（`e2e.mission-edit` 注册、UI 开销判定键）、`apps/web/perf/thresholds.json`（新键 `ui_over50_delta_pct` = 1）；测试 `tests/m06/viewcube.test.ts`、`tests/m15/fxr3.browser.test.tsx`、`tests/m15/__snapshots__/lf-render.test.ts.snap`；`THIRD_PARTY_NOTICES.md`。
- 文档：`docs/03-设计基线与决策记录.md`（§7.0 索引与附录 E **ADR-072**）、`docs/14-UI交互设计PRD.md`（§6.8 区域绘制：交还租约与 D1 生成器）、`docs/15-视觉设计规范与色卡.md`（§12 LF-CHART-01）、`docs/18-性能与测试方案.md`（§6.1 新增第 9、10 条，第 7 条预热样本，§8.5 mission-edit 用例，§13.1 LF-CHART-01）、`docs/modules/M06-Web视口与渲染后端PRD.md`（FR-068、§6.13 正交 ViewCube，§7.1 门面四个新成员）、`docs/modules/M15-前端UI壳与设计体系组件PRD.md`（FR-022、FR-024、FR-034、FR-072、NFR-001、M15-AC-016 与 §14 用例路径）。
- 阈值：没有放宽，没有冻结。D1-AC-23、D1-AC-24 的 harness 判定键由误用的 2 个百分点（弱网剖面）改为 AWR-03 §8.4 的 1 个百分点（收紧，按 03 §1.3 第 3 条不走放宽流程）。

## 9 遗留问题与建议

1. **D1-AC-23 未达标**（harness 三次 +1.28、+1.41、+2.24，同页 +0.94；开壳单块 2.78–5.20，块间极差本身就超过 1 个百分点）。剩余构成：
   - 合成整块壳面的"存在成本"：开壳每帧 GPU 进程多约 8–9 ms CPU（26 个合成层，SwiftShader 逐像素混合约半屏的面板），渲染进程原生工作每帧多约 1.5 ms；同页 A/B 中没有再占大头的单一部分，栅格岛不能去掉（去掉差 5 个百分点）。
   - GC：开壳 12 s 内 GC 193 ms 对 53 ms，主要来自更大的常驻堆而不是热点分配；一次 20–40 ms 的老生代 GC 就是一帧 > 50 ms。
   - 建议：①（web-engine 与 web-ui 协同）右栏与左栏在默认演示布局下收起为图标条，或在 Tier S 默认只展开一侧（需按 03 §1.3 以 ADR 改 AWR-14 §3.5 的演示预设）；②时间轴明细画布在直播中以整数像素平移复用上一帧（`drawImage` 自身平移后只画新列），可减少画布上传面积与绘制分配；③在验收前按 FX2-R2 的建议做一次 A/A（关对关）得到噪声底：本机关壳三块的极差在本包的运行中为 0.26–0.46 个百分点（第三块受外部负载影响的那次除外），开壳三块的极差为 0.62–1.87 个百分点，与剩余增量同一量级，需要至少 5 块交替才能稳定判定 1 个百分点。
2. **命令面板的首次打开**（D1-AC-25"切换天气"经命令面板）：按键任务 57–60 ms 中约 20 ms 强制布局（第 3 轮验收 4.2a 第 4 条），本包没有改动；可考虑把约 100 个条目的列表在打开后的过渡中渲染（需同时保证 Enter 在列表就绪前的行为）。
3. **选中后的 GPU 侧帧**（D1-AC-25 m06 的"选中 250 ms"）：UI 侧提交已减半，剩余在 GPU 进程（详情页挂载的栅格与选中高亮），与 P4-WEB 的 WebGL 侧尖峰同时复测。
4. 任务编辑：右键菜单中的"删除航点"、违规航段的"细校验失败后高亮首段"只在 `follow_path` 失败带 `detail.seg` 时生效；1000 航点时视口符号受 Tier S 符号上限（256）截断（表格与提交不受影响）。
