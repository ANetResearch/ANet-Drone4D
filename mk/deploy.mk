# mk/deploy.mk（DEMO-PUBLIC；D2 WP-16）：ANet4D 公开站点（https://anet4d.agentnetwork.org.cn；拓扑沿用 V0.2-demo：T4，AWR-19 §3.7；
# ADR-083、ADR-085、ADR-109、ADR-118）的本机构建、
# 打包与本机模拟安装。只在本机执行，不改动 apps/web/dist（make run 使用的默认构建），不连接服务器；上传与安装见
# tools/deploy/emax/README.md。
#   make demo-build              VITE_AWR_DEMO=public 前端构建到 .cache/deploy/emax/web-dist（构建信息 channel=demo），并做生产包扫描
#   make deploy-pack             tools/deploy/emax/build-local.sh：演示构建 + 部署包 + 世界包（WORLDS=all 缺省：public profile 的
#                                七个世界与派生缓存；WORLDS=none 不打世界包；WORLDS=synthcity,shenzhen 只打这几个；WORLD=1 同 D1，
#                                只打 synthcity）
#   make deploy-dry-run          最近一次 deploy-pack 的部署包与世界包按 install.sh --dry-run 本机模拟（校验 MANIFEST、构建信息、
#                                世界包 sha256 与成员、派生缓存 deep 校验与世界状态；不需要 root，不改系统）
#   make deploy-nginx-check      在一次性 Ubuntu 24.04 容器中 nginx -t 站点文件（新域名站点、引导站点与旧域名 301 跳转站点）并跑
#                                nginx 对拍与端到端用例（需要 docker；本机不装 nginx；
#                                含 D2-AC-42 的容量微缓存：100 个并发客户端与五种上游故障）；证据写到 NGINX_CHECK_OUT（缺省
#                                .cache/deploy/emax/nginx-check/<UTC 时刻>/，汇总 nginx-check.json）
#   make lab-build               D3 /lab/ 公开只读静态构建（扩展点 15，ADR-129：VITE_AWR_LAB_ONLY=1、base /lab/）到
#                                .cache/deploy/emax/lab-dist，做 K9 生产包扫描与公开产物扫描（tools/bench/d3/scan_public.py，D3-AC-21）
#   make lab-scan                公开产物扫描：lab-dist（lab 档）与 web-dist（demo 档）；LAB_STATIC=<目录> 另扫公开静态目录（test 集与
#                                真值文件 0、单文件 ≤ 64 MB、总量 ≤ 3 GB；TEST_IDS=<文件> 列出 test 划分的事件与运行 id）
#   make demo-parity             扩展点 15 构建对拍（D3-AC-01，UX-AC-082）：D2 标签（D2_REF，缺省 v0.3.1：V0.3.1 起公开站主构建为该标签的演示构建）与当前工作树的演示构建
#                                （开关缺省关闭）逐字节一致；git 目录在工作树外时先导出 GIT_DIR
# 报告写到 .cache/deploy/emax/scan/*.json（awr.d3.scan_public.report.v1）。
# 构建信息（ADR-118）：AWR_BUILD_CHANNEL、AWR_BUILD_SHA、AWR_BUILD_DIRTY、AWR_BUILD_TIME、AWR_RELEASE 由 build-local.sh 计算并导出给
# vite 的 buildInfoPlugin()；demo-build 只导出 AWR_BUILD_CHANNEL=demo，其余由插件只读调用 git 推出（发布 id 为 local-<commit>）。

.PHONY: demo-build deploy-pack deploy-dry-run deploy-nginx-check lab-build lab-scan demo-parity

DEMO_DIST := $(ROOT)/.cache/deploy/emax/web-dist
LAB_DIST := $(ROOT)/.cache/deploy/emax/lab-dist
DEPLOY_OUT := $(ROOT)/.cache/deploy/emax
SCAN_OUT := $(DEPLOY_OUT)/scan
D2_REF ?= v0.3.1
SCAN_PUBLIC := $(PY) tools/bench/d3/scan_public.py
WORLDS ?= $(if $(filter 1,$(WORLD)),synthcity,all)

demo-build: _need-installed ## ANet4D 公开站点前端构建（VITE_AWR_DEMO=public → .cache/deploy/emax/web-dist，含生产包扫描）
	@[ -z "$(VITE_AWR_TEST_SWITCHES)" ] || $(call fail,$(EXIT_GENERIC),VITE_AWR_TEST_SWITCHES 与演示构建互斥（ADR-083）,unset VITE_AWR_TEST_SWITCHES)
	@$(call with_lock_sh,cd $(WEB) && AWR_BUILD_CHANNEL=demo VITE_AWR_DEMO=public $(NPX) vite build --outDir $(DEMO_DIST) --emptyOutDir)
	@cd $(ROOT) && node apps/web/tests/m06/lint/prod-bundle-scan.mjs $(DEMO_DIST)
	@cd $(ROOT) && $(SCAN_PUBLIC) build $(DEMO_DIST) --profile demo --json $(SCAN_OUT)/web-dist.json

lab-build: _need-installed ## D3 /lab/ 公开只读静态构建（VITE_AWR_LAB_ONLY=1、base /lab/ → .cache/deploy/emax/lab-dist，含 K9 与公开产物扫描）
	@[ -z "$(VITE_AWR_TEST_SWITCHES)$(VITE_AWR_DEMO)$(VITE_AWR_LAB_PUBLIC)" ] || $(call fail,$(EXIT_GENERIC),/lab/ 静态构建与 VITE_AWR_DEMO、VITE_AWR_LAB_PUBLIC、测试开关互斥（ADR-129）,unset VITE_AWR_DEMO VITE_AWR_LAB_PUBLIC VITE_AWR_TEST_SWITCHES)
	@$(call with_lock_sh,cd $(WEB) && AWR_BUILD_CHANNEL=demo VITE_AWR_LAB_ONLY=1 $(NPX) vite build --outDir $(LAB_DIST) --emptyOutDir)
	@cd $(ROOT) && node apps/web/tests/m06/lint/prod-bundle-scan.mjs $(LAB_DIST)
	@cd $(ROOT) && $(SCAN_PUBLIC) build $(LAB_DIST) --profile lab --json $(SCAN_OUT)/lab-dist.json

lab-scan: ## 公开产物扫描（D3-AC-21）：lab-dist（lab 档）与 web-dist（demo 档）；LAB_STATIC=<目录> 另扫公开静态目录，TEST_IDS=<文件>
	@cd $(ROOT) && $(SCAN_PUBLIC) build $(LAB_DIST) --profile lab --json $(SCAN_OUT)/lab-dist.json
	@cd $(ROOT) && $(SCAN_PUBLIC) build $(DEMO_DIST) --profile demo --json $(SCAN_OUT)/web-dist.json
	@if [ -n "$(LAB_STATIC)" ]; then cd $(ROOT) && $(SCAN_PUBLIC) static $(LAB_STATIC) $(if $(TEST_IDS),--test-ids $(TEST_IDS)) --json $(SCAN_OUT)/lab-static.json; fi

demo-parity: _need-installed ## 扩展点 15 构建对拍：D2 标签（D2_REF，缺省 v0.3.1：V0.3.1 起公开站主构建为该标签的演示构建）与当前工作树的演示构建逐字节一致（D3-AC-01）
	@cd $(ROOT) && $(SCAN_PUBLIC) d2-parity --ref $(D2_REF) --json $(SCAN_OUT)/d2-parity.json

deploy-pack: _need-installed ## ANet4D 公开站点部署包与世界包（tools/deploy/emax/build-local.sh；WORLDS=all|none|<id,…>，WORLD=1 只打 synthcity）
	@cd $(ROOT) && bash tools/deploy/emax/build-local.sh --worlds $(WORLDS)

deploy-dry-run: _need-installed ## 最近一次 deploy-pack 的产物按 install.sh --dry-run 本机模拟（不需要 root，不改系统）
	@cd $(ROOT) && B=$$(ls -1d $(DEPLOY_OUT)/stage/anet-drone4d-* 2>/dev/null | tail -1); \
	  [ -n "$$B" ] || { echo "deploy-dry-run: 先 make deploy-pack" >&2; exit 1; }; \
	  W=""; [ -f $(DEPLOY_OUT)/worlds/WORLDS.json ] && W="--worlds $(DEPLOY_OUT)/worlds"; \
	  TMPDIR=$(DEPLOY_OUT) AWR_DRY_PYTHON=$(ROOT)/.venv/bin/python bash tools/deploy/emax/install.sh --dry-run --bundle "$$B" $$W

deploy-nginx-check: ## 站点文件（含旧域名 301 跳转站点）nginx -t 与 nginx 对拍、端到端用例（一次性 Ubuntu 24.04 容器，需要 docker；NGINX_CHECK_OUT=证据目录）
	@cd $(ROOT) && bash tools/deploy/emax/nginx-check.sh $(if $(NGINX_CHECK_OUT),--out $(NGINX_CHECK_OUT))
