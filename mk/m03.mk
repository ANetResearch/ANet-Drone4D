# mk/m03.mk（M03）：World 构建、校验与原始数据获取（AWR-03 §4.3、ADR-034、ADR-050；M03-FR-043 至 FR-045、FR-053）。
# make worlds 的配方在根 Makefile（持共享性能锁）；本文件只登记 WORLDS_TARGETS += worlds-build。
#   make worlds                 构建缺失或失效的世界（worldpkg build --missing --jobs $(J)）
#   make worlds-force           无条件重建六城
#   make validate               worldpkg validate worlds/* --deep（D1-AC-01）
#   make fetch-data [VERIFY=1]  下载并校验 UrbanScene3D 原始数据（awr data fetch urbanscene3d [--verify]）
#   make test-world             只运行 tests/world 与 tests/jobs（make test 已包含，便捷目标）
#   make perf-world             构建性能用例（pytest -m perf，排他性能锁）

WORLDPKG  := $(PY) -m awr.world.package.cli
J         ?= 3
AWR_WORLD ?= shenzhen

WORLDS_TARGETS += worlds-build

.PHONY: worlds-build worlds-force validate fetch-data test-world perf-world worlds-status

# 构建失败的世界只标 INVALID/failed，不阻塞 make run；只有默认世界 AWR_WORLD 不可用时按 19 §16.2 以 4（缺数据）或 6（世界无效）中止
worlds-build: ## 构建缺失或失效的世界（--missing，最多 $(J) 城并行）
	@cd $(ROOT) && $(WORLDPKG) build --missing --jobs $(J); rc=$$?; \
	if [ $$rc -eq 4 ]; then echo "提示：有城市缺少原始数据，运行 make fetch-data" >&2; fi; \
	if [ ! -f "$(AWR_WORLDS_DIR)/$(AWR_WORLD)/world.json" ]; then \
	  if [ $$rc -eq 4 ]; then $(call fail,4,默认世界 $(AWR_WORLD) 缺少原始数据,make fetch-data); \
	  else $(call fail,6,默认世界 $(AWR_WORLD) 不可用（构建失败）,$(WORLDPKG) build $(AWR_WORLD) --keep-staging); fi; \
	fi; \
	if [ $$rc -ne 0 ]; then echo "警告：部分世界构建失败（退出码 $$rc），其余照常；详情见 worlds/.status/" >&2; fi

worlds-force: ## 无条件重建六城（不做 --missing 判定）
	@$(call with_lock_sh,cd $(ROOT) && $(WORLDPKG) build --jobs $(J))

validate: ## worldpkg validate worlds/* --deep 与 Ajv strict 结构校验（错误为 0 才通过，D1-AC-01）
	@cd $(ROOT) && $(WORLDPKG) validate $(AWR_WORLDS_DIR)/* --deep || \
	  $(call fail,1,World Package 校验存在错误,$(WORLDPKG) build --missing)
	@cd $(ROOT) && node tools/contracts/check-world.mjs $(AWR_WORLDS_DIR)/* || \
	  $(call fail,1,World Package JSON 未通过 Ajv strict 校验,$(WORLDPKG) build --missing)

fetch-data: ## 下载并校验 UrbanScene3D 原始数据（VERIFY=1 只校验）
	@cd $(ROOT) && $(PY) -m awr.world.ingest.data_cli fetch urbanscene3d $(if $(VERIFY),--verify,) $(if $(FORCE),--force,)

worlds-status: ## 逐城状态与 --missing 预演（只读）
	@cd $(ROOT) && $(WORLDPKG) status

test-world: ## 只运行 M03 的 pytest（tests/world、tests/jobs）
	@$(call with_lock_sh,cd $(ROOT) && $(PY) -m pytest -q -m "not perf" tests/world tests/jobs $(PYTEST_ARGS))

perf-world: ## 构建性能用例（ADR-033 性能运行协议，排他锁）
	@$(call with_lock_ex,cd $(ROOT) && $(PY) -m pytest -q -m perf tests/world $(PYTEST_ARGS))
