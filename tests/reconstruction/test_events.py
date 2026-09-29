"""Job events (M01-AC-015; M01-FR-040; M01-NFR-004).

From a complete job: any 1 s window holds <= 4 `job.progress`, `progress_pct` never decreases, exactly one terminal
`job.state`, and `world.added` precedes it. Over the real event plane (M11 EventPublisher / EventSubscriber on LocalBus)
with 30 % of the messages dropped on the consumer side, the terminal state is recovered through `_replay` within 1 s.
"""

from __future__ import annotations

import itertools
import time

import numpy as np
from recon_common import needs_world

from awr.reconstruction.jobs.progress import JobEvents
from awr.reconstruction.types import RECON_STAGES


def _windows_ok(ts: list[float], limit: int = 4) -> bool:
    ts = sorted(ts)
    j = 0
    for i, t in enumerate(ts):
        while ts[j] <= t - 1.0:
            j += 1
        if i - j + 1 > limit:
            return False
    return True


@needs_world
def test_event_stream_of_a_job(chain_run):
    ev = chain_run["events"]
    prog = [(t, d) for t, k, d in ev if k == "job.progress"]
    states = [(t, d) for t, k, d in ev if k == "job.state"]
    assert prog and _windows_ok([t for t, _ in prog])
    pct = [d["progress_pct"] for _, d in prog]
    assert all(b >= a for a, b in itertools.pairwise(pct)) and pct[-1] >= 90.0
    terminal = [d for _, d in states if d["state"] in ("SUCCEEDED", "FAILED", "CANCELLED")]
    assert len(terminal) == 1 and terminal[0]["state"] == "SUCCEEDED" and terminal[0]["progress_pct"] == 100.0
    assert terminal[0]["output"]["scale_status"] == "gnss" and set(terminal[0]["stage_durations_s"]) == set(RECON_STAGES)
    kinds = [k for _, k, _ in ev]
    assert kinds.index("world.added") < max(i for i, k in enumerate(kinds) if k == "job.state")
    p = prog[len(prog) // 2][1]
    assert {"job_id", "kind", "state", "stage", "stage_index", "stage_progress", "progress_pct", "eta_s", "rss_mb",
            "paused_reason"} <= set(p) and p["kind"] == "recon"
    inf = [d for _, d in prog if d["stage"] == "INFERRING" and "frames_done" in d]
    assert inf and inf[-1]["frames_total"] == 60


def test_throttle_keeps_latest_and_state_is_unthrottled():
    clock = [0.0]
    je = JobEvents("j-x", clock=lambda: clock[0])
    for i in range(1000):                       # 1000 offers over 10 s of fake wall clock
        clock[0] = i * 0.01
        je.progress({"progress_pct": i / 10.0})
        if i % 100 == 0:
            je.state({"state": "INFERRING"})
    prog = [t for t, k, _ in je.sent if k == "job.progress"]
    assert _windows_ok(prog) and 38 <= len(prog) <= 41
    assert len([k for _, k, _ in je.sent if k == "job.state"]) == 10
    je.progress({"progress_pct": 5.0}, force=True)                           # never decreases
    assert je.sent[-1][2]["progress_pct"] >= 99.9


def test_terminal_state_recovered_through_replay():
    from awr.runtime.bus import LocalBus, local_net
    from awr.runtime.events import EventPublisher, EventSubscriber

    net = local_net(f"recon-events-{time.monotonic_ns()}")
    pub_bus = LocalBus.open("job-worker", net=net, announce=False)
    sub_bus = LocalBus.open("api", net=net, announce=False)
    pub = EventPublisher(pub_bus, "job-worker", epoch=1)
    got: list[dict] = []
    gaps: list = []
    sub = EventSubscriber(sub_bus, on_events=lambda prod, evs: got.extend(evs), on_gap=lambda *a: gaps.append(a))
    r = np.random.default_rng(3)
    sub.filter = lambda key, raw: r.random() >= 0.30                          # drop 30 % of the messages
    je = JobEvents("j-replay", sink=pub)
    for s in RECON_STAGES:
        je.state({"state": s})
        for i in range(20):
            je.progress({"progress_pct": RECON_STAGES.index(s) * 14 + i * 0.5}, force=True)
    je.world_added("shenzhen-recon-09", "0123456789ab")
    je.state({"state": "SUCCEEDED"})
    t0 = time.monotonic()
    done = False
    while time.monotonic() - t0 < 1.0 and not done:
        sub.pump()
        pub.serve_replays()
        done = any(e["kind"] == "job.state" and e["data"]["state"] == "SUCCEEDED" for e in got)
        time.sleep(0.01)
    assert done, (len(got), sub.stats)
    assert not gaps and sub.stats["gaps_filled"] >= 1
    seqs = [e["seq"] for e in got]
    # delivery starts at the first event this subscriber saw (no back-fill before subscription), then has no hole
    assert seqs == list(range(seqs[0], pub.last_seq + 1))
    assert [e["data"]["state"] for e in got if e["kind"] == "job.state"].count("SUCCEEDED") == 1
    pub.close()
    sub_bus.close()
    pub_bus.close()
