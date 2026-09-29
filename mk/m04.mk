# mk/m04.mk（M04）：几何世界查询服务的派生缓存预热、测试与基准（AWR-03 §4.3；M04-FR-005、§9.1）。
#   make geo-warm          为全部世界派生 worlds/.geo-cache（make worlds 之后执行；失败只告警）
#   make test-world-query  只运行 tests/world_query（make test 已包含，便捷目标）
#   make bench-geo         六城几何查询微基准（排他性能锁）

GEO_CLI := $(PY) -m awr.world.geometry.cli

# make worlds 在 worldpkg build --missing 之后预热派生缓存（mk/m03.mk 先被包含，因而排在 worlds-build 之后）
WORLDS_TARGETS += geo-warm

.PHONY: geo-warm test-world-query bench-geo

geo-warm: ## 预热几何派生缓存（worlds/.geo-cache；失败只告警，sim-core 装载时会自行派生）
	@cd $(ROOT) && $(GEO_CLI) warm --all --worlds $(AWR_WORLDS_DIR) || echo "警告：geo warm 未完成（sim-core 装载时自行派生）" >&2

test-world-query: ## 只运行 M04 的 pytest（tests/world_query）
	@$(call with_lock_sh,cd $(ROOT) && $(PY) -m pytest -q -m "not perf" tests/world_query $(PYTEST_ARGS))

bench-geo: ## 六城几何查询微基准（p50 / p99）
	@$(call with_lock_ex,cd $(ROOT) && $(GEO_CLI) bench --all --worlds $(AWR_WORLDS_DIR))
