# mk/deploy.mk（DEMO-PUBLIC；D2 WP-16）：公开演示站 V0.2-demo（T4，AWR-19 §3.7；ADR-083、ADR-085、ADR-109、ADR-118）的本机构建、
# 打包与本机模拟安装。只在本机执行，不改动 apps/web/dist（make run 使用的默认构建），不连接服务器；上传与安装见
# tools/deploy/emax/README.md。
#   make demo-build              VITE_AWR_DEMO=public 前端构建到 .cache/deploy/emax/web-dist（构建信息 channel=demo），并做生产包扫描
#   make deploy-pack             tools/deploy/emax/build-local.sh：演示构建 + 部署包 + 世界包（WORLDS=all 缺省：public profile 的
#                                七个世界与派生缓存；WORLDS=none 不打世界包；WORLDS=synthcity,shenzhen 只打这几个；WORLD=1 同 D1，
#                                只打 synthcity）
#   make deploy-dry-run          最近一次 deploy-pack 的部署包与世界包按 install.sh --dry-run 本机模拟（校验 MANIFEST、构建信息、
#                                世界包 sha256 与成员、派生缓存 deep 校验与世界状态；不需要 root，不改系统）
#   make deploy-nginx-check      在一次性 Ubuntu 24.04 容器中 nginx -t 站点文件并跑 nginx 对拍与端到端用例（需要 docker；本机不装 nginx；
#                                含 D2-AC-42 的容量微缓存：100 个并发客户端与五种上游故障）；证据写到 NGINX_CHECK_OUT（缺省
#                                .cache/deploy/emax/nginx-check/<UTC 时刻>/，汇总 nginx-check.json）
# 构建信息（ADR-118）：AWR_BUILD_CHANNEL、AWR_BUILD_SHA、AWR_BUILD_DIRTY、AWR_BUILD_TIME、AWR_RELEASE 由 build-local.sh 计算并导出给
# vite 的 buildInfoPlugin()；demo-build 只导出 AWR_BUILD_CHANNEL=demo，其余由插件只读调用 git 推出（发布 id 为 local-<commit>）。

.PHONY: demo-build deploy-pack deploy-dry-run deploy-nginx-check

DEMO_DIST := $(ROOT)/.cache/deploy/emax/web-dist
DEPLOY_OUT := $(ROOT)/.cache/deploy/emax
WORLDS ?= $(if $(filter 1,$(WORLD)),synthcity,all)

demo-build: _need-installed ## 公开演示站前端构建（VITE_AWR_DEMO=public → .cache/deploy/emax/web-dist，含生产包扫描）
	@[ -z "$(VITE_AWR_TEST_SWITCHES)" ] || $(call fail,$(EXIT_GENERIC),VITE_AWR_TEST_SWITCHES 与演示构建互斥（ADR-083）,unset VITE_AWR_TEST_SWITCHES)
	@$(call with_lock_sh,cd $(WEB) && AWR_BUILD_CHANNEL=demo VITE_AWR_DEMO=public $(NPX) vite build --outDir $(DEMO_DIST) --emptyOutDir)
	@cd $(ROOT) && node apps/web/tests/m06/lint/prod-bundle-scan.mjs $(DEMO_DIST)

deploy-pack: _need-installed ## 公开演示站部署包与世界包（tools/deploy/emax/build-local.sh；WORLDS=all|none|<id,…>，WORLD=1 只打 synthcity）
	@cd $(ROOT) && bash tools/deploy/emax/build-local.sh --worlds $(WORLDS)

deploy-dry-run: _need-installed ## 最近一次 deploy-pack 的产物按 install.sh --dry-run 本机模拟（不需要 root，不改系统）
	@cd $(ROOT) && B=$$(ls -1d $(DEPLOY_OUT)/stage/anet-drone4d-* 2>/dev/null | tail -1); \
	  [ -n "$$B" ] || { echo "deploy-dry-run: 先 make deploy-pack" >&2; exit 1; }; \
	  W=""; [ -f $(DEPLOY_OUT)/worlds/WORLDS.json ] && W="--worlds $(DEPLOY_OUT)/worlds"; \
	  TMPDIR=$(DEPLOY_OUT) AWR_DRY_PYTHON=$(ROOT)/.venv/bin/python bash tools/deploy/emax/install.sh --dry-run --bundle "$$B" $$W

deploy-nginx-check: ## 站点文件 nginx -t 与 nginx 对拍、端到端用例（一次性 Ubuntu 24.04 容器，需要 docker；NGINX_CHECK_OUT=证据目录）
	@cd $(ROOT) && bash tools/deploy/emax/nginx-check.sh $(if $(NGINX_CHECK_OUT),--out $(NGINX_CHECK_OUT))
