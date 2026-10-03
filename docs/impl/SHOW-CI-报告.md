# SHOW-CI 干净克隆验证与 GitHub Actions CI

| 项 | 内容 |
|---|---|
| 工作包 | SHOW-CI（M00 工程流程：干净克隆验证、托管 CI；跨模块修正测试的数据依赖） |
| 日期 | 2026-10-03 |
| 依据 | 任务书 SHOW-CI；AWR-03 §1.3、§4.5、ADR-033、ADR-077；AWR-18 §8.2、§12；AWR-19 §7；D1 验收报告第 3 轮；DEMO-W 报告 |
| 环境 | VMware 虚拟机 8 vCPU（E5-2603 v4 1.7 GHz）、无 GPU，与其他工作包并行（负载均值约 9）；Node 22.12.0、npm 10.9.0；系统 Python 3.12.3；Chrome for Testing 151 存在但验证中有意屏蔽 |
| 约束执行 | 没有安装依赖（干净克隆只复用本机 uv 与 npm 的下载缓存，`.venv`、`node_modules` 全部新建）；没有执行 git 写操作（只用了只读的 `git ls-files`、`git status`、`git check-ignore`）；没有占用排他性能锁；干净克隆中的构建与测试都持主仓库的共享性能锁 `runs/.perf.lock`（`AWR_PERF_LOCK` 指向它）；`make lint` 在主工作树与干净克隆中都通过 |
| 规格变更 | 新增 AWR-03 ADR-078（附录 E 正文与 §7.0 索引），修订 §4.5 第 3 条；AWR-18 §8.2、§12.1（新增 G1h 行）、§12.2；AWR-19 §7.2、§7.3、§7.5 第 4 条、§8.4、OPS-FR-013；M15-FR-045；CONTRIBUTING、README（中英）。**没有冻结或放宽任何阈值** |

## 1 结论

| 任务 | 结果 |
|---|---|
| 1 干净克隆验证 | 完成。修复后在全新目录中 `make setup`、`make lint`、`make test-contracts`、`make typecheck`、pytest 无数据子集（两路）、Vitest unit、`make build`、`make demo-world` 与其校验和测试全部通过，无数据用例全部跳过或被筛掉，没有一个因缺数据而失败（§3） |
| 发现的问题 | 10 项：源码包被 `.gitignore` 挡在仓库外（已公开的提交同样缺失，最严重）；lint 依赖不入库的 `refs/`；2 个 pytest 用例与 4 个 Vitest 文件缺数据即失败；52 个用例需要世界却没有 `needs_data` 标记；一个夹具在无世界时白等 120 s；一个用例写死深圳命名空间，只有 synthcity 时失败；假子进程的日志行在重负载下被拼坏；锁文件指向国内镜像；文档与实现不一致（车辆模型）。全部在主工作树修复（§4） |
| 2 GitHub Actions | 完成。`.github/workflows/ci.yml`：`checks` 作业与 pytest 两路矩阵并行，Python 3.12 与 Node 按 `.nvmrc`，缓存 npm 与 uv、pip 下载，并发取消，超时 30 / 45 min；每条命令都是 `mk/hosted-ci.mk` 的 make 目标，并已在干净克隆中实际跑通（§5） |
| 3 文档 | ADR-078 与上列各处（§6） |

## 2 干净克隆的做法

1. **待入库清单**：`GIT_DIR=/data/projs/.anet-drone4d.git GIT_WORK_TREE=/data/projs/anet-drone git ls-files --cached --others --exclude-standard`，初次 2,281 个；修复 `.gitignore` 后多出 `python/awr/swarm/coverage/` 的 5 个文件，加上本包新增的 2 个文件为 2,288 个；最后一轮时其他工作包又新增了文件，为 2,294 个。
2. **复制**：清空 `/data/projs/anet-drone/.cache/cleanroom/`，`rsync --from0 --files-from` 只复制清单内的文件（7–8 s）。没有 `.venv`、`node_modules`、`data/raw`、`worlds/`、`refs/`、`runs/`、`.env.local`、`apps/web/dist`、`apps/web/public/bench`。
3. **仓库根标记**：干净克隆位于主工作树的 `.cache/` 之下，而主工作树 `.gitignore` 有 `/.cache/`。oxlint 沿父目录读取 `.gitignore`，没有 `.git` 时把整个干净克隆当作被忽略，报"No files found to lint"并以 1 退出。真实克隆（含 GitHub Actions 的检出）有 `.git`，不受影响。因此在干净克隆根下建一个空目录 `.git` 作为仓库根标记（只是 `mkdir`，不是 git 命令；git 不认空目录为仓库，`git rev-parse` 照常失败并走无 git 的分支）。为确认 oxlint 在有标记时确实在检查文件，临时放入一个 `import 'lucide-react'` 的探针文件，oxlint 报 TS-BND-01，随即删除。
4. **CI 同款环境**：`npm_config_registry=https://registry.npmjs.org/`、`npm_config_replace_registry_host=always`、`PYTHON_BOOT=python3.12`；`PW_CHROME` 指向不存在的路径（确认没有任何一步需要浏览器）；`NUMBA_CACHE_DIR` 指向干净克隆内的空目录（与 CI 一样冷缓存）；`AWR_PERF_LOCK` 指向主仓库的锁，以便与其他工作包的性能用例互斥。
5. **依赖泄漏检查**：Node 的模块解析会沿父目录查找 `node_modules`，干净克隆的父目录链上有主工作树的 `node_modules`。比对两边顶层包集合（含 scope 包）：各 670 个、完全相同（同一锁文件安装），因此不存在"干净克隆缺包、从父目录借到"的情况。Python 不沿父目录解析，`pip install -e .` 指向干净克隆自己的 `python/`。

## 3 修复后的两轮完整验证（每轮从零开始）

修复完成后共做了两轮从零开始的完整验证（每轮都先清空目录、重新复制、重新 `make setup`）。第 1 轮暴露了 3 个问题（下文），修复后做第 2 轮。下表为第 2 轮；pytest 第 1 路在第 2 轮中有 1 例受主机负载影响失败，同一目录重跑该路通过（见表后说明）。

| 步骤（与 CI 一致） | 第 2 轮耗时 | 结果 | 第 1 轮耗时 |
|---|---|---|---|
| 复制待入库文件 | 7.4 s | 2,294 个文件 | 8.0 s（2,288 个） |
| `make setup` | 68.5 s | 通过：建 `.venv`、自举 uv 0.12.19、`uv pip sync`、`pip install -e .`、`npm ci`（829 个包）、契约生成物 `--check`、`awr doctor --quick` 通过（只有"原始数据 0 个 PLY""默认世界未构建"两条 WARN）。uv 与 npm 的下载缓存是热的；CI 首次无缓存时要多下载约 0.5 GB | 76.9 s |
| `make lint` | 28.6 s | 通过（ruff、oxlint type-aware、tools/lint 全部规则、m06、m15、m16 注册表）；`gen-motion-tokens` 打印"快照不存在，跳过重新生成比较" | 36.8 s |
| `make test-contracts` | 73.5 s | 通过：生成物 `--check`、单位与 import lint、pytest 335 通过 3 跳过（与数据无关的既有跳过）、Vitest 225 通过 | 82.1 s |
| `make typecheck` | 8.9 s | 通过 | 11.3 s |
| `make test-web-unit` | 42.4 s | 通过：Vitest unit 898 通过、34 跳过（全部是缺世界的 `skipIf`）；`tests/safety/web` 4 通过、`tests/agent/web` 8 通过 | 46.0 s |
| `make build` | 5.9 s | 通过（只有既有的 chunk 体积提示） | 4.5 s |
| `make demo-world` | 30.6 s | 通过：synthcity 28.0 s 发布，`contentVersion` 9e05c2364f58，与 DEMO-W 报告的值一致（不同目录、冷 numba 缓存下仍逐字节可复现） | 28.8 s |
| `make validate-demo-world` | 4.4 s | 0 错误 0 警告；11 个 JSON 通过 Ajv strict | 4.2 s |
| `make test-demo-world` | 22.0 s | 6 通过（已发布事实 1、剧本加载器规则 5） | 187.0 s，1 失败（S0 端到端，见下） |
| `make test-py-nodata PYTEST_SHARD=1` | 1,198.1 s | 585 通过、1 失败、1 跳过；同一目录重跑：1,021.2 s，586 通过、1 跳过 | 1,077.7 s，586 通过、1 跳过 |
| `make test-py-nodata PYTEST_SHARD=2` | 577.2 s | 1,884 通过 | 543.1 s，2 失败（见下） |

两路收集数之和等于不分路的收集数（587 + 1,884 = 2,471）。第 1 路唯一的跳过是 `test_profiles` 的高模 glb（`make vehicles-models` 的生成物，§7 第 3 条）。

按 junit 统计的分目录用时（重跑的第 1 路与第 2 轮第 2 路）：第 1 路 1,012 s，其中 sim 561、mission 285、safety 150、planning 15、swarm 1；第 2 路 545 s，其中 rt 116、runtime 96、world 92、environment 59、reconstruction 48、contracts 34、recorder 24、sensors 20、agent 18、world_query 15、georef 12，其余合计不到 10。两路约 2:1，把 safety 移到第 2 路可得约 860 s 与 700 s；本包没有在验证后再改分路（改了就要重跑两路），留作托管机实测后的调整（§7 第 2 条）。

第 1 轮暴露、随后修复的问题：

1. `make test-demo-world` 当时包含标 `slow` 的 S0 ci ×5 端到端：剧本 `SUCCEEDED`、谓词全真，但墙钟 162.4 s，超过用例断言的 120 s（冷 numba 缓存下 sim-core 编译，加上主机负载）。按 G1h 的口径（`not slow`）把它移出，`test-demo-world` 改为 `needs_data and not perf and not slow`。
2. 第 2 路 `tests/runtime/test_supervisor.py::test_zenoh_integration_sys_procs_ready_and_cli`：第 1 轮在 `make demo-world` 之后跑 pytest，干净克隆里只有 synthcity，supervisor 按 ADR-077 回退到 synthcity，而用例按写死的 `awr/shenzhen/<run>` 查询 `sys/procs`，永远无回复（主工作树有深圳，所以一直通过）。改为从 READY 行取运行世界拼命名空间；另把查询循环改为 20 s 内容忍 `BusTimeout` 重试（断言不变）。
3. 第 2 路 `test_kill9_detected_fast_and_restarted_with_backoff`：`json.loads` 读到 `fake child ready pid=... args=None,None{"t_wall_ns":...}`。supervisor 把子进程的 stdout 与 stderr 合并为同一管道，子进程带 `PYTHONUNBUFFERED=1`，假子进程的 `print` 把正文与换行分两次写入，api 的 JSON 日志由 QueueListener 线程写 stderr，重负载下插进两次写入之间。干净克隆中 3 次复现 2 次，主工作树同一时刻也偶发。`tests/runtime/fake_child.py` 改为一行一次 `write`（小于 PIPE_BUF，原子）后连续 5 次通过。

第 2 轮第 1 路的失败是 `tests/sim/test_checkpoint.py::test_background_capture_in_idle_windows`：断言后台 checkpoint 拷贝全部落在模拟的空闲窗口内（`forced == 0`），实测 `captures 4, forced 1`。该用例用 2 ms 的真实睡眠模拟主循环空闲窗口，后台线程在主机负载均值约 9 时 250 ms 内没有被调度进窗口，于是强制执行一次。它在第 1 轮与修复前的整轮中都通过；属于对调度敏感的既有用例，本包没有改它（改窗口长度会改变 M08 用例的含义），列入 §7。

耗时说明：本机核慢（1.7 GHz、无睿频）且与其他工作包并行（负载均值约 9），pytest 两路串行合计约 30 min。GitHub 托管的 `ubuntu-latest` 为 4 vCPU，单核更快、核数更少；两路并行时墙钟由较慢的第 1 路决定。作业超时取 30 min（checks）与 45 min（每路 pytest），按本机数据留有余量。修复前同一子集不分路一次 1,882 s（31 min），其中 120 s 是 `test_processes` 白等。

## 4 发现的问题与修复

| # | 问题 | 现象 | 修复 |
|---|---|---|---|
| 1 | **源码包不在仓库里** | `.gitignore` 的 `coverage/`（本意是前端覆盖率输出）不锚定，匹配到 `python/awr/swarm/coverage/`；`git check-ignore -v` 指向 `.gitignore:42`。干净克隆 pytest 收集期 `ModuleNotFoundError: No module named 'awr.swarm.coverage'`（`awr.sim.mission.generators.helix_scan` 与 `tests/swarm/test_coverage.py`）。lint 没有发现：ruff 与 `check_py_imports` 都不检查被 import 的模块是否存在。**已公开的提交同样缺这 5 个文件**，克隆下来的 sim-core 在加载任务生成器时就会失败 | 改为 `/coverage/`、`/apps/web/coverage/` 与 `.coverage`，并在注释中写明原因；修复后 5 个文件进入待入库清单。需要在下次提交中加入（本包不做 git 写操作） |
| 2 | lint 依赖不入库的 `refs/` | `lint-m15` 的 `gen-motion-tokens.py --check` 读 `refs/design/transitions.dev/.../_root.css`，`FileNotFoundError` | 快照缺失时 `--check` 只确认 `tokens.css` 存在且带生成文件头，打印说明后通过；有快照时照旧逐字节比较；非 `--check` 模式缺快照以 2 退出并提示获取方式（M15-FR-045）。transitions.dev 条款不允许把配方集合原样再发布，因此不把 `_root.css` 放进仓库 |
| 3 | pytest：缺世界即失败 | `tests/recorder/test_runs_rest.py::test_list_and_meta`：api 不知道世界的 `contentVersion` 时兼容性判定无从比较，"content_version 不同即不兼容"的断言失败。`tests/rt/test_epoch.py::test_health_states_and_status`：`/api/health/ready` 还要求 `world_loaded`，恢复后仍是 503 | 前者在无世界时为 api 注入 RUN 的 `contentVersion`，断言保持不变；后者在无世界时断言 `sim = ok`、`world_loaded = false` 的 503，有世界时仍断言 200。两者都保留了网关逻辑的覆盖，没有整体改标 `needs_data` |
| 4 | Vitest：缺世界即失败 | `tests/pointcloud/firstscreen.test.ts`、`openworld.test.ts`：`describe.skipIf` 的 describe 体在收集期就读世界文件（Vitest 收集时仍执行被跳过的 describe 体）；`tests/m06/frames.test.ts`、`zones.test.ts`：没有任何跳过 | 前两者在 describe 体首行 `if (!have) return`；后两者 `it.skipIf(!BUILT)`（六城都已构建才运行）。主工作树（有世界）4 个文件 32 例照常通过 |
| 5 | 需要世界却没有 `needs_data` 标记 | 52 个用例只靠运行期 skip：`tests/reconstruction` 32 个、`tests/rt` 7 个（`test_skeleton_chain` 6、`test_seat_grace` 1）、`tests/pointcloud/test_flight60.py` 5 个、`tests/e2e/test_demo_check.py` 3 个、`tests/environment/test_env_e2e.py` 2 个、`tests/sensors/test_pose_chain_e2e.py` 1 个、`tests/world/test_synthcity.py` 1 个、`tests/recorder/test_processes.py` 1 个 | `tests/rt/conftest.py` 与 `tests/reconstruction/conftest.py` 以 `tryfirst` 的 `pytest_collection_modifyitems` 给使用 `stack`、`recon_base`（含经 `chain_run` 间接使用）的用例补标记；`recon_common.needs_world` 与 `test_synthcity.needs_synth` 改为同时加 `needs_data` 与 skip 的装饰器；其余用模块级 `pytestmark` 或用例装饰器。主工作树 `--co -m needs_data` 在这些文件中收集到 62 个 |
| 6 | 夹具白等 120 s | `tests/recorder/test_processes.py` 的夹具启动 supervisor 后轮询就绪 120 s，无世界时 api 永远不就绪，最后才 skip | 标 `needs_data`；夹具开头先检查 `worlds/shenzhen` 或 `worlds/synthcity`（supervisor 的默认世界与 ADR-077 回退世界）是否存在，都没有就立即 skip |
| 7 | npm 包源 | `package-lock.json` 的 1,023 个 `resolved` 全是 registry.npmmirror.com，GitHub 托管机上从国内镜像拉包慢且不稳 | 锁文件不改；CI 设 `npm_config_registry` 与 `npm_config_replace_registry_host=always`。用只含 `cn@0.4.0` 的最小锁文件、空缓存实测：npm 10.9 改从 `registry.npmjs.org/cn/-/cn-0.4.0.tgz` 取包，integrity 校验通过 |
| 8 | 用例写死运行世界 | `tests/runtime/test_supervisor.py::test_zenoh_integration_sys_procs_ready_and_cli` 按 `awr/shenzhen/<run>` 查询；只有 synthcity 时 supervisor 回退（ADR-077），命名空间是 `awr/synthcity/<run>`，查询无回复 | 从 READY 行（`world=...`）取运行世界；查询循环在 20 s 内容忍 `BusTimeout` 重试 |
| 9 | 假子进程拼坏日志行 | `tests/runtime/fake_child.py` 在 `PYTHONUNBUFFERED=1` 下 `print` 分两次写入合并日志管道，api 的日志线程插入其间，`test_kill9_detected_fast_and_restarted_with_backoff` 偶发 `JSONDecodeError` | 一行一次 `write`（原子）；修复后该例在干净克隆中连续 5 次通过，第 2 轮整路也通过 |
| 10 | 文档与实现不一致 | AWR-19 §7.2、§7.3、§8.4 与 OPS-FR-013 写 `make setup` 生成车辆模型且模型不入库；实际 `SETUP_TARGETS` 只有 `contracts-check`，前端副本 `apps/web/public/models/{p600,p600_lowpoly}.glb` 已入库（THIRD_PARTY_NOTICES 也这样写） | 文档改为与实现一致；干净克隆不需要 Prometheus STL |

本包改动的测试文件只改标记、跳过、数据无关的断言与测试辅助的写法，不改变被测行为。主工作树（有六城与 synthcity）复核：`tests/rt/test_epoch.py`、`tests/recorder/test_runs_rest.py`、`tests/recorder/test_processes.py` 14 例通过；`test_supervisor.py` 的两例通过；上述 4 个 Vitest 文件 32 例通过；`make lint` 通过。

## 5 GitHub Actions 工作流

`.github/workflows/ci.yml`（工作流注释为英文，与 `.github/` 下其他文件一致）：

| 项 | 取值 |
|---|---|
| 触发 | push 到 `main`、所有 `pull_request`、`workflow_dispatch` |
| 并发 | `group: ci-<PR 号或 ref>`，`cancel-in-progress: true` |
| 权限 | `contents: read` |
| 运行环境 | `ubuntu-latest`；`actions/checkout@v7`、`actions/setup-python@v7`（3.12）、`actions/setup-node@v7`（`node-version-file: .nvmrc`，`cache: npm`）、`actions/cache@v6`（`~/.cache/uv`、`~/.cache/pip`，键为 `requirements.lock` 哈希）；版本号取自各 action 仓库 2026 年 6–7 月的最新发布 |
| 环境变量 | `npm_config_registry`、`npm_config_replace_registry_host`（§4 第 7 条）、`PYTHON_BOOT=python3.12` |
| `checks`（超时 30 min） | `make setup` → `make lint` → `make test-contracts` → `make typecheck` → `make test-web-unit` → `make build` → `make demo-world` → `make validate-demo-world` → `make test-demo-world` |
| `pytest`（矩阵 `shard: [1, 2]`，`fail-fast: false`，各超时 45 min） | `make setup` → `make test-py-nodata PYTEST_SHARD=<shard>` |
| 不含 | Vitest browser、Playwright、perf 与 chaos、`slow`、需要六城的 `needs_data`、GPU、UrbanScene3D |

`mk/hosted-ci.mk` 新增目标：`ci-nodata`（本机依次运行 lint、契约、tsc、pytest 无数据子集、Vitest unit、build）、`typecheck`、`test-py-nodata [PYTEST_SHARD=1|2]`、`test-web-unit`、`validate-demo-world`、`test-demo-world`。工作流里的每条命令都在第 3 节的干净克隆中按同样的写法执行过。没有 actionlint（不安装新依赖），工作流的结构用 PyYAML 解析核对。

## 6 文档变更

- **AWR-03**：§4.5 第 3 条补充托管 CI；§7.0 索引新增 ADR-078；附录 E 新增 ADR-078（背景、9 条决策、7 条备选、后果、实测）。
- **AWR-18**：§8.2 增加 G1h 的无数据纪律；§12.1 新增 G1h 行，"性能用例不进入 G0、G1、G1h"；§12.2 新增 `mk/hosted-ci.mk` 入口。
- **AWR-19**：§7.2 修正 `make setup` 行、新增 `make ci-nodata` 行；§7.3 删去车辆模型一行；§7.5 第 4 条合并门禁增加 G1h；§8.4 两行与 OPS-FR-013 改为车辆模型前端副本入库。
- **M15 PRD**：M15-FR-045 补充快照缺失时 `--check` 的行为。
- **CONTRIBUTING.md**：删去"没有托管 CI"，说明 G1 与 G1h 的分工和 `needs_data` 纪律。
- **README.md、README.zh-CN.md**：徽章行增加 CI 徽章（工作流首次运行前显示无状态）；"更多命令"表增加 `make ci-nodata`。

## 7 遗留与提示

1. **需要入库**：`python/awr/swarm/coverage/`（5 个文件）、`.github/workflows/ci.yml`、`mk/hosted-ci.mk` 以及本包修改的文件需要在下次提交中加入；在此之前 GitHub 上的仓库仍缺 `awr.swarm.coverage`，克隆后 sim-core 加载任务生成器会失败。
2. **托管机上未实测**：本包无法在 GitHub 托管机上运行。与本机的已知差异：4 vCPU（`cpu_pin_enabled` 在少于 8 核时本来就关闭，ci profile 也关闭钉核）、首次运行无 uv 与 npm 缓存、`/dev/shm` 与端口均由托管机提供。工作流首次运行后应核对两路 pytest 的墙钟：两路本机约 2:1，可先把 safety 从 `PYTEST_SHARD1_DIRS` 移到第 2 路；若较慢一路超过 45 min 的一半，再增加一路。
3. **`test_profiles` 的高模用例**在无 `vehicles/p600/model/p600.glb` 时跳过：高模由 `make vehicles-models` 从 Prometheus STL 生成（需下载），不属于 `needs_data` 的定义（UrbanScene3D 或世界），保持运行期 skip。
4. **S0 ci ×5 端到端**（`slow`）只在 G1 中运行；它的墙钟断言（≤ 120 s）在冷 numba 缓存与重负载下会超（本机 162 s），若以后要放进 G1h，需要先 `make numba-warm` 或调整该断言，后者属于 M16 的规格。
5. **Node 引擎提示**：`npm ci` 对 4 个包（eslint-scope、eslint-visitor-keys、react-doctor、oxlint-plugin-react-doctor）给出 EBADENGINE 警告（它们要求 Node ≥ 22.13，`.nvmrc` 是 22.12.0），只是警告，不影响安装与测试；升级 `.nvmrc` 属于 M00 依赖流程。
6. **对调度敏感的既有用例**：`tests/sim/test_checkpoint.py::test_background_capture_in_idle_windows`（M08）用 2 ms 真实睡眠模拟空闲窗口并断言 `forced == 0`，本机负载均值约 9 时偶发失败（§3）；托管机若也出现，建议由 M08 把窗口改为按调度时间判定，或把强制次数的断言放宽为"窗口足够长时"才检查。
7. **干净克隆目录** `.cache/cleanroom/`（约 1.8 GB，含 `.venv`、`node_modules` 与 synthcity）保留供复核，可随时删除；它在 `.cache/` 下，不进入待入库清单。
