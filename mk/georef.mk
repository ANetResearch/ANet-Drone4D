# mk/georef.mk（M02）：帧换算、时间基、Sim3 与 LiDAR 契约桩的测试目标（M02 §9.1；AWR-03 §4.3）。
#   make test-georef      只运行 tests/georef 与 apps/web/tests/geo（不含 perf；make test 已包含）
#   make golden-frames    重新生成 packages/contracts/golden/frames（M00 的生成器）并校验；补充 golden 见下一目标
#   make golden-georef    重新生成 tests/georef/golden/local_time_supplement.json（独立 oracle 的 uavNN/local 与 GPST/UTC 用例）
#   make bench-georef     M02 性能用例（tap 批量、5e6 点 world_to_lla、traj_sim3、TS 零分配；排他性能锁）

.PHONY: test-georef golden-frames golden-georef bench-georef

test-georef: ## 只运行 M02 的 pytest（tests/georef）与 Vitest（apps/web/tests/geo）
	@$(call with_lock_sh,cd $(ROOT) && $(PY) -m pytest -q -m "not perf" tests/georef $(PYTEST_ARGS))
	@cd $(WEB) && $(NPX) vitest run --project unit tests/geo $(VITEST_ARGS)

golden-frames: ## 重新生成帧换算 golden（tools/contracts/gen_frames_golden.py，M00）并对拍
	@cd $(ROOT) && $(PY) tools/contracts/gen_frames_golden.py && $(PY) -m pytest -q tests/georef/test_frames.py tests/georef/test_time.py

golden-georef: ## 重新生成独立 oracle 的补充 golden（tests/georef/golden/）
	@cd $(ROOT) && $(PY) tests/georef/gen_supplement_golden.py && $(PY) -m pytest -q tests/georef/test_golden_supplement.py

bench-georef: ## M02 性能用例（性能运行协议）
	@$(call with_lock_ex,cd $(ROOT) && $(PY) -m pytest -q -m perf tests/georef/test_perf.py)
	@$(call with_lock_ex,cd $(WEB) && $(NPX) vitest bench --run tests/geo/frames.alloc.bench.ts)
