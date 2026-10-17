# 快速开始

> 克隆、安装、生成世界、运行共享展示与沙盒，并用 Python SDK 操作一个会话

本页从"机器上还没有源码"开始，到"我在本机运行了共享展示，并用程序创建、操作并结束了一个沙盒会话"为止。终点是三样具体的东西：
本机一个运行中的 World Runtime（共享展示，浏览器可访问）、一个本机沙盒控制面（sandbox-pool 与 sandbox-api），以及一段经开放接口
完成"创建会话、添加无人机、读取状态、结束会话"的 Python 程序。

下面每一段输出都来自 2026-10-13 在一台 8 核 x86-64 开发机（Ubuntu、Python 3.12.3、Node 22.12.0、无 GPU）上按本页步骤从
`git clone` 开始的真实运行。为便于阅读，仓库路径统一写成 `~/ANet-Drone4D`，端口写成默认值（8000、8002、7447）；运行编号、
进程号、会话编号与耗时会随机器不同。

:::{note}
不想安装？[公开实例](https://drone4d.agentnetwork.org.cn)提供同一版本的共享展示与沙盒，见 {doc}`../guide/showcase`。环境要求、端口与
配置方式的完整说明见 {doc}`install`。
:::

---

## 前提条件

| 项 | 要求 |
|---|---|
| 系统 | Linux x86-64（Ubuntu 22.04 或 24.04） |
| 工具 | Python 3.12（含 venv）、Node 22.12.x、make、git、curl |
| 资源 | 约 20 GB 可用磁盘，`/dev/shm` 至少 1 GiB 可用；不需要 GPU |
| 网络 | 首次安装需访问 GitHub、PyPI 与 npm 仓库 |

## 获取源码：`git clone`

```sh
cd ~
git clone https://github.com/ANetResearch/ANet-Drone4D.git
cd ANet-Drone4D
```

```text
Cloning into 'ANet-Drone4D'...
Updating files: 100% (3245/3245), done.
```

本次运行的工作树约 126 MB，克隆用时 5.8 s。仓库不包含任何点云、世界包或运行产物：`worlds/`、`data/raw/` 与 `runs/` 都在
`.gitignore` 中，由后续步骤在本机生成。

`main` 分支是持续维护的分支；需要与已发布版本逐字节一致时，切到版本标签：

```sh
git checkout v0.2.0
```

## 安装依赖：`make setup`

```sh
make setup
```

```text
创建 .venv（python3.12）
自举 uv 0.12.19
cd ~/ANet-Drone4D && ~/ANet-Drone4D/.venv/bin/uv pip sync requirements.lock --python ~/ANet-Drone4D/.venv/bin/python
Resolved 70 packages in 757ms
Uninstalled 1 package in 118ms
Installed 69 packages in 174ms
 + annotated-doc==0.0.5
 + annotated-types==0.8.0
 ...
 + zstandard==0.25.0
cd ~/ANet-Drone4D && ~/ANet-Drone4D/.venv/bin/pip install --quiet -e . --no-deps

added 829 packages in 36s
gen.py: up to date (layout_id 0x009600AE, contracts 1.2.0)
gen_golden.py: up to date
gen_frames_golden.py: 4106 cases, up to date
gen_env_golden.py: 10261 cases, up to date
gen_fixtures.py: up to date
gen.mjs: up to date (layout_id 0x009600AE, contracts 1.2.0)
DOC-01  OK    CPU 架构：x86_64
DOC-02  OK    Python 3.12：3.12.3
DOC-04  OK    Node 22.12.x：22.12.0
DOC-08  OK    依赖版本（含 zenoh 1.10.1）：与 pyproject 锁定一致
DOC-22  OK    配置与访问模式：profile=dev access=loopback bind=127.0.0.1
DOC-09  OK    端口空闲：[8000, 7447, 5173] 空闲
DOC-10  OK    /dev/shm 可写且 ≥ 1 GiB：/dev/shm/awr free=31.4 GiB writable=True
DOC-11  OK    残留运行目录：无
DOC-12  OK    磁盘余量：free=18.7 GB
DOC-14  WARN  原始数据：0 个 PLY（~/ANet-Drone4D/data/raw/urbanscene3d）
        修复：make fetch-data
DOC-15  WARN  默认世界：~/ANet-Drone4D/worlds/shenzhen/world.json 尚未构建（make run 会自动构建）
        修复：make worlds
M11-R1  OK    StateRing 自测：publish/attach/read_latest/header
M11-R2  OK    zenoh 自环：query/reply 成功（35 ms）
awr doctor --quick: 通过（0.2 s）
make setup 完成
```

`make setup` 是幂等的，按顺序做四件事：

1. **Python 依赖。** 用 `python3.12` 创建 `.venv`，自举固定版本的 uv，再以 `uv pip sync requirements.lock` 按锁文件（带哈希）安装
   70 个包，最后以可编辑方式安装本仓库的 `awr` 包。上面省略了 69 行安装清单。
2. **前端依赖。** `npm ci` 按 `package-lock.json` 安装；锁文件未变化且上次安装成功时跳过（`FORCE=1` 强制重装）。
3. **契约生成物校验。** 75 份 JSON Schema 生成的 Python 与 TypeScript 类型、二进制布局与 golden 用例必须与仓库中的版本一致
   （`up to date`）；不一致说明工作树被改动过，按提示执行 `make contracts` 重新生成。
4. **快速诊断。** `awr doctor --quick` 逐项检查环境，`WARN` 不阻塞，`ERROR` 以非零退出码失败并给出修复命令。

本次运行用时 73 s，uv 与 npm 的本地缓存已存在；首次在新机器上安装需要下载依赖，通常为 3 至 8 min。

### 输出中的告警

两项 `WARN` 在没有 UrbanScene3D 数据的机器上是预期的：DOC-14 说明 `data/raw/` 中没有六城原始数据，DOC-15 说明缺省世界深圳尚未构建。
下一步生成的合成演示城市会成为回退世界。

完整输出中还有两类与功能无关的告警，上面未列出：npm 对 `react-doctor`、`eslint-scope` 等四个开发期工具包给出的
`EBADENGINE`（它们声明需要 Node 22.13 或更高，不参与构建与运行），以及 FastAPI 生成 OpenAPI 时，对唯一一条同时服务 `GET` 与 `HEAD`
的接口（`segment_file`，运行分段下载）给出的 `Duplicate Operation ID` 提示。生成的操作 id 以 `_get` 或 `_head` 结尾取决于解释器的
哈希种子，两次运行可能不同（`segment_file_api_runs__run__segments__name__get` 或 `..._head`）；两种形式都是预期且无害的，已提交的
OpenAPI 快照不受影响（`tools/contracts/gen_openapi.py` 会将其规范化）。

## 生成合成演示城市：`make demo-world`

```sh
make demo-world
```

```text
[worldpkg] synthcity: generate (missing; synthetic, ADR-077)
[worldpkg] 1-minute loadavg 7.2 > 4: --jobs reduced to 1 (M03 R-2)
[synthcity] ingest    done  13.56 s points=3924500
[synthcity] dsm       done   3.30 s
[synthcity] tile      done   5.30 s
[synthcity] tile summary roots=1 nodes=439 depth=5 first_screen_g=190507/2286084
[synthcity] zones     done   0.15 s
[synthcity] hash      done   0.73 s
[synthcity] validate  done   1.85 s
[synthcity] published 9e05c2364f58 in 25.3 s
OK   synthcity     exit=0 cv=9e05c2364f58 built 25.3 s
```

ANet Synthetic City 按固定种子程序生成：1.2 km × 1.2 km、约 390 万点，含道路、河道、公园与两座 300 m 级塔楼，不含任何第三方数据。
它走与真实城市相同的流水线：

| 阶段 | 产物 |
|---|---|
| ingest | 规范化后的 World ENU 点（米、右手、+Z 向上）与逐点属性 |
| dsm | 2 m 数字表面模型与 10 m 数字地面模型 |
| tile | Potree 2.0 兼容八叉树（ANET_Q16 点编码），以及首屏层级的字节范围 |
| zones | 剧本使用的区域与禁飞区（`zones.geojson`） |
| hash | 内容版本 `cv`（这里为 `9e05c2364f58`），用于缓存与录制兼容判定 |
| validate | 语义与结构校验，错误为 0 才发布 |

第二行说明构建时 1 分钟负载高于 4，构建器自动把并行度降为 1，避免挤占同机的其他工作；空闲机器上不会出现这一行。
`--missing` 语义保证重复执行时只重建缺失或失效的世界。

## 启动共享展示：`make run`

```sh
make run
```

```text
DOC-01  OK    CPU 架构：x86_64
...
awr doctor --quick: 通过（0.2 s）
~/ANet-Drone4D/.venv/bin/python -m awr.sim.runtime.warm
numba warmup s plugins=2.04 m08=0.34 m10_plan=0.06 m20_cover=0.01 m04_geom=0.06
m10 numba warmup s 1.77
[worldpkg] chicago: 原始数据缺失（~/ANet-Drone4D/data/raw/urbanscene3d/Chicago_sampled_5m.ply）；修复：make fetch-data
...
[worldpkg] synthcity: up to date
FAIL chicago       exit=4 0.0 s chicago: 原始数据缺失（~/ANet-Drone4D/data/raw/urbanscene3d/Chicago_sampled_5m.ply）；修复：make fetch-data
...
OK   synthcity     exit=0 cv=9e05c2364f58 0.0 s
提示：原始数据缺失，运行 make fetch-data（无数据试用：make demo-world 生成合成演示城市 synthcity，ADR-077）
提示：有城市缺少原始数据，运行 make fetch-data
提示：默认世界为合成演示城市 synthcity（/world/synthcity，剧本 s0-synthcity-showcase）
警告：部分世界构建失败（退出码 4），其余照常；详情见 worlds/.status/
[synthcity] geo warm hit 0.02 s derive_sha8=d98119e8 empty=0.01633 pits_filled=1.0 mem_mib=6.5 grid25=[301, 301]
cd ~/ANet-Drone4D && AWR_PROFILE=${AWR_PROFILE:-demo} exec ~/ANet-Drone4D/.venv/bin/python -m awr.runtime.supervisor
提示：默认世界 shenzhen 未构建，回退到合成演示城市 synthcity（剧本 s0-synthcity-showcase；ADR-077）；有 UrbanScene3D 数据时运行 make fetch-data && make worlds
READY run=r20261013-143216-2988 profile=demo world=synthcity access=loopback
  api      http://127.0.0.1:8000/world/synthcity
  bus      tcp/127.0.0.1:7447（namespace awr/synthcity/r20261013-143216-2988）
  logs     ~/ANet-Drone4D/runs/r20261013-143216-2988/logs
  ssh      ssh -N -L 8000:127.0.0.1:8000 <user>@<host>
  admin    ~/ANet-Drone4D/runs/r20261013-143216-2988/admin.token
```

`make run` 在前台运行，按 Ctrl+C 或在另一个终端执行 `make stop` 停止。启动前它依次完成：

1. **预检。** 再次执行快速诊断；端口被占用等 `ERROR` 会在这里以退出码 3 中止（见 {ref}`zh-qs-troubleshooting`）。
2. **numba 预热。** 把飞行、规划、覆盖与几何内核编译进 `~/.cache/awr/numba`。上面是第二次启动（缓存命中，合计约 4 s）；同一台机器
   第一次启动时为全量编译，本次在负载较高的 8 核机器上为 `plugins=103.73 m08=57.76`（单位 s）。
3. **世界构建。** 对全部七个世界执行 `--missing` 构建；六城缺少原始数据时标为失败（退出码 4）但不阻塞，只要缺省世界可用即可继续。
   `geo warm` 生成规划用的 25 m 栅格等派生缓存。
4. **生产构建。** `apps/web/dist` 不存在或比源码旧时执行 `npm run build -w apps/web`（本次首次构建 vite 用时 5.8 s），随后
   `prod-bundle-scan` 检查产物中不含部署机密。上面的第二次启动跳过了这一步。
5. **启动 supervisor。** 按运行档 `demo` 拉起 sim-core、api、recorder、agent-runtime 与 job-worker，全部就绪后打印 `READY`。

`READY` 块的五行依次是：浏览器入口、进程间总线与命名空间、本次运行的日志目录、远程访问用的 SSH 转发命令，以及管理令牌文件
（操作员界面与 `/api` 的受保护接口使用，文件权限 0600，不要分享）。

supervisor 的结构化日志（每行一个 JSON 对象，例如 `proc state RUNNING`）与上述输出交错打印，这里省略。sim-core 缺少
`CAP_SYS_NICE` 时会输出一行 `nice not permitted, using 0`，不影响运行。

## 确认服务就绪：`make status`

在另一个终端中：

```sh
make status
```

```text
~/ANet-Drone4D/.venv/bin/awr status
run r20261013-143216-2988  supervisor pid 2305695
ring  head=3231  t_sim_s=25.84  state=PLAYING  rate=1.0  epoch=1  segment=0  writer_pid=2305735  writer_age_ms=3.9  step_seq=6460  step_p99_us=3199  rtf=0.997  roster_version=9
NAME           STATE    PID      RESTARTS  UPTIME_S  HB_AGE_MS  CPU%  RSS_MB
sim-core       RUNNING  2305735  0         31.4      7.2        17.9  245.1
api            RUNNING  2305831  0         28.5      10.6       3.0   93.2
recorder       RUNNING  2305833  0         28.5      10.3       7.0   57.9
replay-worker  STOPPED  -        0         -         -          -     -
agent-runtime  RUNNING  2305839  0         28.5      79.4       4.0   54.8
job-worker     RUNNING  2305841  0         28.4      323.5      -     54.9
```

`ring` 一行读自共享内存状态环的头部：`t_sim_s` 是仿真时间，`state=PLAYING` 表示时钟在走，`rtf` 是实时因子（1.0 为与墙钟同速），
`step_p99_us` 是最近窗口内单步耗时的 p99。进程表中 `RESTARTS` 应为 0；`replay-worker` 按需启动，平时为 `STOPPED`。

就绪探针与健康接口不需要令牌：

```sh
curl -s http://127.0.0.1:8000/api/health/ready
curl -s http://127.0.0.1:8000/api/health
```

```text
{"status":"ready","sim":"ok","ring_attached":true,"world_loaded":true,"run_id":"r20261013-143216-2988"}
{"status":"ok","build":{"version":"0.2.0","channel":"local","release":"local-06731a8f76"}}
```

`release` 中的 `06731a8f76` 是构建时的提交短哈希，与浏览器启动屏上的版本行一致。

## 在浏览器中打开

打开 **http://localhost:8000/world/synthcity**。本机没有 UrbanScene3D 数据时，合成演示城市是缺省世界，运行剧本 S0：七架 P600
分上下两段螺旋扫描 318 m 的 ANet Tower、两架覆盖两个街区、三架以 V 形编队沿河巡航，天气从晴转小雨再转雾（约 5 min 仿真时间）。

服务器在远程机器上时，在本地电脑执行 `READY` 块中的 SSH 转发命令，再打开同一个地址。界面的使用方式见 {doc}`../guide/showcase`。

## 停止：`make stop`

```sh
make stop
```

```text
~/ANet-Drone4D/.venv/bin/awr stop
已停止 r20261013-143216-2988
```

`awr stop` 经 supervisor 的控制通道优雅停止全部进程（每个进程 5 s 宽限期），删除 `/dev/shm/awr/<run>` 下的运行目录，
`runs/<run>/` 中的日志与录制保留。前台的 `make run` 随之退出。

## 启动沙盒服务：`AWR_PROFILE=sandbox make run`

```sh
AWR_PROFILE=sandbox make run
```

```text
...
cd ~/ANet-Drone4D && AWR_PROFILE=${AWR_PROFILE:-demo} exec ~/ANet-Drone4D/.venv/bin/python -m awr.runtime.supervisor
提示：默认世界 shenzhen 未构建，回退到合成演示城市 synthcity（剧本 s0-synthcity-showcase；ADR-077）；有 UrbanScene3D 数据时运行 make fetch-data && make worlds
READY run=r20261013-144754-296c profile=sandbox world=synthcity access=loopback
  api      http://127.0.0.1:8000/world/synthcity
  bus      tcp/127.0.0.1:7447（namespace awr/synthcity/r20261013-144754-296c）
  logs     ~/ANet-Drone4D/runs/r20261013-144754-296c/logs
  ssh      ssh -N -L 8000:127.0.0.1:8000 <user>@<host>
  admin    ~/ANet-Drone4D/runs/r20261013-144754-296c/admin.token
```

运行档 `sandbox` 在共享展示之外再启动沙盒控制面，`make status` 的进程表中多出三行：

```text
sandbox-pool   RUNNING  2320617  0         17.3      67.2       6.0   140.7
sandbox-api    RUNNING  2320754  0         15.6      18.4       6.9   92.9
vite           RUNNING  2320756  0         15.6      -          -     73.3
```

- **sandbox-pool** 负责会话的准入、排队、驻留与回收，并为每个会话拉起一个独立的 sim-core（沙盒时钟档 sandbox100）。
- **sandbox-api** 在 `http://127.0.0.1:8002/api/sandbox/v1` 提供沙盒 REST v1 与实时连接，即 SDK 的缺省地址。
- **vite** 是开发服务器，随运行档中的 dev 进程组启动。

本机配额档为 `local`：不做地址前缀配额与工作量证明，批处理端点开放。

## 查询沙盒接口

```sh
curl -s http://127.0.0.1:8002/api/sandbox/v1/meta
```

```text
{"access_mode":"loopback","active_sessions":0,"api_version":1,"build":{"channel":"local","release":"local-06731a8f76","version":"0.2.0"},"contracts":"1.2.0","features":["queue","batch"],"max_sessions":1,"pow":{"bits":20,"required_now":false},"queue_len":0,"sdk":{"min_version":"1.0.0"}}
```

`max_sessions` 是本机可同时运行的沙盒会话数，由容量模型按 CPU 预算计算（硬上限 6）；本机为 1，第二个会话会进入排队。
`pow.required_now=false` 表示当前不要求工作量证明。全部端点见 {doc}`../api/rest`。

## 用 Python SDK 操作一个会话

SDK 是单文件 `tools/sdk/awr_sandbox.py`，依赖 httpx、websockets 与 msgpack，`.venv` 中已经具备。在仓库根目录创建
`sdk_quickstart.py`：

```python
"""快速开始：创建沙盒会话、添加无人机、读取状态、结束会话。"""
import sys
import time

sys.path.insert(0, "tools/sdk")  # 仓库内的单文件 SDK

from awr_sandbox import Sandbox

sb = Sandbox("http://127.0.0.1:8002", timeout_s=30)
meta = sb.meta()
print("server:", meta["build"]["version"], "api", meta["api_version"], "max_sessions", meta["max_sessions"])
print("worlds:", [w["world_id"] for w in sb.worlds() if w["available"]])

with sb.create("synthcity", "t5_area_guard") as s:
    info = s.state()
    print("session:", s.sid, info["state"], "expires_in_s", info["expires_in_s"])
    print("template:", info["template_id"], "vehicles", len(s.vehicles.list()["items"]),
          "targets", len(s.targets.list()["items"]), "nests", len(s.nests.list()["items"]))

    v = s.vehicles.add("p600_mid360", nest_id="nest-b", label="docs-1")
    print("added:", v)

    for _ in range(20):  # 新加入的机体在数秒内完成启动
        snap = s.vehicles.state(v["id"])
        st, ext = snap["state"], snap.get("state_ext") or {}
        if ext.get("battery"):
            break
        time.sleep(0.5)
    print("state:", st["lifecycle"], st["flight_state"], "pos_enu_m", st["pos_enu_m"],
          "soc_pct", ext["battery"]["soc_pct"])

print("closed:", s.sid)
```

```sh
.venv/bin/python sdk_quickstart.py
```

```text
server: 0.2.0 api 1 max_sessions 1
worlds: ['synthcity']
session: sb-da5ygnnr3z READY expires_in_s 1795
template: t5_area_guard vehicles 7 targets 2 nests 2
added: {'id': 'p600-07', 'agent_no': 7, 'lifecycle': 'STARTING', 'model_id': 'p600_mid360', 'nest_id': 'nest-b'}
state: BOOTED DISARMED pos_enu_m [-120.9, -339.7, 0.0] soc_pct 100.0
closed: sb-da5ygnnr3z
```

全程 6 s。逐行说明：

1. **`Sandbox(...)`** 连接时先读取 `GET /meta`，校验接口主版本与契约主版本，不兼容时立即报错。`timeout_s=30` 把单次请求超时
   设为 30 s：服务器满员时创建请求进入排队，SDK 以单次最长 25 s 的长轮询等待空位，超时必须大于它（见 {ref}`zh-qs-troubleshooting`）。
2. **`worlds()`** 返回七个世界及其可用性。本机只构建了合成演示城市，因此只有 `synthcity` 可用。
3. **`create("synthcity", "t5_area_guard")`** 创建会话并等待其就绪：需要时自动求解工作量证明，满员时自动排队并在轮到时认领。
   T5 区域值守与接力是缺省场景模板，装配 2 个机巢（机巢箱与垂起起降台）、7 架无人机（P600 与 AWR-V1）与 2 个识别物。
   会话初始寿命 30 min（`expires_in_s` 约 1,800）。
4. **`vehicles.add("p600_mid360", nest_id="nest-b")`** 添加一架 MID-360 配置的 P600，放入垂起起降台 `nest-b`。`nest_id` 与
   `home_enu_m`（World ENU 坐标）必须且只能给出一个。返回的 `lifecycle` 为 `STARTING`，机体在数秒内完成启动。
5. **`vehicles.state(vid)`** 返回完整状态：`BOOTED DISARMED` 表示已启动、未解锁、停在机巢；`pos_enu_m` 是 World ENU 坐标（米），
   `state_ext.battery.soc_pct` 是电量百分比。
6. **离开 `with` 块** 时 SDK 先导出一次配置快照（结束后 10 min 内仍可取回），再以 `DELETE /sessions/{sid}` 结束会话、释放槽位。

本机沙盒的浏览器界面需要把 `/api/sandbox/` 路径分流到 8002 端口（公开实例由 nginx 完成，见 {doc}`../topics/deployment`）；
本页的步骤只使用开放接口。

## 停止沙盒服务

```sh
make stop
```

```text
~/ANet-Drone4D/.venv/bin/awr stop
已停止 r20261013-144754-296c
```

sandbox-pool 停止时先停止接受创建，再为仍在运行的会话导出配置快照并以 `maintenance` 结束，在 12 s 宽限期内完成。

---

(zh-qs-troubleshooting)=
## 排错

### 启动时报"端口空闲"错误

```text
错误（退出码 3）：DOC-09 端口空闲
修复：ss -ltnp | grep -E ':(7447)'；或设置 AWR_PORT_OFFSET=<k>
...
awr doctor --quick: 失败（0.2 s）
make[1]: *** [Makefile:83: _need-supervisor] Error 3
make: *** [Makefile:68: run] Error 2
```

预检发现端口被占用。本次运行中，它出现在 `make stop` 之后立即再次 `make run` 时：上一次运行的总线端口尚未释放，数秒后重试即通过。
端口确实被其他实例占用时，按提示查看占用者，或以 `AWR_PORT_OFFSET=<k>` 让本实例的全部端口平移 `10·k`。

### sim-core 反复被判定为挂起

supervisor 日志中连续出现：

```text
{"lvl":"WARN","proc":"supervisor","logger":"awr.runtime.supervisor","msg":"proc hung","kv":{"name":"sim-core","heartbeat_age_ms":2027}}
{"lvl":"INFO","proc":"supervisor","logger":"awr.runtime.supervisor","msg":"standby promoted","kv":{"name":"sim-core","pid":2302046,"restarts":1}}
```

（为便于阅读，删去了时间戳等字段。）sim-core 的心跳超过 2 s 未更新，supervisor 判定挂起，用热备用进程接替。窗口内第 6 次重启会触发熔断。
本次运行中的原因是：同机另一份实例也把它的 sim-core 钉在 1 号核上，两个 250 Hz 主循环争用同一个核。8 核及以上的机器缺省启用钉核；
与其他负载共用的机器上请关闭：

```sh
AWR_CPU_PIN=off make run
```

也可以把 `AWR_CPU_PIN=off` 写入仓库根目录的 `.env.local`，对之后的每次 `make` 生效。

### 添加无人机返回 110 PARAM_OUT_OF_RANGE

```text
awr_sandbox.AwrError: 110 PARAM_OUT_OF_RANGE: 参数越界或超过上限
```

`vehicles.add()` 既没有给 `nest_id` 也没有给 `home_enu_m`（或两者都给了）。错误的 `detail` 为
`{"field": "nest_id|home_enu_m", "why": "EXACTLY_ONE"}`。二者择一即可。

### 添加无人机返回 525 NEST_INCOMPATIBLE

```text
awr_sandbox.AwrError: 525 NEST_INCOMPATIBLE: 机巢类型、充电器节数或槽位不兼容
```

`detail` 为 `{"why": "slots", "slots": 6}`：T5 模板中的机巢箱 `nest-a` 已放满 6 架。改放 `nest-b`，或先用 `s.nests.list()` 查看各机巢的
槽位与库存。机巢箱只接收多旋翼，垂起起降台接收垂起机型与多旋翼。

### 创建会话返回 409 SANDBOX_EXISTS

```text
awr_sandbox.AwrError: 409 SANDBOX_EXISTS: an active session already exists for this principal and address prefix; attach() with its token or create(force_new=True)
```

同一主体与地址前缀已有一个未结束的会话（例如上一次脚本异常退出而未离开 `with` 块，会话处于运行或驻留状态）。持有其令牌时用
`sb.attach(sid, token)` 接回；否则以 `create(..., force_new=True)` 新建，或等待旧会话因无连接空闲而被驻留并过期。SDK 的主体提示
保存在 `~/.config/awr_sandbox/principal`，只用于幂等与配额聚合，不是凭据。

### 排队时出现 httpx.ReadTimeout

```text
httpx.ReadTimeout: timed out
```

服务器满员，创建请求进入排队；SDK 1.1.0 及更早版本以单次最长 25 s 的长轮询等待，却沿用 `Sandbox()` 缺省的单次请求超时 10 s。
升级到 SDK 1.1.1（长轮询使用单独的读超时），或在创建 `Sandbox` 时传入 `timeout_s=30`（本页示例的写法）。

### 六城构建失败的提示

`make run` 输出 `FAIL chicago exit=4` 等六行与"部分世界构建失败（退出码 4）"是没有 UrbanScene3D 数据时的预期结果，不影响合成演示城市。
需要六城时执行 `make fetch-data && make worlds`，见 {doc}`install`。

更多问题见 {doc}`../project/faq`。

## 这一步之后你有什么、没有什么

**有了：** 一份锁定依赖的本机安装；一个可自由分享的合成世界 `worlds/synthcity`；能在浏览器中观看并在 `make status` 中检视的
World Runtime；一个本机沙盒控制面；以及一段经开放接口完成会话全生命周期的程序。每次运行的日志与生效配置都在 `runs/<run>/` 下：

```text
~/ANet-Drone4D/runs/r20261013-144754-296c/
├── logs/                    # 每个进程一个日志文件：sim-core.log、api.log、sandbox-pool.log 等
├── effective-config.yaml    # 生效配置（机密已掩码）
├── admin.token              # 管理令牌（0600）
├── rec-000.mcap             # 共享展示的录制（MCAP），可在本机回放
├── inputs.msgpack           # 输入日志，可据此确定性地重新仿真
├── audit.jsonl              # 审计记录
└── ckpt/                    # 崩溃恢复用的 checkpoint
```

**还没有：** UrbanScene3D 六城（需要下载数据并接受其条款）；本机沙盒的浏览器界面（需要按部署套件做路径分流）；以及 GPU 重建、
真实飞控栈等后续版本的能力。下一步可以：

- 阅读 {doc}`../guide/sandbox`，了解场景模板、管理面板与手动控制。
- 阅读 {doc}`../api/sdk`，用 `tools/sdk/examples/` 中的示例驱动手动飞行、传感器、识别物捕获与 GSD 扫描预览。
- 阅读 {doc}`../topics/architecture`，了解进程拓扑与坐标约定。
