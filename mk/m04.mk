# mk/m04.mk（M04）：几何世界查询服务的派生缓存预热、测试与基准（AWR-03 §4.3；M04-FR-005、§9.1；D2 WP-14）。
#   make geo-warm          为全部世界派生 worlds/.geo-cache（含 Grid25；make worlds 之后执行；失败只告警）
#   make geo-check         派生缓存可用性（allow_derive = false 的部署判定，有世界不可用时退出码 4；GEO_CHECK_ARGS=--deep）
#   make test-world-query  只运行 tests/world_query（make test 已包含，便捷目标）
#   make bench-geo         七个世界的几何基准（tools/bench/geo：缓存与 Grid25 字节数、热打开、los_batch numba 与 numpy；
#                          排他性能锁）；make bench-geo-micro 为 D1 的点查询微基准

GEO_CLI := $(PY) -m awr.world.geometry.cli

# make worlds 在 worldpkg build --missing 之后预热派生缓存（mk/m03.mk 先被包含，因而排在 worlds-build 之后）
WORLDS_TARGETS += geo-warm

.PHONY: geo-warm geo-check test-world-query bench-geo bench-geo-micro

geo-warm: ## 预热几何派生缓存（worlds/.geo-cache；失败只告警，sim-core 装载时会自行派生）
	@cd $(ROOT) && $(GEO_CLI) warm --all --worlds $(AWR_WORLDS_DIR) || echo "警告：geo warm 未完成（sim-core 装载时自行派生）" >&2

test-world-query: ## 只运行 M04 的 pytest（tests/world_query）
	@$(call with_lock_sh,cd $(ROOT) && $(PY) -m pytest -q -m "not perf" tests/world_query $(PYTEST_ARGS))

geo-check: ## 派生缓存可用性探测（不派生；有世界不可用时退出码 4）
	@cd $(ROOT) && $(GEO_CLI) check --all --worlds $(AWR_WORLDS_DIR) $(GEO_CHECK_ARGS)

bench-geo: ## 七个世界的几何基准（派生缓存与 Grid25、热打开、los_batch numba 与 numpy；输出 runs/perf/geo-*）
	@$(call with_lock_ex,cd $(ROOT) && $(PY) tools/bench/geo/run.py --all --worlds $(AWR_WORLDS_DIR))

bench-geo-micro: ## 六城点查询微基准（D1，p50 / p99）
	@$(call with_lock_ex,cd $(ROOT) && $(GEO_CLI) bench --all --worlds $(AWR_WORLDS_DIR))
