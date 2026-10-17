# D3 环境（AWR-05 v1.2 §4.8；M22 §7.4）：D3 依赖装在独立虚拟环境 .venv-d3，内容 = requirements.lock + requirements-d3.lock；
# D2 的 .venv、pyproject.toml 与 requirements.lock 不变。D3 测试在缺少 D3 依赖时跳过（pytest.importorskip）。
D3_VENV := $(ROOT)/.venv-d3
D3_PY   := $(D3_VENV)/bin/python
D3_LOCK_CMD = $(UV) pip compile requirements-d3.in -c requirements.lock --generate-hashes --python $(PY) \
  --custom-compile-command "make lock-d3" --quiet

.PHONY: lock-d3 setup-d3 test-d3
lock-d3: ## 由 requirements-d3.in 生成 requirements-d3.lock（以 requirements.lock 为约束）
	cd $(ROOT) && $(D3_LOCK_CMD) -o requirements-d3.lock

setup-d3: ## 创建 .venv-d3：requirements.lock + requirements-d3.lock，可编辑安装 awr
	@[ -x "$(D3_PY)" ] || $(PY) -m venv "$(D3_VENV)"
	cd $(ROOT) && $(UV) pip sync requirements.lock requirements-d3.lock --python $(D3_PY) $(if $(OFFLINE),--offline,)
	cd $(ROOT) && $(D3_VENV)/bin/pip install --quiet -e . --no-deps $(if $(OFFLINE),--no-index --no-build-isolation,)

test-d3: ## D3 测试（tests/d3，.venv-d3）
	cd $(ROOT) && $(D3_PY) -m pytest -q tests/d3 $(PYTEST_ARGS)
