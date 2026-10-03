# R09 研究笔记：PotreeConverter / PDAL / LAStools：离线点云切片与 World Package 管道

> 研究单元：r09 ｜ 日期：2026-09-28 ｜ 对应设计：`docs/01-design.md` §6、§14–15、§33、§41、§43–44
> 仓库快照：
> - `refs/world/PotreeConverter` @ `c2cb618`（2026-09-23，818 stars，BSD-2，C++23）
> - `refs/world/PDAL` @ `0e132bd`（2026-09-21，1416 stars，BSD，`project(PDAL VERSION 2.10.0)`）
> - `refs/world/LAStools` @ `a6a88da`（2026-09-21，1070 stars，LASlib/LASzip 与部分工具为 LGPL，其余工具闭源）
>
> 本文路径均相对各自仓库根目录。所有结论来自源码精读，另附本机实测，未经实测的地方会标"未验证"。实测脚本放在 `/data/projs/anet-drone/.cache/research/`：
> - `r09_octree_fast.py`：numpy 八叉树
> - `r09_potree2_writer.py`：Potree 2.0 writer
> - `r09_sampling_quality.py`：采样质量对比
> - `r09_range_server.py`：支持 Range 的静态服务器
> - `r09_cdp_shot.mjs`：headless Chromium 驱动
>
> 截图为 `r09_potree_newyork*.png`。

---

## 0. 结论速览

| 仓库 | 定位 | 复用方式 | 落点版本 | 推荐度 |
|---|---|---|---|---|
| **PotreeConverter 2.x** | 把 LAS/LAZ 转成可流式加载的加法式（additive）八叉树 LOD。输出 3 个文件：`metadata.json`、`hierarchy.bin`、`octree.bin` | **port**：计数排序分块、网格/Poisson 抽稀、节点命名、22 B 层级记录、proxy 分页、BFS 字节布局，移植为 Python/numpy 版 `worldpkg tile`。**reference**：BROTLI 的 Morton SoA 编码、读端 NodeLoader 与 LOD 遍历。**adopt（可选）**：超过 1 亿点时通过 Docker 调用原版 | V0.1（Web 点云 MVP）→ V0.5（大规模 LiDAR） | 5/5（算法与格式的事实标准） |
| **PDAL 2.10** | 点云 ETL 管道。JSON pipeline 串起 Reader、Filter、Writer，支持流式模式 | **adopt（Docker/conda）**：<br>• 坐标：`filters.reprojection`/`filters.projpipeline`（WGS84<->UTM<->ENU）、`filters.georeference`（轨迹直接地理配准）<br>• 清洗：`filters.outlier`/`filters.elm`<br>• 地面：`filters.smrf`/`filters.csf`<br>• 地形：`writers.gdal`（DEM）、`filters.hag_dem`<br>• 着色：`filters.colorization`<br>• 互操作：`writers.copc`<br>**port**：`filters.sample`（体素哈希 Poisson）、`LocalCartesian`（topocentric ENU） | V0.1 只用 numpy 替代实现；V0.5 起正式进入 World Package 工具链 | 4/5（地理与地形链路首选） |
| **LAStools** | LAS/LAZ 的参考实现（LASzip），外加一组高效命令行工具 | **adopt（小范围）**：laszip、lasinfo、las2las、lascopcindex（开源部分），用于 QA 和互操作。**reference**：lascopcindex 的单遍流式 COPC 构建（shuffle、swap 概率、finalizer）。**skip**：lasground、lasthin、blast2dem、laspublish 等闭源工具 | V0.5（QA、互操作） | 2.5/5（可替代性高） |

**关键结论（实现者先读这几条）：**

1. **PotreeConverter 是最好的"算法 + 格式"参考，但不适合直接当依赖。**
   - 输入只接受 `.las/.laz`：`Converter/src/main.cpp::curateSources` 按扩展名过滤。
   - 当前 master 使用 C++23 的 `<print>`，需要 GCC ≥ 14。本机实测：GCC 13.3 编译 `#include <print>` 直接报 `No such file`，本机也没有 CMake 和 TBB。
   - 结论：**移植为 numpy 实现**，原版只作为 Docker 化的备选。
2. **numpy 原型已在本机验证通过。**
   - 5M 点建树耗时 5.0–6.7 s（单线程）。
   - 输出的 Potree 2.0（DEFAULT 和 BROTLI 两种编码）都被 PotreeConverter 自带的 **Potree 1.8 viewer** 在 headless Chromium（SwiftShader）中正确加载：27 个可见节点，230,504 点，预算 300k，无报错。这说明 §3.1 的二进制规范是对的。
3. **格式建议：保留 Potree 2.0 的"三文件容器"，新增 `ANET_Q16` 节点编码。**
   - 容器部分完全兼容：`metadata.json`、`hierarchy.bin`（22 B 记录、BFS、proxy）、`octree.bin`（HTTP Range 读取）。
   - `ANET_Q16` 负载为节点局部 `unorm16x4` 位置（xyz + oct16 法线）加 `unorm8x4` 颜色，**12 B/点**。
   - 下载后零解码，直接作为 GPU attribute；x4 格式同时满足 WebGPU 的顶点格式约束。
   - 节点内点序随机，所以可以只渲染一个节点的前缀（fractional draw），用于平滑调节疏密。
   - DEFAULT 编码保留，用来和官方 viewer 交叉验证。
4. **Potree 的 LOD 是加法式的，每个点只存一次。** 父节点从子节点里"拿走"代表点，渲染时把"父 + 子"叠加。我们用 **top-down 网格竞选 + 中心优先**实现同样的语义：一次 Morton 排序，之后每层做一次 `reduceat`，复杂度 O(N log N + N·depth)。
   - 实测 L≤2 的"近邻过近点"（NN < 0.25 cell）比例从随机选取的 14.5% 降到 3.8%，粗层级更均匀。
5. **octree.bin 按 BFS 顺序写，整城概览可以一次 Range 取回。** 实测 New York 的前 **3.27 MB（DEFAULT）/ 1.86 MB（BROTLI）** 就包含 L0–L2 的 181,872 点。
6. **UrbanScene3D 数据必须先做归一化（本机实测）：**
   - 6 个 `*_sampled_5m.ply` **都没有 RGB**，只有 xyz 和法线（float32）。
   - **Suzhou 是 Y-up**：y 轴法线正向占比 0.86，y 范围只有 155。
   - **Chicago 的单位疑似 km**：包围盒 8.0×4.2×0.6；z_max≈0.5 对应 Willis Tower 的 442/527 m。
   - 入库时需要一个 `ingest` 步骤处理 up-axis、单位、伪彩色、法线旋转。
7. **PDAL 是 World Package 地理与地形链路的最佳工具**（reprojection/ENU、地面分类、DEM、HAG、着色、COPC）。但 PyPI 没有 `pdal` 二进制 wheel（本机 `pip download --only-binary` 实测失败），本机也没有 conda，只有 docker 29.4 可用。因此：
   - **V0.1 不依赖 PDAL**，ENU/UTM 用 numpy 加 `pyproj`（有 wheel，3.8.0）。
   - **V0.5 起**用 `pdal/pdal` Docker 镜像，或 pixi/conda 环境。
8. **LAStools 只取开源部分**，用于 QA 和互操作。它的闭源工具（地面、抽稀、DEM、发布）全部用 PDAL 或自研替代。LAZ 读写在 Python 侧用 `laspy` + `lazrs`（`laspy` 2.7.0 已在 venv；`lazrs` 有 0.8.2 wheel）。

---

## 1. 仓库概览

| 项 | PotreeConverter | PDAL | LAStools |
|---|---|---|---|
| 最后提交 | 2026-09-23（`fix NIR, which should be stored in rgb[3]`） | 2026-09-21（zstd 依赖检测） | 2026-09-21（LASzip README） |
| 语言与规模 | C++23，核心约 11k 行（`Converter/src` + `include`）。附带 `resources/page_template`，即 Potree 1.8 viewer | C++17，约 1024 个 .cpp/.hpp。另有 `plugins/`：arrow、draco、e57、spz、trajectory、tiledb 等 | C++17，LASlib + LASzip 约 65k 行，`src/` 下是开源工具 |
| 核心依赖 | 内置 laszip（`Converter/libs/laszip`）、brotli、nlohmann json；Linux 需要 TBB 和 Threads | GDAL/PROJ（必需）、Eigen、nlohmann、lazperf；插件各有依赖 | 自包含。PROJ 以动态加载方式使用（`src/proj_loader.cpp`） |
| 构建 | CMake ≥ 3.16，`CMAKE_CXX_STANDARD 23`。**本机不可编译**：GCC 13 缺 `<print>`，且无 CMake、无 TBB | pixi（`pixi.toml`，conda-forge）或 CMake + conda。PyPI 无二进制 wheel | CMake ≥ 3.10，C++17。本机 GCC 13 理论上可编，缺 CMake 时可 `pip install cmake` 装进 venv（未验证） |
| 输出/能力 | Potree 2.0 三文件；`--encoding BROTLI`；`-m poisson/random`；`--stage CHUNKING/INDEXING/MERGING` 可断点续跑。注意 README 写"compression 尚不可用"，这句已过时 | 100+ 个 stage，覆盖读写、重投影、滤波、分类、栅格化、COPC 等 | laszip、las2las、lasinfo、lasindex（.lax）、lascopcindex（COPC）、txt2las、lasmerge |
| 与本项目的关系 | Web 点云 LOD 的格式与算法源头 | World Package 的地理与地形工具链 | LAZ 规范参考与 QA 工具 |

---

## 2. 源码结构与关键模块

### 2.1 PotreeConverter 2.x

#### 2.1.1 目录与入口

```text
Converter/src/main.cpp                    入口：parseArguments → curateSources → computeOutputAttributes → computeStats → chunking/indexing/merging
Converter/src/chunker_countsort_laszip.cpp  CHUNKING：countPointsInCells → createLUT → distributePoints → writeMetadata(chunks/metadata.json)
Converter/src/indexer.cpp                 INDEXING/MERGING：getChunks、buildHierarchy(32³ 计数金字塔)、doIndexing、doMerging、createHierarchy、createMetadata、serialize/load_stage_chunkroots
Converter/include/indexer.h               Indexer / HierarchyFlusher(48 B 临时记录) / CRNode；maxPointsPerChunk=10'000
Converter/include/HierarchyBuilder.h      把 .hierarchyChunks/*.bin 组装成 hierarchy.bin（22 B 记录 + proxy）
Converter/include/sampler_poisson.h       SamplerPoisson（默认）
Converter/include/sampler_random.h        SamplerRandom（128³ 网格 + 中心距离阈值）
Converter/src/Writer.cpp / include/Writer.h  octree.bin 环形缓冲写入（1 GiB）；BROTLI：toStructOfArrays + Morton 排序 + brotli q6
Converter/include/PotreeConverter.h       computeScaleOffset（offset=min、30 bit 保护）、LAS PDRF 0–8 → 属性列表、Extra Bytes VLR(4)
Converter/include/structures.h            Node（name/children/min/max/byteOffset/byteSize/numPoints/sampled）、Sampler 接口
Converter/include/converter_utils.h       Options、State、mortonEncode_magicbits、childBoundingBoxOf
Converter/include/VBuffer*.h, src/VBuffer.cpp  虚拟内存预留 + 按需 commit 的可增长缓冲（mmap PROT_NONE → mprotect）
resources/page_template/libs/potree/      Potree 1.8 viewer（读端参考：potree.js 中的 NodeLoader/OctreeLoader、workers/2.0/DecoderWorker*.js）
```

`ChunkRefiner.h` 和 `sampler_poisson_average.h` 不在 CMake 的 `add_executable` 里；`poisson_average` 在 `main.cpp::indexing` 中被显式禁用。

#### 2.1.2 三阶段流水线

```text
LAS/LAZ ─> [CHUNKING] 计数 → 合并 LUT → 分发 ─> chunks/<id>.bin(+metadata.json)
        ─> [INDEXING] 每个 chunk 并行：buildHierarchy(细分到≤10k) → sampler.sample(自底向上) → 完成节点立即写 octree.bin
                       chunk 根剩余点 → tmpChunkRoots.bin；阶段状态 → stage_chunkroots/state.json（可 --stage 续跑）
        ─> [MERGING] processChunkRoots(小于 5M 的子树合并) → 逐个 sample → sample 到根 → HierarchyBuilder → hierarchy.bin → metadata.json → 清理
```

#### 2.1.3 Chunking：把点计数排序到网格

`chunker_countsort_laszip::doChunking` 分三步：

1. **网格规模**
   - 总点数 < 1e8 时 `gridSize=128`；< 5e8 时 256；否则 512。
   - `maxPointsPerChunk = min(total/20, 10'000'000)`。
2. **`countPointsInCells`**
   - 以 100 万点为一个 Task，交给 `TaskPool`，线程数等于核数。
   - 每点先按 LAS scale/offset 转成 int，再归一化：`ux=(X*scale+offset-min)/cubeSize`。
   - 用 `atomic_int32` 做 `grid[ix + iy*G + iz*G*G]++`。
   - 越界容差是 `nextafter(1.0)`；点在包围盒外就直接 `exit`，提示用 `lasinfo -repair_bb` 修复。
3. **`createLUT`**
   - 自底向上逐级合并。若 2×2×2 个子格之和 ≤ `maxPointsPerChunk`，且没有子格被标记为 `-1`，就合并。
   - 否则把非空子格登记为 chunk 节点，节点名由 `toNodeID(level, gridSize, x, y, z)` 生成，并把父格标为 `-1`。
   - 最后得到一张 `G³` 的 LUT，记录"每个格子属于哪个 chunk"。
4. **`distributePoints`**
   - 再读一遍源文件，把每点写成输出 AoS 记录：position 为 int32×3，后接各属性。
   - 每批数据做一次计数排序：先 `counts[nodeIndex]`，再分桶。
   - 通过 `ConcurrentWriter` 追加到 `chunks/<id>.bin`。
   - `--compress-chunks` 时改写 `.br`：每批数据前加 16 B 头 `[u64 uncompressed][u64 compressed]`，brotli q5。
   - 内存节流：`writer->waitUntilMemoryBelow(2'000)`，单位 MB。

#### 2.1.4 Indexing：每个 chunk 内部建树

`indexer.cpp::buildHierarchy(indexer, node, points, numPoints)`：

- 若 `numPoints < 10'000`（`indexer::maxPointsPerChunk`），直接成为叶子。
- 否则：
  1. 在节点包围盒内建 **32³ 计数网格**（5 级金字塔，`computeSumPyramid` 用 Morton 索引 `mortonEncode_magicbits(z,y,x)` 逐级求和并做前缀和）。
  2. 按前缀和做 **原地计数排序**：`offsets[index]++`。
  3. `createNodes` 自顶向下：计数 > 10k 的格子继续分，否则接受为节点。
  4. `expandTo` 在树上补齐中间节点。
  5. 仍 > 10k 的节点递归调用自身。
- **重复点保护**：如果细分后点数不变，就统计 int 坐标唯一值。重复点 ≥ 5k 时丢弃重复点并重试；否则只告警。

#### 2.1.5 Subsampling：自底向上，点被父节点"拿走"

两个采样器共用框架：对 chunk 根做 `traversePost`（后序），对每个内部节点：

- 汇集 8 个子节点的全部点，判定每点接受还是拒绝。
- **接受的点移到父节点，拒绝的点留在子节点。** 子节点剩 0 点时：若它是叶子则丢弃，否则变成"空内部节点"（代码注释引用 potree issue #1125）。
- 这就是 **additive LOD**：全树每点只出现一次，渲染某节点时必须同时渲染其祖先。

| 采样器 | 规则 | 参数 |
|---|---|---|
| `SamplerPoisson`（`-m poisson`，默认） | 1. 子节点点按"到节点中心的距离"并行排序（`std::sort(par_unseq)`）<br>2. 逐点贪心：与已接受点的距离都 ≥ `spacing` 才接受<br>3. **剪枝**：从后往前遍历已接受点；一旦某点到中心的距离 < 候选点到中心的距离 − spacing，就可以断定剩下的都不冲突（三角不等式）<br>4. 最多检查 10,000 次 | `spacing = baseSpacing / 2^level`，`baseSpacing = cubeSize / 128`（`doIndexing` 中 `(max-min).x/128`） |
| `SamplerRandom`（`-m random`） | 1. 每节点一张 **128³ 网格**（`thread_local`，用 `iteration` 戳复用，免清零）<br>2. 每格只接受第一个点，且该点在格内的归一化中心距离² 须 < (0.7√3)²，即排除格子角落<br>3. 子节点点数 < 100 时全部上提，避免碎节点<br>4. **叶子节点 Fisher-Yates 洗牌**，保证任意前缀都是随机子集<br>5. 接受标志存成位图（`BitEdit`） | gridSize=128，阈值 0.7·√3 |

> 源码小瑕疵：两个采样器都写成 `if (numRejected == 0 && child->isLeaf()) {...} if (numRejected > 0) {...} else if (numRejected == 0) {...}`，第一个 `if` 后少了 `else`。结果是被丢弃的叶子仍会以 0 点身份进入 `onNodeCompleted`，写进层级。所以 **Potree 输出里可能有 `numPoints=0` / `byteSize=0` 的叶子**。读端 `NodeLoader.load` 对 `byteSize === 0n` 只打 warning，我们的加载器也必须容忍这种节点。

#### 2.1.6 Writer 与 BROTLI 编码（`Writer.cpp`）

- `Writer::writeAndUnload(node)`：`numPoints==0` 时直接返回。
- DEFAULT 编码：把节点的 AoS 缓冲原样写出。
- BROTLI 编码：先 `compress()`，再写入 **1 GiB 环形缓冲**。缓冲按 `writePos/flushPos` 单调计数，由一个后台线程刷到 `octree.bin`，并返回该节点的 `byteOffset`。节点写出顺序等于完成顺序，不是 BFS。
- `compress()` 的步骤：
  1. `toStructOfArrays` 把数据转成 SoA。
  2. **position → `position_morton`**：把 int32 的 x/y/z 各拆成低 16 位和高 16 位，分别做 3D Morton（`mortonEncode_magicbits(x,y,z)` = `split(x) | split(y)<<1 | split(z)<<2`）。每点 16 B，布局为 `[u64 upper][u64 lower]`。
  3. **rgb → `rgb_morton`**：3 个 u16 通道做 Morton，得到 u64，每点 8 B。
  4. 其余属性原样保留。
  5. 所有 SoA 列按 (upper, lower) 的 Morton 顺序重排，逐列拼接后用 brotli q6 压缩。
- 读端 `workers/2.0/DecoderWorker_brotli.js` 用 `dealign24b` 反解 Morton。本机已用自写 writer 验证，viewer 正确渲染。

#### 2.1.7 Hierarchy 的构建

1. **临时记录**：`HierarchyFlusher` 每 1 万个节点刷一次盘。每条记录 48 B：`name[31] + u32 numPoints + i64 byteOffset + i32 byteSize + '\n'`。按"名字前 `step+1` 个字符"分组，写成 `.hierarchyChunks/<batch>.bin`；深度 ≤ step 的节点归入 `r.bin`。
2. **组装**：`HierarchyBuilder::build()` 的步骤：
   - 先为根批次预留空间。
   - 其余批次各自 `loadBatch`：按每 4 层切 chunk（`(len-2)/4`），BFS 排序，并从子节点反推父节点的 `childMask`。
   - 然后 `processBatch` 计算批内各 chunk 的偏移，`serializeBatch` 输出 22 B 记录。
   - 最后回填根批次里 proxy 节点的 `byteOffset/byteSize`，指向对应子 chunk 在 `hierarchy.bin` 中的位置。
3. **主路径是 `hierarchyStepSize = 4`**（`indexer.cpp` 常量）。`Indexer::createHierarchy`（内存版）逻辑相同，每个 chunk 覆盖 `start..start+4` 层，其中最后一层是 proxy。
4. **proxy 的语义**：父 chunk 里第 `start+4` 层的节点记为 type=2，它的 offset/size 指向 `hierarchy.bin` 中的子 chunk。子 chunk 的第一条记录就是这个节点本身，是真实记录，offset/size 指向 `octree.bin`。

#### 2.1.8 metadata.json（`Indexer::createMetadata`）

- 字段：`version:"2.0"`、`name`、`description`、`points`、`projection`、`hierarchy{firstChunkSize, stepSize, depth}`、`offset[3]`、`scale[3]`、`spacing`、`boundingBox{min,max}`（**立方体**包围盒）、`encoding`（"DEFAULT" 或 "BROTLI"）、`attributes[]`。
- `attributes[]` 每项包含：`name`、`description`、`size`、`numElements`、`elementSize`、`type`、`min`、`max`、`scale`、`offset`；1 字节属性再加 `histogram`。
- `computeScaleOffset`：
  - `offset = bbox.min`。注释说明：为兼容 Potree 1.7 把坐标当 uint 读的 bug，不能用中心作原点。
  - `scale = max(LAS 最小 scale, size/2^30)`。
  - 数值格式用 `format("{}")` 输出最短可回读表示，避免极小 scale 被写成 0.000000，导致所有点塌到一处。

#### 2.1.9 读端：Potree 1.8 的 2.0 loader（`page_template/libs/potree/potree.js`）

- **`OctreeLoader.load(url)`**：
  - 属性名 `rgb` 会被映射成 `rgba`。`NormalX/Y/Z` 三个属性会被合成一个 `NORMAL` 向量。
  - 根节点初始 `nodeType=2`，`hierarchyByteOffset=0`，`hierarchyByteSize=firstChunkSize`。
  - 树的原点设在 `boundingBox.min`，节点包围盒相对这个原点。
- **`NodeLoader.load(node)`**：
  1. 若 `nodeType===2`，先 `loadHierarchy`：对 `hierarchy.bin` 发 `Range: bytes=o-(o+s-1)` 请求，再 `parseHierarchy`。
  2. 对 `octree.bin` 发 Range 请求，取回节点数据。
  3. 交给 Worker 解码，结果建成 `BufferGeometry`。
- **`parseHierarchy`**：
  - 以 22 B 为步长顺序读取记录。
  - `nodes[0]` 就是当前节点。若它原先是 proxy，则用记录里的真实 offset/size 替换。
  - 按 `childMask` 的第 0..7 位**依次追加**子节点，形成隐式 BFS。子节点包围盒由 `createChildAABB` 计算（bit 0b100 表示 x，0b010 表示 y，0b001 表示 z），`spacing` 逐层减半。
- **`DecoderWorker.js`（DEFAULT）**：
  - 位置解码为 `x = X*scale + offset − nodeMin.x`，即 **节点局部 float32**，从根本上避免 float32 精度问题。
  - 颜色：`r>255 ? r/256 : r`。
  - 额外算一个 `density = numPoints / 32³ 网格中被占用的格数`。
- **LOD 遍历**（`updateVisibility`）：
  - 用 `BinaryHeap` 按优先级处理。
  - 权重：`weight = screenPixelRadius = r · (0.5·H) / (tan(fov/2)·d)`。
  - 若 `screenPixelRadius < minimumNodePixelSize`，不加入该子节点。viewer 默认 `minNodeSize=30`；`PointCloudOctree` 默认 150。
  - 相机在包围球内时，权重取 `MAX_VALUE`。
  - `numVisiblePoints + node.numPoints > pointBudget` 时整体停止。默认预算 1M，page template 里设为 2M。
  - 每帧最多把 2 个节点上传到 GPU（`loadedToGPUThisFrame < 2`）。
  - 同时最多 4 个节点在加载（`maxNodesLoading=4`）。
  - LRU 上限 `pointLoadLimit = 2 × pointBudget`。
- **自适应点大小**：`worldSize = size · 1.7·spacing / 2^LOD`。其中 LOD 通过 `visibleNodes` 纹理在 shader 里逐层查询：某点所在位置上可见的最深层级。

#### 2.1.10 关键常量

| 常量 | 值 | 位置 |
|---|---|---|
| chunk 网格 | 128 / 256 / 512（按总点数 1e8 / 5e8 分档） | `chunker_countsort_laszip.cpp::doChunking` |
| chunk 上限 | min(N/20, 1e7) | 同上 |
| 读取批大小 | 1,000,000 点/Task | `countPointsInCells` / `distributePoints` |
| 叶子阈值 | 10,000 | `indexer.h::maxPointsPerChunk` |
| 计数金字塔 | 5 级（32³） | `indexer.cpp::buildHierarchy` |
| 层级分页 | stepSize = 4 | `indexer.cpp` |
| 根 spacing | cube / 128 | `doIndexing` |
| Poisson 最大检查次数 | 10,000 | `sampler_poisson.h::checkAccept` |
| 小节点合并 | < 100 点的子节点整体上提（random 采样器） | `sampler_random.h` |
| chunk 根合并阈值 | 5,000,000 | `Indexer::processChunkRoots` |
| brotli 质量 | 6（octree）/ 5（chunks） | `Writer.cpp::compress` / `addBuckets` |
| 写缓冲 | 1 GiB 环形 | `Writer.h::capacity` |
| 线程 | chunking = 核数；indexing = 核数/3 + 2 | `chunker…` / `doIndexing` |

### 2.2 PDAL 2.10

**架构**：

- `Stage` 分为 `Reader`、`Filter`、`Writer`。
- 标准模式：数据放在 `PointTable`/`PointView` 中，整批在内存里处理。
- 流式模式：实现了 `Streamable` 的 stage 通过 `processOne(PointRef&)` 逐点处理，数据流经 `StreamPointTable`，内存上限就是它的 capacity。
- `PipelineManager` 与 `PipelineReaderJSON` 负责解析 JSON pipeline。
- 命令行 `pdal` 由 `apps/pdal.cpp` 分发到各 Kernel：`translate`、`pipeline`、`info`、`tile`、`tindex`、`merge`、`split`、`density`、`ground` 等（`kernels/*.cpp`）。

与本项目相关的 stage（"流式"一列来自对应 `.hpp` 是否继承 `Streamable`）：

| Stage | 用途 | 关键参数（源码 `addArgs`） | 流式 |
|---|---|---|---|
| `filters.reprojection` | CRS 重投影（GDAL/PROJ） | `in_srs`、`out_srs`、`in/out_axis_ordering`、`in/out_coord_epoch`、`error_on_failure` | yes |
| `filters.projpipeline` | 任意 PROJ pipeline 串，**适合转 ENU** | `coord_op`、`reverse_transfo`、`out_srs` | yes |
| `filters.georeference` | 由轨迹、`scan2imu` 和 NED/ENU 直接把扫描点地理配准到 EPSG:4978 | `trajectory_file`、`scan2imu`（4×4）、`time_offset`、`coordinate_system`（NED/ENU）、`reverse` | yes |
| `filters.transformation` | 4×4 仿射变换（**只变换 X/Y/Z，不变换法线**） | `matrix`（行主序 16 个数）、`invert` | yes |
| `filters.outlier` | 统计或半径离群点检测，默认把离群点标为 class 7 | `method`（statistical/radius）、`mean_k=8`、`multiplier=2.0`、`radius=1.0`、`min_k=2` | no |
| `filters.elm` | 扩展局部最小，去除低点噪声 | `cell=10`、`threshold=1.0` | no |
| `filters.smrf` / `filters.csf` | 地面分类（Pingel 2013 / 布料模拟 Zhang 2016） | smrf：`cell=1`、`slope=0.15`、`window=18`、`threshold=0.5`、`scalar=1.25`<br>csf：`resolution=1`、`rigidness=3`、`threshold=0.5`、`iterations=500` | no |
| `writers.gdal` | 点云栅格化成 DEM/DSM | `resolution`、`radius`（默认 res·√2）、`output_type`（min/max/mean/idw/count/stdev）、`window_size`、`nodata`、`data_type`、`gdaldriver=GTiff` | — |
| `filters.hag_dem` / `filters.hag_nn` | 计算高于地面的高度（HeightAboveGround 维度） | hag_dem：`raster`、`zero_ground`、`min/max_clamp`<br>hag_nn：`count`、`max_distance` | yes / no |
| `filters.dem` | 按 DEM 容差过滤点 | `raster`、`limits` | yes |
| `filters.colorization` | 从正射影像给点着色 | `raster`、`dimensions`（如 `"Red:1:256.0,Green:2:256.0,Blue:3:256.0"`） | yes |
| `filters.sample` | **体素哈希 Poisson 抽稀** | `radius` 或 `cell`（cell = 2r/√3）、`dimension`（只打标记、不删点） | yes |
| `filters.voxeldownsize` | 每个体素只留第一个点或体素中心 | `cell`、`mode`（center/first） | yes |
| `filters.splitter` / `filters.chipper` / `pdal tile` | 平面切块（带 buffer）/ 按容量切块 | `length`、`origin_x/y`、`buffer` / `capacity` | no |
| `writers.copc` | COPC（LAZ 1.4 + EPT 层级） | `scale_*`（默认 0.01）、`offset_*`、`a_srs`、`extra_dims` | — |
| `readers.ply` / `readers.las` / `readers.e57` / `readers.pcd` | 输入 | PLY 属性按原名注册为维度，例如 `nx`、`ny`、`nz` | — |
| 插件 `spz` / `draco` | Gaussian Splat（SPZ）/ Draco 压缩 | —— | — |

**`writers.copc` 内部**（`io/private/copcwriter/`，源自 untwine）：

- 分层：`Grid::calcLevel` 反复"点数 ÷ 8、边长减半"，直到每节点点数 ≤ `MaxPointsPerNode=100000`；支持非立方体（`m_cubic`）。
- 每个 `VoxelInfo` 有一张网格，根节点 `RootCellCount = 128·√3/1.5 ≈ 147`，其余 `ChildCellCount = 128·√3 ≈ 221`。
- `Processor::sample()` 的步骤：
  1. 对子节点的点做 `shuffle`。
  2. 每个网格格子先到先得，接受的点上提到父节点。
  3. 子节点点数 < `MinimumPoints=100` 时整体上提；8 个子节点合计 < 1500 时全部上提。
- 以上与 Potree 的 random 采样器同构。
- 层级页（`Output::emitRoot`）：每 `LevelBreak=4` 层分一页，子树 ≤ 50 个节点时不分页。每条记录 32 B：`i32 d,x,y,z + u64 offset + i32 byteSize + i32 pointCount`，`pointCount=-1` 表示"指向子页"。
- `spacing = 2·halfsize / RootCellCount`。

**读源码发现的小问题**：

- `filters/SampleFilter.cpp::voxelize` 里写了 `m_originZ = y;`，应为 `z`。影响不大，只是体素原点的 z 取错。
- 同文件 `run()` 在 `keepPoint` 之后又调用了一次 `voxelize`。第二次调用会因为和自身的距离为 0 而返回 false，无害但浪费。
- 移植时直接写正确版本即可。

### 2.3 LAStools

- **开源部分**：
  - `LASlib/`：读写库。读取器覆盖 las/laz、txt、ply（`lasreader_ply.cpp`，会把 `nx/ny/nz` 映射为 I16 extra bytes，scale 0.00005）、bin、shp、asc/bil/dtm；还有 `lasfilter`、`lastransform`（`-switch_y_z`、`-scale_xyz`、`-transform_matrix`、`-transform_helmert` 等）、`laskdtree`、`lascopc`。
  - `LASzip/`：LAZ 编解码的参考实现（算术编码 v1–v4），**PotreeConverter 内置的就是它**。
  - `src/` 下的工具：laszip、las2las、las2txt、txt2las、lasinfo、lasindex、lasmerge、lasdiff、lasprecision、lasvalidate、**lascopcindex**。
  - 投影：`geoprojectionconverter.cpp` 支持 UTM、LCC、TM、经纬度、ECEF 等；可选动态加载 PROJ。
- **闭源部分**（`bin/*_README.md` 只有说明，没有源码）：lasground(_new)、lasthin、lasnoise、lasduplicate、lasoptimize、lassort、lastile、blast2dem、las2dem、lasheight、lasclassify、**laspublish**（它内部调用的也是 PotreeConverter 来生成 Potree 门户）。
- **lascopcindex 的单遍流式建树**（`src/lascopcindex.cpp`，2023 年加入）：
  - 与 PotreeConverter 的"先分块、再自底向上"相反，它是 **自顶向下插入**：每点从根开始，在当前节点的占用网格里找空格（`root_grid_size` 默认 256）。找到就落在这一层，否则进入下一层；`max_depth` 这一层无条件接受。
  - 最大深度由 `EPToctree::compute_max_depth` 决定：每加一层，对"跨度 ≥ 当前格边长"的每个轴，点数各 ÷2，然后格边长减半，直到每节点 ≤ 100k 点。这样扁平数据会自动少分几层。
  - 读入的点先在缓冲内 **shuffle**。
  - 如果输入本身按时间或空间有序，先到先得会产生偏差。为此它按概率与已占位点 **swap**：`p = 1-(1-0.95)^(1/E[n])`，其中 E[n] 是由占用网格估算的每格期望点数。
  - `LASfinalizer` 记录每个区域是否已不会再有新点，一旦确定就提前把完成的节点写出为 LAZ chunk 并释放内存。所以大数据量下内存占用低。
- **结论**：lascopcindex 的"自顶向下、每层网格先到先得"与我们的 numpy 算法同构（§3.2）。它的 finalizer 思路可用于将来的流式或增量建图（V1.0 在线建图）。

---

## 3. 可复用算法与实现

### 3.1 Potree 2.0 二进制格式规范（兼容实现必读）

**metadata.json**（最小必需字段；本机已验证可被 Potree 1.8 加载）：

```json
{
  "version": "2.0", "name": "newyork", "description": "", "points": 5000065, "projection": "",
  "hierarchy": { "firstChunkSize": 18744, "stepSize": 4, "depth": 5 },
  "offset": [ -1447.047, -1607.730, -21.991 ],
  "scale":  [ 0.001, 0.001, 0.001 ],
  "spacing": 49.479,
  "boundingBox": { "min": [ -1447.047, -1607.730, -21.991 ], "max": [ 1719.639, 1558.956, 3144.695 ] },
  "encoding": "DEFAULT",
  "attributes": [
    { "name": "position", "description": "", "size": 12, "numElements": 3, "elementSize": 4, "type": "int32",
      "min": [..], "max": [..], "scale": [1,1,1], "offset": [0,0,0] },
    { "name": "rgb", "description": "", "size": 6, "numElements": 3, "elementSize": 2, "type": "uint16",
      "min": [0,0,0], "max": [65535,65535,65535], "scale": [1,1,1], "offset": [0,0,0] }
  ]
}
```

- `boundingBox` 必须是**立方体**：`max = min + maxExtent`。
- `spacing` 表示根节点点间距。它会影响自适应点大小；如果我们用网格采样，取 `cube/G`。
- `hierarchy.firstChunkSize` 等于根 chunk 的字节数，即根 chunk 节点数 × 22。
- 法线：属性名写成 `NormalX/NormalY/NormalZ`（float），loader 会自动合成 `NORMAL`。

**hierarchy.bin**：由若干 chunk 拼接而成。每个 chunk 内节点按 **BFS 顺序**排列：先按层级，同层再按名字字典序。每条记录 22 B，小端：

| 偏移 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 0 | u8 | type | 0=NORMAL（有子节点）、1=LEAF、2=PROXY（子层级需要另外加载） |
| 1 | u8 | childMask | 第 i 位为 1 表示子节点 i 存在，i = (x<<2)\|(y<<1)\|z |
| 2 | u32 | numPoints | 本节点自身的点数（加法式，不含子孙） |
| 6 | u64 | byteOffset | 非 PROXY：在 octree.bin 中的偏移；PROXY：子 chunk 在 hierarchy.bin 中的偏移 |
| 14 | u64 | byteSize | 同上 |

解析伪代码（与 `parseHierarchy` 等价）：

```python
def parse_chunk(buf, start_node):
    nodes = [start_node]; i = 0
    while i < len(buf) // 22:
        t, mask, n, off, size = struct.unpack_from("<BBIqq", buf, 22*i)
        cur = nodes[i]
        if cur.type == PROXY:      cur.byte_offset, cur.byte_size = off, size      # 被 proxy 指向的节点，本条是它的真实记录
        elif t == PROXY:           cur.h_offset, cur.h_size = off, size             # 子层级需要再发一次 Range 请求
        else:                      cur.byte_offset, cur.byte_size = off, size
        cur.num_points, cur.type = n, t
        if t != PROXY:
            for c in range(8):
                if mask >> c & 1:
                    nodes.append(Node(cur.name + str(c), child_aabb(cur.aabb, c), spacing=cur.spacing/2))
        i += 1
```

**分页写法（写端）**：

- 第 k 个 chunk 以某个节点为根，包含它往下 `stepSize` 层的子孙，共 `stepSize+1` 层。最深一层的节点若还有子孙，就写成 PROXY，并各自开一个新 chunk。
- 根 chunk 写在文件开头，大小记在 `firstChunkSize`。
- **MVP 可简化**：节点总数 ≤ 约 40k，即层级文件 ≤ 1 MB 时，可以只写一个 chunk，全部为 NORMAL/LEAF，不用 PROXY。本机测试就是这么做的：`stepSize` 字段 loader 并不读取，写 32 也不影响加载。
- 节点数更多时，按 step=4 分页（算法同 `Indexer::createHierarchyChunks`）。

**octree.bin**：

- 节点负载顺序拼接。**我们按 BFS 顺序写**，Potree 原版是按完成顺序写的，两种都兼容。
- **DEFAULT**：AoS，每点按 `attributes` 的顺序依次存放。`position` 是 int32×3，`X = round((x − offset.x)/scale.x)`；`rgb` 是 u16×3。
- **BROTLI**：
  1. 按 SoA 布局：`position_morton`（每点 16 B，`[u64 高16位 Morton][u64 低16位 Morton]`）、`rgb_morton`（每点 8 B，u64），其余属性原样。
  2. 所有列按位置 Morton 升序排列。
  3. 整块数据 brotli 压缩。
  4. 位 Morton 的位序：x 在 bit0、y 在 bit1、z 在 bit2（`split(x)|split(y)<<1|split(z)<<2`）。

**节点命名、整数坐标与包围盒的换算**（Potree 名 <-> EPT/COPC 的 key）：

```python
def name_to_key(name):            # "r" + digits  →  (d, x, y, z)
    d = len(name) - 1; x = y = z = 0
    for ch in name[1:]:
        c = int(ch); x = 2*x + (c>>2 & 1); y = 2*y + (c>>1 & 1); z = 2*z + (c & 1)
    return d, x, y, z
def key_to_name(d, x, y, z):
    return "r" + "".join(str(((x>>k)&1)<<2 | ((y>>k)&1)<<1 | ((z>>k)&1)) for k in range(d-1, -1, -1))
def node_aabb(cube_min, cube_size, d, x, y, z):
    s = cube_size / 2**d; mn = cube_min + s*np.array([x, y, z]); return mn, mn + s
```

### 3.2 自研 numpy 八叉树转换器 `worldpkg tile`（推荐算法）

**设计目标**：

- 纯 Python/numpy，不需要编译。
- 5M 点 < 10 s，50M 点 < 2 min（估算）。
- 输出 Potree 2.0 容器，编码支持 DEFAULT、BROTLI 和 ANET_Q16。
- 语义是加法式 LOD。
- 节点内点序随机，任意前缀都是均匀子集。

**参数**：

| 参数 | 默认 | 说明 |
|---|---|---|
| `G` | 64 | 每节点每轴的采样格数，`spacing_L = cube/(G·2^L)`。实测 G=64 时节点粒度更适合流式加载（见下表） |
| `LEAF` | 20000 | 节点剩余点数 ≤ LEAF 时整体成为叶子 |
| `B` | 21 | 量化位数（每轴 2^21 格，63 位 Morton）。最大层级 = B − log2(G) = 15 |
| `PRIORITY` | `center` | 格内竞选规则：`center` 选离格中心最近的点（仿 lasthin `-central`）；`random` 随机 |
| `MIN_CHILD` | 100 | 可选：点数 < 100 的子节点整体上提（同 COPC/Potree random 的做法） |
| `SEED` | 1 | 结果可复现 |

**伪代码**（已实现为 `.cache/research/r09_octree_fast.py`）：

```python
def build_octree(P, G=64, LEAF=20000, B=21, priority="center", seed=1):
    mn = P.min(0); size = (P.max(0) - mn).max() * (1 + 1e-9)          # 立方体包围盒
    q  = clip(floor((P - mn) / size * 2**B), 0, 2**B - 1).astype(int64)
    mc = spread3(q.x) << 2 | spread3(q.y) << 1 | spread3(q.z)          # x 为高位：与 Potree child index 一致
    order = argsort(mc); mc = mc[order]; P = P[order]                  # 全局只排一次序
    alive = ones(N, bool); level = full(N, -1)
    for L in range(0, B - log2(G) + 1):
        node   = mc >> 3*(B - L)                                       # 第 L 层的节点 id
        nb     = flatnonzero(r_[True, node[1:] != node[:-1]])          # Morton 有序，同一节点的点连续
        cnt    = add.reduceat(alive, nb)                               # 每节点剩余点数
        leafpt = repeat(cnt <= LEAF, diff(r_[nb, N])) & alive
        level[leafpt] = L; alive &= ~leafpt
        cell   = mc >> 3*(B - L - log2(G))                             # 第 L 层的采样格 id（全局）
        cb     = flatnonzero(r_[True, cell[1:] != cell[:-1]])
        pr     = center_dist2(P, L, G) + 1e-6*rand(N) if priority == "center" else rank
        pr     = where(alive, pr, inf)
        win    = alive & (pr == repeat(minimum.reduceat(pr, cb), diff(r_[cb, N])))
        level[win] = L; alive &= ~win
        if not alive.any(): break
    nid = mc >> 3*(B - level)                                          # 每点所属节点
    return order, level, nid, mn, size

def write_package(P, attrs, level, nid, mn, size, enc):
    nodes = groupby(sorted(zip(level, nid)))                            # (level, nid) 排序 == BFS（nid 各位数字就是名字各位）
    off = 0
    for (L, n), idx in nodes:
        idx = rng.permutation(idx)                                      # 节点内随机：前缀 = 均匀子集（BROTLI 改为 Morton 排序）
        payload = encode(enc, P[idx], attrs[idx], node_aabb(mn, size, L, *deinterleave(n)))
        octree.write(payload); rec[(L, n)] = (off, len(payload), len(idx)); off += len(payload)
    hierarchy = bfs_records(rec)                                        # 22 B/节点；childMask 由子节点存在性求得；无子节点记为 LEAF
    metadata  = potree2_metadata(mn, size, spacing=size/G, enc, attrs) | {"anet": anet_ext(...)}
```

**本机实测**（单线程 numpy 2.5，Python 3.12；UrbanScene3D，每个约 5M 点）：

| 场景 | up 轴 | 立方体边长 | G=128：spacing_root / 节点数 / 深度 / 建树耗时 |
|---|---|---|---|
| San Francisco | z | 740.1 m | 5.78 m / 505 / 5 / 5.8 s |
| New York | z | 3166.7 m | 24.74 m / 402 / 5 / 5.6 s |
| Shenzhen | z | 1999.0 m | 15.62 m / 645 / 5 / 5.9 s |
| Shanghai | z | 7736.3 m | 60.44 m / 637 / 5 / 5.9 s |
| Suzhou | **y** | 4406.9 m | 34.43 m / 563 / **7** / 6.7 s（场景是细长条，立方体浪费了好几层） |
| Chicago | z | **8.0**（km？） | 0.063 / 584 / 5 / 5.3 s |

New York 上的粒度对比（LEAF=20000）：

| 配置 | 节点数 | 单节点最大点数 | 中位点数 | 累计点数 L0 / L≤1 / L≤2 / L≤3 |
|---|---|---|---|---|
| G=128 | 402 | 92,697 | 9,135 | 29,720 / 176,253 / 793,280 / 2,796,610 |
| **G=64（推荐）** | 852 | **31,745** | 3,860 | 5,647 / 35,351 / 181,872 / 851,080 |
| G=32 | 951 | 19,955 | 5,124 | 1,157 / 6,803 / 36,505 / 223,500 |

选 G=64 的理由：

- 单节点 ≤ 32k 点。ANET_Q16 下即 ≤ 384 KB，每帧上传 2 个节点约 0.77 MB，不卡顿。
- L≤2 共 18 万点，恰好是一屏整城概览。

**端到端（New York，G=64）**：

- 读取 + 建树 + DEFAULT 写出共 9.5 s，`octree.bin` 90.0 MB，`hierarchy.bin` 18.7 KB（852×22）。
- BROTLI 写出共 31 s（Python 单线程 brotli q6 占约 21 s），`octree.bin` 42.8 MB（47.5%）。
- 两份输出都能被 Potree 1.8 viewer 正确渲染：`.cache/research/r09_potree_newyork.png` 和 `r09_potree_newyork_br.png`。

**BFS 前缀预取**（实测，New York）：

| 层级 | 累计点数 | DEFAULT 前缀 | BROTLI 前缀 |
|---|---|---|---|
| L≤0 | 5,647 | 0.10 MB | 0.06 MB |
| L≤1 | 35,351 | 0.64 MB | 0.38 MB |
| **L≤2** | **181,872** | **3.27 MB** | **1.86 MB** |
| L≤3 | 851,080 | 15.32 MB | 7.95 MB |

用法：metadata 扩展中写入 `levelsByteEnd`。首屏发一个 `Range: bytes=0-<L2 结束偏移>` 请求，再在 Worker 里按层级记录切片。整城概览只需 1 个请求。

**格内竞选规则对比**（New York，G=64；指标为最近邻距离 < 0.25 或 0.5 个格宽的点所占比例，越低越均匀）：

| 规则 | L≤2：NN<0.25 / NN<0.5 | L≤3：NN<0.25 / NN<0.5 | 相对耗时 |
|---|---|---|---|
| random（COPC、lascopcindex、Potree random 的做法） | 0.145 / 0.560 | 0.191 / 0.594 | 1× |
| **center**（离格中心最近，接近 Poisson 盘） | **0.038 / 0.236** | **0.103 / 0.388** | 约 1.8×（未优化版本） |

结论：默认用 `center`。优化办法：把中心距离量化为 u16，再与随机数拼成 int64 作为优先级，可避免浮点 `reduceat`。写出时节点内再随机打乱一次，保留"前缀即均匀子集"的性质。Potree 的 Poisson 采样器是串行贪心，numpy 无法直接向量化，不移植。

**规模扩展**（V0.5，> 5000 万点，**未验证**）：

- 整体仿照 PotreeConverter 的"先分块、再建树"：
  1. 第一遍：流式读取，统计第 K 层网格计数（8^K ≈ N/2000 万）。
  2. 第二遍：把点散列到 K 层各 chunk 的文件（`np.save` 追加）。
  3. 每个 chunk 独立运行上面的算法，只处理 ≥K 的层级。
  4. 0..K−1 层用各 chunk 的"每格最优候选"做全局归约：按候选的 (cell_key, priority) 取最小。选中的点从 chunk 中剔除。
- 或者直接用 Docker 跑原版 PotreeConverter：需要 GCC 14 + TBB 的镜像，**未验证**。

### 3.3 `ANET_Q16` 节点编码（自定义扩展，推荐作为 Web 主编码）

**metadata 扩展**：`"encoding": "ANET_Q16"`，外加：

```json
"anet": { "formatVersion": 1, "frame": "ENU", "units": "m", "upAxis": "Z",
          "origin": { "lat": 31.8206, "lon": 117.2272, "h": 35.0 },   // 合成数据集填 null
          "sampling": { "method": "grid-center", "G": 64, "leaf": 20000, "seed": 1 },
          "pointOrder": "shuffled", "compression": "none",            // none | gzip
          "buffers": [ { "name": "position", "format": "unorm16x4", "comp": ["x","y","z","octNormal"] },
                       { "name": "color",    "format": "unorm8x4",  "comp": ["r","g","b","class"] } ],
          "levelsByteEnd": [67764, 424212, 2182464, 10212960, 42612324, 60000780],   // NY 实测点数 × 12 B（L0..L5 的累计结束偏移）
          "tightBoundsFile": "hierarchy_ext.bin" }
```

**每个节点的负载**（SoA，n 个点，共 12n 字节）：

- 先是 `pos[n]`，类型 u16×4：
  - x、y、z 按节点立方体量化：`q = round((p − nodeMin)/nodeSize · 65535)`。
  - w 放 oct16 法线：`(octU<<8) | octV`；没有法线时填 0。
- 然后是 `col[n]`，类型 u8×4：rgb 加 1 字节 class 或 intensity。

**着色器解码**：WebGL2 GLSL 与 WebGPU TSL 同理。两个 attribute 都设 `normalized=true`，取值落在 [0,1]：

```glsl
vec3 p   = uNodeMin + a_pos.xyz * uNodeSize;                 // 节点局部 → 世界坐标（ENU，米）
float w  = floor(a_pos.w * 65535.0 + 0.5);
vec2 oct = vec2(floor(w / 256.0), mod(w, 256.0)) / 255.0 * 2.0 - 1.0;
vec3 n   = vec3(oct, 1.0 - abs(oct.x) - abs(oct.y));
if (n.z < 0.0) n.xy = (1.0 - abs(n.yx)) * sign(n.xy);
n = normalize(n);
```

**精度**：

- 量化步长 = `nodeSize/65535 = spacing_L · G/65535`。G=64 时约为 spacing/1024，远小于点间距。
- Shanghai 根节点：7736 m / 65535 ≈ 0.118 m，而该层点间距 121 m；L5 的步长为 3.7 mm。
- 世界原点放在 ENU，场景尺度 < 10 km，float32 精度约 1 mm。量化是按节点做的，UTM 大坐标不会进入 GPU。

**为什么用 x4**：

- WebGPU 没有 `unorm16x3`/`unorm8x3` 这类顶点格式，只有 x2 和 x4，且要求 4 字节对齐。
- 用 x4 格式时，three.js 的 WebGPURenderer 和 WebGL2 可以共用同一份 buffer，也不需要补齐或重新打包。

**体积实测**（San Francisco，G=128；颜色为高度渐变伪彩，这对 u16×3 的压缩偏乐观；在约 300 个节点的子集上测）：

| 编码 | 原始 | gzip-6 | brotli-6 |
|---|---|---|---|
| Potree DEFAULT（int32×3 + u16×3，18 B/pt） | 90.0 MB | 0.484（约 43.6 MB） | 0.422（约 38.0 MB） |
| ANET_Q16 节点内随机顺序（12 B/pt） | **60.0 MB** | 0.704（约 42.2 MB） | 0.710（约 42.6 MB） |
| ANET_Q16 节点内 Morton 顺序 | 60.0 MB | — | 0.592（约 35.5 MB） |

结论：

- 局域网或本机场景下用 **`none`**：零解码，瓶颈只剩 GPU 上传。
- 公网部署用 **gzip**：浏览器原生 `DecompressionStream('gzip')` 支持，体积与 Potree gzip 相当。
- 随机顺序比 Morton 顺序大约 20%，换来的是"部分节点渲染"能力，值得。
- 需要最小体积时，可对节点内按 256 点为一块做 Morton 排序，再把块的顺序随机化（折中方案，**未验证**）。

**可选的 `hierarchy_ext.bin`**：

- 按 BFS 顺序与 `hierarchy.bin` 逐条对应，每节点 12 B：tight bbox，以 u16 相对节点立方体编码。
- 用途是视锥裁剪和屏幕空间误差计算。城市数据很扁（例如 NY 的 z 范围是 292 m，而立方体边长 3167 m），只用立方体包围球会高估节点大小，导致过度细分。

**格式对比**：

| 格式 | 文件数 | 负载 | 浏览器侧解码 | 生态 | 本项目用途 |
|---|---|---|---|---|---|
| Potree 2.0 DEFAULT | 3 | AoS int32 + u16 | Worker 逐点换算成 float32 | Potree 1.8/Next、potree-core、`potree23dtiles`（读 metadata/hierarchy/octree 输出 3D Tiles） | 交叉验证、导出 |
| Potree 2.0 BROTLI | 3 | Morton SoA + brotli | JS brotli 解压 + 反 Morton，较慢 | 同上 | 可选 |
| **ANET_Q16（本文）** | 3（外加可选 ext） | SoA u16x4 + u8x4 | **无**（可选原生 gzip） | 自有 loader | **Web 主编码** |
| COPC | 1 个 .laz | LAZ 1.4 chunk + EPT 层级（32 B 记录） | LAZ 解码需要 WASM（laz-perf） | PDAL、QGIS、CloudCompare、LAStools | V0.5 GIS 互操作与归档 |
| EPT | 每节点 1 个文件 | LAZ 或 binary | 同上 | PDAL、Potree 1.8 | 不用（文件数太多） |
| 3D Tiles 1.1（glTF 点 + 隐式分块） | 多个 | glTF / meshopt | Cesium 或 3d-tiles-renderer | Cesium/GIS | V1.0 导出 |

### 3.4 Web 端渐进加载与疏密自动调节（给前端的接口约定）

1. **启动**：
   1. 读取 `metadata.json`。
   2. 读取整个 `hierarchy.bin`（≤ 1 MB；超过时按 proxy 分页加载）。
   3. 用一个 Range 请求取 `[0, levelsByteEnd[2])`，在 Worker 中按节点切片。
   4. 首屏直接显示 L0–L2。
2. **节点选择**：每帧或节流到 10 Hz 执行一次，用优先队列（最大堆）：
   - 先用 tight bbox 或节点立方体做视锥裁剪。
   - 投影点间距：`s_px(L) = spacing_L · (0.5·H) / (tan(fov/2) · max(d − r, near))`。
   - 细分条件：`s_px > τ`，τ = 1.5 px，可由质量档位调节。
   - 优先级：`prio = s_px · (1 + 0.5 · max(0, dot(viewDir, normalize(c − cam))))`，屏幕中心优先。
   - 相机在节点内部时，优先级取 ∞，与 Potree 相同。
   - 预算：累计点数 + 当前节点点数 > `budget` 时，**当前节点只画一部分**：`drawRange.count = budget − used`。这是节点内随机顺序才能做到的。然后停止遍历。
3. **加载调度**：
   - 最多 4–6 个并发请求。
   - 每帧最多上传 2 个节点到 GPU，或 ≤ 20 万点。
   - Worker 负责解码或解压，结果以 transferable 方式传回。
   - 用 LRU 淘汰，GPU 常驻上限 `2 × budget` 点，节点对象复用 `BufferAttribute`。
4. **FPS 反馈控制**（自动调节疏密）：

```ts
const T = 1000 / targetFps;                 // 60 FPS 对应 16.7 ms；SwiftShader/headless 环境目标设 30 FPS
ft = 0.9 * ft + 0.1 * frameMs;              // EMA
if (interacting) budgetEff = 0.5 * budget;  // 交互中减半，停止 300 ms 后恢复并渐进细化
else if (ft > 1.15 * T) budget = max(minB, budget * 0.85);
else if (ft < 0.80 * T && idleMs > 500) budget = min(maxB, budget * 1.08);
// 默认 budget：桌面 GPU 1.5M；集显 800k；SwiftShader 250k；上下限 [100k, 5M]
```

5. **平滑过渡**：
   - 新节点的 `drawRange.count` 在 200–300 ms 内从 0 线性增长到 n，淡入，与 transitions.dev 的动效曲线保持一致。
   - 节点被淘汰时，先把 count 递减到 0，再释放。
6. **点大小**：
   - MVP 做法：每个节点一个 uniform，`size_px = k · spacing_L · projFactor`，k 约 1.0–1.5，钳制到 [1, 8] px，配合 EDL。
   - 完整做法：移植 Potree 的 `visibleNodes` 纹理，逐点查询可见的最深层级。这部分的细节交给 Web3D 单元（potree-core 相关）。
7. **服务端要求**：
   - `octree.bin` 和 `hierarchy.bin` 必须支持 **HTTP Range（206）**。
   - **注意**：Python 自带的 `http.server` 不支持 Range（本机为此写了 `r09_range_server.py`）。
   - Vite dev server、nginx 支持 Range。Starlette（FastAPI）的 `FileResponse` 在新版本中支持 Range，接入时需要验证。
   - 还需配置 CORS 和 `Cache-Control: immutable`；文件名带内容哈希，或放在带版本号的目录下。

### 3.5 World Package 离线管道（PDAL 用法）

```text
source/*.ply|las|laz|e57  ──ingest──>  规范化（up 轴 / 单位 / 法线 / 伪彩色 / 去重）
        ──georef──>  WGS84/UTM → ENU（RTK 原点）；无地理信息时写 LOCAL 并登记 unit_scale
        ──clean───>  outlier(statistical) + elm + range(去 class 7)
        ──terrain─>  smrf/csf → writers.gdal(DEM 0.5 m, idw) → hag_dem → HeightAboveGround
        ──color───>  colorization(有正射影像时) 或 伪彩色（高度 × 法线 Lambert）
        ──tile────>  worldpkg tile（§3.2，ANET_Q16 + 可选 DEFAULT） ; writers.copc（V0.5）
        ──derive──>  occupancy voxel(0.5 m) / SDF / 碰撞网格（供 Geometry World）
        ──qa──────>  qa/report.json（点数、包围盒、密度、重复率、离群率、DEM 覆盖率）
```

**(a) UrbanScene3D 规范化**：Suzhou 是 Y-up，要转成 Z-up；Chicago 的单位 km 转成 m，只对 Chicago 做。

```json
{ "pipeline": [
  { "type": "readers.ply", "filename": "data/raw/urbanscene3d/Suzhou_sampled_5m.ply" },
  { "type": "filters.transformation", "matrix": "1 0 0 0  0 0 -1 0  0 1 0 0  0 0 0 1" },
  { "type": "filters.outlier", "method": "statistical", "mean_k": 8, "multiplier": 3.0 },
  { "type": "filters.range", "limits": "Classification![7:7]" },
  { "type": "writers.las", "filename": "worlds/suzhou/geometry/pointcloud/source/points.laz",
    "minor_version": 4, "dataformat_id": 6, "extra_dims": "all",
    "scale_x": 0.001, "scale_y": 0.001, "scale_z": 0.001, "offset_x": 0, "offset_y": 0, "offset_z": 0 }
]}
```

> 坑：`filters.transformation::processOne` 只变换 X/Y/Z，**法线 `nx/ny/nz` 不会跟着旋转**。解决办法有两个：用 `filters.assign` 或 `filters.expression` 显式交换并取反法线分量；或者直接在 numpy ingest 阶段统一做（推荐，见 `r09_octree_proto.py::to_z_up`）。

**(b) 真实采集数据转到 ENU**。例子是 P600、MID-360 + RTK，原点取合肥：lat 31.8206、lon 117.2272、h 35 m。

```json
{ "pipeline": [
  { "type": "readers.las", "filename": "raw/mid360_georef_wgs84.laz" },
  { "type": "filters.projpipeline",
    "coord_op": "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad +step +proj=cart +ellps=WGS84 +step +proj=topocentric +ellps=WGS84 +lon_0=117.2272 +lat_0=31.8206 +h_0=35.0" },
  { "type": "filters.elm" },
  { "type": "filters.outlier", "method": "statistical", "mean_k": 8, "multiplier": 2.5 },
  { "type": "filters.range", "limits": "Classification![7:7]" },
  { "type": "writers.las", "filename": "geometry/pointcloud/source/points.laz", "minor_version": 4, "dataformat_id": 7,
    "scale_x": 0.001, "scale_y": 0.001, "scale_z": 0.001, "offset_x": 0, "offset_y": 0, "offset_z": 0 }
]}
```

- `+proj=topocentric` 就是 PDAL `filters/private/georeference/LocalCartesian.cpp` 用的 ECEF→ENU 实现。
- 输入的 x/y 必须按 **lon/lat** 顺序（传统 GIS 顺序）。
- 如果源数据是 UTM（合肥位于 UTM 50N，EPSG:32650），在 pipeline 最前面加 `+proj=utm +zone=50 +inv`。或者先用 `filters.reprojection` 从 `EPSG:32650` 转到 `EPSG:4979`。

**(c) 地面、DEM、HAG**：结果供风场地形、AGL 高度、碰撞检测使用。

```json
[ "geometry/pointcloud/source/points.laz",
  { "type": "filters.smrf", "cell": 1.0, "slope": 0.15, "window": 18, "threshold": 0.5 },
  { "type": "filters.range", "limits": "Classification[2:2]" },
  { "type": "writers.gdal", "filename": "geometry/terrain/dem_0p5m.tif", "resolution": 0.5,
    "output_type": "idw", "window_size": 3, "nodata": -9999, "data_type": "float32" } ]
```

之后再跑一个 pipeline：`filters.hag_dem`（`raster: dem_0p5m.tif`）→ `writers.las`。结果带 `HeightAboveGround` 维度，可用于建筑、树木的粗分语义，也可用于 occupancy 体素。

**(d) 没有 PDAL 时的 numpy 替代**（V0.1 用）：

```python
A, F = 6378137.0, 1/298.257223563; E2 = F*(2-F)
def geodetic_to_ecef(lat, lon, h):
    la, lo = np.radians(lat), np.radians(lon); N = A/np.sqrt(1 - E2*np.sin(la)**2)
    return np.stack([(N+h)*np.cos(la)*np.cos(lo), (N+h)*np.cos(la)*np.sin(lo), (N*(1-E2)+h)*np.sin(la)], -1)
def ecef_to_enu(xyz, lat0, lon0, h0):
    la, lo = np.radians(lat0), np.radians(lon0)
    R = np.array([[-np.sin(lo),              np.cos(lo),             0],
                  [-np.sin(la)*np.cos(lo), -np.sin(la)*np.sin(lo),  np.cos(la)],
                  [ np.cos(la)*np.cos(lo),  np.cos(la)*np.sin(lo),  np.sin(la)]])
    return (xyz - geodetic_to_ecef(lat0, lon0, h0)) @ R.T
# UTM 用 pyproj：Transformer.from_crs("EPSG:32650", "EPSG:4979", always_xy=True)
```

**(e) `coordinate.json` 规范**（World Package 必备）：

```json
{ "frame": "ENU", "units": "m", "up": "Z",
  "origin": { "lat": 31.8206, "lon": 117.2272, "h": 35.0, "datum": "WGS84", "epoch": 2026.7 },
  "source": { "crs": "EPSG:32650 | EPSG:4979 | LOCAL", "up_axis": "Y", "unit_scale": 1000.0 },
  "T_source_to_world": [[1,0,0,0],[0,0,-1,0],[0,1,0,0],[0,0,0,1]],
  "extent_m": [2928.2, 3166.7, 292.5], "curvature_drop_at_corner_m": 0.39 }
```

### 3.6 LAStools 用法速查（只用开源工具）

| 命令 | 用途 |
|---|---|
| `lasinfo -i points.laz -cd -histo z 1` | QA：包围盒、各类别计数、密度 |
| `lasinfo -repair_bb` | 修复包围盒。PotreeConverter 要求包围盒正确，否则直接退出 |
| `las2las -i in.ply -o out.laz` | PLY 转 LAZ。LASlib 能读 PLY，法线会存成 I16 extra bytes |
| `las2las -switch_y_z` / `-scale_xyz 1000 1000 1000` / `-transform_matrix ...` | 轴与单位修正 |
| `las2las -target_utm auto` / `-target_longlat` | 投影转换 |
| `laszip -i *.las` | 压缩成 LAZ |
| `lasindex -i points.laz` | 生成 .lax 四叉树空间索引 |
| `lascopcindex -i in.laz -o out.copc.laz -progress` | 生成 COPC，可作为 PDAL `writers.copc` 的交叉验证 |

Python 侧用 `laspy`（venv 已装 2.7.0）加 `lazrs` 读写 LAZ，不需要编译 LAStools。

---

## 4. 在本项目中的落点与复用方式

| 可复用项 | 来源 | 落点模块 | 版本 | 方式 | 理由 |
|---|---|---|---|---|---|
| Potree 2.0 三文件容器规范（metadata、22 B hierarchy、proxy、Range） | `indexer.cpp::createMetadata`/`createHierarchy`、`HierarchyBuilder.h`、`potree.js::NodeLoader` | `world/pointcloud/format`、`apps/web/pointcloud/loader` | V0.1 | port | 已实测兼容；可用官方 viewer 验证；potree23dtiles、potree-core 等生态都能读 |
| 加法式八叉树（top-down 网格竞选、一次 Morton 排序、`reduceat`） | `sampler_random.h`、COPC `Processor::sample`、`lascopcindex` | `world/pointcloud/tiler.py`（`worldpkg tile`） | V0.1 | port | 纯 numpy，5M 点约 6 s；无需编译 |
| 中心优先竞选 + 节点内随机顺序 | `sampler_random.h`（中心阈值、叶子洗牌）、lasthin `-central` | 同上 | V0.1 | port | 粗层级更均匀；支持部分节点渲染 |
| Morton 编码、`childBoundingBoxOf`、命名 <-> key 换算 | `converter_utils.h` | `world/pointcloud/octree_key.py` + TS 对应实现 | V0.1 | port | 前后端共用 |
| DEFAULT 与 BROTLI（Morton SoA）写出 | `Writer.cpp::compress`、`DecoderWorker_brotli.js` | `tiler.py --encoding` | V0.1（DEFAULT）/ V0.5（BROTLI） | port | 交叉验证、外部导出 |
| ANET_Q16 编码 + BFS 前缀 + `hierarchy_ext` | 本文设计（参考 Potree 的节点局部 float 解码） | `tiler.py` + `apps/web/pointcloud/decoder.worker.ts` | V0.1 | 自研 | 零解码直传 GPU；兼容 WebGPU |
| LOD 遍历：优先级、点预算、每帧上传 2 个节点、最多 4 个并发、LRU 为 2×预算 | `potree.js::updateVisibility` | `apps/web/pointcloud/lod.ts` | V0.1 | port | 成熟参数，直接用 |
| FPS 反馈调节预算 + 部分节点渲染 + 淡入 | 本文设计 | `apps/web/pointcloud/budget.ts` | V0.1 | 自研 | 满足"疏密自动调节" |
| 支持 Range 的静态服务 | `r09_range_server.py`（实测） | `apps/api` 的 StaticFiles 或 nginx | V0.1 | adopt | Potree 的读法依赖 Range |
| UrbanScene3D ingest（up 轴检测、单位、伪彩色、法线旋转） | 本机实测 + `filters.transformation` 的坑 | `world/ingest/urbanscene3d.py` | V0.1 | 自研 | 数据本身有缺陷，必须处理 |
| WGS84<->ECEF<->ENU（topocentric）、UTM | `LocalCartesian.cpp`、`filters.projpipeline`；pyproj | `world/georef/` | V0.1（numpy）/ V0.5（PDAL） | port + adopt | V0.1 就需要把 RTK 原点落地（与 r01 结论一致） |
| 轨迹直接地理配准（scan2imu，NED/ENU） | `filters.georeference` | `reconstruction/lidar/georef` | V0.5 | adopt | MID-360 + RTK/IMU 链路 |
| 去噪：statistical/radius 离群点、ELM | `filters.outlier`、`filters.elm` | ingest | V0.1（numpy KNN 近似）/ V0.5 | adopt | 重建点云噪声多 |
| 地面分类与 DEM、HAG | `filters.smrf`/`filters.csf`、`writers.gdal`、`filters.hag_dem` | `world/terrain`、`environment/wind`（地形风）、`simulation`（AGL） | V0.4–V0.5 | adopt（Docker） | 地形风场与碰撞需要 DEM |
| 正射着色 | `filters.colorization` | ingest | V0.5 | adopt | 真实采集数据有正射影像时使用 |
| COPC 导出 | `writers.copc`、`lascopcindex` | `world/export` | V0.5 | adopt | GIS 互操作、归档 |
| 流式与增量建树思路（finalizer、swap 概率） | `lascopcindex.cpp` | `reconstruction/fusion`（在线建图） | V1.0 | reference | 实时增量更新世界 |
| 原版 PotreeConverter（Docker） | 整个仓库 | 工具链的可选后端 | V0.5+ | adopt（可选） | 1 亿点以上时 C++ 更快（未验证） |
| laszip、lasinfo、las2las | LAStools 开源部分 | QA 与互操作 | V0.5 | adopt | 行业标准工具 |
| lasground、lasthin、blast2dem、laspublish | LAStools 闭源部分 | —— | —— | skip | 需要授权；PDAL 或自研可替代 |

---

## 5. 对比与推荐

| 维度 | PotreeConverter | PDAL | LAStools |
|---|---|---|---|
| Star / 2026 活跃度 | 818 / 活跃（9 月仍有修复，新增 `--stage` 断点续跑、VBuffer） | **1416** / 很活跃（v2.10，pixi，插件多） | 1070 / 活跃（lascopcindex 等仍在演进） |
| 与本项目的契合度 | **最高**：Web 流式 LOD 就是本项目的核心难点 | 高：地理配准与地形，属于 V0.4–V0.5 的核心 | 中：LAZ 与 QA |
| 本机可用性 | 编译不了（C++23、TBB、CMake）→ 移植 | 没有 wheel → Docker 或 conda | 理论上可编译（C++17）；Python 侧用 laspy 替代 |
| 算法可读性与可移植性 | 高（约 1.1 万行，结构清晰） | 中（体量大，但每个 stage 独立） | 中（lascopcindex 单文件 1.5k 行） |
| 许可（按要求忽略） | BSD-2 | BSD | LGPL + 闭源 |

**推荐顺序**：

1. **PotreeConverter**：port 算法与格式。它决定 V0.1 Web 点云的成败。
2. **PDAL**：V0.5 起作为 Docker 化的工具链正式引入；V0.1 用 numpy/pyproj 实现 ENU、离群点这类最小子集。
3. **LAStools**：只作为 QA 和互操作的辅助。

三者不互相替代：PotreeConverter 负责 Web 切片，PDAL 负责地理与地形，LAStools 负责 LAZ 标准与 QA。

---

## 6. 风险与注意事项

1. **构建**：
   - PotreeConverter master 需要 GCC ≥ 14 或 Clang ≥ 18，外加 TBB；本机 GCC 13.3 实测编不过。
   - 如果坚持用原版，要写 `Dockerfile`（基于 `gcc:14`，装 `cmake libtbb-dev`），**未验证**。
   - 它只吃 LAS/LAZ，PLY 需要先转换；包围盒不合法会直接 `exit(123)`。
2. **PDAL 依赖重**：
   - GDAL 和 PROJ 必需，PyPI 没有二进制 wheel。
   - Docker 镜像约数百 MB（估算）。
   - `writers.copc`、`filters.smrf`、`filters.outlier` 不支持流式，数据量大时内存吃紧，需要先 `pdal tile` 切块，并带 buffer 防止边界效应。
3. **Open3D 在本机 headless 环境无法 import**：报 `libEGL.so.1` 缺失。原设计 §6 把 Open3D 列为点云处理主力，需要装 `libegl1` 或换 `open3d-cpu`。能用 numpy 或 PDAL 做的步骤，不要依赖 Open3D。
4. **数据陷阱**（UrbanScene3D）：
   - 没有颜色。
   - Suzhou 是 Y-up。
   - Chicago 的单位疑似 km。
   - Chicago 在 1 mm 量化下唯一点只有 4.80M / 5.00M。如果在 km 单位下直接用 `scale=0.001` 量化，实际分辨率就是 1 m，4% 的点会被 PotreeConverter 当成重复点。所以必须先修正单位。
   - 真实采集数据还会有时间戳错乱、强度范围不一致等问题。
5. **坐标**：
   - float32 的 ULP：1e5 m 时 0.0078 m；1e6 m 时 0.0625 m；UTM 北向坐标约 3.5e6 m（合肥）时 **0.25 m**；ECEF（6.4e6 m）时 0.5 m。
   - 规则：**一律在 ENU 局部原点下计算，再做节点局部量化**，不允许把 UTM 或 ECEF 坐标直接送进 GPU。
   - ENU 平面近似的高程误差为 d²/2R：1 km 时 0.08 m，3 km 时 0.71 m，5 km 时 1.96 m，10 km 时 7.8 m。Shanghai（约 7.7 km）边缘约 2 m，需要写进 `coordinate.json`。超过 10 km 的场景要分区设原点，或者改用 UTM 地图坐标系。
6. **HTTP Range**：
   - 开发服务器或反向代理不支持 Range 时，加载会静默失败。Python `http.server` 就不支持。
   - 若启用 `Content-Encoding: br/gzip`，Range 针对的是压缩后的表示，会和按字节偏移取数冲突。应改为在节点负载内部做压缩（ANET_Q16 的 gzip 模式），传输层不压缩 `octree.bin`。
7. **精度与语义**：
   - 加法式 LOD 下，**碰撞、规划、传感器仿真不能使用 LOD 数据**，必须用 `source/points.laz` 的全分辨率点云或 DEM、体素。
   - 否则远处的"稀疏世界"会让无人机穿墙。
8. **软件渲染**：
   - headless Chromium（SwiftShader）下 Potree 渲染 23 万点仍然可用，但帧率低。
   - 流畅性测试时要把预算下限设到约 25 万，并关闭或降低 EDL 分辨率；要把"算法正确性"和"帧率"分开测。
   - WebGPU 在本机大概率不可用，必须保证 WebGL2 回退路径和 WebGPU 使用同一份 buffer 格式（ANET_Q16 的 x4 设计正是为此）。
9. **层级规模**：
   - 单个 chunk 的 `hierarchy.bin` 在节点数超过数万时会超过 1 MB，首屏变慢。数据超过约 1 亿点时必须实现 proxy 分页（step=4），并在测试中覆盖 PROXY 类型的解析。
10. **brotli 在浏览器端**：
    - 压缩流 API 对 brotli 的支持不统一（需要实测），而 JS 版 brotli 解码慢。
    - 所以 BROTLI 只用于导出和兼容，Web 主路径用 none 或 gzip。

---

## 7. 对设计文档（01-design.md）的优化建议

1. **§15 "点云数据格式"写得太抽象，建议定死**：
   - Web 端的 "Binary Tile + Octree Index" 改为 **"Potree 2.0 三文件容器 + ANET_Q16 编码"**，同时写出 DEFAULT 兼容输出。
   - 明确 **不用"每节点一个文件"**（EPT、Potree 1.x 那种），避免数百万个小文件。
   - GIS 互操作用 COPC（V0.5）；3D Tiles 从 V1.0 起**由转换得到**（glTF 点 + 隐式分块，或借鉴 potree23dtiles 的思路），不作为主格式。
2. **§14 LOD 需要改写**：
   - "远处 10K、近处 1M"应改为 **全局点预算 + 屏幕空间误差（投影点间距 > τ px）+ 优先队列 + FPS 反馈**。
   - 注明 **LOD 是加法式的**：父子叠加，每点只存一次。
   - 补充"首屏 BFS 前缀预取"（实测一次 Range 请求 1.9–3.3 MB 即可拿到整城概览）和"部分节点淡入"。
3. **§41 World Package 需要补全**：
   - 加 `manifest.json`：版本、各文件哈希、生成工具与参数。
   - 加 `coordinate.json`：见 §3.5(e)。要写 ENU 原点、源 CRS、源 up 轴、单位缩放、4×4 变换，以及曲率误差说明。
   - `geometry/pointcloud/` 下细分为 `source/`（全分辨率 LAZ）、`web/`（ANET_Q16）、`potree/`（可选）、`copc/`（可选）。
   - 加 `geometry/terrain/`（DEM/DSM、HAG 统计）和 `qa/report.json`。
   - 原文里 `── visual/` 缺了一个 `├` 字符，是个笔误。
4. **§6/§33 的工具边界要划清**：
   - Open3D：配准、ICP、法线、网格重建。
   - PDAL：地理配准、重投影、地面分类、DEM、HAG、着色、COPC。
   - 自研 numpy 转换器：Web 切片。
   - LAStools：QA 和 LAZ。
   - 原文里 "Coordinate: RTK/ENU/WGS84" 应落到具体实现：`pyproj` + `+proj=topocentric`。
5. **把地理配准从 V0.5 提前到 V0.1**（与 r01 结论一致）：
   - LingBot-Map 输出的是相对尺度、不对齐重力的坐标，UrbanScene3D 也存在轴向和单位混乱。
   - 所以 V0.1 就要有 `ingest → georef/normalize → tile` 三步，否则 m/s、风速、AGL 都没有意义。
6. **§8 双表达需要补充"数据来源分离"**：
   - Visual World 用 LOD 切片。
   - Geometry World 必须来自全分辨率点云，经 DEM、occupancy 体素（0.5 m）、SDF 得到。
   - 两者由同一个管道在同一次运行中生成，保证一致。
7. **§43 MVP 链路要加入"离线切片 Job"**：
   - 链路改为：`导入 → ingest → tile → 发布到静态目录（支持 Range）→ Web 加载`。
   - Job 应分阶段、幂等（仿 PotreeConverter 的 `--stage` 与 `stage_chunkroots/state.json`，可断点续跑），进度通过 WebSocket 推送。
8. **§36 通信补充**：
   - 点云数据走 **HTTP Range 静态文件**，不走 WebSocket，也不走 REST 的 JSON。
   - WebSocket 只推送遥测和事件。
   - 场景切换时，由 REST 返回 World Package 的 `manifest.json` 地址。
9. **UrbanScene3D 作为内置 mock 世界时的约定**：
   - 统一转成 ENU、Z-up、米制。
   - 伪彩色规则：高度渐变 × 法线 Lambert（用 PLY 自带法线）。
   - `origin` 设为 null，标记为 LOCAL。
   - 6 个城市在切片后每个约 60 MB（ANET_Q16 未压缩）或约 42 MB（gzip），可直接随仓库的 data 目录分发，也可在首次启动时生成。
10. **§34 前端栈补一行**：Point Cloud 由 "Custom Octree / Potree concepts" 改为 **"Potree 2.0 容器 + ANET_Q16 + 自研 LOD（参考 potree-core）"**，并注明需要 WebGL2 与 WebGPU 共用 x4 顶点格式。
