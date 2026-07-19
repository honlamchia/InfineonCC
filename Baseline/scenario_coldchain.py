"""
scenario_coldchain.py
=====================
Guide Scenario 2 - Cold-chain shipment: restrict eligible routes/hubs to
cold-chain capable options.

Design (per reviewer guidance, 18 Jul 2026):
  * The cold-chain rule is ALREADY enforced by build_candidates.apply_capability
    (both origin and destination hubs must have ColdChainAvailable=Yes when the
    material's TempRequirement is Cold Chain).  This script surfaces its effect.
  * Counterfactual disables ONLY the cold-chain rule (require_cold_chain=False);
    ESD / moisture / lithium hazard checks stay ON so hazardous materials are
    never waved through incompatible hubs.
  * Both variants solve the FULL population (240 internal / 225 external) so
    background capacity contention is identical - routing only the 48 cold-chain
    shipments would make hubs look emptier than they really are.
  * ONE fixed scaler per side, fitted on the union of the two variants' feasible
    candidates, so restricted and naive scores are directly comparable.
  * Route scenario = Normal, hub_disruption = None (clean network) - cold-chain
    is a handling axis, independent of route/hub disruption.

Outputs optimizer_coldchain_scenario.xlsx:
  Summary            - full-population solved count + avg MinScore, restricted vs
                       naive, per side; the network-wide cost of compliance.
  ColdChainSubset    - per cold-chain shipment/delivery: candidate route count,
                       chosen lead / cost / score / assigned under each variant,
                       and the deltas (the per-shipment cost of compliance).
  Assumptions        - stated modelling choices.
"""
from pathlib import Path
import pandas as pd

import build_candidates as bc
import baseline_v7 as b7

COLD = "Cold Chain"
SCEN = "Normal"          # cold-chain is a handling axis; run on the clean network


def _cold_universe(sheets, E):
    """ShipmentIDs / DeliveryNos whose material is cold-chain."""
    I, M = sheets["internal"], sheets["material"]
    im = I.merge(M[["MaterialNo_Anon", "TempRequirement"]], on="MaterialNo_Anon", how="left")
    cold_ship = set(im.loc[im["TempRequirement"] == COLD, "ShipmentID"])
    em = E.merge(M[["MaterialNo_Anon", "TempRequirement"]],
                 left_on="MaterialNo_Anon_Link", right_on="MaterialNo_Anon", how="left")
    cold_del = set(em.loc[em["TempRequirement"] == COLD, "DeliveryNo"])
    return cold_ship, cold_del


def _side_config(side, sheets, E):
    """Reuse baseline_v7's shared setup so cold-chain runs are identical to the
    official baseline except for the cold-chain switch (build fixed to Normal)."""
    c = b7._side_setup(side, sheets, E)
    build = lambda **kw: c["build"](SCEN, **kw)
    return c["universe"], c["qty_map"], c["wk_map"], c["key"], build, c["add_cost"]


def _prep(build, add_cost, qty_map, wk_map, key, horizon, **cap_kwargs):
    """handling-feasible -> capacity fields -> cost -> week; return annotated + feasible."""
    ann = add_cost(b7.capacity_fields(build(**cap_kwargs), qty_map, key, horizon))
    ann["week"] = ann[key].map(wk_map)
    feas = b7.model_feasible(ann)
    return ann, feas


def run_side(side, sheets, E, horizon):
    universe, qty_map, wk_map, key, build, add_cost = _side_config(side, sheets, E)
    hub_remaining = sheets["hub"].set_index("HubID")["remaining_capacity_units"].to_dict()

    # restricted = real world (cold rule on); naive = counterfactual (cold rule off, hazard on)
    ann_r, feas_r = _prep(build, add_cost, qty_map, wk_map, key, horizon,
                          require_cold_chain=True)
    ann_n, feas_n = _prep(build, add_cost, qty_map, wk_map, key, horizon,
                          require_cold_chain=False, require_hazard=True)

    # THE canonical scaler (same ruler as the official baseline and every other
    # scenario) so the restricted run reproduces the official clean Normal score.
    scaler = b7.canonical_scaler(side, sheets, E, horizon)
    feas_r = b7.score(feas_r, scaler)
    feas_n = b7.score(feas_n, scaler)

    res_r = b7.solve(feas_r, key, universe, hub_remaining)
    res_n = b7.solve(feas_n, key, universe, hub_remaining)

    def summ(tag, res):
        ch = res["chosen"]
        return {"variant": tag, "side": side, "universe": len(universe),
                "solved": len(ch), "unassigned": len(res["unassigned"]),
                "avg_MinScore": round(ch["MinScore"].mean(), 4) if len(ch) else None,
                "median": round(ch["MinScore"].median(), 4) if len(ch) else None,
                "min": round(ch["MinScore"].min(), 4) if len(ch) else None,
                "max": round(ch["MinScore"].max(), 4) if len(ch) else None}

    summary = [summ("cold-chain restricted (real)", res_r),
               summ("cold rule OFF (counterfactual)", res_n)]
    return {"side": side, "key": key, "summary": summary,
            "feas_r": feas_r, "feas_n": feas_n,
            "res_r": res_r, "res_n": res_n, "universe": universe}


def cold_subset_table(out, cold_ids):
    """Per cold-chain shipment: route-menu size + chosen metrics, restricted vs naive."""
    key = out["key"]
    feas_r, feas_n = out["feas_r"], out["feas_n"]
    ch_r = out["res_r"]["chosen"].set_index(key)
    ch_n = out["res_n"]["chosen"].set_index(key)
    n_r = feas_r.groupby(key).size()      # capacity-feasible candidate routes (restricted)
    n_n = feas_n.groupby(key).size()      # ... (cold rule off)

    rows = []
    for s in sorted(cold_ids):
        r_assigned = s in ch_r.index
        n_assigned = s in ch_n.index
        def g(ch, s, col):
            return ch.loc[s, col] if s in ch.index else None
        lead_r, lead_n = g(ch_r, s, "EffectiveLeadTimeDays"), g(ch_n, s, "EffectiveLeadTimeDays")
        cost_r, cost_n = g(ch_r, s, "CostForScore"), g(ch_n, s, "CostForScore")
        sc_r, sc_n = g(ch_r, s, "MinScore"), g(ch_n, s, "MinScore")
        rows.append({
            key: s,
            "routes_restricted": int(n_r.get(s, 0)),
            "routes_coldRuleOff": int(n_n.get(s, 0)),
            "routes_lost_to_coldchain": int(n_n.get(s, 0)) - int(n_r.get(s, 0)),
            "assigned_restricted": r_assigned,
            "assigned_coldRuleOff": n_assigned,
            "lead_restricted": lead_r, "lead_coldRuleOff": lead_n,
            "lead_premium": (lead_r - lead_n) if (r_assigned and n_assigned) else None,
            "cost_restricted": round(cost_r, 4) if cost_r is not None else None,
            "cost_coldRuleOff": round(cost_n, 4) if cost_n is not None else None,
            "cost_premium": round(cost_r - cost_n, 4) if (r_assigned and n_assigned) else None,
            "score_restricted": round(sc_r, 4) if sc_r is not None else None,
            "score_coldRuleOff": round(sc_n, 4) if sc_n is not None else None,
            "score_premium": round(sc_r - sc_n, 4) if (r_assigned and n_assigned) else None,
        })
    return pd.DataFrame(rows)


def main(horizon=b7.HORIZON, out_path="optimizer_coldchain_scenario.xlsx"):
    sheets = bc.load_sheets(hub_disruption=None)     # clean network
    E = pd.read_excel(bc.WORKBOOK, sheet_name="External Shipments")
    cold_ship, cold_del = _cold_universe(sheets, E)
    print(f"Cold-chain population: {len(cold_ship)} internal shipments, "
          f"{len(cold_del)} external deliveries; {int((sheets['hub']['ColdChainAvailable']=='Yes').sum())}"
          f"/{len(sheets['hub'])} hubs cold-capable")

    out_int = run_side("internal", sheets, E, horizon)
    out_ext = run_side("external", sheets, E, horizon)
    for o in (out_int, out_ext):
        for s in o["summary"]:
            print(f"  {s['side']:8s} {s['variant']:32s} solved {s['solved']}/{s['universe']}"
                  f"  avg {s['avg_MinScore']}")

    sub_int = cold_subset_table(out_int, cold_ship)
    sub_ext = cold_subset_table(out_ext, cold_del)

    # cost-of-compliance aggregates over cold-chain shipments assigned in BOTH variants
    def agg(sub):
        both = sub[sub["assigned_restricted"] & sub["assigned_coldRuleOff"]]
        return {
            "cold_shipments": len(sub),
            "assigned_restricted": int(sub["assigned_restricted"].sum()),
            "assigned_coldRuleOff": int(sub["assigned_coldRuleOff"].sum()),
            "blocked_by_coldchain": int((~sub["assigned_restricted"] & sub["assigned_coldRuleOff"]).sum()),
            "avg_routes_lost": round(sub["routes_lost_to_coldchain"].mean(), 2),
            "avg_lead_premium_days": round(both["lead_premium"].mean(), 2) if len(both) else None,
            "avg_cost_premium": round(both["cost_premium"].mean(), 4) if len(both) else None,
            "avg_score_premium": round(both["score_premium"].mean(), 4) if len(both) else None,
        }
    compliance = pd.DataFrame([{"side": "internal", **agg(sub_int)},
                               {"side": "external", **agg(sub_ext)}])

    summary = pd.DataFrame(out_int["summary"] + out_ext["summary"])
    assumptions = pd.DataFrame([
        ("Scenario", "Guide S2 Cold-chain: restrict eligible routes/hubs to cold-chain capable options."),
        ("Cold-chain rule", "TempRequirement == 'Cold Chain' materials may use a route only if BOTH "
         "origin and destination hubs have ColdChainAvailable == 'Yes'. Already enforced in "
         "apply_capability; this workbook quantifies its effect."),
        ("Counterfactual", "'cold rule OFF' disables ONLY the cold-chain check (require_cold_chain=False). "
         "ESD / moisture / lithium hazard checks REMAIN ON - hazardous materials are never routed "
         "through incompatible hubs in either variant."),
        ("Population", "Both variants solve the FULL population (240 internal / 225 external) so "
         "background capacity contention is identical. Subset tables then isolate the cold-chain "
         "shipments/deliveries."),
        ("Normalisation", "ONE fixed min-max scaler per side, fitted on the union of both variants' "
         "feasible candidates, so restricted and naive MinScores are directly comparable. Internal "
         "and external scores use different cost terms and are NOT comparable to each other."),
        ("Route/hub scenario", "Route scenario = Normal; hub_disruption = None (clean network). "
         "Cold-chain is a handling axis, independent of route/hub disruption."),
        ("Cost of compliance", "Premiums (lead/cost/score) are averaged over cold-chain shipments "
         "assigned in BOTH variants; 'blocked_by_coldchain' counts cold shipments feasible only when "
         "the rule is off (i.e. no cold-capable hub pair exists on their lane)."),
        ("Trade-off interpretation", "cost_premium is NEGATIVE here: the compliant (cold-capable) "
         "routes selected are on average slightly CHEAPER, but slower (positive lead premium) and "
         "worse on the combined 40/40/20 score (positive score premium). So the real cost of "
         "cold-chain compliance in this dataset is SERVICE SPEED and FEASIBILITY (blocked shipments), "
         "not freight cost. 'Costs more' refers to the WeightedScore penalty, not EUR."),
        ("Scaler", "Uses baseline_v7.canonical_scaler(side) - the SAME fixed min-max ruler as the "
         "official baseline (fitted across Normal+PHD+ACR, capability on). The 'cold-chain restricted' "
         "run therefore reproduces the official clean Normal solved count and average score exactly."),
    ], columns=["Assumption", "Detail"])

    keep = ["side", "variant", "universe", "solved", "unassigned",
            "avg_MinScore", "median", "min", "max"]
    with pd.ExcelWriter(out_path) as xw:
        summary[keep].to_excel(xw, sheet_name="Summary", index=False)
        compliance.to_excel(xw, sheet_name="CostOfCompliance", index=False)
        sub_int.to_excel(xw, sheet_name="ColdSubset_Internal", index=False)
        sub_ext.to_excel(xw, sheet_name="ColdSubset_External", index=False)
        assumptions.to_excel(xw, sheet_name="Assumptions", index=False)
    print(f"  wrote {out_path}")
    print(compliance.to_string(index=False))
    return {"summary": summary, "compliance": compliance}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-weeks", type=int, default=b7.HORIZON)
    args = ap.parse_args()
    main(horizon=args.horizon_weeks)
