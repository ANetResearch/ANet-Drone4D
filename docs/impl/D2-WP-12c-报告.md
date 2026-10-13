# D2.1 WP-12c 移动端与 D2.1 界面：实现报告

| 项 | 内容 |
|---|---|
| 工作包 | WP-12c 界面：启动屏、节能、身份、场视图控件、移动布局（所有者 M15） |
| 日期 | 第 1 轮 2026-10-06；第 2 轮 2026-10-11（第 0 节） |
| 依据 | 用户原文 `docs/inputs/D2.1-需求原文-2026-10-05.md`；AWR-04 v1.3 §15（R-D2-29 至 R-D2-43、ADR-117 至 ADR-126、D2-AC-40 至 D2-AC-55）；14 §20.26（UX-FR-148 至 161、UX-NFR-031 至 035、UX-AC-069 至 078）；M15 PRD §15.12（M15-FR-223 至 240、NFR-203 至 205、AC-220 至 230）；17 §17.20；发给 M15 的 d2-WP-04b、d2-WP-11b、d2-WP-16、d2-WP-19b 请求 |
| 路径 | M15：`apps/web/src/{app,ui,lib,styles}/**`、`apps/web/src/stores/{identity,prefs}.ts`、`apps/web/public/manifest.webmanifest`、`apps/web/public/brand/**`（图标生成物）、`tools/shadcn/**`、`apps/web/tests/m15/**`、`apps/web/perf/m15/**` |
| 约束执行 | 只写本包路径，跨所有权需求写成 6 份 `.cache/impl/requests/d2-WP-12c-to-*.md`；未执行 git 写操作；未安装依赖；未跑性能基准；无 emoji 与禁用字形；颜色只用 token（新增 `--field-ramp-1…5`）；组件只用 shadcn（base-mira），新增的 `ui/components/ui/bottom-sheet.tsx` 是对 `Sheet side="bottom"` 的封装（无新依赖）；表格用 lieflat `LfTable`；动效用 transitions.dev token；图标用 morphicons（新增 21 个语义键）；无新着色器、UBO 或纹理（场视图渲染属 M06）；口令明文未出现在任何文件，测试在运行时生成随机口令；`make lint` 通过 |
| 结论 | P0 全部落地并有功能用例：启动屏版本号（静态文本、复制诊断信息）与五段分格进度条、载入期容量行与"需排队"、站点已更新横条；节能状态机（交互检测、安静档、倒计时、SAVING 关闭实时连接并挂起渲染、覆盖层、恢复加载层、服务端 4430、观看排队 4431、沙盒驻留 SB-69/72 与恢复 SB-70）；排队卡片三种类型；设置页隐藏的身份区、内部徽标与恢复的隐藏入口；场视图工具条、图例与感知读数卡；设备档与手机、平板两套外壳（标签栏、快捷指令、可停靠侧栏、任务条、竖屏吸附抽屉、长按菜单、44 px 命中区）；PWA 清单与图标、全屏与横屏锁、指挥器配对（P1）。 |
| 第 2 轮结论 | 处理第 1 轮之后发给 M15 的 5 份请求（d2-WP-13b、d2-INT、d2-ACC-1、d2-FX-R2-sandbox、d2-FX-R2-deploy）：已由他人修好的 7 项复核保留；新改 9 项——节能期间 M15 的周期任务全部冻结（诊断 32 → 10 次回调/10 s，只剩 M06 采样器）、性能读数节能期间"已暂停"不判红、节能时机群栏无 STALE 与中性的命令禁用原因、快捷指令"盯防"共用识别物面板列表与位置回退、读数卡精简列、识别物列表以 roster 为准的回退剔除、会话条操作区不再压住读数、机群栏高度按 AGL/MSL 设置、手机 Toast 不再盖住"指令"按钮；"REST 删除的识别物留在面板"经 live 栈复现确认为用例时序（转 M16）。D2.1 功能用例 20/20、M15 回归 36/36、Vitest 1433 例、`make lint` 通过 |

## 0. 第 2 轮（2026-10-11，验收第 1 轮与 D2-FX-R2 之后）

第 1 轮交付后，D2-INT、D2-ACC-1（验收第 1 轮）与 D2-FX-R2 各区修复先后改动了本包路径，并发来 5 份新请求。第 2 轮先逐条核对现状（代码、live 栈复现、带 source map 的诊断构建），再只处理仍未解决的部分；已由他人修好的条目只复核、不重复改动。

### 0.1 输入与处置总表

| 来源与条目 | 第 2 轮所见现状 | 处置 | 验证 |
|---|---|---|---|
| d2-WP-13b-to-M15 第 1 条：手机"盯防"列不出识别物（D2-AC-48） | D2-INT 后实时 TargetLite32 行已送到 store，真实会话可列出；但快捷指令仍只读 `useTargets(items)`，与识别物面板取数不同，行未到达时仍为空 | 快捷指令改用识别物面板同一取数 `useTargetList()`（store 行，否则 roster 加配置缓存），新增 `targetPos()`：store 行、模型实时行、配置位置（地面目标 z 取 0）依次取；位置未知时以选中机位置作盯防站位，两者都未知才禁用该项 | `d21-mobile` 手机例新增"盯防"四次点按（列出 person-01、坐标、发出 `watch:`）；`d21r2.test.ts` `targetPos` |
| 同上第 2 条：返航受理 Toast 等到降落 | D2-INT 已改为首个 `accepted`/`running` 出受理 Toast | 复核保留 | 代码复核；`d21-mobile` 悬停与返航 |
| 同上第 3 条：揭开前进入节能 | D2-INT 已在遮罩期间重置交互时刻、揭开时重新起算 | 复核保留；1 Hz 判定改为可冻结（见第 9 行） | `d21-shell` 观看节能例 |
| 同上第 4 条：恢复排队卡片"仿真时间 —" | D2-INT 已加回退（服务端 `t_sim_ns` 未到时取进入节能时冻结的仿真时刻） | 复核保留 | 代码复核 |
| 同上第 5 条：QUEUED 期间断线指示与红色性能读数 | 顶栏、横幅、连接点已按 `frozen` 中性显示（D2-INT）；性能 HUD 与性能面板仍把挂起间隙算进 2 s 窗口，p95 数千 ms 判红；机群栏行仍显示"STALE 0.1 S"；环境等面板的命令禁用原因仍为"与服务器断开" | 节能 store 新增 `perfPaused`（SAVING、RESUMING、QUEUED 置位，回到 ACTIVE 后 `PERF_SETTLE_MS` = 2.5 s 清除）：HUD 只显示"已暂停"、不判红，性能面板呈现间隔卡显示"已暂停"、P50/P95 为"—"、不判红、1 Hz 卡片刷新冻结；机群栏在 `frozen` 时不显示 STALE 龄；`writeDeniedKey()` 在节能断开时返回新原因 `hint.powerSaving`（"节能中，任意操作恢复实时连接后可下发命令"） | `d21-shell` 观看节能例断言 HUD"已暂停"且无红、恢复后 2.5 s 内取消；截图自检 |
| 同上第 6 条：读数卡在 360 px 详情栏被截断 | 外层可横向滚动，但 LfTable 自带滚动容器，"余量"之后的列仍被裁掉 | 卡宽 < 600 px 用精简列：机体、声学 / 阈（dB(A)）、视觉 / 阈（′）、判定（R-D2-40 的分贝阈值与角尺寸）；斜距、余量、分模态概率、等效像素在选中行的完整读数句中；"全部列"按钮切到 10 列（卡内横向滚动）。宽度测量放在始终挂载的外层（读数到达前首帧就已测量） | `d21-shell` 读数卡例：精简列、所有表头在卡内、"全部列"切换；截图自检 |
| 同上第 7 条：手机识别物窗口打开时停在空详情 | D2-INT 已修（切到识别物或机群时另一实体的详情页回到列表） | 复核保留 | `d21-mobile` 手机例 |
| 同上第 8 条：节能期间容量轮询与"无网络请求" | 当前代码只有排队卡片与开启沙盒 Sheet 持有容量轮询租约，普通 SAVING 不轮询；诊断 10 s 内 0 个请求 | 无改动；口径仍待文档总编（WP-13b 已转） | 诊断 `timers.spec.ts`；`d21-shell` 观看节能例 3 s 无请求 |
| d2-FX-R2-sandbox-to-M15 第 1 条：SAVING 只保留容量轮询（D2-AC-46 ≤ 1%） | FX-R2-web 冻结了 4 Hz 仲裁、事件桥、`useSecond` 与图表调度器；带 source map 的诊断构建显示节能 10 s 内仍有 32 次周期回调：`main.tsx` store 写入窗口 1 Hz、`ui/shell/urlSync.ts` 1 Hz、PowerController 1 Hz 判定（SAVING 中无事可做）、沙盒自动续期检查 5 s、M06 动画采样器 1 Hz | 前四项改为 `lib/powerFreeze` 的 `freezableInterval`（恢复时立即补跑一次）；性能面板 1 Hz 卡片同样冻结；剩余 M06 `viewport/animationSampler.ts` 请 M06 处理 | 诊断：viewer 与 sandbox 两种角色节能 10 s 内周期回调 32 → 10（只剩采样器）；`d21-shell` 观看节能例断言节能 3 s 内 `setInterval` 回调 0 次；`d21r2.test.ts` |
| 同上第 1 条后半：容量响应值未变时不触发重渲染 | M15 的容量消费者均用原始值选择器（`UpdateBanner` 的 `updated`、`release`）或只在可见时订阅（排队卡片、开启沙盒 Sheet）；PowerController 订阅只在阈值变化时写 store | 无改动 | 代码复核 |
| 同上第 2 条：`t_unix_ns` 逐次更新 | M15 代码无任何以 `t_unix_ns` 判断"未变"的逻辑 | 无改动 | `grep` 复核 |
| d2-ACC-1-to-M15 第 1–4 条（揭开、载入期容量、节能主线程、软围栏提示） | FX-R2-web 已处置 | 复核；第 3 条的剩余周期任务见上 | — |
| d2-ACC-1-to-M15 第 5 条、d2-WP-13-to-M15 第 1 条：REST 删除的识别物留在面板 | live 栈复现（`.cache/wp12c/specs/tdel.spec.ts`，等待会话 ACTIVE 后再写）：第 1 轮之后的构建与本轮构建上删除后 2.5 s 内行即消失（行、store、roster 都去掉该 id）。`perf/m18/targets-panel.spec.ts` 第 2 例的失败来自用例时序：`start()` 在会话条出现时返回，会话仍为 SPAWNING，`POST /targets` 得到 503（211），`id` 为 undefined，被 `test.fail` 记为"预期失败" | 回退路径加固：store 无行时以 roster 为准，曾在 roster 中出现、现已离开 roster 的 id 不再因配置缓存保留（`RosterTargetGate`，随沙盒会话复位；刚由本页新增、roster 尚未列出的 id 照常显示）；请 M16 在 `start()` 等待 ACTIVE 并去掉 `test.fail` | live 诊断 2 次；`d21r2.test.ts` 门控用例 |
| d2-INT-to-M15-M17 第 1 条：D2-INT 在 M15 文件中的改动 | 逐项复核：会话对象缺失或 SPAWNING 时退避重取、`power.expectNavigation()`、顶栏节能中性显示、手机识别物窗口回列表、盯防从选中机位置看向识别物 | 全部保留 | — |
| 同上第 3 条：`ui/testing/uiOnly.ts` 的订阅在端点切换后未重建 | rt worker 保存订阅表并在每次重连握手后整体重发（`net/rt/session.ts`），UI-only 外壳的订阅在端点切换后仍有效；`manual-keepout` 接管后泵停的根因是服务端 250 ms 流看门狗（209），FX-R2-web §3.5 已以 10 Hz 零速度保活修复 | 无改动 | 代码复核 |
| 同上第 4 条：1600 px 会话条日历压住"切换底图"；机群栏高度为 ENU z | shadcn `AlertAction` 绝对定位叠在不收缩的读数上；机群栏不论"设置 › 高度基准"都显示世界 z | 会话条操作区改为流内靠右（`static shrink-0`），读数区裁切，按条自身宽度的容器查询先隐藏日历（< 72 rem）再隐藏实体计数（< 62 rem）；机群栏按设置显示：AGL（缺省）= z − 地形高度（M06 `viewport.groundZ`），MSL = 世界 z（纯函数 `railAltitude`） | `d21-shell` 读数卡例几何检查（1600 与 1920 px 操作区不覆盖读数、1920 px 计数可见）；`d21r2.test.ts`；截图自检 |
| d2-FX-R2-deploy-to-M15：复核 `lib/demo.ts`、`WorldPanel`、`AboutDialog`、i18n；决定 `world.dataset` 通用文字 | 复核无问题 | `world.dataset` 改为中性的"数据 {name} {version} · 使用条款见数据集说明"；D1 报告页脚"科研用途"受 M16 报告检查约束，未改，转 M16 与文档总编 | `check-cn-keys`；`datasetNote.test.ts` |
| 第 1 轮偏差 2：net 层没有 presence | M11 已在 `RtClient` 接口与 worker 中提供 `presence()` 与 4430/4431 不重连 | `RtProvider` 的 presence 发送改为直接调用 `client.presence()`（去掉鸭子类型）；观看排队 R81–R83 仍在 `app/power/viewerQueue.ts`（net 层仍无该函数） | `tsc`、`d21-shell` |
| 本轮用例发现：手机上 Toast 盖住"指令"按钮 | Toast 栈在未遮挡矩形右下角，正是手机视口窗口的"图层""指令"按钮位置；高负载下 Toast 停留较久时第二次点按"指令"被拦截（`d21-mobile` 手机例超时） | `styles/mobile.css`：手机上 Toast 栈上移 8 rem、宽度不超过矩形宽减 1.5 rem（360 px 手机不越界）；平板与桌面不变 | `d21-mobile` 手机例通过 |

### 0.2 改动文件（均在 M15 路径内）

| 文件 | 改动 |
|---|---|
| `apps/web/src/app/power/PowerController.ts` | 1 Hz 判定改为 `freezableInterval`；`perfPause()` 与 `PERF_SETTLE_MS` 清除定时器；卸载时清理 |
| `apps/web/src/app/power/store.ts` | `perfPaused` 字段、`PERF_SETTLE_MS` |
| `apps/web/src/main.tsx`、`apps/web/src/ui/shell/urlSync.ts`、`apps/web/src/ui/sandbox/session.ts` | store 写入窗口、URL 同步、自动续期检查改为可冻结定时器 |
| `apps/web/src/ui/hud/PerfHud.tsx`、`apps/web/src/ui/panels/perf/PerfPanel.tsx` | 节能期间"已暂停"、不判红、1 Hz 卡片冻结 |
| `apps/web/src/ui/shell/guards.ts`、`apps/web/src/ui/panels/drones/DronesPanel.tsx` | `hint.powerSaving`；节能时无 STALE 龄；`railAltitude` 按高度基准 |
| `apps/web/src/ui/mobile/QuickCommand.tsx` | 共用识别物列表、`targetPos()` |
| `apps/web/src/ui/panels/targets/{QuantCard,TargetsPanel,targetModel}.tsx/ts` | 精简列与"全部列"、外层测宽；`RosterTargetGate` 与 `itemsFromDetails(keepCfg)` |
| `apps/web/src/ui/sandbox/SessionBar.tsx` | 操作区流内、容器查询隐藏顺序 |
| `apps/web/src/ui/shell/RtContext.tsx` | presence 直接调用 `RtClient.presence` |
| `apps/web/src/ui/testing/d21Hooks.ts` | 测试构建 `__uxInject.targets()`（行、store、roster、配置与详情的 id，诊断用） |
| `apps/web/src/styles/mobile.css` | 手机 Toast 位置 |
| `apps/web/src/app/i18n/{zh-CN,en}.json` | 新增 `perf.paused`、`quant.cols.all`、`quant.cols.compact`、`hint.powerSaving`；改 `world.dataset`（共 2516 键，两种语言键集相同） |
| `apps/web/tests/m15/d21r2.test.ts`（新） | 7 例：roster 门控、旧行为、`targetPos`、精简列判定、`railAltitude`、可冻结定时器、`PERF_SETTLE_MS` |
| `apps/web/perf/m15/d21-shell.spec.ts`、`d21-mobile.spec.ts` | 见 0.1 各行"验证"列 |

诊断脚本（不登记、不入构建）：`.cache/wp12c/specs/timers.spec.ts`（init script 包装 `setInterval`、`setTimeout`、`requestAnimationFrame`，按创建栈归类节能期间的回调次数，经构建 source map 还原到源码行；只计次数，不测时延或占比）、`.cache/wp12c/specs/tdel.spec.ts`（live 栈 REST 删除识别物）。

### 0.3 第 2 轮偏差

1. **读数卡精简列只有 4 列**（14 §20.26.7 列出 10 列）：360 px 详情栏放不下更多列；其余读数在选中行的完整读数句与"全部列"中。
2. **性能读数"已暂停"的 2.5 s 余量**是界面侧处理；挂起间隙仍进入 M06 的帧间隔环（请 M06 评估是否影响 PerfGovernor）。
3. **`perf/m18/targets-panel.spec.ts` 未改**（属 M16 的 `apps/web/perf/**`）：以请求交 M16。
4. **D1 报告页脚"科研用途"未改**：改动会使 M16 的报告检查与 `tests/m16/report.test.ts` 失败，待文档总编定口径。
5. 第 1 轮偏差 1（会话 store 在 M15 过渡实现）、3（容量 store 接管用测试钩子）、6（内部页面未懒加载分块）、7（触控 Tooltip 只改关键处）仍然成立；偏差 2 部分解决（presence 与关闭码已在 net 层，观看排队函数仍在 M15）。

### 0.4 第 2 轮测试结果

| 项 | 结果 |
|---|---|
| 静态检查 | `tsc --noEmit` 0 错误；改动文件 `oxlint --type-aware` 0 问题（首次发现 `ui/**` 直接导入 `@/engine/targets` 违反 TS-BND-01，已改经 `@/app/shell/engineD2`）；`make lint` 退出码 0（`check-cn-keys` ok、2516 键；生成物 `--check` up to date；`m06-lint` ok；postadd 幂等；registry 140 例） |
| Vitest | unit + browser 全量：188 个文件通过、1 个跳过（原状），1433 例通过、1 例跳过、0 失败；其中新增 `tests/m15/d21r2.test.ts` 7 例；`tests/m15/` 36 个文件 287 例 |
| Playwright（测试构建，`ci` 通道与固定 release，`--project perf`，只有功能断言） | `perf/m15/d21-shell.spec.ts` 9/9、`perf/m15/d21-mobile.spec.ts` 11/11（最终构建，5.1 min）；M15 回归 `fxweb2`、`interaction`、`layout-probe`、`report`、`responsive`、`sandbox-d2`、`smoke`、`tasks-d2` 36/36 |
| Playwright live 栈（`tests/e2e/sbstack.py`，测试构建） | `perf/d21/mobile-live.spec.ts` 快捷指令三例：悬停与返航、盯防（列出 person-01 并生成 `point_watch`）通过；"返航 20 s 内受理"在三例连续运行时失败 1 次（同机另一工作包 pytest 在跑，load 约 12），单独运行在本轮构建与第 1 轮之后的构建上各通过 1 次，本轮未改该路径（已告知 M16）；REST 删除识别物诊断见 0.1 |
| 诊断（不登记） | 节能 10 s 内周期回调：第 1 轮之后 32 次（5 个来源）→ 本轮 10 次（只剩 M06 动画采样器），viewer 与 sandbox 两种角色相同，期间 0 个网络请求 |
| pytest | 全量 `pytest -m "not perf"`（与另一工作包的 pytest 同机并行，本进程 `nice 15`、`AWR_PORT_OFFSET=8`，1 h 29 min）：4244 通过、9 失败、88 跳过、62 未选。9 例失败都在 Python e2e 与运行时（`test_capacity_e2e` 观看席位 1 例、`test_identity_e2e::test_r79_constant_time`、`test_sandbox_park` 4 例、`test_cli::test_config_print_masks_and_profile`、`test_supervisor::test_zenoh_integration_sys_procs_ready_and_cli`、`test_pose_chain_e2e` 1 例），本包未改 Python。安静后单独复跑：容量、身份、位姿链 3 例通过；`test_sandbox_park.py` 整文件 8/8；`test_cli` 与 `test_supervisor` 两例断言缺省端口，去掉 `AWR_PORT_OFFSET` 后 2/2 通过 |
| 性能基准 | 未跑（并行阶段）；D2-AC-44/46 主线程占比、旋转重排、揭开劣化留给 M16 harness |

截图（`.cache/impl/shots/m15-d21-*.png`，本轮 34 张，测试构建）逐张用 Read 自检，本轮发现并修复：

1. 节能时机群栏每行"STALE 0.1 S"、环境面板"与服务器断开，命令暂不可用"：改为不显示 STALE 龄、命令禁用原因"节能中，任意操作恢复实时连接后可下发命令"。
2. 节能时 HUD 的"—"以 KPI 字重显示成一条粗横线：节能时数值留空，只显示"已暂停"。
3. 读数卡精简列在 360 px 详情栏仍裁掉"P 合成""判定"（LfTable 自带滚动容器，外层"无横向滚动"的断言没有发现）：精简列减为 4 列，用例改为检查每个表头都在卡内。
4. 1600 px 会话条在约 310 px 空余时也隐藏了实体计数（阈值过保守）：容器查询阈值改为计数 62 rem、日历 72 rem，1920 px 计数可见且不与操作区重叠。
5. 手机 Toast 盖住"指令"按钮：手机 Toast 栈上移并收窄。
6. 读数卡首次显示时为全部列（测宽的元素在读数到达前不存在）：测宽改在始终挂载的外层。

### 0.5 第 2 轮遗留

- 节能期间剩余的周期任务只有 M06 动画采样器（d2-WP-12c-to-M06 第 5 条）；D2-AC-44 ≤ 2%、D2-AC-46 ≤ 1% 的主线程占比须在 M06 处理后由 M16 在安静环境复测（并行阶段未测）。
- 会话 store 整体替换、容量 store 生产接管 API（M17，第 1 轮请求仍未处理）。
- `targets-panel` 用例的 SPAWNING 时序与 `test.fail` 标记（M16）。
- D2.1 性能部分（旋转重排、揭开劣化、场视图切换、节能主线程）留给 M16 harness。

### 0.6 第 2 轮请求（追加在第 1 轮的同名文件末尾）

| 文件 | 内容 |
|---|---|
| `.cache/impl/requests/d2-WP-12c-to-M06.md` 第 5、6 条 | 动画采样器按 `powerFrozen()` 停止；挂起间隙不计入帧间隔环（或恢复后首帧丢弃） |
| `.cache/impl/requests/d2-WP-12c-to-M16.md` 第 5–7 条 | `targets-panel` 第 2 例等待 ACTIVE 并去掉 `test.fail`；本轮新增断言；D1 报告页脚口径 |
| `.cache/impl/requests/d2-WP-12c-to-文档总编.md` 第 7–12 条 | 性能读数暂停、读数卡精简列、机群栏高度基准、`world.dataset` 文案与报告页脚、会话条窄宽度取舍、手机 Toast 位置 |

---

以下第 1–5 节为第 1 轮（2026-10-06）的原始记录，第 2 轮的变化见第 0 节。

## 1. 实现清单（需求与验收编号）

| 需求 | 实现 | 主要文件 | 验收与测试 |
|---|---|---|---|
| M15-FR-223，UX-FR-148 版本号（R-D2-29，ADR-118） | `lib/build.ts` 读 `__AWR_BUILD__`，显示串与插件 `buildDisplay` 一致（`V0.2.0-demo · 3f9a1c2 · 2026-10-12`，dirty、nogit）；启动屏版本行（静态文本由 M00 写入）点击或长按复制完整构建信息（演示入口按需加载 `lib/build`，本机构建在 `main.tsx`）；关于与设置 › 关于显示同一串并有"复制诊断信息"；BOOT_ERROR 时静态行仍在 | `lib/build.ts`、`app/demo/entry.tsx`、`main.tsx`、`ui/views/AboutDialog.tsx` | D2-AC-40 功能部分：`d21-shell` 第 1 例（禁用 JS 可见、与 build-info.json 与 meta 一致）、第 2 例（关于页同串）；`d21.test.ts` build 组 |
| M15-FR-224，UX-FR-149 五段分格进度条（R-D2-30，ADR-119） | 写入器对 `[data-seg]` 逐段写 `--seg-p`（段内单调、减少动效时整段跳变、揭开全满），总进度仍写 `--boot-p`；`boot.css` 删除 D2 单条缩放规则，段填充 g50、未开始段只有轨道；演示入口的 bundle 监视写 bundle 段；底图切换加载卡与节能恢复加载层用同一分格轨道（`SegmentBar`） | `app/boot/progress.ts`、`styles/boot.css`、`ui/boot/{LoadingLayer,SegmentBar}.tsx` | D2-AC-41：段宽比与权重 ±2 px（`d21-shell` 第 1 例）；`d21.test.ts` 写入器组；`bootProgress.test.ts` 原 11 例通过 |
| M15-FR-225，UX-FR-150 载入期容量与"需排队"（R-D2-32，ADR-119） | 演示入口首个微任务即发 `GET /api/sandbox/v1/capacity`（1.5 s 超时，遮罩期间每 5 s），不经 store、不门控；应用启动后 `app/boot/capacity.ts` 把在途请求交给 M17 `stores/capacity`（不重复请求，间隔 ≥ 5 s 不破），揭开即停；文案 `capacity.*`（开放、观看已满、沙盒已满、两者、维护、不可用、"+1 内部"、`< 1 min / n min / n h / 暂无法估计`）；落地页（无 zustand 的同一请求，每 10 s）、开启沙盒 Sheet（容量 store 租约）显示同一口径与"需排队 · 第 y + 1 位 · 约 t"；`build.release` 不一致时"站点已更新 · 刷新"横条（不自动刷新、同一 release 一次） | `app/i18n/capacity.ts`、`app/boot/capacity.ts`、`app/demo/{entry,Landing}.tsx`、`ui/sandbox/StartSheet.tsx`、`ui/notify/UpdateBanner.tsx`、`styles/boot.css` | D2-AC-41 文案部分：`d21-shell` 第 1、2 例（四种响应、间隔 ≥ 5 s、不可达不推迟揭开、横条一次）；`d21.test.ts` 容量组 |
| M15-FR-226，UX-FR-151 排队卡片三种类型（R-D2-31、34，ADR-120） | `QueueStack`（观看卡在上）：`sandbox`（D2）、`resume`（恢复沙盒需排队 · 第 n 位，仿真时间与预计等待，离开队列、结束并下载快照；页面可见时自动认领）、`viewer`（实时观看排队中 · 第 n 位，期间静态浏览，`seat_ready` 自动连接）；手机上全宽、位于标签栏上方；可见时持有容量 10 s 轮询租约 | `ui/sandbox/QueueCard.tsx`、`app/power/viewerQueue.ts`、`ui/sandbox/session.ts` | D2-AC-43 前端部分：`d21-shell` 第 4 例（4431 → 卡片 → 长轮询 → 自动连接）；`d21.test.ts` 恢复票组 |
| M15-FR-227，UX-FR-152 节能控制器（R-D2-33、34，ADR-121） | 纯状态机 `machine.ts`（ACTIVE、QUIET、WARNING、SAVING、RESUMING、QUEUED，AWR-04 §15.7.3 全部转移；`none` 角色不进安静档）+ 控制器：交互检测（pointerdown、按下或累计 ≥ 8 px 的 pointermove、wheel、touchstart、keydown（单独修饰键不计）、方向变化、≥ 10 % 尺寸变化、手动控制中）、可见性（隐藏 hidden_s、pagehide 与 freeze 立即）、1 Hz 判定；QUIET 帧上限 30（手机 20）；SAVING：最后一次 presence、viewer 关闭实时连接（`linkOpen` → RtProvider 关闭客户端）、沙盒 `park(idle|hidden)` 或信标 SB-72、服务端 4430 直接进入、渲染挂起（`frameCap.suspend('power')` + `requestFrame`）；恢复：viewer 重连、沙盒 SB-70（201 新令牌、200、202 恢复票、410 结束），恢复加载层三段；阈值取容量 `power` 与 `serverInfo.power`；揭开前不计时；本机 `local` 表面不节能 | `app/power/{machine,PowerController,store,viewerQueue}.ts`、`ui/shell/RtContext.tsx`、`ui/sandbox/session.ts`、`app/shell/surface.ts` | D2-AC-44、45 前端部分：`d21-shell` 第 3 例（倒计时、3 px 抖动不取消、移动取消、SAVING 后 3 s 帧增量 ≤ 1 且无请求、首次拖动恢复到 LIVE、4430）、第 8 例（沙盒阈值后 SB-69 `reason: idle`、任意键 SB-70、回到 ACTIVE）；`d21.test.ts` 状态机组（6 例）与驻留组（3 例） |
| M15-FR-228 倒计时横条与覆盖层 | 横条（Alert 中性、role status、1 Hz）；覆盖层（`bg-overlay` 暗层 pointer-events none，不 inert、不抢焦点，第一次交互照常生效；角色文案、最后更新、版本）；会话条与时间轴"已暂停 · 最后更新 hh:mm:ss"；节能中不显示断线横条与红色断线徽标；内部身份"保持运行"开关 | `ui/power/PowerOverlay.tsx`、`ui/sandbox/SessionBar.tsx`、`ui/layout/{TimelineBar,AppHeader,ViewportOverlay}.tsx`、`ui/notify/Banners.tsx` | `d21-shell` 第 3、8 例截图自检 |
| M15-FR-229 设备档（ADR-122） | `lib/formFactor.ts`：narrow < 360、phone（宽 < 768 或短边 < 600）、tablet、desktop ≥ 1280；`pointer: coarse` 另判；写 `<html data-ff data-coarse data-orient>`；只在档位、方向、指针类别变化时通知（缩放风暴不重渲染）；切换只换外壳，画布、连接、选择、相机保留；`perceptionActions.setDeviceClass` 随动；小窗口守卫改为宽 < 360 | `lib/formFactor.ts`、`app/App.tsx`、`ui/layout/{breakpoints,layoutState}.ts`、`ui/sandbox/session.ts`、`ui/views/Sandbox.tsx` | D2-AC-47：`d21-mobile` 设备矩阵 8 例；`d21.test.ts` 设备档组 |
| M15-FR-230，UX-FR-153 平板指挥器 | 48 px 顶栏（品牌、世界 › 会话、连接、角色、场视图 Popover、横屏锁、全屏、更多）；左栏实体列表 320 px、右栏详情 360 px，停靠、浮动、48 px 图标栏，状态存 `awr.ui.dock.v1`，两栏都停靠时视口不足一半则后打开者浮动，无详情时浮动右栏折叠；64 px 任务条（任务药丸、四个快捷指令、倍率、告警计数 → 告警抽屉）；竖屏：视口 + 非模态吸附抽屉（25/50/90 %，任务条在抽屉顶部，机群、任务、识别物、详情四页）；虚拟摇杆沿用 D2 | `ui/mobile/{TabletShell,DockPanel,TaskStrip,parts}.tsx` | `d21-mobile` 平板例（停靠、浮动、收起、任务条、场视图菜单、长按、旋转保留选择、抽屉吸附） |
| M15-FR-231，UX-FR-153 手机 | 36 px 状态条（世界、角色、剩余或已暂停、连接、更多含会话操作）；五个单窗口（视口、机群、任务、识别物、告警），隐藏窗口保持挂载与滚动；离开视口窗口时挂起画布；竖屏底部 56 px 标签栏、横屏左侧竖向；告警未确认数；视口窗口：选中机芯片、"图层"抽屉（相机模式、视图工具、场视图、感知体、图例）、"指令"按钮；手机不提供摇杆 | `ui/mobile/{MobileShell,TabBar,safeArea}.tsx` | `d21-mobile` 手机例（窗口切换一次一个、挂起与恢复、图层抽屉、横屏标签栏、旋转保留选择） |
| M15-FR-232，UX-FR-154 快捷指令 | 纯状态机（CLOSED、SELECT、ACTION、PICK_POINT、PICK_TARGET、CONFIRM）；前往（视口点地，高度取当前或 60 m）、悬停、返航（单机 `uav/{id}/cmd/*`，多机 `fleet/cmd/*`）、盯防（选识别物 → `point_watch` 任务）；结果 Toast（受理或原因码）；从"指令"按钮起每个指令 ≤ 4 次点按（已选机）；只读访客无入口 | `ui/mobile/{quickModel,QuickCommand}.ts(x)` | D2-AC-48 前端部分：`d21-mobile` 手机例（悬停与返航发出、`__ux.rtCalls` 含 `cmd/hover`）；`d21.test.ts` 快捷指令组（点按计数） |
| M15-FR-233，UX-FR-155 触控 | `styles/mobile.css`（不分层规则）：coarse 下按钮、标签、开关触发器、菜单项、输入等最小 44 × 44 CSS px，开关、复选、单选、滑块拇指以 44 px 伪元素扩展命中区（拇指 28 px），水平 Field 行 44 px；`touch-action: manipulation`、`overscroll-behavior: none`；安全区内缩；`bottom-sheet.tsx`（模态 BottomSheet 与非模态 BottomDrawer，拖拽把手与吸附点，把手为 Button 可点按循环）；长按菜单（M06 `touch.onLongPress`，锚定 DropdownMenu） | `styles/mobile.css`、`ui/components/ui/bottom-sheet.tsx`、`ui/mobile/parts.tsx` | `d21-mobile` 命中区审计（6 个 coarse 设备档 + 手机图层抽屉 + 平板两个方向）0 违例 |
| M15-FR-234，UX-FR-156 PWA 与全屏（P1） | `public/manifest.webmanifest`（fullscreen、display_override、start_url `/world/synthcity?source=pwa`、192/512/maskable 图标）；图标由 `tools/shadcn/brand/make-pwa-icons.py` 从品牌头像生成（标准库，确定性），brand.lock.json 登记；全屏按钮（不支持时为"添加到主屏幕" Popover）、横屏锁（全屏或独立模式且 API 存在时） | `public/manifest.webmanifest`、`public/brand/{pwa-*,apple-touch-icon}.png`、`tools/shadcn/brand/*`、`ui/layout/FullscreenButton.tsx` | D2-AC-50 功能部分：`d21-mobile` PWA 例；`check-brand` 通过 |
| M15-FR-235，UX-FR-157 身份区（R-D2-39，ADR-123） | 设置对话框最底部"高级"折叠区（普通说明文字样式、无图标），身份：说明、`Input type=password`（autoComplete off、spellCheck false、autoCapitalize none）、验证；口令只在组件局部状态，提交时先清空再把值交给 `identity.verify`（只进一次请求体）；四种结果（错误、锁定按 Retry-After 倒计时禁用、未配置禁用、错误提示）；成功：徽标、到期时刻、记住此设备 12 h（sessionStorage 与 localStorage 之间切换）、保持运行、退出（AlertDialog，R80 撤销）；不进命令面板、快捷键、帮助与深链 | `stores/identity.ts`、`ui/settings/IdentitySection.tsx`、`ui/panels/settings/SettingsPanel.tsx` | D2-AC-51 前端部分：`d21-shell` 第 5 例（折叠、错误、锁定、成功、请求体、存储与页面与控制台扫描口令 0 处、命令面板无条目、退出清除）；`d21.test.ts` 身份组 |
| M15-FR-236，UX-FR-158 内部界面 | `surface.internal` 修饰；演示构建解锁后重新登记被隐藏的路由（Jobs、Runs、回放、Bench、报告）与面板动作；顶栏"内部 · 全功能"徽标下拉：申请控制（身份令牌换 run admin token，R01）、释放、身份设置；菜单恢复重建任务、录制列表、Bench 入口；沙盒创建附加 `X-AWR-Identity`（M15 过渡 store 与 M17 net 两处提供者） | `app/shell/{internal,surface}.ts`、`app/router/table.ts`、`ui/layout/AppHeader.tsx`、`ui/shell/control.ts` | `d21-shell` 第 5 例（顶栏徽标）；分块扫描请求 M06（偏差 6） |
| M15-FR-237，UX-FR-159 场视图控件（R-D2-41、42，ADR-125） | 工具条（场视图五选一、感知体三档、物理参考；Shift+V 循环，manual 作用域屏蔽；`awr.ui.prefs.v1` 持久化）：桌面在未遮挡矩形底部中央，平板在顶栏 Popover，手机在"图层"抽屉；图例卡：灰阶 `--field-ramp-1…5`、单位、五档边界、可闻壳与背景噪声壳线型、模态点形（圆、菱形、环、十字，纯灰阶）、参考传感器、"模拟参考值 · 自由空间场"；PerfGovernor 关闭场视图时 Toast | `ui/field/FieldToolbar.tsx`、`stores/prefs.ts`、`styles/{theme,layout}.css`、`tools/shadcn/gen-theme-tokens.mjs` | D2-AC-53 控件部分：`d21-shell` 第 6 例（M06 合成剖面下的图例、Shift+V 五档、感知体、物理参考、持久化） |
| M15-FR-238，UX-FR-160 感知读数卡（R-D2-40，ADR-124） | 识别物详情"感知读数"标签：LfTable（机体、斜距、声学主导带级 / 阈、余量、P 声、视觉角尺寸 / 阈、等效像素、P 视、P 合成、判定），行点击 → 聚焦该机并高亮等值壳，选中行的完整读数句；判据模式徽标与"概率为物理参考"；概率说明 Popover；空状态（未选识别物、无机体、等待） | `ui/panels/targets/{QuantCard,TargetDetailPanel}.tsx` | D2-AC-52 前端部分：`d21-shell` 第 7 例（注入 `target/{id}/quant`，表格、读数句、Popover，无写请求）；`d21.test.ts` 读数句 |
| M15-FR-239，UX-FR-161 指挥器配对（P1） | 沙盒持有端菜单"配对指挥器"（桌面世界菜单、平板与手机"更多"）：SB-76 取 8 位一次性配对码，显示配对码、沙盒编号与 120 s 倒计时，可换新码；`/pair` 输入页（沙盒编号可由 `?sid=` 预填）：SB-77 兑换同一 principal 的沙盒令牌，会话端口 `sandbox.adopt` 采用后打开 `/sandbox/{sid}`，两端共享席位；错误显示原因码文案 | `ui/sandbox/PairDialog.tsx`、`ui/views/PairPage.tsx`、`app/routes/pair.tsx`、`lib/demo.ts`（`DEMO_ROUTES` 加 `pair`）、`ui/sandbox/{dialogs,session}.ts(x)` | M15-AC-226 配对部分：`d21-shell` 配对例（持有端显示码、平板上下文兑换、请求体、存储与跳转） |
| M15-FR-240 文案与图标 | 新增 i18n 键 204 个、改既有 2 个（`sandbox.smallWindow`、`shell.tooSmallHint`，按 ADR-122；zh-CN 与 en 键集相同，共 2509 个）；morphicons 语义键 21 个（`ui.fullscreen`、`ui.orientation-lock`、`ui.dock`、`ui.undock`、`ui.drag-handle`、`power.saving`、`power.resume`、`identity.shield`、`field.*`、`cmd.watch`、`cmd.quick`、`device.pair`、`tab.*` 等） | `app/i18n/*.json`、`tools/shadcn/icons/inventory.mjs` | `check-cn-keys`、`icons/verify`（0 错误）、`i18n.test.ts` |

请求处置：d2-WP-04b-to-M15（容量 store 的启动与租约、`etaBucket` 口径、驻留与恢复、`setSandboxIdentityProvider`、小窗口 360 px）已接线，会话部分落在 M15 过渡 store（偏差 1）；d2-WP-11b-to-M15 第 1–7 条已接线（第 2 条 `field.highlight`、第 4 条长按、第 5 条挂起与按需帧、第 6 条设备档、第 7 条小窗口挂起）；d2-WP-16-to-M15 第 1–3、6 条已处置（分段写入、删除单条规则、`lib/build.ts` 声明、apple-touch-icon 文件名回复 M00）；d2-WP-19b-to-M15 第 1–4 条已接线（读数列用 `lp_band_db / thr_dba`）。

## 2. 偏差

1. **沙盒会话仍在 M15 过渡 store**（`ui/sandbox/session.ts`）：驻留、信标、4430 标记、SB-70 恢复与恢复票、驻留令牌保留与身份头都加在过渡 store 上，与 M17 `stores/sandboxSession.ts` 的同名能力重复。整体替换牵涉 13 个使用方与 D2 用例注入点，按 d2-WP-12b-to-M17 第 3 条留给集成步骤（d2-WP-12c-to-M17 第 2 条）。
2. **net 层尚无 presence、4430/4431 处理与观看排队函数**（WP-03b 未交付）：presence 以鸭子类型调用 `client.presence`（方法不存在时不发），4430/4431 由 `onConnState` 的关闭码判断后关闭客户端停止退避，观看排队 R81–R83 过渡实现在 `app/power/viewerQueue.ts`（d2-WP-12c-to-M11）。
3. **容量 store 的启动接管用了 `configureCapacity`**（M17 标为测试钩子）以复用入口的在途请求，请 M17 提供生产 API（d2-WP-12c-to-M17 第 1 条）。
4. **容量文案模块放在 `app/i18n/capacity.ts`**：演示构建的落地页分块由 M00 的 `LANDING_MODULES` 正则决定，`app/i18n/` 已在其中；放在 `app/boot/` 会让落地页静态闭包带上 ui、state、three（已实测后改正）。
5. **平板浮动侧栏是非模态浮层，并计入未遮挡矩形**（PRD 写"以 Sheet 覆盖视口"）：模态 Sheet 会挡住视口操作；浮层仍覆盖画布，但相机工具、排队卡片与 HUD 不再被它盖住。拖动栏头切换停靠改为栏头按钮。无详情时浮动右栏折叠为图标栏。
6. **FR-236 分块扫描未做**：`prod-bundle-scan` 属 M06（d2-WP-12c-to-M06 第 4 条）。恢复的页面组件仍随路由表一起打包（路由模块为 eager glob），未改成懒加载分块；未解锁时这些路由不登记、入口不显示。
7. **触控下 Tooltip 改 Popover 只做了关键处**（概率说明、添加到主屏幕）；其余 Tooltip 的文字同时作为 aria-label 存在，触控设备不依赖悬停也能操作，但看不到提示文字。
8. **手机二级详情为窗口内页面栈**，不是全高 `Sheet side="bottom"`；手机会话条移入状态条与"更多"菜单；手机视口内不放相机模式条与视图工具（在"图层"抽屉），避免与 ViewCube 重叠。
9. **`local` 表面不节能也不进安静档**：非演示构建的本机运行（基准、浸泡、D1 用例）不进入节能，也不在 60 s 无交互后降到 30 fps（否则 flight60 等无人值守的性能用例会被限帧）；演示构建的共享展示为 viewer、沙盒为 sandbox；测试构建可用 `?power=` 指定角色与阈值。
10. **"保持运行"开关在身份区**，未另在"设置 › 显示"放一份。
11. **ViewCube 偏移由 M15 样式覆盖**（coarse 下视图工具列 44 px），请 M06 接手（d2-WP-12c-to-M06 第 2 条）。
12. **用例路径**：PRD 写 `tests/m15/{power,mobile-layout,touch-targets}.spec.ts`，该目录是 vitest；Playwright 用例放在 `perf/m15/d21-{shell,mobile}.spec.ts`（与 D2 的 `perf/m15/*-d2.spec.ts` 一致）。

## 3. 测试结果

### 3.1 静态检查

- `npm run typecheck`：0 错误。`npx oxlint --type-aware`（src、tests、perf）：0 问题。
- `make lint`：全部通过（退出码 0；check-release 在无 git 环境按设计跳过）。逐项含 no-emoji、no-hex、lint-lf、motion-lint、check-icons、no-raw-controls、check-brand（新增 4 个图标已入锁）、check-version、生成物 `--check`（含新的 `SCENE.fieldRamp`、281 个图标键）、check-cn-keys、postadd 幂等、m06-lint、registry。
- 演示构建分块复核（与 `tests/ops/test_deploy_install.py::test_demo_chunks_landing_and_api_docs` 同一算法）：落地页静态闭包 {index, landing, react, rolldown-runtime}；`/sandbox/api` 闭包 {rolldown-runtime, react, state, apidocs, landing}，不含 three、r3f、ui、engine、main。

### 3.2 Vitest

- 新增 `tests/m15/d21.test.ts` 26 例：构建串与诊断、五段写入器、容量文案六种与等待分档、节能状态机（安静、倒计时、取消、隐藏、pagehide、4430、排队与轮到、static 与保持运行、繁忙与内部阈值、服务端阈值合并）、8 px 抖动、设备档矩阵、停靠适配、吸附点、平板与手机矩形、快捷指令点按计数与回退、前往高度、图例档位、读数句、身份 store（成功、口令不入 store 与存储、记住此设备、退出发 DELETE、401/429 516/429 111/403 115）、过渡会话驻留与恢复（202 停放、201 新令牌、202 恢复票与离队回到 parked、410 结束 parked_expired）、驻留表面。全部通过。
- `tests/m15/sandboxSession.test.ts` 的小窗口用例按 ADR-122 改为 320 px（1200 × 700 与 390 × 844 现在可以开沙盒）。
- 全量 `npx vitest run --project unit --project browser`：184 个文件（183 通过、1 跳过），1393 例通过、1 例跳过、0 失败。

### 3.3 Playwright 功能用例（测试构建，`ci` 通道与固定 release，`--project perf`，只有功能断言）

| 用例 | 结果 |
|---|---|
| `perf/m15/d21-shell.spec.ts`（新增，9 例，含指挥器配对） | 9 / 9 通过 |
| `perf/m15/d21-mobile.spec.ts`（新增，11 例：设备矩阵 8、手机、平板、PWA） | 11 / 11 通过 |
| M15 原有：`fxweb2`、`interaction`、`layout-probe`、`report`、`responsive`、`sandbox-d2`、`smoke`、`tasks-d2`（回归，同一测试构建） | 36 / 36 通过（`smoke` 的小窗口例按 ADR-122 改为 320 × 640；d2-WP-16-to-M15 第 7 条提到的 `sandbox-d2` 识别物例本轮通过） |

截图（`.cache/impl/shots/m15-d21-*.png`，共 30 余张）逐张用 Read 自检，发现并修复的问题：

1. 节能时出现"与服务器的连接已断开，正在重连"横条、顶栏红色"已断开"与视口"离线"徽标（节能是有意断开）：节能期间不显示这三处。
2. 平板顶栏下重复渲染了会话条且在本机表面也出现：去掉重复的一份（会话条由 Banners 渲染）。
3. 平板浮动右栏盖住相机工具条与世界卡片：浮动栏计入未遮挡矩形；无详情时折叠。
4. 会话条与横幅压住相机工具与视图工具：平板、手机的未遮挡矩形加上横幅高度，浮动栏从横幅下方开始。
5. 手机上相机模式条与左上"合成数据"徽标、ViewCube 与视图工具列重叠：手机移入"图层"抽屉；平板与 coarse 桌面给 ViewCube 让出 44 px。
6. 手机会话条"剩余"两个字被挤成竖排：手机不显示会话条，剩余时间在状态条，会话操作在"更多"菜单。
7. 节能卡片沙盒文案"仿真时间 —"：服务器未给 `t_sim_ns` 时用进入节能时的时间轴仿真时刻。
8. 会话条加了"已暂停"徽标后计数挤叠：节能时以"已暂停 · 最后更新"替换剩余时间，而不是追加。
9. 读数卡表格只露一行、标题与"判据"徽标折行：表高按行数加表头计算，标题去掉（标签已是"感知读数"），判据徽标改短文案。
10. 图例"可闻壳（人（缺省阈值））"括号嵌套：改为"人，缺省阈值"。
11. 命中区审计首轮 6 处（Tooltip 包裹的按钮 `data-slot` 被改写、输入组 28 px、贴边开关的命中区被滚动容器裁掉、开关上方行压住命中区）：44 px 规则改为按元素与角色匹配，输入组自适应高度，停靠栏内边距 12 px，水平 Field 行 44 px。
12. 一处条件调用 hook（`Banners` 中 `&& !usePower(...)`）在节能切换时触发根错误边界（M15-E004），由 `d21-shell` 的观看节能例暴露：改为先调用 hook 再做逻辑运算。
13. 手机横屏的状态条被左侧标签栏盖住世界名：状态条从标签栏右侧开始；手机窗口底部与标签栏之间露出画布：窗口铺到底边，内容留出标签栏高度。

### 3.4 pytest

本包未改 Python 代码。`pytest -m "not perf" --continue-on-collection-errors`（与其他工作包同机，1 h 7 min）：4073 例通过，9 例失败，89 例跳过，1 例 xfail，1 个收集错误。失败与 D2-WP-12b 报告 §3.5 所列相同的既有问题一致，都在其他工作包正在改动的 Python 部分：机型目录与 P600 能量口径（`tests/agent/test_scoring.py::test_norms_by_profile`、`tests/mission/test_energy.py::test_s1_precheck_soc`、`tests/mission/test_s1.py::test_s1_end_to_end`、`tests/safety/test_battery.py::test_p600_hover_endurance`、`tests/safety/test_energy_rtl.py::test_energy_rtl_lands_home`、`tests/sim/test_profiles.py` 两例）、端到端时序（`tests/sensors/test_pose_chain_e2e.py`、`tests/sim/test_startup.py`），收集错误为 `tests/sim/test_lowpoly_fw.py` 的 `_common` 模块重名。与前端产物相关的 `tests/ops/test_deploy_install.py`（含落地页分块用例）、`test_deploy_nginx.py`、`test_skeleton.py`、`test_lockfiles.py`、`test_nginx_site_e2e.py`、`tests/rt/test_static_models.py` 单独复跑：184 通过、12 跳过。

## 4. 遗留

- **指挥器配对的大屏展示**：配对码以对话框显示，未做二维码（AWR-04 §15.3 列为不在 D2.1）。
- **会话 store 整体替换**（偏差 1）、**net 层 presence 与关闭码、观看排队函数**（偏差 2）、**容量 store 生产接管 API**（偏差 3）。
- **分块扫描与懒加载的内部页面**（偏差 6）。
- **触控 Tooltip 全量改 Popover**（偏差 7）。
- **PWA 清单的 theme_color、background_color 注入与 apple-touch-icon 链接**（M00，d2-WP-12c-to-M00 第 1、2 条）。
- **共享展示被内部人员持有席位时 viewer 端"内部人员操作中"徽标**（Q-D2-13）：需要 serverInfo 告知持席者身份类别，等 WP-03b。
- **性能部分**（并行阶段不跑）：节能期间主线程 ≤ 2 %、旋转重排 ≤ 300 ms、揭开劣化 ≤ 5 %、场视图切换 ≤ 300 ms、移动端真机帧率，交 M16 harness 与 MS5 人工记录。

## 5. 跨所有权请求

| 文件 | 内容 |
|---|---|
| `d2-WP-12c-to-M11.md` | presence 方法；4430/4431 不自动重连与 error 517/518；serverInfo.power 与 identity；观看排队函数；身份令牌的 REST 头与 WS 子协议；服务端端点；本机 profile 不兜底回收 |
| `d2-WP-12c-to-M17.md` | 容量 store 生产接管 API；会话 store 替换清单（含 D2.1 驻留与恢复行为）；容量 `power` 可选字段；SB-70 接受驻留期间过期令牌 |
| `d2-WP-12c-to-M06.md` | 已接线清单；ViewCube coarse 偏移；`SCENE.fieldRamp`；prod-bundle-scan 的身份与口令检查 |
| `d2-WP-12c-to-M00.md` | 清单颜色注入；apple-touch-icon 链接；落地页分块约束；nginx 路由与缓存头；测试构建的构建信息环境 |
| `d2-WP-12c-to-M16.md` | 新用例与运行方式；测试钩子与 `?power=`；命中区审计；未跑的性能部分 |
| `d2-WP-12c-to-文档总编.md` | 文件路径、浮动栏语义、手机二级页与会话条、保持运行位置、本机不节能、竖屏抽屉缺省吸附点 |
