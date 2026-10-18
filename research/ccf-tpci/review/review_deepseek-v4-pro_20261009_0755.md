**Review**

**(1) Summary**  
The paper presents a 4D world runtime that fuses city geometry, a time‑varying weather field, and vehicle energy to drive coupled decisions for urban drone fleets. Through motivational studies and closed‑loop simulations across seven cities and twelve weather conditions, the authors argue that decoupled tools (PX4‑style return, weather‑blind scan, etc.) lead to frequent unsafe sorties and poor task performance, while their coupled policy can virtually eliminate building collisions and more than double completed sorties. The system design uses batched queries on a shared clock to make the coupling affordable, and the evaluation reports substantial gains in safety, scan coverage, and delegation success.

**(2) Main strengths**  
- The architecture of shared models with batched, vectorised queries and a single simulation clock is well motivated and addresses genuine integration challenges.  
- The decision layer covers a comprehensive set of operations (launch precheck, return planning, perception‑driven scan, capability‑based delegation) with a clear coupling matrix.  
- The evaluation employs a diverse set of real cities and weather presets and reports relative differences between policies, giving a rich picture of the benefits.

**(3) Main weaknesses, ordered by severity with concrete fixes**

1. **Inconsistent / inaccurate PX4 baseline in the motivation.**  
   The motivational study (F1) states that with the “PX4 default (climb to 30 m above home)”, 44.6 % of return lines enter the clearance envelope for a drone loitering at 40 m. However, in the evaluation the PX4 default is described as “returns at the higher of its current altitude and 30 m above home”. The motivational test uses a different rule (forced descent to 30 m) that does not match the actual PX4 behavior, thereby exaggerating the danger of the baseline. This inconsistency undermines the credibility of the motivational claims.  
   *Fix:* Re‑run the motivational study with the correct PX4 altitude rule, or clearly rename that case to “naive fixed‑altitude return” and ensure the closed‑loop baselines are aligned.

2. **Unsubstantiated and potentially unrealistic energy‑wind model.**  
   The paper asserts that in the reference model the power drawn changes by only ±1 % below 10 m/s and that a wind‑blind estimate stays within 2.5 %. Such behavior is contrary to established multicopter power models, where headwind significantly increases energy consumption. No validation, citation, or comparison with measured data is provided, making the model’s fidelity questionable. If the model underestimates wind effects, the claimed benefits of wind‑aware decisions are not reliable.  
   *Fix:* Adopt a validated power model (with references) or present a thorough justification, including a sensitivity analysis with a more realistic model.

3. **Missing geometry‑aware but non‑coupled return baseline.**  
   The overall evaluation compares the fully coupled policy mostly against PX4 default and a decoupled stack that is geometry‑blind. A natural incremental baseline is a “climb‑to‑clear” policy that uses the height map to compute a collision‑free return altitude but ignores wind, shared energy, and detours. Such a baseline appears only in a distribution plot (Fig. overall‑outcomes b) without its full completion/safety metrics in the main campaign.  
   *Fix:* Include a “geometry‑only” return baseline in the overall heatmap and outcome analysis, reporting unsafe fraction, completed fraction, and climb energy.

4. **Scan planning lacks ablation and in‑flight validation.**  
   The coupled scan planner is compared only to a weather‑blind plan; no ablation isolates the roles of visibility, line‑of‑sight, and wind‑adjusted endurance. Moreover, the paper does not report whether the planned sorties actually complete within the estimated endurance when flown in the simulation with wind and battery drain.  
   *Fix:* Add ablation experiments for the scan planner factors, and verify that the flown missions achieve the planned capture without energy starvation.

5. **Scalability claims supported only by micro‑benchmarks; no closed‑loop large‑fleet test.**  
   The paper states that “1,000 drones run at 5.3 × real time on one core”, but all closed‑loop decision campaigns use 8–12 drones. The impact of batching and rotation on decision safety and correctness at scale is not demonstrated.  
   *Fix:* Run a closed‑loop experiment with at least 100 simultaneously flying drones performing coupled return, scan, and delegation to validate that the runtime’s scheduling does not degrade decision outcomes.

6. **“One consistent model” not fully proven for geometry and visual display.**  
   The environment‑field consistency between server and browser is tested, but there is no evidence that the browser‑rendered point‑cloud view corresponds to the height map used for collision checking, or that the visual weather effects quantitatively match the optical depth driving perception. The claim “the weather the operator sees is the weather the drones fly in” is therefore over‑stated.  
   *Fix:* Add quantitative comparisons (e.g., rendered building silhouettes vs. the clearance envelope, or visibility range from the visual rendering vs. optical depth metric) or qualify the claim.

**(4) Claims not supported by the presented evidence**  
- *“With the PX4 default (climb to 30 m above home), 44.6 % of return lines … enter the envelope.”* This uses an incorrect return‑altitude rule; the evaluation later employs a different rule, so the motivational result is not representative of the baseline used in experiments.  
- *“The energy drawn per second barely changes with wind in our reference model: below 10 m/s the change is within ±1%.”* No source or experimental validation is given.  
- *“The weather the operator sees is the weather the drones fly in.”* Only environment‑model bit‑identity is verified, not the perceptual equivalence of the visual rendering to the actual optical quantities.

**(5) Missing experiments or baselines a reviewer would ask for**  
- A closed‑loop campaign with a fleet of at least 100 drones to stress‑test the coupled decision layer.  
- A “geometry‑only” return baseline (climb‑to‑clear but wind‑blind, with standard battery triggers) compared in the main overall metrics.  
- Ablation of the scan planner coupling components (visibility, line‑of‑sight, wind‑based endurance) and verification that planned sorties complete with the estimated energy.  
- Sensitivity of results to errors in the energy and wind models, possibly using a validated alternative model.

**(6) Unclear writing, inconsistent numbers or terminology**  
- The behaviour of the PX4 baseline differs between the motivation (forced descent to 30 m) and the evaluation (higher‑of‑current‑altitude). This needs to be reconciled and clearly stated.  
- The “climb to clear” policy appears under the label “no detour” (ND) in the numbers and text, but its relationship to the coupled policy and ablation is confusing.  
- In the overall heatmap, the meaning of the “completed” cells in severe weather (0 % everywhere) is not explained; a brief note would improve readability.  
- The ablation bar names (“NO GEOMETRY”, etc.) are not explicitly linked to the `DecisionAwareness` flags described in Section 4.4.1; a mapping would aid reproducibility.

**(7) Score: 2 (Reject – major revisions needed)**  
The paper tackles an important integration problem and contains substantial system engineering, but its evaluation is critically weakened by an inaccurate representation of the baseline PX4 behaviour and by an energy model that is presented without justification. These flaws, combined with missing baselines (geometry‑only return, large‑fleet closed‑loop tests, scan ablation) and oversold claims about visual consistency, mean that the reported safety and productivity gains are not convincingly validated. A major revision that corrects the baseline inconsistency, adopts or validates a credible energy model, and strengthens the evaluation with the suggested baselines and scale experiments would be necessary before the work can be considered for acceptance.