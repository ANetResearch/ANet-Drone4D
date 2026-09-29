# mk/ops.mk（M00）：运维目标，全部转发到 awr CLI（入口 awr.runtime.cli:main，M11；AWR-19 §7.2、§7.6）。
# awr CLI 尚未交付时以退出码 8（DEP_MISSING）失败并提示。

AWR_CLI := $(VENV)/bin/awr

.PHONY: stop status logs doctor gc-runs backup fetch-worlds images _need-awr-cli

_need-awr-cli:
	@$(PY) -c "import awr.runtime.cli" 2>/dev/null || \
	  $(call fail,$(EXIT_DEP_MISSING),awr CLI 不可用（awr.runtime.cli 尚未交付或未安装）,make setup；或等待 M11 交付 python/awr/runtime/cli.py)

stop: _need-awr-cli ## 停止当前运行（FORCE=1 回收失联运行）
	$(AWR_CLI) stop $(if $(FORCE),--force,)

status: _need-awr-cli ## 进程、指标、告警与容量（OFFLINE=1 读 StateRing 头）
	$(AWR_CLI) status $(if $(OFFLINE),--offline,)

logs: _need-awr-cli ## 查看日志：make logs P=<proc> [F=1]
	@[ -n "$(P)" ] || $(call fail,$(EXIT_GENERIC),缺少进程名,make logs P=sim-core)
	$(AWR_CLI) logs $(P) $(if $(F),-f,)

doctor: _need-awr-cli ## 环境诊断（DEEP=1 含 sha256 与世界深校验）
	$(AWR_CLI) doctor $(if $(DEEP),--deep,--quick)

gc-runs: _need-awr-cli ## 立即执行 runs/ 配额回收
	$(AWR_CLI) runs gc

backup: _need-awr-cli ## 备份 keep 运行与原始数据归档（P1）
	$(AWR_CLI) backup

fetch-worlds: _need-awr-cli ## 下载预构建世界制品（P1）
	$(AWR_CLI) data fetch worlds

images: ## 构建后端 Docker 镜像（V0.2，P2）
	@[ -f "$(ROOT)/tools/docker/backend.Dockerfile" ] || $(call fail,$(EXIT_GENERIC),tools/docker/backend.Dockerfile 尚未提供（V0.2）,无)
	docker build -f tools/docker/backend.Dockerfile $(ROOT)
