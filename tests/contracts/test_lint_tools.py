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
