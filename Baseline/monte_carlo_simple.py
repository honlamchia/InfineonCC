"""
monte_carlo_simple.py
=====================
SIMPLIFIED Monte Carlo stress test of the official v7 internal baseline.
Presentation version: THREE varied inputs, THREE outputs.

Why these three inputs (and only these)
  MinScore = 0.40*lead + 0.40*cost + 0.20*risk, subject to hub/route CAPACITY.
  We shock exactly the levers the optimiser trades off:
    1. LEAD  : BaseLeadTimeDays x LogNormal(0, 0.15) per route
               (transit delays - port congestion, customs; the guide's 1-12 day
                lead times are estimates, not guarantees)
    2. COST  : BaseCostEUR x LogNormal(0, 0.10) per route
               (freight-rate volatility - fuel, spot-market swings)
    3. ROUTE AVAILABILITY : each route option knocked out with prob 0.03 -
               this is the DATASET'S OWN disruption mechanism: Route_Options
               models every disruption (PrimaryHubDown, AirCapacityReduced)
               by flipping AvailableFlag to "No". We simply randomise WHICH
               routes flip instead of using the two fixed patterns.
  RiskScore is deliberately NOT varied: it is the dataset's estimate of how
  likely disruption is; the Monte Carlo is that risk REALISED. Varying it
  would double-count. Hub capacity cuts are NOT varied either: they are
  already covered deterministically by guide Scenario 1 (Port congestion),
  and our runs show the network absorbs them - the simulator should not
  duplicate a scenario that already exists.

Tie to the MinScore optimisation
  Each trial re-runs the SAME PuLP optimiser with the SAME 40/40/20 objective
  and the SAME fixed canonical scaler - only the inputs move. The clean plan
  is also held frozen in each shocked world, so the gap between "frozen" and
  "re-optimised" isolates the value of re-planning.

Three outputs
  1. Coverage under stress   : frozen-plan shipments served (median / P5) vs clean 170
  2. Value of re-optimising  : mean shipments recovered per trial by re-solving
  3. Watchlist               : top-10 most fragile shipments (fail rate + route)

Run:  IFX_WORKBOOK=... python monte_carlo_simple.py --trials 300 --seed 42
"""
from pathlib import Path
import argparse, time
import numpy as np
import pandas as pd

import build_candidates as bc
import baseline_v7 as b7
import monte_carlo_stress as mcs   # reuse the verified evaluation machinery

KEY = "ShipmentID"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--horizon", type=int, default=b7.HORIZON)
    ap.add_argument("--sigma-lead", type=float, default=0.15)
    ap.add_argument("--sigma-cost", type=float, default=0.10)
    ap.add_argument("--p-route", type=float, default=0.03)
    ap.add_argument("--out", default="monte_carlo_simple.xlsx")
    a = ap.parse_args()

    t0 = time.time()
    S = mcs.fixed_setup(a.horizon)
    rng = np.random.default_rng(a.seed)

    neutral_mode = {m: 1.0 for m in S["modes"]}
    neutral_hub = {h: 1.0 for h in S["hubs"]}
    no_delay = {r: 0.0 for r in S["routes"]}

    trials, fail_count = [], {}
    for t in range(a.trials):
        lead_f = {r: rng.lognormal(0.0, a.sigma_lead) for r in S["routes"]}
        cost_f = {r: rng.lognormal(0.0, a.sigma_cost) for r in S["routes"]}
        route_out = {r for r in S["routes"] if rng.random() < a.p_route}
        shocks = (neutral_mode, cost_f, lead_f, no_delay, route_out, neutral_hub)

        ann, hub_rem_t = mcs.shocked_world(S, shocks, a.horizon)
        live, fail = mcs.eval_frozen(S, ann, hub_rem_t)          # frozen plan
        feas = ann[(ann["BottleneckCapacityPerWeek"] > 0) & (~ann["Escalation"])
                   & (~ann["RouteOut"])]
        res = b7.solve(feas.copy(), KEY, S["cfg"]["universe"], hub_rem_t)  # re-optimise

        frozen, reopt = len(live), len(res["chosen"])
        trials.append({"trial": t, "frozen_served": frozen, "reopt_served": reopt,
                       "recovered": reopt - frozen,
                       "frozen_avg_MinScore": float(live["MinScore"].astype(float).mean()),
                       "reopt_avg_MinScore": float(res["chosen"]["MinScore"].mean()),
                       "n_routes_out": len(route_out)})
        for sid in fail:
            fail_count[sid] = fail_count.get(sid, 0) + 1
        if (t + 1) % 50 == 0:
            print(f"  trial {t+1}/{a.trials} ({time.time()-t0:.0f}s)")

    T = pd.DataFrame(trials)
    p = lambda s, q: float(np.percentile(s, q))

    headline = pd.DataFrame([
        ("OUTPUT 1 - Coverage under stress", ""),
        ("Clean plan (no shocks)", f"{S['clean_served']}/240 served, avg MinScore {S['clean_avg']:.4f}"),
        ("Frozen plan, median future", f"{p(T['frozen_served'],50):.0f}/240 served"),
        ("Frozen plan, bad future (P5)", f"{p(T['frozen_served'],5):.0f}/240 served"),
        ("OUTPUT 2 - Value of re-optimising", ""),
        ("Shipments recovered per trial (mean / max)",
         f"{T['recovered'].mean():.1f} / {T['recovered'].max():.0f}"),
        ("Re-optimised plan never falls below", f"{T['reopt_served'].min():.0f}/240 served"),
        ("OUTPUT 3 - Watchlist", "see FragileShipments sheet"),
        ("", ""),
        ("Trials / seed", f"{a.trials} / {a.seed} (reproducible)"),
    ], columns=["Metric", "Value"])

    plan_routes = S["plan"].set_index(KEY)["RouteOptionID"].to_dict()
    frag = (pd.DataFrame([{KEY: s, "BaselineRoute": plan_routes.get(s),
                           "fail_rate": n / a.trials} for s, n in fail_count.items()])
            .sort_values("fail_rate", ascending=False).head(10))

    assumptions = pd.DataFrame([
        ("Varied 1: lead time", f"BaseLeadTimeDays x LogNormal(0,{a.sigma_lead}) per route - transit "
         "delays; lead carries 40% of MinScore."),
        ("Varied 2: cost", f"BaseCostEUR x LogNormal(0,{a.sigma_cost}) per route - freight-rate "
         "volatility; cost carries 40% of MinScore."),
        ("Varied 3: route availability", f"Each route option knocked out with prob {a.p_route}. This is "
         "the dataset's own disruption mechanism - Route_Options models PrimaryHubDown and "
         "AirCapacityReduced by flipping AvailableFlag to 'No'; we randomise WHICH routes flip."),
        ("NOT varied: RiskScore", "RiskScore is the dataset's estimate of disruption likelihood; the "
         "simulation is that risk realised - varying it would double-count."),
        ("NOT varied: hub capacity", "Hub capacity cuts are already covered deterministically by guide "
         "Scenario 1 (Port congestion); our runs show the network absorbs them."),
        ("Tie to optimiser", "Every trial re-runs the same PuLP model, same 40/40/20 objective, same "
         "fixed canonical scaler; only inputs move. Frozen vs re-optimised isolates the value of "
         "re-planning."),
        ("Evaluation", "Frozen plan checked for zero-capacity, >12-week escalation, and joint "
         "route-week/hub-week overloads (worst-score shipments shed first); re-solve is a full CBC run."),
    ], columns=["Assumption", "Detail"])

    with pd.ExcelWriter(a.out) as xw:
        headline.to_excel(xw, sheet_name="Summary", index=False)
        frag.to_excel(xw, sheet_name="FragileShipments", index=False)
        T.round(4).to_excel(xw, sheet_name="TrialResults", index=False)
        assumptions.to_excel(xw, sheet_name="Assumptions", index=False)
    T.to_csv(Path(a.out).with_suffix(".csv"), index=False)
    print(f"\nwrote {a.out} in {time.time()-t0:.0f}s")
    print(headline.to_string(index=False))


if __name__ == "__main__":
    main()
