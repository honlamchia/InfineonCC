"""
scenario_resilience.py
======================
Guide Scenario 5 - Network resilience: compare baseline vs alternate hub usage
under disruption.

This is a COMPARISON layer over the runs the other scripts already produce - it
does not introduce a new objective.  For each side it solves four columns with
the SAME canonical 40/40/20 scaler (baseline_v7.canonical_scaler), so score
deltas reflect the disruption, not a different ruler:

  Normal              route Normal,             hub_disruption None   (baseline)
  PrimaryHubDown      route PrimaryHubDown,      hub_disruption None
  AirCapacityReduced  route AirCapacityReduced,  hub_disruption None
  PortCongestion      route Normal,              hub_disruption 'Port congestion'

Route-side (PHD/ACR) and hub-side (PortCongestion) disruptions therefore sit in
one table.  Reported per side:

  Summary            - per column: coverage, avg/median/std MinScore, avg
                       EffectiveLeadTimeDays, total BaseCostEUR, total CO2Kg,
                       IsPrimary share.
  SamePopulation     - vs Normal, restricted to shipments assigned in BOTH
                       columns (the ONLY fair score comparison), plus coverage.
  RouteSwitch        - per shipment: how its decision changed vs Normal
                       (same / rerouted / different hub / mode change / newly
                       un/assigned), with a category-count summary.
  HubUsage           - hub x column pivot of chosen WeeklyFootprint (self-loops
                       charged once) - the "alternate hub usage" evidence.
  Assumptions

Primary-lane note: under PHD/ACR the Route_Options pool contains no IsPrimary=Yes
lanes, so IsPrimary share is 0 because the primaries are UNAVAILABLE, not because
the optimiser rejected them.
"""
from pathlib import Path
import argparse
import pandas as pd

import build_candidates as bc
import baseline_v7 as b7

# (column label, hub_disruption, route_scenario)
COLUMNS = [
    ("Normal",             None,              "Normal"),
    ("PrimaryHubDown",     None,              "PrimaryHubDown"),
    ("AirCapacityReduced", None,              "AirCapacityReduced"),
    ("PortCongestion",     "Port congestion", "Normal"),
]
KEYS = {"internal": "ShipmentID", "external": "DeliveryNo"}


def solve_column(side, E, hub_disruption, route_scenario, scaler, horizon):
    """Solve one resilience column and return the chosen routes + universe."""
    sheets = bc.load_sheets(hub_disruption=hub_disruption)
    cfg = b7._side_setup(side, sheets, E)
    ann = cfg["add_cost"](b7.capacity_fields(cfg["build"](route_scenario),
                                             cfg["qty_map"], cfg["key"], horizon))
    ann["week"] = ann[cfg["key"]].map(cfg["wk_map"])
    feas = b7.score(b7.model_feasible(ann), scaler)
    hub_rem = sheets["hub"].set_index("HubID")["remaining_capacity_units"].to_dict()
    res = b7.solve(feas, cfg["key"], cfg["universe"], hub_rem)
    return res["chosen"].copy(), cfg["universe"]


def _switch_category(n, c):
    """How a shipment's decision changed: Normal row n vs column row c
    (either may be None = unassigned in that column)."""
    if n is None and c is None:
        return "unassigned_both"
    if n is not None and c is None:
        return "newly_unassigned"
    if n is None and c is not None:
        return "newly_assigned"
    if n["RouteOptionID"] == c["RouteOptionID"]:
        return "same_route"
    same_from = n["FromHub"] == c["FromHub"]
    same_to = n["ToHub"] == c["ToHub"]
    if same_from and same_to:
        return "diff_route_same_hubs"
    if not same_from and same_to:
        return "diff_origin_hub"
    if same_from and not same_to:
        return "diff_dest_hub"
    return "diff_both_hubs"


def run_side(side, E, horizon):
    key = KEYS[side]
    clean = bc.load_sheets(hub_disruption=None)
    scaler = b7.canonical_scaler(side, clean, E, horizon)

    chosen, universe = {}, None
    for label, hubdis, route in COLUMNS:
        ch, uni = solve_column(side, E, hubdis, route, scaler, horizon)
        chosen[label] = ch.set_index(key)
        universe = uni

    labels = [c[0] for c in COLUMNS]
    norm = chosen["Normal"]

    # ---- per-column summary -------------------------------------------------
    summary = []
    for label in labels:
        ch = chosen[label]
        prim = (ch["IsPrimary"] == "Yes").sum() if len(ch) else 0
        summary.append({
            "side": side, "column": label, "universe": len(universe),
            "solved": len(ch), "coverage_pct": round(100 * len(ch) / len(universe), 1),
            "avg_MinScore": round(ch["MinScore"].mean(), 4) if len(ch) else None,
            "median_MinScore": round(ch["MinScore"].median(), 4) if len(ch) else None,
            "std_MinScore": round(ch["MinScore"].std(), 4) if len(ch) else None,
            "avg_EffLeadDays": round(ch["EffectiveLeadTimeDays"].mean(), 2) if len(ch) else None,
            "total_BaseCostEUR": round(ch["BaseCostEUR"].sum(), 1) if len(ch) else None,
            "total_CO2Kg": round(ch["CO2Kg"].sum(), 1) if len(ch) else None,
            "primary_lanes_used": int(prim),
            "primary_share_pct": round(100 * prim / len(ch), 1) if len(ch) else None,
        })

    # ---- same-population comparison vs Normal (fair score delta) ------------
    same_pop = []
    for label in labels[1:]:
        ch = chosen[label]
        common = norm.index.intersection(ch.index)
        same_pop.append({
            "side": side, "column": label,
            "common_with_Normal": len(common),
            "Normal_avg_on_common": round(norm.loc[common, "MinScore"].mean(), 4) if len(common) else None,
            "column_avg_on_common": round(ch.loc[common, "MinScore"].mean(), 4) if len(common) else None,
            "avg_score_delta": round((ch.loc[common, "MinScore"].mean()
                                      - norm.loc[common, "MinScore"].mean()), 4) if len(common) else None,
            "avg_effLead_delta": round((ch.loc[common, "EffectiveLeadTimeDays"].mean()
                                        - norm.loc[common, "EffectiveLeadTimeDays"].mean()), 2) if len(common) else None,
            "Normal_solved": len(norm), "column_solved": len(ch),
        })

    # ---- route-switch matrix vs Normal -------------------------------------
    rows, cat_counts = [], []
    for s in universe:
        n = norm.loc[s] if s in norm.index else None
        row = {key: s}
        for label in labels[1:]:
            ch = chosen[label]
            c = ch.loc[s] if s in ch.index else None
            cat = _switch_category(n, c)
            row[f"{label}_vs_Normal"] = cat
            row[f"{label}_mode_change"] = (n is not None and c is not None
                                           and n["TransportMode"] != c["TransportMode"])
        rows.append(row)
    switch = pd.DataFrame(rows)
    for label in labels[1:]:
        vc = switch[f"{label}_vs_Normal"].value_counts()
        cat_counts.append({"side": side, "column": label, **vc.to_dict(),
                           "mode_changes": int(switch[f"{label}_mode_change"].sum())})

    # ---- hub-usage pivot (self-loops charged once) -------------------------
    usage = {}
    for label in labels:
        ch = chosen[label]
        for _, r in ch.iterrows():
            for hub in {r["FromHub"], r["ToHub"]}:
                usage.setdefault(hub, {}).setdefault(label, 0.0)
                usage[hub][label] += r["WeeklyFootprint"]
    hub_rows = []
    for hub, d in usage.items():
        rec = {"HubID": hub}
        for label in labels:
            rec[f"{label}_footprint"] = round(d.get(label, 0.0), 1)
        rec["delta_PortCongestion_vs_Normal"] = round(
            d.get("PortCongestion", 0.0) - d.get("Normal", 0.0), 1)
        hub_rows.append(rec)
    hub_usage = pd.DataFrame(hub_rows).sort_values("Normal_footprint", ascending=False)

    return {"side": side, "summary": summary, "same_pop": same_pop,
            "switch": switch, "cat_counts": cat_counts, "hub_usage": hub_usage}


def main(horizon=b7.HORIZON, out_path="optimizer_resilience_scenario.xlsx"):
    E = pd.read_excel(bc.WORKBOOK, sheet_name="External Shipments")
    outs = {side: run_side(side, E, horizon) for side in ("internal", "external")}

    for side in ("internal", "external"):
        print(f"\n{side.upper()} resilience:")
        for s in outs[side]["summary"]:
            print(f"  {s['column']:20s} solved {s['solved']}/{s['universe']} "
                  f"({s['coverage_pct']}%)  avg {s['avg_MinScore']}  "
                  f"avgLead {s['avg_EffLeadDays']}d  CO2 {s['total_CO2Kg']}  "
                  f"primary {s['primary_share_pct']}%")

    summary = pd.DataFrame(outs["internal"]["summary"] + outs["external"]["summary"])
    same_pop = pd.DataFrame(outs["internal"]["same_pop"] + outs["external"]["same_pop"])
    cat = pd.DataFrame(outs["internal"]["cat_counts"] + outs["external"]["cat_counts"]).fillna(0)

    assumptions = pd.DataFrame([
        ("Scenario", "Guide S5 Network resilience: compare baseline vs alternate hub usage under "
         "disruption. A comparison layer over existing solves, not a new objective."),
        ("Columns", "Normal (route Normal, no hub cut) = baseline; PrimaryHubDown & AirCapacityReduced "
         "(route-side, no hub cut); PortCongestion (route Normal + hub_disruption='Port congestion'). "
         "Route-side and hub-side disruptions shown side by side."),
        ("Scaler", "ONE canonical 40/40/20 scaler (baseline_v7.canonical_scaler, fitted clean across "
         "Normal+PHD+ACR) applied to every column, so score differences are disruption effects, not "
         "different rulers. Scaler bounds use BaseLeadTime/Cost/Risk (independent of hub capacity), so "
         "the same ruler is valid for the port-congestion column."),
        ("Fair comparison", "SamePopulation sheet compares average MinScore ONLY over shipments "
         "assigned in BOTH Normal and the disruption column - averaging over different assigned sets "
         "(e.g. 170 vs 153) would be misleading. Coverage (solved counts) is reported separately."),
        ("Route switch", "Per shipment vs its Normal decision: same_route / diff_route_same_hubs / "
         "diff_origin_hub / diff_dest_hub / diff_both_hubs / newly_unassigned / newly_assigned / "
         "unassigned_both, plus a mode-change flag."),
        ("Hub usage", "chosen WeeklyFootprint summed per hub per column (FromHub & ToHub as a set, so "
         "self-loops are charged once). delta_PortCongestion_vs_Normal shows where volume reroutes when "
         "the 41 port-congestion hubs lose capacity."),
        ("Primary lanes", "Under PrimaryHubDown / AirCapacityReduced the Route_Options pool has no "
         "IsPrimary=Yes lanes, so primary_share = 0 because primaries are UNAVAILABLE, not rejected."),
        ("Route/hub axes", "Route scenario filters Route_Options; hub disruption cuts Hub_Constraints "
         "capacity. The two DisruptionScenario columns are never matched to each other."),
    ], columns=["Assumption", "Detail"])

    with pd.ExcelWriter(out_path) as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        same_pop.to_excel(xw, sheet_name="SamePopulationVsNormal", index=False)
        cat.to_excel(xw, sheet_name="RouteSwitchSummary", index=False)
        outs["internal"]["switch"].to_excel(xw, sheet_name="RouteSwitch_Internal", index=False)
        outs["external"]["switch"].to_excel(xw, sheet_name="RouteSwitch_External", index=False)
        outs["internal"]["hub_usage"].to_excel(xw, sheet_name="HubUsage_Internal", index=False)
        outs["external"]["hub_usage"].to_excel(xw, sheet_name="HubUsage_External", index=False)
        assumptions.to_excel(xw, sheet_name="Assumptions", index=False)
    print(f"\n  wrote {out_path}")
    return outs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-weeks", type=int, default=b7.HORIZON)
    args = ap.parse_args()
    main(horizon=args.horizon_weeks)
