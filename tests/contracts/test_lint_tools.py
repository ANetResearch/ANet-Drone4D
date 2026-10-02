"""tools/lint self tests (AWR-18 §13; D1-AC-20): JS rules via selftest.mjs, Python import boundaries on a synthetic tree."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import ROOT, node, run


def test_js_lint_selftest():
    n = node()
    if n is None:
        pytest.skip("node not found")
    r = run([n, "tools/lint/selftest.mjs"])
    assert r.returncode == 0, r.stdout + r.stderr


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_py_imports", ROOT / "tools/lint/check_py_imports.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_py_import_rules(tmp_path, monkeypatch):
    m = _load_checker()
    pkg = tmp_path / "python" / "awr"
    files = {
        "api/rest/x.py": "import numba\nfrom scipy.optimize import least_squares\nimport numpy\n",
        "runtime/bus.py": "import zenoh\n",
        "sim/fleet/y.py": "import zenoh\nimport numba\nfrom tools.contracts import gen\nK = 'ctl/sim-core/clock'\nT = 'sys/procs'\n",
        "swarm/z.py": "from awr.sim.core import state_model\n",
        "contracts/bus_keys.py": "CTL_CLOCK = 'ctl/sim-core/clock'\n",
    }
    for rel, src in files.items():
        p = pkg / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    monkeypatch.setattr(m, "PKG", pkg)
    monkeypatch.setattr(m, "ROOT", ROOT)
    key_rx, topic_rx = m._key_patterns()
    out: list = []
    for p in sorted(pkg.rglob("*.py")):
        monkeypatch.setattr(m, "ROOT", tmp_path)
        m.check_file(p, out, key_rx, topic_rx)
    got = sorted((v[0].split("awr/", 1)[1], v[3], v[4].split(":")[0]) for v in out)
    assert got == sorted([
        ("api/rest/x.py", "imports/boundary", "numba"),
        ("api/rest/x.py", "imports/boundary", "scipy.optimize"),
        ("api/rest/x.py", "imports/boundary", "scipy.optimize.least_squares"),
        ("sim/fleet/y.py", "imports/boundary", "zenoh"),
        ("sim/fleet/y.py", "imports/boundary", "tools.contracts"),
        ("sim/fleet/y.py", "imports/boundary", "tools.contracts.gen"),
        ("sim/fleet/y.py", "KEY-01", "zenoh key literal 'ctl/sim-core/clock'; use awr.contracts.bus_keys"),
        ("swarm/z.py", "imports/boundary", "awr.sim.core"),
        ("swarm/z.py", "imports/boundary", "awr.sim.core.state_model"),
    ])


def test_repository_passes_py_imports():
    r = run([sys.executable, "tools/lint/check_py_imports.py"])
    assert r.returncode == 0, r.stdout + r.stderr


def test_repository_passes_unit_suffix_lint():
    """AWR-17 §10.6 item 10 (UNIT-01)."""
    n = node()
    if n is None:
        pytest.skip("node not found")
    r = run([n, "tools/lint/check-units.mjs"])
    assert r.returncode == 0, r.stdout + r.stderr


def test_py_package_boundaries(tmp_path, monkeypatch):
    """AWR-10 §3.3 rule 2 (AWR-03 §4.2 rule 1): ARCH-AC-001 mutation `import awr.sim` in awr/api/rest/*.py is blocked,
    relative and `from awr import x` forms are resolved; ADR-045 wall clock ban in simulation paths."""
    m = _load_checker()
    pkg = tmp_path / "python" / "awr"
    files = {
        "api/rest/mut.py": "import awr.sim\nfrom awr import recorder\nfrom ..rest import other\nfrom ...world.geometry import q\n"
                           "from awr.runtime import bus\nfrom awr.world.package import catalog\nimport awr.world.georef.frames\n",
        "contracts/gen.py": "from awr.runtime import bus\nfrom awr.contracts import enums\n",
        "recorder/w.py": "import awr.sim.core\nfrom awr.runtime import statering\n",
        "sim/core/c.py": "from awr.sim.safety import guard\nfrom awr.world.geometry import open_world_query\nimport time\nt = time.monotonic()\n",
        "sim/fleet/f.py": "from time import perf_counter\n",
        "environment/stage/s.py": "from awr.sim.backends.base import X\nfrom awr.sim.core import Y\n",
        "world/geometry/g.py": "from awr.api.rest import world_query\n",
        "agent/runtime/a.py": "from awr.swarm import formation\nfrom awr.sim.core import state_model\n",
    }
    for rel, src in files.items():
        p = pkg / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    monkeypatch.setattr(m, "PKG", pkg)
    monkeypatch.setattr(m, "_api_whitelist", lambda: ("awr.world.georef.frames",))
    key_rx, topic_rx = m._key_patterns()
    monkeypatch.setattr(m, "ROOT", tmp_path)
    out: list = []
    for p in sorted(pkg.rglob("*.py")):
        m.check_file(p, out, key_rx, topic_rx)
    got = sorted({(v[0].split("awr/", 1)[1], v[4].split(":")[0]) for v in out})
    assert got == sorted([
        ("api/rest/mut.py", "awr.sim"),
        ("api/rest/mut.py", "awr.recorder"),
        ("api/rest/mut.py", "awr.world.geometry"),
        ("api/rest/mut.py", "awr.world.geometry.q"),
        ("contracts/gen.py", "awr.runtime"),
        ("contracts/gen.py", "awr.runtime.bus"),
        ("recorder/w.py", "awr.sim.core"),
        ("sim/core/c.py", "awr.sim.safety"),
        ("sim/core/c.py", "awr.sim.safety.guard"),
        ("sim/core/c.py", "time.monotonic()"),
        ("sim/fleet/f.py", "time.perf_counter"),
        ("environment/stage/s.py", "awr.sim.core"),
        ("environment/stage/s.py", "awr.sim.core.Y"),
        ("world/geometry/g.py", "awr.api.rest"),
        ("world/geometry/g.py", "awr.api.rest.world_query"),
        ("agent/runtime/a.py", "awr.sim.core"),
        ("agent/runtime/a.py", "awr.sim.core.state_model"),
    ])


def _load_callbacks():
    spec = importlib.util.spec_from_file_location("check_py_callbacks", ROOT / "tools/lint/check_py_callbacks.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_py_callback_rule(tmp_path, monkeypatch):
    """PY-CB-01 (AWR-18 §13.1; M11 §9.5 item 3): bus callbacks only enqueue."""
    m = _load_callbacks()
    f = tmp_path / "python" / "awr" / "sim" / "core" / "h.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        "def slow(req):\n    req.reply({'ok': True})\n\n\n"
        "def fast(req):\n    '''doc'''\n    q = INBOX\n    q.put(req)\n    SEEN[req.id] = 1\n\n\n"
        "async def coro(req):\n    await req.reply_async()\n\n\n"
        "def setup(bus, q):\n"
        "    bus.serve('k', slow)\n    bus.serve('k', fast)\n    bus.serve('k', coro)\n    bus.serve('k', q.put)\n"
        "    bus.subscribe('k', lambda s: print(s))\n    bus.subscribe('k', lambda s: q.put_nowait(s))\n"
        "    bus.watch('k', callback=slow)\n")
    monkeypatch.setattr(m, "ROOT", tmp_path)
    out: list = []
    m.check_file(f, out)
    assert sorted((v[1], v[3]) for v in out) == [(17, "PY-CB-01"), (21, "PY-CB-01"), (23, "PY-CB-01")]


def test_repository_passes_py_callbacks():
    r = run([sys.executable, "tools/lint/check_py_callbacks.py"])
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_thresholds_rules(tmp_path):
    """tools/ci/check-thresholds.mjs（AWR-18 §1.3 第 4 条，FX-GW）：仓库现状通过；THR-01/02/03/04 在合成输入上各触发一次。"""
    n = node()
    if n is None:
        pytest.skip("node not found")
    r = run([n, "tools/ci/check-thresholds.mjs"])
    assert r.returncode == 0, r.stdout + r.stderr
    script = tmp_path / "t.mjs"
    mod = (ROOT / "tools/ci/check-thresholds.mjs").as_posix()
    script.write_text("""
import { checkThresholds, isD1P0 } from 'MODULE'
const doc18 = ['| PERF-AC-001 | a | x | `vitest run t` | S | P0 | - |', '| PERF-AC-002 | b | x | `perf/b.spec.ts` | S | P0 | - |',
  '| PERF-AC-003 | c | x | `perf/c.spec.ts` | S | P0 | - |', '| PERF-AC-004 | d | x | `perf/d.spec.ts` | S | P0（V0.3） | - |'].join('\\n')
const doc03 = '| D1-AC-01 | w |'
const thr = { kinds: { other: 'x' }, entries: {
  ok: { op: '<=', value: 1, unit: 'ms', kind: 'other', status: 'frozen', source: '18', ac: ['PERF-AC-003'] },
  bad: { op: '~', value: 'x', unit: 'ms', kind: 'nope', status: 'frozen', source: '18', ac: ['PERF-AC-999'] } } }
const caseAcs = new Map([['@specs', new Map()]])
const r = checkThresholds({ thrText: JSON.stringify(thr, null, 1), doc18, doc03, caseAcs, strict: true })
console.log(JSON.stringify({ rules: r.violations.map((v) => v.rule).sort(), p0: r.p0, v03: isD1P0('P0（V0.3）'), bs: isD1P0('P0（B、S）/ P1（A）') }))
""".replace("MODULE", mod))
    r = run([n, str(script)])
    assert r.returncode == 0, r.stderr
    import json

    out = json.loads(r.stdout.strip().splitlines()[-1])
    # THR-01 bad 条目；THR-02 未知编号；THR-03 PERF-AC-002 无任何判据；THR-04 PERF-AC-001 只有 G1（--strict）
    assert out["rules"] == ["THR-01", "THR-02", "THR-03", "THR-04"], out
    assert out["p0"] == 3 and out["v03"] is False and out["bs"] is True


def test_check_deps_bom_rules(tmp_path):
    """tools/ci/bom.json（AWR-11 §7.3，FX-GW 续）：仓库 BOM 通过 --bom-only 与完整检查；validateBom 在合成文档上报出缺字段、
    枚举越界、非精确版本、runtime 缺边界与预案、最后提交晚于采集日、未排序与重复；PyPI 直接依赖解析只取锁定的 extra。"""
    n = node()
    if n is None:
        pytest.skip("node not found")
    for args in (["--bom-only"], []):
        r = run([n, "tools/lint/check-deps.mjs", *args])
        assert r.returncode == 0, r.stdout + r.stderr
    import json

    bom = json.loads((ROOT / "tools/ci/bom.json").read_text(encoding="utf-8"))
    assert bom["schemaVersion"] == "awr.bom.v1" and len(bom["items"]) >= 90
    script = tmp_path / "b.mjs"
    mod = (ROOT / "tools/lint/check-deps.mjs").as_posix()
    common = (ROOT / "tools/lint/_common.mjs").as_posix()
    script.write_text("""
import { validateBom, pythonDirectDeps } from 'MODULE'
import { Reporter } from 'COMMON'
const ok = { id: 'T11', upstream: 'mrdoob/three.js', ecosystem: 'npm', name: 'three', version: '0.186.1', scope: 'runtime', d1: 'core',
  adapterBoundary: ['apps/web/src/engine/**'], fallback: 'x', stars: 1, lastCommit: '2026-09-28', new2026: 'no', status: 'LOCKED' }
const doc = { schemaVersion: 'awr.bom.v2', generatedFrom: 'AWR-11', collectedAt: '2026-09-28', items: [
  ok,
  { ...ok, id: 'T12', name: 'b', version: '^1.0.0' },
  { ...ok, id: 'T13', name: 'c', adapterBoundary: [], fallback: undefined },
  { ...ok, id: 'T14', name: 'd', status: 'FROZEN', lastCommit: '2026-09-30' },
  { ...ok, id: 'T15', name: 'e', ecosystem: 'self', version: '-', upstream: '-', stars: null, lastCommit: null },
  { ...ok, id: 'T15', name: 'e', ecosystem: 'self', version: '-', upstream: '-', stars: null, lastCommit: null },
  { ...ok, id: 'T02', name: 'f', ecosystem: 'pypi', version: '1.2.3', scope: 'dev', stars: undefined } ] }
const R = new Reporter('t')
validateBom(R, doc)
const py = pythonDirectDeps('[project]\\ndependencies = [\\n  "numpy==2.5.3",\\n]\\n[project.optional-dependencies]\\n' +
  'test = ["pytest==9.1.1"]\\ngeo-worker = ["open3d==0.20.0"]\\n', 'uv==0.12.19\\n# x\\n')
console.log(JSON.stringify({ msgs: R.violations.map((v) => v.msg), rules: [...new Set(R.violations.map((v) => v.rule))], py: [...py.keys()].sort() }))
""".replace("MODULE", mod).replace("COMMON", common))
    r = run([n, str(script)])
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    msgs = "\n".join(out["msgs"])
    assert out["rules"] == ["deps/version"], out
    for needle in ("schemaVersion", "b (items[1]): version \"^1.0.0\" is not exact", "c (items[2]): runtime entries need a non-empty",
                   "c (items[2]): runtime entries need a fallback", "d (items[3]): status FROZEN", "lastCommit 2026-09-30 is after",
                   "duplicate entry self:e", "f (items[6]): stars must be", "items must be sorted"):
        assert needle in msgs, (needle, msgs)
    assert "e (items[4])" not in msgs  # self 条目无移植来源时 stars 与 lastCommit 可为 null
    assert out["py"] == ["numpy", "pytest", "uv"], out
