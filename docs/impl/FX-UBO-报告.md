# FX-UBO 真实 GPU 的 uniform block 上限：根因、修复与回归报告

| 项 | 内容 |
|---|---|
| 工作包 | FX-UBO（区域：前端渲染后端，M06 为主，涉及 M05 点材质、M07 降水材质、M11 FakeSource 测试参数、R3F 宿主） |
| 日期 | 2026-10-04 |
| 起因 | 公开演示站 `https://drone4d.agentnetwork.org.cn/world/synthcity` 在真实 GPU 浏览器（macOS Chrome，ANGLE Metal）上报错：THREE.Clock 弃用告警 1 次、"THREE.AttributeNode: Vertex attribute "position" not found on geometry." 多次、"THREE.WebGLRenderer: Maximum number of simultaneously usable uniforms groups reached." 29 次、"GL_INVALID_OPERATION: glDrawArrays / glDrawElements / glDrawArraysInstanced: It is undefined behaviour to use a uniform buffer that is too small." 数百次，随后 "too many errors" |
| 依据 | ADR-007（Tier B/S 的 AnetNodesHandler）、ADR-064、ADR-071；M06 PRD §6.2、§6.3、§6.20；g01 §6.2；three r186 `examples/jsm/tsl/WebGLNodesHandler.js`、`build/three.module.js` 的 `WebGLUniformsGroups`、`WebGLState.uniformBlockBinding`、`setProgram`，`build/three.webgpu.js` 的 `GLSLNodeBuilder.getUniforms` / `getUniformFromNode`、`NodeMaterial.setupPosition`、`AttributeNode`、`OverrideContextNode`；R3F 9.8.1 `createStore` |
| 环境 | 8 核 CPU、无 GPU；Chrome for Testing 151（SwiftShader，C1）；Node 22.12；本机私有构建（`.cache/gpu-limits/{test,demo}`，scratchpad 下的复现脚本），未使用共享 `apps/web/dist` |
| 约束执行 | 未安装或升级任何依赖；未执行 git 写操作（本目录不是 git 仓库）；无 emoji；新 ADR 为 ADR-086 并登记 §7.0；M06 / M05 / M07 PRD 与 AWR-18 同步；`make lint` 通过；修复前的对照构建放在 `.cache/fxubo-orig`（只关闭修复 5，统计完成后已删除） |
| 结论 | 根因是 three r186 经典路径为每个节点 uniform 组终身分配一个全局 UBO 绑定点：synthcity 整景 Tier S 需要 60 个、Tier B 需要 68 个，WebGL2 最低保证与大量真实 GPU 只有 24 个，SwiftShader 报 72 个，所以全部用例都没有发现。改为 `AnetNodesHandler` 修复 5 逐 draw 绑定后，八种组合（测试构建与演示构建 × Tier S/B × 上限 24/12/12/24 与 36/14/14/28）下使用中的绑定点都是 3，单个程序最多顶点 3 / 片元 2 个 block，WebGL 错误与 three 告警、错误均为 0，全部功能正常；另两类告警已消除。新增回归 `perf/m06/gpu-limits.spec.ts`（harness `m06.gpu-limits`）8 例通过，浏览器与单元测试通过 |

## 1 根因

### 1.1 复现

- 线上站点：`limits-repro.mjs`（scratchpad）以 `addInitScript` 覆盖 `WebGL2RenderingContext.prototype.getParameter`，把 `MAX_UNIFORM_BUFFER_BINDINGS`、`MAX_VERTEX_UNIFORM_BLOCKS`、`MAX_FRAGMENT_UNIFORM_BLOCKS` 压到 24 / 12 / 12，得到与用户完全相同的报错。
- 本地：测试构建 + 本地静态服务（FakeSource），Tier S、24/12/12：40 s 内 36 次 "Maximum number…"、GL 告警 256 条后 "too many errors"；不模拟上限时（SwiftShader 72）无任何 GL 错误，只有 THREE.Clock 1 次与 AttributeNode 4 次。

### 1.2 three r186 的机制

1. `WebGLNodesHandler.build` 用 GLSL 节点构建器生成程序。节点 uniform 按组名进入 `layout( std140 ) uniform <组名> { … }` 块：TSL 缺省组 `object`（对象矩阵、材质 uniform）、`render`（相机矩阵、`EnvUniforms` 等 renderGroup），每个 `buffer` 节点（低模与 P600 的实例矩阵数组）各一个块 `NodeBuffer_<id>`。
2. `WebGLRenderer.setProgram` 在程序变化时调用 `onUpdateProgram`，handler 的 `collectUniformsGroups` 为**每个（材质，程序）对**新建 `UniformsGroup`（每个组名一个，每个 buffer 一个），并赋给 `material.uniformsGroups`。
3. `setProgram` 每次 draw 对这些组调用 `WebGLUniformsGroups.update` / `bind`：组第一次出现时 `allocateBindingPointIndex` 从 0 起找空闲的**全局**绑定点并 `bindBufferBase` 一次，此后该绑定点一直属于这个组；用尽时报 "Maximum number of simultaneously usable uniforms groups reached." 并返回 0。于是第 25 个起的组都与第 0 个组共用绑定点 0（每个程序的 block 经 `uniformBlockBinding` 指向 0），draw 时绑定点 0 上是另一个组的缓冲，大小不符，ANGLE 以 GL_INVALID_OPERATION 拒绝 draw（"uniform buffer that is too small"）。
4. 回收：材质 dispose 时 handler 的 `onDisposeMaterialCallback` dispose 该材质全部组，`onUniformsGroupsDispose` 归还绑定点。应用的图层材质在会话内不 dispose（只有启动自检与微基准的 3 个组用完即还），所以回收不能缓解问题。
5. 另一个隐患：`WebGLState.uniformBlockBinding` 每个程序只缓存一个 block 下标；两个材质共用同一个只有一个 block 的程序时，第二个材质不会重新绑定，读到第一个材质的缓冲。节点程序至少两个 block，D1 中没有触发，修复后也不再依赖这段代码。

### 1.3 哪些材质生成了多少个组（修复前构建，整景操作序列结束时）

每个材质只为一个目标编译一个程序（Tier B 的点云只画进 cloudRT，Tier S 画到屏幕），所以"按 pass 重复"在本景中没有出现；组数 = 材质数 × 每个程序的组数。

| 图层 | 材质（对象） | 每程序的组 | Tier S | Tier B |
|---|---|---|---|---|
| 点云（M05） | PointCloudLayer、PointCloudPick（拾取 pass） | object、render | 4 | 4 |
| 环境（M07） | EnvDust、EnvSnow（点）、EnvRain、EnvRainFade、EnvArrows；Tier B 另有 EnvStreamlines ×2 | object、render | 10 | 14 |
| 地面天空 | SkyQuad、GroundGrid | object、render | 4 | 4 |
| 禁飞区 | ZoneWalls、ZoneTopSolid、ZoneTopDashed、ZoneEdges | object、render | 8 | 8 |
| 无人机 | DroneLowPoly.p600、DroneLowPoly.x500、DroneHero | object、render、NodeBuffer | 9 | 9 |
| 无人机 | DroneMarkers、DroneHeroHull | object、render | 4 | 4 |
| 轨迹 | TrailHalo、TrailSelected、TrailFocus | object、render | 6 | 6 |
| 符号 | GlyphLayer | object、render | 2 | 2 |
| 视锥 | FrustumEdges、FrustumFill | object、render | 4 | 4 |
| 任务 | MissionAreas、MissionLines、MissionPlanned | object、render | 6 | 6 |
| P2 合成（M06 / M05） | 内置合成四边形、EDL 合成四边形 | object、render | — | 4 |
| 调试 | Line（从未绘制） | — | 0 | 0 |
| 合计 | 28 / 31 个节点材质、27 / 31 个程序 | | **57** | **65** |

加上启动自检与微基准先占后还的 3 个组，three 的绑定点分配峰值为 Tier S 60、Tier B 68。单个程序的 block 数：顶点 2–3、片元 2、合计至多 5（链接后用 `UNIFORM_BLOCK_REFERENCED_BY_*` 实测相同），远低于每阶段 12。问题完全在全局绑定点总数随材质数线性增长；天气预设、选中、Third/FPV、P600、图层开关、拾取与着色切换都不新增材质或程序（这些材质在 shader zoo 中已全部预热），所以组数在揭开后不变。

### 1.4 为什么测试没有发现

C1（SwiftShader）报告 `MAX_UNIFORM_BUFFER_BINDINGS` 72、`MAX_VERTEX_UNIFORM_BLOCKS` 与 `MAX_FRAGMENT_UNIFORM_BLOCKS` 各 14、`MAX_COMBINED_UNIFORM_BLOCKS` 60。Tier B 的 68 只比上限少 4 个；WebGL2 规范最低值 24 / 12 / 12 / 24 正是大量真实 GPU 的取值。所有用例都在 C1 上运行，没有任何用例模拟真实 GPU 的上限。

### 1.5 两类告警

| 告警 | 来源 | 说明 |
|---|---|---|
| THREE.Clock 弃用（1 次） | R3F 9.8.1 `createStore` 中 `clock: new THREE.Clock()` | 应用代码、`engine/time` 与帧循环都不使用 THREE.Clock；R3F 在 frameloop="never" 下由 `advance(tS)` 直接写 `clock.elapsedTime` 与 `oldTime` |
| AttributeNode "position" not found（每个程序 1 次，4 次） | 四个 `GLPointsNodeMaterial`：PointCloudLayer、PointCloudPick、EnvDust、EnvSnow | 无属性几何（按 `vertexIndex` 寻址），位置来自 `positionNode`；`NodeMaterial.setupPosition` 仍执行 `positionLocal.assign(positionNode)`，而 `positionLocal` 是以 `attribute('position')` 初始化的 varying，three 告警并生成 `positionLocal = vec3( 0.0, 0.0, 0.0 )`。使用 vertexNode 的网格材质（降水条、标记、符号、轨迹）不引用 `positionGeometry`，不告警 |

## 2 修复

| 路径 | 内容 |
|---|---|
| `apps/web/src/viewport/backend/uboBinder.ts`（新增） | `UboBinder`：每个节点组一个 GL 缓冲（大小取 three std140 计算值与程序报告的 `UNIFORM_BLOCK_DATA_SIZE` 的较大者）；每个程序的第 i 个 block 一次性 `uniformBlockBinding` 到绑定点 i；`bind(groups, program)` 按 three 的值类型、std140 偏移与变化判定（数值比较、对象克隆后 `equals`、类型数组总是上传）上传变化的值，再 `bindBufferBase`；组 dispose 时删除缓冲。`readUboLimits`、`blockNames` / `programBlocks`（从生成的 GLSL 统计每阶段、合计、去重 block 数）、`overBudget`；统计 `UboStats`（上限、使用中的绑定点、单程序最大 block 数、存活组、创建与删除、超限、被裁剪图层） |
| `apps/web/src/viewport/anetNodesHandler.ts` | 修复 5：`onUpdateProgram` 在 stock 逻辑后把 `material.uniformsGroups` 置为空数组并为新程序绑定；包装 `onBeforeRenderCallback`，在 stock 逻辑（节点更新）之后为当前程序绑定；`renderStart` 在任何 draw 之前按 `setProgram` 的同一条件把相机切换到反向深度投影（上传提前后第一帧的投影矩阵与 stock 一致）；`build` 之后按生成的 GLSL 检查设备预算，超限时换成不绘制的空程序（输出在 discard 前写入，否则 ANGLE 报 "Active draw buffers with missing fragment shader outputs"）并经 `onViolation` 报告；`AnetNodesHandler.of(renderer)`、`limitsOverride`（测试）、`uboStats` |
| `apps/web/src/viewport/backend/webgl2.ts` | `__perf.gpu.ubo` 指向 handler 的统计；设备上限低于 WebGL2 最低值时一次 M06-E017；超限程序在帧后（以及 shader zoo 之后）按 `layerOf` 找到图层：非必要图层 `setVisible(false)` 并记入 `pruned`，点云、地面天空、无人机（`UBO_ESSENTIAL`）不隐藏；每图层一次 M06-E017 |
| `apps/web/src/viewport/backend/guards.ts` | 编码规范第 12 条：图层注册时非节点材质带 `uniformsGroups` 即 M06-E017（dev / test 构建） |
| `apps/web/src/engine/perf/probe.ts`、`index.ts` | `gpu.ubo`（`UboProbe` 类型） |
| `apps/web/src/viewport/glPointsNodeMaterial.ts` | `contextNode = overrideNode(positionGeometry, …)`：几何没有 `position` 属性时把 `positionGeometry` 解析为常量 `vec3(0)`（生成的 GLSL 与原来相同，有属性的几何照旧读属性；不加假属性，规则 10） |
| `apps/web/src/viewport/r3fClock.ts`、`r3fThree.ts`（新增），`apps/web/vite.config.ts` | `R3fClock`：字段与方法同 THREE.Clock 的 `performance.now()` 秒表，不打印弃用告警；`r3fThree.ts` 原样再导出 three 并以它替换 `Clock`；`awr-r3f-clock` 插件只把 `@react-three/fiber` 自身模块对 `three` 的导入解析到该文件 |
| `apps/web/src/net/rt/FakeSource.ts`、`client.ts` | 测试参数：`fakeWorld`（合成机群所在世界，缺省 shenzhen）、`fakeEnvPeriodS`、`fakeEnvPresets`（逗号列表或 `all`）、`fakeEnvStep=1`（阶跃关键帧）、`fakeEnvCycles`（轮换若干遍后回到第一个预设并保持）；缺省行为不变 |
| `apps/web/src/viewport/README.md` | 规则 12、修复 5 与无属性点的说明 |
| 文档 | ADR-086（附录 E）与 §7.0 索引；M06 PRD 新增 FR-085、AC-058、§6.3 修复 3–5 说明与规则 12、§6.20 M06-E017，修订 FR-016；M05 PRD FR-028 与实现指引；M07 PRD FR-040、雪与沙尘一节、EnvStore 接口注释；AWR-18 §8.5 登记 spec、§9.3 登记 `gpu.ubo`、§10 增加"SwiftShader 上限偏宽，真实 GPU 上限必须模拟覆盖"；AWR-10 TSL 单源一条与风险 R7 |

为什么不用其他手段（详见 ADR-086 备选）：节点组的 std140 布局随程序而异，`render` 组无法跨程序共用一个缓冲，即使共用，28–31 个材质仍各剩一个 `object` 组，超过 24；把节点 uniform 降级为普通 uniform 时，WebGLRenderer 只在材质、程序或相机切换时上传，共用材质的连续对象会读到旧值；`WebGLUniformsGroups` 是渲染器闭包私有对象，不分叉 three 就无法在其中做 LRU 或回收；在 GL draw 调用处上传会与 SwiftShader 顶点输入槽重置（ADR-071，同样覆盖 draw 调用）冲突。

逐 draw 绑定后，每次 draw 多出每组一次变化比较与一次 `bindBufferBase`（D1 每帧约 30 个 draw、每个 2–3 组）；上传量与 stock 相同（stock 每次 draw 递增 `info.render.frame`，同样逐 draw 比较与上传）。r3f 分块因再导出 three 命名空间增加约 13.7 KB（gzip 约 4.7 KB），落地页不加载该分块。

## 3 各上限下的绑定点与 block 统计

整景操作序列：揭开、12 个天气预设（3 s 阶跃）、选中、Third、FPV、P600 近景、8 个图层逐一关闭再打开、点云拾取、5 种着色。"修复前"为只关闭修复 5 的对照构建（测试构建）；"修复后"为 `gpu-limits.spec.ts` 的结果（测试构建与演示构建数值相同，下表各取一列）。GL 告警在 Chrome 中最多上报 256 条，之后只有一条 "too many errors"。

| 档位 | 上限（绑定点 / 顶点 / 片元 / 合计） | 修复前：three 分配的绑定点 | 修复前："Maximum number…" | 修复前：GL 告警 | 修复后：使用中的绑定点 | 修复后：单程序 block（顶点 / 片元 / 合计 / 去重） | 修复后：存活组 | 修复后：WebGL 错误 / three 告警 |
|---|---|---|---|---|---|---|---|---|
| Tier S | 72 / 14 / 14 / 60（SwiftShader，不模拟） | 60 | 0 | 0 | 3 | 3 / 2 / 5 / 3 | 57 | 0 / 0 |
| Tier S | 36 / 14 / 14 / 28 | 36（溢出 24 个组） | 24 | 257（截断） | 3 | 3 / 2 / 5 / 3 | 57 | 0 / 0 |
| Tier S | 24 / 12 / 12 / 24 | 24（溢出 36 个组） | 36 | 257（截断） | 3 | 3 / 2 / 5 / 3 | 57 | 0 / 0 |
| Tier B | 72 / 14 / 14 / 60（SwiftShader，不模拟） | 68 | 0 | 0 | 3 | 3 / 2 / 5 / 3 | 65 | 0 / 0 |
| Tier B | 36 / 14 / 14 / 28 | 36（溢出 32 个组） | 32 | 257（截断） | 3 | 3 / 2 / 5 / 3 | 65 | 0 / 0 |
| Tier B | 24 / 12 / 12 / 24 | 24（溢出 44 个组） | 44 | 257（截断） | 3 | 3 / 2 / 5 / 3 | 65 | 0 / 0 |

修复后的绑定点数与上限无关，只取决于单个程序的最大 block 数（3），对 WebGL2 最低上限还有 21 个绑定点、每阶段 9–10 个 block 的余量。每个分配失败的组报一次错，用户看到的 29 次即线上页面约需 53 个组（24 + 29；线上为实时后端与 S0 剧本 7 架，图层与预设状态和本地 FakeSource 不同，组数略少于本地整景）。

## 4 回归结果

### 4.1 `perf/m06/gpu-limits.spec.ts`（新增，harness `m06.gpu-limits`，M06-AC-058）

- 上限模拟（init script，严格）：`getParameter` 报告目标上限；超出绑定点的 `bindBufferBase`、`bindBufferRange`、`uniformBlockBinding` 不执行并记 console error（等同真实驱动的 GL_INVALID_VALUE）；每个程序首次使用时按 `UNIFORM_BLOCK_REFERENCED_BY_*` 统计，超出每阶段或合计上限即记错误（等同链接失败）。演示构建 Tier B 另把 `UNMASKED_RENDERER_WEBGL` 伪装为 "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, …)"（按名称分为 iGPU、Tier B，与报告用户的环境相同）。
- 数据：FakeSource，`fakeWorld=synthcity`、6 架地面机、`fakeEnvPresets=all&fakeEnvStep=1&fakeEnvPeriodS=3`，轮换 2 遍（演示构建 Tier B 为 3 遍，因其在 SwiftShader 上约 1 fps、揭开更晚）后保持晴天，像素检查不被换天气打断。
- 操作：测试构建用测试钩子（`__vp`、`__pc`、`__env`）；演示构建没有钩子，全部经 UI：机群栏选中、数字键 3 / 4 / 1、Third 下滚轮推近到 5 m 出现 P600、左栏图层开关（标签点击）、命令面板着色、视口点击拾取，预设由环境面板的按下项记录。
- 断言：WebGL 错误 0（GL_INVALID_*、uniform buffer too small、Maximum number…、模拟器错误）、three 告警与错误 0（含 THREE.Clock、AttributeNode）、其他 console error 0（静态测试服务器的 `/api` 404 除外）、pageerror 0；`__perf.gpu.ubo.limits` 等于模拟值（证明上限到达页面）、绑定点与单程序 block 数不超限、超限 0、无裁剪；12 个预设全部出现；点云与无人机切换显隐的像素差高于帧间噪声（三帧夹逼，噪声 < 1% 才计数）；揭开后 `gpu.programs` 不增加；结束时 `gl.getError()` 为 0。
- 结果（最终一轮，8/8 通过，19.2 min）：

| 构建 | 档位 | 上限 | 绑定点 | 单程序 block 顶点 / 片元 / 合计 / 去重（链接后实测顶点 / 片元，链接的 GL 程序数，含自检与槽位重置程序） | 存活组 | programs（揭开 → 结束） | 点云像素差 / 噪声 | 无人机像素差 / 噪声 | 预设 | WebGL / three / error | 用时 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 测试 | S | 24/12/12/24 | 3 | 3 / 2 / 5 / 3（3 / 2，30 个程序） | 57 | 27 → 27 | 130231 / 0 | 2130 / 0 | 12 | 0 / 0 / 0 | 1.6m |
| 测试 | S | 36/14/14/28 | 3 | 3 / 2 / 5 / 3（3 / 2，30 个程序） | 57 | 27 → 27 | 130231 / 0 | 2130 / 0 | 12 | 0 / 0 / 0 | 1.6m |
| 测试 | B | 24/12/12/24 | 3 | 3 / 2 / 5 / 3（3 / 2，34 个程序） | 65 | 31 → 31 | 130157 / 0 | 2130 / 0 | 12 | 0 / 0 / 0 | 1.8m |
| 测试 | B | 36/14/14/28 | 3 | 3 / 2 / 5 / 3（3 / 2，34 个程序） | 65 | 31 → 31 | 130157 / 0 | 2130 / 0 | 12 | 0 / 0 / 0 | 1.8m |
| 演示 | S | 24/12/12/24 | 3 | 3 / 2 / 5 / 3（3 / 2，30 个程序） | 57 | 27 → 27 | 150333 / 0 | 1318 / 0 | 12 | 0 / 0 / 0 | 1.8m |
| 演示 | S | 36/14/14/28 | 3 | 3 / 2 / 5 / 3（3 / 2，30 个程序） | 57 | 27 → 27 | 150333 / 0 | 1260 / 0 | 12 | 0 / 0 / 0 | 1.8m |
| 演示 | B | 24/12/12/24 | 3 | 3 / 2 / 5 / 3（3 / 2，33 个程序） | 65 | 31 → 31 | 153314 / 0 | 941 / 0 | 12 | 0 / 0 / 0 | 4.3m |
| 演示 | B | 36/14/14/28 | 3 | 3 / 2 / 5 / 3（3 / 2，33 个程序） | 65 | 31 → 31 | 153314 / 0 | 941 / 0 | 12 | 0 / 0 / 0 | 4.2m |

### 4.2 其他测试

| 测试 | 结果 |
|---|---|
| `tests/m06/ubo.browser.test.ts`（新增，真实 WebGLRenderer，模拟 24/12/12/24） | 6/6：stock handler 复现 "Maximum number…"；40 个材质各自的 uniform 颜色逐像素正确（2 个绑定点），改值后再次正确；同一材质 5 个对象各画在自己的位置；dispose 10 个材质后缓冲删除；顶点 block 上限设为 1 时程序不绘制、报告 "vertex blocks 2 > 1"、无 GL 错误；反向深度新相机第一帧正确；无属性 GLPointsNodeMaterial 无 AttributeNode 告警且点画在 positionNode 处 |
| `tests/m06/uboBinder.test.ts`（新增，Node） | 10/10：block 名称与预算判定；std140 偏移与 three `prepareUniformsGroup` 的算法逐项一致（float、vec3、vec2、mat3、mat4、Float32Array、vec4 混排）；绑定与上传调用序列（只上传变化的值、每 draw 重新绑定、dispose 删除）；R3fClock 与 THREE.Clock 的 getDelta / getElapsedTime / stop 逐项一致且不告警；`r3fThree` 再导出与插件解析范围（只有 R3F 对 `three` 的导入）；FakeSource 参数映射与"轮换 2 遍后保持" |
| `tests/m06/constraints.test.ts` | 新增规则 12 用例，19/19（与 uboBinder 同跑） |
| 功能矩阵（`tests/m06/backend.browser.test.ts`，Tier S/B 28 项 + PointPool） | 通过。修正上传时机之前，`depthTex_EDL_cloud_fsq_reversedZ` 与 `mask_alpha_and_fsq_depthWrite` 在新相机首帧失败（反向深度投影在 `setProgram` 内才切换），即 ADR-086 决策 2 |
| Vitest browser（全部，17 个文件） | 54/54 通过；输出中没有 THREE.Clock 与 AttributeNode 告警 |
| Vitest unit（全部） | 122 个文件通过（1 个按条件跳过），972 例 |
| 既有 Playwright（私有测试构建）：`perf/m06/{smoke,drone-tiers,feat-matrix,warmup}.spec.ts`；`perf/m05/{modes,pick}.spec.ts`、`perf/m07/{env-gpu,env-visual}.spec.ts` | 9/9 与 6/6 通过（含设备丢失后重建视口、Tier A 功能矩阵、揭开后不编译、着色切换不新增程序、点云拾取；`perf/m05/visual-tierb.spec.ts` 为 `M05_VISUAL=1` 的人工评审用例，按设计跳过） |
| `make lint` | 通过（ruff、oxlint type-aware、lint-m06 等全部目标；harness 注册表 109 个用例校验通过） |

## 5 说明与遗留

1. 公开演示站需要用本次代码重新打包部署（`tools/deploy/emax/build-local.sh` → 服务器 `install.sh`）后才生效；本工作包没有连接服务器。
2. `awr-r3f-clock` 插件作用于生产构建（`vite build`，测试构建与演示构建都已验证 r3f 分块使用 `R3fClock`）；`vite dev` 与 Vitest 浏览器模式的依赖预构建不经过插件，开发服务器中仍会出现一次 THREE.Clock 告警（构建产物中没有）。
3. 超限程序的空程序与图层裁剪在符合规范的 WebGL2 上不会触发（D1 的程序最多 3 + 2 个 block）；它是新增材质或 buffer 节点超出设备能力时的保护，并以 M06-E017 与 `__perf.gpu.ubo` 报告，不会静默失败。`ubo.browser.test.ts` 以人为降低的上限覆盖该路径。
4. 新增材质、uniform 组或 buffer 节点时须跑 `m06.gpu-limits`（约 20 min，8 例；AWR-18 §10）。三处 three 内部依赖（`programCache` 条目的 `uniformsGroups`、`onBeforeRenderCallback` 实例属性、`setProgram` 中 `onUpdateProgram` 的位置与反向深度切换条件）由该 spec、`ubo.browser.test.ts` 与功能矩阵守护。
5. 复现与统计脚本留在 scratchpad（`limits-repro.mjs`、`ubo/scenario.mjs`、`ubo/explore.mjs`），不入库。
