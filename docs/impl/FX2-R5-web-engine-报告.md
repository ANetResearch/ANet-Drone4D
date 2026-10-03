# FX2-R5-web-engine 前端引擎：揭开后、天气过渡与首次操作的帧节奏（第 5 轮修复）

| 项 | 内容 |
|---|---|
| 工作包 | FX2-R5-web-engine（修复区域 web-engine：`apps/web/src/{engine,viewport}`、相关用例；为本区域的 D1-AC-25 改动了 M15 的启动遮罩） |
| 日期 | 2026-10-03（06:48–10:40） |
| 依据 | `docs/impl/D1-验收报告-第4轮.md`（§3、§4.2、§4.2b、§4.5、§5、§7）；`docs/impl/FX2-R3-web-engine-报告.md`；AWR-03 §1.3、§8.4、ADR-033、ADR-041、ADR-067、ADR-069、ADR-071；AWR-18 §1.5、§2.3、§4.4、§6.2、§8.5、K-09；M05、M06、M07、M15、M16 PRD |
| 本区域待修项 | D1-AC-04（P0，整景进入目标带 2.42 s）、D1-AC-19（P0，天气过渡 > 100 ms 帧 1.10%）、D1-AC-25（P0，操作后 1 s 内最大间隔 183 ms）；另处理第 4 轮新发现 4.2b（点云整段不绘制）与第 5 节中属本区域的暂定阈值冻结建议 |
| 环境 | 8 vCPU Xeon E5-2603 v4、无 GPU；Chrome for Testing 151（SwiftShader，C1）；Node 22.12；`.venv`。本阶段 sim、web-ui 等修复包同时在本机运行，排他锁常需排队 10–40 min |
| 约束执行 | 没有安装依赖；没有执行 git 写操作（只用只读的 `git status`、`git show`、`git diff`）；诊断与自测全部在排他锁 `runs/.perf.lock` 下（`.cache/fx2r5web/diag/runlock.sh`：取锁后等 1 分钟 load ≤ 4 至多 240 s，`taskset -c 2-6` 加 PR-6 守护，单次持锁上限 540 s；harness 用例自带排他锁），构建与功能测试持共享锁；颜色只用 token，无 emoji 与禁用字形，未引入 lucide-react 与 backdrop-filter，没有新增 UI 组件；`make lint` 通过 |
| 结论 | 三项的根因都已定位并修复，没有放宽任何阈值。D1-AC-04：整景以只有点云时的起步预算 25k 揭开，而 CAS 在揭开后 30 帧冻结、首次评估才降到 lo（约 2.3 s）；改为按固定层预算计算整景起步预算（= B_floor 20k），整景从揭开起即在目标带内（harness 3 次 59、54、61 ms）。D1-AC-19：30 s 窗口在遮罩揭开之前就开始，10 帧 > 100 ms 中 7 帧来自揭开与 PerfGovernor 首轮，且首轮把环境降到 Off；窗口改为揭开且 PerfGovernor 静止后、环境保持 Low（03 §8.4 的"预设切换（Low）"），同一构建诊断 0.13%，harness 3 次 0、0.13、0.25%（中位 0.13%；785–791 帧，环境 Low，降水约 14 s 起出现），programs 不增加。D1-AC-25：首次成本几乎全部来自命令面板第一次合成（13–15 个 SwiftShader 例程），加上 PerfGovernor 首轮落进操作窗口；新增遮罩下的命令面板与 Toast 预演，操作改在 PerfGovernor 静止后开始，最终构建 harness 3 次 117、117、100 ms（中位 117 ms）。另修复 ⑥ motion 的可见性误判（滚动驱动动画被当作运行中的动画）与 4.2b 的预热竞态，新增页面隐藏子项的仓库用例，并以 ADR-076 冻结 D1-AC-02 冷启动、03b 最大间隔、09a p95、26 credit_skips 四个暂定阈值 |

## 1 结论摘要

- **D1-AC-04（P0）整景进入目标带**：按 18 §1.5，进入目标带需要"最近 1 s p50 ≤ 36.7 ms"或"B = 当前档位 lo"。整景揭开后约 2 s 内的帧在任何降载组合下都是 50–67 ms（PerfGovernor 关闭、①–⑥ 预先全降、①–⑦ 全降各 2 次），CAS 揭开后冻结 30 帧、再要 6 个样本才首次评估（约 2.3 s），起步 25k 在首次评估时一律降到 lo = 20k（第 4 轮全部整景运行如此）。修复：software 设备在固定层与点云同帧时，起步 B = max(B_floor, 25k ·(T* − 10 ms)/T*) = 20k（18 §5.1 固定层合计预算），`scene=pc` 仍为 25k。同时修正 ⑥ motion 的可见性（第 2 条），整景首轮串联在揭开后 4.3–5.1 s 一次走到 ⑦，30 fps 由第 4 轮的 6.6–19.8 s 提前到约 5 s。harness（生产构建，3 次）：进入目标带 59、54、61 ms，换档 0、来回 0、B 反向 4.1 次/分钟；整景帧节奏 p50 33.3 / p95 50 / p99 66.7 ms、> 50 ms 2.07%、> 100 ms 0.06%、最大 116.7 ms（第 4 轮 2.80%、0.12%、116.7 ms）。页面隐藏子项新增仓库用例 `cas.hidden`（模拟隐藏 6 s：200 帧、CAS 评估 0、冻结帧 200，恢复后 3 s 内评估 11 次，PASS）。
- **⑥ motion 可见性**：`document.getAnimations()` 中持续"运行"的只有 `.fade-scroll-y` 的两条滚动驱动边缘渐隐，它们不受 `html[data-motion]` 影响，却使 ⑥ 每次都判为可见：每次会话揭开后约 5 s 一条 motion Toast（其首次合成约 10 个 JIT 映射、153–180 ms 的帧），首轮串联停在 ⑥，⑦ 再等 2–15 s。改为只计文档时间线上运行中的动画。
- **D1-AC-19（P0）天气过渡**：旧用例在 `__env` 出现 1.5 s 后开始 30 s 过渡，诊断中揭开发生在过渡开始后 1.0 s；30 s 内 10 帧 > 100 ms（1.44%）：揭开与遮罩下编译尾部 4 帧（671、123、112、219 ms），PerfGovernor 首轮 3 帧（⑤ 可见步骤与 Toast 133 ms、⑥ 的整页样式重算 106 ms、⑦ 的 Toast 104 ms），其余 3 帧 104–151 ms；⑤ 在约 5.7 s 把环境降到 Off，此后降水根本不绘制。用例改为揭开且 PerfGovernor 静止（揭开 ≥ 8 s 且最近 6 s 无步骤）后开始、环境保持 Low：同一构建 746 帧中 1 帧 > 100 ms（0.13%），云量 0.05 → 1、降水 236 → 1947 个四边形，过渡期间 JIT 映射增量 0。harness（测试构建，3 次）：0、0.13、0.25%（中位 0.13%；785–791 帧，环境 Low，降水约 14 s 起出现），programs 不增加。
- **D1-AC-25（P0）首次操作**：旧用例揭开后固定 3 s 开始，PerfGovernor 首轮（4.5–5.1 s）与其 Toast 落进前两个操作的窗口。PerfGovernor 静止后拆开测（各 2 次）：只开、输入、Esc 关命令面板的第一次 283、250 ms（JIT 15、13 个映射，168、136 KB），第二次 167、100 ms；之后带成功 Toast 的切换天气 100、83 ms（JIT 0）；`?chrome=0` 下经 `__vp.select` 选中只有 2 个映射。修复：遮罩下的命令面板与 Toast 预演（ADR-069 预光栅阶段之后，见 2.4），操作在 PerfGovernor 静止后开始，逐操作数值写入 `metrics.json`。harness（生产构建，3 次）：每次运行的最大值 100、133、150 ms（中位 133 ms），programs 增量 0；逐操作：切换天气 100、83、100，选中 83、133、150，跟随 67–83，近景 50，拾取 50–67 ms。最终构建（预演阶段上限缩短、Toast 只在最后一遍）再测 3 次：117、117、100 ms（中位 117 ms），逐操作切换天气 117、83、100，选中 83、117、100，跟随 83、67、67 ms。
- **4.2b 点云整段不绘制**：`PointCloudEngine.warmupEnd()` 恢复的是 shader zoo 第一次 `prepare()` 时保存的根节点可见性；图层适配器在世界打开前 `setVisible(true)`（根节点为 false），zoo `await compileAsync` 的数秒内世界打开（`open()` 置为可见），`warmupEnd()` 又把它恢复成 false，整个会话不再绘制。改为恢复当时的实际状态；Node 单测按该时序复现（修复前失败、修复后通过）。
- **阈值**：没有放宽。ADR-076 按第 4 轮验收第 5 节的建议冻结 D1-AC-02 冷启动可交互 ≤ 4.0 s（`scene=pc`）、D1-AC-03b 最大间隔 ≤ 250 ms、D1-AC-09a p95 ≤ 66.7 ms、D1-AC-26 credit_skips ≤ 1%；D1-AC-09a 最大间隔、D1-AC-26 的 t_sim 到像素与命令到可见维持暂定。

## 2 定位过程与证据

工具与原始数据在 `.cache/fx2r5web/`（不进仓库）：`diag/jit.mjs`（GPU 进程 `/proc/<pid>/maps` 中 `memfd:swiftshader_jit` 映射的增量，每 40 ms 采样，新映射即一次 SwiftShader 例程编译）、`diag/reveal.mjs` 与 `multi.sh`（live S1 整景揭开前后的帧窗口、LoAF、运行中的 UI 动画、PerfGovernor 历史）、`diag/env.mjs`（env-switch 流程的逐帧时间线，`--settle`、`--pinLow`）、`diag/ops.mjs`（warmup 操作逐项的最大间隔、窗口内 JIT、LoAF 与状态，`--wait settle`）、`mkx.sh`（实验副本：`?govx=off|fix:<ids>`，从首次评估起把指定旋钮置于最后一级，只在 `.cache` 下）、`diag/batch*.sh` 与 `*.log`。以下除第 4 节外都是单次诊断运行，不判定。

### 2.1 D1-AC-04：揭开后 2 s 的帧与起步预算（live S1 整景，测试构建实验副本，各 2 次）

| 变体 | 进入目标带（ms） | 揭开后 0–2 s 各 0.5 s 窗口的帧数 / p50（ms） | 首轮串联 |
|---|---|---|---|
| 默认（修复前） | 2996、2579 | 2/250、7/67、8/67、9/50 | 5.06–5.10 s 停在 ⑥（motion 可见），⑦ 在 7.2–8.1 s |
| PerfGovernor 关闭 | 2371、2413 | 3–4/133–150、8/67、8–9/67–50、9–10/50 | — |
| ①–⑥ 从首次评估起全降 | 2140、2250 | 5–7/67–83、7–9/50–67、9–10/50–67、8–9/50–67 | — |
| ①–⑦ 全降（B 1 万） | 2762、2624 | 3–8/67–100、6–9/50–117、7–9/50、8–9/50；约 2.5 s 起 33 ms | — |
| 起步预算 + ⑥ 修正（修复后） | 329、63 | 3/50–200、7–9/50、9–10/33–50、8–10/50 | 4.45、5.11 s 一次到 ⑦ |
| 加命令面板与 Toast 预演（最终） | 65、46、78、41、51 | 5–8/50–83、9–10/50、8–10/50–67、9–10/50 | 4.3–4.9 s 一次到 ⑦（其中 1 次 ⑥ 可见，见下） |

- 揭开后约 2 s 的帧与降载程度无关：①–⑥ 预先全降帧不变，连 ①–⑦ 都降也要到约 2.5 s 才稳定在 33 ms。揭开时 GPU 进程仍有编译尾部（修复前揭开后 0–0.6 s 每 40 ms 新增 2–5 个 JIT 映射，预演后缩短到约 0.3 s）。
- 只靠帧在 2 s 内满足 p50 ≤ 36.7 ms 做不到；CAS 冻结 30 帧加 6 个样本是 03 §8.4 的规定，不改。起步 25k 是 g02 在只有点云时测得的取值，整景从未用上（首次评估一律降到 20k），却使揭开后约 2 s 的帧多画 5k 点（FX2-R3 2.2：2 万到 1.5 万点 GPU 进程时间约差 15 ms/帧）。改为按固定层预算计算的起步预算后，B 从揭开起等于 lo，按 18 §1.5 即在目标带内；CAS 有余量时照常向上探（单测）。
- 揭开后 1、2.5、4 s 采样 `document.getAnimations()`：除揭开瞬间的 `.t-digit` 数字动画外，持续运行的只有 `fade-scroll-show`、`fade-scroll-hide`（`DIV.fade-scroll-y`，滚动时间线）。修正后 ⑥ 在多数运行中不可见；有一次在 4.3 s 仍判为可见（当时有时间驱动的 UI 动画在跑，属正确行为），⑦ 在 6.4 s。

### 2.2 D1-AC-19：过渡窗口里的帧（FakeSource S1，测试构建，`tier=S`）

| 条件 | 帧数 | > 100 ms | 长帧（过渡开始后 s：ms） | JIT（过渡期间） |
|---|---|---|---|---|
| 旧流程（`__env` 后 1.5 s 开始，揭开在 +1.0 s） | 695 | 10（1.44%） | 0.67：671、0.86：123、0.97：112、1.26：219、6.13：133、7.88：106、8.71：129、9.85：104、11.68：118、24.12：151 | 揭开前后每 40 ms 2–5 个，直到 +1.7 s；6.02–6.15 s 7 个 |
| 揭开且 PerfGovernor 静止后开始，环境 Low | 746 | 1（0.13%） | 4.30：104 | 0 |

- 旧流程中 PerfGovernor 步骤：5.7 s 串联到 ⑤（云已出现，可见，Toast），7.8 s ⑥（LoAF render 88 ms，整页样式重算），9.8 s ⑦（Toast）；⑤ 之后环境为 Off，降水实况数一直为 0，整个 30 s 没有测到 03 §8.4 写明的 Low。
- FX2-R3 第 7 节第 1 条的"降水开始 2–3 帧约 175 ms、约 5–8 s 一帧 300–470 ms"是在 PerfGovernor 关闭、过渡同样从揭开前开始的条件下测得；ADR-071 之后、在静止页面上以 Low 过渡，降水 236 → 1947 个四边形、云量 0.05 → 1 的整段没有新增 JIT 映射，也没有对应的长帧。

### 2.3 PerfGovernor 首轮的帧（live S1 整景）

| 修复前 | 修复后 |
|---|---|
| 揭开后 5.02–5.06 s：串联帧 108–121 ms（LoAF render 68–79 ms、style 56–65 ms，即 ⑥ 改写 `<html data-motion>` 的整页样式重算）；5.36 s：153–180 ms（motion Toast 首次合成，JIT 约 10 个映射、80 KB）；⑦ 在 7.2–8.1 s 再有一条 Toast | 揭开后 4.3–5.1 s：一次串联到 ⑦，串联帧仍含 ⑥ 的样式重算；随后一条 ⑦ Toast（预演后其 JIT 0–1 个映射）；harness 整景最大间隔 116.7 ms（116.7、150、83.3） |

### 2.4 D1-AC-25：操作逐项（live S1，PerfGovernor 静止后开始，单次诊断，每格为各次运行的最大间隔 ms / 窗口内 JIT 映射）

| 构建 | 只开命令面板（输入后 Esc） | 切换天气（含成功 Toast） | 选中 | 跟随 | 近景 | 拾取 |
|---|---|---|---|---|---|---|
| 修复前（揭开后 3 s 开始，作对照） | — | 383 / 24 | 150 / 8 | 67 / 1 | 50 / 0 | 50 / 0 |
| 修复前，静止后（2 次，第 1 遍） | 283 / 15、250 / 13 | 100 / 0、83 / 0 | 133 / 6、117 / 7 | — | — | — |
| 同上，第 2 遍 | 167 / 1、100 / 0 | 67 / 0、83 / 0 | 100 / 5、83 / 0 | — | — | — |
| 面板预演一遍（不输入） | — | 133 / 8 | 133 / 3 | 83 / 0 | 83 / 0 | 50 / 0 |
| 面板预演一遍（输入） | — | 183 / 4 | 117 / 6 | 83 / 0 | 50 / 0 | 67 / 0 |
| 两遍（lite 与 reduced） | — | 117 / 0、83 / 0、167 / 4、100 / 0、100 / 0 | 83 / 0、83 / 0、133 / 6、83 / 0、117 / 7 | 350 / 24、83 / 2、83 / 0、67 / 0、67 / 1 | 50–67 / 0 | 50–67 / 0 |
| 两遍加 Toast（3 次） | — | 100 / 0、83 / 0、150 / 4 | 117 / 12、100 / 6、100 / 5 | 83 / 0、83 / 0、83 / 1 | 67 / 0、67 / 0、50 / 0 | — |

- 命令面板第一次合成编译 13–15 个例程（136–168 KB），第二次 0–1 个：成本在"第一次"，与操作时刻无关；`motion=reduced` 下第一次同样 250 ms、13 个映射，与弹层过渡无关。预热舞台已有面板列表的静态样本（ADR-069），仍覆盖不到：SwiftShader 按同一提交内此前的绘制为合成器绘制取例程变体（ADR-071 后果段），只有真实层叠顺序下的真实对话框（全视口遮罩层在画布与壳之上）才得到相同变体。
- 选中的其余成本在 UI 侧（`?chrome=0` 只经 `__vp.select` 选中：117 ms、2 个映射），主要是右栏切到单机详情页；跟随一次 350 ms / 24 个映射的离群（5 次中 1 次）不在操作本身，原因未定位（第 7 节）。
- 用例中的"跟随""近景"在 Esc 清除选中之后执行（`warmup.spec.ts` 每个操作后按 Esc），P600 近景模型实际没有出现（诊断中 hero 数恒为 0），见第 7 节。

### 2.5 4.2b：点云预热后的可见性

- `warmupBegin()` 把根节点可见性存入 `warmSave.visible`，`warmupEnd()` 恢复它；`warmupZoo` 自己也在 `finally` 中恢复第一次 `prepare()` 时的 `visible`。根节点可见性不是逐帧重算的（只在 `open()`、`setVisible()`、`close()` 中写）。图层适配器挂载时 `bindPrefs` → `setVisible(true)` 在世界打开之前，根节点为 false；zoo 第一次 `prepare()` 保存 false，`await compileAsync` 期间世界打开、根节点变为可见，最后被恢复成 false。之后选择、上传、CAS 照常，pass 计划少点云一项，与第 4 轮 4.2b 的现象（pass 计划 7 而不是 8、`pc.drawn` 12.7 万、B 升到 15 万）一致；只在世界打开晚于 zoo 首次 `prepare()` 时出现，故为偶发（ladder n200 的 200 架无人机使图层注册更晚）。
- `tests/pointcloud/engine.stream.test.ts` 新增的用例按此时序复现：恢复快照时根节点为 false（失败），恢复实际状态后为 true；用户关闭点云图层时仍为 false。

## 3 改动清单

| 文件 | 所有者 | 改动 |
|---|---|---|
| `src/engine/pointcloud/params.ts` | M05 | `PC.fixedLayersMsSoftware = 10`；`startBudget(dev, software, fixedLayers)`：software 且有固定层时 max(B_floor, b0 ·(T* − 10)/T*) |
| `src/engine/pointcloud/PointCloudEngine.ts` | M05 | 选项 `fixedLayers`；CAS `initialB` 取 `startBudget`；`warmupEnd()` 恢复实际可见性 `visible && t !== null` |
| `src/viewport/layers/pointcloud.tsx` | M05 | 传 `fixedLayers: !pcOnlyScene()` |
| `src/viewport/animationSampler.ts`（新增，由 `hostRuntime.ts` 移出） | M06 | `motionAffected()`：只计文档时间线上 running 的动画；`installAnimationSampler()` 用它 |
| `src/viewport/hostRuntime.ts` | M06 | 改为引用 `animationSampler.ts` |
| `src/app/boot/BootMask.tsx` | M15（本区域 D1-AC-25） | 预光栅阶段之后的 `rehearsePalette()`：真实命令面板打开、输入查询、清空并关闭，等遮罩层卸载；Tier S 再以 reduced 重做一遍并显示、关闭 info 与 success Toast；`awr.boot.rehearse.{start,end}` User Timing 标记 |
| `src/ui/motion/tier.ts` | M15 | `getGovernorMotion()`（预演后恢复 PerfGovernor 的 motion 输入） |
| `src/ui/shell/overlays.ts` | M15 | 注释：启动遮罩的预演也会写一次 |
| `perf/m07/env-switch.spec.ts` | M07 用例（D1-AC-19，本区域） | 等揭开与 PerfGovernor 静止后开始；环境置回 Low；> 100 ms 帧与环境采样写 `env-switch.json` |
| `perf/warmup.spec.ts`、`perf/fixtures/perf.ts` | M16 用例（D1-AC-25，本区域） | `PerfPage.waitGovernorSettled()`；操作在静止后开始；逐操作 `after_op_gap_<op>_ms` 与 `settled_after_reveal_ms` 写入 `metrics.json` |
| `perf/m06/warmup.spec.ts` | M06 用例（M06-AC-008） | `M06_PERF=1` 时各步在 PerfGovernor 首轮走完后开始（与 `warmup.spec.ts` 同一口径；功能断言不变） |
| `perf/cas-hidden.spec.ts`（新增）、`perf/harness/cases/flight60.mjs` | M16 用例（D1-AC-04，本区域） | 页面隐藏子项：模拟隐藏 6 s，断言 CAS 评估 0、冻结帧增加、恢复后重新评估；harness 用例 `cas.hidden` |
| `perf/thresholds.json` | M16 | `frame_p95_full`、`tti_ms`、`credit_skips_sel_pct` 改为 frozen，`max_gap` 来源注明 ADR-076（数值不变） |
| `tests/pointcloud/cas.unit.test.ts`、`tests/pointcloud/engine.stream.test.ts`、`tests/m06/animationSampler.browser.test.ts`（新增）、`tests/m15/bootRehearsal.test.ts`（新增） | M05、M06、M15 | 起步预算、预热可见性竞态、滚动驱动动画、预演流程 |

文档：`docs/03`（ADR-076 正文、§7.0 索引、§8.4 D1-AC-02、03b、09a、26 行的冻结状态与 D1-AC-04、19、25 行的测法注记），`docs/18`（§2.3 冷启动行、§4.4 冻结行、§6.2 无运行期编译行、§8.5 warmup / cas-hidden / env-switch 行、PERF-AC-004、010、012、040、K-09），`docs/modules/M05`（FR-037、FR-042、§6.8 起步 B 与首帧目标集）、`M06`（FR-076、M06-AC-008）、`M07`（M07-AC-018）、`M15`（FR-006）、`M16`（§6.7.4 `cas.hidden` 行与 `warmup` 行）。

## 4 本区域验收项逐条结果

harness 自测（M16 harness，排他锁、开跑前 load ≤ 4、PR-6 守护、3 次取中位；同机有其他修复包，运行内 load 见括号）。最终判定以验收阶段为准。

| 编号 | 第 4 轮 | 根因 | 处理 | 自测（harness） | 预期状态 |
|---|---|---|---|---|---|
| D1-AC-04 | 整景进入目标带 2.42 s；页面隐藏子项无仓库用例 | 起步 25k 只适合只有点云的场景；CAS 冻结 30 帧后首次评估才到 lo；⑥ 误判可见使降载推迟 | 整景起步预算 = B_floor；⑥ 只计时间驱动动画；`cas.hidden` 用例 | `flight60.shenzhen.full`：进入目标带 59、54、61 ms，换档 0、来回 0、B 反向 4.1 次/分钟（运行内 load 均值 4.0–5.2）；最终构建再测 41、55、58 ms，B 反向 5.2 次/分钟（5.2、4.1、6.2）；`cas.hidden` PASS（隐藏 6 s 200 帧、评估 0、冻结帧 200，恢复后 3 s 评估 11 次）；`scene=pc`（单次，起步 25k 不变）进入目标带 1.66 s | 通过 |
| D1-AC-19 | > 100 ms 1.10% | 窗口从揭开前开始，揭开与 PerfGovernor 首轮的帧计入；首轮使环境为 Off | 窗口改为揭开且静止后、环境保持 Low | `m07.env-switch`：0、0.13、0.25%（中位 0.13%；785–791 帧，环境 Low，降水约 14 s 起出现），programs 不增加 | 通过 |
| D1-AC-25 | 最大间隔 183 ms | 命令面板第一次合成（13–15 个例程）；PerfGovernor 首轮落进操作窗口 | 遮罩下的面板与 Toast 预演；操作在静止后开始 | `warmup`：每次最大 100、133、150 ms（中位 133），programs 增量 0；`m06.warmup`（M06-AC-008，单次）：选中 100、Third 100、FPV 67、P600 50、拾取 50、配色 50、切换世界 67 ms，PASS 最终构建（预演阶段上限缩短、Toast 只在最后一遍）再测 3 次：117、117、100 ms（中位 117 ms），逐操作切换天气 117、83、100，选中 83、117、100，跟随 83、67、67 ms。 | 通过（选中一项余量小，第 7 节） |
| 4.2b（新发现） | 点云整段不绘制（ladder n200 9 次中 4 次） | 预热恢复了过时的可见性快照 | 恢复实际状态 | 单测复现并通过；整景 3 次 pass 计划 8、B 1 万 | 待验收复测 D1-AC-09a |
| D1-AC-03b（回归） | 通过 | — | — | p50 33.3 / p95 50 / p99 66.7 ms，> 50 ms 2.07%（2.45、2.07、1.87），> 100 ms 0.06%（0.06、0.06、0），最大 116.7 ms（116.7、150、83.3），ours_over50 0；最终构建 p99 66.7 ms、> 50 ms 3.46%（5.19、3.46、2.59）、> 100 ms 0.06%（0.25、0、0.06）、最大 116.7 ms（133.3、100、116.7），pass 计划 8 | 通过 |

## 5 规格变更与阈值

- **ADR-076**（`docs/03` 附录 E）：整景 CAS 起步预算；⑥ motion 可见性只计时间驱动动画；点云预热后的可见性；遮罩下的命令面板与 Toast 预演（修订 ADR-069 的预光栅阶段）；D1-AC-19 与 D1-AC-25 的测量窗口；页面隐藏子项用例；四个暂定阈值冻结。目标带定义、冻结帧数、ADR-041 的降级顺序与间隔、各阈值数值都不变。
- **冻结**（03 §1.3 第 3 条，数据为第 2–4 轮验收的 3 次中位）：D1-AC-02 冷启动可交互 ≤ 4.0 s（`scene=pc`；第 2–4 轮六城 3.12–3.66 s；本包改动不影响该场景）；D1-AC-03b 最大间隔 ≤ 250 ms（第 4 轮 116.7 ms，PR-6 守护前后一致；本包自测 116.7 ms）；D1-AC-09a p95 ≤ 66.7 ms（第 3、4 轮 66.7、50.0 ms）；D1-AC-26 credit_skips ≤ 1%（第 4 轮 0.67%）。维持暂定：D1-AC-09a 最大间隔（4.2b 修复后以 3 次有效运行决定）、D1-AC-26 t_sim 到像素与命令到可见（第 4 轮 5 节的两条前置处理未做）。
- **整景冷启动（记录项）**：预演使整景揭开推后约 2.5–3 s（最终构建 harness 3 次冷启动 17.7 s（17.7、18.0、17.5），第 4 轮 14.5 s；`pc.firstFrame` 到揭开由第 4 轮的 2.9 s 变为 4.8–5.8 s）；`scene=pc` 无 UI 壳，不受影响（D1-AC-02 的判定场景）。

## 6 测试

| 范围 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `npx tsc -p tsconfig.json --noEmit` | 通过 |
| vitest unit | `vitest run --project unit tests/m15 tests/m06 tests/pointcloud tests/perf tests/environment tests/m16 tests/net tests/time` | 93 个文件 563 例通过（新增：起步预算 3 例、预热可见性竞态 1 例、预演 3 例） |
| vitest browser | `vitest run --project browser tests/m06/animationSampler.browser.test.ts` | 3 例通过（滚动驱动动画不计、时间驱动动画计入、采样器） |
| harness 注册表 | `node perf/harness/run.mjs --check` | 108 个用例通过校验 |
| 阈值表 | `node tools/ci/check-thresholds.mjs` | 通过 |
| make lint | `make lint` | 通过（oxlint type-aware、no-emoji、no-hex、lint-lf、motion、icons、no-raw-controls、brand、deps、units、perf flags、thresholds 等） |
| 性能自测 | harness `flight60.shenzhen.full`、`warmup`、`m07.env-switch`、`cas.hidden`、`m06.warmup`、`flight60.shenzhen.pc`（`runs/perf/fx2r5web-v1/`、`fx2r5web-v2/`） | 见第 4 节 |

## 7 遗留问题与建议

| 编号 | 问题 | 区域 | 建议 |
|---|---|---|---|
| 1 | D1-AC-25 的"选中"仍有 3–12 个 JIT 映射（右栏切到单机详情页的首次合成），3 次中 1 次恰为 150 ms | web-ui | 预演中再加单机详情页（需要一架机体的数据；或在预热舞台用真实详情页组件与假数据），或减少详情页首屏的合成层种类 |
| 2 | 跟随操作 5 次中 1 次出现 24 个 JIT 映射、350 ms；`?chrome=0`（live S1）下首轮串联之后约 0.4 s 有一次 28 个映射的成批编译；`flight60.shenzhen.pc` 自测单次在运行内 load 均值 5.2 时 PerfGovernor 走到 ⑦（第 4 轮 pc 一直在第 0 步），约 1.7 s 后一帧 417 ms（LoAF 中我方脚本 0）。对照：PerfGovernor 关闭时单独放开点云下限并把 B 降到 1.3 万（`diag/floor.mjs`），4 s 内 JIT 映射 0、无 > 80 ms 的帧，即成批编译不来自 ⑦ 本身；整景 6 次运行中 ⑦ 前后没有长帧 | web-engine | 用 P4-WEB 的 Vulkan 层逐绘制记录管线与槽格式，在 `?chrome=0` 下逐个施加 ①–⑥ 的旋钮，查成批编译时新出现的状态（轨迹批次重排或低模转标记点最可疑） |
| 3 | `warmup.spec.ts` 每个操作后按 Esc 清除选中，"跟随""近景"在无选中时执行，P600 近景模型实际没有出现 | M16 | 用例改为选中后保持选中执行跟随、近景，或另设 P600 近景操作（m06.warmup 已覆盖 P600 模型的程序数） |
| 4 | Tier S 上 PerfGovernor 首轮在晴天把 ⑤ 作为不可见步骤降到 Off，之后天气出现时不再显示（直到 10 s 上限饱和才恢复）；D1-AC-19 因此改为把环境置回 Low 测量 | web-engine、产品 | 产品决定：⑤ 在不可见时是否只记账、不降级（天气出现后再按常规降级并提示） |
| 5 | 整景冷启动增加约 2.5–3 s（预演；记录项，17.7 s） | web-ui | 若需要预算，可只在 Tier S 做 reduced 一遍或缩短各阶段上限 |
| 6 | harness 尚不识别"pass 计划不含点云而 `pc.drawn > 0`"的无效运行（第 4 轮 4.5） | M16 | 在 `snapshotErrors` 中加入（需要快照中有点云图层是否绘制的字段） |
