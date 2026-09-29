# mk/m15.mk（M15）：前端 UI 壳与设计体系的生成物校验、shadcn 安装与冒烟（M15 §9.5、§9.6；AWR-03 §4.3）。
# make lint 追加 lint-m15：token 与图标注册表等生成物 --check、codemod 幂等（postadd --check）、cn 键登记（CN-01）、
# 图标清单校验。Vitest（tests/m15/**）已由 make test 全量运行，这里只提供便捷目标，不追加到 TEST_TARGETS。
#   make lint-m15          只运行 M15 的生成物与 codemod 检查
#   make shadcn-install    离线（registry-mirror）重装 44 个 D1 组件并执行 codemod（M15-FR-081..085）
#   make shadcn-mirror     联网刷新 tools/shadcn/registry-mirror（L2 镜像，升级时使用）
#   make m15-gen           重新生成 token、图标注册表、原因码文案与内联的 shadcn/tailwind.css
#   make test-m15          只运行 tests/m15（unit + browser）
#   make smoke-m15         测试构建 + Playwright 冒烟（perf/m15/smoke.spec.ts；截图写 .cache/impl/shots/）
#   make ui-m15            UI 功能用例（FakeSource，?viewport=off 的测试构建写到 M15_DIST；interaction、layout-probe、
#                          report、responsive；并行开发期视口不稳定时加 M15_STUB_VIEWPORT=1）

SHADCN := $(ROOT)/tools/shadcn

LINT_TARGETS += lint-m15

.PHONY: lint-m15 shadcn-install shadcn-mirror m15-gen test-m15 smoke-m15 ui-m15

lint-m15: ## M15 生成物 --check、codemod 幂等、CN-01、图标清单
	@cd $(ROOT) && node $(SHADCN)/gen-theme-tokens.mjs --check
	@cd $(ROOT) && $(PY) $(SHADCN)/gen-motion-tokens.py --check
	@cd $(ROOT) && node $(SHADCN)/inline-shadcn-tailwind.mjs --check
	@cd $(ROOT) && node $(SHADCN)/icons/gen-registry.mjs --check
	@cd $(ROOT) && node $(SHADCN)/gen-reasons-i18n.mjs --check
	@cd $(ROOT) && node $(SHADCN)/check-cn-keys.mjs
	@cd $(ROOT) && node $(SHADCN)/postadd.mjs --check --no-tsc
	@cd $(ROOT) && node $(SHADCN)/icons/verify.mjs > /dev/null

shadcn-install: ## 离线重装 D1 组件（不跑 npm install，package.json 与锁文件逐字节恢复）
	@cd $(ROOT) && node $(SHADCN)/install.mjs

shadcn-mirror: ## 联网刷新 L2 registry 镜像（禁用组件与演示 block 不入镜像）
	@cd $(SHADCN) && OUT=registry-mirror bash mirror.sh

m15-gen: ## 重新生成 M15 的 token、图标、原因码与 shadcn/tailwind 内联样式
	@cd $(ROOT) && node $(SHADCN)/gen-theme-tokens.mjs
	@cd $(ROOT) && $(PY) $(SHADCN)/gen-motion-tokens.py
	@cd $(ROOT) && node $(SHADCN)/inline-shadcn-tailwind.mjs
	@cd $(ROOT) && node $(SHADCN)/icons/gen-registry.mjs
	@cd $(ROOT) && node $(SHADCN)/gen-reasons-i18n.mjs

test-m15: ## 只运行 tests/m15（make test 已包含，便捷目标）
	@cd $(WEB) && $(NPX) vitest run --project unit --project browser tests/m15 $(VITEST_ARGS)

smoke-m15: ## 测试构建后运行 M15 冒烟（无需后端）
	@cd $(WEB) && VITE_AWR_TEST_SWITCHES=1 $(NPX) vite build
	@cd $(WEB) && $(NPX) playwright test perf/m15/smoke.spec.ts --project perf

M15_DIST ?= $(ROOT)/runs/m15-dist
ui-m15: ## M15 UI 功能用例（FakeSource；不含性能用例）
	@cd $(WEB) && M15_DIST=$(M15_DIST) node perf/m15/helpers/build-ui.mjs $(if $(M15_STUB_VIEWPORT),--stub-viewport,)
	@cd $(WEB) && M15_DIST=$(M15_DIST) M15_UI_ONLY=1 $(NPX) playwright test perf/m15/interaction.spec.ts perf/m15/layout-probe.spec.ts perf/m15/report.spec.ts perf/m15/responsive.spec.ts --project perf
