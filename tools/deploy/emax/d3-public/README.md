# emax 公开样例发布线：`/lab/` 与 `/lab-static/`（D3-AC-21）

本目录是 D3 公开只读样例在 ANet4D 公开站点 emax（`https://anet4d.agentnetwork.org.cn`；旧域名 `drone4d.agentnetwork.org.cn` 只做
301）上的发布、校验、回滚与演练材料，对应
[AWR-05](../../../../docs/05-D3-设计增补与决策记录.md) §7.9 实现约束 1–5、Q-D3-02、ADR-129、ADR-184、ADR-194、ADR-201，
[AWR-17](../../../../docs/17-接口与通信协议说明书.md) §18.8.5 与 M27-FR-134、FR-136、FR-138。它是脚本目录，不是 Python 包；
脚本只用标准库（emax 系统 Python 3.12 即可运行），演练脚本 `drill.py` 例外（需要仓库 `.venv` 与 docker）。

公开站主构建保持 D2 标签构建（ADR-194 (5) ⑤）：本目录不修改公开站点文件 `anet4d.agentnetwork.org.cn.conf`（下文称 D2 站点文件）
与 `install.sh`，只在 D2 站点文件上插入两段增补，
得到"D2 + /lab/"站点文件；回滚到 D2 就是重新安装 D2 站点文件。公开站不运行任何 D3 进程（公开组合根 `AWR_D3_ENABLED = 0`），
`/api/bench/*` 一律 404，只读页只发起 `/lab/` 与 `/lab-static/` 的静态 GET。

| 文件 | 用途 |
|---|---|
| `lab-http.conf` | http 层增补：`/lab-static/v/` 的并发区（每地址前缀 4、全站 32）、整段与分段下载的 `limit_rate_after` 映射、GET/HEAD 判定 |
| `lab-server.conf` | 443 server 增补：`/api/bench` 404、`/lab/`（SPA 回退、assets immutable）、`/lab-static/index.json`（no-cache）、`/lab-static/v/<ver>/`（immutable、限速与并发上限） |
| `labsite.py` | `render`（D2 站点文件 + 两段增补）、`check`（emax 上复核待安装的站点文件）、`strip`（还原 D2 站点文件）；`localize` 供演练与测试 |
| `labctl.py` | 发布线：`index`、`check`、`push`、`activate`、`rollback`、`enable-site`、`withdraw`、`status`、`budget`、`pin` |
| `verify.py` | 上线后的验收检查：站点检查（bench404、lab、static、rate、conn_ip、conn_all、nmax、回滚后的 d2）与本机检查（root、disk、content） |
| `nginx-check.sh` | 开发机：一次性 Ubuntu 24.04 容器（nginx 1.24）中 `nginx -t` 两种站点文件（各自与旧域名跳转站点同时启用），并在真实 nginx 上跑端到端用例与 verify.py |
| `drill.py` | 开发机：cgroup 模拟演练（D2 栈 + 容器 nginx + 16 路并发下载 + WS 推送时延 + D2-AC-04 负载 + 回滚演练） |

**硬性约束（沿用 [D2 部署手册](../README.md) 的约束 1、4）：** 只新增与替换本站点的文件，不改 `nginx.conf` 与其他站点；
增补段的区名、map 变量一律带 `anet4d_lab` 前缀；`enable-site` 与 `withdraw` 只写本站点的 `sites-available` 文件（缺省
`/etc/nginx/sites-available/anet4d.agentnetwork.org.cn.conf`），先 `nginx -t`，失败即恢复原文件且不 reload；写入内容的
`server_name` 与站点文件名的域名不一致时拒绝（防止把更名前保存的旧站点文件装进新域名的站点文件）。旧域名的跳转站点不插入增补段。
`/srv/drone4d/` 与 `drone4d.lab_static_index.v1` 是沿用的遗留标识（服务器目录与契约 id，不随产品更名）。任何密钥不进入版本目录、索引、日志与本目录。

## 1 emax 上的目录

| 路径 | 内容 |
|---|---|
| `/srv/drone4d/lab/<build>/` | `/lab/` 独立静态构建（`make lab-build`，`VITE_AWR_LAB_ONLY = 1`、`base = /lab/`），目录名为 `build-info.json` 的 release |
| `/srv/drone4d/lab/current` | 指向当前构建的符号链接（原子替换） |
| `/srv/drone4d/lab-static/index.json` | 当前版本的 `drone4d.lab_static_index.v1`（契约 `packages/contracts/bench/lab_static_index.v1.schema.json`），no-cache，写临时文件后 rename |
| `/srv/drone4d/lab-static/v/<YYYY.MM.DD-n>/` | 版本目录（immutable，只增不改）：`LICENSE`、`NOTICE`、`SHA256SUMS`、`events/`、`runs/`、`worlds/`；`.index.json` 为该版本的索引（不直出，回滚时复制回 `index.json`） |
| `/srv/drone4d/{lab,lab-static}/.staging/` | `push` 写入、`activate` 校验后移入（不直出） |
| `/srv/drone4d/.state/` | `state.json`（当前与上一版本、构建、站点模式）、`history.jsonl`（每次操作的 UTC 时刻、耗时与操作者）、`d2-site.conf`（`enable-site` 时保存的 D2 站点文件） |
| `/srv/drone4d/bin/` | `push` 同步的本目录脚本与 `scan_public.py`（emax 上的 D2 发布不含 D3 工具） |

保留最近 2 个版本目录及其构建（当前与上一版本，即回滚目标）。磁盘账（Q-D3-02）：`/srv/drone4d` 全部内容按 inode 去重计数
（`push` 以 `rsync --link-dest` 链接上一版本，内容未变的制品为硬链接）≤ 3 GB，且无 > 64 MB 的单文件；`activate` 按"切换并清理后"
的预计占用判定，超出即拒绝且不切换。> 64 MB 的制品与样例包经 ModelScope 分发（ADR-201），索引条目以 `external` 给出链接。

## 2 发布

```bash
# 开发机或 dmax：/lab/ 构建与版本目录
make lab-build                                                            # → .cache/deploy/emax/lab-dist（K9 与 lab 档扫描）
V=2026.11.03-1; S=/data/d3/publish/lab-static/$V                          # 版本目录按 17 §18.8.5 布局放好制品
python3 tools/deploy/emax/d3-public/labctl.py index --dir $S --draft draft.json \
  --slices /data/d3/runs/<rid>/derived/slices/slices.json --lab-build "$(jq -r .release .cache/deploy/emax/lab-dist/build-info.json)"
python3 tools/deploy/emax/d3-public/labctl.py check --dir $S --lab-dist .cache/deploy/emax/lab-dist --test-ids configs/bench/test_ids.txt
python3 tools/deploy/emax/d3-public/labctl.py push --static $S --lab-dist .cache/deploy/emax/lab-dist --dest emax:/srv/drone4d
PYTHONPATH=python python3 tools/deploy/emax/d3-public/labctl.py pin --bench-root /data/d3 --index $S/.index.json   # dmax：引用的切片置 pinned

# emax（root）：校验 staging、移入、原子切换、保留 2 个版本
sudo python3 /srv/drone4d/bin/labctl.py activate --version $V --chown root:root
sudo python3 /srv/drone4d/bin/labctl.py enable-site --base /opt/anet-drone4d/current/tools/deploy/emax/anet4d.agentnetwork.org.cn.conf   # 首次上线与域名迁移
sudo python3 /srv/drone4d/bin/verify.py --connect 127.0.0.1 --source-ips 127.0.0.2-127.0.0.12 --expect-nmax 4 --local --out /srv/drone4d/.state/verify-$V.json
```

`draft.json` 是不含 `bytes`、`sha256` 的索引草稿（`license`、`worlds`、`events[].runs[]`，字段见契约）；`index` 计算每条制品的
字节数与摘要、`files_bytes_total`、`features`（`leaderboard`、`submit` 恒为 false），写出 `SHA256SUMS` 与 `<版本目录>/.index.json`；
R04 的 `slices.json` 以 `--slices` 并入同一 `run_id` 的运行条目。运行 id 须为 `run-[a-z2-7]{10}`、事件 id 须为
`[a-z][a-z0-9-]{2,63}`（与前端解析器一致，不符时前端拒绝整份索引）。

`check`（`push` 与 `activate` 前自动执行）：版本目录名、`SHA256SUMS` 完整且逐文件一致、只含普通文件（无符号链接）、路径段可直出、
单文件 ≤ 64 MB、索引引用与字节数一致、ModelScope 外链规则、test 集文件 0、真值文件 0（`scan_public.py static`）、无
`leaderboard/`；`/lab/` 构建按 `scan_public.py build --profile lab` 扫描禁用字符串。

## 3 回滚（目标 ≤ 10 min）

```bash
sudo python3 /srv/drone4d/bin/labctl.py rollback                       # 恢复上一版本的 index.json 与它发布时的 /lab/ 构建（秒级）
sudo python3 /srv/drone4d/bin/labctl.py rollback --to 2026.11.03-1     # 恢复指定的保留版本
sudo python3 /srv/drone4d/bin/labctl.py withdraw                       # 回滚到 D2：恢复 D2 站点文件（撤下 /lab/），nginx -t 后 reload
sudo python3 /srv/drone4d/bin/labctl.py withdraw --d2-release <release> # 同时 install.sh --rollback 到上一个 D2 标签构建
sudo python3 /srv/drone4d/bin/verify.py --connect 127.0.0.1 --expect d2 # 回滚到 D2 后的复核
```

`withdraw` 不删除版本目录与构建，之后 `enable-site` 即可恢复。上线实测任一条不通过时，按 ADR-194 (5) ⑥ 在 10 min 内执行
`withdraw --d2-release <上一 D2 标签构建>`，在 `main` 修复后自发布候选重新执行。

## 4 验收检查（verify.py）

| 检查 | 判据（D3-AC-21 与 AWR-05 §7.9） |
|---|---|
| bench404 | `/api/bench`、`/api/bench/**`（含 `/api/bench/v1/public/**`）的 GET、HEAD、POST 一律 404 |
| lab | `/lab/` 与 History 路由返回同一份 index.html（no-cache）；`/lab` 301；assets immutable、缺失 404；非 GET/HEAD 405；点路径 404 |
| static | `index.json` no-cache、ETag、304；制品 immutable、长度与索引一致；`SHA256SUMS` 与抽样文件摘要一致；目录、`.staging`、`.index.json`、非法版本名 404 |
| rate | 单连接整段下载前 8 MiB 不限，之后稳态 ≤ 2 MiB/s（容差 5 %）；分段请求从首字节起限速 |
| conn_ip、conn_all | 同一地址前缀第 5 条并发下载 429；全站第 33 条 429（回环多源地址，只在 emax 本机可测） |
| nmax | 容量接口 `sandbox.max` 等于 `--expect-nmax`（D2-AC-04 的 N_max 不变） |
| root（`--local`） | `/etc/anet-drone4d/env` 中 `AWR_D3_ENABLED` 未设或为 0，无进程环境为 1；进程表中无 D3 进程，无 `d3-*` 容器 |
| disk、content（`--local`） | 磁盘账 ≤ 3 GB、无 > 64 MB 文件；当前版本目录与构建 `check` 通过 |

退出码 0 通过、1 不通过、2 参数错误或站点不可达；`--out` 写出 `awr.deploy.d3_public.verify.v1` 报告。

## 5 上线前演练（开发机）

```bash
tools/deploy/emax/d3-public/nginx-check.sh                        # 容器 nginx 1.24：nginx -t、端到端用例、verify.py、回滚与撤下
.venv/bin/python tools/deploy/emax/d3-public/drill.py             # cgroup 模拟演练（约 10 min，需要 docker 与 ≥ 8 核）
```

`drill.py` 以 `tools/bench/sandbox/_sbbench.Stack`（CPUQuota 250%、MemoryHigh 1.8G、MemoryMax 2G、cpuset 0-3）运行 D2 栈与
D2-AC-04 变体 A 负载，nginx 容器与 D2 栈共用 cpuset 0-3；客户端不占 0-3 号核：WS 探针独占 4 号核（收到帧的时刻即测量值，不与下载
进程争核），演练进程（operator、直连 viewer、采样）在 5-7 号核，16 路下载在 6-7 号核；8 个 viewer 经 nginx（wss）订阅共享展示，测 TIME 帧推送时延（收到时刻
减去 sim-core 心跳时刻，时钟偏移由最小往返的 pong 估计）；无下载与 16 路并发下载交替 3 轮，判据为下载阶段 p95 相对基线的增幅
≤ 10 %（报告另给每轮增幅 `round_increase` 与负载前置条件 `load_valid`：采样期间 1 min 平均负载均值 < 6，与 capacity.py
同一口径；不满足时数字只作参考）；另核对负载期间 N_max 不变与变体 A 判据（整段时长），并按阶段对比沙盒 RTF 与单步 p99
（`d2_ac04.by_phase`：下载阶段单步 p99 中位数 ≤ 基线 × 1.1、RTF 均值不低于基线 0.01 以上，参考项，用于区分下载造成的回退与主机
本身不满足变体 A），并计时 `rollback`、`withdraw`、再上线三步（各 ≤ 10 min）。报告
`.cache/deploy/emax/d3-public/drill/<UTC 时刻>/drill.json`（`awr.deploy.d3_public.drill.v1`）。演练只作上线前检查，封板以上线后
emax 实测判定（ADR-194 (4)）。

## 6 上线批次（H05）执行清单

1. `make lab-build`；按 F07 的公开样例目录准备版本目录与草稿；`labctl index`、`check`、`push`、`pin`。
2. emax：`activate`、`enable-site`（`--base` 为 D2 标签构建的站点文件），`verify.py --local` 全部通过（`--expect-nmax` 取上线前
   容量接口的值）。
3. 在 emax 本机复测 D2-AC-04（`tools/bench/sandbox/capacity.py` 不在 D2 发布中，按 D2 部署手册 5.7 的观测与容量接口核对 N_max）
   与 16 路并发下载期间的 WS 推送时延（`drill.py` 的 `_wsprobe`、`_download` 子命令可单独在 emax 上以 D2 发布的 `.venv` 运行）。
4. 回滚演练：`withdraw --d2-release <上一 D2 标签构建>` 并以 `verify.py --expect d2` 复核，记录从开始到复核通过的耗时（≤ 10 min），
   随后 `enable-site` 恢复；全部记录写入阶段报告。
