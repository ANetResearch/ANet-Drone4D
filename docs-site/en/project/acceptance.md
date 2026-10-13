# Acceptance summary

V0.2-demo (D2 and D2.1) is accepted against 55 items: D2-AC-01 to D2-AC-55 in sections 13 and 15.14 of the design document docs/04, of which
50 are P0 (release blocking) and 5 are P1. This page summarizes the results of acceptance round 2; per-item results, evidence and diagnosis
are in the full reports on GitHub.

## Results

Round 1 (2026-10-11 to 10-12) measured all 55 items. After the fix work package D2-FX-R2, round 2 (2026-10-12 11:59 to 10-13 13:45 UTC,
Seattle 10-12 04:59 to 10-13 06:45, build 0.2.0) re-measured the 18 items that had failed or were not measured in round 1, and covered the
others with a full functional regression:

| Items | Passed | Failed | Not measured |
|---|---|---|---|
| P0 (50 items) | **41** | 9 | 0 |
| P1 (5 items) | **3** | 0 | 2 |
| **Total (55 items)** | **44** | **9** | **2** |

The D2 exit criteria (all P0 passing, at least 80% of P1 passing) are not yet met; 0.2.0 was released with known issues.

## Fixes confirmed

- **24 h soak (D2-AC-39)**: S7 relay with 24 viewers ran for 24.5 h with no unexpected restart and no out-of-memory event; sim-core memory
  grew by 0.48 MB/h, and the day rollover fired on time.
- **Time to first frame**: 64% shorter than D1 (round 1 measured 34% longer).
- **Session cost**: the simulation cost of one sandbox session dropped from 0.46 to 0.22 cores; the capacity variants no longer show the
  "reclaim, resume, starve again" cycle, and creation no longer queues.
- **Tasking**: the defects of fixed-wing flight in wind and of concurrent area patrol and perimeter patrol are gone, with 0 collisions and
  0 entries into configured volumes across three seeds; the maximum single step of a fleet-wide return to home dropped from 13.85 ms to 7.99 ms.
- **T5 first capture**: 46.5 to 56.5 s.
- **Resume from parking**: with the clock paused, task and relay state are identical field by field before and after resuming.
- **Micro cache of the capacity endpoint**: at most 1 request per second reaches the back end.

## Failed items and root causes

| Root cause | Items | Observation |
|---|---|---|
| Sandbox capacity of the public site | D2-AC-02 sandbox creation, 04 concurrent capacity, 07 rate governance, 08 sandbox100, 55 resources | With 4 fully loaded sessions the sandbox CPU group stays at its 1.36-core limit, the real-time factor of a session drops to about 0.58, and a new session takes about 8.8 s to become ready under load (threshold 6 s). The public site therefore limits concurrent sandboxes to 2, and further visitors queue |
| Daytime relay gaps | D2-AC-19 relay 24 h, 20 relay 7x24 h, 35 application scenario one end to end | Randomly walking targets briefly leave the line of sight of the drone on station, producing a number of short monitoring gaps in 24 h batches; the battery-swap rhythm of T5 on the synthetic demo city causes `battery` gaps |
| Frame pacing with software rendering | D2-AC-30 visualization performance | Without a hardware GPU, the median frame time of the default view of the synthetic demo city is 50 ms (the Tier S threshold is 33.4 ms) |

The two items not measured are D2-AC-27 (72 h and seven-day observation of the public site) and D2-AC-46 (measured power-saving gains on the
public site); both lack only the public-site part and are measured after deployment, and the local sub-items of the latter all passed.

## Test conditions

The same 8-core virtual machine as D1 (Xeon E5-2603 v4, 1.7 GHz, 62 GiB memory, no GPU), with Chrome for Testing 151 and SwiftShader
software rendering. Items with performance, capacity or latency thresholds run under an exclusive lock, start only when the 1-minute load is
at most 4, and take the median of 3 runs. Public-site resources are emulated with cgroups: `CPUQuota=250%`, `MemoryHigh=1.8G`,
`MemoryMax=2G`, 4 cores.

The closing full run of `pytest -m "not perf"` gave 4,331 passed, 97 skipped and 2 failed; both failures relate to test isolation or the
pinning environment, and they pass when run alone or match round 1.

## Full reports

- [D2 acceptance report (round 2)](https://github.com/ANetResearch/ANet-Drone4D/blob/main/docs/impl/D2-验收报告-第2轮.md)
- [D2 acceptance report (round 1)](https://github.com/ANetResearch/ANet-Drone4D/blob/main/docs/impl/D2-验收报告-第1轮.md)
- [D1 acceptance report (round 6)](https://github.com/ANetResearch/ANet-Drone4D/blob/main/docs/impl/D1-验收报告-第6轮.md)
- [D2 design supplement and acceptance criteria (docs/04)](https://github.com/ANetResearch/ANet-Drone4D/blob/main/docs/04-D2-设计增补与决策记录.md)

The reports are written in Chinese.
