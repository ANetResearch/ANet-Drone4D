# R14 研究笔记：react-three-fiber / drei（3D 视口架构：R3F 宿主 + 命令式引擎）

> 研究单元：r14 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §3、§4、§9–§16、§21、§28、§34、§36–§40、§43
>
> 仓库快照（refs 下均为 master 的 shallow clone；下文路径相对各仓库根目录）：
> - `refs/web3d/react-three-fiber` @ `db32547`（2026-09-27，32582 stars）。master 即稳定线 **`@react-three/fiber@9.8.1`**，npm latest 发布于 2026-09-24。
> - `refs/web3d/drei` @ `66b9bda`（2026-09-25，9900 stars）。master 即稳定线 **`@react-three/drei@10.7.9`**，npm latest 发布于 2026-09-25。
>
> 补充 clone（只读，放在 `.cache/research/r14/`，原因：WebGPU 原生支持只存在于 alpha 分支，master 上没有）：
> - `r3f-v10`：R3F `v10` 分支 @ `14007b4`（2026-09-26），即 **`10.0.0-alpha.5`**（npm alpha tag，2026-09-08）。另有 canary 版本几乎每天发布。
> - `drei-v11`：drei `v11-working` 分支 @ `fbd5b12`（2026-09-27），即 **`11.0.0-alpha.7`**（2026-09-05）。
>
> 本机实测产物在 `.cache/research/r14/`：
> - `bench/src/cpu.jsx`：CPU 框架开销微基准，用 stub renderer 加 `flushSync`
> - `bench/src/{vanilla.js, r3f_hybrid.jsx, r3f_decl.jsx}`：整帧基准。机器负载过高，结果仅作参考
> - `bench/src/smoke.jsx` 与 `bench10/src/smoke10.jsx`：WebGPU 兼容性冒烟测试，覆盖 3 种后端和 23 个 drei 组件
> - `sizes/`：bundle 体积对比
> - 原始结果：`bench/cpu_run{1,2}.json`、`bench/smoke_results.jsonl`
>
> 环境：Chromium（playwright chromium-1234），SwiftShader 软件渲染。WebGPU 需要加 `--enable-unsafe-webgpu --use-webgpu-adapter=swiftshader` 才可用，但**headless 截图拿不到 WebGPU canvas 的画面**（r11 单元也遇到了同样问题），所以像素检查只在 WebGL 和 WebGL2 fallback 两条路径上做。测试期间机器 load average 为 14–16（8 核，其他研究单元在并行跑），**绝对耗时有 1.5–2 倍左右的膨胀，应以比值为准**。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **react-three-fiber** v9.8.1（稳定） | three.js 的 React reconciler：`<Canvas>`/`createRoot`、帧循环（`useFrame`/`invalidate`/`advance`）、zustand root store、指针事件系统、`performance.regress` 自适应钩子，支持 async `gl` 工厂接入 WebGPURenderer | **adopt**，作为 3D 视口的场景宿主和组合层，只负责低频、结构性、可编辑的对象。热路径（点云 LOD、遥测、粒子）**不走 React** | V0.1 起 | 5/5 |
| **react-three-fiber** v10.0.0-alpha.5 | 原生 WebGPU：入口拆分（`/webgpu`、`/legacy`、`/extension`），基于 `@pmndrs/scheduler` 的**相位调度**（phase/before/after/fps/drop），多 canvas 共享同一个 WebGPURenderer，`@react-three/tsl` hooks，`setRenderOverride`，`onFramed`/`onOccluded` 可见性事件 | **reference 加迁移目标**。适配层按 v10 语义设计（相位、render override），等 v10 stable 后切换 | V0.3–V0.4 评估，稳定后迁移 | 4/5（alpha 扣分） |
| **drei** v10.7.9（稳定） | R3F 组件库，约 120 个组件 | **选择性 adopt**：CameraControls、GizmoHelper（要设 renderPriority）、Html（只用于选中对象）、Detailed、meshBounds、Bvh、PerformanceMonitor、AdaptiveDpr/AdaptiveEvents、StatsGl（仅开发期）、View（FPV 画中画）。**reference**：Instances、Trail、Line、Clouds。**skip**：`Points`/`PointMaterial`（每帧整块重传，不兼容 WebGPU）、Sky/Stars/Sparkles/Grid/Outlines/Text（GLSL，在 WebGPURenderer 下失效或崩溃）、Stats（DOM 样式与设计体系冲突） | V0.1 起 | 4/5 |
| **drei** v11.0.0-alpha.7 | 按渲染器拆分成 `core`/`legacy`/`webgpu`/`external`/`experimental`，`component-status.json` 追踪每个组件的 WebGPU 移植状态（144 个组件：107 个 agnostic，27 个 implemented，4 个 todo，6 个 wont-port），新增 three Inspector 集成 | **迁移目标**。实测它的 `/webgpu` 入口下 14 个组件在 WebGPU 与 WebGL2 两个后端都能正常渲染 | 随 R3F v10 一起迁移 | 4/5 |

**关键结论（实现者先读这几条）**

1. **本项目的 3D 视口采用"R3F 宿主 + 命令式引擎"（下文称 B′ 方案），不用纯声明式 R3F，也不用"纯命令式 Three.js + React 只做 UI 壳"（A 方案）。** 具体分工如下：
   - 点云引擎、环境粒子引擎、无人机渲染层、标签层、拾取器都写成**不依赖 React 的命令式类**，放在 `engine/` 包里。
   - R3F 只做三件事：用 `<primitive>` 挂载引擎的根节点；在 `useFrame` 里按相位驱动 `engine.update()`；声明式管理少量可编辑对象（航点、区域、传感器视锥、选中高亮、Gizmo）。
   - 每个适配层不超过 150 行。这样随时可以退回 A 方案，或把引擎搬进 Worker + OffscreenCanvas。
   - 这正是 3DTilesRendererJS 官方 R3F 绑定的写法（`src/r3f/components/TilesRenderer.jsx`，见 §3.5）。
2. **实测依据（§5.3）**，均在 CPU 上测量、使用 stub renderer：
   - 热路径命令式写法下，R3F 与原生 three 每帧开销**无可测差异**（误差 ±30% 以内）。
   - 若把 LOD 节点增删做成 React state（每个节点一个 `<points key>`），单次 LOD tick 的耗时如下：

     | 可见节点数 | 每次增删节点数 | 声明式耗时 | 命令式耗时 | 倍数 |
     |---|---|---|---|---|
     | 200 | 20 | 2–3 ms | 0.02–0.05 ms | 约 40–170× |
     | 2000 | 20 | 15–27 ms | 0.05–0.1 ms | 约 150–500× |
     | 2000 | 100 | **99–154 ms** | 0.2–0.3 ms | 约 350–750× |

   - 遥测若走 React state：500 架无人机每次更新提交 6.5–8.4 ms，20 Hz 下占主线程 13–17%。
   - **结论：点云 LOD 和遥测严禁进入 React state。**
3. **R3F v9 接入 WebGPURenderer 用 async `gl` 工厂**：`gl={async (p) => { const r = new WebGPURenderer({ canvas: p.canvas, forceWebGL }); await r.init(); return r }}`。实测 WebGPU 和 WebGL2 fallback 两条路径都可用。
   - `root.ready` 在工厂返回前处于 pending，`Canvas` 用内部的 `useGate` 挂起、不闪烁（`packages/fiber/src/web/Canvas.tsx`、`core/root.tsx`）。
   - 卸载时会等待 `WebGPURenderer.dispose()` 这个 Promise（three r186 起变为异步）。
   - 但 **drei v10 里依赖 GLSL 的组件在 WebGPURenderer 下全部失效**：Line、Segments、Edges、Grid、Sky、Sparkles、Outlines、Trail 失效；GizmoHelper 因 `gl.capabilities.getMaxAnisotropy` 不存在而**崩溃整个 Canvas**；Text 在 WebGPU 后端报 `drawIndexed` 错误。
4. **WebGPURenderer 下 `THREE.Points` 只有 1 px，两个后端都是如此。** 实测 `PointsMaterial.size=4` 在 WebGLRenderer 下覆盖 31961 个像素，在 WebGPURenderer（WebGL2 后端）下只有 3509 个。要得到更大的点，必须用 `Sprite + PointsNodeMaterial + instancedBufferAttribute` 做实例化精灵（实测恢复到 32251 个像素），代价是每个点 6 个顶点，或者改用 compute 光栅化。**这直接影响点云引擎的渲染路径设计（§3.3，§7）。**
5. **帧序契约**：遥测插值 → 相机控制 → LOD / 环境 / 标签 → 渲染管线 → HUD。
   - v9 用 `useFrame` 的数字 priority 表达：负数先跑；**priority 大于 0 会接管渲染**，因为 `internal.priority` 计数不为 0 时默认 render 会被跳过（`core/store.ts` 的 `subscribe`、`core/loop.ts` 的 `update`）。
   - v10 改用相位加 `before`/`after`。
   - **使用自定义渲染管线（EDL）时，drei 的 GizmoHelper/Hud 必须设 `renderPriority={2}`**，否则 Hud 会在 priority 1 上重复渲染主场景。
6. **`frameloop` 策略**：仿真运行且有无人机可见时用 `always`；暂停、回放暂停或纯浏览时切到 `demand`，由相机交互、瓦片到达、UI 变更触发 `invalidate()`。
   - `invalidate` 在 `useFrame` 里调用时会把待渲染帧数设为 2，累计上限 60 帧（`core/loop.ts`）。
   - **root store 的任何 `set` 都会触发 invalidate**（`core/store.ts` 末尾的 `rootStore.subscribe((state) => invalidate(state))`）。
   - drei 的 PerformanceMonitor 在 demand 模式下测得的 fps 没有意义，必须暂停。
7. **性能自适应**：drei 的 PerformanceMonitor 窗口为 10 次 × 250 ms = 2.5 s，需要 8/10 次采样越界才动作，反应偏慢，只适合做档位粗调。项目需要自写 `PerfGovernor`，逻辑如下：
   - 用帧时 EMA（CPU 与 GPU timer query）驱动点预算、DPR 和粒子数的乘性调节，带滞回和冷却。
   - 再叠加 R3F 自带的 `performance.regress()`：相机运动期间降到 `min=0.5`，200 ms 后恢复。
   - 最终表现为"移动时降质、静止时回升"，与 Potree 的行为一致（§3.4）。
8. **drei 里几个"看起来合用"的组件，用在本项目会成为性能陷阱**：
   - `<Points positions={Float32Array}>` 的 `PointsBuffer` 每帧给 position/color/size 设 `needsUpdate = true`，**整块重传 GPU**（`src/core/Points.tsx`，约第 183 行）。
   - `<Html occlude>` 每帧对整个 scene 做递归 raycast，**会遍历点云每一个点**，而且每个 `<Html>` 都单独创建一个 ReactDOM root。
   - `<Trail>` 每帧 `shiftLeft` 做 O(n) 数组搬移，再调用 `MeshLine.setPoints` 重建几何。
   - R3F 的事件系统用 `raycaster.intersectObject(obj, true)` 做递归检测：**只要在点云祖先节点上挂一个 `onClick`，每次点击就会 CPU 遍历全部点**。
9. **版本策略**：
   - V0.1–V0.2 锁定 R3F 9.8.x + drei 10.7.x + three 0.186.x（React 19.2，R3F 9 要求 `<19.4`）。默认 WebGLRenderer，WebGPURenderer 作为可选开关。
   - 自定义材质采用双实现（GLSL 与 TSL），由引擎内部的 RenderBackend 抽象隔离。
   - V0.3 做环境 compute 时评估 R3F v10 + drei v11。前提：v10 进入 beta 或 rc，且 `/webgpu` 入口 bundle 从目前 alpha.5 的 **587 KB gz** 降下来；alpha.5 把 `three.module.js`、Inspector UI、EXRLoader 全部打进来了，而 v9 最小 Canvas 为 308 KB gz。

---

## 1. 仓库概览

| 项 | react-three-fiber | drei |
|---|---|---|
| Star / 活跃度 | 32582 stars；master 最后提交 2026-09-27；2026 年发布了 9.5 到 9.8.1（共 5 个 minor），以及 v10 的 alpha.0 到 alpha.5；canary 每天发布 | 9900 stars；master 最后提交 2026-09-25（10.7.8 发布于 08-05，10.7.9 于 09-25）；v11 的 alpha.1 到 alpha.7 在 2026 年发布 |
| 稳定线 | `@react-three/fiber@9.8.1`，peer 为 `react >=19 <19.4`、`three >=0.156`；依赖 zustand 5、its-fine、react-use-measure、suspend-react、scheduler 0.28 | `@react-three/drei@10.7.9`，peer 为 `@react-three/fiber ^9`、`three >=0.159`；依赖 camera-controls ^3.1、three-stdlib ^2.35、three-mesh-bvh ^0.8.3、stats-gl、troika-three-text、meshline、maath、tunnel-rat、@mediapipe/tasks-vision、hls.js |
| 下一代 | `10.0.0-alpha.5`（v10 分支，pnpm 加 unbuild 构建）；新增 `@pmndrs/scheduler` 与 `@react-three/tsl`；peer 为 `three >=0.185`、`react >=19.0 <19.3` | `11.0.0-alpha.7`（`v11-working` 分支），peer 为 `@react-three/fiber >=10.0.0-0`、`three >=0.185`；新增 exports：`/core`、`/legacy`、`/webgpu`、`/external`、`/experimental`、`/native` |
| 规模 | v9 核心 `packages/fiber/src` 约 3.8k 行 TS；v10 约 8.4k 行（`core/renderer.tsx` 1204 行，`core/events.ts` 880 行） | 约 120 个组件，每个组件一个文件（v10）；v11 改为每个组件一个目录，含 stories、docs、test |
| 构建 | preconstruct（v9）；本项目直接 `npm i` 使用 | rollup；直接 `npm i` 使用 |
| 许可 | MIT | MIT |
| 与本项目关系 | **3D 视口宿主**（`<Canvas>`、帧循环、事件、Suspense 资源加载） | **组件工具箱**：相机控制、Gizmo、LOD、性能工具、HTML 叠加 |

生态中与本单元相关、2026 年仍活跃的包（npm 查询时间 2026-09-28）：

| 包 | 版本 / 最近发布 | 用途 |
|---|---|---|
| `camera-controls` | 3.1.2 / 2025-12 | drei 的 `CameraControls` 底层：阻尼、过渡、`fitToBox`、`colliderMeshes`、边界 |
| `three-mesh-bvh` | **0.9.15** / 2026-09-09 | 0.9 起新增 **`PointsBVH`**、`LineBVH`、`ObjectBVH`，可用于点云瓦片的 CPU 拾取；drei 10 仍锁定在 ^0.8.3 |
| `3d-tiles-renderer` | 0.5.3 / 2026-09-18 | 官方 R3F 绑定（`src/r3f`），是"命令式引擎加 R3F 外壳"的标杆写法 |
| `@react-three/postprocessing` | 3.1.3 / 2026-09-27 | 只支持 WebGL；WebGPU 下改用 three 的 `PostProcessing`（R3F v10 的 `useRenderPipeline`） |
| `@react-three/offscreen` | 0.0.8 / 2026-08-07 | 把 R3F 放进 Worker + OffscreenCanvas，仍不成熟 |
| `@pmndrs/scheduler` | 0.2.0 / 2026-08-24 | v10 帧调度器，可脱离 Canvas 单独使用 |
| `r3f-perf` | 7.2.3 / 2024-11 | **已停更**，不采用 |

---

## 2. 源码结构与关键模块

### 2.1 R3F v9（`refs/web3d/react-three-fiber/packages/fiber/src`）

| 文件 | 关键符号 | 要点（与本项目相关） |
|---|---|---|
| `web/Canvas.tsx` | `CanvasImpl`、`useGate`、`useMeasure` | 用 `react-use-measure` 测量容器尺寸，拿到后在 layout effect 里调用 `createRoot(canvas).configure({...})`。如果 `root.ready` 是 pending（async `gl` 工厂），就用 `waitFor(root.ready)` 挂起，不向上层抛 Suspense fallback。`eventSource` 与 `eventPrefix` 可以把事件源挂到上层 DOM 容器，这是 UI 叠层（shadcn 面板盖在画布上）的必要配置。`useInsertionEffect` 负责兼容 React 19.2 的 `<Activity>` 隐藏：只有最终移除时才卸载 root |
| `core/root.tsx` | `createRoot`、`configure`、`transitionRoot`、`unmountComponentAtNode`、`disposeRenderer` | root 有 `open → closing → disposed` 三态状态机，可以取消 pending 的卸载。`configure` 同步执行，**只有 async `gl` 工厂会让它变成异步**（`isPromiseLike(gl)`）。`ownsRenderer` 规则：由工厂创建的 renderer 归 R3F 所有并由它 dispose，直接传入的实例归调用者。`disposeRenderer` 会**等待 `WebGPURenderer.dispose()` 的 Promise**（r186 起为异步），若 `hasInitialized() === false` 则跳过 |
| `core/configuration.ts` | `createRenderer`、`applyRootConfiguration`、`GLProps` | `GLProps` 可以是 renderer 实例、同步工厂、async 工厂，或 `WebGLRendererParameters`。默认值：`powerPreference: 'high-performance'`、`antialias: true`、`alpha: true`；`dpr = [1, 2]`；`frameloop = 'always'`；默认相机 `PerspectiveCamera(75, 0, 0.1, 1000)`，z = 5；`flat=false` 时使用 ACESFilmic；`shadows=true` 时设为 PCFSoft（WebGPURenderer 会打印 "PCFSoftShadowMap has been removed" 警告，可忽略）。配置采用增量应用，用 `changed(key)` 做浅比较 |
| `core/loop.ts` | `loop`、`update`、`invalidate`、`advance`、`addEffect`/`addAfterEffect`/`addTail` | 全局只有**一个** rAF 循环，按顺序遍历所有 root。`update()` 的执行顺序：取 `clock.getDelta()`，按 priority 从小到大调用 `useFrame` 订阅者，若 `!state.internal.priority` 再执行 `gl.render(scene, camera)`，最后 `frames--`。`invalidate(state, frames)`：在 useFrame 执行期间调用会把 frames 设为 2（多补一帧）；frames 参数大于 1 时累加，上限 60。没有任何 root 需要渲染时停止 rAF 并执行 `tail` 回调 |
| `core/store.ts` | `createStore`、`RootState`、`Performance`、`internal.subscribe` | `performance = { current: 1, min: 0.5, max: 1, debounce: 200, regress() }`：`regress()` 立即把 current 设为 min，200 ms 后恢复为 max。`subscribe(ref, priority)`：priority 大于 0 时 `internal.priority++`，订阅者重新排序（升序）。**store 的任何变化都会 `invalidate`**；尺寸或 DPR 变化会触发 `gl.setPixelRatio` 与 `gl.setSize` |
| `core/events.ts` | `createEvents`、`intersect`、`filterPointerEvents` | 只对 `internal.interaction`（挂了事件处理器的对象）做 raycast，但用的是 **`raycaster.intersectObject(obj, true)`，会递归检测所有子节点**。pointermove 只检测挂了 Move/Over/Enter/Out/Leave 的对象。命中结果按 `events.priority` 降序、再按 distance 升序排序，然后逐级冒泡（`eventObject`）。可以用 `events.filter` 自定义排序或裁剪 |
| `core/reconciler.tsx` | `removeChild`、`disposeOnIdle`、`flushSync` | 卸载时，非 `primitive` 对象会在 idle 优先级自动 `dispose()`；`dispose={null}` 可以跳过这一步。**`<primitive>` 永远不会被自动 dispose**，引擎自己管理资源正好依赖这一点。`flushSync` 可以同步提交 R3F 的更新（本单元的基准测试就用它计时） |
| `core/hooks.tsx` | `useFrame(cb, priority)`、`useThree(selector)`、`useLoader`（`suspend-react` 缓存） | 不带 selector 调用 `useThree()` 时，store 的任何变化都会让组件重渲染。这是常见的性能陷阱 |
| `docs/API/canvas.mdx` | "Renderers / WebGPU / Ownership" 小节 | 官方给出的 v9 WebGPU 写法：`extend(THREE from 'three/webgpu')` 加 async 工厂 |

### 2.2 R3F v10 alpha（`.cache/research/r14/r3f-v10`）

| 文件 | 关键符号 | 要点 |
|---|---|---|
| `packages/fiber/src/index.tsx`、`webgpu/index.tsx`、`legacy.tsx`、`extension.tsx` | `provider = { webgl: () => import(...), webgpu: () => import(...) }` | 默认入口通过 `<Canvas renderer>` 选用 WebGPURenderer，并**动态导入**对应的 support 模块。`/webgpu` 入口静态绑定 WebGPU，`useThree`/`useFrame` 的类型收窄为 `WebGPURenderer`。`/extension` 是给库作者用的无 three 依赖入口 |
| `support/webgpu.ts`、`support/webgl.ts` | `webgpuSupport = { Renderer, RenderTarget, CanvasTarget, occlusion: {...} }` | fiber 内唯一引用 `three/webgpu` 的地方 |
| `core/renderer.tsx` | `createRoot` 中的 `rendererSetup` | ① 用 `rendererSetup` 串行化并发的 `configure()`，避免工厂被重复调用（#3752）。② 在 `init()` 之前先 `setPixelRatio` 和 `setSize`，避免 depth attachment 停留在 300×150（#3847）。③ `await renderer.init()`，然后通过 `backend.isWebGPUBackend` 设置 `state.webGPUSupported`。④ `primaryCanvas` 允许多个 canvas 共享同一个 WebGPURenderer（每个 canvas 一个 `CanvasTarget`）。⑤ 向 scheduler 注册以下系统 job：`canvasTarget`（start 相位）、`events.flush`（input 相位）、`frustum`（render 之前）、`visibility`（render 之前）、默认 `render`（render 相位；若存在用户注册的 render 相位 job，或 `internal.priority` 不为 0，或设置了 `renderOverride`，则让位） |
| `core/hooks/useFrame/index.ts` | `useFrame(cb, { id, phase, priority, fps, drop, enabled, before, after })` | 默认相位为 `update`。数字 priority 大于 0 仍保留 v9 的"接管渲染"语义，但会打印弃用提示。返回 `{ pause, resume, step, stepAll, invalidate, isPaused }` |
| `docs/frame-loop.mdx` | 默认相位：`start → input → physics → update → render → finish`；支持 `addPhase` | `fps` 节流：每个 rAF 最多执行一次，`drop:true` 丢弃落后的帧，`drop:false` 按整数步长追赶时间戳。`frameloop` 按 root 独立设置；`invalidate(frames, stackFrames)` 上限 60；`onIdle` |
| `core/visibility.ts`、`docs/scene/visibility.mdx` | `onFramed`、`onOccluded`、`onVisible` | 在对象上挂回调，状态翻转时触发。`onOccluded` 基于 WebGPU occlusion query 与节点 observer，只在 WebGPU 下生效，比 drei `Html occlude` 的 raycast 便宜得多 |
| `packages/tsl` | `useUniforms`、`useNodes`、`useLocalNodes`、`useBuffers`、`useGPUStorage`、`useRenderPipeline` | TSL 资源的作用域化管理，支持 HMR 重建。`useBuffers` 与 `useGPUStorage` 标注为 Experimental |
| `docs/API/additional-exports.mdx` | `registerRootExtension`、`setRenderOverride` | **给"命令式渲染管线"预留的官方接口**：引擎自带的 EDL 或后处理可以挂成 render override，同时保留 fps 节流和错误边界 |
| `packages/test-renderer/src/WebGPUContext.ts` | WebGPU mock | 没有 GPU 的 CI 也能测试 R3F 场景图逻辑 |

### 2.3 drei v10.7.9（`refs/web3d/drei/src`）：与本项目相关的组件精读

| 组件（文件） | 机制（读源码所得） | 每帧成本、坑 | 结论 |
|---|---|---|---|
| `core/PerformanceMonitor.tsx` | 每帧把 `performance.now()` 压入 `frames`；累计满 `ms=250` 后算一次 fps，写入 `averages[index++ % iterations]`；攒满 `iterations=10` 个后按 `bounds(refreshrate)` 判定（默认刷新率大于 100 Hz 时为 [60,100]，否则 [40,60]），超过 `threshold=0.75`（即 ≥8/10）的采样越界就调整 `factor ± step(0.1)`；`flipflops` 超限后进入 fallback，此后不再调整 | 每帧只有 O(1) 开销。反应延迟 ≥2.5 s。fps 基于 rAF 间隔计算，**在 demand 模式、后台标签页、SwiftShader 下都会失真** | adopt，用于**档位粗调**（DPR 与画质档）；细调交给自写的 PerfGovernor |
| `core/AdaptiveDpr.tsx` | 监听 `performance.current`，执行 `setDpr(current * initialDpr)`，`pixelated` 时设置 `imageRendering` | DPR 变化会重新分配 drawing buffer，因此只在 regress 时切换 | adopt，配合控制器的 `regress` 使用 |
| `core/AdaptiveEvents.tsx` | `performance.current !== 1` 时 `events.enabled = false` | 相机运动期间不做 hover raycast | adopt |
| `core/BakeShadows.tsx` | `shadowMap.autoUpdate=false`，只更新一次 | 适用于静态场景 | V0.4 之后若启用阴影再用 |
| `core/Detailed.tsx` | 包装 `THREE.LOD`，每帧 `lod.update(camera)`，可设 `hysteresis` | 按距离分级，只适用于**少量**模型（P600 的 glTF 三档） | adopt，用于选中或近处的无人机 |
| `core/Instances.tsx` | `<Instances>` 加若干 `<Instance>`（`PositionMesh` 虚拟对象）；每帧对**每个实例**执行 decompose/compose 并写入 `instanceMatrix`；每个实例一个 React 组件；提供 `raycast` 代理 | 每实例每帧 O(1) 的 CPU 与 GC 开销，另有 React 组件开销。适合几十到几百个可交互对象 | reference。无人机层直接用 InstancedMesh（§3.6） |
| `core/Points.tsx` | `PointsBuffer`：**每帧**给 position/color/size 设 `needsUpdate = true`，且使用 `DynamicDrawUsage`；`PointsInstances`：每个点一个 React 组件 | **每帧整块重传**（1M 点约 15 MB/帧） | **skip**，点云必须自研 |
| `core/PointMaterial.tsx` | 继承 `PointsMaterial`，用 `onBeforeCompile` 注入圆点 AA，并读取 `renderer.capabilities.isWebGL2` | NodeMaterial 不会调用 onBeforeCompile，**在 WebGPU 下静默退化为方点**（实测只剩 3 个像素） | skip |
| `core/CameraControls.tsx` | 包装 yomotsu `camera-controls`；以 **priority −1** 调用 `useFrame(controls.update(delta))`；`controlstart`、`control`、`update`、`wake` 事件触发 `invalidate()`，开启 `regress` 时还会调用 `performance.regress()`；`makeDefault` 写入 `state.controls` | 默认 `smoothTime=0.25`、`draggingSmoothTime=0.125`、`restThreshold=0.01`、`dollyToCursor=false`；`colliderMeshes` 每次更新发射 4 条 ray | **adopt**，作为主相机控制器 |
| `core/MapControls.tsx`、`OrbitControls.tsx` | three-stdlib 的控制器，`change` 事件触发 invalidate | 可选 | 备选 |
| `core/GizmoHelper.tsx` + `GizmoViewport.tsx` | 用 `<Hud renderPriority=1>` 渲染一个正交子场景；点击坐标轴时以 `turnRate = 2π rad/s` 做四元数插值（slerp）旋转主相机，并兼容 CameraControls（`setPosition`） | Hud 的 priority 1 会**接管渲染并重复绘制主场景**；在 WebGPURenderer 下 `gl.capabilities.getMaxAnisotropy` 不存在，**直接崩溃** | adopt（仅 WebGLRenderer，需设 `renderPriority={2}`）；WebGPU 路线等 drei v11（已修复为 `getMaxAnisotropy(renderer)`） |
| `core/Hud.tsx` | priority 1：`autoClear=true`，渲染主场景；随后 `clearDepth()` 并渲染 HUD 场景 | 与自定义渲染管线冲突 | 按需用 |
| `web/Html.tsx` | 每个 `<Html>` 调用一次 `ReactDOM.createRoot`；每帧 `calculatePosition` 投影，位移超过 `eps=0.001` 才改 `style.transform`；`occlude=true` 时执行 `raycaster.intersectObjects([scene], true)` | N 个标签就有 N 个 React root，外加 N 次 DOM 写入；**occlude 会遍历点云的每一个点** | adopt，**只用于选中无人机的详情卡片**；大量标签改用自研 LabelLayer（§3.8） |
| `web/View.tsx` | 单个 WebGL 上下文，用 `setScissor`/`setViewport` 渲染多个 DOM 区域，经 tunnel-rat 挂载；`useFrame(index)` 接管渲染 | 每个 View 都会重新渲染一次场景 | adopt，用于 FPV 画中画（V0.2）；v10 下换成多 canvas 方案 |
| `core/Line.tsx` | three-stdlib 的 `Line2`/`LineSegments2` 加 `LineMaterial`（GLSL，屏幕空间线宽） | 在 WebGPURenderer 下失效或崩溃 | WebGL 路线可用，用于航线 |
| `core/Trail.tsx` | `useTrail`：每帧对 Float32Array 做 `shiftLeft`（O(n)），然后调用 `MeshLineGeometry.setPoints` 重建 | O(length) 并产生分配，MeshLine 为 GLSL | reference。轨迹用环形缓冲自研（§3.7） |
| `core/Bvh.tsx` | 给子网格挂 `acceleratedRaycast`，并调用 `computeBoundsTree`（默认 SAH 分割、maxLeafTris 10） | 只处理 Mesh | 用于任务编辑时拾取建筑 mesh |
| `core/meshBounds.tsx` | raycast 只检测包围球加包围盒 | 极廉价 | adopt，作为无人机代理体的 raycast |
| `core/Stats.tsx`、`StatsGl.tsx` | stats.js DOM 面板；stats-gl 通过 `addAfterEffect` 调用 `stats.update()`，支持 GPU timer | 视觉风格与 shadcn 不统一 | StatsGl 只在开发期使用；产品内的性能 HUD 自研（lieflat 风格，§3.4） |
| `core/Cloud.tsx`（`Clouds`） | 实例化 billboard，**每帧按距离排序**后写入 instanceMatrix，默认 limit 200，底层为 MeshLambertMaterial | 使用 MeshBasicMaterial 时 WebGPU fallback 下可用 | 可作为 V0.3 云 Level 1 的原型 |
| `core/Grid.tsx`、`Sky.tsx`、`Sparkles.tsx` | 都是 GLSL ShaderMaterial | WebGPURenderer 下失效 | 环境效果由 Environment Engine 自研（TSL） |

### 2.4 drei v11 alpha（`.cache/research/r14/drei-v11`）

- 目录按渲染器拆分：`src/core`（与渲染器无关）、`src/legacy`（WebGL 专用）、`src/webgpu`（TSL 实现）、`src/external`（依赖第三方包，如 CameraControls、Bvh、Splat）、`src/experimental`。
- `component-status.json`（本单元解析过）共 144 个组件：107 个 agnostic、27 个 implemented（有 legacy 和 webgpu 两份实现）、4 个 todo（PointMaterial、Text、Splat、Outlines）、6 个 wont-port（如 Stars，改用 `@pmndrs/sky`）。
  - 已移植到 WebGPU：Line、Segments、Wireframe、Trail、Sparkles、Grid、SpotLight、ContactShadows、AccumulativeShadows、BakeShadows、MeshTransmissionMaterial 等。
  - 新增 `webgpu/Performance/Inspector`，接入 three 的 Inspector，要求 `frameloop="always"`。
- **PerformanceMonitor 相对 v10 没有算法变化**（逐行 diff 只多了注释），仍按 rAF fps 采样。

---

## 3. 可复用算法与实现（含伪代码和参数）

### 3.1 帧循环契约：v9 priority 与 v10 相位

本项目统一使用以下帧序。v9 与 v10 的写法对照：

| 顺序 | 任务 | v9 写法 | v10 写法 | 预算（60 fps，桌面端） |
|---|---|---|---|---|
| 0 | 遥测收包：从 Worker 或 SAB 交换最新快照 | `addEffect(cb)`（全局 before） | `useFrame(cb, { phase: 'input' })` | ≤0.2 ms |
| 1 | 仿真时钟：计算 `renderTime = tServerEst − D` | `useFrame(cb, -3)` | `{ phase: 'physics', id: 'simclock' }` | ~0 |
| 2 | 无人机插值：写 InstancedMesh，更新 follow 目标 | `useFrame(cb, -2)` | `{ phase: 'physics', after: 'simclock', id: 'drones' }` | ≤0.5 ms / 100 架 |
| 3 | 相机控制：CameraControls.update | drei 内部固定为 `-1` | `{ phase: 'update', id: 'camera' }` | ~0.05 ms |
| 4 | 点云 LOD 选择与请求调度 | `useFrame(cb, 0)` | `{ phase: 'update', after: 'camera', id: 'lod' }`，可加 `fps: 30` | ≤1 ms（增量） |
| 5 | 环境：uniform 更新与 compute 派发 | `useFrame(cb, 0)` | `{ after: 'camera' }` | ≤0.3 ms |
| 6 | 标签投影与避让 | `useFrame(cb, 0)` 内部 2 帧执行 1 次 | `{ after: 'lod', fps: 30 }` | ≤0.5 ms |
| 7 | 渲染管线（EDL 或后处理） | `useFrame(cb, 1)`，**接管渲染** | `setRenderOverride(store, fn)` | GPU 为主 |
| 8 | HUD 或 Gizmo | `GizmoHelper renderPriority={2}` | `{ after: 'render' }` | — |
| 9 | 性能采样与统计 | `addAfterEffect(cb)` | `{ phase: 'finish' }` | ~0 |

v9 适配层示例：

```tsx
// engine-agnostic contract
export interface EngineModule {
  readonly root: THREE.Object3D            // mounted via <primitive>
  update(ctx: FrameCtx): void              // hot path, no allocation
  dispose(): void
  on(ev: 'needs-render', cb: () => void): () => void
}
interface FrameCtx { camera: THREE.Camera; renderer: AnyRenderer; size: {w:number;h:number};
  dpr: number; dt: number; renderTime: number; quality: QualityState; moving: boolean }

// R3F v9 adapter (<= 50 lines per module)
function EngineLayer({ engine, priority = 0 }: { engine: EngineModule; priority?: number }) {
  const invalidate = useThree((s) => s.invalidate)          // selector! never useThree() bare
  useEffect(() => engine.on('needs-render', () => invalidate()), [engine, invalidate])
  useFrame((s, dt) => engine.update(buildCtx(s, dt)), priority)
  useEffect(() => () => engine.dispose(), [engine])
  return <primitive object={engine.root} />                 // never auto-disposed by R3F
}
```

注意事项：

- 所有 `useThree` 调用**必须带 selector**。
- 引擎在 `update` 内不得调用 React 的 setState。
- 需要回写 UI 的量（例如已加载点数、当前预算）写进 zustand vanilla store，UI 以 5–10 Hz 节流订阅。

### 3.2 按需渲染（`frameloop`）策略状态机

```text
state := 'always' if any of:
   sim.running && visibleDrones > 0
   camera.transitioning (camera-controls 'transitionstart'..'rest')
   env.particlesEnabled && env.visible
   pointcloud.fadingTiles > 0           # tile fade-in (0.3s) after arrival
   ui.timeline.playing
otherwise 'demand'

in 'demand': invalidate() on
   camera-controls 'control'/'update'/'wake'  (drei does this)
   engine 'needs-render' (tile arrived, budget changed, layer toggled)
   store changes (R3F does automatically: rootStore.subscribe(invalidate))
   window resize / dpr change (automatic)
```

实现方式：`useEffect(() => setFrameloop(policy), [policy])`，其中 `setFrameloop` 通过 `useThree(s => s.setFrameloop)` 获取。进入 demand 时同时**暂停 PerfGovernor 和 PerformanceMonitor 的采样**（demand 下的 rAF 间隔不代表渲染能力）。v10 可以用 `useFrame(..., { enabled })` 实现同样的效果。

### 3.3 WebGPU 接入与后端回退链

**v9（当前稳定版）写法**。本单元已在 headless Chromium 实测，WebGPU 后端和 `forceWebGL` 后端都能渲染：

```tsx
import * as THREE_GPU from 'three/webgpu'
import { Canvas, extend } from '@react-three/fiber'
extend(THREE_GPU)  // enables <meshBasicNodeMaterial/> etc. (docs/API/canvas.mdx)

type Backend = 'webgl' | 'webgpu' | 'webgpu-webgl2'
const gl = (backend: Backend) => backend === 'webgl'
  ? { antialias: false, powerPreference: 'high-performance' }           // legacy WebGLRenderer
  : async (p: { canvas: HTMLCanvasElement }) => {
      const r = new THREE_GPU.WebGPURenderer({ canvas: p.canvas, antialias: false,
                                               forceWebGL: backend === 'webgpu-webgl2' })
      await r.init()                                                     // R3F waits (root.ready)
      return r
    }
<Canvas gl={gl(backend)} dpr={[1, 1.5]} flat frameloop={policy} eventSource={shellRef} eventPrefix="client" />
```

后端选择算法。只在启动时执行一次，结果写入 `localStorage` 缓存：

```text
detectBackend():
  if url ?renderer= override → use it
  hasWebGPU = !!navigator.gpu && (await navigator.gpu.requestAdapter()) != null
  glInfo = WEBGL_debug_renderer_info → UNMASKED_RENDERER
  software = /SwiftShader|llvmpipe|Software/i.test(glInfo) || adapter.info.isFallbackAdapter
  if software → 'webgl' + quality tier 'low' (budget 300k, dpr 1, particles off)
  if V0.1–V0.2 → 'webgl' (default), 'webgpu' behind feature flag
  if V0.3+ and hasWebGPU → 'webgpu' (compute particles), else 'webgl'
```

**关键约束**（实测结果与 three 源码 `src/materials/nodes/PointsNodeMaterial.js` 的注释一致）：

| 需求 | WebGLRenderer（legacy） | WebGPURenderer，WebGL2 后端 | WebGPURenderer，WebGPU 后端 |
|---|---|---|---|
| 点尺寸大于 1 px | `gl_PointSize`（`PointsMaterial.size`、ShaderMaterial） | **1 px**；需要用 Sprite 实例化，每点 6 个顶点 | **1 px**；需要 Sprite 实例化或 compute 光栅化 |
| GLSL ShaderMaterial / onBeforeCompile | 可用 | **不可用**（`NodeBuilder: Material "ShaderMaterial" is not compatible`） | 不可用 |
| compute shader | 不可用 | 不可用（WebGL2 后端没有 compute，只能用 transform feedback 之类的有限替代） | 可用 |
| drei v10 GLSL 组件 | 可用 | 失效 | 失效或崩溃 |

由此推出点云引擎的 **RenderBackend 抽象**（与 r11 单元的 three.js 研究衔接）：

- `GLPointBackend`：WebGLRenderer 加 ShaderMaterial（gl_PointSize、自适应点径、EDL，均为 GLSL）。V0.1 默认使用，也是软件渲染环境下的唯一可用选项。
- `GPUPointBackend`：WebGPURenderer 加 TSL（实例化 Sprite 点，或 compute 光栅化）。V0.3 起可选。
- R3F 宿主不关心具体后端，只把 `renderer` 传给 `engine.update(ctx)`。

R3F v10 的写法是 `<Canvas renderer={{ forceWebGL }}>` 或 `import { Canvas } from '@react-three/fiber/webgpu'`。以 drei v11 的 `/webgpu` 入口实测：GizmoHelper、Line、Segments、Grid、Sky、Trail、Sparkles、Instances、Edges、StatsGl、PerformanceMonitor、AdaptiveDpr、Html、CameraControls（`/external`）在两个后端都**没有报错**，fallback 后端的像素也都正常。

### 3.4 性能自适应：R3F regress、drei PerformanceMonitor 与自研 PerfGovernor

**R3F 内建机制**（`core/store.ts`）：`performance.regress()` 把 `current` 设为 `min(0.5)`，经过 `debounce(200 ms)` 后恢复为 `max(1)`。drei 的控制器在 `regress` 开启时，每个 control 或 update 事件都会调用它。AdaptiveDpr 读取 current，执行 DPR = current × initialDpr。本项目在此基础上把 **current 同时作为点云的"运动预算系数"**，实现 Potree 风格的"移动时降质"。

**PerfGovernor**（新模块，`engine/core/PerfGovernor.ts`，V0.1 就要有）：

```text
params:
  targetMs   = 1000/60 (desktop) | 1000/30 (low tier)      # target frame time
  alpha      = 0.1                                          # EMA
  upThresh   = 0.75*targetMs, downThresh = 1.15*targetMs
  holdDown   = 500 ms (must exceed downThresh continuously)
  holdUp     = 2000 ms, cooldown = 1000 ms after any change
  budget     ∈ [300k, deviceMax]   (deviceMax: 1.5M integrated GPU / 4M discrete / 300k software)
  dpr        ∈ [1.0, min(devicePixelRatio, 2)]   (step 0.25)
  particles  ∈ [0, 300k]
  motionScale = R3F performance.current (1 at rest, 0.5 while moving)

sample (addAfterEffect / phase 'finish'):
  cpuMs = t_afterRender - t_frameStart                       # main-thread JS incl. render submit
  gpuMs = timer query (EXT_disjoint_timer_query_webgl2 / WebGPU timestamp-query) if available
  frameMs = max(cpuMs, gpuMs ?? rafIntervalMs)
  ema = ema + alpha*(frameMs - ema)

control (every frame, cheap):
  if now - lastChange < cooldown: return
  if ema > downThresh for holdDown:
      if budget > minBudget: budget *= 0.8          # 1st knob: points (dominant cost)
      elif dpr > 1:        dpr -= 0.25              # 2nd knob
      elif particles > 0:  particles *= 0.7         # 3rd knob
      lastChange = now
  elif ema < upThresh for holdUp:
      reverse order: particles*=1.1 → dpr+=0.25 → budget*=1.1 (clamp)
      lastChange = now
effectiveBudget = budget * (0.5 + 0.5*motionScale)   # moving → 75% budget with min .5
publish to engine (ctx.quality) and to UI store at 2 Hz (perf HUD, lieflat sparkline)
```

- drei 的 PerformanceMonitor 只作为**档位检测器**使用：`onFallback` 时锁定到 low tier；`flipflops={3}` 用于防止档位来回抖动。
- 两者不要同时操作同一个旋钮：DPR 统一由 PerfGovernor 控制，不同时使用 AdaptiveDpr 和 PerformanceMonitor 的 `setDpr`。

### 3.5 命令式引擎的 R3F 外壳：3DTilesRendererJS 模式

参考 `3d-tiles-renderer`（`.cache/research/r10/3DTilesRendererJS/src/r3f/components/TilesRenderer.jsx`，@b70e594）：

1. 在 `useEffect` 中 `new Impl(url)`，把 `'needs-render'` 和 `'needs-update'` 事件接到 `invalidate()`；卸载时调用 `impl.dispose()`。
2. 每帧依次调用 `camera.updateMatrixWorld()`、`setResolutionFromRenderer(camera, gl)`、`tiles.update()`。
3. 在 `useLayoutEffect` 中执行 `tiles.setCamera(camera)`，清理时 `deleteCamera`。**多相机（FPV、画中画）通过注册多个相机实现，LOD 选择取并集。**
4. 选项通过 `useDeepOptions(tiles, options)` 深度赋值，不重建实例。
5. 返回 `<primitive object={tiles.group} />`，再用 Context 向子组件（插件、控制器）暴露实例。

本项目的 `PointCloudLayer`、`EnvironmentLayer`、`DroneLayer`、`LabelLayer` 都照此实现。**第 3 点尤其重要**：点云引擎的 LOD 选择必须支持多视图（主视图加 FPV），每个视图有自己的预算，最后取并集。drei 的 `View` 或 v10 的多 canvas 只负责"画"，不负责"选"。

### 3.6 无人机层：快照插值加 InstancedMesh 分档

```text
Telemetry ring per drone: snapshots {t_sim, p(xyz), q(quat), v, mode}, capacity 32
Clock sync: offset = EMA_{0.05}(t_sim - t_recv_local); tServerEst = now + offset
D (interp delay) = clamp(2 / telemetryHz, 0.06, 0.25) s     # 20Hz → 100 ms
renderTime = tServerEst - D
for each drone i:
   find s0.t ≤ renderTime ≤ s1.t
   u = (renderTime - s0.t)/(s1.t - s0.t)
   p = lerp(s0.p, s1.p, u); q = slerp(s0.q, s1.q, u)
   if no s1 (late): p = s0.p + s0.v*min(renderTime - s0.t, 0.25)   # bounded extrapolation
LOD bucket by screen radius r_px = R * H_px / (2 * d * tan(fovY/2))  (R = 0.6 m for P600)
   r_px < 4        → marker bucket (instanced sprite, fixed 10 px, always on top option)
   4 ≤ r_px < 48   → low-poly bucket (InstancedMesh, 1 draw call)
   r_px ≥ 48 and rank < K=6 → full glTF P600 (drei <Detailed> / THREE.LOD with props anim)
   hysteresis 15% on thresholds; bucket reassign at most 10 Hz
write: o.position/quaternion → o.updateMatrix() → im.setMatrixAt(k, o.matrix); im.count = n_bucket
       im.instanceMatrix.addUpdateRange(0, n_bucket*16); needsUpdate = true
```

- 实测（§5.3）：500 架无人机每架各挂一个 `useFrame` 时，每帧 1.2–1.4 ms；单个 InstancedMesh 加一个 `useFrame` 时为 0.55–0.77 ms。与原生"500 个 mesh 单循环更新"（0.9–1.16 ms）相比，每个订阅者多约 0.5 µs；与实例化写法相比的差额主要来自 500 个独立对象的 matrixWorld 更新和场景遍历。
- **遥测走 React state 时，500 架每次提交 6.5–8.4 ms。**
- 因此只有在"每架无人机需要各自的 React 交互组件"时才拆成独立组件，而且上限按 50 架控制。

### 3.7 轨迹（Trail）：O(1) 环形缓冲

与 drei `Trail` 的 O(n) 搬移加 MeshLine 重建不同，这里每次只追加一个点：

```text
TrailRing(cap = 2048 per drone, minStep = 0.5 m or 0.2 s):
  pos: Float32Array(cap*3) in one shared BufferAttribute for all drones (slot = droneIdx*cap)
  head, count
push(p): if dist(p, last) < minStep && dt < 0.2: return
         k = slot + head; pos.set(p, 3k); attr.addUpdateRange(3k, 3); attr.needsUpdate = true
         head = (head+1) % cap; count = min(count+1, cap)
draw: LineSegments over (k, k+1) pairs via index buffer rebuilt only on wrap,
      or two Line draws [head,cap) + [0,head) with drawRange;
      vertex shader age = ((head - vid + cap) % cap)/cap → alpha fade
cost: O(1) upload (12 B) per sample; 100 drones × 20 Hz = 24 KB/s
```

- WebGL 路线用 `LineBasicMaterial`（1 px）或 drei `Line`（Line2，有宽度）。
- WebGPU 路线用 drei v11 的 `webgpu/Geometry/Line`（TSL），或自写宽线 TSL 材质。

### 3.8 标签层（LabelLayer）：替代大量 `<Html>`

```text
one overlay <div class="labels"> (position:absolute; inset:0; pointer-events:none; contain:strict)
pool of label elements (max K = 48), content rendered by React once per data change (≤5 Hz)
per frame (fps 30):
  for each anchor a (drones, targets, waypoints):
     ndc = project(a.world, camera); if ndc.z∉(-1,1) or |ndc.x|>1.05 or |ndc.y|>1.05: hide
     screen = ((ndc.x+1)/2*W, (1-ndc.y)/2*H)
     priority = selected ? ∞ : alarm ? 1e6 : -distance
  sort by priority; greedy declutter using grid hash (cell 96×28 px): place if cell free else hide/leader-line
  occlusion (optional, 10 Hz): sample low-res depth / ID buffer (async readback) at anchor, or
          ray-march coarse occupancy voxels (World Geometry) — NEVER scene raycast against Points
  write el.style.transform = `translate3d(${x|0}px,${y|0}px,0)` only if |Δ| ≥ 0.5 px
```

- drei `<Html>` 只给**选中的那一架**无人机做交互卡片（内含 shadcn 组件），必须设 `occlude={false}`。需要遮挡时，WebGPU 下用 v10 的 `onOccluded`，WebGL 下用 depth 采样。
- 标签视觉遵循 shadcn 规范（`Badge` 或 `Card` 样式的纯 CSS 版），图标用 morphicons 的 SVG，禁用 emoji。

### 3.9 拾取：事件系统的正确用法

- `<Canvas eventSource={shellRef} eventPrefix="client">`：UI 面板和画布共用同一个指针事件源，画布本身设 `pointer-events:none`（R3F 会自动处理）。
- 能挂 R3F 事件处理器的只有**少量**对象：航点、区域手柄、无人机代理体。无人机代理体设 `raycast={meshBounds}`，只检测包围球或包围盒。
- **点云根节点及其祖先禁止挂 handler。** 原因是 `intersectObject(obj, true)` 会递归，而 `Points.raycast` 需要逐点测试。
- 点云拾取统一由 `engine/picking/Picker` 负责。它只在 pointerdown 或 click 时触发，hover 不触发。两种实现：
  1. 八叉树 ray 遍历：节点按 ray 进入距离排序；节点内做点到 ray 的距离测试，阈值为 `pickRadiusPx × 该深度下每像素对应的世界尺寸`。也可以在 Worker 里为已加载节点构建 `three-mesh-bvh@0.9` 的 `PointsBVH`。
  2. GPU ID pass：渲染一个 1×1 或 5×5 的 ID render target，异步回读。
- `events.filter` 用于过滤 hits，例如只保留 priority 最高的一层。相机运动期间由 `AdaptiveEvents` 关闭 hover。

### 3.10 相机模式（对应原设计 §40）

| 模式 | 实现 | 关键参数 |
|---|---|---|
| Free / Orbit | drei `CameraControls makeDefault regress` | `smoothTime=0.25`，`draggingSmoothTime=0.08`，`dollyToCursor=true`，`infinityDolly=false`，`minDistance=2`，`maxDistance=5000`，`maxPolarAngle=0.49π`（不能钻到地下），`colliderMeshes=[coarseTerrainMesh]`（**只能用简化碰撞网格，不能用点云**） |
| Bird Eye | 同一个控制器调用 `setLookAt(target + (0, 0, h), target, true)`，并限定 `polar ≈ 0` | 过渡时长由 smoothTime 决定 |
| Third Person / Follow | 每帧（priority −2 之后）：`controls.moveTo(droneP.x, droneP.y, droneP.z, true)`，偏移角度保持用户最近一次调整 | 跟随期间设 `truckSpeed=0`，按 Esc 退出 |
| FPV | 关闭 controls（`enabled=false`），主相机直接 `copy` 无人机相机的世界矩阵（机体加云台外参） | FOV 与传感器参数一致；FPV 期间点云预算按 FPV 视图重新选择 |
| 聚焦 | `controls.fitToBox(box, true, { paddingTop: 0.1, ... })` | 用于选中对象或区域 |

**坐标约定（重要）**：World 采用 ENU Z-up（r10 结论）。three、drei（Grid、Sky、GizmoViewport 标签）和 camera-controls 默认 Y-up。**推荐在 World 根节点统一做一次映射 `three(x,y,z) = (E, U, −N)`**（右手系，可验证 E×U = −N），而不是修改 `camera.up`。这样 drei 的 helper 不需要改。同时以局部 ENU 原点消除 float32 抖动（UTM 坐标约 10⁶ 量级时，float32 的精度只有约 0.06–0.5 m）。

### 3.11 多视图：FPV 画中画与多机相机墙

- **v9**：用 drei `<View>`（单个 WebGL 上下文，scissor 裁剪）。主视图 index 为 1，FPV 为 2。FPV 视图降到 0.5 倍分辨率，隔帧渲染（在 useFrame 里计帧），点预算为主视图的 30%。
- **v10**：`<Canvas id="main" renderer>` 作为主画布，`<Canvas renderer={{ primaryCanvas: 'main', scheduler: { after: 'main', fps: 30 } }}>` 作为 FPV 或相机墙，共享同一个 WebGPU device。
- 禁止开多个独立的 v9 `<Canvas>`。每个 Canvas 都有自己的 WebGL 上下文和资源副本，而浏览器对 WebGL 上下文数有上限（通常为 16）。

---

## 4. 在本项目中的落点与复用方式

| 条目 | 来源 | 用途 | 落点模块 / 版本 | 复用方式 | 理由 |
|---|---|---|---|---|---|
| `<Canvas>` / `createRoot` / `useFrame` / `invalidate` | R3F v9 `web/Canvas.tsx`、`core/loop.ts` | 3D 视口宿主、帧驱动、按需渲染 | `apps/web/src/viewport/WorldCanvas.tsx`，V0.1 | adopt | 稳定、生态完整；实测热路径无额外开销 |
| async `gl` 工厂接入 WebGPURenderer | R3F v9 `core/configuration.ts` 的 `createRenderer`、`core/root.tsx` 的 ready 与 dispose | WebGPU 可选开关、WebGL2 回退 | `viewport/renderer.ts`，V0.1 可选，V0.3 默认启用 WebGPU | adopt | 实测可用；卸载会等待 dispose |
| `performance.regress` 与 `AdaptiveDpr`、`AdaptiveEvents` | R3F `core/store.ts`；drei 对应文件 | 运动期间降质 | `viewport/perf/`，V0.1 | adopt | 与 camera-controls 联动，几乎零成本 |
| PerfGovernor | 自研，参考 drei `PerformanceMonitor` 的采样与滞回 | 点预算、DPR、粒子的闭环控制 | `engine/core/PerfGovernor.ts`，V0.1 | port（重写） | drei 的算法太粗（2.5 s 窗口），且没有 GPU 时间 |
| EngineLayer 适配模式 | 3DTilesRendererJS 的 r3f 绑定 | 引擎与 R3F 的桥接 | `viewport/layers/*`，V0.1 | port | 业界标杆写法，可退回纯命令式 |
| CameraControls | drei `core/CameraControls.tsx` 加 `camera-controls` 3.1 | Free、Bird、Follow 相机，聚焦 | `viewport/controls/`，V0.1 | adopt | 功能完整；WebGPU 下同样可用（实测） |
| GizmoHelper 与 GizmoViewport | drei | 视角方位球 | `viewport/hud/`，V0.1（仅 WebGL；设 renderPriority=2） | adopt / 自研兜底 | v10 在 WebGPU 下崩溃；v11 已修复 |
| Html | drei `web/Html.tsx` | 选中无人机的详情卡片（shadcn） | `viewport/overlay/SelectedCard.tsx`，V0.2 | adopt（限 1–3 个） | 数量多时开销大；不要用 occlude |
| LabelLayer | 自研，参考 drei Html 的投影与 zIndex 公式 | 批量标签 | `engine/labels/`，V0.2 | port | 单一 DOM 层，带避让 |
| Detailed / THREE.LOD | drei `core/Detailed.tsx` | P600 近景三档 LOD | `engine/drones/`，V0.2 | adopt | 简单可靠 |
| InstancedMesh 分档 | 自研，参考 drei `Instances` | 大量无人机 | `engine/drones/DroneLayer.ts`，V0.2（多机为 V0.6） | port | drei Instances 的逐实例 React 组件与 decompose 开销不必要 |
| meshBounds | drei `core/meshBounds.tsx` | 无人机代理体拾取 | `viewport/layers/DroneProxies.tsx`，V0.2 | adopt | 极廉价 |
| Bvh 与 PointsBVH | drei `Bvh`，three-mesh-bvh 0.9 | 建筑 mesh 与点云瓦片的 CPU 拾取 | `engine/picking/`，V0.2 | adopt（three-mesh-bvh 直接依赖 0.9） | drei 锁在 0.8，没有 PointsBVH |
| View（多视图） | drei `web/View.tsx` | FPV 画中画 | `viewport/views/`，V0.2；V0.4 起迁移到 v10 多 canvas | adopt | 单上下文 |
| Line / Segments | drei | 航线、区域边框（WebGL 路线） | `viewport/layers/MissionLayer.tsx`，V0.2 | adopt（WebGL）/ drei v11 webgpu | GLSL 限制 |
| TrailRing | 自研 | 实时轨迹 | `engine/drones/TrailRing.ts`，V0.2 | port | drei Trail 为 O(n) |
| Clouds | drei `core/Cloud.tsx` | 云 Level 1 原型 | `engine/env/clouds`，V0.3 | reference | 每帧排序的写法可参考；正式实现用 TSL |
| StatsGl / Inspector | drei；v11 `webgpu/Performance/Inspector` | 开发期性能面板 | dev only，V0.1 | adopt（dev） | 产品内 HUD 自研（lieflat 风格） |
| `onFramed` / `onOccluded` | R3F v10 `core/visibility.ts` | 标签遮挡、离屏暂停 | V0.4+（迁移 v10 后） | reference 后 adopt | 仅 WebGPU，比 raycast 便宜 |
| 相位调度 | R3F v10 与 `@pmndrs/scheduler` | 帧序契约 | V0.1 先按 §3.1 用 v9 priority 模拟，迁移后切换 | reference | 语义一致，迁移成本低 |
| `setRenderOverride` / `useRenderPipeline` | R3F v10 与 `@react-three/tsl` | EDL、后处理管线 | V0.3+ | reference 后 adopt | 官方接口，保留 fps 节流和错误边界 |
| test-renderer | `@react-three/test-renderer`（v10 含 WebGPU mock） | 场景图单测（任务层、选择逻辑） | CI，V0.1 | adopt | 无 GPU 环境可用 |
| Points / PointMaterial / Sky / Stars / Sparkles / Grid / Text / Outlines（drei v10） | drei | — | — | **skip** | 每帧重传、GLSL 不兼容 WebGPU、或视觉体系不符 |

---

## 5. 对比与推荐

### 5.1 推荐排序

| 排名 | 仓库或线 | 综合评价 |
|---|---|---|
| 1 | **R3F v9.8.x** | 2026 年高度活跃（32.5k stars，发布频率高），是 React 与 three 集成的事实标准。实测：框架热路径开销可以忽略，async WebGPU 工厂可用，卸载与 dispose 行为完善 |
| 2 | **drei v10.7.x（选择性使用）** | 相机、Gizmo、性能钩子、HTML 叠加能节省大量工程量，但**所有 GLSL 组件在 WebGPU 下不可用**，一些"便捷"组件是性能陷阱（§2.3） |
| 3 | **R3F v10 + drei v11（alpha）** | 设计上最契合本项目：相位调度、WebGPU 一等支持、多 canvas、TSL hooks、可见性事件、render override。风险：处于 alpha（alpha.0 发布于 2026-01，到 09 月仍是 alpha.5），bundle 587 KB gz，React peer 要求 `<19.3`。**作为 V0.3–V0.4 的迁移目标** |

### 5.2 核心问题：3D 视口用"R3F 管理场景 + 命令式引擎"还是"纯命令式 + React UI 壳"？

三种方案对比：

| 维度 | A：纯命令式 three，React 只做 UI 壳 | **B′：R3F 宿主加命令式引擎（推荐）** | C：纯声明式 R3F |
|---|---|---|---|
| 热路径每帧开销（实测） | 基准 | ≈A（hybrid 的差异在测量误差内） | 与 A 相当（逐对象 useFrame 每个多约 0.5 µs） |
| LOD 节点增删 | 0.02–0.3 ms / tick | 0.02–0.3 ms / tick | **2–154 ms / tick**，不可用 |
| 遥测驱动 | 直接写 buffer | 直接写 buffer | 走 setState 时 500 架为 6.5–8.4 ms / 次，不可用 |
| 首次挂载 800 个节点 | 5.5–7.3 ms | ≈A（通过 `<primitive>`） | 13–20 ms |
| bundle 体积（最小，gz） | three 132 KB + React 60 KB（UI 壳本来就要 React）= 192 KB | 308 KB（v9；Canvas 的 `extend(THREE)` 使 three 无法 tree-shake） | 同 B′ |
| 任务编辑、Gizmo、选择、拖拽 | 全部自研（TransformControls、事件、命中测试、撤销） | drei 的 PivotControls、TransformControls，R3F 事件冒泡，声明式航点 | 同 B′ |
| 与 shadcn UI 的状态同步 | 需要自建事件总线和订阅 | zustand 共享（R3F 本身基于 zustand），用 `eventSource` 共享指针 | 同 B′ |
| Suspense 资源加载（P600 glTF、贴图） | 自研 | `useGLTF`、`useLoader` 自带缓存与预加载 | 同 B′ |
| HMR 与开发体验 | 需要自己处理引擎重建 | 组件级 HMR；v10 还有 TSL HMR | 同 B′ |
| 迁移到 Worker + OffscreenCanvas | **最容易**（引擎本身与 DOM 无关） | 引擎层易迁移，外壳需要 `@react-three/offscreen`（0.0.8，不成熟）或改成 A | 困难 |
| 测试 | 引擎单测 | 引擎单测，外加 R3F test-renderer 测场景图 | 同 B′ |
| 团队失误风险 | 低（没有 React 陷阱） | **中**：有人把 LOD 或遥测写进 state、裸用 `useThree()`、在点云祖先上挂事件，需要 lint 规则和代码评审（§6） | 高 |
| WebGPU 路线 | 直接使用 three WebGPURenderer | v9 用 async 工厂（drei 的 GLSL 组件不可用）；v10 原生支持 | 同 B′ |

**推荐 B′，理由如下：**

1. 性能上，只要热路径保持命令式，B′ 与 A 在 CPU 上没有可测差异，瓶颈都在 GPU（点数与填充率）。
2. 工程上，数字沙盘有大量**低频、结构化、需要交互编辑**的 3D 对象：航点、禁飞区、任务区域、传感器视锥、选中高亮、坐标轴、Gizmo。这些正是 React 声明式模型和 drei 的强项。A 方案要全部自己实现，而且还得和 shadcn UI 做双向同步。
3. 可逆性：引擎层不依赖 React。如果后续确实需要把 3D 迁到 Worker（例如点云解码和 LOD 的 CPU 压力过大），可以只把 R3F 外壳换成一个约 300 行的命令式 host（相当于切到 A），引擎代码不改。
4. 演进：R3F v10 的相位调度、render override 和多 canvas 与本项目的帧序契约一一对应，迁移成本很低。

**B′ 的硬性护栏**（写进 CONTRIBUTING，并用 eslint 规则约束）：

- 点云、环境、无人机、标签、拾取五个引擎**不得 import React**。
- `useFrame` 回调内禁止调用 setState 和分配对象，只能调用 `engine.update(ctx)`。
- `useThree` 必须带 selector。
- 超过 50 个同类对象时必须实例化，禁止 `.map` 成 JSX。
- 点云和地形的祖先节点禁止挂事件处理器。
- UI 订阅遥测与性能数据时节流到 ≤10 Hz，并用 `startTransition` 包裹。
- 禁止在 WebGPU 路径中使用 drei v10 的 GLSL 组件（在 eslint 的 `no-restricted-imports` 里按路径列出）。

### 5.3 实测数据

**(1) CPU 框架开销微基准**（`bench/src/cpu.jsx`）：

测量方法：
- stub renderer 模拟 WebGLRenderer 的 CPU 侧遍历（`updateMatrixWorld` 加每个可见对象一次 frustum 测试）。
- R3F 以 `frameloop:'never'` 运行，调用 `advance(t, true, store.getState())` 推进帧，用 `flushSync` 同步提交。
- 取 5 或 7 次重复的中位数，两轮结果都列出。机器 load 为 14–16，所以要看**比值**。

| 场景 | 原生 three（µs） | R3F B′ hybrid（µs） | R3F 声明式（µs） |
|---|---|---|---|
| 每帧：50 架无人机（Instanced）+ 200 个点云节点 | 306–361 | 245–290 | — |
| 每帧：500 架无人机（Instanced） | 470–865 | 551–775 | — |
| 每帧：500 架无人机，每架一个 mesh | 895–1162（单循环） | — | 1202–1391（每架一个 useFrame） |
| 遥测经 React state 提交：50 架 | — | — | 357–787 / 次 |
| 遥测经 React state 提交：500 架 | — | — | **6512–8423 / 次** |
| LOD tick：可见 200，增删 20 | 22–23 | 18–50 | **1967–3182** |
| LOD tick：可见 200，增删 100 | 110–137 | 92–115 | **4613–7365** |
| LOD tick：可见 2000，增删 20 | 48–92 | 50–97 | **14872–26932** |
| LOD tick：可见 2000，增删 100 | 160–245 | 203–292 | **99390–153853** |
| 挂载 800 个 Points | 5500–7300 | —（与原生相同） | 13300–20000 |

**(2) WebGPU 兼容性冒烟测试**（`bench/src/smoke.jsx` 与 `bench10/src/smoke10.jsx`）：

判定方法：每个组件单独渲染 30 帧，收集控制台错误和页面异常，并对截图做像素统计。WebGPU 后端的截图不可用，所以只看错误。

| 组件 | R3F9 + drei10，WebGLRenderer | R3F9 + drei10，WebGPURenderer（WebGL2 后端） | R3F9 + drei10，WebGPURenderer（WebGPU 后端） | R3F10α5 + drei11α7 `/webgpu`，两个后端 |
|---|---|---|---|---|
| Points `size=4` | 4 px（31961 像素） | **1 px（3509 像素）** | 1 px（按源码判断） | 1 px；**Sprite 实例化恢复到 32251 像素** |
| CameraControls / MapControls | 正常 | 正常 | 正常 | 正常 |
| GizmoHelper + GizmoViewport | 正常 | **崩溃（getMaxAnisotropy）** | **崩溃** | 正常 |
| Html | 正常 | 正常 | 正常 | 正常 |
| Line / Segments / Edges | 正常 | LineMaterial 不兼容，不渲染 | **drawIndexed 报错** | 正常 |
| Grid / Sky / Sparkles / Outlines | 正常 | ShaderMaterial 不兼容 | ShaderMaterial 不兼容 | Grid、Sky、Sparkles 正常（Outlines 未测，状态为 todo） |
| Trail | 正常 | MeshLineMaterial 不兼容 | 不兼容 | 正常 |
| PointMaterial | 正常 | 静默失效（只剩 3 个像素） | 静默失效 | 仍为 todo |
| Instances / Detailed / Billboard / Bvh | 正常 | 正常 | 正常 | 正常（Instances 已测） |
| Clouds（MeshBasicMaterial） | 正常 | 正常 | 无报错 | 未测 |
| Text（troika） | 正常 | 正常 | **drawIndexed 报错** | todo（`@pmndrs/glyph`） |
| Stats / StatsGl | 正常 | 正常 | 正常 | StatsGl 正常 |

**(3) bundle 体积**（esbuild minify 后 gzip -9；`sizes/`）：

| 构建 | gz |
|---|---|
| React + react-dom（UI 壳基线） | 60 KB |
| three WebGLRenderer 最小场景 | 132 KB |
| three/webgpu 最小场景 | 215 KB |
| R3F 9.8.1 最小 Canvas（含 React） | 308 KB |
| R3F 10.0.0-alpha.5 `/webgpu` 最小 Canvas | **587 KB**（同时打包了 `three.module.js` 349 KB raw、`three.webgpu.js` 696 KB raw、Inspector UI、EXRLoader；v10 的 Unreleased changelog 声称已改为按需加载） |

**(4) 整帧基准的说明**（`bench/src/{vanilla,r3f_hybrid,r3f_decl}`）：在 SwiftShader 且机器高负载的条件下，GPU 光栅化主导帧时间，还会偶发 12–33 s 的 GPU 进程卡顿，三种写法都出现过。**绝对 fps 不可信，已放弃作为证据**，只保留"框架开销可忽略"的定性结论，并与 CPU 微基准相互印证。性能验收必须在真实 GPU 上完成（§6）。

---

## 6. 风险与注意事项

| 风险 | 说明 | 缓解 |
|---|---|---|
| R3F v10 与 drei v11 仍是 alpha | 2026-01 发布 alpha.0，到 2026-09 仍为 alpha.5，canary 每天变；API 仍有破坏性变更（TSL hooks 刚拆出为独立包；`onUpdate` 已移除） | V0.1–V0.2 锁定 v9.8 与 drei 10.7；适配层按 v10 语义设计（§3.1）；迁移门槛：v10 发布 rc 且 bundle ≤350 KB gz |
| drei v10 在 WebGPU 下大面积失效 | GLSL 组件不渲染；GizmoHelper 会**崩溃整个 Canvas**，错误冒泡到 R3F 的 ErrorBoundary 后整棵树卸载 | WebGPU 开关打开时不挂载这些组件（按 `renderer.isWebGPURenderer` 条件渲染），或自研 TSL 替代；视口外包一层 `<ErrorBoundary>`，降级到 WebGL |
| WebGPURenderer 的点只有 1 px | 两个后端都是如此；实例化 Sprite 每点 6 个顶点，顶点开销是 gl_PointSize 的 4–6 倍 | 点云引擎采用双后端（§3.3）；WebGPU 路线评估 compute 光栅化（参考 Potree-Next 与 r11） |
| WebGPURenderer 的 WebGL2 回退不能跑 GLSL | `forceWebGL` 仍然走 NodeMaterial，所以"WebGPU 与 WebGL2 fallback"并不等于"同一套 GLSL" | 材质要么全部 TSL，要么 legacy WebGLRenderer 与 GLSL 并存；原设计 §34 的 "Fallback: WebGL2" 需要澄清（§7） |
| 事件递归 raycast | 在点云祖先上挂 onClick，每次点击 O(N) | 护栏加 Picker（§3.9） |
| `<Html occlude>` 与大量 `<Html>` | 每帧对整个 scene 做 raycast，N 个 ReactDOM root | LabelLayer（§3.8） |
| `useThree()` 不带 selector、store 抖动 | root store 的任何 set 都触发重渲染和 invalidate；AdaptiveDpr 改 DPR 时所有无 selector 的组件都会重渲染 | lint 规则；性能面板监控 React commit 次数 |
| 渲染接管冲突 | v9 中 priority 大于 0 就接管渲染；Hud、GizmoHelper、View、EffectComposer 都用 priority，**多个接管者会重复渲染** | 统一由 `RenderPipeline` 组件使用 priority 1；Hud 与 Gizmo 用 2；View 与 FPV 由 RenderPipeline 内部调度 |
| demand 模式的边界 | 引擎内部状态变化（瓦片到达、淡入动画）不会自动 invalidate；PerformanceMonitor 在 demand 下失真 | 引擎统一 emit `'needs-render'`；PerfGovernor 在 demand 下暂停 |
| React 版本约束 | R3F 9 要求 react `<19.4`，R3F 10 alpha 要求 `<19.3`；shadcn 与 React 19.2 兼容 | 锁定 React 19.2.x |
| THREE.Clock 弃用 | three r18x 的 Clock 会打印弃用警告（R3F v9 内部仍在用） | 可忽略；v10 已换成 scheduler 的时间 |
| headless 测试环境 | SwiftShader 下 WebGPU 截图拿不到画面；fps 数据失真；机器负载高时 GPU 进程会卡顿 | CI 只做功能测试（错误日志、WebGL 与 fallback 的像素统计）；性能验收用真实 GPU 机器，Playwright 加 `--enable-gpu` 或手动测试 |
| bundle 体积 | Canvas 的 `extend(THREE)` 使 three 无法 tree-shake；drei 按具名导入可以 tree-shake，但依赖较重（mediapipe、hls.js 会按需懒加载） | 从 drei 做具名导入；Vite 的 manualChunks 把 three 拆成独立 chunk；视口路由懒加载 |
| 精度与坐标 | ENU 大坐标导致 float32 抖动；drei helper 默认 Y-up | 局部原点，加根节点映射 `(E, U, −N)`（§3.10） |

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§9 / §34 前端技术栈：补上"3D 视口宿主"一层，并把渲染器策略写清楚。**
   - 建议把原表格中的 "3D Engine: Three.js、Renderer: WebGPURenderer、Fallback: WebGL2" 拆成四项：
     - **Viewport Host**：`@react-three/fiber` 9.8（V0.3 以后评估 10.x）。
     - **Viewport Kit**：`@react-three/drei` 10.7（选择性使用，白名单见 §4）。
     - **Engine**：自研命令式模块 `engine/*`，不依赖 React。
     - **Renderer**：WebGLRenderer 为默认（V0.1–V0.2），WebGPURenderer 为可选开关，V0.3 起在支持的设备上默认启用。
   - 原文 "WebGPURenderer + WebGL2 Fallback" 有歧义：`WebGPURenderer(forceWebGL)` 仍然走 TSL 或 NodeMaterial，**不能运行 GLSL**，而且**点只有 1 px**。必须明确点云和粒子材质采用哪种写法（全部 TSL，或 GLSL 与 TSL 双实现）。
2. **§12 WebGPU 定位里的 "Point Rendering" 需要加注。** WebGPU 原生 point primitive 只有 1 px。大点径要用实例化 Sprite（每点 6 个顶点）或 compute 光栅化；后者更快，但实现复杂，参考 Potree-Next。这会影响点预算：同样的 GPU 下，实例化 Sprite 路线的预算大约只有 gl_PointSize 路线的 1/3 到 1/2。
3. **§16 场景结构：从"对象树"升级为"模块 + 帧序契约"。**
   - 各 Layer（World、Environment、Drone、Sensor、Mission、Debug）明确分为两类：**命令式引擎**（点云、环境粒子、无人机实例、轨迹、标签）和 **R3F 声明式**（任务航点、区域、传感器视锥、选中高亮、Gizmo）。
   - 增加 **OverlayLayer**（DOM 标签与选中卡片）和 **PickingLayer**。
   - 写入 §3.1 的帧序表：遥测、相机、LOD、环境、渲染、HUD、采样。
   - 写入"多视图 LOD 取并集"的要求（主视图加 FPV）。
4. **§37 更新频率：补充"渲染策略"和"UI 刷新频率"。**
   - 物理、飞控、后端、WS 的频率按原文保留。增加：浏览器端遥测**插值延迟 D = 2/telemetryHz**（20 Hz 时为 100 ms），限幅外推 250 ms。
   - 3D 渲染采用 always 与 demand 自动切换（§3.2）。
   - **UI 面板刷新 ≤10 Hz**，与渲染解耦（遥测进 zustand vanilla store，3D 在 useFrame 里 `getState()`，UI 节流订阅）。
5. **§28 DroneState：为插值补字段。** 需要 `seq`、`t_sim`（服务端仿真时间）、`quaternion`（替代欧拉角 `orientation`）、`velocity`（用于外推）；浏览器侧另记 `t_recv`。WS 推送建议用二进制紧凑帧（每架约 64 B），格式细节由 WS 协议单元定义。
6. **§38–§40 UI 与交互：补充 3D 标签和相机模式的实现约束。**
   - 3D 标签分两级：普通标签由 LabelLayer 统一绘制，最多 48 个，带避让；选中对象的详情卡片用 drei Html 加 shadcn Card，最多 3 个。
   - 相机模式映射到 camera-controls 的具体参数（§3.10）。
   - 性能 HUD（FPS、帧时、点数、预算、DPR）按 lieflat-charts 风格做 sparkline，放在 Debug 面板中。
7. **新增"坐标约定"小节（归入 §7 World Model 的 Geographic 部分）。** 规定 ENU Z-up 与 three Y-up 的映射 `(E, U, −N)`、局部原点、单位（米）。不写清楚的话，drei 和 camera-controls 的默认 Y-up 会与 World 坐标系冲突。
8. **§43 MVP：增加可量化的性能验收标准。** 建议写入：
   - 1080p 集成显卡：点预算 ≥1.5M 时 p95 帧时 ≤16.7 ms（60 fps）。
   - 主线程 JS ≤4 ms/帧。
   - LOD 选择 ≤1 ms/帧。
   - 首屏 ≤2 s 看到根节点点云（渐进加载）。
   - 相机运动期间无 ≥50 ms 的长任务。
   - 100 架无人机、20 Hz 遥测时 UI 无掉帧。
   - 软件渲染（SwiftShader 或无 GPU）自动降到 low tier，功能可用。
   - **验收必须在真实 GPU 上进行**；CI 的 headless 环境只做功能冒烟。
9. **§42 仓库结构：`apps/web` 内部分层。** 建议为 `apps/web/src/{ui (shadcn shell), viewport (R3F host + adapters), engine (imperative, no React), stores (zustand), net (WS client/worker)}`，并用 eslint 的 import 边界规则保证 `engine/` 不 import `react`、`@react-three/*`。以后若迁移到 Worker 或 OffscreenCanvas，只需要替换 `viewport/`。
10. **§4 "浏览器不是仿真核心"：再加一条"React 不是渲染核心"。** 浏览器内还要再分一层：React 负责结构与交互，引擎负责每帧的热路径。这是本单元实测得出的最重要工程约束：LOD 与遥测一旦进入 React state，开销会高出 2–3 个数量级。
