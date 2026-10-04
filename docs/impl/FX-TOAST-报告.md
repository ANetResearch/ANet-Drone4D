# FX-TOAST 报告（发布前修复：Toast 的首次插入与首次合成）

| 项 | 内容 |
|---|---|
| 工作包 | FX-TOAST：修复 D1 验收第 5 轮发现的两处 P0 退化（D1-AC-03a 纽约 `scene=pc` 最大间隔 383 ms；D1-AC-27 全机 RTL 的我方 > 50 ms LoAF 1 次），两者同一根子：页面第一条 Toast 的插入（强制样式与布局）与 GPU 进程的首次合成；并查明纽约本轮 PerfGovernor 提前触发的原因 |
| 区域 | web-ui（Toast 投递、Toast 表面、遮罩下预演、图标预热、事件桥）为主，web-engine（PerfGovernor 首轮的定性）为辅；没有改动 sim、gateway 与测试工具 |
| 日期 | 2026-10-03（21:19 至 22:45） |
| 依据 | `docs/impl/D1-验收报告-第5轮.md` §1、§3（03a、06、27 行）、§4.2a、§4.3；`docs/impl/D1-交付总结.md` §4.1；AWR-03 §1.3、§8.4、ADR-041、ADR-064、ADR-069、ADR-071、ADR-076；AWR-14 §7.5、§11.4；AWR-15 §8.3；AWR-17 §3（访问模式不受影响）；AWR-18 §6.1、§9.5；AWR-19（部署方式不受影响）；M15 PRD FR-004、FR-006、FR-064、FR-089、FR-097、NFR-005；M16 §6.7.4（`flight60.<city>.pc`、`storm.rtl`） |
| 环境 | VMware 8 vCPU（Xeon E5-2603 v4 1.70 GHz）、62 GiB、无 GPU；Chrome for Testing 151.0.7922.34（SwiftShader，Tier S，C1）；Node 22.12.0；Python 3.12.3（`.venv`）；六城 `worlds/` |
| 约束执行 | 没有安装依赖；没有执行任何 git 命令（工作目录不是 git 仓库）；颜色只用 token（本包没有新增颜色）；UI 只用 shadcn 部件（页面 toaster 是 shadcn toast.tsx 部件的组合，没有改 `ui/components/ui/**`）；动效沿用 transitions.dev 配方 22、32 的时长、缓动、位移与缩放；没有新增图表、表格与图标；`make lint` 通过；性能运行与诊断都持 `runs/.perf.lock` 排他锁并等开跑前 load ≤ 4，构建、Vitest 与 `make lint` 持共享锁 |
| 新 ADR | ADR-081（附录 E，§7.0 索引已登记；任务说明写的"从 ADR-079 起"时 ADR-079、080 已被 FINAL-REVIEW 占用，按实际下一个编号取 081） |

## 1 结论摘要

- **D1-AC-03a 纽约 `scene=pc`：尖峰消失。** harness `flight60.newyork.pc`（生产构建，1 次）最终构建最大间隔 **83.3 ms**（修复前构建同时段 A/B 333 ms，第 5 轮 383 ms），我方 > 50 ms LoAF 0（修复前 1），p95 50 ms、> 50 ms 2.06%、> 100 ms 0，帧节奏与 CAS 各项通过。PerfGovernor 仍在揭开后 4.0 s 串联到 ⑦，但无壳页面不再挂 Toast：⑦ 之后没有 DOM 写入，GPU 进程新增 SwiftShader 例程 0（诊断，第 5 轮为 21–22 个、一帧 350–400 ms）。
- **D1-AC-27 全机 RTL：我方 > 50 ms LoAF 0。** harness `storm.rtl`（1 次）最终构建 0（第 5 轮 1、0、1）；同流程诊断中一次 Toast 新增的我方脚本由 25–46 ms（第 5 轮，强制样式与布局 17–31 ms）降到 10–35 ms，改写降到 6–11 ms（强制 1–3 ms），与新增同帧的 Base UI 内容重测不再出现。Toast 合并后 2 条、DroneRail 多渲染 8 行，均满足。
- **纽约 PerfGovernor 提前触发的原因：正常降级。** 用第 3–5 轮全部纽约运行快照里的帧间隔环按 CAS 规则重放：纽约这段飞行在 B_floor 2 万时帧间隔在 33 与 50 ms 之间摆动，CAS 首次评估（揭开后冻结 30 帧结束、与飞行开始运动同时）起每轮都有一段连续"停在下限"的评估，第 3 轮最长 7、5、5 个，第 4 轮 3、4、5 个，第 5 轮 9、10、8 个；PerfGovernor 的条件是连续 ≥ 2 s（8–9 个评估），只有第 5 轮越过。今天修复前与修复后的构建在同一机器状态下都在 4.0–5.0 s 走到 ⑦（9–13 个），第 4 到第 5 轮之间无壳路径上的代码改动都不改变这段帧。即纽约 `scene=pc` 在本机 Tier S 上处于下限边缘，⑦ 属 ADR-041 设计内的降级；修复落在"降级步骤不产生 DOM 突发"（ADR-081 第 4、6 条）。
- **按要求落实的约束**：Toast 容器常驻、预先成层、尺寸固定（408 × 432 px），`contain: size layout paint style`（计算值 `strict`）；Toast 只过渡 transform 与 opacity（不用配方的 blur 与高度过渡）；插入在帧的布局后时段，不读脏布局；遮罩下 Toast 预演覆盖每一种能出现 Toast 的模式（起始 motion 档与 reduced 档各一遍，面板已开时照常；`?chrome=0` 没有 Toast 表面，提示走非 DOM 通道）。
- **另外两处为稳妥而做的改动**（诊断推出，第 2.2 节）：堆叠变化只重算 Toast 根（内容盒取自身高度、栈变量登记为不继承、列表按条记忆化）；同帧让路（事件桥刷入或图标预热切片刚结束时 Toast 写入让到下一帧，至多 4 帧）。
- **没有放宽任何阈值。** 规格变化以 ADR-081 记录，并同步 M15、AWR-14、AWR-15、AWR-18 与文档索引（第 6 节）。
- **未达成与风险**（第 7 节）：自测只各跑 1 次且同机有其他工作包未持锁的负载，不能代替 3 次中位的验收；同流程诊断中仍有一次在外部负载突增时被计入（80 ms）；本机负载下 `scene=pc` 冷启动 4.15–4.39 s（修复前构建同时段 4.01 s，第 5 轮 3.54 s），各阶段的拖慢都在 GPU 进程（上下文创建与 shader zoo），与 Toast 无关，须由验收在协议环境中复测；`storm.rtl` 的 sim-core 单步最大（18–97 ms）被同机未钉核的后端拉长，不属本包。

## 2 定位

### 2.1 纽约 `scene=pc`：第一条 Toast 与 PerfGovernor 首轮

1. **尖峰的组成**（第 5 轮诊断 `runs/acceptance/round5/diag/pc-pcny{1,2}.json`）：⑦ 的提示是页面第一条 Toast，空闲回调中插入 43 ms（强制布局 21 ms），随后 GPU 进程首次合成它编译约 22 个 SwiftShader 例程，形成一帧 350–400 ms。`?chrome=0` 不挂 UI 壳，遮罩下的预光栅与预演都不运行，但 `Providers` 里的 toaster 仍在。M15-FR-004 本就规定无壳页面"只保留 WorldCanvas 与 LabelLayer"，AWR-14 §7.5 规定降级"永远可见（HUD 或面板）"，无壳页面两者都没有，Toast 在那里没有承载面。
2. **为什么不在无壳页面也做遮罩下预演**：预演要把遮罩改为 0.996、等首次合成的编译（一帧约 0.3 s 的 GPU 进程时间）与稳定判据，`scene=pc` 冷启动（≤ 4.0 s，ADR-076 冻结，苏州第 5 轮 3.76 s）会多 0.5–1 s；只跳过 ⑦ 的 Toast 也不够，`backend.notice`、连接状态等提示同样会成为无壳页面的第一条 Toast。所以无壳页面不挂 toaster，`notify()` 只记入非 DOM 通道。
3. **PerfGovernor 首轮的原因**：CAS 重放脚本（每 250 ms、24 帧窗口，`p50 > 1.1·T*` 或 `p90 > 2·T*` 且 B 在下限）对快照帧间隔环的结果：

   | 运行 | 每 0.5 s 帧数（揭开后 2–10 s） | 最长连续"停在下限"评估 | PerfGovernor ⑦ |
   |---|---|---|---|
   | 第 3 轮纽约 ×3 | 10–14 | 7、5、5 | 60 s 内未触发 |
   | 第 4 轮纽约 ×3 | 11–14 | 3、4、5 | 未触发 |
   | 第 5 轮纽约 ×3 | 11–13 | 9、10、8 | 揭开后 7.0、3.9、4.0 s |
   | 第 4、5 轮深圳 ×6 | 4 s 后 15–18 | 3–5 | 未触发 |
   | 本包：修复前构建（A/B） | 11–13 | 13 | 4.0 s |
   | 本包：最终构建 ×3 | 11–14 | 9–12 | 4.0–5.0 s |

   纽约三轮的起步都在首次评估时由 2.5 万降到 2 万，此后在下限上；"停在下限"的连续段都从 CAS 首次评估开始（飞行开始运动、新节点流入）。第 4 到第 5 轮之间无壳路径的改动（ADR-076 的起步预算只作用于有固定层的整景；点云预热后的可见性；⑥ 的动画采样，⑥ 在无壳页面本就不可见；预演只在有壳时运行）都不改变这段帧，运行内 load 两轮相当（4.3–5.8 对 4.4–5.1）。图标预热的空闲切片（每片 5–14 ms，揭开后头 1 s 有 6 片）在无壳页面没有用处，本包一并去掉，但它在第 3、4 轮就存在，不是第 5 轮的变化。结论：⑦ 是 CAS 与 PerfGovernor 对开头流式加载帧的设计内响应，ADR-041 的条件与间隔不改。

### 2.2 全机 RTL：Toast 插入成本的组成（逐步诊断）

诊断脚本 `.cache/fxtoast/diag.mjs rtl`（与 `storm.spec.ts` 同流程：ladder n1000 稳态后打开深圳，Ctrl+A、Shift+R、确认，观察 20 s；记录每个 LoAF 的全部脚本归因与强制样式布局时间），每行一个构建、一次运行：

| 构建（累积改动） | 产品探针计数 | 新增一条的我方脚本（强制样式与布局） | 被计入或最接近 50 ms 的 LoAF |
|---|---|---|---|
| 第 5 轮（空闲回调投递） | 1（4 次诊断中） | 25–46 ms（17–31 ms） | Toast 45.8 + 图标预热 6.0 + Toast 自身 ResizeObserver 5.1 = 57 ms |
| A：布局后投递、无壳不挂 Toast | 0 | 11–37 ms（7–25 ms）；改写 7–9 ms（1–3 ms） | 37.0 ms（只有插入） |
| B：A + 时钟在挂载前创建 | 1 | 13–36 ms（5–26 ms） | 插入 35.9 + 3 个内容 ResizeObserver 23.8 + 图标预热 7.1 = 66.9 ms |
| C：B + 内容自身高度、栈变量不继承 | 0 | 9–36 ms（5–21 ms） | 36.4 ms（只有插入）；按键帧 42.2 ms（与 Toast 无关） |
| D：C + 同帧让路 | 1（运行中途另一工作包启动 pytest，约 70% CPU） | 16–69 ms（9–49 ms） | 插入 68.5 + 新 Toast 内容 ResizeObserver 11.5 = 80 ms |
| E（最终）：D + 记忆化列表 | 0 | 10–35 ms（7–22 ms） | 34.5 ms（只有插入） |

由此得到的判断：

1. **布局后投递有效但不够**：改写（`toast.update`）的强制布局由 17–31 ms 降到 1–3 ms，说明布局后时段整页确实干净；新增仍要 5–26 ms 的强制样式（A、B 两次诊断）。把时钟提前到应用挂载之前创建（B）没有改变新增的成本，即不是别的 ResizeObserver 先弄脏了布局（仍保留，作为顺序保证）。
2. **新增的成本是 Toast 自身的样式重算**：一次 Chrome trace（`devtools.timeline` 加 invalidation tracking，B 构建）中，新增内的 `UpdateLayoutTree` 只有 14–77 个元素却用 9–28 ms，`Layout` ≤ 1.5 ms；线程 CPU 时间约为墙钟的 70%（主线程与 8 个 SwiftShader 线程争用 5 个核）。元素数来自栈：每条已有 Toast 因 `--toast-index` 等继承变量改变而整棵重算。静止页面用同一份 `index-*.css` 与同结构的 Toast 测量：四条 Toast 时一次新增 4.4–5.6 ms，没有样式表约 1 ms；把根上的栈变量以 `@property` 登记为不继承后 3.1–3.5 ms（−35–40%）。风暴中同一工作要放大 4–5 倍。
3. **与新增同帧的其他我方脚本**：①每条已有 Toast 的内容盒是 `h-full`，栈高随最前一条变化，Base UI 对每个内容的 ResizeObserver 都以 `flushSync` 重测（3 × 7–9 ms）——内容盒改取自身高度后消失（C）；②shadcn 的 `ToastList` 在每次 store 变化（新增、新 Toast 的高度回报、1 Hz 改写）时重渲染每一条的子树——改为同一组部件的记忆化组合后只重渲染变化的那一条（E）；③事件桥刷入（至多约 21 ms）与图标预热的空闲切片（至多约 21 ms）落在同一帧窗口时与新增相加——同帧让路（D）。
4. **对负载的敏感性**：D 构建那次 80 ms 发生在诊断中途另一工作包启动 pytest 时（新增的强制样式 48.6 ms，是平时的 2 倍以上）。最终构建在无外部突增时最坏一帧 34.5 ms，离 50 ms 有约 15 ms 余量；在协议环境中的余量要由验收的 3 次运行确认（第 7 节）。

### 2.3 首次合成

有壳页面的遮罩下 Toast 预演（ADR-076）只在 Tier S 的 reduced 档做，起始档（lite）的第一条 Toast（PerfGovernor 走不到 ⑥ 的会话、⑥ 之前的命令结果、风暴中第一个可见步骤为 ① 的提示）没有预演。现在两档各做一遍，面板已被打开时只跳过面板。Toast 不再有 blur 滤镜（一个 render surface 与模糊 pass，Tier S 上首条 Toast 还多一组例程）与高度过渡，预演与运行期的绘制状态更少。

## 3 改动清单

| 文件 | 模块 | 改动 |
|---|---|---|
| `apps/web/src/ui/notify/afterLayout.ts`（新增） | M15 | 布局后时钟：`<body>` 下 1 × 1 隐藏、`contain: strict` 的哨兵与一个 ResizeObserver，`afterLayout(cb)` 切换哨兵宽度以安排一帧、在观察回调（样式与布局之后、绘制之前）执行；页面隐藏或无 ResizeObserver 时 macrotask，1 s 无帧计时器兜底；`installLayoutClock()`；同帧让路的 `noteUiWork()`、`uiWorkWithin()` |
| `apps/web/src/app/providers/ToastProvider.tsx` | M15 | 投递改为布局后时段（去掉 rAF + `requestIdleCallback`）；同帧让路（`TOAST_MAX_DEFER` = 4）；toaster 作为应用的兄弟节点，`surface` 为假（`?chrome=0`）时不挂载，`notify()` 走非 DOM 通道（`headlessNotices()`，最近 16 条；`__ux.toasts.headless`）；`setSurface()`；页面 toaster 用 shadcn toast.tsx 部件组合、列表按 Toast 对象记忆化；`toastWritePending()` |
| `apps/web/src/app/providers/Providers.tsx`、`apps/web/src/app/App.tsx` | M15 | `ErrorBoundaryShell toasts={chrome}`，无壳页面不挂 toaster |
| `apps/web/src/main.tsx` | M15 | 挂载前 `installLayoutClock()`；`?chrome=0` 不做图标预热 |
| `apps/web/src/app/boot/BootMask.tsx` | M15 | Toast 预演在起始 motion 档与 Tier S 的 reduced 档各一遍（第二遍 id 前缀 `reduced-`）；面板已被打开时只跳过面板预演 |
| `apps/web/src/ui/icons/prewarm.ts` | M15 | 有 Toast 待写时让出本次空闲期；每片结束时登记（同帧让路） |
| `apps/web/src/ui/notify/eventBridge.ts` | M15 | 刷入结束时登记（同帧让路） |
| `apps/web/src/ui/hud/governorToasts.ts` | M15 | 注释：无壳页面的提示进入非 DOM 通道 |
| `apps/web/src/ui/testing/uxProbe.ts` | M15 | `__ux.toasts.headless` |
| `apps/web/src/styles/layout.css` | M15 | 页面 toaster 视口：常驻、固定尺寸（列宽 22.5 rem + 2 × 1.5 rem，堆叠区 24 rem + 2 × 1.5 rem）、`contain: size layout paint style`、`will-change: transform`，Toast 内移 1.5 rem（右下角仍在未遮挡区内 12 px）；Toast 内容取自身高度；10 个只在 Toast 根上使用的栈变量以 `@property`（`syntax: '*'`、`inherits: false`）登记 |
| `apps/web/src/styles/motion/base-ui.css` | M15 | 配方 22：Toast 根只过渡 transform 与 opacity，去掉 blur 与高度过渡 |
| `apps/web/tests/m15/toastDelivery.test.ts` | M15 | 改为布局后时段的口径；新增：无表面时的非 DOM 通道、布局后时段（假 ResizeObserver：只在观察回调中执行、每帧一次、按序）、隐藏页面与无帧兜底、同帧让路（含上限） |
| `apps/web/tests/m15/bootRehearsal.test.ts` | M15 | Tier S 两档 Toast 预演的 id 与 motion 档；面板已开时 Toast 预演照常 |
| `apps/web/tests/m15/toastSurface.browser.test.tsx`（新增） | M15 | 真实浏览器加应用样式：视口常驻、`contain: strict`、`will-change: transform`、尺寸与 Toast 位置；投递不在调用者的任务中；只过渡 transform 与 opacity、无滤镜；内容自身高度；栈变量不传到子元素而 `--gap` 继承；无表面时无视口、提示进非 DOM 通道 |

没有改动 `apps/web/src/ui/components/ui/**`（shadcn 生成物，`postadd --check` 幂等）、测试工具、harness、阈值表与 sim、gateway 代码。

## 4 自测（短时，各 1 次，非验收）

运行协议：harness `node perf/harness/run.mjs --case <id> --runs 1 --gate local`（自带排他锁、开跑前 load ≤ 4、PR-6 守护，构建指向 `.cache/fxtoast/dist`）；诊断与 trace 同样持排他锁、等 load ≤ 4、`taskset -c 2-6` 并守护。同机另有其他工作包未持锁的进程：pytest（21:37、22:17 起，约 70–116% CPU）与一个 `AWR_PROFILE=public` 的后端（21:58 起、22:13 重启，未钉核，约 26% CPU），本包的运行内 load 均值 2.3–6.1，高于第 5 轮。

### 4.1 `flight60.newyork.pc`（D1-AC-03a、06）

| 构建 | run-id | load 开跑前 / 最大 / 均值 | 最大间隔 | 我方 > 50 ms LoAF | ⑦（揭开后） | 其余 |
|---|---|---|---|---|---|---|
| 修复前（`apps/web/dist`，20:56 构建，同时段 A/B） | `fxtoast-baseline` | 3.82 / 7.33 / 5.82 | **333.3 ms**（4.0 s 处一帧） | 1 | 4.0 s | p95 50、> 50 ms 2.45%、TTI 4.01 s |
| A（布局后投递、无壳不挂 Toast） | `fxtoast-self1` | 3.67 / 8.69 / 6.06 | 100.0 ms | 0 | 5.0 s | 运行内 load 均值 6.06 下 > 50 ms 5.56%、B 反向 16.6 次/分钟、进入目标带 2.03 s 越线（负载） |
| D | `fxtoast-final` | 3.70 / 7.61 / 5.85 | 116.7 ms | 0 | 4.0 s | 其余帧节奏与 CAS 项通过；TTI 4.15 s |
| E（最终） | `fxtoast-final2` | 3.68 / 7.60 / 5.52 | **83.3 ms** | **0** | 4.0 s | p50 33.3 / p95 50 / p99 66.7 ms、> 50 ms 2.06%、> 100 ms 0、进入目标带 1.78 s、B 反向 5.2 次/分钟；TTI 4.39 s |

诊断 `pcny-a`（A 构建，同 harness 流程，加 LoAF 逐脚本与 GPU 进程 JIT 映射）：⑦ 在揭开后 4.0 s；页面没有 toast 视口、没有 Toast；⑦ 之后 SwiftShader 例程新增 0（揭开后只有 135 ms 处 2 个，属揭开尾部）；揭开后没有任何我方空闲回调（第 5 轮头 1 s 有 6 个 5–14 ms 的图标预热切片）。

冷启动可交互（D1-AC-02 P1 记录项，`scene=pc` 冻结阈值 4.0 s）：本包各次 4.15–4.39 s，修复前构建同时段 4.01 s，第 5 轮 3.54 s。对比各阶段标记，修复前与修复后构建都比第 5 轮慢在 GPU 进程阶段（`boot.canvas` 1.2–1.5 s 对 1.0 s，`boot.gl` 与 shader zoo），选择自检到 zoo 的"层安静"等待呈第 5 轮已有的两种形态（约 250 ms 后接约 270 ms 的 zoo，或 430–740 ms 后接 140–165 ms 的 zoo；第 5 轮上海、苏州也有后一种），修复后构建自检到 warm 共 800–900 ms，修复前构建同时段 1250–1260 ms。本包对无壳页面的启动只减少了工作（不挂 toaster），没有可能拖慢启动的机制；须由验收在协议环境中复测。

### 4.2 `storm.rtl`（D1-AC-27）

| 构建 | run-id | load 开跑前 / 最大 / 均值 | 我方 > 50 ms LoAF | Toast | 多渲染行 | sim-core 单步最大 |
|---|---|---|---|---|---|---|
| A | `fxtoast-self1` | 3.93 / 5.50 / 3.14 | 1 | 2 | 8 | 26.6 ms |
| B | `fxtoast-self2` | 3.58 / 5.41 / 2.27 | 1 | 2 | 8 | 8.6 ms |
| C | `fxtoast-self3` | 3.75 / 5.25 / 2.71 | 0 | 2 | 8 | 96.8 ms |
| D | `fxtoast-final` | 2.75 / 6.43 / 3.32 | 0 | 2 | 8 | 19.4 ms |
| E（最终） | `fxtoast-final2` | 3.88 / 6.30 / 3.40 | **0** | 2 | 8 | 18.3 ms |

sim-core 单步最大（第 5 轮 5.7–7.2 ms）在本包各次 8.6–96.8 ms，起伏与同机未钉核的 `public` 后端和 pytest 一致（本包只改前端）；D1-AC-27 的这一子项须由验收在没有其他工作包的环境中复测。同流程诊断见第 2.2 节表（E 构建：探针 0，最坏一帧我方 34.5 ms）。

### 4.3 外观核对

`.cache/fxtoast/shot.mjs`（live S1，生产构建，等页面第一条 Toast）：未遮挡区 976 × 628（顶边 44），Toast 右下角在 (964, 660)，即未遮挡区内 12 px，与改动前一致；视口盒 408 × 432、`contain: strict`、`will-change: transform`、父节点为 `toast-portal`；Toast 计算的 `transition-property` 为 `transform, opacity`，`filter: none`；悬停展开的截图正常。截图在 `.cache/fxtoast/out/shot-a.png`、`shot-a-hover.png`。

## 5 功能测试与 lint

| 项 | 结果 |
|---|---|
| `make lint`（含 tools/lint 全部规则、oxlint、ruff、`postadd --check`、harness 注册表） | 通过（`.cache/fxtoast/out/lint.log`）；中途一次 MOT-03（组合列表时照抄了 toast.tsx 加载态的 `animate-spin`），已去掉加载态分支（页面从不发 loading Toast） |
| `tsc -p apps/web` | 通过 |
| Vitest unit（`apps/web`，全部） | 121 个文件、961 例通过（1 个文件跳过，原有） |
| Vitest browser `tests/m15`（含新增 `toastSurface.browser.test.tsx`） | 5 个文件、18 例通过 |

## 6 规格变更

- **ADR-081**（`docs/03-设计基线与决策记录.md` 附录 E 末尾；§7.0 索引新增一行）：布局后投递与同帧让路；常驻、预先成层、固定尺寸、`contain: size layout paint style` 的 Toast 容器；只用 transform/opacity 动画；堆叠变化只重算 Toast 根；无壳页面不挂 Toast、提示走非 DOM 通道；预演覆盖起始档与 reduced 档；纽约 `scene=pc` PerfGovernor 首轮属正常降级。修订 ADR-069 第 3 条（投递时段）与 ADR-076 第 5 条（预演只在 reduced 档）。阈值与测法不变。
- **M15 PRD**：FR-004（无壳页面不挂 Toast 表面）、FR-006（Toast 预演两档、面板已开时照常）、FR-064（图标预热让路与登记、无壳页面不预热）、FR-089（投递时段、同帧让路、表面、动画、内容高度、不继承的栈变量、记忆化列表、非 DOM 通道）、FR-097（无壳页面降级不写 DOM）、NFR-005（实现约束）、§6.7.3 BU Toast 行、§7.1.7 `toasts` 字段。
- **AWR-14**：§7.5 原则段（无壳页面）、§8.2 第 10 行（Toast 只过渡 transform 与 opacity）、§11.4 第 1、2 条。
- **AWR-15**：§8.3 配方 22 行（不用 blur 与堆叠高度过渡）。
- **AWR-18**：§6.1 第 7 条（预演覆盖）、第 8 条（布局后投递、容器、同帧让路）、§9.5 `?chrome=0` 行。
- **索引**：`docs/README.md`（报告索引加 FX-TOAST；ADR 范围与数目按 §7.0 索引的现状改为 ADR-085 / 85 条：本包写入 ADR-081 之后，并行的 DEMO-PUBLIC 工作包追加了 ADR-082 至 085）、根 `README.md` 与 `README.zh-CN.md`（ADR 数目）。
- 没有改 AWR-03 §8.4 验收表、AWR-17、AWR-19：阈值、接口、访问模式与部署方式都不变。

## 7 遗留问题与建议

| # | 问题 | 区域 | 建议 |
|---|---|---|---|
| 1 | 自测只各 1 次，且同机有其他工作包未持锁的负载 | 验收 | 下一轮验收按 ADR-033 复测 `flight60.newyork.pc`、`flight60.{shenzhen,shanghai,suzhou}.pc`（D1-AC-02 冷启动与 03a）、`storm.rtl`、`storm.linkdrop`（各 3 次），并回归 `warmup`、`m06.warmup`（D1-AC-25：Toast 预演多一遍、Toast 不再有 blur）、`flight60.shenzhen.full`（整景冷启动记录项）、`ui-overhead`（D1-AC-23 的"只有画布"基线少了空的 toaster 视口） |
| 2 | 风暴中一次 Toast 新增仍要 10–35 ms（样式重算对 CPU 争用敏感，外部负载突增时一次 80 ms） | web-ui | 若验收仍有计数：风暴期间把 PerfGovernor 的可见步骤提示合并为一条（D1 验收第 5 轮 §4.3 的建议），或评估在风暴中关闭被 `limit` 挤出的 Toast（改变 AWR-14 §11.4 的行为，需 ADR） |
| 3 | 整景冷启动（记录项）因 Tier S 多一遍 lite 档 Toast 预演而增加；自测中 `boot.warm` 到揭开 6.5–8.0 s（第 5 轮 5.9–6.2 s，同时有负载差异） | web-ui | 验收复测时记录；若需缩短，可把 lite 档 Toast 预演并入第一遍面板预演之后的同一稳定判据 |
| 4 | 纽约 `scene=pc` 在本机下限边缘，⑦ 在揭开后 4–5 s 触发后 B 降到 1–1.5 万 | web-engine | 属设计行为（ADR-041）；D1-AC-05 的平均绘制点数只在深圳判定，不受影响。若要纽约不触发，需要单独评估 CAS 首次评估与飞行开始运动的相位（改变 03 §8.4 的冻结规则或 ADR-041，本包不做） |
| 5 | `storm.rtl` 的 sim-core 单步最大在本包各次 8.6–96.8 ms | sim、环境 | 同机未钉核的 `public` 后端与 pytest 所致（本包只改前端）；验收前确认没有其他后端在跑 |

## 8 证据与复现

| 内容 | 路径（不进仓库） |
|---|---|
| 驱动、诊断、trace 与外观脚本 | `.cache/fxtoast/run.sh`、`diag.mjs`、`trace.mjs`、`shot.mjs`、`shot.sh` |
| 最终构建 | `.cache/fxtoast/dist`（`npx vite build --outDir`，生产构建） |
| harness 结果 | `runs/perf/fxtoast-{baseline,self1,self2,self3,final,final2}/<case>/`（`result.json`、`run-1/snapshot.json`） |
| 诊断输出 | `.cache/fxtoast/out/pcny-a.json`、`rtl-{a,b,c,d,e}.json`、`trace-a.trace.json`、`shot-a.json`、`progress.log`、`lint.log`、`vitest-unit.log`、`vitest-browser-m15.log` |
| CAS 重放与静态样式测量 | 本包会话的临时目录（`casim.py`、`exp*.mjs`），方法见第 2.1、2.2 节 |

```bash
# 构建与单项自测（仓库根目录）
cd apps/web && npx vite build --outDir ../../.cache/fxtoast/dist && cd ../..
FX_RID=<id> .cache/fxtoast/run.sh case:flight60.newyork.pc:1 case:storm.rtl:1 rtl:<name> pc:newyork:<name>
# 功能门禁
make lint && (cd apps/web && npx vitest run --project unit && npx vitest run --project browser tests/m15)
```
