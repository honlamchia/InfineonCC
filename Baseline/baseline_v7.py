"""
baseline_v7.py
==============
OFFICIAL BASELINE per Infineon AI Engineer clarification (v7):
two INDEPENDENT optimisers, no internal<->external join anywhere.

  1. Internal baseline : all 240 internal shipments.  Cost term = BaseCostEUR
     (raw route cost - internal legs have no kg; supported by the
     LowestRouteCostEUR benchmark field).  NOT called cost/kg.
  2. External baseline : all 225 external deliveries.  Cost term = CostPerKG =
     BaseCostEUR / individual ChargeableWeight_KG (matches LowestCostPerKG_EUR
     benchmark 225/225).  Demand = Pieces, capacity week = PUP_Date (stated
     assumptions).

The integrated delivery-grain model (optimize.py) is retained unchanged as the
optional combined extension (optimizer_combined_extension.xlsx).

Score (both, lower=better):  0.4*norm(lead) + 0.4*norm(cost term) + 0.2*norm(risk)
One fixed min-max scaler per optimiser, fitted on the union of feasible
candidates across all three scenarios.  Capacity: bottleneck>0, WeeklyFootprint
charged to route + both hubs per ISO week, WeeksRequired<=12 horizon,
unassigned slack, reason classification.  Self-loop routes (FromHub==ToHub)
are kept, flagged, and charged once per hub (set semantics); impact reported.
"""
from pathlib import Path
import argparse, math
import pandas as pd
import pulp

import build_candidates as bc
import optimize as opt

W_LEAD, W_COST, W_RISK = 0.40, 0.40, 0.20
PENALTY = 10.0
SCENARIOS = opt.SCENARIOS
HORIZON = opt.PLANNING_HORIZON_WEEKS

ACTIONS = {
    "handling_infeasible": "No handling-compatible hub pair - source alternate hub / exception",
    "zero_capacity":       "Only routes via zero-capacity hub/route - free capacity or reroute",
    "capacity_escalation": f"Exceeds {HORIZON}-week throughput horizon - split order or add capacity",
    "capacity_contention": "Weekly capacity contention - reschedule to another week"}


# ---------------------------------------------------------------- candidates
def capacity_fields(c, qty_map, key, horizon):
    c = c.copy()
    c["DemandQty"] = c[key].map(qty_map)
    c["BottleneckCapacityPerWeek"] = c[[
        "CapacityUnitsPerWeek", "orig_remaining_capacity_units",
        "dest_remaining_capacity_units"]].min(axis=1)
    c["WeeksRequired"] = [10**9 if b <= 0 else max(1, math.ceil(q / b))
                          for q, b in zip(c["DemandQty"], c["BottleneckCapacityPerWeek"])]
    c["MultiWeek"] = (c["WeeksRequired"] > 1) & (c["BottleneckCapacityPerWeek"] > 0)
    c["EffectiveLeadTimeDays"] = c["BaseLeadTimeDays"] + 7 * (c["WeeksRequired"].clip(upper=520) - 1)
    c["WeeklyFootprint"] = c[["DemandQty", "BottleneckCapacityPerWeek"]].min(axis=1).clip(lower=0)
    c["SelfLoop"] = c["FromHub"] == c["ToHub"]
    c["Escalation"] = (c["BottleneckCapacityPerWeek"] > 0) & (c["WeeksRequired"] > horizon)
    return c


def model_feasible(c):
    return c[(c["BottleneckCapacityPerWeek"] > 0) & (~c["Escalation"])].copy()


def internal_candidates(sheets, scenario):
    return bc.apply_capability(bc.build_candidates(sheets, scenario))


def external_candidates(sheets, E, scenario):
    """external_shipments -> material_families -> route_options -> hubs.
    NO join to internal_shipments (lane comes from the stage-link columns
    already stored on the external row)."""
    M, R, H = sheets["material"], sheets["route"], sheets["hub"]
    em = E.merge(
        M[["MaterialNo_Anon", "MaterialFamily", "HazardClass", "TempRequirement", "PriorityClass"]],
        left_on="MaterialNo_Anon_Link", right_on="MaterialNo_Anon",
        how="left", suffixes=("_ext", ""))
    routes = R[(R["DisruptionScenario"] == scenario) & (R["AvailableFlag"] == "Yes")]
    cand = em.merge(
        routes,
        left_on=["MaterialFamily", "InternalStageFrom_Link", "InternalStageTo_Link"],
        right_on=["MaterialFamily", "StageFrom", "StageTo"],
        how="inner", suffixes=("", "_route"))
    hub_cols = ["HubID", "remaining_capacity_units", "ColdChainAvailable",
                "ESDHandlingAvailable", "MoistureControlAvailable", "LithiumHandlingAvailable"]
    cand = cand.merge(H[hub_cols].add_prefix("orig_"), left_on="FromHub",
                      right_on="orig_HubID", how="left") \
               .merge(H[hub_cols].add_prefix("dest_"), left_on="ToHub",
                      right_on="dest_HubID", how="left")
    return bc.apply_capability(cand)


# ---------------------------------------------------------------- scoring
def fit_scaler(frames):
    u = pd.concat(frames, ignore_index=True)
    return {"lead": (float(u["BaseLeadTimeDays"].min()), float(u["BaseLeadTimeDays"].max())),
            "cost": (float(u["CostForScore"].min()), float(u["CostForScore"].max())),
            "risk": (float(u["RiskScore"].min()), float(u["RiskScore"].max()))}


def score(c, scaler):
    c = c.copy()
    c["n_lead"] = opt._scale(c["BaseLeadTimeDays"], *scaler["lead"])
    c["n_cost"] = opt._scale(c["CostForScore"], *scaler["cost"])
    c["n_risk"] = opt._scale(c["RiskScore"], *scaler["risk"])
    c["MinScore"] = W_LEAD * c["n_lead"] + W_COST * c["n_cost"] + W_RISK * c["n_risk"]
    return c


# ---------------------------------------------------------------- solver
def solve(cand, key, universe, hub_remaining):
    cand = cand.reset_index(drop=True)
    rows = cand.index.tolist()
    uidx = {s: j for j, s in enumerate(universe)}

    m = pulp.LpProblem("baseline", pulp.LpMinimize)
    x = {i: pulp.LpVariable(f"x_{i}", cat="Binary") for i in rows}
    u = {s: pulp.LpVariable(f"u_{uidx[s]}", cat="Binary") for s in universe}

    m += (pulp.lpSum(cand.loc[i, "MinScore"] * x[i] for i in rows)
          + PENALTY * pulp.lpSum(u.values()))

    by_key = cand.groupby(key).groups
    for s in universe:
        m += (pulp.lpSum(x[i] for i in by_key.get(s, [])) + u[s] == 1, f"assign_{uidx[s]}")

    for (rid, wk), grp in cand.groupby(["RouteOptionID", "week"]):
        m += (pulp.lpSum(cand.loc[i, "WeeklyFootprint"] * x[i] for i in grp.index)
              <= grp["CapacityUnitsPerWeek"].iloc[0], f"rc_{rid}_{wk}")

    usage = {}
    for i in rows:
        r = cand.loc[i]
        for hub in {r["FromHub"], r["ToHub"]}:          # set: self-loop charged once
            usage.setdefault((hub, r["week"]), []).append((r["WeeklyFootprint"], x[i]))
    for (hub, wk), terms in usage.items():
        m += (pulp.lpSum(w * v for w, v in terms)
              <= hub_remaining.get(hub, float("inf")), f"hc_{hub}_{wk}")

    m.solve(pulp.PULP_CBC_CMD(msg=False))
    chosen = cand.loc[[i for i in rows if x[i].value() == 1]].copy()
    unassigned = [s for s in universe if u[s].value() == 1]
    return {"status": pulp.LpStatus[m.status], "chosen": chosen, "unassigned": unassigned}


def classify(unassigned, annotated, feasible, key):
    cap_ok = set(annotated[key])
    pos = set(annotated.loc[annotated["BottleneckCapacityPerWeek"] > 0, key])
    horiz = set(feasible[key])
    out = {}
    for s in unassigned:
        if s not in cap_ok:   out[s] = "handling_infeasible"
        elif s not in pos:    out[s] = "zero_capacity"
        elif s not in horiz:  out[s] = "capacity_escalation"
        else:                 out[s] = "capacity_contention"
    return out


# ---------------------------------------------------------------- runners
def run_side(side, sheets, E, out_path, horizon):
    """side: 'internal' or 'external'."""
    if side == "internal":
        universe = sheets["internal"]["ShipmentID"].tolist()
        qty_map = sheets["internal"].set_index("ShipmentID")["Qty"].to_dict()
        wk_map = dict(zip(sheets["internal"]["ShipmentID"],
                          opt.iso_week(sheets["internal"]["ShipDate"])))
        key, cost_label = "ShipmentID", "BaseCostEUR (raw route cost - no kg on internal legs)"
        build = lambda sc: internal_candidates(sheets, sc)
        add_cost = lambda c: c.assign(CostForScore=c["BaseCostEUR"])
    else:
        universe = E["DeliveryNo"].tolist()
        qty_map = E.set_index("DeliveryNo")["Pieces"].to_dict()
        wk_map = dict(zip(E["DeliveryNo"], opt.iso_week(E["PUP_Date"])))
        key, cost_label = "DeliveryNo", "CostPerKG = BaseCostEUR / individual ChargeableWeight_KG"
        wmap = E.set_index("DeliveryNo")["ChargeableWeight_KG"].to_dict()
        build = lambda sc: external_candidates(sheets, E, sc)
        add_cost = lambda c: c.assign(CostForScore=c["BaseCostEUR"] / c[key].map(wmap))

    hub_remaining = sheets["hub"].set_index("HubID")["remaining_capacity_units"].to_dict()

    per_scen = {}
    frames = []
    for sc in SCENARIOS:
        ann = add_cost(capacity_fields(build(sc), qty_map, key, horizon))
        ann["week"] = ann[key].map(wk_map)
        feas = model_feasible(ann)
        per_scen[sc] = (ann, feas)
        frames.append(feas)
    scaler = fit_scaler(frames)

    summaries, sel_frames, un_rows = [], [], []
    for sc in SCENARIOS:
        ann, feas = per_scen[sc]
        feas = score(feas, scaler)
        res = solve(feas, key, universe, hub_remaining)
        reasons = classify(res["unassigned"], ann, feas, key)
        real = ann[ann["BottleneckCapacityPerWeek"] > 0]
        best = real.sort_values("EffectiveLeadTimeDays").groupby(key).first() if len(real) else pd.DataFrame()
        for s in res["unassigned"]:
            un_rows.append({
                "scenario": sc, key: s, "Reason": reasons[s],
                "BestAvailableWeeks": int(best.loc[s, "WeeksRequired"]) if len(best) and s in best.index else None,
                "BestAvailableRoute": best.loc[s, "RouteOptionID"] if len(best) and s in best.index else None,
                "RecommendedAction": ACTIONS[reasons[s]]})
        ch = res["chosen"]
        cnt = {r: sum(1 for v in reasons.values() if v == r) for r in ACTIONS}
        summaries.append({
            "scenario": sc, "solver": res["status"], "universe": len(universe),
            "solved": len(ch), "unassigned": len(res["unassigned"]),
            "avg_MinScore": round(ch["MinScore"].mean(), 4) if len(ch) else None,
            "median": round(ch["MinScore"].median(), 4) if len(ch) else None,
            "std": round(ch["MinScore"].std(), 4) if len(ch) else None,
            "min": round(ch["MinScore"].min(), 4) if len(ch) else None,
            "max": round(ch["MinScore"].max(), 4) if len(ch) else None,
            "multiweek_selected": int(ch["MultiWeek"].sum()),
            "selfloop_selected": int(ch["SelfLoop"].sum()),
            **{f"unassigned_{k}": v for k, v in cnt.items()}})
        ch = ch.copy(); ch.insert(0, "scenario", sc); sel_frames.append(ch)
        print(f"  {side.upper()} {sc}: solved {len(ch)}/{len(universe)} "
              f"avg {summaries[-1]['avg_MinScore']} (mw {summaries[-1]['multiweek_selected']}, "
              f"selfloop {summaries[-1]['selfloop_selected']})")

    keep = ["scenario", key, "MaterialFamily", "RouteOptionID", "FromHub", "ToHub",
            "TransportMode", "IsPrimary", "DemandQty", "week", "WeeklyFootprint",
            "BottleneckCapacityPerWeek", "WeeksRequired", "MultiWeek", "SelfLoop",
            "BaseLeadTimeDays", "EffectiveLeadTimeDays", "BaseCostEUR", "CostForScore",
            "RiskScore", "CO2Kg", "n_lead", "n_cost", "n_risk", "MinScore"]
    if side == "external":
        keep.insert(2, "ChargeableWeight_KG")
    sel = pd.concat(sel_frames, ignore_index=True)
    sel = sel[[c for c in keep if c in sel.columns]]

    bounds = pd.DataFrame([
        {"metric": "BaseLeadTimeDays", "min": scaler["lead"][0], "max": scaler["lead"][1]},
        {"metric": "CostForScore",     "min": scaler["cost"][0], "max": scaler["cost"][1]},
        {"metric": "RiskScore",        "min": scaler["risk"][0], "max": scaler["risk"][1]}])

    assumptions = pd.DataFrame([
        ("Baseline definition", f"Independent {side} optimiser per Infineon AI Engineer clarification. "
         "No join between internal_shipments and external_shipments anywhere in this model."),
        ("Objective", "MinScore = 0.4*norm(BaseLeadTimeDays) + 0.4*norm(cost term) + 0.2*norm(RiskScore); "
         "lower is better. Min-max normalisation is a stated modelling assumption."),
        ("Cost term", cost_label),
        ("Universe", f"All {len(universe)} {'internal shipments' if side=='internal' else 'external deliveries'}; "
         "solved + unassigned = universe in every scenario."),
        ("Demand & week", ("Qty, ISO week of ShipDate" if side == "internal" else
         "Pieces as demand quantity (route capacity is units/week - kg must not be compared to it); "
         "ISO week of PUP_Date. Both stated as assumptions pending AI-Engineer confirmation.")),
        ("Normalisation", "ONE fixed scaler fitted on the union of feasible candidates across "
         "Normal + PrimaryHubDown + AirCapacityReduced (see ScalerBounds). Never refit per scenario. "
         "Internal and external scores are NOT comparable to each other (different cost terms)."),
        ("Capability", "Cold-chain + hazard booleans; BOTH origin and destination hubs must qualify."),
        ("Capacity", "remaining = weekly*(max_util*(1-reduction)) - current_load (fractions). "
         "bottleneck = min(route, origin, dest); bottleneck<=0 rejected; WeeklyFootprint = "
         "min(demand, bottleneck) charged identically to route + both hubs per week; "
         f"WeeksRequired > {horizon} -> capacity escalation. Internal and external capacity are "
         "NOT jointly constrained in the baseline (per spec); the combined extension coordinates them."),
        ("Self-loops", "Routes with FromHub == ToHub are kept and flagged (SelfLoop); the hub is "
         "charged once (set semantics). Selected self-loop counts are reported per scenario."),
        ("Solver", "Binary PuLP/CBC; one route or explicit unassigned slack per unit of demand; "
         "unassigned reasons classified (handling / zero-capacity / escalation / contention)."),
    ], columns=["Assumption", "Detail"])

    with pd.ExcelWriter(out_path) as xw:
        pd.DataFrame(summaries).to_excel(xw, sheet_name="Summary", index=False)
        sel.to_excel(xw, sheet_name="SelectedRoutes", index=False)
        pd.DataFrame(un_rows).to_excel(xw, sheet_name="Unassigned", index=False)
        bounds.to_excel(xw, sheet_name="ScalerBounds", index=False)
        assumptions.to_excel(xw, sheet_name="Assumptions", index=False)
    print(f"  wrote {out_path}")
    return {"summaries": summaries, "selected": sel, "scaler": scaler}


def cost_basis_check(sheets, E):
    """No organiser benchmark exists (HackathonObjectiveScore ships empty 0/240, 0/225).
    Verify the two benchmark fields the guide DOES provide."""
    I, R = sheets["internal"], sheets["route"]
    avail = R[R["AvailableFlag"] == "Yes"]
    # benchmark fields are computed over available routes across ALL scenarios
    lane_min = avail.groupby(["MaterialFamily", "StageFrom", "StageTo"])["BaseCostEUR"].min()
    key = list(zip(I["MaterialFamily"], I["StageFrom"], I["StageTo"]))
    recon = pd.Series([lane_min.get(k) for k in key], index=I.index)
    int_match = int((recon == I["LowestRouteCostEUR"]).sum())
    ext_lane_min = pd.Series([lane_min.get(k) for k in zip(
        E["MaterialFamily"], E["InternalStageFrom_Link"], E["InternalStageTo_Link"])], index=E.index)
    ext_recon = ext_lane_min / E["ChargeableWeight_KG"]
    ext_match = int((ext_recon.round(6) == E["LowestCostPerKG_EUR"].round(6)).sum())
    return int_match, ext_match


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-weeks", type=int, default=HORIZON)
    args = ap.parse_args()

    sheets = bc.load_sheets()
    E = pd.read_excel(bc.WORKBOOK, sheet_name="External Shipments")

    im, em = cost_basis_check(sheets, E)
    print(f"Benchmark checks: LowestRouteCostEUR reconstructed (raw BaseCostEUR basis) {im}/240; "
          f"LowestCostPerKG_EUR reconstructed (per-delivery kg basis) {em}/225")
    print("HackathonObjectiveScore ships EMPTY in the student dataset - no organiser score to "
          "reconstruct; raw BaseCostEUR chosen for internal (see Assumptions).")

    print("\nINTERNAL BASELINE (240 shipments)")
    run_side("internal", sheets, E, "optimizer_internal_baseline.xlsx", args.horizon_weeks)
    print("\nEXTERNAL BASELINE (225 deliveries)")
    run_side("external", sheets, E, "optimizer_external_baseline.xlsx", args.horizon_weeks)
