# VERIFY-UBO FX-UBO 修复的对抗式验证报告

| 项 | 内容 |
|---|---|
| 工作包 | VERIFY-UBO（对 FX-UBO 的对抗式验证；区域：前端渲染后端 M06，涉及测试 spec 与文档） |
| 日期 | 2026-10-04 |
| 起因 | 公开演示站 `https://drone4d.agentnetwork.org.cn/world/synthcity` 在真实 GPU 浏览器上报 "Maximum number of simultaneously usable uniforms groups reached"、"uniform buffer that is too small"、THREE.Clock 与 AttributeNode 告警；FX-UBO 已提交修复（`docs/impl/FX-UBO-报告.md`，ADR-086） |
| 依据 | ADR-007、ADR-086；M06 PRD §6.3、FR-085、AC-058；AWR-18 §8.5、§10；three r186（`node_modules/three` 0.186.1）`WebGLNodesHandler.js`、`three.module.js` 的 `setProgram` / `renderObject` / `render`、`three.webgpu.js` 的 `createInstanceMatrixNode`、`instance()`、`EventNode`、`NodeBuilder.buildUpdateNodes`、`WebGLCapabilities.getUniformBufferLimit` |
| 环境 | 8 核 CPU、无 GPU；Chrome for Testing 151（SwiftShader，C1）；Node 22.12；本机私有构建 `.cache/verify-ubo/{test,default,demo}`（测试构建、默认生产构建、演示构建；生产包扫描两者均 clean）；对照用第 6 轮构建 `apps/web/dist`（默认，10-03 22:48）与 `.cache/acc6/dist-test`（测试） |
| 约束执行 | 未安装或升级依赖；未执行 git 写操作（目录不是 git 仓库）；无 emoji；新 ADR 为 ADR-087 并登记 §7.0；M06 PRD 同步，M05 / M07 无对应变化；`make lint` 通过 |
| 结论 | **可以上线（需按 `tools/deploy/emax/build-local.sh` → 服务器 `install.sh` 用本次代码重新打包部署）。** FX-UBO 的修复 5 在全部上限组合、三种构建、两档下成立：WebGL 错误与 three 告警为 0，使用中的绑定点恒为 3，200 架、全部天气与标签、20 次世界切换的加压下也没有超限路径。验证中发现并修复了 1 个同源的真实 GPU 缺陷（Tier B 低模无人机在 `MAX_UNIFORM_BLOCK_SIZE` = 16 384 的设备上画在原点，修复 6，ADR-087）与 1 个 spec 覆盖缺口；性能经同机新旧构建交替对照没有可测的退化 |

## 1 结论与问题清单

### 1.1 可否上线

可以。理由：

1. 用户报错的根因（绑定点随材质数线性增长）已消除：26 例上限矩阵、加压、实时后端（public profile + 演示构建 + 剧本 S0）全部 0 条 WebGL 错误、0 条 three 告警与错误，`gl.getError()` 为 0。
2. 验证中新发现的问题 P1 与用户同一类硬件直接相关（ANGLE Metal 的 uniform block 大小上限按 GLES 3.0 最低值 16 384 报告，与 24/12/12/24 同一组常量），已修复并有回归覆盖；不修复时线上 Tier B 的低模无人机全部画在原点且不报错。
3. 性能：交替对照下与第 6 轮构建没有可测差别（第 5 节）。

上线前提：用本次工作树重新打包部署；部署后建议在一台 Apple Silicon Mac 的 Chrome 上打开 `/world/synthcity`，确认控制台干净并在控制台执行 `document.querySelector('[data-viewport] canvas').getContext('webgl2').getParameter(0x8a30)`（`MAX_UNIFORM_BLOCK_SIZE`）记录真实值（本机无真实 GPU，16 384 这一取值来自规范最低值与 ANGLE Metal 常量，未在真机核对）。

### 1.2 问题清单

| 编号 | 严重度 | 问题 | 处置 | 状态 |
|---|---|---|---|---|
| P1 | 高（真实 GPU 上功能错误，无任何告警） | three 的实例矩阵在 `count × 64 B > MAX_UNIFORM_BLOCK_SIZE` 时改用交错实例属性，靠 `OnBeforeFrameUpdate` 节点同步版本；`WebGLNodesHandler` 从不运行 updateBefore 节点，交错缓冲停在首次上传（单位矩阵）。Tier B 低模批次容量 300（19 200 B）在报告 16 384 的设备上走此路径：全部低模无人机画在 ENU 原点且不随位置更新。SwiftShader 报 65 536，所有用例都走 uniform block 路径，测不到 | `AnetNodesHandler` 修复 6（ADR-087，M06-FR-086）：FRAME 类 updateBefore 节点在 `renderStart`（早于本帧属性上传）运行，被绘制程序的全部 updateBefore 节点在其 `onBeforeRender` 运行；spec 最低上限组增加 16 384 与低模 GPU 回读（M06-AC-059）；浏览器单元测试 2 例 | 已修复并复验 |
| P2 | 中（测试覆盖缺口） | `gpu-limits.spec.ts` 在演示构建上经命令面板输入 "HAG" / "Class" / "Height" 切换着色，但命令面板没有着色动作，三次都是"没有匹配的结果"，FX-UBO 报告所说的"命令面板着色"实际上没有执行；结束截图被面板遮住 | 改为点击图层面板的着色分组（离地、类别、法线、高度），并断言每项按下 | 已修复并复验（演示构建 Tier S/B、默认构建 Tier S/B 通过） |
| P3 | 低（spec 能力） | spec 只覆盖测试构建与演示构建、两组上限，不覆盖默认生产构建与 SwiftShader 自身上限；链接检查不含 block 大小 | 增加 `GPU_LIMITS_SETS`（`min`、`wide` 24/16/16/32、`mid`、`swiftshader`）与 `GPU_LIMITS_BUILDS`（`test`、`default`、`demo`），链接时检查 `UNIFORM_BLOCK_DATA_SIZE`；缺省仍为 10 例（约 20 min） | 已完成 |
| P4 | 低（既有问题，与本次修复无关） | `m06.layout`（`perf/m06/layout.spec.ts`）Tier S 一例的 "> 50 ms 占比 ≤ 基线 + 1 个百分点" 在性能锁下失败（交互期 39–51 %，基线 11–29 %）；第 6 轮测试构建同样失败（51.3 %、40.0 %），最近一次通过记录在第 1 轮。D1-AC-24 的正式用例 `layout`（根 spec，生产构建）通过 | 不在本工作包范围；建议 M06 / web-ui 复查该例的阈值口径（SwiftShader 下 Ctrl+B 与 Dock 拖动的单帧代价） | 待处理 |
| P5 | 低（观察） | 加压与天气用例中偶见 ANGLE 性能提示 "GL Driver Message (OpenGL, Performance, …): Running out of reserved outsideRenderPass queueSerial. ending renderPass"（Tier S 加压 4 条；天气用例 Tier S 模拟与 Tier B 不模拟各 1 条），不是 GL 错误。深圳 200 架 90 s 的新旧构建对照两边都没有出现；它出现在天气每秒切换与世界切换期间，是 SwiftShader 所走的 ANGLE Vulkan 后端特有的提示，ANGLE Metal 没有这条路径 | 记录；`gpu-limits` 的 WebGL 错误判定不含这类性能提示 | 观察 |
| P6 | 提示 | 修复 6 不运行 updateAfter 节点；D1 场景中没有（spec 断言），新增依赖 updateAfter 的节点须先扩展 handler | 写入 ADR-087、M06-FR-086、`viewport/README.md` | 已登记 |

## 2 FX-UBO 修复 5 的代码复核

逐条对照 three r186 源码核对 `anetNodesHandler.ts` 与 `backend/uboBinder.ts`：

| 检查点 | 结论 |
|---|---|
| 绑定时机：`renderObject` 中 `object.onBeforeRender` → 计算 `modelViewMatrix` → `material.onBeforeRender` → `renderBufferDirect` → `setProgram` | 修复 5 在材质 `onBeforeRender` 中按 `currentProgram` 绑定，此时 `modelViewMatrix` 已更新；程序在 `setProgram` 内切换（`needsProgramChange`）时 `onUpdateProgram` 在 `useProgram` 之前为新程序再绑定一次，最终状态正确 |
| 透明双面材质（同一次 `renderObject` 内两次 `renderBufferDirect`，`needsUpdate` 触发程序切换） | 每次都经 `onUpdateProgram` 重新绑定 |
| `compile` / `compileAsync`（着色器预热）只调用 `getProgram`，不调用 `onUpdateProgram` | 首次 draw 时 `materialProperties.__version` 未设置，`needsProgramChange` 为真，`onUpdateProgram` 必被调用 |
| 绕过 `renderObject` 的 `renderBufferDirect` 直接调用 | 应用代码中没有（`grep`） |
| 非节点材质的 `uniformsGroups`（会与绑定点 0..2 冲突） | 规则 12 在 dev / test 构建断言；three 内置材质不使用 |
| 缓冲大小与 std140 布局 | 取 three 布局与 `UNIFORM_BLOCK_DATA_SIZE` 的较大者；单元测试逐项对照 |
| 组、程序与缓冲的生命周期 | 组按（材质，程序）创建，材质 dispose 时删除缓冲；20 次世界切换中存活组恒为 57 / 63、programs 恒为 27 / 31（第 4 节），没有泄漏 |
| 首帧反向深度投影 | `renderStart` 在 `projectObject` 之前按 `setProgram` 的同一条件切换；视锥裁剪也因此用到正确的投影（stock 首帧用的是旧投影） |
| 每帧 GL 调用 | 深圳 40 架同一场景：第 6 轮构建每帧 `uniformBlockBinding` 16、`bindBuffer(UNIFORM_BUFFER)` 32、`bindBufferBase` 0；修复后 `bindBufferBase` 16、`bindBuffer` 4、`uniformBlockBinding` 0；draw 17、`useProgram` 26、`bufferSubData` 8.2–8.6 次（约 41 B）两者相同 |

由此发现的风险点只有一处：three 生成的着色器本身依赖设备上限，而不仅是 block 数。据此做了第 3 节的资源审计，找到 P1。

## 3 新发现的问题 P1 与修复 6

### 3.1 资源审计（SwiftShader 与 WebGL2 规范最低值）

C1 实测上限与规范最低值：

| 参数 | SwiftShader（C1） | WebGL2 最低值 | D1 程序实测最大（链接后审计） |
|---|---|---|---|
| `MAX_UNIFORM_BUFFER_BINDINGS` / 每阶段 block / 合计 | 72 / 14 / 60 | 24 / 12 / 24 | 绑定点 3，block 顶点 3 / 片元 2 |
| `MAX_UNIFORM_BLOCK_SIZE` | 65 536 | 16 384 | **19 200 B（Tier B 低模 NodeBuffer）**；修复后在 16 384 下为 1 088 B |
| `MAX_VERTEX_UNIFORM_VECTORS` / `MAX_FRAGMENT_UNIFORM_VECTORS` | 4096 / 4096 | 256 / 224 | 默认块 uniform 0（全部在 block 中） |
| `MAX_TEXTURE_IMAGE_UNITS` / `MAX_VERTEX_TEXTURE_IMAGE_UNITS` / 合计 | 32 / 32 / 64 | 16 / 16 / 32 | 顶点 7、片元 3 |
| `MAX_VARYING_VECTORS` / `MAX_VERTEX_OUTPUT_COMPONENTS` | 31 / 128 | 15 / 64 | 6 行、12 分量 |
| `MAX_VERTEX_ATTRIBS` | 16 | 16 | 节点程序至多 7；16 个属性的只有 SwiftShader 专用的顶点槽重置程序（无 block、无 varying） |
| `MAX_TEXTURE_SIZE` / `MAX_DRAW_BUFFERS` | 8192 / 6 | 2048 / 4 | 未逐项统计（MRT 不在共享路径，规则 9） |

唯一超出最低值的是 Tier B 低模批次的实例矩阵 block。three r186 `createInstanceMatrixNode` 在 `count × 64 B` 超过 `getUniformBufferLimit()`（即 `MAX_UNIFORM_BLOCK_SIZE`）时不会生成超限的 block，而是改用交错实例属性，所以不会链接失败，而是走一条 SwiftShader 上从不出现的代码路径。

### 3.2 复现

测试构建，Tier B，FakeSource 40 架绕飞，相机距首架约 85 m（邻近机落入低模档），init script 模拟 24/12/12/24 与 `MAX_UNIFORM_BLOCK_SIZE`；在低模批次的 draw 调用处回读 GPU 实际读取的首个实例平移（交错属性 `nodeAttribute3` 所在缓冲，或 NodeBuffer 所在绑定点的缓冲），与 CPU `instanceMatrix` 比较：

| 构建 | 模拟的 block 大小 | 路径 | GPU 读到的平移（4 次） | CPU `instanceMatrix`（同时刻） |
|---|---|---|---|---|
| 修复前（FX-UBO 代码） | 16 384 | 交错属性，缓冲版本恒为 0（`instanceMatrix` 版本 24 → 114） | (0, 0, 0) ×4 | (−49.8, −4.3, 28.4)、(−45.7, −20.2, 28.1)、(−36.7, −33.9, 28.0)、(−23.8, −44.0, 28.3) |
| 修复前 | 不模拟（65 536） | NodeBuffer（绑定点 2） | 与 CPU 逐分量一致 | — |
| 修复后 | 16 384 | 交错属性 | 与 CPU 逐分量一致（误差 0） | — |

GPU 拿到的是着色器预热时的单位矩阵：低模无人机全部叠在世界原点，且之后不再移动；标记、标签与轨迹照常在真实位置，控制台没有任何告警。截图对照见 scratchpad `verify/origin-{nofix6,test}-fleet.png`（修复前该处只有标记点，修复后出现低模机体）。

### 3.3 修复 6

`apps/web/src/viewport/anetNodesHandler.ts`：

- `onUpdateProgram` 新建程序条目时，从 `material._latestBuilder.updateBeforeNodes` 记下该程序的 updateBefore 节点（FRAME 类另存一份）。
- `renderStart`（super 之后、`projectObject` 之前）对全部存活程序的 FRAME 类节点调用 `nodeFrame.updateBeforeNode`：交错缓冲的版本与更新区间在本帧属性上传之前同步，本帧矩阵本帧到达 GPU。遍历用预先绑定的 `Map.forEach` 回调，每次渲染不分配对象（AWR-03 §3.6 规则 1）。
- 材质 `onBeforeRender` 在 stock 的 update 节点之前运行被绘制程序的全部 updateBefore 节点（OBJECT / RENDER 语义；FRAME 类由 NodeFrame 按 frameId 去重），次序与 WebGPURenderer 的 `updateBefore → updateForRender` 一致。
- updateAfter 节点不运行（D1 中没有，spec 断言）。

为什么不放在 `onBeforeRender` 里一并处理：几何属性在 `projectObject` 阶段上传，早于任何 `onBeforeRender`，只在那里同步会让低模机体比标记与标签晚一帧。其他备选（降低低模容量到 256、直接改交错缓冲版本、分叉 three）见 ADR-087。

### 3.4 回归

| 用例 | 结果 |
|---|---|
| `tests/m06/ubo.browser.test.ts` 新增 2 例：300 实例的 InstancedMesh 在模拟 16 384 下走实例属性；stock `WebGLNodesHandler` 移动实例后像素仍在原处（复现），`AnetNodesHandler` 画在新位置、无 GL 错误 | 8/8 通过 |
| `perf/m06/gpu-limits.spec.ts` 新增低模 2 例（测试构建 Tier S / B，M06-AC-059）：4 次回读 GPU 与 CPU 一致、机体确实移动、Tier B 走属性而 Tier S 走 NodeBuffer、场景中没有带 updateAfter 节点的程序 | 通过（全矩阵与最终 harness 各 1 次） |
| 对照：关闭修复 6 的测试构建跑 Tier B 低模例 | 失败（GPU (0, 0, 0)，收到 37.95，要求 < 0.001），说明用例能抓到该缺陷 |

## 4 上限矩阵、加压与实时后端

### 4.1 上限矩阵（`gpu-limits.spec.ts`，`GPU_LIMITS_SETS=all GPU_LIMITS_BUILDS=test,default,demo`，26 例全部通过，1.1 h）

整景操作序列与 FX-UBO 相同：揭开、12 个天气预设（3 s 阶跃）、选中、Third、FPV、P600 近景、8 个图层逐一关闭再打开、拾取、着色切换。默认构建与演示构建全部经 UI 操作，Tier B 伪装 Apple M2 渲染器字符串。所有例：绑定点 3，单程序 block 顶点 3 / 片元 2 / 合计 5 / 去重 3（链接后实测相同），超限 0，裁剪 0，12 个预设全部经过，揭开后 programs 不变，WebGL 错误 / three 告警 / 其他错误均为 0，`gl.getError()` 为 0。

| 构建 | 档位 | 上限（绑定点 / 顶点 / 片元 / 合计 / block 大小） | 链接的程序 | 存活组 | programs | 点云像素差 / 噪声 | 无人机像素差 / 噪声 |
|---|---|---|---|---|---|---|---|
| 测试 | S | 24/12/12/24/16384、24/16/16/32/16384、36/14/14/28/65536、72/14/14/60/65536（不模拟） | 30 | 57 | 27 → 27 | 130 231 / 0 | 2 130 / 0 |
| 测试 | B | 同上四组 | 34 | 63（16 384）/ 65（65 536） | 31 → 31 | 130 157 / 0 | 2 130 / 0 |
| 默认 | S | 同上四组 | 30 | 57 | 27 → 27 | 154 296–154 298 / 0–850 | 1 234–1 270 / 0 |
| 默认 | B | 同上四组 | 33 | 63 / 65 | 31 → 31 | 153 314 / 0 | 941 / 0 |
| 演示 | S | 同上四组 | 30 | 57 | 27 → 27 | 154 296–154 298 / 850–2 179 | 1 193–1 263 / 0 |
| 演示 | B | 同上四组 | 33 | 63 / 65 | 31 → 31 | 153 314 / 0 | 940–941 / 0 |
| 测试 | S / B | 低模回读（24/12/12/24/16384） | — | — | — | GPU = CPU（Tier S NodeBuffer、Tier B 交错属性） | — |

Tier B 在 16 384 下存活组少 2 个（两个低模批次改走属性，没有 NodeBuffer 组）。修复 6 与 spec 修改之后，在最终构建上重跑 harness `m06.gpu-limits`（缺省 10 例）与默认构建的最低上限 2 例，全部通过（19.5 min、5.6 min）。

截图（用 Read 逐张查看，`.cache/verify-ubo/shots/`）：各组合的揭开视图都有完整点云城市与 6 架无人机标记，P600 近景的机体、选中环与视锥线正常，图层面板与着色分组正常；没有黑屏、缺层或错位。

### 4.2 加压（测试构建，模拟 24/12/12/24/16384，`stress.mjs`）

FakeSource 200 架绕飞，全部图层与标签打开，天气预设每 1 s 阶跃切换；选中 32 架（关注集、轨迹、标签），Third / FPV / 轨道，相机三次进入机群（hero、低模、标记同时存在），P600 近景，6 种着色与一次拾取，8 个图层开关 3 轮，经路由切换世界 20 次（深圳、纽约、上海、苏州、芝加哥、旧金山、synthcity 循环）。

| 档位 | 用时 | WebGL 错误 | three 告警 | 其他错误 / pageerror | 绑定点 | 存活组（24 次快照） | programs（24 次快照） | 链接后最大 block | `gl.getError()` |
|---|---|---|---|---|---|---|---|---|---|
| S | 104 s | 0 | 0 | 0 / 0 | 3 | 恒为 57（创建 60、删除 3） | 恒为 27 | 2 048 B | 0 |
| B | 116 s | 0 | 0 | 0 / 0 | 3 | 恒为 63（创建 66、删除 3） | 恒为 31 | 1 088 B | 0 |

没有找到仍会超限的路径：绑定点只取决于单程序 block 数，世界切换与天气切换都不新建材质或程序，组数不增长。加压期间 Tier S 有 4 条 ANGLE 性能提示（P5）。截图：200 架机群、标签与轨迹、各世界切换后的画面正常（"近景"一张因机体在等待 hero 的过程中飞离而为空，属脚本取景问题）。

天气（`weather.mjs`，测试构建 40 架，暴雨、雷暴、雪、暴风雪、雾、沙尘、晴，Tier S / B 各在模拟最低上限与不模拟下截图）：两种上限下的同一预设画面一致（雾的远景衰减、沙尘的色调、暴风雪的白化、降水），控制台除 P5 外无告警。

### 4.3 实时后端（用户的路径）

public profile（与 `tools/deploy/emax` 的 ExecStart 相同：剧本 S0、只服务 synthcity、18640）服务演示构建，浏览器直接打开 `/world/synthcity`，模拟 24/12/12/24/16384，观察 90 s 剧本后经 UI 选中、Third / FPV / 轨道、滚轮推近、图层 16 次开关、点击拾取（该脚本的着色沿用了命令面板写法，与 P2 相同未生效；着色在 4.1 的演示构建用例中经图层面板覆盖）：

| 档位 | 揭开 | 无人机（剧本 S0） | WebGL 错误 | three 告警 | 其他错误 / HTTP 4xx | 绑定点 | 存活组 | programs | `gl.getError()` |
|---|---|---|---|---|---|---|---|---|---|
| S | 19.5 s | 7 | 0 | 0 | 0 / 0 | 3 | 57 | 27 → 27 | 0 |
| B（Apple M2 伪装） | 20.9 s | 7 | 0 | 0 | 0 / 0 | 3 | 63 | 31 → 31 | 0 |

修复前同一路径（线上站点，FX-UBO 以 `limits-repro.mjs` 复现）是数十次 "Maximum number…" 与数百条 GL_INVALID_OPERATION。截图中点云、7 架无人机、标签、近景机体与拾取标记正常（Tier B 在 SwiftShader 上约 1 fps，链路标签显示 STALE，与渲染无关）。

## 5 性能复核

### 5.1 按任务要求的单次运行（排他锁，harness `--runs 1 --gate G4`，默认生产构建）

| 用例 | 状态 | p50 / p95 / p99 | > 50 ms | > 100 ms | 最大间隔 | TTFP | 第 6 轮（3 次中位） |
|---|---|---|---|---|---|---|---|
| `flight60.shenzhen.full` | FAIL | 33.3 / 66.7 / 100.0 | 10.07 % | 0.47 % | 300.0 ms | 7.0 ms | 33.3 / 50.0 / 66.7，2.56 %，0.060 %，116.7 ms，6.6 ms |
| `flight60.newyork.pc` | PASS | 33.3 / 50.0 / 66.7 | 4.70 % | 0.125 % | 133.3 ms | 6.0 ms | 33.3 / 50.0 / 66.7，3.78 %，0，83.3 ms，1.5 ms |

这两次紧接在 1.1 h 的上限矩阵之后运行（开跑前 1 分钟 load 2.05 / 3.58，15 分钟 load 约 6），深圳整景的 > 50 ms 与最大间隔超过阈值（10 %、250 ms）。为判断是代码还是环境，在同一台机器上交替运行第 6 轮构建（`apps/web/dist`）与本次构建。

### 5.2 新旧构建交替对照（同一协议，单次运行，冷却到 1 分钟 load ≤ 1.5 后开跑的标为"冷却"）

| 用例 | 构建 | 运行 | p95 | p99 | > 50 ms | > 100 ms | 最大间隔 | TTFP |
|---|---|---|---|---|---|---|---|---|
| shenzhen.full | 第 6 轮 | 不冷却 2 次 | 66.7、50.0 | 83.3、66.7 | 6.20、3.80 % | 0.256、0.062 % | 233.3、166.7 | 1.6、6.3 |
| shenzhen.full | 本次 | 不冷却 2 次 | 66.7、50.0 | 100.0、83.3 | 6.53、5.90 % | 0.314、0.507 % | 150.0、266.7 | 2.3、5.5 |
| shenzhen.full | 第 6 轮 | 冷却 3 次 | 50.0 ×3 | 83.3、66.7、66.7 | 4.73、4.96、3.05 % | 0.128、0.064、0 | 116.7、133.3、83.3 | 97、107、115 |
| shenzhen.full | 本次 | 冷却 3 次 | 50.0 ×3 | 66.7 ×3 | 3.50、4.82、3.40 % | 0.062、0.129、0.124 % | 133.3 ×3 | 7.2、6.5、104.7 |
| newyork.pc | 第 6 轮 | 冷却 2 次 | 50.0、50.0 | 66.7、50.0 | 1.68、1.01 % | 0、0 | 100.0、66.7 | 5.4、3.3 |
| newyork.pc | 本次 | 冷却 2 次 | 50.0、50.0 | 66.7、50.0 | 1.56、0.92 % | 0、0 | 66.7、83.3 | 1.2、3.6 |

- 冷却后两边分布重合，全部满足阈值；不冷却时两边都明显变差（第 6 轮构建的 > 50 ms 也到 6.2 %、最大间隔 233 ms）。5.1 的失败是紧接长时间满载之后的环境影响，不是代码退化。本机今天的后端单步 p99（2.2–6.8 ms）也普遍高于第 6 轮（2.8–3.8 ms）。
- TTFP 在部分运行中为 97–115 ms（两种构建都出现，首像素记录同时缩短到 0.2–0.4 s），是测量口径上的现象，与构建无关，仍远低于 1 s 阈值。
- 主线程：本次 `main_js_p50` 1.10–1.28 ms，第 6 轮构建 1.27–1.34 ms。
- 稳态吞吐（不限帧、同一场景深圳 40 架、默认环境）：第 6 轮构建 22.78、22.82 fps，本次 22.78、22.90 fps；200 架 90 s：24.54–25.09 对 25.27–25.32 fps。（用 synthcity 加 `fakeEnv*` 参数对比时本次构建慢约 4 %，原因是旧构建不认识这些 FX-UBO 新增的 FakeSource 参数，环境状态与 pass 数不同：7 对 8 个 pass、15 对 17 个 draw，不可比，已弃用。）

结论：没有明显退化。正式的 3 次中位应在下一轮验收按 ADR-033 协议复测。

## 6 测试与 lint

| 项 | 结果 |
|---|---|
| `make lint` | 通过（ruff、oxlint type-aware、lint-m06、文档扫描等全部目标；harness 注册表 109 个用例校验通过） |
| Vitest unit（全部） | 122 个文件通过（1 个按条件跳过），972 例 |
| Vitest browser（全部） | 17 个文件、56 例通过（含 `ubo.browser.test.ts` 8 例） |
| harness 功能用例（最终构建，`--runs 1`） | `skeleton` PASS（2 passed）；`warmup` PASS（揭开后 programs 增量 0，操作后 1 s 内最大间隔 133.3 ms：天气 100、选中 133.3、跟随 100、近景 50、拾取 50；第 6 轮中位 133）；`layout` PASS（RT 重分配 0，> 50 ms 相对基线 −4.66 个百分点）；`m06.feat-matrix.S`、`m06.feat-matrix.B`、`feat-matrix.A`、`m06.warmup` PASS；`m06.gpu-limits` PASS（10/10）；`m06.layout` FAIL（P4，第 6 轮测试构建同样失败 2/2） |
| `gpu-limits.spec.ts` 全矩阵 | 26/26（4.1） |

## 7 修改清单

| 路径 | 内容 |
|---|---|
| `apps/web/src/viewport/anetNodesHandler.ts` | 修复 6：程序条目记下 updateBefore 节点；`renderStart` 运行 FRAME 类（无分配的 `Map.forEach`），`onBeforeRender` 与 `onUpdateProgram` 运行被绘制程序的全部 updateBefore 节点；文件头说明与依赖的 three 内部结构 |
| `apps/web/tests/m06/ubo.browser.test.ts` | `emulate` 可选模拟 `MAX_UNIFORM_BLOCK_SIZE`；新增 2 例（stock 复现、修复后实例移动） |
| `apps/web/perf/m06/gpu-limits.spec.ts` | 上限含 block 大小（最低组 16 384），链接时检查 block 大小；`GPU_LIMITS_SETS` / `GPU_LIMITS_BUILDS` 与默认生产构建（经 UI）；不模拟时读取设备自身上限并照常强制；低模 GPU 回读 2 例；演示与默认构建的着色改经图层面板并断言按下（P2） |
| `apps/web/perf/m06/cases.mjs` | `m06.gpu-limits` 的 `acIds` 增加 M06-AC-059 |
| `apps/web/src/viewport/README.md` | updateBefore 一条 |
| `docs/03-设计基线与决策记录.md` | ADR-087（附录 E）与 §7.0 索引 |
| `docs/modules/M06-Web视口与渲染后端PRD.md` | 新增 M06-FR-086、M06-AC-059、§6.3 修复 6；修订 M06-AC-058（16 384 与 block 大小） |
| `docs/18-性能与测试方案.md` | §8.5 的 `m06.gpu-limits` 一条；§10 增加 C1 与规范最低值的逐项差别和审计结论 |
| `docs/README.md` | ADR 范围更新到 ADR-087 |

M05、M07 没有实例化对象，也不依赖 updateBefore 节点，PRD 不变。

## 8 复现

```bash
# 构建（测试、演示、默认）
cd apps/web
VITE_AWR_TEST_SWITCHES=1 npx vite build --outDir ../../.cache/verify-ubo/test --emptyOutDir
VITE_AWR_DEMO=public npx vite build --outDir ../../.cache/verify-ubo/demo --emptyOutDir
npx vite build --outDir ../../.cache/verify-ubo/default --emptyOutDir
# 全矩阵（26 例，约 1.1 h）
V=$PWD/../../.cache/verify-ubo
GPU_LIMITS_SETS=all GPU_LIMITS_BUILDS=test,default,demo GPU_LIMITS_TEST_DIST=$V/test GPU_LIMITS_DEFAULT_DIST=$V/default \
  GPU_LIMITS_DEMO_DIST=$V/demo npx playwright test -c perf/m06/playwright.m06.config.ts perf/m06/gpu-limits.spec.ts
# 缺省 10 例（harness）
GPU_LIMITS_TEST_DIST=$V/test GPU_LIMITS_DEMO_DIST=$V/demo node perf/harness/run.mjs --case m06.gpu-limits --runs 1
# 性能单次
AWR_WEB_DIST=$V/default AWR_PERF_DIST=$V/default node perf/harness/run.mjs --case flight60.shenzhen.full --gate G4 --runs 1
```

加压、天气、实时后端、资源审计、GPU 回读与对照脚本留在 scratchpad 的 `verify/` 目录（`stress.mjs`、`weather.mjs`、`live.sh` + `live.mjs`、`audit.mjs`、`instgpu.mjs`、`glcalls2.mjs`、`drvmsg2.mjs`），不入库。证据：`.cache/verify-ubo/`（构建、截图、日志、加压与实时后端的 JSON）；性能运行 `runs/perf/p20261004-193548-verify-ubo/`、`runs/perf/p20261004-194017-ab-*`、`runs/perf/p20261004-194851-ab-*`、`runs/perf/p20261004-200630-nyab-*`、`runs/perf/p20261004-213357-layab-*`；功能用例 `runs/perf/<func-rid>/`（`.cache/verify-ubo/logs/func-rid`）。

## 9 遗留

1. 部署：公开演示站须用本次代码重新打包部署后才生效；本工作包没有连接服务器。
2. 真机核对：本机没有 GPU，16 384 的 `MAX_UNIFORM_BLOCK_SIZE` 与 24/12/12/24 一样是模拟值，部署后建议在 Mac 上按 1.1 核对一次。
3. P4 `m06.layout` Tier S 的帧占比断言为既有失败，需要 M06 / web-ui 另行处理。
4. 正式性能结论按 ADR-033 在下一轮验收以 3 次中位复测。
