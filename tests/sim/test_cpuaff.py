"""sim-core 后台线程亲和性（`awr.sim.runtime.cpuaff`；ADR-017 CPU 分区的细化，ADR-070）。"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from awr.sim.runtime.cpuaff import AuxPinner, aux_cpus_for


def test_aux_cpus_rule() -> None:
    assert aux_cpus_for({1}, 8) == {0, 2, 3, 4, 5, 6, 7}
    assert aux_cpus_for(set(range(8)), 8) is None          # 未钉核（ci profile）：不调整
    assert aux_cpus_for(set(), 8) is None
    assert aux_cpus_for({1}, 8, env="7") == {7}
    assert aux_cpus_for({1}, 8, env="6,7,99") == {6, 7}    # 越界的 CPU 号忽略
    assert aux_cpus_for({1}, 8, env="x") is None


def test_unpinned_process_is_left_alone() -> None:
    p = AuxPinner(aux=None)
    assert not p.active and p.scan() == 0


@pytest.mark.skipif(not hasattr(os, "sched_setaffinity") or (os.cpu_count() or 1) < 2, reason="需要 ≥ 2 个 CPU")
def test_threads_moved_off_main_cpu() -> None:
    """子进程把自己钉在一个 CPU 上（等价于 supervisor 的 `cpus: [1]`），之后创建的线程继承该亲和性；scan 把主线程以外的
    线程改到其余 CPU，主线程不动；再次 scan 不重复改动。"""
    code = textwrap.dedent("""
        import os, threading, time
        from awr.sim.runtime.cpuaff import AuxPinner
        cpus = sorted(os.sched_getaffinity(0))
        main = {cpus[0]}
        os.sched_setaffinity(0, main)
        ev = threading.Event()
        t = threading.Thread(target=ev.wait, daemon=True)
        t.start()
        time.sleep(0.05)
        p = AuxPinner()
        n1 = p.scan()
        aff_t = set(os.sched_getaffinity(t.native_id))
        aff_main = set(os.sched_getaffinity(0))
        n2 = p.scan()
        ev.set()
        print(n1, n2, aff_main == main, aff_t == set(range(os.cpu_count())) - main)
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "AWR_SIM_AUX_CPUS": ""})
    assert out.returncode == 0, out.stderr
    n1, n2, main_ok, aux_ok = out.stdout.split()
    assert int(n1) >= 1 and int(n2) == 0 and main_ok == "True" and aux_ok == "True", out.stdout
