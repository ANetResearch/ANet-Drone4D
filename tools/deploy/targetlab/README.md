# 场景目标引擎私有验收部署指南（实验）

本目录提供场景目标引擎（场景活动审计、精细外观、确定性生活群体与受约束的定制编排）的**私有验收部署**脚本。
功能边界、启用条件与限制见 [升级说明](../../../docs/impl/scene-target-system.zh-CN.md)，验收记录见
[验收报告](../../../docs/audit/target-life/REVIEW.zh-CN.md)。

本部署仅用于内部验收，不属于公开演示站点：公开构建（`VITE_AWR_DEMO=public` 且未设置 `VITE_AWR_TARGET_LIFE=1`）
在构建期剔除生活群体编排界面与定制编排入口，公开模板目录不列出依赖未入库配方的实验模板（T7–T13）。

## 1. 部署约束

- 使用独立检出目录与独立运行时 profile（`targetlab`），不与公开演示或其他服务共用端口、共享内存目录或数据目录。
- 服务只监听回环地址（`net.access_mode=loopback`），经运维方批准的 SSH 端口转发或内网反向代理访问；不得暴露到公网。
- 世界包、资产导出产物、配方与计划（`worlds/`、`var/`、`.cache/`）均不入库。跨主机复制模板时须同时复制其引用的配方、计划与资产包；
  哈希、世界版本或尺寸不匹配时，服务端拒绝准入。
- 定制编排的模型服务凭据只放在仓库外或部署私有目录（缺省 `var/private/gravitex.json`，权限 0600），由
  `AWR_LIFE_PROVIDER_CONFIG` 指向；仓库、日志与审计证据中不得出现凭据。

端口由 `AWR_PORT_OFFSET` 统一偏移（`run.sh` 缺省为 27，即主 API 8270、沙盒 API 8272、总线 7717）；本地代理
`proxy.mjs` 监听回环地址 5443。以下示例以 `<LAB_URL>` 表示访问入口（例如经端口转发后的 `http://127.0.0.1:<本地端口>`）。

## 2. 启动

依赖按仓库锁文件安装。启动后端：

```sh
bash tools/deploy/targetlab/run.sh
```

构建验收用前端（实验开关仅用于本部署）：

```sh
VITE_AWR_DEMO=public VITE_AWR_TARGET_LIFE=1 VITE_AWR_TARGET_ASSETS=1 VITE_AWR_LIFE_CONTINUOUS=1 npm run build -w apps/web
```

| 开关 | 作用 |
|---|---|
| `VITE_AWR_TARGET_LIFE=1` | 保留生活群体生成器、行为预演与定制编排界面（缺省在公开构建中剔除） |
| `VITE_AWR_TARGET_ASSETS=1` | 启用精细外观资产加载；未设置时目标以线框显示 |
| `VITE_AWR_LIFE_CONTINUOUS=1` | 验收期间禁止因页面隐藏或空闲自动驻留沙盒；显式暂停与会话寿命限制仍有效 |
| `AWR_TARGET_LIFE_ENABLE=1`（服务端） | 启用 `/target-life/preview`、`/{sha256}/scene`、`/{sha256}/rehearsal`；未设置时返回 404 |

构建后启动只依赖 Node 标准库的 REST/WS 分流代理：

```sh
node tools/deploy/targetlab/proxy.mjs
```

运行中的 sim-core 不热加载 Python 代码；部署更新后使用新会话，或经正常驻留与恢复启动新进程。

## 3. 资产包

`packages/contracts/target/assets.catalog.json` 只记录资产元数据、来源、许可、尺寸、布局与内容哈希；资产二进制不入库。
导出工具从已锁定来源的资产目录生成内容寻址的浏览器资产包：

```sh
.venv/bin/python tools/deploy/targetlab/export_assets.py <ASSET_SOURCE_DIR> apps/web/dist/assets/target-assets
.venv/bin/python tools/deploy/targetlab/export_extra_assets.py <EXTRA_SOURCE_DIR> apps/web/dist/assets/target-assets
```

`<ASSET_SOURCE_DIR>` 须包含原始来源锁文件；`export_extra_assets.py` 按 `extra-assets.lock.json` 校验固定源文件。
浏览器逐项核验网格与纹理的长度和 SHA-256，缺失或不匹配时显示失败并保留线框。资产许可须按
`THIRD_PARTY_NOTICES.md` 逐项复核后方可再分发。

## 4. 活动计划与生活配方

```sh
PYTHONPATH=python .venv/bin/python tools/deploy/targetlab/build_activities.py worlds/synthcity --seed 20261022
PYTHONPATH=python .venv/bin/python tools/deploy/targetlab/build_life.py worlds/synthcity --people 24 --vehicles 6
PYTHONPATH=python .venv/bin/python tools/deploy/targetlab/release_life.py var/target-life/network.json scenarios/target-life/custom-intents
```

- `build_activities.py` 输出 `var/target-activities/` 下的网络、内容寻址计划与 `audit.json`，并更新 T8 模板；
  非仓库根目录启动时设置 `AWR_TARGET_ACTIVITY_DIR`（绝对路径）。
- `build_life.py` 输出 `var/target-life/network.json`（场景能力输入）与 `life-<sha256>.json`（内容寻址配方）。
  配方绑定世界 ID、内容版本、坐标摘要与运行时代码摘要，不匹配时拒绝载入；完整群体原子导入，不允许部分加载。
- `release_life.py` 由已保存的定制意图重新编译配方，不调用模型服务。
- 多场景部署在 `AWR_TARGET_LIFE_DIR/<world_id>.network.json` 提供各自经验证的场景图；缺少场景数据时拒绝生成，
  不得套用其他场景的坐标。当前只提供 synthcity 适配器。

## 5. 实时长测

`check_live_life.py` 创建独立 SDK 会话，按墙钟时间等待并采样全体目标，与本地同版本配方的重放逐项对照；
不发送保活、暂停或模型请求，结束后关闭会话。输出路径必须不存在，以免覆盖既有证据。

```sh
.venv/bin/python tools/deploy/targetlab/check_live_life.py var/target-life/life-<sha256>.json \
  --url <LAB_URL> --template t13_all_vehicle_types --output .cache/targetlab/live-soak.json
```

缺省时长 1500 s、每 60 s 采样一次；短时冒烟不能替代完整长测。执行端须与服务端使用一致的运行时代码与配方。

## 6. 已知限制

- 车辆为带速度与朝向约束的运动学模型，采用覆盖完整车身的多圆包络与扫掠间距检查；不包含轮胎、悬挂与传动动力学。
- 设施内部以门户占用表示，不仿真室内空间与作业动作；公交停靠、货物交付以目的地与服务时长表示。
- 犬步态为程序变形；尚未实现足部 IK、叉车搬运载荷、车轮转向与旋翼动力学。
- 生活群体在 `TargetRuntime` 内按 0.1 s 固定步长推进，尚未编译为 Canonical Event Tape；随机数尚未登记到仓库的随机流机制。
- 精细外观仅接入 Web 世界视口与共享 targets 图层的 Web 传感器视图；服务端离线渲染路径未接入。
- 验收结论仅适用于所记录的世界、配方、seed 与软硬件条件，不构成任意场景的视觉或性能认证。
