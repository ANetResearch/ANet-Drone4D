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

# ---- D3 回归门：合入前强制（D3-AC-01；AWR-05 §4.8；封板任务 G05） -----------------------------------------------------
# 步骤登记在 tests/d3/regress_gate/gate.json，由 tools/bench/d3/regress_gate.py 执行；本节只定义档位、被检查的树与证据位置。
#   make gate-d3              完整门：gate.json 的缺省步骤（第 1–6 条；第 3 条性能子集自 G04 起为缺省步骤，GATE_PERF 保留兼容）
#   make gate-d3-light        轻量门：不需要数据、GPU、浏览器与独占主机的步骤（GATE_D3_LIGHT）；托管 CI 在 main 的 push 与
#                             指向 main 的 PR 上运行同一目标（.github/workflows/ci.yml 的 d3-gate 任务）
#   make gate-d3-dmax         重载部分（GATE_D3_HEAVY：D2 Python 测试全集、零影响对拍、resim、性能子集、import 方向）在 dmax CPU 池容器中
#                             运行（tools/deploy/dmax/run_d3_gate.sh；不挂 GPU）
#   make gate-d3-nightly      夜间作业：取 origin/main，dmax 跑重载部分，本机跑其余缺省步骤；两份报告合起来覆盖完整门
#   make gate-d3-check        合入前检查：被检查的树已有覆盖所需步骤且全部通过的报告（GATE_TIER=light 缺省，full 要求完整门，
#                             可由多份报告合并覆盖，每步取最近一次结果）；不通过时 make 以非零状态退出
#   make gate-d3-hook         安装 pre-push 钩子：推送 main 前对被推送的提交执行 gate-d3-check（GATE_TIER 同上）
# 被检查的树：GATE_REF 为空时为当前工作树；给出提交（HEAD、origin/main、sha）时 git archive 导出到 GATE_WORK/src-<tag>.<随机>
# （缺省 ~/.cache/awr/d3-gate，在本检出之外：检出内的 .cache/ 被 .gitignore 忽略，oxlint 会因此找不到文件），在副本中运行（node_modules 逐项链接本检出、@awr/* 工作区包指向副本，worlds/ 链接 AWR_WORLDS_DIR，PYTHONPATH = 副本的
# python/，git 用临时索引 read-tree 该提交，本检出的索引与工作树不受影响）。多个任务共用一个工作树时，以 GATE_REF 检查
# 将要合入的提交，避免把他人未提交的改动计入证据。
# 证据：runs/perf/d3-gate/<tag>/<档位>/{report.json, meta.json, gate.log}；<tag> 为被检查内容的 git tree id（12 位），工作树有
# 改动时加 -dirty-<改动摘要 8 位>（同 run_d3_tests.sh）。以 tree id 为键：提交前在工作树上运行的结论在提交后仍然对应同一份内容。
# 运行期间工作树内容发生变化时 meta.json 记 tree_changed = true，gate-d3-check 不采信该报告。
# 夜间作业（每日 09:30 UTC，西雅图 02:30）登记在本机 crontab，需用户确认后安装：
#   30 9 * * * cd $(ROOT) && make gate-d3-nightly >> runs/perf/d3-gate/nightly.log 2>&1
GATE_D3_LIGHT ?= contracts,web-unit,lint,imports,resim,public-build,k9-d3,demo-parity,lab-build
GATE_D3_HEAVY ?= pytest,zero-impact,resim,perf,imports
GATE_D3_ROOT  ?= $(ROOT)/runs/perf/d3-gate
GATE_D3_JSON  := $(ROOT)/tests/d3/regress_gate/gate.json
GATE_REF      ?=
GATE_STEPS    ?=
GATE_TIER     ?= light
GATE_PERF     ?= 0
GATE_KEEP     ?= 0
GATE_WORK     ?= $(HOME)/.cache/awr/d3-gate
NIGHTLY_REF   ?= origin/main

# 门运行脚本（bash；以 $(value …) 原样导出，脚本内的 $ 不经 make 展开）。参数：tag | run <档位> <步骤>；
# 输入环境：GATE_ROOT_DIR（本检出）、GATE_PY、GATE_REF、GATE_D3_ROOT、GATE_KEEP、GATE_WORK、AWR_WORLDS_DIR。
define _GATE_D3_SH
set -euo pipefail
cd "$GATE_ROOT_DIR"
export GIT_OPTIONAL_LOCKS=0
if [ ! -e .git ] && [ -z "${GIT_DIR:-}" ] && [ -d /data/projs/.anet-drone4d.git ]; then
  export GIT_DIR=/data/projs/.anet-drone4d.git GIT_WORK_TREE=$GATE_ROOT_DIR
fi
tree_tag() {
  if ! git rev-parse --verify -q HEAD >/dev/null 2>&1; then echo nogit; return; fi
  if [ -n "${1:-}" ]; then git rev-parse --verify -q "$1^{tree}" | cut -c1-12; return; fi
  local t; t=$(git rev-parse 'HEAD^{tree}' | cut -c1-12)
  if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
    printf '%s-dirty-%s\n' "$t" "$({ git diff HEAD --binary; git ls-files -o --exclude-standard -z | xargs -0 -r sha256sum; } | sha256sum | cut -c1-8)"
  else
    echo "$t"
  fi
}
case "$1" in
  tag) tree_tag "${GATE_REF:-}"; exit 0;;
  run) ;;
  *) echo "usage: tag | run <tier> <steps>" >&2; exit 2;;
esac
tier=$2 steps=${3:-} ref=${GATE_REF:-}
tag=$(tree_tag "$ref")
if [ -z "$tag" ]; then echo "[d3-gate] cannot resolve GATE_REF=$ref" >&2; exit 2; fi
if [ "$tag" = nogit ]; then tag=nogit-$(date -u +%Y%m%dT%H%M%SZ); fi
out=$GATE_D3_ROOT/$tag/$tier
rm -rf "$out"; mkdir -p "$out"
sha=$(git rev-parse --verify -q "${ref:-HEAD}^{commit}" 2>/dev/null || echo none)
src=$GATE_ROOT_DIR
if [ -n "$ref" ]; then
  gd=$(git rev-parse --absolute-git-dir)
  mkdir -p "$GATE_WORK"
  src=$(mktemp -d "$GATE_WORK/src-$tag.XXXXXX")
  mkdir -p "$src/node_modules/@awr"
  git archive --format=tar "$sha" | tar -x -C "$src"
  for e in "$GATE_ROOT_DIR"/node_modules/* "$GATE_ROOT_DIR"/node_modules/.[!.]*; do
    n=${e##*/}
    case "$n" in '.[!.]*'|.vite-temp|@awr) ;; *) ln -s "$e" "$src/node_modules/$n";; esac
  done
  for e in "$GATE_ROOT_DIR"/node_modules/@awr/*; do cp -P "$e" "$src/node_modules/@awr/"; done
  [ -e "$src/worlds" ] || ln -s "$AWR_WORLDS_DIR" "$src/worlds"
  export GIT_DIR=$gd GIT_WORK_TREE=$src GIT_INDEX_FILE=$src.index
  git read-tree "$sha"
  git update-index -q --refresh >/dev/null 2>&1 || true
fi
drop=
if [ -n "$steps" ]; then
  known=$(GJ="$src/tests/d3/regress_gate/gate.json" "$GATE_PY" -c 'import json, os; print(",".join(s["id"] for s in json.load(open(os.environ["GJ"]))["steps"]))')
  keep=
  for s in ${steps//,/ }; do
    case ",$known," in *",$s,"*) keep=$keep${keep:+,}$s;; *) drop=$drop${drop:+,}$s;; esac
  done
  if [ -n "$drop" ]; then echo "[d3-gate] not registered in the gate.json of this tree, not run (gate-d3-check reports them missing): $drop" >&2; fi
  steps=$keep
fi
t0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "[d3-gate] tier $tier  tag $tag  ref ${ref:-worktree} ($sha)  steps ${steps:-default}  src $src"
if [ -z "$steps" ] && [ -n "$drop" ]; then echo "[d3-gate] no registered step to run" >&2; exit 2; fi
set +e
(cd "$src" && PYTHONPATH="$src/python${PYTHONPATH:+:$PYTHONPATH}" "$GATE_PY" tools/bench/d3/regress_gate.py ${steps:+--steps "$steps"} \
  --out "$out") 2>&1 | tee "$out/gate.log"
rc=${PIPESTATUS[0]}
set -e
after=$tag
if [ -z "$ref" ]; then after=$(tree_tag ""); fi
M_TIER=$tier M_TAG=$tag M_AFTER=$after M_REF=${ref:-worktree} M_SHA=$sha M_STEPS=$steps M_DROP=$drop M_T0=$t0 M_RC=$rc M_SRC=$src \
"$GATE_PY" -c 'import datetime, json, os, platform, sys
e = os.environ
m = {"schema": "awr.d3.regress_gate.meta.v1", "tier": e["M_TIER"], "tag": e["M_TAG"], "ref": e["M_REF"], "commit": e["M_SHA"],
     "dirty": "-dirty-" in e["M_TAG"], "tree_changed": e["M_AFTER"] != e["M_TAG"], "tag_after": e["M_AFTER"],
     "steps": [s for s in e["M_STEPS"].split(",") if s] or "default", "not_registered": [s for s in e["M_DROP"].split(",") if s],
     "started_utc": e["M_T0"],
     "finished_utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "exit": int(e["M_RC"]),
     "host": platform.node(), "runner": "local", "src": e["M_SRC"]}
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(m, indent=1, ensure_ascii=False) + "\n")' "$out/meta.json"
if [ "$after" != "$tag" ]; then echo "[d3-gate] WARNING: the tree changed during the run ($tag -> $after); gate-d3-check ignores this report" >&2; fi
if [ -n "$ref" ] && [ "${GATE_KEEP:-0}" != 1 ]; then rm -rf "$src" "$GIT_INDEX_FILE"; fi
echo "[d3-gate] evidence $out"
exit "$rc"
endef

# 合入前检查（python）：argv = 档位 tag 证据根 gate.json 轻量步骤 重载步骤 GATE_PERF；每步取该 tag 下各报告中最近一次结果。
define _GATE_D3_CHECK_PY
import json, sys
from pathlib import Path
tier, tag, root, gate, light, heavy, perf = sys.argv[1:8]
g = json.loads(Path(gate).read_text(encoding="utf-8"))
default = [s["id"] for s in g["steps"] if s.get("default", True)]
need = {"light": light.split(","), "heavy": heavy.split(","), "full": default + (["perf"] if perf == "1" else [])}.get(tier)
if need is None:
    sys.exit(f"[d3-gate-check] unknown GATE_TIER={tier} (light | heavy | full)")
latest, ignored = {}, []
for rep in sorted((Path(root) / tag).glob("*/report.json")):
    mp = rep.parent / "meta.json"
    meta = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    if meta.get("tag") != tag or meta.get("tree_changed", True):
        ignored.append(f"{rep.parent.name} ({'tree changed during the run' if meta else 'no meta.json'})")
        continue
    r = json.loads(rep.read_text(encoding="utf-8"))
    for sid, s in r.get("steps", {}).items():
        if sid not in latest or r["utc"] >= latest[sid][0]:
            latest[sid] = (r["utc"], bool(s.get("pass")), rep.parent.name)
missing = [s for s in need if s not in latest]
failed = [f"{s} ({latest[s][2]} {latest[s][0]})" for s in need if s in latest and not latest[s][1]]
print(f"[d3-gate-check] tree {tag}  tier {tier}  need {len(need)} steps  evidence {Path(root) / tag}")
for x in ignored:
    print(f"[d3-gate-check]   ignored report: {x}")
if "-dirty-" in tag:
    print("[d3-gate-check]   note: uncommitted changes; the verdict holds for this working tree only")
if missing or failed:
    print(f"[d3-gate-check] FAIL  missing {missing or '-'}  failed {failed or '-'}")
    sys.exit(1)
print("[d3-gate-check] PASS  " + ", ".join(f"{s}@{latest[s][2]}" for s in need))
endef

export _GATE_D3_SH_TEXT = $(value _GATE_D3_SH)
export _GATE_D3_CHECK_TEXT = $(value _GATE_D3_CHECK_PY)
GATE_D3_ENV = GATE_ROOT_DIR=$(ROOT) GATE_PY=$(PY) GATE_REF=$(GATE_REF) GATE_D3_ROOT=$(GATE_D3_ROOT) GATE_KEEP=$(GATE_KEEP) GATE_WORK=$(GATE_WORK) \
  VENV=$(VENV) AWR_PERF_LOCK=$(AWR_PERF_LOCK)
GATE_D3_RUN = $(GATE_D3_ENV) bash -c "$$_GATE_D3_SH_TEXT" d3-gate
GATE_D3_TAG = $(GATE_D3_ENV) bash -c "$$_GATE_D3_SH_TEXT" d3-gate tag

.PHONY: gate-d3 gate-d3-light gate-d3-dmax gate-d3-nightly gate-d3-check gate-d3-tag gate-d3-hook
gate-d3: _need-installed ## D3 回归门完整门（gate.json 缺省步骤；GATE_REF=<提交> 在导出副本中运行，GATE_STEPS=、GATE_PERF=1）
	@$(GATE_D3_RUN) run full "$(if $(GATE_STEPS),$(GATE_STEPS),$(if $(filter 1,$(GATE_PERF)),$(shell $(PY) -c 'import json; print(",".join([s["id"] for s in json.load(open("$(GATE_D3_JSON)"))["steps"] if s.get("default", True)] + ["perf"]))'),))"

gate-d3-light: _need-installed ## D3 回归门轻量门（GATE_D3_LIGHT；托管 CI 同一目标；GATE_REF=<提交> 检查将要合入的提交）
	@$(GATE_D3_RUN) run light "$(GATE_D3_LIGHT)"

gate-d3-dmax: ## D3 回归门重载部分在 dmax CPU 池运行（GATE_D3_HEAVY；GATE_REF 缺省 HEAD；证据回传 runs/perf/d3-gate/<tag>/heavy）
	@cd $(ROOT) && REF=$(if $(GATE_REF),$(GATE_REF),HEAD) STEPS=$(GATE_D3_HEAVY) GATE_D3_ROOT=$(GATE_D3_ROOT) \
	  bash tools/deploy/dmax/run_d3_gate.sh

gate-d3-nightly: _need-installed ## 夜间作业：取 NIGHTLY_REF（origin/main），dmax 跑重载部分、本机跑其余缺省步骤，再按完整门检查
	@cd $(ROOT) && $(GATE_D3_ENV) bash -c 'cd "$$GATE_ROOT_DIR"; [ -e .git ] || export GIT_DIR=$${GIT_DIR:-/data/projs/.anet-drone4d.git} GIT_WORK_TREE=$$GATE_ROOT_DIR; \
	  git fetch --quiet origin main'
	@echo "[d3-gate] nightly $(NIGHTLY_REF) $$(date -u +%Y-%m-%dT%H:%M:%SZ)"
	-@$(MAKE) --no-print-directory gate-d3-dmax GATE_REF=$(NIGHTLY_REF)
	-@$(MAKE) --no-print-directory gate-d3 GATE_REF=$(NIGHTLY_REF) GATE_STEPS=$$($(PY) -c 'import json; h = set("$(GATE_D3_HEAVY)".split(",")); print(",".join(s["id"] for s in json.load(open("$(GATE_D3_JSON)"))["steps"] if s.get("default", True) and s["id"] not in h))')
	@$(MAKE) --no-print-directory gate-d3-check GATE_REF=$(NIGHTLY_REF) GATE_TIER=full

gate-d3-tag: ## 打印被检查的树的 tag（GATE_REF 为空时为当前工作树）
	@$(GATE_D3_TAG)

gate-d3-check: ## 合入前检查：被检查的树已有覆盖 GATE_TIER（light | heavy | full）全部步骤且通过的报告；否则失败
	@tag=$$($(GATE_D3_TAG)) && [ -n "$$tag" ] || { echo "[d3-gate-check] cannot resolve GATE_REF=$(GATE_REF)" >&2; exit 2; }; \
	  $(PY) -c "$$_GATE_D3_CHECK_TEXT" "$(GATE_TIER)" "$$tag" "$(GATE_D3_ROOT)" "$(GATE_D3_JSON)" "$(GATE_D3_LIGHT)" "$(GATE_D3_HEAVY)" "$(GATE_PERF)"

gate-d3-hook: ## 安装 pre-push 钩子：推送 refs/heads/main 前对被推送的提交执行 gate-d3-check（GATE_TIER，缺省 light）
	@cd $(ROOT) && { [ -e .git ] || export GIT_DIR=$${GIT_DIR:-/data/projs/.anet-drone4d.git} GIT_WORK_TREE=$(ROOT); } && \
	  h=$$(git rev-parse --git-path hooks)/pre-push && mkdir -p "$${h%/*}" && \
	  printf '%s\n' '#!/usr/bin/env bash' '# D3 回归门合入前检查（mk/d3.mk gate-d3-hook 安装；D3-AC-01）' 'set -euo pipefail' \
	    'while read -r lref lsha rref rsha; do' \
	    '  [ "$$rref" = refs/heads/main ] && [ "$$lsha" != 0000000000000000000000000000000000000000 ] || continue' \
	    "  make -s -C $(ROOT) gate-d3-check GATE_REF=\"\$$lsha\" GATE_TIER=$(GATE_TIER)" 'done' > "$$h" && chmod +x "$$h" && \
	  echo "pre-push hook installed: $$h (GATE_TIER=$(GATE_TIER))"
