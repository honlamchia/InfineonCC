# ChainLab Route Optimisers

PuLP pipeline for the Infineon route-optimisation challenge.

## OFFICIAL BASELINE (v7 - per Infineon AI Engineer clarification)
Two INDEPENDENT optimisers; internal_shipments and external_shipments are never joined.
```bash
python baseline_v7.py    # -> optimizer_internal_baseline.xlsx  (all 240 internal shipments,
                         #    cost term = raw BaseCostEUR - NOT cost/kg, internal legs have no kg)
                         # -> optimizer_external_baseline.xlsx  (all 225 deliveries,
                         #    cost/kg = BaseCostEUR / individual ChargeableWeight_KG;
                         #    demand = Pieces, week = PUP_Date - stated assumptions)
```
Each workbook: Summary / SelectedRoutes / Unassigned / ScalerBounds / Assumptions.
One fixed scaler per optimiser (union of all scenarios); internal and external scores are NOT
comparable to each other. Verified: LowestRouteCostEUR reconstructed 240/240 and
LowestCostPerKG_EUR 225/225 from available routes across all scenarios (HackathonObjectiveScore
ships empty - there is no organiser score to reconstruct). 19/19 acceptance tests pass.

## COMBINED EXTENSION (optional integrated upstream-downstream coordination model)
The delivery-grain integrated model below is retained UNCHANGED as an extension -
`optimizer_combined_extension.xlsx` (copy of optimizer_official_costperkg.xlsx).
Do not present it as the official baseline.

## Run (extension + legacy)
```bash
pip install pandas openpyxl pulp
cd pulp_optimizer
python build_candidates.py                       # sanity: 1,137 Normal candidates -> 42/16/182
python optimize.py --objective both              # -> optimizer_official_costperkg.xlsx (integrated extension)
                                                 #    + optimizer_proxy_resilience.xlsx (all-240 resilience)
python external_impact.py --scenario Normal      # customer-impact layer -> external_impact.xlsx
```
Workbook is auto-located one level up. Override with `IFX_WORKBOOK=/path/to.xlsx`.

## Files
- `build_candidates.py` - joins the 5 sheets into the feasible shipment-route candidate table.
- `optimize.py` - binary PuLP model, WeightedScore 40/40/20, weekly capacity, unassigned slack, baseline stats.
- `external_impact.py` - propagates the internal decision to customer deliveries; cost/kg here.

## Key modelling decisions (stated assumptions)
1. Two scenario dimensions are independent: route scenario (Normal/PrimaryHubDown/AirCapacityReduced)
   filters `route_options`; hub disruption is baked per-hub in `hub_constraints`. The two
   `DisruptionScenario` columns are NEVER matched to each other.
2. Handling capability via the boolean hub columns; BOTH origin+dest must qualify.
3. `*_pct` fields are decimal fractions (renamed `*_ratio`); remaining capacity =
   weekly * (max_util * (1 - reduction)) - current_load.
4. Capacity = weekly throughput, throughput-adjusted (no bypass):
   bottleneck = min(route_cap, origin_remaining, dest_remaining).
   bottleneck <= 0 -> candidate rejected (no zero-capacity hub can be selected).
   weeks_required = ceil(qty / bottleneck); effective_lead = base + 7*(weeks-1).
   One physical WeeklyFootprint = min(Qty, bottleneck) is charged IDENTICALLY to the route
   and both hub constraints (same flow through all three resources). Hub inbound+outbound
   are combined into ONE constraint per hub-week.
   Planning horizon = THROUGHPUT weeks to clear capacity: escalate when WeeksRequired > 12
   (transit time NOT counted; default 12, CLI --horizon-weeks). Routes beyond it are
   "capacity escalation" and never shown as a normal solution. (Total EffectiveLeadTimeDays
   may exceed 84 for a 12-week route once base transit is added - that is expected.)
   Both workbooks export an Unassigned sheet (one row per dropped shipment: Reason /
   LinkedDeliveries / BestAvailableWeeks / BestAvailableRoute / RecommendedAction). The external
   layer inherits the exact per-shipment reason, so escalation deliveries are labelled
   CAPACITY ESCALATION REQUIRED, not BLOCKED.
   (Approximation: a multi-week footprint is charged in the ship week; a full weekly-flow
   model f[s,r,w] is the documented next step.)
5. Objective (40/40/20) with cost IN the score BEFORE the solve. Two modes:
     official (cost/kg) -> DELIVERY grain. cost/kg = BaseCostEUR / per-delivery
       ChargeableWeight_KG (matches benchmark LowestCostPerKG_EUR 225/225). Scores the 225
       deliveries; one shared route decision per 132 internal shipments; capacity charged
       once per shipment. -> optimizer_official_costperkg.xlsx with sheets: Summary (live
       AVERAGE/MEDIAN/STDEV/MIN/MAX/QUARTILE formulas + beats-baseline / Q1 / Q3 statements),
       Assumptions (stated assumptions + yellow normalisation-bound cells the formulas
       reference), DeliveryScores + BaselineScores (per-delivery live formulas:
       cost/kg, n_lead/n_cost/n_risk, MinScore), RouteDecisions, Unassigned.
       This is the graded MinScore - formulas visible, per the submission format.
     proxy (cost/piece) -> BaseCostEUR / Qty; Internal Routing Proxy Score for all 240;
       drives the resilience / scenario-swap story -> optimizer_proxy_resilience.xlsx
   external_impact.py uses the OFFICIAL delivery-grain routes and per-delivery cost/kg,
   so customer cost/kg and routes match the graded optimiser exactly (verified 0 mismatch).
   Call the metric a "normalised 40/40/20 MinScore" - min-max normalisation is a stated
   modelling assumption, not in the brief; the exact normalisation population may differ
   from the reference answer.
   LEAD term uses BaseLeadTimeDays (official definition). EffectiveLeadTimeDays + capacity are
   CONSTRAINTS / reporting only, never substituted into the score. ONE fixed min-max scaler per
   objective, shared across scenarios. CLI: --objective official|proxy|both.
6. Infeasible shipments are captured by an unassigned variable, never an infeasible model.
7. Baseline = primary planned lanes (IsPrimary=Yes) run through the SAME capacity-constrained
   solver, at the same grain as each objective (delivery grain for official; shipment grain for
   proxy), same fixed normalisation. Q1 (excellent) / Q3 (weak) thresholds are quartiles of the
   baseline delivery-score distribution, per the Hackathon_Guide. Reported coverage-aware:
   solved counts, same-population average, penalised objective - never a raw average over
   different populations. No primary lanes exist under the disruption scenarios; the baseline
   is reported as "no primary routes marked available", not as a zero score.
8. `optimizer_results.xlsx` is superseded and archived in `archive/` - the two authoritative
   outputs are optimizer_official_costperkg.xlsx (graded) and optimizer_proxy_resilience.xlsx.
