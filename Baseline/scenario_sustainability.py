"""
scenario_sustainability.py
=========================
Guide Scenario 4 - Sustainability: minimize CO2e while keeping delivery within SLA.

THREE-STAGE LEXICOGRAPHIC solve (per reviewer guidance) so the model never drops
a shipment merely to lower the carbon total:

  Stage 1  maximise SLA-feasible coverage : min  sum u   (over all shipments;
                                             candidates pre-filtered to
                                             EffectiveLeadTimeDays <= SLA)
  Stage 2  minimise CO2e                   : lock coverage, then min sum CO2Kg * x
  Stage 3  tie-break                       : lock CO2, then min sum MinScore*x
                                             + penalty (the 40/40/20 breaks CO2 ties
                                             toward cheaper / lower-risk lanes)

CO2Kg is used as the guide DEFINES it - a per-route-option emissions indicator
(104-244 kg). It is NOT scaled by weight or quantity (that interpretation is
unconfirmed). SLA is a stated PROXY, not a contractual figure:
  * internal  SLA = Internal_Shipments.LeadTimeDays  (planned lead-time allowance;
              == ExpectedArrival - ShipDate for all 240 rows).
  * external  SLA = External_Shipments.BestLeadTimeDays + --external-sla-buffer-days
              (default 2). POD-PUP is NOT used (126 nulls, negative values).
The SLA test uses EffectiveLeadTimeDays (capacity-aware) so a lane that needs
multiple throughput weeks cannot silently blow the SLA.

Unassigned shipments are classified, adding a new reason `sla_infeasible`
(capacity-feasible candidates exist, but none within SLA). Scoring uses the
canonical 40/40/20 scaler. Route scenario = Normal, hub_disruption = None.

Outputs optimizer_sustainability_scenario.xlsx:
  Summary        - per side: CO2 (min-CO2 solve) vs weighted-baseline, kg & %
                   saved, WeightedScore give-up, avg lead, coverage, SLA breaches
                   the baseline would incur, shipments at the SLA boundary.
  ModeShift      - transport-mode mix, min-CO2 solve vs weighted-baseline.
  Unassigned     - per unserved shipment: reason (incl. sla_infeasible) + action.
  Detail         - per shipment: chosen route / CO2 / lead / score, both solves.
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

ACTIONS = {
    "handling_infeasible": "No handling-compatible hub pair - source alternate hub / exception",
    "zero_capacity":       "Only routes via zero-capacity hub/route - free capacity or reroute",
    "capacity_escalation": f"Exceeds {b7.HORIZON}-week throughput horizon - split order or add capacity",
    "sla_infeasible":      "No route meets the SLA within capacity - relax SLA, expedite, or split",
    "capacity_contention": "Weekly capacity contention - reschedule to another week"}


def _side_config(side, sheets, E, buffer_days):
    c = b7._side_setup(side, sheets, E)
    build = lambda: c["build"](SCEN)
    if side == "internal":
        sla_map = sheets["internal"].set_index("ShipmentID")["LeadTimeDays"].to_dict()
        sla_label = "Internal_Shipments.LeadTimeDays (planned lead-time allowance proxy)"
    else:
        sla_map = (E.set_index("DeliveryNo")["BestLeadTimeDays"] + buffer_days).to_dict()
        sla_label = f"External_Shipments.BestLeadTimeDays + {buffer_days} buffer days"
    return c["universe"], c["qty_map"], c["wk_map"], c["key"], build, c["add_cost"], sla_map, sla_label


def _build_model(cand, key, universe, hub_remaining):
    cand = cand.reset_index(drop=True)
    rows = cand.index.tolist()
    m = pulp.LpProblem("sustainability", pulp.LpMinimize)
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


def lexicographic_min_co2(cand, key, universe, hub_remaining):
    m, x, u, cand, rows = _build_model(cand, key, universe, hub_remaining)
    co2 = cand["CO2Kg"].to_dict()
    # Stage 1: maximise SLA-feasible coverage
    m.setObjective(pulp.lpSum(u.values()))
    _solve(m)
    cov = int(round(pulp.value(m.objective)))
    m += (pulp.lpSum(u.values()) <= cov, "lock_coverage")
    # Stage 2: minimise total CO2
    m.setObjective(pulp.lpSum(co2[i] * x[i] for i in rows))
    _solve(m)
    total_co2 = pulp.value(m.objective)
    m += (pulp.lpSum(co2[i] * x[i] for i in rows) <= total_co2 + 0.5, "lock_co2")
    # Stage 3: tie-break on the 40/40/20 score
    m.setObjective(pulp.lpSum(cand.loc[i, "MinScore"] * x[i] for i in rows)
                   + PENALTY * pulp.lpSum(u.values()))
    status = _solve(m)
    chosen = cand.loc[[i for i in rows if x[i].value() == 1]].copy()
    unassigned = [s for s in universe if u[s].value() == 1]
    return {"status": status, "chosen": chosen, "unassigned": unassigned,
            "total_co2": total_co2}


def classify(unassigned, annotated, sla_feasible_ids, key):
    """handling -> zero-capacity -> escalation -> sla -> contention."""
    cap_ok = set(annotated[key])
    within_cap = set(annotated.loc[annotated["BottleneckCapacityPerWeek"] > 0, key])
    within_horiz = set(annotated.loc[(annotated["BottleneckCapacityPerWeek"] > 0)
                                     & (~annotated["Escalation"]), key])
    out = {}
    for s in unassigned:
        if s not in cap_ok:            out[s] = "handling_infeasible"
        elif s not in within_cap:      out[s] = "zero_capacity"
        elif s not in within_horiz:    out[s] = "capacity_escalation"
        elif s not in sla_feasible_ids: out[s] = "sla_infeasible"
        else:                          out[s] = "capacity_contention"
    return out


def run_side(side, sheets, E, horizon, buffer_days):
    universe, qty_map, wk_map, key, build, add_cost, sla_map, sla_label = \
        _side_config(side, sheets, E, buffer_days)
    hub_remaining = sheets["hub"].set_index("HubID")["remaining_capacity_units"].to_dict()
    scaler = b7.canonical_scaler(side, sheets, E, horizon)

    ann = add_cost(b7.capacity_fields(build(), qty_map, key, horizon))
    ann["week"] = ann[key].map(wk_map)
    feas = b7.score(b7.model_feasible(ann), scaler)
    feas["SLA"] = feas[key].map(sla_map)
    feas["within_SLA"] = feas["EffectiveLeadTimeDays"] <= feas["SLA"]
    feas_sla = feas[feas["within_SLA"]].copy()
    sla_feasible_ids = set(feas_sla[key])

    # sustainability solve (min CO2 within SLA) and weighted-baseline (40/40/20, no SLA)
    sus = lexicographic_min_co2(feas_sla, key, universe, hub_remaining)
    base = b7.solve(feas, key, universe, hub_remaining)

    ch_s, ch_b = sus["chosen"], base["chosen"]
    reasons = classify(sus["unassigned"], ann, sla_feasible_ids, key)

    # baseline SLA breaches: weighted-baseline routes that exceed the shipment's SLA.
    # A breach is either RE-ROUTED within SLA by the sustainability solve, or it
    # becomes UNSERVED (no within-SLA route). Do NOT call the total "fixed".
    ch_b2 = ch_b.copy()
    ch_b2["SLA"] = ch_b2[key].map(sla_map)
    breach_ids = set(ch_b2.loc[ch_b2["EffectiveLeadTimeDays"] > ch_b2["SLA"], key])
    sus_served = set(ch_s[key])
    rerouted_within_sla = len(breach_ids & sus_served)
    unserved_due_to_sla = len(breach_ids - sus_served)
    at_boundary = int((ch_s["EffectiveLeadTimeDays"] == ch_s[key].map(sla_map)).sum())

    # --- fair, same-population comparison (shipments assigned in BOTH solves) ---
    s_idx, b_idx = ch_s.set_index(key), ch_b.set_index(key)
    common = s_idx.index.intersection(b_idx.index)
    co2_s_common = s_idx.loc[common, "CO2Kg"].sum()
    co2_b_common = b_idx.loc[common, "CO2Kg"].sum()

    # total CO2 over each solve's own set (different populations - context only)
    co2_s = ch_s["CO2Kg"].sum()
    co2_b = ch_b["CO2Kg"].sum()
    summary = {
        "side": side, "universe": len(universe), "sla_definition": sla_label,
        "sustainability_solved": len(ch_s), "weighted_baseline_solved": len(ch_b),
        "sla_infeasible": sum(1 for v in reasons.values() if v == "sla_infeasible"),
        # ---- fair CO2 / score comparison on shipments served by BOTH solves ----
        "common_population": len(common),
        "CO2_common_sustainability": int(co2_s_common),
        "CO2_common_weighted_baseline": int(co2_b_common),
        "CO2_saved_kg_common": int(co2_b_common - co2_s_common),
        "CO2_saved_pct_common": round(100 * (co2_b_common - co2_s_common) / co2_b_common, 2)
        if co2_b_common else None,
        "score_common_sustainability": round(s_idx.loc[common, "MinScore"].mean(), 4) if len(common) else None,
        "score_common_weighted_baseline": round(b_idx.loc[common, "MinScore"].mean(), 4) if len(common) else None,
        # ---- totals over each solve's own (different) population - context ----
        "total_CO2_sustainability": int(co2_s), "total_CO2_weighted_baseline": int(co2_b),
        "avg_MinScore_sustainability": round(ch_s["MinScore"].mean(), 4) if len(ch_s) else None,
        "avg_MinScore_weighted_baseline": round(ch_b["MinScore"].mean(), 4) if len(ch_b) else None,
        "avg_EffLead_sustainability": round(ch_s["EffectiveLeadTimeDays"].mean(), 2) if len(ch_s) else None,
        "avg_EffLead_weighted_baseline": round(ch_b["EffectiveLeadTimeDays"].mean(), 2) if len(ch_b) else None,
        # ---- baseline SLA breaches, split honestly (total = rerouted + unserved) ----
        "baseline_SLA_breaches_total": len(breach_ids),
        "baseline_breaches_rerouted_within_SLA": rerouted_within_sla,
        "baseline_breaches_unserved_due_to_SLA": unserved_due_to_sla,
        "shipments_at_SLA_boundary": at_boundary,
    }

    # per-shipment detail
    det = []
    for s in universe:
        a, b = s in s_idx.index, s in b_idx.index
        def g(idx, s, col): return idx.loc[s, col] if s in idx.index else None
        det.append({
            key: s, "SLA": sla_map.get(s),
            "assigned_sustainability": a, "assigned_weighted_baseline": b,
            "route_sus": g(s_idx, s, "RouteOptionID"), "route_base": g(b_idx, s, "RouteOptionID"),
            "CO2_sus": g(s_idx, s, "CO2Kg"), "CO2_base": g(b_idx, s, "CO2Kg"),
            "CO2_saved": (g(b_idx, s, "CO2Kg") - g(s_idx, s, "CO2Kg")) if (a and b) else None,
            "effLead_sus": g(s_idx, s, "EffectiveLeadTimeDays"),
            "effLead_base": g(b_idx, s, "EffectiveLeadTimeDays"),
            "mode_sus": g(s_idx, s, "TransportMode"), "mode_base": g(b_idx, s, "TransportMode"),
            "score_sus": round(g(s_idx, s, "MinScore"), 4) if a else None,
            "score_base": round(g(b_idx, s, "MinScore"), 4) if b else None,
        })
    detail = pd.DataFrame(det)

    # mode mix on the COMMON population only, so the shift is a true mode change
    # (not an artefact of the two solves routing different numbers of shipments)
    mode_s = s_idx.loc[common, "TransportMode"].value_counts()
    mode_b = b_idx.loc[common, "TransportMode"].value_counts()
    modes = sorted(set(mode_s.index) | set(mode_b.index))
    mode_mix = pd.DataFrame([{"side": side, "population": f"common ({len(common)})",
                              "TransportMode": m,
                              "sustainability": int(mode_s.get(m, 0)),
                              "weighted_baseline": int(mode_b.get(m, 0)),
                              "shift": int(mode_s.get(m, 0)) - int(mode_b.get(m, 0))}
                             for m in modes])

    un_rows = [{"side": side, key: s, "Reason": reasons[s],
                "RecommendedAction": ACTIONS[reasons[s]]} for s in sus["unassigned"]]
    return {"summary": summary, "detail": detail, "mode_mix": mode_mix, "unassigned": un_rows}


def main(horizon=b7.HORIZON, buffer_days=2, out_path="optimizer_sustainability_scenario.xlsx"):
    sheets = bc.load_sheets(hub_disruption=None)
    E = pd.read_excel(bc.WORKBOOK, sheet_name="External Shipments")
    print(f"Sustainability (min CO2 within SLA); external SLA buffer = {buffer_days} days")

    outs = {side: run_side(side, sheets, E, horizon, buffer_days)
            for side in ("internal", "external")}
    for side in ("internal", "external"):
        s = outs[side]["summary"]
        print(f"  {side:8s} solved {s['sustainability_solved']}/{s['universe']} "
              f"(sla_infeasible {s['sla_infeasible']}) | same-pop({s['common_population']}) CO2 "
              f"{s['CO2_common_sustainability']} vs {s['CO2_common_weighted_baseline']} "
              f"(saved {s['CO2_saved_kg_common']}kg {s['CO2_saved_pct_common']}%) | score "
              f"{s['score_common_sustainability']} vs {s['score_common_weighted_baseline']} | "
              f"baseline breaches {s['baseline_SLA_breaches_total']} "
              f"({s['baseline_breaches_rerouted_within_SLA']} rerouted, "
              f"{s['baseline_breaches_unserved_due_to_SLA']} unserved)")

    summary = pd.DataFrame([outs["internal"]["summary"], outs["external"]["summary"]])
    mode_mix = pd.concat([outs["internal"]["mode_mix"], outs["external"]["mode_mix"]],
                         ignore_index=True)
    unassigned = pd.DataFrame(outs["internal"]["unassigned"] + outs["external"]["unassigned"])

    assumptions = pd.DataFrame([
        ("Scenario", "Guide S4 Sustainability: minimise CO2e while keeping delivery within SLA."),
        ("Method", "THREE-STAGE LEXICOGRAPHIC: (1) maximise SLA-feasible coverage; (2) lock coverage, "
         "minimise total CO2Kg; (3) lock CO2, tie-break on the 40/40/20 MinScore. Coverage-first so a "
         "shipment is never dropped merely to reduce the carbon total."),
        ("CO2 definition", "CO2Kg used AS THE GUIDE DEFINES IT - a per-route-option emissions indicator "
         "(104-244 kg). NOT scaled by weight or quantity; per-kg / per-unit interpretations are "
         "unconfirmed and would change totals."),
        ("SLA proxy (stated, not contractual)", "internal SLA = Internal_Shipments.LeadTimeDays "
         "(planned lead-time allowance; == ExpectedArrival - ShipDate, 240/240). external SLA = "
         "External_Shipments.BestLeadTimeDays + buffer (--external-sla-buffer-days, default 2). "
         "POD-PUP is NOT used as SLA (126 nulls, negative values - it is an outcome, not a promise)."),
        ("SLA test", "A candidate is admitted only if EffectiveLeadTimeDays <= SLA (capacity-aware, so "
         "a lane needing multiple throughput weeks cannot silently exceed the SLA)."),
        ("Unassigned reasons", "handling_infeasible -> zero_capacity -> capacity_escalation -> "
         "sla_infeasible (capacity-feasible but no route within SLA) -> capacity_contention."),
        ("Comparison", "'weighted_baseline' = the standard 40/40/20 solve with NO SLA / CO2 objective. "
         "The HEADLINE CO2 saving is CO2_saved_*_common - measured only over shipments served by BOTH "
         "solves, so it is not inflated by the sustainability solve routing fewer shipments. total_CO2_* "
         "and avg_* are over each solve's OWN (different) population and are context, not the comparison."),
        ("Baseline SLA breaches (do NOT call all 'fixed')", "baseline_SLA_breaches_total = weighted-"
         "baseline routes exceeding the shipment SLA. These split into breaches_rerouted_within_SLA "
         "(genuinely re-routed onto a within-SLA lane) and breaches_unserved_due_to_SLA (no within-SLA "
         "route exists, so escalated/unassigned). In this dataset only 4 internal / 1 external are "
         "re-routed; 57 internal / 24 external are unserved - the SLA gate reveals a feasibility gap, "
         "it does not repair most breaches."),
        ("Coverage note", "The SLA is a HARD constraint, so enforcing it strictly reduces coverage "
         "(sla_infeasible shipments have no within-SLA route). This is a real speed/sustainability vs "
         "coverage trade-off, reported explicitly rather than hidden."),
        ("Scaler", "baseline_v7.canonical_scaler(side) - same fixed 40/40/20 ruler as every scenario. "
         "Route scenario = Normal; hub_disruption = None."),
    ], columns=["Assumption", "Detail"])

    with pd.ExcelWriter(out_path) as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        mode_mix.to_excel(xw, sheet_name="ModeShift", index=False)
        unassigned.to_excel(xw, sheet_name="Unassigned", index=False)
        outs["internal"]["detail"].to_excel(xw, sheet_name="Detail_Internal", index=False)
        outs["external"]["detail"].to_excel(xw, sheet_name="Detail_External", index=False)
        assumptions.to_excel(xw, sheet_name="Assumptions", index=False)
    print(f"  wrote {out_path}")
    return outs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-weeks", type=int, default=b7.HORIZON)
    ap.add_argument("--external-sla-buffer-days", type=int, default=2)
    args = ap.parse_args()
    main(horizon=args.horizon_weeks, buffer_days=args.external_sla_buffer_days)
