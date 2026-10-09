## Review: 4D World Runtime for Coupled Decisions of Urban Drone Fleets

### 1. Summary
The paper proposes a runtime that unifies city geometry, an analytic time-varying weather field, and a reference energy model, and uses these shared models for coupled launch precheck, return planning, scan planning, and task delegation. Four motivational studies quantify failures of decoupled practice: fixed-altitude returns enter building envelopes, separate wind triggers disagree with the energy model, weather-blind scan plans silently miss targets, and naive per-drone queries exceed the real-time budget. The evaluation over seven cities and twelve weather presets reports large relative improvements in safety, completion, delegation, and scan capture, plus CPU-only real-time operation and browser access, but all results rely on simulated reference models without physical validation or strong planner baselines.

### 2. Main strengths
- The motivational studies F1–F4 are concrete, quantitative, and directly motivate the design; F2’s model-disagreement result is especially useful.
- The DecisionAwareness switchboard is a sound evaluation mechanism: all baselines and ablations run on the same ground truth, reducing code-base confounds.
- The evaluation is large in scale, covering six real cities, a synthetic city, twelve weather conditions, 36,288 return sorties, micro-benchmarks, deterministic replay, and browser streaming experiments.

### 3. Main weaknesses, ordered by severity

**W1. No physical validation of the core models.**  
The safety, energy, wind, and perception results are entirely simulation-internal against “reference” analytic models. There is no comparison with measured urban wind, real flight logs, battery data, or camera imagery.  
*Fix:* Validate the wind profile and gusts against urban meteorological data or high-fidelity CFD; validate the hexacopter energy model against real P600-class flight logs; validate the Johnson/Koschmieder perception chain against rendered or real camera images. Report absolute errors, not only relative policy differences.

**W2. No state-of-the-art planner baselines.**  
The return baseline is a fixed-altitude PX4-like rule, and the delegation baseline is nearest-drone or contract net with clear-air quotes. There is no comparison with any modern wind- or energy-aware path planner, coverage planner, or optimization-based task allocation.  
*Fix:* Add at least one geometry-aware return planner (e.g., A*/RRT with energy/wind cost using the same environment) and one optimization-based delegation/coverage baseline (min-cost flow or Hungarian with full costs), reporting safety, completion, and compute cost.

**W3. The “4D” time-varying aspect is not actually evaluated.**  
The environment field is analytic and time-varying, but all closed-loop campaigns use static presets; no experiment changes weather during a sortie or tests decision adaptation to a weather front, gust onset, or precipitation transition.  
*Fix:* Add scenarios with time-varying keyframes and report whether the precheck, return trigger, and scan planner adapt to changing wind, visibility, gust events, and energy margins while remaining consistent with the display.

**W4. Overstated safety/proof claims from a coarse model.**  
The design derives safety from a 2 m DSM, a 4 m dilation, and a 10 m margin, and claims a path “provably” clears the inflated surface. This does not address thin obstacles, wires, overhangs, bridges, moving objects, or DSM errors.  
*Fix:* Qualify all safety statements as model-relative; add sensitivity to DSM resolution and dilation/margin parameters; include thin obstacles or no-fly zones; report actual clearance distributions rather than only binary collision counts.

**W5. Real-time and browser-access claims are not fully substantiated.**  
The single-core 1000-drone measurement uses “the complete runtime” but the text does not state exactly which stages run simultaneously. The browser claim includes “a phone,” but no phone was tested. The server-load claim is made without a multi-viewer experiment.  
*Fix:* Define exactly which stages are included in the single-core pipeline; add simultaneous scan planning/delegation/streaming workloads; test actual mobile hardware and multiple concurrent viewers; report server CPU/network load as a function of viewer count.

**W6. Statistical rigor is weak.**  
Bootstrap intervals are computed “over sorties,” but sorties are nested within city, preset, and seed, so the effective sample size is smaller than 36,288. No inference is provided for many headline numbers.  
*Fix:* Use cluster bootstrap over cities/seeds, report per-seed distributions, and correct confidence intervals for delegation, coverage, and heatmap results.

### 4. Claims that are not supported by the presented evidence
- “The client therefore holds its frame rate on a laptop, a phone or a software renderer without any GPU on the server.”  
  No phone was tested; only an Apple M3 laptop, software SwiftShader, and an A100-equipped headless Chromium were used.

- “the server load does not depend on the number of viewers beyond serving static byte ranges.”  
  No experiment varies the number of concurrent viewers; this is asserted without data.

- “the weather the operator sees is the weather the drones fly in”  
  Only golden-vector outputs are compared; there is no end-to-end comparison of browser-rendered weather/visibility with the simulated flight state.

- “nothing a decision or a pixel depends on differs”  
  Only 98.7% of environment outputs are bit-identical, and the maximum absolute difference is 3.6e-12; the paper does not show that these non-identical outputs never affect a decision or rendered pixel.

- “No fixed altitude is both safe and cheap”  
  This is demonstrated only for the six studied cities and one clearance envelope; it is not a general proof and may not hold for cities without tall buildings or with different terrain.

### 5. Missing experiments or baselines a reviewer would ask for
- Real-world or hardware-in-the-loop validation of wind, energy, and perception models.
- Direct comparison with actual PX4/Gazebo SITL, or at least a published wind/energy-aware return planner.
- Dynamic weather scenarios where wind, gusts, precipitation, and optical depth change during a sortie.
- Sensitivity to DSM resolution, obstacle model fidelity, energy parameters, ground effect, payload, and gust/turbulence seeds.
- Fleet sizes beyond 1,000 drones and mixed workloads (scan + delegation + return) running concurrently on one core.
- Multi-client browser evaluation: actual phone, multiple simultaneous viewers, constrained latency/bandwidth, packet loss, and long sessions.
- Failure-mode experiments with sensor noise, GPS dropout, no-fly zones, and imperfect battery state.

### 6. Unclear writing, inconsistent numbers, or terminology
- **Fog scan capture inconsistency:** F3 states the recognition plan captures “4% of targets in fog,” while the evaluation text says the weather-blind plan captures “5% at recognition and identification level.” The abstract also says “from 5% to 89%.” These should be reconciled.
- **Unclear unsafe/lost definitions in the overall campaign:** “unsafe” includes both lost and launched beyond wind rating, but the heatmap is labeled “sorties lost or launched beyond wind rating,” making direct comparison with F1 and outcome shares confusing.
- **Delegation percentages are ambiguous:** “34.1% of its awards break the launch reserve (16.2% would strand), 35.8% send a drone beyond the wind rating” overlaps and does not state whether the denominator is awards or all requests. Use mutually exclusive categories.
- **PX4 climb description:** “climbs a fixed 30 m above home (plus the 10 m difference to its loiter altitude)” is confusing; state exactly what altitude is used for the baseline and whether it depends on current altitude.
- **Consistency tolerance:** Reporting “98.7% bit-identical” while also claiming “nothing a decision or pixel depends on differs” is contradictory; the acceptable tolerance for decisions and pixels should be defined.
- **P600-class reference model:** the 222 Wh battery, 85% usable, and 13.8 m/s wind rating are not cited or justified.

### 7. Score: 2/5 (weak reject)
The architecture and motivational study are well executed, and the DecisionAwareness approach is a good way to isolate coupling effects. However, the paper’s central claims about safety, real-time fleet operation, and device access are based solely on unvalidated analytic models and incomplete or overstated experiments. The absence of any state-of-the-art planner baseline, the lack of time-varying weather evaluation, and the weak statistical reporting mean the current evidence does not yet support the strong claims made. A major revision with physical validation, stronger baselines, dynamic scenarios, and complete reporting is necessary before this work is publishable in its current form.