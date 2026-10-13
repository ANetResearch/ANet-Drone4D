# D2 WP-12b UI 接线与打磨：实现报告

| 项 | 内容 |
|---|---|
| 工作包 | WP-12b UI 接线与打磨（所有者 M15；WP-12 的第二部分） |
| 日期 | 2026-10-05 |
| 依据 | AWR-14 §20.5.4、§20.9（20.9.1 至 20.9.4）、§20.10.5、§20.10.6、§20.13.5、§20.2.2；M15 PRD §15（M15-FR-211 至 FR-214、FR-218 至 FR-220，AC-207 至 AC-214）；ADR-086、ADR-087；各模块交来的 d2-WP-06/09/10/11-to-M15 |
| 路径 | M15 的路径：`apps/web/src/{app,ui}/**`、`apps/web/src/stores/prefs.ts`（M15 PRD §1.1 规定归 M15）、`apps/web/tests/m15/**`、`apps/web/perf/m15/**`（登记请求见 d2-WP-12b-to-文档总编 第 1 条） |
| 约束执行 | 没有写其他工作包的路径，跨所有权的需求写成 9 份 `.cache/impl/requests/d2-WP-12b-to-*.md`；没有安装依赖；没有执行 git 写操作；没有跑性能基准；没有 emoji 和禁用字形；颜色只用 token；组件只用 shadcn（base-mira），图表与表格用 lieflat，动效用 transitions.dev token，图标用 morphicons；不新增渲染代码（HUD 与捕获框是 DOM 浮层，每帧只写 transform），不增加着色器、UBO 或纹理，WebGL2 规范最低限值不受影响；身份口令没有写进任何文件 |
| 结论 | P0 部分全部接上真实 store：任务（M20 `stores/tasking.ts`）、感知（M19 `stores/perception.ts` 与计算器）、目录（M21 `stores/catalog.ts`）、识别物（M18 `stores/targets.ts`、`engine/targets`）、传感器视图（M06 门面）、环境日历（M07）。新增内容：新建任务页（地图绘制、参数、所需 GSD、前置校验、预览分配、启动）、任务表、接力监控页、FPV 传感器视图条与 HUD、GSD 与识别距离计算器、日历与声学背景 Popover。按 14 §20 在 1920 × 1080 和 1280 × 720 两个尺寸下截图自检了 34 张，发现并修复 11 处问题。沙盒会话 store 只统一了工作量证明，整体替换留到集成步骤（偏差 1）。`make lint` 全部通过 |

## 0. 摘要

1. **新建任务**（M15-FR-214，UX-FR-126、127）：右栏 `task-new` 页。七种类型用 ToggleGroup 切换，每种带适用说明。绘制工具复用 D2 工具状态机，新增 `TASK_POINT`、`TASK_LINE`、`TASK_AREA`、`TASK_PICK_TARGET`：通过 `pick.pickAt` 在地面上取点；Enter 或双击结束绘制；Backspace 删除最后一个点；Esc 取消；最多 64 个点；与上一个点相距不到 0.5 m 的点不添加。绘制过程中实时显示顶点数和面积。参数表单按 14 §20.9.1 给出缺省值：目标类别与所需等级（同时显示"所需 GSD：人 R 级 ≤ 2.7 cm"，口径同 Python `decompose.g_req_m`）、重访周期与架数（自动或指定）、定点的高度、驻留、朝向与盘旋半径、巡线的往返方式、圈数、速度与相机朝向、扫描的 GSD、旁向重叠、最大入射角、时间上限、限高、净空与多架次、巡边的外扩距离与方向、接力等级，"更多参数"里有优先级、开始时刻、允许的机巢与机型、发现后的动作。前置校验与服务端用同一组规则：自相交、面积 > 1 km²、顶点 > 64、条带数粗估、航点数。不通过时显示 585 文案，"预览分配"按钮置灰。提交的请求体是 SB-53：环一律为逆时针，坐标取到 0.1 m，公共参数只在改过缺省值时才带上。
2. **预览分配与启动**（UX-FR-128）：`taskingActions.preview(spec)` 的结果画在 M20 的规划图层上，以虚线显示。预览卡给出：派出架数和"为什么是 n 架"、完工时间和时间上限、条带与子区、每架机的工作项（`itemsText`）、可持续性与瓶颈、告警。不可行时显示原因码文案、`detail.why` 和 `remedies[].text_zh`；`fleet_short` 会额外给出"添加无人机"按钮，打开机队的添加对话框。启动时带上预览的 `preview_id`。
3. **任务表与接力监控**（UX-FR-129、130 部分）：Dock 的"任务"标签用 LfTable 列出任务：类型图标、状态、架数、20 格进度刻度、完工或剩余时间、发现后的动作、接力 KPI，可按运行中、已结束、不可行筛选。行菜单可以开始、暂停、恢复、取消（取消要 AlertDialog 确认）、在视口聚焦、查看分配。展开后显示工作项、子任务和告警。"接力监控"是单独的 Dock 标签：用下拉框选择接力任务，6 个 LfStat（覆盖率、间隙次数与最长间隙、被发现次数（大于 0 时显示红色）、交接次数与平均重叠、最小禁入余量、电池最短可用库存）；站位表列出站位、角色、在岗机和在岗时长；"下一架接替"显示接替机、倒计时和原因；间隙按原因分列。覆盖率曲线（P1）用静态 LfLine 显示最近 120 个点。倒计时用 BoundText 以 4 Hz 更新，不经过 React 渲染。
4. **传感器视图与 HUD**（M15-FR-218，UX-FR-136、137）：FPV 顶部的传感器视图条可在 FPV、日视、夜视、热像之间切换（只列出可用的视图；键 B 循环切换）。另外有：传感器选择；调色板（白热、黑热，operator 同时发 `sensor/mode`）；LWIR 数字变焦 ×1、×2、×4；补光开关（键 I）；右侧变焦轨（对数刻度，operator 才能用，每 200 ms 最多发一次 `zoom`，键 - 和 =）；云台菜单（跟踪识别物、回中）；"视觉近似"标注。HUD 读数卡显示最近识别物的名称、所需等级、像素进度 `N_eff / N_req`（例如 57.8 / 14 px）和进度条、限制因素的本地化文字、焦距、HFOV、斜距、GSD，以及捕获前的保持计时。捕获框最多 8 个：每帧用 `sensorView.projectBox` 求出位置，再用 `loop.register('overlay')` 写入 transform，标签不会超出视口边缘。框的红色由一处红仲裁决定：只有视口红色主体为空或者就是这架主机时才显示红色。环境（照度、太阳方向、地表温度与发射率）以不超过 4 Hz 的频率送给 `sensorView.setEnv`；目录详情到达后，用 `vehicleModels.register` 登记机型外形。
5. **GSD 与识别距离计算器**（UX-FR-144，M15-FR-220 的计算器部分）：入口：命令面板的"GSD"、新建任务页和机型目录传感器旁的"GSD 计算器"按钮（Dialog）。可选择参考传感器（GX40 及其近红外、近红外加外挂照明、LWIR 640 的 25 mm 与 100 mm 镜头）或目录中的成像传感器，再选择焦距（对数滑条）、识别物类别、所需等级、距离和照度档。即时给出 GSD、目标上的像素数、TTPF 概率、各等级的最大距离和可行窗口（`feasibilityDetail`，调用间隔不小于 400 ms）。所有计算都用 M19 的 `engine/perception`。这一项也回应了用户要求的"识别物在视野内的分辨率（尺寸、距离）"量化。viewer 可以使用，不发写请求。
6. **日历与声学背景**（UX-FR-114）：会话条和环境面板的 Popover 只读显示世界时区的当地时间、照度档、太阳高度角和照度。有写权限时，可以输入 `YYYY-MM-DD HH:mm`，转成 UTC 后调用 `env/calendar {start_utc, tz}`；有白天 10:00、黄昏 18:30、夜间 23:00 三个快捷时段；可以设置声学背景（乡村、郊区、城市，经 `env/set`，并显示背景声级 dBA）。能见度低于 3 km 时提示改用长焦热像。
7. **领域 store 接线**：机巢面板改读 `stores/tasking.ts` 的 `nests` 和 `nestStatus`（槽位、库存、能量参数、充电通道和电池队列、在巢与在空机体、回收队列）。目录 Sheet 和克隆表单改用 `stores/catalog.ts`：先读型号再读传感器；克隆前先取详情，用于预填并锁定字段。识别物列表优先用 `targetsModel` 的行；拿不到行时，用 roster、会话识别物配置和 detail 消息合成列表（偏差 3）。手动控制的禁入体预测改用 M18 的 `manualKeepoutWarning`。接管确认框显示这架机当前的任务工作项。写权限统一为 `d2WriteAllowed(surface, canEdit, stale) = !stale && (surface === 'sandbox' || canEdit)`。

## 1. 实现清单（需求与验收编号）

| 需求 | 实现 | 主要文件 | 验收与测试 |
|---|---|---|---|
| M15-FR-214，UX-FR-126 七类任务的绘制工具与参数缺省 | 类型栏（带适用说明 Tooltip）；`drawKindOf` 把类型映射到点、线、面或识别物；参数表单与缺省值；所需 GSD 显示；"更多参数" | `ui/panels/tasks/{TaskNewPanel,taskDraft,taskModel}.ts(x)`、`ui/tools/d2Tools.ts`、`ui/panels/builtin.ts`（`task-new` 登记）、`ui/layout/DroneRail.tsx`、`stores/prefs.ts`（`right.page = 'task'`） | UX-AC-055 功能部分：`tests/m15/tasking.test.ts`；Playwright `tasks-d2` 第 2 例（真实点击绘制多边形） |
| UX-FR-127 绘制交互与前置校验 | 绘制提示条（键位、顶点数和面积）；Enter 或双击结束；Backspace；Esc；最多 64 个点（超出时闪现提示）；自相交多边形不能闭合；面积、顶点、条带、航点校验（585） | `ui/tools/d2Tools.ts`、`taskModel.precheck` | `tasking.test.ts`（9 种 issue）；`tasks-d2` 第 2 例 |
| UX-FR-128 预览分配 | 规划图层虚线（M20 图层）；架数与理由；完工与时间上限；覆盖摘要；分配表；可持续性与瓶颈；告警；不可行原因、why、建议，`fleet_short` 时给出"添加无人机"；启动时带 `preview_id` | `ui/panels/tasks/TaskPreviewCard.tsx`、`taskModel.{previewSummary,itemsText,warningKey}` | UX-AC-056 功能部分：`tasks-d2` 第 2、3 例 |
| UX-FR-129 任务表与接力子页 | 任务表（LfTable）、筛选、热行、行操作（取消要确认）、展开显示工作项、子任务和告警；接力监控标签有 6 个 KPI、站位表、下一架接替和倒计时、按原因分列的间隙；被发现次数大于 0 时显示红色 | `ui/panels/tasks/{TasksPanel,RelayPanel}.tsx`、`ui/layout/{dockTabs,BottomDock}.ts(x)` | UX-AC-057 功能部分：`tasks-d2` 第 1 例；`tasking.test.ts`（筛选、进度、热行、行操作、站位行、倒计时、Dock 标签切换） |
| UX-FR-130 覆盖率曲线（P1） | 静态 LfLine，最近 120 个点，有参考线（目标覆盖率） | `RelayPanel.tsx`、`ui/lf/LfLine.tsx`（增加 `refLine`、`heroIndex`、`xLabels`） | 人工检查截图 |
| M15-FR-218，UX-FR-136 传感器视图 | 视图条（四种视图，不可用的置灰）；键 B；传感器选择；调色板；数字变焦；补光（键 I）；变焦轨（operator，≤ 5 Hz；键 - 和 =）；云台菜单；"视觉近似"标注；URL 参数 `sv` | `ui/fpv/{SensorViewBar,bindings}.ts(x)`、`ui/actions/d2Actions.ts`、`app/GlobalLayers.tsx`、`ui/layout/ViewportOverlay.tsx` | UX-AC-060 功能部分：`tasks-d2` 第 5 例（真实 viewport）；切换耗时和 programs 增量归 M06 的性能用例 |
| UX-FR-137 HUD | 读数卡（像素进度、限制因素、焦距、HFOV、斜距、GSD、保持计时）；最多 8 个捕获框（每帧写 transform，标签不超出视口）；一处红 | `ui/fpv/{PerceptionHud,hudModel}.ts(x)` | UX-AC-061 功能部分：`tasking.test.ts`（`sensorFrameOf`、`boxState`、`nearestTargets`）；`tasks-d2` 第 5 例 |
| UX-FR-144，M15-FR-220（计算器） | 计算器 Dialog；参考传感器加目录传感器；焦距对数滑条；GSD、N、TTPF、各等级最大距离、可行窗口 | `ui/views/{GsdCalculator,gsdModel}.ts(x)` | UX-AC-065 的"与 M19 一致"：`tasking.test.ts` 直接与 `engine/perception` 对拍；`tasks-d2` 第 4 例 |
| UX-FR-114 日历与声学背景 | Popover：只读显示时间和照度；有写权限时可输入日历、选快捷时段、设置声学背景；拒绝时把原因写在字段下方；低能见度时给出提示 | `ui/sandbox/{CalendarPopover,calendar}.ts(x)`、`ui/sandbox/SessionBar.tsx`、`ui/panels/env/EnvPanel.tsx` | `tasking.test.ts`（`localToUtc`、`periodText`）；截图 |
| M15-FR-213（接线） | 机巢面板读 M20：通道按 1 起编号，空闲和阻塞分组；库存；能量参数；充电器说明；机体列表；回收队列；电池延迟用虚线描边 | `ui/panels/nests/{NestsPanel,nestModel}.ts(x)` | AC-209 功能部分：`tasks-d2` 第 4 例；`tasking.test.ts`（`channelRows`） |
| M15-FR-211（接线） | 目录与克隆改用 M21：读取顺序；克隆前先取详情；预填和锁定字段（`LOCKED`、`baseValues`、`changedFields`）；目录卡片上的 GSD 提示；D2 表面上隐藏 D1 的"添加 P600" | `ui/panels/fleet/{CatalogSheet,FleetBar,fleetModel,fleetDialogs,PayloadControls}.ts(x)` | AC-207 功能部分：`tasks-d2` 第 4 例、`sandbox-d2` 第 4 例；`tasking.test.ts` |
| M15-FR-212（接线） | 识别物列表优先用 `targetsModel` 的行，拿不到时用合成数据（偏差 3）；详情；放置后回调；提示文案 | `ui/panels/targets/{TargetsPanel,TargetDetailPanel,targetModel}.ts(x)` | AC-208 功能部分：`sandbox-d2` 第 4 例；`tasking.test.ts`（`itemsFromDetails`） |
| M15-FR-215（接线） | 禁入体预测改用 M18 的 `manualKeepoutWarning`；接管确认框显示当前工作项 | `ui/manual/{controller,ManualOverlay}.ts(x)`、`app/shell/install.ts` | `tests/m15/manualControl.test.ts`；截图 |
| 世界组（D2 表面） | 左栏"世界"组：场景、底图、数据集说明、统计、流式点数 | `ui/panels/world/WorldPanel.tsx`、`app/query/options.ts` | 截图 |
| M15-FR-220 viewer 只读交互（AC-214 功能部分） | viewer（共享展示或没有写权限的连接）可以使用任务标签、接力监控、计算器、FPV 四种传感器视图、调色板和 HUD。viewer 看不到变焦轨、云台菜单和"新建任务"；变焦和补光键只在有写权限时生效。测试构建的 `__ux.rtCalls` 记录页面客户端的每次 call | `ui/testing/uxProbe.ts`（`probeRtCalls`）、`ui/shell/RtContext.tsx` | `tasks-d2` 第 6 例：REST 写请求 0 个（M04 世界查询和连接 token 除外），实时 call 0 个；第 5 例的 operator 变焦键是这个探针的正对照 |
| §20.2.2 深链 | `?panel=tasks`、`relay` 打开 Dock 标签，`?panel=task-new` 打开新建任务页 | `ui/sandbox/entities.ts` | `tasking.test.ts` |
| 命令面板与键位 | `camera.sensorView`（B）、`tool.gsd`、`task.new`（有写权限时）、`panel.tasks`、`panel.relay`、`fpv.zoomOut`（-）、`fpv.zoomIn`（=）、`fpv.illum`（I） | `ui/actions/d2Actions.ts` | `tasks-d2` 第 4、5 例 |
| 文案 | 新增 418 个键（zh-CN 与 en 键集合相同）；`tOr(key, fallback)` 用于开放枚举 | `app/i18n/{zh-CN,en}.json`、`app/i18n/index.ts` | `tests/m15/i18n.test.ts`；`check-cn-keys` |
| 工作量证明统一 | 改用 M17 的 `net/sandbox/pow.ts`，删除 `ui/sandbox/pow.ts` 和 `pow.worker.ts`；`ui/sandbox/domain.ts` 精简 | `ui/sandbox/{session,domain}.ts` | `tests/m15/sandboxPow.test.ts`、`sandbox-d2` 第 3 例 |

## 2. 偏差

1. **沙盒会话 store 没有整体替换**。M17 的 `stores/sandboxSession.ts` 在 19:27 补上了 WP-12 当时缺的字段，但过渡 store `ui/sandbox/session.ts` 仍有 13 个使用方，其中 `app/demo/SandboxLaunchSheet.tsx` 和 D2.1 的停放、节能状态由 WP-12c 在并行阶段接入。现在换掉会和 WP-12c 正在改的文件冲突，`sandbox-d2.spec.ts` 的注入点也要一起改写。本轮只统一了工作量证明。替换清单见 d2-WP-12b-to-M17 第 3 条，建议在集成步骤中替换。
2. **引擎模块经过 `app/shell/engineD2.ts` 转出**：TS-BND-01 规定 `ui/**` 只能经 `@/engine` 门面取引擎模块，而门面没有导出 `engine/perception` 和 `engine/targets`（`LevelName` 重名）。`engineD2.ts` 是唯一的过渡入口，只转出面板用到的名字。门面补上导出后，改两行 import 即可（d2-WP-12b-to-M06 第 1 条）。
3. **识别物行的过渡数据源**：`swarm/target/state` 的 raw 行到不了 `RtClient.onData`（M11 的 `decode.ts` 判为 `K_NONE`），所以识别物列表退回到 roster、配置和 detail 合成的数据（2 Hz），FPV 捕获框退回到感知帧的 `meas_enu_m`。M11 修复后，两处会自动改用 `targetsModel` 的行（d2-WP-12b-to-M11 第 1 条）。
4. **任务绘制用 `d2Tools`，没有抽出 `PolyEditor`**：14 §20.9.2 要求复用 D1 的 `EditViewportLayer` 并抽出 `PolyEditor`。WP-12b 复用的是识别物区域绘制所用的 D2 工具状态机，交互规则相同。没有矩形拖拽和撤销重做，D1 航线编辑不受影响（d2-WP-12b-to-文档总编 第 4 条）。
5. **接力监控是单独的 Dock 标签**：14 §20.9.4 把它放在任务标签的子页里，M15 PRD 则写成底栏的 `relay` 面板。WP-12b 采用与 PRD 一致的单独标签，任务行和详情可以跳转到这里，`?panel=relay` 也打开它（d2-WP-12b-to-文档总编 第 2 条）。
6. **写权限包含沙盒表面**：FakeSource 夹具的 serverInfo 没有 role，所以沙盒表面（会话持有者）总是可写，共享展示按 `canEdit` 判断，数据过期时一律不可写。
7. **接力倒计时按墙钟递减**：`next.eta_s` 是相对量，倍速或暂停时，两次统计消息之间的显示会有偏差（d2-WP-12b-to-M20 第 1 条）。
8. **FPV 内拖动改云台没有做**：FPV 下的指针事件归 M06 的 `viewport/interaction.ts`。UI 另盖一层捕获层会和拾取、触控手势冲突，需要 M06 提供拖动回调（d2-WP-12b-to-M06 第 5 条）。目前云台只能用菜单操作（跟踪识别物、回中）。
9. **规划层开关隐藏**：`stores/layers` 没有 `tasking` 键，任务详情里的"规划层"开关只有在这个键存在时才显示（d2-WP-12b-to-M05 第 1 条）。
10. **新文件名与 PRD 示意名不同**：FPV 用 `SensorViewBar.tsx`，没有分成 `SensorViewSwitch.tsx` 和 `ZoomSlider.tsx`；接力在 `ui/panels/tasks/RelayPanel.tsx`，不在 `ui/panels/relay/`（d2-WP-12b-to-文档总编 第 2、3 条）。

## 3. 测试结果

### 3.1 静态检查

- `npm run typecheck`：0 个错误。
- `npx oxlint --type-aware src/ui src/app tests/m15 perf/m15`：0 个问题。
- `make lint`：全部通过（退出码 0）。收尾前的一次运行中，`lint-no-emoji` 在 `tests/e2e/test_sandbox_templates.py:78` 报过 3 处，那是其他工作包正在改的文件；最后一次运行时该文件已经修好。逐项通过：ruff、oxlint、no-emoji、no-hex、lint-lf、motion-lint、check-icons、no-raw-controls、check-brand、check-deps、check-units、check-version、check_py_imports、check_py_callbacks、check-perf-flags、check-thresholds、m06-lint、生成物 `--check`、check-cn-keys、registry。

### 3.2 Vitest

`npm test`（unit 与 browser 两个项目）：183 个文件（182 个通过，1 个跳过），1367 例通过，1 例跳过，0 例失败。加入 `__ux.rtCalls` 探针后又跑了一次全量，当时 pytest 正在同机运行，有 6 个文件的 7 例超时或因超时连带失败（`bootRehearsal`、`uboBinder`、`sensors.alloc`，以及 M06 的 3 个 browser 用例）。这 6 个文件单独重跑，26 例全部通过。WP-12b 新增 `tests/m15/tasking.test.ts`，共 26 例：任务绘制与前置校验、SB-53 请求体、所需 GSD、预览摘要、任务表筛选、进度、热行、行操作、站位行、倒计时、HUD 帧与框、Dock 标签切换、计算器与 M19 对拍、日历换算、机巢通道、克隆基线与锁定字段、识别物合成列表、太阳方向与机型外形、写权限、深链。因为删除了过渡 pow 和精简了 `domain.ts`，同步修改了 `d2Models.test.ts`、`d2Notices.test.ts` 和 `sandboxPow.test.ts`。

### 3.3 Playwright 功能用例（测试构建，`--project perf`，只有功能断言）

| 用例 | 结果 |
|---|---|
| `perf/m15/tasks-d2.spec.ts`（新增，6 例）：Dock 任务页与接力监控；绘制多边形、前置校验、预览、带 preview_id 启动；不可行预览与"添加无人机"；机巢、目录与计算器；真实 viewport 上的 FPV 视图条与 HUD（含 `cmd/zoom` 正对照）；viewer 零写请求 | 6 / 6 通过 |
| `perf/m15/shots-d2.spec.ts`（新增，1 例）：截图自检，无页面错误，无禁用字形 | 1 / 1 通过 |
| `perf/m15/sandbox-d2.spec.ts`（WP-12，5 例） | 5 / 5 通过 |
| M15 原有功能用例 `smoke`、`interaction`、`responsive`、`fxweb2`、`layout-probe`、`report` | 25 / 25 通过 |
| `perf/m06/gpu-limits.spec.ts`（WebGL2 规范最低限值回归，ADR-086/087） | 5 / 8 通过。3 例失败都在 D2.1 field views 的 mobile phone / tablet 档，报 `EXT_color_buffer_float extension not supported`。这是 WP-11b 的 field view 渲染目标在移动端限值下的问题，WP-12b 没有改渲染代码，已告知 M06（d2-WP-12b-to-M06 第 4 条）。与 WP-12b 相关的各例（统一块上限、低模实例、桌面 field view）都通过，`gl 0, three 0, errors 0` |

### 3.4 截图自检（14 §20，1920 × 1080 与 1280 × 720）

截图共 34 张，由 `shots-d2.spec.ts` 生成，逐张用 Read 查看过，存放在 `.cache/impl/shots/m15-d2s-*.png`（`tasks-d2` 另外产生 16 张 `m15-d2b-*.png`）。覆盖的页面：主视图、识别物列表、详情和高级属性、机巢详情、机型目录、克隆表单、绘制中的新建任务（真实点击）、预览、任务表、接力监控、FPV 及日视、夜视、热像、接管确认、计算器、换底图、模板。

发现并修复的问题：

1. D2 表面的左栏还是 D1 的"世界"组，没有底图和数据集信息。改为显示 `WorldPanel`。
2. D2 表面上出现了 D1 的"添加 P600"按钮。在 D2 表面隐藏。
3. 机巢详情只有表单，没有状态信息。增加槽位与 RTK、库存、能量、充电通道、空闲通道、电池、在巢与在空机体。
4. 克隆表单没有预填，锁定字段也可以编辑。改为先取详情再预填，锁定字段只读。
5. 接力 KPI 的说明文字写在大写标签里，又挤在一起，各列也没有对齐。说明改成数值下方的小字，用网格对齐。
6. HUD 读数卡挡住了视图条，手动控制时又和键位提示重叠。调整位置，手动控制时下移。
7. 捕获框的标签在视口边缘会被截断。改为限制在视口内。
8. 模板对话框只有模板名称。增加摘要和"首次反馈"一行。
9. 右栏页面被敏感体表格撑宽，出现横向溢出。给网格加 `min-w-0`。
10. 接管确认框没有说明这架机正在执行什么。增加"当前任务：…"。
11. 1280 宽度下，新建任务页的等级 ToggleGroup 超出面板。改为按列宽等分。

截图中还有两处问题来自夹具，不是界面的问题：实时夹具的会话到期时刻已过，所以"剩余 00:00"显示为红色；模板的 `first_feedback_zh` 都是同一句。已转 M00（d2-WP-12b-to-M00 第 1、2 条）。

### 3.5 pytest

`pytest -m "not perf" --continue-on-collection-errors`：4070 例通过，11 例失败，89 例跳过，1 例 xfail，1 个收集错误，用时 1 h 16 min（与 Playwright 和 vitest 同机运行）。WP-12b 没有改 Python 代码，失败都来自其他工作包正在改的部分：

- **收集错误** `tests/sim/test_lowpoly_fw.py`：`tools/vehicles/lowpoly.py` 用 `from _common import GLTF_FROM_FLU`，但全量运行时 `_common` 先被解析到了 `tools/bench/ipc/_common.py`，两个目录里的模块同名。不加 `--continue-on-collection-errors` 时，整个运行在收集阶段就中断。
- **机型与能量口径变化**（7 例）：`tests/sim/test_profiles.py` 的两例（机型集合多出 `awr_q1`、`awr_q9`、`awr_v1`、`awr_f1`）、`tests/safety/test_battery.py::test_p600_hover_endurance`（193.8 s 对 188.7 s）、`tests/agent/test_scoring.py::test_norms_by_profile`（19.38 对 18.87）、`tests/mission/test_energy.py::test_s1_precheck_soc` 与 `tests/mission/test_s1.py::test_s1_end_to_end`（SOC 0.5054 对 0.36），以及 `tests/safety/test_energy_rtl.py::test_energy_rtl_lands_home`（低电返航没有落地）。这些看起来都是机型目录与 P600 参数正在变动所致。
- **时序与端到端**（4 例）：`tests/runtime/test_supervisor.py::test_on_failure_policy_and_startup_timeout`（silent 没有进入 BACKOFF）、`tests/sensors/test_pose_chain_e2e.py`（等待超时）、`tests/sim/test_startup.py::test_started_events_and_meta`、`tests/e2e/test_sandbox_bench_tools.py::test_world_costs_smoke`。

## 4. 遗留

- **FPV 内拖动改云台**（FR-218）：等 M06 提供拖动回调（偏差 8）。
- **UX-FR-131 手工指定架次**（P1）：D1 航线编辑作为新建任务页的子模式，这一项没有做。
- **间隙条码**（UX-FR-130，P1）：覆盖率曲线已经做了，间隙条码没有做。
- **MID-360 扫描显示**（d2-WP-11-to-M15 第 9 条）：`lidarScan.ingest`、`setPersist` 和 `clear` 没有接，blob 的送达也还要等 M11。
- **AC-214 的其余部分**：满员排队期间的 viewer 交互，要等 WP-12c 接入 D2.1 的排队状态后再补用例。
- **AC-212 的时间部分**（视图切换 ≤ 300 ms、programs 增量 0）和 AC-219 的性能部分：属于性能用例，并行阶段不跑。
- **识别物图层与规划图层的开关**：等 M05 加键（偏差 9）。
- **会话 store 替换**：见偏差 1。
- **用户提出的 D2.1 需求**（版本号与进度条、实例容量与排队、空闲节能、手机与平板适配、隐藏的身份切换、感知场视图）由 WP-12c、WP-11b 等工作包负责，不在 WP-12b 范围内。WP-12b 做了其中的量化部分：计算器（GSD、目标上的像素数、TTPF 概率、各等级最大距离）、HUD 的像素进度和 GSD、声学背景声级（dBA）。

## 5. 接线点

| 过渡实现 | 正式接口（所有者） | 替换方法 |
|---|---|---|
| `app/shell/engineD2.ts` 转出 `engine/perception` 和 `engine/targets` | `@/engine` 门面（M06） | 文件里的两条 import 改成 `from '@/engine'`，面板代码不变 |
| `targetModel.itemsFromDetails` 合成的识别物列表；HUD 的 `meas_enu_m` | `targetsModel` 的行（M11 送达 TargetLite32） | 不用改代码：有行时自动改用 |
| `ui/sandbox/session.ts` | `stores/sandboxSession.ts`（M17） | 按 d2-WP-12b-to-M17 第 3 条的清单替换 |
| `taskModel.reliefLeftS` 按墙钟递减 | `next.eta_t_sim_ns`（M20） | 改用 timeline 的仿真时刻相减 |
| 任务详情的"规划层"开关（隐藏） | `layers.visible.tasking`（M05） | 不用改代码：有这个键时自动显示 |
| `perf/m15/helpers/d2rest.ts` 的平地 `ray_hit` | M04 的 `ray_hit` | 只在用例里使用，真实环境不受影响 |

## 6. 跨所有权请求

| 文件 | 内容 |
|---|---|
| `d2-WP-12b-to-M06.md` | 门面导出 perception 和 targets（解决 `LevelName` 重名）；传感器视图接口的用法（请核对）；拾取依赖；gpu-limits 移动端 3 例失败（告知）；FPV 拖动回调 |
| `d2-WP-12b-to-M11.md` | 再次请求送达 TargetLite32 的 raw 行；ServerInfo 的 role 和 seat |
| `d2-WP-12b-to-M17.md` | 确认新增字段已经收到；整体替换延后的原因；替换清单 |
| `d2-WP-12b-to-M05.md` | `visible.tasking` 和 `visible.targets`；世界组的变化（告知） |
| `d2-WP-12b-to-M21.md` | `catalog.load` 中传感器失败时连带丢掉型号；克隆详情；机型外形登记（告知） |
| `d2-WP-12b-to-M00.md` | 实时夹具缺 role 和 seat、会话到期时刻已过；模板的 `first_feedback_zh`；接力统计夹具 |
| `d2-WP-12b-to-M20.md` | `next.eta_t_sim_ns`；UI 用到的 store 接口（请核对）；给 WP-12c 的汇总选择器建议 |
| `d2-WP-12b-to-M16.md` | 登记 `tasks-d2`、`shots-d2` 两个用例和两个辅助文件；运行方式 |
| `d2-WP-12b-to-文档总编.md` | 路径登记；任务面板与接力页位置在两份文档中不一致；FPV 文件名；绘制没有用 PolyEditor；`engineD2` 过渡入口；会话 store |

## 7. 对他方请求的处理

| 请求 | 处理 |
|---|---|
| d2-WP-06-to-M15（M19） | 第 1 条：计算器和载荷的 GSD 提示都用 `sensorFromCatalog`、`sensorSummary`、`gsdAtRange`、`perceptionLevel`、`feasibilityDetail`、`maxRangeForLevel`。第 2 条：HUD 读 `usePerception` 的 `frame`。第 3 条：`perception.limiting.*` 和 `perception.limitedBy` 文案已加。第 4 条：Toast 去重沿用 `d2Notices` |
| d2-WP-09-to-M15（M07） | 第 1 至 4 条已实现：日历 Popover、换算成 UTC 后调用 `env/calendar`、快捷时段、声学背景、低能见度提示 |
| d2-WP-10-to-M15（M13） | 视图条和载荷控件读 `stores/sensors` 的 `payload`；变焦、模式、补光和云台发 `uav/{id}/cmd/{zoom,sensor/mode,gimbal}`；`sensor.mode` 事件文案已经在 WP-12 加过 |
| d2-WP-11-to-M15（M06） | 第 1 至 8 条和第 10 条已接（第 8 条由 `ui/fpv/bindings.ts` 以不超过 4 Hz 的频率推送）。第 9 条 MID-360 扫描显示没有做（§4）。第 11 条等 M05 加键。第 12 条：HUD 和捕获框是 DOM 层，红色只经过一处红仲裁 |
| d2-WP-11b-to-M15（M06，D2.1） | 发给 WP-12c，不在 WP-12b 范围内 |
