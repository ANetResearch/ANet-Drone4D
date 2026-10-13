# docs-site：ANet Drone4D 文档站源文件

本目录是 [docs.agentnetwork.org.cn/drone4d](https://docs.agentnetwork.org.cn/drone4d/) 的源文件，采用 Sphinx、sphinx-rtd-theme 与 MyST Markdown，
目录布局与 Read the Docs 一致（`/drone4d/<lang>/latest/`）。设计文档仍以仓库的 `docs/` 为准；本站面向使用者。

| 路径 | 内容 |
|---|---|
| `zh_CN/`、`en/` | 两种语言的源文件，文档名一一对应（中文为准，英文准确对应）；各自的 `conf.py` 只调用公共配置 |
| `_shared/conf_common.py` | 公共 Sphinx 配置：主题、标志、语言与版本、链接检查 |
| `_shared/_ext/drone4d_api.py` | `drone4d-endpoints` 指令：由 `packages/contracts/rest/*.snapshot.json` 生成端点表；说明译文在 `api_summaries.json` |
| `_shared/_ext/drone4d_site.py` | 两种语言的文档名一致性检查、语言与版本切换链接、中文软换行处理 |
| `_shared/_templates/` | 主题模板覆盖：语言与版本浮层、hreflang、窄屏顶栏标志 |
| `_shared/_static/` | 样式（ANet Graphite 色卡）、图标（由 `_shared/tools/gen-icons.mjs` 从 lucide 生成）、Agent Network 标志 |
| `_shared/root/index.html` | 站点根页：按浏览器语言跳转到 `zh-cn` 或 `en` |
| `requirements.txt` | 固定版本的构建依赖，安装在 `.venv-docs` |

常用命令（仓库根目录）：

```sh
make docs-site             # 构建两种语言（告警即失败）与根跳转页，输出 docs-site/_build/html/
make docs-site-linkcheck   # 链接检查
make docs-site-serve       # 本机预览，缺省 http://127.0.0.1:8080/
tools/deploy/docs/publish.sh   # 准备待发布目录并打印上传命令（不上传）
```

编写约定：用户可见文字采用企业级的克制表述，不使用感叹号与 emoji；时间写 UTC；快速开始中的每段输出都来自真实运行，
本机路径统一写成 `~/ANet-Drone4D`，端口写默认值。新增页面时两种语言同时增加，否则构建失败。
