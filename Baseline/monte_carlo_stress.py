"""
monte_carlo_stress.py
=====================
Monte Carlo stress test of the OFFICIAL v7 INTERNAL baseline (240 shipments).

Question answered: "How robust is the Normal-scenario plan when the world
doesn't cooperate - and how much does re-optimising buy us?"

Each trial draws ONE random future ("full stress"):
  1. COST shock      : systemic per-TransportMode factor  ~ LogNormal(0, sigma_mode)
                       x idiosyncratic per-route factor   ~ LogNormal(0, sigma_cost)
  2. LEAD-TIME shock : per-route multiplier ~ LogNormal(0, sigma_lead), plus a
                       customs/port hold of U(2,10) extra days with prob p_delay
  3. HUB disruption  : each hub independently hit with prob p_hub; severity
                       ~ U(0.20, 0.60) capacity cut (mirrors the dataset's
                       recorded 25/35% CapacityReductionPct values)
  4. ROUTE outage    : each Normal route option knocked out with prob p_route

Then the SAME shocked world is evaluated twice:
  FROZEN PLAN   - the official clean Normal solution (170/240, avg 0.3150) is
                  held fixed. Assignments on knocked-out routes fail; capacities
                  are re-checked jointly per route-week and hub-week; overloads
                  are resolved by shedding the worst-scoring shipments first.
  RE-OPTIMISE   - the full PuLP model is re-solved against the shocked inputs
                  (same universe, same 40/40/20 objective).

METHODOLOGY GUARANTEES (consistent with the audited v7 pipeline):
  * ONE fixed canonical scaler (Normal+PHD+ACR, clean network) scores every
    trial - differences reflect the shocks, never a moving ruler. Shocked
    values may fall outside the fitted bounds; the ruler is extended linearly
    (scores can exceed 1), which preserves ordering.
  * SAME-POPULATION rule: frozen-vs-reoptimised score gaps are reported ONLY
    on shipments served by BOTH plans in that trial; coverage is reported
    separately.
  * Structural shocks perturb the CLEAN network (hub_disruption=None); the
    route-side axis stays Normal. This is a stochastic layer ON TOP of the
    deterministic guide scenarios, not a replacement for them.

Outputs
  monte_carlo_stress.xlsx : Summary / TrialResults / FragileShipments /
                            HubCriticality / Assumptions
  (charts are produced separately by mc_charts.py)

Run
  IFX_WORKBOOK=... python monte_carlo_stress.py --trials 300 --seed 42
"""
from pathlib import Path
import argparse, time
import numpy as np
import pandas as pd

import build_candidates as bc
import baseline_v7 as b7

KEY = "ShipmentID"

FAIL_REASONS = ["route_outage", "zero_capacity", "capacity_escalation", "capacity_contention"]


# ---------------------------------------------------------------- fixed setup
def fixed_setup(horizon):
    """Everything that does NOT change across trials."""
    sheets = bc.load_sheets(hub_disruption=None)             # clean network
    E = pd.read_excel(bc.WORKBOOK, sheet_name="External Shipments")
    cfg = b7._side_setup("internal", sheets, E)
    scaler = b7.canonical_scaler("internal", sheets, E, horizon)

    base = cfg["add_cost"](cfg["build"]("Normal"))           # Normal candidates, pre-capacity
    base["week"] = base[KEY].map(cfg["wk_map"])

    hub_rem = sheets["hub"].set_index("HubID")["remaining_capacity_units"].to_dict()
    hubs = sorted(hub_rem)
    modes = sorted(base["TransportMode"].dropna().unique())
    routes = sorted(base["RouteOptionID"].unique())

    # official clean Normal plan (the frozen plan)
    ann0 = b7.capacity_fields(base, cfg["qty_map"], KEY, horizon)
    feas0 = b7.score(b7.model_feasible(ann0), scaler)
    res0 = b7.solve(feas0, KEY, cfg["universe"], hub_rem)
    plan = res0["chosen"][[KEY, "RouteOptionID"]].copy()
    print(f"Frozen plan = clean Normal solve: {len(plan)}/{len(cfg['universe'])} "
          f"avg {res0['chosen']['MinScore'].mean():.4f}")

    return dict(sheets=sheets, cfg=cfg, scaler=scaler, base=base, hub_rem=hub_rem,
                hubs=hubs, modes=modes, routes=routes, plan=plan,
                clean_served=len(plan), clean_avg=float(res0["chosen"]["MinScore"].mean()))


# ---------------------------------------------------------------- one trial
def draw_shocks(rng, S, a):
    mode_f = {m: rng.lognormal(0.0, a.sigma_mode) for m in S["modes"]}
    route_cost_f = {r: rng.lognormal(0.0, a.sigma_cost) for r in S["routes"]}
    route_lead_f = {r: rng.lognormal(0.0, a.sigma_lead) for r in S["routes"]}
    route_delay = {r: (rng.uniform(2.0, 10.0) if rng.random() < a.p_delay else 0.0)
                   for r in S["routes"]}
    route_out = {r for r in S["routes"] if rng.random() < a.p_route}
    hub_f = {}
    for h in S["hubs"]:
        hub_f[h] = 1.0 - rng.uniform(0.20, 0.60) if rng.random() < a.p_hub else 1.0
    return mode_f, route_cost_f, route_lead_f, route_delay, route_out, hub_f


def shocked_world(S, shocks, horizon):
    """Apply one draw to the clean Normal candidate table -> scored trial table."""
    mode_f, rc_f, rl_f, r_delay, r_out, hub_f = shocks
    c = S["base"].copy()
    rid = c["RouteOptionID"]
    c["BaseCostEUR"] = (c["BaseCostEUR"]
                        * rid.map(rc_f).astype(float)
                        * c["TransportMode"].map(mode_f).astype(float))
    c["CostForScore"] = c["BaseCostEUR"]                     # internal cost term = raw EUR
    c["BaseLeadTimeDays"] = (c["BaseLeadTimeDays"] * rid.map(rl_f).astype(float)
                             + rid.map(r_delay).astype(float))
    c["orig_remaining_capacity_units"] = (
        c["orig_remaining_capacity_units"] * c["orig_HubID"].map(hub_f).astype(float))
    c["dest_remaining_capacity_units"] = (
        c["dest_remaining_capacity_units"] * c["dest_HubID"].map(hub_f).astype(float))
    c["RouteOut"] = rid.isin(r_out)

    ann = b7.capacity_fields(c, S["cfg"]["qty_map"], KEY, horizon)
    ann = b7.score(ann, S["scaler"])                         # FIXED canonical scaler
    hub_rem_t = {h: S["hub_rem"][h] * hub_f[h] for h in S["hubs"]}
    return ann, hub_rem_t


def eval_frozen(S, ann, hub_rem_t):
    """Hold the clean plan fixed in the shocked world; classify failures."""
    pk = list(zip(S["plan"][KEY], S["plan"]["RouteOptionID"]))
    ann_k = ann.set_index([KEY, "RouteOptionID"])
    rows, fail = [], {}
    for sid, rid in pk:
        r = ann_k.loc[(sid, rid)]
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        if r["RouteOut"]:
            fail[sid] = "route_outage"
        elif r["BottleneckCapacityPerWeek"] <= 0:
            fail[sid] = "zero_capacity"
        elif r["Escalation"]:
            fail[sid] = "capacity_escalation"
        else:
            rows.append(r.to_frame().T.assign(**{KEY: sid, "RouteOptionID": rid}))
    if rows:
        live = pd.concat(rows, ignore_index=True)
        for col in ["WeeklyFootprint", "CapacityUnitsPerWeek", "MinScore"]:
            live[col] = live[col].astype(float)
    else:
        live = pd.DataFrame(columns=ann.reset_index().columns)

    # joint capacity re-check: shed worst-scoring shipments from overloaded resources
    def shed(live, group_cols, cap_of):
        dropped = []
        for gkey, grp in live.groupby(group_cols):
            cap = cap_of(gkey, grp)
            load = grp["WeeklyFootprint"].sum()
            if load <= cap + 1e-9:
                continue
            for i in grp.sort_values("MinScore", ascending=False).index:
                if load <= cap + 1e-9:
                    break
                load -= live.loc[i, "WeeklyFootprint"]
                dropped.append(i)
        return dropped

    d1 = shed(live, ["RouteOptionID", "week"],
              lambda gk, g: float(g["CapacityUnitsPerWeek"].iloc[0]))
    for i in d1:
        fail[live.loc[i, KEY]] = "capacity_contention"
    live = live.drop(index=d1)

    # hub-week loads (self-loops charged once - set semantics, as in the model)
    hub_load = {}
    for i, r in live.iterrows():
        for hub in {r["FromHub"], r["ToHub"]}:
            hub_load.setdefault((hub, r["week"]), []).append(i)
    over = True
    while over:
        over = False
        for (hub, wk), idxs in hub_load.items():
            idxs = [i for i in idxs if i in live.index]
            load = live.loc[idxs, "WeeklyFootprint"].sum()
            cap = hub_rem_t.get(hub, float("inf"))
            if load > cap + 1e-9:
                worst = live.loc[idxs].sort_values("MinScore", ascending=False).index[0]
                fail[live.loc[worst, KEY]] = "capacity_contention"
                live = live.drop(index=worst)
                over = True
    return live, fail


def run_trial(S, rng, a):
    shocks = draw_shocks(rng, S, a)
    ann, hub_rem_t = shocked_world(S, shocks, a.horizon)

    live, fail = eval_frozen(S, ann, hub_rem_t)
    frozen_ids = set(live[KEY])

    feas = ann[(ann["BottleneckCapacityPerWeek"] > 0) & (~ann["Escalation"]) & (~ann["RouteOut"])]
    res = b7.solve(feas.copy(), KEY, S["cfg"]["universe"], hub_rem_t)
    re_ids = set(res["chosen"][KEY])

    common = frozen_ids & re_ids
    f_common = live[live[KEY].isin(common)]["MinScore"].astype(float)
    r_common = res["chosen"][res["chosen"][KEY].isin(common)]["MinScore"].astype(float)

    hub_f = shocks[5]
    out = {
        "frozen_served": len(frozen_ids),
        "frozen_avg": float(live["MinScore"].astype(float).mean()) if len(live) else np.nan,
        "reopt_served": len(re_ids),
        "reopt_avg": float(res["chosen"]["MinScore"].mean()) if len(re_ids) else np.nan,
        "coverage_recovered": len(re_ids) - len(frozen_ids),
        "n_common": len(common),
        "frozen_avg_common": float(f_common.mean()) if len(common) else np.nan,
        "reopt_avg_common": float(r_common.mean()) if len(common) else np.nan,
        "score_gap_common": float(f_common.mean() - r_common.mean()) if len(common) else np.nan,
        "n_hubs_disrupted": sum(1 for v in hub_f.values() if v < 1.0),
        "n_routes_out": len(shocks[4]),
        "solver": res["status"],
    }
    for reason in FAIL_REASONS:
        out[f"frozen_fail_{reason}"] = sum(1 for v in fail.values() if v == reason)
    return out, fail, {h for h, v in hub_f.items() if v < 1.0}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--horizon", type=int, default=b7.HORIZON)
    ap.add_argument("--sigma-mode", type=float, default=0.08)
    ap.add_argument("--sigma-cost", type=float, default=0.10)
    ap.add_argument("--sigma-lead", type=float, default=0.15)
    ap.add_argument("--p-delay", type=float, default=0.05)
    ap.add_argument("--p-hub", type=float, default=0.06)
    ap.add_argument("--p-route", type=float, default=0.03)
    ap.add_argument("--out", default="monte_carlo_stress.xlsx")
    a = ap.parse_args()

    t0 = time.time()
    S = fixed_setup(a.horizon)
    rng = np.random.default_rng(a.seed)

    trials, fail_count, fail_reason, hub_hits = [], {}, {}, []
    for t in range(a.trials):
        out, fail, hubs_hit = run_trial(S, rng, a)
        out["trial"] = t
        trials.append(out)
        hub_hits.append((hubs_hit, out["frozen_served"], out["coverage_recovered"]))
        for sid, reason in fail.items():
            fail_count[sid] = fail_count.get(sid, 0) + 1
            fail_reason.setdefault(sid, {}).setdefault(reason, 0)
            fail_reason[sid][reason] += 1
        if (t + 1) % 25 == 0:
            el = time.time() - t0
            print(f"  trial {t+1}/{a.trials}  ({el:.0f}s, {el/(t+1):.2f}s/trial)")

    T = pd.DataFrame(trials).set_index("trial")

    # ---------- summary
    def pct(s, q): return float(np.nanpercentile(s, q))
    metrics = ["frozen_served", "reopt_served", "coverage_recovered",
               "frozen_avg", "reopt_avg", "score_gap_common",
               "n_hubs_disrupted", "n_routes_out"]
    summary = []
    for m in metrics:
        s = T[m].astype(float)
        summary.append({"metric": m, "mean": s.mean(), "std": s.std(),
                        "P5": pct(s, 5), "P50": pct(s, 50), "P95": pct(s, 95),
                        "min": s.min(), "max": s.max()})
    summary = pd.DataFrame(summary).round(4)

    worst_tail = T.nsmallest(max(1, a.trials // 20), "frozen_served")   # worst 5 %
    headline = pd.DataFrame([
        ("clean Normal plan (no shocks)", f"{S['clean_served']}/240 served, avg {S['clean_avg']:.4f}"),
        ("frozen plan under stress (median)", f"{pct(T['frozen_served'],50):.0f}/240 served"),
        ("frozen plan under stress (P5 tail)", f"{pct(T['frozen_served'],5):.0f}/240 served"),
        ("CVaR: mean served in worst 5% of futures", f"{worst_tail['frozen_served'].mean():.1f}/240"),
        ("re-optimising recovers (mean)", f"{T['coverage_recovered'].mean():.1f} shipments/trial"),
        ("re-optimising recovers (max)", f"{T['coverage_recovered'].max():.0f} shipments in one trial"),
        ("same-population score gap (mean)",
         f"{T['score_gap_common'].mean():.4f} (frozen minus re-opt, >0 = re-opt better)"),
        ("trials with >=1 frozen failure", f"{int((T['frozen_served'] < S['clean_served']).sum())}/{a.trials}"),
    ], columns=["Headline", "Value"])

    # ---------- fragile shipments
    plan_routes = S["plan"].set_index(KEY)["RouteOptionID"].to_dict()
    frag = [{KEY: sid, "BaselineRoute": plan_routes.get(sid),
             "fail_trials": n, "fail_rate": n / a.trials,
             "dominant_reason": max(fail_reason[sid], key=fail_reason[sid].get),
             **{f"n_{r}": fail_reason[sid].get(r, 0) for r in FAIL_REASONS}}
            for sid, n in fail_count.items()]
    frag = (pd.DataFrame(frag).sort_values("fail_rate", ascending=False)
            if frag else pd.DataFrame(columns=[KEY, "fail_rate"]))

    # ---------- hub criticality (conditional means)
    rows = []
    for h in S["hubs"]:
        hit = [(srv, rec) for hubs_, srv, rec in hub_hits if h in hubs_]
        miss = [srv for hubs_, srv, _ in hub_hits if h not in hubs_]
        if len(hit) >= 3 and miss:
            srv_hit = np.mean([x[0] for x in hit])
            rows.append({"HubID": h, "trials_disrupted": len(hit),
                         "served_when_hit": round(srv_hit, 1),
                         "served_when_not_hit": round(float(np.mean(miss)), 1),
                         "coverage_impact": round(float(np.mean(miss)) - srv_hit, 1),
                         "recovered_when_hit": round(float(np.mean([x[1] for x in hit])), 1)})
    crit = (pd.DataFrame(rows).sort_values("coverage_impact", ascending=False)
            if rows else pd.DataFrame())

    assumptions = pd.DataFrame([
        ("What this is", "Stochastic stress layer on the official v7 internal baseline (clean network, "
         "route scenario = Normal). Complements - does not replace - the 5 deterministic guide scenarios."),
        ("Trials / seed", f"{a.trials} trials, numpy default_rng(seed={a.seed}) - fully reproducible."),
        ("Cost shock", f"BaseCostEUR x LogNormal(0,{a.sigma_mode}) per TransportMode (systemic) "
         f"x LogNormal(0,{a.sigma_cost}) per route (idiosyncratic). Median multiplier = 1."),
        ("Lead-time shock", f"BaseLeadTimeDays x LogNormal(0,{a.sigma_lead}) per route + U(2,10) extra "
         f"days with prob {a.p_delay} (customs/port hold)."),
        ("Hub disruption", f"Each hub independently disrupted with prob {a.p_hub}; severity U(20%,60%) "
         "capacity cut - bracketing the dataset's recorded 25%/35% CapacityReductionPct values. "
         "Applied to remaining capacity (constraint RHS and bottleneck)."),
        ("Route outage", f"Each Normal route option unavailable with prob {a.p_route}."),
        ("Scoring", "FIXED canonical 40/40/20 scaler (Normal+PHD+ACR union, clean) for every trial; "
         "shocked values outside fitted bounds extend the ruler linearly (scores may exceed 1)."),
        ("Frozen plan", "The clean Normal solution held fixed. Failure classes: route_outage, "
         "zero_capacity, capacity_escalation (>horizon weeks), capacity_contention (joint route-week / "
         "hub-week overload; overloads shed worst-MinScore shipments first - stated tie-break rule)."),
        ("Re-optimise", "Full PuLP/CBC re-solve on the shocked inputs, same universe and objective."),
        ("Same-population rule", "score_gap_common compares frozen vs re-optimised ONLY on shipments "
         "served by both in that trial; coverage reported separately. Never compare averages across "
         "different served sets."),
        ("Multi-week approximation", "WeeklyFootprint charged in the ship week (inherited from the "
         "base model; documented next step is a full weekly-flow formulation)."),
    ], columns=["Assumption", "Detail"])

    out = Path(a.out)
    with pd.ExcelWriter(out) as xw:
        headline.to_excel(xw, sheet_name="Summary", index=False, startrow=0)
        summary.to_excel(xw, sheet_name="Summary", index=False, startrow=len(headline) + 3)
        T.reset_index().round(4).to_excel(xw, sheet_name="TrialResults", index=False)
        frag.to_excel(xw, sheet_name="FragileShipments", index=False)
        crit.to_excel(xw, sheet_name="HubCriticality", index=False)
        assumptions.to_excel(xw, sheet_name="Assumptions", index=False)
    T.reset_index().to_csv(out.with_suffix(".csv"), index=False)
    print(f"\nwrote {out} (+ {out.with_suffix('.csv').name}) in {time.time()-t0:.0f}s")
    print(headline.to_string(index=False))


if __name__ == "__main__":
    main()
