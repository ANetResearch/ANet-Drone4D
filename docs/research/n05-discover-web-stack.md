# n05 研究笔记：2026 前端工程栈核实（React / Vite / TS / Tailwind / shadcn / three / R3F / 状态管理 / 序列化 / 测试与性能）

> 研究单元：n05（Discovery）｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §9、§12、§14、§34（前端栈）、§36（实时通信）、§37（刷新频率）、§42（Repo 结构）、§43–§44（MVP / V0.1）
>
> 版本号都用 `npm view <pkg> version|dist-tags|time|peerDependencies` 实测过，镜像是 `registry.npmmirror.com`，时间为 2026-09-28。star 数和最近提交日期用 curl 抓 GitHub 页面及 `commits.atom` 得到（脚本在 `.cache/research/n05/meta.sh`，原始输出在 `stars_core.txt`、`stars_disc.txt`）。
>
> **本机实测工程**：`/data/projs/anet-drone/.cache/research/n05/trial/`。用下文 §3.1 推荐的依赖版本，从零搭了一个 Vite 8 + React 19.3 + TS 7 + Tailwind 4.3 + three r186 `WebGPURenderer` + R3F 9.8.1 + zustand 5 + TanStack Query 5 的小沙盘，并实际跑通以下内容：
> - **数据**：UrbanScene3D Shenzhen 的 5M 点 PLY，随机打散后截取前 2M 点，量化成每点 12 B（`prep_city.py`）。
> - **渲染**：流式渐进加载，按 point budget 自适应控制密度，200 架 mock 无人机在 Worker 里以 20 Hz 发送遥测，主线程插值渲染。
> - **测试**：Vitest 5 的 unit、browser（SwiftShader WebGPU）和 bench 三个项目；Playwright 1.63 性能用例，覆盖 CDP tracing、rAF 采样、LoAF，并对比三种 GPU 标志组合。
> - **质量检查**：oxlint type-aware 和 `tsc`（TS 7 原生版）。
>
> **Discovery 克隆**（只读，位于 `refs/discovery/`）：`scheduler`（pmndrs/scheduler）、`stats-gl`、`koota`、`pacer`（TanStack）、`msgpack-javascript`、`msgpackr`。
>
> 环境：8 核 Xeon，没有 GPU，Chrome for Testing 151（Playwright 缓存 rev 1234），Node 22.12.0。性能测试时机器上还有别的研究单元在跑，load average 在 5–15 之间波动，**所以绝对数值偏悲观，只应拿来比较相对大小**。

---

## 0. 结论速览

| 仓库 / 包（实测版本） | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **react / react-dom 19.3.0**（2026-09-09；250,798 stars；facebook/react 现在重定向到 react/react） | UI 运行时。`<ViewTransition>` 和 Fragment ref 已稳定；transition 之间不再互相阻塞 | **adopt**（精确锁定 19.3.0） | V0.1 | 5/5 |
| **vite 8.3.1**（2026-09-24；83,061 stars）+ `@vitejs/plugin-react 6.1.1` | Rolldown + Oxc 一体化构建；插件不再依赖 Babel | **adopt** | V0.1 | 5/5 |
| **typescript 7.0.2**（2026-07-08，Go 原生实现；111,253 stars） | 类型检查。trial 全量 `tsc` 只需 1.5 s；**没有 JS API** | **adopt**（只用作 tsc）。需要 TS API 的工具走 `@typescript/typescript6` 别名 | V0.1 | 4/5 |
| **tailwindcss / @tailwindcss/vite 4.3.3**（97,723 stars） | shadcn 的样式底座；v4.3 新增 scrollbar-\*、`@container-size`、`zoom-*` | **adopt** | V0.1 | 5/5 |
| **shadcn 4.21.0**（124,753 stars） | 组件分发 CLI，具体结论见 d04 | **adopt**（devDependency） | V0.1 | 5/5 |
| **three 0.186.1**（r186；116,014 stars） | `three/webgpu`（WebGPURenderer，并能回退到 WebGL2）和 `three/tsl` | **adopt**，锁定 `~0.186.1`（结论见 r11） | V0.1 | 5/5 |
| **@react-three/fiber 9.8.1**（32,586 stars） | 3D 视口宿主，peer 要求 `react >=19 <19.4` | **adopt** | V0.1 | 5/5 |
| @react-three/fiber **10.0.0-alpha.5** / drei 11.0.0-alpha.7 | 原生支持 WebGPU/TSL，以 `@pmndrs/scheduler` 为内核 | **reference**。**peer 要求 `react <19.3`，与 React 19.3.0 冲突（已实测 ERESOLVE）** | V0.3+ 迁移 | 3/5 |
| **zustand 5.0.15**（58,763 stars） | vanilla store 加 selector，承载 UI 摘要态 | **adopt** | V0.1 | 5/5 |
| **@tanstack/react-query 5.104.0**（50,371 stars） | REST 服务端状态：Scene、Mission、Config、File | **adopt** | V0.1 | 4/5 |
| **@msgpack/msgpack 3.1.3**（1,558 stars；2026-07 仍有提交） | 低频异构消息的编解码，与 Python `msgpack` 互通 | **adopt**（控制面）。高频遥测**不用**它，改用 raw struct | V0.1 | 4/5 |
| msgpackr 2.1.0（695 stars） | 更快的 msgpack 实现，records 扩展可以再快 1.6–2 倍 | **reference**。records 与 Python 不互通 | — | 3/5 |
| **@playwright/test 1.63.0**（96,804 stars） | E2E 与性能用例：CDP、tracing、LoAF | **adopt**（配合本地 Chrome 151，用 `executablePath`） | V0.1 | 5/5 |
| **vitest 5.0.2** + `@vitest/browser-playwright`（17,167 stars） | unit、真实浏览器组件测试和 bench（以 fixture 形式提供） | **adopt** | V0.1 | 5/5 |
| **oxlint 1.86.0 + oxlint-tsgolint 7.0.2003**（oxc 22,904 stars，tsgolint 1,446 stars） | 基于 typescript-go 的 type-aware lint。trial 耗时 3.2 s，并实测能拦截 engine 层越界 import | **adopt**，替代受 TS 7 阻塞的 typescript-eslint | V0.1 | 4/5 |
| **pmndrs/scheduler 0.2.0**（2026 年新仓库；8 stars，2026-09-27 仍在提交） | 与框架无关的帧调度器，也是 R3F v10 的帧循环内核：phase、fps 限频、demand 模式、fixed timestep | **adopt**（V0.2 起用来统一帧循环，v9 桥接方式已实测）。**port** fixed-step 算法 | V0.2 | 4/5 |
| **RenaudRohlinger/stats-gl 4.2.3**（280 stars） | 无 DOM 的 `StatsProfiler`，为 WebGPU/WebGL 提供 GPU timestamp | **adopt**，作为性能 HUD 的数据源，界面按 lieflat 风格自绘 | V0.1 | 4/5 |
| cloudflare/partykit 下的 `partysocket 1.3.0`（1,276 stars） | 带退避重连的 WebSocket | **port**（约 60 行重连逻辑，放进 Worker） | V0.1 | 3/5 |
| pmndrs/koota 0.6.6（744 stars） | 面向实时应用的 ECS 状态库 | **reference**，V0.6 多机实体模型时再评估 | V0.6 | 2/5 |
| TanStack/pacer 0.22（777 stars，beta） | 节流、防抖、队列 | **skip**：用 scheduler 的 fps 任务替代（§5.6） | — | 2/5 |
| aidenybai/react-scan 0.5.7（21,858 stars） | 检测 React 重渲染风暴 | **adopt**，仅开发期使用 | V0.1 | 3/5 |
| utsuboco/r3f-perf 7.2.3（最后提交 2024-11） | R3F 性能面板 | **skip**：已停更，只支持 WebGL | — | 1/5 |

**实现者先读这 14 条（每条都有实测或源码依据）：**

1. **峰值兼容矩阵（已实测）。**
   - React 19.3.0 与 R3F 9.8.1 兼容，后者 peer 为 `>=19 <19.4`。
   - **R3F `10.0.0-alpha.5` 和 canary、drei `11.0.0-alpha.7` 的 peer 都是 `react >=19.0 <19.3`**。执行 `npm i @react-three/fiber@10.0.0-alpha.5` 会直接报 ERESOLVE。
   - 所以 V0.1 采用 **R3F 9.8.1 + React 19.3.0**，适配层按 v10 的语义来设计（见 r14）。如果将来要提前切到 v10，就必须把 React 降回 19.2.x。
2. **TypeScript 7.0.2 已经是 npm 的 `latest`（Go 原生实现）。**
   - trial 的 `tsc -p` 耗时 1.5–1.7 s。
   - 它**没有编程 API**：包的 `exports["."]` 只指向 `lib/version.cjs`。`typescript-eslint@8.70.1` 的 peer 是 `typescript <6.1.0`，已实测 ERESOLVE。
   - 对策：`tsc` 用 7.0.2；lint 改用 **oxlint + tsgolint**（基于 typescript-go 的 type-aware lint，实测 3.2 s）。
   - 如果某个工具必须用 TS API（typescript-eslint、ts-morph、vite-plugin-checker），就在 devDependencies 里加上 `"typescript": "npm:@typescript/typescript6@^6.0.2"` 和 `"@typescript/native": "npm:typescript@^7.0.2"` 这组别名（这是微软官方的迁移方案）。
3. **TS 7 默认值变了，tsconfig 必须显式写出以下几项**（否则 `import.meta.env` 和 node 类型会找不到）：
   - `types: ["vite/client","node"]`，因为默认值变成了 `[]`
   - `rootDir`
   - `paths` 改用相对路径，`baseUrl` 已被移除
   - 另外 `strict` 默认开启，`target es5`、`moduleResolution node10` 都已移除
4. **Vite 8 与 Rolldown 的实测表现：**
   - trial 生产构建 1.0–1.2 s。
   - `manualChunks` 已废弃，改为 `build.rolldownOptions.output.codeSplitting.groups`。
   - `resolve.tsconfigPaths: true` 已内置，不再需要 vite-tsconfig-paths。
   - 默认 `build.target` 是 `baseline-widely-available`，也就是 `chrome111/edge111/firefox114/safari16.4`（出自 `vite/dist/node/chunks/node.js` 的 `ESBUILD_BASELINE_WIDELY_AVAILABLE_TARGET`）。本项目显式设为 `es2023`。
   - plugin-react 6 用 Oxc 做 Refresh 转换，不再依赖 Babel。React Compiler 有两种接法：`react({ compiler: true })`（依赖 `oxc-transform-react`，实验性，**peer 锁定 `^0.145.0`，而 npm latest 是 0.151.0，需要手动锁版本**），或者 `@rolldown/plugin-babel` 加 `babel-plugin-react-compiler`。
5. **包体问题：**
   - three 的 chunk 有 1.32 MB（gzip 后 348 KB），因为 R3F v9 会 `import 'three'`，把 `three.module.js`（WebGLRenderer，源码 647 KB）也打了进来，再加上 `three.webgpu.js` 和 `three.core.js`。
   - 把 `three` 用别名指向 `three/webgpu` 后，可以减少 241 KB（min）/ 54 KB（gzip），**代价**是 R3F 默认的 `new THREE.WebGLRenderer` 会变成 undefined（Rolldown 报 `IMPORT_IS_UNDEFINED`）。所以这个别名**只有在始终传 `gl` 工厂函数时才安全**，列为 V0.2 的可选优化。
6. **Vitest 5 有两处破坏性变化：**
   - browser provider 改为工厂函数：`playwright({ launchOptions: { executablePath, args } })`，从 `@vitest/browser-playwright` 导入。
   - **bench 变成了 test-context fixture**：写法是 `test('x', async ({ bench }) => bench.compare(bench('a',fn), …))`，文件名必须匹配 `*.bench.ts`。Vitest 会自动为每个 project 生成名为 `"<name> (bench)"` 的基准项目，同一份 bench 可以**同时在 Node 和 Chromium 里跑**（§3.6）。在普通 test 里使用 `bench` 会直接报错。
7. **Playwright 1.63.0 需要 chromium rev 1243（Chrome 153），本机缓存只有 rev 1234（Chrome 151）。** 不重新下载，通过 `launchOptions.executablePath`（或环境变量 `PW_CHROME`）指向本地 Chrome，Vitest browser 模式和 Playwright test 都已实测可用。
8. **GPU 标志组合（实测）：**
   - 标志：

     | 用途 | 标志 |
     |---|---|
     | WebGL2 | `--use-angle=swiftshader --enable-unsafe-swiftshader --ignore-gpu-blocklist` |
     | WebGPU（追加在 WebGL2 那组之后） | `--enable-unsafe-webgpu --enable-features=Vulkan --use-webgpu-adapter=swiftshader` |

   - 在 Vitest browser 测试里，`requestAdapter()` 返回了非 null 的 adapter，`isWebGPUBackend` 为 true，Points 的 drawCalls 大于 0。
   - **`--disable-gpu-vsync --disable-frame-rate-limit` 会让 rAF 与实际呈现脱钩**：p50 显示 1–4 ms，但真实吞吐没变，单个用例的运行时间还从约 11 s 拉长到 2.1–2.5 分钟。**这组标志不能用于帧时间测量。**
9. **SwiftShader 下的性能基线**（1280×720，1 px 点，200 架无人机；机器负载约 5 时）：

   | 场景 | FPS | p50 帧时间 |
   |---|---|---|
   | 10–15 万点 | 5.5–6.6 | 约 140–175 ms |
   | 200 万点 | 0.5–0.7 | 1.5–1.9 s |

   WebGL2 后端与 WebGPU-SwiftShader 后端基本持平。所以 **CI 里的性能门禁只能做相对判断或行为判断**，例如控制器是否收敛、有没有来自自身 JS 的长任务、每帧脚本耗时是否超标。**绝对 FPS 门禁只能放在真 GPU runner 上。**
10. **计时精度：** 页面没有 cross-origin isolation 时，`performance.now()` 会被粗化到 100 µs（Vitest browser bench 的 p99 全是 0.1 ms 的整数倍）。给 dev 和 preview 加上 COOP `same-origin` 和 COEP `require-corp` 响应头后，实测 `crossOriginIsolated === true`，精度变为 5 µs，而且可以使用 `SharedArrayBuffer`，为以后的遥测环形缓冲打下基础。
11. **序列化实测**（500 架，每架 8 个 f32 字段，写入 SoA；Node / Chromium 的均值）：

    | 编码 | 解码耗时（Node / Chromium） | 消息体积 |
    |---|---|---|
    | raw struct | 1.8 / 1.5 µs | 16,016 B |
    | @msgpack/msgpack | 794 / 849 µs | 53,135 B |
    | msgpackr | 1049 / 817 µs | 54,137 B |
    | msgpackr records | 662 / 402 µs | 43,225 B |
    | JSON | 1998 / 1031 µs | 100,492 B |

    结论与 r27 一致：**高频遥测用 raw struct**，比 msgpack 快约 450 倍。控制面默认用 **@msgpack/msgpack**：它与 msgpackr（不开 records）速度相当，符合规范，和 Python 能直接互通。msgpackr 的 records 必须两端共享 structures 或开启 `sequential`，否则会报 `Data read, but end of buffer not reached`（已实测）。
12. **帧循环统一到 `@pmndrs/scheduler`（v9 桥接已实测）。**
    - 做法：`<Canvas frameloop="never">`，然后在 scheduler 的 `render` phase 里注册一个任务，调用 R3F 导出的 `advance(st.time)`。实测可以正常出图。
    - 一个 `fps: 4` 的 UI 摘要任务实测频率 3.25 Hz。限频任务的触发点会被量化到帧边界，11 fps 时每 3 帧触发一次。
    - scheduler 实例用 `Symbol.for('@pmndrs/scheduler')` 挂在 globalThis 上，R3F v10 用的是同一个单例，将来迁移时可以无缝衔接。
    - fixed timestep（`physics` phase 默认 1/60 s、最多 8 个子步、提供 `overstep` 插值因子）在 2026-09-27 才合并进主干，**npm 上的 0.2.0 还没有这个功能**（dist 里 `timestep` 出现 0 次）。
13. **自适应预算控制器踩过的两个坑**（都已修复，见 §3.7）：
    - 按"每 15 帧评估一次"来设计时，在 1 fps 下要 15 s 才做第一次决策，相当于死锁。改为按时间（250 ms）评估。
    - 在 R3F 的 `useFrame` 里调用 `state.clock.getDelta()` 会得到约 0，因为 R3F 在同一帧里已经调用过它，控制器就会误以为帧率达标。必须使用回调参数里的 `delta`。
    - 修复后，控制器在 SwiftShader 上约 2 s 内把 point budget 从 1M 压到下限 100k。
14. **oxlint 1.86 自带 React Compiler 风格的规则**（`react(purity)`、`react(refs)`），还包括 `typescript(no-floating-promises)`。它抓到了一个真实问题：three r186 的 `renderer.dispose()` 返回 Promise，但调用处没有 await。在 `overrides` 里配置 `no-restricted-imports`，可以禁止 `src/engine/**` 和 `src/net/**` 引入 react、@react-three、zustand，实测生效。这样就能保证"R3F 宿主 + 命令式引擎"的边界（见 r14）不被破坏。

---

## 1. 仓库概览

### 1.1 核心栈（版本与活跃度）

| 包 | npm latest（发布日） | 其他 dist-tag | GitHub（stars / 最近提交） | 关键 peer / engines |
|---|---|---|---|---|
| react / react-dom | 19.3.0（2026-09-09）；19.2.0 发布于 2025-10-01 | canary 19.3.0-canary-…-20260922 | facebook/react 250,798 stars / 2026-09-22 | — |
| vite | 8.3.1（2026-09-24）；8.0.0 发布于 2026-03-12 | previous 7.3.6 | vitejs/vite 83,061 stars / 2026-09-28 | node `^20.19 \|\| >=22.12`；依赖 rolldown ~1.2.9、lightningcss ^1.33 |
| @vitejs/plugin-react | 6.1.1（2026-08-28） | — | 同上 | peer `vite ^8`，可选 `oxc-transform-react ^0.145.0`、`@rolldown/plugin-babel` |
| typescript | 7.0.2（2026-07-08） | next 7.1.0-dev.20260928；6.x 最后一版 6.0.3（2026-04-16） | microsoft/TypeScript 111,253 stars / 2026-09-25；typescript-go 26,165 stars | 无 JS API；`@typescript/typescript6@6.0.2` 提供 `tsc6` |
| tailwindcss / @tailwindcss/vite | 4.3.3（2026-07-16）；4.3.0 发布于 2026-05-08 | v3-lts 3.4.19 | tailwindlabs/tailwindcss 97,723 stars / 2026-09-25 | peer `vite ^5.2–^8` |
| shadcn | 4.21.0（2026-09-04） | — | shadcn-ui/ui 124,753 stars / 2026-09-28 | 结论见 d04 |
| three / @types/three | 0.186.1（2026-09-24）/ 0.186.0 | — | mrdoob/three.js 116,014 stars / 2026-09-28 | 大约每 1–3 个月发一个 minor（r184 在 04，r185 在 06，r186 在 09） |
| @react-three/fiber | 9.8.1 | alpha 10.0.0-alpha.5；canary 10.0.0-canary.14007b4（2026-09-26） | pmndrs/react-three-fiber 32,586 stars / 2026-09-26；v10 milestone 87%（74 closed / 11 open，无截止日期） | v9：`react >=19 <19.4`；**v10：`react <19.3`、`three >=0.185`** |
| @react-three/drei | 10.7.9（2026-09-25） | alpha 11.0.0-alpha.7 | pmndrs/drei 9,902 stars / 2026-09-25 | v11 alpha 要求 `react <19.3` |
| zustand | 5.0.15（2026-08-13） | — | pmndrs/zustand 58,763 stars / 2026-08-24 | peer react ≥18 |
| @tanstack/react-query | 5.104.0（2026-09-26） | 没有 v6 | TanStack/query 50,371 stars / 2026-09-28 | react ^18 \|\| ^19 |
| @msgpack/msgpack | 3.1.3（2025-12-26） | — | msgpack/msgpack-javascript 1,558 stars / 2026-07-13 | 零依赖 |
| msgpackr | 2.1.0（2026-08-27） | previous 1.12.1 | kriszyp/msgpackr 695 stars / 2026-08-27 | 在 Node 下可选装原生扩展 |
| @playwright/test / playwright | 1.63.0（2026-09-04） | next 1.64.0-alpha-2026-09-28 | microsoft/playwright 96,804 stars / 2026-09-28 | 需要 chromium rev **1243**（Chrome 153） |
| vitest / @vitest/browser-playwright | 5.0.2（2026-09-25）；5.0.0 发布于 2026-09-03 | V4 4.1.11 | vitest-dev/vitest 17,167 stars / 2026-09-28 | node `^22.12 \|\| ^24 \|\| >=26`（**本机 22.12.0 恰好卡在下限**）、vite `^6.4–^8` |
| oxlint / oxlint-tsgolint | 1.86.0（2026-09-28）/ 7.0.2003 | 每周发版 | oxc 22,904 stars；tsgolint 1,446 stars / 2026-09-28 | — |
| rolldown | 1.2.11 | — | rolldown/rolldown 13,956 stars / 2026-09-28 | 随 Vite 8 一起安装 |

### 1.2 Discovery 候选（2025–2026 新出现，或 2026 年仍活跃）

| 仓库 | stars / 最近提交 | npm | 结论 |
|---|---|---|---|
| **pmndrs/scheduler** | 8 / 2026-09-27（2026 年新仓库，由 R3F v10 抽离出来） | @pmndrs/scheduler 0.2.0（2026-08-24） | 已克隆。adopt（V0.2） |
| **RenaudRohlinger/stats-gl** | 280 / 2026-07-10 | stats-gl 4.2.3 | 已克隆。adopt（HUD 数据源） |
| **pmndrs/koota** | 744 / 2026-08-25 | koota 0.6.6（2026-09-16） | 已克隆。reference |
| **TanStack/pacer** | 777 / 2026-09-27 | @tanstack/pacer 0.22.0，beta | 已克隆。skip |
| **msgpack/msgpack-javascript** | 1,558 / 2026-07-13 | 3.1.3 | 已克隆。adopt |
| **kriszyp/msgpackr** | 695 / 2026-08-27 | 2.1.0 | 已克隆。reference |
| cloudflare/partykit（partysocket） | 1,276 / 2026-08-03；旧仓库 partykit/partykit 5,724 stars，最近提交 2025-09 | partysocket 1.3.0 | 读了 node_modules 里的 dist。port |
| aidenybai/react-scan | 21,858 / 2026-08-16 | 0.5.7 | 仅开发期 adopt |
| pmndrs/detect-gpu | 1,213 / 2026-09-27 | 5.0.70 | reference，用来设初始预算档位 |
| utsuboco/r3f-perf | 781 / 2024-11-08 | 7.2.3 | skip（已停更） |
| GoogleChromeLabs/comlink | 12,797 / 2025-06-18 | 4.4.2 | skip（2025 年后几乎不维护；Worker 协议很简单，手写即可） |
| pmndrs/jotai 3.0.0（2026-09-08）/ valtio 2.3.2 | 21,284 / 10,240 | — | skip，状态管理统一用 zustand |
| TanStack/router 1.170 / react-router 8.4.0 | 15,136 / 56,589 | — | V0.4 按需引入（§5.5） |
| biomejs/biome 2.5.14 | 25,871 / 2026-09-28 | — | 备选格式化工具 |
| mswjs/msw 2.15.0、colinhacks/zod 4.6.5 | 18,227 / 44,032 | — | msw 用于 REST mock；zod 用于协议和配置校验 |
| @paulirish/trace_engine 0.0.65 | — | 2026-06 | reference：解析 DevTools trace 时可用（V0.3 深度分析） |

---

## 2. 源码结构与关键模块

### 2.1 pmndrs/scheduler（`refs/discovery/scheduler` @ f00432d，2026-09-27）

| 文件 / 符号 | 要点 |
|---|---|
| `src/core/phaseGraph.ts` · `DEFAULT_PHASES` | `start → input → physics(timestep 1/60) → update → render → finish`。`addPhase(name,{before/after,timestep,maxSubsteps})` 可以在运行时插入新 phase。`DEFAULT_MAX_SUBSTEPS = 8` |
| `src/core/scheduler.ts` · `class Scheduler` | 单例挂在 `globalThis[Symbol.for('@pmndrs/scheduler')]` 上，跨 bundle、跨 HMR 共享。整个应用只有一个 rAF 循环（`startLoop/stopLoop`）。多 root（`registerRoot(id,{getState,frameloop,order,before,after,maxDelta})`）；demand 模式（`invalidate(frames)`）；`step/stepRoot/stepJob` 手动推进（便于测试）；`onIdle` |
| `Scheduler.tickRoot`（L1276–1340） | 先调用 `advanceClock` 推进所有 fixed phase 的时钟，再按 bucket 执行。fixed phase 整桶重复执行 `pending` 次；普通 job 先经过 `shouldRun` 限频判断，限频 job 的 delta 用 `accumulatedTime` 的差值计算，这样不会出现"半速"问题 |
| `Scheduler.advanceClock`（L1356） | `acc += delta; pending = floor((acc+1e-9)/h); if pending>max {pending=max; acc %= h}; acc -= pending*h; overstep = acc/h` |
| `src/core/rateLimiter.ts` · `shouldRun` | `fps` 限频加 `drop`：drop 为 true 时丢弃落后的帧，为 false 时按间隔的整数倍追赶。容差 1 ms。第一次调用一定会执行 |
| `src/core/rootSorter.ts`、`sorter.ts` | 同一 phase 内按 priority 和 before/after 做拓扑排序 |
| `src/hooks/useFrame.ts`（`@pmndrs/scheduler/react`） | `useFrame(cb,{phase,fps,priority,before,after})` 返回 `FrameControls`，包括 `pause/resume/isPaused/invalidate/step` |
| `src/types.ts` | `FrameTimingState{time,delta,elapsed,frame,overstep}`、`RootOptions.maxDelta`（默认一帧，防止休眠后"瞬移"） |

### 2.2 stats-gl（`refs/discovery/stats-gl` @ dae82f5，2026-07-10）

| 文件 / 符号 | 要点 |
|---|---|
| `lib/core.ts` · `class StatsCore` | 选项：`trackGPU/trackCPT(compute)/trackHz/trackFPS/logsPerSecond(4)/samplesLog(40)/maxTimestampPairs(2048)` |
| `StatsCore.handleWebGPURenderer` | 检测到 `renderer.isWebGPURenderer` 后，先设 `renderer.backend.trackTimestamp = true`，然后 `await renderer.init()`，再检查 `hasFeature('timestamp-query')`。`patchThreeWebGPU` 通过包装 `renderer.info.reset` 拿到帧边界（three 内部的 Animation 每帧都会调用一次 reset） |
| `StatsCore.initializeGPUTracking`（WebGL） | 使用 `EXT_disjoint_timer_query_webgl2` 的 `TIME_ELAPSED_EXT`，查询异步回收，并判断 `GPU_DISJOINT_EXT` |
| `StatsCore.resolveTimestampsAsync`（原生 WebGPU） | 维护 QuerySet 和 readBuffer 池，`mapAsync` 按提交顺序回收，用 `BigUint64Array` 差值区分 render 和 compute |
| `lib/profiler.ts` · `class StatsProfiler` | **不带 DOM 的 profiler**：调用 `update()` 后用 `getData()` 取 `{fps,cpu,gpu,gpuCompute}`。主包导出了它 |
| `lib/statsGLNode.ts` · `StatsGLCapture` | TSL 节点抓图（调试用） |

### 2.3 koota（`refs/discovery/koota` @ cb1d015）

- `packages/core/src/storage/stores.ts` 的 `createStore`：schema 写成对象时是 **SoA**，每个字段一个**普通 JS 数组**，不是 TypedArray；写成工厂函数时是 AoS。
- `query/query-result.ts` 的 `updateEach(cb,{changeDetection:'auto'|'always'|'never'})` 和 `useStores(cb)`：直接拿到 SoA 数组批量处理。
- React 层 `packages/react/src/hooks/use-trait.ts` 按实体订阅 trait 变化，`use-query.ts` 订阅查询结果集。
- **判断**：它的 SoA 用的是普通数组，不能直接交给 GPU 上传，也不能零拷贝接收 WebSocket 二进制。我们的热路径（Float32Array SoA → `instanceMatrix`）用 koota 反而多一次拷贝。但它的 "trait/relation/query" 模型适合 V0.6 的多智能体、任务、编队关系（实体上挂 `Mission`、`Formation`、`AgentOf(relation)`），所以列为 reference。

### 2.4 TanStack Pacer（`refs/discovery/pacer` @ d01174b）

- `packages/pacer/src/throttler.ts` 的 `Throttler.maybeExecute`：**每次调用都会更新一次 `@tanstack/store` 状态**（`maybeExecuteCount+1`）。以 60–1000 Hz 调用时这是额外开销。
- `pacer-lite` 包没有 store。库整体仍是 0.x beta。
- 本项目的节流需求（UI 4–10 Hz）可以由 scheduler 的 `fps` 任务直接满足，没必要多引入一个依赖。

### 2.5 @msgpack/msgpack（`refs/discovery/msgpack-javascript` @ 6d4b666）

- `src/Encoder.ts`：`Encoder`（可复用实例，`initialBufferSize`，`ensureBufferSizeToWrite` 按倍数扩容）。`encodeSharedRef` 返回共享 buffer 的视图，**必须在下一次 encode 之前用完**。遇到 `ArrayBuffer.isView` 走 `encodeBinary`，所以 TypedArray 会被编码成 bin。
- `src/Decoder.ts`：`Decoder`（`useBigInt64`、`rawStrings`、`maxStrLength`、`mapKeyConverter`）；`decodeMulti`（一个 buffer 里连续多条消息）；`decodeAsync/decodeStream`（针对 ReadableStream）。`decodeBinary` 返回的是 `bytes.subarray`，**零拷贝视图**，byteOffset 不一定按 4 字节对齐，所以不能直接 `new Float32Array(bin.buffer, bin.byteOffset)`，要先 `slice()`，或者由服务端把二进制放在对齐的位置。
- `src/CachedKeyDecoder.ts`：长度 ≤16 字节的 map key 会被缓存，每个长度最多 16 条记录，从而减少字符串分配。

### 2.6 msgpackr（`refs/discovery/msgpackr` @ a9b9f1a，2.1.0）

- `unpack.js` 的 records 解码用 `new Function` 为每种结构生成专门的读取函数（约 L501），CSP 环境下要用 `index-no-eval.js`。
- `pack.js` 在开启 `moreTypes` 时用扩展类型 `0x74 't'` 编码 TypedArray。
- README 的 "Upgrading to 2.0"：移除了 `randomAccessStructure` 和 `struct.js`。
- 常用选项：`useRecords/structures/sequential/bundleStrings/int64AsType/copyBuffers/maxSharedStructures(32)`。
- **records、bundleStrings、moreTypes 都是非标准扩展**，Python 的 `msgpack`/`ormsgpack` 无法解码。

### 2.7 从 node_modules 精读的关键点

- **`@vitest/browser-playwright/dist/index.d.ts`**：
  - `PlaywrightProviderOptions{launchOptions,connectOptions,contextOptions,actionTimeout,persistentContext}`。
  - `getCDPSession()` 通过 `page.context().newCDPSession(page)` 实现，所以 browser 测试可以直接用 CDP（例如 `Performance.getMetrics`）。
- **`vitest/dist/chunks/index.*.js` 的 `expandBenchmarksInEntries`**：
  - 为每个 project 生成 `"<name> (bench)"` 项目，`maxWorkers=1`、`maxConcurrency=1`，`testTimeout` 至少 60 s，coverage 关闭，放在单独的 group 里运行。
- **`vite/dist/node/index.d.ts`**：
  - `rollupOptions` 已废弃，改用 `rolldownOptions`。
  - 新增 `resolve.tsconfigPaths`。
  - CLI 的 `--minify` 默认是 `oxc`。
- **`rolldown/.../define-config-*.d.mts`**：
  - `output.codeSplitting: { minSize, groups: [{ name, test }] }`，旧的 `manualChunks` 和 `advancedChunks` 已废弃。
- **`partysocket/dist/ws.js`** 的默认值：
  - `minReconnectionDelay=3000`、`maxReconnectionDelay=10000`、`reconnectionDelayGrowFactor=1.3`、`minUptime=5000`、`connectionTimeout=4000`、`maxRetries=∞`、**`maxEnqueuedMessages=∞`**、**`binaryType='blob'`**。
  - 后两个默认值必须改掉：遥测需要 `'arraybuffer'`，控制命令队列要设上限。
- **`@vitejs/plugin-react/README.md`**：
  - `react({ compiler: true | { compilationMode:'annotation', logDiagnostics } })`。
  - 另一种接法是 Babel 路径的 `reactCompilerPreset`。

---

## 3. 可复用算法与实现（含伪代码 / 参数）

### 3.1 推荐 package.json（精确锁定；已在 trial 中安装、构建、测试）

```jsonc
{
  "type": "module",
  "engines": { "node": ">=22.12.0" },               // Vitest 5 下限；建议升到 22 LTS 最新版或 24
  "dependencies": {
    "react": "19.3.0", "react-dom": "19.3.0",
    "three": "~0.186.1",
    "@react-three/fiber": "9.8.1",
    "@react-three/drei": "10.7.9",                  // 按 r14 的白名单做具名导入
    "zustand": "5.0.15",
    "@tanstack/react-query": "5.104.0",
    "@msgpack/msgpack": "3.1.3",
    "@pmndrs/scheduler": "0.2.0",                   // V0.2 起统一帧循环；需要 fixed-step 时锁定 git commit
    "stats-gl": "4.2.3",
    "@base-ui/react": "1.8.0", "lucide-react": "1.48.0", "cn": "0.4.0",   // 由 shadcn init 引入（见 d04）
    "zod": "4.6.5"
  },
  "devDependencies": {
    "vite": "8.3.1", "@vitejs/plugin-react": "6.1.1",
    "typescript": "7.0.2",
    "tailwindcss": "4.3.3", "@tailwindcss/vite": "4.3.3",
    "shadcn": "4.21.0",
    "@types/react": "19.3.0", "@types/react-dom": "19.3.0", "@types/three": "0.186.0",
    "@types/node": "22",                            // 注意：不锁的话会装到 26.x，与本机 Node 22 不符
    "vitest": "5.0.2", "@vitest/browser-playwright": "5.0.2",
    "playwright": "1.63.0", "@playwright/test": "1.63.0",
    "oxlint": "1.86.0", "oxlint-tsgolint": "7.0.2003",
    "prettier": "3.9.9", "prettier-plugin-tailwindcss": "0.8.1",
    "react-scan": "0.5.7", "msw": "2.15.0"
  },
  "scripts": {
    "dev": "vite", "build": "vite build", "preview": "vite preview --port 4173 --strictPort",
    "typecheck": "tsc -p tsconfig.json",
    "lint": "oxlint --type-aware src tests perf",
    "test": "vitest run", "bench": "vitest bench",
    "perf": "vite build && playwright test"
  }
}
```

实测数据（trial）：

| 项 | 结果 |
|---|---|
| `npm i` 耗时 | 生产依赖 15 s，开发依赖 37 s |
| node_modules 体积 | 318 MB，其中 rolldown 37 MB、oxlint 30 MB、typescript 原生二进制 27 MB、tsgolint 22 MB |
| `tsc` | 1.5 s |
| `vite build` | 1.0–1.2 s |
| `oxlint --type-aware` | 3.2 s |
| `vitest run`（3 个文件，含 browser） | 3.4 s |

### 3.2 tsconfig（TS 7）

```jsonc
{
  "compilerOptions": {
    "target": "es2023", "lib": ["ES2023", "DOM", "DOM.Iterable"],
    "module": "esnext", "moduleResolution": "bundler", "jsx": "react-jsx",
    "strict": true, "noEmit": true, "skipLibCheck": true, "verbatimModuleSyntax": true, "noUnusedLocals": true,
    "rootDir": ".",                                  // TS7：rootDir 默认值改为 ./，建议显式写出
    "types": ["vite/client", "node"],                // TS7：types 默认 []，不写会找不到 import.meta.env
    "paths": { "@/*": ["./src/*"] }                  // TS7：baseUrl 已移除，paths 用相对路径
  },
  "include": ["src", "tests", "perf", "*.config.ts"]
}
```

Worker 文件不要在同一个 program 里混用 `lib: ["WebWorker"]`，它和 DOM 的类型声明会冲突。trial 的做法是把 `self` 断言成一个最小接口（`telemetry.worker.ts`）。如果 Worker 代码量变大，可以单独给它建一个 tsconfig。

### 3.3 vite.config.ts（已实测）

```ts
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { tsconfigPaths: true },
  server:  { headers: COI },          // COI = { 'Cross-Origin-Opener-Policy':'same-origin', 'Cross-Origin-Embedder-Policy':'require-corp' }
  preview: { headers: COI },          // 生产网关（nginx/Caddy/FastAPI StaticFiles）也要加同样的响应头
  worker:  { format: 'es' },
  optimizeDeps: { include: ['@msgpack/msgpack', 'three/webgpu'] },   // 避免 Vitest 首跑时依赖重新优化导致页面 reload
  build: {
    target: 'es2023',
    rolldownOptions: { output: { codeSplitting: { groups: [
      { name: 'three', test: /node_modules[\\/]three[\\/]/ },
      { name: 'react', test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/ },
      { name: 'r3f',   test: /node_modules[\\/]@react-three[\\/]/ },
    ] } } },
  },
})
```

构建产物（gzip 后）：three 348 KB、react 68 KB、r3f 55 KB、应用 27 KB、telemetry worker 50 KB（包含两个 msgpack 实现；正式版只保留一个，约 15 KB）。

### 3.4 渐进加载 + 密度控制：数据布局与 ingest 算法（trial `src/engine/pointCloudLayer.ts`）

**离线预处理**（`prep_city.py`）：
1. 读取 PLY（xyz 加法线，每点 24 B）。
2. 用 `rng.permutation(n)` 随机打散点序，这样**文件的任意前缀都是一个均匀子样本**。
3. 以 bbox 最小角为原点、最大边长为尺度，把坐标量化为 `uint16`：`q = round((p-min)/extent*65535)`。
4. 颜色由高度渐变和兰伯特光照合成，存为 `unorm8x4`。
5. 每点 12 B：`u16 x,y,z,pad | u8 r,g,b,a`。另写一个 `.json` 头：`{count,stride,min,extent,upAxis}`。

5M 点处理 2M 点只需 1.4 s。

**在线 ingest**：

```text
res = fetch(url); reader = res.body.getReader(); carry = []
loop:
  chunk = await reader.read(); if done: break
  buf = concat(carry, chunk); n = floor(len(buf)/12)
  u16 = Uint16Array(buf, 0, n*6)            // stride 为 12，天然 2 字节对齐
  for i in 0..n: pos[(loaded+i)*4 .. +3] = u16[i*6 .. i*6+2], 0
                 col[(loaded+i)*4 .. +3] = buf[i*12+8 .. +10], 255
  dirtyFrom = dirtyFrom<0 ? loaded : dirtyFrom; loaded += n; carry = buf[n*12:]
every frame (update):
  if dirtyFrom>=0:                            // 每帧合并成一次上传：一段 writeBuffer / bufferSubData
     attr.clearUpdateRanges(); attr.addUpdateRange(dirtyFrom*4, (loaded-dirtyFrom)*4); attr.needsUpdate=true
  geometry.setDrawRange(0, min(loaded, floor(budget)))   // 调整密度不需要重新上传
```

- 反量化放进模型矩阵：`points.scale = extent`，`position = R·min`。Z-up 时 `R = rotX(-90°)`，所以 `position = (min.x, min.z, -min.y)`。
- 包围球直接给定 `Sphere((0.5,0.5,0.5), 0.87)`，省掉 `computeBoundingSphere`。
- 实测：首批点可见时间（TTFP）95–850 ms；24 MB 全部载入 0.6–2.8 s（localhost，受负载影响）。主线程上 ingest 与渲染交替进行，没有出现来自自身 JS、超过 50 ms 的长任务（trace 中 `longTasks50` 在负载约 5 时为 0）。
- **正式实现的差异**：八叉树的节点命名和二进制布局按 r09/r12 的结论来做（每个节点一个文件，或 HTTP Range 分块，外加 SSE 选点）。本节的"打散前缀 + drawRange"作为**节点内部**的密度控制手段（与 r11 §3.3 一致），而 `AdaptiveBudget` 输出的是全局点数上限。

### 3.5 自适应 point budget 控制器（AIMD + 迟滞 + 按时间评估；trial `src/engine/adaptiveBudget.ts`）

```text
params: targetMs=16.7, hi=1.2, lo=1.05, up=1.1, downFloor=0.6, alpha=0.2,
        evalEveryMs=250, cooldownMs=1000, min=1e5, max=8e6（按设备档位；detect-gpu 分 tier 后给 max）
sample(dt, now):
  if dt<=0 or dt>5000: return            // 切 tab 或断点导致的暂停，丢弃
  dt = min(dt, 1000)                     // 单次卡顿不能主导 EMA
  ema += alpha*(dt-ema)
  if now-lastEval < evalEveryMs: return  // 按时间而不是按帧数评估，否则 1 fps 时会卡死
  lastEval = now; r = ema/targetMs
  if r > hi:  budget = max(min, floor(budget * clamp(1/r, downFloor, 0.9))); lastDecrease = now   // 快降
  elif r < lo and now-lastDecrease > cooldownMs: budget = min(max, ceil(budget*up))               // 慢升试探
```

- 输入必须是**帧间隔**：R3F `useFrame((s, delta))` 里的 `delta*1000`，或者自己记录的 rAF 时间戳差。**不要调用 `state.clock.getDelta()`**（已实测会拿到约 0）。
- 开了 vsync 时帧间隔不会低于刷新周期，所以 `lo` 必须 ≥1.0：帧率达标就往上试探，一旦掉帧就快速回退。
- 如果有 stats-gl 或 three 的 `info.render.timestamp`（GPU 耗时），可以改用 `max(cpuMs, gpuMs)` 作为 `dt`，这样能看到还剩多少余量，上调也更准。
- 实测（SwiftShader）：控制器约 2 s 内从 1M 降到下限 100k，此后稳定在约 6 fps（负载约 5 时）。单测覆盖了"25 fps 时预算降到一半以下、恢复后回升、永远不低于 min"这几种情况。

### 3.6 性能测量基础设施

**(a) 页内采样器**（`src/perf/metrics.ts`，暴露为 `window.__perf`）：
- rAF 帧间隔写入 `Float64Array(65536)` 环形缓冲。
- 输出 `p50/p95/p99/max/mean/fps`，外加 `jank = #(dt > 2·target)` 和 `stutter50 = #(dt > 50ms)`。
- `PerformanceObserver({type:'long-animation-frame'})` 统计 LoAF 的次数和 `blockingDuration`。
- 页面需要开启 cross-origin isolation，否则计时精度只有 100 µs（§0 第 10 条）。

**(b) Playwright 用例流程**（`perf/viewport.perf.spec.ts`）：

```text
for project in [webgl2, webgpu-sw]:            // 不使用 uncapped 组（§0 第 8 条）
  for scenario in [budget=auto, 150000, 2000000]:
    goto(`/?backend=..&budget=..&n=200&hz=20&codec=raw`)
    waitForFunction(p && p.total>0 && p.loaded===p.total)      // 注意：不能写成 p?.loaded===p?.total，两边都是 undefined 时也成立（已踩坑）
    wait 1500 ms（让控制器预热）
    cdp = context.newCDPSession(page); Performance.enable; m0 = Performance.getMetrics
    __perf.reset(); browser.startTracing(page,{categories:['devtools.timeline','disabled-by-default-devtools.timeline.frame','toplevel']})
    wait SAMPLE_MS(6000); stopTracing; m1 = getMetrics
    digest trace:
      RunTask 事件：Chrome 151 中名为 'ThreadControllerImpl::RunTask'，用 endsWith('RunTask') 匹配
      长任务数 = #(dur > 50ms)；主线程忙碌 = Σdur
      BeginFrame / DrawFrame / DroppedFrame 的计数
    cpu = { scriptMs: ΔScriptDuration, taskMs: ΔTaskDuration, heapMB: JSHeapUsedSize }
    写出 results/<project>-<scenario>.json；断言：没有 pageerror（忽略 404 与 "WebGPU is not available"）
```

**(c) CI 门禁建议**（SwiftShader 下只看相对量和行为）：
- 渲染没有报错；两个后端都能出图（WebGPU 的截图可能是空白，见 r11/r14，所以像素检查只做 WebGL2）。
- `budget=auto` 时，3 s 内 `budget ≤ 初始值`，而且 `drawn ≤ budget`。
- 来自我们自己 JS 的长任务数为 0：只统计 `FunctionCall` 且脚本 URL 属于 `/assets/index-*` 的部分。
- 遥测解码均值 ≤ 50 µs/条，需在 COI 下测量。
- `JSHeapUsedSize` 与基线相比增长不超过 20%。
- 真 GPU runner 另设绝对门禁：1080p、500 万点、200 架无人机时 p95 ≤ 20 ms。

实测汇总（`trial/perf/results_nocoi_load5/`，负载约 5）：

| 项目 / 预算 | 后端 | FPS | p50 ms | p95 ms | 实际 budget | TTFP ms | 载入 ms | DroppedFrame / BeginFrame |
|---|---|---|---|---|---|---|---|---|
| webgl2 / auto | webgl2 | 5.6 | 176 | 367 | 100,000（收敛到下限） | 643 | 1348 | 318/352 |
| webgl2 / 150k | webgl2 | 5.8 | 165 | 378 | 150,000 | 95 | 738 | 325/360 |
| webgl2 / 2M | webgl2 | 0.7 | 1541 | 2785 | 2,000,000 | 323 | 1162 | 95/97 |
| webgpu-sw / auto | webgpu | 6.6 | 141 | 262 | 100,000 | 322 | 631 | 327/367 |
| webgpu-sw / 150k | webgpu | 5.5 | 167 | 342 | 150,000 | 287 | 1230 | 328/362 |
| webgpu-sw / 2M | webgpu | 0.5 | 1896 | 2421 | 2,000,000 | 849 | 1217 | 170/172 |
| webgl2-uncapped / 150k | webgl2 | 30.6（假象） | 2.9 | 10.3 | 150,000 | 1201 | 1860 | 0/1279 |

另有一组在负载约 14 时开启 COI 的结果（`results_coi_load14/`）：FPS 降到 2.4–2.9，主线程忙碌约 5.8 s / 6 s。原因是 SwiftShader 在 GPU 进程里用满了 CPU，主线程在同步 IPC 上等待；**COI 本身并不会拖慢渲染**。

**(d) Vitest bench**：`tests/bench/codec.bench.ts`，同一份代码分别在 `unit (bench)`（Node）和 `browser (chromium) (bench)` 两个项目里运行，调用 `bench.compare(...)` 与 `expect(r.get('raw')).toBeFasterThan(r.get('msgpack'))`。可以作为协议层的性能回归门禁。

### 3.7 帧循环：scheduler phase 映射与 v9 桥接（trial `SchedulerDriver`）

| phase | 本项目任务 | 频率 |
|---|---|---|
| `input` | 相机控制（CameraControls）、键鼠与手柄 | 每帧 |
| `physics`（fixed 1/60） | 前端本地的 mock 积分（离线演示用）；平时只推进"插值时钟" | 固定步长 |
| `update` | 遥测插值（SoA prev/cur）、点云 LOD 与 budget、环境粒子、标签布局 | 每帧；LOD 可设 `fps:15` |
| `render` | `advance(time)`，由 R3F 渲染；也可以改为自定义 `RenderPipeline`（EDL） | 每帧 |
| `finish` | `StatsProfiler.update()`；`hud.setState(summary)` | `fps:4` |

```ts
// v9：Canvas 设 frameloop="never"，由 scheduler 驱动（已实测可用）
const s = getScheduler()
s.register((st) => advance(st.time), { id: 'r3f-render', phase: 'render' })
s.register(() => hud.setState(summary()), { id: 'ui-summary', phase: 'finish', fps: 4 })
```

**fixed timestep 算法**（从 `Scheduler.advanceClock` 移植；0.2.0 发布前可以自己实现）：

```text
acc += delta
pending = floor((acc + 1e-9) / h)
if pending > maxSub (8): pending = maxSub; acc = acc mod h      // 丢弃超出的时间，避免死亡螺旋
acc -= pending*h
repeat pending: step(h)                                        // 同一 phase 内的多个 job 交替执行：A B, A B
overstep = clamp(acc/h, 0, 0.999999)
render_state = lerp(prev_state, cur_state, overstep)
```

### 3.8 遥测链路（前端部分）

```text
Worker(ws)：用 partysocket 风格重连（minDelay=500ms, grow=1.5, max=10s, connectionTimeout=4s,
            maxEnqueuedMessages=32 且只保留控制命令），binaryType='arraybuffer'
  onmessage(ab)：读头部 u8 encoding → raw 帧 postMessage(ab, [ab])（Transferable，零拷贝）
                 → msgpack 帧 Decoder.decode → postMessage(obj)（低频）
Main：DroneLayer.onmessage → 交换 prev/cur 两个 SoA → cur.set(Float32Array(ab,16,n*F))
      tPrev=tCur, tCur=now
update(now)：w = clamp((now - tCur)/(tCur - tPrev) + 1 - 1, 0, 1)
            → 按 w 在 prev/cur 之间插值（渲染滞后一个包的间隔）
            → 写 instanceMatrix；needsUpdate=true
V0.3：COI 开启后改为 SharedArrayBuffer 环形缓冲（Worker 写，主线程每帧读 seq 最新的槽），省掉 postMessage
```

raw 帧头为 `u32 seq | u32 n | f64 t`，后面跟 `n×F` 个 f32（小端）。完整的信封与通道设计以 r27 为准（`layouts.json` 同时生成 numpy dtype 和 TS 访问器）。trial 中 20 Hz、200 架的主线程解码均值实测为 22–60 µs/条，这是在负载很高、主线程被 SwiftShader 抢占 CPU 时的数字；bench 里纯解码只需 1.5 µs。

### 3.9 状态分层（落到代码）

```text
engine SoA（Float32Array：点云 attribute、无人机 prev/cur、粒子）      ← 每帧读写，不经过 React
   │ 4 Hz（scheduler 的 fps:4 任务）
zustand vanilla store（hud：fps/p95/budget/loaded/decode/告警摘要；selection；ui prefs）
   │ useStore(store, selector) + useShallow
React（shadcn 面板、lieflat 图表）
TanStack Query（REST：/api/scenes、/api/missions、/api/config；queryOptions() 工厂；staleTime 30 s）
   └─ WS 低频事件（mission_state 等）→ queryClient.setQueryData 就地更新缓存，避免重复请求
```

### 3.10 lint 规则（oxlint，已实测）

```jsonc
{
  "plugins": ["typescript", "react", "import"],
  "categories": { "correctness": "error" },
  "ignorePatterns": ["dist/**", "node_modules/**", "perf/results*/**"],   // 没有 .gitignore 时 oxlint 会去扫 dist 和 node_modules（踩过坑，耗时 98 s）
  "rules": { "typescript/no-floating-promises": "error", "react/rules-of-hooks": "error" },
  "overrides": [{ "files": ["src/engine/**", "src/net/**"],
    "rules": { "no-restricted-imports": ["error", { "patterns": [{ "group": ["react", "react-dom", "@react-three/*", "zustand"],
      "message": "engine/ and net/ must stay framework-free" }] }] } }]
}
```

---

## 4. 在本项目中的落点与复用方式

| 项 | 用途 | 落点（模块） | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|
| Vite 8 + plugin-react 6 | 构建与开发服务器 | `apps/web` | V0.1 | adopt | 2026 年的主流方案，Rolldown 构建约 1 s |
| TS 7 `tsc` | 类型检查 | 全仓库 | V0.1 | adopt | 速度快 8–12 倍；没有 API 的问题靠 oxlint 绕开 |
| `@typescript/typescript6` 别名 | 给需要 TS API 的工具用 | devDependencies | 按需 | adopt（备用） | 官方迁移方案 |
| Tailwind 4.3 | 样式 | `apps/web/src/index.css`（`@theme`） | V0.1 | adopt | shadcn 基于它；可用 `scrollbar-thin`、`scrollbar-thumb-*` 统一深色滚动条 |
| React 19.3 `<ViewTransition>` | 面板或页面切换的动画 | UI 壳层 | V0.2 | reference | 与 transitions.dev（d02）重叠；只用于路由级切换，组件级仍以 d02 方案为准 |
| R3F 9.8.1 | 3D 宿主 | `viewport/` | V0.1 | adopt | 见 r14 |
| R3F v10 / drei v11 | WebGPU 原生入口 | `viewport/` | V0.3+ | reference | React 19.3 的 peer 冲突 |
| `three` → `three/webgpu` 别名 | 包体减少 54 KB（gzip） | vite.config | V0.2 | port（可选） | 前提是始终传 `gl` 工厂 |
| @pmndrs/scheduler | 统一帧循环、限频、fixed step | `engine/loop.ts` | V0.2 | adopt + port（fixed step） | 与 R3F v10 共用同一内核 |
| AdaptiveBudget | 点云密度自适应 | `engine/pointcloud/budget.ts` | V0.1 | port（本文 §3.5） | 满足用户"疏密自动调节"的要求 |
| 前缀打散 + drawRange + 流式 ingest | 渐进加载 | `engine/pointcloud/` | V0.1 | port（本文 §3.4） | 零重传即可调节密度；TTFP 小于 1 s |
| stats-gl `StatsProfiler` | FPS、CPU、GPU 时间 | `engine/perf/` → HUD | V0.1 | adopt | 提供数据，界面自己按 lieflat 风格绘制 |
| FrameSampler + LoAF | 页内帧统计与 perf API | `engine/perf/` | V0.1 | port | Playwright 通过 `window.__perf` 读取 |
| zustand 5 | UI 摘要态 | `stores/` | V0.1 | adopt | 与 R3F 一致 |
| TanStack Query 5 | REST 服务端状态 | `net/api.ts` | V0.1 | adopt | 缓存、重试、失效由它统一处理 |
| raw struct 编解码 | 高频遥测 | `net/codec/raw.ts` | V0.1 | port（r27 加本文 §3.8） | 比 msgpack 快约 450 倍 |
| @msgpack/msgpack | 低频异构消息 | `net/codec/msgpack.ts` | V0.1 | adopt | 与 Python 互通 |
| partysocket 重连逻辑 | WS 客户端 | `net/ws.worker.ts` | V0.1 | port | 约 60 行，要改掉默认值 |
| Vitest 5（unit / browser / bench） | 单元、渲染冒烟、协议基准 | `tests/` | V0.1 | adopt | 同一份 bench 同时跑 Node 和 Chromium |
| Playwright 1.63 perf | 流畅性测试 | `perf/` | V0.1 | adopt | 用户要求做"流畅性测试" |
| oxlint + tsgolint | lint 与架构边界 | 全仓库 | V0.1 | adopt | 兼容 TS 7；边界规则已实测 |
| react-scan | 发现重渲染风暴 | 仅开发环境 | V0.1 | adopt | 用来保证高频数据不进 React |
| koota | ECS 实体、关系模型 | `agent/` 或多机 | V0.6 | reference | 适合任务和编队关系，不适合放在 GPU 热路径 |
| msw | REST mock | `tests/`，也用于离线演示 | V0.2 | adopt（可选） | 后端没就绪时可以先演示 UI |
| detect-gpu | 设备分档，决定初始 budget 和 `max` | `engine/perf/tier.ts` | V0.2 | reference | 分档表需要联网 CDN，要改成本地 JSON |

---

## 5. 对比与推荐

### 5.1 构建与语言
- **Vite 8（adopt）**：Rolldown 已经合并进 Vite 本体，rolldown-vite 7.3.1 作为过渡包已经过时。Next.js 16 对一个纯 SPA 加 WebGPU 沙盘来说太重了（shadcn 官网用的是 Next，但我们不需要 SSR）。
- **TS 7（adopt 为 tsc）而不是 TS 6**：大型 monorepo 的类型检查快 8–12 倍。短板是没有 API，影响 typescript-eslint、Vue/Svelte 模板和 ts-morph；本项目不依赖这些，或者可以用 oxlint 替代。**TS 7.1 预计补上 API，届时再重新评估 typescript-eslint。**

### 5.2 Lint / 格式化
| 方案 | 与 TS 7 的兼容性 | type-aware | 速度（trial） | 结论 |
|---|---|---|---|---|
| **oxlint 1.86 + tsgolint** | 兼容，基于 typescript-go | 支持（`no-floating-promises`、`unbound-method` 等） | 3.2 s | **adopt** |
| ESLint 10.11 + typescript-eslint 8.70 | **不兼容**，peer 要求 `<6.1`，实测 ERESOLVE | 支持 | 慢 | 除非走 TS 6 别名，否则 skip |
| Biome 2.5.14 | 与 TS 无关 | 有限 | 快 | 备选格式化工具 |
| Prettier 3.9.9 + tailwind 插件 0.8.1 | — | — | — | **adopt**，负责格式化与 class 排序 |

### 5.3 状态管理
- **zustand 5（adopt）**：R3F 本身也用它，vanilla store 可以在引擎侧读写。
- jotai 3.0（2026-09-08 刚发布大版本）和 valtio 2.3：同一职责没必要引入第二套，skip。
- koota：见 §2.3，reference。

### 5.4 序列化（与 r27 的差异说明）
r27 当时建议浏览器端用 msgpackr。本单元在**真实 Chromium（开启 JIT）里、用我们自己的 DroneState 对象形态**测试，结果是 msgpackr（不开 records）与 @msgpack/msgpack 基本持平（817 µs 对 849 µs），在 Node 下 msgpackr 甚至更慢（1049 µs 对 794 µs）。开启 records 的 msgpackr 快 1.6–2 倍，但 Python 端无法解码 records。**因此修正为：控制面默认用 @msgpack/msgpack（它有 TS 类型和 `decodeMulti/decodeAsync`，而且与 Python 互通）；只有在"浏览器到浏览器"或 Node 到浏览器的场景下，才考虑 msgpackr 的 records。** 热路径两份笔记的结论相同，都是 raw struct。

### 5.5 路由
V0.1 只有一个沙盘页，视图切换用状态驱动，不引入路由库。V0.4 如果需要 `/world/:id?cam=…&t=…` 这类深链接（相机和时间轴状态写进 URL），用 **TanStack Router 1.170**（search params 有类型、有校验，可配合 zod）。React Router 8.4 可以作为备选。

### 5.6 节流与调度
scheduler 的 `fps` 任务（adopt）优于 TanStack Pacer：Pacer 每次调用都会更新 store，而且仍是 beta。Pacer 更适合搜索框防抖一类的输入场景，这类需求 10 行代码就能写完，所以 skip。

### 5.7 性能工具
- **stats-gl 的 StatsProfiler（adopt）**：支持 WebGPU timestamp。r3f-perf 2024-11 后停更，只支持 WebGL，skip。
- drei 的 `<StatsGl>` 只在开发期使用。
- react-scan 只在开发期使用。
- @paulirish/trace_engine 留到 V0.3，需要逐帧归因分析时再引入。

---

## 6. 风险与注意事项

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| 1 | R3F v10 / drei v11 alpha 要求 `react <19.3` | 升级 React 或 R3F 时 ERESOLVE | 锁定 React 19.3.0 + R3F 9.8.1；想试 v10 就单独开分支，并把 React 降到 19.2.x；**不要用 `--legacy-peer-deps` 硬装** |
| 2 | TS 7 没有编程 API | typescript-eslint、ts-morph、部分插件无法使用 | 用 oxlint + tsgolint；需要时走 `@typescript/typescript6` 别名；等 7.1 |
| 3 | TS 7 默认值变化（`types:[]`、rootDir、strict） | 迁移时报错一大片 | 按 §3.2 显式写出 |
| 4 | `@types/node` 不锁版本会装到 26.x | 类型与 Node 22 运行时不一致 | 锁 `@types/node@22` |
| 5 | Node 22.12.0 正好是 Vitest 5 的下限 | 本机一升级就可能出问题；CI 镜像可能更旧 | `engines.node >=22.12`；CI 用 22 LTS 最新版或 24 |
| 6 | Playwright 1.63 需要 chromium rev 1243，本机只有 1234 | 默认启动失败（`Executable doesn't exist`） | 用 `executablePath` 或 `PW_CHROME`；联网环境执行 `npx playwright install chromium`；Playwright 与浏览器缓存版本一起锁 |
| 7 | SwiftShader 下的 FPS 不代表真实 GPU | 误判性能 | CI 只做行为门禁（§3.6c）；绝对门禁放在真 GPU runner |
| 8 | `--disable-gpu-vsync --disable-frame-rate-limit` | rAF 与呈现脱钩，数据是假象，用例还会变慢 10 倍以上 | 帧指标测量禁用这组标志 |
| 9 | headless 下 WebGPU canvas 截图是空白（r11/r14） | 无法做像素验收 | 像素检查只在 WebGL2 做；WebGPU 只检查报错和 drawCalls |
| 10 | 计时精度 100 µs | µs 级指标不可信 | 开启 COOP/COEP |
| 11 | COEP `require-corp` 会拦截跨源资源（地图瓦片、CDN 字体、外链图片） | 资源加载失败 | 所有资源同源或加 CORP/CORS；也可以改用 `Cross-Origin-Embedder-Policy: credentialless`（Chrome 96+）；字体用 `@fontsource-variable` 本地打包（见 d04） |
| 12 | R3F 中 `state.clock.getDelta()` 返回约 0 | 自适应控制失效 | 只用 `useFrame` 的 `delta` 参数 |
| 13 | three r186 的 `dispose()` 是异步的 | 资源释放顺序出错；lint 报 floating promise | `await renderer.dispose()`；卸载流程写成 async |
| 14 | StrictMode 下 effect 在开发期执行两次 | 创建两个 Worker、WS 或渲染器 | effect 里必须有对称的 cleanup（trial 在 cleanup 里 `terminate()`） |
| 15 | msgpackr records、`bundleStrings`、`moreTypes` 与 Python 不互通 | 解码失败 | 跨语言通道只用标准 msgpack |
| 16 | @msgpack/msgpack 解出的 bin 是零拷贝视图，可能没对齐 | `new Float32Array` 抛 RangeError | 先 `slice()`，或服务端对齐；大块数组走 raw 帧 |
| 17 | partysocket 默认 `binaryType='blob'`、队列无上限 | 解码要多走一步；断线期间命令无限堆积 | 设为 `'arraybuffer'`；控制命令队列设上限并加 TTL |
| 18 | Vite 8 安装体积增加约 15 MB，还带原生二进制（rolldown、lightningcss、oxide、tsgolint） | 离线或跨平台（arm64）安装会失败 | 准备 npm 离线缓存；lockfile 要覆盖目标平台的 optionalDependencies |
| 19 | Vitest browser 首跑时依赖重新优化，引发 reload | 测试不稳定 | `optimizeDeps.include` 预声明 |
| 20 | `oxc-transform-react` 的 peer 锁在 `^0.145.0` | 装 latest（0.151）会 ERESOLVE | 用 React Compiler 时锁 0.145.x；V0.1 先不启用 Compiler |
| 21 | pmndrs/scheduler 的 fixed timestep 还没发布；8 stars，是新仓库 | 用 git 依赖不稳定 | V0.2 只用 phase 和 fps 功能；fixed step 按 §3.7 自己实现约 20 行，等 0.3 发布后再切换 |

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§34 前端技术栈表要写明版本，并补齐工程要素。** 当前表中只列了框架名。建议改成下表，并附上 §3.1 的 package.json 作为"版本基线"：

   | 模块 | 选型（2026-09 基线） |
   |---|---|
   | Build | **Vite 8.3（Rolldown/Oxc）** |
   | Language | **TypeScript 7.0（tsc），lint 用 oxlint + tsgolint** |
   | UI | React 19.3 + shadcn 4.21（base-mira）+ Tailwind 4.3 |
   | 3D | three r186（`three/webgpu`，自动回退 WebGL2）+ R3F 9.8（宿主）→ v10（V0.3+） |
   | **Shader** | **TSL（同时编译到 WGSL 和 GLSL）**。原表写的"WGSL"不对：手写 WGSL 会丢掉 WebGL2 回退 |
   | Frame loop | **@pmndrs/scheduler**（phase、fps、fixed step） |
   | State | zustand 5（UI 摘要）+ 引擎 SoA（热数据） |
   | Server state | **TanStack Query 5** |
   | Realtime | WebSocket 放在 Worker 里；**raw struct（高频）+ msgpack（低频）** |
   | Perf | **stats-gl StatsProfiler + FrameSampler/LoAF** |
   | Test | **Vitest 5（unit/browser/bench）+ Playwright 1.63（E2E/perf）** |

2. **§12 WebGPU 的定位要补一句："WebGL2 回退是一等公民"。** 在无 GPU 的 headless 或 CI 环境里，默认 adapter 是 null，会自动走 WebGL2；只有加上 SwiftShader 标志才有软件 WebGPU。另外很多用户的浏览器或驱动也会落到 WebGL2。所有渲染特性（点大小、compute）都要有 WebGL2 路径（见 r11）。
3. **§36 实时通信要补充：**
   - WebSocket 放在 Dedicated Worker 里，主线程只接收 Transferable 或 SAB。
   - 二进制优先：raw struct 加 `layouts.json`（见 r27）。
   - REST 由 TanStack Query 管理。
   - 部署时需要 **COOP/COEP 响应头**（计时精度与 SAB）。
   - 重连参数：`minDelay` 500 ms，`grow` 1.5，`max` 10 s，控制命令队列设上限。
4. **§37 刷新频率要多加一层"UI 刷新 4–10 Hz"**，并写明"渲染比最新包滞后一个包间隔（20 Hz 时约 50 ms），用来做插值"。60 FPS 的目标要注明**参考硬件**（例如 RTX 3060 或 M2 集显、1080p），并说明 CI 的 SwiftShader 环境只做行为验收。
5. **§43/§44（MVP 与 V0.1）要把用户硬性要求写进验收标准：**
   - 渐进加载：TTFP < 1 s（本地），加载期间主线程没有来自自身 JS、超过 50 ms 的长任务。
   - 疏密自动调节：budget 控制器在 2 s 内收敛，`drawn ≤ budget`。
   - 流畅性测试：Playwright perf 套件覆盖 WebGL2 和 WebGPU-SW 两个 project，外加一个真 GPU 手动基准，结果写入 `perf/results/*.json` 并在 HUD 里回放。

   这三项可以直接复用本单元的 trial 代码结构。
6. **§42 Repo 结构要细化 `apps/web` 的分层，并加上工程目录：**

   ```text
   apps/web/src/{ui,viewport,engine,net,stores,perf}
   apps/web/{tests/unit,tests/browser,tests/bench,perf}
   packages/protocol/（layouts.json → TS/py codegen）
   packages/design-tokens/（ANet Graphite）
   ```

   用 oxlint 的 `no-restricted-imports` 守住 `engine/` 和 `net/` 不依赖 React 的边界。monorepo 用 pnpm workspace（shadcn 官方仓库也是 pnpm + turbo）。
7. **新增一章"前端工程规范与依赖治理"：**
   - 精确锁版本（`npm i -E`），提交 lockfile。
   - 列出 peer 矩阵：React、R3F、drei、three 的兼容区间。
   - three 大约每 1–3 个月出一个 minor，每次单独开一个 PR 升级并跑 perf 套件。
   - Playwright 与浏览器缓存版本一起锁。
   - 准备离线 npm 缓存和原生二进制（rolldown、oxide、tsgolint 以及目标平台的包）。
   - 把 TS 7.1、R3F v10 stable 设为"升级触发点"。
8. **§16 的 Three.js 场景结构建议补上"帧序契约"**：input → physics(fixed) → update（遥测插值、LOD、环境）→ render → finish（stats、HUD）。按 scheduler 的 phase 命名，这样和 R3F v10 的语义一致，以后迁移时零改动。
9. **§38 UI 设计要注明**：React 19.3 的 `<ViewTransition>` 只用于路由或页面级切换；组件级动效按 d02（transitions.dev）执行；HUD 数据全部来自 zustand 的 4 Hz 摘要。

---

### 附：本单元产物清单

| 路径 | 内容 |
|---|---|
| `.cache/research/n05/trial/` | 完整可运行的 trial 工程：`src/engine/{adaptiveBudget,pointCloudLayer,droneLayer}.ts`、`src/net/{codec,telemetry.worker}.ts`、`src/perf/metrics.ts`、`src/Viewport.tsx`（R3F 加 scheduler 桥接）、`vitest.config.ts`、`playwright.config.ts`、`perf/viewport.perf.spec.ts`、`perf/loopcheck.mjs`、`.oxlintrc.json` |
| `.cache/research/n05/trial/perf/results_nocoi_load5/`、`results_coi_load14/` | 性能 JSON 与 trace 事件名统计 |
| `.cache/research/n05/trial/perf/shot-scheduler.png` | scheduler 驱动下的渲染截图（10 万点加红色无人机加 HUD） |
| `.cache/research/n05/{versions_tags.txt,peers.txt,stars_core.txt,stars_disc.txt,prep_city.py}` | 版本、peer、star 的原始记录与预处理脚本 |
| `refs/discovery/{scheduler,stats-gl,koota,pacer,msgpack-javascript,msgpackr}` | 本单元新克隆的只读仓库 |
