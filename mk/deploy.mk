# mk/deploy.mk（DEMO-PUBLIC）：公开演示站（T4，AWR-19 §3.7；ADR-083、ADR-085）的本机构建与打包。只在本机执行，不改动
# apps/web/dist（make run 使用的默认构建），不连接服务器；上传与安装见 tools/deploy/emax/README.md。
#   make demo-build    VITE_AWR_DEMO=public 前端构建到 .cache/deploy/emax/web-dist，并做生产包扫描（M06-AC-010）
#   make deploy-pack   tools/deploy/emax/build-local.sh：演示构建 + 部署包（WORLD=1 另附 synthcity 世界包）

.PHONY: demo-build deploy-pack

DEMO_DIST := $(ROOT)/.cache/deploy/emax/web-dist

demo-build: _need-installed ## 公开演示站前端构建（VITE_AWR_DEMO=public → .cache/deploy/emax/web-dist，含生产包扫描）
	@[ -z "$(VITE_AWR_TEST_SWITCHES)" ] || $(call fail,$(EXIT_GENERIC),VITE_AWR_TEST_SWITCHES 与演示构建互斥（ADR-083）,unset VITE_AWR_TEST_SWITCHES)
	@$(call with_lock_sh,cd $(WEB) && VITE_AWR_DEMO=public $(NPX) vite build --outDir $(DEMO_DIST) --emptyOutDir)
	@cd $(ROOT) && node apps/web/tests/m06/lint/prod-bundle-scan.mjs $(DEMO_DIST)

deploy-pack: _need-installed ## 公开演示站部署包（tools/deploy/emax/build-local.sh；WORLD=1 附带 synthcity 世界包）
	@cd $(ROOT) && bash tools/deploy/emax/build-local.sh $(if $(filter 1,$(WORLD)),--with-world,)
