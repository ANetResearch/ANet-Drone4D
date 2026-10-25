#!/usr/bin/env bash
# d3-gz 宿主侧 Python 依赖 $D/pylib-gz 的锁定与重建（K07；A10 遗留 F-78 (a)：原 pylib-gz 缺 mcap、lz4，Env 录制 raw.mcap 时
# ModuleNotFoundError；numpy、scipy 等与 requirements-d3.lock 不一致）。run_proto_board.sh、run_subworker.sh、run_gz_tests.sh、
# run_d3_tests.sh 与 bench_ctl.sh 以 PYTHONPATH 前置挂入 $D/pylib-gz，不再使用私有 pylib 目录。
# 用法（本机仓库根目录）：
#   tools/deploy/dmax/images/gz/pylib.sh lock      由 pylib-gz.in 生成 pylib-gz.lock（uv；以 requirements.lock、requirements-d3.lock
#                                                  为约束，cp312、manylinux_2_39，即 d3-gz 的 Ubuntu 24.04）
#   tools/deploy/dmax/images/gz/pylib.sh build     dmax 宿主按锁文件下载 wheel（--require-hashes，--no-deps）到 $D/wheels-gz；d3-gz 容器内
#                                                  离线安装到 $D/pylib-gz.new 并核对（pylib_check.py：逐包版本与导入）；通过后替换
#                                                  $D/pylib-gz，旧目录改名为 $D/pylib-gz.prev（回滚用，封板清理时删除）
#   tools/deploy/dmax/images/gz/pylib.sh install   只做离线安装到 $D/pylib-gz.new 并核对（不下载、不替换）
#   tools/deploy/dmax/images/gz/pylib.sh verify    核对现用 $D/pylib-gz 与锁文件一致并做导入核对（含 awr 入口，需 $D/python）
#   tools/deploy/dmax/images/gz/pylib.sh rollback  以 $D/pylib-gz.prev 换回
# 替换前确认没有挂载 $D/pylib-gz 的运行中容器（运行中的进程会在新旧两版之间混合导入）。
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../../../.." && pwd)
D=/data/anet-drone4d-d3/d3dev
B=/data/anet-drone4d-d3/build/pylib-gz
CMD=${1:?lock | build | install | verify | rollback}
PLATS="manylinux_2_39_x86_64 manylinux_2_35_x86_64 manylinux_2_31_x86_64 manylinux_2_28_x86_64 manylinux_2_27_x86_64 manylinux_2_26_x86_64
manylinux_2_24_x86_64 manylinux_2_17_x86_64 manylinux2014_x86_64 manylinux2010_x86_64 manylinux1_x86_64 linux_x86_64"
check() {   # $1 = 容器内目标目录（$D 挂载为 /repo）
  ssh dmax "docker run --rm --name d3-test-pylib-check-\$\$ --network none --cpus 2 --memory 4g --cgroup-parent d3.slice -v $D:/repo:ro -v $B:/b:ro --entrypoint python3 \
    d3-gz:dev /b/pylib_check.py $1 /b/pylib-gz.lock --repo /repo"
}
fetch() {   # dmax 宿主按锁文件下载 wheel（宿主有包索引镜像；容器内无 DNS）
  ssh dmax "mkdir -p $B $D/wheels-gz"
  rsync -a "$HERE/pylib-gz.lock" "$HERE/pylib_check.py" "dmax:$B/"
  ssh dmax "set -e; P=''; for t in $(echo $PLATS); do P=\"\$P --platform \$t\"; done; \
    pip3 download -q --only-binary=:all: --no-deps --require-hashes --python-version 3.12 --implementation cp --abi cp312 --abi abi3 --abi none \
      \$P -r $B/pylib-gz.lock -d $D/wheels-gz"
}
install() {   # d3-gz 容器内离线安装到 $D/pylib-gz.new（先清空）
  ssh dmax "mkdir -p $D/pylib-gz.new && docker run --rm --name d3-test-pylib-build-\$\$ --network none --cpus 4 --memory 8g --cgroup-parent d3.slice \
    -v $D/wheels-gz:/w:ro -v $B:/b:ro -v $D/pylib-gz.new:/out --entrypoint sh d3-gz:dev -c 'find /out -mindepth 1 -delete && \
    python3 -m pip install -q --no-index --find-links /w --no-deps --require-hashes --target /out -r /b/pylib-gz.lock && chmod -R a+rX /out'"
}
swap() {   # 新目录核对通过后替换；有挂载 $D 或 $D/pylib-gz 的运行中容器时不替换
  ssh dmax "set -e; if [ -n \"\$(docker ps -q --filter volume=$D --filter volume=$D/pylib-gz)\" ]; then echo 'running containers mount $D; retry when idle' >&2; exit 3; fi; \
    rm -rf $D/pylib-gz.prev; if [ -d $D/pylib-gz ]; then mv $D/pylib-gz $D/pylib-gz.prev; fi; mv $D/pylib-gz.new $D/pylib-gz"
}
case "$CMD" in
  lock)
    cd "$HERE"
    UV=${UV:-$(command -v uv || echo "$ROOT/.venv/bin/uv")}
    "$UV" pip compile pylib-gz.in -c "$ROOT/requirements.lock" -c "$ROOT/requirements-d3.lock" --generate-hashes --python-version 3.12 \
      --python-platform x86_64-manylinux_2_39 --custom-compile-command "tools/deploy/dmax/images/gz/pylib.sh lock" --quiet -o pylib-gz.lock
    ;;
  build) fetch; install; check /repo/pylib-gz.new; swap; check /repo/pylib-gz ;;
  install) install; check /repo/pylib-gz.new ;;     # 只安装到 pylib-gz.new 并核对（不替换）
  verify) check /repo/pylib-gz ;;
  rollback)
    ssh dmax "set -e; [ -d $D/pylib-gz.prev ]; rm -rf $D/pylib-gz.bad; mv $D/pylib-gz $D/pylib-gz.bad; mv $D/pylib-gz.prev $D/pylib-gz"
    ;;
  *) echo "unknown command $CMD" >&2; exit 2;;
esac
