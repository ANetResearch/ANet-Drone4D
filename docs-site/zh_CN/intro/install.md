# 安装与环境要求

本页列出在本机运行 ANet Drone4D 所需的环境、端口、目录与配置方式。逐条命令的操作过程见 {doc}`quickstart`。

## 服务器

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Linux x86-64（Ubuntu 22.04 或 24.04） | 只支持 x86-64：共享内存状态环 StateRing 依赖 x86 的 TSO 内存序，`make setup` 在其他架构上以退出码 13 拒绝 |
| Python | 3.12（带 venv 模块） | `make setup` 用它创建 `.venv`，并自举 uv 0.12.19 按 `requirements.lock` 锁定安装 |
| Node.js | 22.12.x（见仓库根目录 `.nvmrc`） | 前端构建与契约生成物校验；`make` 优先使用 `~/.local/node/bin` 下的 Node |
| 工具 | make、git、curl | — |
| 磁盘 | 约 20 GB 可用 | 依赖约 2 GB；合成演示城市约 100 MB；六城数据与世界包另需约 2 GB；`runs/` 默认配额 20 GB |
| 共享内存 | `/dev/shm` 至少 1 GiB 可用 | 状态环与运行目录位于 `/dev/shm/awr/<run>` |
| GPU | 不需要 | 物理、安全、任务、感知与环境都在 CPU 上计算 |
| 网络 | 首次安装需访问 PyPI 与 npm 仓库 | 之后可离线运行；`make setup OFFLINE=1` 使用本地缓存 |

CPU 核数没有硬性下限。8 核及以上且运行档不是 `ci` 时，supervisor 缺省把 sim-core、api 等进程钉在固定核上（`cpu.pin: auto`）；
与其他负载共用的机器上请关闭钉核，见 {ref}`zh-install-env`。

## 浏览器

任何支持 WebGL2 的现代浏览器都可以连接。桌面浏览器提供完整界面；平板采用横屏指挥器布局；手机采用五标签布局（见 {doc}`../guide/mobile`）。
观看端有硬件 GPU 时可以使用更高的画质档；没有 GPU 时以软件渲染运行，画质控制器会自动降低点预算。

## 端口

服务器缺省只监听回环地址 127.0.0.1。

| 端口 | 进程 | 用途 |
|---|---|---|
| 8000 | api（show-api） | 前端页面、REST 接口 `/api/...`、世界静态文件、`awr.rt.v1` 实时连接 |
| 8001 | api | 只在静态文件拆分回退启用时监听 |
| 8002 | sandbox-api | 沙盒 REST v1 `/api/sandbox/v1/...` 与沙盒实时连接（`sandbox` 运行档） |
| 7447 | zenoh 汇合点 | 进程间总线，只允许回环地址 |
| 5173 | vite | 开发服务器（`dev` 与 `sandbox` 运行档） |

同一台机器运行多份实例时，设置 `AWR_PORT_OFFSET=k`（0 至 31），上述端口统一加 `10·k`。例如 `AWR_PORT_OFFSET=3` 时 api 在 8030、
汇合点在 7477。每个工作树的覆盖项可以写入仓库根目录的 `.env.local`（不入库），`make` 会读入并导出其中的变量。

## 目录

| 目录 | 内容 | 是否入库 |
|---|---|---|
| `worlds/` | 构建得到的 World Package（`make demo-world`、`make worlds`） | 否 |
| `data/raw/` | 原始数据，例如 UrbanScene3D 采样点云（`make fetch-data`） | 否 |
| `runs/` | 每次运行的日志、录制、生效配置与管理令牌（`runs/<run>/`） | 否 |
| `var/` | 本机运行状态：沙盒池密钥与驻留会话、内部身份的锁定表 | 否 |
| `apps/web/dist/` | 前端生产构建 | 否 |
| `.venv/` | Python 依赖 | 否 |

`worlds/`、`data/raw/` 与 `runs/` 的位置可以分别用 `AWR_WORLDS_DIR`、`AWR_DATA_DIR` 与 `AWR_RUNS_DIR` 改写。

(zh-install-env)=
## 环境变量

配置的优先级为：命令行 `--set 键=值` > `AWR_*` 环境变量 > `configs/runtime.yaml` 中的运行档 > 该文件的基础值 > 内置缺省值。
未知键与类型错误以退出码 2 拒绝启动并指出键路径。常用变量如下：

| 变量 | 作用 | 缺省 |
|---|---|---|
| `AWR_PROFILE` | 运行档（见下表） | `make run` 为 `demo`，`make dev` 为 `dev` |
| `AWR_WORLD`、`AWR_SCENARIO` | 启动的世界与剧本 | 深圳与 S1；深圳未构建而合成演示城市已构建时回退到 synthcity 与 S0 |
| `AWR_PORT_OFFSET` | 端口偏移 k（0 至 31，端口加 10·k） | 0 |
| `AWR_BIND`、`AWR_ORIGINS` | 监听地址与允许的来源；局域网演示时两者同时设置 | `127.0.0.1`、空 |
| `AWR_CPU_PIN` | 钉核：`auto`、`on`、`off` | `auto` |
| `AWR_SANDBOX_ENABLED`、`AWR_SANDBOX_PORT` | 沙盒开关与 sandbox-api 端口 | 随运行档；8002 |
| `AWR_RUNS_QUOTA_GB` | `runs/` 配额 | 20 |

## 运行档

| 运行档 | 进程组 | 用途 |
|---|---|---|
| `demo` | core、ext | `make run` 的缺省：共享展示加录制、智能体运行时与作业进程 |
| `dev` | core、ext、dev | `make dev`：另启动 vite 开发服务器，支持热更新 |
| `sandbox` | core、ext、dev、sandbox | 本机沙盒：另启动 sandbox-pool 与 sandbox-api，配额档 `local`（无前缀配额与工作量证明），批处理开放 |
| `ci`、`perf` | core、ext | 功能门禁与性能用例（端口偏移 9） |
| `public` | core、sandbox | 公开演示站（见 {doc}`../topics/deployment`） |

其中 core 为 sim-core 与 api，ext 为 recorder、replay-worker、agent-runtime 与 job-worker，sandbox 为 sandbox-pool 与 sandbox-api，
dev 为 vite。

## 可选数据：UrbanScene3D 六城

不下载任何数据即可运行：`make demo-world` 按固定种子生成合成演示城市 ANet Synthetic City。要在本机加入深圳、上海、纽约、旧金山、
苏州与芝加哥六城，需要下载 UrbanScene3D 采样点云并接受其条款（只允许非商业使用，禁止再分发原始数据或其改动版本）：

```sh
make fetch-data   # 下载 253 MB，解压约 720 MB，逐个校验 sha256
make worlds       # 在本机构建六城的 World Package，约 1 至 2.5 min
```

授权与引用见 {doc}`../topics/worlds`。

## 从另一台机器访问

服务器只监听回环地址。在本地电脑上转发端口后，用同一个地址访问：

```sh
ssh -N -L 8000:127.0.0.1:8000 -o ExitOnForwardFailure=yes -o Compression=no <user>@<server>
```

局域网演示时，显式开放端口并登记来源：

```sh
AWR_BIND=0.0.0.0 AWR_ORIGINS=http://<server-ip>:8000 make run
```

## 环境诊断

`make doctor` 检查架构、Python 与 Node 版本、依赖锁定、端口、共享内存、磁盘、原始数据与默认世界，并对每项不通过给出修复命令；
`make doctor DEEP=1` 另做 sha256 与世界深度校验。`make setup` 结束时会自动执行一次快速诊断，输出示例见 {doc}`quickstart`。

## 构建本文档

文档站使用 Sphinx、sphinx-rtd-theme 与 MyST Markdown，源文件在仓库的 `docs-site/` 下（`zh_CN/` 与 `en/` 两套同构）。
依赖固定在 `docs-site/requirements.txt`，安装在独立的虚拟环境 `.venv-docs` 中，不影响项目主依赖：

```sh
make docs-site            # 构建两种语言到 docs-site/_build/html/{en,zh-cn}/latest，并生成根跳转页
make docs-site-linkcheck  # 链接检查，结果写入 docs-site/_build/linkcheck/
```
