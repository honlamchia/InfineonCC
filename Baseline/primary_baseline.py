"""
primary_baseline.py
===================
Primary-planned-lane baseline for the INTERNAL side (guide "beat baseline" rule).

Baseline = each internal shipment executed on its OWN primary planned lane
(Route_Options row with Notes == "Primary planned lane" matching
MaterialFamily + StageFrom + StageTo + FromHub==ShipFrom_Alias +
ToHub==ShipTo_Alias, Normal scenario), pushed through EXACTLY the same
machinery as the official optimiser:

  * same capability rules (cold-chain + hazard, both hubs),
  * same capacity model (route + hub weekly capacity, WeeksRequired horizon),
  * same CANONICAL scaler (fitted Normal+PHD+ACR) -> scores share one ruler,
  * same PuLP solve, but each shipment's ONLY candidate is its primary lane
    (take it or be unassigned) -> "best possible execution of the plan".

Outputs baseline distribution + Q1/Q3 thresholds for the submission benchmark.
Run from pulp_optimizer/Baseline/.
"""
import json
import pandas as pd

import build_candidates as bc
import baseline_v7 as b7
import optimize as opt

HORIZON = opt.PLANNING_HORIZON_WEEKS


def primary_candidates(sheets, cfg, horizon):
    """Normal-scenario candidates restricted to each shipment's own primary lane."""
    ann = cfg["add_cost"](b7.capacity_fields(
        cfg["build"]("Normal"), cfg["qty_map"], cfg["key"], horizon))
    ann["week"] = ann[cfg["key"]].map(cfg["wk_map"])
    prim = ann[(ann["IsPrimary"] == "Yes")
               & (ann["FromHub"] == ann["ShipFrom_Alias"])
               & (ann["ToHub"] == ann["ShipTo_Alias"])].copy()
    return ann, prim


def main():
    sheets = bc.load_sheets(hub_disruption=None)
    E = pd.read_excel(bc.WORKBOOK, sheet_name="External Shipments")
    cfg = b7._side_setup("internal", sheets, E)
    scaler = b7.canonical_scaler("internal", sheets, E, HORIZON)

    ann, prim = primary_candidates(sheets, cfg, HORIZON)

    n_ship = len(cfg["universe"])
    per = prim.groupby("ShipmentID").size()
    assert per.max() == 1, f"shipment with >1 primary lane: {per[per > 1]}"
    have_primary = set(per.index)
    no_primary = [s for s in cfg["universe"] if s not in have_primary]

    # capability check already applied inside cfg["build"] (apply_capability).
    prim_feas = b7.model_feasible(prim)
    prim_feas = b7.score(prim_feas, scaler)

    hub_remaining = sheets["hub"].set_index("HubID")["remaining_capacity_units"].to_dict()
    res = b7.solve(prim_feas, "ShipmentID", cfg["universe"], hub_remaining)
    # res["unassigned"] already contains shipments with no capability-passing
    # primary lane (they simply have no candidate row) - classify covers them.
    reasons = b7.classify(res["unassigned"], prim, prim_feas, "ShipmentID")
    assert set(no_primary) <= set(res["unassigned"])

    ch = res["chosen"]
    q1 = float(ch["MinScore"].quantile(0.25))
    q3 = float(ch["MinScore"].quantile(0.75))

    # Unconstrained "plan as scored" view (every capability-passing primary lane
    # scored, capacity ignored) - reported for transparency only.
    plan_scored = b7.score(prim, scaler)

    out = {
        "universe": n_ship,
        "baseline_solved": len(ch),
        "baseline_unassigned": len(res["unassigned"]),
        "no_capability_passing_primary_lane": len(no_primary),
        "unassigned_reasons": {r: sum(1 for v in reasons.values() if v == r)
                               for r in b7.ACTIONS},
        "baseline_avg": round(float(ch["MinScore"].mean()), 4),
        "baseline_median": round(float(ch["MinScore"].median()), 4),
        "baseline_std": round(float(ch["MinScore"].std()), 4),
        "baseline_min": round(float(ch["MinScore"].min()), 4),
        "baseline_max": round(float(ch["MinScore"].max()), 4),
        "baseline_q1": round(q1, 4),
        "baseline_q3": round(q3, 4),
        "plan_scored_lanes": len(plan_scored),
        "plan_scored_avg_capacity_ignored": round(float(plan_scored["MinScore"].mean()), 4),
        "scaler": {k: list(v) for k, v in scaler.items()},
    }
    print(json.dumps(out, indent=2))

    cols = ["ShipmentID", "MaterialFamily", "RouteOptionID", "FromHub", "ToHub",
            "TransportMode", "DemandQty", "week", "WeeklyFootprint",
            "BottleneckCapacityPerWeek", "WeeksRequired",
            "BaseLeadTimeDays", "EffectiveLeadTimeDays", "BaseCostEUR",
            "RiskScore", "CO2Kg", "n_lead", "n_cost", "n_risk", "MinScore"]
    ch[cols].to_csv("primary_baseline_selected.csv", index=False)
    pd.DataFrame([{"ShipmentID": s, "Reason": r} for s, r in reasons.items()]) \
        .to_csv("primary_baseline_unassigned.csv", index=False)
    with open("primary_baseline_summary.json", "w") as f:
        json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
