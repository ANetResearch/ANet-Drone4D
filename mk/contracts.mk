# mk/contracts.mk（M00-B）：契约生成物、golden 与夹具的生成、校验与测试。
# 依据：AWR-03 §4.2 第 3 条、§5.10；AWR-17 §10；AWR-18 §8.3（CI 顺序：--check → pytest → vitest）；M07-FR-007。
# 生成器：tools/contracts/{gen.py, gen.mjs, gen_golden.py, gen_frames_golden.py, gen_env_golden.py, gen_fixtures.py,
# gen_openapi.py}。gen_openapi.py 写 REST 快照 packages/contracts/rest/openapi.snapshot.json（AWR-17 §10.6 第 9 条，FX-GW 交付）。

.PHONY: contracts contracts-check test-contracts

CONTRACT_PY_GENS := gen.py gen_golden.py gen_frames_golden.py gen_env_golden.py gen_fixtures.py gen_openapi.py

contracts: ## 重新生成契约生成物、golden、夹具与 OpenAPI 快照（python/awr/contracts、packages/contracts/gen/ts、golden、fixtures、rest）
	cd $(ROOT) && $(PY) tools/contracts/gen.py
	cd $(ROOT) && node tools/contracts/gen.mjs
	cd $(ROOT) && for g in gen_golden.py gen_frames_golden.py gen_env_golden.py gen_fixtures.py gen_openapi.py; do $(PY) tools/contracts/$$g || exit $$?; done

contracts-check: ## 校验生成物、golden 与夹具和契约源一致（--check，不改写文件）
	@cd $(ROOT) && for g in $(CONTRACT_PY_GENS); do $(PY) tools/contracts/$$g --check || \
	  $(call fail,$(EXIT_GENERIC),tools/contracts/$$g --check 发现生成物与契约源不一致,make contracts); done
	@cd $(ROOT) && node tools/contracts/gen.mjs --check || \
	  $(call fail,$(EXIT_GENERIC),tools/contracts/gen.mjs --check 发现 TS 生成物与契约源不一致,make contracts)

test-contracts: contracts-check ## 契约 CI（AWR-17 §10.6）：--check → 单位与 key lint → pytest tests/contracts tests/georef → vitest
	cd $(ROOT) && node tools/lint/check-units.mjs && $(PY) tools/lint/check_py_imports.py
	cd $(ROOT) && $(PY) -m pytest -q tests/contracts tests/georef $(PYTEST_ARGS)
	cd $(WEB) && $(NPX) vitest run --project unit tests/contracts tests/geo $(VITEST_ARGS)

# 早期数据源与协议夹具（D1-AC-35、DATA-AC-018、M11-AC-040；请求 M11-R-to-M00 第 4 条）：
#   gen_fixtures --check → pytest 夹具解码与 payload golden、fake_gw N ∈ {1, 200, 1000} 合成与 .awrrt 回放对拍
#   → Vitest TS 参考路径解码夹具（tests/contracts/frame.test.ts）与 FakeSource 用例（apps/web/tests/net、tests/m11，M11 交付后自动纳入）。
# make test 已全量运行这些 pytest 与 Vitest 用例，这里只是 D1-AC-35 的验收入口，不追加到 TEST_TARGETS。
.PHONY: test-fixtures
test-fixtures: ## 早期数据源与协议夹具（D1-AC-35）：夹具 --check、夹具解码 golden、fake_gw 合成与回放、TS 解码与 FakeSource
	cd $(ROOT) && $(PY) tools/contracts/gen_fixtures.py --check
	cd $(ROOT) && $(PY) -m pytest -q tests/contracts/test_fixtures.py tests/runtime/test_fake_gw.py $(PYTEST_ARGS)
	cd $(WEB) && $(NPX) vitest run --project unit --passWithNoTests tests/contracts/frame.test.ts tests/net tests/m11 $(VITEST_ARGS)

# make setup 末尾校验生成物；make ci 在 pytest 与 Vitest 之前先做 --check（make test 中 tests/contracts/test_codegen.py 也会执行）
SETUP_TARGETS += contracts-check
CI_TARGETS += contracts-check
