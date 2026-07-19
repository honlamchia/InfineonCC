"""
scenario_expedite.py
====================
Guide Scenario 3 - Expedite priority: choose the fastest route under the
capacity limit, even if cost increases.

Implemented as a THREE-STAGE LEXICOGRAPHIC solve (per reviewer guidance,
18 Jul 2026) rather than a weighted objective, because a single weighted sum
does NOT guarantee expedite shipments win contested capacity - several normal
shipments' combined score gain can outbid one expedite shipment for a scarce
lane.  Expedite-only: the guide names "Expedite priority", and the dataset
keeps Standard / Critical / Expedite as three distinct PriorityClass tiers, so
Critical and Standard stay under the normal 40/40/20 objective (a full
three-tier hierarchy is offered as an optional --hierarchy mode).

  Stage 1  maximise expedite coverage      : min  sum_{s in Expedite} u_s
  Stage 2  minimise expedite completion    : lock stage-1 coverage, then
                                              min sum_{Expedite} EffectiveLeadTime * x
  Stage 3  optimise everyone else          : lock stages 1-2, then
                                              min sum_all MinScore*x + PENALTY*sum u

Selection uses EffectiveLeadTimeDays (capacity-aware: a "fast" lane that needs
many throughput weeks is not really fast).  REPORTING still uses the official
40/40/20 MinScore (fixed scaler) so results stay comparable to every other
scenario.  Route scenario = Normal, hub_disruption = None (clean network).

Outputs optimizer_expedite_scenario.xlsx:
  Summary          - expedite coverage & avg lead/score, priority vs cost-optimal
  ExpediteDetail   - per expedite shipment: chosen route, lead/cost/mode under the
                     priority solve vs the cost-optimal solve; days saved, cost
                     increase, mode shift (the "even if cost increases" evidence)
  Assumptions
"""
from pathlib import Path
import argparse
import pandas as pd
import pulp

import build_candidates as bc
import baseline_v7 as b7

SCEN = "Normal"
PENALTY = b7.PENALTY
EXPEDITE = "Expedite"
TIER_ORDER = ["Expedite", "Critical", "Standard"]   # used only in --hierarchy mode


def _side_config(side, sheets, E):
    """Reuse baseline_v7's shared setup (build fixed to Normal) and add the
    priority tier for the FULL universe from master data."""
    M = sheets["material"]
    c = b7._side_setup(side, sheets, E)
    build = lambda: c["build"](SCEN)
    if side == "internal":
        im = sheets["internal"].merge(M[["MaterialNo_Anon", "PriorityClass"]],
                                      on="MaterialNo_Anon", how="left")
        tier_of = dict(zip(im["ShipmentID"], im["PriorityClass"]))
    else:
        em = E.merge(M[["MaterialNo_Anon", "PriorityClass"]],
                     left_on="MaterialNo_Anon_Link", right_on="MaterialNo_Anon", how="left")
        tier_of = dict(zip(em["DeliveryNo"], em["PriorityClass"]))
    return c["universe"], c["qty_map"], c["wk_map"], c["key"], build, c["add_cost"], tier_of


def _build_model(cand, key, universe, hub_remaining):
    """Assignment + route-cap + hub-cap constraints, no objective yet."""
    cand = cand.reset_index(drop=True)
    rows = cand.index.tolist()
    m = pulp.LpProblem("expedite", pulp.LpMinimize)
    x = {i: pulp.LpVariable(f"x_{i}", cat="Binary") for i in rows}
    u = {s: pulp.LpVariable(f"u_{s}", cat="Binary") for s in universe}

    by_key = cand.groupby(key).groups
    for s in universe:
        m += (pulp.lpSum(x[i] for i in by_key.get(s, [])) + u[s] == 1, f"assign_{s}")
    for (rid, wk), grp in cand.groupby(["RouteOptionID", "week"]):
        m += (pulp.lpSum(cand.loc[i, "WeeklyFootprint"] * x[i] for i in grp.index)
              <= grp["CapacityUnitsPerWeek"].iloc[0], f"rc_{rid}_{wk}")
    usage = {}
    for i in rows:
        r = cand.loc[i]
        for hub in {r["FromHub"], r["ToHub"]}:
            usage.setdefault((hub, r["week"]), []).append((r["WeeklyFootprint"], x[i]))
    for (hub, wk), terms in usage.items():
        m += (pulp.lpSum(w * v for w, v in terms)
              <= hub_remaining.get(hub, float("inf")), f"hc_{hub}_{wk}")
    return m, x, u, cand, rows


def _solve(m):
    m.solve(pulp.PULP_CBC_CMD(msg=False))
    return pulp.LpStatus[m.status]


def lexicographic(cand, key, universe, hub_remaining, tier_of, tiers):
    """tiers = ordered list of PriorityClass tiers to prioritise (Expedite-only
    => ['Expedite']; hierarchy => ['Expedite','Critical','Standard'])."""
    m, x, u, cand, rows = _build_model(cand, key, universe, hub_remaining)
    idx_lead = cand["EffectiveLeadTimeDays"].to_dict()
    ship_of = cand[key].to_dict()

    stage_log = []
    for tier in tiers:
        tier_ships = [s for s in universe if tier_of.get(s) == tier]
        if not tier_ships:
            continue
        # Stage A: maximise coverage of this tier (min unassigned in tier)
        m.setObjective(pulp.lpSum(u[s] for s in tier_ships))
        _solve(m)
        cov = int(round(pulp.value(m.objective)))
        m += (pulp.lpSum(u[s] for s in tier_ships) <= cov, f"lock_cov_{tier}")
        # Stage B: minimise this tier's total effective lead time, coverage locked
        tier_rows = [i for i in rows if tier_of.get(ship_of[i]) == tier]
        m.setObjective(pulp.lpSum(idx_lead[i] * x[i] for i in tier_rows))
        _solve(m)
        lead = pulp.value(m.objective)
        m += (pulp.lpSum(idx_lead[i] * x[i] for i in tier_rows) <= lead + 0.5,
              f"lock_lead_{tier}")
        stage_log.append({"tier": tier, "tier_size": len(tier_ships),
                          "unassigned": cov, "total_eff_lead": round(lead, 1)})

    # Final stage: optimise the global 40/40/20 among remaining freedom
    m.setObjective(pulp.lpSum(cand.loc[i, "MinScore"] * x[i] for i in rows)
                   + PENALTY * pulp.lpSum(u.values()))
    status = _solve(m)
    chosen = cand.loc[[i for i in rows if x[i].value() == 1]].copy()
    unassigned = [s for s in universe if u[s].value() == 1]
    return {"status": status, "chosen": chosen, "unassigned": unassigned,
            "stage_log": stage_log}


def run_side(side, sheets, E, horizon, hierarchy=False):
    universe, qty_map, wk_map, key, build, add_cost, tier_of = _side_config(side, sheets, E)
    hub_remaining = sheets["hub"].set_index("HubID")["remaining_capacity_units"].to_dict()

    ann = add_cost(b7.capacity_fields(build(), qty_map, key, horizon))
    ann["week"] = ann[key].map(wk_map)

    feas = b7.model_feasible(ann)
    # THE canonical scaler (same ruler as the official baseline and every scenario)
    scaler = b7.canonical_scaler(side, sheets, E, horizon)
    feas = b7.score(feas, scaler)

    tiers = TIER_ORDER if hierarchy else [EXPEDITE]
    lex = lexicographic(feas, key, universe, hub_remaining, tier_of, tiers)

    # comparison baseline: the standard 40/40/20 solve (NO priority). Called
    # 'weighted_baseline' - it minimises the full 0.4 lead + 0.4 cost + 0.2 risk
    # score, NOT cost alone; it reproduces the official clean Normal result.
    opt = b7.solve(feas, key, universe, hub_remaining)

    exp_ids = sorted(s for s in universe if tier_of.get(s) == EXPEDITE)
    ch_p = lex["chosen"].set_index(key)
    ch_o = opt["chosen"].set_index(key)

    rows = []
    for s in exp_ids:
        p, o = s in ch_p.index, s in ch_o.index
        def g(ch, col): return ch.loc[s, col] if s in ch.index else None
        lead_p, lead_o = g(ch_p, "EffectiveLeadTimeDays"), g(ch_o, "EffectiveLeadTimeDays")
        cost_p, cost_o = g(ch_p, "CostForScore"), g(ch_o, "CostForScore")
        rows.append({
            key: s,
            "assigned_priority": p, "assigned_weighted_baseline": o,
            "route_priority": g(ch_p, "RouteOptionID"), "route_weighted_baseline": g(ch_o, "RouteOptionID"),
            "mode_priority": g(ch_p, "TransportMode"), "mode_weighted_baseline": g(ch_o, "TransportMode"),
            "mode_shift": (g(ch_p, "TransportMode") != g(ch_o, "TransportMode")) if (p and o) else None,
            "effLead_priority": lead_p, "effLead_weighted_baseline": lead_o,
            "days_saved": (lead_o - lead_p) if (p and o) else None,
            "cost_priority": round(cost_p, 4) if cost_p is not None else None,
            "cost_weighted_baseline": round(cost_o, 4) if cost_o is not None else None,
            "cost_increase": round(cost_p - cost_o, 4) if (p and o) else None,
            "score_priority": round(g(ch_p, "MinScore"), 4) if p else None,
            "score_weighted_baseline": round(g(ch_o, "MinScore"), 4) if o else None,
        })
    detail = pd.DataFrame(rows)
    both = detail[detail["assigned_priority"] & detail["assigned_weighted_baseline"]]

    summary = {
        "side": side, "universe": len(universe), "expedite_shipments": len(exp_ids),
        "expedite_assigned_priority": int(detail["assigned_priority"].sum()),
        "expedite_assigned_weighted_baseline": int(detail["assigned_weighted_baseline"].sum()),
        "expedite_avg_effLead_priority": round(ch_p.loc[[s for s in exp_ids if s in ch_p.index],
                                               "EffectiveLeadTimeDays"].mean(), 2) if len(exp_ids) else None,
        "expedite_avg_effLead_weighted_baseline": round(ch_o.loc[[s for s in exp_ids if s in ch_o.index],
                                              "EffectiveLeadTimeDays"].mean(), 2) if len(exp_ids) else None,
        "avg_days_saved": round(both["days_saved"].mean(), 2) if len(both) else None,
        "avg_cost_increase": round(both["cost_increase"].mean(), 4) if len(both) else None,
        "mode_shifts": int(both["mode_shift"].sum()) if len(both) else 0,
        "total_solved_priority": len(lex["chosen"]),
        "total_solved_weighted_baseline": len(opt["chosen"]),
        "avg_MinScore_priority_all": round(lex["chosen"]["MinScore"].mean(), 4) if len(lex["chosen"]) else None,
        "avg_MinScore_weighted_baseline_all": round(opt["chosen"]["MinScore"].mean(), 4) if len(opt["chosen"]) else None,
    }
    return {"side": side, "summary": summary, "detail": detail, "stage_log": lex["stage_log"]}


def main(horizon=b7.HORIZON, hierarchy=False, out_path="optimizer_expedite_scenario.xlsx"):
    sheets = bc.load_sheets(hub_disruption=None)
    E = pd.read_excel(bc.WORKBOOK, sheet_name="External Shipments")
    mode = "3-tier hierarchy (Expedite>Critical>Standard)" if hierarchy else "Expedite-only"
    print(f"Expedite priority mode: {mode}")

    out_int = run_side("internal", sheets, E, horizon, hierarchy)
    out_ext = run_side("external", sheets, E, horizon, hierarchy)
    for o in (out_int, out_ext):
        s = o["summary"]
        print(f"  {s['side']:8s} expedite {s['expedite_assigned_priority']} of {s['expedite_shipments']} assigned"
              f" | avg eff-lead {s['expedite_avg_effLead_priority']}d "
              f"(weighted-baseline {s['expedite_avg_effLead_weighted_baseline']}d)"
              f" | avg days saved {s['avg_days_saved']} | mode shifts {s['mode_shifts']}"
              f" | avg cost +{s['avg_cost_increase']}")

    summary = pd.DataFrame([out_int["summary"], out_ext["summary"]])
    stage = pd.DataFrame([{"side": o["side"], **row}
                          for o in (out_int, out_ext) for row in o["stage_log"]])
    assumptions = pd.DataFrame([
        ("Scenario", "Guide S3 Expedite priority: fastest route under capacity limit, even if cost rises."),
        ("Priority definition", "Expedite = Material_Families.PriorityClass == 'Expedite' "
         "(80 internal / 74 external). Critical and Standard are distinct tiers kept under the normal "
         "40/40/20 objective. --hierarchy escalates Critical then Standard after Expedite as an option."),
        ("Method", "THREE-STAGE LEXICOGRAPHIC solve, not a weighted objective. Stage 1 maximises "
         "expedite coverage (min unassigned); Stage 2 locks that coverage and minimises expedite "
         "total EffectiveLeadTimeDays; Stage 3 locks stages 1-2 and minimises the global 40/40/20 "
         "MinScore + unassigned penalty for the remaining freedom."),
        ("Why lexicographic", "A single weighted sum can allocate a scarce fast lane to several normal "
         "shipments whose combined score gain exceeds one expedite shipment's - contradicting 'priority'. "
         "Lexicographic guarantees expedite is served first and fastest."),
        ("Lead metric", "Selection uses EffectiveLeadTimeDays (BaseLeadTimeDays + throughput weeks) so a "
         "capacity-bottlenecked 'fast' lane is not treated as genuinely fast. Reporting scores still use "
         "the official 40/40/20 MinScore (BaseLeadTimeDays term) with the fixed scaler."),
        ("Capacity", "The 'under capacity limit' clause is enforced by keeping ALL route/hub capacity "
         "constraints active in every stage - expedite cannot exceed feasible throughput."),
        ("Comparison", "'weighted_baseline' column = the standard 40/40/20 solve with NO priority "
         "(minimises 0.4 lead + 0.4 cost + 0.2 risk - NOT cost alone, hence not 'cost-optimal'). "
         "days_saved / cost_increase / mode_shift are per expedite shipment assigned in BOTH solves - "
         "this is the 'even if cost increases' evidence."),
        ("Scaler", "Uses baseline_v7.canonical_scaler(side) - the SAME fixed ruler as the official "
         "baseline (Normal+PHD+ACR, capability on). The weighted_baseline solve therefore reproduces "
         "the official clean Normal solved count and average score exactly."),
        ("Route/hub scenario", "Route scenario = Normal; hub_disruption = None (clean network)."),
    ], columns=["Assumption", "Detail"])

    with pd.ExcelWriter(out_path) as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        stage.to_excel(xw, sheet_name="StageLog", index=False)
        out_int["detail"].to_excel(xw, sheet_name="ExpediteDetail_Internal", index=False)
        out_ext["detail"].to_excel(xw, sheet_name="ExpediteDetail_External", index=False)
        assumptions.to_excel(xw, sheet_name="Assumptions", index=False)
    print(f"  wrote {out_path}")
    return {"summary": summary}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-weeks", type=int, default=b7.HORIZON)
    ap.add_argument("--hierarchy", action="store_true",
                    help="escalate Critical then Standard after Expedite (optional extension)")
    args = ap.parse_args()
    main(horizon=args.horizon_weeks, hierarchy=args.hierarchy)
