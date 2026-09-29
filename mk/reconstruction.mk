# mk/reconstruction.mk（M01）：重建引擎的测试与演示目标（M01 §9.5；AWR-03 §4.3 各模块只新增 mk/<module>.mk）。
#   make test-recon    只运行 tests/reconstruction（不含 perf 与 slow；make test 已包含全部非 perf 用例）
#   make recon-demo    对深圳在进程内跑一次 Mock 重建（helix、600 帧），发布 shenzhen-recon-01（演示段 D6b）
#   make recon-golden  写出 golden 会话与 12 个变异样本到 runs/recon-golden/（供 M00 合入 packages/contracts/fixtures）
# fetch-da3（V0.2，DA3-SMALL 冒烟）尚未提供。

RECON := $(PY) -m awr.reconstruction
RECON_TARGET ?= shenzhen-recon-01

.PHONY: test-recon recon-demo recon-golden

test-recon: ## 只运行 M01 的 pytest（tests/reconstruction，不含 perf、slow）
	@$(call with_lock_sh,cd $(ROOT) && $(PY) -m pytest -q -m "not perf and not slow" tests/reconstruction $(PYTEST_ARGS))

recon-demo: ## Mock 重建演示：深圳 helix 600 帧 → worlds/$(RECON_TARGET)
	@$(call with_lock_sh,cd $(ROOT) && $(RECON) run --world shenzhen --path helix --frames 600 --target $(RECON_TARGET) --seed 1)

recon-golden: ## 写出 Recon IR golden 会话与变异样本（runs/recon-golden/）
	@cd $(ROOT) && $(RECON) golden runs/recon-golden
