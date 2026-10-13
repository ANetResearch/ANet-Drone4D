# D2 WP-11b 场视图渲染与移动画质：实现报告

| 项 | 内容 |
|---|---|
| 工作包 | WP-11b 场视图渲染与移动画质（所有者 M06；AWR-04 §15.15.1 第 4 行；依赖 WP-19b 剖面格式、WP-12c 界面；第 4–7 周） |
| 日期 | 2026-10-05（第 1 次执行）；2026-10-11（第 2 次执行：复核、请求答复与增补，见 §8） |
| 依据 | 用户 D2.1 原文 `docs/inputs/D2.1-需求原文-2026-10-05.md`（"节能省电模式""适配移动端手机端、以及平板操控""球形散点""声音视图……递减的辐射场"）；AWR-04 v1.3 §15.7.7、§15.8.1、§15.8.4、§15.8.6、§15.11（ADR-121、ADR-122、ADR-125、ADR-126；D2-AC-44、47、48、49、53、54）；14 §20.26.5、§20.26.7；AWR-03 ADR-086、ADR-087；请求 `d2-WP-19b-to-M06`（5 条）、`d2-WP-12-to-M06` 第 1 条；第 2 次执行另读 `d2-WP-12c-to-M06`、`d2-WP-12b-to-M06`、`d2-ACC-1-to-M06`、`d2-FX-R2-web-to-M06`、`d2-FX-R2-other-to-M06`、`d2-WP-10-to-M06`、`d2-WP-05-to-M06`、D2-INT 集成报告与 D2 验收第 1 轮报告 |
| 路径（只写本包） | `apps/web/src/viewport/**`、`apps/web/src/engine/{field,camera,drones}/**`、`apps/web/src/engine/loop.ts`、`apps/web/perf/m06/**`；M06 测试目录 `apps/web/tests/m06/**` |
| 约束执行 | 未写他人路径（跨所有权需求见 §7 的 6 份请求）；未执行 git 写操作；未装依赖；未跑性能基准（300 ms 切换只在 `M06_PERF=1` 断言，本包未设）；无 emoji 与禁用字形；颜色只取场景 token（`SCENE.windRamp` 作为 `--field-ramp-1…5` 的同值来源，无 hex、无色相）；未新增 UI 组件、动效与图标（界面属 WP-12c）；新渲染代码在 WebGL2 规范最低上限（24/12/12/24、16 384 B、点径上限 1、`MAX_TEXTURE_SIZE` 4096、无浮点颜色缓冲扩展）模拟下 0 错误。第 2 次执行同样只写本包路径（跨所有权需求追加到 `d2-WP-11b-to-{M15,M16,文档总编}.md`，§8.6）；颜色改为直接取 `SCENE.fieldRamp`；未新增 UI 组件、动效与图标（ViewCube 只改外边距工具类）；口令明文未写入任何文件（扫描测试用临时随机口令文件） |
| 结论 | P0 全部实现并有测试：确定性球形散点（与服务端 `pattern()` 逐点一致）、辐射场与感知体实例数据（剖面插值、等级带、壳内高亮、配置体掩码、表模型 P）、两个程序（场点 glpoint/quad、等值壳）进 shader zoo、切换不编译、PerfGovernor 四级降级、按设备档的点数与壳圆预算；触控手势（单指旋转、捏合、双指平移、双指旋转、长按、单击、双击）；`deviceClass = mobile` 与 phone/tablet 画质档（帧上限、像素上限、DPR、阶梯起点、EDL、标签与模型上限、订阅率）；节能挂起不跑 rAF、`loop.requestFrame()` 单帧、静态浏览交互唤醒；gpu-limits 移动上限组与场视图用例。P1：雷达识别物的叠加壳（通用实现）、平板降档 30 fps、warm-up 进度上报。Vitest unit 与 browser、M06 Playwright 功能用例、gpu-limits（D2.1 全矩阵 12 例与原回归 3 例）通过；最后一次 `make lint` 通过（退出码 0；此前他包文件的 no-emoji 3 处已由该包修复，§5.4）；pytest 全量的 11 个失败均在他包进行中的 Python 用例（§5.3）。**第 2 次执行（§8）**：复核 P0 在当前工作树全部成立（`m06.field-views`、gpu-limits 10 例、M06 回归 14 例、M15 D2.1 移动 11 例、Vitest 全量、`make lint` 通过）；答复 M06 请求：场视图灰阶直接读 `SCENE.fieldRamp`、ViewCube 在 coarse 指针下自留 44 px 工具列、FPV 内拖动改云台的视口接口 `fpv.onDrag`（M15-FR-218）、prod-bundle-scan 的 REL-04 与身份分块检查（M15-FR-236）；修正 FPV 下触控交给页面滚动与缩放的缺陷（宿主常设 `touch-action: none`）；P1：视口边缘 16 px 不起手势；场视图不可见时不写锚点表 |

## 0. 摘要

1. **场视图**（`engine/field/**`、`viewport/layers/field.tsx`）：服务端（M19）下发剖面与感知体表，前端只做图案、插值与绘制。每架我方飞行器一个锚点：
   Halton 方向、对数均匀半径（`r_min = 2 m`，`r_max` 取剖面降到绘制下限处，与 SB-78 的 `r_max_of` 同口径），值为 `profileValue(r, e = -asin u_z)`，
   等级带 1–5 决定灰阶（g500…g50）、透明度（0.15–0.85）与点径（2–6 px，Tier S 与手机 2–4 px），点形表达模态（声学圆点、视觉菱形、雷达圆环、热十字）；
   位于实线壳（选中识别物对该机型的有效半径，否则帧的缺省"人"壳）内的点按最高档绘制并加大 1 px。每个（识别物，敏感体）一个锚点：`r_min = 1 m`、
   `r_max` = 配置半径，只保留配置体内的点（`bodyMask`，与禁入预测同一几何），透明度 `0.05 + 0.80·P`（几何模式 P = 1，物理模式或"物理参考"为表模型 P）。
   等值壳：每架机一个实线壳与帧的虚线 iso 壳（声学为背景噪声壳），各以 1–3 个大圆（赤道与两条子午线，半径随仰角）绘制。
2. **绘制**：一次场点 draw（'glpoint' 为非实例化 GL 点，'quad' 为实例化四边形）、一次等值壳 draw（1 px 线段）；锚点位置为 64 × 1 RGBA32F 纹理（每帧写入），
   每点数据 offset vec3 + info vec4（≤ 1 Hz 上传）；两个程序在遮罩下预热，切换视图、感知体、物理参考与高亮只改数据（programs 增量 0）。
3. **移动画质**（`viewport/quality.ts`）：设备档按 §15.8.1（读 `<html data-ff>`，缺省自算），`deviceClass = mobile` = 移动 GPU 族（Adreno、Mali、PowerVR、
   Apple GPU）且 `pointer: coarse`；画质档在后台创建时固定为 `be.quality`，M06 应用 DPR 与像素上限、帧上限（phone 30 fps；tablet 降档后 30 fps）、
   阶梯起点、EDL、标签、近景与低模上限、`swarm/state` 与选中机订阅率、场点预算；点预算上限与环境档由 M05、M07 读取（请求）。
4. **触控**（`viewport/input/touch.ts`）：纯识别器 + 视口绑定；相机侧为 `CameraRig.touch*`（camera-controls 自带触控关闭），阻尼与鼠标拖动一致；
   长按发 `viewport.longpress {x, y, clientX, clientY, pick}` 供 M15 的锚定菜单。
5. **节能**（`engine/loop.ts`）：挂起即取消 rAF 链（不再每帧回调）；`requestFrame()` 渲染一帧（冻结帧）；`wake(ms)` 与 `viewport.setWakeOnInput` 支持
   观看排队的静态浏览（交互后 2 s 回到挂起）；恢复时 dt 0 并冻结 CAS 与 PerfGovernor 30 帧；挂起时画布尺寸变化自动补一帧。

## 1. 实现清单（需求与验收编号）

| 需求 | 实现 | 验收与测试 |
|---|---|---|
| R-D2-41 球形散点（ADR-125；AWR-04 §15.11.2） | `engine/field/pattern.ts`：Halton H2/H3/H5、黄金角错开、对数均匀半径，与 `python/awr/sim/perception/field.py pattern()` 同式 | `tests/m06/fieldPattern.test.ts`：8 组剖面 × 256 点的（r、观察者仰角）与服务端样本逐点相等（相对 1e-9），感知体世界点与服务端样本相等（1e-6） |
| R-D2-42 按指标切换的场视图（§15.11.3） | `engine/field/build.ts` 辐射场实例数据：剖面插值、等级带、灰阶 / 透明度 / 点径随档递增、模态点形、实线壳内高亮；`FieldRenderer` 视图切换即时重建 | `fieldBuild.test.ts`：每种指标插值与服务端精确样本 ≤ 0.5 dB 或 2 %、绘制点数与服务端样本吻合（±5 %）、声学随距离不增、编码与壳内规则；`perf/m06/field-views.spec.ts`（D2-AC-53） |
| 感知体（§15.11.1、§15.11.2；M18-FR-032 掩码） | 感知体锚点、`bodyMask` 筛选、表模型 P 与几何模式 P = 1、物理参考、选中 / 全部（最近的 ≤ 预算个） | `fieldBuild.test.ts`：保留点集合、位置与 P 与服务端显式样本逐点一致（物理模式），几何模式 0.85 透明度，透明度随 P 单调，选中模式与 level 2 只留选中识别物 |
| 等值壳（§15.11.3） | 实线壳（选中识别物对该机型的有效半径 `volumeREff`，否则 `shell_m`）、虚线 iso 壳（`isoRadius`），大圆数按预算递减（3 → 2 → 1 → 去虚线 → 远处机体） | `fieldBuild.test.ts`：圆数 ≤ 预算（96/32）、高亮、level 3 无虚线、缺省壳半径 = `shell_m`、选中识别物改变壳半径 |
| §15.11.5 采样数与刷新预算 | `engine/field/budget.ts`：四列预算、按比例缩减；数据驱动重建 ≤ 1 Hz（手机由 store 隔帧，0.5 Hz）；节能挂起时全部停止 | `fieldBuild.test.ts`（各列 24 架时总点数、每机都有份额）、`quality.test.ts`；`field-views.spec.ts`（Tier S ≤ 3072 点、≤ 32 圆） |
| §15.11.6 GPU 上限下的绘制（ADR-086、087） | `engine/field/materials.ts`：场点与等值壳两个程序；顶点 2 块、片元 0 块；属性 ≤ 3；varying ≤ 2.5 vec4；点形距离场（无纹理）；预乘透明度、深度测试开写关；隐藏点折叠到裁剪体外 | `tests/m06/field.browser.test.ts`（最低上限模拟，glpoint 与 quad 两种：GL 错误 0、绑定越界 0、单程序 V ≤ 3、F ≤ 2、属性 ≤ 8、varying ≤ 16 float、四种指标与感知体都点亮且像素不偏红、切换不增程序、锚点缺失时其点折叠） |
| 两个程序进 shader zoo（D1-AC-25） | `FieldRenderer.warmItems()`（放置一个锚点，画 1 点与 1 段）；LayerSpec `field`（`sensorClass: hidden`） | `field-views.spec.ts`：四种切换 programs 增量 0、`compiledAfterReveal` 不变；`warmup.spec.ts` 回归通过 |
| PerfGovernor 降级（§15.11.5） | knob `field`（step 1，5 级：全量 → 点数减半 → 只选中识别物的感知体 → 去虚线壳 → 关闭并发 `field.governorOff`）；测试可固定级别 | `fieldBuild.test.ts`、`field-views.spec.ts`（level 1 点数约减半、level 3 无虚线、level 4 draw 0） |
| 与 WP-12c 对接（UX-FR-159、160） | 门面 `field.{stats, highlight, onGovernorOff}`；场视图由 M19 store 的 `perceptionActions` 驱动；图层挂载时 `attachPerceptionFeed()` 与 `setDeviceClass(ff)` | 请求 d2-WP-11b-to-M15 |
| R-D2-35 设备档与 `deviceClass = mobile`（ADR-122；§15.8.1、§15.8.6） | `viewport/quality.ts`（`formFactorOf`、`currentFormFactor`、`onFormFactorChange`）；`backend/deviceClass.ts`（`isMobileGpu`、`isMobileDevice`）；`renderer.ts` 在创建时求画质档 | `quality.test.ts`：D2-AC-47 设备矩阵 10 个尺寸、移动 GPU 串 6 正 4 负、coarse 条件 |
| 画质档（§15.8.6） | phone / tablet / desktop 三行；DPR ≤ 2 与像素上限（`qualityDpr`，WorldCanvas）；帧上限（`loop.setQualityFps`，不冻结治理；tablet 降档后 30 fps）、目标帧时间；阶梯起点（phone ≤ 2、tablet ≤ 3）；EDL（phone 关、tablet 未降档时开）；标签 8/16、近景 1/2、低模 32/64；`swarm/state` 5/10 Hz、选中机 30 Hz；场点预算 2048/4096 | `quality.test.ts`；`field-views.spec.ts`（phone 390 × 844 DPR 3、tablet 1180 × 820 DPR 2，Adreno 串：设备档、mobile、DPR ≤ 2、绘制像素 ≤ 1.0/2.0 MP、帧上限、阶梯起点、低模 / 近景 / 标签上限、场点预算、无横向溢出） |
| `pointSizeMode = quad`（§15.8.6、§15.11.6 第 4 条） | 后台读 `ALIASED_POINT_SIZE_RANGE`，上限 < 8 报 'quad'；`caps.maxPointSize`；场点在 quad 下走实例化四边形 | gpu-limits mobile 组：`pointSizeMode` = quad、场渲染器 mode = quad |
| R-D2-36 触控手势（§15.8.4） | `viewport/input/touch.ts`（`TouchGestures`、`installTouch`）、`CameraRig.touchActive/touchRotate/touchPan/touchDolly/touchTwist`、`interaction.ts`（鼠标路径忽略触控指针与触控后的合成 dblclick；单击、双击、长按） | `touch.test.ts`（阈值 6 px、4 %、8 px、5°、500 ms / 10 px、300 ms / 24 px、三指不用、双指转单指不旋转；相机：旋转角与鼠标一致、捏合中点不动、平移、Orbit 与 Bird 的偏航、FPV 忽略）；`field-views.spec.ts`（CDP 触控：五种手势、长按事件、单击选中、双击飞行、页面不滚动不缩放） |
| R-D2-33 节能：前端静态渲染（ADR-121；§15.7.7） | `loop.setSuspended` 取消 rAF、`requestFrame()`、`wake(ms)`、`suspendedFrames`；LoopDriver 尺寸变化补帧；`interaction.ts` 唤醒；门面 `viewport.{requestFrame, setWakeOnInput, suspended}` | `loopPower.test.ts`（挂起无回调、单帧且冻结、合并请求、唤醒时长、恢复 dt 0 与冻结、质量帧率不冻结而帧上限冻结）；`field-views.spec.ts`（挂起 2 s 帧数不变、单帧 +1、拖动唤醒并在 2.5 s 内停止、恢复） |
| D2-AC-49、54 上限回归 | `perf/m06/gpu-limits.spec.ts`：mobile 组（最低块数 + 点径 1 + 纹理 4096 + 无 `EXT_color_buffer_float`、`EXT_float_blend`、`OES_texture_float_linear`）与"D2.1 field views"用例；后台在无浮点颜色缓冲时静默 three 的浮点纹理扩展查询 | §5.1 |
| 请求 d2-WP-12-to-M06 第 1 条（warming 段） | `backend/warmup.ts` 的 `progress` 回调与 `hostRuntime` 的 `reportWarmPart('shaders', x)`（0.25 → 0.75 按目标 → 1） | warmup 与 smoke 回归通过 |

D2-AC 对应：D2-AC-53（场视图与感知体：前端部分，图例与读数卡属 M15）、D2-AC-54（M06 部分）、D2-AC-49（M06 部分；点预算上限、环境档属 M05、M07）、
D2-AC-48（视口手势部分；快捷指令与摇杆属 M15）、D2-AC-44（前端静态渲染部分；PowerController 与 presence 属 M15、M11）、D2-AC-47（设备档判定口径）。

## 2. 设计要点（供 M06 PRD D2.1 增补节并入）

### 2.1 场视图的数据与重建

| 项 | 规则 |
|---|---|
| 数据入口 | `engine/perception fieldModel`（M19 store 写入）：`profiles`、`volumes`、`kind`、`volumesMode`、`physicalRef`、`selectedTarget`、`version` |
| 锚点 | 感知体锚点优先（≤ 预算个，按到相机距离），飞行器锚点用剩余（总 64）；超出时取最近者 |
| 重建 | 结构键（视图、感知体模式、物理参考、选中识别物、治理级别、预算、高亮、体机型、剖面存在、roster 版本）变化即重建；数据版本变化 ≥ 0.9 s 一次；显示感知体或有未放置锚点时每 1 s 一次（识别物转向重做扇形掩码） |
| 每帧 | 锚点纹素（飞行器渲染位姿、识别物行位置；缺失时 w = 0，其点折叠）、DPR 与绘制缓冲尺寸 uniform |
| 体机型 | 主选机的 roster 机型；不在表中时取剖面第一架的机型，再取 `models[0]` |
| 节能 | 循环挂起时不更新，保留最后一帧 |

### 2.2 编码

| 字段 | 场点 | 等值壳顶点 |
|---|---|---|
| 位置 | offset vec3 = u·r（相对锚点，ENU m） | offset vec3（相对锚点） |
| info.x | 锚点槽位 | 锚点槽位 |
| info.y | 灰阶序号 + 5 × 点形 | 灰阶序号（实线 g200、高亮 g50、虚线 g300） |
| info.z | 透明度 | 虚线坐标（每单位一段，半段亮）或 -1（实线） |
| info.w | 点径（CSS px，乘 DPR） | 透明度 |

### 2.3 画质档

| 项 | phone | tablet | desktop |
|---|---|---|---|
| 帧上限 | 30 fps（`setQualityFps`，治理照常评估） | 60；PerfGovernor 降档后 30 | D1 |
| 绘制像素 | ≤ 1.0 MP，DPR ≤ 2 | ≤ 2.0 MP，DPR ≤ 2 | D1 |
| 阶梯起点 / 点预算上限 | ≤ 2 / 0.5 M（预算上限待 M05） | ≤ 3 / 1.0 M | D1 |
| EDL | 关 | PerfGovernor 未降档时开 | 开 |
| 环境档 | Low（待 M07） | Low（待 M07） | D1 |
| 标签 / 近景 / 低模 | 8 / 1 / 32 | 16 / 2 / 64 | D1 |
| 订阅 | swarm 5 Hz、选中机 30 Hz | 10 Hz、30 Hz | D1 |
| 场点 / 每机 / 每体 / 感知体 / 壳圆 | 2048 / 160 / 128 / 6 / 32 | 4096 / 320 / 256 / 12 / 64 | B/A 8192 / 512 / 384 / 16 / 96；S 3072 / 192 / 128 / 6 / 32 |

移动 GPU 在桌面尺寸屏（12.9 英寸平板横屏）取 tablet 行。

## 3. 文件清单

| 路径 | 新增或改动 |
|---|---|
| `apps/web/src/engine/field/{pattern,budget,build,materials,FieldRenderer,index}.ts` | 新 |
| `apps/web/src/viewport/layers/field.tsx` | 新：FieldLayer（≤ 150 行） |
| `apps/web/src/viewport/input/touch.ts` | 新 |
| `apps/web/src/viewport/quality.ts` | 新 |
| `apps/web/src/viewport/dev/fieldDemo.ts` | 新：测试构建的合成剖面、感知体与识别物行（懒加载，生产构建不含） |
| `apps/web/src/engine/loop.ts` | 改：挂起取消 rAF、`requestFrame`、`wake`、`suspendedFrames`、`setQualityFps`；`QualityView`、`FormFactor`、`caps.maxPointSize` |
| `apps/web/src/engine/camera/CameraRig.ts` | 改：触控方法、camera-controls 触控关闭、Bird 偏航后 `northUp` 复位 |
| `apps/web/src/engine/drones/DroneLayer.ts` | 改：`setHeroCap` |
| `apps/web/src/viewport/backend/{deviceClass,webgl2,warmup}.ts` | 改：移动 GPU 判定；画质档、`pointSizeMode` quad、浮点扩展查询静默；预热进度 |
| `apps/web/src/viewport/{renderer,hostRuntime,LoopDriver,WorldCanvas,interaction,facade,testHooks}.ts(x)` | 改：画质档、帧上限与目标帧时间、EDL、尺寸变化补帧、DPR、FieldLayer 挂载、触控与唤醒、门面与测试钩子 |
| `apps/web/src/viewport/layers/{registry.ts,drones.tsx}`、`bindings/selection.ts`、`overlay/LabelHost.tsx`、`sensorView/pass.ts` | 改：`field` 图层 id、订阅率与模型上限、选中机 30 Hz、标签上限、传感器视图中隐藏 |
| `apps/web/tests/m06/{fieldPattern,fieldBuild,quality,touch,loopPower}.test.ts`、`field.browser.test.ts` | 新 |
| `apps/web/perf/m06/field-views.spec.ts` | 新 |
| `apps/web/perf/m06/{gpu-limits.spec.ts,cases.mjs}` | 改：mobile 组与场视图用例；登记 `m06.field-views`、gpu-limits 的 D2-AC |

## 4. 偏差

| # | 主题 | 基线 | 实现 | 理由 |
|---|---|---|---|---|
| 1 | `deviceClass = mobile` | §15.8.6"新增 deviceClass = mobile" | 与硬件类正交的标志 `be.quality.mobile`，`DeviceClass` 联合类型不变 | `DeviceClass` 进入线上 `hello`、`clientStats` 与 M11 校验；移动 GPU 的硬件类仍按 D1 规则（iGPU 族）决定起点与 DPR |
| 2 | 锚点数组 | §15.11.6 第 3 条 `vec4[64]` uniform | 64 × 1 RGBA32F 纹理（顶点 texelFetch） | uniform 数组在经典路径为独立 NodeBuffer 块；纹理不增加块（WP-11 发射源表同一做法），块数仍为顶点 2、片元 0 |
| 3 | 场点 draw | §15.11.6 第 1 条 `drawArraysInstanced` | glpoint 下非实例化 GL 点（一次 drawArrays），quad 下实例化四边形 | SwiftShader 逐实例执行实例化 draw（约 25 µs/实例，D1 FX2-R3）；每点数据仍为逐点属性，一次 draw |
| 4 | 等值壳 | "一次实例化线段 draw" | 一次非实例化 1 px LineSegments | 同上；1 px 原生线在各上限下可靠 |
| 5 | 每点数据 | `iA = (方向, h1)`、`iB = (锚点, 值, 点形, 标志)` | offset vec3（u·r）+ info vec4（锚点、灰阶 + 5·点形、透明度、点径） | 灰阶、透明度、点径在 CPU 端按档位或 P 求好，片元无分支表；内容等价，28 B/点 |
| 6 | PerfLayerId | — | 场视图计入 'trails'（与 targets 相同） | `engine/perf/probe.ts` 的预算表以该类型为键，`engine/perf` 不在本包路径（请求文档总编） |
| 7 | 画质档释义 | "阶梯 ≤ 2 档时开"（tablet EDL）、"第 4 档起 / 第 2 档起" | tablet EDL 在 PerfGovernor 未降档时开；阶梯起点 phone ≤ 2（minimum）、tablet ≤ 3（low） | 原文与 M05 LADDER 索引对不上（phone 起点高于 tablet），按点预算上限取不高于预算的档 |
| 8 | three 浮点扩展告警 | D2-AC-54 three 告警 0 | 后台在无 `EXT_color_buffer_float` 时静默 three 的查询 | three r186 上传任何浮点纹理（含只采样的 DTM、绘制表、发射源表、锚点）都查询该扩展并告警；视口不渲染浮点颜色目标，行为不变 |
| 9 | 热视图图案 r_max | SB-78 用 `r_max_of`（NETD 截止） | 最远可绘网格半径 | 剖面不带热视图的截止半径；其余三种与服务端逐点一致（请求 M19 可选字段） |
| 10 | `field.tsx` 的 PerfGovernor 关闭提示 | "关闭场视图并提示" | 发 `field.governorOff`，提示文案由 M15 显示 | 视口不渲染 UI |

## 5. 测试结果

本机运行（多工作包并行，load average 13–18）；性能基准未运行。私有测试构建：`VITE_AWR_TEST_SWITCHES=1 npx vite build --outDir <scratchpad>/dist-test`。

| 范围 | 命令 | 结果 |
|---|---|---|
| TS 类型 | `npx tsc -p apps/web/tsconfig.json --noEmit` | 本包 0 错误（唯一报错为他包并行中的 `perf/m15/zz-*.spec.ts` 未用导入） |
| Vitest unit 本包 | `npx vitest run --project unit tests/m06/{fieldPattern,fieldBuild,quality,touch,loopPower}.test.ts` | 5 文件 31 例通过 |
| Vitest unit 全量 | `npx vitest run --project unit` | 161 文件 1305 例通过、1 跳过 |
| Vitest browser | `npx vitest run --project browser` | 21 文件 61 例中 60 通过；`vertexOutput.browser.test.ts` 在负载约 18 时 15 s 超时，单独复跑（与 `ubo`）10 例全部通过（WP-11 报告同一现象）；新增 `field.browser.test.ts` 2 例通过 |
| Playwright 本包 | `M06_DIST=… npx playwright test -c perf/m06/playwright.m06.config.ts perf/m06/field-views.spec.ts` | 5 例全部通过（D2-AC-53 Tier S、phone 与 tablet 画质档、触控手势、节能）；触控用例的双击一步在负载约 18 时曾因两次 CDP 触控间隔超过 300 ms 未判为双击，已改为连发两次、最多 3 次，重复 2 遍通过 |
| Playwright M06 回归 | smoke、warmup、wiring、sensor-views、layout、drone-tiers | 14 例全部通过 |
| gpu-limits | 见 §5.1 | 通过 |
| pytest | `pytest -m "not perf" tests/contracts tests/perception` | 547 通过（本包无 Python 改动）；全量见 §5.3 |
| lint | `make lint` | 最后一次通过（退出码 0；含 oxlint、m06-lint、no-hex、no-emoji、check-deps、M16 registry）；中途一次只剩他包文件 no-emoji 3 处，见 §5.4 |

### 5.1 gpu-limits（ADR-086、087；D2-AC-49、54）

| 用例 | 结果 |
|---|---|
| D2.1 field views：test 构建 × Tier S、B × min、mobile × desktop、phone、tablet（`GPU_LIMITS_FIELD=all`，12 例；缺省 4 例） | 见下行与 §5.2 |
| 缺省 4 例（S min desktop、S mobile phone、S mobile tablet、B mobile phone，B 为 Adreno 渲染器串） | 全部通过：WebGL 错误 0、three 告警 0、使用中的绑定点 3、单程序块数 V 3 F 2、programs 揭开后不变（S 38 → 38、B 41 → 41）、mobile 组 `pointSizeMode = quad`、`MAX_TEXTURE_SIZE` 4096、`floatColorRT` false、四种指标与感知体均有点且 draw ≥ 1 |
| 原回归：test 构建 Tier S min（12 种天气、传感器视图、机型外形、全图层开关、拾取、着色模式） | 通过：points 3、V 3 F 2、distinct 3、violations 0、programs 38 → 38、gl 0、three 0 |
| 原回归：低模实例矩阵回读 Tier S、B | 通过 |

### 5.2 全矩阵

`GPU_LIMITS_FIELD=all GPU_LIMITS_BUILDS=test`，12 例全部通过（11.4 min）：

| 档 | 上限 | desktop | phone | tablet |
|---|---|---|---|---|
| S | min | glpoint，1328 点，programs 38 → 38 | glpoint，1136 点，38 → 38 | glpoint，2267 点，38 → 38 |
| S | mobile | quad，1328 点，38 → 38 | quad，1136 点，38 → 38 | quad，2267 点，38 → 38 |
| B | min | glpoint，3592 点，42 → 42 | glpoint，1136 点，41 → 41 | glpoint，2267 点，42 → 42 |
| B | mobile | quad，3592 点，42 → 42 | quad，1136 点，41 → 41 | quad，2267 点，42 → 42 |

全部 12 例：WebGL 错误 0、three 告警 0、使用中的绑定点 3、单程序块数 V 3 F 2、违例 0、无被剔除图层；点数为四种指标加"全部"感知体的场点
（6 架、2 个识别物，各档按预算缩减；phone 的 Tier B 为 Adreno 串，其余 Tier B desktop 为 `?tier=B`）。

### 5.3 pytest 全量

`pytest -m "not perf" --continue-on-collection-errors`（1 h 18 min，负载 13–18）：4071 通过、89 跳过、11 失败、1 个收集错误。全部与本包无关：
本包没有 Python 改动，失败用例都不读取 `apps/web`（逐个 grep 确认），且运行期间他包仍在改动 `python/awr/**`。

| 用例 | 现象 |
|---|---|
| `tests/sim/test_lowpoly_fw.py`（收集错误） | 全量收集时 `_common` 模块名冲突（`tools/bench/ipc/_common.py` 先被导入）；单独运行 3 例通过 |
| `tests/agent/test_scoring.py::test_norms_by_profile`、`tests/e2e/test_sandbox_sdk_e2e.py::test_scenario2_example_script` | 断言数值与示例脚本，他包进行中 |
| `tests/mission/{test_energy,test_s1}.py`、`tests/safety/{test_battery,test_energy_rtl}.py` | 能量与续航数值（0.5054 对 0.36 等），他包进行中 |
| `tests/sensors/test_pose_chain_e2e.py`、`tests/sim/{test_profiles,test_startup}.py`、`tests/targets/test_wire.py` | sim-core 启动、剖面与识别物线格式，他包进行中 |

与前端相关的子集 `pytest -m "not perf" tests/contracts tests/perception`：547 通过。

### 5.4 `make lint` 中途的他包失败（最后一次已通过）

| 规则 | 文件 | 归属 |
|---|---|---|
| no-emoji（GLYPH-01 两处、EMOJI-01 一处） | `tests/e2e/test_sandbox_templates.py:78` | M16（WP-19b 报告已记录，非本包） |

## 6. 遗留

| 编号 | 事项 | 负责 | 说明 |
|---|---|---|---|
| WP11b-L1 | 工具条、Shift+V、图例卡、读数卡与行联动、长按菜单、PowerController 接线、Sandbox 小窗口挂起口径 | M15（WP-12c） | 接口已交付（d2-WP-11b-to-M15） |
| WP11b-L2 | 点预算上限 0.5 M / 1.0 M；'quad' 下点云写 `gl_PointSize`；隐藏 30 min 释放一半点云缓冲 | M05 | d2-WP-11b-to-M05 |
| WP11b-L3 | 环境视觉档 Low；'quad' 下降水点写 `gl_PointSize` | M07 | d2-WP-11b-to-M07 |
| WP11b-L4 | 切换 ≤ 300 ms、场视图帧成本、手机与平板真机帧率 | M16 / M06 | 并行阶段不跑基准；本机功能运行中 rebuild 在下一帧完成（记录于用例注解） |
| WP11b-L5 | 演示构建走 UI 的场视图 gpu-limits 分支 | M06 / M16 | 需 WP-12c 的工具条 |
| WP11b-L6 | 热视图图案与 SB-78 逐点对齐 | M19 | 剖面可选 `r_max_m`（偏差第 9 条） |
| WP11b-L7 | `PerfLayerId` 'field'、AWR-03 §3.8 预算登记、M06 PRD D2.1 节 | 文档总编 | d2-WP-11b-to-文档总编 |

## 7. 请求（`.cache/impl/requests/d2-WP-11b-to-*.md`）

- **M15**：场视图控件与图例（`--field-ramp-1…5` token、点形图例）、读数行高亮 `field.highlight`、治理关闭提示与 i18n `perf.governor.field`、长按锚定菜单、
  PowerController 的 `setSuspended / requestFrame / setWakeOnInput` 用法、`lib/formFactor` 同一规则、Sandbox 小窗口挂起改为宽 < 360。
- **M05**：`be.quality.pointBudgetMax` 作为 CAS 预算上限；'quad' 下写 `gl_PointSize`；`releaseLowPriority` 与挂起期间不发新请求。
- **M07**：`be.quality.envLevel` 上限 Low；'quad' 下降水点写 `gl_PointSize`。
- **M19**：答复 d2-WP-19b-to-M06（已按 5 条接入）；r_max 口径与可选 `r_max_m`。
- **M16**：新用例 `m06.field-views`、gpu-limits 的 mobile 组与场视图用例（`GPU_LIMITS_FIELD`）、D2-AC-46 的挂起测量提示。
- **文档总编**：偏差第 1–8 条、`engine/perf` 与 `engine/index.ts` 归属、AWR-03 §3.8 登记、M06 PRD D2.1 节（本报告 §2）。

## 8. 第 2 次执行（2026-10-11）：复核、请求答复与增补

本次按编排要求重新执行 WP-11b（D2.1 追加需求的场视图与移动端部分）。先复核第 1 次执行的交付在当前工作树（D2-INT、D2 验收第 1 轮与 D2-FX-R2 各包合入之后）
是否仍然成立，再处理之后发给 M06 且落在本包路径内的请求，最后做 P1 增补。路径约束同第 1 次。

### 8.1 复核结论（P0）

| 项 | 当前状态 | 依据 |
|---|---|---|
| 场视图与感知体（R-D2-41、R-D2-42；D2-AC-53 前端部分） | 成立：四种指标切换与感知体、等值壳、治理级别、无色相，切换 programs 增量 0 | `field-views` 第 1 例、`fieldBuild`、`fieldPattern`、`field.browser` |
| 移动画质档与 `deviceClass = mobile`（R-D2-35；D2-AC-49） | 成立：phone、tablet 画质档、DPR 与像素上限、帧上限、阶梯起点、各上限、场点预算 | `field-views` 第 2、3 例、`quality.test.ts` |
| 触控手势（R-D2-36；D2-AC-48 视口部分） | 成立；另修正 FPV 下触控交给页面滚动与缩放的缺陷（8.2 第 4 条） | `field-views` 第 4、5 例、`touch.test.ts` |
| 节能前端静态渲染（R-D2-33；D2-AC-44 视口部分） | 成立：挂起无 rAF、单帧、静态浏览唤醒、恢复 | `field-views` 第 6 例、`loopPower.test.ts` |
| WebGL2 规范最低上限（ADR-086、087；D2-AC-49、54） | 成立：见 8.4（揭开前程序数在 D2-FX-R2-web 的去重后为 27 / 29） | `gpu-limits` 10 例 |
| D2-FX-R2-web 对场视图治理器的改动（D2-AC-30） | 已核对：knob 级数 2（最多点数减半），治理器不再隐藏访客选的场视图；渲染器的 2–4 级保留给固定级别（测试） | 本次只同步了 `field.tsx`、`budget.ts` 的注释 |
| d2-WP-12-to-M06 第 1 条（warming 段着色器进度） | 已在第 1 次执行实现（`hostRuntime` 以 `reportWarmPart('shaders', x)` 上报）；D2-INT 报告 M06 行列为"延后"属误记 | `viewport/hostRuntime.ts` 第 143 行 |
| d2-WP-10-to-M06 第 4 条（测试构建 Tier S 最低上限 GL_INVALID_OPERATION） | 不再复现：该用例通过（WebGL 错误 0） | `gpu-limits` 第 1 例 |

### 8.2 实现清单（本次）

| # | 需求 / 请求 | 实现 | 测试 |
|---|---|---|---|
| 1 | d2-WP-12c-to-M06 第 3 条：场视图灰阶 token（AWR-04 §15.11.2 第 2 条，`--field-ramp-1…5`） | `engine/field/materials.ts fieldRamp()` 直接读 `SCENE.fieldRamp`（M15 已登记并生成），去掉 `windRamp` 回退 | `fieldBuild.test.ts`"no hue"：灰阶即 `SCENE.fieldRamp`、5 档中性灰、无 r500 |
| 2 | d2-WP-12c-to-M06 第 2 条：ViewCube 与 coarse 指针下 44 px 视图工具列重叠（AWR-04 §15.8.5） | `viewport/overlay/ViewCube.tsx`：`[html[data-coarse]:not([data-ff=phone])_&]:mr-12`（44 px 工具列 + 4 px 间距），非 coarse 仍 `mr-9`；M15 的临时覆盖规则可以删除 | 诊断（iPad Pro 11 横屏、12.9 横屏、桌面）：coarse 下右边距 48 px，立方体右侧命中视图工具按钮；生成的 CSS 含该变体选择器；`d21-mobile` 11 例通过 |
| 3 | d2-WP-12b-to-M06 第 5 条：FPV 内拖动改云台的视口接口（M15-FR-218，P0） | `viewport/interaction.ts`：FPV 相机下鼠标主键拖动（超过 4 px 点击容差，指针捕获）与单指触控拖动发事件 `fpv.drag {dxRad, dyRad, phase}`（start 带按下以来的位移，move 为增量，end 为 0）；角度按 FPV 投影的画面中心焦距（投影矩阵第 0、5 元素 × 尺寸 / 2）与 LWIR 数字变焦换算（`fpvRadPerPx`）；拖动不产生点击、不改选择；`pointercancel` 结束拖动。`viewport/input/touch.ts`：`touchDispatch` 在无触控相机（FPV）时把单指拖动交给 `deps.drag`（拖动一旦开始，相机中途恢复也在拖动路径上结束）。门面 `fpv.onDrag(cb)`、`fpv.radPerPx()`；测试钩子 `__vp.fpvDrags()`、`__vp.fpvRadPerPx()` | `touch.test.ts`"touch dispatch"2 例；`field-views` 第 5 例：FPV 下触控拖动为 start、move × 3、end，Σdx = 120 px × radPerPx、Σdy = 0；鼠标拖动 start…end，Σdy = 50 px × radPerPx、选择不变；FPV 内单击无拖动事件；非 FPV 的拖动无事件 |
| 4 | FPV 下触控交给页面（本次发现的缺陷；AWR-04 §15.8.4"视口画布 touch-action: none"） | camera-controls 在禁用时（FPV 相机）把宿主内联 `touch-action` 清空，FPV 下的触控因此被浏览器当作页面平移与缩放（第一次 pointermove 后即 `pointercancel`，捏合会缩放页面）。`viewport/WorldCanvas.tsx` 宿主加类 `touch-none`，内联值被清除后仍为 `none` | `field-views` 第 5 例：FPV 下宿主计算值 `touch-action: none`，捏合后 `scrollX/scrollY/visualViewport.scale` 为 0/0/1；修复前的诊断记录到 `pointercancel` |
| 5 | 视口边缘 16 px 不起手势（AWR-04 §15.16 风险对策，P1） | `touch.ts`：`TOUCH.edgePx = 16`、纯函数 `nearEdge`；首指落在宿主边缘 16 px 内的触控序列整体忽略（后续手指也忽略到抬起），留给系统的返回与下拉手势 | `touch.test.ts`"edge band"；`field-views` 第 5 例：从左缘 6 px 起手的拖动不改相机位姿，同样的拖动在内部起手则旋转 |
| 6 | d2-WP-12c-to-M06 第 4 条：prod-bundle-scan 的口令与身份检查（M15-FR-236、M15-AC-227 的工具部分；ADR-123） | `tests/m06/lint/prod-bundle-scan.mjs`：(a) REL-04 同源规则：本机有部署口令文件（`--secret-file`、`AWR_ADMIN_SECRET_FILE`、`~/.config/awr/admin.secret`，读取函数复用 `tools/lint/check-release.mjs` 的 `deploySecret`）时扫描构建的全部文本文件，只报告文件与偏移，口令值不进入任何输出；(b) `/api/auth/identity` 字面量只允许出现在一个分块（`stores/identity` 所在分块）；(c) viewer 首屏分块 = `index.html` 入口脚本与模块预加载的静态导入闭包（演示构建即落地页集合）∪ 入口动态导入的应用入口 `main-*` 的静态闭包，其中不得出现隐藏入口标记 `data-internal-entry`（标记与懒加载由 M15 落实，请求第 14 条）；输出各项计数。`make scan-m06-bundle`、`make demo-build`、`build-local.sh` 的调用方式不变 | `bundleScan.test.ts` 新增 3 例：入口与静态 / 动态导入解析；首屏闭包与身份分块的正反例；临时文件中的随机口令注入到分块与 `build-info.json` 后按文件报告、退出码 1、stdout 与 stderr 不含口令。实际构建：演示构建与普通生产构建均 clean（`/api/auth/identity` 在 1 个分块；首屏 12 / 9 个分块；标记 0 个）；`tests/ops/test_deploy_install.py`（真实 `build-local.sh` 演示构建与扫描）50 例通过 |
| 7 | 场视图不显示时的每帧工作（D2-AC-30 的余量；d2-ACC-1-to-M06 第 2 条相关） | `FieldRenderer.update`：根节点不可见（无视图、无感知体、图层隐藏）时不写锚点表、不置纹理更新标志 | `field.browser.test.ts`、`field-views` 第 1 例（切换、锚点缺失折叠照常） |

### 8.3 偏差（本次新增，编号接 §4）

| # | 主题 | 基线 | 实现 | 理由 |
|---|---|---|---|---|
| 11 | FPV 拖动载荷的角度口径 | d2-WP-12b-to-M06 第 5 条只给出 `{dxRad, dyRad, phase}` | 角度为画面坐标（x 右、y 下）的增量，按画面中心焦距换算；yaw 的符号由 M15 按 M13 云台 `yaw_rad`（相对机头）的正向约定取号 | 视口不掌握云台正向约定；画面中心近似在 5 Hz 节流的增量控制下足够（60° 视场的画面边缘误差约 10%） |
| 12 | 边缘 16 px 的范围 | §15.16"视口边缘 16 px 不起手势" | 以视口宿主矩形为界，包括起手于边缘的单击与长按 | 手机上视口即全屏，宿主边缘即屏幕边缘；平板上宿主边缘邻接停靠面板，忽略无副作用 |
| 13 | 首屏隐藏入口检查的标记 | M15-FR-236"viewer 首屏分块不含这些入口" | 以页面根元素属性 `data-internal-entry` 为标记（压缩后仍是字符串字面量） | 压缩后的分块没有模块路径可依；当前 0 个分块带标记，检查在 M15 加标记并懒加载后生效（请求第 14 条） |

### 8.4 测试结果（本次）

本机运行（其他工作包并行，load average 约 10–13）；未跑性能基准。私有构建均在 scratchpad（未动共享 `apps/web/dist`）：测试构建
`VITE_AWR_TEST_SWITCHES=1 npx vite build`、演示构建 `AWR_BUILD_CHANNEL=demo VITE_AWR_DEMO=public npx vite build`、普通生产构建各一份。

| 范围 | 命令 | 结果 |
|---|---|---|
| TS 类型 | `npx tsc -p apps/web/tsconfig.json --noEmit` | 0 错误 |
| Vitest unit 全量 | `npx vitest run --project unit` | 167 文件 1372 例通过、1 跳过（原状） |
| Vitest browser 全量 | `npx vitest run --project browser` | 21 文件 61 例全部通过（含 `field.browser.test.ts`） |
| Playwright `m06.field-views` | `M06_DIST=… npx playwright test -c perf/m06/playwright.m06.config.ts perf/m06/field-views.spec.ts` | 6 例全部通过（新增第 5 例） |
| gpu-limits（ADR-086、087；D2-AC-49、54） | `GPU_LIMITS_BUILDS=test,demo GPU_LIMITS_SETS=min`（两种构建为本次私有构建） | 10 例全部通过（12.0 min）：测试与演示构建 × Tier S、B × 最低上限 4 例；低模实例矩阵 Tier S、B；D2.1 场视图缺省 4 例（S min desktop glpoint 1328 点；S mobile phone quad 1136 点；S mobile tablet quad 2267 点；B mobile phone quad 1136 点），全部 WebGL 错误 0、three 告警 0、绑定点 3、单程序块数 V 3 F 2、揭开后 programs 不变（27 → 27、29 → 29） |
| Playwright M06 回归 | smoke、warmup、wiring、sensor-views、layout、drone-tiers | 14 例全部通过 |
| Playwright M15 D2.1 移动（受 ViewCube 与触控改动影响） | `M15_DIST=… npx playwright test --project=perf perf/m15/d21-mobile.spec.ts` | 11 例全部通过（设备矩阵 8 例含命中区审计 0 违例、手机五窗口、平板指挥器含长按、PWA） |
| prod-bundle-scan | 演示构建、普通生产构建；测试构建 | 前两者 clean（退出码 0）；测试构建按设计拒绝（退出码 2） |
| pytest 子集 | `pytest -m "not perf" tests/contracts tests/perception`；`tests/ops/test_deploy_install.py` | 570 通过；50 通过 |
| pytest 全量 | `pytest -m "not perf" --continue-on-collection-errors` | 4251 通过、88 跳过、2 失败（1 h 20 min）：`tests/sensors/test_pose_chain_e2e.py::test_roster_sensors_pose_topic_and_state_ext` 与 `tests/targets/test_wire.py::test_lite32_and_detail` 均为异步等待超时（`TimeoutError`），两文件单独复跑 2 例全部通过；两者不读取 `apps/web`，本包无 Python 改动（第 1 次执行的全量运行中同两例也曾失败，§5.3） |
| lint | `make lint` | 通过（退出码 0）；首次运行报本包新增代码的 2 处 `require-array-sort-compare`，已改正 |

### 8.5 遗留（更新）

| 编号 | 状态 | 说明 |
|---|---|---|
| WP11b-L1 | 已由 WP-12c 接线 | 工具条、图例、读数卡、长按菜单、PowerController 均已接（d2-WP-12c-to-M06 第 1 条）；Sandbox 小窗口挂起口径待 M15 确认 |
| WP11b-L2 | 未落实 | M05 尚未读取 `be.quality.pointBudgetMax` 与 `releaseLowPriority`（D2-INT 同） |
| WP11b-L3 | 未落实 | M07 尚未读取 `be.quality.envLevel` |
| WP11b-L4 | 未变 | 切换 ≤ 300 ms、场视图帧成本、真机帧率待安静环境基准 |
| WP11b-L5 | 已由 M16 覆盖 | `perf/d21/gpu-limits-d21.spec.ts` 走 UI 工具条覆盖测试与演示构建的场视图（D2 验收第 1 轮 D2-AC-54 通过） |
| WP11b-L6 | 未变 | M19 剖面仍无 `r_max_m` |
| WP11b-L7 | 未变 | 文档总编（并入本次第 8–12 条） |
| WP11b-L8（新） | M15 | `fpv.onDrag` 接到 5 Hz 云台发送；删除 `mobile.css` 的 ViewCube 覆盖；隐藏入口加 `data-internal-entry` 并懒加载（d2-WP-11b-to-M15 第 9–14 条） |
| WP11b-L9（新） | M06 / 文档总编 | d2-WP-12b-to-M06 第 1 条（`engine/index.ts` 门面导出 perception 与 targets）：该文件不在 WP-11b 的路径（AWR-04 §15.15.1），本包未改，UI 仍经 `app/shell/engineD2.ts` 过渡 |
| WP11b-L10（新） | M05、M06、M15 | d2-WP-12-to-M06 第 3 条原地换世界（P1）：需要 M05 点云与世界的原地重载和 M15 `useWorldFollow` 改动，超出本包范围；沙盒换底图仍整页刷新（D2-INT §9.4） |
| WP11b-L11（新） | M16 | d2-FX-R2-web-to-M06 第 6 条的安静环境复测（`layers`、`flight60`、`m06.sensor-views`、`m06.field-views`、gpu-limits 全组）属基准阶段；本次只跑了功能组 |

### 8.6 请求（本次追加到既有文件）

- `d2-WP-11b-to-M15.md` 第 9–14 条：删除 `mobile.css` 的 ViewCube 覆盖；`SCENE.fieldRamp` 已用；`fpv.onDrag` 的载荷、符号与 5 Hz 接线；FPV 触控修复（告知）；
  边缘 16 px（告知）；prod-bundle-scan 的三项检查、`data-internal-entry` 标记与隐藏入口懒加载。
- `d2-WP-11b-to-M16.md` 第 5–6 条：`m06.field-views` 第 6 例；M15-AC-227 的扫描判定方法（临时随机口令文件）。
- `d2-WP-11b-to-文档总编.md` 第 8–12 条：门面 `fpv` 与事件 `fpv.drag`；§15.8.4 增"边缘 16 px"与宿主 `touch-action` 常设；M06-AC-010 扫描范围扩展；
  场视图降级表与 D2-FX-R2-web 同一修订；`engine/index.ts` 的工作包归属。

### 8.7 文件清单（本次）

| 路径 | 改动 |
|---|---|
| `apps/web/src/engine/field/{materials,FieldRenderer,budget}.ts` | 灰阶 token；不可见时不写锚点；注释同步治理器级数 |
| `apps/web/src/viewport/layers/field.tsx` | 注释同步治理器级数 |
| `apps/web/src/viewport/input/touch.ts` | `touchDispatch`（FPV 单指拖动路径）、`nearEdge` 与边缘带、`TOUCH.edgePx` |
| `apps/web/src/viewport/interaction.ts` | FPV 鼠标与触控拖动、`fpvRadPerPx`、`FpvDrag`、`pointercancel` |
| `apps/web/src/viewport/{facade,testHooks}.ts` | 门面 `fpv.{onDrag, radPerPx}`；测试钩子 `fpvDrags`、`fpvRadPerPx` |
| `apps/web/src/viewport/WorldCanvas.tsx` | 宿主类 `touch-none` |
| `apps/web/src/viewport/overlay/ViewCube.tsx` | coarse 指针下的右边距 |
| `apps/web/tests/m06/lint/prod-bundle-scan.mjs` | REL-04、身份分块、首屏隐藏入口 |
| `apps/web/tests/m06/{bundleScan,touch,fieldBuild}.test.ts` | 新增 6 例、改 1 例 |
| `apps/web/perf/m06/field-views.spec.ts` | 新增第 5 例 |
