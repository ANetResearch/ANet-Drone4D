# navsvc：NavMesh 烘焙与 Detour 查询服务

本目录为 M22 navmesh 构建阶段与 M23 事件编译器共用的 Node 工具（ADR-196），使用 recast-navigation 0.43.1 的 WebAssembly 构建。

## 组成

| 文件 | 用途 |
|---|---|
| `src/bake.mjs` | 分瓦片烘焙：逐三角形 Detour 区域号、worker_threads 并行、种子洪泛裁剪，输出 `exportNavMesh` 字节与统计 |
| `src/navsvc.mjs` | 常驻查询服务：标准输入输出上的行分隔 JSON，`snap`、`path`、`los`、`random_point_around`、`random_point`、`height`、`batch` |
| `src/core.mjs` | 同构查询核心（不引用 Node 内置模块），供 navsvc 与后续浏览器事件编辑器加载同一 WASM 与 NavMesh 二进制 |
| `src/tiles.mjs` | 瓦片划分与单瓦片 Recast 流程 |
| `src/recast.mjs` | WASM 装载与版本核对 |
| `WASM_SHA256` | `recast-navigation.wasm.wasm` 的锁定 sha256（单线程 MVP 二进制，不含 SIMD 与 relaxed SIMD 指令） |

版本锁：三个 `@recast-navigation/*` 包精确锁定为 `tools/worldglb/RECAST_VERSION`；版本或 WASM sha256 不符时烘焙与查询均拒绝启动。

## 安装与自测

```sh
npm ci --prefix tools/navsvc
node --test tools/navsvc/test/*.test.mjs
```

Python 侧入口为 `awr.world.glb.derive.navmesh`：`write_navmesh` 在构建流水线中调用 `bake.mjs`，`NavSvc` 以子进程启动 `navsvc.mjs`。

## 坐标与确定性

- 对外坐标为世界帧 `[E, N, U]` 整数毫米；Recast 轴向 `(E, U, −N)` 的换算只在 `core.mjs` 内进行。
- 瓦片按 `(ty, tx)` 行主序装配，烘焙结果与线程数、调度次序和主机指令集无关；同一 NavMesh 与同一请求序列的应答逐字节相同。
- 耗时、线程数、RSS 与 Node 版本只写入统计输出与 `qa/report.json`，不进入图层清单与 `contentVersion`。
